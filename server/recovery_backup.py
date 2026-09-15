"""Opt-in, encrypted operator recovery backups. Never restores a running app.

The only archive members are a consistent SQLite snapshot and a fixed allowlist
of configuration files. age performs encryption/authentication; this module does
not implement cryptography. Scheduler jobs only need the public recipient.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values

from .settings import PROJECT_DIR, Settings, account_usernames
from . import platform_files

AGE_RELATIVE = (Path(".tools/age/age.exe") if platform_files.IS_WINDOWS
                else Path(".tools/age-1.1.1-ubuntu24.04.3/usr/bin/age"))
POWERSHELL = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
              if platform_files.IS_WINDOWS else Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"))
MAX_DATABASE_BYTES = 512 * 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024
MAX_PLAINTEXT_BYTES = MAX_DATABASE_BYTES + 8 * MAX_CONFIG_BYTES
MAX_CIPHERTEXT_BYTES = MAX_PLAINTEXT_BYTES + 1024 * 1024
MEMBERS = frozenset({"database.sqlite3", "settings.env", "drive-identity.key",
                     "drive-oauth-client.json", "drive-token.json"})
DRIVE_MEMBERS = MEMBERS - {"database.sqlite3", "settings.env"}
BUNDLE_NAME = re.compile(r"yeobaek-recovery-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}\.age")
RECIPIENT = re.compile(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}")
PUBLIC_ERROR_CODES = frozenset({"cancelled", "timeout", "unsafe_path", "unsafe_directory", "unsafe_file",
    "size_limit", "external_tool_failed", "account_mismatch", "configuration_changed", "database_integrity",
    "database_foreign_keys", "missing_configuration", "incomplete_drive_credentials", "already_running",
    "invalid_pending_bundle", "pending_bundle_changed", "destination_unavailable", "backup_failed",
    "copy_verification_failed", "invalid_configuration"})


class BackupError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        # Never incorporate subprocess output, configuration values or paths.
        super().__init__(f"Recovery backup could not complete ({code}).")


def _check(cancel: threading.Event | None, deadline: float) -> None:
    if cancel is not None and cancel.is_set():
        raise BackupError("cancelled")
    if time.monotonic() >= deadline:
        raise BackupError("timeout")


def _no_symlinks(path: Path) -> None:
    if platform_files.IS_WINDOWS:
        try:
            platform_files.reject_links(path)
        except OSError:
            raise BackupError("unsafe_path") from None
        return
    current = Path(path.anchor)
    for part in path.absolute().parts[1:]:
        current /= part
        if current.is_symlink():
            raise BackupError("unsafe_path")


def _private_directory(path: Path, *, create: bool = False) -> None:
    if platform_files.IS_WINDOWS:
        try:
            if create:
                platform_files.ensure_private_directory(path)
            else:
                platform_files.validate_private_path(path, directory=True)
        except OSError:
            raise BackupError("unsafe_directory") from None
        return
    _no_symlinks(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BackupError("unsafe_directory")


@contextmanager
def _temporary_private_directory(*, prefix: str, directory: Path):
    temporary = platform_files.make_private_temporary_directory(prefix=prefix, directory=directory)
    try:
        yield temporary
    finally:
        shutil.rmtree(temporary)


def _read_private(path: Path, limit: int = MAX_CONFIG_BYTES) -> bytes:
    _no_symlinks(path)
    descriptor = (platform_files.open_file(path, os.O_RDONLY, private=True) if platform_files.IS_WINDOWS
                  else os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK))
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or (not platform_files.IS_WINDOWS
                and (info.st_uid != os.getuid() or info.st_mode & 0o077)) or info.st_size > limit):
            raise BackupError("unsafe_file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(limit + 1)
        if len(content) > limit:
            raise BackupError("size_limit")
        return content
    finally:
        os.close(descriptor)


def _write_new(path: Path, content: bytes) -> None:
    descriptor = platform_files.open_file(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, private=True)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _json(content: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BackupError("duplicate_json_key")
            result[key] = value
        return result
    value = json.loads(content, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise BackupError("invalid_metadata")
    return value


def _replace_private(path: Path, value: dict) -> None:
    if platform_files.IS_WINDOWS:
        try:
            platform_files.atomic_write_private(path, _json_bytes(value))
        except OSError:
            raise BackupError("unsafe_file") from None
        return
    if path.exists() or path.is_symlink():
        _read_private(path)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        _write_new(temporary, _json_bytes(value))
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _hash_file(path: Path, limit: int, cancel=None, deadline=float("inf")) -> tuple[int, str]:
    _no_symlinks(path)
    digest = hashlib.sha256()
    count = 0
    descriptor = platform_files.open_file(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise BackupError("unsafe_file")
        while block := source.read(1024 * 1024):
            _check(cancel, deadline)
            count += len(block)
            if count > limit:
                raise BackupError("size_limit")
            digest.update(block)
    return count, digest.hexdigest()


def _run(arguments: list[str], output, cancel, deadline: float) -> None:
    _check(cancel, deadline)
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=output,
                               stderr=subprocess.DEVNULL, close_fds=True)
    try:
        while process.poll() is None:
            _check(cancel, deadline)
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        if process.returncode != 0:
            raise BackupError("external_tool_failed")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def _accounts(env: bytes) -> tuple[str, ...]:
    values = dotenv_values(stream=io.StringIO(env.decode("utf-8")), interpolate=False)
    return tuple(sorted(account_usernames(values.get("ACCOUNT_USERNAMES"))))


def _database_info(path: Path, cancel=None, deadline=float("inf")) -> dict:
    if path.stat().st_size > MAX_DATABASE_BYTES:
        raise BackupError("size_limit")
    uri = f"{path.absolute().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.set_progress_handler(lambda: int((cancel is not None and cancel.is_set())
                                                   or time.monotonic() >= deadline), 10000)
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("database_integrity")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("database_foreign_keys")
        accounts = tuple(row[0] for row in connection.execute("SELECT username FROM users ORDER BY username"))
        if tuple(sorted(account_usernames(",".join(accounts)))) != accounts:
            raise BackupError("account_mismatch")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        jobs = 0
        for table in ("imports", "transcript_corrections", "lecture_summaries", "lecture_translations", "lecture_questions", "lecture_study_notes"):
            if table in tables:
                jobs += connection.execute(f"SELECT COUNT(*) FROM {table} WHERE status IN ('uploading','queued','processing')").fetchone()[0]
        if "chunks" in tables:
            jobs += connection.execute("SELECT COUNT(*) FROM chunks WHERE status='pending'").fetchone()[0]
        unfinalized = connection.execute("SELECT COUNT(*) FROM lectures WHERE recording_finalized=0").fetchone()[0] if "lectures" in tables else 0
    return {"accounts": accounts, "schema_version": version,
            "unfinished_jobs": jobs, "unfinalized_lectures": unfinalized}


def _local_wav_count(root: Path, cancel=None, deadline=float("inf")) -> int:
    """Count omitted local WAVs, never read audio or follow symbolic links."""
    if not root.exists():
        return 0
    _no_symlinks(root)
    count = examined = 0
    for directory, directories, files in os.walk(root, followlinks=False):
        _check(cancel, deadline)
        examined += len(directories) + len(files)
        if examined > 100000:
            raise BackupError("size_limit")
        def regular_entry(path: Path, *, directory: bool = False) -> bool:
            try:
                info = path.lstat()
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return False
            return stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        directories[:] = [name for name in directories if regular_entry(Path(directory) / name, directory=True)]
        for name in files:
            if name.lower().endswith(".wav") and regular_entry(Path(directory) / name):
                count += 1
    return count


class RecoveryBackupManager:
    def __init__(self, settings: Settings, *, project_dir: Path = PROJECT_DIR,
                 config_dir: Path | None = None, age_binary: Path | None = None,
                 copy_runner=None):
        self.settings = settings
        self.project_dir = Path(project_dir).absolute()
        self.config_dir = Path(config_dir or settings.data_dir / "backup").absolute()
        self.age_binary = Path(age_binary or self.project_dir / AGE_RELATIVE).absolute()
        self.copy_runner = copy_runner or self._copy_to_windows
        self.operation_lock = threading.Lock()
        self._running = False

    def _config(self) -> dict | None:
        path = self.config_dir / "config.json"
        if not path.exists() and not path.is_symlink():
            return None
        _private_directory(self.config_dir)
        value = _json(_read_private(path, 16384))
        if (set(value) != {"version", "enabled", "destination", "recipient", "interval_seconds"}
                or value["version"] != 1 or type(value["enabled"]) is not bool
                or value["destination"] != "windows-d"
                or not isinstance(value["recipient"], str) or not RECIPIENT.fullmatch(value["recipient"])
                or type(value["interval_seconds"]) is not int or value["interval_seconds"] != 86400):
            raise BackupError("invalid_configuration")
        if _read_private(self.config_dir / "recipient.txt", 1024).decode("ascii").strip() != value["recipient"]:
            raise BackupError("invalid_configuration")
        return value

    def _state(self) -> dict:
        path = self.config_dir / "state.json"
        return _json(_read_private(path, 16384)) if path.exists() else {}

    def initialize(self) -> dict:
        """Explicit operator-only action. Does not export or enable scheduling."""
        _private_directory(self.config_dir, create=True)
        if any((self.config_dir / name).exists() for name in ("identity.txt", "recipient.txt", "config.json")):
            raise BackupError("already_initialized")
        identity = self.config_dir / "identity.txt"
        recipient_path = self.config_dir / "recipient.txt"
        deadline = time.monotonic() + 30
        for destination, arguments in (
            (identity, [str(self.age_binary.with_name("age-keygen.exe" if platform_files.IS_WINDOWS else "age-keygen"))]),
            (recipient_path, [str(self.age_binary.with_name("age-keygen.exe" if platform_files.IS_WINDOWS else "age-keygen")), "-y", str(identity)]),
        ):
            descriptor = platform_files.open_file(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, private=True)
            with os.fdopen(descriptor, "wb") as output:
                _run(arguments, output, None, deadline)
                output.flush(); os.fsync(output.fileno())
        recipient = _read_private(recipient_path).decode("ascii").strip()
        if not RECIPIENT.fullmatch(recipient):
            raise BackupError("invalid_recipient")
        _write_new(self.config_dir / "config.json", _json_bytes({"version": 1, "enabled": False,
                   "destination": "windows-d", "recipient": recipient, "interval_seconds": 86400}))
        return {"initialized": True, "enabled": False, "private_key_needs_separate_safe_copy": True}

    def set_enabled(self, enabled: bool) -> dict:
        config = self._config()
        if config is None or type(enabled) is not bool:
            raise BackupError("not_initialized")
        config["enabled"] = enabled
        _replace_private(self.config_dir / "config.json", config)
        return self.status()

    def status(self) -> dict:
        try:
            config = self._config()
            if config is None:
                return {"configured": False, "enabled": False, "running": False}
            state = self._state()
            result = {"configured": True, "enabled": config["enabled"], "running": self._running,
                      "pending_copy": bool(state.get("pending")), "failure_count": max(0, int(state.get("failure_count", 0)))}
            for key in ("last_success_at", "last_success_bytes", "last_error_code"):
                if key in state:
                    value = state[key]
                    if key == "last_error_code":
                        result[key] = value if value is None or value in PUBLIC_ERROR_CODES else "backup_failed"
                    elif type(value) is int and 0 <= value <= 2 ** 53:
                        result[key] = value
            return result
        except Exception:
            return {"configured": False, "enabled": False, "running": self._running,
                    "last_error_code": "invalid_configuration"}

    def _sources(self) -> dict[str, Path]:
        private = self.settings.data_dir / "google-drive"
        configured_env = os.environ.get("STT_ENV_FILE")
        environment = Path(configured_env) if configured_env else self.project_dir / "server" / ".env"
        if not environment.is_absolute():
            raise BackupError("unsafe_path")
        sources = {"settings.env": environment,
                   "drive-identity.key": private / "identity.key",
                   "drive-oauth-client.json": self.settings.google_drive_oauth_client_path or private / "oauth-client.json",
                   "drive-token.json": self.settings.google_drive_token_path or private / "token.json"}
        present = {name: path for name, path in sources.items() if path.exists() or path.is_symlink()}
        if "settings.env" not in present or (self.settings.google_drive_enabled and not DRIVE_MEMBERS <= present.keys()):
            raise BackupError("missing_configuration")
        if present.keys() & DRIVE_MEMBERS and not DRIVE_MEMBERS <= present.keys():
            raise BackupError("incomplete_drive_credentials")
        return present

    def _build(self, recipient: str, cancel, deadline: float) -> dict:
        sources = self._sources()
        contents = {name: _read_private(path) for name, path in sources.items()}
        configured_accounts = _accounts(contents["settings.env"])
        if configured_accounts != tuple(sorted(self.settings.accounts)):
            raise BackupError("account_mismatch")
        source_db = self.settings.database_path.absolute()
        _no_symlinks(source_db)
        if source_db.stat().st_size > MAX_DATABASE_BYTES:
            raise BackupError("size_limit")
        before = _database_info(source_db, cancel, deadline)
        if before["accounts"] != configured_accounts:
            raise BackupError("account_mismatch")
        name = f"yeobaek-recovery-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.age"
        staging = self.config_dir / "staging"
        _private_directory(staging, create=True)
        with _temporary_private_directory(prefix="build-", directory=self.config_dir) as temporary:
            workspace = Path(temporary)
            if platform_files.IS_WINDOWS:
                platform_files.validate_private_path(workspace, directory=True)
            else:
                workspace.chmod(0o700)
            database = workspace / "database.sqlite3"
            _write_new(database, b"")
            uri = f"{source_db.as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=2)) as source, closing(sqlite3.connect(database)) as target:
                page_size = source.execute("PRAGMA page_size").fetchone()[0]
                def progress(_status, _remaining, total):
                    _check(cancel, deadline)
                    if total * page_size > MAX_DATABASE_BYTES:
                        raise BackupError("size_limit")
                source.backup(target, pages=256, progress=progress, sleep=0.05)
                # This is the private snapshot, not the live source. Make the
                # recovery database self-contained rather than WAL-dependent.
                target.execute("PRAGMA journal_mode=DELETE")
                target.commit()
            info = _database_info(database, cancel, deadline)
            after = _database_info(source_db, cancel, deadline)
            if info["accounts"] != configured_accounts or after["accounts"] != configured_accounts:
                raise BackupError("configuration_changed")
            if self._sources() != sources or any(_read_private(path) != contents[name] for name, path in sources.items()):
                raise BackupError("configuration_changed")
            for alias, content in contents.items():
                _write_new(workspace / alias, content)
            files = {}
            for alias in ["database.sqlite3", *contents]:
                size, digest = _hash_file(workspace / alias, MAX_DATABASE_BYTES if alias == "database.sqlite3" else MAX_CONFIG_BYTES, cancel, deadline)
                files[alias] = {"size": size, "sha256": digest}
            manifest = {"format": "yeobaek-recovery", "version": 1, "bundle_id": name.removesuffix(".age"),
                        "created_at": int(time.time()), "files": files, "schema_version": info["schema_version"],
                        "account_count": len(configured_accounts),
                        "accounts_sha256": hashlib.sha256(_json_bytes({"accounts": configured_accounts})).hexdigest(),
                        "warnings": {"unfinished_jobs": info["unfinished_jobs"],
                                     "unfinalized_lectures": info["unfinalized_lectures"],
                                     "local_wav_files_omitted": _local_wav_count(self.settings.data_dir / "recordings", cancel, deadline)}}
            _write_new(workspace / "manifest.json", _json_bytes(manifest))
            archive = workspace / "payload.tar"
            with archive.open("xb") as output:
                platform_files.set_private_file(output.fileno())
                with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                    for alias in [*files, "manifest.json"]:
                        _check(cancel, deadline)
                        path = workspace / alias
                        member = tarfile.TarInfo(alias)
                        member.size = path.stat().st_size
                        member.mode = 0o600
                        with path.open("rb") as source:
                            tar.addfile(member, source)
            ciphertext = staging / name
            try:
                descriptor = platform_files.open_file(ciphertext, os.O_WRONLY | os.O_CREAT | os.O_EXCL, private=True)
                with os.fdopen(descriptor, "wb") as output:
                    _run([str(self.age_binary), "--encrypt", "-r", recipient, str(archive)], output, cancel, deadline)
                    output.flush(); os.fsync(output.fileno())
                size, digest = _hash_file(ciphertext, MAX_CIPHERTEXT_BYTES, cancel, deadline)
                return {"name": name, "bytes": size, "sha256": digest}
            except BaseException:
                ciphertext.unlink(missing_ok=True)
                raise

    @contextmanager
    def _exclusive(self):
        if not self.operation_lock.acquire(blocking=False):
            raise BackupError("already_running")
        descriptor = None
        try:
            _private_directory(self.config_dir)
            descriptor = platform_files.open_file(self.config_dir / "operation.lock", os.O_RDWR | os.O_CREAT, private=True)
            with platform_files.file_lock(descriptor, blocking=False):
                self._running = True
                yield
        except BlockingIOError:
            raise BackupError("already_running") from None
        finally:
            self._running = False
            if descriptor is not None:
                os.close(descriptor)
            self.operation_lock.release()

    def _copy_to_windows(self, source: Path, pending: dict, cancel, deadline: float) -> None:
        with _temporary_private_directory(prefix="copy-", directory=self.config_dir) as temporary:
            output_path = Path(temporary) / "path.txt"
            for linux_path, key in ((source, "source"), (self.project_dir / "scripts" / "copy-backup-to-d.ps1", "script")):
                if platform_files.IS_WINDOWS:
                    value = str(linux_path.absolute())
                else:
                    with output_path.open("wb") as output:
                        _run(["wslpath", "-w", str(linux_path)], output, cancel, deadline)
                    value = output_path.read_text().strip()
                if len(value) > 4096 or "\n" in value or "\r" in value:
                    raise BackupError("unsafe_path")
                if key == "source":
                    source_windows = value
                else:
                    script_windows = value
            with (Path(temporary) / "result.json").open("wb") as output:
                _run([str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                      "-File", script_windows, "-Source", source_windows, "-Name", pending["name"],
                      "-ExpectedSha256", pending["sha256"], "-ExpectedBytes", str(pending["bytes"])],
                     output, cancel, min(deadline, time.monotonic() + 180))
            result = _json((Path(temporary) / "result.json").read_bytes())
            if result != {"verified": True, "bytes": pending["bytes"], "sha256": pending["sha256"]}:
                raise BackupError("copy_verification_failed")

    def export(self, cancel_event: threading.Event | None = None) -> dict:
        """One encrypted bundle; retry a pending bundle instead of making copies."""
        config = self._config()
        if config is None:
            raise BackupError("not_initialized")
        deadline = time.monotonic() + 600
        with self._exclusive():
            state = self._state()
            try:
                pending = state.get("pending")
                if not pending:
                    pending = self._build(config["recipient"], cancel_event, deadline)
                    state["pending"] = pending
                    _replace_private(self.config_dir / "state.json", state)
                if (not isinstance(pending, dict) or set(pending) != {"name", "bytes", "sha256"}
                        or not BUNDLE_NAME.fullmatch(pending["name"])
                        or type(pending["bytes"]) is not int or not 1 <= pending["bytes"] <= MAX_CIPHERTEXT_BYTES
                        or not re.fullmatch(r"[0-9a-f]{64}", pending["sha256"])):
                    raise BackupError("invalid_pending_bundle")
                source = self.config_dir / "staging" / pending["name"]
                if _hash_file(source, MAX_CIPHERTEXT_BYTES, cancel_event, deadline) != (pending["bytes"], pending["sha256"]):
                    raise BackupError("pending_bundle_changed")
                for attempt in range(3):
                    _check(cancel_event, deadline)
                    try:
                        self.copy_runner(source, pending, cancel_event, deadline)
                        break
                    except Exception:
                        if attempt == 2:
                            raise BackupError("destination_unavailable") from None
                        if cancel_event is not None:
                            cancel_event.wait(2 ** attempt)
                        else:
                            time.sleep(2 ** attempt)
                state.pop("pending", None)
                state.update(last_success_at=int(time.time()), last_success_bytes=pending["bytes"],
                             failure_count=0, last_error_code=None)
                _replace_private(self.config_dir / "state.json", state)
                source.unlink()
                return {"bundle_id": pending["name"].removesuffix(".age"),
                        "bytes": pending["bytes"], "sha256": pending["sha256"]}
            except Exception as error:
                code = error.code if isinstance(error, BackupError) else "backup_failed"
                state.update(last_error_code=code, failure_count=int(state.get("failure_count", 0)) + 1)
                _replace_private(self.config_dir / "state.json", state)
                raise BackupError(code) from None

    def restore_check(self, archive: Path, identity: Path, *, cancel_event=None) -> dict:
        return verify_recovery_archive(archive, identity, age_binary=self.age_binary, cancel_event=cancel_event)


def verify_recovery_archive(archive: Path, identity: Path, *, age_binary: Path = PROJECT_DIR / AGE_RELATIVE,
                            cancel_event=None) -> dict:
    """Authenticate first, then verify/extract only into a new private temporary directory."""
    _read_private(Path(identity), 65536)
    _hash_file(Path(archive), MAX_CIPHERTEXT_BYTES)
    destination = platform_files.make_private_temporary_directory(
        prefix="stt-recovery-check-", directory=None if platform_files.IS_WINDOWS else Path("/tmp"))
    deadline = time.monotonic() + 600
    try:
        payload = destination / "authenticated.tar"
        with payload.open("xb") as output:
            platform_files.set_private_file(output.fileno())
            _run([str(age_binary), "--decrypt", "-i", str(identity), str(archive)], output, cancel_event, deadline)
        # age may emit partial plaintext before the final authentication check.
        # No tar parser or database is touched until its successful process exit.
        if payload.stat().st_size > MAX_PLAINTEXT_BYTES:
            raise BackupError("size_limit")
        seen = set()
        # Parse fixed USTAR headers ourselves: tarfile's iterator expands PAX /
        # GNU long-name records before a caller can reject those allocations.
        with payload.open("rb") as tar:
            while True:
                _check(cancel_event, deadline)
                header = tar.read(512)
                if header == bytes(512):
                    if tar.read(512) != bytes(512):
                        raise BackupError("unsafe_archive")
                    while block := tar.read(1024 * 1024):
                        _check(cancel_event, deadline)
                        if any(block):
                            raise BackupError("unsafe_archive")
                    break
                if len(header) != 512 or len(seen) >= len(MEMBERS) + 1:
                    raise BackupError("unsafe_archive")
                member = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
                if (member.name not in MEMBERS | {"manifest.json"} or member.name in seen
                        or member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE} or member.linkname
                        or header[257:263] != b"ustar\x00"
                        or member.size < 0 or member.size > (MAX_DATABASE_BYTES if member.name == "database.sqlite3" else MAX_CONFIG_BYTES)):
                    raise BackupError("unsafe_archive")
                seen.add(member.name)
                with (destination / member.name).open("xb") as output:
                    platform_files.set_private_file(output.fileno())
                    remaining = member.size
                    while remaining:
                        _check(cancel_event, deadline)
                        block = tar.read(min(1024 * 1024, remaining))
                        if not block:
                            raise BackupError("truncated_archive")
                        output.write(block); remaining -= len(block)
                padding = (-member.size) % 512
                if tar.read(padding) != bytes(padding):
                    raise BackupError("unsafe_archive")
        if not {"manifest.json", "database.sqlite3", "settings.env"} <= seen:
            raise BackupError("missing_archive_member")
        manifest = _json(_read_private(destination / "manifest.json"))
        if (set(manifest) != {"format", "version", "bundle_id", "created_at", "files", "schema_version",
                             "account_count", "accounts_sha256", "warnings"}
                or manifest["format"] != "yeobaek-recovery" or manifest["version"] != 1
                or not isinstance(manifest["files"], dict) or set(manifest["files"]) != seen - {"manifest.json"}):
            raise BackupError("invalid_manifest")
        if (not isinstance(manifest["bundle_id"], str) or not BUNDLE_NAME.fullmatch(manifest["bundle_id"] + ".age")
                or type(manifest["created_at"]) is not int or manifest["created_at"] <= 0
                or type(manifest["schema_version"]) is not int or manifest["schema_version"] < 0
                or type(manifest["account_count"]) is not int or not 2 <= manifest["account_count"] <= 10
                or not isinstance(manifest["warnings"], dict)
                or set(manifest["warnings"]) != {"unfinished_jobs", "unfinalized_lectures", "local_wav_files_omitted"}
                or any(type(value) is not int or not 0 <= value <= 10 ** 9 for value in manifest["warnings"].values())):
            raise BackupError("invalid_manifest")
        for alias, expected in manifest["files"].items():
            if (not isinstance(expected, dict) or set(expected) != {"size", "sha256"}
                    or type(expected["size"]) is not int or expected["size"] < 0
                    or not isinstance(expected["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"])
                    or _hash_file(destination / alias, MAX_DATABASE_BYTES, cancel_event, deadline) != (expected["size"], expected["sha256"])):
                raise BackupError("manifest_hash_mismatch")
        accounts = _accounts(_read_private(destination / "settings.env"))
        info = _database_info(destination / "database.sqlite3", cancel_event, deadline)
        if (info["accounts"] != accounts or manifest["account_count"] != len(accounts)
                or manifest["accounts_sha256"] != hashlib.sha256(_json_bytes({"accounts": accounts})).hexdigest()
                or manifest["schema_version"] != info["schema_version"]):
            raise BackupError("account_mismatch")
        if seen & DRIVE_MEMBERS and not DRIVE_MEMBERS <= seen:
            raise BackupError("incomplete_drive_credentials")
        payload.unlink()
        return {"verified": True, "directory": str(destination), "file_count": len(seen),
                "schema_version": info["schema_version"], "warnings": manifest["warnings"]}
    except BaseException as error:
        # This directory was generated by this invocation and contains only its
        # authenticated/test payload. Never remove caller-selected directories.
        shutil.rmtree(destination)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise BackupError(error.code if isinstance(error, BackupError) else "verification_failed") from None


class BackupScheduler:
    def __init__(self, manager: RecoveryBackupManager):
        self.manager = manager
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def status(self) -> dict:
        return self.manager.status()

    def start(self) -> bool:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return False
            if not self.manager.status().get("enabled"):
                return False
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._main, name="encrypted-recovery-backup", daemon=True)
            try:
                self.thread.start()
            except Exception:
                # A failed native thread allocation leaves a Thread object
                # that cannot be joined. Let the lifespan report the original
                # startup failure while still cleaning up every other worker.
                self.thread = None
                self.stop_event.set()
                raise
            return True

    def stop(self, timeout: float = 5) -> bool:
        self.request_shutdown()
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0, min(timeout, 10)))
        return thread is None or not thread.is_alive()

    def request_shutdown(self) -> None:
        """Signal only; callers may share a shutdown deadline across workers."""
        self.stop_event.set()

    def _main(self) -> None:
        failures = 0
        while not self.stop_event.is_set():
            status = self.manager.status()
            if not status.get("enabled"):
                return
            remaining = max(0, status.get("last_success_at", 0) + 86400 - time.time())
            if remaining and not status.get("pending_copy"):
                self.stop_event.wait(min(remaining, 60))
                continue
            try:
                self.manager.export(self.stop_event)
                failures = 0
            except Exception:
                failures += 1
                self.stop_event.wait(min(3600, 60 * 2 ** min(failures - 1, 6)))
