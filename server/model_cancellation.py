"""Request-local cancellation. No model, account, audio or global kill state."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time

from .model_protocol import ModelRequestCancelledError

_CURRENT = ContextVar("qwen_request_cancellation", default=None)


def check_cancelled(event=None):
    event = event if event is not None else _CURRENT.get()
    if event is not None and event.is_set():
        raise ModelRequestCancelledError() from None


@contextmanager
def cancellation_scope(event):
    token = _CURRENT.set(event)
    try:
        check_cancelled()
        yield
        check_cancelled()
    finally:
        _CURRENT.reset(token)


class RequestStoppingCriteria:
    def __init__(self):
        self.event = _CURRENT.get()

    def __call__(self, input_ids, scores, **kwargs):
        # Transformers' StoppingCriteriaList broadcasts this bool to all rows
        # in this one request. No CUDA-to-CPU synchronization is introduced.
        return self.event is not None and self.event.is_set()


class CancellableAligner:
    """Check around the existing single forward pass, never interrupt CUDA."""
    def __init__(self, aligner):
        self._aligner = aligner

    def __getattr__(self, name):
        return getattr(self._aligner, name)

    def align(self, *args, **kwargs):
        check_cancelled()
        result = self._aligner.align(*args, **kwargs)
        check_cancelled()
        return result


class CancellationRegistry:
    """Bounded precancel tombstones close cancel-before-admission races."""
    def __init__(self, *, limit=256, ttl=610, now=time.monotonic):
        self._lock = threading.Lock()
        self._active, self._cancelled = {}, {}
        self._limit, self._ttl, self._now = limit, ttl, now

    def _prune(self):
        now = self._now()
        self._cancelled = {key: expiry for key, expiry in self._cancelled.items() if expiry > now}

    def register(self, request_id):
        with self._lock:
            self._prune()
            if request_id in self._active:
                return None
            event = threading.Event()
            if request_id in self._cancelled:
                event.set()
            self._active[request_id] = event
            return event

    def cancel(self, request_id):
        with self._lock:
            self._prune()
            event = self._active.get(request_id)
            if event is not None:
                event.set()
            if request_id not in self._cancelled and len(self._cancelled) >= self._limit:
                return event is not None
            self._cancelled[request_id] = self._now() + self._ttl
            return True

    def finish(self, request_id, event):
        with self._lock:
            if self._active.get(request_id) is event:
                del self._active[request_id]
