from __future__ import annotations

import hashlib
import hmac
import fcntl
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from contextlib import contextmanager
from urllib.parse import quote

from .db import Database
from .drive_storage import (
    DriveAccountIdentity,
    DriveAuthenticationError,
    DriveFileMetadata,
    DriveIntegrityError,
    DriveNotFoundError,
    DriveStorageError,
    DriveUploadSessionExpired,
    GoogleDriveStorage,
    UploadCheckpoint,
    derive_object_key,
)
from .recordings import RecordingCorruptError, RecordingStore
from .settings import Settings


_IDENTITY_BYTES = 32
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ROOT_FOLDER_NAME = "STT 수업 녹음"
_FOLDER_SCHEMA = "stt-archive-folder-v1"
_FOLDER_LAYOUT_VERSION = 1
_UPLOAD_SESSION_EXPIRY_GRACE_SECONDS = 8 * 24 * 60 * 60
_RECOVERABLE_AUTH_ERRORS = (
    "credential_directory",
    "credential_invalid",
    "credential_lock",
    "credential_permissions",
    "credential_scope",
    "credential_unavailable",
    "credential_write_failed",
    "drive_authorization_rejected",
    "drive_binding_mismatch",
    "token_refresh_invalid",
    "token_refresh_rejected",
)


def _now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _token_path(settings: Settings) -> Path:
    return settings.google_drive_token_path or settings.data_dir / "google-drive" / "token.json"


def _identity_path(settings: Settings) -> Path:
    return settings.data_dir / "google-drive" / "identity.key"


def _operation_lock_path(settings: Settings) -> Path:
    return settings.data_dir / "google-drive" / "archive.lock"


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise RuntimeError("Google Drive private directory is unsafe")
    path.chmod(0o700)


def _load_or_create_identity(path: Path) -> bytes:
    """Load the stable HMAC key without exposing account IDs in Drive."""

    _ensure_private_directory(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        value = secrets.token_bytes(_IDENTITY_BYTES)
        create_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(path, create_flags, 0o600)
        except FileExistsError:
            return _load_or_create_identity(path)
        try:
            view = memoryview(value)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short identity write")
                view = view[written:]
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return value
    except OSError as error:
        raise RuntimeError("Google Drive identity key cannot be opened safely") from error

    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) & 0o077
            or details.st_size != _IDENTITY_BYTES
        ):
            raise RuntimeError("Google Drive identity key is invalid")
        value = os.read(descriptor, _IDENTITY_BYTES + 1)
        if len(value) != _IDENTITY_BYTES:
            raise RuntimeError("Google Drive identity key is invalid")
        return value
    finally:
        os.close(descriptor)


def _safe_error_code(error: BaseException) -> str:
    candidate = getattr(error, "code", "")
    if isinstance(candidate, str) and _ERROR_CODE.fullmatch(candidate):
        return candidate
    if isinstance(error, RecordingCorruptError):
        return "local_recording_corrupt"
    if isinstance(error, OSError):
        return "local_storage_unavailable"
    return "archive_internal_error"


def _retry_delay(attempts: int, maximum: int) -> int:
    return min(maximum, max(5, 5 * (2 ** min(max(0, attempts - 1), 6))))


def _recording_store(
    settings: Settings, *, create_directories: bool = True
) -> RecordingStore:
    return RecordingStore(
        settings.data_dir / "recordings",
        settings.accounts,
        max_total_bytes=settings.max_recordings_bytes,
        min_free_bytes=settings.recording_free_reserve_bytes,
        max_seconds=settings.max_import_seconds,
        max_gap_seconds=180,
        create_directories=create_directories,
    )


def _database(settings: Settings) -> Database:
    database = Database(settings.database_path, settings.accounts)
    database.initialize()
    return database


class DriveArchiveManager:
    """Move immutable finalized WAV files to one operator-owned Google Drive.

    Network operations never run inside a SQLite transaction or the recording
    store lock.  A local WAV remains authoritative until verified Drive
    metadata has committed, and only then is local cleanup attempted.
    """

    def __init__(
        self,
        settings: Settings,
        database: Database,
        recording_store: RecordingStore,
        *,
        client: Any | None = None,
    ):
        self.settings = settings
        self.database = database
        self.recording_store = recording_store
        self.enabled = bool(settings.google_drive_enabled)
        self.identity_key = _load_or_create_identity(_identity_path(settings)) if self.enabled else None
        self.client = client
        if self.enabled and self.client is None:
            self.client = GoogleDriveStorage.from_token_file(
                _token_path(settings),
                chunk_size=settings.google_drive_upload_chunk_bytes,
                connect_timeout_seconds=settings.google_drive_connect_timeout_seconds,
                read_timeout_seconds=settings.google_drive_read_timeout_seconds,
            )
        self.operation_lock = threading.RLock()
        self.worker_lock = threading.Lock()
        self.worker_wake = threading.Event()
        self.worker_shutdown = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.deletion_retry: dict[str, tuple[int, float]] = {}

    @contextmanager
    def _exclusive_operation(self):
        """Serialize upload/trash across the API worker and maintenance CLI."""

        path = _operation_lock_path(self.settings)
        _ensure_private_directory(path.parent)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        with self.operation_lock:
            descriptor = os.open(path, flags, 0o600)
            try:
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode):
                    raise RuntimeError("Google Drive archive lock is unsafe")
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def close(self) -> None:
        if self.client is not None and hasattr(self.client, "close"):
            self.client.close()

    def _object_key(self, username: str, lecture_id: str) -> str:
        if self.identity_key is None:
            raise RuntimeError("Google Drive recording archive is disabled")
        return derive_object_key(self.identity_key, username, lecture_id)

    def queue(self, connection: Any, username: str, lecture_id: str) -> bool:
        """Durably enqueue one finalized local WAV in the caller transaction."""

        if not self.enabled:
            return False
        object_key = self._object_key(username, lecture_id)
        changed = connection.execute(
            "INSERT OR IGNORE INTO recording_archives"
            "(lecture_id, state, object_key, updated_at) VALUES (?, 'pending', ?, ?)",
            (lecture_id, object_key, _now_text()),
        ).rowcount
        return changed == 1

    def wake(self) -> None:
        self.worker_wake.set()

    def storage(self, username: str, lecture_id: str, finalized: bool) -> dict[str, Any]:
        """Return the public compatibility flags without any Drive locator."""

        local_available = self.recording_store.available(username, lecture_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT state, drive_file_id, local_deleted, attempts, last_error_code "
                "FROM recording_archives WHERE lecture_id = ?",
                (lecture_id,),
            ).fetchone()
        archive = dict(row) if row is not None else None
        remote_present = bool(
            archive
            and archive["state"] in {"ready", "attention"}
            and archive["drive_file_id"]
        )
        remote_available = bool(self.enabled and self.client is not None and remote_present)
        available = local_available or remote_available
        if archive and archive["state"] == "attention":
            state = "attention_required"
        elif archive and archive["state"] == "ready":
            if not self.enabled or self.client is None:
                state = "local_recording" if local_available else "attention_required"
            elif archive["last_error_code"] == "cleanup_source_mismatch":
                state = "attention_required"
            else:
                state = (
                    "drive_ready"
                    if archive["local_deleted"] or not local_available
                    else "drive_cleanup_pending"
                )
        elif archive and archive["state"] == "uploading":
            state = "uploading"
        elif archive and archive["state"] == "pending":
            state = "retrying" if archive["last_error_code"] else "upload_queued"
        elif local_available:
            state = "upload_queued" if self.enabled and finalized else "local_recording"
        else:
            state = "none"
        return {
            "recording_available": available,
            "recording_storage_state": state,
        }

    def recover(self, *, enqueue_existing: bool = True) -> None:
        if not self.enabled:
            return
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET state = CASE "
                "WHEN drive_file_id IS NOT NULL THEN 'ready' ELSE 'pending' END, "
                "next_attempt_at = 0, "
                "last_error_code = 'server_restarted', updated_at = ? "
                "WHERE state = 'uploading'",
                (_now_text(),),
            )
            # OAuth failures should not spin continuously, but they must become
            # recoverable after the operator renews authorization and restarts
            # the server. Integrity/conflict failures remain in attention.
            connection.execute(
                "UPDATE recording_archives SET state = CASE "
                "WHEN drive_file_id IS NOT NULL THEN 'ready' ELSE 'pending' END, "
                "next_attempt_at = 0, "
                "updated_at = ? WHERE state = 'attention' AND last_error_code IN ("
                + ",".join("?" for _ in _RECOVERABLE_AUTH_ERRORS)
                + ")",
                (_now_text(), *_RECOVERABLE_AUTH_ERRORS),
            )
        if enqueue_existing:
            self.enqueue_existing()
        self.wake()

    def enqueue_existing(self, *, limit: int | None = None) -> dict[str, int]:
        if not self.enabled:
            return {"enqueued_count": 0, "attention_count": 0}
        with self.database.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT id, username FROM lectures "
                    "WHERE recording_finalized = 1 AND deleting = 0 ORDER BY created_at, id"
                ).fetchall()
            ]
        enqueued = 0
        attention = 0
        examined = 0
        for lecture in rows:
            if limit is not None and examined >= limit:
                break
            with self.database.connect() as connection:
                existing = connection.execute(
                    "SELECT state FROM recording_archives WHERE lecture_id = ?",
                    (lecture["id"],),
                ).fetchone()
            if existing is not None:
                continue
            try:
                info = self.recording_store.info(lecture["username"], lecture["id"])
            except RecordingCorruptError:
                object_key = self._object_key(lecture["username"], lecture["id"])
                with self.database.connect() as connection:
                    changed = connection.execute(
                        "INSERT OR IGNORE INTO recording_archives"
                        "(lecture_id, state, object_key, last_error_code, updated_at) "
                        "VALUES (?, 'attention', ?, 'local_recording_corrupt', ?)",
                        (lecture["id"], object_key, _now_text()),
                    ).rowcount
                attention += int(changed == 1)
                examined += 1
                continue
            if info is None:
                continue
            examined += 1
            with self.database.connect() as connection:
                enqueued += int(self.queue(connection, lecture["username"], lecture["id"]))
        if enqueued:
            self.wake()
        return {"enqueued_count": enqueued, "attention_count": attention}

    def _ensure_folder(self) -> str:
        return self._verify_binding(create=True)

    def _root_folder_key(self) -> str:
        if self.identity_key is None:
            raise RuntimeError("Google Drive identity is unavailable")
        return hmac.new(
            self.identity_key,
            b"stt-drive-root-folder-v1",
            hashlib.sha256,
        ).hexdigest()

    def _user_folder_key(self, username: str) -> str:
        if self.identity_key is None:
            raise RuntimeError("Google Drive identity is unavailable")
        if username not in self.settings.accounts:
            raise DriveIntegrityError(
                "archive_owner_invalid", "Recording archive owner is invalid."
            )
        return hmac.new(
            self.identity_key,
            b"stt-drive-user-folder-v1\0" + username.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _binding_key(self, identity: DriveAccountIdentity) -> str:
        if self.identity_key is None:
            raise RuntimeError("Google Drive identity is unavailable")
        material = (
            identity.permission_id.encode("utf-8")
            + b"\0"
            + identity.oauth_client_fingerprint.encode("ascii")
        )
        return hmac.new(
            self.identity_key,
            b"stt-drive-account-and-app-v1\0" + material,
            hashlib.sha256,
        ).hexdigest()

    def _verify_binding(self, *, create: bool) -> str:
        """Pin destructive operations to one Google user and OAuth client."""

        if self.client is None:
            raise DriveAuthenticationError(
                "drive_binding_unavailable",
                "Google Drive archive binding is unavailable.",
            )
        identity = self.client.account_identity()
        if not isinstance(identity, DriveAccountIdentity):
            raise DriveAuthenticationError(
                "drive_binding_invalid",
                "Google Drive account identity could not be verified.",
            )
        binding_key = self._binding_key(identity)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT binding_key, folder_id FROM drive_archive_binding WHERE singleton = 1"
            ).fetchone()
        if row is not None:
            if not hmac.compare_digest(row["binding_key"], binding_key):
                raise DriveAuthenticationError(
                    "drive_binding_mismatch",
                    "Google Drive account or OAuth client does not match this archive.",
                )
            folder_key = self._root_folder_key()
            metadata = self.client.get_metadata(row["folder_id"])
            if (
                metadata.trashed
                or metadata.mime_type != "application/vnd.google-apps.folder"
                or metadata.app_properties.get("stt_schema") != _FOLDER_SCHEMA
                or metadata.app_properties.get("stt_object") != folder_key
            ):
                raise DriveIntegrityError(
                    "archive_folder_mismatch",
                    "Drive archive folder metadata is invalid.",
                )
            self.client.folder_id = row["folder_id"]
            return row["folder_id"]
        if not create:
            raise DriveAuthenticationError(
                "drive_binding_missing",
                "Google Drive archive binding has not been established.",
            )
        folder_key = self._root_folder_key()
        metadata = self.client.ensure_folder(
            object_key=folder_key,
            name=_ROOT_FOLDER_NAME,
        )
        if (
            metadata.trashed
            or metadata.mime_type != "application/vnd.google-apps.folder"
            or metadata.app_properties.get("stt_schema") != _FOLDER_SCHEMA
            or metadata.app_properties.get("stt_object") != folder_key
        ):
            raise DriveIntegrityError(
                "archive_folder_mismatch",
                "Drive archive folder metadata is invalid.",
            )
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO drive_archive_binding"
                "(singleton, binding_key, folder_id, updated_at) VALUES (1, ?, ?, ?)",
                (binding_key, metadata.file_id, _now_text()),
            )
        self.client.folder_id = metadata.file_id
        return metadata.file_id

    def _ensure_user_folder(self, username: str, *, root_folder_id: str | None = None) -> str:
        """Return one owner-labelled folder bound to an opaque local identity."""

        if self.client is None:
            raise DriveAuthenticationError(
                "drive_binding_unavailable",
                "Google Drive archive binding is unavailable.",
            )
        root_id = root_folder_id or self._verify_binding(create=True)
        folder_key = self._user_folder_key(username)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT folder_key, folder_id FROM drive_archive_user_folders "
                "WHERE username = ?",
                (username,),
            ).fetchone()
        if row is not None:
            if not hmac.compare_digest(row["folder_key"], folder_key):
                raise DriveIntegrityError(
                    "user_folder_binding_mismatch",
                    "Drive user folder binding is invalid.",
                )
            metadata = self.client.get_metadata(row["folder_id"])
        else:
            # The runtime account ID makes the operator's private Drive easy to
            # navigate. Only its HMAC key is stored in appProperties; archive
            # metadata/API responses do not add the ID, and source control never
            # contains the configured account list.
            metadata = self.client.ensure_folder(
                object_key=folder_key,
                name=username,
                parent_id=root_id,
            )
        if (
            metadata.trashed
            or metadata.mime_type != "application/vnd.google-apps.folder"
            or metadata.app_properties.get("stt_schema") != _FOLDER_SCHEMA
            or metadata.app_properties.get("stt_object") != folder_key
            or metadata.parents != (root_id,)
        ):
            raise DriveIntegrityError(
                "user_folder_mismatch", "Drive user folder metadata is invalid."
            )
        if row is None:
            try:
                with self.database.connect() as connection:
                    connection.execute(
                        "INSERT INTO drive_archive_user_folders"
                        "(username, folder_key, folder_id, updated_at) VALUES (?, ?, ?, ?)",
                        (username, folder_key, metadata.file_id, _now_text()),
                    )
            except sqlite3.IntegrityError:
                raise DriveIntegrityError(
                    "user_folder_binding_conflict",
                    "Drive user folder binding conflicts with private archive state.",
                ) from None
        return metadata.file_id

    def _place_recording_in_user_folder(
        self,
        row: dict[str, Any],
        metadata: DriveFileMetadata,
        *,
        root_folder_id: str,
        user_folder_id: str,
    ) -> DriveFileMetadata:
        """Idempotently move a verified legacy/root recording to its owner folder."""

        if not self._remote_matches_archive(row, metadata):
            raise DriveIntegrityError(
                "remote_integrity_mismatch",
                "Drive recording changed before folder placement.",
            )
        if metadata.parents == (user_folder_id,):
            return metadata
        if metadata.parents != (root_folder_id,):
            raise DriveIntegrityError(
                "recording_parent_mismatch",
                "Drive recording is in an unexpected folder.",
            )
        moved = self.client.move_file(
            metadata.file_id,
            previous_parent_id=root_folder_id,
            new_parent_id=user_folder_id,
        )
        if not self._remote_matches_archive(row, moved) or moved.parents != (user_folder_id,):
            raise DriveIntegrityError(
                "folder_move_not_confirmed",
                "Drive recording folder move was not confirmed.",
            )
        return moved

    def verify_connection(self) -> bool:
        if not self.enabled or self.client is None:
            return False
        self.client.verify_connection()
        with self.database.connect() as connection:
            bound = connection.execute(
                "SELECT 1 FROM drive_archive_binding WHERE singleton = 1"
            ).fetchone()
        if bound is not None:
            self._verify_binding(create=False)
        else:
            # Even before the first upload, make sure about.get is available
            # under the exact drive.file grant used to establish the binding.
            identity = self.client.account_identity()
            if not isinstance(identity, DriveAccountIdentity):
                raise DriveAuthenticationError(
                    "drive_binding_invalid",
                    "Google Drive account identity could not be verified.",
                )
        return True

    @staticmethod
    def _checkpoint(row: dict[str, Any]) -> UploadCheckpoint | None:
        if not row.get("upload_session_uri"):
            return None
        if not all(
            row.get(key) is not None
            for key in ("source_bytes", "source_sha256", "source_md5")
        ):
            return None
        try:
            return UploadCheckpoint(
                session_uri=row["upload_session_uri"],
                object_key=row["object_key"],
                total_size=int(row["source_bytes"]),
                sha256_checksum=row["source_sha256"],
                md5_checksum=row["source_md5"],
                committed_bytes=int(row["uploaded_bytes"]),
            )
        except DriveStorageError:
            return None

    def _save_checkpoint(self, lecture_id: str, checkpoint: UploadCheckpoint) -> None:
        with self.database.connect() as connection:
            changed = connection.execute(
                "UPDATE recording_archives SET upload_session_uri = ?, source_bytes = ?, "
                "source_sha256 = ?, source_md5 = ?, uploaded_bytes = ?, updated_at = ? "
                "WHERE lecture_id = ? AND state = 'uploading' AND object_key = ?",
                (
                    checkpoint.session_uri,
                    checkpoint.total_size,
                    checkpoint.sha256_checksum,
                    checkpoint.md5_checksum,
                    checkpoint.committed_bytes,
                    _now_text(),
                    lecture_id,
                    checkpoint.object_key,
                ),
            ).rowcount
        if changed != 1:
            raise DriveIntegrityError(
                "archive_state_changed",
                "Recording archive state changed during upload.",
            )

    def _claim_pending(self) -> dict[str, Any] | None:
        now = time.time()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT a.*, l.username, l.created_at FROM recording_archives AS a "
                "JOIN lectures AS l ON l.id = a.lecture_id "
                "WHERE a.state = 'pending' AND a.next_attempt_at <= ? "
                "AND l.recording_finalized = 1 AND l.deleting = 0 "
                "ORDER BY a.updated_at, a.lecture_id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                "UPDATE recording_archives SET state = 'uploading', attempts = attempts + 1, "
                "updated_at = ? WHERE lecture_id = ? AND state = 'pending'",
                (_now_text(), row["lecture_id"]),
            ).rowcount
        if changed != 1:
            return None
        result = dict(row)
        result["attempts"] += 1
        result["state"] = "uploading"
        return result

    def _record_failure(self, row: dict[str, Any], error: BaseException) -> None:
        retryable = bool(getattr(error, "retryable", False)) or isinstance(error, OSError)
        state = "pending" if retryable else "attention"
        next_attempt = (
            time.time() + _retry_delay(row["attempts"], self.settings.google_drive_retry_max_seconds)
            if retryable
            else 0
        )
        clear_checkpoint = getattr(error, "code", None) in {
            "upload_session_expired",
            "checkpoint_mismatch",
        }
        with self.database.connect() as connection:
            if clear_checkpoint:
                connection.execute(
                    "UPDATE recording_archives SET state = ?, next_attempt_at = ?, "
                    "last_error_code = ?, upload_session_uri = NULL, uploaded_bytes = 0, "
                    "source_bytes = NULL, source_sha256 = NULL, source_md5 = NULL, updated_at = ? "
                    "WHERE lecture_id = ? AND state = 'uploading'",
                    (
                        state,
                        next_attempt,
                        _safe_error_code(error),
                        _now_text(),
                        row["lecture_id"],
                    ),
                )
            else:
                connection.execute(
                    "UPDATE recording_archives SET state = ?, next_attempt_at = ?, "
                    "last_error_code = ?, updated_at = ? "
                    "WHERE lecture_id = ? AND state = 'uploading'",
                    (
                        state,
                        next_attempt,
                        _safe_error_code(error),
                        _now_text(),
                        row["lecture_id"],
                    ),
                )

    def _claim_ready_organization(
        self, candidate: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Revalidate and count one layout attempt while holding the file lock."""

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT a.*, l.username FROM recording_archives AS a "
                "JOIN lectures AS l ON l.id = a.lecture_id "
                "WHERE a.lecture_id = ? AND a.drive_file_id = ? "
                "AND a.state = 'ready' AND a.folder_layout_version < ? "
                "AND a.next_attempt_at <= ? AND l.deleting = 0",
                (
                    candidate["lecture_id"],
                    candidate["drive_file_id"],
                    _FOLDER_LAYOUT_VERSION,
                    time.time(),
                ),
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                "UPDATE recording_archives SET attempts = attempts + 1, updated_at = ? "
                "WHERE lecture_id = ? AND drive_file_id = ? AND state = 'ready' "
                "AND folder_layout_version < ? AND attempts = ?",
                (
                    _now_text(),
                    row["lecture_id"],
                    row["drive_file_id"],
                    _FOLDER_LAYOUT_VERSION,
                    row["attempts"],
                ),
            ).rowcount
            if changed != 1:
                return None
        result = dict(row)
        result["attempts"] = int(result["attempts"]) + 1
        return result

    def _record_organization_failure(
        self,
        row: dict[str, Any],
        error: BaseException,
        *,
        retryable: bool,
        attempt_claimed: bool,
    ) -> None:
        """Persist a redacted layout failure without overwriting stale success."""

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT a.attempts FROM recording_archives AS a "
                "JOIN lectures AS l ON l.id = a.lecture_id "
                "WHERE a.lecture_id = ? AND a.drive_file_id = ? "
                "AND a.state = 'ready' AND a.folder_layout_version < ? "
                "AND l.deleting = 0",
                (
                    row["lecture_id"],
                    row["drive_file_id"],
                    _FOLDER_LAYOUT_VERSION,
                ),
            ).fetchone()
            if current is None:
                return
            expected_attempts = int(row["attempts"])
            if int(current["attempts"]) != expected_attempts:
                # A different process claimed this row after our outer read.
                # Its result, not this stale failure, owns the state update.
                return
            attempts = expected_attempts + (0 if attempt_claimed else 1)
            connection.execute(
                "UPDATE recording_archives SET state = ?, attempts = ?, "
                "last_error_code = ?, next_attempt_at = ?, updated_at = ? "
                "WHERE lecture_id = ? AND drive_file_id = ? AND state = 'ready' "
                "AND folder_layout_version < ? AND attempts = ? "
                "AND EXISTS (SELECT 1 FROM lectures WHERE id = ? AND deleting = 0)",
                (
                    "ready" if retryable else "attention",
                    attempts,
                    _safe_error_code(error),
                    time.time()
                    + _retry_delay(
                        attempts, self.settings.google_drive_retry_max_seconds
                    )
                    if retryable
                    else 0,
                    _now_text(),
                    row["lecture_id"],
                    row["drive_file_id"],
                    _FOLDER_LAYOUT_VERSION,
                    expected_attempts,
                    row["lecture_id"],
                ),
            )

    def _organize_ready(self) -> dict[str, int] | None:
        """Place one legacy ready object in its owner's folder before cleanup."""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT a.*, l.username FROM recording_archives AS a "
                "JOIN lectures AS l ON l.id = a.lecture_id "
                "WHERE a.state = 'ready' AND a.folder_layout_version < ? "
                "AND a.next_attempt_at <= ? AND l.deleting = 0 "
                "ORDER BY a.updated_at, a.lecture_id LIMIT 1",
                (_FOLDER_LAYOUT_VERSION, time.time()),
            ).fetchone()
        if row is None:
            return None
        row = dict(row)
        attempt_claimed = False
        try:
            with self._exclusive_operation():
                claimed = self._claim_ready_organization(row)
                if claimed is None:
                    # Another maintenance process may have completed or
                    # deleted this candidate while this process waited for
                    # the cross-process Drive lock. Treat that as a safe no-op.
                    return {
                        "migrated_count": 0,
                        "deleted_local_count": 0,
                        "failed_count": 0,
                    }
                row = claimed
                attempt_claimed = True
                if self.client is None or not row.get("drive_file_id"):
                    raise DriveIntegrityError(
                        "organization_remote_unavailable",
                        "Verified Drive recording metadata is unavailable.",
                    )
                root_folder_id = self._verify_binding(create=False)
                user_folder_id = self._ensure_user_folder(
                    row["username"], root_folder_id=root_folder_id
                )
                remote = self.client.get_metadata(row["drive_file_id"])
                self._place_recording_in_user_folder(
                    row,
                    remote,
                    root_folder_id=root_folder_id,
                    user_folder_id=user_folder_id,
                )
                with self.database.connect() as connection:
                    changed = connection.execute(
                        "UPDATE recording_archives SET folder_layout_version = ?, "
                        "last_error_code = NULL, next_attempt_at = 0, updated_at = ? "
                        "WHERE lecture_id = ? AND state = 'ready' AND drive_file_id = ? "
                        "AND folder_layout_version < ?",
                        (
                            _FOLDER_LAYOUT_VERSION,
                            _now_text(),
                            row["lecture_id"],
                            row["drive_file_id"],
                            _FOLDER_LAYOUT_VERSION,
                        ),
                    ).rowcount
                if changed != 1:
                    raise DriveIntegrityError(
                        "archive_state_changed",
                        "Recording archive state changed during folder placement.",
                    )
            return {"migrated_count": 0, "deleted_local_count": 0, "failed_count": 0}
        except DriveIntegrityError as error:
            self._record_organization_failure(
                row,
                error,
                retryable=False,
                attempt_claimed=attempt_claimed,
            )
            return {"migrated_count": 0, "deleted_local_count": 0, "failed_count": 1}
        except DriveStorageError as error:
            self._record_organization_failure(
                row,
                error,
                retryable=bool(error.retryable),
                attempt_claimed=attempt_claimed,
            )
            return {"migrated_count": 0, "deleted_local_count": 0, "failed_count": 1}
        except OSError as error:
            self._record_organization_failure(
                row,
                error,
                retryable=True,
                attempt_claimed=attempt_claimed,
            )
            return {"migrated_count": 0, "deleted_local_count": 0, "failed_count": 1}
        except RuntimeError as error:
            self._record_organization_failure(
                row,
                error,
                retryable=False,
                attempt_claimed=attempt_claimed,
            )
            return {"migrated_count": 0, "deleted_local_count": 0, "failed_count": 1}

    def _cleanup_ready(self, *, delete_local: bool) -> dict[str, int] | None:
        if not delete_local:
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT a.*, l.username FROM recording_archives AS a "
                "JOIN lectures AS l ON l.id = a.lecture_id "
                "WHERE a.state = 'ready' AND a.local_deleted = 0 "
                "AND a.folder_layout_version >= ? "
                "AND a.next_attempt_at <= ? AND l.deleting = 0 "
                "ORDER BY a.updated_at, a.lecture_id LIMIT 1",
                (_FOLDER_LAYOUT_VERSION, time.time()),
            ).fetchone()
        if row is None:
            return None
        row = dict(row)
        try:
            with self._exclusive_operation():
                if self.client is None or not row.get("drive_file_id"):
                    raise DriveIntegrityError(
                        "cleanup_remote_unavailable",
                        "Verified Drive recording metadata is unavailable.",
                    )
                root_folder_id = self._verify_binding(create=False)
                user_folder_id = self._ensure_user_folder(
                    row["username"], root_folder_id=root_folder_id
                )
                remote = self.client.get_metadata(row["drive_file_id"])
                if (
                    not self._remote_matches_archive(row, remote)
                    or remote.parents != (user_folder_id,)
                ):
                    raise DriveIntegrityError(
                        "cleanup_remote_mismatch",
                        "Drive recording changed before local cleanup.",
                    )
                with self.recording_store.lock:
                    recording = self.recording_store.open_info(
                        row["username"], row["lecture_id"]
                    )
                    if recording is None:
                        deleted = True
                    else:
                        descriptor = recording["descriptor"]
                        try:
                            sha256 = hashlib.sha256()
                            md5 = hashlib.md5(usedforsecurity=False)
                            os.lseek(descriptor, 0, os.SEEK_SET)
                            while True:
                                block = os.read(descriptor, 1024 * 1024)
                                if not block:
                                    break
                                sha256.update(block)
                                md5.update(block)
                            if (
                                recording["bytes"] != row["source_bytes"]
                                or not hmac.compare_digest(
                                    sha256.hexdigest(), row["source_sha256"]
                                )
                                or not hmac.compare_digest(
                                    md5.hexdigest(), row["source_md5"]
                                )
                            ):
                                raise DriveIntegrityError(
                                    "cleanup_source_mismatch",
                                    "Local recording no longer matches the verified Drive file.",
                                )
                        finally:
                            os.close(descriptor)
                        self.recording_store.delete(row["username"], row["lecture_id"])
                        deleted = True
            if deleted:
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE recording_archives SET local_deleted = 1, last_error_code = NULL, "
                        "next_attempt_at = 0, updated_at = ? "
                        "WHERE lecture_id = ? AND state = 'ready'",
                        (_now_text(), row["lecture_id"]),
                    )
                return {"deleted_local_count": 1, "failed_count": 0}
        except DriveIntegrityError as error:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE recording_archives SET state = 'attention', last_error_code = ?, "
                    "next_attempt_at = 0, "
                    "updated_at = ? WHERE lecture_id = ? AND state = 'ready'",
                    (_safe_error_code(error), _now_text(), row["lecture_id"]),
                )
            return {"deleted_local_count": 0, "failed_count": 1}
        except DriveStorageError as error:
            retryable = bool(error.retryable)
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE recording_archives SET state = ?, last_error_code = ?, "
                    "next_attempt_at = ?, updated_at = ? "
                    "WHERE lecture_id = ? AND state = 'ready'",
                    (
                        "ready" if retryable else "attention",
                        _safe_error_code(error),
                        time.time()
                        + _retry_delay(
                            row["attempts"],
                            self.settings.google_drive_retry_max_seconds,
                        )
                        if retryable
                        else 0,
                        _now_text(),
                        row["lecture_id"],
                    ),
                )
            return {"deleted_local_count": 0, "failed_count": 1}
        except (OSError, RecordingCorruptError) as error:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE recording_archives SET last_error_code = ?, next_attempt_at = ?, "
                    "updated_at = ? WHERE lecture_id = ? AND state = 'ready'",
                    (
                        _safe_error_code(error),
                        time.time() + _retry_delay(row["attempts"], self.settings.google_drive_retry_max_seconds),
                        _now_text(),
                        row["lecture_id"],
                    ),
                )
            return {"deleted_local_count": 0, "failed_count": 1}
        return None

    def _retry_deleting_once(self) -> dict[str, int] | None:
        """Finish one durable deletion without starving archive uploads."""

        with self.database.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT id, username FROM lectures WHERE deleting = 1 "
                    "ORDER BY created_at, id"
                ).fetchall()
            ]
        live = {row["id"] for row in rows}
        for lecture_id in tuple(self.deletion_retry):
            if lecture_id not in live:
                self.deletion_retry.pop(lecture_id, None)
        now = time.time()
        row = next(
            (
                candidate
                for candidate in rows
                if self.deletion_retry.get(candidate["id"], (0, 0))[1] <= now
            ),
            None,
        )
        if row is None:
            return None
        try:
            if not self.trash_for_deletion(row["id"]):
                raise RuntimeError("remote deletion is not yet confirmed")
            with self.recording_store.lock:
                self.recording_store.delete(row["username"], row["id"])
            with self.database.connect() as connection:
                connection.execute(
                    "DELETE FROM lectures WHERE id = ? AND username = ? AND deleting = 1",
                    (row["id"], row["username"]),
                )
            self.deletion_retry.pop(row["id"], None)
            return {
                "migrated_count": 0,
                "deleted_local_count": 1,
                "failed_count": 0,
            }
        except Exception:
            attempts = self.deletion_retry.get(row["id"], (0, 0))[0] + 1
            self.deletion_retry[row["id"]] = (
                attempts,
                now
                + _retry_delay(
                    attempts, self.settings.google_drive_retry_max_seconds
                ),
            )
            return {
                "migrated_count": 0,
                "deleted_local_count": 0,
                "failed_count": 1,
            }

    def run_once(self, *, delete_local: bool = True) -> dict[str, int] | None:
        deletion = self._retry_deleting_once()
        if deletion is not None:
            return deletion
        if not self.enabled or self.client is None:
            return None
        organization = self._organize_ready()
        if organization is not None:
            if organization["failed_count"] == 0:
                cleanup = self._cleanup_ready(delete_local=delete_local)
                if cleanup is not None:
                    organization["deleted_local_count"] += cleanup["deleted_local_count"]
                    organization["failed_count"] += cleanup["failed_count"]
            return organization
        cleanup = self._cleanup_ready(delete_local=delete_local)
        if cleanup is not None:
            return {"migrated_count": 0, **cleanup}
        row = self._claim_pending()
        if row is None:
            return None
        try:
            with self._exclusive_operation():
                recording = self.recording_store.info(row["username"], row["lecture_id"])
                if recording is None:
                    raise DriveIntegrityError(
                        "source_unavailable",
                        "Finalized local recording is unavailable.",
                    )
                root_folder_id = self._ensure_folder()
                user_folder_id = self._ensure_user_folder(
                    row["username"], root_folder_id=root_folder_id
                )
                checkpoint = self._checkpoint(row)
                metadata = self.client.upload_recording(
                    recording["path"],
                    object_key=row["object_key"],
                    name=f"{row['lecture_id']}.wav",
                    parent_id=user_folder_id,
                    checkpoint=checkpoint,
                    on_checkpoint=lambda value: self._save_checkpoint(row["lecture_id"], value),
                )
                if metadata.size is None or metadata.md5_checksum is None or metadata.sha256_checksum is None:
                    raise DriveIntegrityError(
                        "remote_integrity_missing",
                        "Drive did not return recording checksums.",
                    )
                placement_row = {
                    **row,
                    "source_bytes": metadata.size,
                    "source_sha256": metadata.sha256_checksum,
                    "source_md5": metadata.md5_checksum,
                }
                metadata = self._place_recording_in_user_folder(
                    placement_row,
                    metadata,
                    root_folder_id=root_folder_id,
                    user_folder_id=user_folder_id,
                )
                with self.database.connect() as connection:
                    changed = connection.execute(
                        "UPDATE recording_archives SET state = 'ready', drive_file_id = ?, "
                        "source_bytes = ?, source_sha256 = ?, source_md5 = ?, uploaded_bytes = ?, "
                        "folder_layout_version = ?, "
                        "upload_session_uri = NULL, last_error_code = NULL, next_attempt_at = 0, "
                        "updated_at = ? WHERE lecture_id = ? AND state = 'uploading'",
                        (
                            metadata.file_id,
                            metadata.size,
                            metadata.sha256_checksum,
                            metadata.md5_checksum,
                            metadata.size,
                            _FOLDER_LAYOUT_VERSION,
                            _now_text(),
                            row["lecture_id"],
                        ),
                    ).rowcount
                if changed != 1:
                    raise DriveIntegrityError(
                        "archive_state_changed",
                        "Recording archive state changed after upload.",
                    )
            result = {"migrated_count": 1, "deleted_local_count": 0, "failed_count": 0}
            cleanup = self._cleanup_ready(delete_local=delete_local)
            if cleanup:
                result["deleted_local_count"] += cleanup["deleted_local_count"]
                result["failed_count"] += cleanup["failed_count"]
            return result
        except Exception as error:
            self._record_failure(row, error)
            return {"migrated_count": 0, "deleted_local_count": 0, "failed_count": 1}

    @staticmethod
    def _remote_matches_archive(
        row: dict[str, Any],
        metadata: DriveFileMetadata,
        *,
        allow_trashed: bool = False,
    ) -> bool:
        """Fail closed if a stored locator now points at different content."""

        properties = metadata.app_properties
        if (
            (metadata.trashed and not allow_trashed)
            or metadata.mime_type != "audio/wav"
            or properties.get("stt_schema") != "stt-recording-v1"
            or properties.get("stt_object") != row["object_key"]
        ):
            return False
        expected = (
            ("source_bytes", metadata.size, str),
            ("source_sha256", metadata.sha256_checksum, lambda value: value.casefold()),
            ("source_md5", metadata.md5_checksum, lambda value: value.casefold()),
        )
        for key, actual, normalize in expected:
            stored = row.get(key)
            if stored is None:
                continue
            if actual is None or normalize(actual) != normalize(stored):
                return False
        if row.get("source_bytes") is not None and properties.get("stt_size") != str(
            row["source_bytes"]
        ):
            return False
        if row.get("source_sha256") is not None and not hmac.compare_digest(
            properties.get("stt_sha256", "").casefold(),
            row["source_sha256"].casefold(),
        ):
            return False
        if row.get("source_md5") is not None and not hmac.compare_digest(
            properties.get("stt_md5", "").casefold(),
            row["source_md5"].casefold(),
        ):
            return False
        return True

    def _find_exact_recording(self, row: dict[str, Any]) -> DriveFileMetadata | None:
        return self.client.find_recording(
            row["object_key"],
            expected_size=row["source_bytes"],
            expected_sha256=row["source_sha256"],
            expected_md5=row["source_md5"],
        )

    def _defer_upload_session_recheck(self, row: dict[str, Any]) -> None:
        """Leave a queried 308 untouched long enough for documented expiry."""

        retry_at = time.time() + _UPLOAD_SESSION_EXPIRY_GRACE_SECONDS
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE recording_archives SET last_error_code = "
                "'upload_session_incomplete', next_attempt_at = ?, updated_at = ? "
                "WHERE lecture_id = ? AND drive_file_id IS NULL "
                "AND upload_session_uri = ?",
                (
                    retry_at,
                    _now_text(),
                    row["lecture_id"],
                    row["upload_session_uri"],
                ),
            )

    def trash_for_deletion(self, lecture_id: str) -> bool:
        """Trash a visible or response-lost remote object before DB deletion."""

        if not self.enabled or self.client is None:
            with self.database.connect() as connection:
                return connection.execute(
                    "SELECT 1 FROM recording_archives WHERE lecture_id = ?",
                    (lecture_id,),
                ).fetchone() is None
        with self._exclusive_operation():
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM recording_archives WHERE lecture_id = ?",
                    (lecture_id,),
                ).fetchone()
                binding = connection.execute(
                    "SELECT 1 FROM drive_archive_binding WHERE singleton = 1"
                ).fetchone()
            if row is None:
                return True
            row = dict(row)
            # The first queued recording can be deleted before any upload has
            # established the archive binding. Only a pristine queue entry
            # proves this is still local-only: upload claims increment attempts
            # before creating a binding, and upload never starts before that
            # binding is committed. Any attempt, locator or upload fingerprint
            # must still take the verified remote reconciliation path below.
            if (
                binding is None
                and row["state"] == "pending"
                and row["attempts"] == 0
                and row["uploaded_bytes"] == 0
                and row["local_deleted"] == 0
                and row["folder_layout_version"] == 0
                and row["last_error_code"] is None
                and all(
                    row[field] is None
                    for field in (
                        "drive_file_id",
                        "upload_session_uri",
                        "source_bytes",
                        "source_sha256",
                        "source_md5",
                    )
                )
            ):
                return True
            try:
                self._verify_binding(create=False)
                metadata = None
                if row["drive_file_id"]:
                    try:
                        metadata = self.client.get_metadata(row["drive_file_id"])
                    except DriveNotFoundError:
                        # A 404 can also mean that this credential lost access.
                        # Never erase the last local/DB link for a known remote
                        # locator merely because it is currently invisible.
                        return False
                    if metadata is None or metadata.trashed:
                        # A previous trash may have completed after a response
                        # was lost while another reconciled copy still exists.
                        metadata = self._find_exact_recording(row)
                else:
                    checkpoint = self._checkpoint(row)
                    if row["upload_session_uri"] and checkpoint is None:
                        # Even a malformed private checkpoint proves that an
                        # upload may still complete. Keep its durable locator.
                        return False
                    if checkpoint is not None:
                        session_expired = False
                        if (
                            row["last_error_code"] == "upload_session_incomplete"
                            and row["next_attempt_at"] > time.time()
                        ):
                            # Search can discover a response-lost completion
                            # without touching the still-live session. Avoiding
                            # PUT status queries lets the session become inactive.
                            metadata = self._find_exact_recording(row)
                        else:
                            try:
                                metadata = self.client.reconcile_upload_session(checkpoint)
                            except DriveUploadSessionExpired:
                                session_expired = True
                                # Drive defines a session 404 as expiry. Only
                                # then can an exact search establish absence.
                                metadata = self._find_exact_recording(row)
                        if metadata is None:
                            if session_expired:
                                return True
                            if row["last_error_code"] != "upload_session_incomplete" or (
                                row["next_attempt_at"] <= time.time()
                            ):
                                self._defer_upload_session_recheck(row)
                            # A 308 session can still create the file after a
                            # lost final response. Never erase its tracking row.
                            return False
                        if metadata.trashed:
                            return self._remote_matches_archive(
                                row, metadata, allow_trashed=True
                            )
                    else:
                        metadata = self._find_exact_recording(row)
                if metadata is None or metadata.trashed:
                    return True
                if not self._remote_matches_archive(row, metadata):
                    return False
                self.client.trash(metadata.file_id)
                return True
            except DriveStorageError:
                return False

    def open_download(self, lecture_id: str, *, start: int | None, end: int | None):
        if not self.enabled or self.client is None:
            raise DriveNotFoundError(
                "recording_unavailable",
                "Google Drive recording backend is unavailable.",
            )
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recording_archives "
                "WHERE lecture_id = ? AND state IN ('ready', 'attention')",
                (lecture_id,),
            ).fetchone()
        if row is None or not row["drive_file_id"]:
            raise DriveNotFoundError(
                "recording_unavailable",
                "Google Drive recording is unavailable.",
            )
        row = dict(row)
        with self._exclusive_operation():
            self._verify_binding(create=False)
            stream = self.client.open_download(
                row["drive_file_id"], start=start, end=end
            )
            metadata = getattr(stream, "file", None)
            if not isinstance(
                metadata, DriveFileMetadata
            ) or not self._remote_matches_archive(row, metadata):
                stream.close()
                raise DriveIntegrityError(
                    "remote_integrity_mismatch",
                    "Google Drive recording no longer matches the archived lesson.",
                )
        return stream

    def remote_size(self, lecture_id: str) -> int | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT source_bytes FROM recording_archives "
                "WHERE lecture_id = ? AND state IN ('ready', 'attention') "
                "AND drive_file_id IS NOT NULL",
                (lecture_id,),
            ).fetchone()
        if row is None or row["source_bytes"] is None:
            return None
        return int(row["source_bytes"])

    def local_download_matches_archive(
        self, lecture_id: str, recording: dict[str, Any]
    ) -> bool:
        """Reject a structurally valid local WAV that differs from archived bytes."""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT source_bytes, source_sha256, source_md5 "
                "FROM recording_archives WHERE lecture_id = ? "
                "AND drive_file_id IS NOT NULL",
                (lecture_id,),
            ).fetchone()
        if row is None or any(
            row[key] is None for key in ("source_bytes", "source_sha256", "source_md5")
        ):
            return True
        descriptor = recording.get("descriptor")
        if not isinstance(descriptor, int) or isinstance(descriptor, bool):
            return False
        sha256 = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                sha256.update(block)
                md5.update(block)
            return bool(
                recording.get("bytes") == row["source_bytes"]
                and hmac.compare_digest(sha256.hexdigest(), row["source_sha256"])
                and hmac.compare_digest(md5.hexdigest(), row["source_md5"])
            )
        except OSError:
            return False
        finally:
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
            except OSError:
                pass

    def _worker_main(self) -> None:
        while not self.worker_shutdown.is_set():
            self.worker_wake.clear()
            try:
                result = self.run_once(delete_local=True)
            except Exception:
                # Never log provider text, credentials, paths, titles, account
                # IDs, file IDs, or resumable session capabilities.
                result = None
            if result is None:
                self.worker_wake.wait(1.0)

    def start(self) -> None:
        with self.worker_lock:
            if self.worker_thread and self.worker_thread.is_alive():
                return
            self.worker_shutdown.clear()
            self.worker_thread = threading.Thread(
                target=self._worker_main,
                name="google-drive-recording-archive",
                daemon=True,
            )
            self.worker_thread.start()

    def request_shutdown(self) -> None:
        self.worker_shutdown.set()
        self.worker_wake.set()

    def stop(self, *, timeout: float = 10.0) -> bool:
        self.request_shutdown()
        with self.worker_lock:
            worker = self.worker_thread
        if worker and worker.is_alive():
            worker.join(timeout=max(0.0, timeout))
        return not worker or not worker.is_alive()


def _manager(settings: Settings) -> DriveArchiveManager:
    database = _database(settings)
    store = _recording_store(settings)
    return DriveArchiveManager(settings, database, store)


def plan_existing_recordings(settings: Settings, *, limit: int | None = None) -> dict[str, int]:
    """Inspect migration candidates without initializing or changing storage."""

    database_path = settings.database_path
    try:
        details = database_path.lstat()
    except OSError as error:
        raise RuntimeError("Private classroom database is unavailable") from error
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
    ):
        raise RuntimeError("Private classroom database is unsafe")
    uri = f"file:{quote(str(database_path.resolve()), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as error:
        raise RuntimeError("Private classroom database cannot be opened read-only") from error
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        users = {
            row[0] for row in connection.execute("SELECT username FROM users").fetchall()
        }
        if users != set(settings.accounts):
            raise RuntimeError("Configured accounts do not match the private database")
        lecture_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(lectures)").fetchall()
        }
        required = {"id", "username", "created_at", "recording_finalized", "deleting"}
        if not required.issubset(lecture_columns):
            raise RuntimeError("Classroom database must be upgraded before Drive planning")
        has_archives = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'recording_archives'"
        ).fetchone()
        archive_columns = (
            set()
            if has_archives is None
            else {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(recording_archives)"
                ).fetchall()
            }
        )
        if has_archives is None:
            query = (
                "SELECT id, username, NULL AS state, 0 AS folder_layout_version FROM lectures "
                "WHERE recording_finalized = 1 AND deleting = 0 "
                "ORDER BY created_at, id"
            )
        else:
            layout_value = (
                "a.folder_layout_version"
                if "folder_layout_version" in archive_columns
                else "0"
            )
            query = (
                "SELECT l.id, l.username, a.state, "
                + layout_value
                + " AS folder_layout_version FROM lectures AS l "
                "LEFT JOIN recording_archives AS a ON a.lecture_id = l.id "
                "WHERE l.recording_finalized = 1 AND l.deleting = 0 "
                "ORDER BY l.created_at, l.id"
            )
        rows = [dict(row) for row in connection.execute(query).fetchall()]
    except sqlite3.Error as error:
        raise RuntimeError("Private classroom database cannot be inspected safely") from error
    finally:
        connection.close()
    store = _recording_store(settings, create_directories=False)
    candidates = 0
    candidate_bytes = 0
    already_ready = 0
    organization_pending = 0
    attention = 0
    for row in rows:
        if row["state"] == "ready":
            if int(row["folder_layout_version"] or 0) >= _FOLDER_LAYOUT_VERSION:
                already_ready += 1
            else:
                organization_pending += 1
            continue
        if row["state"] == "attention":
            attention += 1
            continue
        try:
            info = store.info(row["username"], row["id"])
        except RecordingCorruptError:
            attention += 1
            continue
        if info is None:
            continue
        if limit is not None and candidates >= limit:
            break
        candidates += 1
        candidate_bytes += int(info["bytes"])
    return {
        "candidate_count": candidates,
        "candidate_bytes": candidate_bytes,
        "already_ready_count": already_ready,
        "organization_pending_count": organization_pending,
        "attention_count": attention,
    }


def enqueue_existing_recordings(settings: Settings, *, limit: int | None = None) -> dict[str, int]:
    manager = _manager(settings)
    try:
        return manager.enqueue_existing(limit=limit)
    finally:
        manager.close()


def archive_status(settings: Settings) -> dict[str, Any]:
    database = _database(settings)
    store = _recording_store(settings)
    with database.connect() as connection:
        counts = {
            row["state"]: row["count"]
            for row in connection.execute(
                "SELECT state, COUNT(*) AS count FROM recording_archives GROUP BY state"
            ).fetchall()
        }
        owners = [
            dict(row)
            for row in connection.execute(
                "SELECT id, username FROM lectures"
            ).fetchall()
        ]
        deleting_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM lectures WHERE deleting = 1"
            ).fetchone()[0]
        )
        organization_pending_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM recording_archives "
                "WHERE state = 'ready' AND folder_layout_version < ?",
                (_FOLDER_LAYOUT_VERSION,),
            ).fetchone()[0]
        )
    local_count = 0
    local_bytes = 0
    for row in owners:
        try:
            info = store.info(row["username"], row["id"])
        except RecordingCorruptError:
            continue
        if info is not None:
            local_count += 1
            local_bytes += int(info["bytes"])
    configured = bool(settings.google_drive_enabled and _token_path(settings).is_file())
    connected = False
    if configured:
        try:
            manager = DriveArchiveManager(settings, database, store)
            try:
                connected = manager.verify_connection()
            finally:
                manager.close()
        except Exception:
            connected = False
    return {
        "configured": configured,
        "connected": connected,
        "pending_count": counts.get("pending", 0),
        "uploading_count": counts.get("uploading", 0),
        "ready_count": counts.get("ready", 0),
        "organization_pending_count": organization_pending_count,
        "attention_count": counts.get("attention", 0),
        "retrying_count": connection_retry_count(database),
        "deleting_count": deleting_count,
        "local_count": local_count,
        "local_bytes": local_bytes,
    }


def connection_retry_count(database: Database) -> int:
    with database.connect() as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM recording_archives "
                "WHERE last_error_code IS NOT NULL AND (state = 'pending' OR "
                "(state = 'ready' AND folder_layout_version < ?))",
                (_FOLDER_LAYOUT_VERSION,),
            ).fetchone()[0]
        )


def run_archive_until_idle(
    settings: Settings,
    *,
    delete_local: bool = True,
    limit: int | None = None,
) -> dict[str, int]:
    manager = _manager(settings)
    migrated = 0
    deleted = 0
    failed = 0
    operations = 0
    try:
        manager.recover(enqueue_existing=False)
        while limit is None or operations < limit:
            result = manager.run_once(delete_local=delete_local)
            if result is None:
                break
            operations += 1
            migrated += result["migrated_count"]
            deleted += result["deleted_local_count"]
            failed += result["failed_count"]
        with manager.database.connect() as connection:
            remaining = int(
                connection.execute(
                    "SELECT COUNT(*) FROM recording_archives "
                    "WHERE state != 'ready' OR folder_layout_version < ?",
                    (_FOLDER_LAYOUT_VERSION,),
                ).fetchone()[0]
            )
        return {
            "migrated_count": migrated,
            "deleted_local_count": deleted,
            "failed_count": failed,
            "remaining_count": remaining,
        }
    finally:
        manager.close()
