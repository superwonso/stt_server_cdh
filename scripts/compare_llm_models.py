#!/usr/bin/env python3
"""Opt-in NOVA comparison using public synthetic data and real app adapters.

Never reads lectures, audio, or the application database. Uses only the existing
gateway credential through Settings; no direct provider API or AI judge. Output
is an exclusive, owner-only JSONL report. No response headers or reasoning text
are recorded. Each request is bounded and automatic retries are disabled.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.llm_eval_cases import DATASET_VERSION, RUBRIC, evaluate_case, get_cases
from server.postprocessor import MindlogicPostprocessor
from server.question_answerer import QuestionAnswerer, select_evidence
from server.settings import Settings
from server.summarizer import MindlogicSummarizer
from server.translator import MindlogicTranslator


# General NOVA public tariff, 2026-09-02 table (not a verified school debit).
# Credits per 1K uncached input / output tokens, NOT USD per 1M tokens.
PRICES = {
    "solar-pro4": (0.3, 1.2),
    "gpt-5.6-luna": (0.2, 1.2),
    "deepseek-v4-flash": (0.2, 0.4),
    "glm-5.3-flash": (0.15, 0.5),
    "glm-5.3": (1.4, 4.4),
}
PRICE_SOURCE = "https://docs.mindlogic.ai/docs/general/factchat/product/model-credits"


class RequestBudget:
    def __init__(self, maximum: int, deadline: float):
        self.maximum, self.deadline, self.used = maximum, deadline, 0
        self.lock = threading.Lock()

    def take(self):
        with self.lock:
            if self.used >= self.maximum or time.monotonic() >= self.deadline:
                raise RuntimeError("evaluation_budget_exhausted")
            self.used += 1


def safe_usage(response):
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return {}
    result = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if type(value) is int and value >= 0:
            result[key] = value
    for key, child in (("prompt_tokens_details", "cached_tokens"),
                       ("completion_tokens_details", "reasoning_tokens")):
        detail = usage.get(key)
        value = detail.get(child) if isinstance(detail, dict) else None
        if type(value) is int and value >= 0:
            result[child] = value
    return result


def estimated_credits(model, calls):
    if not calls or not all({"prompt_tokens", "completion_tokens"} <= call.get("usage", {}).keys()
                            for call in calls):
        return None
    input_rate, output_rate = PRICES[model]
    return round(sum(call["usage"]["prompt_tokens"] * input_rate
                     + call["usage"]["completion_tokens"] * output_rate
                     for call in calls) / 1000, 6)


def run_case(base, model, case, budget):
    inputs = case["inputs"]
    settings = replace(base, mindlogic_model=model, summary_model=model, translation_model=model,
                       correction_max_retries=0, correction_read_timeout_seconds=60,
                       **inputs.get("settings_hint", {}))
    feature = case["feature"]
    engine = {"correction": MindlogicPostprocessor, "summary": MindlogicSummarizer,
              "translation": MindlogicTranslator, "question_answering": QuestionAnswerer}[feature](settings)
    transport = engine if feature == "correction" else engine._transport
    original = transport._request
    calls = []
    start = time.monotonic()
    result = {"type": "case", "model": model, "case_id": case["id"], "feature": feature,
              "calls": calls, "status": "failed"}

    def interrupted():
        return time.monotonic() - start >= 180 or time.monotonic() >= budget.deadline

    def counted(payload, interrupted=None):
        budget.take()
        call = {"usage": {}, "elapsed_seconds": None}
        calls.append(call)
        call_start = time.monotonic()
        try:
            response = original(payload, interrupted)
            call["usage"] = safe_usage(response)
            returned = response.get("model")
            if isinstance(returned, str) and returned in PRICES:
                call["returned_model"] = returned
            # Only the final assistant content from public synthetic inputs,
            # to review failures without logging credentials or hidden thought.
            choices = response.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                finish_reason = choices[0].get("finish_reason")
                if finish_reason is None or (isinstance(finish_reason, str) and finish_reason in {
                    "stop", "length", "content_filter", "tool_calls",
                }):
                    call["finish_reason"] = finish_reason
                message = choices[0].get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str):
                    call["assistant_content"] = content[:64_000]
            return response
        finally:
            call["elapsed_seconds"] = round(time.monotonic() - call_start, 3)

    transport._request = counted
    try:
        if feature == "correction":
            document = engine.correct(title=inputs.get("title", ""), language=inputs.get("language"),
                                      segments=inputs["segments"], interrupted=interrupted)
            output = {"segments": document.segments, "uncertain_terms": document.uncertain_terms}
        elif feature == "summary":
            output = engine.summarize(language=inputs.get("language"), segments=inputs["segments"],
                                      interrupted=interrupted).to_dict()
        elif feature == "translation":
            output = engine.translate(language=inputs.get("language"), segments=inputs["segments"],
                                      interrupted=interrupted).to_dict()
        else:
            evidence = select_evidence(inputs["question"], inputs["segments"])
            result["evidence_segments"] = len(evidence["segments"])
            output = engine.answer(inputs["question"], evidence["segments"], interrupted=interrupted)
        result.update(status="completed", output=output, evaluation=evaluate_case(case["id"], output))
    except Exception as error:
        # Exception messages from dependencies may contain response/request data.
        code = getattr(error, "code", None)
        result["error_type"] = type(error).__name__
        result["error_code"] = code if isinstance(code, str) and code in {
            "invalid_response", "gateway_unavailable", "authentication_failed", "credit_exhausted",
            "rate_limited", "source_too_large", "interrupted", "cancelled", "not_configured",
            "protected_content_changed", "privacy_placeholder_changed",
        } else "evaluation_failed"
    finally:
        try:
            engine.close()
        except Exception:
            # Cleanup diagnostics can include transport state. Keep the case
            # result and continue independent comparisons, without that text.
            result["cleanup_failed"] = True
    result["elapsed_seconds"] = round(time.monotonic() - start, 3)
    result["estimated_general_uncached_credits"] = estimated_credits(model, calls)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly spend existing NOVA credits.")
    parser.add_argument("--models", nargs="+", choices=tuple(PRICES), required=True)
    parser.add_argument("--repeats", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--max-calls", type=int, default=80)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--output", type=Path, required=True, help="New JSONL report; never overwrite.")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; no requests made")
    if not 1 <= args.max_calls <= 120 or len(set(args.models)) != len(args.models):
        parser.error("use unique models and --max-calls between 1 and 120")
    settings = Settings.from_env()
    if not settings.mindlogic_api_key:
        parser.error("existing NOVA gateway credential is not configured")
    started = time.monotonic()
    budget = RequestBudget(args.max_calls, started + 1200)
    lock = threading.Lock()
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as report:
        def write(row):
            with lock:
                report.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                report.flush()

        write({"type": "metadata", "created_at": datetime.now(timezone.utc).isoformat(),
               "dataset_version": DATASET_VERSION, "models": args.models, "repeats": args.repeats,
               "max_calls": args.max_calls, "workers": args.workers, "automatic_retries": 0,
               "price_unit": "credits per 1000 tokens; general tariff, NOT verified school debit",
               "price_source": PRICE_SOURCE, "prices": PRICES, "rubric": RUBRIC})

        def run_model(model):
            for repeat in range(1, args.repeats + 1):
                for case in get_cases():
                    result = run_case(settings, model, case, budget)
                    result["repeat"] = repeat
                    write(result)
                    print(json.dumps({key: result.get(key) for key in (
                        "model", "case_id", "repeat", "status", "elapsed_seconds", "error_code")},
                        ensure_ascii=False), flush=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(run_model, args.models))
        write({"type": "complete", "http_requests": budget.used,
               "wall_seconds": round(time.monotonic() - started, 3)})
    print(json.dumps({"report_written": True, "http_requests": budget.used}), flush=True)


if __name__ == "__main__":
    main()
