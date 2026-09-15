"""Native production launcher contracts using temporary synthetic data only."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import windows_service as service
from server.model_process import ModelProcessError, PROJECT_DIR
from server.platform_files import atomic_write_private, ensure_private_directory, open_file
from server.settings import Settings
from server.win_model_transport import AUTH_HEADER, request_auth, verify_response


class ServiceSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "production-synthetic"
        ensure_private_directory(self.root)
        self.config = self.root / "config"
        self.data = self.root / "data"
        ensure_private_directory(self.config)
        ensure_private_directory(self.data)
        self.env = self.config / "service.env"
        self.model = self.root / "model"
        self.patchers = [
            patch.object(service, "SERVICE_ROOT", self.root),
            patch.object(service, "ENV_FILE", self.env),
            patch.object(service, "DATA_DIR", self.data),
            patch.object(service, "MODEL_RUNTIME", self.model),
            patch.object(service, "API_RUNTIME", self.root / "api-control"),
        ]
        for patched in self.patchers:
            patched.start()
            self.addCleanup(patched.stop)

    def settings(self, **changes):
        values = dict(data_dir=self.data, model_cache_dir=PROJECT_DIR / ".models",
                      local_model_runtime=self.model, accounts=("synthetic-alpha", "synthetic-beta"),
                      site_origins=("https://synthetic.invalid",), model_warmup=False)
        values.update(changes)
        return Settings(**values)

    def write_db(self, *, synthetic=False, users=("synthetic-alpha", "synthetic-beta")):
        path = self.data / "classroom.sqlite3"
        descriptor = open_file(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, private=True)
        os.close(descriptor)
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE users(username TEXT PRIMARY KEY)")
            connection.executemany("INSERT INTO users VALUES(?)", [(user,) for user in users])
            if synthetic:
                connection.execute("CREATE TABLE windows_local_profile(profile_id TEXT)")
        return path

    def test_api_environment_omits_shell_keys_and_model_profile_is_unchanged(self):
        with patch.dict(os.environ, {"USERNAME": "synthetic-os-user", "STT_ENV_FILE": "unrelated.env",
                                     "MINDLOGIC_API_KEY": "secret-should-not-copy",
                                     "CLOVA_SPEECH_SECRET_KEY": "secret-should-not-copy",
                                     "ACCOUNT_USERNAMES": "wrong-a,wrong-b", "DATA_DIR": "wrong-data",
                                     "PYTHONPATH": "wrong-hooks", "HTTPS_PROXY": "http://wrong.invalid",
                                     "ASR_MODEL": "wrong-model"}, clear=True):
            environment = service.api_environment()
            model_environment = service.fixed_model_environment()
        self.assertEqual(environment["STT_ENV_FILE"], str(self.env))
        self.assertEqual(environment["USERNAME"], "synthetic-os-user")
        for key in ("MINDLOGIC_API_KEY", "CLOVA_SPEECH_SECRET_KEY", "ACCOUNT_USERNAMES",
                    "DATA_DIR", "PYTHONPATH", "HTTPS_PROXY", "ASR_MODEL"):
            self.assertNotIn(key, environment)
        self.assertNotIn("STT_ENV_FILE", model_environment)
        self.assertNotIn("MINDLOGIC_API_KEY", model_environment)
        self.assertEqual(model_environment["ASR_MODEL"], "Qwen3-ASR-1.7B")

    def test_explicit_private_environment_wins_over_ambient_production_and_test_settings(self):
        text = "\n".join([
            f"DATA_DIR={self.data.as_posix()}", f"MODEL_CACHE_DIR={(PROJECT_DIR / '.models').as_posix()}",
            f"LOCAL_MODEL_RUNTIME={self.model.as_posix()}", "ACCOUNT_USERNAMES=synthetic-alpha,synthetic-beta",
            "SITE_ORIGINS=https://synthetic.invalid", "MODEL_WARMUP=0",
            "MINDLOGIC_API_KEY=synthetic-only-key",
        ])
        atomic_write_private(self.env, text.encode())
        with patch.dict(os.environ, {"DATA_DIR": "unrelated", "ACCOUNT_USERNAMES": "wrong-a,wrong-b",
                                     "STT_ENV_FILE": "unrelated.env", "MINDLOGIC_API_KEY": "ambient-key"}, clear=True):
            settings = service.load_service_settings()
            self.assertEqual(settings.data_dir, self.data)
            self.assertEqual(settings.mindlogic_api_key, "synthetic-only-key")
            self.assertEqual(os.environ["STT_ENV_FILE"], str(self.env))
            self.assertEqual(settings.accounts, ("synthetic-alpha", "synthetic-beta"))

    def test_fixed_profile_refuses_wrong_db_inline_model_and_changed_model(self):
        for changes in ({"data_dir": self.root / "unrelated"}, {"local_model_runtime": None},
                        {"model_cache_dir": self.root / "models"}, {"model": "changed"},
                        {"device": "cpu"}, {"attention": "flash_attention_2"}, {"model_warmup": True}):
            with self.subTest(changed_field=next(iter(changes))):
                with self.assertRaises(ModelProcessError):
                    service.validate_service_settings(self.settings(**changes))

    def test_missing_database_is_never_created(self):
        path = self.settings().database_path
        with self.assertRaises(OSError):
            service.verify_service_database(self.settings())
        self.assertFalse(path.exists())

    def test_restored_database_check_only_reads_account_inventory_and_integrity(self):
        path = self.write_db()
        before = hashlib.sha256(path.read_bytes()).digest()
        service.verify_service_database(self.settings())
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)

    def test_wrong_account_set_or_local_test_marker_is_rejected_without_writes(self):
        path = self.write_db(users=("synthetic-alpha", "unexpected-synthetic"))
        before = path.read_bytes()
        with self.assertRaises(ModelProcessError):
            service.verify_service_database(self.settings())
        self.assertEqual(path.read_bytes(), before)
        path.unlink()
        path = self.write_db(synthetic=True)
        before = path.read_bytes()
        with self.assertRaises(ModelProcessError):
            service.verify_service_database(self.settings())
        self.assertEqual(path.read_bytes(), before)

    def test_status_does_not_read_service_environment_or_database(self):
        with patch.object(service, "load_service_settings", side_effect=AssertionError("must not read")), \
             patch.object(service, "verify_service_database", side_effect=AssertionError("must not read")):
            result = service.status()
        self.assertFalse(result["api"]["running"])
        self.assertFalse(result["model"]["running"])
        self.assertEqual(result["api_url"], "http://127.0.0.1:8765")

    def test_controller_command_and_runtime_are_separate_from_test_profile(self):
        controller = service.ServiceAPIController()
        self.assertEqual(controller.port, 8765)
        self.assertEqual(controller.pid_file, self.root / "api-control" / "api.pid.json")
        command = controller.command(False, "a" * 32)
        self.assertEqual(command[1:4], ["-m", "server.windows_service", "run"])
        self.assertNotIn("server.windows_local", command)
        self.assertEqual(service.MODEL_PORT, 18775)


class ServiceAPIBoundaryTests(unittest.TestCase):
    def test_production_factory_and_control_keep_public_and_private_auth_separate(self):
        fake = FastAPI()
        @fake.get("/health")
        async def health():
            return {"status": "ok"}
        fake.state.lease_renewer = Mock()
        lease_metadata = {"enabled": True, "state": "waiting", "expires_in_seconds": 70000,
                          "last_attempt_at": None, "last_success_at": None, "renewing": False, "error_code": ""}
        fake.state.lease_renewer.status.return_value = {**lease_metadata, "private-extra": "must-not-appear"}
        token, instance = "a" * 64, "b" * 32
        shutdown = Mock()
        with patch("server.app.create_app", return_value=fake) as factory:
            app = service.create_service_app(token=token, instance=instance, shutdown=shutdown)
        factory.assert_called_once_with()
        base_url = "http://127.0.0.1:8765"
        path = service.CONTROL_PREFIX + "/shutdown"
        with TestClient(app, base_url=base_url, client=("127.0.0.1", 55555)) as client:
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            control_health = service.CONTROL_PREFIX + "/health"
            self.assertEqual(client.get(control_health).status_code, 401)
            fake.state.lease_renewer.status.assert_not_called()
            health_auth, health_nonce = request_auth(token, instance, "GET", control_health)
            health_response = client.get(control_health, headers={AUTH_HEADER: health_auth})
            verify_response(token, {"instance": instance}, health_nonce, health_response, health_response.content)
            self.assertEqual(health_response.json()["lease_renewal"], lease_metadata)
            self.assertNotIn("private-extra", health_response.text)
            self.assertEqual(client.get("/health", headers={"Host": "public.invalid"}).status_code, 403)
            self.assertEqual(client.post(path, headers={"Authorization": "Bearer synthetic-session"}).status_code, 401)
            auth, _ = request_auth(token, instance, "POST", path)
            self.assertEqual(client.post(path, headers={AUTH_HEADER: auth, "Origin": "https://synthetic.invalid"}).status_code, 403)
            auth, nonce = request_auth(token, instance, "POST", path)
            response = client.post(path, headers={AUTH_HEADER: auth})
            self.assertEqual(response.status_code, 200)
            verify_response(token, {"instance": instance}, nonce, response, response.content)
            shutdown.assert_called_once_with()
        with TestClient(app, base_url=base_url, client=("192.0.2.2", 55555)) as client:
            self.assertEqual(client.get("/health").status_code, 403)

    @unittest.skipUnless(os.name == "nt", "Windows launcher CLI")
    def test_start_error_never_reflects_private_config_or_exception(self):
        with patch.object(service, "load_service_settings", side_effect=ValueError("private-account-and-key")), \
             contextlib.redirect_stderr(io.StringIO()) as error, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.main(["start", "--api-only", "--json"]), 1)
        self.assertNotIn("private-account-and-key", error.getvalue())



@unittest.skipUnless(os.name == "nt", "Native Windows synthetic service lifecycle")
class ServiceNativeLifecycleTests(unittest.TestCase):
    def test_detached_synthetic_api_private_control_duplicate_start_and_owned_stop(self):
        import socket
        import sys
        import httpx
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "service"
            ensure_private_directory(root)
            data, config = root / "data", root / "config"
            ensure_private_directory(data)
            ensure_private_directory(config)
            db = data / "classroom.sqlite3"
            descriptor = open_file(db, os.O_CREAT | os.O_EXCL | os.O_RDWR, private=True)
            os.close(descriptor)
            with contextlib.closing(sqlite3.connect(db)) as connection, connection:
                connection.execute("CREATE TABLE users(username TEXT PRIMARY KEY)")
                connection.executemany("INSERT INTO users VALUES(?)", [("synthetic-alpha",), ("synthetic-beta",)])
            values = "\n".join([
                f"DATA_DIR={data.as_posix()}", f"MODEL_CACHE_DIR={(PROJECT_DIR / '.models').as_posix()}",
                f"LOCAL_MODEL_RUNTIME={(root / 'model').as_posix()}",
                "ACCOUNT_USERNAMES=synthetic-alpha,synthetic-beta",
                "SITE_ORIGINS=https://synthetic.invalid", "MODEL_WARMUP=0",
            ])
            atomic_write_private(config / "service.env", values.encode())
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            fixture = root / "service_fixture.py"
            script = """from pathlib import Path
import sys
sys.path.insert(0, PROJECT_PATH)
from fastapi import FastAPI
from server import windows_service as service
service.SERVICE_ROOT = Path(__file__).resolve().parent
service.ENV_FILE = service.SERVICE_ROOT / "config" / "service.env"
service.DATA_DIR = service.SERVICE_ROOT / "data"
service.MODEL_RUNTIME = service.SERVICE_ROOT / "model"
service.API_RUNTIME = service.SERVICE_ROOT / "api-control"
service.API_PORT = TEST_PORT
def fake_app(**control):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    @app.get("/health")
    async def health():
        return {"status":"ok"}
    app.mount(service.CONTROL_PREFIX, service.create_control_app(**control))
    app.add_middleware(service.ServiceBoundaryMiddleware)
    return app
service.create_service_app = fake_app
raise SystemExit(service.main())
""".replace("PROJECT_PATH", repr(str(PROJECT_DIR))).replace("TEST_PORT", str(port))
            atomic_write_private(fixture, script.encode())
            with patch.object(service, "API_RUNTIME", root / "api-control"), patch.object(service, "API_PORT", port):
                controller = service.ServiceAPIController()
            controller.command_prefix = [sys.executable, str(fixture)]
            try:
                started = controller.start(warmup=False, wait_ready=True, timeout=15,
                                           inherited=service.api_environment())
                self.assertTrue(started["ready"])
                again = controller.start(warmup=False, wait_ready=True, timeout=15,
                                         inherited=service.api_environment())
                self.assertEqual(started["pid"], again["pid"])
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                    self.assertEqual(client.get("/health").status_code, 200)
                    self.assertEqual(client.post(service.CONTROL_PREFIX + "/shutdown").status_code, 401)
                    self.assertEqual(client.get("/health", headers={"Host": "external.invalid"}).status_code, 403)
                self.assertTrue(controller.status()["running"])
            finally:
                controller.stop(timeout=10)
            self.assertFalse(controller.stop(timeout=10)["running"])
            self.assertFalse(any((root / "api-control" / name).exists()
                                 for name in ("api.pid.json", "auth.token", "endpoint.json")))

if __name__ == "__main__":
    unittest.main()
