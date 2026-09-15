"""Offline runner regression tests; synthetic adapters, clocks and reports only."""
from __future__ import annotations

import concurrent.futures
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import compare_llm_models as runner
from scripts.llm_eval_cases import get_cases
from server.postprocessor import PostprocessingError
from server.settings import Settings
from tests.test_llm_eval_cases import valid_outputs


def test_settings(**updates):
    base = Settings(data_dir=Path(tempfile.gettempdir()) / "unused-model-compare-data",
                    model_cache_dir=Path(tempfile.gettempdir()) / "unused-model-compare-cache",
                    mindlogic_api_key="synthetic-runner-key",
                    mindlogic_model="original-correction", summary_model="original-summary",
                    translation_model="original-translation", correction_max_retries=2,
                    correction_read_timeout_seconds=90)
    return replace(base, **updates)


class SyntheticAdapters:
    """Exercise the real runner without constructing any HTTP client."""
    def __init__(self, *, response=None, error=None, before_call=None, requests_per_case=1, close_error=None):
        self.response = response if response is not None else {
            "model": "gpt-5.6-luna", "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            "choices": [{"message": {"content": "public synthetic assistant result"}}],
        }
        self.error, self.before_call, self.requests_per_case = error, before_call, requests_per_case
        self.close_error = close_error
        self.instances, self.requests, self.invocations = [], [], []
        self.outputs = valid_outputs()
        self.by_source = {(case["feature"], case["inputs"]["segments"][0]["id"]): case["id"]
                          for case in get_cases() if case["feature"] != "question_answering"}

    def factory(self, feature):
        owner = self

        class Adapter:
            def __init__(self, settings):
                self.settings, self.closed = settings, 0
                self._request = self.original_request
                self._transport = SimpleNamespace(_request=self.original_request)
                owner.instances.append(self)

            def original_request(self, payload, interrupted):
                if owner.before_call:
                    owner.before_call(interrupted)
                if interrupted and interrupted():
                    raise PostprocessingError("interrupted", "synthetic interrupt")
                owner.requests.append({"feature": feature, "payload": copy.deepcopy(payload)})
                if owner.error is not None:
                    raise owner.error
                return copy.deepcopy(owner.response)

            def dispatch(self, **inputs):
                owner.invocations.append({"feature": feature, **inputs})
                transport = self if feature == "correction" else self._transport
                for _ in range(owner.requests_per_case):
                    transport._request({"model": self.settings.mindlogic_model}, inputs["interrupted"])
                if feature == "question_answering":
                    identifier = ("qa_related_but_missing_student_count" if "몇 명" in inputs["question"]
                                  else "qa_grounded_temperature_and_conditions")
                else:
                    identifier = owner.by_source[(feature, inputs["segments"][0]["id"])]
                return copy.deepcopy(owner.outputs[identifier])

            def correct(self, **inputs):
                value = self.dispatch(**inputs)
                return SimpleNamespace(segments=value["segments"], uncertain_terms=[])

            def summarize(self, **inputs):
                value = self.dispatch(**inputs)
                return SimpleNamespace(to_dict=lambda: copy.deepcopy(value))

            def translate(self, **inputs):
                value = self.dispatch(**inputs)
                return SimpleNamespace(to_dict=lambda: copy.deepcopy(value))

            def answer(self, question, segments, interrupted):
                return self.dispatch(question=question, segments=segments, interrupted=interrupted)

            def close(self):
                self.closed += 1
                if owner.close_error is not None:
                    raise owner.close_error

        return Adapter

    def patches(self):
        return patch.multiple(runner, MindlogicPostprocessor=self.factory("correction"),
                              MindlogicSummarizer=self.factory("summary"),
                              MindlogicTranslator=self.factory("translation"),
                              QuestionAnswerer=self.factory("question_answering"))


class RequestBudgetTests(unittest.TestCase):
    def test_cap_is_atomic_under_concurrent_attempts(self):
        budget = runner.RequestBudget(7, float("inf"))
        barrier = threading.Barrier(8)

        def take_many(_):
            barrier.wait()
            accepted = 0
            for _ in range(20):
                try:
                    budget.take()
                    accepted += 1
                except RuntimeError as error:
                    self.assertEqual(str(error), "evaluation_budget_exhausted")
            return accepted

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            accepted = sum(pool.map(take_many, range(8)))
        self.assertEqual(accepted, 7)
        self.assertEqual(budget.used, 7)

    def test_at_or_after_deadline_denies_without_spending(self):
        for now in (100.0, 101.0):
            budget = runner.RequestBudget(3, 100.0)
            with patch.object(runner.time, "monotonic", return_value=now):
                with self.assertRaisesRegex(RuntimeError, "^evaluation_budget_exhausted$"):
                    budget.take()
            self.assertEqual(budget.used, 0)
        budget = runner.RequestBudget(1, 100.0)
        with patch.object(runner.time, "monotonic", return_value=99.0):
            budget.take()
            with self.assertRaises(RuntimeError):
                budget.take()
        self.assertEqual(budget.used, 1)


class UsageTests(unittest.TestCase):
    def test_usage_retains_only_nonnegative_integer_allowlist(self):
        response = {"usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                              "secret": "synthetic-secret", "reasoning_text": "synthetic-thought",
                              "prompt_tokens_details": {"cached_tokens": 30, "secret": "private"},
                              "completion_tokens_details": {"reasoning_tokens": 10, "text": "private"}}}
        original = copy.deepcopy(response)
        self.assertEqual(runner.safe_usage(response), {"prompt_tokens": 100, "completion_tokens": 20,
                         "total_tokens": 120, "cached_tokens": 30, "reasoning_tokens": 10})
        self.assertEqual(response, original)

    def test_boolean_negative_string_float_and_malformed_details_are_rejected(self):
        for invalid in (True, False, -1, "30", 1.5, None, [], {}):
            usage = {key: invalid for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
            usage["prompt_tokens_details"] = {"cached_tokens": invalid}
            usage["completion_tokens_details"] = {"reasoning_tokens": invalid}
            self.assertEqual(runner.safe_usage({"usage": usage}), {})
        for usage in (None, [], "private", 30, True):
            self.assertEqual(runner.safe_usage({"usage": usage}), {})
        self.assertEqual(runner.safe_usage({}), {})
        self.assertEqual(runner.safe_usage({"usage": {"prompt_tokens": 0, "completion_tokens": 0}}),
                         {"prompt_tokens": 0, "completion_tokens": 0})

    def test_estimate_is_unknown_if_any_call_lacks_usage(self):
        complete = {"usage": {"prompt_tokens": 1000, "completion_tokens": 500}}
        for calls in ([], [{}], [{"usage": {}}], [complete, {"usage": {"prompt_tokens": 10}}],
                      [complete, {"usage": {"completion_tokens": 10}}]):
            self.assertIsNone(runner.estimated_credits("solar-pro4", calls))
        self.assertEqual(runner.estimated_credits("solar-pro4", [complete]), 0.9)
        self.assertEqual(runner.estimated_credits("deepseek-v4-flash", [complete]), 0.4)
        self.assertEqual(runner.estimated_credits("solar-pro4", [complete, complete]), 1.8)
        self.assertEqual(runner.estimated_credits("solar-pro4", [{"usage": {"prompt_tokens": 0, "completion_tokens": 0}}]), 0.0)


class RunCaseTests(unittest.TestCase):
    def test_all_feature_branches_complete_close_and_override_three_models_without_changing_base(self):
        adapters = SyntheticAdapters()
        base = test_settings()
        before = copy.deepcopy(vars(base))
        budget = runner.RequestBudget(20, float("inf"))
        with adapters.patches(), patch("httpx.Client", side_effect=AssertionError("no HTTP client")):
            reports = [runner.run_case(base, "gpt-5.6-luna", case, budget) for case in get_cases()]
        self.assertEqual(len(adapters.requests), 6)
        self.assertEqual(budget.used, 6)
        self.assertEqual({report["feature"] for report in reports}, {"correction", "summary", "translation", "question_answering"})
        for instance, report in zip(adapters.instances, reports, strict=True):
            self.assertEqual(instance.closed, 1)
            self.assertEqual(instance.settings.mindlogic_model, "gpt-5.6-luna")
            self.assertEqual(instance.settings.summary_model, "gpt-5.6-luna")
            self.assertEqual(instance.settings.translation_model, "gpt-5.6-luna")
            self.assertEqual(instance.settings.correction_max_retries, 0)
            self.assertEqual(instance.settings.correction_read_timeout_seconds, 60)
            self.assertEqual(report["status"], "completed")
            self.assertTrue(report["evaluation"]["hard_pass"])
            self.assertEqual(len(report["calls"]), 1)
            self.assertEqual(report["calls"][0]["returned_model"], "gpt-5.6-luna")
            self.assertEqual(report["estimated_general_uncached_credits"], 0.044)
            self.assertGreaterEqual(report["elapsed_seconds"], 0)
        bank_index = next(i for i, case in enumerate(get_cases()) if "late_bank" in case["id"])
        self.assertEqual(adapters.instances[bank_index].settings.translation_chunk_chars, 300)
        self.assertEqual(vars(base), before)
        self.assertTrue(all(report["evidence_segments"] for report in reports if report["feature"] == "question_answering"))

    def test_failures_are_redacted_close_every_engine_and_do_not_retry(self):
        for code in ("invalid_response", "authentication_failed", "protected_content_changed",
                     "privacy_placeholder_changed", "synthetic-private-code", ["synthetic-private-code"]):
            adapters = SyntheticAdapters(error=PostprocessingError(code, "synthetic-secret-key and raw response body"))
            budget = runner.RequestBudget(10, float("inf"))
            with adapters.patches():
                reports = [runner.run_case(test_settings(), "solar-pro4", case, budget) for case in get_cases()]
            self.assertEqual(len(adapters.requests), 6)
            self.assertTrue(all(instance.closed == 1 for instance in adapters.instances))
            for report in reports:
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["error_type"], "PostprocessingError")
                self.assertEqual(report["error_code"], code if code in (
                    "invalid_response", "authentication_failed", "protected_content_changed", "privacy_placeholder_changed"
                ) else "evaluation_failed")
                self.assertEqual(len(report["calls"]), 1)
                self.assertIsNone(report["estimated_general_uncached_credits"])
                self.assertNotIn("synthetic-secret", json.dumps(report))
                self.assertNotIn("raw response body", json.dumps(report))
                self.assertNotIn("synthetic-private-code", json.dumps(report))

    def test_cleanup_failure_is_redacted_and_does_not_abort_completed_or_failed_cases(self):
        for error in (None, PostprocessingError("invalid_response", "private-operation-diagnostic")):
            adapters = SyntheticAdapters(error=error, close_error=RuntimeError("private-close-key-and-state"))
            with adapters.patches():
                reports = [runner.run_case(test_settings(), "solar-pro4", case, runner.RequestBudget(2, float("inf")))
                           for case in get_cases()]
            self.assertEqual(len(reports), 6)
            for report, instance in zip(reports, adapters.instances, strict=True):
                self.assertEqual(report["status"], "failed" if error else "completed")
                self.assertEqual(instance.closed, 1)
                self.assertIs(report["cleanup_failed"], True)
                self.assertNotIn("private-close", json.dumps(report))
                self.assertNotIn("private-operation", json.dumps(report))
                if error:
                    self.assertEqual(report["error_code"], "invalid_response")
                else:
                    self.assertTrue(report["evaluation"]["hard_pass"])

    def test_only_allowlisted_finish_reasons_are_recorded(self):
        for reason in (None, "stop", "length", "content_filter", "tool_calls", "private-reason", True, ["private"], {"secret": "private"}):
            adapters = SyntheticAdapters(response={"usage": {}, "choices": [{"finish_reason": reason,
                "message": {"content": "public synthetic content"}}]})
            with adapters.patches():
                report = runner.run_case(test_settings(), "solar-pro4", get_cases()[0], runner.RequestBudget(2, float("inf")))
            if reason is None or (isinstance(reason, str) and reason in {"stop", "length", "content_filter", "tool_calls"}):
                self.assertEqual(report["calls"][0]["finish_reason"], reason)
            else:
                self.assertNotIn("finish_reason", report["calls"][0])
            self.assertNotIn("private-reason", json.dumps(report))

    def test_global_cap_blocks_adapter_transport_without_losing_prior_attempts(self):
        adapters = SyntheticAdapters(requests_per_case=3)
        budget = runner.RequestBudget(2, float("inf"))
        with adapters.patches():
            report = runner.run_case(test_settings(), "solar-pro4", get_cases()[0], budget)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(budget.used, 2)
        self.assertEqual(len(adapters.requests), 2)
        self.assertEqual(len(report["calls"]), 2)
        self.assertEqual(adapters.instances[0].closed, 1)
        self.assertEqual(report["estimated_general_uncached_credits"], 0.108)

    def test_case_and_global_deadlines_interrupt_without_sending_to_synthetic_provider(self):
        for advanced, deadline in ((280.0, float("inf")), (110.0, 110.0)):
            clock = [100.0]
            checks = []

            def advance(interrupted):
                checks.append(interrupted())
                clock[0] = advanced
                checks.append(interrupted())

            adapters = SyntheticAdapters(before_call=advance)
            with adapters.patches(), patch.object(runner.time, "monotonic", side_effect=lambda: clock[0]):
                report = runner.run_case(test_settings(), "solar-pro4", get_cases()[0], runner.RequestBudget(2, deadline))
            self.assertEqual(checks, [False, True])
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["error_code"], "interrupted")
            self.assertEqual(adapters.requests, [])
            self.assertEqual(adapters.instances[0].closed, 1)

    def test_unknown_model_metadata_reasoning_and_header_content_never_enter_report(self):
        adapters = SyntheticAdapters(response={"model": "synthetic-private-returned-name", "headers": {"secret": "private-header"},
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "secret": "private-usage"},
            "choices": [{"message": {"content": "a" * 65000, "reasoning_content": "private-reasoning",
                                      "reasoning": "private-reasoning"}}]})
        with adapters.patches():
            report = runner.run_case(test_settings(), "solar-pro4", get_cases()[0], runner.RequestBudget(2, float("inf")))
        serialized = json.dumps(report)
        for forbidden in ("synthetic-private-returned-name", "private-header", "private-usage", "private-reasoning", "synthetic-runner-key"):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn("returned_model", report["calls"][0])
        self.assertEqual(len(report["calls"][0]["assistant_content"]), 64000)
        self.assertEqual(report["calls"][0]["usage"], {"prompt_tokens": 1, "completion_tokens": 1})


class MainTests(unittest.TestCase):
    def output_path(self, directory, name):
        from server.platform_files import ensure_private_directory
        parent = Path(directory) / "private"
        ensure_private_directory(parent)
        return parent / name

    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(runner.sys, "argv", ["compare_llm_models", *args]), redirect_stdout(stdout), redirect_stderr(stderr):
            runner.main()
        return stdout.getvalue(), stderr.getvalue()

    def test_without_explicit_live_does_not_read_credentials_construct_http_or_create_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "not-created.jsonl"
            with patch.object(runner.Settings, "from_env") as settings, patch("httpx.Client") as client:
                with self.assertRaises(SystemExit) as caught:
                    self.invoke(["--models", "solar-pro4", "--output", str(output)])
            self.assertEqual(caught.exception.code, 2)
            settings.assert_not_called(); client.assert_not_called()
            self.assertFalse(output.exists())

    def test_invalid_caps_and_duplicate_models_fail_before_credentials_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "not-created.jsonl"
            for options in (["--models", "solar-pro4", "solar-pro4"],
                            ["--models", "solar-pro4", "--max-calls", "0"],
                            ["--models", "solar-pro4", "--max-calls", "121"]):
                with patch.object(runner.Settings, "from_env") as settings:
                    with self.assertRaises(SystemExit) as caught:
                        self.invoke(["--live", *options, "--output", str(output)])
                self.assertEqual(caught.exception.code, 2)
                settings.assert_not_called()
                self.assertFalse(output.exists())

    def test_missing_credentials_stops_before_output_and_existing_report_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.output_path(directory, "report.jsonl")
            args = ["--live", "--models", "solar-pro4", "--output", str(output)]
            with patch.object(runner.Settings, "from_env", return_value=test_settings(mindlogic_api_key=None)), \
                    patch.object(runner, "run_case") as run:
                with self.assertRaises(SystemExit):
                    self.invoke(args)
                run.assert_not_called()
            self.assertFalse(output.exists())
            output.write_text("synthetic old report\n", encoding="utf-8")
            with patch.object(runner.Settings, "from_env", return_value=test_settings()), patch.object(runner, "run_case") as run:
                with self.assertRaises(FileExistsError):
                    self.invoke(args)
                run.assert_not_called()
            self.assertEqual(output.read_text(encoding="utf-8"), "synthetic old report\n")

    def test_main_writes_private_jsonl_metadata_cases_and_completion_for_named_models(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.output_path(directory, "report.jsonl")
            adapters = SyntheticAdapters()
            with patch.object(runner.Settings, "from_env", return_value=test_settings()), adapters.patches(), \
                    patch.object(runner, "get_cases", return_value=get_cases()[:2]), \
                    patch("httpx.Client", side_effect=AssertionError("no HTTP client")):
                stdout, stderr = self.invoke(["--live", "--models", "solar-pro4", "gpt-5.6-luna", "deepseek-v4-flash",
                                              "--repeats", "2", "--workers", "2", "--max-calls", "12", "--output", str(output)])
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            if os.name == "nt":
                from server.platform_files import validate_private_path
                validate_private_path(output)
            else:
                self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            self.assertEqual(rows[0]["type"], "metadata")
            self.assertEqual(rows[0]["automatic_retries"], 0)
            self.assertEqual(rows[0]["max_calls"], 12)
            self.assertEqual(rows[-1]["type"], "complete")
            self.assertEqual(rows[-1]["http_requests"], 12)
            cases = [row for row in rows if row["type"] == "case"]
            self.assertEqual(len(cases), 12)
            self.assertEqual({row["model"] for row in cases}, {"solar-pro4", "gpt-5.6-luna", "deepseek-v4-flash"})
            self.assertEqual({row["repeat"] for row in cases}, {1, 2})
            self.assertTrue(all(row["status"] == "completed" for row in cases))
            self.assertEqual(len(adapters.requests), 12)
            self.assertEqual(stderr, "")
            self.assertNotIn("synthetic-runner-key", output.read_text(encoding="utf-8") + stdout)
            self.assertNotIn("public synthetic assistant result", stdout, "stdout is aggregate metadata only")


if __name__ == "__main__":
    unittest.main()
