import unittest

import numpy as np

from scripts.validate_chunk_pipeline import ChunkSpec, transcribe_validation_chunk


class ValidationBoundaryContractTests(unittest.TestCase):
    def test_validation_passes_absolute_time_and_detached_committed_context(self):
        class Engine:
            def transcribe(self, samples, language, **kwargs):
                self.kwargs = kwargs
                kwargs["boundary_output"].update({"version": 1, "tokens": ["test"]})
                return [{"start": 0, "end": 1, "text": "검증"}]
        engine = Engine()
        original = {"version": 1, "tokens": ["previous"]}
        result, committed = transcribe_validation_chunk(
            engine, np.zeros(16000), ChunkSpec(16000, 32000, 8000, True), original,
        )
        self.assertEqual(result[0]["text"], "검증")
        self.assertEqual(engine.kwargs["start_seconds"], 1)
        self.assertEqual(engine.kwargs["overlap_seconds"], .5)
        self.assertTrue(engine.kwargs["final_chunk"])
        self.assertIs(engine.kwargs["boundary_context"], original)
        self.assertEqual(committed, {"version": 1, "tokens": ["test"]})
        engine.kwargs["boundary_output"]["tokens"].append("changed")
        self.assertEqual(committed["tokens"], ["test"])

    def test_baseline_does_not_activate_context_and_failed_call_does_not_commit(self):
        class Engine:
            def transcribe(self, samples, language, **kwargs):
                self.kwargs = kwargs
                return []
        engine = Engine()
        result, context = transcribe_validation_chunk(
            engine, np.zeros(16000), ChunkSpec(0, 16000, 0, True), use_context=False,
        )
        self.assertEqual(result, [])
        self.assertIsNone(context)
        self.assertNotIn("boundary_output", engine.kwargs)
        class Broken:
            def transcribe(self, *args, **kwargs):
                kwargs["boundary_output"]["new"] = "not committed"
                raise RuntimeError("synthetic failure")
        with self.assertRaises(RuntimeError):
            transcribe_validation_chunk(Broken(), np.zeros(16000), ChunkSpec(0, 16000, 0, True))
