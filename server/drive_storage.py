from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import parse_qs, urlsplit

import httpx


DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API_ROOT = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"

_METADATA_FIELDS = (
    "id,name,mimeType,size,md5Checksum,sha256Checksum,trashed,"
    "createdTime,modifiedTime,version,parents,appProperties"
)
_RECORDING_SCHEMA = "stt-recording-v1"
_FOLDER_SCHEMA = "stt-archive-folder-v1"
_CHECKPOINT_SCHEMA = 1
_CHUNK_GRANULARITY = 256 * 1024
_DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024
_MAX_JSON_BYTES = 1024 * 1024
_MAX_TOKEN_JSON_BYTES = 64 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")
_OBJECT_KEY = re.compile(r"[0-9a-f]{64}")
_UPLOAD_RANGE = re.compile(r"bytes=0-(\d+)")
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


class DriveStorageError(RuntimeError):
    """A redacted Google Drive storage error safe to show or persist."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class DriveConfigurationError(DriveStorageError):
    """Local OAuth or Drive configuration is invalid or unsafe."""


class DriveAuthenticationError(DriveStorageError):
    """Google rejected or can no longer refresh the OAuth credential."""


class DriveTransportError(DriveStorageError):
    """A retryable network or remote service failure occurred."""


class DriveProtocolError(DriveStorageError):
    """Google returned a response that cannot be used safely."""


class DriveNotFoundError(DriveStorageError):
    """An expected Drive object does not exist or is inaccessible."""


class DriveConflictError(DriveStorageError):
    """More than one Drive object claims the same application identity."""


class DriveIntegrityError(DriveStorageError):
    """Remote size or checksums do not match the finalized local file."""


class DriveUploadSessionExpired(DriveStorageError):
    """A resumable upload session expired before it could be completed."""


def _open_private_directory(path: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise DriveConfigurationError(
            "credential_directory", "Google Drive credential directory is unsafe."
        ) from None
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise DriveConfigurationError(
                "credential_directory",
                "Google Drive credential directory must be private and owner-controlled.",
            )
        return descriptor
    except OSError:
        os.close(descriptor)
        raise DriveConfigurationError(
            "credential_directory", "Google Drive credential directory is unsafe."
        ) from None
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _private_token_file_lock(token_path: Path) -> Iterator[None]:
    """Coordinate token replacement with the local OAuth setup command."""

    if token_path.name in {"", ".", "..", "token.lock"}:
        raise DriveConfigurationError(
            "credential_path", "Google Drive OAuth token path is invalid."
        )
    directory_descriptor = _open_private_directory(token_path.parent)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        try:
            descriptor = os.open("token.lock", flags, 0o600, dir_fd=directory_descriptor)
        except OSError:
            raise DriveConfigurationError(
                "credential_lock", "Google Drive OAuth token lock is unsafe."
            ) from None
    finally:
        os.close(directory_descriptor)
    try:
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid():
                raise DriveConfigurationError(
                    "credential_lock", "Google Drive OAuth token lock is unsafe."
                )
            os.fchmod(descriptor, 0o600)
        except DriveStorageError:
            raise
        except OSError:
            raise DriveConfigurationError(
                "credential_lock", "Google Drive OAuth token lock is unsafe."
            ) from None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError:
            raise DriveConfigurationError(
                "credential_lock", "Google Drive OAuth token lock is unavailable."
            ) from None
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class DriveFileMetadata:
    """Only metadata required by the private recording backend.

    The file ID and name are deliberately omitted from ``repr`` because an
    exception or routine debug print must not disclose a Drive locator or a
    lecture title.
    """

    file_id: str = field(repr=False)
    name: str = field(repr=False)
    mime_type: str
    size: int | None
    md5_checksum: str | None
    sha256_checksum: str | None
    trashed: bool
    created_time: str | None = None
    modified_time: str | None = None
    version: str | None = None
    parents: tuple[str, ...] = field(default_factory=tuple, repr=False)
    app_properties: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class DriveAccountIdentity:
    """Opaque identifiers used only to pin one archive credential locally."""

    permission_id: str = field(repr=False)
    oauth_client_fingerprint: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.permission_id, str)
            or not self.permission_id
            or len(self.permission_id) > 256
            or any(
                character.isspace() or ord(character) < 0x20
                for character in self.permission_id
            )
            or not isinstance(self.oauth_client_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.oauth_client_fingerprint) is None
        ):
            raise DriveProtocolError(
                "invalid_account_identity",
                "Google Drive returned an invalid account identity.",
            )


@dataclass(frozen=True)
class UploadCheckpoint:
    """Durable state for resuming a Drive upload after process interruption.

    ``session_uri`` is a bearer-like capability and must only be persisted in
    the private local database or a mode-0600 file.  It is never represented.
    """

    session_uri: str = field(repr=False)
    object_key: str
    total_size: int
    sha256_checksum: str
    md5_checksum: str
    committed_bytes: int = 0

    def __post_init__(self) -> None:
        _validate_session_uri(self.session_uri)
        _validate_object_key(self.object_key)
        if self.total_size <= 0:
            raise DriveConfigurationError(
                "invalid_checkpoint", "Upload checkpoint has an invalid file size."
            )
        if not 0 <= self.committed_bytes <= self.total_size:
            raise DriveConfigurationError(
                "invalid_checkpoint", "Upload checkpoint has an invalid offset."
            )
        if re.fullmatch(r"[0-9a-f]{64}", self.sha256_checksum) is None:
            raise DriveConfigurationError(
                "invalid_checkpoint", "Upload checkpoint has an invalid SHA-256."
            )
        if re.fullmatch(r"[0-9a-f]{32}", self.md5_checksum) is None:
            raise DriveConfigurationError(
                "invalid_checkpoint", "Upload checkpoint has an invalid MD5."
            )

    def to_dict(self) -> dict[str, object]:
        """Serialize for private durable storage, never for logs or clients."""

        return {
            "schema": _CHECKPOINT_SCHEMA,
            "session_uri": self.session_uri,
            "object_key": self.object_key,
            "total_size": self.total_size,
            "sha256_checksum": self.sha256_checksum,
            "md5_checksum": self.md5_checksum,
            "committed_bytes": self.committed_bytes,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "UploadCheckpoint":
        try:
            if value.get("schema") != _CHECKPOINT_SCHEMA:
                raise ValueError
            return cls(
                session_uri=str(value["session_uri"]),
                object_key=str(value["object_key"]),
                total_size=int(value["total_size"]),
                sha256_checksum=str(value["sha256_checksum"]),
                md5_checksum=str(value["md5_checksum"]),
                committed_bytes=int(value["committed_bytes"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DriveConfigurationError(
                "invalid_checkpoint", "Upload checkpoint is malformed."
            ) from None


@dataclass(frozen=True)
class _SourceFingerprint:
    size: int
    sha256_checksum: str
    md5_checksum: str
    device: int
    inode: int
    modified_ns: int


class DriveDownloadStream:
    """A validated, closeable Drive media response suitable for ASGI streaming."""

    def __init__(
        self,
        response: httpx.Response,
        *,
        file: DriveFileMetadata,
        start: int,
        end_exclusive: int,
        status_code: int,
    ):
        self._response = response
        self.file = file
        self.start = start
        self.end_exclusive = end_exclusive
        self.total_size = file.size or 0
        self.status_code = status_code
        self.content_length = end_exclusive - start
        self.content_range = (
            f"bytes {start}-{end_exclusive - 1}/{self.total_size}"
            if status_code == 206
            else None
        )
        self.content_type = response.headers.get("content-type") or file.mime_type
        self.etag = response.headers.get("etag")
        self._iterated = False
        self._closed = False

    def iter_bytes(self, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
        if self._iterated:
            raise DriveProtocolError(
                "download_already_consumed", "Drive download stream was already consumed."
            )
        self._iterated = True
        received = 0
        try:
            if chunk_size <= 0:
                raise ValueError("chunk_size must be positive")
            for chunk in self._response.iter_raw(chunk_size):
                received += len(chunk)
                if received > self.content_length:
                    raise DriveIntegrityError(
                        "download_size_mismatch", "Drive returned too much recording data."
                    )
                if chunk:
                    yield chunk
            if received != self.content_length:
                raise DriveIntegrityError(
                    "download_size_mismatch", "Drive returned an incomplete recording."
                )
        except httpx.HTTPError:
            raise DriveTransportError(
                "download_interrupted", "Drive download was interrupted.", retryable=True
            ) from None
        finally:
            self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._response.close()

    def __enter__(self) -> "DriveDownloadStream":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def derive_object_key(deployment_key: bytes, owner_id: str, lecture_id: str) -> str:
    """Derive an opaque stable appProperty without placing an account ID in Drive."""

    if len(deployment_key) < 32:
        raise DriveConfigurationError(
            "invalid_identity_key", "Drive identity key must contain at least 32 bytes."
        )
    try:
        normalized_lecture = str(uuid.UUID(lecture_id))
    except (AttributeError, ValueError):
        raise DriveConfigurationError(
            "invalid_recording_identity", "Recording identity is invalid."
        ) from None
    if not owner_id or len(owner_id.encode("utf-8")) > 256 or "\0" in owner_id:
        raise DriveConfigurationError(
            "invalid_recording_identity", "Recording owner identity is invalid."
        )
    message = f"{owner_id}\0{normalized_lecture}".encode("utf-8")
    return hmac.new(deployment_key, message, hashlib.sha256).hexdigest()


class _AuthorizedUserToken:
    def __init__(
        self,
        path: Path,
        client: httpx.Client,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.path = Path(path)
        self.client = client
        self.clock = clock
        self.lock = threading.Lock()
        self._rejected_access_token: str | None = None
        with _private_token_file_lock(self.path):
            self._set_cached_data(self._read_locked())

    def _read_locked(self) -> dict[str, Any]:
        directory_descriptor = _open_private_directory(self.path.parent)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            try:
                descriptor = os.open(self.path.name, flags, dir_fd=directory_descriptor)
            except OSError:
                raise DriveConfigurationError(
                    "credential_unavailable", "Google Drive OAuth token file cannot be opened."
                ) from None
        finally:
            os.close(directory_descriptor)
        try:
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or not 0 < details.st_size <= _MAX_TOKEN_JSON_BYTES
            ):
                raise DriveConfigurationError(
                    "credential_invalid", "Google Drive OAuth token file is invalid."
                )
            if stat.S_IMODE(details.st_mode) != 0o600:
                raise DriveConfigurationError(
                    "credential_permissions",
                    "Google Drive OAuth token file must have mode 0600.",
                )
            content = bytearray()
            remaining = details.st_size
            while remaining:
                chunk = os.read(descriptor, min(16 * 1024, remaining))
                if not chunk:
                    break
                content.extend(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if (
                remaining
                or os.read(descriptor, 1)
                or after.st_dev != details.st_dev
                or after.st_ino != details.st_ino
                or after.st_size != details.st_size
                or after.st_mtime_ns != details.st_mtime_ns
            ):
                raise DriveConfigurationError(
                    "credential_invalid", "Google Drive OAuth token changed while being read."
                )
        except DriveStorageError:
            raise
        except OSError:
            raise DriveConfigurationError(
                "credential_unavailable", "Google Drive OAuth token file cannot be read."
            ) from None
        finally:
            os.close(descriptor)
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DriveConfigurationError(
                "credential_invalid", "Google Drive OAuth token file is malformed."
            ) from None
        if not isinstance(value, dict) or value.get("type") != "authorized_user":
            raise DriveConfigurationError(
                "credential_invalid", "Google Drive OAuth token must be an authorized user."
            )
        if value.get("token_uri") != GOOGLE_TOKEN_URL:
            raise DriveConfigurationError(
                "credential_invalid", "Google Drive OAuth token endpoint is not allowed."
            )
        scopes = value.get("scopes")
        if isinstance(scopes, str):
            granted_scopes = frozenset(scopes.split())
        elif isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes):
            granted_scopes = frozenset(scopes)
        else:
            granted_scopes = frozenset()
        if granted_scopes != {DRIVE_FILE_SCOPE}:
            raise DriveConfigurationError(
                "credential_scope", "Google Drive OAuth token must use only drive.file scope."
            )
        for key, limit in (("client_id", 4096), ("client_secret", 4096), ("refresh_token", 4096)):
            secret = value.get(key)
            if (
                not isinstance(secret, str)
                or not secret
                or len(secret) > limit
                or any(ord(character) < 0x20 for character in secret)
            ):
                raise DriveConfigurationError(
                    "credential_invalid", "Google Drive OAuth token is incomplete."
                )
        return value

    def _set_cached_data(self, value: dict[str, Any]) -> None:
        self.data = value
        self._access_token = self._optional_token(value.get("token"))
        self._expires_at = self._parse_expiry(value.get("expiry"))

    @staticmethod
    def _optional_token(value: object) -> str | None:
        if not isinstance(value, str) or not value or len(value) > 4096:
            return None
        if any(character.isspace() or ord(character) < 0x20 for character in value):
            return None
        return value

    @staticmethod
    def _parse_expiry(value: object) -> float:
        if not isinstance(value, str):
            return 0.0
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return 0.0
            return parsed.timestamp()
        except (OverflowError, ValueError):
            return 0.0

    def invalidate(self) -> None:
        with self.lock:
            self._rejected_access_token = self._access_token
            self._expires_at = 0.0

    def oauth_client_fingerprint(self) -> str:
        """Hash the client ID associated with the currently cached grant."""

        with self.lock:
            client_id = self.data["client_id"]
            return hashlib.sha256(client_id.encode("utf-8")).hexdigest()

    def access_token(self) -> str:
        with self.lock:
            if self._access_token and self._expires_at > self.clock() + 60:
                return self._access_token
            return self._refresh_locked()

    def _refresh_locked(self) -> str:
        # The OAuth setup command replaces this file while holding the same
        # advisory lock.  Reload only after acquiring it so an old server
        # process can never write its former refresh token over a new grant.
        with _private_token_file_lock(self.path):
            self._set_cached_data(self._read_locked())
            if (
                self._access_token
                and self._expires_at > self.clock() + 60
                and self._access_token != self._rejected_access_token
            ):
                self._rejected_access_token = None
                return self._access_token
            return self._request_refresh_and_write_locked()

    def _request_refresh_and_write_locked(self) -> str:
        try:
            request = self.client.build_request(
                "POST",
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.data["client_id"],
                    "client_secret": self.data["client_secret"],
                    "refresh_token": self.data["refresh_token"],
                    "grant_type": "refresh_token",
                },
                headers={"Accept": "application/json"},
            )
            response = self.client.send(request, stream=True, follow_redirects=False)
        except httpx.RequestError:
            raise DriveTransportError(
                "token_refresh_unavailable",
                "Google OAuth token refresh is temporarily unavailable.",
                retryable=True,
            ) from None
        try:
            if response.status_code != 200:
                raise DriveAuthenticationError(
                    "token_refresh_rejected", "Google OAuth authorization must be renewed."
                )
            payload = _bounded_json(response, max_bytes=_MAX_TOKEN_JSON_BYTES)
        finally:
            response.close()
        token = self._optional_token(payload.get("access_token"))
        token_type = payload.get("token_type", "Bearer")
        try:
            expires_in = int(payload["expires_in"])
        except (KeyError, TypeError, ValueError):
            expires_in = 0
        returned_scope = payload.get("scope")
        if returned_scope is not None and frozenset(str(returned_scope).split()) != {
            DRIVE_FILE_SCOPE
        }:
            token = None
        if (
            token is None
            or str(token_type).casefold() != "bearer"
            or not 0 < expires_in <= 7 * 24 * 3600
        ):
            raise DriveAuthenticationError(
                "token_refresh_invalid", "Google OAuth returned an invalid access token."
            )
        replacement_refresh_token = payload.get("refresh_token")
        if replacement_refresh_token is not None:
            if (
                not isinstance(replacement_refresh_token, str)
                or not replacement_refresh_token
                or len(replacement_refresh_token) > 4096
                or any(
                    character.isspace() or ord(character) < 0x20
                    for character in replacement_refresh_token
                )
            ):
                raise DriveAuthenticationError(
                    "token_refresh_invalid", "Google OAuth returned an invalid refresh token."
                )
            self.data["refresh_token"] = replacement_refresh_token
        self._access_token = token
        self._expires_at = self.clock() + expires_in
        self._rejected_access_token = None
        self.data["token"] = token
        self.data["expiry"] = datetime.fromtimestamp(self._expires_at, UTC).isoformat().replace(
            "+00:00", "Z"
        )
        self._write_locked()
        return token

    def _write_locked(self) -> None:
        encoded = json.dumps(self.data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_TOKEN_JSON_BYTES:
            raise DriveConfigurationError(
                "credential_invalid", "Google Drive OAuth token file is too large."
            )
        directory_descriptor = _open_private_directory(self.path.parent)
        try:
            try:
                current = os.stat(
                    self.path.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                current = None
            if (
                current is None
                or not stat.S_ISREG(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or current.st_uid != os.geteuid()
                or stat.S_IMODE(current.st_mode) != 0o600
            ):
                raise DriveConfigurationError(
                    "credential_write_failed", "Google Drive OAuth token target is unsafe."
                )
        except BaseException:
            os.close(directory_descriptor)
            raise
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = -1
        temporary_name: str | None = None
        try:
            temporary_name = f".{self.path.name}.{secrets.token_hex(12)}.tmp"
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_descriptor)
            os.fchmod(descriptor, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(
                temporary_name,
                self.path.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            temporary_name = None
            os.fsync(directory_descriptor)
        except DriveStorageError:
            raise
        except OSError:
            raise DriveConfigurationError(
                "credential_write_failed", "Refreshed Google OAuth token could not be saved."
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                except OSError:
                    pass
            os.close(directory_descriptor)


class GoogleDriveStorage:
    """Private Google Drive v3 backend for finalized WAV recordings.

    This class never deletes local files.  The caller may unlink a finalized
    recording only after this class returns verified remote metadata and the
    database transaction recording that metadata has committed.
    """

    def __init__(
        self,
        token_file: Path,
        *,
        folder_id: str | None = None,
        client: httpx.Client | None = None,
        chunk_size: int = _DEFAULT_CHUNK_BYTES,
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ):
        if folder_id is not None:
            _validate_drive_id(folder_id)
        if chunk_size <= 0 or chunk_size % _CHUNK_GRANULARITY:
            raise DriveConfigurationError(
                "invalid_chunk_size", "Drive upload chunk size must be a multiple of 256 KiB."
            )
        if (
            not 1 <= connect_timeout_seconds <= 60
            or not 5 <= read_timeout_seconds <= 600
        ):
            raise DriveConfigurationError(
                "invalid_timeout", "Google Drive timeout configuration is invalid."
            )
        self.folder_id = folder_id
        self.chunk_size = chunk_size
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
                write=read_timeout_seconds,
                pool=connect_timeout_seconds,
            ),
            follow_redirects=False,
            trust_env=False,
        )
        try:
            self._credentials = _AuthorizedUserToken(
                Path(token_file), self.client, clock=clock
            )
        except BaseException:
            if self._owns_client:
                self.client.close()
            raise

    @classmethod
    def from_token_file(
        cls,
        token_file: Path,
        *,
        folder_id: str | None = None,
        client: httpx.Client | None = None,
        chunk_size: int = _DEFAULT_CHUNK_BYTES,
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> "GoogleDriveStorage":
        return cls(
            token_file,
            folder_id=folder_id,
            client=client,
            chunk_size=chunk_size,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            clock=clock,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "GoogleDriveStorage":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def verify_connection(self) -> bool:
        self._request_json(
            "GET",
            f"{DRIVE_API_ROOT}/files",
            params={"pageSize": "1", "spaces": "drive", "fields": "files(id)"},
            expected={200},
            operation="verify_connection",
        )
        if self.folder_id is not None:
            folder = self.get_metadata(self.folder_id)
            if folder.trashed or folder.mime_type != "application/vnd.google-apps.folder":
                raise DriveConfigurationError(
                    "invalid_folder", "Configured Google Drive folder is unavailable."
                )
        return True

    def account_identity(self) -> DriveAccountIdentity:
        """Return an opaque stable user/app pair without requesting an email."""

        payload = self._request_json(
            "GET",
            f"{DRIVE_API_ROOT}/about",
            params={"fields": "user(permissionId)"},
            expected={200},
            operation="account_identity",
        )
        user = payload.get("user")
        if not isinstance(user, dict):
            raise DriveProtocolError(
                "invalid_account_identity",
                "Google Drive returned an invalid account identity.",
            )
        return DriveAccountIdentity(
            permission_id=user.get("permissionId"),
            oauth_client_fingerprint=self._credentials.oauth_client_fingerprint(),
        )

    def ensure_folder(
        self,
        *,
        object_key: str,
        name: str,
        parent_id: str | None = None,
    ) -> DriveFileMetadata:
        """Find or create one app-owned archive folder.

        ``object_key`` should be derived from deployment-local random key
        material, not from an account name or a lecture title.  A retry after
        an uncertain create response searches the private appProperty first,
        which reconciles a remotely completed request without another folder.
        """

        _validate_object_key(object_key)
        _validate_name(name)
        if parent_id is not None:
            _validate_drive_id(parent_id)
        query = (
            "trashed = false and "
            f"appProperties has {{ key='stt_schema' and value='{_FOLDER_SCHEMA}' }} and "
            f"appProperties has {{ key='stt_object' and value='{object_key}' }}"
        )
        matches = self._find_by_query(query, operation="find_folder")
        if len(matches) > 1:
            raise DriveConflictError(
                "duplicate_archive_folder",
                "Multiple Drive folders claim the same archive identity.",
            )
        if matches:
            folder = matches[0]
            if (
                folder.trashed
                or folder.mime_type != "application/vnd.google-apps.folder"
                or folder.app_properties.get("stt_schema") != _FOLDER_SCHEMA
                or folder.app_properties.get("stt_object") != object_key
                or (parent_id is not None and folder.parents != (parent_id,))
            ):
                raise DriveIntegrityError(
                    "archive_folder_mismatch", "Drive archive folder metadata is invalid."
                )
            return folder
        body: dict[str, object] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "appProperties": {
                "stt_schema": _FOLDER_SCHEMA,
                "stt_object": object_key,
            },
        }
        if parent_id is not None:
            body["parents"] = [parent_id]
        payload = self._request_json(
            "POST",
            f"{DRIVE_API_ROOT}/files",
            params={"fields": _METADATA_FIELDS},
            json_value=body,
            expected={200, 201},
            operation="create_folder",
        )
        folder = _parse_metadata(payload)
        if (
            folder.mime_type != "application/vnd.google-apps.folder"
            or folder.trashed
            or folder.app_properties.get("stt_schema") != _FOLDER_SCHEMA
            or folder.app_properties.get("stt_object") != object_key
            or (parent_id is not None and folder.parents != (parent_id,))
        ):
            raise DriveIntegrityError(
                "archive_folder_mismatch", "Drive did not confirm the archive folder."
            )
        return folder

    def find_recording(
        self,
        object_key: str,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        expected_md5: str | None = None,
    ) -> DriveFileMetadata | None:
        _validate_object_key(object_key)
        query = (
            "trashed = false and "
            f"appProperties has {{ key='stt_schema' and value='{_RECORDING_SCHEMA}' }} and "
            f"appProperties has {{ key='stt_object' and value='{object_key}' }}"
        )
        files = self._find_by_query(query, operation="find_recording")
        if len(files) > 1:
            raise DriveConflictError(
                "duplicate_recording", "Multiple Drive files claim the same recording identity."
            )
        if not files:
            return None
        metadata = files[0]
        if any(value is not None for value in (expected_size, expected_sha256, expected_md5)):
            self._verify_metadata(
                metadata,
                object_key=object_key,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                expected_md5=expected_md5,
            )
        return metadata

    def _find_by_query(
        self, query: str, *, operation: str
    ) -> list[DriveFileMetadata]:
        files: list[DriveFileMetadata] = []
        page_token: str | None = None
        for _ in range(10):
            params = {
                "q": query,
                "spaces": "drive",
                "pageSize": "100",
                "fields": f"nextPageToken,files({_METADATA_FIELDS})",
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._request_json(
                "GET",
                f"{DRIVE_API_ROOT}/files",
                params=params,
                expected={200},
                operation=operation,
            )
            raw_files = payload.get("files")
            if not isinstance(raw_files, list):
                raise DriveProtocolError(
                    "invalid_metadata", "Drive returned invalid file search metadata."
                )
            files.extend(_parse_metadata(item) for item in raw_files)
            next_token = payload.get("nextPageToken")
            if next_token is None:
                break
            if not isinstance(next_token, str) or not next_token or len(next_token) > 4096:
                raise DriveProtocolError(
                    "invalid_metadata", "Drive returned an invalid search cursor."
                )
            page_token = next_token
        else:
            raise DriveConflictError(
                "too_many_matches", "Drive recording reconciliation exceeded its safe limit."
            )
        return files

    def get_metadata(self, file_id: str) -> DriveFileMetadata:
        _validate_drive_id(file_id)
        payload = self._request_json(
            "GET",
            f"{DRIVE_API_ROOT}/files/{file_id}",
            params={"fields": _METADATA_FIELDS},
            expected={200},
            operation="get_metadata",
        )
        return _parse_metadata(payload)

    def upload_recording(
        self,
        path: Path,
        *,
        object_key: str,
        name: str,
        parent_id: str | None = None,
        checkpoint: UploadCheckpoint | None = None,
        on_checkpoint: Callable[[UploadCheckpoint], None] | None = None,
    ) -> DriveFileMetadata:
        _validate_object_key(object_key)
        _validate_name(name)
        if parent_id is not None:
            _validate_drive_id(parent_id)
        descriptor = self._open_source(Path(path))
        try:
            fingerprint = self._fingerprint(descriptor)
            existing = self.find_recording(
                object_key,
                expected_size=fingerprint.size,
                expected_sha256=fingerprint.sha256_checksum,
                expected_md5=fingerprint.md5_checksum,
            )
            if existing is not None:
                self._ensure_source_unchanged(descriptor, fingerprint)
                return existing

            current = checkpoint
            if current is not None:
                self._validate_checkpoint(current, object_key, fingerprint)
                try:
                    current, completed = self._query_upload(current)
                except DriveUploadSessionExpired:
                    # A completion response may have been lost.  Search once
                    # more before replacing an expired session.
                    existing = self.find_recording(
                        object_key,
                        expected_size=fingerprint.size,
                        expected_sha256=fingerprint.sha256_checksum,
                        expected_md5=fingerprint.md5_checksum,
                    )
                    if existing is not None:
                        self._ensure_source_unchanged(descriptor, fingerprint)
                        return existing
                    current = None
                    completed = None
                if current is not None and on_checkpoint is not None:
                    on_checkpoint(current)
                if completed is not None:
                    verified = self._verified_remote(
                        completed.file_id, object_key, fingerprint
                    )
                    self._ensure_source_unchanged(descriptor, fingerprint)
                    return verified

            if current is None:
                current = self._begin_upload(
                    object_key=object_key,
                    name=name,
                    fingerprint=fingerprint,
                    parent_id=parent_id,
                )
                if on_checkpoint is not None:
                    on_checkpoint(current)

            metadata = self._upload_remaining(
                descriptor,
                checkpoint=current,
                on_checkpoint=on_checkpoint,
            )
            self._ensure_source_unchanged(descriptor, fingerprint)
            verified = self._verified_remote(metadata.file_id, object_key, fingerprint)
            self._ensure_source_unchanged(descriptor, fingerprint)
            return verified
        finally:
            os.close(descriptor)

    def reconcile_upload_session(
        self, checkpoint: UploadCheckpoint
    ) -> DriveFileMetadata | None:
        """Query one saved session without creating or advancing an upload."""

        _, metadata = self._query_upload(checkpoint)
        if metadata is None:
            return None
        self._verify_metadata(
            metadata,
            object_key=checkpoint.object_key,
            expected_size=checkpoint.total_size,
            expected_sha256=checkpoint.sha256_checksum,
            expected_md5=checkpoint.md5_checksum,
            allow_trashed=True,
        )
        return metadata

    def open_download(
        self,
        file_id: str,
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> DriveDownloadStream:
        metadata = self.get_metadata(file_id)
        if metadata.trashed or metadata.size is None or metadata.size <= 0:
            raise DriveNotFoundError(
                "recording_unavailable", "Drive recording is unavailable."
            )
        total = metadata.size
        first = 0 if start is None else start
        last_exclusive = total if end is None else end
        if (
            isinstance(first, bool)
            or isinstance(last_exclusive, bool)
            or not isinstance(first, int)
            or not isinstance(last_exclusive, int)
            or first < 0
            or first >= total
            or last_exclusive <= first
            or last_exclusive > total
        ):
            raise DriveConfigurationError(
                "invalid_range", "Requested Drive recording range is invalid."
            )
        # A syntactically present HTTP Range request remains a 206 even when
        # it happens to cover the whole file.  Callers signal that distinction
        # by passing either boundary; omitting both requests a normal 200.
        partial = start is not None or end is not None
        headers = {"Accept-Encoding": "identity"}
        if partial:
            headers["Range"] = f"bytes={first}-{last_exclusive - 1}"
        response = self._send(
            "GET",
            f"{DRIVE_API_ROOT}/files/{file_id}",
            params={"alt": "media"},
            headers=headers,
            stream=True,
        )
        expected_status = 206 if partial else 200
        if response.status_code != expected_status:
            try:
                self._raise_response_error(response, "download")
            finally:
                response.close()
            raise DriveProtocolError(
                "invalid_download_response", "Drive returned an invalid download status."
            )
        try:
            encoding = response.headers.get("content-encoding")
            if encoding and encoding.casefold() != "identity":
                raise DriveProtocolError(
                    "invalid_download_response", "Drive returned encoded recording data."
                )
            length = _header_integer(response, "content-length")
            expected_length = last_exclusive - first
            if length != expected_length:
                raise DriveIntegrityError(
                    "download_size_mismatch", "Drive returned an invalid recording length."
                )
            if partial:
                content_range = response.headers.get("content-range", "")
                match = _CONTENT_RANGE.fullmatch(content_range)
                if (
                    match is None
                    or tuple(map(int, match.groups()))
                    != (first, last_exclusive - 1, total)
                ):
                    raise DriveIntegrityError(
                        "download_range_mismatch", "Drive returned a different recording range."
                    )
        except BaseException:
            response.close()
            raise
        return DriveDownloadStream(
            response,
            file=metadata,
            start=first,
            end_exclusive=last_exclusive,
            status_code=response.status_code,
        )

    def trash(self, file_id: str) -> DriveFileMetadata:
        _validate_drive_id(file_id)
        payload = self._request_json(
            "PATCH",
            f"{DRIVE_API_ROOT}/files/{file_id}",
            params={"fields": _METADATA_FIELDS},
            json_value={"trashed": True},
            expected={200},
            operation="trash",
        )
        metadata = _parse_metadata(payload)
        if metadata.file_id != file_id or not metadata.trashed:
            raise DriveProtocolError(
                "trash_not_confirmed", "Drive did not confirm moving the recording to trash."
            )
        return metadata

    def move_file(
        self,
        file_id: str,
        *,
        previous_parent_id: str,
        new_parent_id: str,
    ) -> DriveFileMetadata:
        """Move one app-owned file and require an exact single-parent result."""

        _validate_drive_id(file_id)
        _validate_drive_id(previous_parent_id)
        _validate_drive_id(new_parent_id)
        if previous_parent_id == new_parent_id:
            raise DriveConfigurationError(
                "invalid_folder_move", "Google Drive folder move is invalid."
            )
        payload = self._request_json(
            "PATCH",
            f"{DRIVE_API_ROOT}/files/{file_id}",
            params={
                "addParents": new_parent_id,
                "removeParents": previous_parent_id,
                "fields": _METADATA_FIELDS,
            },
            json_value={},
            expected={200},
            operation="move_file",
        )
        metadata = _parse_metadata(payload)
        if metadata.file_id != file_id or metadata.parents != (new_parent_id,):
            raise DriveIntegrityError(
                "folder_move_not_confirmed",
                "Drive did not confirm the recording folder move.",
            )
        return metadata

    @staticmethod
    def _open_source(path: Path) -> int:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError:
            raise DriveConfigurationError(
                "source_unavailable", "Finalized recording cannot be opened safely."
            ) from None
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size <= 0:
            os.close(descriptor)
            raise DriveConfigurationError(
                "source_invalid", "Finalized recording is not a non-empty regular file."
            )
        return descriptor

    @staticmethod
    def _fingerprint(descriptor: int) -> _SourceFingerprint:
        before = os.fstat(descriptor)
        sha256 = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            sha256.update(chunk)
            md5.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise DriveIntegrityError(
                "source_changed", "Finalized recording changed while it was being hashed."
            )
        return _SourceFingerprint(
            size=after.st_size,
            sha256_checksum=sha256.hexdigest(),
            md5_checksum=md5.hexdigest(),
            device=after.st_dev,
            inode=after.st_ino,
            modified_ns=after.st_mtime_ns,
        )

    @staticmethod
    def _ensure_source_unchanged(descriptor: int, fingerprint: _SourceFingerprint) -> None:
        details = os.fstat(descriptor)
        if (
            details.st_dev != fingerprint.device
            or details.st_ino != fingerprint.inode
            or details.st_size != fingerprint.size
            or details.st_mtime_ns != fingerprint.modified_ns
        ):
            raise DriveIntegrityError(
                "source_changed", "Finalized recording changed during Drive upload."
            )

    @staticmethod
    def _validate_checkpoint(
        checkpoint: UploadCheckpoint,
        object_key: str,
        fingerprint: _SourceFingerprint,
    ) -> None:
        if (
            checkpoint.object_key != object_key
            or checkpoint.total_size != fingerprint.size
            or not hmac.compare_digest(
                checkpoint.sha256_checksum, fingerprint.sha256_checksum
            )
            or not hmac.compare_digest(checkpoint.md5_checksum, fingerprint.md5_checksum)
        ):
            raise DriveConflictError(
                "checkpoint_mismatch", "Upload checkpoint belongs to a different recording."
            )

    def _begin_upload(
        self,
        *,
        object_key: str,
        name: str,
        fingerprint: _SourceFingerprint,
        parent_id: str | None,
    ) -> UploadCheckpoint:
        app_properties = {
            "stt_schema": _RECORDING_SCHEMA,
            "stt_object": object_key,
            "stt_size": str(fingerprint.size),
            "stt_sha256": fingerprint.sha256_checksum,
            "stt_md5": fingerprint.md5_checksum,
        }
        body: dict[str, object] = {
            "name": name,
            "mimeType": "audio/wav",
            "appProperties": app_properties,
        }
        target_parent = parent_id if parent_id is not None else self.folder_id
        if target_parent is not None:
            body["parents"] = [target_parent]
        response = self._send(
            "POST",
            DRIVE_UPLOAD_URL,
            params={"uploadType": "resumable", "fields": _METADATA_FIELDS},
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "audio/wav",
                "X-Upload-Content-Length": str(fingerprint.size),
            },
            json_value=body,
            stream=True,
        )
        try:
            if response.status_code not in {200, 201}:
                self._raise_response_error(response, "begin_upload")
            session_uri = response.headers.get("location", "")
            _validate_session_uri(session_uri)
        finally:
            response.close()
        return UploadCheckpoint(
            session_uri=session_uri,
            object_key=object_key,
            total_size=fingerprint.size,
            sha256_checksum=fingerprint.sha256_checksum,
            md5_checksum=fingerprint.md5_checksum,
        )

    def _query_upload(
        self, checkpoint: UploadCheckpoint
    ) -> tuple[UploadCheckpoint, DriveFileMetadata | None]:
        response = self._send(
            "PUT",
            checkpoint.session_uri,
            headers={
                "Content-Length": "0",
                "Content-Range": f"bytes */{checkpoint.total_size}",
            },
            content=b"",
            stream=True,
        )
        try:
            if response.status_code in {200, 201}:
                metadata = _parse_metadata(_bounded_json(response))
                return replace(checkpoint, committed_bytes=checkpoint.total_size), metadata
            if response.status_code == 308:
                committed = _committed_bytes(response, checkpoint.total_size)
                return replace(checkpoint, committed_bytes=committed), None
            if response.status_code in {404, 410}:
                raise DriveUploadSessionExpired(
                    "upload_session_expired",
                    "Google Drive upload session expired and must be replaced.",
                    retryable=True,
                )
            self._raise_response_error(response, "query_upload")
            raise AssertionError("unreachable")
        finally:
            response.close()

    def _upload_remaining(
        self,
        descriptor: int,
        *,
        checkpoint: UploadCheckpoint,
        on_checkpoint: Callable[[UploadCheckpoint], None] | None,
    ) -> DriveFileMetadata:
        current = checkpoint
        while current.committed_bytes < current.total_size:
            start = current.committed_bytes
            length = min(self.chunk_size, current.total_size - start)
            os.lseek(descriptor, start, os.SEEK_SET)
            content = bytearray()
            while len(content) < length:
                part = os.read(descriptor, length - len(content))
                if not part:
                    raise DriveIntegrityError(
                        "source_changed", "Finalized recording became shorter during upload."
                    )
                content.extend(part)
            end = start + length
            response = self._send(
                "PUT",
                current.session_uri,
                headers={
                    "Content-Type": "audio/wav",
                    "Content-Length": str(length),
                    "Content-Range": f"bytes {start}-{end - 1}/{current.total_size}",
                },
                content=bytes(content),
                stream=True,
            )
            try:
                if response.status_code in {200, 201}:
                    if end != current.total_size:
                        raise DriveProtocolError(
                            "upload_completed_early",
                            "Drive completed an incomplete recording upload.",
                        )
                    metadata = _parse_metadata(_bounded_json(response))
                    current = replace(current, committed_bytes=current.total_size)
                    if on_checkpoint is not None:
                        on_checkpoint(current)
                    return metadata
                if response.status_code == 308:
                    committed = _committed_bytes(response, current.total_size)
                    if committed == start:
                        raise DriveTransportError(
                            "upload_no_progress",
                            "Drive did not acknowledge recording upload progress.",
                            retryable=True,
                        )
                    if committed < start or committed > end:
                        raise DriveProtocolError(
                            "upload_progress_invalid", "Drive returned invalid upload progress."
                        )
                    current = replace(current, committed_bytes=committed)
                    if on_checkpoint is not None:
                        on_checkpoint(current)
                    continue
                if response.status_code in {404, 410}:
                    raise DriveUploadSessionExpired(
                        "upload_session_expired",
                        "Google Drive upload session expired and must be replaced.",
                        retryable=True,
                    )
                self._raise_response_error(response, "upload_chunk")
                raise AssertionError("unreachable")
            finally:
                response.close()
        raise DriveProtocolError(
            "upload_completion_missing", "Drive upload reached its end without metadata."
        )

    def _verified_remote(
        self,
        file_id: str,
        object_key: str,
        fingerprint: _SourceFingerprint,
    ) -> DriveFileMetadata:
        metadata = self.get_metadata(file_id)
        self._verify_metadata(
            metadata,
            object_key=object_key,
            expected_size=fingerprint.size,
            expected_sha256=fingerprint.sha256_checksum,
            expected_md5=fingerprint.md5_checksum,
        )
        return metadata

    @staticmethod
    def _verify_metadata(
        metadata: DriveFileMetadata,
        *,
        object_key: str,
        expected_size: int | None,
        expected_sha256: str | None,
        expected_md5: str | None,
        allow_trashed: bool = False,
    ) -> None:
        properties = metadata.app_properties
        mismatch = (
            (metadata.trashed and not allow_trashed)
            or metadata.mime_type != "audio/wav"
            or properties.get("stt_schema") != _RECORDING_SCHEMA
            or properties.get("stt_object") != object_key
        )
        if expected_size is not None:
            mismatch = mismatch or metadata.size != expected_size
            mismatch = mismatch or properties.get("stt_size") != str(expected_size)
        if expected_sha256 is not None:
            mismatch = mismatch or metadata.sha256_checksum is None
            mismatch = mismatch or not _safe_checksum_equal(
                metadata.sha256_checksum, expected_sha256
            )
            mismatch = mismatch or not _safe_checksum_equal(
                properties.get("stt_sha256"), expected_sha256
            )
        if expected_md5 is not None:
            mismatch = mismatch or metadata.md5_checksum is None
            mismatch = mismatch or not _safe_checksum_equal(metadata.md5_checksum, expected_md5)
            mismatch = mismatch or not _safe_checksum_equal(
                properties.get("stt_md5"), expected_md5
            )
        if mismatch:
            raise DriveIntegrityError(
                "remote_integrity_mismatch",
                "Drive recording metadata does not match the finalized local file.",
            )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json_value: object | None = None,
        expected: set[int],
        operation: str,
    ) -> dict[str, Any]:
        response = self._send(
            method,
            url,
            params=params,
            json_value=json_value,
            stream=True,
        )
        try:
            if response.status_code not in expected:
                self._raise_response_error(response, operation)
            return _bounded_json(response)
        finally:
            response.close()

    def _send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json_value: object | None = None,
        content: bytes | None = None,
        stream: bool,
    ) -> httpx.Response:
        _validate_google_request_url(url)
        for attempt in range(2):
            token = self._credentials.access_token()
            request_headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
            if headers:
                request_headers.update(headers)
            try:
                request = self.client.build_request(
                    method,
                    url,
                    params=params,
                    headers=request_headers,
                    json=json_value,
                    content=content,
                )
                response = self.client.send(request, stream=stream, follow_redirects=False)
            except httpx.RequestError:
                raise DriveTransportError(
                    "drive_unavailable",
                    "Google Drive is temporarily unavailable.",
                    retryable=True,
                ) from None
            if response.status_code == 401 and attempt == 0:
                response.close()
                self._credentials.invalidate()
                continue
            return response
        raise AssertionError("unreachable")

    @staticmethod
    def _raise_response_error(response: httpx.Response, operation: str) -> None:
        status = response.status_code
        if status in {301, 302, 303, 307, 308}:
            raise DriveProtocolError(
                "unexpected_redirect", "Google Drive returned an unexpected redirect."
            )
        if status in {401, 403}:
            raise DriveAuthenticationError(
                "drive_authorization_rejected", "Google Drive authorization was rejected."
            )
        if status == 404:
            raise DriveNotFoundError("drive_file_not_found", "Google Drive file was not found.")
        if status in {409, 412}:
            raise DriveConflictError(
                "drive_conflict", "Google Drive rejected a conflicting change."
            )
        if status in {408, 425, 429, 500, 502, 503, 504}:
            raise DriveTransportError(
                "drive_temporarily_unavailable",
                "Google Drive is temporarily unavailable.",
                retryable=True,
            )
        raise DriveProtocolError(
            "drive_request_failed", f"Google Drive operation {operation} failed safely."
        )


def _safe_checksum_equal(left: object, right: str) -> bool:
    return isinstance(left, str) and hmac.compare_digest(left.casefold(), right.casefold())


def _validate_drive_id(value: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise DriveConfigurationError("invalid_drive_id", "Google Drive file ID is invalid.")


def _validate_object_key(value: str) -> None:
    if not isinstance(value, str) or _OBJECT_KEY.fullmatch(value) is None:
        raise DriveConfigurationError(
            "invalid_object_key", "Drive recording object key is invalid."
        )


def _validate_name(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 1024
        or any(ord(character) < 0x20 for character in value)
    ):
        raise DriveConfigurationError("invalid_file_name", "Drive recording name is invalid.")


def _validate_session_uri(value: str) -> None:
    if not isinstance(value, str) or len(value) > 4096:
        raise DriveConfigurationError(
            "invalid_upload_session", "Drive upload session URI is invalid."
        )
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.googleapis.com"
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.path != "/upload/drive/v3/files"
        or parsed.fragment
        or query.get("uploadType") != ["resumable"]
        or len(query.get("upload_id", [])) != 1
        or not query["upload_id"][0]
    ):
        raise DriveConfigurationError(
            "invalid_upload_session", "Drive upload session URI is not an allowed Google URL."
        )


def _validate_google_request_url(value: str) -> None:
    if value.startswith(DRIVE_API_ROOT + "/") or value == DRIVE_UPLOAD_URL:
        return
    _validate_session_uri(value)


def _bounded_json(response: httpx.Response, *, max_bytes: int = _MAX_JSON_BYTES) -> dict[str, Any]:
    content = bytearray()
    try:
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > max_bytes:
                raise DriveProtocolError(
                    "response_too_large", "Google returned an oversized metadata response."
                )
    except httpx.RequestError:
        raise DriveTransportError(
            "drive_response_interrupted",
            "Google Drive metadata response was interrupted.",
            retryable=True,
        ) from None
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DriveProtocolError(
            "invalid_json", "Google returned invalid metadata."
        ) from None
    if not isinstance(value, dict):
        raise DriveProtocolError("invalid_json", "Google returned invalid metadata.")
    return value


def _parse_metadata(value: object) -> DriveFileMetadata:
    if not isinstance(value, dict):
        raise DriveProtocolError("invalid_metadata", "Drive returned invalid file metadata.")
    file_id = value.get("id")
    name = value.get("name", "")
    mime_type = value.get("mimeType")
    size_value = value.get("size")
    try:
        _validate_drive_id(file_id)
        _validate_name(name)
        if not isinstance(mime_type, str) or not mime_type or len(mime_type) > 255:
            raise ValueError
        size = None if size_value is None else int(size_value)
        if size is not None and size < 0:
            raise ValueError
        md5 = value.get("md5Checksum")
        sha256 = value.get("sha256Checksum")
        if md5 is not None and re.fullmatch(r"[0-9a-fA-F]{32}", str(md5)) is None:
            raise ValueError
        if sha256 is not None and re.fullmatch(r"[0-9a-fA-F]{64}", str(sha256)) is None:
            raise ValueError
        properties = value.get("appProperties", {})
        if not isinstance(properties, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in properties.items()
        ):
            raise ValueError
        trashed = value.get("trashed", False)
        if not isinstance(trashed, bool):
            raise ValueError
        raw_parents = value.get("parents", [])
        if (
            not isinstance(raw_parents, list)
            or len(raw_parents) > 1
            or not all(isinstance(parent, str) for parent in raw_parents)
        ):
            raise ValueError
        parents = tuple(raw_parents)
        for parent in parents:
            _validate_drive_id(parent)
    except (DriveConfigurationError, TypeError, ValueError):
        raise DriveProtocolError(
            "invalid_metadata", "Drive returned invalid file metadata."
        ) from None
    return DriveFileMetadata(
        file_id=file_id,
        name=name,
        mime_type=mime_type,
        size=size,
        md5_checksum=str(md5).casefold() if md5 is not None else None,
        sha256_checksum=str(sha256).casefold() if sha256 is not None else None,
        trashed=trashed,
        created_time=_optional_metadata_string(value.get("createdTime")),
        modified_time=_optional_metadata_string(value.get("modifiedTime")),
        version=_optional_metadata_string(value.get("version")),
        parents=parents,
        app_properties=dict(properties),
    )


def _optional_metadata_string(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or any(ord(character) < 0x20 for character in value)
    ):
        raise DriveProtocolError("invalid_metadata", "Drive returned invalid file metadata.")
    return value


def _committed_bytes(response: httpx.Response, total_size: int) -> int:
    header = response.headers.get("range")
    if header is None:
        return 0
    match = _UPLOAD_RANGE.fullmatch(header)
    if match is None:
        raise DriveProtocolError(
            "upload_progress_invalid", "Drive returned invalid resumable upload progress."
        )
    committed = int(match.group(1)) + 1
    if not 0 < committed <= total_size:
        raise DriveProtocolError(
            "upload_progress_invalid", "Drive returned invalid resumable upload progress."
        )
    return committed


def _header_integer(response: httpx.Response, name: str) -> int | None:
    value = response.headers.get(name)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
