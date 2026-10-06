"""Bounded administrator recovery of the fixed Windows production model only.

Status reads never launch a child. Only the authenticated POST may schedule a
worker; private runtime records and authenticated health are checked again under
the lifecycle lock immediately before stopping or starting a process.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("classroom")
STATES = frozenset({"ready", "error", "offline", "loading", "unknown"})
OPERATIONS = frozenset({"idle", "restarting", "restart_succeeded", "restart_failed"})
MESSAGES = {
    "unsupported": "이 실행 환경에서는 화면에서 로컬 음성 모델을 재시작할 수 없습니다.",
    "restarting": "로컬 음성 모델을 다시 시작하고 있습니다. 잠시 기다려 주세요.",
    "restart_succeeded": "로컬 음성 모델이 다시 준비되었습니다.",
    "restart_failed": "로컬 음성 모델을 다시 시작하지 못했습니다. 현재 상태를 확인해 주세요.",
    "ready": "로컬 음성 모델이 정상 작동 중입니다.",
    "loading": "로컬 음성 모델을 준비하고 있습니다. 잠시 기다려 주세요.",
    "error": "로컬 음성 모델에 오류가 발생했습니다. 다시 시작할 수 있습니다.",
    "offline": "로컬 음성 모델이 꺼져 있습니다. 다시 시작할 수 있습니다.",
    "unknown": "로컬 음성 모델의 실행 상태를 안전하게 확인하지 못했습니다.",
}


class ModelControlError(RuntimeError):
    def __init__(self, code="unavailable"):
        self.code = code if code in {"unsupported", "busy", "state_changed", "unavailable"} else "unavailable"
        super().__init__("로컬 음성 모델 재시작 요청을 확인하지 못했습니다.")


def public_status(*, supported=False, state="unknown", operation="idle", available=False):
    state = state if state in STATES else "unknown"
    operation = operation if operation in OPERATIONS else "idle"
    key = ("unsupported" if not supported else operation if operation == "restarting" else
           "restart_failed" if operation == "restart_failed" and state != "ready" else
           "restart_succeeded" if operation == "restart_succeeded" and state == "ready" else state)
    return {"supported": supported is True, "restart_available": bool(
        supported is True and available is True and state in {"error", "offline"} and operation != "restarting"),
        "operation": operation, "state": state, "message": MESSAGES[key]}


def sanitized_status(value):
    """Do not trust a controller hook's message, keys, or truthy values."""
    if not isinstance(value, dict):
        return public_status()
    return public_status(supported=value.get("supported") is True,
                         state=value.get("state") if isinstance(value.get("state"), str) else "unknown",
                         operation=value.get("operation") if isinstance(value.get("operation"), str) else "idle",
                         available=value.get("restart_available") is True)


class WindowsModelBackend:
    def __init__(self, settings):
        from .windows_service import MODEL_RUNTIME, MODEL_PORT, validate_service_settings
        from .win_model_process import WindowsModelController
        validate_service_settings(settings)
        self.controller = WindowsModelController(MODEL_RUNTIME, port=MODEL_PORT)
        self.port = MODEL_PORT

    def inspect(self):
        # status verifies ACLs, process creation time/executable, and HMAC health.
        # A second record read must still identify exactly the same launch.
        controller = self.controller
        record = controller.record() if controller.directory.exists() else None
        if record is not None and record["port"] != self.port:
            raise ModelControlError()
        status = controller.status()
        current = controller.record() if controller.directory.exists() else None
        if current != record:
            raise ModelControlError("state_changed")
        if status.get("running") is False and status.get("model_state") == "stopped":
            state = "offline"
        elif status.get("running") is True and status.get("alive") is True:
            state = status.get("model_state")
            state = "loading" if state == "unloaded" else state
        elif status.get("running") is True and status.get("model_state") == "starting":
            state = "unknown"  # A running but unauthenticated endpoint is not safely restartable.
        else:
            state = "unknown"
        return {"state": state if state in STATES else "unknown", "record": record}

    def restart(self, snapshot, cancelled):
        from .windows_local import fixed_model_environment
        if cancelled.is_set():
            raise ModelControlError()
        fresh = self.inspect()
        if fresh != snapshot or fresh["state"] not in {"error", "offline"}:
            raise ModelControlError("state_changed")
        expected = fresh["record"]
        if fresh["state"] == "error":
            self.controller.stop(timeout=20, expected_record=expected, require_error=True)
            expected = None
        if cancelled.is_set():
            raise ModelControlError()
        # The start lock checks that no other launch appeared since stop/inspect.
        # Only model selections + safe OS locations are inherited, never API keys.
        self.controller.start(warmup=True, inherited=fixed_model_environment(),
                              wait_ready=False, expected_record=expected)
        deadline = time.monotonic() + 120
        while not cancelled.is_set() and time.monotonic() < deadline:
            current = self.inspect()
            if current["state"] == "ready":
                return
            if current["state"] in {"error", "offline"}:
                raise ModelControlError()
            cancelled.wait(.25)
        # A timeout or API shutdown never kills a separately owned model that is
        # still loading. The administrator sees its fresh state on the next GET.
        raise ModelControlError()


class ModelControl:
    def __init__(self, backend=None):
        self.backend = backend
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._operation = "idle"
        self._thread = None

    def status(self):
        with self._lock:
            operation = self._operation
        state = "unknown"
        if self.backend is not None:
            try:
                state = self.backend.inspect()["state"]
            except Exception:
                pass  # No exception text, path, token, or raw health payload leaves the boundary.
        return public_status(supported=self.backend is not None, state=state, operation=operation,
                             available=not self._shutdown.is_set())

    def request_restart(self, *, audit, authorize):
        with self._lock:
            if self.backend is None:
                raise ModelControlError("unsupported")
            if self._operation == "restarting" or self._shutdown.is_set():
                raise ModelControlError("busy")
            try:
                snapshot = self.backend.inspect()
            except Exception:
                raise ModelControlError() from None
            if snapshot["state"] not in {"error", "offline"} or not authorize():
                raise ModelControlError("state_changed")
            # Never start a privileged action if its accepted audit cannot commit.
            audit("accepted")
            self._operation = "restarting"
            self._thread = threading.Thread(target=self._run, args=(snapshot, audit, authorize),
                                            name="admin-model-restart", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._operation = "restart_failed"
                self._record(audit, "failed")
                raise ModelControlError() from None
            return public_status(supported=True, state=snapshot["state"], operation="restarting")

    @staticmethod
    def _record(audit, result):
        try:
            audit(result)
        except Exception:
            log.warning("Administrator model audit could not be stored.")

    def _run(self, snapshot, audit, authorize):
        result = "failed"
        try:
            if self._shutdown.is_set() or not authorize():
                raise ModelControlError()
            self.backend.restart(snapshot, self._shutdown)
            result = "success"
        except Exception:
            # Details are available only through the existing private model logs.
            pass
        finally:
            self._record(audit, result)
            with self._lock:
                self._operation = "restart_succeeded" if result == "success" else "restart_failed"

    def request_shutdown(self):
        self._shutdown.set()

    def stop(self, timeout=0):
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0, timeout))
        return thread is None or not thread.is_alive()


def create_model_control(settings, *, enabled=False):
    # Explicit Settings fixtures / local-test profile / Linux never gain process
    # control just by pointing LOCAL_MODEL_RUNTIME at a private directory.
    if not enabled or os.name != "nt":
        return ModelControl()
    try:
        from .windows_service import ENV_FILE, SERVICE_ROOT
        from .platform_files import validate_private_path
        if Path(os.environ.get("STT_ENV_FILE", "")) != ENV_FILE:
            return ModelControl()
        validate_private_path(SERVICE_ROOT, directory=True)
        validate_private_path(ENV_FILE.parent, directory=True)
        validate_private_path(ENV_FILE)
        return ModelControl(WindowsModelBackend(settings))
    except Exception:
        return ModelControl()
