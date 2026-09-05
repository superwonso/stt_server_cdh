from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from server.qwen_boundary import MAX_BOUNDARY_BYTES, MAX_BOUNDARY_TOKENS, reconcile_tokens


def token(text, start, end):
    return SimpleNamespace(text=text, start_time=start, end_time=end)


def partition(items, *, start=0.0, duration=8.0, overlap=0.0, final=False, context=None):
    lower = max(0.0, overlap - 0.6)
    upper = duration if final else max(lower, duration - 0.6)
    keep = [index for index, item in enumerate(items) if lower <= (item.start_time + item.end_time) / 2 < upper]
    return reconcile_tokens(
        items,
        keep,
        start_seconds=start,
        duration=duration,
        overlap_seconds=overlap,
        lower=lower,
        upper=upper,
        context=context,
    )


class QwenBoundaryTests(unittest.TestCase):
    def test_withheld_word_is_recovered_when_alignment_moves_back_by_point_two_seconds(self):
        first, context = partition([token("경계어", 7.4, 7.6)])
        self.assertEqual(first, [])
        self.assertFalse(context["tokens"][0]["emitted"])
        second, output = partition(
            [token("경계어", 2.2, 2.4)],
            start=5.0, duration=4.0, overlap=3.0, final=True, context=context,
        )
        self.assertEqual(second, [0])
        self.assertTrue(output["tokens"][0]["emitted"])

    def test_two_distinct_anchors_remove_replay_after_point_two_seconds_drift(self):
        first_items = [
            token("앞문맥", 6.8, 7.0),
            token("경계어", 7.2, 7.4),
            token("뒷문맥", 7.6, 7.8),
        ]
        first, context = partition(first_items)
        self.assertEqual(first, [0, 1])
        second, output = partition(
            [token("앞문맥", 2.0, 2.2), token("경계어", 2.4, 2.6), token("뒷문맥", 2.8, 3.0)],
            start=5.0, duration=4.0, overlap=3.0, final=True, context=context,
        )
        self.assertEqual(second, [2])
        self.assertTrue(next(item for item in output["tokens"] if item["text"] == "경계어")["emitted"])

    def test_a_single_equal_word_is_not_enough_evidence_to_delete_a_new_repeat(self):
        _, context = partition([token("정말", 7.2, 7.4)])
        second, _ = partition(
            [token("정말", 2.4, 2.6)],
            start=5.0, duration=4.0, overlap=3.0, final=True, context=context,
        )
        self.assertEqual(second, [0])

    def test_periodic_speech_is_preserved_even_with_a_distinct_neighbor(self):
        _, context = partition([token("이건", 6.8, 7.0), token("정말", 7.2, 7.4)])
        second, _ = partition(
            [token("이건", 2.0, 2.2), token("정말", 2.4, 2.6), token("정말", 2.6, 2.8)],
            start=5.0, duration=4.0, overlap=3.0, final=True, context=context,
        )
        self.assertEqual(second, [1, 2])

    def test_inconsistent_neighbor_timing_does_not_authorize_removal(self):
        _, context = partition([token("앞문맥", 6.8, 7.0), token("경계어", 7.2, 7.4)])
        second, _ = partition(
            [token("앞문맥", 1.7, 1.9), token("경계어", 2.4, 2.6)],
            start=5.0, duration=4.0, overlap=3.0, final=True, context=context,
        )
        self.assertEqual(second, [1])

    def test_context_is_not_mutated_and_a_retry_returns_the_same_result(self):
        _, context = partition([token("경계어", 7.4, 7.6)])
        saved = copy.deepcopy(context)
        args = dict(start=5.0, duration=4.0, overlap=3.0, final=True, context=context)
        first = partition([token("경계어", 2.2, 2.4)], **args)
        second = partition([token("경계어", 2.2, 2.4)], **args)
        self.assertEqual(first, second)
        self.assertEqual(context, saved)

    def test_stale_unknown_malformed_and_oversized_context_fall_back(self):
        _, valid = partition([token("경계어", 7.4, 7.6)])
        stale = {**valid, "audio_end": 8.5}
        unknown = {**valid, "version": 2}
        nan = {**valid, "audio_end": float("nan")}
        boolean = {**valid, "version": True}
        extra = {**valid, "unexpected": "private"}
        bad_token = copy.deepcopy(valid)
        bad_token["tokens"][0]["text"] = "x" * 129
        too_many = {**valid, "tokens": valid["tokens"] * 97}
        wrong_flag = copy.deepcopy(valid)
        wrong_flag["tokens"][0]["emitted"] = 0
        for context in (None, {}, stale, unknown, nan, boolean, extra, bad_token, too_many, wrong_flag):
            with self.subTest(context_type=type(context).__name__):
                keep, metadata = partition(
                    [token("경계어", 2.2, 2.4)],
                    start=5.0, duration=4.0, overlap=3.0, final=True, context=context,
                )
                self.assertEqual(keep, [])
                self.assertEqual(metadata["version"], 1)

    def test_context_cannot_affect_a_nonoverlapping_or_discontinuous_window(self):
        _, context = partition([token("경계어", 7.4, 7.6)])
        keep, _ = partition(
            [token("경계어", 2.2, 2.4)],
            start=7.0, duration=4.0, overlap=3.0, final=True, context=context,
        )
        self.assertEqual(keep, [])
        keep, _ = partition([token("경계어", 0.1, 0.3)], duration=1.0, final=True, context=context)
        self.assertEqual(keep, [0])

    def test_reconciliation_never_promotes_the_still_unstable_right_guard(self):
        _, context = partition([token("경계어", 7.4, 7.6)])
        keep, _ = partition(
            [token("경계어", 2.0, 2.2)],
            start=5.5, duration=2.5, overlap=2.5, final=False, context=context,
        )
        self.assertEqual(keep, [])

    def test_final_overlap_only_window_can_recover_a_pending_last_word(self):
        _, context = partition([token("경계어", 7.4, 7.6)])
        keep, _ = partition(
            [token("경계어", 2.2, 2.4)],
            start=5.0, duration=3.0, overlap=3.0, final=True, context=context,
        )
        self.assertEqual(keep, [0])

    def test_private_metadata_is_bounded_and_contains_no_unrelated_fields(self):
        items = [token("가" * 128, index * 0.02, index * 0.02 + 0.01) for index in range(600)]
        _, metadata = partition(items, duration=12.0)
        self.assertEqual(set(metadata), {"version", "audio_end", "tokens"})
        self.assertLessEqual(len(metadata["tokens"]), MAX_BOUNDARY_TOKENS)
        self.assertLessEqual(len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")), MAX_BOUNDARY_BYTES)
        self.assertTrue(all(item["start"] >= 6.0 for item in metadata["tokens"]))
        self.assertTrue(all(set(item) == {"text", "start", "end", "emitted"} for item in metadata["tokens"]))

    def test_metadata_clamps_inference_padding_to_real_pcm_end(self):
        _, metadata = partition([token("끝", 0.1, 0.48)], duration=0.4, final=True)
        self.assertEqual(metadata["tokens"][0]["end"], 0.4)
        self.assertEqual(metadata["audio_end"], 0.4)

    def test_legacy_left_overlap_is_not_mislabeled_as_a_withheld_right_tail(self):
        keep, metadata = partition(
            [token("옛문맥", 2.1, 2.3), token("새문맥", 3.5, 3.7)],
            start=5.0, duration=4.0, overlap=3.0, context=None,
        )
        self.assertEqual(keep, [])
        self.assertEqual([item["text"] for item in metadata["tokens"]], ["새문맥"])
        self.assertFalse(metadata["tokens"][0]["emitted"])


if __name__ == "__main__":
    unittest.main()
