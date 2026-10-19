"""Synthetic live recording controls: no models, provider calls, or real data."""
from __future__ import annotations

import io
import threading
import time
import unittest
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import test_api as fixtures
from server.model_protocol import AlignmentUnavailableError, ModelRequestCancelledError, ModelUnavailableError
from server.recording_control import RecordingControl


class RecordingControlTests(unittest.TestCase):
    activate = fixtures.ApiTests.activate
    headers = fixtures.ApiTests.headers
    lecture = fixtures.ApiTests.lecture
    upload = fixtures.ApiTests.upload
    tearDown = fixtures.ApiTests.tearDown

    def setUp(self):
        fixtures.ApiTests.setUp(self)
        self.control = self.app.state.recording_control
        self.now = 1000.0
        self.control.clock = lambda: self.now
        self.token = self.activate()
        self.lecture_id = self.lecture(self.token)
        self.capture_id = str(uuid.uuid4())

    def command(self, action, *, capture=None, lecture=None, token=None, **extra):
        return self.client.post(f"/lectures/{lecture or self.lecture_id}/recording-control",
            headers=self.headers(token or self.token), json={
                "action": action, "capture_id": capture or self.capture_id, **extra})

    def status(self, lecture=None, token=None):
        return self.client.get(f"/lectures/{lecture or self.lecture_id}/recording-control",
                               headers=self.headers(token or self.token))

    def send(self, *, chunk_id=None, final=False, start="0"):
        return self.upload(self.token, self.lecture_id, chunk_id=chunk_id, start=start,
            extra_headers={"X-Recording-Capture-ID": self.capture_id, "X-Final-Chunk": str(final).lower()})

    def start(self):
        response = self.command("start")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def counts(self):
        with self.database.connect() as connection:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table} WHERE lecture_id=?",
                    (self.lecture_id,)).fetchone()[0] for table in ("chunks", "segments", "transcription_issues")}

    def test_owner_auth_capture_validation_and_safe_no_store(self):
        self.assertEqual(self.status().json()["state"], "unregistered")
        self.start()
        other = self.activate("user-beta")
        self.assertEqual(self.status(token=other).status_code, 404)
        self.assertEqual(self.command("stop", token=other).status_code, 404)
        unauth = self.client.get(f"/lectures/{self.lecture_id}/recording-control")
        self.assertEqual(unauth.status_code, 401)
        self.assertEqual(self.command("stop", capture="invalid").status_code, 422)
        mismatch = self.command("stop", capture=str(uuid.uuid4()))
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.json()["detail"]["code"], "recording_capture_mismatch")
        self.assertEqual(self.status().json()["state"], "active")
        self.assertEqual(self.status().headers["cache-control"], "no-store")
        self.assertEqual(self.upload(self.token, self.lecture_id).status_code, 409)
        self.assertEqual(self.engine.calls, 0)

    def test_no_text_expires_at_180_not_before_and_persists(self):
        first = self.start()
        self.assertEqual(first["deadline_at"], 1180)
        self.assertIsNone(first["last_text_at"])
        self.now = 1179.999
        self.assertEqual(self.control.expire_due(), 0)
        self.assertEqual(self.command("start").json()["deadline_at"], 1180)
        self.now = 1180
        self.assertEqual(self.control.expire_due(), 1)
        state = self.status().json()
        self.assertEqual((state["state"], state["reason"]), ("stopped", "no_text_timeout"))
        resumed = RecordingControl(self.database, self.clova.close_session, clock=lambda: 1400)
        self.assertEqual(resumed.status(self.lecture_id, "user-alpha")["state"], "stopped")
        self.assertEqual(self.send().status_code, 409)
        self.assertEqual(self.engine.calls, 0)

    def test_pause_excludes_elapsed_time_without_resetting_budget(self):
        self.start()
        self.now = 1100
        paused = self.command("pause").json()
        self.assertEqual(paused["state"], "paused")
        self.assertIsNone(paused["deadline_at"])
        self.now = 2000
        self.assertEqual(self.control.expire_due(), 0)
        self.assertEqual(self.command("resume").json()["deadline_at"], 2080)
        self.now = 2079
        self.assertEqual(self.control.expire_due(), 0)
        self.now = 2080
        self.assertEqual(self.control.expire_due(), 1)

    def test_paused_capture_does_not_exempt_an_outstanding_180_second_request(self):
        self.start()
        with self.control.request(self.lecture_id, "user-alpha", self.capture_id) as pending:
            self.now = 1100
            self.command("pause")
            self.now = 1179.999
            self.assertEqual(self.control.expire_due(), 0)
            self.assertFalse(pending.cancel_event.is_set())
            self.now = 1180
            self.assertEqual(self.control.expire_due(), 1)
            self.assertTrue(pending.cancel_event.is_set())
            self.assertEqual(self.status().json()["state"], "stopped")

    def test_paused_short_failures_cannot_restart_provider_after_original_deadline(self):
        self.start()
        self.now = 1010
        self.command("pause")
        calls = []
        def transcribe(*args):
            calls.append(1)
            raise ModelUnavailableError("model_timeout")
        self.engine.transcribe = transcribe
        for self.now in (1100, 1179):
            self.assertEqual(self.send().status_code, 503)
        self.now = 1180
        response = self.send()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.status().json()["state"], "stopped")

    def test_paused_late_result_is_fenced_before_watchdog_tick(self):
        self.start()
        self.now = 1010
        self.command("pause")
        def transcribe(*args):
            self.now = 1180
            return [{"start": 0, "end": 1, "text": "합성 늦은 결과"}]
        self.engine.transcribe = transcribe
        self.assertEqual(self.send().status_code, 409)
        self.assertEqual(self.counts()["segments"], 0)
        self.assertEqual(self.status().json()["state"], "stopped")

    def test_silence_replay_presence_and_raw_audio_never_extend_text_deadline(self):
        self.start()
        self.engine.result = []
        self.now = 1100
        chunk = str(uuid.uuid4())
        self.assertEqual(self.send(chunk_id=chunk).status_code, 200)
        self.now = 1170
        self.assertEqual(self.send(chunk_id=chunk).status_code, 200)
        self.client.post("/presence", headers=self.headers(self.token), json={"activity": "recording"})
        state = self.status().json()
        self.assertEqual(state["deadline_at"], 1180)
        self.assertIsNone(state["last_text_at"])
        self.now = 1180
        self.assertEqual(self.control.expire_due(), 1)

    def test_only_new_committed_text_advances_deadline_and_normal_final_finishes(self):
        self.start()
        self.now = 1100
        chunk = str(uuid.uuid4())
        self.assertEqual(self.send(chunk_id=chunk).status_code, 200)
        state = self.status().json()
        self.assertEqual((state["last_text_at"], state["deadline_at"]), (1100, 1280))
        self.now = 1200
        self.assertEqual(self.send(chunk_id=chunk).status_code, 200)
        self.assertEqual(self.status().json()["deadline_at"], 1280)
        last_chunk = str(uuid.uuid4())
        self.assertEqual(self.send(chunk_id=last_chunk, final=True, start="1").status_code, 200)
        self.assertEqual(self.status().json()["state"], "finished")
        self.assertEqual(self.command("start").json()["state"], "finished")
        self.assertEqual(self.send(chunk_id=last_chunk, final=True, start="1").status_code, 200)
        self.now = 4000
        self.assertEqual(self.control.expire_due(), 0)

    def test_stop_before_start_is_durable_and_does_not_reactivate(self):
        self.assertEqual(self.command("stop").status_code, 200)
        for action in ("start", "pause", "resume"):
            response = self.command(action)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "recording_stopped")
        self.assertEqual(self.command("stop").status_code, 200)
        self.assertEqual(self.send().status_code, 409)
        self.assertEqual(self.engine.calls, 0)

    def test_late_success_is_not_committed_and_only_target_request_cancelled(self):
        self.start()
        entered, release = threading.Event(), threading.Event()
        observed = []
        def transcribe(samples, language, overlap, final, *, cancel_event):
            observed.append(cancel_event)
            entered.set()
            self.assertTrue(release.wait(5))
            return [{"start": 0, "end": 1, "text": "합성 늦은 결과"}]
        self.engine.supports_cancellation = True
        self.engine.transcribe = transcribe
        second = self.lecture(self.token)
        other_capture = str(uuid.uuid4())
        self.command("start", lecture=second, capture=other_capture)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.send)
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(self.command("stop").status_code, 200)
                self.assertTrue(observed[0].is_set())
                self.assertEqual(self.status(lecture=second).json()["state"], "active")
            finally:
                release.set()
            response = future.result(5)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.counts(), {"chunks": 0, "segments": 0, "transcription_issues": 0})
        self.assertFalse((self.settings.data_dir / "recordings" / "user-alpha" / f"{self.lecture_id}.wav").exists())

    def test_deadline_fence_rejects_late_text_even_before_watchdog_tick(self):
        self.start()
        def transcribe(*args):
            self.now = 1180
            return [{"start": 0, "end": 1, "text": "합성 늦은 결과"}]
        self.engine.transcribe = transcribe
        response = self.send()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.status().json()["state"], "stopped")
        self.assertEqual(self.counts()["segments"], 0)

    def test_deadline_elapsed_during_raw_save_cannot_renew_transcript(self):
        self.start()
        self.now = 1179
        store = self.app.state.recording_store
        original = store.write_chunk
        def slow_save(*args, **kwargs):
            result = original(*args, **kwargs)
            self.now = 1180
            return result
        with mock.patch.object(store, "write_chunk", side_effect=slow_save):
            response = self.send()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.counts()["segments"], 0)
        self.assertEqual(self.status().json()["state"], "stopped")

    def test_cancelled_model_is_terminal_not_retryable_and_does_not_log_error(self):
        self.start()
        def transcribe(*args, cancel_event):
            self.command("stop")
            self.assertTrue(cancel_event.is_set())
            raise ModelRequestCancelledError()
        self.engine.supports_cancellation = True
        self.engine.transcribe = transcribe
        with mock.patch("server.app.log.exception") as logger:
            response = self.send()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.headers["x-local-model-retryable"], "0")
        logger.assert_not_called()

    def test_late_alignment_error_does_not_store_partial_text(self):
        self.start()
        def transcribe(*args):
            self.command("stop")
            raise AlignmentUnavailableError("합성 임시 텍스트")
        self.engine.transcribe = transcribe
        self.assertEqual(self.send().status_code, 409)
        self.assertEqual(self.counts()["transcription_issues"], 0)

    def test_stopped_raw_tail_can_upload_and_download_without_new_asr(self):
        self.start()
        self.command("stop")
        audio = fixtures.wav_audio()
        raw = self.client.post(f"/lectures/{self.lecture_id}/recording-chunks", content=audio,
            headers=self.headers(self.token) | {"Content-Type": "audio/wav", "X-Chunk-ID": str(uuid.uuid4()),
                "X-Start-Seconds": "0", "X-Final-Chunk": "true"})
        self.assertEqual(raw.status_code, 200, raw.text)
        self.assertTrue(raw.json()["recording_audio_finalized"])
        ticket = self.client.post(f"/lectures/{self.lecture_id}/recording-download-ticket", headers=self.headers(self.token))
        self.assertEqual(ticket.status_code, 200, ticket.text)
        download = self.client.get(ticket.json()["path"])
        self.assertEqual(download.status_code, 200)
        with wave.open(io.BytesIO(download.content)) as stream:
            self.assertEqual(stream.getnframes(), 16000)
        self.assertEqual(self.engine.calls, 0)
        self.assertEqual(self.status().json()["state"], "stopped")

    def test_stopped_final_guard_never_invokes_model_and_saved_results_remain_readable(self):
        self.start()
        chunk = str(uuid.uuid4())
        self.assertEqual(self.send(chunk_id=chunk).status_code, 200)
        self.command("stop")
        response = self.client.post(f"/lectures/{self.lecture_id}/recording-finalize",
            headers=self.headers(self.token) | {"X-Recording-Capture-ID": self.capture_id})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.engine.calls, 1)
        result = self.client.get(f"/lectures/{self.lecture_id}/chunks/{chunk}/result",
            headers=fixtures.ApiTests.chunk_result_headers(self, self.token, fixtures.wav_audio()))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["state"], "done")

    def test_file_imports_cannot_be_registered_or_stopped_and_legacy_remains_compatible(self):
        with self.database.connect() as connection:
            connection.execute("INSERT INTO imports(id,username,lecture_id,title,filename,file_fingerprint,total_bytes,status,"
                               "created_at,updated_at) VALUES(?,?,?,?,?,?,?,'processing',?,?)",
                               (str(uuid.uuid4()), "user-alpha", self.lecture_id, "Synthetic", "synthetic.wav", "a" * 64, 100, "now", "now"))
        self.assertEqual(self.command("start").status_code, 409)
        self.assertEqual(self.command("stop").status_code, 409)
        legacy = self.lecture(self.token)
        self.assertEqual(self.upload(self.token, legacy).status_code, 200)
        self.now = 9999
        self.assertEqual(self.control.expire_due(), 0)

    def test_clova_stop_closes_only_target_and_late_failure_is_terminal(self):
        self.clova.configured = True
        self.lecture_id = self.lecture(self.token, asr_provider="clova")
        self.start()
        def transcribe(*args, **kwargs):
            self.command("stop")
            raise fixtures.ClovaTranscriptionError("timeout")
        self.clova.transcribe = transcribe
        response = self.send()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.clova.closed_sessions, [("user-alpha", self.lecture_id)])
        self.assertEqual(self.counts()["segments"], 0)

    def test_restart_expires_due_active_sessions_and_keeps_paused_deadlines(self):
        self.start()
        second = self.lecture(self.token)
        capture = str(uuid.uuid4())
        self.command("start", lecture=second, capture=capture)
        self.command("pause", lecture=second, capture=capture)
        restarted = RecordingControl(self.database, self.clova.close_session, clock=lambda: 3000)
        self.assertEqual(restarted.expire_due(), 1)
        self.assertEqual(restarted.status(self.lecture_id, "user-alpha")["state"], "stopped")
        self.assertEqual(restarted.status(second, "user-alpha")["state"], "paused")

    def test_other_owner_request_event_and_state_are_untouched(self):
        self.start()
        other_token = self.activate("user-beta")
        other_lecture = self.lecture(other_token)
        other_capture = str(uuid.uuid4())
        self.command("start", token=other_token, lecture=other_lecture, capture=other_capture)
        with self.control.request(self.lecture_id, "user-alpha", self.capture_id) as own:
            with self.control.request(other_lecture, "user-beta", other_capture) as other:
                self.command("stop")
                self.assertTrue(own.cancel_event.is_set())
                self.assertFalse(other.cancel_event.is_set())
                self.assertEqual(self.status(other_lecture, other_token).json()["state"], "active")

    def test_final_guard_in_flight_cancel_cannot_commit_or_finalize(self):
        self.start()
        self.assertEqual(self.send().status_code, 200)
        self.engine.block = True
        self.engine.entered.clear()
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.client.post, f"/lectures/{self.lecture_id}/recording-finalize",
                headers=self.headers(self.token) | {"X-Recording-Capture-ID": self.capture_id})
            self.assertTrue(self.engine.entered.wait(5))
            try:
                self.command("stop")
            finally:
                self.engine.release.set()
            response = future.result(5)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.counts()["segments"], 1)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT recording_finalized FROM lectures WHERE id=?",
                (self.lecture_id,)).fetchone()[0], 0)

    def test_capture_header_allowed_by_cors(self):
        response = self.client.options(f"/lectures/{self.lecture_id}/chunks", headers={
            "Origin": "https://student.github.io", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,x-recording-capture-id,content-type"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("x-recording-capture-id", response.headers["access-control-allow-headers"].lower())


if __name__ == "__main__":
    unittest.main()
