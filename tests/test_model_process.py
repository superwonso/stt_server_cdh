from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

from server.model_process import (
    ModelController, ModelProcessError, ModelSettings, atomic_record, checked_file,
    model_environment, process_identity, process_lock, socket_path,
)

ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVER = '''
import json, os, signal, socket, sys
from pathlib import Path
sys.path.insert(0, ROOT_VALUE)
import server.model_process as lifecycle

def serve(settings, listener):
    target = Path(sys.argv[sys.argv.index('--socket')+1]).parent / 'child-env.json'
    with target.open('w') as out:
        json.dump(dict(os.environ), out)
    running = True
    def stop(*args):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    listener.settimeout(.1)
    while running:
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            continue
        with connection:
            connection.settimeout(1)
            try:
                data = connection.recv(2048)
                state = settings.model.removeprefix('synthetic-')
                if state not in {'unloaded','loading','ready','error'}:
                    state = 'ready'
                body = json.dumps({'status':'ok','model_state':state}).encode()
                response = b'HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\nConnection: close\\r\\nContent-Length: '+str(len(body)).encode()+b'\\r\\n\\r\\n'+body
                connection.sendall(response)
            except OSError:
                pass

lifecycle.serve_model = serve
raise SystemExit(lifecycle.main())
'''.replace('ROOT_VALUE', repr(str(ROOT)))


class ModelEnvironmentTests(unittest.TestCase):
    def test_only_model_keys_are_copied_without_loading_api_settings(self):
        with tempfile.TemporaryDirectory(prefix="model-env-") as tmp:
            env = Path(tmp) / "config"
            env.write_text('ACCOUNT_USERNAMES=private-example\nMINDLOGIC_API_KEY=fake-secret\n'
                           'CLOVA_SPEECH_SECRET_KEY=another-fake\nDATA_DIR=/private-example\n'
                           'ASR_MODEL="model with spaces" # comment\nMODEL_CACHE_DIR=.models\n')
            inherited = {"ASR_MODEL": "synthetic-ready", "GOOGLE_TOKEN": "fake-token", "PYTHONPATH": "/fake-danger",
                         "HOME": "/fake-home", "HTTPS_PROXY": "fake-secret-url"}
            result = model_environment(env, inherited)
            self.assertEqual(result["ASR_MODEL"], "synthetic-ready")
            self.assertEqual(result["MODEL_CACHE_DIR"], ".models")
            for key in ("ACCOUNT_USERNAMES", "MINDLOGIC_API_KEY", "CLOVA_SPEECH_SECRET_KEY", "DATA_DIR",
                        "GOOGLE_TOKEN", "PYTHONPATH", "HOME", "HTTPS_PROXY"):
                self.assertNotIn(key, result)
            self.assertEqual(result["HF_HUB_OFFLINE"], "1")
            self.assertEqual(result["TRANSFORMERS_OFFLINE"], "1")
            self.assertNotIn("fake-secret", json.dumps(result))

    def test_wsl_runtime_settings_preserve_explicit_values_without_forcing_defaults(self):
        with tempfile.TemporaryDirectory(prefix="model-env-") as tmp:
            env = Path(tmp) / "config"
            env.write_text('HSA_ENABLE_DXG_DETECTION=1\n'
                           'LD_LIBRARY_PATH="/synthetic/rocm/lib:/synthetic/wsl/lib"\n'
                           'LD_PRELOAD=/synthetic/unsafe.so\n'
                           'MINDLOGIC_API_KEY=synthetic-secret\n')
            configured = model_environment(env, {})
            self.assertEqual(configured["HSA_ENABLE_DXG_DETECTION"], "1")
            self.assertEqual(configured["LD_LIBRARY_PATH"], "/synthetic/rocm/lib:/synthetic/wsl/lib")
            inherited = model_environment(env, {
                "HSA_ENABLE_DXG_DETECTION": "0", "LD_LIBRARY_PATH": "/synthetic/inherited/lib",
                "LD_PRELOAD": "/synthetic/unsafe.so", "USE_TF": "1", "GOOGLE_TOKEN": "synthetic-secret",
            })
            self.assertEqual(inherited["HSA_ENABLE_DXG_DETECTION"], "0")
            self.assertEqual(inherited["LD_LIBRARY_PATH"], "/synthetic/inherited/lib")
            for result in (configured, inherited):
                for key in ("LD_PRELOAD", "USE_TF", "GOOGLE_TOKEN", "MINDLOGIC_API_KEY"):
                    self.assertNotIn(key, result)
            self.assertEqual(model_environment(None, inherited), inherited)
            empty = model_environment(None, {})
            self.assertNotIn("HSA_ENABLE_DXG_DETECTION", empty)
            self.assertNotIn("LD_LIBRARY_PATH", empty)

    def test_wsl_runtime_settings_keep_value_validation_and_no_interpolation(self):
        for key in ("HSA_ENABLE_DXG_DETECTION", "LD_LIBRARY_PATH"):
            for value in ("${PRIVATE_VALUE}", "`echo unsafe`", "/synthetic/lib\nother", "x" * 4097):
                with self.subTest(key=key, value_length=len(value)):
                    with self.assertRaises(ModelProcessError):
                        model_environment(None, {key: value})

    def test_model_env_does_not_expand_secret_references_or_execute_shell(self):
        with tempfile.TemporaryDirectory(prefix="model-env-") as tmp:
            env = Path(tmp) / "config"
            for value in ('"${MINDLOGIC_API_KEY}"', '`echo unsafe`', '"$(echo unsafe)"'):
                env.write_text(f"ASR_MODEL={value}\n")
                with self.assertRaises(ModelProcessError):
                    model_environment(env, {})
            env.write_text('UNKNOWN_KEY="unterminated\nASR_MODEL=synthetic-ready\n')
            self.assertEqual(model_environment(env, {})["ASR_MODEL"], "synthetic-ready")

    def test_model_only_settings_have_no_account_database_or_provider_credentials(self):
        settings = ModelSettings.from_model_env({"MODEL_CACHE_DIR": ".models", "ASR_MODEL": "synthetic-ready"}, warmup=False)
        self.assertEqual(settings.model_path, ROOT / ".models" / "synthetic-ready")
        self.assertFalse(settings.model_warmup)
        self.assertEqual(set(vars(settings)), {"model_cache_dir", "model", "aligner", "device", "compute_type",
                                               "attention", "stability_guard_seconds", "model_warmup"})
        for value in ("nan", "inf", "secret-invalid"):
            with self.assertRaises(ModelProcessError):
                ModelSettings.from_model_env({"STABILITY_GUARD_SECONDS": value}, warmup=True)

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture; Windows reparse checks have separate native tests")
    def test_env_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="model-env-") as tmp:
            path = Path(tmp)
            (path / "actual").write_text("ASR_MODEL=synthetic-ready")
            (path / "linked").symlink_to(path / "actual")
            with self.assertRaises(ModelProcessError):
                model_environment(path / "linked", {})

    @unittest.skipIf(os.name == "nt", "POSIX Unix socket address limit")
    def test_socket_path_length_matches_the_107_byte_transport_contract(self):
        accepted = Path("/tmp/" + "x" * 91 + "/model.sock")
        self.assertEqual(len(os.fsencode(accepted)), 107)
        self.assertEqual(socket_path(accepted), accepted)
        with self.assertRaises(ModelProcessError):
            socket_path(Path("/tmp/" + "x" * 92 + "/model.sock"))


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(signal, "pidfd_send_signal"), "Linux/WSL owned process tests")
class ModelProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="model-life-")
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "private" / "model.sock"
        self.fake = self.directory / "fake_model.py"
        self.fake.write_text(FAKE_SERVER)
        self.controller = ModelController(self.path, command_prefix=[sys.executable, str(self.fake)])

    def tearDown(self):
        try:
            self.controller.stop(timeout=2)
        except ModelProcessError:
            pass
        self.temporary.cleanup()

    def start(self, state="ready"):
        result = self.controller.start(warmup=False, env_file=None,
            inherited={"ASR_MODEL": f"synthetic-{state}", "MINDLOGIC_API_KEY": "dummy-secret-not-inherited",
                       "HSA_ENABLE_DXG_DETECTION": "1", "LD_LIBRARY_PATH": "/usr/lib:/lib"})
        self.assertTrue(result["running"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.controller.status()
            if result["alive"]:
                return result
            time.sleep(.05)
        self.fail("Fake model did not serve health")

    def test_status_of_missing_directory_is_read_only(self):
        self.assertEqual(self.controller.status()["model_state"], "stopped")
        self.assertFalse(self.path.parent.exists())

    def test_fake_uds_lifecycle_is_private_and_second_start_keeps_the_same_pid(self):
        first = self.start()
        second = self.controller.start(warmup=True, inherited={})
        self.assertEqual(first["pid"], second["pid"])
        self.assertTrue(first["alive"]); self.assertTrue(first["ready"])
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.controller.pid_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.controller.log_file.stat().st_mode), 0o600)
        environment = json.loads((self.path.parent / "child-env.json").read_text())
        self.assertNotIn("MINDLOGIC_API_KEY", environment)
        self.assertNotIn("ACCOUNT_USERNAMES", environment)
        self.assertEqual(environment["HSA_ENABLE_DXG_DETECTION"], "1")
        self.assertEqual(environment["LD_LIBRARY_PATH"], "/usr/lib:/lib")
        self.assertFalse(self.controller.stop(timeout=2)["running"])
        self.assertFalse(self.path.exists()); self.assertFalse(self.controller.pid_file.exists())

    def test_alive_loading_is_not_ready_and_readiness_timeout_keeps_the_process(self):
        result = self.start("loading")
        self.assertTrue(result["alive"]); self.assertFalse(result["ready"])
        with self.assertRaises(ModelProcessError):
            self.controller.start(wait_ready=True, timeout=.15, inherited={})
        self.assertEqual(self.controller.status()["pid"], result["pid"])
        self.assertTrue(self.controller.status()["running"])

    def test_alive_model_error_does_not_masquerade_as_readiness(self):
        result = self.start("error")
        self.assertEqual(result["model_state"], "error")
        self.assertTrue(result["alive"]); self.assertFalse(result["ready"])

    def test_controller_locks_reject_overlapping_start_without_model_loading(self):
        socket_path(self.path, create=True)
        with process_lock(self.controller.lock_file):
            with self.assertRaises(ModelProcessError):
                self.controller.start(inherited={})
        with process_lock(self.controller.run_lock):
            with self.assertRaises(ModelProcessError):
                self.controller.start(inherited={})
        self.assertFalse(self.controller.pid_file.exists())

    def test_unknown_socket_is_not_deleted_or_rebound(self):
        socket_path(self.path, create=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as other:
            other.bind(str(self.path)); os.chmod(self.path, 0o600); other.listen(1)
            inode = self.path.stat().st_ino
            for action in (lambda: self.controller.start(inherited={}), self.controller.stop, self.controller.status):
                with self.assertRaises(ModelProcessError):
                    action()
                self.assertEqual(self.path.stat().st_ino, inode)

    def test_foreign_pid_record_is_never_signalled_or_removed(self):
        self.start()
        record = self.controller.record()
        forged = {**record, "pid": os.getpid()}
        atomic_record(self.controller.pid_file, forged)
        try:
            with mock.patch.object(self.controller, "_signal") as send:
                for action in (self.controller.stop, lambda: self.controller.start(inherited={}), self.controller.status):
                    with self.assertRaises(ModelProcessError):
                        action()
                send.assert_not_called()
            self.assertEqual(self.controller.record(), forged)
        finally:
            atomic_record(self.controller.pid_file, record)

    def test_known_crash_removes_only_its_attested_stale_socket_and_can_restart(self):
        first = self.start()
        os.kill(first["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 3
        while process_identity(first["pid"]) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertIsNone(process_identity(first["pid"]))
        self.assertTrue(self.path.exists())
        second = self.start()
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertTrue(second["ready"])

    def test_replaced_socket_is_preserved_when_original_process_stops(self):
        self.start()
        self.path.unlink()  # Explicitly replacing our disposable test socket.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as replacement:
            replacement.bind(str(self.path)); os.chmod(self.path, 0o600); replacement.listen(1)
            inode = self.path.stat().st_ino
            self.assertFalse(self.controller.status()["alive"])
            with self.assertRaises(ModelProcessError):
                self.controller.stop(timeout=2)
            self.assertEqual(self.path.stat().st_ino, inode)

    def test_public_directory_symlinks_and_non_socket_files_fail_closed(self):
        self.path.parent.mkdir(mode=0o755)
        with self.assertRaises(ModelProcessError):
            socket_path(self.path)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o755)
        self.path.parent.chmod(0o700)
        target = self.directory / "untouched"
        target.write_text("synthetic untouched")
        self.path.symlink_to(target)
        with self.assertRaises(ModelProcessError):
            socket_path(self.path)
        self.assertEqual(target.read_text(), "synthetic untouched")
        self.path.unlink(); self.path.write_text("not a socket")
        with self.assertRaises(ModelProcessError):
            self.controller.start(inherited={})
        self.assertEqual(self.path.read_text(), "not a socket")

    def test_pid_log_symlink_and_hardlinks_are_rejected(self):
        socket_path(self.path, create=True)
        target = self.directory / "untouched"
        target.write_text("synthetic untouched"); target.chmod(0o600)
        self.controller.log_file.symlink_to(target)
        with self.assertRaises(ModelProcessError):
            self.controller.start(inherited={})
        self.assertEqual(target.read_text(), "synthetic untouched")
        self.controller.log_file.unlink()
        os.link(target, self.controller.pid_file)
        with self.assertRaises(ModelProcessError):
            checked_file(self.controller.pid_file)


if __name__ == "__main__":
    unittest.main()
