"""Private, bounded transport for the existing stateless Qwen call contract.

No account identifiers, filesystem paths, or credentials belong in this protocol.
The API keeps ownership, committed boundary context and all durable audio state.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import uuid

import numpy as np

PROTOCOL_VERSION = 1
SAMPLE_RATE = 16000
MAX_SAMPLES = 16 * SAMPLE_RATE
MAX_CONTEXT_BYTES = 65536
MAX_REQUEST_BYTES = 1500000
MAX_RESPONSE_BYTES = 1048576
MAX_STATUS_BYTES = 8192
MAX_SEGMENTS = 4096
MAX_TEXT_CHARACTERS = 262144


class ModelUnavailableError(RuntimeError):
    """A safe, temporary local-model failure; never contains upstream details."""

    def __init__(self, code: str = "model_unavailable"):
        messages = {
            "model_unavailable": "로컬 음성 모델 서버에 연결할 수 없습니다. 음성은 보관하고 잠시 후 다시 시도하세요.",
            "model_loading": "로컬 음성 모델을 준비하고 있습니다. 잠시 후 다시 시도하세요.",
            "model_busy": "로컬 음성 모델이 다른 음성을 처리하고 있습니다. 잠시 후 다시 시도하세요.",
            "model_timeout": "로컬 음성 모델의 처리 응답을 기다리는 시간이 초과됐습니다. 음성은 보관합니다.",
            "model_protocol_error": "로컬 음성 모델 응답을 안전하게 확인하지 못했습니다. 음성은 보관합니다.",
        }
        self.code = code if isinstance(code, str) and code in messages else "model_unavailable"
        super().__init__(messages[self.code])


class ProtocolError(ValueError):
    def __init__(self):
        super().__init__("Invalid local model protocol")


def _reject_constant(_):
    raise ProtocolError()


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError()
        value[key] = item
    return value


def load_json(payload: bytes, limit: int):
    if not isinstance(payload, bytes) or len(payload) > limit:
        raise ProtocolError()
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ProtocolError() from None


def dump_json(value, limit: int) -> bytes:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError() from None
    if len(encoded) > limit:
        raise ProtocolError()
    return encoded


def _number(value, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ProtocolError()
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ProtocolError() from None
    if not math.isfinite(result) or not 0 <= result <= maximum:
        raise ProtocolError()
    return result


def _context(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProtocolError()
    # Bound nesting/work independently of encoded length. Keep contents exact;
    # qwen_boundary owns the semantic validation of legacy/stale context.
    remaining = 8192

    def check(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 12:
            raise ProtocolError()
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if isinstance(item, list):
            for child in item:
                check(child, depth + 1)
            return
        if isinstance(item, dict) and all(type(key) is str for key in item):
            for child in item.values():
                check(child, depth + 1)
            return
        raise ProtocolError()

    check(value)
    return load_json(dump_json(value, MAX_CONTEXT_BYTES), MAX_CONTEXT_BYTES)


def _request_id(value) -> str:
    if not isinstance(value, str):
        raise ProtocolError()
    try:
        if str(uuid.UUID(value)) != value:
            raise ProtocolError()
    except ValueError:
        raise ProtocolError() from None
    return value


def make_request(samples, language, overlap_seconds, final_chunk, *, start_seconds,
                 boundary_context, boundary_requested, request_id: str) -> dict:
    if not isinstance(samples, np.ndarray) or samples.ndim != 1 or samples.dtype.kind != "f" or samples.dtype.itemsize != 4:
        raise ProtocolError()
    if not 1 <= len(samples) <= MAX_SAMPLES or not np.isfinite(samples).all() or np.any(np.abs(samples) > 1):
        raise ProtocolError()
    value = {
        "version": PROTOCOL_VERSION, "request_id": request_id,
        "sample_rate": SAMPLE_RATE, "sample_count": len(samples), "encoding": "float32-le",
        "pcm": base64.b64encode(samples.astype("<f4", copy=False).tobytes()).decode("ascii"),
        "language": language, "overlap_seconds": overlap_seconds, "final_chunk": final_chunk,
        "start_seconds": start_seconds, "boundary_context": boundary_context,
        "boundary_requested": boundary_requested,
    }
    # Use exactly the same validation on both ends, before any transport call.
    validated = read_request(value)
    value["boundary_context"] = validated["boundary_context"]
    return value


def read_request(value: dict) -> dict:
    keys = {"version", "request_id", "sample_rate", "sample_count", "encoding", "pcm",
            "language", "overlap_seconds", "final_chunk", "start_seconds", "boundary_context", "boundary_requested"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ProtocolError()
    if type(value["version"]) is not int or value["version"] != PROTOCOL_VERSION:
        raise ProtocolError()
    if type(value["sample_rate"]) is not int or value["sample_rate"] != SAMPLE_RATE or value["encoding"] != "float32-le":
        raise ProtocolError()
    count = value["sample_count"]
    if type(count) is not int or not 1 <= count <= MAX_SAMPLES:
        raise ProtocolError()
    if type(value["final_chunk"]) is not bool or type(value["boundary_requested"]) is not bool:
        raise ProtocolError()
    if value["language"] is not None and (type(value["language"]) is not str or value["language"] not in ("ko", "en")):
        raise ProtocolError()
    _request_id(value["request_id"])
    start = _number(value["start_seconds"], 86400)
    overlap = _number(value["overlap_seconds"], 3)
    duration = count / SAMPLE_RATE
    if overlap > duration or (not value["final_chunk"] and overlap >= duration):
        raise ProtocolError()
    encoded = value["pcm"]
    if type(encoded) is not str or len(encoded) != 4 * ((count * 4 + 2) // 3):
        raise ProtocolError()
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ProtocolError() from None
    if len(payload) != count * 4:
        raise ProtocolError()
    samples = np.frombuffer(payload, dtype="<f4").copy()
    if not np.isfinite(samples).all() or np.any(np.abs(samples) > 1):
        raise ProtocolError()
    context = _context(value["boundary_context"])
    if context is not None and not value["boundary_requested"]:
        raise ProtocolError()
    return {"request_id": value["request_id"], "samples": samples, "language": value["language"],
            "overlap_seconds": overlap, "final_chunk": value["final_chunk"], "start_seconds": start,
            "boundary_context": context, "boundary_requested": value["boundary_requested"]}


def make_result(request_id, segments, boundary_output, sample_count):
    result = {"version": PROTOCOL_VERSION, "request_id": request_id,
              "segments": segments, "boundary_output": boundary_output}
    read_result(result, request_id, sample_count)
    dump_json(result, MAX_RESPONSE_BYTES)
    return result


def read_result(value, request_id, sample_count):
    if not isinstance(value, dict) or set(value) != {"version", "request_id", "segments", "boundary_output"}:
        raise ProtocolError()
    if type(value["version"]) is not int or value["version"] != PROTOCOL_VERSION or value["request_id"] != request_id:
        raise ProtocolError()
    dump_json(value, MAX_RESPONSE_BYTES)
    segments = value["segments"]
    if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
        raise ProtocolError()
    duration = sample_count / SAMPLE_RATE
    text_length, previous_start = 0, -1
    result = []
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) != {"start", "end", "text"}:
            raise ProtocolError()
        start, end = _number(segment["start"], duration), _number(segment["end"], duration)
        text = segment["text"]
        if start > end or start < previous_start or not isinstance(text, str) or not text or len(text) > 65536:
            raise ProtocolError()
        previous_start = start
        text_length += len(text)
        if text_length > MAX_TEXT_CHARACTERS:
            raise ProtocolError()
        # Never strip punctuation/whitespace, renumber IDs, or alter alignment.
        result.append(dict(segment))
    context = _context(value["boundary_output"])
    return result, context


def safe_status(raw, *, model="unknown", device="unknown", state=None):
    raw = raw if isinstance(raw, dict) else {}
    actual_state = state if state is not None else raw.get("model_state")
    if not isinstance(actual_state, str) or actual_state not in {"unloaded", "loading", "ready", "error", "offline", "busy"}:
        actual_state = "error"
    # Do not pass through adapter dictionaries: they may contain private paths.
    def label(value, fallback, length):
        if not isinstance(value, str):
            value = fallback
        value = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return "".join(char for char in value if char.isprintable())[:length] or fallback
    return {"model_state": actual_state, "engine": "qwen3-asr-transformers-uds",
            "model": label(raw.get("model", model), "unknown", 128),
            "device": label(raw.get("device", device), "unknown", 32)}
