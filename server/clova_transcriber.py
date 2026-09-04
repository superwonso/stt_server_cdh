from __future__ import annotations

import copy
import inspect
import json
import math
import queue
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import grpc
import numpy as np

from . import clova_nest_pb2 as nest_pb2
from . import clova_nest_pb2_grpc as nest_pb2_grpc


CLOVA_GRPC_TARGET = "clovaspeech-gw.ncloud.com:50051"
SAMPLE_RATE = 16_000
MAX_DATA_BYTES = 32_000
MAX_INPUT_SAMPLES = 15 * SAMPLE_RATE
MAX_OVERLAP_SECONDS = 3.0
MAX_ACTIVE_SESSIONS = 10
MAX_PENDING_OPERATIONS = 32
MAX_CACHE_ENTRIES = 512
MAX_REQUEST_QUEUE = 64
MAX_RESPONSES_PER_CHUNK = 128
MAX_RESPONSE_BYTES = 512 * 1024
MAX_RESPONSE_TEXT = 64 * 1024
MAX_SESSION_TEXT = 256 * 1024
MAX_ALIGN_ITEMS = 8_000
MAX_ALIGN_WORD = 512
MAX_PENDING_BYTES = 2 * 1024 * 1024
MAX_PENDING_ALIGN_ITEMS = 20_000
MAX_PENDING_TEXT = 256 * 1024
MAX_CACHE_RESULT_TEXT = 32 * 1024
MAX_TIMELINE_SECONDS = 24 * 60 * 60
RESPONSE_TIMESTAMP_SLOP_SECONDS = 0.1
CLOSE_JOIN_SECONDS = 0.25
_QUEUE_END = object()


_SAFE_ERROR_CODES = frozenset(
    {
        "not_configured",
        "closed",
        "invalid_input",
        "chunk_conflict",
        "session_capacity",
        "provider_auth",
        "provider_rejected",
        "provider_unavailable",
        "provider_timeout",
        "invalid_response",
    }
)


class ClovaTranscriptionError(RuntimeError):
    """A provider-safe failure containing no remote body or credential detail."""

    def __init__(self, code: str, *, retryable: bool = False):
        if code not in _SAFE_ERROR_CODES:
            code = "provider_unavailable"
        super().__init__(code)
        self.code = code
        self.retryable = bool(retryable)


@dataclass(frozen=True)
class _Alignment:
    word: str
    start: float
    end: float
    text_start: int
    text_end: int


@dataclass(frozen=True)
class _Transcript:
    text: str
    position: int
    ep_flag: bool
    seq_id: int
    alignments: tuple[_Alignment, ...]
    wire_bytes: int

    @property
    def end_position(self) -> int:
        return self.position + len(self.text)


@dataclass
class _Session:
    owner_key: tuple[str, str] = field(repr=False)
    language: str | None
    stub: Any = field(repr=False)
    channel: Any = field(repr=False)
    created_at: float
    last_used: float
    requests: queue.Queue = field(default_factory=lambda: queue.Queue(MAX_REQUEST_QUEUE), repr=False)
    condition: threading.Condition = field(default_factory=threading.Condition, repr=False)
    reader: threading.Thread | None = field(default=None, repr=False)
    rpc_call: Any = field(default=None, repr=False)
    configured: bool = False
    closing: bool = False
    error: ClovaTranscriptionError | None = field(default=None, repr=False)
    expected_seq: int | None = None
    acknowledged_seq: int | None = None
    batch_response_count: int = 0
    pending: list[_Transcript] = field(default_factory=list, repr=False)
    full_text: str = ""
    committed_text_length: int = 0
    sent_seconds: float = 0.0
    expected_fresh_start: float | None = None


@dataclass(frozen=True)
class _CacheEntry:
    fingerprint: tuple[Any, ...]
    result: tuple[tuple[float, float, str], ...]


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClovaTranscriptionError("invalid_response")
    result = float(value)
    if not math.isfinite(result):
        raise ClovaTranscriptionError("invalid_response")
    return result


def _input_number(value: Any, *, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClovaTranscriptionError("invalid_input")
    result = float(value)
    if not math.isfinite(result) or not lower <= result <= upper:
        raise ClovaTranscriptionError("invalid_input")
    return result


def _setting_number(settings: Any, name: str, default: float, lower: float, upper: float) -> float:
    value = getattr(settings, name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not lower <= result <= upper:
        raise ValueError(f"{name} is outside its safe range")
    return result


def _valid_secret(value: Any) -> str | None:
    if not isinstance(value, str) or not 16 <= len(value) <= 512:
        return None
    if any(character.isspace() or ord(character) < 33 for character in value):
        return None
    return value


def _safe_json_object(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ClovaTranscriptionError("invalid_response")
    try:
        if len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ClovaTranscriptionError("invalid_response")

        def reject_constant(_value: str):
            raise ValueError

        value = json.loads(raw, parse_constant=reject_constant)
    except ClovaTranscriptionError:
        raise
    except Exception:
        raise ClovaTranscriptionError("invalid_response") from None
    if not isinstance(value, dict):
        raise ClovaTranscriptionError("invalid_response")
    return value


def _copy_result(result: tuple[tuple[float, float, str], ...]) -> list[dict]:
    return [{"start": start, "end": end, "text": text} for start, end, text in result]


class ClovaStreamingTranscriber:
    """Bounded, stateful adapter for CLOVA Speech's NEST bidirectional RPC.

    ``stub_factory`` is a test seam. It is called without arguments (or with
    ``None`` when its signature requires one) and may return either a stub or
    ``(stub, channel)``. Production always creates a TLS channel to the pinned
    CLOVA host; credentials are sent only as RPC metadata.
    """

    def __init__(
        self,
        settings: Any,
        *,
        stub_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._secret = _valid_secret(getattr(settings, "clova_speech_secret_key", None))
        self._stub_factory = stub_factory
        self._clock = clock
        self._response_timeout = _setting_number(
            settings,
            "clova_stream_response_timeout_seconds",
            15.0,
            0.05,
            120.0,
        )
        requested_max_age = _setting_number(
            settings,
            "clova_stream_max_age_seconds",
            240.0,
            1.0,
            270.0,
        )
        # A DATA request can legitimately consume the whole response timeout.
        # Keep that wait, plus a 15-second safety margin, inside NAVER's
        # documented five-minute connection lifetime even if an operator sets
        # an overly optimistic rotation age.
        self._max_age = min(
            requested_max_age,
            max(60.0, 285.0 - self._response_timeout),
        )
        self._idle_seconds = _setting_number(
            settings,
            "clova_stream_idle_seconds",
            60.0,
            1.0,
            3600.0,
        )
        self._epd_gap_ms = int(
            _setting_number(settings, "clova_epd_gap_ms", 2000, 0, 60_000)
        )
        self._epd_duration_ms = int(
            _setting_number(settings, "clova_epd_duration_ms", 20_000, 0, 120_000)
        )
        self._operation_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._sessions: OrderedDict[tuple[str, str], _Session] = OrderedDict()
        self._opening: dict[tuple[str, str], _Session] = {}
        self._operation_tokens: dict[tuple[str, str], set[object]] = {}
        self._cache: OrderedDict[tuple[str, str, str], _CacheEntry] = OrderedDict()
        self._next_seq_id = 1
        self._closed = False
        self._state = "ready" if self.configured else "unconfigured"

    @property
    def configured(self) -> bool:
        return self._secret is not None

    def status(self) -> dict:
        # Deliberately return only aggregate counters. Host, credentials, and
        # session keys are private deployment data.
        if self._operation_lock.acquire(blocking=False):
            try:
                if not self._closed:
                    try:
                        self._cleanup_idle()
                    except ClovaTranscriptionError:
                        pass
            finally:
                self._operation_lock.release()
        with self._state_lock:
            return {
                "configured": self.configured,
                "model_state": self._state,
                "engine": "clova-speech-streaming",
                "active_sessions": len(self._sessions) + len(self._opening),
                "cached_chunks": len(self._cache),
            }

    def transcribe(
        self,
        samples: np.ndarray,
        language: str | None,
        overlap_seconds: float = 0.0,
        final_chunk: bool = True,
        *,
        lecture_id: str,
        username: str,
        start_seconds: float,
        payload_hash: str,
    ) -> list[dict]:
        if not isinstance(username, str) or not isinstance(lecture_id, str):
            raise ClovaTranscriptionError("invalid_input")
        owner_key = (username, lecture_id)
        # Register before waiting for the global operation lock. A concurrent
        # lecture deletion can therefore invalidate both the active call and
        # already-queued calls before either sends CONFIG or private audio.
        with self._track_operation(owner_key) as operation_token:
            return self._transcribe_tracked(
                samples,
                language,
                overlap_seconds,
                final_chunk,
                lecture_id=lecture_id,
                username=username,
                start_seconds=start_seconds,
                payload_hash=payload_hash,
                operation_token=operation_token,
            )

    def _transcribe_tracked(
        self,
        samples: np.ndarray,
        language: str | None,
        overlap_seconds: float,
        final_chunk: bool,
        *,
        lecture_id: str,
        username: str,
        start_seconds: float,
        payload_hash: str,
        operation_token: object,
    ) -> list[dict]:
        clean, duration, overlap, start = self._validate_request(
            samples,
            language,
            overlap_seconds,
            final_chunk,
            lecture_id,
            username,
            start_seconds,
            payload_hash,
        )
        cache_key = (username, lecture_id, float(start).hex())
        fingerprint = (
            payload_hash,
            language,
            len(clean),
            round(overlap * SAMPLE_RATE),
            bool(final_chunk),
        )
        owner_key = (username, lecture_id)

        with self._operation_lock:
            with self._state_lock:
                if self._closed:
                    raise ClovaTranscriptionError("closed")
                if operation_token not in self._operation_tokens.get(owner_key, ()):
                    raise ClovaTranscriptionError(
                        "provider_unavailable",
                        retryable=True,
                    )
            if not self.configured:
                raise ClovaTranscriptionError("not_configured")
            self._cleanup_idle()
            with self._state_lock:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    if cached.fingerprint != fingerprint:
                        raise ClovaTranscriptionError("chunk_conflict")
                    self._cache.move_to_end(cache_key)
                    return _copy_result(cached.result)
                session = self._sessions.get(owner_key)
            now = self._now()
            overlap_samples = round(overlap * SAMPLE_RATE)
            rotate = False
            if session is not None:
                if session.language != language:
                    raise ClovaTranscriptionError("chunk_conflict")
                with session.condition:
                    unusable = session.closing or session.error is not None
                if unusable:
                    self._remove_and_shutdown(owner_key, session)
                    session = None
            # Browsers can finish with a guard chunk containing only the
            # overlap that CLOVA already received and acknowledged. There is
            # no fresh PCM to bill or recognize, so close the native stream
            # and persist an idempotent empty result without opening another.
            if final_chunk and overlap_samples == len(clean):
                if session is not None:
                    self._remove_and_shutdown(owner_key, session)
                with self._state_lock:
                    if (
                        self._closed
                        or operation_token
                        not in self._operation_tokens.get(owner_key, ())
                    ):
                        code = "closed" if self._closed else "provider_unavailable"
                        raise ClovaTranscriptionError(code, retryable=not self._closed)
                    self._cache[cache_key] = _CacheEntry(
                        fingerprint=fingerprint,
                        result=(),
                    )
                    self._cache.move_to_end(cache_key)
                    while len(self._cache) > MAX_CACHE_ENTRIES:
                        self._cache.popitem(last=False)
                self._set_state("ready")
                return []

            if session is not None:
                fresh_start = start + overlap
                discontinuous = (
                    session.expected_fresh_start is not None
                    and abs(fresh_start - session.expected_fresh_start) > (2 / SAMPLE_RATE)
                )
                rotate = now - session.created_at >= self._max_age or discontinuous
            if rotate and session is not None:
                self._remove_and_shutdown(owner_key, session)
                session = None

            is_new_session = session is None
            if session is None:
                try:
                    session = self._open_session(
                        owner_key,
                        language,
                        now,
                        operation_token,
                    )
                except ClovaTranscriptionError:
                    self._set_state("error")
                    raise
            else:
                with self._state_lock:
                    if self._sessions.get(owner_key) is session:
                        self._sessions.move_to_end(owner_key)

            send_from = 0 if is_new_session else overlap_samples

            provider_start = session.sent_seconds
            sent_local_start = send_from / SAMPLE_RATE
            context_seconds = overlap if is_new_session else 0.0
            try:
                responses, provider_end = self._send_and_wait(
                    session,
                    clean[send_from:],
                    owner_key,
                    operation_token,
                )
                result = self._local_segments(
                    responses,
                    provider_start=provider_start,
                    provider_end=provider_end,
                    sent_local_start=sent_local_start,
                    context_seconds=context_seconds,
                    duration=duration,
                )
                session.expected_fresh_start = start + duration
                session.last_used = self._now()
                with self._state_lock:
                    if self._closed or session.closing:
                        raise ClovaTranscriptionError("closed")
                self._set_state("ready")
            except ClovaTranscriptionError:
                self._set_state("error")
                self._remove_and_shutdown(owner_key, session)
                raise
            except Exception:
                self._set_state("error")
                self._remove_and_shutdown(owner_key, session)
                raise ClovaTranscriptionError("provider_unavailable", retryable=True) from None

            if final_chunk:
                self._remove_and_shutdown(owner_key, session)
            packed = tuple((item["start"], item["end"], item["text"]) for item in result)
            with self._state_lock:
                if (
                    self._closed
                    or operation_token not in self._operation_tokens.get(owner_key, ())
                ):
                    code = "closed" if self._closed else "provider_unavailable"
                    raise ClovaTranscriptionError(code, retryable=not self._closed)
                self._cache[cache_key] = _CacheEntry(fingerprint=fingerprint, result=packed)
                self._cache.move_to_end(cache_key)
                while len(self._cache) > MAX_CACHE_ENTRIES:
                    self._cache.popitem(last=False)
            return copy.deepcopy(result)

    def close_session(self, username: str, lecture_id: str) -> None:
        # Idempotent and bounded: it never waits for a provider response.
        if not isinstance(username, str) or not isinstance(lecture_id, str):
            return
        key = (username, lecture_id)
        with self._state_lock:
            session = self._sessions.pop(key, None)
            opening = self._opening.pop(key, None)
            # Invalidate a successful response that has not reached the cache
            # yet, so lecture deletion cannot race a late transcript insert.
            self._operation_tokens.pop(key, None)
            cache_keys = [
                cache_key
                for cache_key in self._cache
                if cache_key[:2] == key
            ]
            for cache_key in cache_keys:
                self._cache.pop(cache_key, None)
        if session is not None:
            self._shutdown(session)
        if opening is not None and opening is not session:
            self._shutdown(opening)

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            sessions = list(self._sessions.values()) + list(self._opening.values())
            self._sessions.clear()
            self._opening.clear()
            self._operation_tokens.clear()
            self._cache.clear()
            self._state = "closed"
        unique = {id(session): session for session in sessions}
        for session in unique.values():
            self._shutdown(session)

    @contextmanager
    def _track_operation(self, owner_key: tuple[str, str]):
        token = object()
        with self._state_lock:
            pending = sum(len(tokens) for tokens in self._operation_tokens.values())
            if pending >= MAX_PENDING_OPERATIONS:
                raise ClovaTranscriptionError("session_capacity", retryable=True)
            self._operation_tokens.setdefault(owner_key, set()).add(token)
        try:
            yield token
        finally:
            with self._state_lock:
                tokens = self._operation_tokens.get(owner_key)
                if tokens is not None:
                    tokens.discard(token)
                    if not tokens:
                        self._operation_tokens.pop(owner_key, None)

    def _validate_request(
        self,
        samples: np.ndarray,
        language: str | None,
        overlap_seconds: float,
        final_chunk: bool,
        lecture_id: str,
        username: str,
        start_seconds: float,
        payload_hash: str,
    ) -> tuple[np.ndarray, float, float, float]:
        if language not in {"ko", "en", "ja", None} or not isinstance(final_chunk, bool):
            raise ClovaTranscriptionError("invalid_input")
        if (
            not isinstance(username, str)
            or not 1 <= len(username) <= 128
            or not isinstance(lecture_id, str)
            or not 1 <= len(lecture_id) <= 256
            or any(ord(value) < 32 for value in username + lecture_id)
        ):
            raise ClovaTranscriptionError("invalid_input")
        if (
            not isinstance(payload_hash, str)
            or len(payload_hash) != 64
            or any(value not in "0123456789abcdef" for value in payload_hash)
        ):
            raise ClovaTranscriptionError("invalid_input")
        start = _input_number(start_seconds, lower=0.0, upper=MAX_TIMELINE_SECONDS)
        overlap = _input_number(
            overlap_seconds,
            lower=0.0,
            upper=MAX_OVERLAP_SECONDS,
        )
        try:
            source = np.asarray(samples)
        except Exception:
            raise ClovaTranscriptionError("invalid_input") from None
        if source.ndim != 1 or not 1 <= len(source) <= MAX_INPUT_SAMPLES:
            raise ClovaTranscriptionError("invalid_input")
        try:
            clean = np.asarray(source, dtype=np.float32)
        except Exception:
            raise ClovaTranscriptionError("invalid_input") from None
        if not bool(np.all(np.isfinite(clean))):
            raise ClovaTranscriptionError("invalid_input")
        duration = len(clean) / SAMPLE_RATE
        overlap_samples = round(overlap * SAMPLE_RATE)
        if overlap_samples > len(clean) or (not final_chunk and overlap_samples >= len(clean)):
            raise ClovaTranscriptionError("invalid_input")
        return np.clip(clean, -1.0, 1.0), duration, overlap_samples / SAMPLE_RATE, start

    def _now(self) -> float:
        try:
            value = float(self._clock())
        except Exception:
            raise ClovaTranscriptionError("provider_unavailable") from None
        if not math.isfinite(value):
            raise ClovaTranscriptionError("provider_unavailable")
        return value

    def _set_state(self, value: str) -> None:
        with self._state_lock:
            if not self._closed:
                self._state = value

    def _cleanup_idle(self) -> None:
        now = self._now()
        with self._state_lock:
            stale = []
            for key, session in self._sessions.items():
                with session.condition:
                    failed = session.closing or session.error is not None
                if failed or now - session.last_used >= self._idle_seconds:
                    stale.append((key, session))
            for key, session in stale:
                if self._sessions.get(key) is session:
                    self._sessions.pop(key, None)
        for _key, session in stale:
            self._shutdown(session)

    def _open_session(
        self,
        owner_key: tuple[str, str],
        language: str | None,
        now: float,
        operation_token: object,
    ) -> _Session:
        while True:
            with self._state_lock:
                if self._closed:
                    raise ClovaTranscriptionError("closed")
                if len(self._sessions) + len(self._opening) < MAX_ACTIVE_SESSIONS:
                    old_session = None
                elif self._sessions:
                    _old_key, old_session = self._sessions.popitem(last=False)
                else:
                    raise ClovaTranscriptionError("session_capacity", retryable=True)
            if old_session is None:
                break
            self._shutdown(old_session)
        try:
            stub, channel = self._make_transport()
        except ClovaTranscriptionError:
            raise
        except Exception:
            raise ClovaTranscriptionError("provider_unavailable", retryable=True) from None
        session = _Session(
            owner_key=owner_key,
            language=language,
            stub=stub,
            channel=channel,
            created_at=now,
            last_used=now,
        )
        with self._state_lock:
            if self._closed:
                close_code = "closed"
            elif operation_token not in self._operation_tokens.get(owner_key, ()):
                close_code = "provider_unavailable"
            else:
                self._opening[owner_key] = session
                close_code = None
        if close_code is not None:
            self._shutdown(session)
            raise ClovaTranscriptionError(
                close_code,
                retryable=close_code != "closed",
            )
        session.reader = threading.Thread(
            target=self._read_responses,
            args=(session,),
            name="clova-response-reader",
            daemon=True,
        )
        session.reader.start()
        try:
            config: dict[str, Any] = {
                "semanticEpd": {
                    # epFlag acknowledgement is a transcription response. Do
                    # not suppress the empty response for a silent chunk, or
                    # its caller cannot distinguish completion from timeout.
                    "skipEmptyText": False,
                    "useWordEpd": True,
                    "usePeriodEpd": True,
                    "gapThreshold": self._epd_gap_ms,
                    "durationThreshold": self._epd_duration_ms,
                    "syllableThreshold": 0,
                }
            }
            if language is not None:
                config["transcription"] = {"language": language}
            request = nest_pb2.NestRequest(
                type=nest_pb2.CONFIG,
                config=nest_pb2.NestConfig(
                    config=json.dumps(config, ensure_ascii=False, separators=(",", ":"))
                ),
            )
            self._put_request(session, request)
            self._wait_for_config(session)
        except ClovaTranscriptionError:
            with self._state_lock:
                if self._opening.get(owner_key) is session:
                    self._opening.pop(owner_key, None)
            self._shutdown(session)
            raise
        except Exception:
            with self._state_lock:
                if self._opening.get(owner_key) is session:
                    self._opening.pop(owner_key, None)
            self._shutdown(session)
            raise ClovaTranscriptionError("provider_unavailable", retryable=True) from None
        with self._state_lock:
            if self._opening.get(owner_key) is session:
                self._opening.pop(owner_key, None)
            unavailable = (
                self._closed
                or session.closing
                or operation_token not in self._operation_tokens.get(owner_key, ())
            )
            if not unavailable:
                self._sessions[owner_key] = session
        if unavailable:
            self._shutdown(session)
            raise ClovaTranscriptionError("closed" if self._closed else "provider_unavailable")
        return session

    def _make_transport(self) -> tuple[Any, Any]:
        if self._stub_factory is None:
            channel = grpc.secure_channel(
                CLOVA_GRPC_TARGET,
                grpc.ssl_channel_credentials(),
                options=(
                    ("grpc.max_send_message_length", MAX_DATA_BYTES + 4096),
                    ("grpc.max_receive_message_length", MAX_RESPONSE_BYTES),
                    ("grpc.enable_http_proxy", 0),
                ),
            )
            return nest_pb2_grpc.NestServiceStub(channel), channel

        factory = self._stub_factory
        try:
            try:
                signature = inspect.signature(factory)
            except (TypeError, ValueError):
                product = factory()
            else:
                try:
                    signature.bind()
                except TypeError:
                    signature.bind(None)
                    product = factory(None)
                else:
                    product = factory()
        except Exception:
            raise ClovaTranscriptionError("provider_unavailable", retryable=True) from None
        if isinstance(product, tuple):
            if len(product) != 2:
                raise ClovaTranscriptionError("provider_unavailable", retryable=True)
            stub, channel = product
        else:
            stub, channel = product, getattr(product, "channel", None)
        if not callable(getattr(stub, "recognize", None)):
            raise ClovaTranscriptionError("provider_unavailable", retryable=True)
        return stub, channel

    def _request_iterator(self, session: _Session) -> Iterator[nest_pb2.NestRequest]:
        while True:
            request = session.requests.get()
            if request is _QUEUE_END:
                return
            yield request

    def _read_responses(self, session: _Session) -> None:
        try:
            metadata = (("authorization", f"Bearer {self._secret}"),)
            responses = session.stub.recognize(self._request_iterator(session), metadata=metadata)
            session.rpc_call = responses
            for response in responses:
                if session.closing:
                    return
                self._accept_response(session, response)
            if not session.closing:
                self._fail_session(
                    session,
                    ClovaTranscriptionError("provider_unavailable", retryable=True),
                )
        except ClovaTranscriptionError as error:
            self._fail_session(session, error)
        except Exception as error:
            self._fail_session(session, self._sanitize_rpc_error(error))

    @staticmethod
    def _sanitize_rpc_error(error: Exception) -> ClovaTranscriptionError:
        name = ""
        try:
            code = error.code() if callable(getattr(error, "code", None)) else None
            name = str(getattr(code, "name", ""))
        except Exception:
            name = ""
        if name in {"UNAUTHENTICATED", "PERMISSION_DENIED"}:
            return ClovaTranscriptionError("provider_auth")
        if name in {"INVALID_ARGUMENT", "FAILED_PRECONDITION", "OUT_OF_RANGE"}:
            return ClovaTranscriptionError("provider_rejected")
        if name == "DEADLINE_EXCEEDED":
            return ClovaTranscriptionError("provider_timeout", retryable=True)
        return ClovaTranscriptionError("provider_unavailable", retryable=True)

    @staticmethod
    def _fail_session(session: _Session, error: ClovaTranscriptionError) -> None:
        with session.condition:
            if session.error is None and not session.closing:
                session.error = error
            session.condition.notify_all()

    def _accept_response(self, session: _Session, response: Any) -> None:
        raw_contents = getattr(response, "contents", None)
        envelope = _safe_json_object(raw_contents)
        try:
            wire_bytes = len(raw_contents.encode("utf-8"))
        except Exception:
            raise ClovaTranscriptionError("invalid_response") from None
        response_types = envelope.get("responseType")
        if (
            not isinstance(response_types, list)
            or not 1 <= len(response_types) <= 8
            or any(not isinstance(value, str) or len(value) > 64 for value in response_types)
        ):
            raise ClovaTranscriptionError("invalid_response")
        allowed = {
            "config",
            "transcription",
            "recognize",
            "keywordBoosting",
            "Forbidden",
            "semanticEpd",
        }
        if any(value not in allowed for value in response_types):
            raise ClovaTranscriptionError("invalid_response")

        with session.condition:
            active_batch = session.expected_seq is not None
            if active_batch:
                session.batch_response_count += 1
                if session.batch_response_count > MAX_RESPONSES_PER_CHUNK:
                    raise ClovaTranscriptionError("invalid_response")
            if "recognize" in response_types:
                raise ClovaTranscriptionError("provider_rejected")
            if "config" in response_types:
                config = envelope.get("config")
                if (
                    session.configured
                    or not isinstance(config, dict)
                    or config.get("status") != "Success"
                ):
                    raise ClovaTranscriptionError("provider_rejected")
                session.configured = True
            if "transcription" in response_types:
                if not session.configured or session.expected_seq is None:
                    raise ClovaTranscriptionError("invalid_response")
                transcript = self._parse_transcript(
                    envelope.get("transcription"),
                    session.sent_seconds,
                    wire_bytes,
                )
                self._apply_position(session, transcript)
                if transcript.ep_flag:
                    if transcript.seq_id != session.expected_seq:
                        raise ClovaTranscriptionError("invalid_response")
                    session.acknowledged_seq = transcript.seq_id
            if not {"config", "transcription", "recognize"}.intersection(response_types):
                # The protocol lists these control response types but gives
                # them no transcript-completion semantics. Accept them only
                # while a DATA batch is active; they never satisfy seqId ack.
                if not session.configured or not active_batch:
                    raise ClovaTranscriptionError("invalid_response")
            session.condition.notify_all()

    @staticmethod
    def _parse_transcript(value: Any, sent_seconds: float, wire_bytes: int) -> _Transcript:
        if not isinstance(value, dict):
            raise ClovaTranscriptionError("invalid_response")
        text = value.get("text")
        position = value.get("position")
        ep_flag = value.get("epFlag")
        seq_id = value.get("seqId")
        if (
            not isinstance(text, str)
            or len(text) > MAX_RESPONSE_TEXT
            or isinstance(position, bool)
            or not isinstance(position, int)
            or not 0 <= position <= MAX_SESSION_TEXT
            or not isinstance(ep_flag, bool)
            or isinstance(seq_id, bool)
            or not isinstance(seq_id, int)
            or not 0 <= seq_id <= 2_147_483_647
            or (ep_flag and seq_id == 0)
            or (not ep_flag and seq_id != 0)
        ):
            raise ClovaTranscriptionError("invalid_response")
        for timestamp_name in ("startTimestamp", "endTimestamp"):
            if timestamp_name in value:
                timestamp = _number(value[timestamp_name]) / 1000.0
                if not 0 <= timestamp <= sent_seconds + RESPONSE_TIMESTAMP_SLOP_SECONDS:
                    raise ClovaTranscriptionError("invalid_response")
        if "startTimestamp" in value and "endTimestamp" in value:
            if _number(value["endTimestamp"]) < _number(value["startTimestamp"]):
                raise ClovaTranscriptionError("invalid_response")
        if "confidence" in value:
            confidence = _number(value["confidence"])
            if not 0 <= confidence <= 1:
                raise ClovaTranscriptionError("invalid_response")
        # NAVER documents alignInfos as a response field but does not mark it
        # required. An acknowledged silent endpoint can therefore omit it;
        # treat that one shape as an empty alignment list. A non-empty text
        # still fails the exact text/alignment check below.
        raw_alignments = value.get("alignInfos", [])
        if not isinstance(raw_alignments, list) or len(raw_alignments) > MAX_ALIGN_ITEMS:
            raise ClovaTranscriptionError("invalid_response")
        parsed_alignments: list[tuple[str, float, float]] = []
        previous_start = -1.0
        total_word_length = 0
        for item in raw_alignments:
            if not isinstance(item, dict):
                raise ClovaTranscriptionError("invalid_response")
            word = item.get("word")
            if not isinstance(word, str) or len(word) > MAX_ALIGN_WORD:
                raise ClovaTranscriptionError("invalid_response")
            start = _number(item.get("start")) / 1000.0
            end = _number(item.get("end")) / 1000.0
            if (
                start < previous_start
                or not 0 <= start <= end
                or end > sent_seconds + RESPONSE_TIMESTAMP_SLOP_SECONDS
            ):
                raise ClovaTranscriptionError("invalid_response")
            if "confidence" in item:
                confidence = _number(item["confidence"])
                if not 0 <= confidence <= 1:
                    raise ClovaTranscriptionError("invalid_response")
            previous_start = start
            total_word_length += len(word)
            if total_word_length > MAX_RESPONSE_TEXT:
                raise ClovaTranscriptionError("invalid_response")
            parsed_alignments.append((word, start, end))

        # ``alignInfos.word`` is not a lossless copy of ``text``.  NAVER's
        # documented English example has text ``This is text.`` but alignment
        # words ``This``, ``is``, ``text``, ``.`` and a trailing space.  Match
        # every non-whitespace token into the original response in order and
        # retain its exact character span, accepting only whitespace between
        # tokens.  This both preserves Korean/English spacing and fails closed
        # if an alignment cannot be related unambiguously to the response text.
        alignments: list[_Alignment] = []
        text_cursor = 0
        for word, start, end in parsed_alignments:
            if not word.strip():
                if text.startswith(word, text_cursor):
                    text_start = text_cursor
                    text_end = text_cursor + len(word)
                    text_cursor = text_end
                elif text_cursor == len(text):
                    # The official response example includes one trailing-space
                    # alignment even though that space is absent from ``text``.
                    text_start = text_end = text_cursor
                else:
                    raise ClovaTranscriptionError("invalid_response")
            else:
                if text.startswith(word, text_cursor):
                    text_start = text_cursor
                else:
                    text_start = text_cursor
                    while text_start < len(text) and text[text_start].isspace():
                        text_start += 1
                    if not text.startswith(word, text_start):
                        raise ClovaTranscriptionError("invalid_response")
                text_end = text_start + len(word)
                text_cursor = text_end
            alignments.append(
                _Alignment(
                    word=word,
                    start=start,
                    end=end,
                    text_start=text_start,
                    text_end=text_end,
                )
            )
        if text[text_cursor:].strip() or (
            bool(text.strip()) != any(item.word.strip() for item in alignments)
        ):
            raise ClovaTranscriptionError("invalid_response")
        return _Transcript(
            text=text,
            position=position,
            ep_flag=ep_flag,
            seq_id=seq_id,
            alignments=tuple(alignments),
            wire_bytes=wire_bytes,
        )

    @staticmethod
    def _apply_position(session: _Session, transcript: _Transcript) -> None:
        position = transcript.position
        text = transcript.text
        if position < session.committed_text_length:
            end = position + len(text)
            if (
                not text
                or end > session.committed_text_length
                or session.full_text[position:end] != text
            ):
                raise ClovaTranscriptionError("invalid_response")
            # Exact replay of already-acknowledged text is not emitted twice.
            return
        if position > len(session.full_text):
            raise ClovaTranscriptionError("invalid_response")
        retained: list[_Transcript] = []
        for previous in session.pending:
            if previous.end_position <= position:
                retained.append(previous)
            elif previous.position < position:
                # No documented character-to-alignment contract for slicing a
                # partially replaced result, so fail instead of guessing.
                raise ClovaTranscriptionError("invalid_response")
        updated = session.full_text[:position] + text
        if len(updated) > MAX_SESSION_TEXT:
            raise ClovaTranscriptionError("invalid_response")
        session.full_text = updated
        if text or transcript.ep_flag:
            retained.append(transcript)
        if (
            len(retained) > MAX_RESPONSES_PER_CHUNK
            or sum(item.wire_bytes for item in retained) > MAX_PENDING_BYTES
            or sum(len(item.alignments) for item in retained) > MAX_PENDING_ALIGN_ITEMS
            or sum(len(item.text) for item in retained) > MAX_PENDING_TEXT
        ):
            raise ClovaTranscriptionError("invalid_response")
        session.pending = retained

    def _put_request(self, session: _Session, request: nest_pb2.NestRequest) -> None:
        with session.condition:
            if session.closing:
                raise session.error or ClovaTranscriptionError(
                    "provider_unavailable", retryable=True
                )
        try:
            session.requests.put(request, timeout=self._response_timeout)
        except queue.Full:
            raise ClovaTranscriptionError("provider_timeout", retryable=True) from None

    def _wait_for_config(self, session: _Session) -> None:
        deadline = time.monotonic() + self._response_timeout
        with session.condition:
            while not session.configured:
                if session.error is not None:
                    raise session.error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClovaTranscriptionError("provider_timeout", retryable=True)
                session.condition.wait(remaining)

    def _send_and_wait(
        self,
        session: _Session,
        samples: np.ndarray,
        owner_key: tuple[str, str],
        operation_token: object,
    ) -> tuple[list[_Transcript], float]:
        if not len(samples):
            raise ClovaTranscriptionError("invalid_input")
        # The server WAV decoder produces float32 as int16 / 32768. Invert
        # that mapping exactly, including -32768, instead of attenuating every
        # positive sample through a 32767 multiplier.
        pcm = np.rint(
            np.clip(samples, -1.0, 32767.0 / 32768.0) * 32768.0
        ).astype("<i2").tobytes()
        chunks = [pcm[offset : offset + MAX_DATA_BYTES] for offset in range(0, len(pcm), MAX_DATA_BYTES)]
        if not chunks or any(not value or len(value) > MAX_DATA_BYTES for value in chunks):
            raise ClovaTranscriptionError("invalid_input")
        seq_id = self._allocate_seq_id()
        provider_end = session.sent_seconds + len(samples) / SAMPLE_RATE
        # Linearize DATA submission with close_session. If deletion wins the
        # state lock, no private audio is queued after it invalidates the
        # operation token; if submission wins, that request was already in
        # progress when deletion began and close_session immediately cancels it.
        with self._state_lock:
            if operation_token not in self._operation_tokens.get(owner_key, ()):
                raise ClovaTranscriptionError("provider_unavailable", retryable=True)
            with session.condition:
                if session.error is not None:
                    raise session.error
                if session.closing:
                    raise ClovaTranscriptionError(
                        "provider_unavailable",
                        retryable=True,
                    )
                if session.expected_seq is not None or session.pending:
                    raise ClovaTranscriptionError("invalid_response")
                session.expected_seq = seq_id
                session.acknowledged_seq = None
                session.batch_response_count = 0
                # Publish the bound before DATA can be consumed by the response
                # thread, so timestamp validation cannot race a fast provider.
                session.sent_seconds = provider_end
            for index, chunk in enumerate(chunks):
                last = index == len(chunks) - 1
                details = (
                    {"epFlag": False}
                    if not last
                    else {"epFlag": True, "seqId": seq_id}
                )
                extra = json.dumps(details, separators=(",", ":"))
                self._put_request(
                    session,
                    nest_pb2.NestRequest(
                        type=nest_pb2.DATA,
                        data=nest_pb2.NestData(chunk=chunk, extra_contents=extra),
                    ),
                )

        deadline = time.monotonic() + self._response_timeout
        with session.condition:
            while session.acknowledged_seq != seq_id:
                if session.error is not None:
                    raise session.error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClovaTranscriptionError("provider_timeout", retryable=True)
                session.condition.wait(remaining)
            responses = list(session.pending)
            session.pending.clear()
            session.committed_text_length = len(session.full_text)
            session.expected_seq = None
            session.acknowledged_seq = None
        return responses, provider_end

    def _allocate_seq_id(self) -> int:
        if self._next_seq_id > 2_147_483_647:
            # Reusing an acknowledgement ID would make completion ambiguous.
            raise ClovaTranscriptionError("session_capacity", retryable=True)
        value = self._next_seq_id
        self._next_seq_id += 1
        return value

    @staticmethod
    def _local_segments(
        responses: list[_Transcript],
        *,
        provider_start: float,
        provider_end: float,
        sent_local_start: float,
        context_seconds: float,
        duration: float,
    ) -> list[dict]:
        fresh_provider_start = provider_start + context_seconds
        local_offset = sent_local_start - provider_start
        result: list[dict] = []
        seen: set[tuple[float, float, str]] = set()
        result_characters = 0
        for response in responses:
            kept = [
                item
                for item in response.alignments
                if fresh_provider_start
                <= (item.start + item.end) / 2
                < provider_end + RESPONSE_TIMESTAMP_SLOP_SECONDS
            ]
            text_alignments = [item for item in kept if item.word.strip()]
            if not text_alignments:
                continue
            text = response.text[
                text_alignments[0].text_start : text_alignments[-1].text_end
            ].strip()
            if not text:
                continue
            start = max(0.0, min(duration, text_alignments[0].start + local_offset))
            end = max(start, min(duration, text_alignments[-1].end + local_offset))
            packed = (round(start, 6), round(end, 6), text)
            if packed in seen:
                continue
            seen.add(packed)
            result_characters += len(text)
            if result_characters > MAX_CACHE_RESULT_TEXT or len(result) >= MAX_RESPONSES_PER_CHUNK:
                raise ClovaTranscriptionError("invalid_response")
            result.append({"start": packed[0], "end": packed[1], "text": text})
        result.sort(key=lambda item: (item["start"], item["end"], item["text"]))
        return result

    def _remove_and_shutdown(self, key: tuple[str, str], session: _Session) -> None:
        with self._state_lock:
            if self._sessions.get(key) is session:
                self._sessions.pop(key, None)
            if self._opening.get(key) is session:
                self._opening.pop(key, None)
        self._shutdown(session)

    @staticmethod
    def _shutdown(session: _Session) -> None:
        with session.condition:
            if session.closing:
                return
            session.closing = True
            if session.error is None and (not session.configured or session.expected_seq is not None):
                session.error = ClovaTranscriptionError(
                    "provider_unavailable", retryable=True
                )
            session.condition.notify_all()
        try:
            session.requests.put_nowait(_QUEUE_END)
        except queue.Full:
            pass
        call = session.rpc_call
        if callable(getattr(call, "cancel", None)):
            try:
                call.cancel()
            except Exception:
                pass
        closed: set[int] = set()
        for target in (session.channel, session.stub):
            if target is None or id(target) in closed:
                continue
            closed.add(id(target))
            closer = getattr(target, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
        reader = session.reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(CLOSE_JOIN_SECONDS)
