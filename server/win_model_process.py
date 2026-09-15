"""Native Windows model lifecycle with private ACL files and PID-safe handles."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

from .model_process import ModelProcessError, ModelSettings, PROJECT_DIR, command_hash, model_environment
from .platform_files import (atomic_write_private, current_user_sid, ensure_private_directory,
                             file_lock, open_file, validate_private_path)
from .win_model_transport import (AUTH_HEADER, HEX32, LoopbackSecurityMiddleware,
                                  read_endpoint, request_auth, verify_response)
from .model_protocol import ModelUnavailableError

DEFAULT_RUNTIME = PROJECT_DIR / ".data" / "model-server-windows"
DEFAULT_PORT = 18765

if os.name == "nt":
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL


class ProcessHandle:
    """A handle stays bound to one process instance, even if its PID is recycled."""
    def __init__(self, pid, *, terminate=False):
        if os.name != "nt" or type(pid) is not int or pid <= 0:
            raise ModelProcessError("Windows 프로세스 식별자가 올바르지 않습니다.")
        self.pid = pid
        self.handle = kernel.OpenProcess(0x1000 | 0x100000 | (0x0001 if terminate else 0), False, pid)
        if not self.handle:
            if ctypes.get_last_error() == 87:
                raise ProcessLookupError
            raise ModelProcessError("프로세스 소유와 생성 시각을 확인할 권한이 없습니다.")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.handle:
            kernel.CloseHandle(self.handle)
            self.handle = None

    def wait(self, seconds=0):
        result = kernel.WaitForSingleObject(self.handle, min(int(seconds * 1000), 0xFFFFFFFE))
        if result == 0:
            return True
        if result == 258:
            return False
        raise ModelProcessError("프로세스 종료 상태를 안전하게 확인하지 못했습니다.")

    def identity(self):
        if self.wait():
            return None
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(self.handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
            raise ModelProcessError("프로세스 생성 시각을 읽지 못했습니다.")
        name, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if not kernel.QueryFullProcessImageNameW(self.handle, 0, name, ctypes.byref(length)):
            raise ModelProcessError("프로세스 실행 파일을 읽지 못했습니다.")
        return {"pid": self.pid, "created": str((created.dwHighDateTime << 32) | created.dwLowDateTime),
                "exe": os.path.normcase(os.path.realpath(name.value))}

    def terminate(self, expected):
        live = self.identity()
        if live is None:
            return
        if any(live.get(key) != expected.get(key) for key in ("pid", "created", "exe")):
            raise ModelProcessError("PID가 다른 프로세스를 가리켜 종료하지 않았습니다.")
        if not kernel.TerminateProcess(self.handle, 1):
            if self.wait(.1):
                return  # It exited between the identity check and termination.
            raise ModelProcessError("프로젝트 프로세스 종료를 완료하지 못했습니다.")


def process_identity(pid):
    try:
        with ProcessHandle(pid) as handle:
            return handle.identity()
    except ProcessLookupError:
        return None


def runtime_path(value, *, create=False):
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts or str(path).startswith("\\\\")
            or path.resolve() != path):
        raise ModelProcessError("모델 실행 폴더는 재분석점이 없는 로컬 절대 경로여야 합니다.")
    if create:
        ensure_private_directory(path)
    if path.exists():
        validate_private_path(path, directory=True)
    return path


@contextmanager
def process_lock(path):
    fd = open_file(path, os.O_CREAT | os.O_RDWR, private=True)
    try:
        try:
            with file_lock(fd, blocking=False):
                yield
        except BlockingIOError:
            raise ModelProcessError("다른 모델 실행·종료 작업이 진행 중입니다.") from None
    finally:
        os.close(fd)


def read_record(path):
    try:
        validate_private_path(path)
        if path.stat().st_size > 8192:
            raise ValueError
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, TypeError):
        raise ModelProcessError("모델 실행 기록 형식이 올바르지 않습니다.") from None


class WindowsModelController:
    def __init__(self, runtime=DEFAULT_RUNTIME, *, port=DEFAULT_PORT, project_dir=PROJECT_DIR, command_prefix=None):
        self.directory = runtime_path(runtime)
        self.pid_file = self.directory / "model.pid.json"
        self.lock_file = self.directory / "model-control.lock"
        self.run_lock = self.directory / "model-run.lock"
        self.log_file = self.directory / "model.log"
        self.project_dir = Path(project_dir).resolve()
        self.command_prefix = command_prefix or [sys.executable, "-m", "server.model_process"]
        if type(port) is not int or not (port == 0 or 1024 <= port <= 65535):
            raise ModelProcessError("모델 포트 범위가 올바르지 않습니다.")
        self.port = port
        self._spawned = None

    def command(self, warmup, launch_id=""):
        command = [*self.command_prefix, "run", "--runtime", str(self.directory), "--port", str(self.port),
                   "--warmup" if warmup else "--no-warmup"]
        return command + (["--launch-id", launch_id] if launch_id else [])

    def record(self):
        value = read_record(self.pid_file)
        if value is None:
            return None
        expected = {"pid", "created", "exe", "sid", "project", "runtime", "instance", "launch_id", "command_hash", "port"}
        try:
            if (not isinstance(value, dict) or set(value) != expected
                    or type(value["pid"]) is not int or value["pid"] <= 0
                    or not isinstance(value["created"], str) or not value["created"].isdigit()
                    or value["sid"] != current_user_sid() or value["project"] != str(self.project_dir)
                    or value["runtime"] != str(self.directory)
                    or value["exe"] != process_identity(os.getpid())["exe"]
                    or not isinstance(value["instance"], str) or not HEX32.fullmatch(value["instance"])
                    or not isinstance(value["launch_id"], str)
                    or (value["launch_id"] and not HEX32.fullmatch(value["launch_id"]))
                    or value["command_hash"] not in {command_hash(self.command(warmup, value["launch_id"])) for warmup in (False, True)}
                    or type(value["port"]) is not int or not 1024 <= value["port"] <= 65535):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ModelProcessError("모델 실행 기록이 현재 프로젝트와 일치하지 않아 보존했습니다.") from None
        return value

    def matching(self, record):
        live = process_identity(record["pid"])
        if live is None:
            return False
        if any(live[key] != record[key] for key in live):
            raise ModelProcessError("PID가 다른 프로세스를 가리켜 종료하거나 정리하지 않았습니다.")
        return True

    def request(self, record, method, path):
        import httpx
        endpoint, token = read_endpoint(self.directory)
        if endpoint["instance"] != record["instance"] or endpoint["port"] != record["port"]:
            raise ModelProcessError("모델 통신 기록이 바뀌어 요청하지 않았습니다.")
        auth, nonce = request_auth(token, endpoint["instance"], method, path)
        deadline = time.monotonic() + 2
        with httpx.Client(trust_env=False, timeout=2, follow_redirects=False) as client:
            with client.stream(method, f'http://127.0.0.1:{endpoint["port"]}{path}',
                               headers={AUTH_HEADER: auth, "Accept-Encoding": "identity", "Connection": "close"}) as response:
                body = bytearray()
                for part in response.iter_raw():
                    if time.monotonic() > deadline:
                        raise ModelUnavailableError()
                    body.extend(part)
                    if len(body) > 16384:
                        raise ModelUnavailableError()
                verify_response(token, endpoint, nonce, response, bytes(body))
                if response.status_code != 200:
                    raise ModelUnavailableError()
                return json.loads(body)

    def health(self, record):
        import httpx
        try:
            value = self.request(record, "GET", "/health")
            if value.get("status") != "ok" or value.get("model_state") not in {"unloaded", "loading", "ready", "error"}:
                return None
            return {"alive": True, "ready": value["model_state"] == "ready", "model_state": value["model_state"]}
        except (httpx.HTTPError, OSError, ValueError, ModelUnavailableError):
            return None

    def status(self):
        if not self.directory.exists():
            return {"running": False, "alive": False, "ready": False, "model_state": "stopped"}
        runtime_path(self.directory)
        record = self.record()
        if record is None:
            if any((self.directory / name).exists() for name in ("endpoint.json", "auth.token")):
                raise ModelProcessError("실행 기록이 없는 인증정보를 발견하여 보존했습니다.")
            return {"running": False, "alive": False, "ready": False, "model_state": "stopped"}
        running = self.matching(record)
        return {"running": running, "pid": record["pid"], "alive": False, "ready": False,
                "model_state": "starting" if running else "stopped", **((self.health(record) or {}) if running else {})}

    def clean_dead(self, record):
        if self.matching(record):
            raise ModelProcessError("실행 중인 모델 기록은 정리하지 않습니다.")
        _cleanup(self.directory, record)

    def start(self, *, warmup=True, env_file=None, inherited=None, wait_ready=False, timeout=60):
        runtime_path(self.directory, create=True)
        with process_lock(self.lock_file):
            record = self.record()
            if record and self.matching(record):
                result = self.status()
            else:
                if record:
                    self.clean_dead(record)
                self.status()  # Reject unrecorded endpoint/token before creating anything.
                with process_lock(self.run_lock):
                    pass
                environment = model_environment(env_file, inherited)
                ModelSettings.from_model_env(environment, warmup=warmup)
                fd = open_file(self.log_file, os.O_CREAT | os.O_APPEND | os.O_WRONLY, private=True)
                launch_id = secrets.token_hex(16)
                try:
                    from .win_model_launch import OwnedLaunch
                    try:
                        with OwnedLaunch(self.command(warmup, launch_id), cwd=self.project_dir,
                                         env=environment, stdout=fd) as launch:
                            self._spawned = launch.process
                            deadline = time.monotonic() + 10
                            while time.monotonic() < deadline:
                                if self._spawned.poll() is not None:
                                    raise ModelProcessError("모델 프로세스가 시작 중 종료됐습니다. 전용 로그를 확인하세요.")
                                if self.pid_file.exists() and (self.directory / "endpoint.json").exists():
                                    registered = self.record()
                                    endpoint, _ = read_endpoint(self.directory)
                                    if (registered is None or registered["launch_id"] != launch_id
                                            or registered["instance"] != endpoint["instance"]
                                            or registered["port"] != endpoint["port"]):
                                        raise ModelProcessError("다른 시작 작업의 실행 기록을 보존했습니다.")
                                    result = self.status()
                                    launch.commit(registered)
                                    break
                                time.sleep(.05)
                            else:
                                raise ModelProcessError("시작 등록 시간이 지나 이 작업이 만든 프로세스만 종료했습니다.")
                    except BaseException:
                        # The armed job has already stopped only this launch's
                        # descendants. Never remove a concurrent run's record.
                        failed = self.record()
                        if failed is not None and failed["launch_id"] == launch_id and not self.matching(failed):
                            self.clean_dead(failed)
                        self._spawned = None
                        raise
                finally:
                    os.close(fd)
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
                raise ModelProcessError("모델 준비 대기 시간이 지났습니다. 프로세스는 유지합니다.")
        return result

    def stop(self, *, timeout=20):
        import httpx
        if not self.directory.exists():
            return self.status()
        with process_lock(self.lock_file):
            record = self.record()
            if record is None:
                return self.status()
            try:
                with ProcessHandle(record["pid"], terminate=True) as handle:
                    live = handle.identity()
                    if live is not None:
                        if any(live[key] != record[key] for key in live):
                            raise ModelProcessError("PID가 다른 프로세스를 가리켜 종료하지 않았습니다.")
                        try:
                            self.request(record, "POST", "/shutdown")
                        except (httpx.HTTPError, ModelUnavailableError, OSError, ValueError):
                            pass  # Only this verified process handle may be terminated after the grace period.
                        if not handle.wait(timeout):
                            handle.terminate(record)
                            if not handle.wait(3):
                                raise ModelProcessError("모델 종료를 확인하지 못하여 실행 기록을 보존했습니다.")
            except ProcessLookupError:
                pass
            if self.pid_file.exists():
                self.clean_dead(record)
            if self._spawned is not None:
                self._spawned.wait(timeout=3)
                self._spawned = None
            return self.status()


def _cleanup(directory, record, *, pid_name="model.pid.json"):
    if read_record(directory / pid_name) != record:
        raise ModelProcessError("실행 기록이 바뀌어 정리하지 않았습니다.")
    endpoint_file, token_file = directory / "endpoint.json", directory / "auth.token"
    if endpoint_file.exists():
        endpoint, _ = read_endpoint(directory)
        if endpoint["instance"] != record["instance"]:
            raise ModelProcessError("통신 기록이 바뀌어 보존했습니다.")
        endpoint_file.unlink()
    if token_file.exists():
        validate_private_path(token_file)
        token_file.unlink()
    (directory / pid_name).unlink()


def serve_model(settings, listener, *, token, instance):
    import uvicorn
    from .model_server import create_model_app
    server = None
    def shutdown():
        server.should_exit = True
    app = create_model_app(settings, shutdown=shutdown)
    app.add_middleware(LoopbackSecurityMiddleware, token=token, instance=instance, port=listener.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, workers=1, access_log=False, log_level="warning",
                                         proxy_headers=False, timeout_graceful_shutdown=15))
    server.run(sockets=[listener])


def run_model(runtime, *, port=DEFAULT_PORT, warmup=True):
    environment = model_environment(None)
    os.environ.clear()
    os.environ.update(environment)
    settings = ModelSettings.from_model_env(environment, warmup=warmup)
    directory = runtime_path(runtime, create=True)
    with process_lock(directory / "model-run.lock"):
        if any((directory / name).exists() for name in ("model.pid.json", "endpoint.json", "auth.token")):
            raise ModelProcessError("기존 모델 실행 기록이 있어 덮어쓰지 않았습니다.")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        record = None
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen(16)
            listener.setblocking(False)
            token, instance = secrets.token_hex(32), secrets.token_hex(16)
            launch_id = sys.argv[sys.argv.index("--launch-id") + 1] if "--launch-id" in sys.argv else ""
            record = {**process_identity(os.getpid()), "sid": current_user_sid(), "project": str(PROJECT_DIR),
                      "runtime": str(directory), "instance": instance, "launch_id": launch_id,
                      # Windows venv's redirector substitutes orig_argv[0]
                      # with base Python while sys.executable retains the
                      # requested venv executable. Its PID also differs from
                      # Popen.pid, so the child records its own kernel identity.
                      "command_hash": command_hash([sys.executable, *sys.orig_argv[1:]]),
                      "port": listener.getsockname()[1]}
            atomic_write_private(directory / "model.pid.json", json.dumps(record).encode())
            atomic_write_private(directory / "auth.token", token.encode("ascii"))
            endpoint = {"version": 1, "host": "127.0.0.1", "port": record["port"], "instance": instance,
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
            atomic_write_private(directory / "endpoint.json", json.dumps(endpoint).encode())
            serve_model(settings, listener, token=token, instance=instance)
        finally:
            listener.close()
            if record is not None:
                _cleanup(directory, record)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "run", "stop", "status"])
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--launch-id", default="", help=argparse.SUPPRESS)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--warmup", dest="warmup", action="store_true", default=True)
    group.add_argument("--no-warmup", dest="warmup", action="store_false")
    parser.add_argument("--wait-ready", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 3600 or not (args.port == 0 or 1024 <= args.port <= 65535):
        parser.error("Invalid timeout or port")
    try:
        if args.action == "run":
            run_model(args.runtime, port=args.port, warmup=args.warmup)
            return 0
        controller = WindowsModelController(args.runtime, port=args.port)
        if args.action == "start":
            result = controller.start(warmup=args.warmup, env_file=args.env_file,
                                      wait_ready=args.wait_ready, timeout=args.timeout)
        elif args.action == "stop":
            result = controller.stop(timeout=args.timeout)
        else:
            result = controller.status()
        if args.json:
            print(json.dumps(result))
        else:
            print(f"로컬 모델 서버: {result['model_state']} · 프로세스 {'실행' if result['running'] else '종료'}")
        return 0
    except (ModelProcessError, OSError, ValueError, ModelUnavailableError):
        print("Windows 모델 서버 작업을 완료하지 못했습니다. 비공개 로그·ACL·실행 기록을 확인하세요. API는 유지합니다.", file=sys.stderr)
        return 1
