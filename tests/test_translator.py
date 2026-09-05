from __future__ import annotations

import copy
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from server.settings import Settings
from server.translator import LectureTranslation, MindlogicTranslator, TranslationError, validate_translation_segments


LOCKED = re.compile(r"__(?:PRIVATE|KOREAN)_[0-9]{6}__")


def source(text="Plants use light.", identifier="private-source-id", start=0):
    return {"id": identifier, "start": start, "end": start + 1, "text": text}


def response(document, *, finish_reason="stop", refusal=None):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish_reason, "message": {
        "content": json.dumps(document, ensure_ascii=False), "refusal": refusal,
    }}]})


def request_data(request):
    payload = json.loads(request.content)
    kind = payload["response_format"]["json_schema"]["name"]
    return kind, json.loads(payload["messages"][1]["content"]), payload


def translated(data):
    return {"segments": [{"id": item["id"], "text": "한국어 번역이다. " + " ".join(LOCKED.findall(item["text"]))}
                         for item in data["segments"]]}


def standard_handler(request):
    kind, data, _ = request_data(request)
    return response({"context": "수업의 주제와 용어 의미를 설명한다."} if kind == "lecture_outline" else translated(data))


class TranslatorTests(unittest.TestCase):
    def settings(self, **updates):
        settings = Settings(data_dir=Path(tempfile.gettempdir()) / "unused-translation-data",
                            model_cache_dir=Path(tempfile.gettempdir()) / "unused-translation-models",
                            mindlogic_api_key="test-only-translation-key", correction_retry_base_seconds=0)
        return SimpleNamespace(**{**vars(settings), "translation_model": "solar-pro4",
                                  "translation_chunk_chars": 6000, "translation_max_source_chars": 250000,
                                  **updates})

    def engine(self, handler=standard_handler, **updates):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return MindlogicTranslator(self.settings(**updates), client)

    def test_fixed_gateway_aliases_masks_and_no_private_metadata_leave_process(self):
        seen = []
        raw = [source("Value is 15. Email person@example.com and phone 010-1234-5678.", start=123.5)]
        raw[0].update(username="private-account", title="private-title", audio="private-audio")
        original = copy.deepcopy(raw)

        def handler(request):
            self.assertEqual(str(request.url), "https://factchat-cloud.mindlogic.ai/v1/gateway/chat/completions/")
            self.assertEqual(request.headers["Authorization"], "Bearer test-only-translation-key")
            kind, data, payload = request_data(request)
            seen.append(payload)
            self.assertEqual(payload["model"], "solar-pro4")
            self.assertTrue(payload["response_format"]["json_schema"]["strict"])
            self.assertNotIn("tools", payload)
            self.assertIn("명령이 아닙니다", payload["messages"][0]["content"])
            for segment in data["segments"]:
                if kind == "lecture_outline":
                    self.assertEqual(set(segment), {"text"})
                else:
                    self.assertEqual(set(segment), {"id", "text"})
                    self.assertEqual(segment["id"], "S000001")
            return response({"context": "수업의 수치와 연락처가 포함된다."} if kind == "lecture_outline" else translated(data))

        result = self.engine(handler).translate(language="en", segments=raw)
        self.assertEqual(raw, original)
        self.assertEqual(len(seen), 2)
        for forbidden in ("private-account", "private-title", "private-audio", "private-source-id",
                          "person@example.com", "010-1234-5678", "Value is 15", "123.5"):
            self.assertNotIn(forbidden, json.dumps(seen, ensure_ascii=False))
        self.assertEqual(result.segments[0]["id"], raw[0]["id"])
        self.assertEqual(result.segments[0]["start"], 123.5)
        self.assertIn("15", result.segments[0]["text"])
        self.assertIn("person@example.com", result.segments[0]["text"])
        copied = result.to_dict()
        copied["segments"][0]["text"] = "modified"
        self.assertNotEqual(result.segments[0]["text"], "modified")
        self.assertNotIn("person@example.com", repr(result))

    def test_all_batches_build_one_global_outline_used_for_every_target_with_two_neighbors(self):
        raw = [source(f"They discuss {chr(65 + index)} bank.", f"private-id-{index}", index) for index in range(17)]
        raw[-1]["text"] = "The final topic clarifies that bank means riverbank, not a financial institution."
        stage_calls, mapped_texts, contexts = [], [], []

        def handler(request):
            kind, data, _ = request_data(request)
            stage_calls.append(kind)
            if kind == "lecture_outline":
                if "segments" in data:
                    self.assertTrue(all(set(item) == {"text"} for item in data["segments"]))
                    mapped_texts.extend(item["text"] for item in data["segments"])
                    context = " ".join(item["text"] for item in data["segments"])
                else:
                    context = " ".join(data["outlines"])
                return response({"context": context})
            contexts.append(data["course_context"])
            self.assertEqual(mapped_texts, [item["text"] for item in raw])
            self.assertIn("riverbank", data["course_context"])
            target = data["segments"]
            self.assertEqual(len(target), 1)
            index = int(target[0]["id"][1:]) - 1
            self.assertEqual([item["id"] for item in data["segments"]], [f"S{index + 1:06d}"])
            self.assertEqual(data["readonly_context"], {
                "before": [item["text"] for item in raw[max(0, index - 2):index]],
                "unchanged_in_batch": [],
                "after": [item["text"] for item in raw[index + 1:index + 3]],
            })
            return response(translated(data))

        result = self.engine(handler, translation_chunk_chars=1).translate(language="en", segments=raw)
        self.assertEqual(len(stage_calls), 40)  # 17 maps + 6 combines + 17 translations.
        self.assertEqual(stage_calls[:23], ["lecture_outline"] * 23)
        self.assertEqual(len(set(contexts)), 1)
        self.assertEqual([item["id"] for item in result.segments], [item["id"] for item in raw])

    def test_existing_korean_rows_are_exact_and_mixed_korean_spans_are_locked(self):
        raw = [source("기존 한국어 문장입니다. 연락처 person@example.com, 값은 15입니다.", "korean"),
               source("이미 한국어입니다. Plants use light at 온도15도.", "mixed", 1)]
        calls = []

        def handler(request):
            kind, data, _ = request_data(request)
            calls.append(kind)
            if kind == "lecture_outline":
                self.assertIn("기존 한국어", data["segments"][0]["text"])
                return response({"context": "빛과 온도를 설명한다."})
            self.assertEqual([item["id"] for item in data["segments"]], ["S000002"])
            self.assertIn("기존 한국어", data["readonly_context"]["unchanged_in_batch"][0])
            self.assertIn("__KOREAN_", data["segments"][0]["text"])
            self.assertNotIn("이미 한국어", data["segments"][0]["text"])
            return response(translated(data))

        result = self.engine(handler).translate(language="ko", segments=raw)
        self.assertEqual(result.segments[0], raw[0])
        for span in ("이미 한국어입니다", "온도", "도"):
            self.assertIn(span, result.segments[1]["text"])
        self.assertEqual(len(calls), 2)
        untouched = self.engine(lambda request: self.fail("Korean-only source must not call the API"))
        self.assertEqual(untouched.translate(language="ko", segments=raw[:1]).segments, raw[:1])

    def test_translation_schema_and_prompt_admit_only_exact_target_ids_and_count(self):
        raw = [source(f"English row {letter}.", f"private-row-{letter}", index)
               for index, letter in enumerate("ABCDEF")]
        translation_calls = []

        def handler(request):
            kind, data, payload = request_data(request)
            if kind == "lecture_outline":
                return response({"context": "수업 전체의 흐름을 설명한다."})
            translation_calls.append(data)
            ids = [item["id"] for item in data["segments"]]
            self.assertEqual(ids, ["S000001", "S000002", "S000003", "S000004"]
                             if len(translation_calls) == 1 else ["S000005", "S000006"])
            self.assertTrue(all(set(item) == {"id", "text"} for item in data["segments"]))
            self.assertNotIn('"target"', json.dumps(data))
            readonly = data["readonly_context"]
            self.assertEqual(set(readonly), {"before", "unchanged_in_batch", "after"})
            self.assertTrue(all(isinstance(text, str) for group in readonly.values() for text in group))
            self.assertNotRegex(json.dumps(readonly), r"S[0-9]{6}|private-row-")
            schema = payload["response_format"]["json_schema"]["schema"]["properties"]["segments"]
            self.assertEqual(schema["minItems"], len(ids))
            self.assertEqual(schema["maxItems"], len(ids))
            self.assertEqual(schema["items"]["properties"]["id"]["enum"], ids)
            self.assertIn(json.dumps(ids, separators=(",", ":")), payload["messages"][0]["content"])
            self.assertIn("읽기 전용", payload["messages"][0]["content"])
            return response(translated(data))

        result = self.engine(handler, translation_chunk_chars=56).translate(language="en", segments=raw)
        self.assertEqual(len(translation_calls), 2)
        self.assertEqual(translation_calls[0]["readonly_context"]["after"], [row["text"] for row in raw[4:]])
        self.assertEqual(translation_calls[1]["readonly_context"]["before"], [row["text"] for row in raw[2:4]])
        self.assertEqual([row["id"] for row in result.segments], [row["id"] for row in raw])

    def test_real_extra_neighbor_rows_are_rejected_not_filtered_even_with_strict_schema(self):
        raw = [source(f"English row {letter}.", f"private-row-{letter}", index)
               for index, letter in enumerate("ABCDEF")]
        for change in ("append_neighbors", "replace_with_neighbor", "reorder", "duplicate"):
            with self.subTest(change=change):
                calls = []

                def handler(request):
                    kind, data, _ = request_data(request)
                    calls.append(kind)
                    if kind == "lecture_outline":
                        return response({"context": "수업의 문맥이다."})
                    rows = translated(data)["segments"]
                    self.assertEqual(len(rows), 4)
                    if change == "append_neighbors":
                        # Actual failure: the provider returned S000001..6
                        # when only S000001..4 required translation.
                        rows.extend({"id": identifier, "text": "문맥 행을 잘못 번역했다."}
                                    for identifier in ("S000005", "S000006"))
                    elif change == "replace_with_neighbor":
                        rows[-1]["id"] = "S000005"
                    elif change == "reorder":
                        rows[0], rows[1] = rows[1], rows[0]
                    else:
                        rows[-1] = rows[0].copy()
                    return response({"segments": rows})

                with self.assertRaises(TranslationError) as failure:
                    self.engine(handler, translation_chunk_chars=56).translate(language="en", segments=raw)
                self.assertEqual(failure.exception.code, "invalid_response")
                self.assertEqual(calls, ["lecture_outline"] * 3 + ["lecture_translation"])

    def test_readonly_korean_and_neighbor_contexts_keep_private_values_masked(self):
        raw = [source("기존 문장입니다. person@example.com 값은 15입니다.", "korean-before"),
               source("The bank has changed.", "english", 1),
               source("이미 보관한 설명입니다.", "korean-after", 2)]
        seen = []

        def handler(request):
            kind, data, _ = request_data(request)
            seen.append(data)
            if kind == "lecture_outline":
                return response({"context": "수업의 문맥이다."})
            self.assertEqual(data["segments"], [{"id": "S000002", "text": raw[1]["text"]}])
            self.assertEqual(len(data["readonly_context"]["unchanged_in_batch"]), 2)
            self.assertIn("__PRIVATE_", data["readonly_context"]["unchanged_in_batch"][0])
            self.assertNotRegex(json.dumps(data["readonly_context"]), r"S[0-9]{6}|korean-before|korean-after")
            return response(translated(data))

        result = self.engine(handler).translate(language="en", segments=raw)
        self.assertEqual(result.segments[0], raw[0])
        self.assertEqual(result.segments[2], raw[2])
        encoded = json.dumps(seen, ensure_ascii=False)
        self.assertNotIn("person@example.com", encoded)
        self.assertNotIn("15", encoded)

    def test_masked_values_stay_masked_through_outline_combination(self):
        requests = []
        raw = [source("The value is 15.", "one"), source("The next value is 20.", "two", 1)]

        def handler(request):
            kind, data, _ = request_data(request)
            requests.append(data)
            if kind == "lecture_outline":
                text = " ".join(item["text"] for item in data.get("segments", [])) or " ".join(data["outlines"])
                return response({"context": text})
            self.assertIn("__PRIVATE_000001__", data["course_context"])
            self.assertIn("__PRIVATE_000002__", data["course_context"])
            return response(translated(data))

        result = self.engine(handler, translation_chunk_chars=1).translate(language="en", segments=raw)
        self.assertEqual(len(requests), 5)
        self.assertIn("15", result.segments[0]["text"])
        self.assertIn("20", result.segments[1]["text"])
        self.assertNotIn("value is 15", json.dumps(requests))
        self.assertNotIn("value is 20", json.dumps(requests))

    def test_invalid_ids_counts_new_values_and_changed_tokens_are_rejected(self):
        raw = [source("Values are 15 and 20. Mail person@example.com. 기존 한국어.")]
        mutations = {
            "missing": lambda item: [],
            "duplicate": lambda item: [item, item],
            "wrong_id": lambda item: [{**item, "id": "outside"}],
            "extra_key": lambda item: [{**item, "start": 0}],
            "new_number": lambda item: [{**item, "text": item["text"] + " 99"}],
            "new_email": lambda item: [{**item, "text": item["text"] + " intruder@example.com"}],
            "missing_token": lambda item: [{**item, "text": item["text"].replace("__PRIVATE_000001__", "")}],
            "extra_token": lambda item: [{**item, "text": item["text"] + " __PRIVATE_000001__"}],
            "foreign_token": lambda item: [{**item, "text": item["text"].replace("__PRIVATE_000001__", "__PRIVATE_999999__")}],
            "malformed_token": lambda item: [{**item, "text": item["text"].replace("__PRIVATE_000001__", "__PRIVATE_0000001__")}],
            "missing_korean": lambda item: [{**item, "text": re.sub(r"__KOREAN_[0-9]{6}__", "바뀐 내용", item["text"])}],
            "blank": lambda item: [{**item, "text": " "}],
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                def handler(request):
                    kind, data, _ = request_data(request)
                    if kind == "lecture_outline":
                        return response({"context": "여러 값을 설명한다."})
                    item = translated(data)["segments"][0]
                    return response({"segments": mutate(item)})
                with self.assertRaises(TranslationError):
                    self.engine(handler).translate(language="en", segments=raw)

    def test_outline_invented_values_or_omitted_mask_guess_stop_before_translation(self):
        for invalid in ("The value is 99.", "Contact stranger@example.com.", "Value __PRIVATE_999999__.", "가" * 2401):
            with self.subTest(invalid=invalid[:20]):
                calls = []
                def handler(request):
                    calls.append(request_data(request)[0])
                    return response({"context": invalid})
                with self.assertRaises(TranslationError):
                    self.engine(handler).translate(language="en", segments=[source()])
                self.assertEqual(calls, ["lecture_outline"])
        calls = []
        def resurrect(request):
            kind, data, _ = request_data(request)
            calls.append(kind)
            return response({"context": "수업 문맥" if "segments" in data else "__PRIVATE_000001__"})
        with self.assertRaises(TranslationError):
            self.engine(resurrect, translation_chunk_chars=1).translate(
                language="en", segments=[source("Value 15.", "one"), source("Value 20.", "two")])
        self.assertEqual(calls, ["lecture_outline"] * 3)

    def test_outline_omits_alias_ids_but_still_rejects_ids_in_generated_prose(self):
        observed = []
        def handler(request):
            kind, data, _ = request_data(request)
            observed.append(kind)
            if kind == "lecture_outline":
                self.assertEqual(data["segments"], [{"text": "Plants use light."}])
                return response({"context": "빛의 역할을 설명한다."})
            self.assertEqual(data["segments"][0]["id"], "S000001")
            return response(translated(data))
        engine = self.engine(handler)
        engine.translate(language="en", segments=[source()])
        self.assertEqual(observed, ["lecture_outline", "lecture_translation"])
        # Real regression: the model cited S000005 etc. inside its outline.
        # Removing unnecessary input IDs must not permit those new numbers.
        with self.assertRaises(TranslationError) as failure:
            engine._outline({"context": "S000005는 통제 변수를 설명한다."},
                            {"segments": [{"text": "A control variable is held constant."}]})
        self.assertEqual(failure.exception.code, "protected_content_changed")

    def test_real_outline_parenthesized_enumeration_is_only_internal_formatting(self):
        # Captured from the public, self-written English validation fixture:
        # the provider added serial labels despite being told not to do so.
        document = (
            "연구 방법과 흐름을 설명하는 영문 문장들. "
            "(1) 통제 변수는 일정하게 유지해 결과 변화를 연구 요인과 연결한다. "
            "(2) 모델은 가정 안에서만 유효하며 조건이 바뀌면 예측이 불안정해질 수 있다. "
            "(3) 상관관계만으로는 인과를 단정할 수 없고 대안 설명을 비교해야 한다. "
            "(4) 수로 형태가 흐름 속도에 영향을 준다."
        )
        raw = [source("A control variable remains constant.", "one"),
               source("Models are valid within their assumptions.", "two", 1),
               source("Correlation alone does not show causation.", "three", 2),
               source("The channel shape affects flow speed.", "four", 3)]
        contexts, calls = [], []

        def handler(request):
            kind, data, _ = request_data(request)
            calls.append(kind)
            if kind == "lecture_outline":
                return response({"context": document})
            contexts.append(data["course_context"])
            return response(translated(data))

        result = self.engine(handler).translate(language="en", segments=raw)
        self.assertEqual(contexts, [re.sub(r"\([1-4]\)", "•", document)])
        self.assertEqual(calls, ["lecture_outline", "lecture_translation"])
        self.assertEqual([item["id"] for item in result.segments], [item["id"] for item in raw])
        self.assertNotRegex(contexts[0], r"[0-9]")
        # This exception is not available to final row/number validation.
        with self.assertRaises(TranslationError) as failure:
            validate_translation_segments([{**raw[0], "text": document}], raw[:1])
        self.assertEqual(failure.exception.code, "protected_content_changed")

    def test_outline_list_normalization_keeps_tokens_and_never_renumbers_source_values(self):
        engine = self.engine()
        tokens = ["__PRIVATE_000001__", "__PRIVATE_000002__"]
        document = f"(1) 첫 값은 {tokens[0]}이다.\n  (2) 다음 연락처는 {tokens[1]}이다."
        data = {"segments": [{"text": "Values " + " and ".join(tokens)}]}
        normalized = engine._outline({"context": document}, data)
        self.assertEqual(LOCKED.findall(normalized), tokens)
        self.assertEqual(normalized, document.replace("(1)", "•").replace("(2)", "•"))
        # Combine stages use the identical bounded formatting rule and still
        # retain exactly the token text that their source outlines supplied.
        self.assertEqual(engine._outline({"context": document}, {"outlines": [" ".join(tokens)]}), normalized)
        numeric = "(1) 첫 값이다. (2) 다음 값이다."
        self.assertEqual(engine._outline({"context": numeric},
                                        {"segments": [{"text": "Values are 1 and 2."}]}), numeric)
        self.assertEqual(engine._outline({"context": "값은 (25) 이다."},
                                        {"segments": [{"text": "The value is 25."}]}), "값은 (25) 이다.")

    def test_outline_only_complete_bounded_boundary_labels_are_normalized(self):
        invalid = [
            "(25) 단일 수치다.",
            "(1) 첫 항목이다.",
            "(1) 첫 항목이다. (3) 비연속 항목이다.",
            "(2) 역순 항목이다. (1) 다음 항목이다.",
            "조건 (1) 본문 속 수치다. (2) 다음 항목이다.",
            "(1) 항목 중 (2) 본문 속 수치가 있다.",
            "(1) 첫 항목이다.(2) 경계 공백이 없다.",
            "(1)첫 항목이다. (2) 다음 항목이다.",
            "(01) 첫 항목이다. (2) 다음 항목이다.",
            "(1) 첫 항목이다. (2) 수치는 99다.",
            "(1) 첫 항목이다. (2) 연락처는 stranger@example.com이다.",
            "(1) 첫 항목이다. (2) 값은 __PRIVATE_999999__이다.",
            "(1) 첫 항목이다. (2) 값은 __KOREAN_999999__이다.",
            " ".join(f"({index}) 항목이다." for index in range(1, 34)),
        ]
        for document in invalid:
            with self.subTest(document=document[:40]):
                with self.assertRaises(TranslationError) as failure:
                    self.engine()._outline({"context": document}, {"segments": [{"text": "Source words."}]})
                self.assertEqual(failure.exception.code, "protected_content_changed")

    def test_pure_validator_checks_timing_order_numbers_privacy_korean_and_fresh_copy(self):
        raw = [source("Value 15 and 20, email person@example.com.", "one"), source("그대로 보관합니다.", "two", 1)]
        good = [{**raw[0], "text": "값은 15와 20이며 이메일은 person@example.com이다."}, raw[1]]
        self.assertEqual(validate_translation_segments(good, raw), good)
        checked = validate_translation_segments(good, raw)
        checked[0]["text"] = "mutated"
        self.assertNotEqual(good[0]["text"], "mutated")
        variants = [list(reversed(good)), good[:1], [{**good[0], "start": 0.1}, good[1]],
                    [{**good[0], "end": float("nan")}, good[1]], [{**good[0], "start": False}, good[1]],
                    [{**good[0], "text": "값은 20과 15이며 이메일은 person@example.com이다."}, good[1]],
                    [{**good[0], "text": "값은 15와 20이다."}, good[1]],
                    [good[0], {**good[1], "text": "한국어를 고쳤습니다."}]]
        for bad in variants:
            with self.assertRaises(TranslationError):
                validate_translation_segments(bad, raw)

    def test_cancel_between_outline_maps_and_translation_batches_returns_no_partial(self):
        for stop_at in (1, 4):
            with self.subTest(stop_at=stop_at):
                interrupted = threading.Event()
                calls = []
                def handler(request):
                    calls.append(request_data(request)[0])
                    result = standard_handler(request)
                    if len(calls) == stop_at:
                        interrupted.set()
                    return result
                with self.assertRaises(TranslationError) as failure:
                    self.engine(handler, translation_chunk_chars=1).translate(
                        language="en", segments=[source(identifier="one"), source(identifier="two")],
                        interrupted=interrupted.is_set)
                self.assertEqual(failure.exception.code, "interrupted")
                self.assertEqual(len(calls), stop_at)

    def test_response_schema_truncation_refusal_redirect_and_duplicate_json_fail_closed(self):
        malformed = [response({"context": "ok"}, finish_reason="length"),
                     response({"context": "ok"}, refusal="provider-private-message"),
                     httpx.Response(302, headers={"Location": "https://outside.invalid"}),
                     httpx.Response(200, json={"choices": [{"message": {"content": '{"context":"first","context":"second"}'}}]}),
                     httpx.Response(200, json={"choices": [{"message": {"content": '{"context":NaN}'}}]}),
                     httpx.Response(200, json={"choices": []}),
                     httpx.Response(200, content=b"x" * 4097)]
        for bad in malformed:
            with self.subTest(status=bad.status_code):
                with self.assertRaises(TranslationError) as failure:
                    self.engine(lambda request: bad, correction_max_response_bytes=4096).translate(language="en", segments=[source()])
                self.assertNotIn("provider-private", str(failure.exception))

    def test_transient_retry_but_credit_and_auth_errors_never_retry_or_echo_provider(self):
        for status in (429, 503, "network", 401, 402, 403):
            with self.subTest(status=status):
                calls = []
                def handler(request):
                    calls.append(request_data(request)[0])
                    if len(calls) == 1:
                        if status == "network":
                            raise httpx.ConnectError("provider-private-message", request=request)
                        return httpx.Response(status, text="provider-private-key-and-body")
                    return standard_handler(request)
                engine = self.engine(handler, correction_max_retries=1)
                if status in (401, 402, 403):
                    with self.assertRaises(TranslationError) as failure:
                        engine.translate(language="en", segments=[source()])
                    self.assertNotIn("provider-private", str(failure.exception))
                    self.assertEqual(len(calls), 1)
                else:
                    self.assertEqual(len(engine.translate(language="en", segments=[source()]).segments), 1)
                    self.assertEqual(len(calls), 3)

    def test_source_and_mask_expansion_limits_fail_before_any_request(self):
        invalid = [[], [source(" ")], [source("A" * 24001)], [source(), source()],
                   [source(start=float("inf"))], [source(start=-1)],
                   [source("English", f"source-{index}") for index in range(65)]]
        for raw in invalid:
            with self.assertRaises(TranslationError):
                self.engine(lambda request: self.fail("No call should start"), translation_chunk_chars=1).translate(
                    language="en", segments=raw)
        raw = [source("A " + "1 " * 11900, f"source-{index}") for index in range(5)]
        with self.assertRaises(TranslationError) as failure:
            self.engine(lambda request: self.fail("Oversized masked context must be rejected first")).translate(
                language="en", segments=raw)
        self.assertEqual(failure.exception.code, "source_too_large")

    def test_output_segment_and_total_limits(self):
        raw = [source()]
        with self.assertRaises(TranslationError):
            validate_translation_segments([{**raw[0], "text": "가" * 48001}], raw)
        raw = [source(identifier=f"item-{index}") for index in range(21)]
        with self.assertRaises(TranslationError):
            validate_translation_segments([{**item, "text": "가" * 48000} for item in raw], raw)
        def handler(request):
            kind, data, _ = request_data(request)
            return response({"context": "수업 문맥"} if kind == "lecture_outline" else {
                "segments": [{"id": data["segments"][0]["id"], "text": "가" * 48001}]})
        with self.assertRaises(TranslationError):
            self.engine(handler).translate(language="en", segments=[source()])

    def test_literal_protection_tokens_restore_in_one_pass_and_calls_do_not_share_state(self):
        engine = self.engine()
        first = source("Literal __PRIVATE_000002__ and __KOREAN_000003__ with value 15.")
        result = engine.translate(language="en", segments=[first])
        self.assertIn("__PRIVATE_000002__", result.segments[0]["text"])
        self.assertIn("__KOREAN_000003__", result.segments[0]["text"])
        second = engine.translate(language="en", segments=[source("Other value 20.", "other-source")])
        self.assertIn("20", second.segments[0]["text"])
        self.assertNotIn("15", second.segments[0]["text"])
        self.assertNotIn("__PRIVATE_", second.segments[0]["text"])

    def test_configuration_is_pinned_and_no_extra_key_is_required(self):
        with self.assertRaises(TranslationError) as failure:
            self.engine(mindlogic_api_key=None).translate(language="en", segments=[source()])
        self.assertEqual(failure.exception.code, "not_configured")
        for values in ({"mindlogic_base_url": "http://outside.invalid"}, {"translation_model": "bad model"},
                       {"translation_chunk_chars": 0}, {"translation_max_source_chars": 250001}):
            with self.assertRaises(ValueError):
                self.engine(**values)
        client = httpx.Client(transport=httpx.MockTransport(standard_handler), follow_redirects=True)
        self.addCleanup(client.close)
        with self.assertRaises(ValueError):
            MindlogicTranslator(self.settings(), client)
        self.assertTrue(self.engine().configured)
        self.assertEqual(LectureTranslation([source()]).to_dict()["segments"][0]["id"], "private-source-id")


if __name__ == "__main__":
    unittest.main()
