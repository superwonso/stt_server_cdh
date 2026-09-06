"""Synthetic temporary DB and injected auth only; no production service or AI."""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from server import lecture_library
from server.db import Database
from server.security import RateLimiter, digest
from server.settings import Settings


def revision(segments):
    canonical = [{key: row[key] for key in ("id", "start", "end", "text")} for row in segments]
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class LectureLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-library-test-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.settings = Settings(data_dir=root / "data", model_cache_dir=root / "models")
        self.database = Database(self.settings.database_path, self.settings.accounts)
        self.database.initialize()
        self.tokens = {name: "synthetic-library-" + name for name in self.settings.accounts}
        with self.database.connect() as connection:
            for owner, token in self.tokens.items():
                connection.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                                   (digest(token), owner, time.time() + 3600, time.time()))
        self.app, self.limiter, self.ownership_hook = FastAPI(), RateLimiter(), None
        self.raw_calls = []

        def identity(authorization: str | None = Header(default=None)):
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(401, "로그인이 필요합니다.")
            with self.database.connect() as connection:
                row = connection.execute("SELECT username FROM sessions WHERE token_hash=? AND expires_at>?",
                                         (digest(authorization[7:]), time.time())).fetchone()
                if row is None:
                    raise HTTPException(401, "로그인이 필요합니다.")
                if not connection.execute("SELECT access_enabled FROM operational_state").fetchone()[0]:
                    raise HTTPException(503, "운영 접속이 중지되었습니다.")
                return dict(row)

        def owned(lecture_id, username):
            with self.database.connect() as connection:
                row = connection.execute("SELECT * FROM lectures WHERE id=? AND username=? AND deleting=0",
                                         (lecture_id, username)).fetchone()
            if row is None:
                raise HTTPException(404, "수업을 찾을 수 없습니다.")
            if self.ownership_hook:
                self.ownership_hook(lecture_id)
            return dict(row)

        @self.app.exception_handler(RequestValidationError)
        async def invalid(request, error):
            return JSONResponse({"detail": "입력 형식을 확인하세요."}, status_code=422)

        lecture_library.install(self.app, self.settings, self.database, identity=identity, owned_lecture=owned,
                                limiter=self.limiter, raw_segments=self.raw, transcript_revision=revision)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def headers(self, owner="user-alpha"):
        return {"Authorization": "Bearer " + self.tokens[owner]}

    def lecture(self, *, owner="user-alpha", title="합성 수업", finalized=True, deleting=False,
                created="2026-09-06T01:00:00Z", texts=()):
        identifier, chunk = str(uuid.uuid4()), str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lectures(id,username,title,created_at,recording_finalized,deleting) "
                               "VALUES (?,?,?,?,?,?)", (identifier, owner, title, created, int(finalized), int(deleting)))
            if texts:
                connection.execute("INSERT INTO chunks(lecture_id,chunk_id,payload_hash,start_seconds,status) "
                                   "VALUES (?,?,'synthetic-hash',0,'done')", (identifier, chunk))
                for index, text in enumerate(texts):
                    connection.execute("INSERT INTO segments VALUES (?,?,?,?,?,?)",
                                       (str(uuid.uuid4()), identifier, chunk, index * 2, index * 2 + 1, text))
        return identifier

    def raw(self, connection, lecture_id):
        self.raw_calls.append(lecture_id)
        return [dict(row) for row in connection.execute("SELECT id,start,end,text FROM segments WHERE lecture_id=? "
                                                       "ORDER BY start,end,id", (lecture_id,))]

    def corrected(self, lecture_id, texts):
        with self.database.connect() as connection:
            raw = self.raw(connection, lecture_id)
            document = [{**row, "text": text} for row, text in zip(raw, texts, strict=True)]
            connection.execute("INSERT INTO transcript_corrections(lecture_id,raw_revision,status,model,"
                               "corrected_text,corrected_segments,uncertain_terms,created_at,updated_at,completed_at) "
                               "VALUES (?,?,'completed','synthetic-model',?,?,'[]','now','now','now')",
                               (lecture_id, revision(raw), "\n".join(texts), json.dumps(document, ensure_ascii=False)))

    def get(self, identifier, owner="user-alpha"):
        return self.client.get(f"/lectures/{identifier}/metadata", headers=self.headers(owner))

    def patch(self, identifier, payload, owner="user-alpha"):
        return self.client.patch(f"/lectures/{identifier}/metadata", json=payload, headers=self.headers(owner))

    def search(self, owner="user-alpha", **params):
        return self.client.get("/library/search", params=params, headers=self.headers(owner))

    def snapshot(self):
        with self.database.connect() as connection:
            return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in ("users", "sessions", "lectures", "chunks", "segments", "transcript_corrections")}

    def test_default_metadata_is_read_only_and_does_not_create_rows(self):
        identifier = self.lecture(title="원래 생성 제목")
        before = self.snapshot()
        response = self.get(identifier)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"lecture_id": identifier, "display_title": "원래 생성 제목",
                                          "course": "", "semester": "", "revision": 0})
        self.assertEqual(before, self.snapshot())
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_metadata").fetchone()[0], 0)

    def test_metadata_changes_preserve_original_title_audio_transcripts_and_create_replay_fields(self):
        identifier = self.lecture(title="원래 생성 제목", texts=("원래 받아쓴 내용",))
        self.corrected(identifier, ("다듬은 내용",))
        before = self.snapshot()
        response = self.patch(identifier, {"revision": 0, "display_title": "  표시 이름  ", "course": "물리학", "semester": "2026-2"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"lecture_id": identifier, "display_title": "표시 이름", "course": "물리학",
                                          "semester": "2026-2", "revision": 1})
        self.assertEqual(before, self.snapshot())
        with self.database.connect() as connection:
            lecture = connection.execute("SELECT * FROM lectures WHERE id=?", (identifier,)).fetchone()
            self.assertEqual(lecture_library.metadata_for(connection, lecture), {
                "display_title": "표시 이름", "course": "물리학", "semester": "2026-2", "metadata_revision": 1})

    def test_partial_patch_clear_classification_and_restore_original_display_title(self):
        identifier = self.lecture(title="원제목")
        self.patch(identifier, {"revision": 0, "display_title": "새 이름", "course": "과목", "semester": "학기"})
        response = self.patch(identifier, {"revision": 1, "course": " ", "display_title": "원제목"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revision"], 2)
        self.assertEqual(response.json()["semester"], "학기")
        self.assertEqual(response.json()["course"], "")
        with self.database.connect() as connection:
            self.assertIsNone(connection.execute("SELECT display_title FROM lecture_metadata").fetchone()[0])

    def test_stale_revision_conflicts_and_cannot_overwrite_a_newer_edit(self):
        identifier = self.lecture()
        first = self.patch(identifier, {"revision": 0, "course": "first"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.patch(identifier, {"revision": 0, "course": "stale"}).status_code, 409)
        self.assertEqual(self.get(identifier).json()["course"], "first")

    def test_concurrent_revision_zero_writes_have_one_winner(self):
        identifier = self.lecture()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda value: self.patch(identifier, {"revision": 0, "course": value}), ("one", "two")))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        self.assertEqual(self.get(identifier).json()["revision"], 1)

    def test_live_unfinished_lecture_can_be_read_but_not_renamed(self):
        identifier = self.lecture(finalized=False)
        self.assertEqual(self.get(identifier).status_code, 200)
        self.assertEqual(self.patch(identifier, {"revision": 0, "course": "과목"}).status_code, 409)

    def test_metadata_validates_lengths_control_characters_types_and_unknown_fields(self):
        identifier = self.lecture()
        for value in ({"revision": True, "course": "x"}, {"revision": -1, "course": "x"},
                      {"revision": "0", "course": "x"}, {"revision": 0}, {"revision": 0, "title": "x"},
                      {"revision": 0, "display_title": " "}, {"revision": 0, "display_title": "x" * 121},
                      {"revision": 0, "course": "x" * 81}, {"revision": 0, "semester": "x" * 41},
                      {"revision": 0, "course": None}, {"revision": 0, "course": 42},
                      {"revision": 0, "display_title": "x\x00y"}, {"revision": 0, "semester": "x\ny"}):
            with self.subTest(payload=value):
                self.assertEqual(self.patch(identifier, value).status_code, 422)
        self.assertEqual(self.get(identifier).json()["revision"], 0)

    def test_missing_expired_and_revoked_sessions_are_rejected_on_all_routes(self):
        identifier = self.lecture()
        for path, method in ((f"/lectures/{identifier}/metadata", "get"), (f"/lectures/{identifier}/metadata", "patch"),
                             ("/library/options", "get"), ("/library/search?q=x", "get")):
            kwargs = {"json": {"revision": 0, "course": "x"}} if method == "patch" else {}
            self.assertEqual(getattr(self.client, method)(path, **kwargs).status_code, 401)
        with self.database.connect() as connection:
            connection.execute("UPDATE sessions SET expires_at=0 WHERE username='user-alpha'")
        self.assertEqual(self.search(q="x").status_code, 401)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE username='user-beta'")
        self.assertEqual(self.get(identifier, "user-beta").status_code, 401)

    def test_cross_owner_missing_and_deleting_metadata_all_return_same_404(self):
        own = self.lecture()
        deleting = self.lecture(deleting=True)
        responses = []
        for identifier, owner in ((own, "user-beta"), (deleting, "user-alpha"), (str(uuid.uuid4()), "user-alpha")):
            responses += [self.get(identifier, owner), self.patch(identifier, {"revision": 0, "course": "x"}, owner)]
        self.assertEqual({response.status_code for response in responses}, {404})
        self.assertEqual(len({response.text for response in responses}), 1)

    def test_metadata_owner_and_deletion_are_rechecked_inside_transaction(self):
        for column, value in (("username", "user-beta"), ("deleting", 1)):
            for operation in ("get", "patch"):
                with self.subTest(change=column, operation=operation):
                    identifier = self.lecture()
                    def change(identifier):
                        with self.database.connect() as connection:
                            connection.execute(f"UPDATE lectures SET {column}=? WHERE id=?", (value, identifier))
                    self.ownership_hook = change
                    response = self.get(identifier) if operation == "get" else self.patch(identifier, {"revision": 0, "course": "x"})
                    self.ownership_hook = None
                    self.assertEqual(response.status_code, 404)

    def test_options_are_owner_only_distinct_sorted_and_exclude_deleting_rows(self):
        for owner, course, semester in (("user-alpha", "물리", "2026-2"), ("user-alpha", "국어", "2026-1"),
                                         ("user-alpha", "물리", "2026-2"), ("user-beta", "other-only", "other-only")):
            identifier = self.lecture(owner=owner)
            self.patch(identifier, {"revision": 0, "course": course, "semester": semester}, owner)
        removed = self.lecture()
        self.patch(removed, {"revision": 0, "course": "removed-only"})
        with self.database.connect() as connection:
            connection.execute("UPDATE lectures SET deleting=1 WHERE id=?", (removed,))
        response = self.client.get("/library/options", headers=self.headers())
        self.assertEqual(response.json(), {"courses": ["국어", "물리"], "semesters": ["2026-1", "2026-2"]})

    def test_empty_search_browses_filtered_titles_without_loading_transcripts(self):
        wanted = self.lecture(title="이름")
        self.patch(wanted, {"revision": 0, "course": "과목%", "semester": "2026-2"})
        self.lecture(title="다른 것")
        with mock.patch("server.lecture_library._corrected_segments", side_effect=AssertionError("no transcript load")):
            value = self.search(course="과목%", semester="2026-2").json()
        self.assertEqual([row["lecture_id"] for row in value["items"]], [wanted])
        self.assertEqual(value["items"][0]["source"], "title")
        self.assertIsNone(value["items"][0]["segment_id"])
        self.assertEqual(self.search(course="과목").json()["items"], [])

    def test_search_uses_display_title_and_does_not_publish_owner_or_creation_title(self):
        identifier = self.lecture(title="original-hidden")
        self.patch(identifier, {"revision": 0, "display_title": "replacement"})
        self.assertEqual(self.search(q="original-hidden").json()["items"], [])
        response = self.search(q="replacement")
        self.assertEqual(response.json()["items"][0]["display_title"], "replacement")
        self.assertNotIn("user-alpha", response.text)
        self.assertNotIn("original-hidden", response.text)

    def test_raw_corrected_and_all_search_keep_original_ids_and_times(self):
        identifier = self.lecture(texts=("세포 분열 RAW", "다른 내용"))
        self.corrected(identifier, ("세포 분열 CORRECTED", "다듬은 내용"))
        raw = self.search(q="세포", source="raw").json()["items"]
        corrected = self.search(q="세포", source="corrected").json()["items"]
        both = self.search(q="세포", source="all").json()["items"]
        self.assertEqual([row["source"] for row in raw], ["raw"])
        self.assertEqual([row["source"] for row in corrected], ["corrected"])
        self.assertEqual([row["source"] for row in both], ["raw", "corrected"])
        self.assertEqual([(row["segment_id"], row["start"], row["end"]) for row in raw],
                         [(row["segment_id"], row["start"], row["end"]) for row in corrected])
        self.assertEqual([row["lecture_id"] for row in both], [identifier, identifier])

    def test_search_never_matches_another_owner_or_deleted_lecture_or_pending_chunk(self):
        self.lecture(owner="user-beta", title="needle other", texts=("needle",))
        self.lecture(title="needle removed", texts=("needle",), deleting=True)
        pending = self.lecture(texts=("needle",))
        with self.database.connect() as connection:
            connection.execute("UPDATE chunks SET status='pending' WHERE lecture_id=?", (pending,))
        value = self.search(q="needle").json()
        self.assertEqual(value["items"], [])

    def test_percent_underscore_and_backslash_are_literal_not_sql_wildcards(self):
        self.lecture(texts=("평균 25% 값", "unit_name", "path\\file", "unitXname", "평균 25 값", "plain"))
        for query, expected in (("%", "25%"), ("_", "unit_name"), ("\\", "path\\file")):
            rows = self.search(q=query, source="raw").json()["items"]
            self.assertEqual(len(rows), 1)
            self.assertIn(expected, rows[0]["snippet"])
        self.assertEqual(self.search(q="' OR 1=1 --", source="raw").json()["items"], [])

    def test_ascii_case_insensitive_and_korean_search_have_bounded_centered_snippets(self):
        self.lecture(texts=("앞" * 500 + " MiXeD 한국어 " + "뒤" * 500,))
        for query in ("mixed", "한국어"):
            row = self.search(q=query, source="raw").json()["items"][0]
            self.assertLessEqual(len(row["snippet"]), 240)
            self.assertIn(query, row["snippet"].lower())

    def test_stable_pagination_has_no_missing_or_duplicated_hits(self):
        first = self.lecture(created="2026-09-05", texts=("needle one", "needle two"))
        second = self.lecture(created="2026-09-06", texts=("needle three", "needle four"))
        pages = [self.search(q="needle", source="raw", offset=offset, limit=1).json() for offset in range(4)]
        self.assertEqual([page["items"][0]["lecture_id"] for page in pages], [second, second, first, first])
        self.assertEqual([page["items"][0]["start"] for page in pages], [0, 2, 0, 2])
        self.assertEqual([page["has_more"] for page in pages], [True, True, True, False])
        self.assertEqual(len({page["items"][0]["segment_id"] for page in pages}), 4)

    def test_malformed_stale_and_retimed_corrections_are_excluded_without_mutation(self):
        for problem in ("json", "stale", "id", "time", "booltime", "order"):
            with self.subTest(problem=problem):
                identifier = self.lecture(texts=("raw-only one", "raw-only two"))
                self.corrected(identifier, ("corrected-only one", "corrected-only two"))
                with self.database.connect() as connection:
                    row = connection.execute("SELECT corrected_segments FROM transcript_corrections WHERE lecture_id=?", (identifier,)).fetchone()
                    document = json.loads(row[0])
                    if problem == "json":
                        payload = "not-json"
                    else:
                        if problem == "id": document[0]["id"] = "wrong-id"
                        if problem == "time": document[0]["end"] = 99
                        if problem == "booltime": document[0]["start"] = False
                        if problem == "order": document.reverse()
                        payload = json.dumps(document)
                    connection.execute("UPDATE transcript_corrections SET corrected_segments=? WHERE lecture_id=?", (payload, identifier))
                    if problem == "stale":
                        connection.execute("UPDATE segments SET text='new raw-only' WHERE lecture_id=?", (identifier,))
                before = self.snapshot()
                result = self.search(q="corrected-only", source="corrected").json()
                self.assertEqual(result["items"], [])
                self.assertTrue(result["partial_corrected"])
                self.assertEqual(before, self.snapshot())

    def test_oversized_correction_is_not_loaded_and_does_not_block_raw_search(self):
        identifier = self.lecture(texts=("needle",))
        self.corrected(identifier, ("corrected needle",))
        calls_before = list(self.raw_calls)
        with mock.patch("server.lecture_library.MAX_CORRECTION_JSON", 8):
            result = self.search(q="needle").json()
        self.assertEqual(self.raw_calls, calls_before)
        self.assertEqual([item["source"] for item in result["items"]], ["raw"])
        self.assertTrue(result["partial_corrected"])

    def test_unmatched_corrected_lectures_do_not_load_entire_transcripts(self):
        for index in range(20):
            identifier = self.lecture(texts=(f"raw {index}",))
            self.corrected(identifier, (f"corrected {index}",))
        calls_before = list(self.raw_calls)
        result = self.search(q="not-in-this-library").json()
        self.assertEqual(result["items"], [])
        self.assertEqual(self.raw_calls, calls_before)

    def test_trashed_lectures_are_hidden_from_search_options_and_metadata(self):
        identifier = self.lecture(title="trash-only", texts=("trash-only",))
        self.corrected(identifier, ("trash-only corrected",))
        self.patch(identifier, {"revision": 0, "course": "trash-only", "semester": "trash-only"})
        with self.database.connect() as connection:
            connection.execute("UPDATE lectures SET trashed_at='2026-09-06T00:00:00Z' WHERE id=?", (identifier,))
        self.assertEqual(self.search(q="trash-only").json()["items"], [])
        self.assertEqual(self.client.get("/library/options", headers=self.headers()).json(), {"courses": [], "semesters": []})
        self.assertEqual(self.get(identifier).status_code, 404)
        self.assertEqual(self.patch(identifier, {"revision": 1, "course": "changed"}).status_code, 404)

    def test_actual_app_cors_allows_patch_only_from_configured_frontend_origin(self):
        from server.app import create_app
        engine = mock.Mock()
        engine.status.return_value = {"model_state": "ready", "model": "synthetic", "device": "cpu"}
        app = create_app(replace(self.settings, site_origins=("https://student.github.io",)), engine)
        client = TestClient(app)
        self.addCleanup(client.close)
        path = f"/lectures/{uuid.uuid4()}/metadata"
        headers = {"Origin": "https://student.github.io", "Access-Control-Request-Method": "PATCH",
                   "Access-Control-Request-Headers": "authorization,content-type"}
        response = client.options(path, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("PATCH", response.headers["access-control-allow-methods"])
        self.assertEqual(response.headers["access-control-allow-origin"], "https://student.github.io")
        refused = client.options(path, headers={**headers, "Origin": "https://not-allowed.example"})
        self.assertEqual(refused.status_code, 403)
        self.assertNotIn("access-control-allow-origin", refused.headers)

    def test_actual_app_create_retry_after_display_rename_keeps_original_idempotency(self):
        from server.app import create_app
        engine = mock.Mock()
        engine.status.return_value = {"model_state": "ready", "model": "synthetic", "device": "cpu"}
        app = create_app(self.settings, engine)
        client = TestClient(app)
        self.addCleanup(client.close)
        identifier = str(uuid.uuid4())
        headers = {**self.headers(), "X-Lecture-Id": identifier}
        body = {"title": "original creation", "language": "ko", "asr_provider": "qwen"}
        self.assertEqual(client.post("/lectures", headers=headers, json=body).status_code, 201)
        with self.database.connect() as connection:
            connection.execute("UPDATE lectures SET recording_finalized=1 WHERE id=?", (identifier,))
        changed = client.patch(f"/lectures/{identifier}/metadata", headers=self.headers(),
                               json={"revision": 0, "display_title": "renamed display"})
        self.assertEqual(changed.status_code, 200, changed.text)
        replay = client.post("/lectures", headers=headers, json=body)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json()["title"], "original creation")
        self.assertEqual(replay.json()["display_title"], "renamed display")
        self.assertEqual(client.get(f"/lectures/{identifier}", headers=self.headers()).json()["display_title"], "renamed display")

    def test_query_bounds_invalid_modes_and_controls_fail_without_echoing_input(self):
        for params in ({"q": "x" * 121}, {"course": "x" * 81}, {"semester": "x" * 41},
                       {"limit": 0}, {"limit": 51}, {"offset": -1}, {"offset": 10001},
                       {"source": "arbitrary"}, {"q": "synthetic\x00value"}):
            response = self.search(**params)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("synthetic\u0000value", response.text)

    def test_expired_search_budget_fails_clearly_and_releases_capacity(self):
        self.lecture(texts=("needle",))
        with mock.patch("server.lecture_library.SEARCH_SECONDS", -1):
            response = self.search(q="needle")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("items", response.json())
        self.assertEqual(self.search(q="needle").status_code, 200)

    def test_rate_limit_and_operational_pause_do_not_mutate_metadata(self):
        identifier = self.lecture()
        with mock.patch.object(self.limiter, "allow", return_value=False):
            self.assertEqual(self.patch(identifier, {"revision": 0, "course": "x"}).status_code, 429)
            self.assertEqual(self.search(q="needle").status_code, 429)
        with self.database.connect() as connection:
            connection.execute("UPDATE operational_state SET access_enabled=0")
        self.assertEqual(self.get(identifier).status_code, 503)
        self.assertEqual(self.search(q="needle").status_code, 503)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_metadata").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
