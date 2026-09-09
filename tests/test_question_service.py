"""Question worker failure/race tests; never invokes a live provider."""
from __future__ import annotations

import json
import sqlite3
import threading
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from server.postprocessor import PostprocessingError
from server.question_answerer import QuestionAnsweringError
from tests.test_question_api import QuestionFixture


class QuestionServiceTests(QuestionFixture, unittest.TestCase):
    def test_raw_manual_and_other_ai_outputs_remain_unchanged_and_private_metadata_is_not_sent(self):
        lecture_id, segment_id = self.lecture()
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lecture_manual_state VALUES (?, ?,1)", (lecture_id, "a" * 64))
            connection.execute("INSERT INTO lecture_manual_edits VALUES (?,?,'personal edited text','now','now')", (segment_id, lecture_id))
            connection.execute("INSERT INTO lecture_metadata VALUES (?,'private display title','private course','private semester',1,'now')", (lecture_id,))
            tables = ("segments", "chunks", "lecture_manual_state", "lecture_manual_edits", "lecture_metadata",
                      "transcript_corrections", "lecture_summaries", "lecture_translations")
            before = {t: [tuple(r) for r in connection.execute(f"SELECT * FROM {t}")] for t in tables}
        job = self.queued(lecture_id)
        self.service.process_next()
        self.assertEqual(self.get(lecture_id, job["id"]).json()["question"]["status"], "completed")
        with self.database.connect() as connection:
            self.assertEqual({t: [tuple(r) for r in connection.execute(f"SELECT * FROM {t}")] for t in tables}, before)
        self.assertEqual(set(self.engine.calls[0]), {"question", "segments"})
        self.assertEqual(set(self.engine.calls[0]["segments"][0]), {"id", "start", "end", "text"})
        self.assertNotIn("private", json.dumps(self.engine.calls))

    def test_get_never_calls_engine_and_saved_document_is_revalidated_against_sealed_source(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        for _ in range(3):
            self.get(lecture_id)
            self.get(lecture_id, job["id"])
        self.assertEqual(self.engine.calls, [])
        self.service.process_next()
        for _ in range(3):
            self.get(lecture_id)
            self.get(lecture_id, job["id"])
        self.assertEqual(len(self.engine.calls), 1)
        with self.database.connect() as connection:
            connection.execute("UPDATE lecture_questions SET evidence_sha256=? WHERE id=?", ("0" * 64, job["id"]))
        result = self.get(lecture_id, job["id"]).json()["question"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "invalid_saved_answer")
        self.assertIsNone(result["document"])
        self.assertEqual(self.row(job["id"])["status"], "completed")  # GET has no writes.
        self.assertEqual(len(self.engine.calls), 1)

    def test_empty_local_selection_is_explicit_insufficient_evidence(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id, question="스피노사 철학은?")
        self.assertEqual(job["scope"], "none")
        self.assertEqual(job["selected_count"], 0)
        self.assertEqual(job["total_segments"], 1)
        self.service.process_next()
        result = self.get(lecture_id, job["id"]).json()["question"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["document"], {"answerability": "insufficient_evidence", "paragraphs": []})
        self.assertEqual(self.engine.calls[0]["segments"], [])

    def test_completed_result_is_hidden_if_current_source_changes_without_any_call_or_write(self):
        lecture_id, segment_id = self.lecture()
        job = self.queued(lecture_id)
        self.service.process_next()
        with self.database.connect() as connection:
            connection.execute("UPDATE segments SET text='새 원문' WHERE id=?", (segment_id,))
        result = self.get(lecture_id, job["id"]).json()["question"]
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["document"])
        self.assertEqual(self.row(job["id"])["status"], "completed")
        self.assertEqual(len(self.engine.calls), 1)

    def test_selected_ids_tamper_cannot_point_at_another_lecture(self):
        lecture_id, _ = self.lecture()
        _, foreign_segment = self.lecture(username="user-beta", text="private other owner")
        job = self.queued(lecture_id)
        with self.database.connect() as connection:
            connection.execute("UPDATE lecture_questions SET selected_ids_json=? WHERE id=?", (json.dumps([foreign_segment]), job["id"]))
        self.assertTrue(self.service.process_next())
        self.assertEqual(self.row(job["id"])["error_code"], "source_changed")
        self.assertEqual(self.engine.calls, [])

    def test_restart_never_requeues_a_claimed_uuid_but_unclaimed_job_can_run_once(self):
        lecture_id, _ = self.lecture()
        first = self.queued(lecture_id)
        second = self.queued(lecture_id)
        with self.database.connect() as connection:
            connection.execute("UPDATE lecture_questions SET status='processing',attempts=1 WHERE id=?", (first["id"],))
        self.service.recover()
        self.assertEqual(self.row(first["id"])["status"], "failed")
        self.assertEqual(self.row(first["id"])["error_code"], "interrupted_unknown")
        self.assertEqual(self.post(lecture_id, identifier=first["id"]).json()["question"]["status"], "failed")
        self.assertEqual(self.row(second["id"])["status"], "queued")
        self.service.process_next()
        self.service.recover()
        self.assertFalse(self.service.process_next())
        self.assertEqual(self.row(second["id"])["status"], "completed")
        self.assertEqual(len(self.engine.calls), 1)

    def test_recover_without_key_fails_unclaimed_and_does_not_change_completed_history(self):
        lecture_id, _ = self.lecture()
        done = self.queued(lecture_id)
        self.service.process_next()
        queued = self.queued(lecture_id)
        self.engine.configured = False
        self.service.recover()
        self.assertEqual(self.row(queued["id"])["error_code"], "not_configured")
        self.assertEqual(self.row(done["id"])["status"], "completed")
        self.engine.configured = True
        self.assertFalse(self.service.process_next())

    def test_recover_does_not_revive_jobs_of_trashed_or_changed_owner_lectures(self):
        for field, value in (("trashed_at", "now"), ("deleting", 1), ("username", "user-beta"), ("recording_finalized", 0)):
            with self.subTest(field=field):
                lecture_id, _ = self.lecture()
                job = self.queued(lecture_id)
                with self.database.connect() as connection:
                    connection.execute(f"UPDATE lectures SET {field}=? WHERE id=?", (value, lecture_id))
                self.service.recover()
                self.assertEqual(self.row(job["id"])["status"], "failed")
                self.assertFalse(self.service.process_next())
        self.assertEqual(self.engine.calls, [])

    def test_shutdown_mid_call_fails_unknown_without_rebilling_on_same_request(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        def during(interrupted):
            self.service.request_shutdown()
            self.assertTrue(interrupted())
        self.engine.during = during
        self.service.process_next()
        self.assertEqual(self.row(job["id"])["error_code"], "interrupted_unknown")
        self.service.shutdown.clear()
        self.service.recover()
        self.assertEqual(self.post(lecture_id, identifier=job["id"]).json()["question"]["status"], "failed")
        self.assertFalse(self.service.process_next())
        self.assertEqual(len(self.engine.calls), 1)

    def test_provider_exception_is_redacted_terminal_and_requires_a_new_explicit_uuid(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        self.engine.error = RuntimeError("fake-private-key fake-provider-body")
        self.service.process_next()
        result = self.get(lecture_id, job["id"]).json()["question"]
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("fake-private", json.dumps(self.row(job["id"])))
        self.assertEqual(self.post(lecture_id, identifier=job["id"]).json()["question"], result)
        self.service.recover()
        self.assertFalse(self.service.process_next())
        self.engine.error = None
        retry = self.queued(lecture_id)
        self.service.process_next()
        self.assertEqual(self.row(retry["id"])["status"], "completed")
        self.assertEqual(len(self.engine.calls), 2)

    def test_invalid_provider_document_is_never_saved(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        self.engine.invalid = True
        self.service.process_next()
        row = self.row(job["id"])
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["document_json"])
        self.assertFalse(self.service.process_next())
        self.assertEqual(len(self.engine.calls), 1)

    def test_typed_provider_failure_is_fixed_terminal_and_never_replayed(self):
        for code in ("authentication_failed", "credit_exhausted", "rate_limited",
                     "response_truncated", "model_refused", "unsupported_claim", "synthetic-private-code"):
            with self.subTest(code=code):
                lecture_id, _ = self.lecture()
                job = self.queued(lecture_id)
                previous_calls = len(self.engine.calls)
                self.engine.error = PostprocessingError(code, "synthetic-private-provider-body", retryable=True)
                self.service.process_next()
                result = self.get(lecture_id, job["id"]).json()["question"]
                expected = QuestionAnsweringError(code)
                self.assertEqual((result["status"], result["error_code"], result["error"], result["document"]),
                                 ("failed", expected.code, str(expected), None))
                self.assertNotIn("synthetic-private", json.dumps(self.row(job["id"])))
                self.assertEqual(self.post(lecture_id, identifier=job["id"]).json()["question"], result)
                self.service.recover()
                self.assertFalse(self.service.process_next())
                self.assertEqual(len(self.engine.calls), previous_calls + 1)

    def test_source_change_before_claim_prevents_call_and_during_call_discards_answer(self):
        for during_call in (False, True):
            with self.subTest(during_call=during_call):
                lecture_id, segment_id = self.lecture()
                job = self.queued(lecture_id)
                def change(_=None):
                    with self.database.connect() as connection:
                        connection.execute("UPDATE segments SET text='원문이 변경되었습니다.' WHERE id=?", (segment_id,))
                if during_call:
                    self.engine.during = change
                else:
                    change()
                self.service.process_next()
                self.assertEqual(self.row(job["id"])["error_code"], "source_changed")
                self.assertIsNone(self.row(job["id"])["document_json"])
        self.assertEqual(len(self.engine.calls), 1)

    def test_owner_change_mid_call_cannot_publish_answer_to_the_new_owner(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        def during(interrupted):
            with self.database.connect() as connection:
                connection.execute("UPDATE lectures SET username='user-beta' WHERE id=?", (lecture_id,))
            self.assertTrue(interrupted())
        self.engine.during = during
        self.service.process_next()
        self.assertIsNone(self.row(job["id"])["document_json"])
        self.assertEqual(self.get(lecture_id, job["id"]).status_code, 404)
        self.assertEqual(self.get(lecture_id, job["id"], username="user-beta").status_code, 404)
        self.assertEqual(self.get(lecture_id, username="user-beta").json()["questions"], [])

    def test_processing_cancel_stays_busy_until_worker_settles_and_trash_is_blocked(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        def during(interrupted):
            cancelled = self.cancel(lecture_id, job["id"]).json()["question"]
            self.assertEqual(cancelled["status"], "processing")
            self.assertTrue(cancelled["cancel_requested"])
            self.assertTrue(interrupted())
            response = self.client.post(f"/lectures/{lecture_id}/trash", headers=self.headers())
            self.assertEqual(response.status_code, 409)
        self.engine.during = during
        self.service.process_next()
        self.assertEqual(self.row(job["id"])["status"], "cancelled")
        self.assertIsNone(self.row(job["id"])["document_json"])
        self.assertEqual(self.client.post(f"/lectures/{lecture_id}/trash", headers=self.headers()).status_code, 200)

    def test_access_pause_during_call_discards_result_and_does_not_requeue(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        def during(interrupted):
            with self.database.connect() as connection:
                connection.execute("UPDATE operational_state SET access_enabled=0")
            self.assertTrue(interrupted())
        self.engine.during = during
        self.service.process_next()
        self.assertEqual(self.row(job["id"])["error_code"], "interrupted_unknown")
        self.assertIsNone(self.row(job["id"])["document_json"])

    def test_provider_runs_outside_database_lock_and_worker_claims_once(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        def during(_):
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE lecture_questions SET updated_at=updated_at WHERE id=?", (job["id"],))
            self.assertFalse(self.service.process_next())
        self.engine.during = during
        self.service.process_next()
        self.assertEqual(len(self.engine.calls), 1)

    def test_database_enforces_only_one_processing_question_per_owner(self):
        lecture_id, _ = self.lecture()
        first, second = self.queued(lecture_id), self.queued(lecture_id)
        with self.database.connect() as connection:
            connection.execute("UPDATE lecture_questions SET status='processing',attempts=1 WHERE id=?", (first["id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE lecture_questions SET status='processing',attempts=1 WHERE id=?", (second["id"],))
        self.assertFalse(self.service.process_next())
        self.assertEqual(self.engine.calls, [])

    def fault_connect(self, *, completed=False, claimed=False, after_commit=False):
        original = self.database.connect
        raised = False
        @contextmanager
        def replacement():
            nonlocal raised
            matched = False
            with original() as connection:
                class Proxy:
                    def execute(inner, sql, *args, **kwargs):
                        nonlocal raised, matched
                        match = ((completed and "UPDATE lecture_questions SET status='completed'" in sql)
                                 or (claimed and "UPDATE lecture_questions SET status='processing'" in sql))
                        if match and not raised:
                            matched = True
                            if not after_commit:
                                raised = True
                                raise sqlite3.OperationalError("synthetic-save-failure")
                        return connection.execute(sql, *args, **kwargs)
                yield Proxy()
            if matched and not raised:
                raised = True
                raise sqlite3.OperationalError("synthetic-commit-ack-loss")
        return replacement

    def test_failed_result_save_is_terminal_without_a_second_engine_call(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        with patch.object(self.database, "connect", self.fault_connect(completed=True)):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.process_next()
        self.assertEqual(self.row(job["id"])["status"], "processing")
        self.assertFalse(self.service.process_next())
        self.assertEqual(self.row(job["id"])["error_code"], "question_save_failed")
        self.assertEqual(len(self.engine.calls), 1)

    def test_lost_commit_ack_preserves_saved_completed_answer_without_rebilling(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        with patch.object(self.database, "connect", self.fault_connect(completed=True, after_commit=True)):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.process_next()
        self.assertEqual(self.row(job["id"])["status"], "completed")
        self.assertFalse(self.service.process_next())
        self.assertEqual(self.get(lecture_id, job["id"]).json()["question"]["status"], "completed")
        self.assertEqual(len(self.engine.calls), 1)

    def test_lost_claim_commit_ack_is_failed_without_sending_the_provider(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        with patch.object(self.database, "connect", self.fault_connect(claimed=True, after_commit=True)):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.process_next()
        self.assertEqual(self.row(job["id"])["status"], "processing")
        self.assertFalse(self.service.process_next())
        self.assertEqual(self.row(job["id"])["status"], "failed")
        self.assertEqual(self.engine.calls, [])

    def test_engine_mutation_does_not_expand_the_validator_evidence(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        def answer(question, segments, interrupted):
            segments[0]["text"] = "invented 982173"
            return {"answerability": "answered", "paragraphs": [{"text": "invented 982173", "source_ids": [segments[0]["id"]]}]}
        with patch.object(self.engine, "answer", answer):
            self.service.process_next()
        self.assertEqual(self.row(job["id"])["status"], "failed")

    def test_start_failure_and_shutdown_are_safe_and_engine_closes_only_after_thread_stops(self):
        self.start_patch.stop()
        with patch("server.question_service.threading.Thread.start", side_effect=RuntimeError("fake")):
            with self.assertRaises(RuntimeError):
                self.service.start()
        self.assertIsNone(self.service.thread)
        self.assertTrue(self.service.stop(timeout=0))
        self.assertTrue(self.engine.closed)

    def test_shutdown_timeout_does_not_close_an_engine_while_its_thread_is_running(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        entered, release = threading.Event(), threading.Event()
        def during(_):
            entered.set()
            release.wait(3)
        self.engine.during = during
        self.start_patch.stop()
        self.service.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertFalse(self.service.stop(timeout=0))
            self.assertFalse(self.engine.closed)
        finally:
            release.set()
            self.service.stop(timeout=3)
        self.assertTrue(self.engine.closed)
        self.assertEqual(self.row(job["id"])["status"], "failed")
        self.assertEqual(len(self.engine.calls), 1)


if __name__ == "__main__":
    unittest.main()
