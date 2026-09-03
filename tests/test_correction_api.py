from __future__ import annotations

import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from server.app import create_app
from server.postprocessor import CorrectedTranscript, PostprocessingError
from server.security import digest
from server.settings import Settings


class FakeTranscriber:
    def status(self):
        return {"model_state": "ready", "model": "fake", "device": "cpu"}


class FakePostprocessor:
    configured = True
    model = "solar-pro4-test"

    def __init__(self):
        self.calls = 0
        self.error: PostprocessingError | None = None
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def correct(self, *, title, language, segments, interrupted=None):
        self.calls += 1
        self.entered.set()
        if self.block and not self.release.wait(timeout=5):
            raise RuntimeError("test worker timed out")
        if self.error is not None:
            raise self.error
        return CorrectedTranscript(
            [{**segment, "text": segment["text"].replace("번재", "번째")} for segment in segments],
            ["확인이 필요한 용어"],
        )


class CorrectionApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.processor = FakePostprocessor()
        self.settings = Settings(
            data_dir=self.directory / "data",
            model_cache_dir=self.directory / "models",
            site_origins=("https://student.github.io",),
        )
        self.app = create_app(self.settings, FakeTranscriber(), self.processor)
        self.client = TestClient(self.app)
        self.database = self.app.state.database
        self.tokens = {"user-alpha": "alpha-test-token", "user-beta": "beta-test-token"}
        with self.database.connect() as connection:
            for username, token in self.tokens.items():
                connection.execute(
                    "INSERT INTO sessions(token_hash, username, expires_at, created_at) VALUES (?, ?, ?, ?)",
                    (digest(token), username, time.time() + 3600, time.time()),
                )

    def tearDown(self):
        self.processor.release.set()
        self.app.state.stop_correction_worker()
        self.client.close()
        self.temporary.cleanup()

    def headers(self, username="user-alpha"):
        return {"Authorization": f"Bearer {self.tokens[username]}"}

    def lecture(self, username="user-alpha", finalized=True):
        lecture_id = str(uuid.uuid4())
        chunk_id = str(uuid.uuid4())
        segment_id = str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO lectures(id, username, title, language, created_at, recording_finalized) "
                "VALUES (?, ?, '테스트 수업', 'ko', '2026-09-04T00:00:00Z', ?)",
                (lecture_id, username, int(finalized)),
            )
            connection.execute(
                "INSERT INTO chunks(lecture_id, chunk_id, payload_hash, start_seconds, status) "
                "VALUES (?, ?, 'test', 0, 'done')",
                (lecture_id, chunk_id),
            )
            connection.execute(
                "INSERT INTO segments(id, lecture_id, chunk_id, start, end, text) "
                "VALUES (?, ?, ?, 0, 1, '첫 번재 문장 15개입니다.')",
                (segment_id, lecture_id, chunk_id),
            )
        return lecture_id, segment_id

    def wait_for(self, lecture_id, status, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.client.get(
                f"/lectures/{lecture_id}/correction",
                headers=self.headers(),
            )
            if response.status_code == 200 and response.json()["status"] == status:
                return response
            time.sleep(0.02)
        self.fail(f"correction did not reach {status}")

    def test_owner_only_idempotent_job_keeps_raw_and_returns_aligned_segments(self):
        lecture_id, segment_id = self.lecture()
        self.assertEqual(
            self.client.get(
                f"/lectures/{lecture_id}/correction", headers=self.headers()
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/lectures/{lecture_id}/correction", headers=self.headers("user-beta"), json={}
            ).status_code,
            404,
        )

        started = self.client.post(
            f"/lectures/{lecture_id}/correction", headers=self.headers(), json={}
        )
        self.assertEqual(started.status_code, 200, started.text)
        self.assertIn(started.json()["status"], {"queued", "processing", "completed"})
        completed = self.wait_for(lecture_id, "completed").json()
        self.assertEqual(completed["corrected_text"], "첫 번째 문장 15개입니다.")
        self.assertEqual(
            completed["corrected_segments"],
            [{"id": segment_id, "start": 0.0, "end": 1.0, "text": "첫 번째 문장 15개입니다."}],
        )
        self.assertEqual(completed["uncertain_terms"], ["확인이 필요한 용어"])
        repeated = self.client.post(
            f"/lectures/{lecture_id}/correction", headers=self.headers(), json={}
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()["status"], "completed")
        self.assertEqual(self.processor.calls, 1)
        with self.database.connect() as connection:
            raw = connection.execute(
                "SELECT text FROM segments WHERE id = ?", (segment_id,)
            ).fetchone()[0]
            stored = connection.execute(
                "SELECT status, corrected_text FROM transcript_corrections WHERE lecture_id = ?",
                (lecture_id,),
            ).fetchone()
        self.assertEqual(raw, "첫 번재 문장 15개입니다.")
        self.assertEqual(tuple(stored), ("completed", "첫 번째 문장 15개입니다."))

    def test_credit_failure_is_safe_and_post_retries_without_touching_raw(self):
        lecture_id, segment_id = self.lecture()
        self.processor.error = PostprocessingError(
            "credit_exhausted", "provider body and private request must not escape"
        )
        started = self.client.post(
            f"/lectures/{lecture_id}/correction", headers=self.headers(), json={}
        )
        self.assertEqual(started.status_code, 200)
        failed = self.wait_for(lecture_id, "failed").json()
        self.assertEqual(failed["error_code"], "credit_exhausted")
        self.assertNotIn("provider body", str(failed))
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT text FROM segments WHERE id = ?", (segment_id,)).fetchone()[0],
                "첫 번재 문장 15개입니다.",
            )
        self.processor.error = None
        retried = self.client.post(
            f"/lectures/{lecture_id}/correction", headers=self.headers(), json={}
        )
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["status"], "queued")
        self.wait_for(lecture_id, "completed")
        self.assertEqual(self.processor.calls, 2)

    def test_requires_final_transcript_and_blocks_delete_while_external_call_is_running(self):
        unfinished, _ = self.lecture(finalized=False)
        self.assertEqual(
            self.client.post(
                f"/lectures/{unfinished}/correction", headers=self.headers(), json={}
            ).status_code,
            409,
        )
        lecture_id, _ = self.lecture()
        self.processor.block = True
        self.client.post(f"/lectures/{lecture_id}/correction", headers=self.headers(), json={})
        self.assertTrue(self.processor.entered.wait(timeout=2))
        deletion = self.client.delete(f"/lectures/{lecture_id}", headers=self.headers())
        self.assertEqual(deletion.status_code, 409)
        self.processor.release.set()
        self.wait_for(lecture_id, "completed")
        self.assertEqual(
            self.client.delete(f"/lectures/{lecture_id}", headers=self.headers()).status_code,
            200,
        )

    def test_status_reports_configuration_without_disclosing_a_key(self):
        response = self.client.get("/status", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["postprocessing"],
            {"configured": True, "model": "solar-pro4-test"},
        )
        self.assertNotIn("key", response.text.casefold())


if __name__ == "__main__":
    unittest.main()
