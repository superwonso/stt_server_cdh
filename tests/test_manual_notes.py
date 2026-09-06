"""Real HTTP routes with private synthetic SQLite fixtures and no provider calls."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import manual_notes
from server.app import create_app
from server.security import digest
from server.settings import Settings


class FakeTranscriber:
    def status(self):
        return {"model_state": "ready", "model": "synthetic", "device": "cpu"}


class ManualNotesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-manual-test-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.settings = Settings(data_dir=root / "data", model_cache_dir=root / "models")
        self.app = create_app(self.settings, FakeTranscriber())
        self.database = self.app.state.database
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.tokens = {name: "synthetic-manual-" + name for name in self.settings.accounts}
        with self.database.connect() as connection:
            for owner, token in self.tokens.items():
                connection.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                                   (digest(token), owner, time.time() + 3600, time.time()))

    def headers(self, owner="user-alpha"):
        return {"Authorization": "Bearer " + self.tokens[owner]}

    def lecture(self, *, owner="user-alpha", finalized=True, trashed=False, deleting=False, texts=("첫 원문", "다음 원문")):
        lecture, chunk = str(uuid.uuid4()), str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lectures(id,username,title,created_at,recording_finalized,trashed_at,deleting) "
                               "VALUES (?,?,'synthetic title','now',?,?,?)",
                               (lecture, owner, int(finalized), "trashed" if trashed else None, int(deleting)))
            connection.execute("INSERT INTO chunks(lecture_id,chunk_id,payload_hash,start_seconds,status) VALUES (?,?,'hash',0,'done')", (lecture, chunk))
            ids = []
            for index, text in enumerate(texts):
                identifier = str(uuid.uuid4())
                ids.append(identifier)
                connection.execute("INSERT INTO segments VALUES (?,?,?,?,?,?)", (identifier, lecture, chunk, index * 2, index * 2 + 1, text))
        return lecture, ids

    def get(self, lecture, owner="user-alpha"):
        return self.client.get(f"/lectures/{lecture}/manual", headers=self.headers(owner))

    def body(self, lecture, *, action="note_upsert", owner="user-alpha", **changes):
        state = self.get(lecture, owner).json()
        return {"id": str(uuid.uuid4()), "revision": state["revision"], "raw_revision": state["raw_revision"],
                "action": action, **changes}

    def post(self, lecture, body, owner="user-alpha"):
        return self.client.post(f"/lectures/{lecture}/manual", headers=self.headers(owner), json=body)

    def history(self, lecture, owner="user-alpha", **params):
        return self.client.get(f"/lectures/{lecture}/manual/history", headers=self.headers(owner), params=params)

    def saved(self, lecture, *, action="note_upsert", **changes):
        body = self.body(lecture, action=action, **changes)
        response = self.post(lecture, body)
        self.assertEqual(response.status_code, 200, response.text)
        return body, response.json()

    def snapshot(self, *, manual=False):
        names = ("lecture_manual_state", "lecture_manual_notes", "lecture_manual_edits", "lecture_manual_history") if manual else (
            "users", "lectures", "chunks", "segments", "transcript_corrections", "lecture_summaries", "lecture_translations")
        with self.database.connect() as connection:
            return {name: [tuple(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY rowid")] for name in names}

    def seed_ai(self, lecture):
        with self.database.connect() as connection:
            source = [dict(row) for row in connection.execute("SELECT id,start,end,text FROM segments WHERE lecture_id=? ORDER BY start,end,id", (lecture,))]
            raw_revision = hashlib.sha256(json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            connection.execute("INSERT INTO transcript_corrections(lecture_id,raw_revision,status,model,corrected_text,"
                               "corrected_segments,uncertain_terms,created_at,updated_at,completed_at) "
                               "VALUES (?,?,'completed','synthetic','AI',?,'[]','now','now','now')", (lecture, raw_revision, json.dumps(source)))
            for table, column in (("lecture_summaries", "summary_json"), ("lecture_translations", "translation_json")):
                connection.execute(f"INSERT INTO {table}(lecture_id,job_id,raw_revision,status,model,{column},created_at,updated_at,completed_at) "
                                   "VALUES (?,?,?,'completed','synthetic','{}','now','now','now')", (lecture, str(uuid.uuid4()), raw_revision))

    def test_default_state_and_history_are_read_only_and_empty(self):
        lecture, _ = self.lecture()
        before = self.snapshot(manual=True)
        response = self.get(lecture)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"lecture_id", "raw_revision", "revision", "notes", "edits"})
        self.assertEqual(response.json()["revision"], 0)
        self.assertEqual(response.json()["notes"], [])
        self.assertEqual(response.json()["edits"], [])
        self.assertEqual(len(response.json()["raw_revision"]), 64)
        self.assertEqual(self.history(lecture).json()["items"], [])
        self.assertEqual(before, self.snapshot(manual=True))

    def test_time_note_and_segment_note_keep_multiline_text_and_canonical_anchor(self):
        lecture, segments = self.lecture()
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        self.saved(lecture, note_id=first, text="긴 필기\n다음 줄\t추가", start_seconds=12.25)
        self.saved(lecture, note_id=second, text="발언 메모", segment_id=segments[1])
        notes = self.get(lecture).json()["notes"]
        self.assertEqual([note["id"] for note in notes], [second, first])
        self.assertEqual(notes[0]["start_seconds"], 2)
        self.assertEqual(notes[0]["segment_id"], segments[1])
        self.assertIsNone(notes[1]["segment_id"])
        self.assertEqual(notes[1]["text"], "긴 필기\n다음 줄\t추가")

    def test_note_update_preserves_unspecified_anchor_and_appends_history(self):
        lecture, segments = self.lecture()
        note = str(uuid.uuid4())
        self.saved(lecture, note_id=note, segment_id=segments[1], text="v1")
        original = self.get(lecture).json()["notes"][0]
        self.saved(lecture, note_id=note, text="v2")
        updated = self.get(lecture).json()["notes"][0]
        self.assertEqual(updated["created_at"], original["created_at"])
        self.assertEqual(updated["segment_id"], segments[1])
        self.assertEqual(updated["start_seconds"], 2)
        self.assertEqual([item["text"] for item in self.history(lecture, note_id=note).json()["items"]], ["v2", "v1"])

    def test_direct_edits_restore_raw_with_null_without_mutating_raw_or_any_ai(self):
        lecture, segments = self.lecture()
        self.seed_ai(lecture)
        before = self.snapshot()
        self.saved(lecture, action="segment_edit", segment_id=segments[0], text="내가 정정한 문장")
        self.assertEqual(self.get(lecture).json()["edits"][0]["text"], "내가 정정한 문장")
        self.saved(lecture, action="segment_edit", segment_id=segments[0], text=None)
        self.assertEqual(self.get(lecture).json()["edits"], [])
        self.assertEqual([row["text"] for row in self.history(lecture, segment_id=segments[0]).json()["items"]], [None, "내가 정정한 문장"])
        self.assertEqual(before, self.snapshot())

    def test_restoring_an_old_edit_creates_a_new_revision_and_preserves_prior_versions(self):
        lecture, segments = self.lecture()
        for value in ("version 1", "version 2"):
            self.saved(lecture, action="segment_edit", segment_id=segments[0], text=value)
        before = self.history(lecture, segment_id=segments[0]).json()["items"]
        self.saved(lecture, action="segment_edit", segment_id=segments[0], text=before[1]["text"])
        after = self.history(lecture, segment_id=segments[0]).json()["items"]
        self.assertEqual(after[1:], before)
        self.assertEqual([item["revision"] for item in after], [3, 2, 1])
        self.assertEqual([item["text"] for item in after], ["version 1", "version 2", "version 1"])

    def test_note_delete_leaves_null_history_and_old_text_can_restore_same_note(self):
        lecture, _ = self.lecture()
        note = str(uuid.uuid4())
        self.saved(lecture, note_id=note, text="restore me", start_seconds=4)
        self.saved(lecture, action="note_delete", note_id=note)
        self.assertEqual(self.get(lecture).json()["notes"], [])
        history = self.history(lecture, note_id=note).json()["items"]
        self.assertEqual([item["text"] for item in history], [None, "restore me"])
        self.saved(lecture, note_id=note, text=history[1]["text"], start_seconds=history[1]["start_seconds"])
        self.assertEqual(self.get(lecture).json()["notes"][0]["start_seconds"], 4)
        self.assertEqual(self.history(lecture, note_id=note).json()["items"][1:], history)

    def test_same_uuid_replay_returns_exact_old_ack_after_newer_changes(self):
        lecture, segments = self.lecture()
        body, ack = self.saved(lecture, action="segment_edit", segment_id=segments[0], text="old")
        self.saved(lecture, action="segment_edit", segment_id=segments[0], text="new")
        with mock.patch("server.manual_notes._snapshot", side_effect=AssertionError("ACK must not claim current state")):
            replay = self.post(lecture, body)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), ack)
        state = self.get(lecture).json()
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["edits"][0]["text"], "new")
        self.assertEqual(len(self.history(lecture).json()["items"]), 2)

    def test_reusing_uuid_for_different_payload_conflicts_without_change(self):
        lecture, segments = self.lecture()
        body, _ = self.saved(lecture, action="segment_edit", segment_id=segments[0], text="first")
        before = self.snapshot(manual=True)
        response = self.post(lecture, {**body, "text": "different"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(before, self.snapshot(manual=True))

    def test_stale_revision_or_raw_snapshot_conflicts(self):
        lecture, segments = self.lecture()
        old = self.body(lecture, action="segment_edit", segment_id=segments[0], text="stale")
        self.saved(lecture, action="segment_edit", segment_id=segments[1], text="new")
        self.assertEqual(self.post(lecture, old).status_code, 409)
        wrong = self.body(lecture, action="segment_edit", segment_id=segments[0], text="wrong snapshot")
        wrong["raw_revision"] = "f" * 64
        self.assertEqual(self.post(lecture, wrong).status_code, 409)

    def test_concurrent_distinct_requests_have_one_cas_winner(self):
        lecture, segments = self.lecture()
        bodies = [self.body(lecture, action="segment_edit", segment_id=segment, text="changed") for segment in segments]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda body: self.post(lecture, body), bodies))
        self.assertEqual(sorted(row.status_code for row in responses), [200, 409])
        self.assertEqual(self.get(lecture).json()["revision"], 1)
        self.assertEqual(len(self.history(lecture).json()["items"]), 1)

    def test_concurrent_same_request_commits_once_with_identical_ack(self):
        lecture, segments = self.lecture()
        body = self.body(lecture, action="segment_edit", segment_id=segments[0], text="changed")
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.post(lecture, body), range(2)))
        self.assertEqual([row.status_code for row in responses], [200, 200])
        self.assertEqual(responses[0].json(), responses[1].json())
        self.assertEqual(len(self.history(lecture).json()["items"]), 1)

    def test_missing_other_owner_trashed_and_deleting_are_hidden_on_all_methods(self):
        active, segments = self.lecture()
        payload = self.body(active, action="segment_edit", segment_id=segments[0], text="change")
        trashed, _ = self.lecture(trashed=True)
        deleting, _ = self.lecture(deleting=True)
        for lecture, owner in ((active, "user-beta"), (str(uuid.uuid4()), "user-alpha"),
                               (trashed, "user-alpha"), (deleting, "user-alpha")):
            self.assertEqual(self.get(lecture, owner).status_code, 404)
            self.assertEqual(self.post(lecture, payload, owner).status_code, 404)
            self.assertEqual(self.history(lecture, owner).status_code, 404)

    def test_unfinalized_lecture_rejects_read_write_and_history(self):
        lecture, _ = self.lecture(finalized=False)
        body = {"id": str(uuid.uuid4()), "revision": 0, "raw_revision": "a" * 64,
                "action": "note_upsert", "note_id": str(uuid.uuid4()), "text": "x"}
        self.assertEqual(self.get(lecture).status_code, 409)
        self.assertEqual(self.post(lecture, body).status_code, 409)
        self.assertEqual(self.history(lecture).status_code, 409)

    def test_missing_expired_revoked_sessions_and_operational_pause_are_rejected(self):
        lecture, segments = self.lecture()
        payload = self.body(lecture, action="segment_edit", segment_id=segments[0], text="x")
        self.assertEqual(self.client.get(f"/lectures/{lecture}/manual").status_code, 401)
        self.assertEqual(self.client.post(f"/lectures/{lecture}/manual", json=payload).status_code, 401)
        self.assertEqual(self.client.get(f"/lectures/{lecture}/manual/history").status_code, 401)
        with self.database.connect() as connection:
            connection.execute("UPDATE sessions SET expires_at=0 WHERE username='user-alpha'")
        self.assertEqual(self.get(lecture).status_code, 401)
        with self.database.connect() as connection:
            connection.execute("UPDATE sessions SET expires_at=? WHERE username='user-alpha'", (time.time() + 3600,))
            connection.execute("UPDATE operational_state SET access_enabled=0")
        self.assertEqual(self.get(lecture).status_code, 503)
        self.assertEqual(self.post(lecture, payload).status_code, 503)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE username='user-alpha'")
        self.assertEqual(self.history(lecture).status_code, 401)

    def test_note_and_segment_ids_cannot_move_between_lectures_or_owners(self):
        first, segments = self.lecture()
        second, foreign_segments = self.lecture(owner="user-beta")
        note = str(uuid.uuid4())
        self.saved(first, note_id=note, text="private note")
        other = self.body(second, owner="user-beta", note_id=note, text="moved")
        self.assertEqual(self.post(second, other, "user-beta").status_code, 404)
        body = self.body(first, action="segment_edit", segment_id=foreign_segments[0], text="moved")
        self.assertEqual(self.post(first, body).status_code, 404)
        body = self.body(first, note_id=str(uuid.uuid4()), segment_id=foreign_segments[0], text="moved")
        self.assertEqual(self.post(first, body).status_code, 404)
        self.assertEqual(self.history(first, segment_id=foreign_segments[0]).status_code, 404)
        self.saved(first, action="note_delete", note_id=note)
        self.assertEqual(self.post(second, other, "user-beta").status_code, 404, "deleted note identity still belongs to its original history")
        self.saved(first, note_id=note, text="restored private note")
        self.assertEqual(self.get(first).json()["notes"][0]["text"], "restored private note")

    def test_history_anchor_keeps_pages_stable_across_new_changes(self):
        lecture, segments = self.lecture()
        for index in range(4):
            self.saved(lecture, action="segment_edit", segment_id=segments[0], text=f"version {index}")
        first = self.history(lecture, segment_id=segments[0], limit=2).json()
        self.assertEqual(first["at_revision"], 4)
        self.saved(lecture, action="segment_edit", segment_id=segments[0], text="new version")
        second = self.history(lecture, segment_id=segments[0], limit=2, offset=2,
                              at_revision=first["at_revision"]).json()
        self.assertEqual(second["revision"], 5)
        self.assertEqual(second["at_revision"], 4)
        self.assertEqual([item["revision"] for item in first["items"] + second["items"]], [4, 3, 2, 1])
        self.assertFalse(second["has_more"])
        self.assertEqual(self.history(lecture, at_revision=0).json()["items"], [])
        self.assertEqual(self.history(lecture, at_revision=6).status_code, 409)
        self.assertEqual(self.history(lecture, at_revision=-1).status_code, 422)

    def test_history_filters_are_stable_bounded_and_include_reset_and_deleted_versions(self):
        lecture, segments = self.lecture()
        note = str(uuid.uuid4())
        self.saved(lecture, note_id=note, text="note")
        for text in ("first", "second", None):
            self.saved(lecture, action="segment_edit", segment_id=segments[0], text=text)
        self.saved(lecture, action="note_delete", note_id=note)
        first = self.history(lecture, segment_id=segments[0], limit=2).json()
        second = self.history(lecture, segment_id=segments[0], limit=2, offset=2).json()
        self.assertEqual([row["revision"] for row in first["items"] + second["items"]], [4, 3, 2])
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual([row["revision"] for row in self.history(lecture, note_id=note).json()["items"]], [5, 1])
        for params in ({"limit": 21}, {"limit": 0}, {"offset": -1}, {"offset": 2001},
                       {"note_id": note, "segment_id": segments[0]}, {"note_id": "bad"}):
            self.assertEqual(self.history(lecture, **params).status_code, 422)
        self.assertEqual(self.history(lecture, note_id=str(uuid.uuid4())).status_code, 404)

    def test_validation_rejects_blank_text_control_chars_bad_uuid_and_mixed_actions(self):
        lecture, segments = self.lecture()
        base = self.body(lecture, action="segment_edit", segment_id=segments[0], text="text")
        for changes in ({"id": "bad"}, {"revision": True}, {"revision": "0"}, {"revision": -1},
                        {"raw_revision": "A" * 64}, {"text": " "}, {"text": "x" * 5001},
                        {"text": "synthetic\x00private"}, {"text": 4}, {"note_id": str(uuid.uuid4())},
                        {"start_seconds": 1}, {"action": "arbitrary"}, {"extra": "unexpected"}):
            self.assertEqual(self.post(lecture, {**base, **changes}).status_code, 422)
        omitted = {key: value for key, value in base.items() if key != "text"}
        self.assertEqual(self.post(lecture, omitted).status_code, 422)
        self.assertEqual(self.get(lecture).json()["revision"], 0)

    def test_note_anchor_must_match_linked_segment_and_stay_in_supported_time_range(self):
        lecture, segments = self.lecture()
        for changes in ({"segment_id": segments[1], "start_seconds": 1}, {"start_seconds": -1},
                        {"start_seconds": 14401}, {"start_seconds": True}):
            body = self.body(lecture, note_id=str(uuid.uuid4()), text="text", **changes)
            self.assertEqual(self.post(lecture, body).status_code, 422)

    def test_current_note_count_total_text_and_edit_relative_limits_preserve_prior_state(self):
        lecture, segments = self.lecture()
        self.saved(lecture, note_id=str(uuid.uuid4()), text="12345")
        before = self.snapshot(manual=True)
        for target, value in (("MAX_NOTES", 1), ("MAX_NOTE_CHARS", 5)):
            with mock.patch.object(manual_notes, target, value):
                body = self.body(lecture, note_id=str(uuid.uuid4()), text="x")
                self.assertEqual(self.post(lecture, body).status_code, 413)
            self.assertEqual(before, self.snapshot(manual=True))
        body = self.body(lecture, action="segment_edit", segment_id=segments[0], text="x" * 1001)
        self.assertEqual(self.post(lecture, body).status_code, 413)
        self.saved(lecture, action="segment_edit", segment_id=segments[0], text="12345")
        with mock.patch.object(manual_notes, "MAX_EDIT_CHARS", 5):
            body = self.body(lecture, action="segment_edit", segment_id=segments[1], text="x")
            self.assertEqual(self.post(lecture, body).status_code, 413)
        with mock.patch.object(manual_notes, "MAX_EDITS", 1):
            body = self.body(lecture, action="segment_edit", segment_id=segments[1], text="x")
            self.assertEqual(self.post(lecture, body).status_code, 413)

    def test_history_limits_never_prune_old_versions_and_still_allow_exact_receipt_retry(self):
        lecture, segments = self.lecture()
        body, ack = self.saved(lecture, action="segment_edit", segment_id=segments[0], text="first")
        next_body = self.body(lecture, action="segment_edit", segment_id=segments[0], text="second")
        before = self.snapshot(manual=True)
        for field, value in (("MAX_HISTORY", 1), ("MAX_HISTORY_CHARS", 5)):
            with mock.patch.object(manual_notes, field, value):
                self.assertEqual(self.post(lecture, next_body).status_code, 409)
                self.assertEqual(self.post(lecture, body).json(), ack)
            self.assertEqual(before, self.snapshot(manual=True))

    def test_source_bounds_apply_before_loading_transcripts(self):
        lecture, _ = self.lecture()
        with mock.patch.object(manual_notes, "MAX_SOURCE_CHARS", 1):
            self.assertEqual(self.get(lecture).status_code, 413)
            self.assertEqual(self.history(lecture).status_code, 413)

    def test_changed_raw_snapshot_hides_current_overrides_but_does_not_rewrite_history(self):
        lecture, segments = self.lecture()
        body, ack = self.saved(lecture, action="segment_edit", segment_id=segments[0], text="edited")
        before = self.snapshot(manual=True)
        with self.database.connect() as connection:
            connection.execute("UPDATE segments SET text='different source' WHERE id=?", (segments[0],))
        self.assertEqual(self.get(lecture).status_code, 409)
        self.assertEqual(self.history(lecture).status_code, 409)
        self.assertEqual(self.post(lecture, body).json(), ack, "old ACK confirms only that old transaction committed")
        self.assertEqual(before, self.snapshot(manual=True))

    def test_history_insert_failure_rolls_back_note_state_and_revision_before_retry(self):
        lecture, _ = self.lecture()
        body = self.body(lecture, note_id=str(uuid.uuid4()), text="atomic note")
        before = self.snapshot(manual=True)
        with self.database.connect() as connection:
            connection.execute("CREATE TRIGGER synthetic_history_failure BEFORE INSERT ON lecture_manual_history "
                               "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.post(lecture, body)
        self.assertEqual(before, self.snapshot(manual=True))
        with self.database.connect() as connection:
            connection.execute("DROP TRIGGER synthetic_history_failure")
        self.assertEqual(self.post(lecture, body).status_code, 200)
        self.assertEqual(self.get(lecture).json()["revision"], 1)

    def test_save_then_trash_race_preserves_committed_note_and_restoration(self):
        lecture, _ = self.lecture()
        body = self.body(lecture, note_id=str(uuid.uuid4()), text="kept across trash")
        entered, release = threading.Event(), threading.Event()
        original = manual_notes._snapshot
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("synthetic save not released")
            return original(*args, **kwargs)
        with mock.patch.object(manual_notes, "_snapshot", side_effect=blocked), ThreadPoolExecutor(max_workers=2) as pool:
            save = pool.submit(self.post, lecture, body)
            try:
                self.assertTrue(entered.wait(2))
                trash = pool.submit(self.client.post, f"/lectures/{lecture}/trash", headers=self.headers())
                release.set()
                self.assertEqual(save.result(5).status_code, 200)
                self.assertEqual(trash.result(5).status_code, 200)
            finally:
                release.set()
        self.assertEqual(self.get(lecture).status_code, 404)
        self.assertEqual(self.post(lecture, body).status_code, 404, "even committed receipts remain hidden in trash")
        self.assertEqual(self.client.post(f"/lectures/{lecture}/restore", headers=self.headers()).status_code, 200)
        self.assertEqual(self.get(lecture).json()["notes"][0]["text"], "kept across trash")

    def test_permanent_purge_cascades_notes_edits_receipts_and_history(self):
        lecture, segments = self.lecture()
        self.saved(lecture, note_id=str(uuid.uuid4()), text="note")
        self.saved(lecture, action="segment_edit", segment_id=segments[0], text="edit")
        self.assertEqual(self.client.post(f"/lectures/{lecture}/trash", headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.delete(f"/lectures/{lecture}/permanent", headers=self.headers()).status_code, 200)
        self.assertTrue(all(not rows for rows in self.snapshot(manual=True).values()))

    def test_private_notes_are_not_added_to_existing_library_search_or_ai_inputs(self):
        lecture, segments = self.lecture()
        self.saved(lecture, note_id=str(uuid.uuid4()), text="manual-only-private")
        self.saved(lecture, action="segment_edit", segment_id=segments[0], text="manual-only-private")
        response = self.client.get("/library/search", headers=self.headers(), params={"q": "manual-only-private"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])
        with self.database.connect() as connection:
            raw = [row[0] for row in connection.execute("SELECT text FROM segments WHERE lecture_id=?", (lecture,))]
        self.assertNotIn("manual-only-private", str(raw))


if __name__ == "__main__":
    unittest.main()
