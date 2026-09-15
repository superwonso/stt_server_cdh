"""Smoke-runner HTTP contract and secret-safe output (no live service calls)."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import httpx

from server.windows_local import new_profile
from server.windows_local_smoke import API_URL, SmokeFailure, api_checks, main, request


class SmokeRunnerTests(unittest.TestCase):
    def test_http_flow_uses_only_local_api_and_reuses_empty_synthetic_lectures(self):
        profile = new_profile()
        lectures, sessions, calls = {}, {}, []
        def reply(incoming):
            self.assertEqual(str(incoming.url).split("/", 3)[:3], ["http:", "", "127.0.0.1:18766"])
            path, method = incoming.url.path, incoming.method
            calls.append((method, path))
            authorization = incoming.headers.get("authorization", "")
            user = sessions.get(authorization)
            body = json.loads(incoming.content) if incoming.content else None
            if path == "/health":
                return httpx.Response(403 if "origin" in incoming.headers else 200, json={"status": "ok"})
            if path == "/auth/login":
                for index, account in enumerate(profile["accounts"]):
                    if body == account:
                        token = "synthetic-smoke-token-" + str(index)
                        sessions["Bearer " + token] = account["username"]
                        return httpx.Response(200, json={"token": token, "user": {"username": account["username"]}})
                return httpx.Response(401, json={"detail": "denied"})
            if path == "/__windows_control__/shutdown":
                return httpx.Response(401, json={"detail": "denied"})
            if user is None:
                return httpx.Response(401, json={"detail": "denied"})
            if path == "/auth/me":
                return httpx.Response(200, json={"username": user})
            if path == "/auth/logout":
                del sessions[authorization]
                return httpx.Response(200, json={"status": "ok"})
            if path == "/lectures" and method == "POST":
                identifier = incoming.headers["x-lecture-id"]
                lectures.setdefault(identifier, {"id": identifier, "owner": user, "segments": []})
                self.assertEqual(lectures[identifier]["owner"], user)
                return httpx.Response(201, json={"id": identifier})
            if path == "/lectures":
                return httpx.Response(200, json=[row for row in lectures.values() if row["owner"] == user])
            if path.startswith("/lectures/"):
                row = lectures[path.rsplit("/", 1)[1]]
                return httpx.Response(200, json=row) if row["owner"] == user else httpx.Response(404, json={"detail": "missing"})
            if path == "/status":
                return httpx.Response(200, json={"model_state": "offline", "transcription_providers": {"clova": {"configured": False}},
                    **{name: {"configured": False} for name in ("postprocessing", "summarization", "translation", "question_answering", "study_notes")}})
            if path == "/admin/overview":
                if user != profile["accounts"][0]["username"]:
                    return httpx.Response(403, json={"detail": "denied"})
                return httpx.Response(200, json={"drive": {"enabled": False, "configured": False},
                    "backup": {"enabled": False, "configured": False}, "tunnel": {"restart_available": False}})
            self.fail("Unexpected smoke endpoint")
        def streaming_reply(incoming):
            response = reply(incoming)
            return httpx.Response(response.status_code, headers=response.headers,
                                  stream=httpx.ByteStream(response.content))
        with httpx.Client(base_url=API_URL, transport=httpx.MockTransport(streaming_reply)) as client:
            result = api_checks(client, profile, model_offline=True)
            api_checks(client, profile, model_offline=True)
        self.assertEqual(len(lectures), 2)
        self.assertEqual(sessions, {})
        self.assertTrue(result["model_offline_verified"])
        self.assertIn("cross_account_access_denied", result["passed"])
        rendered = json.dumps(result)
        for account in profile["accounts"]:
            self.assertNotIn(account["username"], rendered)
            self.assertNotIn(account["password"], rendered)
        self.assertTrue(all("chunks" not in path and "recording" not in path for _, path in calls))

    def test_failure_output_never_reflects_provider_exception_details(self):
        output = io.StringIO()
        with patch("server.windows_local_smoke.run_smoke", side_effect=RuntimeError("synthetic-private-password-and-classroom-text")), \
             contextlib.redirect_stdout(output):
            result = main([])
        self.assertEqual(result, 1)
        self.assertNotIn("synthetic-private", output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_remote_destination_is_rejected_before_network_access(self):
        called = []
        with httpx.Client(base_url="https://external.invalid", transport=httpx.MockTransport(lambda value: called.append(value))) as client:
            with self.assertRaises(SmokeFailure):
                request(client, "GET", "/health")
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
