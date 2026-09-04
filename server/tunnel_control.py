from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PID = re.compile(rb"[1-9][0-9]{0,9}\n?")
_PROCESS_STATES = frozenset({"owned", "stopped", "conflict"})
_FIXED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

ProcessKind = Literal["server", "tunnel"]
ProcessProbe = Callable[[ProcessKind], str]
ScriptRunner = Callable[[tuple[str, ...], Path, Mapping[str, str], float], int]
GraceWaiter = Callable[[float], None]


@dataclass
class _Operation:
    number: int
    action: Literal["restart", "stop"]
    phase: Literal["scheduled", "running", "succeeded", "failed"]
    error_code: str = ""


class TunnelControlSetupError(RuntimeError):
    """The fixed local tunnel-control boundary is not safe to execute."""


class TunnelController:
    """Observe and asynchronously control only this project's Quick Tunnel.

    `restart()` and `stop()` return before executing a script.  The response
    grace is deliberately inside the worker so a FastAPI sync endpoint can
    serialize its HTTP 202 before the current tunnel is disconnected.  It is a
    delivery margin, not a network-level acknowledgement.

    No public URL, PID, subprocess output, or exception string is returned.
    Only fixed scripts and fixed arguments are executed with ``shell=False``.
    The scripts retain their own flock and PID checks as a second boundary.
    """

    def __init__(
        self,
        project_root: Path = PROJECT_ROOT,
        *,
        port: int = 8765,
        response_grace_seconds: float = 12.0,
        tunnel_start_timeout_seconds: int = 60,
        tunnel_stop_timeout_seconds: int = 20,
        command_timeout_seconds: float = 360.0,
        process_probe: ProcessProbe | None = None,
        script_runner: ScriptRunner | None = None,
        grace_waiter: GraceWaiter | None = None,
        cloudflared_path: Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        if not self.project_root.is_dir():
            raise TunnelControlSetupError("project root is not a directory")
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            raise TunnelControlSetupError("invalid fixed API port")
        if not 0 <= response_grace_seconds <= 20:
            raise TunnelControlSetupError("invalid response grace")
        if not 1 <= tunnel_start_timeout_seconds <= 300:
            raise TunnelControlSetupError("invalid tunnel start timeout")
        if not 1 <= tunnel_stop_timeout_seconds <= 300:
            raise TunnelControlSetupError("invalid tunnel stop timeout")
        if not 1 <= command_timeout_seconds <= 900:
            raise TunnelControlSetupError("invalid command timeout")

        scripts = self.project_root / "scripts"
        self.start_script = self._fixed_executable(scripts / "start-tunnel.sh")
        self.stop_script = self._fixed_executable(scripts / "stop.sh")
        # These are not executed directly here, but their fixed locations are
        # part of the start/stop scripts' publication and observation contract.
        self.status_script = self._fixed_executable(scripts / "status.sh")
        self.publish_script = self._fixed_executable(scripts / "publish-api-url.sh")

        candidate = Path(cloudflared_path) if cloudflared_path is not None else self.project_root / ".tools" / "cloudflared"
        try:
            self.cloudflared_path: Path | None = self._fixed_executable(candidate)
        except TunnelControlSetupError:
            # Read-only status remains useful before setup. A restart fails
            # closed until the pinned executable is installed.
            self.cloudflared_path = None

        self.port = port
        self.response_grace_seconds = float(response_grace_seconds)
        self.tunnel_start_timeout_seconds = tunnel_start_timeout_seconds
        self.tunnel_stop_timeout_seconds = tunnel_stop_timeout_seconds
        self.command_timeout_seconds = float(command_timeout_seconds)
        self.data_dir = self.project_root / ".data"
        self._process_probe = process_probe or self._default_process_probe
        self._script_runner = script_runner or self._run_script
        self._grace_waiter = grace_waiter or self._wait_grace
        self._lock = threading.Lock()
        self._operation: _Operation | None = None
        self._thread: threading.Thread | None = None
        self._next_operation = 0

    @staticmethod
    def _wait_grace(seconds: float) -> None:
        threading.Event().wait(seconds)

    @staticmethod
    def _fixed_executable(path: Path) -> Path:
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
            resolved_metadata = resolved.stat()
        except (OSError, RuntimeError) as error:
            raise TunnelControlSetupError("fixed executable is unavailable") from error
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise TunnelControlSetupError("fixed executable must be a regular file")
        if (metadata.st_dev, metadata.st_ino) != (resolved_metadata.st_dev, resolved_metadata.st_ino):
            raise TunnelControlSetupError("fixed executable changed during validation")
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise TunnelControlSetupError("fixed executable ownership is unsafe")
        if not os.access(resolved, os.X_OK):
            raise TunnelControlSetupError("fixed executable is not executable")
        return resolved

    def _safe_environment(self) -> dict[str, str]:
        environment = {
            "PATH": _FIXED_PATH,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        # gh uses the current account's private configuration. HOME is copied
        # only from the trusted server process and is never request-controlled.
        home = os.environ.get("HOME", "")
        if home and Path(home).is_absolute():
            environment["HOME"] = home
        return environment

    def _read_pid(self, kind: ProcessKind) -> tuple[str, int | None]:
        path = self.data_dir / f"{kind}.pid"
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return "stopped", None
        except OSError:
            return "conflict", None
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o022
            ):
                return "conflict", None
            payload = os.read(descriptor, 33)
            if len(payload) > 32 or os.read(descriptor, 1) or _PID.fullmatch(payload) is None:
                return "conflict", None
            return "candidate", int(payload.strip())
        except (OSError, ValueError, OverflowError):
            return "conflict", None
        finally:
            os.close(descriptor)

    @staticmethod
    def _process_is_live(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="strict")
            state = raw[raw.rfind(")") + 2 :].split(" ", 1)[0]
        except (OSError, UnicodeError, IndexError):
            return False
        return state not in {"Z", "X"}

    def _process_details(self, pid: int) -> tuple[Path, list[str], Path] | None:
        if not self._process_is_live(pid):
            return None
        process_root = Path(f"/proc/{pid}")
        try:
            if process_root.stat().st_uid != os.geteuid():
                return None
            cwd = Path(os.readlink(process_root / "cwd")).resolve(strict=True)
            executable = Path(os.readlink(process_root / "exe")).resolve(strict=True)
            with (process_root / "cmdline").open("rb") as command_file:
                command_bytes = command_file.read(65_537)
            if not command_bytes or len(command_bytes) > 65_536:
                return None
            arguments = [part.decode("utf-8", errors="strict") for part in command_bytes.rstrip(b"\0").split(b"\0")]
        except (OSError, RuntimeError, UnicodeError):
            return None
        return cwd, arguments, executable

    def _default_process_probe(self, kind: ProcessKind) -> str:
        pid_state, pid = self._read_pid(kind)
        if pid_state != "candidate" or pid is None:
            return pid_state
        if not self._process_is_live(pid):
            return "stopped"
        details = self._process_details(pid)
        if details is None:
            return "conflict"
        cwd, arguments, executable = details
        if cwd != self.project_root:
            return "conflict"

        if kind == "server":
            if pid != os.getpid():
                return "conflict"
            module_pair = any(
                left == "-m" and right == "uvicorn"
                for left, right in zip(arguments, arguments[1:])
            )
            if not (
                module_pair
                and "server.app:create_app" in arguments
                and "--factory" in arguments
                and self._option_values(arguments, "--host") == ("127.0.0.1",)
                and self._option_values(arguments, "--port") == (str(self.port),)
                and self._option_values(arguments, "--workers") == ("1",)
            ):
                return "conflict"
            return "owned"

        if self.cloudflared_path is None or executable != self.cloudflared_path:
            return "conflict"
        if not arguments or arguments[0] != str(self.cloudflared_path):
            return "conflict"
        if len(arguments) < 2 or arguments[1] != "tunnel" or arguments.count("--no-autoupdate") != 1:
            return "conflict"
        expected_url = f"http://127.0.0.1:{self.port}"
        return "owned" if self._option_values(arguments, "--url") == (expected_url,) else "conflict"

    @staticmethod
    def _option_values(arguments: list[str], option: str) -> tuple[str | None, ...]:
        """Return every explicit value, preserving malformed/multiple options."""

        values: list[str | None] = []
        for index, argument in enumerate(arguments):
            if argument == option:
                values.append(arguments[index + 1] if index + 1 < len(arguments) else None)
            elif argument.startswith(f"{option}="):
                values.append(argument.removeprefix(f"{option}="))
        return tuple(values)

    def _probe(self, kind: ProcessKind) -> str:
        try:
            result = self._process_probe(kind)
        except Exception:
            return "conflict"
        return result if result in _PROCESS_STATES else "conflict"

    @staticmethod
    def _run_script(
        command: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        timeout: float,
    ) -> int:
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                shell=False,
            )
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                return 124
        except Exception:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            return 126

    def _operation_name(self, operation: _Operation | None) -> str:
        if operation is None:
            return "idle"
        if operation.phase in {"scheduled", "running"}:
            return "restarting" if operation.action == "restart" else "stopping"
        return f"{operation.action}_{operation.phase}"

    def _payload(
        self,
        *,
        accepted: bool | None = None,
        forced_message: str = "",
    ) -> dict[str, object]:
        with self._lock:
            operation = None if self._operation is None else _Operation(**vars(self._operation))

        if operation is not None and operation.phase in {"scheduled", "running"}:
            state = "starting" if operation.action == "restart" else "stopping"
            if forced_message:
                message = forced_message
            elif operation.action == "restart":
                message = (
                    "응답을 보낸 뒤 임시 HTTPS 터널을 다시 연결합니다. "
                    "같은 공개 주소는 보장되지 않습니다."
                )
            else:
                message = (
                    "응답을 보낸 뒤 터널을 완전히 종료합니다. "
                    "이후에는 원격으로 다시 켤 수 없어 이 컴퓨터에서 시작해야 합니다."
                )
            remote_recovery = operation.action == "restart"
            local_start = operation.action == "stop"
            restart_available = False
        else:
            tunnel_state = self._probe("tunnel")
            restart_available = (
                self.cloudflared_path is not None
                and tunnel_state != "conflict"
                and self._probe("server") == "owned"
            )
            if tunnel_state == "conflict":
                state = "error"
                message = forced_message or "터널 PID 소유권을 확인하지 못해 원격 제어를 차단했습니다."
                remote_recovery = False
                local_start = True
            elif tunnel_state == "owned":
                state = "error" if operation is not None and operation.phase == "failed" else "online"
                if forced_message:
                    message = forced_message
                elif operation is not None and operation.phase == "succeeded" and operation.action == "restart":
                    message = "임시 HTTPS 터널을 다시 연결했습니다. 같은 공개 주소는 보장되지 않습니다."
                elif operation is not None and operation.phase == "failed":
                    message = "터널 작업을 완료하지 못했습니다. 이 컴퓨터에서 상태를 확인해 주세요."
                else:
                    message = "이 프로젝트가 소유한 임시 HTTPS 터널 프로세스가 실행 중입니다."
                remote_recovery = True
                local_start = False
            else:
                state = "error" if operation is not None and operation.phase == "failed" else "offline"
                if forced_message:
                    message = forced_message
                elif operation is not None and operation.phase == "failed" and operation.action == "restart":
                    message = "터널 재연결에 실패했습니다. 원격 복구는 불가능하며 이 컴퓨터에서 시작해야 합니다."
                else:
                    message = "터널이 완전히 종료됐습니다. 원격으로 다시 켤 수 없어 이 컴퓨터에서 시작해야 합니다."
                remote_recovery = False
                local_start = True

        result: dict[str, object] = {
            "state": state,
            "operation": self._operation_name(operation),
            "message": message,
            "remote_recovery_possible": remote_recovery,
            "local_start_required": local_start,
            "same_public_url_guaranteed": False,
            "restart_available": restart_available,
        }
        if accepted is not None:
            result["accepted"] = accepted
        return result

    def status(self) -> dict[str, object]:
        """Return a local-process-only status without health checks or URLs."""

        return self._payload()

    def restart(self) -> dict[str, object]:
        """Schedule a new Quick Tunnel after the current response gets a margin."""

        return self._schedule("restart")

    def stop(self) -> dict[str, object]:
        """Schedule a full tunnel stop; only a later local start can recover it."""

        return self._schedule("stop")

    def _schedule(self, action: Literal["restart", "stop"]) -> dict[str, object]:
        with self._lock:
            if self._operation is not None and self._operation.phase in {"scheduled", "running"}:
                busy = True
            else:
                busy = False
        if busy:
            return self._payload(
                accepted=False,
                forced_message="다른 터널 작업이 이미 진행 중입니다. 완료된 뒤 다시 시도해 주세요.",
            )

        if self._probe("server") != "owned":
            return self._payload(
                accepted=False,
                forced_message="현재 API 프로세스의 소유권을 확인하지 못해 터널 제어를 차단했습니다.",
            )
        if self._probe("tunnel") == "conflict":
            return self._payload(
                accepted=False,
                forced_message="터널 PID 소유권을 확인하지 못해 원격 제어를 차단했습니다.",
            )
        if action == "restart" and self.cloudflared_path is None:
            return self._payload(
                accepted=False,
                forced_message="고정된 cloudflared 실행 파일을 확인하지 못해 재연결을 시작하지 않았습니다.",
            )

        with self._lock:
            # Repeat the active-operation test after process inspection so two
            # simultaneous sync endpoints cannot both schedule a worker.
            if self._operation is not None and self._operation.phase in {"scheduled", "running"}:
                conflict = True
            else:
                conflict = False
                self._next_operation += 1
                operation = _Operation(self._next_operation, action, "scheduled")
                self._operation = operation
                worker = threading.Thread(
                    target=self._worker,
                    args=(operation.number,),
                    name="tunnel-control-worker",
                    daemon=True,
                )
                self._thread = worker
        if conflict:
            return self._payload(
                accepted=False,
                forced_message="다른 터널 작업이 이미 진행 중입니다. 완료된 뒤 다시 시도해 주세요.",
            )
        # Snapshot the accepted response before starting the worker.  Even if
        # this thread is descheduled for longer than the response grace, an
        # accepted HTTP request still reports the operation it scheduled.
        accepted_payload = self._payload(accepted=True)
        try:
            worker.start()
        except Exception:
            with self._lock:
                if self._operation is operation:
                    operation.phase = "failed"
                    operation.error_code = "worker_start_failed"
            return self._payload(
                accepted=False,
                forced_message="터널 작업을 시작하지 못했습니다. 이 컴퓨터에서 상태를 확인해 주세요.",
            )
        return accepted_payload

    def _mark(self, number: int, phase: Literal["running", "succeeded", "failed"], error: str = "") -> bool:
        with self._lock:
            if self._operation is None or self._operation.number != number:
                return False
            self._operation.phase = phase
            self._operation.error_code = error
            return True

    def _run_fixed(self, command: tuple[str, ...]) -> bool:
        # Revalidate immediately before execution. The subprocess receives no
        # request-derived value and neither output stream is captured.
        try:
            self._fixed_executable(Path(command[0]))
            result = self._script_runner(
                command,
                self.project_root,
                self._safe_environment(),
                self.command_timeout_seconds,
            )
        except Exception:
            return False
        return type(result) is int and result == 0

    def _worker(self, number: int) -> None:
        try:
            self._worker_body(number)
        except Exception:
            # A worker must always reach a terminal phase; otherwise every
            # later request would remain locked out as "already running".
            self._mark(number, "failed", "worker_failed")

    def _worker_body(self, number: int) -> None:
        try:
            self._grace_waiter(self.response_grace_seconds)
        except Exception:
            self._mark(number, "failed", "grace_failed")
            return
        if not self._mark(number, "running"):
            return
        with self._lock:
            operation = self._operation
            action = operation.action if operation is not None and operation.number == number else None
        if action is None:
            return
        if self._probe("server") != "owned" or self._probe("tunnel") == "conflict":
            self._mark(number, "failed", "ownership_changed")
            return

        stop_command = (
            str(self.stop_script),
            "--tunnel-only",
            "--timeout",
            str(self.tunnel_stop_timeout_seconds),
        )
        start_command = (
            str(self.start_script),
            "--port",
            str(self.port),
            "--cloudflared",
            str(self.cloudflared_path),
            "--timeout",
            str(self.tunnel_start_timeout_seconds),
        )

        initial_tunnel_state = self._probe("tunnel")
        if (action == "stop" or initial_tunnel_state == "owned") and not self._run_fixed(stop_command):
            self._mark(number, "failed", "stop_failed")
            return
        if action == "restart":
            if not self._run_fixed(start_command):
                self._mark(number, "failed", "start_failed")
                return
            if self._probe("tunnel") != "owned":
                self._mark(number, "failed", "start_not_owned")
                return
        elif self._probe("tunnel") == "owned":
            self._mark(number, "failed", "stop_still_running")
            return
        self._mark(number, "succeeded")

    def wait_for_operation(self, timeout: float = 5.0) -> dict[str, object]:
        """Join the current worker for tests/local diagnostics, never API use."""

        with self._lock:
            worker = self._thread
        if worker is not None:
            worker.join(timeout=max(0.0, timeout))
        return self.status()


_default_lock = threading.Lock()
_default_controller: TunnelController | None = None


def _get_default_controller() -> TunnelController:
    global _default_controller
    with _default_lock:
        if _default_controller is None:
            _default_controller = TunnelController()
        return _default_controller


def _unavailable(*, accepted: bool | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "state": "error",
        "operation": "idle",
        "message": "로컬 터널 제어 구성을 확인하지 못했습니다. 이 컴퓨터에서 상태를 확인해 주세요.",
        "remote_recovery_possible": False,
        "local_start_required": True,
        "same_public_url_guaranteed": False,
        "restart_available": False,
    }
    if accepted is not None:
        result["accepted"] = accepted
    return result


def tunnel_status() -> dict[str, object]:
    """Default injectable status callable for the FastAPI integration."""

    try:
        return _get_default_controller().status()
    except Exception:
        return _unavailable()


def tunnel_restart() -> dict[str, object]:
    """Default injectable non-blocking restart callable for HTTP 202 handling."""

    try:
        return _get_default_controller().restart()
    except Exception:
        return _unavailable(accepted=False)
