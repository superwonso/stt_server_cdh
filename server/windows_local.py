"""An isolated, native Windows test profile. Never reads the service .env.

Only fresh synthetic accounts and this project's .windows-local SQLite database
are used. No cloud credentials, tunnel, backup, public address or deployment is
loaded. The API and GPU model are separate, owned processes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import time

from .model_process import PROJECT_DIR, ModelProcessError, command_hash
from .platform_files import (atomic_write_private, current_user_sid, ensure_private_directory,
                             validate_private_path)
from .settings import Settings, account_usernames
from .win_model_process import (DEFAULT_PORT as MODEL_PORT, WindowsModelController, _cleanup,
                                process_identity, process_lock, read_record)
from .win_model_transport import LoopbackSecurityMiddleware

LOCAL_ROOT = PROJECT_DIR / ".windows-local"
DATA_DIR = LOCAL_ROOT / "data"
MODEL_RUNTIME = LOCAL_ROOT / "model"
API_RUNTIME = LOCAL_ROOT / "api-control"
PROFILE_FILE = LOCAL_ROOT / "test-accounts.json"
API_PORT = 18766
PROFILE_KIND = "isolated-windows-test"
CONTROL_PREFIX = "/__windows_control__"
SYSTEM_KEYS = ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "USERNAME")


def new_profile():
    prefix = "test-" + secrets.token_hex(4)
    return {"version": 1, "kind": PROFILE_KIND, "project": str(PROJECT_DIR), "id": secrets.token_hex(16),
            "accounts": [{"username": prefix + suffix, "password": secrets.token_urlsafe(24)} for suffix in ("-a", "-b")]}


def validate_profile(value):
    try:
        if (not isinstance(value, dict) or set(value) != {"version", "kind", "project", "id", "accounts"}
                or type(value["version"]) is not int or value["version"] != 1
                or value["kind"] != PROFILE_KIND or value["project"] != str(PROJECT_DIR)
                or not isinstance(value["id"], str) or len(value["id"]) != 32
                or any(char not in "0123456789abcdef" for char in value["id"])
                or not isinstance(value["accounts"], list) or len(value["accounts"]) != 2):
            raise ValueError
        for account in value["accounts"]:
            if (not isinstance(account, dict) or set(account) != {"username", "password"}
                    or not isinstance(account["username"], str) or not account["username"].startswith("test-")
                    or not isinstance(account["password"], str) or not 24 <= len(account["password"]) <= 128
                    or not account["password"].isascii() or not account["password"].isprintable()):
                raise ValueError
        account_usernames(",".join(account["username"] for account in value["accounts"]))
        return value
    except (ValueError, TypeError, KeyError):
        raise ModelProcessError("독립 테스트 프로필을 안전하게 확인하지 못했습니다.") from None


def read_profile():
    validate_private_path(LOCAL_ROOT, directory=True)
    validate_private_path(PROFILE_FILE)
    if PROFILE_FILE.stat().st_size > 8192:
        raise ModelProcessError("테스트 프로필 크기가 올바르지 않습니다.")
    return validate_profile(json.loads(PROFILE_FILE.read_text(encoding="utf-8")))


def local_settings(profile):
    profile = validate_profile(profile)
    accounts = tuple(account["username"] for account in profile["accounts"])
    return Settings(data_dir=DATA_DIR, model_cache_dir=PROJECT_DIR / ".models", accounts=accounts,
                    admin_username=accounts[0],
                    site_origins=(f"http://127.0.0.1:{API_PORT}", f"http://localhost:{API_PORT}"),
                    model="Qwen3-ASR-1.7B", aligner="Qwen3-ForcedAligner-0.6B",
                    device="cuda:0", compute_type="bfloat16", attention="sdpa", model_warmup=False,
                    local_model_runtime=MODEL_RUNTIME, local_model_socket=None,
                    google_drive_enabled=False, google_drive_oauth_client_path=None, google_drive_token_path=None,
                    clova_speech_secret_key=None, mindlogic_api_key=None)


def verify_test_database(profile):
    settings = local_settings(profile)
    validate_private_path(DATA_DIR, directory=True)
    validate_private_path(settings.database_path)
    # No schema migration or write occurs while checking the provenance marker.
    connection = sqlite3.connect(settings.database_path.as_uri() + "?mode=ro", uri=True)
    try:
        marker = connection.execute("SELECT profile_id FROM windows_local_profile").fetchall()
        users = {row[0] for row in connection.execute("SELECT username FROM users")}
        if marker != [(profile["id"],)] or users != set(settings.accounts):
            raise ModelProcessError("이 DB가 생성한 테스트 프로필과 일치하지 않습니다. 보존했습니다.")
    except sqlite3.Error:
        raise ModelProcessError("테스트 DB 출처를 확인하지 못했습니다. 임의 초기화하지 않았습니다.") from None
    finally:
        connection.close()


def initialize():
    from .db import Database
    from .security import PASSWORD_HASHER
    ensure_private_directory(LOCAL_ROOT)
    with process_lock(LOCAL_ROOT / "profile-init.lock"):
        for directory in (DATA_DIR, API_RUNTIME, MODEL_RUNTIME):
            ensure_private_directory(directory)
        database_path = DATA_DIR / "classroom.sqlite3"
        if not PROFILE_FILE.exists():
            if any(DATA_DIR.iterdir()):
                raise ModelProcessError("출처가 확인되지 않은 기존 테스트 폴더 자료를 보존했습니다.")
            profile = new_profile()
            atomic_write_private(PROFILE_FILE, json.dumps(profile).encode())
        else:
            profile = read_profile()
        if not database_path.exists():
            # Never copy, reset, import or synchronize an existing database.
            if any(DATA_DIR.iterdir()):
                raise ModelProcessError("DB가 없는 기존 테스트 자료를 보존했습니다. 새 DB를 만들지 않았습니다.")
            database = Database(database_path, local_settings(profile).accounts)
            database.initialize()
            with database.connect() as connection:
                connection.execute("CREATE TABLE windows_local_profile (profile_id TEXT PRIMARY KEY)")
                connection.execute("INSERT INTO windows_local_profile(profile_id) VALUES (?)", (profile["id"],))
                for account in profile["accounts"]:
                    connection.execute("UPDATE users SET password_hash = ?, setup_hash = NULL, setup_expires = NULL WHERE username = ?",
                                       (PASSWORD_HASHER.hash(account["password"]), account["username"]))
        verify_test_database(profile)
    return {"initialized": True, "profile": PROFILE_KIND, "api_url": f"http://127.0.0.1:{API_PORT}"}


def fixed_model_environment():
    # Copy only Windows runtime locations and the unchanged model selections.
    env = {key: os.environ[key] for key in SYSTEM_KEYS if key in os.environ}
    env.update({"MODEL_CACHE_DIR": str(PROJECT_DIR / ".models"), "ASR_MODEL": "Qwen3-ASR-1.7B",
                "ASR_ALIGNER": "Qwen3-ForcedAligner-0.6B", "ASR_DEVICE": "cuda:0",
                "ASR_DTYPE": "bfloat16", "ASR_ATTENTION": "sdpa", "STABILITY_GUARD_SECONDS": "0.6"})
    return env


def deny_external_io(event, args):
    """Process-local guard for this API child; normal service paths are untouched."""
    if event in {"socket.connect", "socket.sendto"}:
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise PermissionError("External network is disabled in the isolated test API")
    elif event == "socket.getaddrinfo":
        if args[0] not in {None, "127.0.0.1", "::1", "localhost"}:
            raise PermissionError("External name resolution is disabled in the isolated test API")
    elif event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.posix_spawnp"}:
        raise PermissionError("Child commands are disabled in the isolated test API")


class LocalBoundaryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = scope.get("headers", [])
            hosts = [value for key, value in headers if key.lower() == b"host"]
            if (not scope.get("client") or scope["client"][0] != "127.0.0.1"
                    or len(hosts) != 1 or hosts[0] not in {f"127.0.0.1:{API_PORT}".encode(), f"localhost:{API_PORT}".encode()}):
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-length", b"0"), (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": b""})
                return
        return await self.app(scope, receive, send)


def create_control_app(*, token, instance, shutdown):
    from fastapi import FastAPI
    control = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    @control.get("/health")
    async def health():
        return {"status": "ok", "model_state": "ready"}
    @control.post("/shutdown")
    async def stop():
        shutdown()
        return {"status": "stopping"}
    control.add_middleware(LoopbackSecurityMiddleware, token=token, instance=instance, port=API_PORT)
    return control


def create_local_app(profile, *, token, instance, shutdown, transcriber=None):
    from .app import create_app
    settings = local_settings(profile)
    app = create_app(settings, transcriber=transcriber,
                     tunnel_status=lambda: {"state": "offline", "operation": "idle", "restart_available": False,
                                            "remote_recovery_possible": False, "local_start_required": False},
                     tunnel_restart=None)
    control = create_control_app(token=token, instance=instance, shutdown=shutdown)
    app.mount(CONTROL_PREFIX, control)
    from .windows_web import attach_local_web
    attach_local_web(app, API_PORT)
    app.add_middleware(LocalBoundaryMiddleware)
    return app


class WindowsAPIController(WindowsModelController):
    def __init__(self):
        super().__init__(API_RUNTIME, port=API_PORT, command_prefix=[sys.executable, "-m", "server.windows_local"])
        self.pid_file = self.directory / "api.pid.json"
        self.lock_file = self.directory / "api-control.lock"
        self.run_lock = self.directory / "api-run.lock"
        self.log_file = self.directory / "api.log"

    def command(self, warmup=False, launch_id=""):
        command = [*self.command_prefix, "run"]
        return command + (["--launch-id", launch_id] if launch_id else [])

    def request(self, record, method, path):
        return super().request(record, method, CONTROL_PREFIX + path)

    def clean_dead(self, record):
        if self.matching(record):
            raise ModelProcessError("실행 중인 API 기록은 정리하지 않습니다.")
        _cleanup(self.directory, record, pid_name="api.pid.json")


def run_api():
    import uvicorn
    profile = read_profile()
    verify_test_database(profile)
    ensure_private_directory(API_RUNTIME)
    with process_lock(API_RUNTIME / "api-run.lock"):
        if any((API_RUNTIME / name).exists() for name in ("api.pid.json", "endpoint.json", "auth.token")):
            raise ModelProcessError("기존 API 실행 기록을 보존했습니다.")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        record = None
        try:
            listener.bind(("127.0.0.1", API_PORT))
            listener.listen(64)
            listener.setblocking(False)
            token, instance = secrets.token_hex(32), secrets.token_hex(16)
            launch_id = sys.argv[sys.argv.index("--launch-id") + 1] if "--launch-id" in sys.argv else ""
            record = {**process_identity(os.getpid()), "sid": current_user_sid(), "project": str(PROJECT_DIR),
                      "runtime": str(API_RUNTIME), "instance": instance, "launch_id": launch_id,
                      "command_hash": command_hash([sys.executable, *sys.orig_argv[1:]]), "port": API_PORT}
            atomic_write_private(API_RUNTIME / "api.pid.json", json.dumps(record).encode())
            atomic_write_private(API_RUNTIME / "auth.token", token.encode("ascii"))
            endpoint = {"version": 1, "host": "127.0.0.1", "port": API_PORT, "instance": instance,
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
            atomic_write_private(API_RUNTIME / "endpoint.json", json.dumps(endpoint).encode())
            sys.addaudithook(deny_external_io)
            server = None
            def shutdown():
                server.should_exit = True
            app = create_local_app(profile, token=token, instance=instance, shutdown=shutdown)
            server = uvicorn.Server(uvicorn.Config(app, workers=1, access_log=False, log_level="warning",
                                                 proxy_headers=False, timeout_graceful_shutdown=25))
            server.run(sockets=[listener])
        finally:
            listener.close()
            if record is not None:
                _cleanup(API_RUNTIME, record, pid_name="api.pid.json")


def status(*, api_only=False, model_only=False):
    result = {"profile": PROFILE_KIND, "initialized": PROFILE_FILE.exists(), "api_url": f"http://127.0.0.1:{API_PORT}"}
    if not model_only:
        result["api"] = WindowsAPIController().status()
    if not api_only:
        result["model"] = WindowsModelController(MODEL_RUNTIME, port=MODEL_PORT).status()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "start", "stop", "status", "run"])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--api-only", action="store_true")
    group.add_argument("--model-only", action="store_true")
    parser.add_argument("--launch-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--wait-ready", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 3600:
        parser.error("timeout must be between 1 and 3600 seconds")
    if os.name != "nt":
        parser.error("This profile requires native Windows")
    try:
        if args.action == "run":
            run_api()
            return 0
        if args.action == "init":
            result = initialize()
        else:
            if args.action == "start":
                initialize()
                if not args.model_only:
                    WindowsAPIController().start(warmup=False,
                        inherited={key: os.environ[key] for key in SYSTEM_KEYS if key in os.environ},
                        wait_ready=True, timeout=min(args.timeout, 60))
                if not args.api_only:
                    WindowsModelController(MODEL_RUNTIME, port=MODEL_PORT).start(
                        warmup=True, inherited=fixed_model_environment(), wait_ready=args.wait_ready, timeout=args.timeout)
            elif args.action == "stop":
                if not args.model_only:
                    WindowsAPIController().stop(timeout=args.timeout)
                if not args.api_only:
                    WindowsModelController(MODEL_RUNTIME, port=MODEL_PORT).stop(timeout=args.timeout)
            result = status(api_only=args.api_only, model_only=args.model_only)
        if args.json:
            print(json.dumps(result))
        else:
            print("Windows 독립 테스트 환경: " + ("준비됨" if result["initialized"] else "초기화 전"))
            for key in ("api", "model"):
                if key in result:
                    print(f"{key}: {result[key]['model_state']}")
            print(result["api_url"])
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        print("독립 테스트 환경 작업을 완료하지 못했습니다. 비공개 실행 로그·ACL·프로필을 확인하세요. 기존 운영 환경은 변경하지 않았습니다.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
