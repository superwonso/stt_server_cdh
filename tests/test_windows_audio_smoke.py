"""Audio smoke contract checks; generated PCM and FakeEngine, no real model."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
import numpy as np
from fastapi.testclient import TestClient

from server.app import create_app
from server.security import digest
from server.settings import Settings
from server.windows_audio_smoke import (API_URL, LocalClient, SmokeFailure, audio_checks,
                                        chunk_plan, ensure_isolation, fingerprint, main, no_link_path, wav_bytes)
from server.windows_local import new_profile


class AudioScopeTests(unittest.TestCase):
    def test_exact_chunk_pcm_reconstructs_without_duplicate_overlap(self):
        for frames in (16000, 56080, 174859, 30 * 16000):
            pcm = np.arange(frames, dtype="<i2").tobytes()
            recovered = bytearray()
            for begin, overlap, part in chunk_plan(pcm):
                self.assertLessEqual(len(wav_bytes(part)), 512000)
                self.assertLessEqual(overlap, 48000)
                self.assertEqual(begin + overlap, len(recovered) // 2)
                recovered.extend(part[overlap * 2:])
            self.assertEqual(bytes(recovered), pcm)

    def test_nonlocal_targets_and_out_of_scope_samples_refused(self):
        with httpx.Client(base_url="https://external.invalid") as client:
            with self.assertRaises(SmokeFailure):
                LocalClient(client)
        with httpx.Client(base_url=API_URL) as client:
            local = LocalClient(client)
            for path in ("//external.invalid", "/x?token=secret", "/../credentials", "https://external.invalid"):
                with self.assertRaises(SmokeFailure):
                    local.request("GET", path)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(SmokeFailure):
                no_link_path(root.parent / "unapproved.wav", root)

    def test_preflight_never_invokes_model_or_api_and_exceptions_are_redacted(self):
        pcm = bytes(32000)
        sample = {"id": "synthetic-test", "category": "synthetic", "language": "ko", "pcm": pcm}
        with patch("server.windows_audio_smoke.load_samples", return_value=[sample]), \
             patch("server.windows_audio_smoke.run") as runner, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]), 0)
        runner.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["transcription_performed"])
        with patch("server.windows_audio_smoke.load_samples", side_effect=OSError("private-secret")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]), 1)
        self.assertNotIn("private-secret", output.getvalue())

    def test_model_readiness_waits_for_the_api_cached_probe(self):
        ready = {"model_state": "ready", "transcription_providers": {"clova": {"configured": False}},
                 **{name: {"configured": False} for name in
                    ("postprocessing", "summarization", "translation", "question_answering", "study_notes")}}
        overview = {"drive": {"enabled": False, "configured": False},
                    "backup": {"enabled": False, "configured": False}, "tunnel": {"restart_available": False}}
        from unittest.mock import Mock
        api = Mock()
        api.request.side_effect = [{"model_state": "offline"}, ready, overview]
        with patch("server.windows_audio_smoke.time.sleep") as pause:
            ensure_isolation(api, {})
        pause.assert_called_once_with(.25)
        self.assertEqual(api.request.call_count, 3)

    def test_ordered_full_part_fingerprint_detects_middle_changes(self):
        payload = bytes(600000)
        changed = bytearray(payload)
        changed[100000] ^= 1
        self.assertNotEqual(fingerprint(payload), fingerprint(bytes(changed)))


class FakeEngine:
    calls = 0

    def status(self):
        return {"model_state": "ready", "model": "synthetic-model", "device": "cpu"}

    def transcribe(self, samples, language, overlap_seconds=0, final_chunk=True):
        self.calls += 1
        duration = len(samples) / 16000
        return [{"start": max(0, overlap_seconds - .2), "end": duration, "text": "합성 sample"}]


class AppTransport(httpx.BaseTransport):
    def __init__(self, client):
        self.client = client

    def handle_request(self, incoming):
        assert incoming.url.host == "127.0.0.1" and incoming.url.port == 18766
        response = self.client.request(incoming.method, str(incoming.url), headers=incoming.headers,
                                       content=incoming.content)
        return httpx.Response(response.status_code, headers=response.headers,
                              stream=httpx.ByteStream(response.content))


class AudioContractTests(unittest.TestCase):
    def test_entire_audio_workflow_against_real_api_with_fake_model(self):
        profile = new_profile()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            accounts = tuple(account["username"] for account in profile["accounts"])
            settings = Settings(data_dir=root / "data", model_cache_dir=root / "models", accounts=accounts,
                                admin_username=accounts[0], model_warmup=False)
            engine = FakeEngine()
            app = create_app(settings, engine)
            with TestClient(app, base_url=API_URL) as inner:
                for index, account in enumerate(profile["accounts"]):
                    code = str(index) * 43
                    with app.state.database.connect() as connection:
                        connection.execute("UPDATE users SET setup_hash=?,setup_expires=? WHERE username=?",
                                           (digest(code), time.time() + 3600, account["username"]))
                    activated = inner.post("/auth/activate", json={"username": account["username"],
                                           "setup_code": code, "password": account["password"]})
                    self.assertEqual(activated.status_code, 200)
                    inner.post("/auth/logout", headers={"Authorization": "Bearer " + activated.json()["token"]})
                pcm = np.full(3 * 16000, 1234, dtype="<i2").tobytes()
                samples = [{"id": f"synthetic-{index}", "category": "synthetic", "language": language,
                            "source_sha256": "0" * 64, "pcm": pcm} for index, language in enumerate(("ko", "en", "ko", "en"))]
                with httpx.Client(base_url=API_URL, transport=AppTransport(inner)) as client:
                    result = audio_checks(LocalClient(client), profile, samples)
                self.assertTrue(all(result["checks"].values()))
                self.assertEqual(result["sample_count"], 4)
                self.assertEqual(result["synthetic_audio_lectures_retained"], 8)
                self.assertGreater(engine.calls, 0)
                with app.state.database.connect() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM lectures").fetchone()[0], 8)
                # No session/account/transcript contents are part of the report.
                rendered = json.dumps(result)
                self.assertNotIn("합성 sample", rendered)
                for account in profile["accounts"]:
                    self.assertNotIn(account["username"], rendered)
                    self.assertNotIn(account["password"], rendered)


if __name__ == "__main__":
    unittest.main()
