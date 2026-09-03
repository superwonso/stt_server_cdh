#!/usr/bin/env python3
"""Reproducible local Qwen3-ASR benchmark for Korean WAV samples.

The sample directory and generated results are intentionally ignored by git.
"""

from __future__ import annotations

import argparse
import json
import re
import resource
import time
import unicodedata
from pathlib import Path

import soundfile as sf
import torch
from qwen_asr import Qwen3ASRModel


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, default=Path(".models/Qwen3-ASR-1.7B"))
    parser.add_argument("--samples", type=Path, default=Path(".samples/fleurs/ko_kr"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-new-tokens", type=int, default=256)
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
                "torch": torch.__version__,
                "device": torch.cuda.get_device_name(0),
                "total_gpu_gib": gib(torch.cuda.get_device_properties(0).total_memory),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    load_started = time.perf_counter()
    model = Qwen3ASRModel.from_pretrained(
        str(args.model),
        dtype=torch.bfloat16,
        device_map="cuda:0",
        attn_implementation="sdpa",
        max_inference_batch_size=1,
        max_new_tokens=args.max_new_tokens,
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

    for repetition in range(args.repeat):
        for wav_path in wav_paths:
            samples, sample_rate = sf.read(wav_path, dtype="float32")
            audio_seconds = len(samples) / sample_rate
            torch.cuda.synchronize()
            started = time.perf_counter()
            result = model.transcribe(audio=(samples, sample_rate), language="Korean")[0]
            torch.cuda.synchronize()
            inference_seconds = time.perf_counter() - started

            reference = references[wav_path.stem]
            normalized_reference = normalized_chars(reference)
            edits = edit_distance(normalized_reference, normalized_chars(result.text))
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
                        "text": result.text,
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
                "allocated_growth_mib": round((final_allocated - (first_allocated or final_allocated)) / 2**20, 2),
                "max_process_rss_gib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 3),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
