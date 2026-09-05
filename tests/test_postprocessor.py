from __future__ import annotations

import json
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
            targets = [{"id": row["id"], "text": row["text"]} for row in data["segments"] if row["target"]]
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
            target = next(row for row in data["segments"] if row["target"])
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
            self.assertEqual(body["model"], "solar-pro4")
            self.assertEqual(body["response_format"]["type"], "json_schema")
            user_data = json.loads(body["messages"][1]["content"])
            self.assertNotIn("lecture_title", user_data)
            targets = [item for item in user_data["segments"] if item["target"]]
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
                    targets = [item for item in user_data["segments"] if item["target"]]
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
                    target = next(item for item in user_data["segments"] if item["target"])
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
                    target = next(item for item in user_data["segments"] if item["target"])
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
            target = next(item for item in user_data["segments"] if item["target"])
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


if __name__ == "__main__":
    unittest.main()
