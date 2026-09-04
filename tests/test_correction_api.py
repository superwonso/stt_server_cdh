from __future__ import annotations

import io
import tempfile
import threading
import time
import unittest
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from server.app import create_app
from server.postprocessor import CorrectedTranscript, PostprocessingError
from server.security import digest
from server.settings import Settings


def wav_audio(seconds=1.0, rate=16000, sample=1000):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(int(sample).to_bytes(2, "little", signed=True) * int(seconds * rate))
    return output.getvalue()


class FakeTranscriber:
    def __init__(self):
        self.calls = 0
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def status(self):
        return {"model_state": "ready", "model": "fake", "device": "cpu"}

    def transcribe(self, samples, language, overlap_seconds=0, final_chunk=True):
        self.calls += 1
        self.entered.set()
        if self.block and not self.release.wait(timeout=10):
            raise RuntimeError("test transcriber timed out")
        return [
            {
                "start": 0,
                "end": len(samples) / 16000,
                "text": "새 수업 받아쓰기입니다.",
            }
        ]


class FakePostprocessor:
    configured = True
    model = "solar-pro4-test"

    def __init__(self):
        self.calls = 0
        self.error: PostprocessingError | None = None
        self.block = False
        self.entered = threading.Event()
        self.returned = threading.Event()
        self.release = threading.Event()

    def correct(self, *, title, language, segments, interrupted=None):
        self.calls += 1
        self.entered.set()
        if self.block and not self.release.wait(timeout=5):
            raise RuntimeError("test worker timed out")
        if self.error is not None:
            raise self.error
        result = CorrectedTranscript(
            [{**segment, "text": segment["text"].replace("번재", "번째")} for segment in segments],
            ["확인이 필요한 용어"],
        )
        self.returned.set()
        return result


class CorrectionApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.processor = FakePostprocessor()
        self.transcriber = FakeTranscriber()
        self.settings = Settings(
            data_dir=self.directory / "data",
            model_cache_dir=self.directory / "models",
            site_origins=("https://student.github.io",),
        )
        self.app = create_app(self.settings, self.transcriber, self.processor)
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
        self.transcriber.release.set()
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

    def create_live_lecture(self):
        response = self.client.post(
            "/lectures",
            json={"title": "동시 녹음 수업", "language": "ko"},
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def upload_final_chunk(self, lecture_id):
        return self.client.post(
            f"/lectures/{lecture_id}/chunks",
            content=wav_audio(),
            headers=self.headers()
            | {
                "Content-Type": "audio/wav",
                "X-Chunk-Id": str(uuid.uuid4()),
                "X-Start-Seconds": "0",
                "X-Final-Chunk": "true",
            },
        )

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

    def test_blocked_correction_does_not_block_another_lecture_transcription(self):
        correction_lecture_id, correction_segment_id = self.lecture()
        live_lecture_id = self.create_live_lecture()
        self.processor.block = True

        started = self.client.post(
            f"/lectures/{correction_lecture_id}/correction",
            headers=self.headers(),
        )
        self.assertEqual(started.status_code, 200, started.text)
        self.assertTrue(self.processor.entered.wait(timeout=2), "correction did not start")

        with ThreadPoolExecutor(max_workers=1) as executor:
            upload = executor.submit(self.upload_final_chunk, live_lecture_id)
            try:
                transcribed = upload.result(timeout=3)
                self.assertEqual(transcribed.status_code, 200, transcribed.text)
                self.assertEqual(
                    [segment["text"] for segment in transcribed.json()["segments"]],
                    ["새 수업 받아쓰기입니다."],
                )
                self.assertTrue(transcribed.json()["recording_finalized"])
                self.assertFalse(self.processor.returned.is_set())
            finally:
                self.processor.release.set()

        completed = self.wait_for(correction_lecture_id, "completed").json()
        self.assertEqual(completed["corrected_text"], "첫 번째 문장 15개입니다.")
        with self.database.connect() as connection:
            correction_raw = connection.execute(
                "SELECT text FROM segments WHERE id = ?",
                (correction_segment_id,),
            ).fetchone()[0]
            live_rows = connection.execute(
                "SELECT text FROM segments WHERE lecture_id = ? ORDER BY start, id",
                (live_lecture_id,),
            ).fetchall()
            live_finalized = connection.execute(
                "SELECT recording_finalized FROM lectures WHERE id = ?",
                (live_lecture_id,),
            ).fetchone()[0]
        self.assertEqual(correction_raw, "첫 번재 문장 15개입니다.")
        self.assertEqual([row[0] for row in live_rows], ["새 수업 받아쓰기입니다."])
        self.assertEqual(live_finalized, 1)

    def test_blocked_transcription_does_not_block_another_lecture_correction(self):
        correction_lecture_id, correction_segment_id = self.lecture()
        live_lecture_id = self.create_live_lecture()
        self.transcriber.block = True

        with ThreadPoolExecutor(max_workers=1) as executor:
            upload = executor.submit(self.upload_final_chunk, live_lecture_id)
            self.assertTrue(self.transcriber.entered.wait(timeout=2), "transcription did not start")
            try:
                started = self.client.post(
                    f"/lectures/{correction_lecture_id}/correction",
                    headers=self.headers(),
                )
                self.assertEqual(started.status_code, 200, started.text)
                self.assertTrue(self.processor.entered.wait(timeout=2), "correction did not start")
                self.assertTrue(self.processor.returned.wait(timeout=2), "correction did not return")
                completed = self.wait_for(correction_lecture_id, "completed").json()
                self.assertEqual(completed["corrected_text"], "첫 번째 문장 15개입니다.")
                self.assertFalse(upload.done())
            finally:
                self.transcriber.release.set()
            transcribed = upload.result(timeout=3)

        self.assertEqual(transcribed.status_code, 200, transcribed.text)
        with self.database.connect() as connection:
            correction_raw = connection.execute(
                "SELECT text FROM segments WHERE id = ?",
                (correction_segment_id,),
            ).fetchone()[0]
            live_rows = connection.execute(
                "SELECT text FROM segments WHERE lecture_id = ? ORDER BY start, id",
                (live_lecture_id,),
            ).fetchall()
            correction_count = connection.execute(
                "SELECT COUNT(*) FROM transcript_corrections WHERE lecture_id = ? AND status = 'completed'",
                (correction_lecture_id,),
            ).fetchone()[0]
        self.assertEqual(correction_raw, "첫 번재 문장 15개입니다.")
        self.assertEqual([row[0] for row in live_rows], ["새 수업 받아쓰기입니다."])
        self.assertEqual(correction_count, 1)

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
