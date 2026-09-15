"""Native Windows production API/model lifecycle for the restored service.

This is separate from windows_local's synthetic profile. API startup requires
the explicitly provisioned private service.env and an existing restored DB.
The model receives fixed local Qwen settings and no application credentials.
Cloudflare and public configuration publishing are managed separately.
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

from .model_process import ModelProcessError, PROJECT_DIR, command_hash
from .platform_files import (atomic_write_private, current_user_sid, ensure_private_directory,
                             validate_private_path)
from .settings import Settings
from .win_model_process import WindowsModelController, _cleanup, process_identity, process_lock
from .win_model_transport import LoopbackSecurityMiddleware
from .windows_local import SYSTEM_KEYS, fixed_model_environment

SERVICE_ROOT = PROJECT_DIR.parent / "production"
ENV_FILE = SERVICE_ROOT / "config" / "service.env"
DATA_DIR = SERVICE_ROOT / "data"
API_RUNTIME = SERVICE_ROOT / "api-control"
MODEL_RUNTIME = SERVICE_ROOT / "model"
API_PORT = 8765
MODEL_PORT = 18775
CONTROL_PREFIX = "/__windows_control__"


def api_environment():
    # No parent-shell account configuration, keys, proxies or Python hooks.
    selected = {key: os.environ[key] for key in SYSTEM_KEYS if key in os.environ}
    return {**selected, "STT_ENV_FILE": str(ENV_FILE),
            "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1"}


def validate_service_settings(settings):
    expected = {
        "data_dir": DATA_DIR, "model_cache_dir": PROJECT_DIR / ".models",
        "local_model_runtime": MODEL_RUNTIME, "local_model_socket": None,
        "model": "Qwen3-ASR-1.7B", "aligner": "Qwen3-ForcedAligner-0.6B",
        "device": "cuda:0", "compute_type": "bfloat16", "attention": "sdpa",
        "model_warmup": False,
    }
    if any(getattr(settings, key, None) != value for key, value in expected.items()):
        raise ModelProcessError("Production service settings do not match the fixed Windows profile.")
    if not any(origin.startswith("https://") for origin in settings.site_origins):
        raise ModelProcessError("Production service origins must be explicitly configured.")
    return settings


def load_service_settings():
    # Called only in a dedicated CLI/child process; never in a running test API.
    # The constant path defeats ambient STT_ENV_FILE/server/.env selection.
    environment = api_environment()
    os.environ.clear()
    os.environ.update(environment)
    validate_private_path(SERVICE_ROOT, directory=True)
    validate_private_path(ENV_FILE.parent, directory=True)
    validate_private_path(ENV_FILE)
    settings = Settings.from_env()
    return validate_service_settings(settings)


def verify_service_database(settings):
    """Refuse an absent/wrong/synthetic DB before create_app can initialize it."""
    validate_service_settings(settings)
    validate_private_path(DATA_DIR, directory=True)
    validate_private_path(settings.database_path)
    connection = sqlite3.connect(settings.database_path.as_uri() + "?mode=ro", uri=True)
    try:
        users = {row[0] for row in connection.execute("SELECT username FROM users")}
        synthetic = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='windows_local_profile'"
        ).fetchone()
        if users != set(settings.accounts) or synthetic is not None:
            raise ModelProcessError("Restored service database does not match the configured account set.")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ModelProcessError("Restored service database integrity could not be verified.")
    except sqlite3.Error:
        raise ModelProcessError("Restored service database could not be verified; no empty database was created.") from None
    finally:
        connection.close()


class ServiceBoundaryMiddleware:
    """Only the local reverse proxy may reach the public API listener.

    cloudflared must set httpHostHeader=127.0.0.1:8765. Origin/security-header
    validation remains the existing application's policy. The private control
    mount adds its own HMAC and browser-header rejection.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            hosts = [value for key, value in scope.get("headers", []) if key.lower() == b"host"]
            valid = (scope.get("client") and scope["client"][0] == "127.0.0.1" and
                     len(hosts) == 1 and hosts[0] in {f"127.0.0.1:{API_PORT}".encode(),
                                                    f"localhost:{API_PORT}".encode()})
            if not valid:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await send({"type": "http.response.start", "status": 403,
                                "headers": [(b"content-length", b"0"), (b"cache-control", b"no-store")]})
                    await send({"type": "http.response.body", "body": b""})
                return
        return await self.app(scope, receive, send)


def create_control_app(*, token, instance, shutdown, lease_status=None):
    from fastapi import FastAPI
    control = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @control.get("/health")
    async def health():
        result = {"status": "ok", "model_state": "ready"}
        if lease_status is not None:
            # This cached metadata is visible only after local HMAC validation.
            value = lease_status()
            fields = ("enabled", "state", "expires_in_seconds", "last_attempt_at",
                      "last_success_at", "renewing", "error_code")
            result["lease_renewal"] = {key: value[key] for key in fields if key in value}
        return result

    @control.post("/shutdown")
    async def stop():
        shutdown()
        return {"status": "stopping"}

    control.add_middleware(LoopbackSecurityMiddleware, token=token, instance=instance, port=API_PORT)
    return control


def create_service_app(*, token, instance, shutdown):
    from .app import create_app
    # Use the established production factory after load_service_settings has
    # selected the explicit ACL-validated file and checked the restored DB.
    app = create_app()
    lease_worker = getattr(app.state, "lease_renewer", None)
    app.mount(CONTROL_PREFIX, create_control_app(
        token=token, instance=instance, shutdown=shutdown,
        lease_status=lease_worker.status if lease_worker is not None else None,
    ))
    app.add_middleware(ServiceBoundaryMiddleware)
    return app


class ServiceAPIController(WindowsModelController):
    def __init__(self):
        super().__init__(API_RUNTIME, port=API_PORT,
                         command_prefix=[sys.executable, "-m", "server.windows_service"])
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
            raise ModelProcessError("A live service API record cannot be removed.")
        _cleanup(self.directory, record, pid_name="api.pid.json")


def run_api(*, launch_id=""):
    import uvicorn
    settings = load_service_settings()
    verify_service_database(settings)
    ensure_private_directory(API_RUNTIME)
    with process_lock(API_RUNTIME / "api-run.lock"):
        if any((API_RUNTIME / name).exists() for name in ("api.pid.json", "endpoint.json", "auth.token")):
            raise ModelProcessError("Existing service API runtime records were preserved.")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        record = None
        try:
            listener.bind(("127.0.0.1", API_PORT))
            listener.listen(64)
            listener.setblocking(False)
            token, instance = secrets.token_hex(32), secrets.token_hex(16)
            record = {**process_identity(os.getpid()), "sid": current_user_sid(), "project": str(PROJECT_DIR),
                      "runtime": str(API_RUNTIME), "instance": instance, "launch_id": launch_id,
                      "command_hash": command_hash([sys.executable, *sys.orig_argv[1:]]), "port": API_PORT}
            atomic_write_private(API_RUNTIME / "api.pid.json", json.dumps(record).encode())
            atomic_write_private(API_RUNTIME / "auth.token", token.encode("ascii"))
            endpoint = {"version": 1, "host": "127.0.0.1", "port": API_PORT, "instance": instance,
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
            atomic_write_private(API_RUNTIME / "endpoint.json", json.dumps(endpoint).encode())
            server = None
            def shutdown():
                server.should_exit = True
            app = create_service_app(token=token, instance=instance, shutdown=shutdown)
            server = uvicorn.Server(uvicorn.Config(
                app, workers=1, access_log=False, log_level="warning", proxy_headers=False,
                timeout_graceful_shutdown=25,
            ))
            server.run(sockets=[listener])
        finally:
            listener.close()
            if record is not None:
                _cleanup(API_RUNTIME, record, pid_name="api.pid.json")


def status(*, api_only=False, model_only=False):
    # Never load application credentials or open the DB for status/stop.
    result = {"profile": "windows-production", "api_url": f"http://127.0.0.1:{API_PORT}"}
    if not model_only:
        result["api"] = ServiceAPIController().status()
    if not api_only:
        result["model"] = WindowsModelController(MODEL_RUNTIME, port=MODEL_PORT).status()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "start", "stop", "status", "run"])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--api-only", action="store_true")
    group.add_argument("--model-only", action="store_true")
    parser.add_argument("--launch-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--wait-ready", action="store_true")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("This service launcher requires native Windows.")
    if not 1 <= args.timeout <= 3600:
        parser.error("Invalid timeout.")
    try:
        if args.action == "run":
            run_api(launch_id=args.launch_id)
            return 0
        if args.action == "check" or (args.action == "start" and not args.model_only):
            settings = load_service_settings()
            verify_service_database(settings)
        if args.action == "check":
            result = {"profile": "windows-production", "ready_to_start": True,
                      "api_port": API_PORT, "model_port": MODEL_PORT}
        else:
            if args.action == "start":
                validate_private_path(SERVICE_ROOT, directory=True)
                if not args.model_only:
                    ServiceAPIController().start(warmup=False, inherited=api_environment(),
                                                 wait_ready=True, timeout=min(args.timeout, 60))
                if not args.api_only:
                    WindowsModelController(MODEL_RUNTIME, port=MODEL_PORT).start(
                        warmup=True, inherited=fixed_model_environment(),
                        wait_ready=args.wait_ready, timeout=args.timeout,
                    )
            elif args.action == "stop":
                if not args.model_only:
                    ServiceAPIController().stop(timeout=args.timeout)
                if not args.api_only:
                    WindowsModelController(MODEL_RUNTIME, port=MODEL_PORT).stop(timeout=args.timeout)
            result = status(api_only=args.api_only, model_only=args.model_only)
        if args.json:
            print(json.dumps(result))
        else:
            print("Windows production service")
            if args.action == "check":
                print("Private settings and restored database verified.")
            for name in ("api", "model"):
                if name in result:
                    print(f"{name}: {result[name]['model_state']}")
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        # Never reflect an environment value, account, SQL result or exception.
        print("Service action could not be verified. Check private configuration, ACL and owned runtime records.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
