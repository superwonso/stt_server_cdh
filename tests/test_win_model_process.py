"""Native ACL + detached-model lifecycle integration (synthetic engine only).

These intentionally retain real ACL/locking checks. Run from the ordinary user
PowerShell session; a restricted token that cannot create an owner-only directory
must fail instead of silently relaxing the security contract.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from server.model_process import ModelProcessError
from server.platform_files import atomic_write_private, validate_private_path
from server.win_model_process import WindowsModelController, process_identity, process_lock

ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVER = '''
import sys
sys.path.insert(0, ROOT_VALUE)
import uvicorn
import server.win_model_process as lifecycle
from server.model_server import create_model_app
from server.win_model_transport import LoopbackSecurityMiddleware

class SyntheticEngine:
    def warmup(self):
        pass
    def transcribe(self, samples, language, overlap_seconds=0, final_chunk=True, *, start_seconds=0,
                   boundary_context=None, boundary_output=None):
        if boundary_output is not None:
            boundary_output.update({"version": 1, "audio_end": start_seconds + len(samples) / 16000, "tokens": []})
        return [{"start": 0.0, "end": len(samples) / 16000, "text": "synthetic"}]

def serve(settings, listener, *, token, instance):
    server = None
    def shutdown():
        server.should_exit = True
    app = create_model_app(settings, SyntheticEngine(), shutdown=shutdown)
    app.add_middleware(LoopbackSecurityMiddleware, token=token, instance=instance, port=listener.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical", proxy_headers=False))
    server.run(sockets=[listener])

lifecycle.serve_model = serve
raise SystemExit(lifecycle.main())
'''.replace("ROOT_VALUE", repr(str(ROOT)))


@unittest.skipUnless(os.name == "nt", "Windows ACL and detached-process lifecycle")
class WindowsModelLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="yeobaek-native-model-test-")
        self.directory = Path(self.temporary.name)
        self.fake = self.directory / "synthetic_model.py"
        self.fake.write_text(FAKE_SERVER, encoding="utf-8")
        self.controller = WindowsModelController(self.directory / "private", port=0,
                                                 command_prefix=[sys.executable, str(self.fake)])

    def tearDown(self):
        try:
            self.controller.stop(timeout=3)
        finally:
            self.temporary.cleanup()

    def start(self):
        result = self.controller.start(warmup=True, inherited={}, wait_ready=True, timeout=15)
        self.assertTrue(result["running"])
        self.assertTrue(result["alive"])
        self.assertTrue(result["ready"])
        return result

    def test_private_files_second_start_and_graceful_stop(self):
        first = self.start()
        for name in ("auth.token", "endpoint.json", "model.pid.json", "model.log"):
            validate_private_path(self.controller.directory / name)
        validate_private_path(self.controller.directory, directory=True)
        second = self.controller.start(warmup=True, inherited={})
        self.assertEqual(first["pid"], second["pid"])
        stopped = self.controller.stop(timeout=3)
        self.assertFalse(stopped["running"])
        for name in ("auth.token", "endpoint.json", "model.pid.json"):
            self.assertFalse((self.controller.directory / name).exists())

    def test_stopping_model_keeps_an_unrelated_python_process_alive(self):
        self.start()
        other = subprocess.Popen([process_identity(os.getpid())["exe"], "-c", "import time; time.sleep(30)"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.controller.stop(timeout=3)
            self.assertIsNone(other.poll())
        finally:
            other.terminate()
            other.wait(timeout=3)

    def test_pid_reuse_mismatch_cannot_signal_or_remove_records(self):
        self.start()
        original = self.controller.record()
        other = subprocess.Popen([process_identity(os.getpid())["exe"], "-c", "import time; time.sleep(30)"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            altered = {**original, "pid": other.pid}
            atomic_write_private(self.controller.pid_file, json.dumps(altered).encode())
            with self.assertRaises(ModelProcessError):
                self.controller.stop(timeout=1)
            self.assertIsNone(other.poll())
            self.assertTrue(self.controller.pid_file.exists())
            self.assertIsNotNone(process_identity(original["pid"]))
        finally:
            atomic_write_private(self.controller.pid_file, json.dumps(original).encode())
            other.terminate()
            other.wait(timeout=3)

    def test_held_control_lock_prevents_second_lifecycle_operation(self):
        self.start()
        with process_lock(self.controller.lock_file):
            with self.assertRaises(ModelProcessError):
                self.controller.stop(timeout=1)
        self.assertTrue(self.controller.status()["ready"])

    def test_wrong_authentication_file_fails_closed_and_preserves_api_side_output(self):
        self.start()
        token_file = self.controller.directory / "auth.token"
        original = token_file.read_bytes()
        try:
            atomic_write_private(token_file, b"c" * 64)
            self.assertFalse(self.controller.status()["alive"])
            self.assertTrue(self.controller.status()["running"])
        finally:
            atomic_write_private(token_file, original)
        self.assertTrue(self.controller.status()["alive"])


if __name__ == "__main__":
    unittest.main()
