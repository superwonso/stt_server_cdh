"""Public synthetic classroom fixtures and deterministic, local evaluation.

No environment, credentials, files, network, model, or judge calls are used.
The caller runs each case through the real feature pipeline, then passes its
normalized output here: {"segments": [...]} for correction/translation, or the
summary/Q&A document. Production validators are reused where publicly exposed.

Hard checks establish structure/provenance, not semantic quality. Signals are
deliberately non-scoring hints: Korean paraphrases require the human rubric.
Six short fixtures cannot establish long-lecture, subject-wide or ASR quality.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any


DATASET_VERSION = 1
RUBRIC = {
    "priority": [
        "형식·출처·구간·숫자 검사에 실패한 결과는 품질 비교에서 먼저 제외한다.",
        "원문의 부정, 조건, 원인과 상관의 구별을 보존하는지 사람이 확인한다.",
        "뒤쪽 문맥의 반영과 근거가 없는 질문의 답변 유보를 확인한다.",
        "위 항목을 통과한 모델끼리 한국어 가독성, 지연, 실제 호출 수와 비용을 비교한다.",
    ],
    "semantic_ratings": {
        "pass": "필수 의미를 보존하며 원문에 없는 사실을 덧붙이지 않는다.",
        "partial": "틀린 사실은 없으나 질문에 필요한 설명이나 핵심 조건을 일부 빠뜨린다.",
        "fail": "부정·조건을 뒤집거나, 문맥을 오역하거나, 근거 없는 사실을 답한다.",
    },
    "limitations": [
        "단어 검출 신호만으로 의미 정확도를 판정하거나 합산 점수를 만들지 않는다.",
        "한 번의 성공이나 짧은 합성 예문의 결과로 모든 전공·긴 수업의 품질을 보장하지 않는다.",
        "원문에 없는 수치를 추론·계산해 보충하거나 평가용 AI 심판을 추가하지 않는다.",
        "모든 후보에 같은 입력과 파이프라인 설정을 쓰고 형식 실패·호출 실패도 결과에 남긴다.",
    ],
}


def _segments(prefix: str, texts: list[str]) -> list[dict[str, Any]]:
    return [{"id": f"eval-{prefix}-{index}", "start": index * 10.0,
             "end": (index + 1) * 10.0, "text": text}
            for index, text in enumerate(texts)]


_KOREAN_RAW = _segments("korean", [
    "오늘은 통제 변수를 살펴 봅니다. 온도는 25도로 일정하게 유지 했어요.",
    "빛의 세기만 바꿨지, 온도를 바꾼건 아닙니다.",
    "빛을 더 세게 했을 때 기포 수가 늘었다고 해도, 그 것만으로 원인이 증명 되는 건 아니에요.",
    "센서가 같은 위치에 있고 물의 양도 같을 때만 두 결과를 비교 할 수 있어요.",
    "다음 시간에는 같은 방법으로 다시 측정할 수 있지만, 과제로 제출 하라는 뜻은 아닙니다.",
])
_KOREAN_CLEAR = _segments("lesson", [
    "실험에서는 빛의 세기만 바꾸고 온도를 25도로 일정하게 유지했습니다.",
    "기포 수가 늘었다는 관찰만으로 빛이 원인이라고 단정할 수는 없습니다.",
    "센서가 같은 위치에 있고 물의 양도 같을 때만 두 결과를 비교할 수 있습니다.",
    "다음 시간에 같은 방법으로 다시 측정할 수 있지만 과제로 제출하라는 뜻은 아닙니다.",
])
_RIVER_BANK = _segments("bank", [
    "The bank became unstable after the storm, and the upper part moved toward the channel.",
    "Heavy rain filled the pores between the soil particles and changed the forces inside the ground.",
    "Water removed material near the base of the slope, leaving less support for the soil above it.",
    "A wall alone does not solve this problem if water is trapped behind it and cannot drain away.",
    "We are discussing erosion along a river, including the shape of its channel and the movement of sediment.",
    "In the opening sentence, bank means the side of a river, not a business that lends money.",
])
_ENGLISH_CONDITIONS = _segments("conditions", [
    "Keep the temperature at 25 degrees and record the reading after 15 minutes.",
    "If the sensor is not calibrated, do not compare the two trials.",
    "A larger reading does not prove that light caused the change; the water level must remain the same.",
])


CASES = (
    {
        "id": "ko_correction_constraints", "feature": "correction",
        "inputs": {"title": "", "language": "ko", "segments": _KOREAN_RAW},
        "rubric": {
            "must_preserve": [
                "띄어쓰기·문장부호를 다듬되 모든 원문 구간과 발언 순서를 유지한다.",
                "온도는 바꾼 것이 아니라 25도로 유지했고, 빛의 세기만 바꿨다는 뜻을 보존한다.",
                "기포 증가만으로 인과관계가 증명되지 않으며 센서 위치와 물의 양이 같은 경우에만 비교한다.",
                "다시 측정할 수 있다는 가능성을 확정 일정이나 제출 과제로 바꾸지 않는다.",
            ],
            "acceptable": ["구어체를 유지하거나 자연스럽게 다듬는 두 방식 모두 허용한다.", "애매한 표현을 억지로 새 전문용어로 바꾸지 않는 보수적 정정도 허용한다."],
            "disallowed": ["문장 병합·누락", "25 변경", "온도를 바꿨다는 뜻으로 반전", "과제 제출 지시 추가"],
        },
        "expected": {"signals": [
            {"name": "spacing_cleanup", "segment_id": "eval-korean-0", "any_of": ["살펴봅니다", "유지했"]},
            {"name": "temperature_not_changed", "segment_id": "eval-korean-1", "any_of": ["아니", "아닙", "않"]},
            {"name": "comparison_condition", "segment_id": "eval-korean-3", "any_of": ["때만", "경우에만", "조건"]},
            {"name": "not_an_assignment", "segment_id": "eval-korean-4", "any_of": ["아니", "아닙", "않"]},
        ]},
    },
    {
        "id": "ko_summary_conditions_not_homework", "feature": "summary",
        "inputs": {"language": "ko", "segments": _KOREAN_CLEAR},
        "rubric": {
            "must_preserve": [
                "변경한 변수는 빛의 세기이고 온도는 25도로 유지했다는 실험 구분을 요약한다.",
                "관찰만으로 인과를 단정할 수 없다는 경고와 비교에 필요한 센서 위치·물의 양 조건을 보존한다.",
                "출처가 실제 요약 문장을 뒷받침해야 한다. 원문에 있는 단어만 인용했다고 맞는 요약이 되는 것은 아니다.",
                "재측정 가능성을 과제·제출 기한·확정 공지로 만들지 않는다.",
            ],
            "acceptable": ["전체 의미가 정확하다면 제목·항목 수·문장 표현은 달라도 된다.", "제출 과제가 아니라는 주변 발언은 생략할 수 있으나 과제라는 반대 사실을 만들면 안 된다."],
            "disallowed": ["새 숫자·마감일", "기포 증가가 인과를 증명한다는 주장", "비교 조건 생략으로 무조건 비교 가능하다는 주장"],
        },
        "expected": {"signals": [
            {"name": "temperature_value", "any_of": [r"(?<!\d)25(?!\d)"]},
            {"name": "causal_caution", "any_of": ["단정.*(?:않|없)", "인과.*(?:않|없|주의)", "증명.*(?:않|없)"]},
            {"name": "comparison_conditions", "any_of": ["센서", "물의?\\s*양"]},
        ]},
    },
    {
        "id": "en_translation_late_bank_context", "feature": "translation",
        "inputs": {"language": "en", "segments": _RIVER_BANK,
                   "settings_hint": {"translation_chunk_chars": 300}},
        "rubric": {
            "must_preserve": [
                "첫 구간의 bank를 뒤쪽 설명과 연결해 강둑·강기슭 등 하천 지형으로 번역한다.",
                "아래쪽 토양 유실로 위쪽 지지가 약해졌다는 관계를 보존한다.",
                "물이 빠지지 않는 조건에서는 벽만으로 문제를 해결할 수 없다는 조건과 부정을 보존한다.",
                "뒤쪽 구간의 금융기관이 아니라는 대비도 원래 구간에 남긴다.",
            ],
            "acceptable": ["강둑·강기슭·하안 등 해당 문맥에 맞는 자연스러운 번역을 허용한다.", "문장 표현은 달라도 각 구간의 ID·시각·순서는 그대로다."],
            "disallowed": ["첫 구간을 은행의 경영 불안정으로 번역", "지지 약화의 인과 방향 반전", "배수 조건을 제거하고 벽이 항상 해결한다고 번역"],
        },
        "expected": {"signals": [
            {"name": "river_bank_in_first_segment", "segment_id": "eval-bank-0", "any_of": ["강둑", "강기슭", "제방", "하안", "둑"]},
            {"name": "bank_finance_word_review", "segment_id": "eval-bank-0", "any_of": ["은행", "금융"]},
            {"name": "drainage_condition", "segment_id": "eval-bank-3", "any_of": ["배수", "빠지", "빠져", "흘러"]},
        ]},
    },
    {
        "id": "en_translation_numeric_negation", "feature": "translation",
        "inputs": {"language": "en", "segments": _ENGLISH_CONDITIONS},
        "rubric": {
            "must_preserve": [
                "온도 25도와 측정 시각 15분 뒤를 뒤바꾸거나 새 값으로 바꾸지 않는다.",
                "센서가 보정되지 않았으면 두 실험을 비교하지 말라는 조건부 금지를 유지한다.",
                "더 큰 측정값만으로 빛이 원인이라는 결론은 증명되지 않으며 수위가 같아야 한다는 뜻을 유지한다.",
            ],
            "acceptable": ["보정·교정 등 원래 뜻에 맞는 번역을 허용한다.", "원문 단위인 degrees를 '도'로 옮기며 원문에 없는 섭씨·화씨를 단정하지 않는다."],
            "disallowed": ["25와 15의 위치 교환", "보정하지 않아도 비교하라는 뜻으로 반전", "인과 증명으로 단정", "수위 조건 누락"],
        },
        "expected": {"signals": [
            {"name": "calibration_term", "segment_id": "eval-conditions-1", "any_of": ["보정", "교정"]},
            {"name": "comparison_prohibition", "segment_id": "eval-conditions-1", "any_of": ["비교.*(?:말|마세|않|안|금지|없)", "비교해서는", "비교하면\\s*안"]},
            {"name": "water_level_condition", "segment_id": "eval-conditions-2", "any_of": ["수위", "물의?\\s*높이"]},
        ]},
    },
    {
        "id": "qa_grounded_temperature_and_conditions", "feature": "question_answering",
        "inputs": {"question": "실험에서 온도를 어떻게 유지했고, 두 결과를 비교하려면 어떤 조건이 같아야 하나요?",
                   "segments": _KOREAN_CLEAR},
        "rubric": {
            "must_preserve": [
                "온도는 25도로 유지했다고 답하고 해당 온도 발언을 출처로 연결한다.",
                "센서 위치와 물의 양이 같아야 두 결과를 비교할 수 있다고 답하며 그 조건 발언을 인용한다.",
                "질문에 없고 원문에도 없는 실험 결과·참가자·확정 과제를 덧붙이지 않는다.",
            ],
            "acceptable": ["문단 수와 서술 순서는 달라도 된다.", "추가 설명은 원문에서 확인할 수 있는 범위만 허용한다."],
            "disallowed": ["온도를 변화시켰다는 주장", "비교 조건 없는 단정", "질문 자체를 사실의 근거로 사용"],
        },
        "expected": {"answerability": "answered", "signals": [
            {"name": "temperature_value", "any_of": [r"(?<!\d)25(?!\d)"]},
            {"name": "sensor_condition", "any_of": ["센서.*위치", "위치.*센서"]},
            {"name": "water_amount_condition", "any_of": ["물의?\\s*양"]},
        ]},
    },
    {
        "id": "qa_related_but_missing_student_count", "feature": "question_answering",
        "inputs": {"question": "온도를 통제한 실험에 참가한 학생은 몇 명인가요?", "segments": _KOREAN_CLEAR},
        "rubric": {
            "must_preserve": [
                "질문과 어휘가 관련된 원문을 실제 모델이 읽되, 원문에 학생 수가 없으므로 답변을 유보한다.",
                "온도 25도를 학생 25명으로 바꾸거나 상식으로 인원수를 채우지 않는다.",
                "insufficient_evidence와 빈 paragraphs로 응답해 근거가 부족하다는 안내를 표시하게 한다.",
            ],
            "acceptable": ["이 사례는 검색 실패에 따른 모델 무호출이 아니라 모델의 근거 부족 판단을 평가한다."],
            "disallowed": ["학생 수 추측", "25라는 숫자를 인원으로 재사용", "출처만 붙인 근거 없는 answered 응답"],
        },
        "expected": {"answerability": "insufficient_evidence", "requires_nonempty_evidence": True, "signals": []},
    },
)


def get_cases() -> list[dict[str, Any]]:
    """Return independent JSON-serializable inputs; callers may set hints."""
    return [copy.deepcopy(case) for case in CASES]


def _case(case_id: str) -> dict[str, Any]:
    for case in CASES:
        if case["id"] == case_id:
            return copy.deepcopy(case)
    raise ValueError("unknown evaluation case")


def _output_texts(feature: str, output: Any) -> tuple[str, dict[str, str]]:
    # Only use bounded, validated outputs. Errors are represented in checks,
    # never by returning arbitrary provider diagnostics or response bodies.
    if feature in ("correction", "translation"):
        by_id = {row["id"]: row["text"] for row in output["segments"]}
        return "\n".join(by_id.values()), by_id
    if feature == "summary":
        texts = [output["overview"]]
        for section in output["sections"]:
            texts.extend([section["heading"], *(row["text"] for row in section["bullets"])])
        texts.extend(row["question"] for row in output["review_questions"])
        return "\n".join(texts), {}
    return "\n".join(row["text"] for row in output["paragraphs"]), {}


def evaluate_case(case_id: str, output: Any) -> dict[str, Any]:
    """Report hard invariants separately from non-scoring semantic hints.

    ``hard_pass`` is not an overall quality grade. Human review must still use
    the returned rubric. This function cannot establish semantic entailment.
    """
    from server.postprocessor import _PROTECTED_VALUE
    from server.question_answerer import select_evidence, validate_answer_document
    from server.summarizer import validate_summary_document
    from server.translator import validate_translation_segments

    case = _case(case_id)
    feature, source = case["feature"], case["inputs"]["segments"]
    checks, checked = [], None

    def check(name: str, passed: bool) -> None:
        checks.append({"name": name, "passed": bool(passed)})

    try:
        if feature == "correction":
            rows = output["segments"]
            valid = (isinstance(rows, list) and len(rows) == len(source)
                     and all(isinstance(row, dict) and set(row) == {"id", "start", "end", "text"}
                             and row["id"] == original["id"]
                             and all(type(row[key]) in (int, float) and math.isfinite(row[key])
                                     and row[key] == original[key] for key in ("start", "end"))
                             and isinstance(row["text"], str) and bool(row["text"].strip())
                             and len(row["text"]) <= 24_000
                             for row, original in zip(rows, source)))
            check("segment_identity_time_order", valid)
            if valid:
                preserved = all(_PROTECTED_VALUE.findall(row["text"]) == _PROTECTED_VALUE.findall(original["text"])
                                for row, original in zip(rows, source))
                check("per_segment_protected_values", preserved)
                if preserved:
                    checked = {"segments": copy.deepcopy(rows)}
        elif feature == "translation":
            checked = {"segments": validate_translation_segments(output["segments"], source)}
            check("production_translation_validator", True)
        elif feature == "summary":
            checked = validate_summary_document(output, source)
            check("production_summary_validator", True)
        else:
            selection = select_evidence(case["inputs"]["question"], source)
            if case["expected"].get("requires_nonempty_evidence"):
                check("nonempty_evidence_for_model_abstention", bool(selection["segments"]))
            checked = validate_answer_document(output, selection["segments"])
            check("production_answer_validator", True)
            check("expected_answerability", checked["answerability"] == case["expected"]["answerability"])
    except Exception:
        # Unknown schema/type/validation failures are equivalent for comparing
        # this fixed synthetic corpus. No candidate text enters diagnostics.
        check("valid_normalized_output", False)

    text, by_id = _output_texts(feature, checked) if checked is not None else ("", {})
    signals = []
    for signal in case["expected"].get("signals", []):
        target = by_id.get(signal["segment_id"], "") if "segment_id" in signal else text
        signals.append({"name": signal["name"],
                        "detected": any(re.search(pattern, target, re.IGNORECASE | re.DOTALL)
                                        for pattern in signal["any_of"])})
    return {"case_id": case["id"], "feature": feature, "dataset_version": DATASET_VERSION,
            "hard_pass": bool(checks) and all(item["passed"] for item in checks),
            "checks": checks, "signals": signals,
            "review_required": copy.deepcopy(case["rubric"])}
