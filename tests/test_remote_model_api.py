"""API-side model isolation regressions with only temporary synthetic audio.

Socket codec/server tests live elsewhere. These tests exercise the real HTTP,
SQLite, append-only recording and import worker integration. No provider or
model is loaded; the one actual remote adapter points to an absent temp socket.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
import wave
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server.app import create_app
from server.clova_transcriber import ClovaTranscriptionError
from server.model_protocol import ModelUnavailableError
from server.security import digest
from server.settings import Settings
from server.settings import PROJECT_DIR
import test_api as api_fixture
import test_import_api as import_fixture


class BoundaryProbe:
    """Stateless fake that can fail after mutating an uncommitted output map."""

    supports_boundary_context = True

    def __init__(self, fail_from=None, error=None):
        self.calls = 0
        self.inputs = []
        self.fail_from = fail_from
        self.error = error or ModelUnavailableError("model_unavailable")
        self.available = threading.Event()
        self.failed = threading.Event()
        self.release = threading.Event()
        if fail_from is None:
            self.available.set()

    def status(self):
        return {"model_state": "ready" if self.available.is_set() else "unloaded",
                "model": "synthetic-local", "engine": "remote", "device": "cpu"}

    def transcribe(self, samples, language, overlap_seconds=0, final_chunk=True,
                   *, start_seconds=0, boundary_context=None, boundary_output=None):
        self.calls += 1
        self.inputs.append({
            "start": start_seconds, "overlap": overlap_seconds, "final": final_chunk,
            "context": copy.deepcopy(boundary_context),
            "samples": hashlib.sha256(samples.tobytes()).hexdigest(),
        })
        output = {
            "version": 1, "audio_end": start_seconds + len(samples) / 16000,
            "tokens": [{"text": "synthetic-private-boundary", "start": start_seconds,
                        "end": start_seconds + 0.2, "emitted": True}],
        }
        if boundary_output is not None:
            boundary_output.update(output)
        if self.fail_from is not None and start_seconds >= self.fail_from and not self.available.is_set():
            self.failed.set()
            raise self.error
        duration = len(samples) / 16000
        begin = max(0, overlap_seconds - 0.1) if overlap_seconds == duration else overlap_seconds
        return [{"start": begin, "end": duration, "text": "합성 수업 문장"}]


class RemoteModelChunkApiTests(unittest.TestCase):
    # Reuse setup helpers without inheriting unrelated tests.
    setUp = api_fixture.ApiTests.setUp
    tearDown = api_fixture.ApiTests.tearDown
    activate = api_fixture.ApiTests.activate
    headers = api_fixture.ApiTests.headers
    lecture = api_fixture.ApiTests.lecture
    upload = api_fixture.ApiTests.upload

    def use_probe(self, probe):
        self.engine.supports_boundary_context = True
        self.engine.transcribe = probe.transcribe
        return probe

    def rows(self, lecture_id):
        with self.database.connect() as connection:
            chunks = [dict(row) for row in connection.execute(
                "SELECT * FROM chunks WHERE lecture_id=? ORDER BY start_seconds, rowid", (lecture_id,))]
            segments = [dict(row) for row in connection.execute(
                "SELECT * FROM segments WHERE lecture_id=? ORDER BY start, end, id", (lecture_id,))]
            finalized = connection.execute(
                "SELECT recording_finalized FROM lectures WHERE id=?", (lecture_id,)).fetchone()[0]
        return chunks, segments, finalized

    def recording(self, lecture_id):
        return self.settings.data_dir / "recordings" / "user-alpha" / f"{lecture_id}.wav"

    def test_all_typed_outages_are_marked_safe_503_without_partial_commit(self):
        token = self.activate()
        lecture_id = self.lecture(token)
        chunk_id = str(uuid.uuid4())
        probe = self.use_probe(BoundaryProbe(fail_from=0))
        for code in ("model_unavailable", "model_loading", "model_busy", "model_timeout", "model_protocol_error"):
            with self.subTest(code=code):
                probe.error = ModelUnavailableError(code)
                probe.error.__cause__ = RuntimeError("synthetic-private-socket-and-upstream-detail")
                with mock.patch("server.app.log.exception") as trace:
                    response = self.upload(token, lecture_id, chunk_id=chunk_id)
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.headers.get("X-Local-Model-Retryable"), "1")
                self.assertIn("Retry-After", response.headers)
                self.assertNotIn("synthetic-private", response.text)
                trace.assert_not_called()
                self.assertEqual(self.rows(lecture_id), ([], [], 0))
                self.assertFalse(self.recording(lecture_id).exists())
        probe.available.set()
        self.assertEqual(self.upload(token, lecture_id, chunk_id=chunk_id).status_code, 200)

    def test_same_chunk_retry_preserves_committed_boundary_pcm_ids_and_final_replay(self):
        token = self.activate()
        lecture_id = self.lecture(token)
        probe = self.use_probe(BoundaryProbe(fail_from=5))
        payload = api_fixture.wav_audio(seconds=8)
        first = self.upload(token, lecture_id, payload, extra_headers={"X-Final-Chunk": "false"})
        self.assertEqual(first.status_code, 200)
        before_rows = self.rows(lecture_id)
        before_audio = self.recording(lecture_id).read_bytes()
        chunk_id = str(uuid.uuid4())
        failed = self.upload(token, lecture_id, payload, chunk_id, "5", {"X-Overlap-Seconds": "3"})
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(self.rows(lecture_id), before_rows)
        self.assertEqual(self.recording(lecture_id).read_bytes(), before_audio)
        probe.available.set()
        recovered = self.upload(token, lecture_id, payload, chunk_id, "5", {"X-Overlap-Seconds": "3"})
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(probe.inputs[1], probe.inputs[2], "retry uses the same PCM and committed boundary")
        self.assertEqual(probe.inputs[2]["context"], json.loads(before_rows[0][0]["qwen_boundary_json"]))
        chunks, segments, finalized = self.rows(lecture_id)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0], before_rows[1][0])
        self.assertEqual(finalized, 1)
        with wave.open(io.BytesIO(self.recording(lecture_id).read_bytes()), "rb") as wav:
            self.assertEqual(wav.getnframes(), 13 * 16000)
            self.assertEqual(wav.readframes(wav.getnframes()), b"\xe8\x03" * (13 * 16000))
        calls = probe.calls
        probe.available.clear()
        replay = self.upload(token, lecture_id, payload, chunk_id, "5", {"X-Overlap-Seconds": "3"})
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), recovered.json())
        self.assertEqual(probe.calls, calls, "saved final ACK works even while model is down again")

    def test_failed_final_guard_releases_claim_and_retries_without_touching_pcm(self):
        token = self.activate()
        lecture_id = self.lecture(token)
        probe = self.use_probe(BoundaryProbe())
        self.assertEqual(self.upload(token, lecture_id, api_fixture.wav_audio(seconds=8),
                                    extra_headers={"X-Final-Chunk": "false"}).status_code, 200)
        before_rows = self.rows(lecture_id)
        before_audio = self.recording(lecture_id).read_bytes()
        probe.fail_from = 0
        probe.available.clear()
        response = self.client.post(f"/lectures/{lecture_id}/recording-finalize", headers=self.headers(token))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("X-Local-Model-Retryable"), "1")
        self.assertEqual(self.rows(lecture_id), before_rows)
        self.assertEqual(self.recording(lecture_id).read_bytes(), before_audio)
        probe.available.set()
        recovered = self.client.post(f"/lectures/{lecture_id}/recording-finalize", headers=self.headers(token))
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertTrue(recovered.json()["recording_finalized"])
        self.assertEqual(probe.inputs[1], probe.inputs[2])
        self.assertEqual(self.recording(lecture_id).read_bytes(), before_audio)
        self.assertEqual(len(self.rows(lecture_id)[0]), 2)
        calls = probe.calls
        probe.available.clear()
        replay = self.client.post(f"/lectures/{lecture_id}/recording-finalize", headers=self.headers(token))
        self.assertEqual(replay.json(), recovered.json())
        self.assertEqual(probe.calls, calls)

    def test_owner_denial_precedes_model_failure_and_never_exposes_boundary(self):
        token = self.activate()
        other = self.activate("user-beta")
        lecture_id = self.lecture(token)
        probe = self.use_probe(BoundaryProbe(fail_from=0))
        self.assertEqual(self.upload(other, lecture_id).status_code, 404)
        self.assertEqual(self.client.post(f"/lectures/{lecture_id}/recording-finalize",
                                         headers=self.headers(other)).status_code, 404)
        self.assertEqual(probe.calls, 0)

    def test_clova_ambiguous_failure_does_not_become_local_retryable(self):
        token = self.activate()
        probe = self.use_probe(BoundaryProbe(fail_from=0))
        self.clova.configured = True
        self.clova.error = ClovaTranscriptionError("provider_error")
        lecture_id = self.lecture(token, asr_provider="clova")
        response = self.upload(token, lecture_id)
        self.assertEqual(response.status_code, 424)
        self.assertNotIn("X-Local-Model-Retryable", response.headers)
        self.assertEqual(probe.calls, 0)
        self.assertEqual(self.clova.calls, 1)


class MissingModelApiTests(unittest.TestCase):
    def test_absent_socket_never_loads_local_model_or_blocks_api_auth_and_clova(self):
        with tempfile.TemporaryDirectory(prefix="stt-remote-api-") as directory:
            root = Path(directory)
            settings = Settings(data_dir=root / "data", model_cache_dir=root / "models",
                                local_model_socket=root / "model" / "missing.sock",
                                local_model_timeout_seconds=5, model_warmup=True,
                                site_origins=("https://student.github.io",), device="cpu")
            clova = api_fixture.FakeClovaTranscriber(configured=True)
            with mock.patch("server.app.LocalTranscriber", side_effect=AssertionError("API must not load Qwen")):
                app = create_app(settings, clova_transcriber=clova)
                code = "a" * 43
                with app.state.database.connect() as connection:
                    connection.execute("UPDATE users SET setup_hash=?,setup_expires=? WHERE username=?",
                                       (digest(code), time.time() + 3600, "user-alpha"))
                with TestClient(app) as client:
                    self.assertEqual(client.get("/health").status_code, 200)
                    activated = client.post("/auth/activate", json={
                        "username": "user-alpha", "setup_code": code, "password": "synthetic-password"})
                    self.assertEqual(activated.status_code, 200)
                    login = client.post("/auth/login", json={
                        "username": "user-alpha", "password": "synthetic-password"})
                    self.assertEqual(login.status_code, 200)
                    headers = {"Authorization": f"Bearer {login.json()['token']}"}
                    self.assertEqual(client.get("/auth/me", headers=headers).status_code, 200)
                    status = client.get("/status", headers=headers)
                    self.assertEqual(status.status_code, 200)
                    self.assertNotEqual(status.json()["model_state"], "ready")
                    self.assertTrue(status.json()["transcription_providers"]["clova"]["configured"])
                    self.assertNotIn(str(root), status.text)
                    for provider in ("qwen", "clova"):
                        created = client.post("/lectures", headers=headers,
                                              json={"title": "합성 수업", "language": "ko", "asr_provider": provider})
                        self.assertEqual(created.status_code, 201)
                        response = client.post(f"/lectures/{created.json()['id']}/chunks",
                                               content=api_fixture.wav_audio(), headers=headers | {
                                                   "Content-Type": "audio/wav", "X-Chunk-Id": str(uuid.uuid4()),
                                                   "X-Start-Seconds": "0", "Origin": "https://student.github.io"})
                        self.assertEqual(response.status_code, 503 if provider == "qwen" else 200, response.text)
                        if provider == "qwen":
                            self.assertEqual(response.headers.get("X-Local-Model-Retryable"), "1")
                            exposed = response.headers.get("Access-Control-Expose-Headers", "").lower()
                            self.assertIn("x-local-model-retryable", exposed)
                            self.assertNotIn(str(root), response.text)
                    self.assertEqual(clova.calls, 1)
                    self.assertEqual(client.get("/health").status_code, 200)


class RemoteModelImportApiTests(unittest.TestCase):
    setUp = import_fixture.ImportApiTests.setUp
    tearDown = import_fixture.ImportApiTests.tearDown
    headers = import_fixture.ImportApiTests.headers
    create = import_fixture.ImportApiTests.create
    put = import_fixture.ImportApiTests.put
    wait_terminal = import_fixture.ImportApiTests.wait_terminal

    def start_outage_import(self, *, error=None):
        probe = BoundaryProbe(fail_from=12, error=error)
        self.engine.supports_boundary_context = True
        self.engine.transcribe = probe.transcribe
        payload = import_fixture.wav_file(31)
        result = self.create(payload)
        self.assertEqual(result.status_code, 201, result.text)
        import_id = result.json()["id"]
        part_bytes = result.json()["part_bytes"]
        for offset in range(0, len(payload), part_bytes):
            self.assertEqual(self.put(import_id, payload[offset:offset + part_bytes], offset).status_code, 200)
        queued = self.client.post(f"/imports/{import_id}/complete", headers=self.headers())
        self.assertEqual(queued.status_code, 200, queued.text)
        self.assertTrue(probe.failed.wait(5), "second synthetic chunk must reach the unavailable model")
        return probe, payload, import_id

    def job(self, import_id):
        response = self.client.get(f"/imports/{import_id}", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        return response.json()

    def raw_path(self, import_id):
        return self.settings.data_dir / "imports" / "user-alpha" / f"{import_id}.upload"

    def committed(self, lecture_id, app=None):
        with (app or self.app).state.database.connect() as connection:
            chunks = [dict(row) for row in connection.execute(
                "SELECT * FROM chunks WHERE lecture_id=? AND status='done' ORDER BY start_seconds", (lecture_id,))]
            segments = [dict(row) for row in connection.execute(
                "SELECT * FROM segments WHERE lecture_id=? ORDER BY start,end,id", (lecture_id,))]
        return chunks, segments

    def test_outage_wait_keeps_raw_partial_and_resumes_identical_chunk(self):
        probe, payload, import_id = self.start_outage_import()
        job = self.job(import_id)
        self.assertEqual(job["status"], "processing")
        self.assertFalse(job["raw_deleted"])
        lecture_id = job["lecture_id"]
        before = self.committed(lecture_id)
        self.assertEqual(len(before[0]), 1)
        self.assertEqual(self.raw_path(import_id).read_bytes(), payload)
        probe.available.set()
        completed = self.wait_terminal(import_id)
        self.assertEqual(completed["status"], "completed")
        after = self.committed(lecture_id)
        self.assertEqual(len(after[0]), 3)
        self.assertEqual(len(after[1]), 3)
        self.assertEqual(after[0][0], before[0][0])
        self.assertEqual(after[1][0], before[1][0])
        self.assertEqual(probe.inputs[1], probe.inputs[2])
        expected_ids = [str(uuid.uuid5(uuid.UUID(import_id), f"audio-chunk:{index}")) for index in range(3)]
        self.assertEqual([row["chunk_id"] for row in after[0]], expected_ids)
        self.assertTrue(completed["raw_deleted"])
        recording = self.settings.data_dir / "recordings" / "user-alpha" / f"{lecture_id}.wav"
        self.assertEqual(recording.read_bytes(), payload)
        self.assertEqual(self.clova.calls, 0)

    def test_explicit_cancel_interrupts_backoff_and_keeps_existing_delete_semantics(self):
        probe, _payload, import_id = self.start_outage_import()
        lecture_id = self.job(import_id)["lecture_id"]
        self.assertEqual(len(self.committed(lecture_id)[0]), 1)
        started = time.monotonic()
        response = self.client.post(f"/imports/{import_id}/cancel", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        cancelled = self.wait_terminal(import_id)
        self.assertLess(time.monotonic() - started, 2.5, "cancel wakes outage wait rather than waiting for the model")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["raw_deleted"])
        self.assertIsNone(cancelled["lecture_id"])
        self.assertFalse(self.raw_path(import_id).exists())
        self.assertEqual(self.committed(lecture_id), ([], []))
        self.assertEqual(probe.calls, 2)

    def test_shutdown_during_outage_retains_partial_and_restart_replays_only_missing_chunks(self):
        probe, payload, import_id = self.start_outage_import()
        lecture_id = self.job(import_id)["lecture_id"]
        before = self.committed(lecture_id)
        started = time.monotonic()
        self.app.state.stop_import_worker(timeout=2)
        self.assertLess(time.monotonic() - started, 1.5, "shutdown must interrupt exponential outage wait")
        self.assertEqual(self.job(import_id)["status"], "queued")
        self.assertEqual(self.raw_path(import_id).read_bytes(), payload)
        self.assertEqual(self.committed(lecture_id), before)
        self.assertEqual(probe.calls, 2)
        resumed_engine = BoundaryProbe()
        resumed_app = create_app(self.settings, resumed_engine, clova_transcriber=self.clova)
        with TestClient(resumed_app) as client:
            completed = self.wait_terminal(import_id, client, self.tokens["user-alpha"])
            self.assertEqual(completed["status"], "completed")
            after = self.committed(lecture_id, resumed_app)
            self.assertEqual(len(after[0]), 3)
            self.assertEqual(after[0][0], before[0][0])
            self.assertEqual(after[1][0], before[1][0])
            self.assertEqual(resumed_engine.calls, 2, "committed first chunk is replayed without inference")
            self.assertEqual(resumed_engine.inputs[0], probe.inputs[1])
            self.assertTrue(completed["raw_deleted"])
            self.assertFalse(self.raw_path(import_id).exists())

    def test_unmarked_model_failure_does_not_get_an_indefinite_import_retry(self):
        with mock.patch("server.app.log.exception"):
            probe, _payload, import_id = self.start_outage_import(error=RuntimeError("synthetic-failure"))
            failed = self.wait_terminal(import_id)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(probe.calls, 2)
        self.assertNotIn("synthetic-failure", json.dumps(failed))
        self.assertEqual(self.clova.calls, 0)


class RemoteModelSettingsTests(unittest.TestCase):
    def settings(self, **changes):
        return Settings(data_dir=Path("/tmp/synthetic-data"),
                        model_cache_dir=Path("/tmp/synthetic-models"), **changes)

    def test_legacy_inline_default_and_private_socket_repr(self):
        default = self.settings()
        self.assertIsNone(default.local_model_socket)
        self.assertEqual(default.local_model_timeout_seconds, 90)
        private_path = Path("/tmp/synthetic-private-model.sock")
        remote = self.settings(local_model_socket=private_path)
        self.assertEqual(remote.local_model_socket, private_path)
        self.assertNotIn(str(private_path), repr(remote))

    def test_socket_and_timeout_bounds_fail_closed_without_disclosing_values(self):
        for value in (Path("relative.sock"), "/tmp/string-not-path.sock", Path("/tmp/bad\x00.sock"),
                      Path("/tmp/" + "가" * 35 + ".sock")):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(ValueError) as failure:
                    self.settings(local_model_socket=value)
                self.assertNotIn(str(value), str(failure.exception))
        for value in (True, False, None, "90", 4.9, 300.1, float("inf"), float("nan")):
            with self.subTest(timeout_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    self.settings(local_model_timeout_seconds=value)
        for value in (5, 300):
            self.assertEqual(self.settings(local_model_timeout_seconds=value).local_model_timeout_seconds, value)

    def test_relative_env_socket_normalizes_without_loading_private_dotenv(self):
        with mock.patch.dict(os.environ, {
            "ACCOUNT_USERNAMES": "user-alpha,user-beta", "LOCAL_MODEL_SOCKET": ".data/model/model.sock",
            "LOCAL_MODEL_TIMEOUT_SECONDS": "125.5",
        }, clear=True), mock.patch("server.settings.load_dotenv"):
            settings = Settings.from_env()
        self.assertEqual(settings.local_model_socket, PROJECT_DIR / ".data/model/model.sock")
        self.assertEqual(settings.local_model_timeout_seconds, 125.5)


if __name__ == "__main__":
    unittest.main()
