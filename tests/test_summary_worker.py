"""Summary worker DB-failure regressions using temporary rows and a local fake."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.db import Database
from server.summary_service import SummaryService
from server.summarizer import LectureSummary


class FakeSummaryEngine:
    configured = True

    def __init__(self):
        self.calls = []

    def summarize(self, *, language, segments, interrupted):
        self.calls.append(segments[0]["id"])
        identifier = segments[0]["id"]
        text = "빛을 이용해 양분을 만드는 과정이다."
        return LectureSummary(
            overview=text, overview_source_ids=[identifier],
            sections=[{"heading": "핵심 개념", "bullets": [{"text": text, "source_ids": [identifier]}]}],
            review_questions=[],
        )


class ControlledWake:
    """Advance the worker synchronously without a real thread or sleep."""

    def __init__(self, callback):
        self.callback = callback
        self.waits = 0

    def wait(self, timeout):
        self.waits += 1
        if self.waits > 5:
            raise AssertionError("Worker did not settle after temporary DB recovery")
        self.callback(self.waits)

    def clear(self):
        pass


class SummaryWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "temporary.sqlite3", ("user-alpha", "user-beta"))
        self.database.initialize()
        self.original_connect = self.database.connect
        self.engine = FakeSummaryEngine()
        self.service = SummaryService(SimpleNamespace(summary_model="test-summary"), self.database, self.engine, None)
        self.service.raw_segments = lambda connection, lecture_id: [dict(row) for row in connection.execute(
            "SELECT id,start,end,text FROM segments WHERE lecture_id=? ORDER BY start,end,id", (lecture_id,)
        )]
        self.service.revision = lambda segments: hashlib.sha256(json.dumps(segments, sort_keys=True).encode()).hexdigest()

    def queued(self, username, created_at):
        lecture_id, segment_id, chunk_id = [str(uuid.uuid4()) for _ in range(3)]
        with self.original_connect() as connection:
            connection.execute(
                "INSERT INTO lectures(id,username,title,language,created_at,recording_finalized) "
                "VALUES(?,?,'test-title','ko',?,1)", (lecture_id, username, created_at)
            )
            connection.execute(
                "INSERT INTO chunks(lecture_id,chunk_id,payload_hash,start_seconds,status) "
                "VALUES(?,?,'test-hash',0,'done')", (lecture_id, chunk_id)
            )
            connection.execute(
                "INSERT INTO segments(id,lecture_id,chunk_id,start,end,text) VALUES(?,?,?,0,1,?)",
                (segment_id, lecture_id, chunk_id, "빛을 이용해 양분을 만드는 과정이다.")
            )
            revision = self.service.revision(self.service.raw_segments(connection, lecture_id))
            connection.execute(
                "INSERT INTO lecture_summaries(lecture_id,job_id,raw_revision,status,model,created_at,updated_at) "
                "VALUES(?,?,?,'queued','test-summary',?,?)",
                (lecture_id, str(uuid.uuid4()), revision, created_at, created_at)
            )
        return lecture_id

    def row(self, lecture_id):
        with self.original_connect() as connection:
            return dict(connection.execute("SELECT * FROM lecture_summaries WHERE lecture_id=?", (lecture_id,)).fetchone())

    def run_with_db_failures(self, *, fail_before=(), fail_after=(), observations):
        calls = 0

        @contextmanager
        def faulty_connect():
            nonlocal calls
            calls += 1
            call = calls
            if call in fail_before:
                raise sqlite3.OperationalError("test-private-diagnostic-not-for-persistence")
            with self.original_connect() as connection:
                yield connection
            if call in fail_after:
                raise sqlite3.OperationalError("test-private-commit-response-lost")

        self.service.wake = ControlledWake(observations)
        with patch.object(self.database, "connect", faulty_connect):
            self.service._run()

    def test_failed_result_save_releases_owner_without_automatic_provider_retry(self):
        first = self.queued("user-alpha", "2026-09-01")
        second = self.queued("user-beta", "2026-09-02")

        def observe(wait):
            if wait == 1:
                self.assertEqual(self.row(first)["status"], "processing")
                self.assertEqual(len(self.engine.calls), 1)
            else:
                self.assertEqual(self.row(first)["status"], "failed")
                self.assertEqual(self.row(second)["status"], "completed")
                self.service.shutdown.set()

        self.run_with_db_failures(fail_before={2}, observations=observe)
        failed = self.row(first)
        self.assertEqual(failed["error_code"], "summary_save_failed")
        self.assertIsNone(failed["summary_json"])
        self.assertEqual(failed["attempts"], 1)
        self.assertNotIn("test-private", str(failed))
        self.assertEqual(len(self.engine.calls), 2)
        self.service.shutdown.clear()
        self.service.recover()
        self.assertFalse(self.service.process_next())
        self.assertEqual(len(self.engine.calls), 2)

    def test_cleanup_database_failure_blocks_other_claims_until_cleanup_succeeds(self):
        first = self.queued("user-alpha", "2026-09-01")
        second = self.queued("user-beta", "2026-09-02")

        def observe(wait):
            if wait <= 2:
                self.assertEqual(self.row(first)["status"], "processing")
                self.assertEqual(self.row(second)["status"], "queued")
                self.assertEqual(len(self.engine.calls), 1)
            else:
                self.assertEqual(self.row(first)["status"], "failed")
                self.assertEqual(self.row(second)["status"], "completed")
                self.service.shutdown.set()

        self.run_with_db_failures(fail_before={2, 3}, observations=observe)
        self.assertEqual(self.service.wake.waits, 3)
        self.assertEqual(len(self.engine.calls), 2)

    def test_lost_commit_ack_preserves_completed_document_and_never_rebills(self):
        first = self.queued("user-alpha", "2026-09-01")
        second = self.queued("user-beta", "2026-09-02")
        committed = []

        def observe(wait):
            if wait == 1:
                committed.append(self.row(first))
                self.assertEqual(committed[0]["status"], "completed")
            else:
                self.assertEqual(self.row(first), committed[0])
                self.assertEqual(self.row(second)["status"], "completed")
                self.service.shutdown.set()

        self.run_with_db_failures(fail_after={2}, observations=observe)
        self.assertIsNotNone(self.row(first)["summary_json"])
        self.assertEqual(self.row(first)["attempts"], 1)
        self.assertEqual(len(self.engine.calls), 2)

    def test_database_failure_before_claim_does_not_fail_unstarted_job(self):
        first = self.queued("user-alpha", "2026-09-01")

        def observe(wait):
            if wait == 1:
                self.assertEqual(self.row(first)["status"], "queued")
                self.assertEqual(self.engine.calls, [])
            else:
                self.assertEqual(self.row(first)["status"], "completed")
                self.service.shutdown.set()

        self.run_with_db_failures(fail_before={1}, observations=observe)
        self.assertEqual(self.row(first)["attempts"], 1)
        self.assertEqual(len(self.engine.calls), 1)


if __name__ == "__main__":
    unittest.main()
