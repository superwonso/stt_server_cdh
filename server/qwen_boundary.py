"""Stateless, conservative reconciliation of independently aligned Qwen windows.

Only the caller's last committed chunk may supply this private metadata.  The
caller persists the returned metadata in the same transaction as the returned
segments; this module never retains a lecture, user, or mutable model state.
"""

from __future__ import annotations

import json
import math
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any


BOUNDARY_VERSION = 1
MAX_BOUNDARY_TOKENS = 96
MAX_TOKEN_CHARACTERS = 128
MAX_BOUNDARY_BYTES = 48 * 1024
TAIL_SECONDS = 6.0
MAX_ALIGNMENT_DRIFT = 0.25
ANCHOR_DRIFT_TOLERANCE = 0.10
CONTIGUITY_TOLERANCE = 2 / 16_000
MAX_TIMELINE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class _Token:
    text: str
    start: float
    end: float
    emitted: bool = False

    @property
    def middle(self) -> float:
        return (self.start + self.end) / 2

    @property
    def lexical(self) -> str:
        return "".join(
            character
            for character in unicodedata.normalize("NFKC", self.text).lower()
            if character.isalnum() or character == "'"
        )


def _finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("invalid boundary time")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= MAX_TIMELINE_SECONDS:
        raise ValueError("invalid boundary time")
    return result


def _read_context(context: Any, audio_end: float) -> list[_Token] | None:
    """Invalid/stale data disables reconciliation, never normal transcription."""
    try:
        if not isinstance(context, dict) or set(context) != {"version", "audio_end", "tokens"}:
            return None
        if type(context["version"]) is not int or context["version"] != BOUNDARY_VERSION:
            return None
        previous_end = _finite(context["audio_end"])
        if abs(previous_end - audio_end) > CONTIGUITY_TOLERANCE:
            return None
        values = context["tokens"]
        if not isinstance(values, list) or len(values) > MAX_BOUNDARY_TOKENS:
            return None
        result = []
        last_start = -1.0
        last_end = -1.0
        for value in values:
            if not isinstance(value, dict) or set(value) != {"text", "start", "end", "emitted"}:
                return None
            text = value["text"]
            if not isinstance(text, str) or not 1 <= len(text) <= MAX_TOKEN_CHARACTERS:
                return None
            if type(value["emitted"]) is not bool:
                return None
            start, end = _finite(value["start"]), _finite(value["end"])
            if not max(0.0, previous_end - TAIL_SECONDS) <= start <= end <= previous_end:
                return None
            if start < last_start or end < last_end:
                return None
            last_start, last_end = start, end
            result.append(_Token(text, start, end, value["emitted"]))
        if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) > MAX_BOUNDARY_BYTES:
            return None
        return result
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _current_tokens(items: list, start_seconds: float, duration: float) -> list[_Token] | None:
    try:
        start_seconds = _finite(start_seconds)
        audio_end = _finite(start_seconds + duration)
        result = []
        last_start = -1.0
        last_end = -1.0
        for item in items:
            text = str(item.text)
            start = _finite(float(item.start_time))
            end = _finite(float(item.end_time))
            if not text or start > end or start < last_start or end < last_end:
                return None
            last_start, last_end = start, end
            # Qwen can align the inference-only padded tail past real PCM.
            # Keep the real audio bound in metadata just as returned segments do.
            result.append(_Token(text, min(audio_end, start_seconds + start), min(audio_end, start_seconds + end)))
        return result
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def reconcile_tokens(
    items: list,
    keep: list[int],
    *,
    start_seconds: float,
    duration: float,
    overlap_seconds: float,
    lower: float,
    upper: float,
    context: dict | None,
) -> tuple[list[int], dict | None]:
    """Recover a withheld boundary token; remove replay only with two anchors.

    A lone equal word shifted in time is indistinguishable from a newly spoken
    repetition.  It is deliberately retained.  Replay removal needs two
    adjacent, different, uniquely matched lexical tokens with a consistent
    timestamp shift.  Repeated lexical tokens are never removed by this rule.
    """
    tokens = _current_tokens(items, start_seconds, duration)
    if tokens is None:
        return keep, None
    selected = set(keep)
    already_emitted: set[int] = set()
    previous = _read_context(context, start_seconds + overlap_seconds) if overlap_seconds > 0 else None
    if previous is not None:
        pairs: dict[int, list[int]] = {}
        reverse: dict[int, list[int]] = {}
        relevant_current = [
            index for index, token in enumerate(tokens)
            if token.start <= start_seconds + overlap_seconds + MAX_ALIGNMENT_DRIFT
        ]
        previous_lexical = [token.lexical for token in previous]
        current_lexical = {index: tokens[index].lexical for index in relevant_current}
        previous_counts = Counter(previous_lexical)
        current_counts = Counter(current_lexical.values())
        for current_index in relevant_current:
            token = tokens[current_index]
            lexical = current_lexical[current_index]
            if not lexical:
                continue
            for previous_index, old in enumerate(previous):
                if (
                    lexical == previous_lexical[previous_index]
                    and abs(token.start - old.start) <= MAX_ALIGNMENT_DRIFT + 1e-9
                    and abs(token.end - old.end) <= MAX_ALIGNMENT_DRIFT + 1e-9
                ):
                    pairs.setdefault(current_index, []).append(previous_index)
                    reverse.setdefault(previous_index, []).append(current_index)

        unique = {
            current_index: matches[0]
            for current_index, matches in pairs.items()
            if len(matches) == 1 and len(reverse[matches[0]]) == 1
        }

        def has_distinct_anchor(current_index: int, previous_index: int) -> bool:
            token, old = tokens[current_index], previous[previous_index]
            if (
                previous_counts[token.lexical] != 1
                or current_counts[token.lexical] != 1
            ):
                return False
            for direction in (-1, 1):
                adjacent = current_index + direction
                old_adjacent = previous_index + direction
                if unique.get(adjacent) != old_adjacent:
                    continue
                other, old_other = tokens[adjacent], previous[old_adjacent]
                if (
                    other.lexical == token.lexical
                    or previous_counts[other.lexical] != 1
                    or current_counts[other.lexical] != 1
                ):
                    continue
                if (
                    abs((token.start - old.start) - (other.start - old_other.start))
                    <= ANCHOR_DRIFT_TOLERANCE
                    and abs((token.end - old.end) - (other.end - old_other.end))
                    <= ANCHOR_DRIFT_TOLERANCE
                ):
                    return True
            return False

        for index, token in enumerate(tokens):
            local_middle = token.middle - start_seconds
            if not lower - MAX_ALIGNMENT_DRIFT <= local_middle <= lower + MAX_ALIGNMENT_DRIFT:
                continue
            if local_middle >= upper:
                continue
            matches = pairs.get(index, [])
            # Adding an uncertain withheld word is safer than silently losing
            # it. Ambiguous equal-word matches do not authorize removal.
            if index not in selected and any(not previous[old_index].emitted for old_index in matches):
                selected.add(index)
            previous_index = unique.get(index)
            if (
                index in selected
                and previous_index is not None
                and previous[previous_index].emitted
                and has_distinct_anchor(index, previous_index)
            ):
                selected.remove(index)
                already_emitted.add(index)

    audio_end = start_seconds + duration
    metadata_tokens = []
    for index, token in enumerate(tokens):
        if token.start < max(0.0, audio_end - TAIL_SECONDS) or len(token.text) > MAX_TOKEN_CHARACTERS:
            continue
        if index not in selected and index not in already_emitted and token.middle - start_seconds < upper:
            # An excluded left-overlap word is not a newly withheld right-tail
            # word. Its old ownership may be unknown after legacy/malformed
            # context. Do not let a later short final guard resurrect it.
            continue
        metadata_tokens.append({
            "text": token.text,
            "start": token.start,
            "end": token.end,
            "emitted": index in selected or index in already_emitted,
        })
    metadata = {
        "version": BOUNDARY_VERSION,
        "audio_end": audio_end,
        "tokens": metadata_tokens[-MAX_BOUNDARY_TOKENS:],
    }
    # A pathological tokenizer must not create unbounded private DB state.
    while len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > MAX_BOUNDARY_BYTES:
        metadata["tokens"].pop(0)
    return sorted(selected), metadata
