"""Private Unix-socket model lifecycle. This module never loads API Settings/.env.

Only ``start`` selects model keys from the shared env file. The detached child
receives a new, restricted environment; ``run`` never reads that file.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass

if os.name != "nt":
    import fcntl

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SOCKET = PROJECT_DIR / ".data" / "model-server" / "model.sock"
MODEL_KEYS = frozenset({
    "MODEL_CACHE_DIR", "ASR_MODEL", "ASR_ALIGNER", "ASR_DEVICE", "ASR_DTYPE",
    "ASR_ATTENTION", "STABILITY_GUARD_SECONDS", "WHISPER_MODEL", "WHISPER_DEVICE",
    "WHISPER_COMPUTE_TYPE", "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL",
    "HSA_OVERRIDE_GFX_VERSION", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES",
    "CUDA_VISIBLE_DEVICES", "PYTORCH_CUDA_ALLOC_CONF", "PYTORCH_HIP_ALLOC_CONF",
    "OMP_NUM_THREADS", "MKL_NUM_THREADS", "MIOPEN_FIND_MODE",
    # ROCm on WSL can require these explicit runtime settings. Preserve only
    # configured values; do not copy the rest of the API/shell environment.
    "HSA_ENABLE_DXG_DETECTION", "LD_LIBRARY_PATH",
})
STATES = frozenset({"unloaded", "loading", "ready", "error"})


class ModelProcessError(RuntimeError):
    pass


def model_environment(env_file: Path | None, inherited: dict | None = None) -> dict[str, str]:
    """No interpolation, shell execution, full dotenv loading, or secret copying."""
    selected = {}
    if env_file is not None and env_file.exists():
        if env_file.is_symlink() or env_file.stat().st_size > 256 * 1024:
            raise ModelProcessError("모델 설정 파일을 안전하게 읽지 못했습니다.")
        with env_file.open(encoding="utf-8") as source:
            for line in source:
                match = re.match(r"\s*(?:export\s+)?([A-Z_][A-Z_0-9]*)\s*=(.*)$", line)
                if not match or match[1] not in MODEL_KEYS:
                    continue
                try:
                    values = shlex.split(match[2], comments=True, posix=True)
                except ValueError:
                    raise ModelProcessError("모델 설정의 따옴표 형식을 확인하세요.") from None
                if len(values) > 1:
                    raise ModelProcessError("모델 설정 값의 형식을 확인하세요.")
                selected[match[1]] = values[0] if values else ""
    inherited = os.environ if inherited is None else inherited
    selected.update({key: inherited[key] for key in MODEL_KEYS if key in inherited})
    for value in selected.values():
        if not isinstance(value, str) or len(value) > 4096 or any(c in value for c in "\0\r\n$`"):
            raise ModelProcessError("모델 설정에 지원하지 않는 값이 있습니다.")
    system_environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    if os.name == "nt":
        # Native Python/DLL lookup requires Windows runtime variables. Copy no
        # API credentials, user-supplied Python hooks, proxies or dotenv keys.
        # PyTorch Inductor uses getpass.getuser() for its cache directory; native
        # Windows needs USERNAME because the fallback pwd module is POSIX-only.
        system_environment = {key: value for key in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR",
                              "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "USERNAME")
                              if isinstance((value := inherited.get(key)), str)}
        system_environment.setdefault("SystemRoot", os.environ.get("SystemRoot", r"C:\Windows"))
        system_environment.setdefault("PATH", os.pathsep.join((str(Path(sys.executable).parent),
                                               str(Path(system_environment["SystemRoot"]) / "System32"))))
    return {**system_environment, "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", **selected}


@dataclass(frozen=True)
class ModelSettings:
    model_cache_dir: Path
    model: str = "Qwen3-ASR-1.7B"
    aligner: str = "Qwen3-ForcedAligner-0.6B"
    device: str = "cuda:0"
    compute_type: str = "bfloat16"
    attention: str = "sdpa"
    stability_guard_seconds: float = 0.6
    model_warmup: bool = True

    @property
    def model_path(self):
        path = Path(self.model).expanduser()
        return path if path.is_absolute() else self.model_cache_dir / path

    @property
    def aligner_path(self):
        path = Path(self.aligner).expanduser()
        return path if path.is_absolute() else self.model_cache_dir / path

    @classmethod
    def from_model_env(cls, env: dict, *, warmup: bool):
        path = Path(env.get("MODEL_CACHE_DIR", ".models")).expanduser()
        if not path.is_absolute():
            path = PROJECT_DIR / path
        try:
            guard = float(env.get("STABILITY_GUARD_SECONDS", "0.6"))
        except (ValueError, TypeError):
            raise ModelProcessError("모델 안정화 구간 설정을 확인하세요.") from None
        if not math.isfinite(guard):
            raise ModelProcessError("모델 안정화 구간 설정을 확인하세요.")
        return cls(path, env.get("ASR_MODEL", env.get("WHISPER_MODEL", cls.model)),
                   env.get("ASR_ALIGNER", cls.aligner), env.get("ASR_DEVICE", env.get("WHISPER_DEVICE", cls.device)),
                   env.get("ASR_DTYPE", env.get("WHISPER_COMPUTE_TYPE", cls.compute_type)),
                   env.get("ASR_ATTENTION", cls.attention), max(.2, min(guard, 1.0)), warmup)


def socket_path(value: str | Path, *, create: bool = False) -> Path:
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts or len(os.fsencode(path)) > 107
            or not re.fullmatch(r"[A-Za-z0-9_.-]+\.sock", path.name)
            or path.resolve() != path):
        raise ModelProcessError("소켓은 심볼릭 링크가 없는 짧은 절대 .sock 경로여야 합니다.")
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.exists():
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ModelProcessError("소켓 전용 폴더는 현재 사용자 소유의 0700 권한이어야 합니다.")
    return path


def checked_file(path: Path, *, missing=True):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ModelProcessError("모델 실행 기록·로그의 소유권 또는 권한이 올바르지 않습니다.")
    return info


@contextmanager
def process_lock(path: Path):
    checked_file(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ModelProcessError("다른 모델 실행·종료 작업이 진행 중입니다.") from None
        yield
    finally:
        os.close(fd)


def process_identity(pid: int) -> dict | None:
    try:
        proc = Path(f"/proc/{pid}")
        parts = (proc / "stat").read_text().rsplit(") ", 1)[1].split()
        if parts[0] in {"Z", "X"}:
            return None
        return {"pid": pid, "uid": proc.stat().st_uid, "start_ticks": parts[19],
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "cwd": os.readlink(proc / "cwd"), "exe": os.readlink(proc / "exe"),
                "command_hash": hashlib.sha256((proc / "cmdline").read_bytes()).hexdigest()}
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (PermissionError, OSError, IndexError):
        raise ModelProcessError("모델 프로세스의 소유권을 확인하지 못했습니다.") from None


def command_hash(command: list[str]) -> str:
    return hashlib.sha256(b"\0".join(os.fsencode(arg) for arg in command) + b"\0").hexdigest()


def open_pidfd(pid: int) -> int:
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    # Some standalone Python builds omit os.pidfd_open even though this WSL
    # kernel/glibc supports it. Use libc's typed API, never a PID-only signal.
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        function = libc.pidfd_open
    except AttributeError:
        raise ModelProcessError("이 Python/시스템은 안전한 pidfd 종료를 지원하지 않습니다.") from None
    function.argtypes = [ctypes.c_int, ctypes.c_uint]
    function.restype = ctypes.c_int
    fd = function(pid, 0)
    if fd < 0:
        code = ctypes.get_errno()
        raise OSError(code, "pidfd_open failed")
    return fd


def atomic_record(path: Path, record: dict):
    checked_file(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(record, output); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ModelController:
    def __init__(self, path: Path, *, project_dir: Path = PROJECT_DIR, command_prefix=None):
        self.path = socket_path(path)
        self.directory = self.path.parent
        self.pid_file = self.directory / "model.pid.json"
        self.lock_file = self.directory / "model-control.lock"
        self.run_lock = self.directory / "model-run.lock"
        self.log_file = self.directory / "model.log"
        self.project_dir = project_dir
        self.command_prefix = command_prefix or [sys.executable, "-m", "server.model_process"]
        self._spawned = None

    def command(self, warmup):
        return [*self.command_prefix, "run", "--socket", str(self.path), "--warmup" if warmup else "--no-warmup"]

    def record(self):
        info = checked_file(self.pid_file)
        if info is None:
            return None
        if info.st_size > 8192:
            raise ModelProcessError("모델 실행 기록이 손상되었습니다.")
        try:
            value = json.loads(self.pid_file.read_text())
            expected = {"pid", "uid", "start_ticks", "boot_id", "cwd", "exe", "command_hash", "socket", "socket_dev", "socket_ino"}
            if (set(value) != expected or type(value["pid"]) is not int or value["pid"] <= 1
                    or value["uid"] != os.getuid() or value["socket"] != str(self.path)
                    or value["cwd"] != str(self.project_dir)
                    or value["exe"] != os.path.realpath(self.command_prefix[0])
                    or value["command_hash"] not in {command_hash(self.command(True)), command_hash(self.command(False))}
                    or any(type(value[k]) is not int or value[k] <= 0 for k in ["socket_dev", "socket_ino"])):
                raise ValueError
            return value
        except (ValueError, TypeError, KeyError):
            raise ModelProcessError("모델 실행 기록이 현재 실행 방식과 일치하지 않습니다. 임의로 정리하지 않았습니다.") from None

    def matching(self, record):
        live = process_identity(record["pid"])
        if live is None:
            return False
        if any(live[key] != record[key] for key in live):
            raise ModelProcessError("PID가 다른 프로세스를 가리킵니다. 신호를 보내거나 소켓을 삭제하지 않았습니다.")
        return True

    def socket_info(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ModelProcessError("소켓 소유권·형식·권한을 확인하지 못했습니다. 그대로 보존했습니다.")
        return info

    def health(self, record):
        info = self.socket_info()
        if info is None or (info.st_dev, info.st_ino) != (record["socket_dev"], record["socket_ino"]):
            return None
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        connection = http.client.HTTPConnection("localhost", timeout=2)
        try:
            client.connect(str(self.path))
            pid, uid, _ = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if (pid, uid) != (record["pid"], os.getuid()):
                return None
            connection.sock = client
            connection.request("GET", "/health", headers={"Connection": "close"})
            response = connection.getresponse()
            payload = response.read(16385)
            if response.status != 200 or len(payload) > 16384:
                return None
            value = json.loads(payload)
            if value.get("status") != "ok" or value.get("model_state") not in STATES:
                return None
            return {"alive": True, "ready": value["model_state"] == "ready", "model_state": value["model_state"]}
        except (OSError, ValueError, http.client.HTTPException):
            return None
        finally:
            connection.close(); client.close()

    def clean_dead(self, record):
        if self.matching(record):
            raise ModelProcessError("실행 중인 모델의 소켓을 정리하지 않습니다.")
        info = self.socket_info()
        if info is not None:
            if (info.st_dev, info.st_ino) != (record["socket_dev"], record["socket_ino"]):
                raise ModelProcessError("소켓이 다른 파일로 바뀌어 그대로 보존했습니다.")
            for attempt in range(6):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(.2)
                    try:
                        probe.connect(str(self.path))
                    except OSError as error:
                        if error.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                            raise ModelProcessError("소켓 사용 여부를 확인하지 못해 보존했습니다.") from None
                        break
                    else:
                        pid, uid, _ = struct.unpack("3i", probe.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                        if (pid, uid) != (record["pid"], os.getuid()) or self.matching(record):
                            raise ModelProcessError("소유자가 확인되지 않은 소켓 응답을 보존했습니다.")
                # During SIGKILL teardown /proc can lose identity immediately
                # before the dead process's listener is released. Recheck only
                # this attested dead peer briefly; never unlink a live listener.
                time.sleep(.05)
            else:
                raise ModelProcessError("소켓 종료를 확인하지 못해 그대로 보존했습니다.")
            latest = self.socket_info()
            if latest is not None and (latest.st_dev, latest.st_ino) == (info.st_dev, info.st_ino):
                self.path.unlink()
        if self.record() == record:
            self.pid_file.unlink()

    def status(self):
        if self._spawned is not None and self._spawned.poll() is not None:
            self._spawned = None
        if not self.directory.exists():
            return {"running": False, "alive": False, "ready": False, "model_state": "stopped"}
        record = self.record()
        if record is None:
            if self.path.exists() or self.path.is_symlink():
                raise ModelProcessError("실행 기록이 없는 소켓을 발견했습니다. 임의로 정리하지 않았습니다.")
            return {"running": False, "alive": False, "ready": False, "model_state": "stopped"}
        running = self.matching(record)
        health = self.health(record) if running else None
        return {"running": running, "pid": record["pid"], "alive": False, "ready": False,
                "model_state": "starting" if running else "stopped", **(health or {})}

    def start(self, *, warmup=True, env_file=None, inherited=None, wait_ready=False, timeout=60):
        socket_path(self.path, create=True)
        with process_lock(self.lock_file):
            record = self.record()
            if record and self.matching(record):
                result = self.status()
            else:
                if record:
                    self.clean_dead(record)
                    if self._spawned is not None:
                        self._spawned.wait(timeout=1)
                        self._spawned = None
                elif self.path.exists() or self.path.is_symlink():
                    raise ModelProcessError("기존 소켓의 실행 기록이 없습니다. 덮어쓰지 않았습니다.")
                # A direct ``run`` or an unrecorded startup cannot race a second
                # process into loading another GPU model.
                with process_lock(self.run_lock):
                    pass
                checked_file(self.log_file)
                environment = model_environment(env_file, inherited)
                ModelSettings.from_model_env(environment, warmup=warmup)
                fd = os.open(self.log_file, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                try:
                    process = subprocess.Popen(self.command(warmup), cwd=self.project_dir, env=environment,
                        stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, start_new_session=True, close_fds=True)
                    self._spawned = process
                finally:
                    os.close(fd)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise ModelProcessError("모델 프로세스가 시작 중 종료됐습니다. 전용 로그를 확인하세요.")
                    if self.pid_file.exists():
                        result = self.status()
                        break
                    time.sleep(.05)
                else:
                    result = {"running": True, "pid": process.pid, "alive": False, "ready": False, "model_state": "starting"}
        if wait_ready:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                result = self.status()
                if result["ready"]:
                    break
                if not result["running"] or result["model_state"] == "error":
                    raise ModelProcessError("모델이 준비되지 않았습니다. API는 독립적으로 유지됩니다.")
                time.sleep(.25)
            else:
                raise ModelProcessError("모델 준비 대기 시간이 지났습니다. 프로세스는 그대로 두었습니다.")
        return result

    def _signal(self, record, value):
        # pidfd binds a signal to this process instance, not a recycled PID.
        try:
            fd = open_pidfd(record["pid"])
        except ProcessLookupError:
            return
        try:
            if self.matching(record):
                signal.pidfd_send_signal(fd, value)
        finally:
            os.close(fd)

    def stop(self, *, timeout=20):
        if not self.directory.exists():
            return self.status()
        with process_lock(self.lock_file):
            record = self.record()
            if record is None:
                return self.status()
            if self.matching(record):
                self._signal(record, signal.SIGTERM)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline and self.matching(record):
                    time.sleep(.05)
                if self.matching(record):
                    self._signal(record, signal.SIGKILL)
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and self.matching(record):
                        time.sleep(.05)
                if self.matching(record):
                    raise ModelProcessError("모델 종료를 확인하지 못했습니다. 실행 기록을 보존했습니다.")
            # Normal child shutdown may already have removed its own entries.
            if self.pid_file.exists():
                self.clean_dead(record)
            return self.status()


def serve_model(settings, listener):
    import uvicorn
    from .model_server import create_model_app
    server = uvicorn.Server(uvicorn.Config(create_model_app(settings), workers=1,
        access_log=False, log_level="warning"))
    server.run(sockets=[listener])


def run_model(path: Path, *, warmup=True):
    os.umask(0o077)
    environment = model_environment(None)
    os.environ.clear(); os.environ.update(environment)
    settings = ModelSettings.from_model_env(environment, warmup=warmup)
    path = socket_path(path, create=True)
    directory = path.parent
    pid_file = directory / "model.pid.json"
    with process_lock(directory / "model-run.lock"):
        if path.exists() or path.is_symlink() or pid_file.exists():
            raise ModelProcessError("기존 모델 실행 기록 또는 소켓이 있어 덮어쓰지 않았습니다.")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        record = None
        try:
            listener.bind(str(path)); os.chmod(path, 0o600)
            listener.listen(16); listener.setblocking(False)
            info = path.lstat()
            record = {**process_identity(os.getpid()), "socket": str(path), "socket_dev": info.st_dev, "socket_ino": info.st_ino}
            atomic_record(pid_file, record)
            serve_model(settings, listener)
        finally:
            listener.close()
            if record:
                try:
                    info = path.lstat()
                    if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == (record["socket_dev"], record["socket_ino"]):
                        path.unlink()
                except FileNotFoundError:
                    pass
                if checked_file(pid_file) and json.loads(pid_file.read_text()) == record:
                    pid_file.unlink()


def main(argv=None):
    if os.name == "nt":
        from .win_model_process import main as windows_main
        return windows_main(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "run", "stop", "status"])
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--warmup", dest="warmup", action="store_true", default=True)
    group.add_argument("--no-warmup", dest="warmup", action="store_false")
    parser.add_argument("--wait-ready", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 3600:
        parser.error("timeout must be between 1 and 3600 seconds")
    try:
        if args.action == "run":
            run_model(args.socket, warmup=args.warmup)
            return 0
        controller = ModelController(args.socket)
        if args.action == "start":
            result = controller.start(warmup=args.warmup, env_file=PROJECT_DIR / "server" / ".env",
                                      wait_ready=args.wait_ready, timeout=args.timeout)
        elif args.action == "stop":
            result = controller.stop(timeout=args.timeout)
        else:
            result = controller.status()
        if args.json:
            print(json.dumps(result))
        else:
            print(f"로컬 모델 서버: {result['model_state']} · 프로세스 {'실행' if result['running'] else '종료'}"
                  f" · 통신 {'정상' if result['alive'] else '대기/중단'} · 인식 준비 {'완료' if result['ready'] else '아직'}")
        return 0
    except ModelProcessError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        # No arbitrary subprocess/HTTP/config value is reflected to terminals.
        print("모델 서버 작업을 완료하지 못했습니다. 경로·권한·실행 기록 및 전용 로그를 확인하세요. API는 종료하지 않았습니다.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
