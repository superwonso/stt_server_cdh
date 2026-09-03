#!/usr/bin/env python3
"""Benchmark OpenAI Whisper turbo on the local Korean FLEURS samples.

Run this script from an isolated environment that can see the system ROCm
PyTorch installation.  Model weights and sample audio are kept below ignored
project directories.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import resource
import time
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import whisper


def normalized_chars(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[^0-9a-z가-힣]", "", text)


def edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, 1):
        current = [row]
        for column, right_char in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def read_references(path: Path) -> dict[str, str]:
    references = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        sample_id, text = line.split(maxsplit=1)
        references[sample_id] = text
    return references


def gib(value: int) -> float:
    return round(value / 2**30, 3)


def longest_boundary_repeat(left: str, right: str, minimum: int = 8) -> str:
    left, right = normalized_chars(left), normalized_chars(right)
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
    reference = normalized_chars(reference)
    hypothesis = normalized_chars(hypothesis)
    margin = max(10, math.ceil(len(reference) * 0.25))
    minimum = max(0, len(reference) - margin)
    maximum = min(len(hypothesis), len(reference) + margin)
    candidates = range(minimum, maximum + 1) if maximum >= minimum else (len(hypothesis),)
    best_edits, best_length = min(
        (edit_distance(reference, hypothesis[-length:] if length else ""), length)
        for length in candidates
    )
    return best_edits / max(1, len(reference)), best_length


def repeated_ngram_excess(reference: str, hypothesis: str, width: int = 8) -> int:
    reference = normalized_chars(reference)
    hypothesis = normalized_chars(hypothesis)
    reference_counts = Counter(
        reference[index : index + width]
        for index in range(max(0, len(reference) - width + 1))
    )
    hypothesis_counts = Counter(
        hypothesis[index : index + width]
        for index in range(max(0, len(hypothesis) - width + 1))
    )
    return sum(
        max(0, count - max(1, reference_counts.get(gram, 0)))
        for gram, count in hypothesis_counts.items()
        if count > 1
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="turbo")
    parser.add_argument(
        "--model-cache", type=Path, default=Path(".models/openai-whisper")
    )
    parser.add_argument("--samples", type=Path, default=Path(".samples/fleurs/ko_kr"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument(
        "--fixed-chunk-seconds",
        type=float,
        default=0.0,
        help="concatenate the corpus and benchmark sequential fixed-size chunks",
    )
    parser.add_argument("--gap-seconds", type=float, default=0.25)
    args = parser.parse_args()

    if not torch.cuda.is_available():
        raise SystemExit("ROCm/CUDA device is not visible")

    references = read_references(args.samples / "ko_kr.trans.txt")
    wav_paths = sorted(args.samples.glob("ko_kr_*.wav"))[: args.limit]
    if not wav_paths:
        raise SystemExit(f"No WAV files found in {args.samples}")

    print(
        json.dumps(
            {
                "event": "environment",
                "whisper": getattr(whisper, "__version__", "unknown"),
                "torch": torch.__version__,
                "device": torch.cuda.get_device_name(0),
                "total_gpu_gib": gib(torch.cuda.get_device_properties(0).total_memory),
                "aotriton_experimental": os.getenv(
                    "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", ""
                ),
                "decode": {
                    "language": "ko",
                    "task": "transcribe",
                    "fp16": True,
                    "temperature": 0.0,
                    "beam_size": args.beam_size,
                },
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    args.model_cache.mkdir(parents=True, exist_ok=True)
    load_started = time.perf_counter()
    model = whisper.load_model(
        args.model,
        device="cuda",
        download_root=str(args.model_cache),
    )
    torch.cuda.synchronize()
    print(
        json.dumps(
            {
                "event": "loaded",
                "seconds": round(time.perf_counter() - load_started, 3),
                "allocated_gpu_gib": gib(torch.cuda.memory_allocated()),
                "reserved_gpu_gib": gib(torch.cuda.memory_reserved()),
            }
        ),
        flush=True,
    )

    total_audio = 0.0
    total_inference = 0.0
    total_edits = 0
    total_reference_chars = 0
    first_allocated = None
    torch.cuda.reset_peak_memory_stats()

    if args.fixed_chunk_seconds:
        if args.fixed_chunk_seconds <= 0 or args.gap_seconds < 0:
            parser.error("chunk duration must be positive and gap cannot be negative")
        loaded = []
        for wav_path in wav_paths:
            samples, sample_rate = sf.read(wav_path, dtype="float32")
            if sample_rate != whisper.audio.SAMPLE_RATE or samples.ndim != 1:
                raise SystemExit(f"{wav_path}: expected mono 16000 Hz audio")
            loaded.append((samples, references[wav_path.stem]))
        parts = []
        reference_parts = []
        gap = np.zeros(round(args.gap_seconds * whisper.audio.SAMPLE_RATE), dtype=np.float32)
        utterance_count = len(loaded) * args.repeat
        utterance_index = 0
        for _ in range(args.repeat):
            for samples, reference in loaded:
                parts.append(samples)
                reference_parts.append(reference)
                utterance_index += 1
                if utterance_index < utterance_count and len(gap):
                    parts.append(gap)
        joined_audio = np.concatenate(parts)
        joined_reference = " ".join(reference_parts)
        chunk_samples = round(args.fixed_chunk_seconds * whisper.audio.SAMPLE_RATE)
        texts = []
        repeats = []
        exact_duplicates = []
        previous_nonempty = None
        last_segment_end = None
        for chunk_index, start in enumerate(range(0, len(joined_audio), chunk_samples)):
            samples = joined_audio[start : start + chunk_samples]
            audio_seconds = len(samples) / whisper.audio.SAMPLE_RATE
            torch.cuda.synchronize()
            started = time.perf_counter()
            result = model.transcribe(
                samples,
                language="ko",
                task="transcribe",
                fp16=True,
                temperature=0.0,
                beam_size=args.beam_size,
                verbose=None,
            )
            torch.cuda.synchronize()
            inference_seconds = time.perf_counter() - started
            text = result["text"].strip()
            if previous_nonempty and text:
                previous_index, previous_text = previous_nonempty
                repeated = longest_boundary_repeat(previous_text, text)
                if repeated:
                    repeats.append(
                        {"left": previous_index, "right": chunk_index, "text": repeated}
                    )
                if normalized_chars(previous_text) == normalized_chars(text):
                    exact_duplicates.append({"left": previous_index, "right": chunk_index})
            if text:
                texts.append(text)
                previous_nonempty = (chunk_index, text)
            for segment in result.get("segments", []):
                last_segment_end = start / whisper.audio.SAMPLE_RATE + float(segment["end"])

            total_audio += audio_seconds
            total_inference += inference_seconds
            allocated = torch.cuda.memory_allocated()
            if first_allocated is None:
                first_allocated = allocated
            print(
                json.dumps(
                    {
                        "event": "chunk",
                        "index": chunk_index,
                        "start": round(start / whisper.audio.SAMPLE_RATE, 3),
                        "audio_seconds": round(audio_seconds, 3),
                        "inference_seconds": round(inference_seconds, 3),
                        "rtf": round(inference_seconds / audio_seconds, 3),
                        "allocated_gpu_gib": gib(allocated),
                        "text": text,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        hypothesis = " ".join(texts)
        normalized_reference = normalized_chars(joined_reference)
        normalized_hypothesis = normalized_chars(hypothesis)
        total_edits = edit_distance(normalized_reference, normalized_hypothesis)
        total_reference_chars = len(normalized_reference)
        tail_cer, tail_characters = best_suffix_cer(loaded[-1][1], hypothesis)
        final_allocated = torch.cuda.memory_allocated()
        print(
            json.dumps(
                {
                    "event": "summary",
                    "mode": "fixed_chunks",
                    "chunks": math.ceil(len(joined_audio) / chunk_samples),
                    "chunk_seconds": args.fixed_chunk_seconds,
                    "audio_seconds": round(total_audio, 3),
                    "inference_seconds": round(total_inference, 3),
                    "rtf": round(total_inference / total_audio, 3),
                    "cer": round(total_edits / max(1, total_reference_chars), 4),
                    "reference_characters": len(normalized_reference),
                    "hypothesis_characters": len(normalized_hypothesis),
                    "boundary_repeat_candidates": repeats,
                    "exact_adjacent_duplicate_chunks": exact_duplicates,
                    "excess_repeated_8gram_occurrences": repeated_ngram_excess(
                        joined_reference, hypothesis
                    ),
                    "tail_cer": round(tail_cer, 4),
                    "tail_hypothesis_characters_compared": tail_characters,
                    "exact_common_suffix_characters": longest_common_suffix(
                        normalized_chars(loaded[-1][1]), normalized_hypothesis
                    ),
                    "last_segment_end_seconds": (
                        round(last_segment_end, 3) if last_segment_end is not None else None
                    ),
                    "audio_after_last_segment_seconds": (
                        round(max(0.0, total_audio - last_segment_end), 3)
                        if last_segment_end is not None
                        else round(total_audio, 3)
                    ),
                    "final_chunk_seconds": round(
                        (len(joined_audio) % chunk_samples or chunk_samples)
                        / whisper.audio.SAMPLE_RATE,
                        3,
                    ),
                    "peak_gpu_gib": gib(torch.cuda.max_memory_allocated()),
                    "allocated_growth_mib": round(
                        (final_allocated - (first_allocated or final_allocated)) / 2**20,
                        2,
                    ),
                    "max_process_rss_gib": round(
                        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 3
                    ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return

    for repetition in range(args.repeat):
        for wav_path in wav_paths:
            samples, sample_rate = sf.read(wav_path, dtype="float32")
            if sample_rate != whisper.audio.SAMPLE_RATE:
                raise SystemExit(
                    f"{wav_path}: expected {whisper.audio.SAMPLE_RATE} Hz, got {sample_rate} Hz"
                )
            if samples.ndim != 1:
                raise SystemExit(f"{wav_path}: expected mono audio")

            audio_seconds = len(samples) / sample_rate
            torch.cuda.synchronize()
            started = time.perf_counter()
            result = model.transcribe(
                samples,
                language="ko",
                task="transcribe",
                fp16=True,
                temperature=0.0,
                beam_size=args.beam_size,
                verbose=None,
            )
            torch.cuda.synchronize()
            inference_seconds = time.perf_counter() - started

            text = result["text"].strip()
            reference = references[wav_path.stem]
            normalized_reference = normalized_chars(reference)
            edits = edit_distance(normalized_reference, normalized_chars(text))
            total_audio += audio_seconds
            total_inference += inference_seconds
            total_edits += edits
            total_reference_chars += len(normalized_reference)
            allocated = torch.cuda.memory_allocated()
            if first_allocated is None:
                first_allocated = allocated

            print(
                json.dumps(
                    {
                        "event": "sample",
                        "repetition": repetition + 1,
                        "id": wav_path.stem,
                        "audio_seconds": round(audio_seconds, 3),
                        "inference_seconds": round(inference_seconds, 3),
                        "rtf": round(inference_seconds / audio_seconds, 3),
                        "cer": round(edits / max(1, len(normalized_reference)), 4),
                        "allocated_gpu_gib": gib(allocated),
                        "text": text,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    final_allocated = torch.cuda.memory_allocated()
    print(
        json.dumps(
            {
                "event": "summary",
                "samples": len(wav_paths) * args.repeat,
                "audio_seconds": round(total_audio, 3),
                "inference_seconds": round(total_inference, 3),
                "rtf": round(total_inference / total_audio, 3),
                "cer": round(total_edits / max(1, total_reference_chars), 4),
                "peak_gpu_gib": gib(torch.cuda.max_memory_allocated()),
                "allocated_growth_mib": round(
                    (final_allocated - (first_allocated or final_allocated)) / 2**20,
                    2,
                ),
                "max_process_rss_gib": round(
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 3
                ),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
