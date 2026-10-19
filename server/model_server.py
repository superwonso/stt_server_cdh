"""One private local-model process. No application DB, accounts or cloud keys.

The launcher owns the Unix socket and its filesystem permissions. This factory
never discovers application settings or reads .env. GPU calls are serialized;
health and cached status remain available during loading/inference.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import sys
import threading
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from .model_protocol import (
    MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, AlignmentUnavailableError, ModelUnavailableError, ModelRequestCancelledError, ProtocolError,
    dump_json, load_json, make_alignment_failure, make_result, read_request, safe_status,
    make_cancel_request, read_cancel_request, make_cancelled_result,
)
from .model_cancellation import CancellationRegistry, check_cancelled


_LOG = logging.getLogger(__name__)
_FAILURE_STAGES = frozenset({"warmup", "infer_protocol", "infer", "worker"})
_FAILURE_FILES = frozenset({
    "model_server.py", "model_protocol.py", "transcriber.py", "qwen_boundary.py",
    "qwen_language.py", "qwen3_asr.py", "qwen3_forced_aligner.py",
    "modeling_qwen3_asr.py", "modeling_qwen3_asr_thinker.py",
    "modeling_qwen3_forced_aligner.py", "modeling_qwen3.py",
    "generation.py", "utils.py", "module.py", "functional.py", "_tensor.py",
})


def _log_model_failure(stage, error):
    """Log bounded provenance only; never format an exception or its chain."""
    stage = stage if stage in _FAILURE_STAGES else "unknown"
    kind = type(error).__name__
    if not (len(kind) <= 64 and kind.isascii() and kind.isidentifier()):
        kind = "Exception"
    origin, line = "unknown", 0
    trace = BaseException.__getattribute__(error, "__traceback__")
    for _ in range(128):
        if trace is None:
            break
        basename = trace.tb_frame.f_code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
        if basename in _FAILURE_FILES:
            origin, line = basename, trace.tb_lineno
        trace = trace.tb_next
    # No exc_info/stack_info: traceback text can expose private paths, model
    # inputs, credentials in exception messages, and chained exceptions.
    _LOG.error("Local model failure stage=%s exception=%s origin=%s line=%d",
               stage, kind, origin, line)


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
    cancellations = CancellationRegistry()
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
            except Exception as error:
                _log_model_failure("warmup", error)
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

    async def read_body(request, maximum=MAX_REQUEST_BYTES):
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if not 0 <= int(length) <= maximum:
                    raise ProtocolError()
            except ValueError:
                raise ProtocolError() from None
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ProtocolError()
        body = bytearray()
        async for part in request.stream():
            if len(body) + len(part) > maximum:
                raise ProtocolError()
            body.extend(part)
        return load_json(bytes(body), maximum)

    @app.post("/requests/{request_id}/cancel")
    async def cancel_request(request_id: str, request: Request):
        # The native HMAC middleware / private UDS protects this endpoint just
        # like inference. Never acquire gpu_lock or wait for an inference here.
        try:
            value = read_cancel_request(await asyncio.wait_for(read_body(request, 512), timeout=1))
            if value != request_id:
                raise ProtocolError()
        except (ProtocolError, asyncio.TimeoutError):
            return failure("model_protocol_error", 422)
        if not cancellations.cancel(value):
            return failure("model_busy", 429)
        return JSONResponse({**make_cancel_request(value), "cancel_requested": True},
                            headers={"Cache-Control": "no-store"})

    def infer(value):
        try:
            check_cancelled(value["cancel_event"])
            output = {} if value["boundary_requested"] else None
            try:
                options = {"cancel_event": value["cancel_event"]} if getattr(engine, "supports_cancellation", False) else {}
                segments = engine.transcribe(
                    value["samples"], value["language"], value["overlap_seconds"], value["final_chunk"],
                    start_seconds=value["start_seconds"], boundary_context=value["boundary_context"],
                    boundary_output=output,
                    **options,
                )
            except AlignmentUnavailableError as error:
                check_cancelled(value["cancel_event"])
                # Only this typed, fully validated request failure is nonfatal.
                # Invalid exception bodies still hit the fatal ProtocolError path.
                encoded = dump_json(make_alignment_failure(value["request_id"], error.partial_text), MAX_RESPONSE_BYTES)
                set_state("ready")
                return 422, encoded
            check_cancelled(value["cancel_event"])
            result = make_result(value["request_id"], segments, output, len(value["samples"]))
            encoded = dump_json(result, MAX_RESPONSE_BYTES)
            refresh_gpu()
            set_state("ready")
            return 200, encoded
        except ModelRequestCancelledError:
            # Preserve unloaded status when cancellation preceded lazy loading.
            engine_state = engine.status().get("model_state") if hasattr(engine, "status") else "ready"
            set_state("unloaded" if engine_state == "unloaded" else "ready")
            return 409, dump_json(make_cancelled_result(value["request_id"]), MAX_RESPONSE_BYTES)
        except ProtocolError as error:
            # Invalid output is not an ordinary model-busy retry.
            _log_model_failure("infer_protocol", error)
            set_state("error")
            raise ModelUnavailableError("model_protocol_error") from None
        except Exception as error:
            _log_model_failure("infer", error)
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
            except BaseException as failure:
                _log_model_failure("worker", failure)
                set_state("error")
                error = ModelUnavailableError()
            finally:
                # The actual worker, never the cancellable HTTP handler, owns
                # release after queue submission. Lost clients cannot overlap
                # a second GPU operation with this one.
                gpu_lock.release()
                cancellations.finish(value["request_id"], value["cancel_event"])
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
        value = None
        monitor = None
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
            value["cancel_event"] = cancellations.register(value["request_id"])
            if value["cancel_event"] is None:
                return failure("model_busy", 429)
            if value["cancel_event"].is_set():
                return JSONResponse(make_cancelled_result(value["request_id"]), status_code=409,
                                    headers={"Cache-Control": "no-store"})
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
            async def watch_disconnect():
                while not completed.done():
                    if await request.is_disconnected():
                        value["cancel_event"].set()
                        return
                    await asyncio.sleep(.1)
            monitor = asyncio.create_task(watch_disconnect())
            try:
                status_code, encoded = await completed
            except ModelUnavailableError as error:
                return failure(error.code, 503)
            return Response(encoded, status_code=status_code, media_type="application/json",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        finally:
            if monitor is not None:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
            if value is not None and value.get("cancel_event") is not None:
                # An HTTP coroutine cancelled while waiting must not leave an
                # unowned decode running; the GPU worker still owns its lock.
                value["cancel_event"].set()
                if not worker_owns_lock:
                    cancellations.finish(value["request_id"], value["cancel_event"])
            if not worker_owns_lock:
                gpu_lock.release()

    return app
