from __future__ import annotations

import hashlib
import io
import tempfile
import threading
import time
import unittest
import uuid
import wave
from unittest import mock
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from server.app import create_app
from server.security import digest
from server.settings import Settings


def wav_file(seconds: float = 1.0) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(np.full(round(seconds * 16_000), 1000, dtype="<i2").tobytes())
    return output.getvalue()


def file_fingerprint(payload: bytes) -> str:
    part_bytes = 480 * 1024
    part_count = (len(payload) + part_bytes - 1) // part_bytes
    value = hashlib.sha256(
        f"stt-import-fingerprint-v2\0{len(payload)}\0{part_bytes}\0{part_count}\0".encode()
    )
    for offset in range(0, len(payload), part_bytes):
        value.update(hashlib.sha256(payload[offset : offset + part_bytes]).digest())
    return value.hexdigest()


class FakeTranscriber:
    def __init__(self, *, block: bool = False):
        self.calls = 0
        self.block = block
        self.entered = threading.Event()
        self.release = threading.Event()

    def status(self):
        return {"model_state": "ready", "model": "fake", "device": "cpu"}

    def transcribe(self, samples, language, overlap_seconds=0, final_chunk=True):
        self.calls += 1
        self.entered.set()
        if self.block and not self.release.wait(10):
            raise RuntimeError("test timeout")
        return [{"start": 0, "end": len(samples) / 16_000, "text": "파일 받아쓰기"}]


class ImportApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.settings = Settings(
            data_dir=self.directory / "data",
            model_cache_dir=self.directory / "models",
            site_origins=("https://student.github.io",),
            model_warmup=False,
        )
        self.engine = FakeTranscriber()
        self.app = create_app(self.settings, self.engine)
        self.client = TestClient(self.app)
        self.tokens = {}
        for username, character in (("user-alpha", "a"), ("user-beta", "b")):
            code = character * 43
            with self.app.state.database.connect() as connection:
                connection.execute(
                    "UPDATE users SET setup_hash = ?, setup_expires = ? WHERE username = ?",
                    (digest(code), time.time() + 3600, username),
                )
            response = self.client.post("/auth/activate", json={
                "username": username,
                "setup_code": code,
                "password": "test-file-password",
            })
            self.tokens[username] = response.json()["token"]

    def tearDown(self):
        self.engine.release.set()
        self.app.state.stop_import_worker()
        self.client.close()
        self.temporary.cleanup()

    def headers(self, username="user-alpha"):
        return {"Authorization": f"Bearer {self.tokens[username]}"}

    def create(self, payload: bytes, *, import_id=None, username="user-alpha"):
        headers = self.headers(username)
        if import_id:
            headers["X-Import-Id"] = import_id
        return self.client.post("/imports", headers=headers, json={
            "title": "업로드 수업",
            "language": "ko",
            "filename": "recording.wav",
            "file_fingerprint": file_fingerprint(payload),
            "size": len(payload),
        })

    def put(self, import_id: str, payload: bytes, offset: int, *, username="user-alpha", digest_value=None):
        return self.client.put(f"/imports/{import_id}", content=payload, headers=self.headers(username) | {
            "Content-Type": "application/octet-stream",
            "X-Upload-Offset": str(offset),
            "X-Part-SHA256": digest_value or hashlib.sha256(payload).hexdigest(),
        })

    def wait_terminal(self, import_id, client=None, token=None):
        client = client or self.client
        headers = {"Authorization": f"Bearer {token}"} if token else self.headers()
        for _ in range(200):
            result = client.get(f"/imports/{import_id}", headers=headers).json()
            if result["status"] not in {"queued", "processing"}:
                return result
            time.sleep(0.025)
        self.fail("import did not reach a terminal state")

    def test_owner_part_validation_resume_and_cancel_remove_private_file(self):
        payload = wav_file(16)
        import_id = str(uuid.uuid4())
        created = self.create(payload, import_id=import_id)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["part_bytes"], 480 * 1024)
        preflight = self.client.options("/imports/example", headers={
            "Origin": "https://student.github.io",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "authorization,content-type,x-upload-offset,x-part-sha256",
        })
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers["Access-Control-Allow-Origin"], "https://student.github.io")
        self.assertEqual(self.client.get("/imports").status_code, 401)
        self.assertEqual(self.create(payload).status_code, 409, "one user may have only one active import")
        changed_fingerprint = self.client.post("/imports", headers=self.headers() | {"X-Import-Id": import_id}, json={
            "title": "업로드 수업", "language": "ko", "filename": "recording.wav",
            "file_fingerprint": "0" * 64, "size": len(payload),
        })
        self.assertEqual(changed_fingerprint.status_code, 409)
        too_large = self.client.post("/imports", headers=self.headers("user-beta"), json={
            "title": "큼", "language": "ko", "filename": "large.wav",
            "file_fingerprint": "0" * 64, "size": self.settings.max_import_bytes + 1,
        })
        self.assertEqual(too_large.status_code, 422)
        path = self.settings.data_dir / "imports" / "user-alpha" / f"{import_id}.upload"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.client.get(f"/imports/{import_id}", headers=self.headers("user-beta")).status_code, 404)
        self.assertEqual(self.put(import_id, payload[:100], 0, username="user-beta").status_code, 404)
        self.assertEqual(self.put(import_id, payload[:100], 0).status_code, 422)

        part = payload[: 480 * 1024]
        first = self.put(import_id, part, 0)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["uploaded_bytes"], len(part))
        replay = self.put(import_id, part, 0)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["next_offset"], len(part))
        changed = bytearray(part)
        changed[0] ^= 1
        self.assertEqual(self.put(import_id, bytes(changed), 0).status_code, 409)
        self.assertEqual(self.put(import_id, payload[len(part):], len(part) + 1).status_code, 409)
        self.assertEqual(self.put(import_id, b"last", len(part), digest_value="0" * 64).status_code, 422)
        self.assertEqual(self.client.post(f"/imports/{import_id}/complete", headers=self.headers()).status_code, 409)

        listed = self.client.get("/imports", headers=self.headers()).json()
        self.assertEqual([job["id"] for job in listed], [import_id])
        cancelled = self.client.post(f"/imports/{import_id}/cancel", headers=self.headers())
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertTrue(cancelled.json()["raw_deleted"])
        self.assertFalse(path.exists())
        self.assertIsNone(cancelled.json()["lecture_id"])

    def test_complete_runs_in_background_and_deletes_raw_but_keeps_owned_lecture(self):
        payload = wav_file(2)
        import_id = self.create(payload).json()["id"]
        self.assertEqual(self.put(import_id, payload, 0).status_code, 200)
        queued = self.client.post(f"/imports/{import_id}/complete", headers=self.headers())
        self.assertIn(queued.json()["status"], {"queued", "processing", "completed"})
        completed = self.wait_terminal(import_id)
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["raw_deleted"])
        self.assertEqual(completed["duration_seconds"], 2)
        path = self.settings.data_dir / "imports" / "user-alpha" / f"{import_id}.upload"
        self.assertFalse(path.exists())
        lecture = self.client.get(f"/lectures/{completed['lecture_id']}", headers=self.headers())
        self.assertEqual(lecture.status_code, 200)
        self.assertEqual(len(lecture.json()["segments"]), 1)
        self.assertTrue(lecture.json()["recording_available"])
        self.assertTrue(lecture.json()["recording_finalized"])
        recording = (
            self.settings.data_dir
            / "recordings"
            / "user-alpha"
            / f"{completed['lecture_id']}.wav"
        )
        self.assertTrue(recording.is_file())
        self.assertEqual(recording.stat().st_mode & 0o777, 0o600)
        ticket = self.client.post(
            f"/lectures/{completed['lecture_id']}/recording-download-ticket",
            headers=self.headers(),
        )
        self.assertEqual(ticket.status_code, 200, ticket.text)
        downloaded = self.client.get(ticket.json()["path"])
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        with wave.open(io.BytesIO(downloaded.content), "rb") as audio:
            self.assertEqual(
                (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()),
                (1, 2, 16_000, 32_000),
            )
        self.assertEqual(
            self.client.post(
                f"/lectures/{completed['lecture_id']}/recording-download-ticket",
                headers=self.headers("user-beta"),
            ).status_code,
            404,
        )
        deleted = self.client.delete(
            f"/lectures/{completed['lecture_id']}",
            headers=self.headers(),
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json(), {"status": "deleted"})
        self.assertFalse(recording.exists())
        self.assertEqual(
            self.client.get(f"/imports/{import_id}", headers=self.headers()).status_code,
            404,
        )
        self.assertEqual(self.client.get(ticket.json()["path"]).status_code, 404)
        with self.app.state.database.connect() as connection:
            self.assertIsNone(
                connection.execute("SELECT 1 FROM imports WHERE id = ?", (import_id,)).fetchone()
            )

    def test_complete_rejects_changed_bytes_using_full_bounded_fingerprint(self):
        payload = wav_file(16)
        import_id = self.create(payload).json()["id"]
        for offset in range(0, len(payload), 480 * 1024):
            part = payload[offset : offset + 480 * 1024]
            self.assertEqual(self.put(import_id, part, offset).status_code, 200)
        path = self.settings.data_dir / "imports" / "user-alpha" / f"{import_id}.upload"
        with path.open("r+b") as output:
            # This offset was outside the old first/middle/last 64 KiB samples.
            output.seek(100_000)
            value = output.read(1)
            output.seek(100_000)
            output.write(bytes([value[0] ^ 1]))
        rejected = self.client.post(f"/imports/{import_id}/complete", headers=self.headers())
        self.assertEqual(rejected.status_code, 409)
        job = self.client.get(f"/imports/{import_id}", headers=self.headers()).json()
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["raw_deleted"])
        self.assertFalse(path.exists())

    def test_processing_cancel_is_cooperative_and_removes_partial_private_data(self):
        self.engine.block = True
        payload = wav_file(31)
        import_id = self.create(payload).json()["id"]
        part_size = 480 * 1024
        for offset in range(0, len(payload), part_size):
            part = payload[offset : offset + part_size]
            self.assertEqual(self.put(import_id, part, offset).status_code, 200)
        self.client.post(f"/imports/{import_id}/complete", headers=self.headers())
        self.assertTrue(self.engine.entered.wait(5))
        cancelling = self.client.post(f"/imports/{import_id}/cancel", headers=self.headers())
        self.assertEqual(cancelling.status_code, 200)
        self.assertEqual(cancelling.json()["status"], "processing")
        self.assertTrue(cancelling.json()["cancel_requested"])
        self.engine.release.set()
        cancelled = self.wait_terminal(import_id)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["raw_deleted"])
        self.assertIsNone(cancelled["lecture_id"])
        raw = self.settings.data_dir / "imports" / "user-alpha" / f"{import_id}.upload"
        self.assertFalse(raw.exists())

    def test_graceful_stop_requeues_and_new_app_resumes_deterministic_chunks(self):
        self.app.state.stop_import_worker()
        # A fresh app is needed because stop is intentionally final for one
        # application lifespan.
        first_engine = FakeTranscriber(block=True)
        first_app = create_app(self.settings, first_engine)
        first_client = TestClient(first_app)
        token = self.tokens["user-alpha"]
        headers = {"Authorization": f"Bearer {token}"}
        payload = wav_file(31)
        import_id = str(uuid.uuid4())
        created = first_client.post("/imports", headers=headers | {"X-Import-Id": import_id}, json={
            "title": "재시작 수업", "language": "ko", "filename": "long.wav",
            "file_fingerprint": file_fingerprint(payload), "size": len(payload),
        })
        self.assertEqual(created.status_code, 201, created.text)
        part_size = created.json()["part_bytes"]
        for offset in range(0, len(payload), part_size):
            part = payload[offset : offset + part_size]
            self.assertEqual(first_client.put(f"/imports/{import_id}", content=part, headers=headers | {
                "Content-Type": "application/octet-stream",
                "X-Upload-Offset": str(offset),
                "X-Part-SHA256": hashlib.sha256(part).hexdigest(),
            }).status_code, 200)
        first_client.post(f"/imports/{import_id}/complete", headers=headers)
        self.assertTrue(first_engine.entered.wait(5))
        stopping = threading.Thread(target=first_app.state.stop_import_worker)
        stopping.start()
        first_engine.release.set()
        stopping.join(10)
        self.assertFalse(stopping.is_alive())
        paused = first_client.get(f"/imports/{import_id}", headers=headers).json()
        self.assertEqual(paused["status"], "queued")
        raw = self.settings.data_dir / "imports" / "user-alpha" / f"{import_id}.upload"
        self.assertTrue(raw.is_file())
        first_client.close()

        resumed_engine = FakeTranscriber()
        resumed_app = create_app(self.settings, resumed_engine)
        with TestClient(resumed_app) as resumed_client:
            completed = self.wait_terminal(import_id, resumed_client, token)
            self.assertEqual(completed["status"], "completed")
            self.assertTrue(completed["raw_deleted"])
            self.assertFalse(raw.exists())
            with resumed_app.state.database.connect() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM chunks WHERE lecture_id = ?",
                    (completed["lecture_id"],),
                ).fetchone()[0]
            self.assertGreaterEqual(count, 3)

    def test_terminal_state_never_claims_raw_deletion_and_list_retries_cleanup(self):
        payload = wav_file(2)
        import_id = self.create(payload).json()["id"]
        self.assertEqual(self.put(import_id, payload, 0).status_code, 200)
        raw = self.settings.data_dir / "imports" / "user-alpha" / f"{import_id}.upload"
        original_unlink = Path.unlink

        def fail_only_raw(path, *args, **kwargs):
            if path == raw:
                raise OSError("simulated local file lock")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_only_raw):
            self.client.post(f"/imports/{import_id}/complete", headers=self.headers())
            completed = self.wait_terminal(import_id)
            self.assertEqual(completed["status"], "completed")
            self.assertFalse(completed["raw_deleted"])
            self.assertTrue(raw.exists())

        listed = self.client.get("/imports", headers=self.headers()).json()
        recovered = next(job for job in listed if job["id"] == import_id)
        self.assertTrue(recovered["raw_deleted"])
        self.assertFalse(raw.exists())

    def test_list_expires_a_seven_day_abandoned_upload_without_server_restart(self):
        payload = wav_file(2)
        import_id = self.create(payload).json()["id"]
        raw = self.settings.data_dir / "imports" / "user-alpha" / f"{import_id}.upload"
        with self.app.state.database.connect() as connection:
            connection.execute(
                "UPDATE imports SET updated_at = '2020-01-01T00:00:00Z' WHERE id = ?",
                (import_id,),
            )
        listed = self.client.get("/imports", headers=self.headers()).json()
        expired = next(job for job in listed if job["id"] == import_id)
        self.assertEqual(expired["status"], "failed")
        self.assertTrue(expired["raw_deleted"])
        self.assertIsNone(expired["lecture_id"])
        self.assertFalse(raw.exists())


if __name__ == "__main__":
    unittest.main()
