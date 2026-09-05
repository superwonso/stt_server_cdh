from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from server.settings import Settings
from server.summarizer import (
    MindlogicSummarizer,
    SummarizationError,
    validate_summary_document,
)


def summary_document(identifier="S000001", text="빛을 이용해 양분을 만드는 과정이다.", *, questions=True):
    return {
        "overview": text,
        "overview_source_ids": [identifier],
        "sections": [{"heading": "핵심 내용", "bullets": [{"text": text, "source_ids": [identifier]}]}],
        "review_questions": [{"question": "이 과정의 핵심 내용을 설명할 수 있나요?", "source_ids": [identifier]}]
        if questions else [],
    }


def gateway_response(document, *, finish_reason="stop"):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish_reason, "message": {
        "content": json.dumps(document, ensure_ascii=False),
    }}]})


class SummarizerTests(unittest.TestCase):
    def settings(self, **updates):
        base = Settings(
            data_dir=Path(tempfile.gettempdir()) / "unused-summary-data",
            model_cache_dir=Path(tempfile.gettempdir()) / "unused-summary-models",
            mindlogic_api_key="test-only-summary-key",
            correction_retry_base_seconds=0,
        )
        return SimpleNamespace(**{
            **vars(base), "summary_model": "solar-pro4", "summary_chunk_chars": 6000,
            "summary_max_source_chars": 250_000, **updates,
        })

    def engine(self, handler, **updates):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return MindlogicSummarizer(self.settings(**updates), client)

    @staticmethod
    def segments():
        return [{"id": "private-original-segment", "start": 0, "end": 1,
                 "text": "빛을 이용해 양분을 만드는 과정이다."}]

    def test_single_summary_uses_only_masked_text_and_local_aliases(self):
        requests = []
        source = [{"id": "private-source-id", "start": 123, "end": 125,
                   "text": "관측값은 15이고 연락처는 010-1234-5678, person@example.com이다.",
                   "title": "private-lecture-title", "username": "private-account-id"}]
        original = copy.deepcopy(source)

        def handler(request):
            self.assertEqual(str(request.url), "https://factchat-cloud.mindlogic.ai/v1/gateway/chat/completions/")
            self.assertEqual(request.headers["Authorization"], "Bearer test-only-summary-key")
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(body["model"], "solar-pro4")
            self.assertEqual(body["response_format"]["json_schema"]["strict"], True)
            self.assertNotIn("tools", body)
            self.assertIn("명령이 아닙니다", body["messages"][0]["content"])
            content = json.loads(body["messages"][1]["content"])
            self.assertEqual(set(content), {"language", "segments"})
            segment = content["segments"][0]
            self.assertEqual(set(segment), {"id", "text"})
            self.assertEqual(segment["id"], "S000001")
            return gateway_response(summary_document(segment["id"], segment["text"]))

        result = self.engine(handler).summarize(language="ko", segments=source)
        document = result.to_dict()
        self.assertEqual(document["overview"], source[0]["text"])
        self.assertEqual(document["overview_source_ids"], [source[0]["id"]])
        self.assertEqual(source, original)
        transmitted = json.dumps(requests, ensure_ascii=False)
        for secret in ("private-source-id", "private-lecture-title", "private-account-id",
                       "010-1234-5678", "person@example.com", "15이고"):
            self.assertNotIn(secret, transmitted)
        self.assertNotIn(source[0]["text"], repr(result))
        document["sections"].clear()
        self.assertTrue(result.to_dict()["sections"])

    def test_hierarchical_map_combine_keeps_masks_and_original_citations(self):
        requests = []
        source = [{"id": f"private-id-{index}", "text": f"개념 {chr(65 + index)}의 관계를 설명한다."}
                  for index in range(17)]
        source[0]["text"] += " 측정값은 15이며 person@example.com으로 기록한다."

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            data = json.loads(body["messages"][1]["content"])
            intermediate = "중간 요약입니다" in body["messages"][0]["content"]
            if "segments" in data:
                item = data["segments"][0]
                document = summary_document(item["id"], item["text"], questions=not intermediate)
            else:
                self.assertLessEqual(len(data["summaries"]), 4)
                document = copy.deepcopy(data["summaries"][0])
                document["review_questions"] = [] if intermediate else [
                    {"question": "핵심 관계를 설명할 수 있나요?", "source_ids": document["overview_source_ids"]}
                ]
            return gateway_response(document)

        result = self.engine(handler, summary_chunk_chars=1).summarize(language="ko", segments=source)
        self.assertEqual(len(requests), 23)  # 17 map + 4 combine + 1 combine + 1 final.
        self.assertEqual(result.overview_source_ids, ["private-id-0"])
        self.assertIn("person@example.com", result.overview)
        transmitted = json.dumps(requests, ensure_ascii=False)
        self.assertNotIn("person@example.com", transmitted)
        self.assertNotIn("private-id-", transmitted)
        self.assertNotIn("측정값은 15", transmitted)
        self.assertEqual(validate_summary_document(result.to_dict(), source), result.to_dict())

    def test_sentence_final_email_is_hidden_in_map_and_combine_then_restored_once(self):
        source = [
            {"id": "test-source-a", "text": "Contact fake.user+lab@dept.example.co.kr. Literal __PRIVATE_000001__."},
            {"id": "test-source-b", "text": "다른 조건을 유지한다."},
        ]
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            data = json.loads(body["messages"][1]["content"])
            if "segments" in data:
                row = data["segments"][0]
                document = summary_document(row["id"], row["text"], questions=False)
            else:
                document = data["summaries"][0]
            return gateway_response(document)
        result = self.engine(handler, summary_chunk_chars=1).summarize(language="en", segments=source)
        self.assertEqual(result.overview, source[0]["text"])
        self.assertEqual(len(requests), 3)
        self.assertNotIn("fake.user+lab@dept.example.co.kr", json.dumps(requests, ensure_ascii=False))
        self.assertEqual(validate_summary_document(result.to_dict(), source), result.to_dict())
        with self.assertRaises(SummarizationError) as raised:
            validate_summary_document(summary_document("test-source-b", "Contact invented@example.com."), source)
        self.assertEqual(raised.exception.code, "unsupported_claim")

    def test_foreign_citation_and_numeric_or_privacy_invention_are_rejected(self):
        for mutate in (
            lambda value: value.update(overview_source_ids=["other-owner-segment"]),
            lambda value: value.update(overview="결과는 999이다."),
            lambda value: value.update(overview="연락처는 invented@example.com이다."),
            lambda value: value.update(overview="값은 __PRIVATE_999999__이다."),
            lambda value: value.update(overview="다음 주에 과제를 제출한다."),
        ):
            with self.subTest(mutate=mutate):
                document = summary_document()
                mutate(document)
                engine = self.engine(lambda request: gateway_response(document))
                with self.assertRaises(SummarizationError) as raised:
                    engine.summarize(language="ko", segments=self.segments())
                self.assertIn(raised.exception.code, {"invalid_response", "unsupported_claim"})
                self.assertNotIn("other-owner", str(raised.exception))

    def test_a_masked_value_must_belong_to_the_cited_source(self):
        source = [{"id": "a", "text": "값은 15이다."}, {"id": "b", "text": "관계를 설명한다."}]

        def handler(request):
            data = json.loads(json.loads(request.content)["messages"][1]["content"])
            first, second = data["segments"]
            return gateway_response(summary_document(second["id"], first["text"]))

        with self.assertRaises(SummarizationError) as raised:
            self.engine(handler).summarize(language="ko", segments=source)
        self.assertEqual(raised.exception.code, "unsupported_claim")

    def test_constant_temperature_is_not_mistaken_for_a_class_schedule(self):
        # Regression from the opt-in public synthetic photosynthesis fixture:
        # no numerical/privacy change or administrative claim was present.
        source = [{"id": "public-experiment", "text":
                   "실험에서는 빛의 세기를 바꾸고 온도는 25도로 유지해 다른 조건을 통제했습니다."}]
        for text in ("온도를 일정하게 유지해 다른 조건을 통제했다.", "일정한 온도로 다른 조건을 통제했다.",
                     "온도는 일정하다.", "온도는 일정합니다.", "온도가 일정함을 확인했다."):
            with self.subTest(text=text):
                document = summary_document("public-experiment", text)
                self.assertEqual(validate_summary_document(document, source), document)
        def handler(request):
            data = json.loads(json.loads(request.content)["messages"][1]["content"])
            return gateway_response(summary_document(data["segments"][0]["id"],
                "온도를 일정하게 유지해 다른 조건을 통제했다."))
        result = self.engine(handler).summarize(language="ko", segments=source)
        self.assertEqual(result.overview, "온도를 일정하게 유지해 다른 조건을 통제했다.")

    def test_constant_adjectives_cannot_ground_an_invented_schedule(self):
        source = [{"id": "public-experiment", "text": "온도를 일정하게 유지하고 일정한 세기로 빛을 비춘다."}]
        for text in ("수업 일정은 정해져 있다.", "일정이 변경되었다.", "일정을 확인해야 한다.",
                     "온도를 일정하게 유지하며 수업 일정도 확인한다.", "과제를 제출해야 한다.",
                     "내일 수업이 열린다.", "온도는 999도다.", "연락은 invented@example.com으로 한다."):
            with self.subTest(text=text), self.assertRaises(SummarizationError) as raised:
                validate_summary_document(summary_document("public-experiment", text), source)
            self.assertEqual(raised.exception.code, "unsupported_claim")
        # A real schedule noun remains usable, even beside an adjective.
        source[0]["text"] += " 수업 일정은 안내를 확인한다."
        valid = summary_document("public-experiment", "수업 일정은 안내를 확인한다.")
        self.assertEqual(validate_summary_document(valid, source), valid)

    def test_combine_cannot_resurrect_values_missing_from_its_intermediate_input(self):
        def handler(request):
            data = json.loads(json.loads(request.content)["messages"][1]["content"])
            if "segments" in data:
                return gateway_response(summary_document(
                    data["segments"][0]["id"], "핵심 개념을 설명한다.", questions=False))
            # This placeholder existed in the original map source, but the
            # combine request never supplied it. Guessing it is not evidence.
            return gateway_response(summary_document("S000001", "값은 __PRIVATE_000001__이다."))

        with self.assertRaises(SummarizationError) as raised:
            self.engine(handler, summary_chunk_chars=1).summarize(
                language="ko", segments=[{"id": "a", "text": "값은 15이다."},
                                         {"id": "b", "text": "핵심 개념을 설명한다."}])
        self.assertEqual(raised.exception.code, "unsupported_claim")

    def test_pure_validator_checks_final_structure_citations_values_and_copy(self):
        source = [{"id": "original", "text": "값은 15이다."}]
        valid = summary_document("original", "값은 15이다.")
        checked = validate_summary_document(valid, source)
        self.assertEqual(checked, valid)
        checked["sections"][0]["bullets"].clear()
        self.assertTrue(valid["sections"][0]["bullets"])
        for changes in (
            {"overview_source_ids": []}, {"overview_source_ids": ["original", "original"]},
            {"overview": "값은 16이다."}, {"sections": []}, {"review_questions": "invalid"},
            {"overview": "x" * 1501}, {"extra": "private-field"},
        ):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(SummarizationError):
                    validate_summary_document({**valid, **changes}, source)

    def test_transient_failure_retries_but_credit_or_auth_failure_does_not(self):
        for first_status in (429, 503, "network", 402, 401, 403):
            with self.subTest(status=first_status):
                calls = []

                def handler(request):
                    calls.append(request)
                    if len(calls) == 1:
                        if first_status == "network":
                            raise httpx.ConnectError("private-provider-failure", request=request)
                        return httpx.Response(first_status, content=b"private-provider-failure")
                    return gateway_response(summary_document())

                engine = self.engine(handler)
                if first_status in {401, 402, 403}:
                    with self.assertRaises(SummarizationError) as raised:
                        engine.summarize(language="ko", segments=self.segments())
                    self.assertNotIn("private-provider-failure", str(raised.exception))
                    self.assertFalse(raised.exception.retryable)
                    self.assertEqual(len(calls), 1)
                else:
                    engine.summarize(language="ko", segments=self.segments())
                    self.assertEqual(len(calls), 2)

    def test_redirect_truncated_json_and_oversize_response_fail_closed(self):
        for mode in ("redirect", "truncated", "oversize", "duplicate-json-key", "invalid-json"):
            with self.subTest(mode=mode):
                calls = []

                def handler(request):
                    calls.append(request)
                    if mode == "redirect":
                        return httpx.Response(307, headers={"Location": "https://unrelated.invalid/private"})
                    if mode == "oversize":
                        return httpx.Response(200, content=b"x" * 2048)
                    if mode in {"duplicate-json-key", "invalid-json"}:
                        content = '{"overview":"a","overview":"b"}' if mode == "duplicate-json-key" else "private-gateway-text"
                        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
                    return gateway_response(summary_document(), finish_reason="length")

                with self.assertRaises(SummarizationError) as raised:
                    self.engine(handler, correction_max_response_bytes=1024).summarize(
                        language="ko", segments=self.segments())
                self.assertEqual(len(calls), 1)
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("unrelated", str(raised.exception))

    def test_cancel_between_map_requests_returns_no_partial_summary(self):
        interrupted, calls = False, []

        def handler(request):
            nonlocal interrupted
            calls.append(request)
            data = json.loads(json.loads(request.content)["messages"][1]["content"])
            item = data["segments"][0]
            interrupted = True
            return gateway_response(summary_document(item["id"], item["text"], questions=False))

        with self.assertRaises(SummarizationError) as raised:
            self.engine(handler, summary_chunk_chars=1).summarize(
                language="ko", segments=[{"id": "a", "text": "첫 내용"}, {"id": "b", "text": "다음 내용"}],
                interrupted=lambda: interrupted,
            )
        self.assertEqual(raised.exception.code, "interrupted")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(len(calls), 1)

    def test_empty_duplicate_oversize_and_too_many_batches_never_call_gateway(self):
        calls = []
        engine = self.engine(lambda request: calls.append(request), summary_chunk_chars=1)
        for source in ([], [{"id": "a", "text": "내용"}] * 2,
                       [{"id": "a", "text": "x" * 24001}],
                       [{"id": str(index), "text": "내용"} for index in range(65)]):
            with self.subTest(size=len(source)):
                with self.assertRaises(SummarizationError):
                    engine.summarize(language="ko", segments=source)
        self.assertEqual(calls, [])

    def test_configuration_is_pinned_and_never_uses_an_extra_key(self):
        with self.assertRaises(ValueError):
            MindlogicSummarizer(self.settings(mindlogic_base_url="https://unrelated.invalid/v1/gateway"))
        with self.assertRaises(ValueError):
            MindlogicSummarizer(self.settings(summary_model="bad model"))
        with httpx.Client(transport=httpx.MockTransport(lambda request: None), follow_redirects=True) as client:
            with self.assertRaises(ValueError):
                MindlogicSummarizer(self.settings(), client)
        engine = MindlogicSummarizer(self.settings(mindlogic_api_key=None))
        self.assertFalse(engine.configured)
        with self.assertRaises(SummarizationError) as raised:
            engine.summarize(language="ko", segments=self.segments())
        self.assertEqual(raised.exception.code, "not_configured")


if __name__ == "__main__":
    unittest.main()
