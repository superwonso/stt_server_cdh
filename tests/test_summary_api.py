"""Summary API/worker regressions: temporary storage and an injected local fake only."""
from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.security import digest
from server.settings import Settings
from server.summarizer import LectureSummary


class FakeTranscriber:
    def status(self):
        return {"model_state": "ready", "model": "test-local", "device": "cpu"}


class FakeSummarizer:
    configured = True

    def __init__(self):
        self.calls = []
        self.error = None
        self.during = None
        self.invalid = False

    def summarize(self, *, language, segments, interrupted):
        self.calls.append({"language": language, "segments": copy.deepcopy(segments)})
        if self.during:
            self.during()
        if self.error:
            raise self.error
        identifier = "outside-lecture" if self.invalid else segments[0]["id"]
        return LectureSummary(
            overview="빛을 이용해 양분을 만드는 과정이다.",
            overview_source_ids=[identifier],
            sections=[{"heading": "핵심 개념", "bullets": [{
                "text": "빛을 이용해 양분을 만든다.", "source_ids": [identifier],
            }]}],
            review_questions=[{"question": "이 과정의 핵심을 설명할 수 있나요?",
                               "source_ids": [identifier]}],
        )


class SummaryApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.engine = FakeSummarizer()
        self.settings = Settings(data_dir=directory / "data", model_cache_dir=directory / "models",
                                 site_origins=("https://student.github.io",))
        # Passing Settings directly never reads the deployment's environment or credentials.
        self.app = create_app(self.settings, FakeTranscriber(), summarizer=self.engine)
        self.database = self.app.state.database
        self.service = self.app.state.summary_service
        self.start_patch = patch.object(self.service, "start")
        self.start = self.start_patch.start()
        self.client = TestClient(self.app)
        self.tokens = {"user-alpha": "summary-alpha-token", "user-beta": "summary-beta-token"}
        with self.database.connect() as connection:
            for username, token in self.tokens.items():
                connection.execute(
                    "INSERT INTO sessions(token_hash,username,expires_at,created_at) VALUES(?,?,?,?)",
                    (digest(token), username, time.time() + 3600, time.time()),
                )

    def tearDown(self):
        self.service.stop()
        self.start_patch.stop()
        self.client.close()
        self.temporary.cleanup()

    def headers(self, username="user-alpha"):
        return {"Authorization": f"Bearer {self.tokens[username]}"}

    def lecture(self, *, username="user-alpha", finalized=True, text="빛을 이용해 양분을 만드는 과정이다."):
        lecture_id, chunk_id, segment_id = (str(uuid.uuid4()) for _ in range(3))
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO lectures(id,username,title,language,created_at,recording_finalized) "
                "VALUES(?,?,'test-title-not-sent','ko','2026-09-04T00:00:00Z',?)",
                (lecture_id, username, int(finalized)),
            )
            connection.execute(
                "INSERT INTO chunks(lecture_id,chunk_id,payload_hash,start_seconds,status) "
                "VALUES(?,?,'test-only-hash',0,'done')", (lecture_id, chunk_id),
            )
            if text is not None:
                connection.execute(
                    "INSERT INTO segments(id,lecture_id,chunk_id,start,end,text) VALUES(?,?,?,0,1,?)",
                    (segment_id, lecture_id, chunk_id, text),
                )
        return lecture_id, segment_id

    def post(self, lecture_id, username="user-alpha"):
        return self.client.post(f"/lectures/{lecture_id}/summary", headers=self.headers(username))

    def get(self, lecture_id, username="user-alpha"):
        return self.client.get(f"/lectures/{lecture_id}/summary", headers=self.headers(username))

    def row(self, lecture_id):
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM lecture_summaries WHERE lecture_id=?", (lecture_id,)).fetchone()
            return dict(row) if row is not None else None

    def assert_queued(self, lecture_id, username="user-alpha"):
        response = self.post(lecture_id, username)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["summary"]["status"], "queued")
        return self.row(lecture_id)

    def transcript_snapshot(self, lecture_id):
        with self.database.connect() as connection:
            return {
                "segments": [dict(row) for row in connection.execute(
                    "SELECT * FROM segments WHERE lecture_id=? ORDER BY id", (lecture_id,))],
                "correction": [dict(row) for row in connection.execute(
                    "SELECT * FROM transcript_corrections WHERE lecture_id=?", (lecture_id,))],
            }

    def test_get_and_post_are_owner_scoped_before_and_after_completion(self):
        lecture_id, segment_id = self.lecture()
        initial = self.get(lecture_id)
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.json(), {"configured": True, "model": self.settings.summary_model, "summary": None})
        for method in (self.get, self.post):
            self.assertEqual(method(lecture_id, "user-beta").status_code, 404)
            self.assertEqual(method(str(uuid.uuid4()), "user-beta").status_code, 404)
        self.assert_queued(lecture_id)
        self.assertTrue(self.service.process_next())
        self.assertEqual(self.get(lecture_id).json()["summary"]["document"]["overview_source_ids"], [segment_id])
        for method in (self.get, self.post):
            self.assertEqual(method(lecture_id, "user-beta").status_code, 404)
        self.assertEqual(len(self.engine.calls), 1)

    def test_unfinished_empty_blank_and_oversized_sources_never_queue(self):
        for options in ({"finalized": False}, {"text": None}, {"text": "  "}, {"text": "가" * 24001}):
            with self.subTest(options=list(options)):
                lecture_id, _ = self.lecture(**options)
                response = self.post(lecture_id)
                self.assertEqual(response.status_code, 413 if len(options.get("text") or "") > 24000 else 409)
                self.assertIsNone(self.row(lecture_id))
        lecture_id, _ = self.lecture(text="가" * 80)
        self.service.settings = replace(self.settings, summary_max_source_chars=50)
        self.assertEqual(self.post(lecture_id).status_code, 413)
        self.start.assert_not_called()
        self.assertEqual(self.engine.calls, [])

    def test_unconfigured_service_reports_status_and_never_queues(self):
        self.engine.configured = False
        lecture_id, _ = self.lecture()
        self.assertFalse(self.get(lecture_id).json()["configured"])
        self.assertEqual(self.post(lecture_id).status_code, 503)
        self.assertIsNone(self.row(lecture_id))
        self.assertFalse(self.service.process_next())

    def test_completed_and_inflight_repeated_posts_are_idempotent(self):
        lecture_id, _ = self.lecture()
        queued = self.assert_queued(lecture_id)
        self.assertEqual(self.post(lecture_id).json()["summary"]["status"], "queued")
        self.assertEqual(self.row(lecture_id)["job_id"], queued["job_id"])
        observed = []
        self.engine.during = lambda: observed.append(self.post(lecture_id).json()["summary"]["status"])
        self.assertTrue(self.service.process_next())
        completed = self.get(lecture_id).json()["summary"]
        self.assertEqual(observed, ["processing"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.post(lecture_id).json()["summary"], completed)
        self.assertEqual(self.row(lecture_id)["job_id"], queued["job_id"])
        self.assertEqual(self.row(lecture_id)["attempts"], 1)
        self.assertFalse(self.service.process_next())
        self.assertEqual(len(self.engine.calls), 1)

    def test_summary_never_mutates_raw_or_corrected_transcripts_and_sends_only_raw_snapshot(self):
        lecture_id, segment_id = self.lecture()
        with self.database.connect() as connection:
            raw = self.service.raw_segments(connection, lecture_id)
            connection.execute(
                "INSERT INTO transcript_corrections(lecture_id,raw_revision,status,model,corrected_text,"
                "corrected_segments,uncertain_terms,created_at,updated_at,completed_at) "
                "VALUES(?,?,'completed','test-corrector','교정 버전은 별개입니다.',?,'[]','now','now','now')",
                (lecture_id, self.service.revision(raw), json.dumps([
                    {"id": segment_id, "start": 0, "end": 1, "text": "교정 버전은 별개입니다."}], ensure_ascii=False)),
            )
        before = self.transcript_snapshot(lecture_id)
        self.assert_queued(lecture_id)
        self.service.process_next()
        self.assertEqual(self.get(lecture_id).json()["summary"]["status"], "completed")
        self.assertEqual(self.transcript_snapshot(lecture_id), before)
        self.assertEqual(self.engine.calls, [{"language": "ko", "segments": raw}])
        self.assertNotIn("교정 버전", str(self.engine.calls))
        self.assertNotIn("test-title-not-sent", str(self.engine.calls))

    def test_provider_failure_is_redacted_and_explicit_post_retries_a_new_job(self):
        lecture_id, _ = self.lecture()
        before = self.transcript_snapshot(lecture_id)
        self.engine.error = RuntimeError("secret-provider-key provider-private-body raw-account-name")
        first = self.assert_queued(lecture_id)
        self.service.process_next()
        failed = self.get(lecture_id).json()["summary"]
        self.assertEqual((failed["status"], failed["error_code"], failed["document"]), ("failed", "summary_failed", None))
        self.assertNotIn("secret-provider", str(self.row(lecture_id)))
        self.assertFalse(self.service.process_next())
        self.engine.error = None
        retried = self.assert_queued(lecture_id)
        self.assertNotEqual(retried["job_id"], first["job_id"])
        self.assertEqual(retried["attempts"], 0)
        self.service.process_next()
        self.assertEqual(self.get(lecture_id).json()["summary"]["status"], "completed")
        self.assertEqual(len(self.engine.calls), 2)
        self.assertEqual(self.transcript_snapshot(lecture_id), before)

    def test_worker_rejects_invalid_citations_without_persisting_document(self):
        lecture_id, _ = self.lecture()
        self.engine.invalid = True
        self.assert_queued(lecture_id)
        self.service.process_next()
        self.assertEqual(self.row(lecture_id)["status"], "failed")
        self.assertIsNone(self.row(lecture_id)["summary_json"])
        self.assertIsNone(self.get(lecture_id).json()["summary"]["document"])

    def test_each_owner_has_one_active_job_but_different_owners_are_independent(self):
        first, _ = self.lecture()
        second, _ = self.lecture()
        other, _ = self.lecture(username="user-beta")
        self.assert_queued(first)
        self.assertEqual(self.post(second).status_code, 409)
        self.assert_queued(other, "user-beta")
        self.assertTrue(self.service.process_next())
        self.assertTrue(self.service.process_next())
        self.assertFalse(self.service.process_next())
        self.assertEqual(self.get(other, "user-beta").json()["summary"]["status"], "completed")
        self.assert_queued(second)

    def test_hourly_limit_applies_to_new_jobs_but_not_completed_cache_hits(self):
        completed = []
        for _ in range(6):
            lecture_id, _ = self.lecture()
            self.assert_queued(lecture_id)
            self.service.process_next()
            completed.append(lecture_id)
        limited, _ = self.lecture()
        self.assertEqual(self.post(limited).status_code, 429)
        self.assertIsNone(self.row(limited))
        self.assertEqual(self.post(completed[0]).json()["summary"]["status"], "completed")
        self.assertEqual(len(self.engine.calls), 6)

    def test_restart_recovers_processing_jobs_without_extending_attempt_budget(self):
        lecture_id, _ = self.lecture()
        queued = self.assert_queued(lecture_id)
        with self.database.connect() as connection:
            connection.execute("UPDATE lecture_summaries SET status='processing',attempts=1 WHERE lecture_id=?", (lecture_id,))
        self.service.recover()
        self.assertEqual(self.row(lecture_id)["status"], "queued")
        self.assertEqual(self.row(lecture_id)["job_id"], queued["job_id"])
        self.service.process_next()
        self.assertEqual(self.row(lecture_id)["status"], "completed")
        self.assertEqual(self.row(lecture_id)["attempts"], 2)
        failed, _ = self.lecture()
        self.assert_queued(failed)
        with self.database.connect() as connection:
            connection.execute("UPDATE lecture_summaries SET status='processing',attempts=3 WHERE lecture_id=?", (failed,))
        self.service.recover()
        self.assertEqual((self.row(failed)["status"], self.row(failed)["error_code"]), ("failed", "interrupted"))
        self.assertFalse(self.service.process_next())

    def test_shutdown_drops_returning_result_and_allows_bounded_restart_retry(self):
        lecture_id, _ = self.lecture()
        self.assert_queued(lecture_id)
        self.engine.during = self.service.request_shutdown
        self.service.process_next()
        self.assertEqual(self.row(lecture_id)["status"], "queued")
        self.assertIsNone(self.row(lecture_id)["summary_json"])
        self.assertFalse(self.service.process_next())
        self.service.shutdown.clear()
        self.service.recover()
        self.engine.during = None
        self.assertTrue(self.service.process_next())
        self.assertEqual(self.row(lecture_id)["status"], "completed")
        self.assertEqual(len(self.engine.calls), 2)

    def test_queued_lecture_deletion_cancels_before_any_provider_call(self):
        lecture_id, _ = self.lecture()
        self.assert_queued(lecture_id)
        response = self.client.delete(f"/lectures/{lecture_id}", headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.row(lecture_id))
        self.assertEqual(self.get(lecture_id).status_code, 404)
        self.assertFalse(self.service.process_next())
        self.assertEqual(self.engine.calls, [])

    def test_processing_summary_blocks_deletion_until_completion(self):
        lecture_id, _ = self.lecture()
        self.assert_queued(lecture_id)
        deletion = []
        self.engine.during = lambda: deletion.append(self.client.delete(
            f"/lectures/{lecture_id}", headers=self.headers()))
        self.service.process_next()
        self.assertEqual(deletion[0].status_code, 409)
        self.assertEqual(self.row(lecture_id)["status"], "completed")
        self.assertEqual(self.client.delete(f"/lectures/{lecture_id}", headers=self.headers()).status_code, 200)
        self.assertIsNone(self.row(lecture_id))

    def test_source_change_during_summary_discards_result_and_preserves_changed_raw(self):
        lecture_id, segment_id = self.lecture()
        self.assert_queued(lecture_id)
        def change_source():
            with self.database.connect() as connection:
                connection.execute("UPDATE segments SET text='변경된 원문은 그대로 남아야 한다.' WHERE id=?", (segment_id,))
        self.engine.during = change_source
        self.service.process_next()
        row = self.row(lecture_id)
        self.assertEqual((row["status"], row["error_code"], row["summary_json"]), ("failed", "source_changed", None))
        self.assertEqual(self.transcript_snapshot(lecture_id)["segments"][0]["text"], "변경된 원문은 그대로 남아야 한다.")

    def test_an_old_owner_or_removed_job_cannot_store_a_returning_result(self):
        for change in ("owner", "job", "deleting"):
            with self.subTest(change=change):
                lecture_id, _ = self.lecture()
                self.assert_queued(lecture_id)
                def detach_job():
                    with self.database.connect() as connection:
                        if change == "owner":
                            connection.execute("UPDATE lectures SET username='user-beta' WHERE id=?", (lecture_id,))
                        elif change == "job":
                            connection.execute("DELETE FROM lecture_summaries WHERE lecture_id=?", (lecture_id,))
                        else:
                            connection.execute("UPDATE lectures SET deleting=1 WHERE id=?", (lecture_id,))
                self.engine.during = detach_job
                self.service.process_next()
                row = self.row(lecture_id)
                if change == "job":
                    self.assertIsNone(row)
                else:
                    self.assertIsNone(row["summary_json"])
                    self.assertNotEqual(row["status"], "completed")
                    self.assertEqual(self.get(lecture_id).status_code, 404)
                if change == "owner":
                    self.assertIsNone(self.get(lecture_id, "user-beta").json()["summary"]["document"])

    def test_stale_source_or_model_is_rejected_before_provider_work(self):
        for change in ("source", "model", "finalized"):
            with self.subTest(change=change):
                lecture_id, segment_id = self.lecture()
                self.assert_queued(lecture_id)
                with self.database.connect() as connection:
                    if change == "source":
                        connection.execute("UPDATE segments SET text='새 원문' WHERE id=?", (segment_id,))
                    elif change == "model":
                        connection.execute("UPDATE lecture_summaries SET model='previous-test-model' WHERE lecture_id=?", (lecture_id,))
                    else:
                        connection.execute("UPDATE lectures SET recording_finalized=0 WHERE id=?", (lecture_id,))
                self.service.process_next()
                self.assertEqual(self.row(lecture_id)["status"], "failed")
                self.assertIsNone(self.row(lecture_id)["summary_json"])
        self.assertEqual(self.engine.calls, [])

    def test_corrupted_or_stale_saved_summary_is_hidden_and_explicitly_regenerated(self):
        lecture_id, segment_id = self.lecture()
        self.assert_queued(lecture_id)
        self.service.process_next()
        with self.database.connect() as connection:
            connection.execute("UPDATE lecture_summaries SET summary_json=? WHERE lecture_id=?",
                               (json.dumps({"overview": "broken"}), lecture_id))
        self.assertEqual(self.get(lecture_id).json()["summary"]["error_code"], "invalid_saved_summary")
        self.assertIsNone(self.get(lecture_id).json()["summary"]["document"])
        self.assert_queued(lecture_id)
        self.service.process_next()
        self.assertEqual(self.get(lecture_id).json()["summary"]["status"], "completed")
        with self.database.connect() as connection:
            connection.execute("UPDATE segments SET text=text || ' 원문이 추가되었다.' WHERE id=?", (segment_id,))
        self.assertEqual(self.get(lecture_id).json()["summary"]["status"], "failed")
        self.assert_queued(lecture_id)

    def test_missing_revoked_and_expired_sessions_cannot_read_or_create_summaries(self):
        lecture_id, _ = self.lecture()
        for method in (self.client.get, self.client.post):
            self.assertEqual(method(f"/lectures/{lecture_id}/summary").status_code, 401)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE username='user-alpha'")
            connection.execute("UPDATE sessions SET expires_at=? WHERE username='user-beta'", (time.time() - 1,))
        for username in self.tokens:
            for method in (self.get, self.post):
                self.assertEqual(method(lecture_id, username).status_code, 401)
        self.assertIsNone(self.row(lecture_id))
        self.assertEqual(self.engine.calls, [])


    def test_missing_gateway_key_fails_recovered_jobs_without_touching_terminal_results(self):
        completed, _ = self.lecture()
        self.assert_queued(completed)
        self.service.process_next()
        completed_before = self.row(completed)
        failed, _ = self.lecture()
        self.engine.error = RuntimeError("test-only-failure")
        self.assert_queued(failed)
        self.service.process_next()
        failed_before = self.row(failed)
        self.engine.error = None
        processing, _ = self.lecture()
        first = self.assert_queued(processing)
        queued, _ = self.lecture(username="user-beta")
        self.assert_queued(queued, "user-beta")
        with self.database.connect() as connection:
            connection.execute("UPDATE lecture_summaries SET status='processing',attempts=2 WHERE lecture_id=?", (processing,))
        calls_before = len(self.engine.calls)
        self.engine.configured = False
        self.service.recover()
        for lecture_id in (processing, queued):
            row = self.row(lecture_id)
            self.assertEqual((row["status"], row["error_code"]), ("failed", "not_configured"))
            self.assertIsNone(row["summary_json"])
        self.assertEqual(self.row(completed), completed_before)
        self.assertEqual(self.row(failed), failed_before)
        self.assertFalse(self.service.process_next())
        self.assertEqual(len(self.engine.calls), calls_before)
        self.assertEqual(self.post(processing).status_code, 503)
        self.engine.configured = True
        renewed = self.assert_queued(processing)
        self.assertNotEqual(renewed["job_id"], first["job_id"])
        self.service.process_next()
        self.assertEqual(self.row(processing)["status"], "completed")

    def test_operator_access_pause_blocks_api_and_worker_until_resumed(self):
        lecture_id, _ = self.lecture()
        self.assert_queued(lecture_id)
        with self.database.connect() as connection:
            connection.execute("UPDATE operational_state SET access_enabled=0 WHERE singleton=1")
        self.assertEqual(self.get(lecture_id).status_code, 503)
        self.assertEqual(self.post(lecture_id).status_code, 503)
        self.assertFalse(self.service.process_next())
        self.assertEqual(self.engine.calls, [])
        with self.database.connect() as connection:
            connection.execute("UPDATE operational_state SET access_enabled=1 WHERE singleton=1")
        self.assertTrue(self.service.process_next())
        self.assertEqual(self.get(lecture_id).json()["summary"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
