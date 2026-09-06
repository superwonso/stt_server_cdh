#!/usr/bin/env python3
"""Opt-in synthetic Q&A checks: at most two NOVA calls plus local retrieval."""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.settings import Settings
from server.question_answerer import QuestionAnswerer, select_evidence, validate_answer_document


SOURCE = [
    {"id": "synthetic-light", "start": 0.0, "end": 10.0,
     "text": "식물의 광합성은 빛 에너지를 화학 에너지로 바꾸는 과정입니다."},
    {"id": "synthetic-oxygen", "start": 10.0, "end": 20.0,
     "text": "광합성의 명반응은 빛을 필요로 합니다. 물을 분해하면서 산소를 방출합니다."},
    {"id": "synthetic-control", "start": 20.0, "end": 30.0,
     "text": "실험에서는 빛의 세기만 바꾸고 온도를 25도로 유지했습니다. 다른 조건을 통제해야 결과를 비교할 수 있습니다."},
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use the existing NOVA key and credits; at most two HTTP POSTs.")
    parser.add_argument("--case", choices=("all", "grounded_numeric", "absent_topic", "missing_detail"), default="all")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required")
    engine = QuestionAnswerer(replace(Settings.from_env(), correction_max_retries=0,
                                     correction_read_timeout_seconds=90))
    calls = []
    original = engine._transport._request

    def counted(*args, **kwargs):
        if len(calls) >= 2:
            raise RuntimeError("request budget exceeded")
        calls.append(1)
        return original(*args, **kwargs)

    engine._transport._request = counted
    try:
        for name, question, expected in (
            ("grounded_numeric", "실험에서 온도를 어떻게 유지했고, 왜 다른 조건을 통제했나요?", "answered"),
            ("absent_topic", "이 수업에서 설명한 블랙홀의 사건 지평선 반지름은 얼마인가요?", "insufficient_evidence"),
            ("missing_detail", "온도를 통제한 실험에 참가한 학생은 몇 명인가요?", "insufficient_evidence"),
        ):
            if args.case not in ("all", name):
                continue
            began = time.monotonic()
            selected = select_evidence(question, SOURCE)
            output = validate_answer_document(engine.answer(question, selected["segments"]), selected["segments"])
            assert output["answerability"] == expected
            if expected == "answered":
                assert any("25" in paragraph["text"] and "synthetic-control" in paragraph["source_ids"]
                           for paragraph in output["paragraphs"])
            print(json.dumps({"case": name, "status": "passed", "scope": selected["scope"],
                              "paragraphs": len(output["paragraphs"]), "http_calls_total": len(calls),
                              "seconds": round(time.monotonic()-began, 3)}), flush=True)
    except Exception as error:
        print(json.dumps({"status": "failed", "code": getattr(error, "code", "validation_failed"),
                          "error_type": type(error).__name__, "http_calls_total": len(calls)}), flush=True)
        raise SystemExit(1) from None
    finally:
        engine.close()


if __name__ == "__main__":
    main()
