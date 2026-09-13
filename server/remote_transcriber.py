"""API-side adapter for one private Unix-socket Qwen process."""
from __future__ import annotations

import os
import stat
import threading
import time
import uuid
from pathlib import Path

import httpx

from .model_protocol import (
    MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, MAX_STATUS_BYTES, ModelUnavailableError,
    ProtocolError, dump_json, load_json, make_request, read_result, safe_status,
)


def validate_socket_path(value) -> Path:
    """Fail closed on foreign or group/world-accessible runtime/socket paths."""
    if not isinstance(value, (str, Path)):
        raise ModelUnavailableError()
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or len(os.fsencode(path)) > 107:
        raise ModelUnavailableError()
    try:
        # Reject symlink traversal even when the final node itself is a socket.
        for ancestor in (path, *path.parents):
            if stat.S_ISLNK(ancestor.lstat().st_mode):
                raise ModelUnavailableError()
        parent, node = path.parent.stat(), path.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) != 0o700
                or parent.st_uid != os.getuid() or not stat.S_ISSOCK(node.st_mode)
                or stat.S_IMODE(node.st_mode) != 0o600 or node.st_uid != os.getuid()):
            raise ModelUnavailableError()
    except OSError:
        raise ModelUnavailableError() from None
    return path


def _gpu(value):
    if not isinstance(value, dict) or value.get("available") is not True:
        return {"available": False}
    keys = {"available", "name", "total_bytes", "free_bytes", "used_bytes",
            "process_allocated_bytes", "process_reserved_bytes"}
    if set(value) != keys or not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 80 or not value["name"].isprintable():
        raise ProtocolError()
    names = keys - {"available", "name"}
    if any(type(value[key]) is not int or not 0 <= value[key] <= 2**60 for key in names):
        raise ProtocolError()
    if (value["total_bytes"] <= 0 or value["free_bytes"] + value["used_bytes"] != value["total_bytes"]
            or not value["process_allocated_bytes"] <= value["process_reserved_bytes"] <= value["total_bytes"]):
        raise ProtocolError()
    return dict(value)


class RemoteTranscriber:
    supports_boundary_context = True

    def __init__(self, settings):
        self._path = getattr(settings, "local_model_socket", None)
        timeout = getattr(settings, "local_model_timeout_seconds", 90)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 600:
            raise ValueError("Invalid local model timeout")
        self._timeout = float(timeout)
        self._model = str(getattr(settings, "model", "unknown"))
        self._device = str(getattr(settings, "device", "unknown"))
        self._client = None
        self._client_lock, self._status_lock = threading.Lock(), threading.Lock()
        self._closed = False
        self._status_thread = None
        self._cached_status = self._fallback_status()
        self._status_at = float("-inf")

    def _fallback_status(self):
        return {**safe_status({}, model=self._model, device=self._device, state="offline"),
                "gpu": {"available": False}}

    def _get_client(self):
        path = validate_socket_path(self._path)
        with self._client_lock:
            if self._closed:
                raise ModelUnavailableError()
            if self._client is None:
                self._client = httpx.Client(
                    transport=httpx.HTTPTransport(uds=str(path), trust_env=False, retries=0,
                                                  limits=httpx.Limits(max_connections=2, max_keepalive_connections=0)),
                    base_url="http://local-model", timeout=httpx.Timeout(self._timeout, connect=1),
                    trust_env=False, follow_redirects=False,
                )
            return self._client

    def _request(self, method, path, *, body=None, maximum=MAX_RESPONSE_BYTES, status=False):
        try:
            deadline = time.monotonic() + (0.8 if status else self._timeout)
            client = self._get_client()
            timeout = httpx.Timeout(0.8) if status else httpx.Timeout(self._timeout, connect=1)
            with client.stream(method, path, content=body, timeout=timeout,
                               headers={"Content-Type": "application/json", "Accept-Encoding": "identity"}) as response:
                if response.headers.get("content-encoding", "identity").lower() not in {"identity", ""}:
                    raise ProtocolError()
                content_length = response.headers.get("content-length")
                if content_length is not None and (len(content_length) > 20 or not content_length.isdigit() or int(content_length) > maximum):
                    raise ProtocolError()
                payload = bytearray()
                for part in response.iter_raw():
                    if time.monotonic() > deadline:
                        raise ModelUnavailableError("model_timeout")
                    if len(payload) + len(part) > maximum:
                        raise ProtocolError()
                    payload.extend(part)
                if response.status_code != 200:
                    code = "model_busy" if response.status_code == 429 else "model_unavailable"
                    try:
                        failed = load_json(bytes(payload), maximum)
                        if isinstance(failed, dict) and isinstance(failed.get("code"), str) and failed["code"] in {
                            "model_loading", "model_busy", "model_unavailable", "model_protocol_error",
                        }:
                            code = failed["code"]
                    except ProtocolError:
                        pass
                    raise ModelUnavailableError(code)
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise ProtocolError()
                return load_json(bytes(payload), maximum)
        except httpx.TimeoutException:
            raise ModelUnavailableError("model_timeout") from None
        except (httpx.HTTPError, OSError):
            raise ModelUnavailableError() from None
        except ModelUnavailableError:
            raise
        except RuntimeError:
            # HTTPX client-close races are not application tracebacks.
            raise ModelUnavailableError() from None
        except ProtocolError:
            raise ModelUnavailableError("model_protocol_error") from None

    def warmup(self):
        # Loading belongs to the independent model process, never API startup.
        return None

    def status(self):
        if not self._closed and time.monotonic() - self._status_at >= 2 and self._status_lock.acquire(blocking=False):
            self._status_thread = threading.Thread(target=self._refresh_status, name="local-model-status", daemon=True)
            try:
                self._status_thread.start()
            except RuntimeError:
                self._status_thread = None
                self._cached_status = self._fallback_status()
                self._status_at = time.monotonic()
                self._status_lock.release()
        # A slow/misbehaving model endpoint never holds a public API status GET.
        cached = self._cached_status
        return {**cached, "gpu": dict(cached["gpu"])}

    def _refresh_status(self):
        try:
            try:
                value = self._request("GET", "/status", maximum=MAX_STATUS_BYTES, status=True)
                if not isinstance(value, dict) or set(value) != {"model_state", "engine", "model", "device", "gpu"}:
                    raise ProtocolError()
                if not isinstance(value["model_state"], str) or value["model_state"] not in {"unloaded", "loading", "ready", "error", "offline", "busy"}:
                    raise ProtocolError()
                refreshed = {**safe_status(value), "gpu": _gpu(value["gpu"])}
            except (ModelUnavailableError, ProtocolError):
                refreshed = self._fallback_status()
            self._cached_status = self._fallback_status() if self._closed else refreshed
            self._status_at = time.monotonic()
        finally:
            self._status_lock.release()

    def gpu_resources(self):
        # Introspection never initializes ROCm in the API process.
        return dict(self.status()["gpu"])

    def transcribe(self, samples, language, overlap_seconds=0.0, final_chunk=True, *,
                   start_seconds=0.0, boundary_context=None, boundary_output=None):
        try:
            if boundary_output is not None and not isinstance(boundary_output, dict):
                raise ProtocolError()
            request_id = str(uuid.uuid4())
            request = make_request(samples, language, overlap_seconds, final_chunk,
                                   start_seconds=start_seconds, boundary_context=boundary_context,
                                   boundary_requested=boundary_context is not None or boundary_output is not None,
                                   request_id=request_id)
            body = dump_json(request, MAX_REQUEST_BYTES)
            response = self._request("POST", "/transcribe", body=body)
            segments, output = read_result(response, request_id, len(samples))
            if (output is not None) != request["boundary_requested"]:
                raise ProtocolError()
            if boundary_output is not None:
                boundary_output.clear()
                boundary_output.update(output)
            return segments
        except ProtocolError:
            raise ModelUnavailableError("model_protocol_error") from None

    def close(self):
        with self._client_lock:
            self._closed = True
            if self._client is not None:
                self._client.close()
                self._client = None
        self._cached_status = self._fallback_status()
        self._status_at = time.monotonic()
