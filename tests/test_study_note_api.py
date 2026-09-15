"""Study-note API/worker regressions with temporary DBs and a local fake only."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.postprocessor import PostprocessingError
from server.security import digest
from server.settings import Settings
from server import study_notes
from server.study_notes import StudyNoteDocument, StudyNoteError


class FakeTranscriber:
    def status(self):
        return {"model_state": "ready", "model": "test-local", "device": "cpu"}


class FakeStudyNoteOutput:
    """Allow noncanonical engine outputs without bypassing service coercion."""

    def __init__(self, document):
        self.document = document

    def to_dict(self):
        return copy.deepcopy(self.document)


class FakeStudyNotes:
    configured = True
    model = "synthetic-study-note-model"

    def __init__(self):
        self.calls = []
        self.error = None
        self.during = None
        self.invalid = False
        self.document = None
        self.closed = False

    def create(self, *, language, segments, interrupted):
        self.calls.append({"language": language, "segments": copy.deepcopy(segments)})
        if self.during:
            self.during(segments, interrupted)
        if self.error:
            raise self.error
        if self.document is not None:
            return copy.deepcopy(self.document)
        return StudyNoteDocument([{
            "heading": "핵심 내용", "source_ids": ["outside-source" if self.invalid else segment["id"]],
            "text": segment["text"], "edits": [],
        } for segment in segments])

    def close(self):
        self.closed = True


class StudyNoteApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-study-note-test-")
        directory = Path(self.temporary.name)
        self.settings = Settings(data_dir=directory / "data", model_cache_dir=directory / "models",
                                 site_origins=("https://student.github.io",), admin_username="user-alpha")
        self.engine = FakeStudyNotes()
        self.app = create_app(self.settings, FakeTranscriber(), study_note_maker=self.engine)
        self.database = self.app.state.database
        self.service = self.app.state.study_note_service
        self.start_patch = patch.object(self.service, "start")
        self.start = self.start_patch.start()
        self.client = TestClient(self.app)
        self.tokens = {"user-alpha": "synthetic-study-alpha-token", "user-beta": "synthetic-study-beta-token"}
        with self.database.connect() as connection:
            for username, token in self.tokens.items():
                connection.execute("INSERT INTO sessions VALUES(?,?,?,?)", (digest(token), username, time.time() + 3600, time.time()))

    def tearDown(self):
        self.service.stop()
        self.start_patch.stop()
        self.client.close()
        self.temporary.cleanup()

    def headers(self, username="user-alpha"):
        return {"Authorization": f"Bearer {self.tokens[username]}"}

    def lecture(self, *, username="user-alpha", finalized=True, text="식물은 빛을 이용해 양분을 만든다."):
        identifier, chunk, segment = [str(uuid.uuid4()) for _ in range(3)]
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lectures(id,username,title,language,created_at,recording_finalized) "
                               "VALUES(?,?,'synthetic-private-title','ko','2026-09-09T00:00:00Z',?)",
                               (identifier, username, int(finalized)))
            connection.execute("INSERT INTO chunks(lecture_id,chunk_id,payload_hash,start_seconds,status) "
                               "VALUES(?,?,'synthetic-hash',0,'done')", (identifier, chunk))
            if text is not None:
                connection.execute("INSERT INTO segments VALUES(?,?,?,0,1,?)", (segment, identifier, chunk, text))
        return identifier, segment

    def post(self, identifier, username="user-alpha"):
        return self.client.post(f"/lectures/{identifier}/study-note", headers=self.headers(username))

    def get(self, identifier, username="user-alpha"):
        return self.client.get(f"/lectures/{identifier}/study-note", headers=self.headers(username))

    def row(self, identifier):
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM lecture_study_notes WHERE lecture_id=?", (identifier,)).fetchone()
            return dict(row) if row is not None else None

    def queued(self, identifier, username="user-alpha"):
        response = self.post(identifier, username)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["study_note"]["status"], "queued")
        return self.row(identifier)

    def snapshot(self, identifier):
        with self.database.connect() as connection:
            return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} WHERE lecture_id=? ORDER BY rowid", (identifier,))]
                    for table in ("segments", "chunks", "transcript_corrections", "lecture_summaries", "lecture_translations",
                                  "lecture_manual_state", "lecture_manual_notes", "lecture_manual_edits", "lecture_metadata")}

    def test_owner_only_get_post_and_no_administrator_bypass(self):
        identifier, _ = self.lecture(username="user-beta")
        for call in (self.get, self.post):
            self.assertEqual(call(identifier, "user-alpha").status_code, 404)
        for method in (self.client.get, self.client.post):
            self.assertEqual(method(f"/lectures/{identifier}/study-note").status_code, 401)
        self.queued(identifier, "user-beta")
        self.service.process_next()
        self.assertEqual(self.get(identifier, "user-beta").json()["study_note"]["status"], "completed")
        self.assertEqual(self.get(identifier, "user-alpha").status_code, 404)
        self.assertEqual(self.post(identifier, "user-alpha").status_code, 404)

    def test_get_is_read_only_and_completed_document_and_markdown_are_both_validated(self):
        identifier, segment = self.lecture()
        response = self.get(identifier).json()
        self.assertEqual(set(response), {"configured", "model", "study_note"})
        self.assertIsNone(response["study_note"])
        self.assertEqual(self.engine.calls, [])
        self.queued(identifier)
        self.service.process_next()
        before = self.row(identifier)
        response = self.get(identifier).json()["study_note"]
        self.assertEqual(set(response), {"lecture_id", "status", "model", "error_code", "error", "created_at",
                                         "updated_at", "completed_at", "document", "markdown"})
        self.assertEqual(response["document"]["paragraphs"][0]["source_ids"], [segment])
        self.assertEqual(response["document"]["paragraphs"][0]["text"], "식물은 빛을 이용해 양분을 만든다.")
        self.assertIn(r"식물은 빛을 이용해 양분을 만든다\.", response["markdown"])
        self.assertNotIn("synthetic-private-title", response["markdown"])
        self.assertEqual(self.row(identifier), before)
        self.assertEqual(len(self.engine.calls), 1)

    def test_numbers_contacts_and_restored_terms_are_saved_and_read_back(self):
        identifier, segment = self.lecture(text="지난 2026년 수업에서 광 합 성을 10번 설명했다.")
        paragraph = {
            "heading": "1주차 · 2027년 수업 정리",
            "source_ids": [segment],
            "text": "2027년 수업에서는 광합성을 12번 설명했다. 확인 연락처: classroom@example.invalid, 010-0000-0000.",
            "edits": [
                {"original": "2026년", "replacement": "2027년", "uncertain": True},
                {"original": "광 합 성", "replacement": "광합성", "uncertain": False},
            ],
        }
        self.engine.document = StudyNoteDocument([paragraph])
        source_before = self.snapshot(identifier)
        self.queued(identifier)
        self.assertTrue(self.service.process_next())
        saved = self.row(identifier)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(json.loads(saved["document_json"]), {"paragraphs": [paragraph]})
        self.assertIsNone(saved["error_code"])
        self.assertIsNotNone(saved["completed_at"])

        response = self.get(identifier)
        self.assertEqual(response.status_code, 200, response.text)
        note = response.json()["study_note"]
        self.assertEqual(note["status"], "completed")
        self.assertEqual(note["document"], {"paragraphs": [paragraph]})
        self.assertIn("1주차 · 2027년 수업 정리", note["markdown"])
        self.assertIn("2027년 수업에서는 광합성을 12번 설명했다", note["markdown"])
        self.assertIn(r"classroom@example\.invalid", note["markdown"])
        self.assertIn(r"010\-0000\-0000", note["markdown"])
        self.assertIn("2026년 → 2027년", note["markdown"])
        self.assertEqual(self.get(identifier).json()["study_note"], note)
        self.assertEqual(self.post(identifier).json()["study_note"], note)
        self.assertEqual(self.row(identifier), saved)
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.snapshot(identifier), source_before)
        self.assertEqual(self.get(identifier, "user-beta").status_code, 404)
        self.assertEqual(self.post(identifier, "user-beta").status_code, 404)

    def test_paraphrased_terminology_audit_need_not_match_exact_source_or_output_substrings(self):
        identifier, segment = self.lecture(text="라이트를 받으면 식물이 양분을 만든다는 설명입니다.")
        paragraph = {
            "heading": "광합성과 에너지 전환",
            "source_ids": [segment],
            "text": "식물은 빛에너지를 이용해 양분을 만드는 광합성을 수행한다.",
            "edits": [{
                "original": "빛을 이용해 양분을 생성하는 과정",
                "replacement": "광합성(photosynthesis)",
                "uncertain": True,
            }],
        }
        self.assertNotIn(paragraph["edits"][0]["original"], "라이트를 받으면 식물이 양분을 만든다는 설명입니다.")
        self.assertNotIn(paragraph["edits"][0]["replacement"], paragraph["text"])
        self.engine.document = StudyNoteDocument([paragraph])
        self.queued(identifier)
        self.assertTrue(self.service.process_next())
        saved = self.row(identifier)
        note = self.get(identifier).json()["study_note"]
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(json.loads(saved["document_json"]), {"paragraphs": [paragraph]})
        self.assertEqual(note["status"], "completed")
        self.assertEqual(note["document"], {"paragraphs": [paragraph]})
        self.assertIn(r"광합성\(photosynthesis\)", note["markdown"])
        self.assertIn("추정 · 확인 필요", note["markdown"])
        self.assertEqual(self.get(identifier).json()["study_note"], note)
        self.assertEqual(self.row(identifier), saved)

    def test_previously_rejected_note_can_be_explicitly_retried_and_saved(self):
        identifier, segment = self.lecture()
        self.engine.error = StudyNoteError("protected_content_changed")
        rejected = self.queued(identifier)
        self.assertTrue(self.service.process_next())
        failed = self.get(identifier).json()["study_note"]
        self.assertEqual((failed["status"], failed["error_code"]), ("failed", "protected_content_changed"))
        self.assertIsNone(failed["document"])
        self.assertIsNone(failed["markdown"])
        self.engine.error = None
        paragraph = {
            "heading": "1. 광합성",
            "source_ids": [segment],
            "text": "광합성은 2가지 단계를 통해 빛에너지를 양분으로 전환한다.",
            "edits": [{"original": "문맥에서 복원한 설명", "replacement": "광합성의 두 단계", "uncertain": True}],
        }
        self.engine.document = StudyNoteDocument([paragraph])
        retried = self.queued(identifier)
        self.assertNotEqual(retried["job_id"], rejected["job_id"])
        self.assertTrue(self.service.process_next())
        note = self.get(identifier).json()["study_note"]
        self.assertEqual(note["status"], "completed")
        self.assertIsNone(note["error_code"])
        self.assertEqual(note["document"], {"paragraphs": [paragraph]})
        self.assertIn("2가지 단계", note["markdown"])
        self.assertEqual(len(self.engine.calls), 2)

    def test_original_corrected_translated_and_manual_products_are_untouched(self):
        identifier, segment = self.lecture()
        with self.database.connect() as connection:
            raw = self.service.raw_segments(connection, identifier)
            revision = self.service.revision(raw)
            connection.execute("INSERT INTO transcript_corrections(lecture_id,raw_revision,status,model,corrected_text,corrected_segments,"
                               "uncertain_terms,created_at,updated_at,completed_at) VALUES(?,?,'completed','synthetic','별도 후보정',?,'[]','now','now','now')",
                               (identifier, revision, json.dumps([{**raw[0], "text": "별도 후보정"}])) )
            connection.execute("INSERT INTO lecture_translations(lecture_id,job_id,raw_revision,status,model,translation_json,created_at,updated_at,completed_at) "
                               "VALUES(?,?,?,'completed','synthetic',?,'now','now','now')",
                               (identifier, str(uuid.uuid4()), revision, json.dumps([{**raw[0], "text": "별도 번역"}])))
            connection.execute("INSERT INTO lecture_manual_state VALUES(?,?,1)", (identifier, revision))
            connection.execute("INSERT INTO lecture_manual_edits VALUES(?,?,'보내지 않을 직접 수정','now','now')", (segment, identifier))
            connection.execute("INSERT INTO lecture_metadata VALUES(?,'보내지 않을 표시 이름','분류','학기',1,'now')", (identifier,))
        self.engine.document = StudyNoteDocument([{
            "heading": "1주차 광합성",
            "source_ids": ["outside-source"],
            "text": "식물은 2가지 단계로 광합성을 진행한다.",
            "edits": [{"original": "빛을 이용한 양분 생성", "replacement": "광합성(photosynthesis)", "uncertain": True}],
        }])
        before = self.snapshot(identifier)
        self.queued(identifier)
        self.service.process_next()
        note = self.get(identifier).json()["study_note"]
        self.assertEqual(note["status"], "completed")
        self.assertEqual(note["document"]["format"], "draft")
        self.assertEqual(note["document"]["warnings"], ["invalid_response"])
        self.assertEqual(self.snapshot(identifier), before)
        self.assertEqual(self.engine.calls, [{"language": "ko", "segments": raw}])

    def test_finalized_empty_source_and_preflight_limits_create_no_job(self):
        for values in ({"finalized": False}, {"text": None}, {"text": " "}, {"text": "가" * 24001}):
            with self.subTest(values=list(values)):
                identifier, _ = self.lecture(**values)
                response = self.post(identifier)
                self.assertIn(response.status_code, (409, 413))
                self.assertIsNone(self.row(identifier))
        identifier, _ = self.lecture()
        with patch("server.study_note_service.validate_study_note_source", side_effect=StudyNoteError("source_too_large")):
            self.assertEqual(self.post(identifier).status_code, 413)
        self.assertIsNone(self.row(identifier))
        self.assertEqual(self.engine.calls, [])
        self.start.assert_not_called()

    def test_expired_revoked_sessions_and_disallowed_origins_cannot_access_notes(self):
        identifier, _ = self.lecture()
        self.queued(identifier)
        self.service.process_next()
        for method in (self.client.get, self.client.post):
            self.assertEqual(method(f"/lectures/{identifier}/study-note", headers={
                **self.headers(), "Origin": "https://untrusted.invalid",
            }).status_code, 403)
        with self.database.connect() as connection:
            connection.execute("UPDATE sessions SET expires_at=0 WHERE username='user-alpha'")
        self.assertEqual(self.get(identifier).status_code, 401)
        self.assertEqual(self.post(identifier).status_code, 401)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE username='user-alpha'")
        self.assertEqual(self.get(identifier).status_code, 401)
        self.assertEqual(self.post(identifier).status_code, 401)

    def test_single_active_job_per_owner_but_other_owner_can_queue(self):
        first, _ = self.lecture()
        second, _ = self.lecture()
        other, _ = self.lecture(username="user-beta")
        self.queued(first)
        self.assertEqual(self.post(second).status_code, 409)
        self.queued(other, "user-beta")
        self.assertTrue(self.service.process_next())
        self.assertTrue(self.service.process_next())
        self.assertFalse(self.service.process_next())

    def test_concurrent_posts_reuse_one_job_and_completion_is_cached(self):
        identifier, _ = self.lecture()
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: self.post(identifier), range(2)))
        self.assertTrue(all(response.status_code == 202 for response in responses))
        first = self.row(identifier)
        self.service.process_next()
        completed = self.row(identifier)
        for _ in range(3):
            self.assertEqual(self.post(identifier).json()["study_note"]["status"], "completed")
        self.assertEqual(self.row(identifier), completed)
        self.assertEqual(first["job_id"], completed["job_id"])
        self.assertEqual(len(self.engine.calls), 1)

    def test_model_change_does_not_rewrite_or_regenerate_until_explicit_post(self):
        identifier, _ = self.lecture()
        self.queued(identifier)
        self.service.process_next()
        completed = self.row(identifier)
        self.engine.model = "synthetic-new-model"
        self.service.recover()
        self.assertFalse(self.service.process_next())
        result = self.get(identifier).json()
        self.assertEqual(result["model"], self.engine.model)
        self.assertEqual(result["study_note"]["model"], completed["model"])
        self.assertEqual(self.row(identifier), completed)
        replacement = self.queued(identifier)
        self.assertNotEqual(replacement["job_id"], completed["job_id"])
        self.assertEqual(replacement["model"], self.engine.model)

    def test_six_hourly_requests_but_cache_or_inflight_replay_spends_no_extra_limit(self):
        for _ in range(6):
            identifier, _ = self.lecture()
            self.queued(identifier)
            self.post(identifier)
            self.service.process_next()
            self.assertEqual(self.post(identifier).json()["study_note"]["status"], "completed")
        final, _ = self.lecture()
        self.assertEqual(self.post(final).status_code, 429)
        self.assertIsNone(self.row(final))

    def test_unconfigured_service_returns_existing_results_but_never_starts_new_work(self):
        completed, _ = self.lecture()
        self.queued(completed)
        self.service.process_next()
        pending, _ = self.lecture()
        self.queued(pending)
        self.engine.configured = False
        self.service.recover()
        result = self.get(completed).json()
        self.assertFalse(result["configured"])
        self.assertEqual(result["study_note"]["status"], "completed")
        self.assertEqual(self.row(pending)["error_code"], "not_configured")
        new, _ = self.lecture()
        self.assertEqual(self.post(new).status_code, 503)
        self.assertIsNone(self.row(new))
        self.assertFalse(self.service.process_next())

    def test_known_provider_errors_are_fixed_and_unknown_exceptions_are_redacted(self):
        with patch.object(self.service.limiter, "allow", return_value=True):
            for code in ("credit_exhausted", "authentication_failed", "gateway_unavailable", "rate_limited",
                         "response_truncated", "model_refused", "synthetic-private-code", None):
                identifier, _ = self.lecture()
                self.queued(identifier)
                self.engine.error = (PostprocessingError(code, "synthetic-private-provider-message")
                                     if code else RuntimeError("synthetic-private-provider-message"))
                self.service.process_next()
                result = self.get(identifier).json()["study_note"]
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error_code"], StudyNoteError(code).code if code else "study_note_failed")
                self.assertIsNone(result["document"])
                self.assertIsNone(result["markdown"])
                self.assertNotIn("synthetic-private", json.dumps(self.row(identifier)))
                self.service.recover()
                self.assertFalse(self.service.process_next())

    def test_unknown_source_ids_save_unmapped_draft_and_preserve_all_source_products(self):
        identifier, _ = self.lecture()
        before = self.snapshot(identifier)
        self.queued(identifier)
        self.engine.invalid = True
        self.service.process_next()
        saved = self.row(identifier)
        self.assertEqual(saved["status"], "completed")
        draft = json.loads(saved["document_json"])
        self.assertEqual(set(draft), {"format", "text", "warnings"})
        self.assertEqual(draft["format"], "draft")
        self.assertIn("식물은 빛을 이용해 양분을 만든다.", draft["text"])
        self.assertEqual(draft["warnings"], ["invalid_response"])
        self.assertNotIn("outside-source", draft["text"])
        self.assertEqual(self.snapshot(identifier), before)
        note = self.get(identifier).json()["study_note"]
        self.assertEqual(note["status"], "completed")
        self.assertEqual(note["document"], draft)
        self.assertIn("식물은 빛을 이용해 양분을 만든다", note["markdown"])
        self.assertNotIn("outside-source", note["markdown"])
        self.assertIn(study_notes.STUDY_NOTE_RESULT_WARNING, note["markdown"])
        self.assertGreater(note["markdown"].rindex(study_notes.STUDY_NOTE_RESULT_WARNING),
                           note["markdown"].index("식물은 빛을 이용해 양분을 만든다"))
        self.assertEqual(self.post(identifier).json()["study_note"], note)
        self.assertEqual(self.row(identifier), saved)
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.get(identifier, "user-beta").status_code, 404)
        self.assertEqual(self.post(identifier, "user-beta").status_code, 404)

    def test_canonical_partial_draft_persists_warnings_download_cache_and_owner_scope(self):
        identifier, _ = self.lecture()
        before = self.snapshot(identifier)
        draft = {
            "format": "draft",
            "text": "# 광합성 정리\n\n식물은 빛에너지를 이용해 양분을 만든다.\n\n생성된 부분까지만 보관한다.",
            "warnings": ["response_truncated", "incomplete_batches", "gateway_unavailable"],
        }
        self.engine.document = FakeStudyNoteOutput(draft)
        self.queued(identifier)
        self.assertTrue(self.service.process_next())
        saved = self.row(identifier)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(json.loads(saved["document_json"]), draft)
        self.assertIsNone(saved["error"])
        self.assertIsNone(saved["error_code"])
        self.assertIsNotNone(saved["completed_at"])
        note = self.get(identifier).json()["study_note"]
        self.assertEqual(note["status"], "completed")
        self.assertEqual(note["document"], draft)
        self.assertIn("생성된 부분까지만 보관한다", note["markdown"])
        body, footer = note["markdown"].rsplit(study_notes.STUDY_NOTE_RESULT_WARNING, 1)
        self.assertIn("생성된 부분까지만 보관한다", body)
        self.assertEqual(footer.strip(), "")
        self.assertTrue(note["markdown"].rstrip().endswith(study_notes.STUDY_NOTE_RESULT_WARNING))
        self.assertEqual(note["markdown"].count(study_notes.STUDY_NOTE_RESULT_WARNING), 1)
        self.assertNotIn("생성 중 확인된 문제", note["markdown"])
        self.assertNotIn("AI 서버 연결 문제", note["markdown"])
        self.assertEqual(self.get(identifier).json()["study_note"], note)
        self.assertEqual(self.post(identifier).json()["study_note"], note)
        self.assertEqual(self.row(identifier), saved)
        self.assertEqual(self.snapshot(identifier), before)
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.get(identifier, "user-beta").status_code, 404)
        self.assertEqual(self.post(identifier, "user-beta").status_code, 404)

    def test_no_usable_engine_body_still_fails_without_inventing_a_result(self):
        with patch.object(self.service.limiter, "allow", return_value=True):
            for document in ({}, {"paragraphs": []}, {"format": "draft", "text": " ", "warnings": ["invalid_response"]}):
                with self.subTest(document=document):
                    identifier, _ = self.lecture()
                    before = self.snapshot(identifier)
                    self.engine.document = FakeStudyNoteOutput(document)
                    self.queued(identifier)
                    self.assertTrue(self.service.process_next())
                    self.assertEqual(self.row(identifier)["status"], "failed")
                    self.assertIsNone(self.row(identifier)["document_json"])
                    note = self.get(identifier).json()["study_note"]
                    self.assertEqual(note["status"], "failed")
                    self.assertIsNone(note["document"])
                    self.assertIsNone(note["markdown"])
                    self.assertEqual(self.snapshot(identifier), before)
                    self.assertFalse(self.service.process_next())

    def test_markdown_render_failure_cannot_advertise_a_downloadable_result(self):
        identifier, _ = self.lecture()
        self.queued(identifier)
        self.engine.invalid = True
        with patch("server.study_note_service.study_note_markdown", side_effect=RuntimeError("synthetic-private-renderer")):
            self.service.process_next()
        self.assertIsNone(self.row(identifier)["document_json"])
        self.assertEqual(self.row(identifier)["status"], "failed")

    def test_saved_error_messages_and_unknown_codes_are_never_reflected(self):
        for code in ("credit_exhausted", "study_note_failed", "synthetic-private-code", None):
            identifier, _ = self.lecture()
            self.queued(identifier)
            with self.database.connect() as connection:
                connection.execute("UPDATE lecture_study_notes SET status='failed',error_code=?,error=? WHERE lecture_id=?",
                                   (code, "synthetic-private-saved-message", identifier))
            before = self.row(identifier)
            result = self.get(identifier).json()["study_note"]
            self.assertNotIn("synthetic-private", json.dumps(result))
            self.assertEqual(result["error_code"], "credit_exhausted" if code == "credit_exhausted" else "study_note_failed")
            self.assertEqual(self.row(identifier), before)

    def test_corrupt_saved_document_and_stale_revision_are_hidden_without_writes(self):
        for mutation in ("document", "source"):
            identifier, segment = self.lecture()
            self.queued(identifier)
            self.service.process_next()
            with self.database.connect() as connection:
                if mutation == "document":
                    connection.execute("UPDATE lecture_study_notes SET document_json='{}' WHERE lecture_id=?", (identifier,))
                else:
                    connection.execute("UPDATE segments SET text='원문이 달라졌다.' WHERE id=?", (segment,))
            before = self.row(identifier)
            result = self.get(identifier).json()["study_note"]
            self.assertEqual((result["status"], result["error_code"]), ("failed", "invalid_saved_study_note"))
            self.assertIsNone(result["document"])
            self.assertIsNone(result["markdown"])
            self.assertEqual(self.row(identifier), before)

    def test_saved_draft_is_not_repaired_on_read_and_requires_unchanged_owned_source(self):
        draft = {"format": "draft", "text": "AI가 생성한 일부 수업 정리입니다.", "warnings": ["invalid_response"]}
        with patch.object(self.service.limiter, "allow", return_value=True):
            for mutation in ("unknown_warning", "extra_field", "empty_text", "source"):
                with self.subTest(mutation=mutation):
                    identifier, segment = self.lecture()
                    self.engine.document = FakeStudyNoteOutput(draft)
                    self.queued(identifier)
                    self.service.process_next()
                    with self.database.connect() as connection:
                        if mutation == "source":
                            connection.execute("UPDATE segments SET text='원문이 달라졌다.' WHERE id=?", (segment,))
                        else:
                            damaged = copy.deepcopy(draft)
                            if mutation == "unknown_warning":
                                damaged["warnings"] = ["synthetic-private-provider-message"]
                            elif mutation == "extra_field":
                                damaged["private_metadata"] = "synthetic-private-value"
                            else:
                                damaged["text"] = " "
                            connection.execute("UPDATE lecture_study_notes SET document_json=? WHERE lecture_id=?",
                                               (json.dumps(damaged), identifier))
                    before = self.row(identifier)
                    result = self.get(identifier).json()["study_note"]
                    self.assertEqual((result["status"], result["error_code"]), ("failed", "invalid_saved_study_note"))
                    self.assertIsNone(result["document"])
                    self.assertIsNone(result["markdown"])
                    self.assertNotIn("synthetic-private", json.dumps(result))
                    self.assertEqual(self.row(identifier), before)

    def test_usable_warning_draft_never_bypasses_final_source_ownership_or_shutdown_checks(self):
        with patch.object(self.service.limiter, "allow", return_value=True):
            for mutation in ("source", "owner", "trash", "unfinished", "access_pause", "shutdown"):
                with self.subTest(mutation=mutation):
                    identifier, segment = self.lecture()
                    self.engine.document = FakeStudyNoteOutput({
                        "format": "draft", "text": "생성할 수 있었던 수업 정리 내용입니다.",
                        "warnings": ["invalid_response", "incomplete_batches"],
                    })
                    self.queued(identifier)

                    def change(*_):
                        if mutation == "shutdown":
                            self.service.request_shutdown()
                            return
                        with self.database.connect() as connection:
                            if mutation == "source":
                                connection.execute("UPDATE segments SET text='원문이 달라졌다.' WHERE id=?", (segment,))
                            elif mutation == "owner":
                                connection.execute("UPDATE lectures SET username='user-beta' WHERE id=?", (identifier,))
                            elif mutation == "trash":
                                connection.execute("UPDATE lectures SET trashed_at='synthetic-trash-time' WHERE id=?", (identifier,))
                            elif mutation == "unfinished":
                                connection.execute("UPDATE lectures SET recording_finalized=0 WHERE id=?", (identifier,))
                            else:
                                connection.execute("UPDATE operational_state SET access_enabled=0")

                    self.engine.during = change
                    self.assertTrue(self.service.process_next())
                    row = self.row(identifier)
                    self.assertEqual(row["status"], "failed")
                    self.assertEqual(row["error_code"], "interrupted" if mutation in ("access_pause", "shutdown") else "source_changed")
                    self.assertIsNone(row["document_json"])
                    self.assertIsNone(row["completed_at"])
                    self.engine.during = None
                    self.service.shutdown.clear()
                    with self.database.connect() as connection:
                        connection.execute("UPDATE operational_state SET access_enabled=1")

    def test_source_change_before_claim_prevents_call_and_during_call_discards_result(self):
        for during in (False, True):
            identifier, segment = self.lecture()
            self.queued(identifier)

            def change(*_):
                with self.database.connect() as connection:
                    connection.execute("UPDATE segments SET text='원문이 달라졌다.' WHERE id=?", (segment,))

            previous_calls = len(self.engine.calls)
            if during:
                self.engine.during = change
            else:
                change()
            self.service.process_next()
            self.assertEqual(self.row(identifier)["error_code"], "source_changed")
            self.assertIsNone(self.row(identifier)["document_json"])
            self.assertEqual(len(self.engine.calls), previous_calls + int(during))

    def test_owner_change_or_trash_during_call_discards_result(self):
        for mutation in ("owner", "trash"):
            identifier, _ = self.lecture()
            self.queued(identifier)

            def change(_segments, interrupted):
                with self.database.connect() as connection:
                    if mutation == "owner":
                        connection.execute("UPDATE lectures SET username='user-beta' WHERE id=?", (identifier,))
                    else:
                        connection.execute("UPDATE lectures SET trashed_at='synthetic-trash-time' WHERE id=?", (identifier,))
                self.assertTrue(interrupted())

            self.engine.during = change
            self.service.process_next()
            self.assertEqual(self.row(identifier)["error_code"], "source_changed")
            self.assertIsNone(self.row(identifier)["document_json"])
            self.assertEqual(self.get(identifier).status_code, 404)
            if mutation == "owner":
                self.assertIsNone(self.get(identifier, "user-beta").json()["study_note"])
                self.assertEqual(self.post(identifier, "user-beta").status_code, 404)

    def test_engine_runs_outside_db_lock_and_cannot_mutate_its_validation_snapshot(self):
        identifier, _ = self.lecture()
        before = self.snapshot(identifier)
        self.queued(identifier)

        def mutate(segments, _interrupted):
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
            segments[0]["id"] = "outside-source"

        self.engine.during = mutate
        self.service.process_next()
        self.assertEqual(self.row(identifier)["status"], "completed")
        note = self.get(identifier).json()["study_note"]
        self.assertEqual(note["document"]["format"], "draft")
        self.assertEqual(note["document"]["warnings"], ["invalid_response"])
        self.assertNotIn("outside-source", note["markdown"])
        self.assertEqual(self.snapshot(identifier), before)

    def test_processing_shutdown_never_replays_but_unclaimed_queue_can_resume(self):
        first, _ = self.lecture()
        second, _ = self.lecture(username="user-beta")
        self.queued(first)
        self.queued(second, "user-beta")
        self.engine.during = lambda *_: self.service.request_shutdown()
        self.service.process_next()
        terminal = [identifier for identifier in (first, second) if self.row(identifier)["status"] == "failed"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(self.row(terminal[0])["error_code"], "interrupted")
        self.service.shutdown.clear()
        self.engine.during = None
        self.service.recover()
        self.assertTrue(self.service.process_next())
        self.assertFalse(self.service.process_next())
        self.assertEqual(len(self.engine.calls), 2)
        self.assertEqual(self.row(terminal[0])["status"], "failed")

    def test_restart_of_processing_claim_is_terminal_without_any_provider_call(self):
        identifier, _ = self.lecture()
        self.queued(identifier)
        with self.database.connect() as connection:
            connection.execute("UPDATE lecture_study_notes SET status='processing',attempts=1 WHERE lecture_id=?", (identifier,))
        self.service.recover()
        self.assertEqual(self.row(identifier)["error_code"], "interrupted")
        self.assertFalse(self.service.process_next())
        self.assertEqual(self.engine.calls, [])

    def test_access_pause_before_or_during_work_never_publishes_new_result(self):
        identifier, _ = self.lecture()
        self.queued(identifier)
        with self.database.connect() as connection:
            connection.execute("UPDATE operational_state SET access_enabled=0")
        self.assertEqual(self.get(identifier).status_code, 503)
        self.assertEqual(self.post(identifier).status_code, 503)
        self.assertFalse(self.service.process_next())
        with self.database.connect() as connection:
            connection.execute("UPDATE operational_state SET access_enabled=1")

        def pause(_segments, interrupted):
            with self.database.connect() as connection:
                connection.execute("UPDATE operational_state SET access_enabled=0")
            self.assertTrue(interrupted())

        self.engine.during = pause
        self.service.process_next()
        self.assertEqual(self.row(identifier)["error_code"], "interrupted")
        self.assertIsNone(self.row(identifier)["document_json"])

    def test_active_job_blocks_trash_then_completed_note_survives_restore_and_purge_cascades(self):
        identifier, _ = self.lecture()
        self.queued(identifier)
        self.assertEqual(self.client.delete(f"/lectures/{identifier}", headers=self.headers()).status_code, 409)

        def check_processing(*_):
            self.assertEqual(self.client.delete(f"/lectures/{identifier}", headers=self.headers()).status_code, 409)

        self.engine.during = check_processing
        self.service.process_next()
        saved = self.row(identifier)
        self.assertEqual(self.client.delete(f"/lectures/{identifier}", headers=self.headers()).status_code, 200)
        self.assertEqual(self.get(identifier).status_code, 404)
        self.assertEqual(self.post(identifier).status_code, 404)
        self.assertEqual(self.row(identifier), saved)
        self.assertEqual(self.client.post(f"/lectures/{identifier}/restore", headers=self.headers()).status_code, 200)
        self.assertEqual(self.get(identifier).json()["study_note"]["status"], "completed")
        self.client.delete(f"/lectures/{identifier}", headers=self.headers())
        self.assertEqual(self.client.delete(f"/lectures/{identifier}/permanent", headers=self.headers()).status_code, 200)
        self.assertIsNone(self.row(identifier))

    def test_failed_final_save_and_lost_commit_ack_never_reissue_provider_request(self):
        for lost_ack in (False, True):
            identifier, _ = self.lecture()
            self.queued(identifier)
            original_connect = self.database.connect
            calls = 0

            @contextmanager
            def fail_final_write():
                nonlocal calls
                calls += 1
                # Claim, pre-call interruption, then final result transaction.
                if calls == 3 and not lost_ack:
                    raise sqlite3.OperationalError("synthetic-private-save-failure")
                with original_connect() as connection:
                    yield connection
                if calls == 3 and lost_ack:
                    raise sqlite3.OperationalError("synthetic-private-commit-ack-lost")

            prior_calls = len(self.engine.calls)
            with patch.object(self.database, "connect", fail_final_write):
                with self.assertRaises(sqlite3.OperationalError):
                    self.service.process_next()
            self.assertFalse(self.service.process_next())
            self.assertEqual(len(self.engine.calls), prior_calls + 1)
            self.assertEqual(self.row(identifier)["status"], "completed" if lost_ack else "failed")
            self.assertNotIn("synthetic-private", json.dumps(self.row(identifier)))

    def test_lost_claim_ack_and_worker_process_lock_do_not_send_twice(self):
        identifier, _ = self.lecture()
        self.queued(identifier)
        original_connect = self.database.connect

        @contextmanager
        def lost_claim_ack():
            with original_connect() as connection:
                yield connection
            raise sqlite3.OperationalError("synthetic-private-claim-ack-lost")

        with patch.object(self.database, "connect", lost_claim_ack):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.process_next()
        self.assertFalse(self.service.process_next())
        self.assertEqual(self.row(identifier)["status"], "failed")
        self.assertEqual(self.engine.calls, [])
        self.service.process_lock.acquire()
        try:
            self.assertFalse(self.service.process_next())
        finally:
            self.service.process_lock.release()

    def test_thread_start_failure_is_clean_and_engine_waits_for_running_thread_to_stop(self):
        self.start_patch.stop()
        with patch("server.study_note_service.threading.Thread.start", side_effect=RuntimeError("synthetic-thread-failure")):
            with self.assertRaises(RuntimeError):
                self.service.start()
        self.assertIsNone(self.service.thread)
        done = threading.Event()
        self.service.thread = threading.Thread(target=lambda: done.wait(5), daemon=True)
        self.service.thread.start()
        try:
            self.assertFalse(self.service.stop(timeout=0))
            self.assertFalse(self.engine.closed)
        finally:
            done.set()
            self.service.thread.join(1)
        self.assertTrue(self.service.stop(timeout=0))
        self.assertTrue(self.engine.closed)


if __name__ == "__main__":
    unittest.main()
