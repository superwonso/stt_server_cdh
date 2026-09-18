"""Request-scoped Korean/English auto detection for qwen-asr 0.0.6.

Only the automatic language header is constrained. Transcription text, explicit
language requests, forced alignment, and the upstream chunk merger are retained.
Importing this module does not import torch or load an ASR model.
"""
from __future__ import annotations


AUTO_LANGUAGE_HEADERS = (
    "language Korean<asr_text>",
    "language English<asr_text>",
    "language None<asr_text>",
)


def _header_paths(tokenizer) -> tuple[tuple[int, ...], ...]:
    paths = []
    for text in AUTO_LANGUAGE_HEADERS:
        path = tuple(tokenizer.encode(text, add_special_tokens=False))
        if not path or any(type(token) is not int or token < 0 for token in path):
            raise ValueError("qwen_language_header_tokens_invalid")
        # Never silently use a tokenizer that splits/replaces these control tags.
        if tokenizer.decode(path, skip_special_tokens=False,
                            clean_up_tokenization_spaces=False) != text:
            raise ValueError("qwen_language_header_tokens_invalid")
        paths.append(path)
    if len(set(paths)) != len(paths) or any(
        other[:len(path)] == path for path in paths for other in paths if other != path
    ):
        raise ValueError("qwen_language_header_tokens_invalid")
    return tuple(paths)


def _eos_ids(model, tokenizer) -> tuple[int, ...]:
    value = getattr(getattr(model, "generation_config", None), "eos_token_id", None)
    if value is None:
        value = getattr(getattr(getattr(model, "thinker", None), "generation_config", None),
                        "eos_token_id", None)
    if value is None:
        value = getattr(tokenizer, "eos_token_id", None)
    values = (value,) if type(value) is int else tuple(value or ())
    if not values or any(type(token) is not int or token < 0 for token in values):
        raise ValueError("qwen_language_eos_tokens_invalid")
    return tuple(dict.fromkeys(values))


class LanguageHeaderLogitsProcessor:
    """Mask just the unfinished auto header; do not bias allowed token scores.

    One instance belongs to one generate() call. Completed beams are derived
    from token history, so beam reordering cannot move a mutable per-row state
    between callers. Once every automatic row has completed a valid header,
    later body steps return immediately without copying token IDs to the CPU.
    """

    def __init__(self, header_paths, *, prompt_length: int, automatic_rows,
                 eos_token_ids):
        self.paths = tuple(tuple(path) for path in header_paths)
        self.automatic_rows = tuple(automatic_rows)
        self.eos_token_ids = tuple(eos_token_ids)
        if (type(prompt_length) is not int or prompt_length < 0
                or not self.automatic_rows
                or any(type(value) is not bool for value in self.automatic_rows)
                or not self.paths or any(not path for path in self.paths)
                or any(type(token) is not int or token < 0
                       for path in self.paths for token in path)
                or not self.eos_token_ids
                or any(type(token) is not int or token < 0 for token in self.eos_token_ids)):
            raise ValueError("qwen_language_constraint_invalid")
        self.prompt_length = prompt_length
        self.max_header_length = max(map(len, self.paths))
        self._completed_length = None

    def __call__(self, input_ids, scores):
        if (input_ids.ndim != 2 or scores.ndim != 2
                or input_ids.shape[0] != scores.shape[0]
                or input_ids.shape[0] % len(self.automatic_rows)
                or input_ids.shape[1] < self.prompt_length):
            raise ValueError("qwen_language_constraint_shape_invalid")
        width = input_ids.shape[1]
        if self._completed_length is not None and width >= self._completed_length:
            return scores
        if not any(self.automatic_rows):
            return scores
        if any(token >= scores.shape[1] for path in self.paths for token in path) or any(
            token >= scores.shape[1] for token in self.eos_token_ids
        ):
            raise ValueError("qwen_language_constraint_vocabulary_invalid")
        repeats = input_ids.shape[0] // len(self.automatic_rows)
        result = None
        all_complete = True
        for row in range(input_ids.shape[0]):
            if not self.automatic_rows[row // repeats]:
                continue
            # Only the bounded header is inspected, never the caller's transcript.
            suffix = tuple(input_ids[row, self.prompt_length:
                                     self.prompt_length + self.max_header_length].tolist())
            if suffix and suffix[0] in self.eos_token_ids:
                continue  # A naturally empty response remains possible.
            if any(suffix[:len(path)] == path for path in self.paths):
                continue
            allowed = {path[len(suffix)] for path in self.paths
                       if len(suffix) < len(path) and path[:len(suffix)] == suffix}
            if not suffix:
                allowed.update(self.eos_token_ids)
            if not allowed:
                raise ValueError("qwen_language_header_invalid")
            all_complete = False
            if result is None:
                result = scores.clone()
            result[row].fill_(float("-inf"))
            for token in allowed:
                result[row, token] = scores[row, token]
        if all_complete:
            self._completed_length = width
        return scores if result is None else result


class KoreanEnglishASRMixin:
    def _infer_asr_transformers(self, contexts, wavs, languages):
        # Adapted from qwen-asr 0.0.6 inference/qwen3_asr.py:
        # Copyright 2026 The Alibaba Qwen team. SPDX-License-Identifier: Apache-2.0
        # https://www.apache.org/licenses/LICENSE-2.0
        # The only behavioral addition is a per-generate language-header mask.
        # Keep the upstream processing, batching and decoding contract intact.
        if len(contexts) != len(wavs) or len(wavs) != len(languages):
            raise ValueError("qwen_language_batch_invalid")
        outs = []
        texts = [self._build_text_prompt(context=context, force_language=language)
                 for context, language in zip(contexts, languages)]
        batch_size = self.max_inference_batch_size
        if batch_size is None or batch_size < 0:
            batch_size = len(texts)
        if not texts:
            return outs
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("qwen_language_batch_invalid")
        paths = _header_paths(self.processor.tokenizer) if any(
            language is None for language in languages
        ) else None
        for offset in range(0, len(texts), batch_size):
            sub_text = texts[offset:offset + batch_size]
            sub_wavs = wavs[offset:offset + batch_size]
            sub_languages = languages[offset:offset + batch_size]
            inputs = self.processor(text=sub_text, audio=sub_wavs,
                                    return_tensors="pt", padding=True)
            inputs = inputs.to(self.model.device).to(self.model.dtype)
            kwargs = {}
            if any(language is None for language in sub_languages):
                kwargs["logits_processor"] = [LanguageHeaderLogitsProcessor(
                    paths, prompt_length=inputs["input_ids"].shape[1],
                    automatic_rows=[language is None for language in sub_languages],
                    eos_token_ids=_eos_ids(self.model, self.processor.tokenizer),
                )]
            text_ids = self.model.generate(**inputs, max_new_tokens=self.max_new_tokens,
                                           **kwargs)
            decoded = self.processor.batch_decode(
                text_ids.sequences[:, inputs["input_ids"].shape[1]:],
                skip_special_tokens=True, clean_up_tokenization_spaces=False,
            )
            outs.extend(list(decoded))
        return outs


def load_korean_english_asr(*args, **kwargs):
    """Load the existing pinned model lazily with the auto-language adapter."""
    from qwen_asr import Qwen3ASRModel

    class KoreanEnglishQwenASR(KoreanEnglishASRMixin, Qwen3ASRModel):
        pass

    return KoreanEnglishQwenASR.from_pretrained(*args, **kwargs)
