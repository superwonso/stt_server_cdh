from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import httpx

from server.drive_storage import (
    DRIVE_FILE_SCOPE,
    GOOGLE_TOKEN_URL,
    DriveAuthenticationError,
    DriveAccountIdentity,
    DriveConfigurationError,
    DriveConflictError,
    DriveIntegrityError,
    DriveProtocolError,
    DriveStorageError,
    DriveTransportError,
    GoogleDriveStorage,
    UploadCheckpoint,
    derive_object_key,
)


class TrackingByteStream(httpx.SyncByteStream):
    def __init__(self, content: bytes):
        self.content = content
        self.closed = False

    def __iter__(self):
        yield self.content

    def close(self) -> None:
        self.closed = True


class DriveStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.token_path = self.root / "authorized-user.json"
        self.client_id = "test-client.apps.googleusercontent.com"
        self.client_secret = "test-only-client-secret"
        self.refresh_token = "test-only-refresh-token"
        self.access_token = "test-only-access-token"
        self.write_token(self.token_document())
        self.object_key = "a" * 64

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def token_document(self, **changes: object) -> dict[str, object]:
        value: dict[str, object] = {
            "type": "authorized_user",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "token_uri": GOOGLE_TOKEN_URL,
            "scopes": [DRIVE_FILE_SCOPE],
        }
        value.update(changes)
        return value

    def write_token(self, value: dict[str, object], path: Path | None = None) -> Path:
        target = path or self.token_path
        target.write_text(json.dumps(value), encoding="utf-8")
        target.chmod(0o600)
        return target

    def token_response(self, request: httpx.Request) -> httpx.Response:
        self.assertEqual(str(request.url), GOOGLE_TOKEN_URL)
        form = parse_qs(request.content.decode("ascii"))
        self.assertEqual(form["client_id"], [self.client_id])
        self.assertEqual(form["client_secret"], [self.client_secret])
        self.assertEqual(form["refresh_token"], [self.refresh_token])
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertNotIn(self.refresh_token, str(request.url))
        return httpx.Response(
            200,
            json={
                "access_token": self.access_token,
                "expires_in": 3600,
                "scope": DRIVE_FILE_SCOPE,
                "token_type": "Bearer",
            },
        )

    @staticmethod
    def checksums(content: bytes) -> tuple[str, str]:
        return (
            hashlib.sha256(content).hexdigest(),
            hashlib.md5(content, usedforsecurity=False).hexdigest(),
        )

    def metadata(
        self,
        *,
        content: bytes,
        file_id: str = "driveFile_123",
        name: str = "2026-09-05-recording.wav",
        object_key: str | None = None,
        trashed: bool = False,
        parent_id: str | None = None,
    ) -> dict[str, object]:
        sha256, md5 = self.checksums(content)
        key = object_key or self.object_key
        return {
            "id": file_id,
            "name": name,
            "mimeType": "audio/wav",
            "size": str(len(content)),
            "md5Checksum": md5,
            "sha256Checksum": sha256,
            "trashed": trashed,
            "createdTime": "2026-09-05T00:00:00.000Z",
            "modifiedTime": "2026-09-05T00:00:01.000Z",
            "version": "7",
            "parents": [] if parent_id is None else [parent_id],
            "appProperties": {
                "stt_schema": "stt-recording-v1",
                "stt_object": key,
                "stt_size": str(len(content)),
                "stt_sha256": sha256,
                "stt_md5": md5,
            },
        }

    def write_source(self, content: bytes) -> Path:
        path = self.root / "source.wav"
        path.write_bytes(content)
        path.chmod(0o600)
        return path

    def test_refreshes_authorized_user_token_and_saves_it_mode_0600(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            self.assertEqual(request.url.path, "/drive/v3/files")
            self.assertEqual(request.headers["authorization"], f"Bearer {self.access_token}")
            return httpx.Response(200, json={"files": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        self.assertTrue(storage.verify_connection())
        self.assertEqual(len(requests), 2)

        persisted = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["token"], self.access_token)
        self.assertIn("expiry", persisted)
        self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)
        client.close()

    def test_account_identity_requests_only_an_opaque_permission_id(self) -> None:
        requested_fields: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            self.assertEqual(request.url.path, "/drive/v3/about")
            requested_fields.append(request.url.params["fields"])
            return httpx.Response(200, json={"user": {"permissionId": "opaqueUser_123"}})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        identity = storage.account_identity()

        self.assertIsInstance(identity, DriveAccountIdentity)
        self.assertEqual(identity.permission_id, "opaqueUser_123")
        self.assertEqual(
            identity.oauth_client_fingerprint,
            hashlib.sha256(self.client_id.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(requested_fields, ["user(permissionId)"])
        self.assertNotIn("opaqueUser_123", repr(identity))
        client.close()

    def test_refresh_reloads_a_completed_oauth_replacement_before_writing(self) -> None:
        new_client_id = "replacement-client.apps.googleusercontent.com"
        new_client_secret = "replacement-client-secret"
        new_refresh_token = "replacement-refresh-token"
        refresh_forms: list[dict[str, list[str]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                form = parse_qs(request.content.decode("ascii"))
                refresh_forms.append(form)
                self.assertEqual(form["client_id"], [new_client_id])
                self.assertEqual(form["client_secret"], [new_client_secret])
                self.assertEqual(form["refresh_token"], [new_refresh_token])
                return httpx.Response(
                    200,
                    json={
                        "access_token": "replacement-access-token",
                        "expires_in": 3600,
                        "scope": DRIVE_FILE_SCOPE,
                        "token_type": "Bearer",
                    },
                )
            self.assertEqual(
                request.headers["authorization"], "Bearer replacement-access-token"
            )
            return httpx.Response(200, json={"files": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)

        # Simulate a completed CLI reauthorization after this server object
        # loaded the former grant but before it needs an access token.
        self.write_token(
            self.token_document(
                client_id=new_client_id,
                client_secret=new_client_secret,
                refresh_token=new_refresh_token,
            )
        )
        self.assertTrue(storage.verify_connection())

        persisted = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(len(refresh_forms), 1)
        self.assertEqual(persisted["refresh_token"], new_refresh_token)
        self.assertEqual(persisted["client_secret"], new_client_secret)
        self.assertNotEqual(persisted["refresh_token"], self.refresh_token)
        client.close()

    def test_refresh_holds_the_shared_token_lock_during_network_exchange(self) -> None:
        lock_was_held = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal lock_was_held
            if str(request.url) == GOOGLE_TOKEN_URL:
                descriptor = os.open(self.root / "token.lock", os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_was_held = True
                finally:
                    os.close(descriptor)
                return self.token_response(request)
            return httpx.Response(200, json={"files": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        self.assertTrue(storage.verify_connection())
        self.assertTrue(lock_was_held)
        self.assertEqual(stat.S_IMODE((self.root / "token.lock").stat().st_mode), 0o600)
        client.close()

    def test_rejected_cached_access_token_is_refreshed_instead_of_reused_from_disk(self) -> None:
        self.write_token(
            self.token_document(
                token="cached-but-rejected-token",
                expiry="2099-01-01T00:00:00Z",
            )
        )
        authorizations: list[str] = []
        refreshes = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal refreshes
            if str(request.url) == GOOGLE_TOKEN_URL:
                refreshes += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": "new-after-rejection",
                        "expires_in": 3600,
                        "scope": DRIVE_FILE_SCOPE,
                        "token_type": "Bearer",
                    },
                )
            authorizations.append(request.headers["authorization"])
            if len(authorizations) == 1:
                return httpx.Response(401, content=b"private rejection body")
            return httpx.Response(200, json={"files": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        self.assertTrue(storage.verify_connection())
        self.assertEqual(refreshes, 1)
        self.assertEqual(
            authorizations,
            ["Bearer cached-but-rejected-token", "Bearer new-after-rejection"],
        )
        client.close()

    def test_token_parent_scope_and_lock_paths_fail_closed(self) -> None:
        public_parent = self.root / "public-parent"
        public_parent.mkdir(mode=0o755)
        public_token = self.write_token(
            self.token_document(), public_parent / "token.json"
        )
        with self.assertRaises(DriveConfigurationError) as raised:
            GoogleDriveStorage.from_token_file(public_token)
        self.assertEqual(raised.exception.code, "credential_directory")

        private_parent = self.root / "private-parent"
        private_parent.mkdir(mode=0o700)
        real_token = self.write_token(
            self.token_document(), private_parent / "token.json"
        )
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(private_parent, target_is_directory=True)
        with self.assertRaises(DriveConfigurationError) as raised:
            GoogleDriveStorage.from_token_file(linked_parent / real_token.name)
        self.assertEqual(raised.exception.code, "credential_directory")

        (private_parent / "token.lock").symlink_to(self.root / "outside-lock")
        with self.assertRaises(DriveConfigurationError) as raised:
            GoogleDriveStorage.from_token_file(real_token)
        self.assertEqual(raised.exception.code, "credential_lock")

    def test_owned_http_client_is_closed_when_credential_loading_fails(self) -> None:
        self.token_path.chmod(0o644)
        owned_client = mock.Mock(spec=httpx.Client)
        with mock.patch("server.drive_storage.httpx.Client", return_value=owned_client):
            with self.assertRaises(DriveConfigurationError):
                GoogleDriveStorage.from_token_file(self.token_path)
        owned_client.close.assert_called_once_with()

    def test_scope_is_revalidated_after_an_oauth_file_replacement(self) -> None:
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: self.fail("no network request should use a broadened grant")
            )
        )
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        self.write_token(
            self.token_document(
                scopes=[DRIVE_FILE_SCOPE, "https://www.googleapis.com/auth/drive"]
            )
        )
        with self.assertRaises(DriveConfigurationError) as raised:
            storage.verify_connection()
        self.assertEqual(raised.exception.code, "credential_scope")
        client.close()

    def test_refresh_response_cannot_silently_broaden_the_scope(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), GOOGLE_TOKEN_URL)
            return httpx.Response(
                200,
                json={
                    "access_token": "wrong-scope-token",
                    "expires_in": 3600,
                    "scope": (
                        f"{DRIVE_FILE_SCOPE} https://www.googleapis.com/auth/drive"
                    ),
                    "token_type": "Bearer",
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        with self.assertRaises(DriveAuthenticationError) as raised:
            storage.verify_connection()
        self.assertEqual(raised.exception.code, "token_refresh_invalid")
        client.close()

    def test_metadata_error_and_invalid_download_iterator_close_response_streams(self) -> None:
        error_stream = TrackingByteStream(b"private provider failure")

        def error_handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            return httpx.Response(503, stream=error_stream)

        error_client = httpx.Client(transport=httpx.MockTransport(error_handler))
        storage = GoogleDriveStorage.from_token_file(
            self.token_path, client=error_client
        )
        with self.assertRaises(DriveTransportError):
            storage.verify_connection()
        self.assertTrue(error_stream.closed)
        error_client.close()

        content = b"download body"
        remote = self.metadata(content=content)
        download_stream = TrackingByteStream(content)

        def download_handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("alt") == "media":
                return httpx.Response(
                    200,
                    stream=download_stream,
                    headers={"Content-Length": str(len(content))},
                )
            return httpx.Response(200, json=remote)

        download_client = httpx.Client(transport=httpx.MockTransport(download_handler))
        storage = GoogleDriveStorage.from_token_file(
            self.token_path, client=download_client
        )
        download = storage.open_download("driveFile_123")
        with self.assertRaises(ValueError):
            next(download.iter_bytes(0))
        self.assertTrue(download_stream.closed)
        download_client.close()

    def test_uploads_in_aligned_chunks_and_verifies_both_checksums(self) -> None:
        content = b"WAVE" + bytes(range(251)) * 1300
        source = self.write_source(content)
        remote = self.metadata(content=content)
        session_url = (
            "https://www.googleapis.com/upload/drive/v3/files"
            "?uploadType=resumable&upload_id=test-session"
        )
        uploaded = bytearray()
        content_ranges: list[str] = []
        checkpoints: list[UploadCheckpoint] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            if request.method == "GET" and request.url.path == "/drive/v3/files":
                self.assertIn("stt_object", request.url.params["q"])
                return httpx.Response(200, json={"files": []})
            if request.method == "POST" and request.url.path == "/upload/drive/v3/files":
                body = json.loads(request.content)
                self.assertNotIn("permissions", body)
                self.assertEqual(body["mimeType"], "audio/wav")
                self.assertEqual(body["appProperties"]["stt_object"], self.object_key)
                self.assertEqual(body["parents"], ["folder_123"])
                self.assertNotIn("owner", json.dumps(body))
                self.assertEqual(request.headers["x-upload-content-length"], str(len(content)))
                return httpx.Response(200, headers={"Location": session_url})
            if request.method == "PUT" and request.url.params.get("upload_id"):
                content_ranges.append(request.headers["content-range"])
                uploaded.extend(request.content)
                if len(uploaded) < len(content):
                    return httpx.Response(308, headers={"Range": f"bytes=0-{len(uploaded) - 1}"})
                return httpx.Response(200, json=remote)
            if request.method == "GET" and request.url.path == "/drive/v3/files/driveFile_123":
                return httpx.Response(200, json=remote)
            self.fail(f"unexpected request: {request.method} {request.url.path}")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(
            self.token_path,
            folder_id="folder_123",
            client=client,
            chunk_size=256 * 1024,
        )
        result = storage.upload_recording(
            source,
            object_key=self.object_key,
            name="2026-09-05-recording.wav",
            on_checkpoint=checkpoints.append,
        )

        self.assertEqual(uploaded, content)
        self.assertEqual(
            content_ranges,
            [
                f"bytes 0-{256 * 1024 - 1}/{len(content)}",
                f"bytes {256 * 1024}-{len(content) - 1}/{len(content)}",
            ],
        )
        self.assertEqual(
            [item.committed_bytes for item in checkpoints],
            [0, 256 * 1024, len(content)],
        )
        self.assertEqual(result.size, len(content))
        self.assertEqual(result.sha256_checksum, self.checksums(content)[0])
        self.assertNotIn(session_url, repr(checkpoints[0]))
        self.assertNotIn("2026-09-05-recording.wav", repr(result))
        client.close()

    def test_resumes_from_server_acknowledged_offset_after_interruption(self) -> None:
        content = b"RIFF" + os.urandom(300_000)
        source = self.write_source(content)
        remote = self.metadata(content=content)
        sha256, md5 = self.checksums(content)
        session_url = (
            "https://www.googleapis.com/upload/drive/v3/files"
            "?uploadType=resumable&upload_id=resume-session"
        )
        saved: list[UploadCheckpoint] = []
        phase = "interrupt"
        uploaded_after_resume = bytearray()

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal phase
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            if request.method == "GET" and request.url.path == "/drive/v3/files":
                return httpx.Response(200, json={"files": []})
            if request.method == "POST":
                return httpx.Response(200, headers={"Location": session_url})
            if request.method == "PUT" and request.url.params.get("upload_id"):
                if phase == "interrupt":
                    phase = "resume"
                    return httpx.Response(503, content=b"private-provider-detail")
                if request.headers["content-range"] == f"bytes */{len(content)}":
                    return httpx.Response(308, headers={"Range": "bytes=0-65535"})
                self.assertEqual(
                    request.headers["content-range"],
                    f"bytes 65536-{len(content) - 1}/{len(content)}",
                )
                uploaded_after_resume.extend(request.content)
                return httpx.Response(200, json=remote)
            if request.method == "GET" and request.url.path.endswith("driveFile_123"):
                return httpx.Response(200, json=remote)
            self.fail(f"unexpected request: {request.method} {request.url}")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(
            self.token_path, client=client, chunk_size=512 * 1024
        )
        with self.assertRaises(DriveTransportError) as raised:
            storage.upload_recording(
                source,
                object_key=self.object_key,
                name="recording.wav",
                on_checkpoint=saved.append,
            )
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("private-provider-detail", str(raised.exception))
        self.assertEqual(len(saved), 1)
        serialized = saved[0].to_dict()
        restored = UploadCheckpoint.from_dict(serialized)
        self.assertEqual(restored.sha256_checksum, sha256)
        self.assertEqual(restored.md5_checksum, md5)

        result = storage.upload_recording(
            source,
            object_key=self.object_key,
            name="recording.wav",
            checkpoint=restored,
            on_checkpoint=saved.append,
        )
        self.assertEqual(uploaded_after_resume, content[65536:])
        self.assertEqual(result.file_id, "driveFile_123")
        self.assertEqual(saved[-2].committed_bytes, 65536)
        self.assertEqual(saved[-1].committed_bytes, len(content))
        client.close()

    def test_reconcile_saved_session_never_creates_or_advances_upload(self) -> None:
        content = b"RIFF" + os.urandom(1024)
        sha256, md5 = self.checksums(content)
        remote = self.metadata(content=content)
        session_url = (
            "https://www.googleapis.com/upload/drive/v3/files"
            "?uploadType=resumable&upload_id=reconcile-session"
        )
        checkpoint = UploadCheckpoint(
            session_uri=session_url,
            object_key=self.object_key,
            total_size=len(content),
            sha256_checksum=sha256,
            md5_checksum=md5,
            committed_bytes=len(content) // 2,
        )
        completed = False
        observed_ranges: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            if request.method == "PUT" and str(request.url) == session_url:
                observed_ranges.append(request.headers["content-range"])
                self.assertEqual(request.content, b"")
                if completed:
                    return httpx.Response(200, json=remote)
                return httpx.Response(
                    308,
                    headers={"Range": f"bytes=0-{checkpoint.committed_bytes - 1}"},
                )
            self.fail(f"unexpected request: {request.method} {request.url}")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)

        self.assertIsNone(storage.reconcile_upload_session(checkpoint))
        completed = True
        result = storage.reconcile_upload_session(checkpoint)

        self.assertIsNotNone(result)
        self.assertEqual(result.file_id, "driveFile_123")
        self.assertEqual(
            observed_ranges,
            [f"bytes */{len(content)}", f"bytes */{len(content)}"],
        )
        client.close()

    def test_upload_without_acknowledged_progress_retries_later(self) -> None:
        content = b"RIFF" + os.urandom(300_000)
        source = self.write_source(content)
        session_url = (
            "https://www.googleapis.com/upload/drive/v3/files"
            "?uploadType=resumable&upload_id=no-progress-session"
        )
        saved: list[UploadCheckpoint] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            if request.method == "GET" and request.url.path == "/drive/v3/files":
                return httpx.Response(200, json={"files": []})
            if request.method == "POST":
                return httpx.Response(200, headers={"Location": session_url})
            if request.method == "PUT" and request.url.params.get("upload_id"):
                return httpx.Response(308)
            self.fail(f"unexpected request: {request.method} {request.url}")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(
            self.token_path, client=client, chunk_size=512 * 1024
        )
        with self.assertRaises(DriveTransportError) as raised:
            storage.upload_recording(
                source,
                object_key=self.object_key,
                name="recording.wav",
                on_checkpoint=saved.append,
            )
        self.assertEqual(raised.exception.code, "upload_no_progress")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].committed_bytes, 0)
        client.close()

    def test_existing_remote_file_is_reconciled_without_new_upload(self) -> None:
        content = b"already uploaded recording"
        source = self.write_source(content)
        remote = self.metadata(content=content)
        posts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal posts
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            if request.method == "GET" and request.url.path == "/drive/v3/files":
                return httpx.Response(200, json={"files": [remote]})
            if request.method == "POST":
                posts += 1
            self.fail("reconciled upload must not create another Drive file")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        result = storage.upload_recording(
            source, object_key=self.object_key, name="recording.wav"
        )
        self.assertEqual(result.file_id, "driveFile_123")
        self.assertEqual(posts, 0)
        client.close()

    def test_duplicate_reconciliation_and_checksum_mismatch_fail_closed(self) -> None:
        content = b"local recording"
        source = self.write_source(content)
        valid = self.metadata(content=content)
        duplicate = dict(valid, id="driveFile_456")
        mode = "duplicate"

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            if request.url.path == "/drive/v3/files":
                if mode == "duplicate":
                    return httpx.Response(200, json={"files": [valid, duplicate]})
                mismatch = dict(valid, sha256Checksum="0" * 64)
                return httpx.Response(200, json={"files": [mismatch]})
            self.fail("unexpected request")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        with self.assertRaises(DriveConflictError):
            storage.upload_recording(source, object_key=self.object_key, name="recording.wav")
        mode = "mismatch"
        with self.assertRaises(DriveIntegrityError):
            storage.upload_recording(source, object_key=self.object_key, name="recording.wav")
        client.close()

    def test_ensure_folder_reconciles_and_creates_only_private_app_folder(self) -> None:
        created_body: dict[str, object] = {}
        folder_metadata = {
            "id": "archiveFolder_123",
            "name": "STT recordings",
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
            "appProperties": {
                "stt_schema": "stt-archive-folder-v1",
                "stt_object": self.object_key,
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal created_body
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            if request.method == "GET":
                return httpx.Response(200, json={"files": []})
            if request.method == "POST" and request.url.path == "/drive/v3/files":
                created_body = json.loads(request.content)
                return httpx.Response(200, json=folder_metadata)
            self.fail("unexpected request")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        folder = storage.ensure_folder(
            object_key=self.object_key,
            name="STT recordings",
        )
        self.assertEqual(folder.file_id, "archiveFolder_123")
        self.assertEqual(created_body["mimeType"], "application/vnd.google-apps.folder")
        self.assertNotIn("permissions", created_body)
        self.assertNotIn("owners", created_body)
        client.close()

    def test_ensure_folder_returns_one_existing_folder_and_rejects_duplicates(self) -> None:
        folder = {
            "id": "archiveFolder_123",
            "name": "STT recordings",
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
            "appProperties": {
                "stt_schema": "stt-archive-folder-v1",
                "stt_object": self.object_key,
            },
        }
        duplicates = False

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            self.assertEqual(request.method, "GET")
            files = [folder, dict(folder, id="archiveFolder_456")] if duplicates else [folder]
            return httpx.Response(200, json={"files": files})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        self.assertEqual(
            storage.ensure_folder(object_key=self.object_key, name="STT recordings").file_id,
            "archiveFolder_123",
        )
        duplicates = True
        with self.assertRaises(DriveConflictError):
            storage.ensure_folder(object_key=self.object_key, name="STT recordings")
        client.close()

    def test_child_folder_requires_and_creates_the_exact_private_parent(self) -> None:
        parent_id = "archiveRoot_123"
        folder_metadata = {
            "id": "accountFolder_123",
            "name": "user-alpha",
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
            "parents": [parent_id],
            "appProperties": {
                "stt_schema": "stt-archive-folder-v1",
                "stt_object": self.object_key,
            },
        }
        created_body: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal created_body
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            if request.method == "GET":
                return httpx.Response(200, json={"files": []})
            if request.method == "POST":
                created_body = json.loads(request.content)
                return httpx.Response(200, json=folder_metadata)
            self.fail("unexpected request")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        folder = storage.ensure_folder(
            object_key=self.object_key,
            name="user-alpha",
            parent_id=parent_id,
        )

        self.assertEqual(created_body["parents"], [parent_id])
        self.assertEqual(folder.parents, (parent_id,))
        client.close()

    def test_existing_child_folder_in_an_unexpected_parent_fails_closed(self) -> None:
        folder = {
            "id": "accountFolder_123",
            "name": "user-alpha",
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
            "parents": ["unexpectedParent_1"],
            "appProperties": {
                "stt_schema": "stt-archive-folder-v1",
                "stt_object": self.object_key,
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            return httpx.Response(200, json={"files": [folder]})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        with self.assertRaises(DriveIntegrityError):
            storage.ensure_folder(
                object_key=self.object_key,
                name="user-alpha",
                parent_id="archiveRoot_123",
            )
        client.close()

    def test_move_file_uses_parent_parameters_and_requires_exact_confirmation(self) -> None:
        content = b"private audio"
        remote = self.metadata(
            content=content,
            parent_id="accountFolder_123",
        )
        observed: httpx.Request | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal observed
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            observed = request
            return httpx.Response(200, json=remote)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        moved = storage.move_file(
            "driveFile_123",
            previous_parent_id="archiveRoot_123",
            new_parent_id="accountFolder_123",
        )

        self.assertIsNotNone(observed)
        self.assertEqual(observed.method, "PATCH")
        self.assertEqual(observed.url.params["addParents"], "accountFolder_123")
        self.assertEqual(observed.url.params["removeParents"], "archiveRoot_123")
        self.assertEqual(moved.parents, ("accountFolder_123",))
        client.close()

    def test_move_file_rejects_an_unconfirmed_parent(self) -> None:
        content = b"private audio"
        remote = self.metadata(content=content, parent_id="archiveRoot_123")

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            return httpx.Response(200, json=remote)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        with self.assertRaises(DriveIntegrityError):
            storage.move_file(
                "driveFile_123",
                previous_parent_id="archiveRoot_123",
                new_parent_id="accountFolder_123",
            )
        client.close()

    def test_range_download_validates_headers_and_closes_after_iteration(self) -> None:
        content = b"0123456789abcdef"
        remote = self.metadata(content=content)
        media_request: httpx.Request | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal media_request
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            if request.url.params.get("alt") == "media":
                media_request = request
                return httpx.Response(
                    206,
                    stream=httpx.ByteStream(content[3:11]),
                    headers={
                        "Content-Range": f"bytes 3-10/{len(content)}",
                        "Content-Length": "8",
                        "Content-Type": "audio/wav",
                        "ETag": '"private-etag"',
                    },
                )
            if request.url.path.endswith("driveFile_123"):
                return httpx.Response(200, json=remote)
            self.fail("unexpected request")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        with storage.open_download("driveFile_123", start=3, end=11) as download:
            self.assertEqual(download.status_code, 206)
            self.assertEqual(download.content_length, 8)
            self.assertEqual(download.content_range, f"bytes 3-10/{len(content)}")
            self.assertEqual(b"".join(download.iter_bytes(3)), content[3:11])
        self.assertIsNotNone(media_request)
        self.assertEqual(media_request.headers["range"], "bytes=3-10")
        self.assertEqual(media_request.headers["accept-encoding"], "identity")
        client.close()

    def test_explicit_full_file_range_is_still_proxied_as_206(self) -> None:
        content = b"full range"
        remote = self.metadata(content=content)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            if request.url.params.get("alt") == "media":
                self.assertEqual(request.headers["range"], f"bytes=0-{len(content) - 1}")
                return httpx.Response(
                    206,
                    stream=httpx.ByteStream(content),
                    headers={
                        "Content-Length": str(len(content)),
                        "Content-Range": f"bytes 0-{len(content) - 1}/{len(content)}",
                    },
                )
            return httpx.Response(200, json=remote)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        with storage.open_download(
            "driveFile_123", start=0, end=len(content)
        ) as download:
            self.assertEqual(download.status_code, 206)
            self.assertEqual(b"".join(download.iter_bytes()), content)
        client.close()

    def test_trash_requires_confirmed_trashed_metadata(self) -> None:
        content = b"recording"
        remote = self.metadata(content=content, trashed=True)
        patch_body: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal patch_body
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            self.assertEqual(request.method, "PATCH")
            patch_body = json.loads(request.content)
            return httpx.Response(200, json=remote)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        result = storage.trash("driveFile_123")
        self.assertTrue(result.trashed)
        self.assertEqual(patch_body, {"trashed": True})
        client.close()

    def test_errors_and_reprs_never_reflect_provider_body_or_secrets(self) -> None:
        private_detail = "provider-private-body-with-token"

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                return httpx.Response(400, content=private_detail.encode())
            self.fail("Drive request should not run after rejected refresh")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        with self.assertRaises(DriveAuthenticationError) as raised:
            storage.verify_connection()
        serialized_error = str(raised.exception) + repr(raised.exception)
        for private in (
            private_detail,
            self.client_secret,
            self.refresh_token,
            self.access_token,
        ):
            self.assertNotIn(private, serialized_error)
        self.assertIsInstance(raised.exception, DriveStorageError)
        client.close()

    def test_rejects_redirect_and_malformed_range_without_reading_body(self) -> None:
        content = b"0123456789"
        remote = self.metadata(content=content)
        mode = "redirect"

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                return self.token_response(request)
            if request.url.params.get("alt") == "media":
                if mode == "redirect":
                    return httpx.Response(
                        307,
                        headers={"Location": "https://attacker.invalid/secret"},
                    )
                return httpx.Response(
                    206,
                    stream=httpx.ByteStream(content[:3]),
                    headers={"Content-Range": "bytes 1-3/10", "Content-Length": "3"},
                )
            return httpx.Response(200, json=remote)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        storage = GoogleDriveStorage.from_token_file(self.token_path, client=client)
        with self.assertRaises(DriveProtocolError):
            storage.open_download("driveFile_123", start=0, end=3)
        mode = "range"
        with self.assertRaises(DriveIntegrityError):
            storage.open_download("driveFile_123", start=0, end=3)
        client.close()

    def test_object_key_is_stable_and_hides_owner_and_lecture(self) -> None:
        lecture_id = str(uuid.uuid4())
        deployment_key = bytes(range(32))
        first = derive_object_key(deployment_key, "private-account-name", lecture_id)
        second = derive_object_key(deployment_key, "private-account-name", lecture_id)
        other = derive_object_key(deployment_key, "another-account", lecture_id)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertNotIn("private-account-name", first)
        self.assertNotIn(lecture_id, first)


if __name__ == "__main__":
    unittest.main()
