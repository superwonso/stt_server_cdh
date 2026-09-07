"""Synthetic fixture/metric tests; no model client or environment is loaded."""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from scripts.llm_eval_cases import CASES, DATASET_VERSION, RUBRIC, evaluate_case, get_cases
from server.question_answerer import select_evidence


def valid_outputs():
    cases = {case["id"]: case for case in get_cases()}
    correction = copy.deepcopy(cases["ko_correction_constraints"]["inputs"]["segments"])
    correction[0]["text"] = "오늘은 통제 변수를 살펴봅니다. 온도는 25도로 일정하게 유지했어요."
    correction[1]["text"] = "빛의 세기만 바꿨지, 온도를 바꾼 건 아닙니다."
    correction[2]["text"] = "빛을 더 세게 했을 때 기포 수가 늘었다고 해도, 그것만으로 원인이 증명되는 건 아니에요."
    correction[3]["text"] = "센서가 같은 위치에 있고 물의 양도 같을 때만 두 결과를 비교할 수 있어요."
    correction[4]["text"] = "다음 시간에는 같은 방법으로 다시 측정할 수 있지만, 과제로 제출하라는 뜻은 아닙니다."
    bank = copy.deepcopy(cases["en_translation_late_bank_context"]["inputs"]["segments"])
    for row, text in zip(bank, [
        "폭풍 뒤 강둑이 불안정해졌고 위쪽 부분이 수로를 향해 움직였습니다.",
        "많은 비가 토양 입자 사이의 공극을 채우고 지반 내부의 힘을 변화시켰습니다.",
        "물이 사면 아래쪽의 물질을 제거하여 위의 토양을 지지하는 힘이 줄어들었습니다.",
        "물이 뒤쪽에 갇혀 빠지지 못하면 벽만으로는 이 문제를 해결하지 못합니다.",
        "수로의 형태와 퇴적물의 이동을 포함하여 강을 따라 일어나는 침식을 다룹니다.",
        "첫 문장의 bank는 돈을 빌려주는 사업체가 아니라 강의 가장자리를 뜻합니다.",
    ], strict=True):
        row["text"] = text
    conditions = copy.deepcopy(cases["en_translation_numeric_negation"]["inputs"]["segments"])
    for row, text in zip(conditions, [
        "온도를 25도로 유지하고 15분 뒤 측정값을 기록하세요.",
        "센서가 보정되지 않았다면 두 실험을 비교하지 마세요.",
        "측정값이 커졌다고 해서 빛이 변화의 원인임이 증명되는 것은 아닙니다. 수위는 같게 유지해야 합니다.",
    ], strict=True):
        row["text"] = text
    summary = {
        "overview": "빛의 세기를 바꾸되 온도를 25도로 유지하며 조건을 통제한 실험이다.",
        "overview_source_ids": ["eval-lesson-0"],
        "sections": [{"heading": "실험 해석과 비교 조건", "bullets": [
            {"text": "관찰만으로 빛이 원인이라고 단정할 수 없다.", "source_ids": ["eval-lesson-1"]},
            {"text": "센서 위치와 물의 양이 같을 때만 결과를 비교한다.", "source_ids": ["eval-lesson-2"]},
        ]}],
        "review_questions": [{"question": "결과를 비교하려면 어떤 조건이 같아야 하는가?", "source_ids": ["eval-lesson-2"]}],
    }
    return {
        "ko_correction_constraints": {"segments": correction},
        "ko_summary_conditions_not_homework": summary,
        "en_translation_late_bank_context": {"segments": bank},
        "en_translation_numeric_negation": {"segments": conditions},
        "qa_grounded_temperature_and_conditions": {"answerability": "answered", "paragraphs": [
            {"text": "실험에서 온도를 25도로 유지했습니다.", "source_ids": ["eval-lesson-0"]},
            {"text": "센서의 위치와 물의 양이 같아야 결과를 비교할 수 있습니다.", "source_ids": ["eval-lesson-2"]},
        ]},
        "qa_related_but_missing_student_count": {"answerability": "insufficient_evidence", "paragraphs": []},
    }


class LlmEvalCasesTests(unittest.TestCase):
    def test_six_bounded_public_cases_cover_four_features_and_serialize(self):
        cases = get_cases()
        self.assertEqual(len(cases), 6)
        self.assertEqual(len({case["id"] for case in cases}), 6)
        self.assertEqual({case["feature"] for case in cases}, {"correction", "summary", "translation", "question_answering"})
        json.dumps(cases, ensure_ascii=False, allow_nan=False)
        json.dumps(RUBRIC, ensure_ascii=False, allow_nan=False)
        self.assertEqual(DATASET_VERSION, 1)
        for case in cases:
            with self.subTest(case=case["id"]):
                rows = case["inputs"]["segments"]
                self.assertLessEqual(len(rows), 6)
                self.assertLess(sum(len(row["text"]) for row in rows), 1000)
                self.assertEqual(len({row["id"] for row in rows}), len(rows))
                for row in rows:
                    self.assertEqual(set(row), {"id", "start", "end", "text"})
                    self.assertTrue(row["id"].startswith("eval-"))
                    self.assertGreater(row["end"], row["start"])
                self.assertTrue(case["rubric"]["must_preserve"])
                self.assertTrue(case["rubric"]["disallowed"])

    def test_caller_mutation_never_changes_constants_other_cases_or_later_runs(self):
        baseline = copy.deepcopy(CASES)
        cases = get_cases()
        summary = next(case for case in cases if case["feature"] == "summary")
        question = next(case for case in cases if case["feature"] == "question_answering")
        summary["inputs"]["segments"][0]["text"] = "changed"
        self.assertNotEqual(question["inputs"]["segments"][0]["text"], "changed")
        cases[0]["rubric"]["must_preserve"].clear()
        self.assertEqual(CASES, baseline)
        self.assertEqual(get_cases(), list(baseline))

    def test_late_bank_hint_forces_context_across_batches(self):
        case = next(case for case in get_cases() if case["id"] == "en_translation_late_bank_context")
        limit = case["inputs"]["settings_hint"]["translation_chunk_chars"]
        self.assertGreaterEqual(limit, 300)
        rows = case["inputs"]["segments"]
        used, first_batch = 0, []
        for row in rows:
            if first_batch and used + len(row["text"]) > limit:
                break
            first_batch.append(row)
            used += len(row["text"])
        self.assertNotIn(rows[-1], first_batch)
        self.assertIn("bank means", rows[-1]["text"])

    def test_missing_detail_is_lexically_related_and_requires_actual_abstention(self):
        case = next(case for case in get_cases() if case["id"] == "qa_related_but_missing_student_count")
        selected = select_evidence(case["inputs"]["question"], case["inputs"]["segments"])
        self.assertNotEqual(selected["scope"], "none")
        self.assertTrue(selected["segments"])
        self.assertEqual(case["expected"]["answerability"], "insufficient_evidence")
        self.assertNotIn("학생", " ".join(row["text"] for row in selected["segments"]))

    def test_valid_outputs_pass_real_validators_without_client_or_environment_calls(self):
        outputs = valid_outputs()
        before = copy.deepcopy(outputs)
        with patch("httpx.Client", side_effect=AssertionError("no model client")), \
                patch("server.settings.Settings.from_env", side_effect=AssertionError("no environment read")):
            for identifier, output in outputs.items():
                with self.subTest(case=identifier):
                    report = evaluate_case(identifier, output)
                    self.assertTrue(report["hard_pass"], report["checks"])
                    self.assertTrue(report["review_required"]["must_preserve"])
                    json.dumps(report, ensure_ascii=False, allow_nan=False)
        self.assertEqual(outputs, before)

    def test_foreign_ids_reordered_rows_or_changed_times_fail_closed(self):
        for identifier in ("ko_correction_constraints", "en_translation_late_bank_context"):
            for mutation in ("id", "order", "time"):
                output = valid_outputs()[identifier]
                if mutation == "id":
                    output["segments"][0]["id"] = "invented-source"
                elif mutation == "order":
                    output["segments"].reverse()
                else:
                    output["segments"][0]["start"] += 1
                with self.subTest(case=identifier, mutation=mutation):
                    self.assertFalse(evaluate_case(identifier, output)["hard_pass"])

    def test_changed_and_added_numbers_fail_even_when_other_words_match(self):
        for identifier in ("ko_correction_constraints", "en_translation_numeric_negation"):
            output = valid_outputs()[identifier]
            output["segments"][0]["text"] = output["segments"][0]["text"].replace("25", "30")
            self.assertFalse(evaluate_case(identifier, output)["hard_pass"])
            output = valid_outputs()[identifier]
            output["segments"][1]["text"] += " 99"
            self.assertFalse(evaluate_case(identifier, output)["hard_pass"])

    def test_summary_and_answer_need_real_source_links(self):
        output = valid_outputs()["ko_summary_conditions_not_homework"]
        output["sections"][0]["bullets"][0]["source_ids"] = ["invented"]
        self.assertFalse(evaluate_case("ko_summary_conditions_not_homework", output)["hard_pass"])
        output = valid_outputs()["qa_grounded_temperature_and_conditions"]
        output["paragraphs"][0]["source_ids"] = ["eval-lesson-2"]
        self.assertFalse(evaluate_case("qa_grounded_temperature_and_conditions", output)["hard_pass"],
                         "the numeric answer must cite its own supporting segment")

    def test_plausible_student_count_is_rejected_as_wrong_answerability(self):
        output = {"answerability": "answered", "paragraphs": [
            {"text": "참가한 학생은 25명입니다.", "source_ids": ["eval-lesson-0"]},
        ]}
        report = evaluate_case("qa_related_but_missing_student_count", output)
        self.assertFalse(report["hard_pass"])
        self.assertIn({"name": "expected_answerability", "passed": False}, report["checks"])
        self.assertIn({"name": "production_answer_validator", "passed": True}, report["checks"],
                      "provenance alone does not establish the meaning of a cited number")

    def test_semantic_negation_and_bank_mistakes_are_signals_not_fake_automatic_grades(self):
        output = valid_outputs()["ko_correction_constraints"]
        output["segments"][1]["text"] = "빛의 세기와 온도를 모두 바꿨습니다."
        report = evaluate_case("ko_correction_constraints", output)
        self.assertTrue(report["hard_pass"], "hard pass is deliberately not semantic correctness")
        self.assertIn({"name": "temperature_not_changed", "detected": False}, report["signals"])
        self.assertTrue(report["review_required"]["disallowed"])
        output = valid_outputs()["en_translation_late_bank_context"]
        output["segments"][0]["text"] = "폭풍 뒤 은행의 경영이 불안정해졌습니다."
        report = evaluate_case("en_translation_late_bank_context", output)
        self.assertTrue(report["hard_pass"])
        self.assertIn({"name": "river_bank_in_first_segment", "detected": False}, report["signals"])
        self.assertIn({"name": "bank_finance_word_review", "detected": True}, report["signals"])

    def test_good_phrasing_sets_helpful_signals_but_reports_keep_human_rubric(self):
        for identifier, output in valid_outputs().items():
            report = evaluate_case(identifier, output)
            expected = [signal for signal in report["signals"] if signal["name"] != "bank_finance_word_review"]
            self.assertTrue(all(signal["detected"] for signal in expected), (identifier, expected))
            self.assertNotIn("score", report)
            self.assertNotIn("semantic_pass", report)
            report["review_required"]["must_preserve"].clear()
            self.assertTrue(evaluate_case(identifier, output)["review_required"]["must_preserve"])

    def test_malformed_outputs_have_fixed_checks_not_candidate_diagnostics(self):
        for output in (None, [], "synthetic-provider-error", {"segments": [None]}, {"answerability": "unknown"}):
            for case in get_cases():
                with self.subTest(case=case["id"], kind=type(output).__name__):
                    report = evaluate_case(case["id"], output)
                    self.assertFalse(report["hard_pass"])
                    self.assertNotIn("synthetic-provider-error", json.dumps(report, ensure_ascii=False))
        with self.assertRaisesRegex(ValueError, "^unknown evaluation case$"):
            evaluate_case("arbitrary-private-value", {})


if __name__ == "__main__":
    unittest.main()
