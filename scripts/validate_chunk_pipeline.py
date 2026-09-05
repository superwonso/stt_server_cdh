#!/usr/bin/env python3
"""Validate the browser's pause-aware, overlapped classroom audio pipeline."""

from __future__ import annotations

import argparse
import json
import math
import re
import resource
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.settings import Settings
from server.transcriber import LocalTranscriber


SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class ChunkSpec:
    """One WAV emitted by ``MicrophoneCapture`` (sample offsets are global)."""

    start: int
    end: int
    overlap: int
    final: bool

    @property
    def duration(self) -> int:
        return self.end - self.start

    @property
    def fresh(self) -> int:
        return self.duration - self.overlap


def normalize(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", unicodedata.normalize("NFKC", text).lower())


def edit_distance(left: str, right: str) -> int:
    """Levenshtein distance using Myers' bit-vector algorithm.

    Python's arbitrary-width integers keep this fast enough for a repeated
    hour-long transcript without allocating a quadratic dynamic-programming
    table.
    """

    if not left:
        return len(right)
    if not right:
        return len(left)
    if len(left) > len(right):
        # The algorithm's large integer width is determined by ``left``.
        left, right = right, left
    width = len(left)
    mask = (1 << width) - 1
    high_bit = 1 << (width - 1)
    positions: dict[str, int] = {}
    for index, character in enumerate(left):
        positions[character] = positions.get(character, 0) | (1 << index)
    positive = mask
    negative = 0
    score = width
    for character in right:
        equal = positions.get(character, 0)
        vertical = equal | negative
        horizontal = (((equal & positive) + positive) ^ positive) | equal
        positive_horizontal = negative | ~(horizontal | positive)
        negative_horizontal = positive & horizontal
        if positive_horizontal & high_bit:
            score += 1
        elif negative_horizontal & high_bit:
            score -= 1
        positive_horizontal = (positive_horizontal << 1) | 1
        negative_horizontal <<= 1
        positive = (negative_horizontal | ~(vertical | positive_horizontal)) & mask
        negative = (positive_horizontal & vertical) & mask
    return score


def longest_boundary_repeat(left: str, right: str, minimum: int) -> str:
    left, right = normalize(left), normalize(right)
    for length in range(min(len(left), len(right)), minimum - 1, -1):
        if left[-length:] == right[:length]:
            return right[:length]
    return ""


def longest_common_suffix(left: str, right: str) -> int:
    length = 0
    for left_character, right_character in zip(reversed(left), reversed(right)):
        if left_character != right_character:
            break
        length += 1
    return length


def best_suffix_cer(reference: str, hypothesis: str) -> tuple[float, int]:
    """Compare the last reference utterance with a nearby hypothesis suffix."""

    reference = normalize(reference)
    hypothesis = normalize(hypothesis)
    if not reference:
        return 0.0, 0
    margin = max(10, math.ceil(len(reference) * 0.25))
    minimum = max(0, len(reference) - margin)
    maximum = min(len(hypothesis), len(reference) + margin)
    candidates = range(minimum, maximum + 1) if maximum >= minimum else (len(hypothesis),)
    best = min((edit_distance(reference, hypothesis[-length:] if length else ""), length) for length in candidates)
    return best[0] / len(reference), best[1]


def repeated_ngram_excess(reference: str, hypothesis: str, width: int = 8) -> int:
    """Count repeated hypothesis n-grams beyond occurrences in the reference.

    Unique recognition errors are deliberately excluded. This is a duplicate
    candidate metric, not a claim that every repeated phrase is erroneous.
    """

    reference, hypothesis = normalize(reference), normalize(hypothesis)
    if len(hypothesis) < width:
        return 0
    reference_counts = Counter(reference[index : index + width] for index in range(len(reference) - width + 1))
    hypothesis_counts = Counter(hypothesis[index : index + width] for index in range(len(hypothesis) - width + 1))
    return sum(
        max(0, count - max(1, reference_counts.get(gram, 0)))
        for gram, count in hypothesis_counts.items()
        if count > 1
    )


def simulate_browser_chunks(
    audio: np.ndarray,
    *,
    target_samples: int,
    maximum_samples: int,
    overlap_samples: int,
    pause_samples: int,
    pause_rms: float,
    delivery_ends: list[int],
    pause_aware: bool,
) -> list[ChunkSpec]:
    """Mirror ``web/audio.js`` at 16 kHz, including its stop-only final WAV.

    The browser evaluates a quiet boundary after each resampler delivery. The
    15-second capacity includes retained overlap, exactly as in the web UI.
    """

    chunks: list[ChunkSpec] = []
    position = 0
    chunk_start = 0
    chunk_overlap = 0
    emitted = False

    def emit(final: bool) -> None:
        nonlocal chunk_start, chunk_overlap, emitted
        used = position - chunk_start
        if used <= 0:
            return
        fresh = used - chunk_overlap
        if final and fresh == 0 and not emitted:
            return
        chunks.append(ChunkSpec(chunk_start, position, chunk_overlap, final))
        emitted = True
        if final:
            chunk_start = position
            chunk_overlap = 0
        else:
            retained = min(overlap_samples, used)
            chunk_start = position - retained
            chunk_overlap = retained

    for frame_end in delivery_ends:
        if frame_end < position or frame_end > len(audio):
            raise ValueError("resampler delivery endpoints must be ordered and within the source")
        while position < frame_end:
            used = position - chunk_start
            available = maximum_samples - used
            if available <= 0:
                emit(False)
                continue
            position += min(frame_end - position, available)
            used = position - chunk_start
            fresh = used - chunk_overlap
            quiet_boundary = False
            if pause_aware and fresh >= target_samples and fresh >= pause_samples:
                window = audio[position - pause_samples : position]
                power = float(np.mean(window * window, dtype=np.float64)) if len(window) else 0.0
                quiet_boundary = math.sqrt(power) <= pause_rms
            if used == maximum_samples or quiet_boundary:
                emit(False)

    # This deliberately emits retained overlap by itself when stop follows a
    # normal chunk immediately. That final=true request releases the 0.6 s tail
    # which the preceding non-final request held back.
    emit(True)
    return chunks


def browser_delivery_ends(
    output_samples: int,
    *,
    device_sample_rate: int,
    worklet_block_samples: int,
) -> list[int]:
    """Model AudioWorklet + ``StreamingResampler`` message boundaries.

    ``pcm-worklet.js`` posts 2,048 device-rate frames at a time. The sinc
    resampler retains 32 input frames as look-ahead and releases that tail from
    ``flush()`` during stop. At 16 kHz the browser takes the class's fast path.
    """

    if output_samples <= 0:
        return []
    if device_sample_rate == SAMPLE_RATE:
        return list(range(worklet_block_samples, output_samples, worklet_block_samples)) + [output_samples]

    ratio = device_sample_rate / SAMPLE_RATE

    def javascript_round(value: float) -> int:
        return math.floor(value + 0.5)

    # Choose a device-frame count whose browser-rounded resampled length is the
    # source length. This recreates delivery cadence without resampling an
    # already-16-kHz validation waveform a second time.
    device_samples = javascript_round(output_samples * ratio)
    while javascript_round(device_samples / ratio) < output_samples:
        device_samples += 1
    while javascript_round(device_samples / ratio) > output_samples:
        device_samples -= 1

    delivered = 0
    ends = []
    total_input = 0
    while total_input < device_samples:
        total_input = min(device_samples, total_input + worklet_block_samples)
        final_length = javascript_round(total_input / ratio)
        # In StreamingResampler.read(false), output index i is available only
        # while floor(i * ratio) + radius < totalInput.
        available_without_tail = max(0, math.ceil((total_input - 32) / ratio - 1e-12))
        target = min(final_length, available_without_tail)
        if target > delivered:
            ends.append(target)
            delivered = target
    if delivered < output_samples:
        ends.append(output_samples)
    return ends


def partition_ranges(chunks: list[ChunkSpec], guard_samples: int) -> list[tuple[int, int]]:
    ranges = []
    for chunk in chunks:
        lower = chunk.start + max(0, chunk.overlap - guard_samples)
        upper = chunk.end if chunk.final else max(lower, chunk.end - guard_samples)
        ranges.append((lower, upper))
    return ranges


def contract_report(chunks: list[ChunkSpec], source_samples: int, guard_samples: int) -> dict:
    errors = []
    if source_samples and not chunks:
        errors.append("non-empty input emitted no chunks")
    finals = [index for index, chunk in enumerate(chunks) if chunk.final]
    if chunks and finals != [len(chunks) - 1]:
        errors.append("exactly the last chunk must be final")
    if chunks and chunks[0].overlap:
        errors.append("first chunk unexpectedly has overlap")
    for index, chunk in enumerate(chunks):
        if chunk.duration <= 0 or chunk.overlap < 0 or chunk.overlap > chunk.duration:
            errors.append(f"chunk {index} has invalid duration/overlap")
        if index and chunk.start != chunks[index - 1].end - chunk.overlap:
            errors.append(f"chunk {index} does not retain the advertised overlap")
    fresh_samples = sum(chunk.fresh for chunk in chunks)
    if fresh_samples != source_samples:
        errors.append(f"fresh sample coverage differs by {fresh_samples - source_samples}")
    if chunks and chunks[-1].end != source_samples:
        errors.append("final WAV does not reach the source tail")

    ranges = partition_ranges(chunks, guard_samples)
    partition_gaps = []
    partition_overlaps = []
    for (_, previous_end), (current_start, _) in zip(ranges, ranges[1:]):
        partition_gaps.append(max(0, current_start - previous_end))
        partition_overlaps.append(max(0, previous_end - current_start))
    if ranges and ranges[0][0] != 0:
        errors.append("stability partition does not begin at sample zero")
    if ranges and ranges[-1][1] != source_samples:
        errors.append("final stability partition does not reach the source tail")
    if any(partition_gaps):
        errors.append("stability partitions contain an audio gap")
    if any(partition_overlaps):
        errors.append("stability partitions overlap")

    return {
        "fresh_samples": fresh_samples,
        "coverage_error_samples": fresh_samples - source_samples,
        "partition_gap_samples_max": max(partition_gaps, default=0),
        "partition_overlap_samples_max": max(partition_overlaps, default=0),
        "errors": errors,
    }


def current_rss_bytes() -> int:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def growth_slope_mib_per_hour(elapsed_audio: list[float], values: list[int]) -> float:
    if len(values) < 2 or elapsed_audio[-1] <= elapsed_audio[0]:
        return 0.0
    x = np.asarray(elapsed_audio, dtype=np.float64)
    y = np.asarray(values, dtype=np.float64)
    centered = x - x.mean()
    denominator = float(np.dot(centered, centered))
    if denominator == 0:
        return 0.0
    bytes_per_second = float(np.dot(centered, y - y.mean()) / denominator)
    return bytes_per_second * 3600 / 2**20


def preview_chunks(chunks: list[ChunkSpec]) -> list[dict]:
    selected = list(enumerate(chunks))
    if len(selected) > 8:
        selected = selected[:4] + selected[-4:]
    return [
        {
            "index": index,
            "start": round(chunk.start / SAMPLE_RATE, 3),
            "duration": round(chunk.duration / SAMPLE_RATE, 3),
            "overlap": round(chunk.overlap / SAMPLE_RATE, 3),
            "fresh": round(chunk.fresh / SAMPLE_RATE, 3),
            "final": chunk.final,
        }
        for index, chunk in selected
    ]


def transcribe_validation_chunk(engine, samples, chunk, context=None, *, use_context=True):
    """Match the API's per-call context and committed-JSON handoff contract."""
    metadata = {} if use_context else None
    options = ({"start_seconds": chunk.start / SAMPLE_RATE,
                "boundary_context": context, "boundary_output": metadata} if use_context else {})
    segments = engine.transcribe(samples, "ko", overlap_seconds=chunk.overlap / SAMPLE_RATE,
                                 final_chunk=chunk.final, **options)
    # No shared model state and no advancing to the next context before this
    # call succeeds. A JSON round trip mirrors the private DB metadata format.
    committed = json.loads(json.dumps(metadata, allow_nan=False)) if metadata else None
    return segments, committed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Exercise the same pause/overlap/final contract used by web/audio.js."
    )
    parser.add_argument("--samples", type=Path, default=Path(".samples/fleurs/ko_kr"))
    parser.add_argument("--limit", type=int, default=10, help="FLEURS utterances per repetition (default: 10)")
    parser.add_argument("--repeat", type=int, default=1, help="repeat the selected corpus N times")
    parser.add_argument(
        "--minimum-minutes",
        type=float,
        default=0.0,
        help="increase --repeat until the source is at least this long",
    )
    parser.add_argument("--chunk-seconds", type=float, default=8.0, help="fresh-audio target before pause search")
    parser.add_argument("--max-chunk-seconds", type=float, default=15.0, help="total WAV capacity, overlap included")
    parser.add_argument("--overlap-seconds", type=float, default=3.0)
    parser.add_argument("--guard-seconds", type=float, default=0.6)
    parser.add_argument("--pause-seconds", type=float, default=0.24)
    parser.add_argument("--pause-rms", type=float, default=0.006)
    parser.add_argument(
        "--device-sample-rate",
        type=int,
        default=48_000,
        help="AudioContext input rate used to reproduce resampler deliveries (default: 48000)",
    )
    parser.add_argument("--worklet-block-samples", type=int, default=2048)
    parser.add_argument("--gap-seconds", type=float, default=0.25)
    parser.add_argument(
        "--pause-aware",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="use quiet boundaries after the target (default: true; --no-pause-aware is diagnostic)",
    )
    parser.add_argument(
        "--warmup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="run the same model warmup used before class (default: true)",
    )
    parser.add_argument("--duplicate-min-chars", type=int, default=8)
    parser.add_argument("--quiet-chunks", action="store_true", help="print only plan and summary events")
    parser.add_argument(
        "--progress-every",
        type=int,
        default=50,
        help="with --quiet-chunks, emit a compact progress event every N chunks (default: 50)",
    )
    parser.add_argument("--plan-only", action="store_true", help="validate capture contracts without loading a model")
    parser.add_argument("--boundary-context", action=argparse.BooleanOptionalAction, default=True,
                        help="exercise the current per-lecture boundary context (default true; disable for baseline)")
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.minimum_minutes < 0:
        parser.error("--minimum-minutes cannot be negative")
    if not 0 < args.pause_seconds <= args.chunk_seconds:
        parser.error("--pause-seconds must be positive and no longer than --chunk-seconds")
    if args.device_sample_rate < 8_000 or args.device_sample_rate > 384_000:
        parser.error("--device-sample-rate must be between 8000 and 384000")
    if args.worklet_block_samples < 1:
        parser.error("--worklet-block-samples must be positive")
    if not 0 <= args.pause_rms <= 1:
        parser.error("--pause-rms must be between 0 and 1")
    if args.gap_seconds < 0:
        parser.error("--gap-seconds cannot be negative")
    if not 0 <= args.guard_seconds <= args.overlap_seconds <= 3:
        parser.error("require 0 <= guard <= overlap <= 3 seconds")
    if args.max_chunk_seconds <= args.overlap_seconds:
        parser.error("--max-chunk-seconds must be longer than --overlap-seconds")
    if args.chunk_seconds > args.max_chunk_seconds - args.overlap_seconds:
        parser.error("the fresh-audio target must fit after retained overlap")
    if args.duplicate_min_chars < 1:
        parser.error("--duplicate-min-chars must be positive")
    if args.progress_every < 1:
        parser.error("--progress-every must be positive")

    transcript_path = args.samples / "ko_kr.trans.txt"
    if not transcript_path.is_file():
        parser.error(f"missing transcript file: {transcript_path}")
    references = {}
    for line in transcript_path.read_text(encoding="utf-8").splitlines():
        sample_id, text = line.split(maxsplit=1)
        references[sample_id] = text
    paths = sorted(args.samples.glob("ko_kr_*.wav"))[: args.limit]
    if not paths:
        parser.error(f"no ko_kr_*.wav samples found under {args.samples}")

    loaded = []
    for path in paths:
        audio, sample_rate = sf.read(path, dtype="float32")
        if sample_rate != SAMPLE_RATE:
            parser.error(f"unexpected sample rate for {path}: {sample_rate}")
        if audio.ndim == 2:
            audio = audio.mean(axis=1, dtype=np.float32)
        if audio.ndim != 1:
            parser.error(f"unexpected audio shape for {path}: {audio.shape}")
        if path.stem not in references:
            parser.error(f"missing reference text for {path.stem}")
        loaded.append((np.nan_to_num(audio, copy=False), references[path.stem]))

    gap_samples = round(args.gap_seconds * SAMPLE_RATE)
    sample_total = sum(len(audio) for audio, _ in loaded)
    target_total = round(args.minimum_minutes * 60 * SAMPLE_RATE)
    repeat_count = args.repeat
    if target_total:
        samples_per_repeat_with_boundary = sample_total + len(loaded) * gap_samples
        repeat_count = max(
            repeat_count,
            math.ceil((target_total + gap_samples) / samples_per_repeat_with_boundary),
        )

    audio_parts: list[np.ndarray] = []
    reference_parts: list[str] = []
    gap = np.zeros(gap_samples, dtype=np.float32)
    total_utterances = repeat_count * len(loaded)
    utterance_index = 0
    for _ in range(repeat_count):
        for sample_audio, sample_reference in loaded:
            audio_parts.append(sample_audio)
            reference_parts.append(sample_reference)
            utterance_index += 1
            if utterance_index < total_utterances and gap_samples:
                audio_parts.append(gap)
    audio = np.concatenate(audio_parts)
    reference = " ".join(reference_parts)
    tail_reference = loaded[-1][1]

    target_samples = round(args.chunk_seconds * SAMPLE_RATE)
    maximum_samples = round(args.max_chunk_seconds * SAMPLE_RATE)
    overlap_samples = round(args.overlap_seconds * SAMPLE_RATE)
    guard_samples = round(args.guard_seconds * SAMPLE_RATE)
    pause_samples = round(args.pause_seconds * SAMPLE_RATE)
    if min(target_samples, maximum_samples, pause_samples) < 1:
        parser.error("chunk and pause durations must round to at least one 16 kHz sample")
    delivery_ends = browser_delivery_ends(
        len(audio),
        device_sample_rate=args.device_sample_rate,
        worklet_block_samples=args.worklet_block_samples,
    )
    chunks = simulate_browser_chunks(
        audio,
        target_samples=target_samples,
        maximum_samples=maximum_samples,
        overlap_samples=overlap_samples,
        pause_samples=pause_samples,
        pause_rms=args.pause_rms,
        delivery_ends=delivery_ends,
        pause_aware=args.pause_aware,
    )
    contract = contract_report(chunks, len(audio), guard_samples)
    processed_samples = sum(chunk.duration for chunk in chunks)
    plan = {
        "event": "capture_plan",
        "utterances": total_utterances,
        "repeat": repeat_count,
        "audio_seconds": round(len(audio) / SAMPLE_RATE, 3),
        "chunks": len(chunks),
        "pause_aware": args.pause_aware,
        "device_sample_rate": args.device_sample_rate,
        "worklet_block_samples": args.worklet_block_samples,
        "resampler_deliveries": len(delivery_ends),
        "processed_audio_seconds": round(processed_samples / SAMPLE_RATE, 3),
        "overlap_overhead_ratio": round(processed_samples / max(1, len(audio)) - 1, 4),
        "final_chunk_seconds": round(chunks[-1].duration / SAMPLE_RATE, 3) if chunks else 0,
        "final_overlap_seconds": round(chunks[-1].overlap / SAMPLE_RATE, 3) if chunks else 0,
        "final_fresh_seconds": round(chunks[-1].fresh / SAMPLE_RATE, 3) if chunks else 0,
        "finalization_only_chunk": bool(chunks and chunks[-1].fresh == 0),
        "separator_seconds": args.gap_seconds,
        "contract": contract,
        "preview": preview_chunks(chunks),
    }
    print(json.dumps(plan, ensure_ascii=False), flush=True)
    if contract["errors"]:
        raise SystemExit("capture contract failed: " + "; ".join(contract["errors"]))
    if args.plan_only:
        return

    settings = Settings(
        data_dir=Path(".data/validation"),
        model_cache_dir=Path(".models"),
        stability_guard_seconds=guard_samples / SAMPLE_RATE,
    )
    engine = LocalTranscriber(settings)
    if args.warmup:
        engine.warmup()
        torch.cuda.synchronize()

    baseline_allocated = torch.cuda.memory_allocated()
    baseline_reserved = torch.cuda.memory_reserved()
    baseline_rss = current_rss_bytes()
    torch.cuda.reset_peak_memory_stats()
    started = time.perf_counter()
    texts: list[str] = []
    durations: list[float] = []
    repeats = []
    temporal_overlaps = []
    exact_adjacent_duplicates = []
    previous_nonempty: tuple[int, str] | None = None
    previous_segment_end: float | None = None
    last_segment_end: float | None = None
    elapsed_fresh_audio = []
    allocated_samples = []
    reserved_samples = []
    rss_samples = []
    cumulative_fresh = 0
    boundary_context = None

    for chunk_index, chunk in enumerate(chunks):
        samples = audio[chunk.start : chunk.end]
        chunk_started = time.perf_counter()
        segments, boundary_context = transcribe_validation_chunk(
            engine, samples, chunk, boundary_context, use_context=args.boundary_context,
        )
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - chunk_started
        text = " ".join(segment["text"] for segment in segments)
        if text and previous_nonempty:
            previous_index, previous_text = previous_nonempty
            repeated = longest_boundary_repeat(previous_text, text, args.duplicate_min_chars)
            if repeated:
                repeats.append({"left": previous_index, "right": chunk_index, "text": repeated})
            if normalize(previous_text) == normalize(text):
                exact_adjacent_duplicates.append({"left": previous_index, "right": chunk_index})
        if text:
            texts.append(text)
            previous_nonempty = (chunk_index, text)

        absolute_segments = []
        for segment in segments:
            absolute_start = chunk.start / SAMPLE_RATE + float(segment["start"])
            absolute_end = chunk.start / SAMPLE_RATE + float(segment["end"])
            absolute_segments.append({"start": round(absolute_start, 3), "end": round(absolute_end, 3)})
            if previous_segment_end is not None and absolute_start < previous_segment_end:
                temporal_overlaps.append({
                    "chunk": chunk_index,
                    "seconds": round(previous_segment_end - absolute_start, 3),
                })
            previous_segment_end = max(previous_segment_end or absolute_end, absolute_end)
            last_segment_end = max(last_segment_end or absolute_end, absolute_end)

        durations.append(elapsed)
        cumulative_fresh += chunk.fresh
        elapsed_fresh_audio.append(cumulative_fresh / SAMPLE_RATE)
        allocated_samples.append(torch.cuda.memory_allocated())
        reserved_samples.append(torch.cuda.memory_reserved())
        rss_samples.append(current_rss_bytes())
        if not args.quiet_chunks:
            print(
                json.dumps(
                    {
                        "event": "chunk",
                        "index": chunk_index,
                        "start": round(chunk.start / SAMPLE_RATE, 3),
                        "duration": round(chunk.duration / SAMPLE_RATE, 3),
                        "overlap": round(chunk.overlap / SAMPLE_RATE, 3),
                        "fresh": round(chunk.fresh / SAMPLE_RATE, 3),
                        "final": chunk.final,
                        "keep_from": round((chunk.start + max(0, chunk.overlap - guard_samples)) / SAMPLE_RATE, 3),
                        "keep_until": round((chunk.end if chunk.final else chunk.end - guard_samples) / SAMPLE_RATE, 3),
                        "inference_seconds": round(elapsed, 3),
                        "segments": absolute_segments,
                        "text": text,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        elif (chunk_index + 1) % args.progress_every == 0:
            print(
                json.dumps(
                    {
                        "event": "progress",
                        "chunks_done": chunk_index + 1,
                        "chunks_total": len(chunks),
                        "fresh_audio_seconds": round(cumulative_fresh / SAMPLE_RATE, 3),
                        "inference_seconds": round(sum(durations), 3),
                        "gpu_allocated_gib": round(allocated_samples[-1] / 2**30, 3),
                        "gpu_reserved_gib": round(reserved_samples[-1] / 2**30, 3),
                        "process_rss_gib": round(rss_samples[-1] / 2**30, 3),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    hypothesis = " ".join(texts)
    normalized_reference = normalize(reference)
    normalized_hypothesis = normalize(hypothesis)
    edits = edit_distance(normalized_reference, normalized_hypothesis)
    tail_cer, tail_hypothesis_characters = best_suffix_cer(tail_reference, hypothesis)
    final_allocated = torch.cuda.memory_allocated()
    final_reserved = torch.cuda.memory_reserved()
    final_rss = current_rss_bytes()
    total_elapsed = time.perf_counter() - started
    audio_seconds = len(audio) / SAMPLE_RATE
    processed_audio_seconds = processed_samples / SAMPLE_RATE
    first_allocated = allocated_samples[0] if allocated_samples else final_allocated
    first_reserved = reserved_samples[0] if reserved_samples else final_reserved
    first_rss = rss_samples[0] if rss_samples else final_rss
    print(
        json.dumps(
            {
                "event": "summary",
                "boundary_context": args.boundary_context,
                "chunks": len(durations),
                "audio_seconds": round(audio_seconds, 3),
                "processed_audio_seconds": round(processed_audio_seconds, 3),
                "inference_seconds": round(sum(durations), 3),
                "wall_seconds": round(total_elapsed, 3),
                "rtf": round(sum(durations) / max(audio_seconds, 1e-9), 3),
                "model_input_rtf": round(sum(durations) / max(processed_audio_seconds, 1e-9), 3),
                "cer": round(edits / max(1, len(normalized_reference)), 4),
                "reference_characters": len(normalized_reference),
                "hypothesis_characters": len(normalized_hypothesis),
                "hypothesis_character_delta": len(normalized_hypothesis) - len(normalized_reference),
                "boundary_repeat_candidates": repeats,
                "exact_adjacent_duplicate_chunks": exact_adjacent_duplicates,
                "excess_repeated_8gram_occurrences": repeated_ngram_excess(reference, hypothesis),
                "temporal_overlap_candidates": temporal_overlaps,
                "tail_reference_characters": len(normalize(tail_reference)),
                "tail_hypothesis_characters_compared": tail_hypothesis_characters,
                "tail_cer": round(tail_cer, 4),
                "exact_common_suffix_characters": longest_common_suffix(
                    normalize(tail_reference), normalized_hypothesis
                ),
                "last_segment_end_seconds": round(last_segment_end, 3) if last_segment_end is not None else None,
                "audio_after_last_segment_seconds": (
                    round(max(0.0, audio_seconds - last_segment_end), 3)
                    if last_segment_end is not None
                    else round(audio_seconds, 3)
                ),
                "final_chunk_seconds": round(chunks[-1].duration / SAMPLE_RATE, 3),
                "final_overlap_seconds": round(chunks[-1].overlap / SAMPLE_RATE, 3),
                "final_fresh_seconds": round(chunks[-1].fresh / SAMPLE_RATE, 3),
                "finalization_only_chunk": chunks[-1].fresh == 0,
                "capture_contract": contract,
                "gpu_baseline_allocated_gib": round(baseline_allocated / 2**30, 3),
                "gpu_peak_allocated_gib": round(torch.cuda.max_memory_allocated() / 2**30, 3),
                "gpu_baseline_reserved_gib": round(baseline_reserved / 2**30, 3),
                "gpu_peak_reserved_gib": round(torch.cuda.max_memory_reserved() / 2**30, 3),
                "allocated_growth_first_to_last_mib": round((final_allocated - first_allocated) / 2**20, 2),
                "reserved_growth_first_to_last_mib": round((final_reserved - first_reserved) / 2**20, 2),
                "allocated_slope_mib_per_hour": round(
                    growth_slope_mib_per_hour(elapsed_fresh_audio, allocated_samples), 2
                ),
                "reserved_slope_mib_per_hour": round(
                    growth_slope_mib_per_hour(elapsed_fresh_audio, reserved_samples), 2
                ),
                "process_rss_baseline_gib": round(baseline_rss / 2**30, 3),
                "process_rss_growth_first_to_last_mib": round((final_rss - first_rss) / 2**20, 2),
                "process_rss_slope_mib_per_hour": round(
                    growth_slope_mib_per_hour(elapsed_fresh_audio, rss_samples), 2
                ),
                "max_process_rss_gib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 3),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
