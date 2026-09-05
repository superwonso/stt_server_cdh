from __future__ import annotations

import os
import threading
import unicodedata

import numpy as np
import webrtcvad

from .settings import Settings
from .qwen_boundary import reconcile_tokens

SAMPLE_RATE = 16000
VAD_FRAME_SAMPLES = 320  # 20 ms
MIN_VOICED_FRAMES = 6  # 120 ms of continuous speech; rejects fan/room noise bursts
FINAL_MIN_VOICED_FRAMES = 3  # retain a short final syllable without accepting single clicks


def contains_speech(samples: np.ndarray, min_voiced_frames: int = MIN_VOICED_FRAMES) -> bool:
    """Conservative local gate so silence cannot become a stored hallucination."""
    clean = np.nan_to_num(np.asarray(samples, dtype=np.float32), copy=True)
    if len(clean) < VAD_FRAME_SAMPLES or float(np.max(np.abs(clean), initial=0.0)) < 1e-5:
        return False
    pcm = np.rint(np.clip(clean, -1.0, 1.0) * 32767).astype("<i2")
    remainder = len(pcm) % VAD_FRAME_SAMPLES
    if remainder:
        pcm = np.pad(pcm, (0, VAD_FRAME_SAMPLES - remainder))
    detector = webrtcvad.Vad(0)
    run = 0
    for offset in range(0, len(pcm), VAD_FRAME_SAMPLES):
        voiced = detector.is_speech(pcm[offset : offset + VAD_FRAME_SAMPLES].tobytes(), SAMPLE_RATE)
        source_frame = clean[offset : min(offset + VAD_FRAME_SAMPLES, len(clean))]
        energetic = bool(len(source_frame)) and float(np.sqrt(np.mean(source_frame * source_frame))) >= 5e-5
        run = run + 1 if voiced and energetic else 0
        if run >= min_voiced_frames:
            return True
    return False


def _normalized_with_positions(text: str) -> tuple[str, list[int]]:
    normalized = []
    positions = []
    for index, character in enumerate(text):
        for value in unicodedata.normalize("NFKC", character).lower():
            if unicodedata.category(value).startswith(("L", "N")) or value == "'":
                normalized.append(value)
                positions.append(index)
    return "".join(normalized), positions


def aligned_text_slice(text: str, items: list, keep: list[int]) -> str:
    """Keep an aligned token range while retaining the model's punctuation."""
    if not keep:
        return ""
    normalized, positions = _normalized_with_positions(text)
    spans = []
    cursor = 0
    for item in items:
        token, _ = _normalized_with_positions(str(item.text))
        if not token:
            spans.append((cursor, cursor))
            continue
        found = normalized.find(token, cursor)
        if found < 0:
            # Alignment tokens are already safe words; losing punctuation is
            # preferable to duplicating or dropping audio at a boundary.
            return " ".join(str(items[index].text) for index in keep).strip()
        spans.append((found, found + len(token)))
        cursor = found + len(token)

    first, last = keep[0], keep[-1]
    start_normalized = spans[first][0]
    end_normalized = spans[last + 1][0] if last + 1 < len(spans) else len(normalized)
    if not positions or start_normalized >= len(positions):
        return " ".join(str(items[index].text) for index in keep).strip()
    # A complete first token owns any opening quotation mark/parenthesis too.
    # At an overlap boundary we begin at the kept token, not at punctuation
    # belonging to an earlier token. Sentence-final punctuation belongs to the
    # kept text and must survive, including for a complete final chunk.
    start_original = 0 if first == 0 else positions[start_normalized]
    end_original = positions[end_normalized] if end_normalized < len(positions) else len(text)
    return text[start_original:end_original].strip()


class LocalTranscriber:
    """One model, loaded on demand; callers serialize inference off the event loop."""

    supports_boundary_context = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self._state = "unloaded"
        self._state_lock = threading.Lock()
        self._load_lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            self._set_state("loading")
            try:
                # PyTorch's ROCm API intentionally uses CUDA-style device names.
                # This unlocks the gfx1151 SDPA kernel measured on the target PC.
                os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")
                import torch
                from qwen_asr import Qwen3ASRModel

                if not torch.cuda.is_available():
                    raise RuntimeError("ROCm GPU is not visible to PyTorch")
                if self.settings.compute_type != "bfloat16":
                    raise RuntimeError("This deployment is validated only with ASR_DTYPE=bfloat16")
                model_path = self.settings.model_path
                if not (model_path / "model.safetensors.index.json").is_file():
                    raise RuntimeError(f"ASR model is incomplete: {model_path}")
                aligner_path = self.settings.aligner_path
                if not (aligner_path / "model.safetensors").is_file():
                    raise RuntimeError(f"ASR aligner is incomplete: {aligner_path}")
                self._model = Qwen3ASRModel.from_pretrained(
                    str(model_path),
                    dtype=torch.bfloat16,
                    device_map=self.settings.device,
                    attn_implementation=self.settings.attention,
                    max_inference_batch_size=1,
                    max_new_tokens=256,
                    forced_aligner=str(aligner_path),
                    forced_aligner_kwargs={
                        "dtype": torch.bfloat16,
                        "device_map": self.settings.device,
                        "attn_implementation": self.settings.attention,
                    },
                )
                self._set_state("ready")
                return self._model
            except Exception:
                self._set_state("error")
                raise

    def status(self) -> dict:
        with self._state_lock:
            state = self._state
        return {
            "model_state": state,
            "engine": "qwen3-asr-transformers",
            "model": self.settings.model,
            "device": self.settings.device,
        }

    def _set_state(self, value: str):
        with self._state_lock:
            self._state = value

    def transcribe(
        self,
        samples: np.ndarray,
        language: str | None,
        overlap_seconds: float = 0.0,
        final_chunk: bool = True,
        *,
        start_seconds: float = 0.0,
        boundary_context: dict | None = None,
        boundary_output: dict | None = None,
    ) -> list[dict]:
        # Metadata is returned to this call's transaction only. Never attach a
        # lecture's context to the shared model instance.
        if boundary_output is not None:
            boundary_output.clear()
        duration = len(samples) / SAMPLE_RATE
        lower = max(0.0, overlap_seconds - self.settings.stability_guard_seconds)
        upper = duration if final_chunk else max(lower, duration - self.settings.stability_guard_seconds)

        def partition(items: list, keep: list[int]) -> list[int]:
            if boundary_context is None and boundary_output is None:
                return keep
            selected, metadata = reconcile_tokens(
                items,
                keep,
                start_seconds=start_seconds,
                duration=duration,
                overlap_seconds=overlap_seconds,
                lower=lower,
                upper=upper,
                context=boundary_context,
            )
            if boundary_output is not None and metadata is not None:
                boundary_output.update(metadata)
            return selected

        if not contains_speech(
            samples,
            FINAL_MIN_VOICED_FRAMES if final_chunk else MIN_VOICED_FRAMES,
        ):
            partition([], [])
            return []
        model = self._load()
        # qwen-asr 0.0.6 documents a 0.5 s minimum but does not pad short
        # arrays itself. Pad for inference only so the stored end time stays exact.
        if len(samples) < 8000:
            samples = np.pad(samples, (0, 8000 - len(samples)))
        model_language = {"ko": "Korean", "en": "English", None: None}.get(language)
        if language not in {"ko", "en", None}:
            raise ValueError("Unsupported language")
        try:
            transcription = model.transcribe(
                audio=(np.asarray(samples, dtype=np.float32), SAMPLE_RATE),
                language=model_language,
                return_time_stamps=True,
            )[0]
            text = transcription.text.strip()
            alignment = transcription.time_stamps
            items = list(alignment.items) if alignment is not None else []
            if text and not items:
                raise RuntimeError("Forced alignment returned no timestamps")
            if not text:
                partition([], [])
                self._set_state("ready")
                return []
            keep = [
                index
                for index, item in enumerate(items)
                if lower <= (float(item.start_time) + float(item.end_time)) / 2 < upper
            ]
            keep = partition(items, keep)
            self._set_state("ready")
            if not text or not keep:
                return []
            # Reconciliation can remove a replay token between two retained
            # ranges. Slice each contiguous range so the removed word cannot
            # leak back through a broad first-to-last substring.
            groups: list[list[int]] = []
            for index in keep:
                if not groups or index != groups[-1][-1] + 1:
                    groups.append([])
                groups[-1].append(index)
            result = []
            for group in groups:
                selected_text = aligned_text_slice(text, items, group)
                if selected_text:
                    result.append({
                        "start": max(0.0, float(items[group[0]].start_time)),
                        "end": min(duration, float(items[group[-1]].end_time)),
                        "text": selected_text,
                    })
            return result
        except Exception:
            self._set_state("error")
            raise

    def warmup(self) -> None:
        """Load the model and compile the first inference path before class."""
        try:
            model = self._load()
            model.transcribe(
                audio=(np.zeros(8 * SAMPLE_RATE, dtype=np.float32), SAMPLE_RATE),
                language="Korean",
                return_time_stamps=True,
            )
            self._set_state("ready")
        except Exception:
            self._set_state("error")
            raise
