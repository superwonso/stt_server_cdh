#!/usr/bin/env python3
"""Private Google Drive OAuth setup and recording archive maintenance.

This command intentionally prints aggregate state only.  Account IDs, lecture
titles, local paths, Drive IDs, upload session URLs, and credentials are never
printed. Runtime account IDs may name private Drive subfolders, but are not
written to source control or Drive appProperties.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    # Allow the documented ``.venv/bin/python scripts/google_drive.py`` form.
    sys.path.insert(0, str(PROJECT_ROOT))

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTHORIZATION_ENDPOINTS = frozenset(
    {
        "https://accounts.google.com/o/oauth2/auth",
        "https://accounts.google.com/o/oauth2/v2/auth",
    }
)
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CALLBACK_PATH = "/"
MAX_PRIVATE_JSON_BYTES = 64 * 1024
DEFAULT_AUTH_TIMEOUT_SECONDS = 300


class GoogleDriveSetupError(RuntimeError):
    """A credential-safe operator error."""


def _open_browser_without_output(url: str) -> bool:
    """Open an OAuth URL without letting a failed launcher echo the URL."""

    commands: list[list[str]] = []
    if os.environ.get("WSL_DISTRO_NAME"):
        powershell = Path(
            "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        if powershell.is_file():
            commands.append(
                [
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Start-Process -FilePath $args[0]",
                    url,
                ]
            )
    elif sys.platform == "darwin" and shutil.which("open"):
        commands.append(["open", url])
    elif sys.platform.startswith("linux") and shutil.which("xdg-open"):
        commands.append(["xdg-open", url])
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            return True
    return False


@dataclass(frozen=True, repr=False)
class OAuthClientConfiguration:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    authorization_endpoint: str
    token_endpoint: str


@dataclass(frozen=True, repr=False)
class AuthorizationGrant:
    code: str = field(repr=False)
    redirect_uri: str
    code_verifier: str = field(repr=False)


@dataclass(frozen=True, repr=False)
class OAuthCallbackResult:
    status: int
    terminal: bool
    code: str | None = field(default=None, repr=False)
    error: str | None = None


@dataclass(frozen=True)
class ArchiveFunctions:
    archive_status: Callable[[Any], dict[str, Any]]
    plan_existing_recordings: Callable[..., dict[str, Any]]
    enqueue_existing_recordings: Callable[..., dict[str, Any]]
    run_archive_until_idle: Callable[..., dict[str, Any]]


def _ensure_private_directory(path: Path) -> None:
    """Create one private leaf, but never chmod an arbitrary existing path."""

    created = False
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    except OSError as error:
        raise GoogleDriveSetupError("Google Drive private directory cannot be created safely") from error
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise GoogleDriveSetupError("Google Drive private directory is unsafe") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
            raise GoogleDriveSetupError("Google Drive private directory is unsafe")
        if created:
            os.fchmod(descriptor, 0o700)
        elif stat.S_IMODE(details.st_mode) != 0o700:
            raise GoogleDriveSetupError("Google Drive private directory must have mode 0700")
    finally:
        os.close(descriptor)


def _open_private_regular(path: Path) -> tuple[int, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise GoogleDriveSetupError(
            "Google OAuth desktop client JSON is missing from the private data directory"
        ) from error
    except OSError as error:
        raise GoogleDriveSetupError("Google OAuth desktop client JSON cannot be opened safely") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid():
            raise GoogleDriveSetupError("Google OAuth desktop client JSON is not a regular file")
        if not 0 < details.st_size <= MAX_PRIVATE_JSON_BYTES:
            raise GoogleDriveSetupError("Google OAuth desktop client JSON has an invalid size")
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise GoogleDriveSetupError("Google OAuth desktop client JSON must have mode 0600")
        return descriptor, details
    except BaseException:
        os.close(descriptor)
        raise


def _read_private_json(path: Path) -> dict[str, Any]:
    descriptor, details = _open_private_regular(path)
    try:
        chunks: list[bytes] = []
        remaining = details.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining or os.read(descriptor, 1):
            raise GoogleDriveSetupError("Google OAuth desktop client JSON changed while reading")
    finally:
        os.close(descriptor)
    try:
        document = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise GoogleDriveSetupError("Google OAuth desktop client JSON is invalid") from error
    if not isinstance(document, dict):
        raise GoogleDriveSetupError("Google OAuth desktop client JSON is invalid")
    return document


def load_oauth_client(path: Path) -> OAuthClientConfiguration:
    """Load only a Google *Desktop app* OAuth credential, fail closed otherwise."""

    document = _read_private_json(path)
    installed = document.get("installed")
    if not isinstance(installed, dict) or set(document) != {"installed"}:
        raise GoogleDriveSetupError("A Google OAuth Desktop app client JSON is required")
    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret")
    authorization_endpoint = installed.get("auth_uri")
    token_endpoint = installed.get("token_uri")
    redirect_uris = installed.get("redirect_uris")
    if (
        not isinstance(client_id, str)
        or not 8 <= len(client_id) <= 512
        or any(character.isspace() or ord(character) < 0x20 for character in client_id)
        or not isinstance(client_secret, str)
        or not 8 <= len(client_secret) <= 512
        or any(character.isspace() or ord(character) < 0x20 for character in client_secret)
        or authorization_endpoint not in AUTHORIZATION_ENDPOINTS
        or token_endpoint != TOKEN_ENDPOINT
        or not isinstance(redirect_uris, list)
        or not any(uri in {"http://localhost", "http://127.0.0.1"} for uri in redirect_uris)
    ):
        raise GoogleDriveSetupError("Google OAuth Desktop app client JSON is invalid")
    return OAuthClientConfiguration(
        client_id=client_id,
        client_secret=client_secret,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
    )


def _atomic_private_write(path: Path, value: bytes) -> None:
    """Atomically replace one secret file with mode 0600."""

    _ensure_private_directory(path.parent)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        directory_descriptor = os.open(path.parent, directory_flags)
    except OSError as error:
        raise GoogleDriveSetupError("Google Drive private directory is unsafe") from error
    try:
        try:
            current = os.stat(path.name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.geteuid()
        ):
            raise GoogleDriveSetupError("Google Drive private file target is unsafe")
    except BaseException:
        os.close(directory_descriptor)
        raise

    temporary_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_descriptor)
        view = memoryview(value)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short private file write")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(directory_descriptor)


@contextmanager
def _private_file_lock(lock_path: Path):
    """Hold a private advisory lock without following the lock pathname."""

    _ensure_private_directory(lock_path.parent)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        directory_descriptor = os.open(lock_path.parent, directory_flags)
    except OSError as error:
        raise GoogleDriveSetupError("Google Drive lock directory is unsafe") from error
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(lock_path.name, flags, 0o600, dir_fd=directory_descriptor)
    except OSError as error:
        os.close(directory_descriptor)
        raise GoogleDriveSetupError("Google Drive operation lock cannot be opened safely") from error
    os.close(directory_descriptor)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid():
            raise GoogleDriveSetupError("Google Drive token lock is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def _private_token_lock(token_path: Path):
    """Serialize OAuth replacement with server-side refresh persistence."""

    with _private_file_lock(token_path.parent / "token.lock"):
        yield


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_url(
    configuration: OAuthClientConfiguration,
    *,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    query = urlencode(
        {
            "client_id": configuration.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": DRIVE_FILE_SCOPE,
            "access_type": "offline",
            "prompt": "select_account consent",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{configuration.authorization_endpoint}?{query}"


def parse_oauth_callback(target: str, expected_state: str) -> OAuthCallbackResult:
    """Validate a loopback target without allowing unsolicited cancellation."""

    if len(target) > 8192:
        return OAuthCallbackResult(status=414, terminal=False)
    parsed = urlsplit(target)
    if parsed.path != CALLBACK_PATH:
        return OAuthCallbackResult(status=404, terminal=False)
    try:
        values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
    except ValueError:
        return OAuthCallbackResult(status=400, terminal=False)
    states = values.get("state", [])
    codes = values.get("code", [])
    errors = values.get("error", [])
    if states != [expected_state]:
        return OAuthCallbackResult(status=400, terminal=False)
    if errors:
        return OAuthCallbackResult(
            status=400,
            terminal=True,
            error="Google authorization was not approved",
        )
    if len(codes) != 1 or not codes[0] or len(codes[0]) > 4096:
        return OAuthCallbackResult(
            status=400,
            terminal=True,
            error="Google authorization callback did not contain a valid code",
        )
    return OAuthCallbackResult(status=200, terminal=True, code=codes[0])


class _OAuthCallbackServer:
    """One-use loopback receiver.  Authorization details are never logged."""

    def __init__(self, expected_state: str):
        self.expected_state = expected_state
        self.code: str | None = None
        self.error: str | None = None
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *arguments: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                result = parse_oauth_callback(self.path, receiver.expected_state)
                if result.terminal:
                    receiver.code = result.code
                    receiver.error = result.error
                self._finish(result.status, result.terminal)

            def _finish(self, status: int, terminal: bool) -> None:
                message = (
                    "Google Drive authorization received. You may close this tab."
                    if status == 200
                    else "Google Drive authorization could not be completed. Return to the terminal."
                )
                body = message.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Pragma", "no-cache")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                if not terminal:
                    return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.timeout = 0.5

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}{CALLBACK_PATH}"

    def wait(self, timeout_seconds: int) -> str:
        deadline = time.monotonic() + timeout_seconds
        while self.code is None and self.error is None and time.monotonic() < deadline:
            self.server.handle_request()
        if self.error:
            raise GoogleDriveSetupError(self.error)
        if self.code is None:
            raise GoogleDriveSetupError("Google authorization timed out")
        return self.code

    def close(self) -> None:
        self.server.server_close()

    def __enter__(self) -> "_OAuthCallbackServer":
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def collect_authorization_grant(
    configuration: OAuthClientConfiguration,
    authorization_url_path: Path,
    *,
    timeout_seconds: int = DEFAULT_AUTH_TIMEOUT_SECONDS,
    open_browser: bool = True,
    browser_open: Callable[[str], bool] = _open_browser_without_output,
) -> AuthorizationGrant:
    if not 30 <= timeout_seconds <= 900:
        raise GoogleDriveSetupError("Google authorization timeout must be 30 to 900 seconds")
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    with _OAuthCallbackServer(state) as callback:
        authorization_url = build_authorization_url(
            configuration,
            redirect_uri=callback.redirect_uri,
            state=state,
            code_challenge=_pkce_challenge(code_verifier),
        )
        _atomic_private_write(authorization_url_path, (authorization_url + "\n").encode("utf-8"))
        try:
            print(
                "Google 승인을 기다립니다. 브라우저가 열리지 않으면 비공개 "
                "authorization-url.txt 파일을 로컬에서 여세요. 승인 주소 자체는 출력하지 않습니다."
            )
            if open_browser:
                try:
                    browser_open(authorization_url)
                except Exception:
                    # The private URL file is the explicit WSL fallback.
                    pass
            code = callback.wait(timeout_seconds)
        finally:
            authorization_url_path.unlink(missing_ok=True)
    return AuthorizationGrant(
        code=code,
        redirect_uri=callback.redirect_uri,
        code_verifier=code_verifier,
    )


def exchange_authorization_grant(
    configuration: OAuthClientConfiguration,
    grant: AuthorizationGrant,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    owns_client = client is None
    requester = client or httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
        trust_env=False,
    )
    try:
        try:
            response = requester.post(
                configuration.token_endpoint,
                data={
                    "client_id": configuration.client_id,
                    "client_secret": configuration.client_secret,
                    "code": grant.code,
                    "code_verifier": grant.code_verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": grant.redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as error:
            raise GoogleDriveSetupError("Google token exchange could not connect") from error
        if response.status_code != 200 or len(response.content) > MAX_PRIVATE_JSON_BYTES:
            raise GoogleDriveSetupError("Google token exchange was rejected")
        try:
            result = response.json()
        except (ValueError, UnicodeError) as error:
            raise GoogleDriveSetupError("Google token exchange returned an invalid response") from error
        if not isinstance(result, dict):
            raise GoogleDriveSetupError("Google token exchange returned an invalid response")
        refresh_token = result.get("refresh_token")
        if (
            not isinstance(refresh_token, str)
            or not 16 <= len(refresh_token) <= 4096
            or any(character.isspace() or ord(character) < 0x20 for character in refresh_token)
        ):
            raise GoogleDriveSetupError("Google token exchange did not return offline access")
        granted_scope = result.get("scope")
        if not isinstance(granted_scope, str) or frozenset(granted_scope.split()) != {
            DRIVE_FILE_SCOPE
        }:
            raise GoogleDriveSetupError("Google token exchange did not grant the Drive file scope")
        return {
            "type": "authorized_user",
            "client_id": configuration.client_id,
            "client_secret": configuration.client_secret,
            "refresh_token": refresh_token,
            "token_uri": configuration.token_endpoint,
            "scopes": [DRIVE_FILE_SCOPE],
        }
    finally:
        if owns_client:
            requester.close()


def authorize(
    client_path: Path,
    token_path: Path,
    *,
    timeout_seconds: int = DEFAULT_AUTH_TIMEOUT_SECONDS,
    open_browser: bool = True,
    collect_grant: Callable[..., AuthorizationGrant] = collect_authorization_grant,
    exchange_grant: Callable[..., dict[str, Any]] = exchange_authorization_grant,
) -> None:
    _ensure_private_directory(client_path.parent)
    if token_path.parent != client_path.parent:
        _ensure_private_directory(token_path.parent)
    with _private_token_lock(token_path):
        configuration = load_oauth_client(client_path)
        authorization_url_path = token_path.parent / "authorization-url.txt"
        grant = collect_grant(
            configuration,
            authorization_url_path,
            timeout_seconds=timeout_seconds,
            open_browser=open_browser,
        )
        authorized_user = exchange_grant(configuration, grant)
        serialized = json.dumps(
            authorized_user,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        _atomic_private_write(token_path, serialized)


def _load_archive_functions() -> ArchiveFunctions:
    from server.drive_archive import (
        archive_status,
        enqueue_existing_recordings,
        plan_existing_recordings,
        run_archive_until_idle,
    )

    return ArchiveFunctions(
        archive_status=archive_status,
        plan_existing_recordings=plan_existing_recordings,
        enqueue_existing_recordings=enqueue_existing_recordings,
        run_archive_until_idle=run_archive_until_idle,
    )


def _safe_count(report: Mapping[str, Any], key: str) -> str:
    value = report.get(key)
    return str(value) if type(value) is int and value >= 0 else "unknown"


def _safe_bool(report: Mapping[str, Any], key: str) -> str:
    value = report.get(key)
    return "yes" if value is True else "no" if value is False else "unknown"


def _print_fields(report: Mapping[str, Any], fields: tuple[tuple[str, str, str], ...]) -> None:
    """Print a strict allowlist; never reflect unknown provider/DB values."""

    for label, key, kind in fields:
        value = _safe_bool(report, key) if kind == "bool" else _safe_count(report, key)
        print(f"{label}: {value}")


def _is_project_server_process(pid: int, *, process_root: Path) -> bool:
    """Recognize an exact project uvicorn process without trusting a PID file."""

    try:
        os.kill(pid, 0)
        process_directory = process_root / str(pid)
        working_directory = (process_directory / "cwd").resolve(strict=True)
        arguments = (process_directory / "cmdline").read_bytes().split(b"\0")
    except (OSError, UnicodeError, ValueError):
        return False
    return (
        working_directory == PROJECT_ROOT
        and b"server.app:create_app" in arguments
        and b"--factory" in arguments
        and any(argument.endswith(b"uvicorn") for argument in arguments)
    )


def server_is_running(settings: Any, *, process_root: Path = Path("/proc")) -> bool:
    """Find this project's uvicorn even if its managed PID file was lost."""

    pid_path = Path(settings.data_dir) / "server.pid"
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    managed_pid: int | None = None
    try:
        descriptor = os.open(pid_path, flags)
    except OSError:
        pass
    else:
        try:
            details = os.fstat(descriptor)
            if stat.S_ISREG(details.st_mode) and 0 < details.st_size <= 32:
                raw = os.read(descriptor, 33)
                pid_text = raw.decode("ascii").strip()
                if pid_text.isdigit():
                    managed_pid = int(pid_text)
        except (OSError, UnicodeError, ValueError):
            managed_pid = None
        finally:
            os.close(descriptor)
    if managed_pid is not None and _is_project_server_process(
        managed_pid, process_root=process_root
    ):
        return True

    # A crashed launcher or an interrupted stop can lose server.pid while the
    # worker remains alive. Refuse credential replacement/migration rather
    # than relying on that single bookkeeping file as a safety boundary.
    try:
        process_directories = tuple(process_root.iterdir())
    except OSError:
        return False
    for process_directory in process_directories:
        if not process_directory.name.isdigit():
            continue
        pid = int(process_directory.name)
        if pid == managed_pid:
            continue
        if _is_project_server_process(pid, process_root=process_root):
            return True
    return False


def show_archive_status(settings: Any, archive: ArchiveFunctions) -> None:
    report = archive.archive_status(settings)
    _print_fields(
        report,
        (
            ("configured", "configured", "bool"),
            ("connected", "connected", "bool"),
            ("pending", "pending_count", "count"),
            ("uploading", "uploading_count", "count"),
            ("ready", "ready_count", "count"),
            ("folder organization pending", "organization_pending_count", "count"),
            ("needs attention", "attention_count", "count"),
            ("retrying", "retrying_count", "count"),
            ("deleting", "deleting_count", "count"),
            ("local recordings", "local_count", "count"),
            ("local bytes", "local_bytes", "count"),
        ),
    )


def migrate_existing(
    settings: Any,
    archive: ArchiveFunctions,
    *,
    dry_run: bool,
    keep_local: bool,
    limit: int | None,
) -> bool:
    if dry_run:
        report = archive.plan_existing_recordings(settings, limit=limit)
        _print_fields(
            report,
            (
                ("migration candidates", "candidate_count", "count"),
                ("candidate bytes", "candidate_bytes", "count"),
                ("already archived", "already_ready_count", "count"),
                ("folder organization pending", "organization_pending_count", "count"),
                ("needs attention", "attention_count", "count"),
            ),
        )
        print("dry-run: no upload was started, no job was enqueued, and no local recording was deleted")
        return True

    readiness = archive.archive_status(settings)
    if readiness.get("configured") is not True:
        raise GoogleDriveSetupError(
            "Google Drive recording storage must be authorized and enabled before migration"
        )
    if readiness.get("connected") is not True:
        raise GoogleDriveSetupError(
            "Google Drive authorization could not be verified; check status and authorize again if needed"
        )

    enqueued = archive.enqueue_existing_recordings(settings, limit=limit)
    _print_fields(
        enqueued,
        (
            ("enqueued", "enqueued_count", "count"),
            ("needs attention", "attention_count", "count"),
        ),
    )
    completed = archive.run_archive_until_idle(
        settings,
        delete_local=not keep_local,
        limit=limit,
    )
    _print_fields(
        completed,
        (
            ("migrated", "migrated_count", "count"),
            ("local copies deleted", "deleted_local_count", "count"),
            ("failed", "failed_count", "count"),
            ("remaining", "remaining_count", "count"),
        ),
    )
    attention = enqueued.get("attention_count")
    failed = completed.get("failed_count")
    remaining = completed.get("remaining_count")
    return (
        type(attention) is int
        and attention == 0
        and type(failed) is int
        and failed == 0
        and type(remaining) is int
        and remaining == 0
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Authorize private Google Drive storage and migrate retained recordings."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    auth = subcommands.add_parser("auth", help="Authorize this server's private Google Drive storage")
    auth.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not try to launch a browser; open the private authorization-url.txt file manually",
    )
    auth.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_AUTH_TIMEOUT_SECONDS,
        help="Seconds to wait for the localhost OAuth callback (30-900; default: 300)",
    )
    subcommands.add_parser("status", help="Show aggregate Drive archive state without private IDs")
    migrate = subcommands.add_parser("migrate", help="Archive existing finalized recordings")
    migrate.add_argument("--dry-run", action="store_true", help="Count candidates without changing state")
    migrate.add_argument(
        "--keep-local",
        action="store_true",
        help="Keep verified local copies (default deletes only after verified upload)",
    )
    migrate.add_argument("--limit", type=int, help="Process at most this many candidates")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    settings_loader: Callable[[], Any] | None = None,
    archive: ArchiveFunctions | None = None,
    authorizer: Callable[..., None] = authorize,
    server_running_check: Callable[[Any], bool] = server_is_running,
) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if getattr(arguments, "limit", None) is not None and arguments.limit < 1:
        parser.error("--limit must be at least 1")
    if getattr(arguments, "timeout", DEFAULT_AUTH_TIMEOUT_SECONDS) not in range(30, 901):
        parser.error("--timeout must be 30 to 900 seconds")

    try:
        if settings_loader is None:
            from server.settings import Settings

            settings_loader = Settings.from_env
        settings = settings_loader()
        private_directory = Path(settings.data_dir) / "google-drive"
        client_path = Path(
            getattr(settings, "google_drive_oauth_client_path", None)
            or private_directory / "oauth-client.json"
        )
        token_path = Path(
            getattr(settings, "google_drive_token_path", None)
            or private_directory / "token.json"
        )

        if arguments.command == "auth":
            if server_running_check(settings):
                raise GoogleDriveSetupError(
                    "Stop the managed local API server with the documented server-only stop "
                    "command before replacing Google authorization"
                )
            authorizer(
                client_path,
                token_path,
                timeout_seconds=arguments.timeout,
                open_browser=not arguments.no_browser,
            )
            print("Google Drive authorization was stored in the private data directory.")
            print(
                "중요: 개인 Gmail OAuth 동의 화면이 Testing 상태이면 Drive refresh token은 "
                "일반적으로 7일 뒤 만료됩니다. 장기 운영 전 Production 상태를 확인하세요."
            )
            return 0

        archive = archive or _load_archive_functions()
        if arguments.command == "status":
            show_archive_status(settings, archive)
        else:
            if arguments.dry_run:
                complete = migrate_existing(
                    settings,
                    archive,
                    dry_run=True,
                    keep_local=arguments.keep_local,
                    limit=arguments.limit,
                )
            else:
                if server_running_check(settings):
                    raise GoogleDriveSetupError(
                        "Stop the managed local API server with the documented server-only stop "
                        "command before migrating existing recordings"
                    )
                complete = migrate_existing(
                    settings,
                    archive,
                    dry_run=False,
                    keep_local=arguments.keep_local,
                    limit=arguments.limit,
                )
            return 0 if complete else 1
        return 0
    except GoogleDriveSetupError as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        parser.error("Google Drive operation was interrupted; it is safe to run the command again")
    except Exception:
        # Archive/client exceptions can carry local paths, account IDs, Drive
        # IDs, or provider bodies.  The CLI deliberately never reflects them.
        parser.error(
            "Google Drive archive operation failed without changing verified local-file guarantees; "
            "it is safe to check status and retry"
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
