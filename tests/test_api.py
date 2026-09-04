from __future__ import annotations

import io
import tempfile
import threading
import time
import unittest
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
from fastapi.testclient import TestClient

from server.app import create_app
from server.manage import create_invitations
from server.security import PASSWORD_HASHER, digest
from server.settings import Settings


def wav_audio(seconds=1.0, rate=16000, channels=1, width=2, sample=1000):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        if width == 2:
            content = np.full(int(seconds * rate) * channels, sample, dtype="<i2").tobytes()
        else:
            content = b"\x00" * (int(seconds * rate) * channels * width)
        audio.writeframes(content)
    return output.getvalue()


class FakeTranscriber:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.block = False
        self.result = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def status(self):
        return {"model_state": "ready", "model": "fake", "device": "cpu"}

    def transcribe(self, samples, language, overlap_seconds=0, final_chunk=True):
        self.calls += 1
        self.last_contract = (overlap_seconds, final_chunk)
        self.entered.set()
        if self.block and not self.release.wait(timeout=10):
            raise RuntimeError("Test timed out")
        if self.fail:
            raise RuntimeError("private-test-path-and-error")
        if self.result is not None:
            return self.result
        return [{"start": 0, "end": len(samples) / 16000, "text": " 수업 받아쓰기입니다. "}]


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.engine = FakeTranscriber()
        self.settings = Settings(
            data_dir=self.directory / "data",
            model_cache_dir=self.directory / "models",
            site_origins=("https://student.github.io",),
            max_pending_chunks=1,
        )
        self.app = create_app(self.settings, self.engine)
        self.client = TestClient(self.app)
        self.database = self.app.state.database
        self.codes = {"user-alpha": "a" * 43, "user-beta": "b" * 43}
        self.password = "test-only-password-2026"
        with self.database.connect() as connection:
            for username, code in self.codes.items():
                connection.execute(
                    "UPDATE users SET setup_hash = ?, setup_expires = ? WHERE username = ?",
                    (digest(code), time.time() + 3600, username),
                )

    def tearDown(self):
        self.engine.release.set()
        self.client.close()
        self.temporary.cleanup()

    def activate(self, username="user-alpha"):
        response = self.client.post("/auth/activate", json={
            "username": username, "setup_code": self.codes[username], "password": self.password,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"], {"username": username})
        return response.json()["token"]

    def headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    def lecture(self, token, title="테스트 수업", lecture_id=None):
        headers = self.headers(token)
        if lecture_id:
            headers["X-Lecture-Id"] = lecture_id
        response = self.client.post("/lectures", json={"title": title, "language": "ko"}, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def upload(self, token, lecture_id, payload=None, chunk_id=None, start="0", extra_headers=None):
        headers = self.headers(token) | {
            "Content-Type": "audio/wav", "X-Chunk-Id": chunk_id or str(uuid.uuid4()), "X-Start-Seconds": start,
        } | (extra_headers or {})
        return self.client.post(f"/lectures/{lecture_id}/chunks", content=payload if payload is not None else wav_audio(), headers=headers)

    def test_activation_is_single_use_and_session_is_revocable(self):
        token = self.activate()
        self.assertEqual(self.client.get("/auth/me", headers=self.headers(token)).json(), {"username": "user-alpha"})
        again = self.client.post("/auth/activate", json={"username": "user-alpha", "setup_code": self.codes["user-alpha"], "password": self.password})
        self.assertEqual(again.status_code, 400)
        unknown = self.client.post("/auth/activate", json={"username": "third-person", "setup_code": "x" * 43, "password": self.password})
        self.assertEqual(unknown.status_code, 400)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
            stored = connection.execute("SELECT * FROM users WHERE username='user-alpha'").fetchone()
            self.assertTrue(stored["password_hash"].startswith("$argon2id$"))
            self.assertIsNone(stored["setup_hash"])
            self.assertEqual(connection.execute("SELECT token_hash FROM sessions").fetchone()[0], digest(token))
        self.assertEqual(self.client.post("/auth/logout", headers=self.headers(token)).status_code, 200)
        self.assertEqual(self.client.get("/auth/me", headers=self.headers(token)).status_code, 401)
        login = self.client.post("/auth/login", json={"username": "user-alpha", "password": self.password})
        self.assertEqual(login.status_code, 200)
        with self.database.connect() as connection:
            connection.execute("UPDATE sessions SET expires_at = ?", (time.time() - 1,))
        self.assertEqual(self.client.get("/auth/me", headers=self.headers(login.json()["token"])).status_code, 401)

    def test_activation_accepts_four_characters_and_rejects_three(self):
        too_short = self.client.post("/auth/activate", json={
            "username": "user-alpha", "setup_code": self.codes["user-alpha"], "password": "abc",
        })
        self.assertEqual(too_short.status_code, 422)
        self.assertNotIn("abc", too_short.text)
        accepted = self.client.post("/auth/activate", json={
            "username": "user-alpha", "setup_code": self.codes["user-alpha"], "password": "abcd",
        })
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(
            self.client.post("/auth/login", json={"username": "user-alpha", "password": "abcd"}).status_code,
            200,
        )

    def test_only_configured_accounts_can_authenticate(self):
        # Application authorization remains closed even if a legacy or
        # manually altered database happens to contain another user/session.
        password = "rogue-account-password"
        token = "rogue-session-token"
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO users(username, password_hash) VALUES (?, ?)",
                ("third-person", PASSWORD_HASHER.hash(password)),
            )
            connection.execute(
                "INSERT INTO sessions(token_hash, username, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (digest(token), "third-person", time.time() + 3600, time.time()),
            )
        login = self.client.post("/auth/login", json={"username": "third-person", "password": password})
        self.assertEqual(login.status_code, 401)
        self.assertEqual(self.client.get("/auth/me", headers=self.headers(token)).status_code, 401)

    def test_lecture_and_audio_access_belong_to_owner(self):
        owner, other = self.activate(), self.activate("user-beta")
        lecture_id = self.lecture(owner)
        self.assertEqual(self.client.get("/lectures", headers=self.headers(other)).json(), [])
        self.assertEqual(self.client.get(f"/lectures/{lecture_id}", headers=self.headers(other)).status_code, 404)
        self.assertEqual(self.upload(other, lecture_id).status_code, 404)
        self.assertEqual(self.client.get("/lectures").status_code, 401)
        self.assertEqual(self.client.get("/status").status_code, 401)
        self.assertEqual(self.engine.calls, 0)

    def test_chunk_retry_is_idempotent_and_rejects_changed_payload(self):
        token = self.activate()
        lecture_id, chunk_id = self.lecture(token), str(uuid.uuid4())
        first = self.upload(token, lecture_id, chunk_id=chunk_id, start="12.5")
        self.assertEqual(first.status_code, 200, first.text)
        repeated = self.upload(token, lecture_id, chunk_id=chunk_id, start="12.5")
        self.assertEqual(repeated.json(), first.json())
        self.assertEqual(self.engine.calls, 1)
        self.assertEqual(first.json()["segments"][0]["start"], 12.5)
        self.assertEqual(first.json()["segments"][0]["end"], 13.5)
        self.assertEqual(self.upload(token, lecture_id, chunk_id=chunk_id, start="13").status_code, 409)
        self.assertEqual(self.upload(token, lecture_id, wav_audio(sample=2000), chunk_id=chunk_id, start="12.5").status_code, 409)
        changed_contract = self.upload(
            token,
            lecture_id,
            chunk_id=chunk_id,
            start="12.5",
            extra_headers={"X-Overlap-Seconds": "0.5", "X-Final-Chunk": "false"},
        )
        self.assertEqual(changed_contract.status_code, 409)
        lecture = self.client.get(f"/lectures/{lecture_id}", headers=self.headers(token)).json()
        self.assertEqual(lecture["segments"], first.json()["segments"])
        self.assertEqual(list(self.settings.data_dir.glob("*.wav")), [])

    def test_lecture_creation_is_idempotent_after_a_lost_response(self):
        token = self.activate()
        lecture_id = str(uuid.uuid4())
        first = self.lecture(token, "재전송 수업", lecture_id)
        replayed = self.lecture(token, "재전송 수업", lecture_id)
        self.assertEqual(replayed, first)
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM lectures WHERE id = ?", (lecture_id,)).fetchone()[0],
                1,
            )
        changed = self.client.post(
            "/lectures",
            json={"title": "다른 수업", "language": "ko"},
            headers=self.headers(token) | {"X-Lecture-Id": lecture_id},
        )
        self.assertEqual(changed.status_code, 409)

    def test_audio_contract_and_untrusted_headers_are_validated(self):
        token = self.activate()
        lecture_id = self.lecture(token)
        invalid_payloads = [b"not audio", wav_audio(rate=8000), wav_audio(channels=2), wav_audio(width=4), wav_audio(seconds=0.01), wav_audio(seconds=15.01), wav_audio()[:-100]]
        for payload in invalid_payloads:
            with self.subTest(size=len(payload)):
                self.assertEqual(self.upload(token, lecture_id, payload).status_code, 422)
        for timestamp in ["NaN", "Infinity", "-0.01", "86401", "bad"]:
            with self.subTest(timestamp=timestamp):
                self.assertEqual(self.upload(token, lecture_id, start=timestamp).status_code, 422)
        for overlap in ["NaN", "Infinity", "-0.01", "3.01", "bad"]:
            with self.subTest(overlap=overlap):
                self.assertEqual(self.upload(token, lecture_id, extra_headers={"X-Overlap-Seconds": overlap}).status_code, 422)
        self.assertEqual(self.upload(token, lecture_id, extra_headers={"X-Final-Chunk": "maybe"}).status_code, 422)
        self.assertEqual(
            self.upload(
                token,
                lecture_id,
                wav_audio(seconds=1),
                extra_headers={"X-Overlap-Seconds": "1", "X-Final-Chunk": "false"},
            ).status_code,
            422,
        )
        self.assertEqual(self.upload(token, lecture_id, chunk_id="bad-id").status_code, 422)
        self.assertEqual(self.upload(token, lecture_id, extra_headers={"Content-Type": "audio/webm"}).status_code, 415)
        self.assertEqual(self.engine.calls, 0)
        self.assertEqual(self.upload(token, lecture_id, wav_audio(seconds=0.05)).status_code, 200)

    def test_overlap_and_final_contract_reaches_transcriber(self):
        token = self.activate()
        lecture_id = self.lecture(token)
        response = self.upload(
            token,
            lecture_id,
            wav_audio(seconds=2.5),
            start="12.5",
            extra_headers={"X-Overlap-Seconds": "2", "X-Final-Chunk": "false"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.engine.last_contract, (2.0, False))
        self.assertEqual(response.json()["segments"][0]["start"], 12.5)

    def test_overlap_only_final_flush_can_be_empty_and_is_idempotent(self):
        token = self.activate()
        lecture_id, chunk_id = self.lecture(token), str(uuid.uuid4())
        self.engine.result = []
        first = self.upload(
            token,
            lecture_id,
            wav_audio(seconds=2),
            start="8",
            chunk_id=chunk_id,
            extra_headers={"X-Overlap-Seconds": "2", "X-Final-Chunk": "true"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["segments"], [])
        self.assertEqual(self.engine.last_contract, (2.0, True))
        repeated = self.upload(
            token,
            lecture_id,
            wav_audio(seconds=2),
            start="8",
            chunk_id=chunk_id,
            extra_headers={"X-Overlap-Seconds": "2", "X-Final-Chunk": "true"},
        )
        self.assertEqual(repeated.json(), first.json())
        self.assertEqual(self.engine.calls, 1)

    def test_size_is_bounded_without_content_length(self):
        token = self.activate()
        lecture_id = self.lecture(token)
        oversize = b"x" * (self.settings.max_upload_bytes + 1)
        response = self.upload(token, lecture_id, oversize)
        self.assertEqual(response.status_code, 413)
        # httpx uses chunked transfer for iterators, omitting Content-Length.
        streamed = self.upload(token, lecture_id, iter([oversize[:1000], oversize[1000:]]))
        self.assertEqual(streamed.status_code, 413)
        oversized_json = b'{"username":"' + b"x" * self.settings.max_upload_bytes + b'"}'
        login = self.client.post(
            "/auth/login",
            content=iter([oversized_json[:1000], oversized_json[1000:]]),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(login.status_code, 413, login.text)
        self.assertEqual(self.engine.calls, 0)

    def test_inference_capacity_and_failed_retry(self):
        token = self.activate()
        lecture_id = self.lecture(token)
        self.engine.block = True
        in_flight_id = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(self.upload, token, lecture_id, None, in_flight_id)
            try:
                self.assertTrue(self.engine.entered.wait(timeout=5))
                same = self.upload(token, lecture_id, chunk_id=in_flight_id)
                self.assertEqual(same.status_code, 409)
                self.assertEqual(same.headers["Retry-After"], "2")
                busy = self.upload(token, lecture_id)
                self.assertEqual(busy.status_code, 429)
                self.assertEqual(busy.headers["Retry-After"], "2")
            finally:
                self.engine.release.set()
            self.assertEqual(first.result(timeout=5).status_code, 200)
        replayed = self.upload(token, lecture_id, chunk_id=in_flight_id)
        self.assertEqual(replayed.status_code, 200)
        self.assertEqual(self.engine.calls, 1)
        self.engine.block = False
        self.engine.fail = True
        # The first upload finalized its recording. A failed inference retry
        # belongs to a new lecture because finalized lectures intentionally
        # reject every previously unseen chunk.
        retry_lecture_id = self.lecture(token, "실패 재시도 수업")
        chunk_id = str(uuid.uuid4())
        with self.assertLogs("classroom", level="ERROR"):
            failed = self.upload(token, retry_lecture_id, chunk_id=chunk_id)
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("private-test-path", failed.text)
        self.engine.fail = False
        self.assertEqual(self.upload(token, retry_lecture_id, chunk_id=chunk_id).status_code, 200)

    def test_origins_validation_privacy_and_rate_limit(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        rejected = self.client.get("/health", headers={"Origin": "https://evil.example"})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.headers["Cache-Control"], "no-store")

        def body_must_not_be_read():
            raise AssertionError("foreign-origin body was read before the origin guard")
            yield b""  # pragma: no cover

        foreign_post = self.client.post(
            "/auth/login",
            content=body_must_not_be_read(),
            headers={"Origin": "https://evil.example", "Content-Type": "application/json"},
        )
        self.assertEqual(foreign_post.status_code, 403)
        allowed = self.client.options("/auth/login", headers={
            "Origin": "https://student.github.io", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type",
        })
        self.assertEqual(allowed.headers["Access-Control-Allow-Origin"], "https://student.github.io")
        invalid = self.client.post("/auth/activate", json={"username": "user-alpha", "password": "abc", "setup_code": self.codes["user-alpha"]})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn('"abc"', invalid.text)
        self.assertNotIn(self.codes["user-alpha"], invalid.text)
        for _ in range(10):
            self.assertEqual(self.client.post("/auth/login", json={"username": "unknown", "password": "incorrect"}).status_code, 401)
        self.assertEqual(self.client.post("/auth/login", json={"username": "unknown", "password": "incorrect"}).status_code, 429)

    def test_invite_generation_preserves_active_accounts(self):
        self.activate()
        rejected_path = self.directory / "rejected-invitations.txt"
        with self.assertRaisesRegex(ValueError, "loopback"):
            create_invitations(
                self.database,
                "https://student.github.io/classroom/",
                "http://127.0.0.1:8765",
                rejected_path,
            )
        self.assertFalse(rejected_path.exists())
        output_path = self.directory / "invitations.txt"
        count = create_invitations(
            self.database,
            "https://student.github.io/classroom/",
            "https://private-classroom.trycloudflare.com",
            output_path,
        )
        self.assertEqual(count, 1)
        self.assertEqual(output_path.stat().st_mode & 0o777, 0o600)
        invitation = next(line for line in output_path.read_text(encoding="utf-8").splitlines() if line.startswith("https://"))
        values = parse_qs(urlsplit(invitation).fragment)
        self.assertEqual(values["username"], ["user-beta"])
        self.assertNotIn("api", values)
        self.assertIn("현재 서버 주소: https://private-classroom.trycloudflare.com", output_path.read_text(encoding="utf-8"))
        response = self.client.post("/auth/activate", json={"username": "user-beta", "setup_code": values["setup_code"][0], "password": self.password})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post("/auth/login", json={"username": "user-alpha", "password": self.password}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
