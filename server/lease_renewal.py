"""Renew only a verified, already-online Quick Tunnel's public Pages lease."""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from scripts.runtime_config import parse_timestamp, validate_document
from .tunnel_control import PROJECT_ROOT, TunnelController


class LeaseStateError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DesiredLease:
    state: str
    api_url: str
    published_at: float
    expires_at: float


def _private_file(data_dir: Path, name: str) -> bytes:
    """Bounded, regular, same-user private reads; never follow a symlink/FIFO."""
    try:
        directory = data_dir.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid()
                or directory.st_mode & 0o077):
            raise LeaseStateError("unsafe_permissions")
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(data_dir / name, flags)
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o077 or metadata.st_size > 4096):
                raise LeaseStateError("unsafe_permissions")
            payload = os.read(descriptor, 4097)
            if len(payload) > 4096 or os.read(descriptor, 1):
                raise LeaseStateError("desired_invalid")
            return payload
        finally:
            os.close(descriptor)
    except FileNotFoundError as error:
        raise LeaseStateError("desired_missing") from error
    except (OSError, RuntimeError) as error:
        raise LeaseStateError("unsafe_permissions") from error


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def read_desired_lease(data_dir: Path, *, now: float) -> DesiredLease:
    payload = _private_file(data_dir, "pages-desired-config.json")
    try:
        document = json.loads(payload, object_pairs_hook=_unique_object)
        current = datetime.fromtimestamp(now, timezone.utc)
        validate_document(document, now=current)
        return DesiredLease(document["state"], document["apiUrl"],
                            parse_timestamp(document["publishedAt"]).timestamp(),
                            parse_timestamp(document["expiresAt"]).timestamp())
    except (ValueError, TypeError, OverflowError, UnicodeError, KeyError) as error:
        raise LeaseStateError("desired_invalid") from error


def current_online_lease(data_dir: Path, *, now: float) -> DesiredLease:
    lease = read_desired_lease(data_dir, now=now)
    if lease.state != "online":
        raise LeaseStateError("offline")
    if _private_file(data_dir, "tunnel-url.txt") not in {
        lease.api_url.encode("ascii"), (lease.api_url + "\n").encode("ascii"),
    }:
        raise LeaseStateError("url_changed")
    # Tighten the permissions of the two files which the existing controller
    # then validates structurally and against /proc. Never print their bytes.
    _private_file(data_dir, "server.pid")
    _private_file(data_dir, "tunnel.pid")
    return lease


RenewRunner = Callable[[tuple[str, ...], Path, Mapping[str, str], float, threading.Event], int]


class LeaseRenewer:
    def __init__(self, project_root: Path = PROJECT_ROOT, *, data_dir: Path | None = None,
                 enabled: bool = True, port: int = 8765,
                 renew_before_seconds: float = 6 * 3600,
                 poll_interval_seconds: float = 60, retry_seconds: float = 300,
                 command_timeout_seconds: float = 300,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 controller: TunnelController | None = None,
                 runner: RenewRunner | None = None,
                 lease_reader: Callable[..., DesiredLease] | None = None):
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        for value, low, high in ((renew_before_seconds, 3600, 12 * 3600),
                                 (poll_interval_seconds, 1, 300),
                                 (retry_seconds, 300, 3600),
                                 (command_timeout_seconds, 1, 600)):
            if isinstance(value, bool) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("invalid renewal timing")
        self.project_root = Path(project_root).resolve()
        self.data_dir = Path(data_dir) if data_dir is not None else self.project_root / ".data"
        self.enabled = enabled
        self.renew_before_seconds = renew_before_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.retry_seconds = retry_seconds
        self.command_timeout_seconds = command_timeout_seconds
        self._clock, self._monotonic = clock, monotonic
        self._controller = controller
        self._runner = runner or self._run_script
        self._lease_reader = lease_reader or current_online_lease
        self._shutdown = threading.Event()
        self._state_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._next_attempt = 0.0
        self._unconfirmed_url: str | None = None
        self._expires_at: float | None = None
        self._state = "idle" if enabled else "disabled"
        self._last_attempt: float | None = None
        self._last_success: float | None = None
        self._renewing = False
        self._error_code = ""
        if enabled and self._controller is None:
            try:
                self._controller = TunnelController(self.project_root, port=port)
            except Exception:
                self.enabled = False
                self._state, self._error_code = "disabled", "control_unavailable"

    def status(self) -> dict[str, object]:
        """Cached metadata only: no health call, file read or worker join."""
        with self._state_lock:
            remaining = None if self._expires_at is None else int(self._expires_at - self._clock())
            return {"enabled": self.enabled, "state": self._state,
                    "expires_in_seconds": remaining, "last_attempt_at": self._last_attempt,
                    "last_success_at": self._last_success, "renewing": self._renewing,
                    "error_code": self._error_code}

    def _mark(self, state: str, error: str = "", *, expires: float | None = None):
        with self._state_lock:
            self._state = state
            self._error_code = error
            self._expires_at = expires

    def start(self) -> None:
        with self._state_lock:
            if not self.enabled or self._shutdown.is_set() or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._loop, name="pages-lease-renewal", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                self._state, self._error_code = "blocked", "worker_start_failed"

    def request_shutdown(self) -> None:
        self._shutdown.set()
        # Holding this lock also covers Popen + registration, closing the race
        # where shutdown could otherwise miss a just-started child process.
        with self._process_lock:
            if self._process is not None:
                self._signal_group(self._process, signal.SIGTERM)
        with self._state_lock:
            if self.enabled:
                self._state = "stopping" if self._renewing else "stopped"

    def stop(self, timeout: float = 5.0) -> bool:
        self.request_shutdown()
        deadline = time.monotonic() + max(0.0, timeout)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, deadline - time.monotonic()))
        stopped = (thread is None or not thread.is_alive()) and not self._run_lock.locked()
        if not stopped:
            with self._process_lock:
                if self._process is not None:
                    self._signal_group(self._process, signal.SIGKILL)
        if stopped and self.enabled:
            self._mark("stopped")
        return stopped

    def _loop(self) -> None:
        while not self._shutdown.is_set():
            self.check_once()
            self._shutdown.wait(self.poll_interval_seconds)

    def check_once(self) -> None:
        """Bounded worker step, exposed for fake-clock tests; never call from GET."""
        if not self.enabled or self._shutdown.is_set() or not self._run_lock.acquire(blocking=False):
            return
        try:
            self._check_once()
        except LeaseStateError as error:
            if error.code in {"offline", "desired_missing"}:
                self._unconfirmed_url = None
            self._mark("offline" if error.code in {"offline", "desired_missing"} else "blocked", error.code)
        except Exception:
            self._next_attempt = self._monotonic() + self.retry_seconds
            self._mark("retrying", "renewal_failed")
        finally:
            with self._state_lock:
                self._renewing = False
                if self._shutdown.is_set():
                    self._state = "stopped"
            self._run_lock.release()

    def _check_once(self) -> None:
        now = self._clock()
        lease = self._lease_reader(self.data_dir, now=now)
        if self._unconfirmed_url != lease.api_url:
            self._unconfirmed_url = None
        if self._controller is None or not self._controller.renewal_processes_owned():
            raise LeaseStateError("process_not_owned")
        if lease.expires_at - now > self.renew_before_seconds and self._unconfirmed_url is None:
            self._mark("waiting", expires=lease.expires_at)
            return
        if self._monotonic() < self._next_attempt:
            self._mark("retrying", "retry_wait", expires=lease.expires_at)
            return
        if self._shutdown.is_set():
            return
        command = self._controller.renewal_command()
        with self._state_lock:
            self._state, self._renewing, self._error_code = "renewing", True, ""
            self._last_attempt = now
            self._expires_at = lease.expires_at
        self._next_attempt = self._monotonic() + self.retry_seconds
        self._unconfirmed_url = lease.api_url
        result = self._runner(command, self.project_root, self._controller._safe_environment(),
                              self.command_timeout_seconds, self._shutdown)
        if self._shutdown.is_set():
            return
        # Success requires the real publisher's new desired lease, not merely
        # an exit code or a locally stretched expiration timestamp.
        refreshed = self._lease_reader(self.data_dir, now=self._clock())
        if refreshed.api_url != lease.api_url or not self._controller.renewal_processes_owned():
            raise LeaseStateError("url_or_process_changed")
        if result != 0 or refreshed.published_at <= lease.published_at:
            self._unconfirmed_url = lease.api_url
            self._mark("retrying", "publication_failed", expires=refreshed.expires_at)
            return
        self._unconfirmed_url = None
        with self._state_lock:
            self._last_success = self._clock()
        self._mark("waiting", expires=refreshed.expires_at)

    @staticmethod
    def _signal_group(process: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except OSError:
            pass

    def _run_script(self, command, cwd, environment, timeout, cancelled) -> int:
        process = None
        group_killed = False
        try:
            with self._process_lock:
                if cancelled.is_set():
                    return 130
                process = subprocess.Popen(command, cwd=cwd, env=dict(environment),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True, start_new_session=True, shell=False)
                self._process = process
            deadline = time.monotonic() + timeout
            while not cancelled.is_set() and time.monotonic() < deadline:
                try:
                    return process.wait(timeout=min(0.25, max(0.001, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            self._signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            # Kill the original group even if its leader exited on TERM:
            # timeout/gh descendants must not continue publishing afterward.
            self._signal_group(process, signal.SIGKILL)
            group_killed = True
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            return 130 if cancelled.is_set() else 124
        except Exception:
            if process is not None:
                self._signal_group(process, signal.SIGKILL)
                group_killed = True
                try:
                    process.wait(timeout=1)
                except Exception:
                    pass
            return 126
        finally:
            with self._process_lock:
                if process is not None and cancelled.is_set() and not group_killed:
                    # TERM can make the leader's wait() return before the loop
                    # observes cancellation. Descendants which ignored TERM
                    # must still be killed before unregistering that group.
                    self._signal_group(process, signal.SIGKILL)
                if self._process is process:
                    self._process = None


def create_lease_renewer(*, data_dir: Path, enabled: bool = True, port: int = 8765) -> LeaseRenewer:
    """Production-only default; an isolated API factory never reads live state."""
    if os.name == "nt":
        from .windows_lease_renewal import create_windows_lease_renewer
        return create_windows_lease_renewer(data_dir=data_dir, enabled=enabled, port=port)
    safe = Path(data_dir).absolute() == PROJECT_ROOT / ".data"
    return LeaseRenewer(data_dir=data_dir, enabled=enabled and safe, port=port)


def _check_current_cli(port: int, cloudflared: Path) -> None:
    controller = TunnelController(PROJECT_ROOT, port=port, cloudflared_path=cloudflared)
    lease = current_online_lease(PROJECT_ROOT / ".data", now=time.time())
    if not controller.renewal_processes_owned(script_check=True):
        raise LeaseStateError("process_not_owned")
    print(lease.api_url)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only fixed tunnel lease check")
    parser.add_argument("--check-current", action="store_true", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cloudflared", type=Path, required=True)
    args = parser.parse_args()
    try:
        _check_current_cli(args.port, args.cloudflared)
    except Exception:
        raise SystemExit(1) from None
