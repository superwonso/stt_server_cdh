#!/usr/bin/env python3
"""Opt-in NOVA study-note smoke test: two public, synthetic teaching examples.

No production DB, recordings, notes, titles, or account data are read. Without
--live, settings/credentials are not loaded and no HTTP client is constructed.
With --live there are at most TWO HTTP attempts, no retry or repair calls.
Generated documents stay in a new private temporary directory; stdout contains
only fixed case labels, counts, check booleans, timing, and safe error codes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.settings import Settings
from server.study_notes import MindlogicStudyNotes, StudyNoteError, study_note_markdown, validate_study_note_document


CASES = (
    {
        "id": "mixed_river_context",
        "texts": (
            "The bank was damaged by the current. 이번 수업에서는 왜 그런 변화가 생기는지 설명합니다.",
            "The water level rose by 15 centimeters. A higher level does not always mean a faster current.",
            "If the channel becomes wider, the same inflow can move more slowly. Here, bank means the side of a river, not a financial institution.",
        ),
    },
    {
        "id": "hangul_phonetic_context",
        "texts": (
            "오늘은 배치 놀말라이제이션을 다룹니다. This method normalizes the inputs within each mini-batch.",
            "Here, batch normalization refers to using batch statistics. It does not mean that every sample becomes identical.",
            "표본은 16개입니다. 평가할 때에는 학습과 같은 통계량 계산을 무조건 반복한다고 단정하지 않습니다.",
        ),
    },
)


def case_segments(case):
    return [{"id": f"synthetic-{index}", "start": index * 10, "end": (index + 1) * 10, "text": text}
            for index, text in enumerate(case["texts"])]


def quality_checks(case, document):
    """Transparent lexical smoke checks, not an AI judge or semantic proof."""
    body = "\n".join(paragraph["text"] for paragraph in document["paragraphs"])
    edits = [edit for paragraph in document["paragraphs"] for edit in paragraph["edits"]]
    checks = {"korean_body": any("가" <= char <= "힣" for char in body),
              "negation_retained": any(word in body for word in ("않", "아니", "아닙", "아닌", "아님", "없", "단정할 수"))}
    if case["id"] == "mixed_river_context":
        checks.update(
            river_sense=any(word in body for word in ("강둑", "강기슭", "제방", "하안", "강의 둑")),
            condition_retained="넓" in body and any(word in body for word in ("느리", "느려", "천천", "낮아")),
            english_sentences_translated=not any(text in body for text in (
                "The bank was damaged", "A higher level does not", "If the channel becomes wider")),
            protected_number_retained="15" in body,
        )
    else:
        checks.update(
            korean_term_restored="배치 정규화" in body,
            phonetic_edit_audited=any(edit["original"] in "배치 놀말라이제이션"
                                      and "정규화" in edit["replacement"] for edit in edits),
            english_sentences_translated=not any(text in body for text in (
                "This method normalizes", "Here, batch normalization refers", "It does not mean")),
            protected_number_retained="16" in body,
        )
    return checks


class AttemptBudget:
    """Enforced immediately before HTTP send, including future code changes."""
    def __init__(self):
        self.attempts = 0
        self.deadline = time.monotonic() + 180

    def check_request(self, request):
        if (self.attempts >= 2 or time.monotonic() >= self.deadline
                or request.method != "POST"
                or str(request.url) != "https://factchat-cloud.mindlogic.ai/v1/gateway/chat/completions/"):
            raise StudyNoteError("interrupted")
        self.attempts += 1

    def interrupted(self):
        return time.monotonic() >= self.deadline


def _write_private(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Send at most two NOVA requests and use credits")
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps({"status": "not_run", "http_attempts": 0, "reason": "explicit_live_flag_required"}))
        return 0
    try:
        return _run_live()
    except Exception:
        # Never expose configuration values, HTTP exception bodies, or a
        # provider response in a setup/cleanup traceback.
        print(json.dumps({"status": "failed", "code": "validation_setup_or_cleanup_failed"}), flush=True)
        return 1


def _run_live():
    settings = replace(Settings.from_env(), correction_max_retries=0,
                       correction_connect_timeout_seconds=10, correction_read_timeout_seconds=60)
    directory = Path(tempfile.mkdtemp(prefix="stt-study-note-check-"))
    directory.chmod(0o700)
    budget, reports, all_passed = AttemptBudget(), [], True
    with httpx.Client(timeout=httpx.Timeout(60, connect=10, write=10, pool=10),
                      follow_redirects=False, trust_env=False,
                      event_hooks={"request": [budget.check_request]}) as client:
        engine = MindlogicStudyNotes(settings, client)
        try:
            for case in CASES:
                before, began = budget.attempts, time.monotonic()
                report = {"case": case["id"]}
                try:
                    raw = case_segments(case)
                    result = engine.create(language="auto", segments=raw, interrupted=budget.interrupted)
                    document = validate_study_note_document(result.to_dict(), raw)
                    markdown = study_note_markdown(document, raw)
                    checks = quality_checks(case, document)
                    passed = all(checks.values())
                    report.update(status="passed" if passed else "review_needed", checks=checks,
                                  paragraphs=len(document["paragraphs"]), source_segments=len(raw))
                    _write_private(directory / (case["id"] + ".json"), json.dumps(document, ensure_ascii=False, indent=2) + "\n")
                    _write_private(directory / (case["id"] + ".md"), markdown)
                    all_passed = all_passed and passed
                except Exception as error:
                    safe = StudyNoteError(error.code) if isinstance(error, StudyNoteError) else StudyNoteError("invalid_response")
                    report.update(status="failed", code=safe.code)
                    all_passed = False
                report.update(seconds=round(time.monotonic() - began, 3), http_attempts=budget.attempts - before)
                reports.append(report)
                print(json.dumps(report, ensure_ascii=False), flush=True)
        finally:
            engine.close()
    _write_private(directory / "report.json", json.dumps({"http_attempts": budget.attempts, "results": reports}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "passed" if all_passed else "review_needed", "http_attempts": budget.attempts,
                      "artifact_directory": str(directory), "semantic_accuracy_guaranteed": False}), flush=True)
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
