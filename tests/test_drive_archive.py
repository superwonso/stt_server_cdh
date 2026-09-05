from __future__ import annotations

import asyncio
import hashlib
import hmac
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import anyio
from fastapi.testclient import TestClient

from server.app import CloseableStreamingResponse, create_app
from server.db import Database
from server.drive_archive import DriveArchiveManager
from server.drive_storage import (
    DriveAccountIdentity,
    DriveFileMetadata,
    DriveIntegrityError,
    DriveNotFoundError,
    DriveTransportError,
    DriveUploadSessionExpired,
    UploadCheckpoint,
)
from server.recordings import RecordingStore
from server.security import digest
from server.settings import Settings


ACCOUNTS = ("user-alpha", "user-beta")
SESSION_URI = (
    "https://www.googleapis.com/upload/drive/v3/files"
    "?uploadType=resumable&upload_id=fake-private-session"
)


def _checksums(content: bytes) -> tuple[str, str]:
    return (
        hashlib.sha256(content).hexdigest(),
        hashlib.md5(content, usedforsecurity=False).hexdigest(),
    )


class FakeDownload:
    def __init__(
        self,
        content: bytes,
        start: int | None,
        end: int | None,
        metadata: DriveFileMetadata,
    ):
        first = 0 if start is None else start
        last = len(content) if end is None else end
        self.file = metadata
        self.payload = content[first:last]
        self.status_code = 206 if start is not None or end is not None else 200
        self.content_length = len(self.payload)
        self.content_range = (
            f"bytes {first}-{last - 1}/{len(content)}" if self.status_code == 206 else None
        )
        self.closed = False

    def iter_bytes(self):
        try:
            midpoint = len(self.payload) // 2
            if midpoint:
                yield self.payload[:midpoint]
            if midpoint < len(self.payload):
                yield self.payload[midpoint:]
        finally:
            self.close()

    def close(self):
        self.closed = True


class FakeDrive:
    """Stateful Drive boundary fake; locators never need to reach API clients."""

    def __init__(self):
        self.folder_id = None
        self.folder_locator = "privateFolder_1"
        self.folders_by_object: dict[str, DriveFileMetadata] = {}
        self.folders_by_id: dict[str, DriveFileMetadata] = {}
        self.folder_calls: list[tuple[str, str, str | None]] = []
        self.permission_id = "opaqueGoogleUser_1"
        self.oauth_client_fingerprint = "c" * 64
        self.files_by_object: dict[str, tuple[DriveFileMetadata, bytes]] = {}
        self.files_by_id: dict[str, tuple[DriveFileMetadata, bytes]] = {}
        self.upload_calls = 0
        self.upload_parent_ids: list[str | None] = []
        self.created_count = 0
        self.received_checkpoints: list[UploadCheckpoint | None] = []
        self.saved_checkpoints: list[UploadCheckpoint] = []
        self.reconciled_checkpoints: list[UploadCheckpoint] = []
        self.reconcile_result: DriveFileMetadata | None = None
        self.reconcile_error: BaseException | None = None
        self.find_calls: list[str] = []
        self.find_error: BaseException | None = None
        self.upload_error: BaseException | None = None
        self.fail_after_checkpoint = False
        self.trash_error: BaseException | None = None
        self.trash_calls: list[str] = []
        self.move_calls: list[tuple[str, str, str]] = []
        self.move_error: BaseException | None = None
        self.move_error_after_commit: BaseException | None = None
        self.block_move = False
        self.move_entered = threading.Event()
        self.move_release = threading.Event()
        self.block_trash = False
        self.trash_entered = threading.Event()
        self.trash_release = threading.Event()
        self.download_calls: list[tuple[str, int | None, int | None]] = []
        self.last_download: FakeDownload | None = None
        self.upload_entered = threading.Event()
        self.upload_release = threading.Event()
        self.block_upload = False
        self.closed = False
        self.lock = threading.RLock()

    @staticmethod
    def _metadata(
        file_id: str,
        object_key: str,
        content: bytes,
        *,
        trashed=False,
        parent_id: str | None = None,
    ):
        sha256, md5 = _checksums(content)
        return DriveFileMetadata(
            file_id=file_id,
            name="opaque-recording.wav",
            mime_type="audio/wav",
            size=len(content),
            sha256_checksum=sha256,
            md5_checksum=md5,
            trashed=trashed,
            parents=() if parent_id is None else (parent_id,),
            app_properties={
                "stt_schema": "stt-recording-v1",
                "stt_object": object_key,
                "stt_size": str(len(content)),
                "stt_sha256": sha256,
                "stt_md5": md5,
            },
        )

    def seed(
        self,
        object_key: str,
        content: bytes,
        *,
        file_id: str | None = None,
        parent_id: str | None = None,
    ):
        with self.lock:
            locator = file_id or f"privateDriveFile_{len(self.files_by_id) + 1}"
            metadata = self._metadata(
                locator,
                object_key,
                content,
                parent_id=parent_id or self.folder_id or self.folder_locator,
            )
            self.files_by_object[object_key] = (metadata, content)
            self.files_by_id[locator] = (metadata, content)
            return metadata

    def ensure_folder(
        self, *, object_key: str, name: str, parent_id: str | None = None
    ):
        self.folder_calls.append((object_key, name, parent_id))
        existing = self.folders_by_object.get(object_key)
        if existing is not None:
            return existing
        locator = (
            self.folder_locator
            if parent_id is None
            else f"privateUserFolder_{len(self.folders_by_id) + 1}"
        )
        metadata = DriveFileMetadata(
            file_id=locator,
            name=name,
            mime_type="application/vnd.google-apps.folder",
            size=None,
            md5_checksum=None,
            sha256_checksum=None,
            trashed=False,
            parents=() if parent_id is None else (parent_id,),
            app_properties={
                "stt_schema": "stt-archive-folder-v1",
                "stt_object": object_key,
            },
        )
        self.folders_by_object[object_key] = metadata
        self.folders_by_id[locator] = metadata
        return metadata

    def account_identity(self):
        return DriveAccountIdentity(
            permission_id=self.permission_id,
            oauth_client_fingerprint=self.oauth_client_fingerprint,
        )

    def verify_connection(self):
        return True

    def upload_recording(
        self,
        path: Path,
        *,
        object_key: str,
        name: str,
        parent_id: str | None,
        checkpoint: UploadCheckpoint | None,
        on_checkpoint,
    ):
        del name
        self.upload_calls += 1
        self.upload_parent_ids.append(parent_id)
        self.received_checkpoints.append(checkpoint)
        content = Path(path).read_bytes()
        sha256, md5 = _checksums(content)
        initial = checkpoint or UploadCheckpoint(
            session_uri=SESSION_URI,
            object_key=object_key,
            total_size=len(content),
            sha256_checksum=sha256,
            md5_checksum=md5,
            committed_bytes=0,
        )
        if checkpoint is None:
            on_checkpoint(initial)
            self.saved_checkpoints.append(initial)
        if self.fail_after_checkpoint:
            progressed = replace(initial, committed_bytes=max(1, len(content) // 2))
            on_checkpoint(progressed)
            self.saved_checkpoints.append(progressed)
        self.upload_entered.set()
        if self.block_upload and not self.upload_release.wait(timeout=5):
            raise AssertionError("fake upload was not released")
        if self.upload_error is not None:
            raise self.upload_error
        with self.lock:
            existing = self.files_by_object.get(object_key)
            if existing is not None:
                return existing[0]
            metadata = self.seed(object_key, content, parent_id=parent_id)
            self.created_count += 1
        completed = replace(initial, committed_bytes=len(content))
        on_checkpoint(completed)
        self.saved_checkpoints.append(completed)
        return metadata

    def reconcile_upload_session(self, checkpoint: UploadCheckpoint):
        self.reconciled_checkpoints.append(checkpoint)
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return self.reconcile_result

    def find_recording(
        self,
        object_key: str,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        expected_md5: str | None = None,
    ):
        del expected_size, expected_sha256, expected_md5
        self.find_calls.append(object_key)
        if self.find_error is not None:
            raise self.find_error
        with self.lock:
            value = self.files_by_object.get(object_key)
            return None if value is None else value[0]

    def get_metadata(self, file_id: str):
        with self.lock:
            value = self.files_by_id.get(file_id)
            if value is not None:
                return value[0]
            folder = self.folders_by_id.get(file_id)
            if folder is not None:
                return folder
            raise DriveNotFoundError("recording_unavailable", "missing")

    def move_file(
        self,
        file_id: str,
        *,
        previous_parent_id: str,
        new_parent_id: str,
    ):
        self.move_calls.append((file_id, previous_parent_id, new_parent_id))
        self.move_entered.set()
        if self.block_move and not self.move_release.wait(timeout=5):
            raise AssertionError("fake move was not released")
        if self.move_error is not None:
            raise self.move_error
        with self.lock:
            value = self.files_by_id.get(file_id)
            if value is None:
                raise DriveNotFoundError("recording_unavailable", "missing")
            metadata, content = value
            if metadata.parents != (previous_parent_id,):
                raise DriveIntegrityError("recording_parent_mismatch", "mismatch")
            moved = replace(metadata, parents=(new_parent_id,))
            self.files_by_id[file_id] = (moved, content)
            self.files_by_object[moved.app_properties["stt_object"]] = (moved, content)
            if self.move_error_after_commit is not None:
                raise self.move_error_after_commit
            return moved

    def open_download(self, file_id: str, *, start: int | None, end: int | None):
        with self.lock:
            value = self.files_by_id.get(file_id)
            if value is None or value[0].trashed:
                raise DriveNotFoundError("recording_unavailable", "missing")
            metadata, content = value
        self.download_calls.append((file_id, start, end))
        self.last_download = FakeDownload(content, start, end, metadata)
        return self.last_download

    def trash(self, file_id: str):
        self.trash_calls.append(file_id)
        self.trash_entered.set()
        if self.block_trash and not self.trash_release.wait(timeout=5):
            raise AssertionError("fake trash was not released")
        if self.trash_error is not None:
            raise self.trash_error
        with self.lock:
            metadata, content = self.files_by_id[file_id]
            trashed = replace(metadata, trashed=True)
            self.files_by_id[file_id] = (trashed, content)
            self.files_by_object[metadata.app_properties["stt_object"]] = (
                trashed,
                content,
            )
            return trashed

    def close(self):
        self.closed = True


class FakeTranscriber:
    def status(self):
        return {"model_state": "ready", "model": "fake", "device": "cpu"}

    def transcribe(self, samples, language, overlap_seconds=0, final_chunk=True):
        del samples, language, overlap_seconds, final_chunk
        return []


class FakeClova:
    configured = False

    def status(self):
        return {}

    def close_session(self, username, lecture_id):
        del username, lecture_id

    def close(self):
        return None


class DriveStreamingResponseTests(unittest.TestCase):
    def test_immediate_asgi_disconnect_closes_unstarted_drive_stream(self):
        class Upstream:
            def __init__(self):
                self.closed = False
                self.iterated = False

            def iter_bytes(self):
                self.iterated = True
                yield b"private audio"

            def close(self):
                self.closed = True

        upstream = Upstream()
        response = CloseableStreamingResponse(
            upstream.iter_bytes(), close=upstream.close
        )

        async def exercise_disconnect():
            async def receive():
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.start":
                    # Make the disconnect listener win before the synchronous
                    # iterator is advanced for the first time.
                    await asyncio.sleep(0.05)

            await response(
                {"type": "http", "asgi": {"spec_version": "2.3"}},
                receive,
                send,
            )

        asyncio.run(exercise_disconnect())

        self.assertFalse(upstream.iterated)
        self.assertTrue(upstream.closed)

    def test_external_asgi_cancellation_still_closes_drive_stream(self):
        class Upstream:
            def __init__(self):
                self.closed = False

            def iter_bytes(self):
                yield b"private audio"

            def close(self):
                self.closed = True

        upstream = Upstream()
        response = CloseableStreamingResponse(
            upstream.iter_bytes(), close=upstream.close
        )

        async def exercise_cancellation():
            with anyio.CancelScope() as cancellation:
                async def receive():
                    await anyio.sleep_forever()

                async def send(message):
                    if message["type"] == "http.response.body":
                        cancellation.cancel()
                        await anyio.sleep_forever()

                await response(
                    {"type": "http", "asgi": {"spec_version": "2.4"}},
                    receive,
                    send,
                )

        anyio.run(exercise_cancellation)

        self.assertTrue(upstream.closed)


class DriveArchiveManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings(
            data_dir=self.root / "data",
            model_cache_dir=self.root / "models",
            accounts=ACCOUNTS,
            google_drive_enabled=True,
            max_recordings_bytes=16 * 1024 * 1024,
            recording_free_reserve_bytes=0,
            max_import_seconds=60,
        )
        self.database = Database(self.settings.data_dir / "classroom.sqlite3", ACCOUNTS)
        self.database.initialize()
        self.store = RecordingStore(
            self.settings.data_dir / "recordings",
            ACCOUNTS,
            max_total_bytes=self.settings.max_recordings_bytes,
            min_free_bytes=0,
            max_seconds=60,
        )
        self.drive = FakeDrive()
        self.manager = DriveArchiveManager(
            self.settings,
            self.database,
            self.store,
            client=self.drive,
        )

    def tearDown(self):
        self.drive.move_release.set()
        self.manager.request_shutdown()
        self.manager.stop(timeout=1)
        self.temporary.cleanup()

    def add_finalized_recording(self, username="user-alpha") -> tuple[str, bytes]:
        lecture_id = str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO lectures"
                "(id, username, title, language, created_at, recording_finalized) "
                "VALUES (?, ?, 'private title', 'ko', '2026-09-05T00:00:00Z', 1)",
                (lecture_id, username),
            )
        frames = bytes(range(128)) * 4
        self.store.write_chunk(
            username,
            lecture_id,
            start_seconds=0,
            overlap_seconds=0,
            pcm=frames,
        )
        return lecture_id, self.store.path(username, lecture_id).read_bytes()

    def archive_row(self, lecture_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recording_archives WHERE lecture_id = ?", (lecture_id,)
            ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def test_existing_finalized_recording_uploads_verifies_commits_then_deletes_local(self):
        lecture_id, content = self.add_finalized_recording()

        self.assertEqual(
            self.manager.enqueue_existing(),
            {"enqueued_count": 1, "attention_count": 0},
        )
        result = self.manager.run_once(delete_local=True)

        self.assertEqual(
            result,
            {"migrated_count": 1, "deleted_local_count": 1, "failed_count": 0},
        )
        row = self.archive_row(lecture_id)
        sha256, md5 = _checksums(content)
        self.assertEqual(row["state"], "ready")
        self.assertEqual(row["source_bytes"], len(content))
        self.assertTrue(hmac.compare_digest(row["source_sha256"], sha256))
        self.assertTrue(hmac.compare_digest(row["source_md5"], md5))
        self.assertEqual(row["uploaded_bytes"], len(content))
        self.assertEqual(row["local_deleted"], 1)
        self.assertIsNone(row["upload_session_uri"])
        self.assertFalse(self.store.path("user-alpha", lecture_id).exists())
        self.assertEqual(
            self.manager.storage("user-alpha", lecture_id, True),
            {"recording_available": True, "recording_storage_state": "drive_ready"},
        )

    def test_each_account_uploads_to_a_distinct_runtime_named_private_folder(self):
        lectures: list[tuple[str, str]] = []
        for username in ACCOUNTS:
            lecture_id, _ = self.add_finalized_recording(username)
            lectures.append((username, lecture_id))
            with self.database.connect() as connection:
                self.assertTrue(self.manager.queue(connection, username, lecture_id))

        self.manager.run_once(delete_local=False)
        self.manager.run_once(delete_local=False)

        with self.database.connect() as connection:
            folders = {
                row["username"]: dict(row)
                for row in connection.execute(
                    "SELECT username, folder_key, folder_id "
                    "FROM drive_archive_user_folders"
                ).fetchall()
            }
        self.assertEqual(set(folders), set(ACCOUNTS))
        self.assertNotEqual(
            folders[ACCOUNTS[0]]["folder_id"], folders[ACCOUNTS[1]]["folder_id"]
        )
        for username, lecture_id in lectures:
            folder = folders[username]
            self.assertNotIn(username, folder["folder_key"])
            archive = self.archive_row(lecture_id)
            self.assertEqual(archive["folder_layout_version"], 1)
            remote = self.drive.get_metadata(archive["drive_file_id"])
            self.assertEqual(remote.parents, (folder["folder_id"],))
            self.assertNotIn(username, remote.name)
            self.assertNotIn(username, remote.app_properties.values())
        child_calls = [call for call in self.drive.folder_calls if call[2] is not None]
        self.assertEqual({call[1] for call in child_calls}, set(ACCOUNTS))
        self.assertTrue(all(call[2] == self.drive.folder_locator for call in child_calls))

    def test_legacy_root_pilot_moves_before_its_local_copy_is_deleted(self):
        lecture_id, content = self.add_finalized_recording()
        with self.database.connect() as connection:
            self.manager.queue(connection, "user-alpha", lecture_id)
        with self.manager._exclusive_operation():
            root_folder_id = self.manager._ensure_folder()
        row = self.archive_row(lecture_id)
        remote = self.drive.seed(
            row["object_key"],
            content,
            file_id="legacyPilot_1",
            parent_id=root_folder_id,
        )
        sha256, md5 = _checksums(content)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET state = 'ready', drive_file_id = ?, "
                "source_bytes = ?, source_sha256 = ?, source_md5 = ?, uploaded_bytes = ?, "
                "local_deleted = 0, folder_layout_version = 0 WHERE lecture_id = ?",
                (remote.file_id, len(content), sha256, md5, len(content), lecture_id),
            )

        result = self.manager.run_once(delete_local=True)

        updated = self.archive_row(lecture_id)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(result["deleted_local_count"], 1)
        self.assertEqual(self.drive.upload_calls, 0)
        self.assertEqual(len(self.drive.move_calls), 1)
        self.assertEqual(updated["folder_layout_version"], 1)
        self.assertEqual(updated["local_deleted"], 1)
        self.assertFalse(self.store.path("user-alpha", lecture_id).exists())
        with self.database.connect() as connection:
            user_folder_id = connection.execute(
                "SELECT folder_id FROM drive_archive_user_folders WHERE username = ?",
                ("user-alpha",),
            ).fetchone()[0]
        self.assertEqual(self.drive.get_metadata(remote.file_id).parents, (user_folder_id,))

    def test_folder_move_failure_keeps_ready_locator_and_local_recording(self):
        lecture_id, content = self.add_finalized_recording()
        with self.database.connect() as connection:
            self.manager.queue(connection, "user-alpha", lecture_id)
        with self.manager._exclusive_operation():
            root_folder_id = self.manager._ensure_folder()
        row = self.archive_row(lecture_id)
        remote = self.drive.seed(
            row["object_key"], content, file_id="legacyMoveRetry_1", parent_id=root_folder_id
        )
        sha256, md5 = _checksums(content)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET state = 'ready', drive_file_id = ?, "
                "source_bytes = ?, source_sha256 = ?, source_md5 = ?, uploaded_bytes = ? "
                "WHERE lecture_id = ?",
                (remote.file_id, len(content), sha256, md5, len(content), lecture_id),
            )
        self.drive.move_error = DriveTransportError(
            "drive_temporarily_unavailable", "redacted", retryable=True
        )

        failed = self.manager.run_once(delete_local=True)

        unchanged = self.archive_row(lecture_id)
        self.assertEqual(failed["failed_count"], 1)
        self.assertEqual(unchanged["state"], "ready")
        self.assertEqual(unchanged["drive_file_id"], remote.file_id)
        self.assertEqual(unchanged["folder_layout_version"], 0)
        self.assertEqual(unchanged["local_deleted"], 0)
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())

        self.drive.move_error = None
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET next_attempt_at = 0 WHERE lecture_id = ?",
                (lecture_id,),
            )
        resumed = self.manager.run_once(delete_local=True)
        self.assertEqual(resumed["failed_count"], 0)
        self.assertEqual(self.archive_row(lecture_id)["folder_layout_version"], 1)
        self.assertFalse(self.store.path("user-alpha", lecture_id).exists())

    def test_remote_only_legacy_recording_is_still_organized(self):
        lecture_id, content = self.add_finalized_recording("user-beta")
        with self.database.connect() as connection:
            self.manager.queue(connection, "user-beta", lecture_id)
        with self.manager._exclusive_operation():
            root_folder_id = self.manager._ensure_folder()
        row = self.archive_row(lecture_id)
        remote = self.drive.seed(
            row["object_key"], content, file_id="legacyRemoteOnly_1", parent_id=root_folder_id
        )
        sha256, md5 = _checksums(content)
        self.store.delete("user-beta", lecture_id)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET state = 'ready', drive_file_id = ?, "
                "source_bytes = ?, source_sha256 = ?, source_md5 = ?, uploaded_bytes = ?, "
                "local_deleted = 1 WHERE lecture_id = ?",
                (remote.file_id, len(content), sha256, md5, len(content), lecture_id),
            )

        result = self.manager.run_once(delete_local=True)

        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(result["deleted_local_count"], 0)
        self.assertEqual(self.archive_row(lecture_id)["folder_layout_version"], 1)
        self.assertEqual(len(self.drive.move_calls), 1)

    def test_lost_move_response_reconciles_without_a_second_move(self):
        lecture_id, content = self.add_finalized_recording()
        with self.database.connect() as connection:
            self.manager.queue(connection, "user-alpha", lecture_id)
        with self.manager._exclusive_operation():
            root_folder_id = self.manager._ensure_folder()
        row = self.archive_row(lecture_id)
        remote = self.drive.seed(
            row["object_key"], content, file_id="lostMoveResponse_1", parent_id=root_folder_id
        )
        sha256, md5 = _checksums(content)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET state = 'ready', drive_file_id = ?, "
                "source_bytes = ?, source_sha256 = ?, source_md5 = ?, uploaded_bytes = ? "
                "WHERE lecture_id = ?",
                (remote.file_id, len(content), sha256, md5, len(content), lecture_id),
            )
        self.drive.move_error_after_commit = DriveTransportError(
            "drive_response_interrupted", "redacted", retryable=True
        )

        failed = self.manager.run_once(delete_local=True)

        self.assertEqual(failed["failed_count"], 1)
        self.assertEqual(self.archive_row(lecture_id)["folder_layout_version"], 0)
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())
        self.assertEqual(len(self.drive.move_calls), 1)

        self.drive.move_error_after_commit = None
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET next_attempt_at = 0 WHERE lecture_id = ?",
                (lecture_id,),
            )
        resumed = self.manager.run_once(delete_local=True)

        self.assertEqual(resumed["failed_count"], 0)
        self.assertEqual(self.archive_row(lecture_id)["folder_layout_version"], 1)
        self.assertEqual(len(self.drive.move_calls), 1)
        self.assertFalse(self.store.path("user-alpha", lecture_id).exists())

    def test_folder_organization_retry_attempts_back_off_monotonically(self):
        lecture_id, content = self.add_finalized_recording()
        with self.database.connect() as connection:
            self.manager.queue(connection, "user-alpha", lecture_id)
        with self.manager._exclusive_operation():
            root_folder_id = self.manager._ensure_folder()
        row = self.archive_row(lecture_id)
        remote = self.drive.seed(
            row["object_key"],
            content,
            file_id="layoutBackoff_1",
            parent_id=root_folder_id,
        )
        sha256, md5 = _checksums(content)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET state = 'ready', drive_file_id = ?, "
                "source_bytes = ?, source_sha256 = ?, source_md5 = ?, uploaded_bytes = ? "
                "WHERE lecture_id = ?",
                (remote.file_id, len(content), sha256, md5, len(content), lecture_id),
            )
        self.drive.move_error = DriveTransportError(
            "drive_temporarily_unavailable", "redacted", retryable=True
        )

        with mock.patch(
            "server.drive_archive._retry_delay",
            side_effect=lambda attempts, _maximum: attempts * 10,
        ) as delay:
            first_started = time.time()
            first = self.manager.run_once(delete_local=True)
            first_row = self.archive_row(lecture_id)
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE recording_archives SET next_attempt_at = 0 WHERE lecture_id = ?",
                    (lecture_id,),
                )
            second_started = time.time()
            second = self.manager.run_once(delete_local=True)
            second_row = self.archive_row(lecture_id)

        self.assertEqual(first["failed_count"], 1)
        self.assertEqual(second["failed_count"], 1)
        self.assertEqual(first_row["attempts"], 1)
        self.assertGreaterEqual(first_row["next_attempt_at"], first_started + 10)
        self.assertEqual(second_row["attempts"], 2)
        self.assertGreaterEqual(second_row["next_attempt_at"], second_started + 20)
        self.assertEqual(
            delay.call_args_list,
            [
                mock.call(1, self.settings.google_drive_retry_max_seconds),
                mock.call(2, self.settings.google_drive_retry_max_seconds),
            ],
        )
        self.assertEqual(second_row["state"], "ready")
        self.assertEqual(second_row["folder_layout_version"], 0)
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())

    def test_stale_concurrent_organizer_is_a_safe_noop(self):
        lecture_id, content = self.add_finalized_recording()
        with self.database.connect() as connection:
            self.manager.queue(connection, "user-alpha", lecture_id)
        with self.manager._exclusive_operation():
            root_folder_id = self.manager._ensure_folder()
        row = self.archive_row(lecture_id)
        remote = self.drive.seed(
            row["object_key"],
            content,
            file_id="concurrentLayout_1",
            parent_id=root_folder_id,
        )
        sha256, md5 = _checksums(content)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET state = 'ready', drive_file_id = ?, "
                "source_bytes = ?, source_sha256 = ?, source_md5 = ?, uploaded_bytes = ? "
                "WHERE lecture_id = ?",
                (remote.file_id, len(content), sha256, md5, len(content), lecture_id),
            )
        second = DriveArchiveManager(
            self.settings,
            self.database,
            self.store,
            client=self.drive,
        )
        self.drive.block_move = True
        second_waiting_for_lock = threading.Event()
        real_second_operation = second._exclusive_operation

        @contextmanager
        def announced_second_operation():
            second_waiting_for_lock.set()
            with real_second_operation():
                yield

        try:
            with mock.patch.object(
                second,
                "_exclusive_operation",
                side_effect=announced_second_operation,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first_result = executor.submit(
                        self.manager.run_once, delete_local=False
                    )
                    self.assertTrue(self.drive.move_entered.wait(timeout=2))
                    stale_result = executor.submit(second.run_once, delete_local=False)
                    self.assertTrue(second_waiting_for_lock.wait(timeout=2))
                    self.drive.move_release.set()
                    first = first_result.result(timeout=3)
                    stale = stale_result.result(timeout=3)
        finally:
            self.drive.move_release.set()
            second.request_shutdown()
            second.stop(timeout=1)

        final = self.archive_row(lecture_id)
        self.assertEqual(first["failed_count"], 0)
        self.assertEqual(stale["failed_count"], 0)
        self.assertEqual(len(self.drive.move_calls), 1)
        self.assertEqual(final["state"], "ready")
        self.assertEqual(final["folder_layout_version"], 1)
        self.assertEqual(final["attempts"], 1)
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())

    def test_folder_organization_lock_oserror_is_deferred_and_redacted(self):
        lecture_id, content = self.add_finalized_recording()
        with self.database.connect() as connection:
            self.manager.queue(connection, "user-alpha", lecture_id)
        row = self.archive_row(lecture_id)
        sha256, md5 = _checksums(content)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET state = 'ready', "
                "drive_file_id = 'lockFailureFile_1', source_bytes = ?, "
                "source_sha256 = ?, source_md5 = ?, uploaded_bytes = ? "
                "WHERE lecture_id = ?",
                (len(content), sha256, md5, len(content), lecture_id),
            )
        started = time.time()

        with mock.patch.object(
            self.manager,
            "_exclusive_operation",
            side_effect=OSError("private lock path"),
        ):
            result = self.manager.run_once(delete_local=True)

        failed = self.archive_row(lecture_id)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(failed["state"], "ready")
        self.assertEqual(failed["attempts"], 1)
        self.assertEqual(failed["last_error_code"], "local_storage_unavailable")
        self.assertGreater(failed["next_attempt_at"], started)
        self.assertEqual(failed["folder_layout_version"], 0)
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())

    def test_structural_folder_organization_failure_requires_attention(self):
        lecture_id, content = self.add_finalized_recording()
        with self.database.connect() as connection:
            self.manager.queue(connection, "user-alpha", lecture_id)
        sha256, md5 = _checksums(content)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET state = 'ready', "
                "drive_file_id = 'unsafeLockFile_1', source_bytes = ?, "
                "source_sha256 = ?, source_md5 = ?, uploaded_bytes = ? "
                "WHERE lecture_id = ?",
                (len(content), sha256, md5, len(content), lecture_id),
            )

        with mock.patch.object(
            self.manager,
            "_exclusive_operation",
            side_effect=RuntimeError("private structural detail"),
        ):
            result = self.manager.run_once(delete_local=True)

        failed = self.archive_row(lecture_id)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(failed["state"], "attention")
        self.assertEqual(failed["attempts"], 1)
        self.assertEqual(failed["last_error_code"], "archive_internal_error")
        self.assertEqual(failed["next_attempt_at"], 0)
        self.assertEqual(failed["folder_layout_version"], 0)
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())

    def test_retryable_failure_preserves_local_and_checkpoint_then_resumes(self):
        lecture_id, content = self.add_finalized_recording()
        self.manager.enqueue_existing()
        self.drive.fail_after_checkpoint = True
        self.drive.upload_error = DriveTransportError(
            "drive_temporarily_unavailable", "redacted", retryable=True
        )

        failed = self.manager.run_once(delete_local=True)

        self.assertEqual(failed["failed_count"], 1)
        row = self.archive_row(lecture_id)
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["last_error_code"], "drive_temporarily_unavailable")
        self.assertGreater(row["uploaded_bytes"], 0)
        self.assertLess(row["uploaded_bytes"], len(content))
        self.assertEqual(row["upload_session_uri"], SESSION_URI)
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET next_attempt_at = 0 WHERE lecture_id = ?",
                (lecture_id,),
            )
        self.drive.fail_after_checkpoint = False
        self.drive.upload_error = None
        resumed = self.manager.run_once(delete_local=True)

        self.assertEqual(resumed["migrated_count"], 1)
        self.assertIsNotNone(self.drive.received_checkpoints[-1])
        self.assertGreater(self.drive.received_checkpoints[-1].committed_bytes, 0)
        self.assertFalse(self.store.path("user-alpha", lecture_id).exists())

    def test_nonretryable_failure_requires_attention_and_keeps_source(self):
        lecture_id, _ = self.add_finalized_recording()
        self.manager.enqueue_existing()
        self.drive.upload_error = DriveIntegrityError(
            "remote_integrity_mismatch", "redacted", retryable=False
        )

        self.manager.run_once(delete_local=True)

        row = self.archive_row(lecture_id)
        self.assertEqual(row["state"], "attention")
        self.assertEqual(row["last_error_code"], "remote_integrity_mismatch")
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())
        self.assertEqual(
            self.manager.storage("user-alpha", lecture_id, True)[
                "recording_storage_state"
            ],
            "attention_required",
        )

    def test_recover_changes_uploading_to_pending_and_resumes_saved_checkpoint(self):
        lecture_id, content = self.add_finalized_recording()
        self.manager.enqueue_existing()
        sha256, md5 = _checksums(content)
        checkpoint = UploadCheckpoint(
            session_uri=SESSION_URI,
            object_key=self.archive_row(lecture_id)["object_key"],
            total_size=len(content),
            sha256_checksum=sha256,
            md5_checksum=md5,
            committed_bytes=64,
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET state = 'uploading', "
                "upload_session_uri = ?, source_bytes = ?, source_sha256 = ?, "
                "source_md5 = ?, uploaded_bytes = ? WHERE lecture_id = ?",
                (
                    checkpoint.session_uri,
                    checkpoint.total_size,
                    checkpoint.sha256_checksum,
                    checkpoint.md5_checksum,
                    checkpoint.committed_bytes,
                    lecture_id,
                ),
            )

        self.manager.recover(enqueue_existing=False)

        recovered = self.archive_row(lecture_id)
        self.assertEqual(recovered["state"], "pending")
        self.assertEqual(recovered["last_error_code"], "server_restarted")
        self.manager.run_once(delete_local=False)
        self.assertEqual(self.drive.received_checkpoints[-1], checkpoint)
        self.assertEqual(self.archive_row(lecture_id)["state"], "ready")

    def test_recover_retries_authorization_attention_but_not_integrity_attention(self):
        authorization_id, _ = self.add_finalized_recording()
        integrity_id, _ = self.add_finalized_recording()
        self.manager.enqueue_existing()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET state = 'attention', "
                "last_error_code = 'token_refresh_rejected' WHERE lecture_id = ?",
                (authorization_id,),
            )
            connection.execute(
                "UPDATE recording_archives SET state = 'attention', "
                "last_error_code = 'remote_integrity_mismatch' WHERE lecture_id = ?",
                (integrity_id,),
            )

        self.manager.recover(enqueue_existing=False)

        self.assertEqual(self.archive_row(authorization_id)["state"], "pending")
        self.assertEqual(self.archive_row(integrity_id)["state"], "attention")

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET state = 'attention', "
                "last_error_code = 'token_refresh_invalid' WHERE lecture_id = ?",
                (authorization_id,),
            )
        self.manager.recover(enqueue_existing=False)
        self.assertEqual(self.archive_row(authorization_id)["state"], "pending")

    def test_stable_object_identity_prevents_duplicate_queue_and_remote_creation(self):
        lecture_id, content = self.add_finalized_recording()
        with self.database.connect() as connection:
            self.assertTrue(self.manager.queue(connection, "user-alpha", lecture_id))
        first_key = self.archive_row(lecture_id)["object_key"]
        self.drive.seed(first_key, content, file_id="responseLostFile_1")
        with self.database.connect() as connection:
            self.assertFalse(self.manager.queue(connection, "user-alpha", lecture_id))

        self.manager.run_once(delete_local=True)
        self.manager.recover()

        self.assertEqual(self.drive.created_count, 0)
        self.assertEqual(len(self.drive.files_by_object), 1)
        self.assertEqual(self.archive_row(lecture_id)["drive_file_id"], "responseLostFile_1")
        self.assertEqual(self.manager._object_key("user-alpha", lecture_id), first_key)
        self.assertNotIn("user-alpha", first_key)

    def test_first_queued_recording_can_be_deleted_before_drive_binding_exists(self):
        lecture_id, _ = self.add_finalized_recording()
        self.manager.enqueue_existing()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE lectures SET deleting = 1 WHERE id = ?", (lecture_id,)
            )

        with mock.patch.object(
            self.drive,
            "account_identity",
            side_effect=AssertionError("Unstarted deletion must not contact Drive"),
        ):
            result = self.manager.run_once()

        self.assertEqual(result["failed_count"], 0)
        self.assertFalse(self.store.path("user-alpha", lecture_id).exists())
        with self.database.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM lectures WHERE id = ?", (lecture_id,)
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM recording_archives WHERE lecture_id = ?",
                    (lecture_id,),
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute("SELECT 1 FROM drive_archive_binding").fetchone()
            )
        self.assertEqual(self.drive.folder_calls, [])
        self.assertEqual(self.drive.upload_calls, 0)
        self.assertEqual(self.drive.find_calls, [])
        self.assertEqual(self.drive.trash_calls, [])

    def test_missing_binding_cannot_erase_a_recording_after_an_upload_claim(self):
        lecture_id, _ = self.add_finalized_recording()
        self.manager.enqueue_existing()
        claimed = self.manager._claim_pending()
        self.assertEqual(claimed["lecture_id"], lecture_id)
        self.manager.recover(enqueue_existing=False)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE lectures SET deleting = 1 WHERE id = ?", (lecture_id,)
            )

        result = self.manager.run_once()

        self.assertEqual(result["failed_count"], 1)
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())
        self.assertEqual(self.archive_row(lecture_id)["attempts"], 1)
        self.assertEqual(self.drive.trash_calls, [])

    def test_missing_binding_preserves_locators_and_response_lost_upload_evidence(self):
        # Each case starts with a pending entry so a missing/partial durable
        # field cannot be mistaken for the pristine first-upload exception.
        evidence_cases = (
            {"drive_file_id": "responseLostFile_1"},
            {"upload_session_uri": SESSION_URI},
            {"source_bytes": 556},
            {"source_sha256": "a" * 64},
            {"source_md5": "b" * 32},
            {"uploaded_bytes": 1},
            {"local_deleted": 1},
            {"folder_layout_version": 1},
            {"last_error_code": "server_restarted"},
            {"state": "uploading"},
            {"state": "attention"},
        )
        for evidence in evidence_cases:
            with self.subTest(evidence=tuple(evidence)):
                lecture_id, _ = self.add_finalized_recording()
                self.manager.enqueue_existing()
                with self.database.connect() as connection:
                    assignments = ", ".join(f"{field} = ?" for field in evidence)
                    connection.execute(
                        f"UPDATE recording_archives SET {assignments} WHERE lecture_id = ?",
                        (*evidence.values(), lecture_id),
                    )
                    connection.execute(
                        "UPDATE lectures SET deleting = 1 WHERE id = ?", (lecture_id,)
                    )

                self.assertFalse(self.manager.trash_for_deletion(lecture_id))
                unchanged = self.archive_row(lecture_id)
                for field, value in evidence.items():
                    self.assertEqual(unchanged[field], value)
                self.assertTrue(self.store.path("user-alpha", lecture_id).exists())
        self.assertEqual(self.drive.find_calls, [])
        self.assertEqual(self.drive.trash_calls, [])

    def test_deletion_finds_and_trashes_response_lost_remote_before_db_commit(self):
        lecture_id, content = self.add_finalized_recording()
        with self.manager._exclusive_operation():
            self.manager._ensure_folder()
        self.manager.enqueue_existing()
        row = self.archive_row(lecture_id)
        remote = self.drive.seed(
            row["object_key"], content, file_id="uploadResponseLostFile_1"
        )

        self.assertTrue(self.manager.trash_for_deletion(lecture_id))

        self.assertEqual(self.drive.trash_calls, [remote.file_id])
        self.assertTrue(self.drive.get_metadata(remote.file_id).trashed)
        unchanged = self.archive_row(lecture_id)
        self.assertEqual(unchanged["state"], "pending")
        self.assertIsNone(unchanged["drive_file_id"])
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())

    def test_deletion_keeps_tracking_until_saved_upload_session_is_resolved(self):
        lecture_id, content = self.add_finalized_recording()
        with self.manager._exclusive_operation():
            self.manager._ensure_folder()
        self.manager.enqueue_existing()
        row = self.archive_row(lecture_id)
        sha256, md5 = _checksums(content)
        checkpoint = UploadCheckpoint(
            session_uri=SESSION_URI,
            object_key=row["object_key"],
            total_size=len(content),
            sha256_checksum=sha256,
            md5_checksum=md5,
            committed_bytes=max(1, len(content) // 2),
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET upload_session_uri = ?, "
                "source_bytes = ?, source_sha256 = ?, source_md5 = ?, "
                "uploaded_bytes = ? WHERE lecture_id = ?",
                (
                    checkpoint.session_uri,
                    checkpoint.total_size,
                    checkpoint.sha256_checksum,
                    checkpoint.md5_checksum,
                    checkpoint.committed_bytes,
                    lecture_id,
                ),
            )

        # A 308-like unresolved result can still turn into a Drive file after
        # the client lost the final PUT response, so deletion must fail closed.
        self.assertFalse(self.manager.trash_for_deletion(lecture_id))
        self.assertEqual(self.drive.reconciled_checkpoints, [checkpoint])
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())
        deferred = self.archive_row(lecture_id)
        self.assertEqual(deferred["last_error_code"], "upload_session_incomplete")
        self.assertGreater(deferred["next_attempt_at"], time.time() + 7 * 24 * 60 * 60)

        restarted = DriveArchiveManager(
            self.settings, self.database, self.store, client=self.drive
        )
        self.assertFalse(restarted.trash_for_deletion(lecture_id))
        self.assertEqual(self.drive.reconciled_checkpoints, [checkpoint])

        remote = self.drive.seed(
            row["object_key"], content, file_id="lateCompletedUpload_1"
        )
        self.drive.reconcile_result = remote

        self.assertTrue(restarted.trash_for_deletion(lecture_id))
        self.assertEqual(self.drive.trash_calls, [remote.file_id])
        self.assertTrue(self.drive.get_metadata(remote.file_id).trashed)

    def test_expired_saved_session_requires_exact_remote_search_before_deletion(self):
        lecture_id, content = self.add_finalized_recording()
        with self.manager._exclusive_operation():
            self.manager._ensure_folder()
        self.manager.enqueue_existing()
        row = self.archive_row(lecture_id)
        sha256, md5 = _checksums(content)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET upload_session_uri = ?, "
                "source_bytes = ?, source_sha256 = ?, source_md5 = ?, "
                "uploaded_bytes = ? WHERE lecture_id = ?",
                (SESSION_URI, len(content), sha256, md5, len(content) // 2, lecture_id),
            )
        self.drive.reconcile_error = DriveUploadSessionExpired(
            "upload_session_expired", "redacted", retryable=True
        )
        late = self.drive.seed(row["object_key"], content, file_id="expiredSessionFile_1")

        self.assertTrue(self.manager.trash_for_deletion(lecture_id))

        self.assertEqual(self.drive.trash_calls, [late.file_id])
        self.assertTrue(self.drive.get_metadata(late.file_id).trashed)

    def test_expired_saved_session_with_no_remote_can_finish_deletion(self):
        lecture_id, content = self.add_finalized_recording()
        with self.manager._exclusive_operation():
            self.manager._ensure_folder()
        self.manager.enqueue_existing()
        row = self.archive_row(lecture_id)
        sha256, md5 = _checksums(content)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET upload_session_uri = ?, "
                "source_bytes = ?, source_sha256 = ?, source_md5 = ?, "
                "uploaded_bytes = ? WHERE lecture_id = ?",
                (SESSION_URI, len(content), sha256, md5, len(content) // 2, lecture_id),
            )
        self.drive.reconcile_error = DriveUploadSessionExpired(
            "upload_session_expired", "redacted", retryable=True
        )

        self.assertTrue(self.manager.trash_for_deletion(lecture_id))

        self.assertEqual(self.drive.trash_calls, [])
        self.assertEqual(self.drive.reconciled_checkpoints[0].object_key, row["object_key"])
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())

    def test_locatorless_search_404_never_authorizes_deletion(self):
        lecture_id, _ = self.add_finalized_recording()
        with self.manager._exclusive_operation():
            self.manager._ensure_folder()
        self.manager.enqueue_existing()
        self.drive.find_error = DriveNotFoundError(
            "recording_unavailable", "redacted"
        )

        self.assertFalse(self.manager.trash_for_deletion(lecture_id))

        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())
        self.assertIsNotNone(self.archive_row(lecture_id))

    def test_expired_session_search_404_never_authorizes_deletion(self):
        lecture_id, content = self.add_finalized_recording()
        with self.manager._exclusive_operation():
            self.manager._ensure_folder()
        self.manager.enqueue_existing()
        row = self.archive_row(lecture_id)
        sha256, md5 = _checksums(content)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET upload_session_uri = ?, "
                "source_bytes = ?, source_sha256 = ?, source_md5 = ?, "
                "uploaded_bytes = ? WHERE lecture_id = ?",
                (SESSION_URI, len(content), sha256, md5, len(content) // 2, lecture_id),
            )
        self.drive.reconcile_error = DriveUploadSessionExpired(
            "upload_session_expired", "redacted", retryable=True
        )
        self.drive.find_error = DriveNotFoundError(
            "recording_unavailable", "redacted"
        )

        self.assertFalse(self.manager.trash_for_deletion(lecture_id))

        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())
        self.assertIsNotNone(self.archive_row(lecture_id))

    def test_trash_404_never_authorizes_locatorless_deletion(self):
        lecture_id, content = self.add_finalized_recording()
        with self.manager._exclusive_operation():
            self.manager._ensure_folder()
        self.manager.enqueue_existing()
        row = self.archive_row(lecture_id)
        self.drive.seed(row["object_key"], content, file_id="searchThenMissing_1")
        self.drive.trash_error = DriveNotFoundError(
            "recording_unavailable", "redacted"
        )

        self.assertFalse(self.manager.trash_for_deletion(lecture_id))

        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())
        self.assertIsNotNone(self.archive_row(lecture_id))

    def test_tampered_remote_is_neither_downloaded_nor_trashed_by_locator(self):
        lecture_id, _ = self.add_finalized_recording()
        self.manager.enqueue_existing()
        self.manager.run_once(delete_local=False)
        row = self.archive_row(lecture_id)
        metadata, content = self.drive.files_by_id[row["drive_file_id"]]
        tampered = replace(metadata, md5_checksum="0" * 32)
        self.drive.files_by_id[metadata.file_id] = (tampered, content)
        self.drive.files_by_object[row["object_key"]] = (tampered, content)

        with self.assertRaises(DriveIntegrityError):
            self.manager.open_download(lecture_id, start=None, end=None)
        self.assertIsNotNone(self.drive.last_download)
        self.assertTrue(self.drive.last_download.closed)
        self.assertFalse(self.manager.trash_for_deletion(lecture_id))
        self.assertEqual(self.drive.trash_calls, [])

    def test_cleanup_rechecks_remote_and_does_not_starve_later_uploads(self):
        first_id, _ = self.add_finalized_recording()
        second_id, _ = self.add_finalized_recording()
        self.manager.enqueue_existing()
        uploaded = self.manager.run_once(delete_local=False)
        self.assertEqual(uploaded["migrated_count"], 1)
        rows = {lecture_id: self.archive_row(lecture_id) for lecture_id in (first_id, second_id)}
        ready_id = next(lecture_id for lecture_id, row in rows.items() if row["state"] == "ready")
        pending_id = next(
            lecture_id for lecture_id, row in rows.items() if row["state"] == "pending"
        )
        ready = rows[ready_id]
        metadata, content = self.drive.files_by_id[ready["drive_file_id"]]
        tampered = replace(metadata, trashed=True)
        self.drive.files_by_id[metadata.file_id] = (tampered, content)
        self.drive.files_by_object[ready["object_key"]] = (tampered, content)

        failed_cleanup = self.manager.run_once(delete_local=True)

        self.assertEqual(failed_cleanup["failed_count"], 1)
        self.assertEqual(self.archive_row(ready_id)["state"], "attention")
        self.assertTrue(self.store.path("user-alpha", ready_id).exists())

        next_upload = self.manager.run_once(delete_local=False)
        self.assertEqual(next_upload["migrated_count"], 1)
        self.assertEqual(self.archive_row(pending_id)["state"], "ready")

    def test_changed_google_account_fails_closed_for_existing_archive(self):
        lecture_id, _ = self.add_finalized_recording()
        self.manager.enqueue_existing()
        self.manager.run_once(delete_local=False)

        self.drive.permission_id = "differentGoogleUser_2"

        self.assertFalse(self.manager.trash_for_deletion(lecture_id))
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())
        self.assertIsNotNone(self.archive_row(lecture_id)["drive_file_id"])

    def test_known_remote_404_never_authorizes_local_or_database_erasure(self):
        lecture_id, _ = self.add_finalized_recording()
        self.manager.enqueue_existing()
        self.manager.run_once(delete_local=False)
        row = self.archive_row(lecture_id)
        self.drive.files_by_id.pop(row["drive_file_id"])
        self.drive.files_by_object.pop(row["object_key"])

        self.assertFalse(self.manager.trash_for_deletion(lecture_id))

        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())
        with self.database.connect() as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM lectures WHERE id = ?", (lecture_id,)
                ).fetchone()
            )

    def test_corrected_google_account_restores_ready_cleanup_after_restart_recovery(self):
        lecture_id, _ = self.add_finalized_recording()
        self.manager.enqueue_existing()
        self.manager.run_once(delete_local=False)
        original_permission = self.drive.permission_id
        self.drive.permission_id = "differentGoogleUser_2"

        failed = self.manager.run_once(delete_local=True)

        self.assertEqual(failed["failed_count"], 1)
        self.assertEqual(self.archive_row(lecture_id)["state"], "attention")
        self.assertTrue(self.store.path("user-alpha", lecture_id).exists())

        self.drive.permission_id = original_permission
        self.manager.recover(enqueue_existing=False)
        self.assertEqual(self.archive_row(lecture_id)["state"], "ready")
        cleaned = self.manager.run_once(delete_local=True)
        self.assertEqual(cleaned["deleted_local_count"], 1)
        self.assertFalse(self.store.path("user-alpha", lecture_id).exists())

    def test_worker_retries_a_hidden_deletion_after_drive_recovers(self):
        lecture_id, _ = self.add_finalized_recording()
        self.manager.enqueue_existing()
        self.manager.run_once(delete_local=False)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE lectures SET deleting = 1 WHERE id = ?", (lecture_id,)
            )
        self.drive.trash_error = DriveTransportError(
            "trash_temporarily_unavailable", "redacted", retryable=True
        )

        with mock.patch("server.drive_archive._retry_delay", return_value=0.05):
            self.manager.start()
            deadline = time.monotonic() + 3
            while not self.drive.trash_calls and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.drive.trash_calls)
            self.drive.trash_error = None
            self.manager.wake()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with self.database.connect() as connection:
                    remaining = connection.execute(
                        "SELECT 1 FROM lectures WHERE id = ?", (lecture_id,)
                    ).fetchone()
                if remaining is None:
                    break
                time.sleep(0.02)

        with self.database.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM lectures WHERE id = ?", (lecture_id,)
                ).fetchone()
            )
        self.assertFalse(self.store.path("user-alpha", lecture_id).exists())


class DriveArchiveApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings(
            data_dir=self.root / "data",
            model_cache_dir=self.root / "models",
            accounts=ACCOUNTS,
            site_origins=("https://student.github.io",),
            google_drive_enabled=True,
            max_recordings_bytes=16 * 1024 * 1024,
            recording_free_reserve_bytes=0,
            max_import_seconds=60,
        )
        self.drive = FakeDrive()
        self.app = create_app(
            self.settings,
            FakeTranscriber(),
            clova_transcriber=FakeClova(),
            drive_storage=self.drive,
        )
        self.client = TestClient(self.app)
        self.database = self.app.state.database
        self.tokens = {
            "user-alpha": "drive-test-session-alpha",
            "user-beta": "drive-test-session-beta",
        }
        with self.database.connect() as connection:
            for username, token in self.tokens.items():
                connection.execute(
                    "INSERT INTO sessions(token_hash, username, expires_at, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (digest(token), username, time.time() + 3600, time.time()),
                )

    def tearDown(self):
        self.drive.upload_release.set()
        self.drive.trash_release.set()
        self.app.state.archive_manager.request_shutdown()
        self.app.state.archive_manager.stop(timeout=1)
        self.client.close()
        self.temporary.cleanup()

    def headers(self, username="user-alpha"):
        return {"Authorization": f"Bearer {self.tokens[username]}"}

    def add_remote_recording(self, username="user-alpha") -> tuple[str, bytes, str]:
        lecture_id = str(uuid.uuid4())
        content = b"RIFF" + bytes(range(251)) * 3
        with self.app.state.archive_manager._exclusive_operation():
            root_folder_id = self.app.state.archive_manager._ensure_folder()
            user_folder_id = self.app.state.archive_manager._ensure_user_folder(
                username, root_folder_id=root_folder_id
            )
        object_key = self.app.state.archive_manager._object_key(username, lecture_id)
        metadata = self.drive.seed(object_key, content, parent_id=user_folder_id)
        sha256, md5 = _checksums(content)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO lectures"
                "(id, username, title, language, created_at, recording_finalized) "
                "VALUES (?, ?, 'private remote title', 'ko', '2026-09-05T00:00:00Z', 1)",
                (lecture_id, username),
            )
            connection.execute(
                "INSERT INTO recording_archives"
                "(lecture_id, state, object_key, drive_file_id, source_bytes, "
                "source_sha256, source_md5, uploaded_bytes, local_deleted, "
                "folder_layout_version, updated_at) "
                "VALUES (?, 'ready', ?, ?, ?, ?, ?, ?, 1, 1, '2026-09-05T00:01:00Z')",
                (
                    lecture_id,
                    object_key,
                    metadata.file_id,
                    len(content),
                    sha256,
                    md5,
                    len(content),
                ),
            )
        return lecture_id, content, metadata.file_id

    def test_remote_range_ticket_is_byte_exact_owner_only_and_hides_locator(self):
        lecture_id, content, file_id = self.add_remote_recording()

        other = self.client.post(
            f"/lectures/{lecture_id}/recording-download-ticket",
            headers=self.headers("user-beta"),
        )
        self.assertEqual(other.status_code, 404)
        detail = self.client.get(
            f"/lectures/{lecture_id}", headers=self.headers()
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        serialized_detail = detail.text
        self.assertNotIn(file_id, serialized_detail)
        self.assertNotIn("drive_file_id", serialized_detail)
        self.assertEqual(detail.json()["recording_storage_state"], "drive_ready")

        granted = self.client.post(
            f"/lectures/{lecture_id}/recording-download-ticket",
            headers=self.headers(),
        )
        self.assertEqual(granted.status_code, 200, granted.text)
        path = granted.json()["path"]
        self.assertNotIn(file_id, path)
        self.assertNotIn(lecture_id, path)
        self.assertNotIn(self.tokens["user-alpha"], path)

        first = self.client.get(path, headers={"Range": "bytes=5-28"})
        self.assertEqual(first.status_code, 206, first.text)
        self.assertEqual(first.content, content[5:29])
        self.assertEqual(first.headers["content-range"], f"bytes 5-28/{len(content)}")
        self.assertEqual(first.headers["content-length"], "24")
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertEqual(self.drive.download_calls[-1], (file_id, 5, 29))
        self.assertNotIn(file_id, first.text)

        invalid = self.client.get(path, headers={"Range": f"bytes={len(content)}-"})
        self.assertEqual(invalid.status_code, 416)
        self.assertEqual(invalid.headers["content-range"], f"bytes */{len(content)}")

    def test_startup_health_does_not_wait_for_blocked_drive_deletion(self):
        lecture_id, _, _ = self.add_remote_recording()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE lectures SET deleting = 1 WHERE id = ?", (lecture_id,)
            )
        self.drive.block_trash = True

        started = time.monotonic()
        try:
            with self.client:
                try:
                    response = self.client.get("/health")
                    elapsed = time.monotonic() - started

                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertLess(elapsed, 1.0)
                    self.assertTrue(self.drive.trash_entered.wait(timeout=2))
                    with self.database.connect() as connection:
                        row = connection.execute(
                            "SELECT deleting FROM lectures WHERE id = ?", (lecture_id,)
                        ).fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row["deleting"], 1)
                finally:
                    self.drive.trash_release.set()
        finally:
            self.drive.trash_release.set()

    def test_changed_local_wav_falls_back_to_the_verified_drive_copy(self):
        lecture_id = str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO lectures"
                "(id, username, title, language, created_at, recording_finalized) "
                "VALUES (?, 'user-alpha', 'local mismatch', 'ko', "
                "'2026-09-05T00:00:00Z', 1)",
                (lecture_id,),
            )
        self.app.state.recording_store.write_chunk(
            "user-alpha",
            lecture_id,
            start_seconds=0,
            overlap_seconds=0,
            pcm=bytes(range(128)) * 4,
        )
        original = self.app.state.recording_store.path(
            "user-alpha", lecture_id
        ).read_bytes()
        with self.database.connect() as connection:
            self.app.state.archive_manager.queue(
                connection, "user-alpha", lecture_id
            )
        self.app.state.archive_manager.run_once(delete_local=False)
        path = self.app.state.recording_store.path("user-alpha", lecture_id)
        changed = bytearray(path.read_bytes())
        changed[-1] ^= 1
        path.write_bytes(changed)

        ticket = self.client.post(
            f"/lectures/{lecture_id}/recording-download-ticket",
            headers=self.headers(),
        )
        response = self.client.get(ticket.json()["path"])

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, original)
        self.assertTrue(self.drive.download_calls)

    def test_remote_delete_is_owner_isolated_and_retries_trash_before_db_removal(self):
        lecture_id, _, file_id = self.add_remote_recording()

        hidden = self.client.delete(
            f"/lectures/{lecture_id}", headers=self.headers("user-beta")
        )
        self.assertEqual(hidden.status_code, 200, hidden.text)
        self.assertEqual(self.drive.trash_calls, [])
        self.assertEqual(
            self.client.get(f"/lectures/{lecture_id}", headers=self.headers()).status_code,
            200,
        )

        self.drive.trash_error = DriveTransportError(
            "trash_temporarily_unavailable", "redacted", retryable=True
        )
        failed = self.client.delete(
            f"/lectures/{lecture_id}", headers=self.headers()
        )
        self.assertEqual(failed.status_code, 503, failed.text)
        self.assertNotIn(file_id, failed.text)
        with self.database.connect() as connection:
            retained = connection.execute(
                "SELECT deleting FROM lectures WHERE id = ?", (lecture_id,)
            ).fetchone()
            self.assertEqual(retained["deleting"], 1)
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM recording_archives WHERE lecture_id = ?", (lecture_id,)
                ).fetchone()
            )

        self.drive.trash_error = None
        retried = self.client.delete(
            f"/lectures/{lecture_id}", headers=self.headers()
        )
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertTrue(self.drive.get_metadata(file_id).trashed)
        with self.database.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM lectures WHERE id = ?", (lecture_id,)
                ).fetchone()
            )

    def test_upload_delete_race_trashes_completed_remote_and_leaves_no_local_or_db_row(self):
        lecture_id = str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO lectures"
                "(id, username, title, language, created_at, recording_finalized) "
                "VALUES (?, 'user-alpha', 'race', 'ko', '2026-09-05T00:00:00Z', 1)",
                (lecture_id,),
            )
        self.app.state.recording_store.write_chunk(
            "user-alpha",
            lecture_id,
            start_seconds=0,
            overlap_seconds=0,
            pcm=bytes(range(128)) * 4,
        )
        with self.database.connect() as connection:
            self.assertTrue(
                self.app.state.archive_manager.queue(
                    connection, "user-alpha", lecture_id
                )
            )
        self.drive.block_upload = True

        with ThreadPoolExecutor(max_workers=2) as executor:
            upload = executor.submit(
                self.app.state.archive_manager.run_once, delete_local=True
            )
            self.assertTrue(self.drive.upload_entered.wait(timeout=3))
            deletion = executor.submit(
                self.client.delete,
                f"/lectures/{lecture_id}",
                headers=self.headers(),
            )
            deadline = time.monotonic() + 3
            deleting = False
            while time.monotonic() < deadline:
                with self.database.connect() as connection:
                    row = connection.execute(
                        "SELECT deleting FROM lectures WHERE id = ?", (lecture_id,)
                    ).fetchone()
                if row is not None and row["deleting"]:
                    deleting = True
                    break
                time.sleep(0.01)
            self.assertTrue(deleting, "DELETE did not durably enter deleting state")
            self.drive.upload_release.set()
            upload_result = upload.result(timeout=5)
            delete_result = deletion.result(timeout=5)

        self.assertEqual(upload_result["migrated_count"], 1)
        self.assertEqual(delete_result.status_code, 200, delete_result.text)
        self.assertEqual(len(self.drive.files_by_id), 1)
        remote = next(iter(self.drive.files_by_id.values()))[0]
        self.assertTrue(remote.trashed)
        self.assertFalse(
            self.app.state.recording_store.path("user-alpha", lecture_id).exists()
        )
        with self.database.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM lectures WHERE id = ?", (lecture_id,)
                ).fetchone()
            )


if __name__ == "__main__":
    unittest.main()
