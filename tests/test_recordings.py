from __future__ import annotations

import asyncio
import io
import os
import tempfile
import threading
import time
import unittest
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from server import recordings as recordings_module
from server.app import DescriptorFileResponse, create_app
from server.recordings import (
    RecordingCapacityError,
    RecordingConflict,
    RecordingCorruptError,
    RecordingStore,
)
from server.security import digest
from server.settings import Settings


def wav_from_samples(samples: np.ndarray) -> bytes:
    values = np.asarray(samples, dtype="<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(values.tobytes())
    return output.getvalue()


def read_wav(payload: bytes) -> tuple[tuple[int, int, int], np.ndarray]:
    with wave.open(io.BytesIO(payload), "rb") as audio:
        metadata = (audio.getnchannels(), audio.getsampwidth(), audio.getframerate())
        frames = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").copy()
    return metadata, frames


class FakeTranscriber:
    def __init__(self):
        self.calls = 0
        self.invocations = []
        self.results = []
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def status(self):
        return {"model_state": "ready", "model": "fake", "device": "cpu"}

    def transcribe(self, samples, language, overlap_seconds=0, final_chunk=True):
        self.calls += 1
        self.invocations.append({
            "samples": np.asarray(samples).copy(),
            "language": language,
            "overlap_seconds": overlap_seconds,
            "final_chunk": final_chunk,
        })
        self.entered.set()
        if self.block and not self.release.wait(timeout=10):
            raise RuntimeError("test timeout")
        if self.results:
            return self.results.pop(0)
        return [{"start": 0, "end": len(samples) / 16_000, "text": "녹음 테스트"}]


class RecordingStoreTests(unittest.TestCase):
    def test_overlap_retry_gap_and_orphan_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RecordingStore(
                Path(temporary) / "recordings",
                ("user-alpha", "user-beta"),
                max_total_bytes=1024 * 1024,
                min_free_bytes=0,
                max_seconds=60,
            )
            lecture_id = str(uuid.uuid4())
            first = np.array([1, 2, 3, 4], dtype="<i2")
            overlapping = np.array([3, 4, 5, 6], dtype="<i2")
            store.write_chunk(
                "user-alpha",
                lecture_id,
                start_seconds=0,
                overlap_seconds=0,
                pcm=first.tobytes(),
            )
            store.write_chunk(
                "user-alpha",
                lecture_id,
                start_seconds=2 / 16_000,
                overlap_seconds=2 / 16_000,
                pcm=overlapping.tobytes(),
            )
            # The exact retry compares already-written PCM and appends nothing.
            before = store.path("user-alpha", lecture_id).read_bytes()
            store.write_chunk(
                "user-alpha",
                lecture_id,
                start_seconds=2 / 16_000,
                overlap_seconds=2 / 16_000,
                pcm=overlapping.tobytes(),
            )
            self.assertEqual(store.path("user-alpha", lecture_id).read_bytes(), before)

            with self.assertRaises(RecordingConflict):
                store.write_chunk(
                    "user-alpha",
                    lecture_id,
                    start_seconds=2 / 16_000,
                    overlap_seconds=2 / 16_000,
                    pcm=np.array([3, 4, 9, 9], dtype="<i2").tobytes(),
                )

            # A bounded missing interval is represented as silence, preserving
            # the recording timeline rather than shifting later audio earlier.
            store.write_chunk(
                "user-alpha",
                lecture_id,
                start_seconds=8 / 16_000,
                overlap_seconds=0,
                pcm=np.array([7], dtype="<i2").tobytes(),
            )
            metadata, frames = read_wav(store.path("user-alpha", lecture_id).read_bytes())
            self.assertEqual(metadata, (1, 2, 16_000))
            np.testing.assert_array_equal(frames, np.array([1, 2, 3, 4, 5, 6, 0, 0, 7], dtype="<i2"))
            self.assertEqual(store.path("user-alpha", lecture_id).stat().st_mode & 0o777, 0o600)
            self.assertEqual((store.root / "user-alpha").stat().st_mode & 0o777, 0o700)

            store.remove_orphans({"user-alpha": set(), "user-beta": set()})
            self.assertFalse(store.path("user-alpha", lecture_id).exists())

    def test_failed_append_rolls_back_existing_wav_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RecordingStore(
                Path(temporary) / "recordings",
                ("user-alpha", "user-beta"),
                max_total_bytes=1024 * 1024,
                min_free_bytes=0,
                max_seconds=60,
            )
            lecture_id = str(uuid.uuid4())
            store.write_chunk(
                "user-alpha",
                lecture_id,
                start_seconds=0,
                overlap_seconds=0,
                pcm=np.array([1, 2, 3, 4], dtype="<i2").tobytes(),
            )
            path = store.path("user-alpha", lecture_id)
            original = path.read_bytes()
            append_value = np.array([5, 6, 7], dtype="<i2").tobytes()
            real_write_all = recordings_module._write_all

            def fail_during_append(descriptor, value):
                if bytes(value) == append_value:
                    os.write(descriptor, bytes(value)[:2])
                    raise OSError("simulated short disk write")
                return real_write_all(descriptor, value)

            with mock.patch.object(recordings_module, "_write_all", fail_during_append):
                with self.assertRaises(OSError):
                    store.write_chunk(
                        "user-alpha",
                        lecture_id,
                        start_seconds=4 / 16_000,
                        overlap_seconds=0,
                        pcm=append_value,
                    )
            self.assertEqual(path.read_bytes(), original)
            metadata, frames = read_wav(path.read_bytes())
            self.assertEqual(metadata, (1, 2, 16_000))
            np.testing.assert_array_equal(frames, np.array([1, 2, 3, 4], dtype="<i2"))

    def test_descriptor_response_closes_original_fd_when_send_fails(self):
        with tempfile.NamedTemporaryFile() as temporary:
            temporary.write(b"test recording bytes")
            temporary.flush()
            descriptor = os.open(temporary.name, os.O_RDONLY)
            response = DescriptorFileResponse(
                descriptor,
                media_type="application/octet-stream",
                stat_result=os.fstat(descriptor),
            )

            async def receive():
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.body":
                    raise ConnectionError("simulated client disconnect")

            with self.assertRaises(ConnectionError):
                asyncio.run(
                    response(
                        {"type": "http", "method": "GET", "headers": []},
                        receive,
                        send,
                    )
                )
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_quota_rejection_does_not_change_existing_wav(self):
        with tempfile.TemporaryDirectory() as temporary:
            # Four PCM16 frames plus the 44-byte header exactly fill the quota.
            store = RecordingStore(
                Path(temporary) / "recordings",
                ("user-alpha", "user-beta"),
                max_total_bytes=52,
                min_free_bytes=0,
                max_seconds=60,
            )
            lecture_id = str(uuid.uuid4())
            store.write_chunk(
                "user-alpha",
                lecture_id,
                start_seconds=0,
                overlap_seconds=0,
                pcm=np.array([1, 2, 3, 4], dtype="<i2").tobytes(),
            )
            path = store.path("user-alpha", lecture_id)
            before = path.read_bytes()
            with self.assertRaises(RecordingCapacityError):
                store.write_chunk(
                    "user-alpha",
                    lecture_id,
                    start_seconds=4 / 16_000,
                    overlap_seconds=0,
                    pcm=np.array([5], dtype="<i2").tobytes(),
                )
            self.assertEqual(path.read_bytes(), before)

    def test_symlink_is_never_opened_but_delete_unlinks_only_the_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = RecordingStore(
                directory / "recordings",
                ("user-alpha", "user-beta"),
                max_total_bytes=1024 * 1024,
                min_free_bytes=0,
                max_seconds=60,
            )
            lecture_id = str(uuid.uuid4())
            outside = directory / "outside-private-file"
            outside.write_bytes(b"must remain unchanged")
            path = store.path("user-alpha", lecture_id)
            path.symlink_to(outside)

            with self.assertRaises(RecordingCorruptError):
                store.open_info("user-alpha", lecture_id)
            store.delete("user-alpha", lecture_id)
            self.assertFalse(path.exists())
            self.assertEqual(outside.read_bytes(), b"must remain unchanged")


class RecordingApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.engine = FakeTranscriber()
        self.settings = Settings(
            data_dir=self.directory / "data",
            model_cache_dir=self.directory / "models",
            site_origins=("https://student.github.io",),
            max_pending_chunks=1,
            max_import_seconds=60,
            max_recordings_bytes=16 * 1024 * 1024,
            recording_free_reserve_bytes=0,
        )
        self.app = create_app(self.settings, self.engine)
        self.client = TestClient(self.app)
        self.database = self.app.state.database
        self.tokens = {
            "user-alpha": "recording-session-alpha",
            "user-beta": "recording-session-beta",
        }
        with self.database.connect() as connection:
            for username, token in self.tokens.items():
                connection.execute(
                    "INSERT INTO sessions(token_hash, username, expires_at, created_at) VALUES (?, ?, ?, ?)",
                    (digest(token), username, time.time() + 3600, time.time()),
                )

    def tearDown(self):
        self.engine.release.set()
        self.app.state.stop_import_worker()
        self.client.close()
        self.temporary.cleanup()

    def headers(self, username="user-alpha"):
        return {"Authorization": f"Bearer {self.tokens[username]}"}

    def create_lecture(self, title="녹음 수업") -> str:
        response = self.client.post(
            "/lectures",
            headers=self.headers(),
            json={"title": title, "language": "ko"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def upload(
        self,
        lecture_id: str,
        samples: np.ndarray,
        *,
        start: float,
        overlap: float,
        final: bool,
        chunk_id: str | None = None,
    ):
        return self.client.post(
            f"/lectures/{lecture_id}/chunks",
            headers=self.headers()
            | {
                "Content-Type": "audio/wav",
                "X-Chunk-Id": chunk_id or str(uuid.uuid4()),
                "X-Start-Seconds": str(start),
                "X-Overlap-Seconds": str(overlap),
                "X-Final-Chunk": "true" if final else "false",
            },
            content=wav_from_samples(samples),
        )

    def finalized_recording(self, title="녹음 수업") -> tuple[str, np.ndarray]:
        lecture_id = self.create_lecture(title)
        first = np.arange(800, 2400, dtype="<i2")
        fresh = np.arange(3000, 4000, dtype="<i2")
        second = np.concatenate((first[-800:], fresh)).astype("<i2")
        first_response = self.upload(
            lecture_id,
            first,
            start=0,
            overlap=0,
            final=False,
        )
        self.assertEqual(first_response.status_code, 200, first_response.text)
        second_id = str(uuid.uuid4())
        second_response = self.upload(
            lecture_id,
            second,
            start=0.05,
            overlap=0.05,
            final=True,
            chunk_id=second_id,
        )
        self.assertEqual(second_response.status_code, 200, second_response.text)
        self.assertTrue(second_response.json()["recording_finalized"])

        path = self.app.state.recording_store.path("user-alpha", lecture_id)
        before_retry = path.read_bytes()
        replay = self.upload(
            lecture_id,
            second,
            start=0.05,
            overlap=0.05,
            final=True,
            chunk_id=second_id,
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(path.read_bytes(), before_retry)
        return lecture_id, np.concatenate((first, fresh)).astype("<i2")

    def test_private_resumable_ticket_downloads_exact_normalized_wav(self):
        lecture_id, expected = self.finalized_recording('물리: 파동 / "복습"')
        detail = self.client.get(f"/lectures/{lecture_id}", headers=self.headers()).json()
        self.assertTrue(detail["recording_available"])
        self.assertTrue(detail["recording_finalized"])

        self.assertEqual(
            self.client.post(
                f"/lectures/{lecture_id}/recording-download-ticket",
                headers=self.headers("user-beta"),
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(f"/lectures/{lecture_id}/recording-download-ticket").status_code,
            401,
        )
        ticket = self.client.post(
            f"/lectures/{lecture_id}/recording-download-ticket",
            headers=self.headers(),
        )
        self.assertEqual(ticket.status_code, 200, ticket.text)
        path = ticket.json()["path"]
        self.assertNotIn(lecture_id, path)
        self.assertNotIn(self.tokens["user-alpha"], path)

        first = self.client.get(path, headers={"Range": "bytes=0-99"})
        self.assertEqual(first.status_code, 206, first.text)
        self.assertEqual(first.headers["content-type"], "audio/wav")
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertEqual(first.headers["accept-ranges"], "bytes")
        self.assertEqual(first.headers["content-range"].split(" ", 1)[0], "bytes")
        self.assertIn("attachment", first.headers["content-disposition"])
        self.assertNotIn('"', first.headers["content-disposition"].split("filename=", 1)[-1])
        total_bytes = int(first.headers["content-range"].rsplit("/", 1)[1])
        resumed = self.client.get(path, headers={"Range": f"bytes=100-{total_bytes - 1}"})
        self.assertEqual(resumed.status_code, 206, resumed.text)
        self.assertEqual(resumed.headers["content-range"], f"bytes 100-{total_bytes - 1}/{total_bytes}")
        metadata, frames = read_wav(first.content + resumed.content)
        self.assertEqual(metadata, (1, 2, 16_000))
        np.testing.assert_array_equal(frames, expected)

    def test_ticket_expires_and_unfinalized_recording_cannot_issue_one(self):
        lecture_id = self.create_lecture()
        response = self.upload(
            lecture_id,
            np.full(800, 123, dtype="<i2"),
            start=0,
            overlap=0,
            final=False,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.client.post(
                f"/lectures/{lecture_id}/recording-download-ticket",
                headers=self.headers(),
            ).status_code,
            409,
        )

        final = self.upload(
            lecture_id,
            np.full(800, 124, dtype="<i2"),
            start=0.05,
            overlap=0,
            final=True,
        )
        self.assertEqual(final.status_code, 200, final.text)
        with mock.patch("server.app.time.monotonic", return_value=100.0):
            ticket = self.client.post(
                f"/lectures/{lecture_id}/recording-download-ticket",
                headers=self.headers(),
            ).json()["path"]
        with mock.patch("server.app.time.monotonic", return_value=161.0):
            self.assertEqual(self.client.get(ticket).status_code, 404)

    def test_explicit_finalize_is_owner_only_and_enables_download(self):
        lecture_id = self.create_lecture()
        samples = np.arange(800, 1600, dtype="<i2")
        uploaded = self.upload(
            lecture_id,
            samples,
            start=0,
            overlap=0,
            final=False,
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.assertFalse(uploaded.json()["recording_finalized"])
        calls_before_other_owner = self.engine.calls
        self.assertEqual(
            self.client.post(
                f"/lectures/{lecture_id}/recording-finalize",
                headers=self.headers("user-beta"),
            ).status_code,
            404,
        )
        self.assertEqual(self.engine.calls, calls_before_other_owner)

        finalized = self.client.post(
            f"/lectures/{lecture_id}/recording-finalize",
            headers=self.headers(),
        )
        self.assertEqual(finalized.status_code, 200, finalized.text)
        self.assertTrue(finalized.json()["recording_available"])
        self.assertTrue(finalized.json()["recording_finalized"])
        self.assertEqual(len(finalized.json()["segments"]), 1)
        guard_call = self.engine.invocations[-1]
        self.assertAlmostEqual(guard_call["overlap_seconds"], 0.05)
        self.assertTrue(guard_call["final_chunk"])
        np.testing.assert_allclose(
            guard_call["samples"],
            samples.astype(np.float32) / 32768.0,
        )
        ticket = self.client.post(
            f"/lectures/{lecture_id}/recording-download-ticket",
            headers=self.headers(),
        )
        self.assertEqual(ticket.status_code, 200, ticket.text)
        downloaded = self.client.get(ticket.json()["path"])
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        _, frames = read_wav(downloaded.content)
        np.testing.assert_array_equal(frames, samples)
        self.assertEqual(
            self.upload(
                lecture_id,
                np.full(800, 9, dtype="<i2"),
                start=0.05,
                overlap=0,
                final=True,
            ).status_code,
            409,
        )

    def test_explicit_finalize_recovers_the_saved_guard_once_without_changing_wav(self):
        lecture_id = self.create_lecture()
        samples = (np.arange(4 * 16_000, dtype=np.int32) % 30_000).astype("<i2")
        self.engine.results = [
            [{"start": 0.1, "end": 0.5, "text": "앞부분"}],
            [{"start": 2.5, "end": 2.9, "text": "마지막 문장"}],
        ]
        uploaded = self.upload(
            lecture_id,
            samples,
            start=0,
            overlap=0,
            final=False,
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        recording = self.app.state.recording_store.path("user-alpha", lecture_id)
        before = recording.read_bytes()

        finalized = self.client.post(
            f"/lectures/{lecture_id}/recording-finalize",
            headers=self.headers(),
        )
        self.assertEqual(finalized.status_code, 200, finalized.text)
        result = finalized.json()
        self.assertTrue(result["recording_available"])
        self.assertTrue(result["recording_finalized"])
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(
            {key: result["segments"][0][key] for key in ("start", "end", "text")},
            {"start": 3.5, "end": 3.9, "text": "마지막 문장"},
        )
        self.assertEqual(len(self.engine.invocations), 2)
        guard_call = self.engine.invocations[-1]
        self.assertEqual(guard_call["language"], "ko")
        self.assertEqual(guard_call["overlap_seconds"], 3)
        self.assertTrue(guard_call["final_chunk"])
        np.testing.assert_allclose(
            guard_call["samples"],
            samples[-3 * 16_000 :].astype(np.float32) / 32768.0,
        )
        self.assertEqual(recording.read_bytes(), before, "overlap-only recovery must not rewrite the WAV")

        guard_id = "server:recording-finalize-guard:v1"
        with self.database.connect() as connection:
            guard = connection.execute(
                "SELECT start_seconds, overlap_seconds, final_chunk, status "
                "FROM chunks WHERE lecture_id = ? AND chunk_id = ?",
                (lecture_id, guard_id),
            ).fetchone()
            self.assertEqual(
                dict(guard),
                {
                    "start_seconds": 1.0,
                    "overlap_seconds": 3.0,
                    "final_chunk": 1,
                    "status": "done",
                },
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM segments WHERE lecture_id = ? AND chunk_id = ?",
                    (lecture_id, guard_id),
                ).fetchone()[0],
                1,
            )

        replayed = self.client.post(
            f"/lectures/{lecture_id}/recording-finalize",
            headers=self.headers(),
        )
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertEqual(replayed.json(), result)
        self.assertEqual(len(self.engine.invocations), 2, "a response-loss retry must not infer twice")

        detail = self.client.get(f"/lectures/{lecture_id}", headers=self.headers()).json()
        recovered = [segment for segment in detail["segments"] if segment["text"] == "마지막 문장"]
        self.assertEqual(recovered, result["segments"])

    def test_explicit_finalize_replays_a_committed_normal_final_after_response_loss(self):
        lecture_id = self.create_lecture()
        self.engine.results = [[{"start": 0.7, "end": 0.95, "text": "응답 유실 문장"}]]
        final = self.upload(
            lecture_id,
            np.full(16_000, 321, dtype="<i2"),
            start=0,
            overlap=0,
            final=True,
        )
        self.assertEqual(final.status_code, 200, final.text)
        calls_after_commit = self.engine.calls

        reconciled = self.client.post(
            f"/lectures/{lecture_id}/recording-finalize",
            headers=self.headers(),
        )
        self.assertEqual(reconciled.status_code, 200, reconciled.text)
        self.assertEqual(reconciled.json()["segments"], final.json()["segments"])
        self.assertTrue(reconciled.json()["recording_available"])
        self.assertTrue(reconciled.json()["recording_finalized"])
        self.assertEqual(self.engine.calls, calls_after_commit, "reconciliation must only replay DB rows")

    def test_finalize_claim_blocks_duplicate_inference_and_rejects_a_changed_recording(self):
        lecture_id = self.create_lecture()
        samples = np.full(16_000, 321, dtype="<i2")
        uploaded = self.upload(
            lecture_id,
            samples,
            start=0,
            overlap=0,
            final=False,
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        calls_before_guard = self.engine.calls
        self.engine.entered.clear()
        self.engine.release.clear()
        self.engine.block = True

        with ThreadPoolExecutor(max_workers=1) as executor:
            request = executor.submit(
                self.client.post,
                f"/lectures/{lecture_id}/recording-finalize",
                headers=self.headers(),
            )
            try:
                self.assertTrue(self.engine.entered.wait(timeout=5))
                duplicate = self.client.post(
                    f"/lectures/{lecture_id}/recording-finalize",
                    headers=self.headers(),
                )
                self.assertEqual(duplicate.status_code, 409, duplicate.text)
                self.assertEqual(duplicate.headers.get("Retry-After"), "2")
                self.assertEqual(self.engine.calls, calls_before_guard + 1)

                # A concurrently completed uploader changes the append-only
                # recording revision while final-guard inference is running.
                self.app.state.recording_store.write_chunk(
                    "user-alpha",
                    lecture_id,
                    start_seconds=1,
                    overlap_seconds=0,
                    pcm=np.full(800, 654, dtype="<i2").tobytes(),
                )
            finally:
                self.engine.release.set()
            changed = request.result(timeout=5)

        self.engine.block = False
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(changed.headers.get("Retry-After"), "2")
        guard_id = "server:recording-finalize-guard:v1"
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT recording_finalized FROM lectures WHERE id = ?", (lecture_id,)
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM chunks WHERE lecture_id = ? AND chunk_id = ?",
                    (lecture_id, guard_id),
                ).fetchone()[0],
                0,
                "a failed optimistic commit must release its deterministic pending claim",
            )

        retried = self.client.post(
            f"/lectures/{lecture_id}/recording-finalize",
            headers=self.headers(),
        )
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertTrue(retried.json()["recording_finalized"])

    def test_finalize_without_saved_audio_is_idempotent_and_never_runs_inference(self):
        lecture_id = self.create_lecture()
        before = self.engine.calls
        first = self.client.post(
            f"/lectures/{lecture_id}/recording-finalize",
            headers=self.headers(),
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(
            first.json(),
            {"segments": [], "recording_available": False, "recording_finalized": True},
        )
        repeated = self.client.post(
            f"/lectures/{lecture_id}/recording-finalize",
            headers=self.headers(),
        )
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json(), first.json())
        self.assertEqual(self.engine.calls, before)

    def test_finalize_inference_failure_releases_claim_without_exposing_error(self):
        lecture_id = self.create_lecture()
        uploaded = self.upload(
            lecture_id,
            np.full(16_000, 321, dtype="<i2"),
            start=0,
            overlap=0,
            final=False,
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        guard_id = "server:recording-finalize-guard:v1"
        with mock.patch.object(
            self.engine,
            "transcribe",
            side_effect=RuntimeError("private-final-guard-error"),
        ), self.assertLogs("classroom", level="ERROR"):
            failed = self.client.post(
                f"/lectures/{lecture_id}/recording-finalize",
                headers=self.headers(),
            )
        self.assertEqual(failed.status_code, 503, failed.text)
        self.assertNotIn("private-final-guard-error", failed.text)
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT recording_finalized FROM lectures WHERE id = ?", (lecture_id,)
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM chunks WHERE lecture_id = ? AND chunk_id = ?",
                    (lecture_id, guard_id),
                ).fetchone()[0],
                0,
            )
        retried = self.client.post(
            f"/lectures/{lecture_id}/recording-finalize",
            headers=self.headers(),
        )
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertTrue(retried.json()["recording_finalized"])

    def test_delete_is_owner_only_removes_audio_and_invalidates_ticket(self):
        lecture_id, _ = self.finalized_recording()
        recording = self.app.state.recording_store.path("user-alpha", lecture_id)
        ticket = self.client.post(
            f"/lectures/{lecture_id}/recording-download-ticket",
            headers=self.headers(),
        ).json()["path"]

        hidden = self.client.delete(f"/lectures/{lecture_id}", headers=self.headers("user-beta"))
        self.assertEqual(hidden.status_code, 200, hidden.text)
        self.assertEqual(hidden.json(), {"status": "deleted"})
        self.assertTrue(recording.exists(), "another account's generic DELETE response must not remove data")
        self.assertEqual(self.client.get(f"/lectures/{lecture_id}", headers=self.headers()).status_code, 200)
        preflight = self.client.options(
            f"/lectures/{lecture_id}",
            headers={
                "Origin": "https://student.github.io",
                "Access-Control-Request-Method": "DELETE",
            },
        )
        self.assertEqual(preflight.status_code, 200)
        self.assertIn("DELETE", preflight.headers["access-control-allow-methods"])

        deleted = self.client.delete(f"/lectures/{lecture_id}", headers=self.headers())
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json(), {"status": "deleted"})
        self.assertFalse(recording.exists())
        self.assertEqual(self.client.get(f"/lectures/{lecture_id}", headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.get(ticket).status_code, 404)
        repeated = self.client.delete(f"/lectures/{lecture_id}", headers=self.headers())
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json(), {"status": "deleted"})
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM chunks WHERE lecture_id = ?", (lecture_id,)).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM segments WHERE lecture_id = ?", (lecture_id,)).fetchone()[0],
                0,
            )

    def test_delete_rejects_inference_race_then_succeeds(self):
        lecture_id = self.create_lecture()
        self.engine.block = True
        with ThreadPoolExecutor(max_workers=1) as executor:
            request = executor.submit(
                self.upload,
                lecture_id,
                np.full(800, 321, dtype="<i2"),
                start=0,
                overlap=0,
                final=False,
            )
            try:
                self.assertTrue(self.engine.entered.wait(timeout=5))
                rejected = self.client.delete(f"/lectures/{lecture_id}", headers=self.headers())
                self.assertEqual(rejected.status_code, 409, rejected.text)
            finally:
                self.engine.release.set()
            self.assertEqual(request.result(timeout=5).status_code, 200)
        self.engine.block = False
        self.assertEqual(
            self.client.delete(f"/lectures/{lecture_id}", headers=self.headers()).status_code,
            200,
        )

    def test_failed_unlink_leaves_durable_deletion_for_safe_retry(self):
        lecture_id, _ = self.finalized_recording()
        recording = self.app.state.recording_store.path("user-alpha", lecture_id)
        with mock.patch.object(self.app.state.recording_store, "delete", side_effect=OSError("locked")):
            with self.assertLogs("classroom", level="ERROR"):
                failed = self.client.delete(f"/lectures/{lecture_id}", headers=self.headers())
        self.assertEqual(failed.status_code, 503, failed.text)
        self.assertTrue(recording.exists())
        self.assertEqual(self.client.get(f"/lectures/{lecture_id}", headers=self.headers()).status_code, 404)
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT deleting FROM lectures WHERE id = ?", (lecture_id,)).fetchone()[0],
                1,
            )

        retried = self.client.delete(f"/lectures/{lecture_id}", headers=self.headers())
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertFalse(recording.exists())
        with self.database.connect() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM lectures WHERE id = ?", (lecture_id,)).fetchone())


if __name__ == "__main__":
    unittest.main()
