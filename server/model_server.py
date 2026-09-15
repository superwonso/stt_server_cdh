"""One private local-model process. No application DB, accounts or cloud keys.

The launcher owns the Unix socket and its filesystem permissions. This factory
never discovers application settings or reads .env. GPU calls are serialized;
health and cached status remain available during loading/inference.
"""
from __future__ import annotations

import asyncio
import queue
import sys
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from .model_protocol import (
    MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, ModelUnavailableError, ProtocolError,
    dump_json, load_json, make_result, read_request, safe_status,
)


def _gpu_snapshot(device):
    unavailable = {"available": False}
    torch = sys.modules.get("torch")
    if torch is None or not str(device).startswith("cuda"):
        return unavailable
    try:
        if not torch.cuda.is_available():
            return unavailable
        free, total = (int(value) for value in torch.cuda.mem_get_info(device))
        allocated = int(torch.cuda.memory_allocated(device))
        reserved = int(torch.cuda.memory_reserved(device))
        if not 0 <= free <= total or total <= 0 or not 0 <= allocated <= reserved <= total:
            return unavailable
        name = str(torch.cuda.get_device_name(device))
        name = "".join(char for char in name if char.isprintable())[:80]
        return {"available": True, "name": name, "total_bytes": total, "free_bytes": free,
                "used_bytes": total - free, "process_allocated_bytes": allocated,
                "process_reserved_bytes": reserved}
    except Exception:
        return unavailable


def create_model_app(settings, transcriber=None, *, shutdown=None) -> FastAPI:
    if transcriber is None:
        from .transcriber import LocalTranscriber
        transcriber = LocalTranscriber(settings)
    engine = transcriber
    gpu_lock, state_lock = threading.Lock(), threading.Lock()
    stopping = threading.Event()
    state = "unloaded"
    gpu = {"available": False}
    work_queue = queue.Queue(maxsize=1)
    worker_thread = None

    def set_state(value):
        nonlocal state
        with state_lock:
            state = value

    def current_status():
        with state_lock:
            snapshot = safe_status({}, model=getattr(settings, "model", "unknown"),
                                   device=getattr(settings, "device", "unknown"), state=state)
            snapshot["gpu"] = dict(gpu)
            return snapshot

    def refresh_gpu():
        nonlocal gpu
        value = _gpu_snapshot(getattr(settings, "device", "unknown"))
        with state_lock:
            gpu = value

    def warmup():
        with gpu_lock:
            if stopping.is_set():
                return
            try:
                engine.warmup()
                refresh_gpu()
                set_state("ready")
            except Exception:
                # Never log exception repr: model loaders can contain paths or
                # audio/text. The operator sees only a fixed error state.
                set_state("error")

    @asynccontextmanager
    async def lifespan(app):
        nonlocal worker_thread
        stopping.clear()
        if getattr(settings, "model_warmup", True):
            set_state("loading")
        else:
            set_state("unloaded")
        worker_thread = threading.Thread(target=worker_main, name="local-model-gpu", daemon=True)
        try:
            worker_thread.start()
        except RuntimeError:
            worker_thread = None
            set_state("error")
        try:
            yield
        finally:
            stopping.set()
            if worker_thread is not None:
                # A stuck GPU must not hold the API's independent lifecycle.
                await run_in_threadpool(worker_thread.join, 1.0)

    app = FastAPI(title="Private local speech model", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.transcriber = engine

    def failure(code, status):
        error = ModelUnavailableError(code)
        return JSONResponse({"code": error.code, "detail": str(error)}, status_code=status,
                            headers={"Cache-Control": "no-store", "Retry-After": "2"})

    @app.get("/health")
    async def health():
        return {"status": "ok", "model_state": current_status()["model_state"]}

    @app.get("/status")
    async def status():
        return JSONResponse(current_status(), headers={"Cache-Control": "no-store"})

    if shutdown is not None:
        # Installed only by the authenticated native launcher; never exposed
        # by the API application or the existing Unix-socket factory default.
        @app.post("/shutdown")
        async def request_shutdown():
            stopping.set()
            shutdown()
            return JSONResponse({"status": "stopping"}, headers={"Cache-Control": "no-store"})

    async def read_body(request):
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if not 0 <= int(length) <= MAX_REQUEST_BYTES:
                    raise ProtocolError()
            except ValueError:
                raise ProtocolError() from None
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ProtocolError()
        body = bytearray()
        async for part in request.stream():
            if len(body) + len(part) > MAX_REQUEST_BYTES:
                raise ProtocolError()
            body.extend(part)
        return load_json(bytes(body), MAX_REQUEST_BYTES)

    def infer(value):
        try:
            output = {} if value["boundary_requested"] else None
            segments = engine.transcribe(
                value["samples"], value["language"], value["overlap_seconds"], value["final_chunk"],
                start_seconds=value["start_seconds"], boundary_context=value["boundary_context"],
                boundary_output=output,
            )
            result = make_result(value["request_id"], segments, output, len(value["samples"]))
            encoded = dump_json(result, MAX_RESPONSE_BYTES)
            refresh_gpu()
            set_state("ready")
            return encoded
        except ProtocolError:
            # Invalid output is not an ordinary model-busy retry.
            set_state("error")
            raise ModelUnavailableError("model_protocol_error") from None
        except Exception:
            set_state("error")
            raise ModelUnavailableError() from None

    def deliver(completed, result, error):
        if completed.done():
            return
        if error is not None:
            completed.set_exception(error)
        else:
            completed.set_result(result)

    def worker_main():
        # Keep warmup and every inference on one long-lived GPU thread. This
        # avoids introducing per-request ROCm thread/stream context churn.
        if getattr(settings, "model_warmup", True):
            warmup()
        while not (stopping.is_set() and work_queue.empty()):
            try:
                value, loop, completed = work_queue.get(timeout=.2)
            except queue.Empty:
                continue
            result, error = None, None
            try:
                if stopping.is_set():
                    raise ModelUnavailableError()
                result = infer(value)
            except ModelUnavailableError as failure:
                error = failure
            except BaseException:
                set_state("error")
                error = ModelUnavailableError()
            finally:
                # The actual worker, never the cancellable HTTP handler, owns
                # release after queue submission. Lost clients cannot overlap
                # a second GPU operation with this one.
                gpu_lock.release()
            try:
                loop.call_soon_threadsafe(deliver, completed, result, error)
            except RuntimeError:
                pass  # Shutdown already closed this request's event loop.
            # Do not keep a previous caller's private PCM/context while idle.
            del value, loop, completed, result, error

    @app.post("/transcribe")
    async def transcribe(request: Request):
        current = current_status()["model_state"]
        if stopping.is_set() or current == "error":
            return failure("model_unavailable", 503)
        if current == "loading":
            return failure("model_loading", 503)
        if not gpu_lock.acquire(blocking=False):
            return failure("model_busy", 429)
        worker_owns_lock = False
        try:
            try:
                # Protect both memory admission and inference with one bounded
                # slot. Slow local clients cannot retain unlimited PCM bodies.
                raw = await asyncio.wait_for(read_body(request), timeout=5)
                value = read_request(raw)
            except (ProtocolError, asyncio.TimeoutError):
                return failure("model_protocol_error", 422)
            if stopping.is_set():
                return failure("model_unavailable", 503)
            if current == "unloaded":
                set_state("loading")
            loop = asyncio.get_running_loop()
            completed = loop.create_future()

            try:
                if worker_thread is None or not worker_thread.is_alive():
                    return failure("model_unavailable", 503)
                work_queue.put_nowait((value, loop, completed))
            except queue.Full:
                return failure("model_unavailable", 503)
            worker_owns_lock = True
            try:
                encoded = await completed
            except ModelUnavailableError as error:
                return failure(error.code, 503)
            return Response(encoded, media_type="application/json",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        finally:
            if not worker_owns_lock:
                gpu_lock.release()

    return app
