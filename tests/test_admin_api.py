from __future__ import annotations

import io
import secrets
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

from server.app import create_app
from server.security import digest
from server.settings import Settings


def wav_audio(seconds: float = 0.1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(np.full(round(seconds * 16_000), 900, dtype="<i2").tobytes())
    return output.getvalue()


class FakeTranscriber:
    def __init__(self):
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def status(self):
        return {
            "model_state": "ready",
            "engine": "fake-engine",
            "model": "/private/models/fake-model",
            "device": "cpu",
            "ignored_secret": "must-not-be-returned",
        }

    def transcribe(self, samples, language, overlap_seconds=0, final_chunk=True):
        self.entered.set()
        if self.block and not self.release.wait(10):
            raise RuntimeError("test worker timed out")
        return [{"start": 0, "end": len(samples) / 16_000, "text": "테스트 받아쓰기"}]


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.engine = FakeTranscriber()
        self.tunnel_payload = {
            "state": "online",
            "operation": "idle",
            "message": "private-hook-message-that-must-not-be-reflected",
            "remote_recovery_possible": True,
            "local_start_required": False,
            "same_public_url_guaranteed": False,
            "public_url": "https://private-address.trycloudflare.com",
        }
        self.restart_payload = {**self.tunnel_payload, "accepted": True, "state": "starting", "operation": "restarting"}
        self.settings = Settings(
            data_dir=self.directory / "data",
            model_cache_dir=self.directory / "models",
            accounts=("user-alpha", "user-beta", "user-gamma"),
            admin_username="user-alpha",
            site_origins=("https://student.github.io",),
        )
        self.app = create_app(
            self.settings,
            self.engine,
            tunnel_status=lambda: dict(self.tunnel_payload),
            tunnel_restart=lambda: dict(self.restart_payload),
        )
        self.client = TestClient(self.app)
        self.tokens = {
            "user-alpha": "admin-" + secrets.token_urlsafe(24),
            "user-beta": "student-" + secrets.token_urlsafe(24),
            "user-gamma": "student-" + secrets.token_urlsafe(24),
        }
        with self.app.state.database.connect() as connection:
            for username, token in self.tokens.items():
                connection.execute(
                    "UPDATE users SET password_hash = ? WHERE username = ?",
                    ("test-activated", username),
                )
                connection.execute(
                    "INSERT INTO sessions(token_hash, username, expires_at, created_at) VALUES (?, ?, ?, ?)",
                    (digest(token), username, time.time() + 3600, time.time()),
                )

    def tearDown(self):
        self.engine.release.set()
        self.client.close()
        self.temporary.cleanup()

    def headers(self, username: str = "user-alpha") -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[username]}"}

    def test_admin_setting_is_private_and_missing_configuration_fails_closed(self):
        self.assertNotIn("user-alpha", repr(self.settings))
        invalid_value = "not-a-configured-private-account"
        with self.assertRaises(ValueError) as raised:
            Settings(
                data_dir=self.directory / "bad-data",
                model_cache_dir=self.directory / "bad-models",
                admin_username=invalid_value,
            )
        self.assertNotIn(invalid_value, str(raised.exception))

        no_admin_settings = Settings(
            data_dir=self.directory / "no-admin-data",
            model_cache_dir=self.directory / "no-admin-models",
        )
        no_admin_app = create_app(no_admin_settings, FakeTranscriber())
        no_admin_client = TestClient(no_admin_app)
        token = "ordinary-" + secrets.token_urlsafe(24)
        with no_admin_app.state.database.connect() as connection:
            connection.execute(
                "INSERT INTO sessions(token_hash, username, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (digest(token), "user-alpha", time.time() + 3600, time.time()),
            )
        try:
            response = no_admin_client.get(
                "/admin/overview", headers={"Authorization": f"Bearer {token}"}
            )
            self.assertEqual(response.status_code, 403)
            self.assertNotIn("ADMIN_USERNAME", response.text)
            self.assertEqual(no_admin_client.get("/health").status_code, 200)
        finally:
            no_admin_client.close()

    def test_overview_presence_counts_and_private_data_boundary(self):
        private_title = "PRIVATE-LECTURE-TITLE"
        private_text = "PRIVATE-TRANSCRIPT-TEXT"
        private_filename = "PRIVATE-RECORDING-NAME.wav"
        private_ip = "198.51.100.77"
        private_agent = "PRIVATE-USER-AGENT"
        lecture_id = str(uuid.uuid4())
        created = "2026-09-04T01:02:03Z"
        with self.app.state.database.connect() as connection:
            connection.execute(
                "INSERT INTO lectures(id, username, title, language, created_at) VALUES (?, ?, ?, 'ko', ?)",
                (lecture_id, "user-beta", private_title, created),
            )
            connection.execute(
                "INSERT INTO chunks(lecture_id, chunk_id, payload_hash, start_seconds, overlap_seconds, "
                "final_chunk, status) VALUES (?, ?, ?, 0, 0, 0, 'pending')",
                (lecture_id, str(uuid.uuid4()), "a" * 64),
            )
            done_chunk = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO chunks(lecture_id, chunk_id, payload_hash, start_seconds, overlap_seconds, "
                "final_chunk, status) VALUES (?, ?, ?, 1, 0, 1, 'done')",
                (lecture_id, done_chunk, "b" * 64),
            )
            connection.execute(
                "INSERT INTO segments(id, lecture_id, chunk_id, start, end, text) VALUES (?, ?, ?, 1, 2, ?)",
                (str(uuid.uuid4()), lecture_id, done_chunk, private_text),
            )
            connection.execute(
                "INSERT INTO imports(id, username, lecture_id, title, language, filename, file_fingerprint, "
                "total_bytes, uploaded_bytes, status, created_at, updated_at) "
                "VALUES (?, 'user-beta', ?, ?, 'ko', ?, ?, 100, 0, 'uploading', ?, ?)",
                (str(uuid.uuid4()), lecture_id, private_title, private_filename, "c" * 64, created, created),
            )
            connection.execute(
                "INSERT INTO transcript_corrections(lecture_id, raw_revision, status, model, created_at, updated_at) "
                "VALUES (?, ?, 'queued', 'safe-model', ?, ?)",
                (lecture_id, "d" * 64, created, created),
            )

        self.assertEqual(
            self.client.post(
                "/presence",
                json={"activity": "recording"},
                headers=self.headers("user-alpha") | {"User-Agent": private_agent, "X-Forwarded-For": private_ip},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/presence", json={"activity": "transcribing"}, headers=self.headers("user-beta")
            ).status_code,
            200,
        )
        invalid = self.client.post(
            "/presence",
            json={"activity": "PRIVATE-ACTIVITY", "lesson": private_title},
            headers=self.headers(),
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn("PRIVATE-ACTIVITY", invalid.text)
        self.assertNotIn(private_title, invalid.text)

        non_admin_headers = self.headers("user-beta")
        self.assertEqual(self.client.get("/admin/overview", headers=non_admin_headers).status_code, 403)
        self.assertEqual(
            self.client.post(
                "/admin/access", json={"enabled": False}, headers=non_admin_headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post("/admin/tunnel/restart", headers=non_admin_headers).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/admin/sessions/revoke",
                json={"account_id": "a" * 32},
                headers=non_admin_headers,
            ).status_code,
            403,
        )
        response = self.client.get("/admin/overview", headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["access"]["enabled"])
        self.assertEqual(result["server"]["state"], "online")
        self.assertEqual(result["server"]["model"], "fake-model")
        self.assertEqual(result["queues"], {"transcription": 1, "imports": 1, "corrections": 1,
                                          "summaries": 0, "translations": 0})
        self.assertEqual(result["tunnel"]["state"], "online")
        self.assertEqual(
            set(result["tunnel"]),
            {
                "state",
                "operation",
                "message",
                "remote_recovery_possible",
                "local_start_required",
                "same_public_url_guaranteed",
                "restart_available",
            },
        )
        self.assertNotIn("private-hook-message", result["tunnel"]["message"])
        accounts = {account["label"]: account for account in result["accounts"]}
        self.assertTrue(accounts["user-alpha"]["is_self"])
        self.assertFalse(accounts["user-beta"]["is_self"])
        self.assertTrue(accounts["user-beta"]["activated"])
        self.assertTrue(accounts["user-beta"]["online"])
        self.assertEqual(accounts["user-beta"]["activity"], "transcribing")
        self.assertEqual(
            accounts["user-beta"]["jobs"],
            {"transcription": 1, "imports": 1, "corrections": 1, "summaries": 0, "translations": 0},
        )
        self.assertNotEqual(accounts["user-beta"]["account_id"], "user-beta")
        self.assertEqual(result["recent_audit"], [])
        serialized = response.text
        for private_value in (
            private_title,
            private_text,
            private_filename,
            private_ip,
            private_agent,
            "private-address",
            "must-not-be-returned",
        ):
            self.assertNotIn(private_value, serialized)

    def test_summary_and_translation_counts_are_owned_and_exclude_terminal_jobs(self):
        rows = (
            ("lecture_summaries", "summary_json", "user-beta", "queued"),
            ("lecture_translations", "translation_json", "user-gamma", "processing"),
            ("lecture_summaries", "summary_json", "user-alpha", "completed"),
            ("lecture_translations", "translation_json", "user-alpha", "failed"),
        )
        private_ids = []
        with self.app.state.database.connect() as connection:
            for table, payload_column, username, state in rows:
                lecture_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
                private_ids.extend((lecture_id, job_id))
                connection.execute("INSERT INTO lectures(id,username,title,created_at) VALUES(?,?,'PRIVATE-JOB-TITLE','now')",
                                   (lecture_id, username))
                connection.execute(
                    f"INSERT INTO {table}(lecture_id,job_id,raw_revision,status,model,{payload_column},created_at,updated_at,completed_at) "
                    "VALUES(?,?,?,?,?,?, 'now','now',?)",
                    (lecture_id, job_id, "a" * 64, state, "PRIVATE-JOB-MODEL",
                     '{"private":"PRIVATE-JOB-CONTENT"}' if state == "completed" else None,
                     "now" if state == "completed" else None),
                )
        response = self.client.get("/admin/overview", headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["queues"], {"transcription": 0, "imports": 0, "corrections": 0,
                                          "summaries": 1, "translations": 1})
        accounts = {account["label"]: account for account in result["accounts"]}
        for username, expected in (("user-alpha", (0, 0)), ("user-beta", (1, 0)), ("user-gamma", (0, 1))):
            self.assertEqual((accounts[username]["jobs"]["summaries"], accounts[username]["jobs"]["translations"]), expected)
        for secret in (*private_ids, "PRIVATE-JOB-TITLE", "PRIVATE-JOB-MODEL", "PRIVATE-JOB-CONTENT"):
            self.assertNotIn(secret, response.text)

    def test_third_account_is_listed_but_remains_data_isolated_and_non_admin(self):
        created = self.client.post(
            "/lectures",
            json={"title": "세 번째 계정 수업", "language": "ko"},
            headers=self.headers("user-gamma"),
        )
        self.assertEqual(created.status_code, 201, created.text)
        lecture_id = created.json()["id"]

        self.assertEqual(
            self.client.get(f"/lectures/{lecture_id}", headers=self.headers("user-gamma")).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/lectures/{lecture_id}", headers=self.headers("user-beta")).status_code,
            404,
        )
        # Administrator privileges apply only to operations endpoints and do
        # not bypass per-account transcript ownership.
        self.assertEqual(
            self.client.get(f"/lectures/{lecture_id}", headers=self.headers()).status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/admin/overview", headers=self.headers("user-gamma")).status_code,
            403,
        )
        overview = self.client.get("/admin/overview", headers=self.headers()).json()
        self.assertEqual(
            {account["label"] for account in overview["accounts"]},
            {"user-alpha", "user-beta", "user-gamma"},
        )

    def test_pausing_persists_and_does_not_cancel_an_inflight_transcription(self):
        lecture = self.client.post(
            "/lectures",
            json={"title": "진행 중 수업", "language": "ko"},
            headers=self.headers(),
        )
        self.assertEqual(lecture.status_code, 201, lecture.text)
        lecture_id = lecture.json()["id"]
        self.engine.block = True
        upload_headers = self.headers() | {
            "Content-Type": "audio/wav",
            "X-Chunk-Id": str(uuid.uuid4()),
            "X-Start-Seconds": "0",
            "X-Final-Chunk": "true",
        }
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.client.post,
                f"/lectures/{lecture_id}/chunks",
                content=wav_audio(),
                headers=upload_headers,
            )
            self.assertTrue(self.engine.entered.wait(3), "transcription did not start")
            paused = self.client.post(
                "/admin/access", json={"enabled": False}, headers=self.headers()
            )
            self.assertEqual(paused.status_code, 200, paused.text)
            denied = self.client.get("/lectures", headers=self.headers())
            self.assertEqual(denied.status_code, 503)
            self.assertEqual(denied.headers.get("Retry-After"), "60")
            # A forged download ticket must not reveal whether access is paused.
            self.assertEqual(
                self.client.get("/recording-downloads/" + "x" * 43).status_code,
                404,
            )
            self.assertEqual(self.client.get("/auth/me", headers=self.headers()).status_code, 200)
            self.assertEqual(self.client.get("/status", headers=self.headers()).status_code, 200)
            self.assertEqual(
                self.client.post(
                    "/presence", json={"activity": "transcribing"}, headers=self.headers()
                ).status_code,
                200,
            )
            overview = self.client.get("/admin/overview", headers=self.headers())
            self.assertFalse(overview.json()["access"]["enabled"])
            # Auth endpoints are excluded from the pause gate (bad credentials
            # still produce their ordinary response, never 503).
            self.assertEqual(
                self.client.post(
                    "/auth/login", json={"username": "user-alpha", "password": "wrong"}
                ).status_code,
                401,
            )
            self.engine.release.set()
            self.assertEqual(future.result(timeout=5).status_code, 200)

        second_engine = FakeTranscriber()
        second_app = create_app(self.settings, second_engine)
        second_client = TestClient(second_app)
        try:
            persisted = second_client.get("/admin/overview", headers=self.headers())
            self.assertEqual(persisted.status_code, 200, persisted.text)
            self.assertFalse(persisted.json()["access"]["enabled"])
            self.assertEqual(second_client.get("/lectures", headers=self.headers()).status_code, 503)
            enabled = second_client.post(
                "/admin/access", json={"enabled": True}, headers=self.headers()
            )
            self.assertEqual(enabled.status_code, 200)
            self.assertTrue(enabled.json()["enabled"])
        finally:
            second_engine.release.set()
            second_client.close()

    def test_opaque_session_revoke_blocks_self_lockout_and_clears_presence(self):
        extra_beta_token = "extra-" + secrets.token_urlsafe(24)
        with self.app.state.database.connect() as connection:
            connection.execute(
                "INSERT INTO sessions(token_hash, username, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (digest(extra_beta_token), "user-beta", time.time() + 3600, time.time()),
            )
        self.client.post(
            "/presence", json={"activity": "viewing"}, headers=self.headers("user-beta")
        )
        overview = self.client.get("/admin/overview", headers=self.headers()).json()
        accounts = {item["label"]: item for item in overview["accounts"]}
        self.assertEqual(accounts["user-beta"]["session_count"], 2)

        self_lockout = self.client.post(
            "/admin/sessions/revoke",
            json={"account_id": accounts["user-alpha"]["account_id"]},
            headers=self.headers(),
        )
        self.assertEqual(self_lockout.status_code, 409)
        self.assertEqual(self.client.get("/auth/me", headers=self.headers()).status_code, 200)

        revoked = self.client.post(
            "/admin/sessions/revoke",
            json={"account_id": accounts["user-beta"]["account_id"]},
            headers=self.headers(),
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(revoked.json()["revoked_sessions"], 2)
        self.assertEqual(self.client.get("/auth/me", headers=self.headers("user-beta")).status_code, 401)
        after = self.client.get("/admin/overview", headers=self.headers()).json()
        beta = next(item for item in after["accounts"] if item["label"] == "user-beta")
        self.assertFalse(beta["online"])
        self.assertEqual(beta["activity"], "offline")
        self.assertEqual(beta["session_count"], 0)
        self.assertEqual(
            after["recent_audit"][0],
            {
                "timestamp": after["recent_audit"][0]["timestamp"],
                "action": "sessions_revoked",
                "result": "success",
                "target": "user-beta",
            },
        )

        another_app = create_app(self.settings, FakeTranscriber())
        another_client = TestClient(another_app)
        try:
            another = another_client.get("/admin/overview", headers=self.headers()).json()
            new_admin_id = next(
                item["account_id"] for item in another["accounts"] if item["label"] == "user-alpha"
            )
            self.assertNotEqual(new_admin_id, accounts["user-alpha"]["account_id"])
        finally:
            another_client.close()

    def test_presence_expires_without_persisting_browser_activity(self):
        observed_at = time.time()
        response = self.client.post(
            "/presence", json={"activity": "viewing"}, headers=self.headers("user-beta")
        )
        self.assertEqual(response.status_code, 200)
        with mock.patch("server.app.time.time", return_value=observed_at + 46):
            overview = self.client.get("/admin/overview", headers=self.headers()).json()
        account = next(item for item in overview["accounts"] if item["label"] == "user-beta")
        self.assertFalse(account["online"])
        self.assertEqual(account["activity"], "offline")
        self.assertIsNone(account["last_activity_at"])
        with self.app.state.database.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertNotIn("presence", tables)

    def test_access_and_tunnel_actions_have_bounded_safe_audit_records(self):
        for enabled in (False, False, True):
            response = self.client.post(
                "/admin/access", json={"enabled": enabled}, headers=self.headers()
            )
            self.assertEqual(response.status_code, 200, response.text)

        accepted = self.client.post("/admin/tunnel/restart", headers=self.headers())
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertTrue(accepted.json()["accepted"])
        self.assertNotIn("private-hook-message", accepted.text)
        self.assertNotIn("public_url", accepted.json())

        self.restart_payload = {
            **self.tunnel_payload,
            "accepted": False,
            "state": "starting",
            "operation": "restarting",
            "message": "PRIVATE-BUSY-DETAIL",
        }
        busy = self.client.post("/admin/tunnel/restart", headers=self.headers())
        self.assertEqual(busy.status_code, 409)
        self.assertNotIn("PRIVATE-BUSY-DETAIL", busy.text)

        self.restart_payload = {
            **self.tunnel_payload,
            "accepted": False,
            "state": "error",
            "operation": "idle",
            "message": "PRIVATE-ERROR-DETAIL",
        }
        unavailable = self.client.post("/admin/tunnel/restart", headers=self.headers())
        self.assertEqual(unavailable.status_code, 503)
        self.assertNotIn("PRIVATE-ERROR-DETAIL", unavailable.text)

        overview = self.client.get("/admin/overview", headers=self.headers()).json()
        audit = overview["recent_audit"]
        self.assertLessEqual(len(audit), 20)
        self.assertEqual(sum(item["action"] == "access_changed" for item in audit), 2)
        self.assertEqual(sum(item["action"] == "tunnel_restarted" for item in audit), 3)
        self.assertEqual(
            sum(
                item["action"] == "tunnel_restarted" and item["result"] == "accepted"
                for item in audit
            ),
            1,
        )
        self.assertEqual(set().union(*(item.keys() for item in audit)), {"timestamp", "action", "result", "target"})
        serialized = str(audit)
        self.assertNotIn("PRIVATE-ERROR-DETAIL", serialized)
        self.assertNotIn("PRIVATE-BUSY-DETAIL", serialized)


if __name__ == "__main__":
    unittest.main()
