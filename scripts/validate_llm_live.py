#!/usr/bin/env python3
"""Opt-in smoke test of real NOVA correction, summary, and translation.

Uses only the non-personal teaching examples below, no production DB/audio.
--live sends bounded requests using the existing server key and uses credits.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server.settings import Settings
from server.postprocessor import MindlogicPostprocessor, PostprocessingError
from server.summarizer import MindlogicSummarizer, validate_summary_document
from server.translator import MindlogicTranslator, validate_translation_segments


KOREAN = [
    "오늘은 광합성의 기본 원리를 공부합니다. 식물은 빛 에너지를 화학 에너지로 바꿉니다.",
    "광합성의 명반응은 빛을 필요로 하고 물을 분해해 산소를 방출합니다.",
    "이산화탄소를 고정하는 과정은 명반응이 만든 에너지를 사용합니다. 두 과정은 서로 연결되어 있습니다.",
    "실험에서는 빛의 세기를 바꾸고 온도는 25도로 유지해 다른 조건을 통제했습니다.",
    "자료의 단위와 조건을 함께 확인해야 결과를 올바르게 비교할 수 있습니다.",
]
ENGLISH = [
    "The bank was unstable after the storm. In this lesson we will explain why it failed.",
    "Heavy rain can make the ground saturated. The extra water changes the balance of forces inside the soil.",
    "We distinguish the amount of water entering the soil from the amount that leaves it. Their difference affects storage.",
    "The first measurement was taken after 15 minutes. All measurements used the same instrument and the same units.",
    "A control variable is kept constant so that a change in the result can be connected to the factor under study.",
    "A model is useful only within its assumptions. If the conditions change, its predictions may no longer be reliable.",
    "Correlation alone does not show that one change caused another. We must compare alternative explanations.",
    "The shape of the channel affects the speed of the flow. Faster moving water can remove soil from its sides.",
    "The material was eroded from below. With less support at the bottom, the upper part became unstable.",
    "Here, bank means the side of a river, not a financial institution. Remember this meaning when reading the opening sentence.",
]


def segments(texts):
    return [{"id":f"validation-source-{i}","start":i*10.0,"end":(i+1)*10.0,"text":text}
            for i,text in enumerate(texts)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--feature", choices=("all","correction","summary","translation"), default="all")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this test uses NOVA credits")
    settings = replace(Settings.from_env(), translation_chunk_chars=500,
                       correction_max_retries=0, correction_read_timeout_seconds=90)
    reports, korean, english = [], segments(KOREAN), segments(ENGLISH)
    operations = (
        ("correction", MindlogicPostprocessor, lambda e:e.correct(title="",language="ko",segments=korean)),
        ("summary", MindlogicSummarizer, lambda e:e.summarize(language="ko",segments=korean)),
        ("translation", MindlogicTranslator, lambda e:e.translate(language="en",segments=english)),
    )
    for name, factory, run in operations:
        if args.feature not in ("all",name):
            continue
        engine = factory(settings)
        calls = []
        transport = getattr(engine,"_transport",engine)
        original_request = transport._request
        def counted_request(*arguments, **keywords):
            calls.append(1)
            return original_request(*arguments, **keywords)
        transport._request = counted_request
        began = time.monotonic()
        try:
            output = run(engine)
            report = {"feature":name,"status":"passed","seconds":round(time.monotonic()-began,3),
                      "model_calls":len(calls)}
            if name == "summary":
                document = validate_summary_document(output.to_dict(), korean)
                report.update(sections=len(document["sections"]),review_questions=len(document["review_questions"]))
            elif name == "translation":
                translated = validate_translation_segments(output.segments, english)
                assert len(translated)==len(english)
                assert "은행" not in translated[0]["text"]
                assert any(word in translated[0]["text"] for word in ("강둑","둑","제방","강기슭"))
                assert "15" in translated[3]["text"]
                report.update(segments=len(translated),river_bank_context=True,numeric_preservation=True)
            reports.append(report)
            print(json.dumps(report),flush=True)
        except Exception as error:
            print(json.dumps({"feature":name,"status":"failed","seconds":round(time.monotonic()-began,3),
                              "model_calls":len(calls),"code":error.code if isinstance(error,PostprocessingError)
                              else "validation_failed","error_type":type(error).__name__}),flush=True)
            raise SystemExit(1) from None
        finally:
            engine.close()


if __name__ == "__main__":
    main()
