from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import torch

from server.qwen_language import (
    AUTO_LANGUAGE_HEADERS, KoreanEnglishASRMixin, LanguageHeaderLogitsProcessor,
    _header_paths, load_korean_english_asr,
)


PATHS = ((1, 2, 4), (1, 3, 4), (1, 5, 4))


class TinyTokenizer:
    eos_token_id = 8

    def encode(self, text, *, add_special_tokens):
        assert add_special_tokens is False
        return list(PATHS[AUTO_LANGUAGE_HEADERS.index(text)])

    def decode(self, tokens, **kwargs):
        return AUTO_LANGUAGE_HEADERS[PATHS.index(tuple(tokens))]


class TinyBatch(dict):
    def to(self, _):
        return self


class TinyProcessor:
    tokenizer = TinyTokenizer()

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return TinyBatch(input_ids=torch.tensor([
            [0, 0, 9, 10 if text == 'automatic' else 11] for text in kwargs['text']
        ]))

    def batch_decode(self, sequences, **kwargs):
        assert kwargs == {'skip_special_tokens': True, 'clean_up_tokenization_spaces': False}
        return [','.join(map(str, sequence.tolist())) for sequence in sequences]


class TinyModel:
    device = 'cpu'
    dtype = torch.float32
    generation_config = SimpleNamespace(eos_token_id=[8, 9])

    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        sequence = kwargs['input_ids']
        automatic = sequence[:, -1] == 10
        for step, desired in enumerate([1, 3, 4, 7, 8]):
            scores = torch.zeros((len(sequence), 16))
            scores[:, 7] = 20  # Outside-language token wins without the mask.
            scores[:, desired] = 30 if step >= 3 else 10
            for processor in kwargs.get('logits_processor', []):
                masked = processor(sequence, scores)
                self.assert_forced_rows_unchanged(scores, masked, automatic)
                scores = masked
            sequence = torch.cat((sequence, scores.argmax(dim=-1, keepdim=True)), dim=1)
        return SimpleNamespace(sequences=sequence)

    @staticmethod
    def assert_forced_rows_unchanged(before, after, automatic):
        assert torch.equal(before[~automatic], after[~automatic])


class TinyASR(KoreanEnglishASRMixin):
    def __init__(self, batch_size=2):
        self.processor = TinyProcessor()
        self.model = TinyModel()
        self.max_inference_batch_size = batch_size
        self.max_new_tokens = 256

    def _build_text_prompt(self, context, force_language):
        return 'automatic' if force_language is None else f'forced {force_language}'


class QwenLanguageTests(unittest.TestCase):
    def constraint(self, *, rows=(True,), prompt_length=2):
        return LanguageHeaderLogitsProcessor(PATHS, prompt_length=prompt_length,
                                            automatic_rows=rows, eos_token_ids=(8, 9))

    def scores(self, rows=1):
        return torch.arange(rows * 16, dtype=torch.float32).reshape(rows, 16)

    def test_module_import_does_not_import_torch_or_qwen(self):
        root = Path(__file__).resolve().parents[1]
        code = (f'import runpy,sys; runpy.run_path({str(root / "server/qwen_language.py")!r}); '
                'print(int("torch" in sys.modules),int("qwen_asr" in sys.modules))')
        result = subprocess.run([sys.executable, '-I', '-S', '-c', code],
                                capture_output=True, text=True, timeout=15, check=True)
        self.assertEqual(result.stdout.strip(), '0 0')

    def test_token_paths_come_from_exact_round_trip_tokenizer_encodings(self):
        self.assertEqual(_header_paths(TinyTokenizer()), PATHS)
        with patch.object(TinyTokenizer, 'decode', return_value='unexpected'):
            with self.assertRaisesRegex(ValueError, '^qwen_language_header_tokens_invalid$'):
                _header_paths(TinyTokenizer())

    def test_prefix_mask_only_allows_supported_headers_and_initial_empty_response(self):
        processor = self.constraint()
        original = self.scores()
        for prefix, allowed in [((), {1, 8, 9}), ((1,), {2, 3, 5}), ((1, 2), {4})]:
            ids = torch.tensor([[12, 13, *prefix]])
            masked = processor(ids, original)
            self.assertEqual(set(torch.where(torch.isfinite(masked[0]))[0].tolist()), allowed)
            for token in allowed:
                self.assertEqual(masked[0, token], original[0, token])
            self.assertTrue(torch.equal(original, self.scores()), 'caller scores must not be mutated')

    def test_all_three_complete_headers_release_every_body_token_without_cpu_sync(self):
        for path in PATHS:
            processor = self.constraint()
            scores = self.scores()
            self.assertIs(processor(torch.tensor([[12, 13, *path]]), scores), scores)
            with patch.object(torch.Tensor, 'tolist', side_effect=AssertionError('body copied to CPU')):
                self.assertIs(processor(torch.tensor([[12, 13, *path, 7, 15]]), scores), scores)

    def test_initial_eos_is_preserved_without_forcing_a_hallucinated_language(self):
        for eos in (8, 9):
            scores = self.scores()
            self.assertIs(self.constraint()(torch.tensor([[12, 13, eos]]), scores), scores)

    def test_mixed_rows_and_repeated_beams_use_padded_prompt_width(self):
        processor = self.constraint(rows=(True, False), prompt_length=5)
        ids = torch.tensor([[0, 0, 0, 11, 12, 1], [0, 0, 0, 11, 12, 1],
                            [0, 14, 15, 11, 12, 7], [0, 14, 15, 11, 12, 6]])
        scores = self.scores(4)
        masked = processor(ids, scores)
        self.assertTrue(torch.equal(masked[2:], scores[2:]))
        for row in (0, 1):
            self.assertEqual(set(torch.where(torch.isfinite(masked[row]))[0].tolist()), {2, 3, 5})
        # Beams can select different allowed headers without per-row mutable state.
        completed = torch.tensor([[0, 0, 0, 11, 12, 1, 3, 4], [0, 0, 0, 11, 12, 1, 2, 4],
                                  [0, 14, 15, 11, 12, 7, 7, 7], [0, 14, 15, 11, 12, 6, 6, 6]])
        self.assertIs(processor(completed, scores), scores)

    def test_invalid_short_or_long_header_never_releases_unrestricted_generation(self):
        for suffix in [(7,), (1, 7), (1, 7, 4), (1, 7, 4, 7, 7, 7), (1, 2, 8)]:
            with self.subTest(length=len(suffix)):
                with self.assertRaisesRegex(ValueError, '^qwen_language_header_invalid$'):
                    self.constraint()(torch.tensor([[12, 13, *suffix]]), self.scores())

    def test_unexpected_batch_shape_and_out_of_range_vocabulary_fail_closed(self):
        with self.assertRaisesRegex(ValueError, '^qwen_language_constraint_shape_invalid$'):
            self.constraint(rows=(True, False))(torch.tensor([[12, 13]]), self.scores())
        with self.assertRaisesRegex(ValueError, '^qwen_language_constraint_vocabulary_invalid$'):
            self.constraint()(torch.tensor([[12, 13]]), torch.zeros((1, 5)))

    def test_forced_rows_never_inspect_or_mask_text(self):
        processor = self.constraint(rows=(False,))
        scores = self.scores()
        with patch.object(torch.Tensor, 'tolist', side_effect=AssertionError('forced text inspected')):
            self.assertIs(processor(torch.tensor([[12, 13, 7, 7]]), scores), scores)

    def test_adapter_preserves_subbatches_and_adds_only_request_scoped_generation_masks(self):
        model = TinyASR(batch_size=2)
        result = model._infer_asr_transformers(['', '', ''], [object(), object(), object()],
                                               [None, 'Korean', None])
        self.assertEqual(result, ['1,3,4,7,8', '7,7,7,7,8', '1,3,4,7,8'])
        self.assertEqual([len(call['text']) for call in model.processor.calls], [2, 1])
        for call in model.model.calls:
            self.assertEqual(call['max_new_tokens'], 256)
            self.assertEqual(set(call), {'input_ids', 'max_new_tokens', 'logits_processor'})
        first = model.model.calls[0]['logits_processor'][0]
        second = model.model.calls[1]['logits_processor'][0]
        self.assertIsNot(first, second)
        self.assertEqual(first.automatic_rows, (True, False))
        self.assertEqual(second.automatic_rows, (True,))
        model._infer_asr_transformers([''], [object()], ['English'])
        self.assertNotIn('logits_processor', model.model.calls[-1])
        self.assertFalse(hasattr(model, 'language'), 'no caller language persists on the ASR instance')

    def test_auto_after_forced_generation_gets_a_new_header_constraint(self):
        model = TinyASR(batch_size=1)
        for language in [None, 'English', None]:
            model._infer_asr_transformers([''], [object()], [language])
        self.assertIn('logits_processor', model.model.calls[0])
        self.assertNotIn('logits_processor', model.model.calls[1])
        self.assertIn('logits_processor', model.model.calls[2])
        self.assertIsNot(model.model.calls[0]['logits_processor'][0],
                         model.model.calls[2]['logits_processor'][0])

    def test_lazy_loader_retains_from_pretrained_arguments_and_upstream_class(self):
        class FakeUpstream:
            @classmethod
            def from_pretrained(cls, *args, **kwargs):
                return SimpleNamespace(cls=cls, args=args, kwargs=kwargs)
        with patch.dict(sys.modules, {'qwen_asr': SimpleNamespace(Qwen3ASRModel=FakeUpstream)}):
            loaded = load_korean_english_asr('fixed-model', forced_aligner='fixed-aligner',
                                           max_inference_batch_size=1, attn_implementation='sdpa')
        self.assertTrue(issubclass(loaded.cls, FakeUpstream))
        self.assertTrue(issubclass(loaded.cls, KoreanEnglishASRMixin))
        self.assertEqual(loaded.args, ('fixed-model',))
        self.assertEqual(loaded.kwargs, {'forced_aligner':'fixed-aligner',
                                        'max_inference_batch_size':1, 'attn_implementation':'sdpa'})


if __name__ == '__main__':
    unittest.main()
