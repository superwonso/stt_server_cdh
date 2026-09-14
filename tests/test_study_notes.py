from __future__ import annotations

import copy
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

import httpx

from server import study_notes
from scripts import validate_study_note_llm
from server.settings import Settings
from server.study_notes import (
    MindlogicStudyNotes, StudyNoteDocument, StudyNoteError,
    study_note_markdown, validate_study_note_document, validate_study_note_source,
)


def source(text="수업의 원리를 설명합니다.", identifier="local-row", start=0, end=None):
    return {"id": identifier, "start": start, "end": start + 1 if end is None else end, "text": text}


def response(document, finish="stop", **message):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {
        "content": json.dumps(document, ensure_ascii=False), **message,
    }}]})


def read_request(request):
    payload = json.loads(request.content)
    data = json.loads(payload["messages"][1]["content"])
    parts = re.split(r"(?m)^## (S[0-9]{6})\n\n", data["source_markdown"])
    rows = {}
    for index in range(1, len(parts), 2):
        lines = parts[index + 1].rstrip("\n").split("\n")
        assert all(line.startswith("> ") for line in lines)
        rows[parts[index]] = "\n".join(line[2:] for line in lines)
    return payload, data, rows


def echo_document(data, rows):
    return {"paragraphs": [{"heading": "수업 주제", "source_ids": data["target_source_ids"],
                            "text": "\n".join(rows[identifier] for identifier in data["target_source_ids"]), "edits": []}]}


def raw_document(raw):
    return {"paragraphs": [{"heading": "수업 주제", "source_ids": [row["id"] for row in raw],
                            "text": "\n".join(row["text"] for row in raw), "edits": []}]}


class StudyNoteTests(unittest.TestCase):
    def settings(self, **updates):
        return Settings(data_dir=Path(tempfile.gettempdir()) / "unused-study-note-data",
                        model_cache_dir=Path(tempfile.gettempdir()) / "unused-study-note-models",
                        **{"mindlogic_api_key": "test-only-nova-key", "correction_retry_base_seconds": 0, **updates})

    def test_complete_context_aliases_masking_existing_model_and_metadata_exclusion(self):
        raw = [source("값은 15이고 연락처는 fake.user@example.com. 원래 표식 __PRIVATE_000002__도 보존합니다.", "private-raw-id", 123.75)]
        raw[0].update(username="private-owner", title="private-title", audio="private-audio")
        before = copy.deepcopy(raw)
        calls = []

        def handler(request):
            self.assertEqual(str(request.url), "https://factchat-cloud.mindlogic.ai/v1/gateway/chat/completions/")
            self.assertFalse(request.url.params)
            payload, data, rows = read_request(request)
            calls.append(payload)
            self.assertEqual(payload["model"], "gpt-5.6-luna")
            instructions = payload["messages"][0]["content"]
            self.assertIn("전체적인 맥락을 고려해서 영어로 작성되어 있는 것들을 한글로 번역해줘. 영어인데 한글로 써져있는 것들도 있으니 이것도 해결해주면 좋을거같아", instructions)
            self.assertIn("기존 후보정·번역·수동수정을 적용하지 않은", instructions)
            self.assertIn("Markdown 파일 내용", instructions)
            self.assertIn("단순 요약을 만들지 마세요", instructions)
            self.assertIn("알맞은 한국어로 설명", instructions)
            self.assertEqual(set(data), {"language", "source_markdown", "target_source_ids"})
            self.assertEqual(data["target_source_ids"], ["S000001"])
            schema = payload["response_format"]["json_schema"]
            self.assertTrue(schema["strict"])
            self.assertEqual(schema["schema"]["properties"]["paragraphs"]["items"]["properties"]["source_ids"]["items"]["enum"], ["S000001"])
            self.assertLessEqual(payload["max_tokens"], 16384)
            self.assertNotIn("tools", payload)
            return response(echo_document(data, rows))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = MindlogicStudyNotes(self.settings(), client).create(language="ko", segments=raw)
            self.assertFalse(client.is_closed)
        self.assertEqual(result.paragraphs[0]["source_ids"], ["private-raw-id"])
        self.assertEqual(result.paragraphs[0]["text"], raw[0]["text"])
        transmitted = json.dumps(calls, ensure_ascii=False)
        for forbidden in ("private-raw-id", "private-owner", "private-title", "private-audio", "fake.user@example.com", "123.75", "15이고"):
            self.assertNotIn(forbidden, transmitted)
        self.assertEqual(raw, before)
        clone = result.to_dict(); clone["paragraphs"][0]["source_ids"].clear()
        self.assertEqual(result.paragraphs[0]["source_ids"], ["private-raw-id"])

    def test_hangul_transliterations_can_be_restored_with_source_grounded_edit_audit(self):
        raw = [source("배치 놀말라이제이션은 입력의 분포를 다룹니다.", "first", 1.25),
               source("통계량을 사용하는 조건을 확인합니다.", "second", 3.5, 5.75)]

        def handler(request):
            _, data, rows = read_request(request)
            document = echo_document(data, rows)
            paragraph = document["paragraphs"][0]
            paragraph["heading"] = "분포와 정규화"
            paragraph["text"] = paragraph["text"].replace("배치 놀말라이제이션", "**batch normalization**")
            paragraph["edits"] = [{"original": "배치 놀말라이제이션", "replacement": "batch normalization", "uncertain": True}]
            return response(document)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = MindlogicStudyNotes(self.settings(), client).create(language="ko", segments=raw)
        self.assertEqual(result.paragraphs[0]["source_ids"], ["first", "second"])
        markdown = study_note_markdown(result.to_dict(), raw)
        self.assertIn("[00:01.25–00:05.75] 분포와 정규화", markdown)
        self.assertIn("**batch normalization**", markdown)
        self.assertIn("배치 놀말라이제이션 → batch normalization (추정 · 확인 필요)", markdown)
        self.assertNotIn("first", markdown)

    def test_multiple_requests_each_receive_exact_same_full_markdown(self):
        raw = [source(f"개념을 설명합니다. 끝부분도 참고합니다.", f"local-{index}", index) for index in range(129)]
        requests = []

        def handler(request):
            payload, data, rows = read_request(request)
            requests.append(data)
            self.assertEqual(len(rows), len(raw))
            self.assertLessEqual(len(data["target_source_ids"]), 64)
            self.assertLessEqual(payload["max_tokens"], 16384)
            return response(echo_document(data, rows))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = MindlogicStudyNotes(self.settings(), client).create(language="ko", segments=raw)
        self.assertEqual([len(data["target_source_ids"]) for data in requests], [64, 64, 1])
        self.assertEqual(len({data["source_markdown"] for data in requests}), 1)
        self.assertEqual([identifier for p in result.paragraphs for identifier in p["source_ids"]], [r["id"] for r in raw])

    def test_coverage_rejects_missing_extra_duplicate_reordered_and_noncontiguous_ids(self):
        raw = [source("한 개념입니다.", "a"), source("다른 개념입니다.", "b", 1), source("마지막 개념입니다.", "c", 2)]
        for ids in (["a", "b"], ["a", "b", "c", "other"], ["a", "a", "c"], ["b", "a", "c"], ["a", "c"]):
            with self.subTest(ids=ids):
                document = raw_document(raw); document["paragraphs"][0]["source_ids"] = ids
                with self.assertRaises(StudyNoteError) as error:
                    validate_study_note_document(document, raw)
                self.assertEqual(error.exception.code, "invalid_response")
        document = raw_document(raw)
        document["paragraphs"] = [dict(document["paragraphs"][0], source_ids=["a", "c"]),
                                  dict(document["paragraphs"][0], source_ids=["b"])]
        with self.assertRaises(StudyNoteError): validate_study_note_document(document, raw)

    def test_numbers_contacts_and_literal_markers_may_change_without_discarding_notes(self):
        raw = [source("온도 15, 수량 20, 메일 fake@example.com, 전화 010-1234-5678, __PRIVATE_000007__입니다.")]
        changes = [lambda text: text.replace("15", "16"), lambda text: text.replace("15", ""),
                   lambda text: text.replace("15", "20").replace("수량 20", "수량 15"),
                   lambda text: text + " 수량 15", lambda text: text.replace("fake@example.com", "other@example.org"),
                   lambda text: text.replace("__PRIVATE_000007__", ""), lambda text: text + " 추가 9"]
        for change in changes:
            document = raw_document(raw); document["paragraphs"][0]["text"] = change(raw[0]["text"])
            with self.subTest(text=document["paragraphs"][0]["text"]):
                self.assertEqual(validate_study_note_document(document, raw), document)
                self.assertIn("# 수업 정리본", study_note_markdown(document, raw))
        no_numbers = [source("제 이 법칙입니다.")]
        document = raw_document(no_numbers)
        document["paragraphs"][0].update(heading="2. 제2법칙", text="제2법칙입니다.")
        self.assertEqual(validate_study_note_document(document, no_numbers), document)

    def test_contextual_edits_need_not_be_exact_substrings_but_structure_remains_required(self):
        raw = [source("알파 용어입니다.", "a"), source("베타 용어입니다.", "b", 1)]
        base = {"paragraphs": [{"heading": "용어", "source_ids": ["a"], "text": "alpha 용어입니다.", "edits": [
            {"original": "알파", "replacement": "alpha", "uncertain": False}]},
            {"heading": "다른 용어", "source_ids": ["b"], "text": raw[1]["text"], "edits": []}]}
        self.assertEqual(validate_study_note_document(base, raw), base)
        accepted = [{"original": "없는 용어"}, {"original": "베타"}, {"replacement": "absent"},
                    {"replacement": "term2"}, {"original": "010-1234-5678", "replacement": "fake@example.com"}]
        for changes in accepted:
            document = copy.deepcopy(base); document["paragraphs"][0]["edits"][0].update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(validate_study_note_document(document, raw), document)
        cases = [{"original": "alpha", "replacement": "alpha"}, {"uncertain": "false"},
                 {"original": ""}, {"replacement": "a" * 257}]
        for changes in cases:
            document = copy.deepcopy(base); document["paragraphs"][0]["edits"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(StudyNoteError): validate_study_note_document(document, raw)
        duplicate = copy.deepcopy(base); duplicate["paragraphs"][0]["edits"] *= 2
        with self.assertRaises(StudyNoteError): validate_study_note_document(duplicate, raw)

    def test_generated_numbers_contacts_and_contextual_edits_survive_provider_and_export(self):
        raw = [source("제 이 법칙과 한글 음차로 적힌 용어입니다.")]
        document = {"paragraphs": [{"heading": "2. 핵심 개념", "source_ids": ["S000001"],
            "text": "제2법칙과 batch normalization을 설명합니다. 문의: fake@example.com, 010-1234-5678.",
            "edits": [{"original": "배치 놀말리제이션", "replacement": "배치 정규화", "uncertain": True}]}]}
        with httpx.Client(transport=httpx.MockTransport(lambda request: response(document))) as client:
            result = MindlogicStudyNotes(self.settings(), client).create(language="ko", segments=raw)
        expected = copy.deepcopy(document); expected["paragraphs"][0]["source_ids"] = [raw[0]["id"]]
        self.assertEqual(result.to_dict(), expected)
        markdown = study_note_markdown(result.to_dict(), raw)
        self.assertIn("제2법칙", markdown)
        self.assertIn("fake@example\\.com", markdown)
        self.assertIn("배치 놀말리제이션 → 배치 정규화", markdown)

    def test_masks_are_restored_in_all_fields_without_order_or_count_rejection(self):
        raw = [source("값 15, 메일 fake@example.com, 전화 010-1234-5678입니다.")]
        calls = []
        def handler(request):
            _, data, rows = read_request(request)
            calls.append(request.content.decode())
            masks = study_notes._PLACEHOLDER.findall(rows["S000001"])
            self.assertEqual(len(masks), 3)
            return response({"paragraphs": [{"heading": f"1. 값 {masks[0]}", "source_ids": data["target_source_ids"],
                "text": f"{masks[2]} / {masks[1]} / {masks[1]} / 새 값 16 / __PRIVATE_999999__",
                "edits": [{"original": masks[0], "replacement": "16", "uncertain": True},
                          {"original": "연락처 복원", "replacement": masks[1], "uncertain": True}]}]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = MindlogicStudyNotes(self.settings(), client).create(language="ko", segments=raw)
        paragraph = result.paragraphs[0]
        self.assertEqual(paragraph["heading"], "1. 값 15")
        self.assertEqual(paragraph["text"], "010-1234-5678 / fake@example.com / fake@example.com / 새 값 16 / [가려진 값 확인 필요]")
        self.assertEqual(paragraph["edits"][0]["original"], "15")
        self.assertEqual(paragraph["edits"][1]["replacement"], "fake@example.com")
        self.assertNotIn("__PRIVATE_", study_note_markdown(result.to_dict(), raw))
        for value in ("fake@example.com", "010-1234-5678", "값 15"):
            self.assertNotIn(value, calls[0])

    def test_legacy_value_rejection_message_offers_explicit_retry_without_claiming_saved_result(self):
        message = str(StudyNoteError("protected_content_changed"))
        self.assertIn("이전 검증 기준", message)
        self.assertIn("다시 만들면", message)
        self.assertNotIn("원문에 없는 용어", message)

    def test_exact_schema_types_and_size_limits_remain_local(self):
        raw = [source()]
        changes = [lambda d: d.update(extra="untrusted"), lambda d: d["paragraphs"][0].update(extra=True),
                   lambda d: d.update(paragraphs=[]), lambda d: d["paragraphs"][0].update(heading="가" * 121),
                   lambda d: d["paragraphs"][0].update(heading="주제\n새 문단"),
                   lambda d: d["paragraphs"][0].update(text="가" * 24_001),
                   lambda d: d["paragraphs"][0].update(text="가" * 1100), lambda d: d["paragraphs"][0].update(text="\x00"),
                   lambda d: d["paragraphs"][0].update(source_ids="local-row"), lambda d: d["paragraphs"][0].update(edits={}),
                   lambda d: d["paragraphs"][0].update(edits=[{}] * 17)]
        for change in changes:
            document = raw_document(raw); change(document)
            with self.assertRaises(StudyNoteError): validate_study_note_document(document, raw)
        with patch("server.study_notes.MAX_DOCUMENT_BYTES", 10), self.assertRaises(StudyNoteError):
            validate_study_note_document(raw_document(raw), raw)

    def test_validator_returns_deep_copy_and_does_not_mutate_raw_or_document(self):
        raw = [source()]; document = raw_document(raw)
        before_raw, before_document = copy.deepcopy(raw), copy.deepcopy(document)
        checked = validate_study_note_document(document, raw)
        checked["paragraphs"][0]["source_ids"].append("external")
        self.assertEqual(raw, before_raw); self.assertEqual(document, before_document)

    def test_markdown_escapes_html_links_images_autolinks_and_source_heading_injection(self):
        text = '**핵심** <script>alert("x")</script> [외부](https://example.org) ![이미지](https://example.org/x)\n# 가짜 제목\nhttps://example.org\n가짜 제목\n==='
        raw = [source(text)]
        markdown = study_note_markdown(raw_document(raw), raw)
        self.assertIn("**핵심**", markdown)
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("[외부](", markdown)
        self.assertNotIn("![이미지](", markdown)
        self.assertNotIn("https://", markdown)
        self.assertNotIn("\n# 가짜", markdown)
        self.assertIn("\\# 가짜 제목", markdown)
        self.assertNotIn("\n===", markdown)
        with patch("server.study_notes.MAX_MARKDOWN_BYTES", 10), self.assertRaises(StudyNoteError) as error:
            study_note_markdown(raw_document(raw), raw)
        self.assertEqual(error.exception.code, "invalid_response")

    def test_invalid_and_excessive_sources_fail_before_any_http_call(self):
        bad_sources = [None, [], [source("")], [source(identifier="")], [source(), source()],
                       [source(start=True)], [source(start=float("nan"))], [source(start=2, end=1)],
                       [source(start=1e308, end=1e308)], [source(start=10 ** 1000, end=10 ** 1000)],
                       [source(identifier="late", start=2), source(identifier="early", start=1)],
                       [source("bad\x00text")], [source("\ud800")], [source("가" * 24_001)],
                       [source("가" * 4200)],  # An indivisible row cannot fit its safe output budget.
                       [source("말", f"id-{i}", i) for i in range(2049)]]
        with httpx.Client(transport=httpx.MockTransport(lambda request: self.fail("preflight must not call HTTP"))) as client:
            engine = MindlogicStudyNotes(self.settings(), client)
            for raw in bad_sources:
                with self.subTest(kind=type(raw)), self.assertRaises(StudyNoteError):
                    engine.create(language="ko", segments=raw)
        many = [source("가" * 1000, f"id-{i}", i) for i in range(251)]
        with self.assertRaises(StudyNoteError) as error: validate_study_note_source(many)
        self.assertEqual(error.exception.code, "source_too_large")

    def test_masked_whole_context_byte_limit_is_checked_before_any_http(self):
        raw = [source("값 " + "1 " * 60, f"id-{index}", index) for index in range(60)]
        with patch("server.study_notes.MAX_INPUT_BYTES", 20_000), self.assertRaises(StudyNoteError) as error:
            validate_study_note_source(raw)
        self.assertEqual(error.exception.code, "source_too_large")

    def test_full_context_preflight_is_linear_and_wire_limit_checked_before_first_batch(self):
        raw = [source("가" * 4050, "long-first")]
        raw.extend(source("말", f"short-{i}", i + 1) for i in range(64))
        with httpx.Client(transport=httpx.MockTransport(lambda request: self.fail("all wire bounds must precede HTTP"))) as client:
            engine = MindlogicStudyNotes(self.settings(), client)
            with patch("server.study_notes._encode", wraps=study_notes._encode) as encode:
                masked, _, markdown, ranges = study_notes._prepare(raw)
            self.assertEqual(ranges, [(0, 1), (1, 65)])
            self.assertEqual(sum(isinstance(call.args[0], dict) and "source_markdown" in call.args[0]
                                 for call in encode.call_args_list), 1)
            first_bytes = len(study_notes._encode(engine._payload("auto", markdown, masked[:1])))
            last_bytes = len(study_notes._encode(engine._payload("auto", markdown, masked[1:])))
            self.assertGreater(last_bytes, first_bytes)
            with patch("server.study_notes._MAX_REQUEST_BYTES", first_bytes + 1), self.assertRaises(StudyNoteError) as error:
                engine.create(language="ko", segments=raw)
            self.assertEqual(error.exception.code, "source_too_large")

    def test_timestamp_overlaps_are_allowed_but_order_and_extreme_bounds_are_checked(self):
        raw = [source("앞부분", "a", 0, 5), source("겹친 부분", "b", 2, 3)]
        self.assertEqual(validate_study_note_source(raw), raw)
        maximum = [source("범위 끝", "last", 1e12, 1e12)]
        self.assertIn("16666666666:40", study_note_markdown(raw_document(maximum), maximum))

    def test_truncated_refused_malformed_and_transport_errors_never_retry(self):
        for kind, expected in (("length", "response_truncated"), ("refusal", "model_refused"),
                               ("bad-id", "invalid_response"), ("duplicate-key", "invalid_response"),
                               ("fence", "invalid_response"), ("network", "gateway_unavailable"),
                               (503, "gateway_unavailable"), (402, "credit_exhausted"), (401, "authentication_failed")):
            with self.subTest(kind=kind):
                calls = []
                def handler(request):
                    calls.append(request)
                    if kind == "network": raise httpx.ConnectError("private-provider-error", request=request)
                    if isinstance(kind, int): return httpx.Response(kind, content=b"private-provider-body")
                    _, data, rows = read_request(request); document = echo_document(data, rows)
                    if kind == "bad-id": document["paragraphs"][0]["source_ids"][0] = "foreign-id"
                    if kind in {"duplicate-key", "fence"}:
                        content = json.dumps(document)
                        content = '{"paragraphs":[],' + content[1:] if kind == "duplicate-key" else "```json\n" + content + "\n```"
                        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
                    return response(document, finish="length" if kind == "length" else "stop", refusal="private-provider-refusal" if kind == "refusal" else None)
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(StudyNoteError) as error:
                        MindlogicStudyNotes(self.settings(correction_max_retries=3), client).create(
                            language="ko", segments=[source(identifier="a"), source(identifier="b", start=1)])
                self.assertEqual(error.exception.code, expected)
                self.assertNotIn("private-provider", str(error.exception))
                self.assertEqual(len(calls), 1)

    def test_second_batch_failure_cannot_return_a_partial_document(self):
        raw = [source("말", f"local-{index}", index) for index in range(65)]
        before = copy.deepcopy(raw); calls = []
        def handler(request):
            calls.append(request)
            _, data, rows = read_request(request)
            return response(echo_document(data, rows), finish="length" if len(calls) == 2 else "stop")
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(StudyNoteError) as error:
                MindlogicStudyNotes(self.settings(), client).create(language="ko", segments=raw)
        self.assertEqual(error.exception.code, "response_truncated")
        self.assertEqual(len(calls), 2); self.assertEqual(raw, before)

    def test_exactly_32_requests_are_allowed_and_33_are_rejected_preflight(self):
        calls = []
        def handler(request):
            calls.append(request)
            _, data, rows = read_request(request)
            return response(echo_document(data, rows))
        raw = [source("말", f"local-{index}", index) for index in range(2048)]
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            engine = MindlogicStudyNotes(self.settings(), client)
            result = engine.create(language="ko", segments=raw)
            self.assertEqual(len(calls), 32)
            self.assertEqual(sum(len(p["source_ids"]) for p in result.paragraphs), 2048)
            with self.assertRaises(StudyNoteError):
                engine.create(language="ko", segments=raw + [source("말", "extra-row", 2048)])
        self.assertEqual(len(calls), 32)

    def test_interrupted_and_unconfigured_do_not_call_provider(self):
        with httpx.Client(transport=httpx.MockTransport(lambda request: self.fail("No HTTP expected"))) as client:
            with self.assertRaises(StudyNoteError) as error:
                MindlogicStudyNotes(self.settings(), client).create(language="ko", segments=[source()], interrupted=lambda: True)
            self.assertEqual(error.exception.code, "interrupted")
            engine = MindlogicStudyNotes(self.settings(mindlogic_api_key=None), client)
            self.assertFalse(engine.configured)
            with self.assertRaises(StudyNoteError) as error: engine.create(language="ko", segments=[source()])
            self.assertEqual(error.exception.code, "not_configured")

    def test_redirects_and_non_gateway_hosts_are_rejected_without_calls(self):
        with httpx.Client(follow_redirects=True) as client:
            with self.assertRaises(ValueError): MindlogicStudyNotes(self.settings(), client)
        with self.assertRaises(ValueError):
            MindlogicStudyNotes(self.settings(mindlogic_base_url="https://example.org/"))

    def test_successive_calls_do_not_share_private_text_or_mask_maps(self):
        def handler(request):
            _, data, rows = read_request(request)
            return response(echo_document(data, rows))
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            engine = MindlogicStudyNotes(self.settings(), client)
            first = engine.create(language="ko", segments=[source("값 15입니다.", "first")])
            second = engine.create(language="ko", segments=[source("값 20입니다.", "second")])
        self.assertEqual(first.paragraphs[0]["text"], "값 15입니다.")
        self.assertEqual(second.paragraphs[0]["text"], "값 20입니다.")
        self.assertNotIn("15", json.dumps(second.to_dict()))

    def test_live_smoke_default_never_reads_configuration_or_creates_http_client(self):
        stdout = io.StringIO()
        with patch.object(Settings, "from_env", side_effect=AssertionError("no configuration")), \
                patch("scripts.validate_study_note_llm.httpx.Client", side_effect=AssertionError("no HTTP")), \
                redirect_stdout(stdout):
            self.assertEqual(validate_study_note_llm.main([]), 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["http_attempts"], 0)
        self.assertEqual(report["status"], "not_run")

    def test_live_smoke_enforces_two_attempts_exact_host_and_no_deadline_overrun(self):
        budget = validate_study_note_llm.AttemptBudget()
        target = "https://factchat-cloud.mindlogic.ai/v1/gateway/chat/completions/"
        with self.assertRaises(StudyNoteError): budget.check_request(httpx.Request("POST", "https://example.org/"))
        self.assertEqual(budget.attempts, 0)
        budget.check_request(httpx.Request("POST", target))
        budget.check_request(httpx.Request("POST", target))
        with self.assertRaises(StudyNoteError): budget.check_request(httpx.Request("POST", target))
        self.assertEqual(budget.attempts, 2)
        expired = validate_study_note_llm.AttemptBudget(); expired.deadline = -1
        with self.assertRaises(StudyNoteError): expired.check_request(httpx.Request("POST", target))
        self.assertEqual(expired.attempts, 0)

    def test_live_smoke_fixed_checks_distinguish_unchanged_english_from_korean_drafts(self):
        bodies = ["강둑이 손상되었습니다. 수위가 15센티미터 높아져도 흐름이 빨라진다는 뜻은 아닙니다. 수로가 넓어지면 느려질 수 있습니다.",
                  "배치 정규화는 작은 묶음 안에서 정규화합니다. 표본이 모두 동일해지는 것은 아닙니다. 표본은 16개입니다."]
        for case, body in zip(validate_study_note_llm.CASES, bodies, strict=True):
            raw = validate_study_note_llm.case_segments(case)
            unchanged = raw_document(raw)
            self.assertFalse(all(validate_study_note_llm.quality_checks(case, unchanged).values()))
            document = raw_document(raw); document["paragraphs"][0]["text"] = body
            if case["id"] == "hangul_phonetic_context":
                document["paragraphs"][0]["edits"] = [{"original": "배치 놀말라이제이션", "replacement": "배치 정규화", "uncertain": True}]
            self.assertTrue(all(validate_study_note_llm.quality_checks(case, document).values()))

    def test_live_smoke_setup_exceptions_are_redacted(self):
        stdout = io.StringIO()
        with patch.object(Settings, "from_env", side_effect=RuntimeError("private-configuration-sentinel")), redirect_stdout(stdout):
            self.assertEqual(validate_study_note_llm.main(["--live"]), 1)
        self.assertNotIn("private-configuration-sentinel", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["code"], "validation_setup_or_cleanup_failed")


if __name__ == "__main__":
    unittest.main()
