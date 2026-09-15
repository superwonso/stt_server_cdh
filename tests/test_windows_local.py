"""Pure checks for test-profile separation and authenticated API control."""
from __future__ import annotations

import copy
import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.model_process import ModelProcessError
from server.windows_local import (API_PORT, CONTROL_PREFIX, DATA_DIR, MODEL_RUNTIME, LocalBoundaryMiddleware,
                                  create_control_app, deny_external_io, fixed_model_environment,
                                  local_settings, new_profile, validate_profile)
from server.win_model_transport import AUTH_HEADER, request_auth, verify_response


class IsolatedProfileTests(unittest.TestCase):
    def test_random_fake_accounts_and_fixed_settings_ignore_production_environment(self):
        first, second = new_profile(), new_profile()
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["accounts"], second["accounts"])
        with patch.dict(os.environ, {"DATA_DIR": "C:/synthetic-production", "ACCOUNT_USERNAMES": "real-a,real-b",
                                   "GOOGLE_DRIVE_RECORDINGS": "1", "ASR_MODEL": "changed-model",
                                   "CLOVA_SPEECH_SECRET_KEY": "synthetic-secret-should-not-copy",
                                   "MINDLOGIC_API_KEY": "synthetic-secret-should-not-copy"}), \
             patch("server.settings.Settings.from_env", side_effect=AssertionError("dotenv must not load")):
            settings = local_settings(first)
            environment = fixed_model_environment()
        self.assertEqual(settings.data_dir, DATA_DIR)
        self.assertEqual(settings.local_model_runtime, MODEL_RUNTIME)
        self.assertEqual(settings.model, "Qwen3-ASR-1.7B")
        self.assertEqual(settings.aligner, "Qwen3-ForcedAligner-0.6B")
        self.assertEqual(settings.attention, "sdpa")
        self.assertEqual(settings.accounts, tuple(account["username"] for account in first["accounts"]))
        self.assertFalse(settings.google_drive_enabled)
        self.assertIsNone(settings.google_drive_oauth_client_path)
        self.assertIsNone(settings.google_drive_token_path)
        self.assertIsNone(settings.clova_speech_secret_key)
        self.assertIsNone(settings.mindlogic_api_key)
        self.assertFalse(settings.model_warmup)
        self.assertNotIn("CLOVA_SPEECH_SECRET_KEY", environment)
        self.assertNotIn("MINDLOGIC_API_KEY", environment)
        self.assertNotIn("DATA_DIR", environment)
        self.assertEqual(environment["ASR_MODEL"], "Qwen3-ASR-1.7B")
        for account in first["accounts"]:
            self.assertNotIn(account["username"], repr(settings))
            self.assertNotIn(account["password"], repr(settings))

    @unittest.skipUnless(os.name == "nt", "Native Windows PyTorch cache identity")
    def test_model_environment_preserves_os_username_for_inductor_getpass(self):
        import getpass
        from server.model_process import model_environment
        with patch.dict(os.environ, {"USERNAME": "synthetic-os-user", "ACCOUNT_USERNAMES": "api-a,api-b",
                                      "MINDLOGIC_API_KEY": "synthetic-secret"}, clear=True):
            environment = model_environment(None, fixed_model_environment())
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(getpass.getuser(), "synthetic-os-user")
        self.assertNotIn("ACCOUNT_USERNAMES", environment)
        self.assertNotIn("MINDLOGIC_API_KEY", environment)

    def test_unrecognized_profile_or_account_list_fails_without_reflecting_content(self):
        original = new_profile()
        for mutation in (lambda p: p.update(project="C:/unrelated"), lambda p: p.update(kind="production"),
                         lambda p: p.update(accounts=[]), lambda p: p["accounts"][0].update(username="real-account"),
                         lambda p: p.update(extra="untrusted")):
            value = copy.deepcopy(original)
            mutation(value)
            with self.assertRaises(ModelProcessError) as caught:
                validate_profile(value)
            self.assertNotIn("untrusted", str(caught.exception))
            self.assertNotIn("real-account", str(caught.exception))

    def test_api_audit_guard_blocks_external_network_names_and_commands(self):
        for event, arguments in (("socket.connect", (None, ("203.0.113.1", 443))),
                                 ("socket.sendto", (None, ("198.51.100.1", 443))),
                                 ("socket.getaddrinfo", ("external.invalid", 443)),
                                 ("subprocess.Popen", ("gh", [], None, None)), ("os.system", ("unsafe",))):
            with self.subTest(event=event), self.assertRaises(PermissionError):
                deny_external_io(event, arguments)
        for event, arguments in (("socket.connect", (None, ("127.0.0.1", 18765))),
                                 ("socket.getaddrinfo", ("127.0.0.1", 18765))):
            self.assertIsNone(deny_external_io(event, arguments))


class APIControlTests(unittest.TestCase):
    def test_mounted_control_needs_its_own_signature_and_refuses_browser_bearer(self):
        token, instance = "a" * 64, "b" * 32
        shutdowns = []
        app = FastAPI()
        app.mount(CONTROL_PREFIX, create_control_app(token=token, instance=instance,
                                                     shutdown=lambda: shutdowns.append(True)))
        app.add_middleware(LocalBoundaryMiddleware)
        base_url = f"http://127.0.0.1:{API_PORT}"
        with TestClient(app, base_url=base_url, client=("127.0.0.1", 52345)) as client:
            path = CONTROL_PREFIX + "/shutdown"
            self.assertEqual(client.post(path, headers={"Authorization": "Bearer synthetic-user-session"}).status_code, 401)
            auth, nonce = request_auth(token, instance, "POST", path)
            self.assertEqual(client.post(path, headers={AUTH_HEADER: auth, "Origin": base_url}).status_code, 403)
            auth, nonce = request_auth(token, instance, "POST", path)
            response = client.post(path, headers={AUTH_HEADER: auth})
            self.assertEqual(response.status_code, 200)
            verify_response(token, {"instance": instance}, nonce, response, response.content)
            self.assertEqual(shutdowns, [True])

    def test_api_rejects_wrong_host_and_non_loopback_peers_before_routes(self):
        app = FastAPI()
        @app.get("/health")
        async def health():
            return {"ok": True}
        app.add_middleware(LocalBoundaryMiddleware)
        with TestClient(app, base_url=f"http://127.0.0.1:{API_PORT}", client=("127.0.0.1", 52345)) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/health", headers={"Host": "rebound.invalid"}).status_code, 403)
        with TestClient(app, base_url=f"http://127.0.0.1:{API_PORT}", client=("192.0.2.1", 52345)) as client:
            self.assertEqual(client.get("/health").status_code, 403)


if __name__ == "__main__":
    unittest.main()
