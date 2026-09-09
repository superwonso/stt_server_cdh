from __future__ import annotations

import json
import copy
import tempfile
import unittest
from pathlib import Path

import httpx

from server.postprocessor import MindlogicPostprocessor, PostprocessingError, _EMAIL
from server.settings import Settings


class MindlogicPostprocessorTests(unittest.TestCase):
    def settings(self, **changes):
        values = {
            "data_dir": Path(tempfile.gettempdir()) / "unused-stt-test-data",
            "model_cache_dir": Path(tempfile.gettempdir()) / "unused-stt-test-models",
            "mindlogic_api_key": "test-only-nova-key",
            "correction_retry_base_seconds": 0,
        }
        values.update(changes)
        return Settings(**values)

    @staticmethod
    def source_segments():
        return [
            {"id": "s1", "start": 0.0, "end": 1.0, "text": "첫 번재 문장 15개입니다."},
            {
                "id": "s2",
                "start": 1.0,
                "end": 2.0,
                "text": "연락처는 010-1234-5678이고 test@example.com입니다.",
            },
            {"id": "s3", "start": 2.0, "end": 3.0, "text": "마지막 문장입니다."},
        ]

    def test_email_boundary_keeps_punctuation_and_complete_domains_distinct(self):
        for address in ("fake.user@example.com", "fake-user+lab@dept.example.co.kr", "fake_user@school-domain.edu"):
            for suffix in ("", ".", ". Next", ",", ";", ":", "!", "?", ")", "]", "…", "입니다.", "으로 보냅니다."):
                with self.subTest(address=address, suffix=suffix):
                    self.assertEqual(_EMAIL.findall(f"연락: {address}{suffix}"), [address])
        self.assertEqual(_EMAIL.findall("fake.user@example.com.continuation."), ["fake.user@example.com.continuation"])
        for not_email in ("v1.2.3.", "3.14.", "a.b + c.d", "user @ example.com", "name@localhost",
                          "__PRIVATE_000001__", "name@example.com_unsupported", "name@example.com.123",
                          "name@example.com.bad-suffix"):
            with self.subTest(not_email=not_email):
                self.assertEqual(_EMAIL.findall(not_email), [])

    def test_sentence_final_email_and_literal_tokens_are_masked_once_and_round_trip(self):
        texts = [
            "Contact fake.user@example.com.",
            "Contact fake-user+lab@dept.example.co.kr, please.",
            "연락은 fake_user@school-domain.edu입니다.",
            "표식 __PRIVATE_000001__과 fake.user@example.com. 수치 3.14와 버전 v1.2.3은 보존한다.",
        ]
        source = [{"id": f"test-segment-{index}", "start": index, "end": index + 1, "text": text}
                  for index, text in enumerate(texts)]
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            data = json.loads(body["messages"][1]["content"])
            targets = [{"id": row["id"], "text": row["text"]} for row in data["segments"]]
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
                {"segments": targets, "uncertain_terms": []}, ensure_ascii=False)}}]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            processor = MindlogicPostprocessor(self.settings(correction_chunk_chars=60, correction_overlap_segments=1), client)
            result = processor.correct(title="test-only-title", language="ko", segments=source)
        self.assertEqual([row["text"] for row in result.segments], texts)
        self.assertEqual(source[3]["text"], texts[3])
        transmitted = json.dumps(requests, ensure_ascii=False)
        for address in ("fake.user@example.com", "fake-user+lab@dept.example.co.kr", "fake_user@school-domain.edu"):
            self.assertNotIn(address, transmitted)
        self.assertNotIn("3.14", transmitted)
        self.assertNotIn("v1.2.3", transmitted)

    def test_sentence_final_email_cannot_be_replaced_with_another_contact(self):
        def handler(request):
            data = json.loads(json.loads(request.content)["messages"][1]["content"])
            target = data["segments"][0]
            result = {"segments": [{"id": target["id"],
                       "text": target["text"].replace("__PRIVATE_000001__", "other@example.org")}],
                      "uncertain_terms": []}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            processor = MindlogicPostprocessor(self.settings(), client)
            with self.assertRaises(PostprocessingError) as raised:
                processor.correct(title="", language="en", segments=[{
                    "id": "test-segment", "start": 0, "end": 1, "text": "Contact fake.user@example.com."}])
        self.assertEqual(raised.exception.code, "privacy_placeholder_changed")

    def test_strict_json_chunks_use_overlap_without_duplicate_output_and_restore_masked_values(self):
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/gateway/chat/completions/")
            self.assertFalse(request.url.params)
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(body["model"], "gpt-5.6-luna")
            self.assertEqual(body["response_format"]["type"], "json_schema")
            user_data = json.loads(body["messages"][1]["content"])
            self.assertNotIn("lecture_title", user_data)
            targets = user_data["segments"]
            result = {
                "segments": [
                    {"id": item["id"], "text": item["text"].replace("번재", "번째")}
                    for item in targets
                ],
                "uncertain_terms": [],
            }
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        processor = MindlogicPostprocessor(
            self.settings(correction_chunk_chars=18, correction_overlap_segments=1),
            client,
        )
        result = processor.correct(
            title="개인 이름이 들어간 수업",
            language="ko",
            segments=self.source_segments(),
        )

        self.assertGreater(len(requests), 1)
        self.assertEqual([item["id"] for item in result.segments], ["s1", "s2", "s3"])
        self.assertEqual(result.segments[0]["text"], "첫 번째 문장 15개입니다.")
        self.assertIn("010-1234-5678", result.segments[1]["text"])
        self.assertIn("test@example.com", result.segments[1]["text"])
        serialized_requests = json.dumps(requests, ensure_ascii=False)
        self.assertNotIn("15개", serialized_requests)
        self.assertNotIn("010-1234-5678", serialized_requests)
        self.assertNotIn("test@example.com", serialized_requests)
        self.assertNotIn("개인 이름이 들어간 수업", serialized_requests)
        client.close()

    def test_transient_gateway_failure_retries_but_credit_exhaustion_does_not(self):
        for transient in (408, 425, 429, 503, "network"):
            with self.subTest(transient=transient):
                calls = 0

                def retry_handler(request: httpx.Request) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        if transient == "network":
                            raise httpx.ConnectError("provider-private-error", request=request)
                        return httpx.Response(transient, content=b"provider-private-error")
                    user_data = json.loads(json.loads(request.content)["messages"][1]["content"])
                    targets = user_data["segments"]
                    parsed = {
                        "segments": [{"id": item["id"], "text": item["text"]} for item in targets],
                        "uncertain_terms": [],
                    }
                    return httpx.Response(
                        200,
                        json={
                            "choices": [
                                {"message": {"content": json.dumps(parsed, ensure_ascii=False)}}
                            ]
                        },
                    )

                retry_client = httpx.Client(transport=httpx.MockTransport(retry_handler))
                processor = MindlogicPostprocessor(
                    self.settings(correction_max_retries=1), retry_client
                )
                processor.correct(title="", language="ko", segments=self.source_segments()[:1])
                self.assertEqual(calls, 2)
                retry_client.close()

        credit_calls = 0

        def credit_handler(request: httpx.Request) -> httpx.Response:
            nonlocal credit_calls
            credit_calls += 1
            return httpx.Response(402, content=b"do-not-reflect-this")

        credit_client = httpx.Client(transport=httpx.MockTransport(credit_handler))
        processor = MindlogicPostprocessor(self.settings(correction_max_retries=3), credit_client)
        with self.assertRaises(PostprocessingError) as raised:
            processor.correct(title="", language="ko", segments=self.source_segments()[:1])
        self.assertEqual(raised.exception.code, "credit_exhausted")
        self.assertNotIn("do-not-reflect-this", str(raised.exception))
        self.assertEqual(credit_calls, 1)
        credit_client.close()

    def test_changed_ids_numbers_placeholders_and_oversized_sources_fail_closed(self):
        cases = ("id", "number", "placeholder")
        for case in cases:
            with self.subTest(case=case):
                def handler(request: httpx.Request, case=case) -> httpx.Response:
                    user_data = json.loads(json.loads(request.content)["messages"][1]["content"])
                    target = user_data["segments"][0]
                    identifier = "wrong" if case == "id" else target["id"]
                    text = target["text"]
                    if case == "number":
                        text = text.replace("__PRIVATE_000001__", "16")
                    if case == "placeholder":
                        text = text.replace("__PRIVATE_000001__", "")
                    content = json.dumps(
                        {"segments": [{"id": identifier, "text": text}], "uncertain_terms": []},
                        ensure_ascii=False,
                    )
                    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

                segment = self.source_segments()[1:2] if case == "placeholder" else self.source_segments()[:1]
                client = httpx.Client(transport=httpx.MockTransport(handler))
                processor = MindlogicPostprocessor(self.settings(), client)
                with self.assertRaises(PostprocessingError):
                    processor.correct(title="", language="ko", segments=segment)
                client.close()

        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: self.fail("oversized source must not make an HTTP request")
            )
        )
        processor = MindlogicPostprocessor(self.settings(), client)
        with self.assertRaises(PostprocessingError) as raised:
            processor.correct(
                title="",
                language="ko",
                segments=[{"id": "large", "start": 0, "end": 1, "text": "가" * 24_001}],
            )
        self.assertEqual(raised.exception.code, "source_too_large")
        client.close()

    def test_new_numbers_are_retained_with_a_local_uncertainty_warning(self):
        warning = "AI가 원문에 없던 숫자 표기를 추가했습니다. 원문과 비교하세요."
        cases = [("제 이법칙입니다.", "제2법칙입니다.")]
        for source_text, corrected_text in cases:
            with self.subTest(source=source_text):
                def handler(request: httpx.Request) -> httpx.Response:
                    user_data = json.loads(json.loads(request.content)["messages"][1]["content"])
                    target = user_data["segments"][0]
                    content = json.dumps(
                        {
                            "segments": [{"id": target["id"], "text": corrected_text}],
                            "uncertain_terms": [],
                        },
                        ensure_ascii=False,
                    )
                    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

                client = httpx.Client(transport=httpx.MockTransport(handler))
                processor = MindlogicPostprocessor(self.settings(), client)
                result = processor.correct(
                    title="",
                    language="ko",
                    segments=[{"id": "s1", "start": 0, "end": 1, "text": source_text}],
                )
                self.assertEqual(result.segments[0]["text"], corrected_text)
                self.assertEqual(result.uncertain_terms, [warning])
                client.close()

    def test_existing_number_cannot_be_changed_and_appended_elsewhere(self):
        def handler(request: httpx.Request) -> httpx.Response:
            user_data = json.loads(json.loads(request.content)["messages"][1]["content"])
            target = user_data["segments"][0]
            protected = target["text"].removeprefix("온도는 ").removesuffix("도입니다.")
            content = json.dumps(
                {
                    "segments": [
                        {
                            "id": target["id"],
                            "text": f"온도는 16도입니다. 참고로 원문 숫자는 {protected}였습니다.",
                        }
                    ],
                    "uncertain_terms": [],
                },
                ensure_ascii=False,
            )
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        processor = MindlogicPostprocessor(self.settings(), client)
        with self.assertRaises(PostprocessingError) as raised:
            processor.correct(
                title="",
                language="ko",
                segments=[{"id": "s1", "start": 0, "end": 1, "text": "온도는 15도입니다."}],
            )
        self.assertEqual(raised.exception.code, "protected_content_changed")
        client.close()

    def test_incomplete_refused_and_multiple_choice_responses_fail_closed(self):
        content = json.dumps({"segments": [{"id": "s1", "text": "정상 문장입니다."}], "uncertain_terms": []})
        valid = {"finish_reason": "stop", "message": {"content": content}}
        cases = {
            "length": {"choices": [{**valid, "finish_reason": "length"}]},
            "content_filter": {"choices": [{**valid, "finish_reason": "content_filter"}]},
            "tool_calls": {"choices": [{**valid, "finish_reason": "tool_calls"}]},
            "refusal": {"choices": [{**valid, "message": {"content": content, "refusal": "provider-private-refusal"}}]},
            "multiple_choices": {"choices": [valid, valid]},
            "empty_choices": {"choices": []},
            "non_list_choices": {"choices": {"0": valid}},
            "non_object_choice": {"choices": [None]},
            "non_object_message": {"choices": [{"message": [content]}]},
        }
        for name, envelope in cases.items():
            with self.subTest(case=name):
                calls = []

                def handler(request):
                    calls.append(request)
                    return httpx.Response(200, json=envelope)

                source = [{"id": "s1", "start": 0, "end": 1, "text": "정상 문장입니다."}]
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    processor = MindlogicPostprocessor(self.settings(correction_max_retries=3), client)
                    with self.assertRaises(PostprocessingError) as raised:
                        processor.correct(title="", language="ko", segments=source)
                expected_code = ("response_truncated" if name == "length"
                                 else "model_refused" if name in {"refusal", "content_filter"}
                                 else "invalid_response")
                self.assertEqual(raised.exception.code, expected_code)
                self.assertNotIn("provider-private", str(raised.exception))
                self.assertEqual(len(calls), 1)
                self.assertEqual(source[0]["text"], "정상 문장입니다.")

    def test_duplicate_json_keys_and_nonstandard_constants_fail_closed(self):
        # Each duplicate has a valid last value, so ordinary json.loads would
        # silently discard the earlier value and accept the correction.
        item = '{"id":"s1","text":"정상 문장입니다."}'
        contents = {
            "root": '{"segments":[],"segments":[' + item + '],"uncertain_terms":[]}',
            "id": '{"segments":[{"id":"wrong","id":"s1","text":"정상 문장입니다."}],"uncertain_terms":[]}',
            "text": '{"segments":[{"id":"s1","text":"provider-private-text","text":"정상 문장입니다."}],"uncertain_terms":[]}',
            "uncertain_terms": '{"segments":[' + item + '],"uncertain_terms":["provider-private-text"],"uncertain_terms":[]}',
        }
        for constant in ("NaN", "Infinity", "-Infinity"):
            contents[constant] = '{"segments":[' + item + '],"uncertain_terms":[' + constant + ']}'
        for name, content in contents.items():
            with self.subTest(case=name):
                calls = []

                def handler(request):
                    calls.append(request)
                    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    processor = MindlogicPostprocessor(self.settings(), client)
                    with self.assertRaises(PostprocessingError) as raised:
                        processor.correct(title="", language="ko", segments=[{
                            "id": "s1", "start": 0, "end": 1, "text": "정상 문장입니다."}])
                self.assertEqual(raised.exception.code, "invalid_response")
                self.assertNotIn("provider-private", str(raised.exception))
                self.assertEqual(len(calls), 1)

    def test_stop_and_legacy_missing_finish_reason_preserve_valid_corrections(self):
        content = json.dumps({"segments": [{"id": "s1", "text": "정상 문장입니다."}], "uncertain_terms": []})
        for fields in ({}, {"finish_reason": None}, {"finish_reason": "stop"}):
            with self.subTest(fields=fields):
                def handler(request):
                    return httpx.Response(200, json={"choices": [{**fields, "message": {"content": content, "refusal": None}}]})

                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    result = MindlogicPostprocessor(self.settings(), client).correct(
                        title="", language="ko", segments=[{"id": "s1", "start": 0, "end": 1, "text": "정상 문장입니다."}])
                self.assertEqual(result.segments, [{"id": "s1", "start": 0, "end": 1, "text": "정상 문장입니다."}])
                self.assertEqual(result.uncertain_terms, [])

    @staticmethod
    def echo_response(body):
        data = json.loads(body["messages"][1]["content"])
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"segments": data["segments"], "uncertain_terms": []}, ensure_ascii=False),
        }}]})

    def test_context_is_readonly_text_not_extra_output_rows_and_ids_are_schema_locked(self):
        calls = []
        source = self.source_segments()
        before = copy.deepcopy(source)

        def handler(request):
            body = json.loads(request.content)
            data = json.loads(body["messages"][1]["content"])
            calls.append(data)
            self.assertEqual(set(data), {"language", "segments", "readonly_context"})
            self.assertTrue(all(set(row) == {"id", "text"} for row in data["segments"]))
            context = data["readonly_context"]
            self.assertEqual(set(context), {"before", "after"})
            self.assertTrue(all(isinstance(text, str) for text in context["before"] + context["after"]))
            expected = [row["id"] for row in data["segments"]]
            schema = body["response_format"]["json_schema"]["schema"]
            self.assertEqual(schema["properties"]["segments"]["items"]["properties"]["id"]["enum"], expected)
            # Reproduces the old failure mode: a model echoes every row in
            # segments, rather than interpreting the former target=false flag.
            return self.echo_response(body)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = MindlogicPostprocessor(self.settings(correction_chunk_chars=18), client).correct(
                title="never-send-this-title", language="ko", segments=source,
            )
        self.assertEqual(result.segments, source)
        self.assertEqual(source, before)
        self.assertEqual(len(calls), 3)
        self.assertTrue(calls[0]["readonly_context"]["after"])
        self.assertTrue(calls[1]["readonly_context"]["before"])
        self.assertTrue(calls[1]["readonly_context"]["after"])
        self.assertNotIn("test@example.com", json.dumps(calls, ensure_ascii=False))
        self.assertNotIn("never-send-this-title", json.dumps(calls, ensure_ascii=False))

    def test_many_short_segments_are_bounded_by_row_count_not_just_characters(self):
        source = [{"id": f"source-{index}", "start": index, "end": index + 1, "text": "말"}
                  for index in range(129)]
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append(len(json.loads(body["messages"][1]["content"])["segments"]))
            return self.echo_response(body)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = MindlogicPostprocessor(self.settings(), client).correct(title="", language="ko", segments=source)
        self.assertEqual(calls, [64, 64, 1])
        self.assertEqual(result.segments, source)

    def test_numeric_mask_expansion_and_json_overhead_bound_plans_and_output_budget(self):
        source = [{"id": f"source-{index}", "start": index, "end": index + 1,
                   "text": "값 " + "1 " * 80 + "끝"} for index in range(40)]
        calls = []

        def handler(request):
            body = json.loads(request.content)
            data = json.loads(body["messages"][1]["content"])
            expected = {"segments": data["segments"], "uncertain_terms": []}
            size = len(json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            self.assertLessEqual(size, 12 * 1024)
            self.assertLessEqual(len(data["segments"]), 64)
            self.assertGreater(body["max_tokens"], 8192)
            self.assertLessEqual(body["max_tokens"], 16384)
            self.assertGreaterEqual(body["max_tokens"], size + 4096)
            calls.append(body)
            return self.echo_response(body)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = MindlogicPostprocessor(self.settings(), client).correct(title="", language="ko", segments=source)
        self.assertGreater(len(calls), 2, "raw 6k char batches miss most placeholder output cost")
        self.assertEqual(result.segments, source)

    def test_masked_text_growth_does_not_weaken_the_restored_source_length_limit(self):
        source = [{"id": "numeric-source", "start": 0, "end": 1, "text": "값 " + "1 " * 80 + "끝"}]
        original = copy.deepcopy(source)
        calls = []

        def handler(request):
            body = json.loads(request.content)
            data = json.loads(body["messages"][1]["content"])
            target = data["segments"][0]
            allowed = max(1000, len(source[0]["text"]) * 4 + 500)
            self.assertGreater(len(target["text"]), allowed,
                               "unmodified protected output alone triggered the old length rejection")
            target["text"] += "가" * (allowed - len(source[0]["text"]) + 1)
            calls.append(body)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
                {"segments": [target], "uncertain_terms": []}, ensure_ascii=False)}}]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(PostprocessingError) as raised:
                MindlogicPostprocessor(self.settings(), client).correct(title="", language="ko", segments=source)
        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertEqual(len(calls), 1)
        self.assertEqual(source, original)

    def test_default_large_korean_source_can_still_fit_the_existing_64_base_plans(self):
        source = [{"id": f"source-{index}", "start": index, "end": index + 1, "text": "가" * 1000}
                  for index in range(250)]
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append(body["max_tokens"])
            return self.echo_response(body)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = MindlogicPostprocessor(self.settings(), client).correct(title="", language="ko", segments=source)
        self.assertEqual(len(calls), 63)
        self.assertTrue(all(8192 <= value <= 16384 for value in calls))
        self.assertEqual(result.segments, source)

    def test_only_confirmed_truncation_splits_targets_and_preserves_ids_times_and_context(self):
        source = [{"id": f"source-{index}", "start": index / 2, "end": index / 2 + 0.5,
                   "text": f"값은 {index + 10}이며 조건은 유지합니다."} for index in range(8)]
        original = copy.deepcopy(source)
        calls = []

        def handler(request):
            body = json.loads(request.content)
            data = json.loads(body["messages"][1]["content"])
            calls.append(data)
            if len(data["segments"]) > 4:
                return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": '{"segments":['}}]})
            return self.echo_response(body)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = MindlogicPostprocessor(self.settings(), client).correct(title="", language="ko", segments=source)
        self.assertEqual([len(data["segments"]) for data in calls], [8, 4, 4])
        self.assertEqual(result.segments, original)
        self.assertEqual(source, original)
        self.assertTrue(calls[1]["readonly_context"]["after"])
        self.assertTrue(calls[2]["readonly_context"]["before"])
        self.assertEqual([row["id"] for row in calls[1]["segments"] + calls[2]["segments"]],
                         [row["id"] for row in source])

    def test_truncated_large_single_segment_is_not_split_or_saved_and_tokens_are_capped(self):
        source = [{"id": "large-single", "start": 0, "end": 1, "text": "가" * 24_000}]
        original = copy.deepcopy(source)
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            self.assertEqual(body["max_tokens"], 16384)
            return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{"}}]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(PostprocessingError) as raised:
                MindlogicPostprocessor(self.settings(), client).correct(title="", language="ko", segments=source)
        self.assertEqual(raised.exception.code, "response_truncated")
        self.assertEqual(len(calls), 1)
        self.assertEqual(source, original)

    def test_truncation_split_depth_is_bounded(self):
        source = [{"id": f"source-{index}", "start": index, "end": index + 1, "text": "말"}
                  for index in range(64)]
        calls = []

        def handler(request):
            data = json.loads(json.loads(request.content)["messages"][1]["content"])
            calls.append(len(data["segments"]))
            return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{"}}]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(PostprocessingError) as raised:
                MindlogicPostprocessor(self.settings(), client).correct(title="", language="ko", segments=source)
        self.assertEqual(raised.exception.code, "response_truncated")
        self.assertEqual(calls, [64, 32, 16, 8, 4])

    def test_total_split_budget_is_shared_across_chunks_and_capped_at_80_logical_calls(self):
        source = [{"id": f"source-{index}", "start": index, "end": index + 1, "text": "말"}
                  for index in range(128)]
        calls = []

        def handler(request):
            body = json.loads(request.content)
            data = json.loads(body["messages"][1]["content"])
            calls.append(data)
            index = int(data["segments"][0]["id"].split("-")[-1])
            if len(data["segments"]) == 2 and index < 16:
                return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{"}}]})
            return self.echo_response(body)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = MindlogicPostprocessor(self.settings(correction_chunk_chars=2), client).correct(
                title="", language="ko", segments=source)
        self.assertEqual(len(calls), 80)
        self.assertEqual(result.segments, source)

    def test_exhausted_split_budget_fails_without_returning_preceding_partial_corrections(self):
        source = [{"id": f"source-{index}", "start": index, "end": index + 1, "text": "말"}
                  for index in range(18)]
        before = copy.deepcopy(source)
        calls = []

        def handler(request):
            body = json.loads(request.content)
            data = json.loads(body["messages"][1]["content"])
            calls.append(data)
            if len(data["segments"]) == 2:
                return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{"}}]})
            return self.echo_response(body)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(PostprocessingError) as raised:
                MindlogicPostprocessor(self.settings(correction_chunk_chars=2), client).correct(title="", language="ko", segments=source)
        self.assertEqual(raised.exception.code, "response_truncated")
        self.assertEqual(len(calls), 25)  # Nine planned calls + sixteen extra.
        self.assertEqual(source, before)

    def test_interruption_after_truncation_does_not_start_split_requests(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{"}}]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(PostprocessingError) as raised:
                MindlogicPostprocessor(self.settings(), client).correct(
                    title="", language="ko", segments=self.source_segments(), interrupted=lambda: bool(calls))
        self.assertEqual(raised.exception.code, "interrupted")
        self.assertEqual(len(calls), 1)

    def test_malformed_or_refused_multirow_outputs_never_trigger_split_rebilling(self):
        for mode in ("extra", "reordered", "unknown_id", "code_fence", "prose", "missing_uncertainty", "refused_length"):
            with self.subTest(mode=mode):
                calls = []

                def handler(request):
                    body = json.loads(request.content)
                    data = json.loads(body["messages"][1]["content"])
                    result = {"segments": data["segments"], "uncertain_terms": []}
                    if mode == "extra": result["segments"].append(dict(result["segments"][-1]))
                    if mode == "reordered": result["segments"].reverse()
                    if mode == "unknown_id": result["segments"][0]["id"] = "unknown-context-id"
                    if mode == "missing_uncertainty": del result["uncertain_terms"]
                    content = json.dumps(result, ensure_ascii=False)
                    if mode == "code_fence": content = "```json\n" + content + "\n```"
                    if mode == "prose": content += "\nprovider-private-extra-prose"
                    message = {"content": content}
                    if mode == "refused_length": message["refusal"] = "provider-private-refusal"
                    calls.append(body)
                    return httpx.Response(200, json={"choices": [{"finish_reason": "length" if mode == "refused_length" else "stop", "message": message}]})

                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(PostprocessingError) as raised:
                        MindlogicPostprocessor(self.settings(correction_max_retries=3), client).correct(
                            title="", language="ko", segments=self.source_segments())
                self.assertEqual(raised.exception.code, "model_refused" if mode == "refused_length" else "invalid_response")
                self.assertEqual(len(calls), 1)
                self.assertNotIn("provider-private", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
