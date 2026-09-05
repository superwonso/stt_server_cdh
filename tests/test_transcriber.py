from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from server.settings import Settings
from server.transcriber import LocalTranscriber, aligned_text_slice, contains_speech


class FakeQwen:
    def __init__(self, text="받아쓰기 결과", items=None, error=None):
        self.calls = []
        self.text = text
        self.items = items
        self.error = error

    def transcribe(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        items = self.items
        if items is None:
            items = [] if not self.text else [SimpleNamespace(text=self.text, start_time=0.1, end_time=0.3)]
        return [SimpleNamespace(text=self.text, time_stamps=SimpleNamespace(items=items))]


class TranscriberTests(unittest.TestCase):
    def engine(self, model):
        engine = LocalTranscriber(Settings(data_dir=Path("unused"), model_cache_dir=Path("unused")))
        engine._model = model
        engine._set_state("ready")
        return engine

    def test_silence_and_short_noise_are_not_sent_to_the_model(self):
        model = FakeQwen()
        engine = self.engine(model)
        self.assertEqual(engine.transcribe(np.zeros(128000, dtype=np.float32), "ko"), [])
        impulse = np.zeros(128000, dtype=np.float32)
        impulse[100:500] = 0.8
        self.assertEqual(engine.transcribe(impulse, "ko"), [])
        self.assertEqual(model.calls, [])

    def test_final_chunk_uses_a_shorter_vad_gate_for_the_last_syllable(self):
        time = np.arange(1280, dtype=np.float32) / 16000
        short_voice = (0.3 * np.sin(2 * np.pi * 180 * time)).astype(np.float32)
        model = FakeQwen(items=[SimpleNamespace(text="끝", start_time=0.01, end_time=0.05)])
        engine = self.engine(model)
        self.assertEqual(engine.transcribe(short_voice, "ko", final_chunk=False), [])
        self.assertTrue(engine.transcribe(short_voice, "ko", final_chunk=True))
        self.assertEqual(len(model.calls), 1)

    def test_short_tail_is_padded_only_for_inference(self):
        # Speech-like amplitude modulation passes the least-aggressive WebRTC VAD.
        time = np.arange(6400, dtype=np.float32) / 16000
        samples = (0.3 * np.sin(2 * np.pi * 180 * time)).astype(np.float32)
        model = FakeQwen()
        result = self.engine(model).transcribe(samples, "ko")
        self.assertEqual(len(model.calls[0]["audio"][0]), 8000)
        self.assertEqual(result, [{"start": 0.1, "end": 0.3, "text": "받아쓰기 결과"}])
        self.assertEqual(model.calls[0]["language"], "Korean")

    def test_language_mapping_and_empty_result(self):
        time = np.arange(16000, dtype=np.float32) / 16000
        samples = (0.3 * np.sin(2 * np.pi * 180 * time)).astype(np.float32)
        model = FakeQwen("")
        engine = self.engine(model)
        self.assertEqual(engine.transcribe(samples, None), [])
        self.assertIsNone(model.calls[-1]["language"])
        engine.transcribe(samples, "en")
        self.assertEqual(model.calls[-1]["language"], "English")
        with self.assertRaises(ValueError):
            engine.transcribe(samples, "ja")

    def test_aligned_slice_preserves_punctuation_between_kept_words(self):
        items = [
            SimpleNamespace(text="앞부분", start_time=0.1, end_time=0.5),
            SimpleNamespace(text="프랑스", start_time=1.5, end_time=2.1),
            SimpleNamespace(text="남부를", start_time=2.2, end_time=2.8),
            SimpleNamespace(text="침략했다", start_time=2.9, end_time=3.6),
        ]
        text = "앞부분. 프랑스 남부를 침략했다."
        self.assertEqual(aligned_text_slice(text, items, [1, 2]), "프랑스 남부를")
        self.assertEqual(aligned_text_slice(text, items, [1, 2, 3]), "프랑스 남부를 침략했다.")

    def test_aligned_slice_keeps_final_question_mark_and_complete_quotes(self):
        items = [SimpleNamespace(text="맞나요")]
        self.assertEqual(aligned_text_slice("맞나요?", items, [0]), "맞나요?")
        self.assertEqual(aligned_text_slice('“맞나요?”', items, [0]), '“맞나요?”')

    def test_aligned_slice_keeps_punctuation_at_the_last_kept_token(self):
        items = [SimpleNamespace(text="첫문장"), SimpleNamespace(text="둘째문장")]
        self.assertEqual(aligned_text_slice("첫문장! 둘째문장?", items, [0]), "첫문장!")
        self.assertEqual(aligned_text_slice("첫문장! 둘째문장?", items, [1]), "둘째문장?")

    def test_overlap_guard_assigns_boundary_words_exactly_once(self):
        time = np.arange(8 * 16000, dtype=np.float32) / 16000
        samples = (0.3 * np.sin(2 * np.pi * 180 * time)).astype(np.float32)
        first_items = [
            SimpleNamespace(text="앞", start_time=5.8, end_time=6.2),
            SimpleNamespace(text="안정", start_time=7.0, end_time=7.4),
            SimpleNamespace(text="경계", start_time=7.4, end_time=7.8),
        ]
        first = self.engine(FakeQwen("앞 안정 경계", first_items)).transcribe(
            samples, "ko", overlap_seconds=0, final_chunk=False
        )
        self.assertEqual(first[0]["text"], "앞 안정")

        tail_samples = samples[: 4 * 16000]
        second_items = [
            SimpleNamespace(text="안정", start_time=2.0, end_time=2.4),
            SimpleNamespace(text="경계", start_time=2.4, end_time=2.8),
            SimpleNamespace(text="뒤", start_time=3.3, end_time=3.7),
        ]
        second = self.engine(FakeQwen("안정 경계 뒤", second_items)).transcribe(
            tail_samples, "ko", overlap_seconds=3, final_chunk=True
        )
        self.assertEqual(second[0]["text"], "경계 뒤")
        self.assertEqual(f"{first[0]['text']} {second[0]['text']}", "앞 안정 경계 뒤")

    def test_call_scoped_boundary_metadata_recovers_tail_without_leaking_to_another_lecture(self):
        model = FakeQwen("경계어?", [SimpleNamespace(text="경계어", start_time=7.4, end_time=7.6)])
        engine = self.engine(model)
        first_metadata = {}
        with patch("server.transcriber.contains_speech", return_value=True):
            self.assertEqual(engine.transcribe(
                np.zeros(8 * 16000, dtype=np.float32), "ko", final_chunk=False,
                start_seconds=0.0, boundary_output=first_metadata,
            ), [])
            model.items = [SimpleNamespace(text="경계어", start_time=2.2, end_time=2.4)]
            recovered = engine.transcribe(
                np.zeros(4 * 16000, dtype=np.float32), "ko", overlap_seconds=3.0,
                start_seconds=5.0, boundary_context=first_metadata, boundary_output={},
            )
            unrelated = engine.transcribe(
                np.zeros(4 * 16000, dtype=np.float32), "ko", overlap_seconds=3.0,
                start_seconds=5.0, boundary_output={},
            )
        self.assertEqual(recovered, [{"start": 2.2, "end": 2.4, "text": "경계어?"}])
        self.assertEqual(unrelated, [])
        self.assertFalse(first_metadata["tokens"][0]["emitted"])
        self.assertEqual(set(engine.__dict__), {"settings", "_model", "_state", "_state_lock", "_load_lock"})

    def test_silence_returns_an_empty_bounded_frontier_without_loading_a_model(self):
        model = FakeQwen()
        output = {"stale": "value"}
        result = self.engine(model).transcribe(
            np.zeros(8 * 16000, dtype=np.float32), "ko", start_seconds=5.0, boundary_output=output,
        )
        self.assertEqual(result, [])
        self.assertEqual(model.calls, [])
        self.assertEqual(output, {"version": 1, "audio_end": 13.0, "tokens": []})

    def test_failed_inference_does_not_return_stale_boundary_metadata(self):
        model = FakeQwen(error=RuntimeError("inference failed"))
        output = {"stale": "value"}
        with patch("server.transcriber.contains_speech", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "inference failed"):
                self.engine(model).transcribe(
                    np.zeros(8 * 16000, dtype=np.float32), "ko", boundary_output=output,
                )
        self.assertEqual(output, {})

    def test_warmup_failure_sets_error_state(self):
        engine = self.engine(FakeQwen(error=RuntimeError("warmup failed")))
        with self.assertRaisesRegex(RuntimeError, "warmup failed"):
            engine.warmup()
        self.assertEqual(engine.status()["model_state"], "error")


if __name__ == "__main__":
    unittest.main()
