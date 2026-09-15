"""Preserve received answer bodies without claiming validation succeeded.

Only explicit assistant content and known result body fields are recovered.
Provider diagnostics, hidden reasoning and invented source links are excluded.
"""
from __future__ import annotations
import json
import re
from typing import Any

RESULT_WARNING = "일부 내용을 확인하지 못했습니다. 원문과 함께 확인해 주세요."
WARNING_CODES = frozenset({
    "validation_failed", "invalid_response", "unsupported_claim",
    "response_truncated", "model_refused", "placeholder_unresolved",
    "content_limited", "incomplete_batches", "context_unverified",
})
MAX_TEXT_CHARS = 250_000
MAX_DOCUMENT_BYTES = 2_000_000
_NON_BODY_TYPES = frozenset({"reasoning", "reasoning_content", "analysis", "thinking", "tool_call", "tool_calls", "tool_result", "function_call", "error", "refusal", "metadata", "system", "developer", "tool"})
BODY_KEYS = frozenset({"text", "markdown", "content", "body", "summary", "translation",
                      "overview", "answer", "question", "corrected_text", "translated_text"})
CONTAINER_KEYS = frozenset({"paragraphs", "sections", "bullets", "review_questions", "segments"})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_PLACEHOLDER = re.compile(r"__(?:PRIVATE|KOREAN)_[A-Za-z0-9_-]{1,64}?__")
_HIDDEN = re.compile(r"<(think|analysis|reasoning)(?:\s[^>]*)?>.*?(?:</\1\s*>|$)", re.I | re.S)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def safe_model_content(response: Any) -> str:
    """Select generated content only, never the raw HTTP/provider envelope."""
    if not isinstance(response, dict) or not isinstance(response.get("choices"), list):
        raise ValueError("No received answer")
    choices = response["choices"]
    if not 1 <= len(choices) <= 8:
        raise ValueError("No received answer")
    texts = []
    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(part["text"] for part in content[:256]
                         if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
                         and isinstance(part.get("text"), str))
    text = "\n\n".join(readable_text(item) for item in texts) if len(choices) > 1 else "\n\n".join(texts)
    if not text.strip() or len(text) > 2_000_000:
        raise ValueError("No bounded received answer")
    return text

def _partial_json_body(value):
    """Recover strings from body fields without crossing metadata subtrees.

    This is a bounded lexical reader, not JSON repair: it never makes source
    mappings, never interprets strings as keys, and stops on ambiguous nesting.
    Incomplete final strings retain their valid prefix only.
    """
    body_keys = BODY_KEYS
    metadata_keys = {"heading", "title", "id", "source_ids", "edits", "format", "warnings",
                     "role", "model", "type", "usage", "finish_reason", "refusal", "tool_calls",
                     "function_call", "arguments", "reasoning", "reasoning_content", "metadata",
                     "error", "errors", "error_code", "traceback", "stack", "stack_trace", "debug"}
    # The transport already bounds bytes. Also bound this local fallback when
    # called directly by creation-only coercion or another injected engine.
    limit = min(len(value), 2_000_000)
    frames, texts, position, recovered = [], [], 0, 0

    def context():
        if not frames:
            return False, False
        parent = frames[-1]
        if parent["state"] != "value":
            return True, False
        key = parent.get("key")
        return (parent["blocked"] or key in metadata_keys
                or (parent["kind"] == "{" and key not in body_keys and key not in CONTAINER_KEYS),
                parent["body"] or key in body_keys or key in CONTAINER_KEYS)

    def consumed():
        if frames:
            frames[-1]["state"] = "separator"
            frames[-1]["key"] = None

    def quoted(start):
        end = start + 1
        while end < limit:
            if value[end] == "\\":
                end += 2
                continue
            if value[end] == '"':
                try:
                    return json.loads(value[start:end + 1], strict=False), end + 1, True
                except (ValueError, RecursionError):
                    return None, end + 1, True
            end += 1
        tail = value[start + 1:limit]
        # At most one terminal escape can be incomplete. Bound repair work;
        # do not search for an arbitrary suffix that invents a valid document.
        for trim in range(min(12, len(tail)) + 1):
            try:
                text = json.loads('"' + (tail[:-trim] if trim else tail) + '"', strict=False)
                return text, limit, False
            except (ValueError, RecursionError):
                continue
        return None, limit, False

    while position < limit:
        char = value[position]
        if char.isspace():
            position += 1
            continue
        if char in "{[":
            if frames and frames[-1]["state"] != "value":
                break
            blocked, body = context()
            consumed()
            if len(frames) >= 64:
                break
            frames.append({"kind": char, "state": "key" if char == "{" else "value",
                           "key": None, "blocked": blocked, "body": body, "text_start": len(texts)})
            position += 1
            continue
        if not frames:
            break
        frame = frames[-1]
        if char in "}]":
            if (char == "}" and frame["kind"] != "{") or (char == "]" and frame["kind"] != "["):
                break
            frames.pop()
            position += 1
            continue
        if char == '"':
            text, position, complete = quoted(position)
            if frame["kind"] == "{" and frame["state"] == "key":
                if not complete or not isinstance(text, str):
                    break
                frame["key"], frame["state"] = text, "colon"
                continue
            if frame["state"] != "value":
                break
            if (isinstance(frame.get("key"), str) and frame["key"].casefold() in {"type", "role"}
                    and isinstance(text, str) and text.casefold() in _NON_BODY_TYPES):
                frame["blocked"] = True
                # A later type marker also invalidates previously collected
                # text from this same object, without discarding its siblings.
                del texts[frame["text_start"]:]
            blocked, body = context()
            # In objects, only explicit body fields count. An inherited body
            # context permits plain string array items, not arbitrary metadata.
            allowed = body if frame["kind"] == "[" else frame.get("key") in body_keys
            if not blocked and allowed and isinstance(text, str) and text.strip():
                texts.append(text.strip())
                recovered += len(text)
                if recovered > MAX_TEXT_CHARS:
                    break
            consumed()
            continue
        if char == ":":
            if frame["kind"] != "{" or frame["state"] != "colon":
                break
            frame["state"] = "value"
            position += 1
            continue
        if char == ",":
            if frame["state"] != "separator":
                break
            frame["state"] = "key" if frame["kind"] == "{" else "value"
            position += 1
            continue
        if frame["state"] != "value":
            break
        # Ignore scalar values. Quotes/nesting are never swallowed as scalar
        # content, so malformed syntax cannot reset a metadata boundary.
        start = position
        while position < limit and not value[position].isspace() and value[position] not in '{}[],:"':
            position += 1
        if position == start:
            break
        consumed()
    return "\n\n".join(texts)



def readable_text(value: Any, depth: int = 0) -> str:
    if depth > 32:
        return ""
    if isinstance(value, dict):
        if any(isinstance(value.get(key), str) and value[key].casefold() in _NON_BODY_TYPES for key in ("type", "role")):
            return ""
        parts = []
        for key, child in value.items():
            if key in BODY_KEYS or (key in CONTAINER_KEYS and isinstance(child, (dict, list))):
                text = readable_text(child, depth + 1)
                if text:
                    parts.append(text)
        if not parts:
            return ""
        heading = value.get("heading", value.get("title"))
        if isinstance(heading, str) and heading.strip():
            parts.insert(0, heading.strip())
        return "\n\n".join(parts)
    if isinstance(value, list):
        return "\n\n".join(filter(None, (readable_text(row, depth + 1) for row in value[:50_000])))
    if not isinstance(value, str):
        return ""
    text = value.strip().lstrip("\ufeff").strip()
    if depth:
        return text
    if text.startswith("```"):
        text = re.sub(r"^```[^\n]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text).strip()
    if text.startswith(("{", "[")):
        if re.match(r'^(?:\[[^\[\]{}":,\r\n]{1,120}\]|\{[^{}\[\]":,\r\n]{1,120}\})\s+\S', text):
            return text
        try:
            return readable_text(json.loads(text, object_pairs_hook=_unique), depth + 1)
        except (ValueError, TypeError, RecursionError):
            return _partial_json_body(text)
    return text


def safe_draft_text(text: str, *, replacements=None, max_chars=MAX_TEXT_CHARS) -> str:
    if not isinstance(text, str) or not 1 <= max_chars <= 2_000_000:
        raise ValueError("No bounded received answer")
    text = _HIDDEN.sub("", text)
    if replacements is not None:
        text = _PLACEHOLDER.sub(lambda match: replacements.get(match.group(0), "[보호된 내용]"), text)
    text = _CONTROL.sub("", text).encode("utf-8", errors="replace").decode("utf-8").strip()
    if not text:
        raise ValueError("No received answer body")
    return text[:max_chars].rstrip()


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def draft_document(value, *, warnings=("validation_failed",), replacements=None,
                   max_chars=MAX_TEXT_CHARS, max_bytes=MAX_DOCUMENT_BYTES):
    text = readable_text(value)
    codes = list(dict.fromkeys(code if code in WARNING_CODES else "validation_failed"
                               for code in warnings if isinstance(code, str))) or ["validation_failed"]
    if replacements is not None and any(m.group(0) not in replacements for m in _PLACEHOLDER.finditer(text)):
        codes = list(dict.fromkeys([*codes, "placeholder_unresolved"]))
    safe = safe_draft_text(text, replacements=replacements, max_chars=max_chars)
    if len(text) > max_chars:
        codes = list(dict.fromkeys([*codes, "content_limited"]))
    document = {"format": "draft", "text": safe, "warnings": codes}
    if len(_encode(document)) > max_bytes:
        codes = list(dict.fromkeys([*codes, "content_limited"]))
        low, high = 0, len(safe)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = {"format": "draft", "text": safe[:middle], "warnings": codes}
            if len(_encode(candidate)) <= max_bytes:
                low = middle
            else:
                high = middle - 1
        document = {"format": "draft", "text": safe[:low].rstrip(), "warnings": codes}
    return validate_draft_document(document, max_chars=max_chars, max_bytes=max_bytes)


def draft_from_response(response, *, warning="validation_failed", replacements=None,
                        max_chars=MAX_TEXT_CHARS, max_bytes=MAX_DOCUMENT_BYTES):
    return draft_document(safe_model_content(response), warnings=(warning,), replacements=replacements,
                          max_chars=max_chars, max_bytes=max_bytes)


def validate_draft_document(document, *, max_chars=MAX_TEXT_CHARS, max_bytes=MAX_DOCUMENT_BYTES):
    if (not isinstance(document, dict) or set(document) != {"format", "text", "warnings"}
            or document["format"] != "draft" or not isinstance(document["text"], str)
            or not document["text"].strip() or len(document["text"]) > max_chars
            or _CONTROL.search(document["text"]) or _HIDDEN.search(document["text"])
            or not isinstance(document["warnings"], list) or not 1 <= len(document["warnings"]) <= 16
            or any(not isinstance(code, str) or code not in WARNING_CODES for code in document["warnings"])
            or len(set(document["warnings"])) != len(document["warnings"])):
        raise ValueError("Invalid saved answer envelope")
    try:
        if len(_encode(document)) > max_bytes:
            raise ValueError("Invalid saved answer envelope")
    except (UnicodeError, TypeError):
        raise ValueError("Invalid saved answer envelope") from None
    return {"format": "draft", "text": document["text"], "warnings": list(document["warnings"])}
