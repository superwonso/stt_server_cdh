"""Best-effort study-note regressions; synthetic HTTP responses, no API calls."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from server import study_notes
from server.settings import Settings
from server.study_notes import MindlogicStudyNotes, StudyNoteError, study_note_markdown, validate_study_note_document


def source(text="식물이 빛을 이용하는 원리를 설명합니다.", identifier="synthetic-local-row", start=0):
    return {"id": identifier, "text": text, "start": start, "end": start + 1}


def payload(request):
    envelope = json.loads(request.content)
    return json.loads(envelope["messages"][1]["content"])


def response(content, finish="stop", **message):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {"content": content, **message}}]})


def paragraph(text="빛에너지로 양분을 만드는 광합성입니다.", ids=None):
    return {"heading": "광합성", "source_ids": ids if ids is not None else ["S000001"], "text": text, "edits": []}


class StudyNoteDraftTests(unittest.TestCase):
    def settings(self):
        return Settings(data_dir=Path(tempfile.gettempdir()) / "unused-note-draft-data",
                        model_cache_dir=Path(tempfile.gettempdir()) / "unused-note-draft-models",
                        mindlogic_api_key="synthetic-not-real-provider-key", correction_retry_base_seconds=0)

    def run_engine(self, handler, raw=None):
        raw = raw if raw is not None else [source()]
        before = copy.deepcopy(raw)
        calls = []

        def tracked(request):
            calls.append(request)
            return handler(request)

        with httpx.Client(transport=httpx.MockTransport(tracked)) as client:
            engine = MindlogicStudyNotes(self.settings(), client)
            self.assertEqual(engine._transport.max_retries, 0)
            output = engine.create(language="en", segments=raw).to_dict()
        self.assertEqual(raw, before)
        self.assertEqual(validate_study_note_document(output, raw), output)
        return output, calls

    def assert_draft(self, document, *warnings):
        self.assertEqual(set(document), {"format", "text", "warnings"})
        self.assertEqual(document["format"], "draft")
        self.assertTrue(document["text"].strip())
        self.assertEqual(len(set(document["warnings"])), len(document["warnings"]))
        for code in warnings:
            self.assertIn(code, document["warnings"])

    def test_plain_markdown_is_saved_as_literal_safe_draft_with_footer(self):
        content = '# 식물과 에너지\n\n**광합성**을 설명합니다.\n<script>alert("x")</script>\n[링크](https://synthetic.invalid)'
        output, calls = self.run_engine(lambda _: response(content))
        self.assert_draft(output, "invalid_response")
        self.assertEqual(output["text"], content)
        self.assertEqual(len(calls), 1)
        markdown = study_note_markdown(output, [source()])
        self.assertIn("**광합성**", markdown)
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("[링크](https://", markdown)
        body, footer = markdown.rsplit(study_notes.STUDY_NOTE_RESULT_WARNING, 1)
        self.assertIn("식물과 에너지", body)
        self.assertEqual(footer.strip(), "")
        self.assertTrue(markdown.rstrip().endswith(study_notes.STUDY_NOTE_RESULT_WARNING))
        self.assertEqual(markdown.count(study_notes.STUDY_NOTE_RESULT_WARNING), 1)

    def test_error_and_metadata_subtrees_are_not_returned_as_generated_notes(self):
        for key in ("error", "errors", "metadata", "reasoning_content", "Reasoning-Content", "traceback", "debug", "analysis", "thinking", "reasoning_details"):
            for partial in (False, True):
                with self.subTest(key=key, partial=partial):
                    content = json.dumps({key: {"text": "hidden-metadata-body"}})
                    if partial:
                        content = content[:-1]
                    with self.assertRaises(StudyNoteError):
                        self.run_engine(lambda _: response(content))

    def test_typed_hidden_body_is_removed_without_discarding_visible_siblings(self):
        for kind in ("reasoning", "analysis", "thinking", "error", "tool_result", "tool_calls", "metadata"):
            for partial in (False, True):
                for type_first in (False, True):
                    with self.subTest(kind=kind, partial=partial, type_first=type_first):
                        hidden = ({"type": kind, "text": "hidden-typed-body"} if type_first
                                  else {"text": "hidden-typed-body", "type": kind})
                        content = json.dumps({"content": [{"text": "앞부분을 보존합니다."}, hidden,
                                                          {"text": "마지막 부분을 보존합니다."}]}, ensure_ascii=False)
                        if partial:
                            content = content[:-1]
                        output, _ = self.run_engine(lambda _: response(content))
                        self.assert_draft(output, "invalid_response")
                        self.assertIn("앞부분을 보존합니다.", output["text"])
                        self.assertIn("마지막 부분을 보존합니다.", output["text"])
                        self.assertNotIn("hidden-typed-body", output["text"])

    def test_fenced_json_recovers_only_generated_paragraph_body(self):
        content = "```json\n" + json.dumps({"paragraphs": [paragraph()]}, ensure_ascii=False) + "\n```"
        output, calls = self.run_engine(lambda _: response(content))
        self.assert_draft(output, "invalid_response")
        self.assertIn("빛에너지로 양분을 만드는 광합성", output["text"])
        self.assertNotIn("S000001", output["text"])
        self.assertNotIn('"paragraphs"', output["text"])
        self.assertEqual(len(calls), 1)

    def test_duplicate_json_text_keys_preserve_both_received_bodies_without_mapping(self):
        content = '{"paragraphs":[{"text":"처음 받은 설명입니다.","text":"이어 받은 설명입니다."}]}'
        output, calls = self.run_engine(lambda _: response(content))
        self.assert_draft(output, "invalid_response")
        self.assertIn("처음 받은 설명입니다.", output["text"])
        self.assertIn("이어 받은 설명입니다.", output["text"])
        self.assertEqual(len(calls), 1)

    def test_partial_json_preserves_completed_text_and_unfinished_final_sentence(self):
        content = '{"paragraphs":[{"text":"완성된 설명입니다."},{"text":"마지막 문장은 여기까지'
        output, calls = self.run_engine(lambda _: response(content, finish="length"))
        self.assert_draft(output, "response_truncated", "invalid_response")
        self.assertIn("완성된 설명입니다.", output["text"])
        self.assertTrue(output["text"].endswith("마지막 문장은 여기까지"))
        self.assertEqual(len(calls), 1)

    def test_partial_json_unfinished_unicode_escape_keeps_valid_prefix(self):
        content = '{"text":"앞부분은 보관합니다.\\u12'
        output, _ = self.run_engine(lambda _: response(content, finish="length"))
        self.assert_draft(output, "response_truncated")
        self.assertEqual(output["text"], "앞부분은 보관합니다.")

    def test_malformed_metadata_subtrees_are_never_mistaken_for_a_body(self):
        cases = ('{"reasoning":{"text":"synthetic-private-reasoning"}',
                 '{"tool_calls":[{"arguments":{"text":"synthetic-private-tool"}}]',
                 '{"content":{"reasoning_content":{"markdown":"synthetic-private-reasoning"}}',
                 '{"source_ids":[{"text":"synthetic-private-source"}]',
                 '{"metadata":{"body":"synthetic-private-metadata"}',
                 '{"reasoning":{"text":"synthetic-private-reasoning"],"text":"unsafe-boundary"}',
                 '{"reasoning":{"text":"synthetic-private-reasoning","text":"synthetic-private-tail')
        for content in cases:
            with self.subTest(content=content):
                calls = []

                def handler(request):
                    calls.append(request)
                    return response(content)

                with self.assertRaises(StudyNoteError) as error:
                    self.run_engine(handler)
                self.assertEqual(error.exception.code, "invalid_response")
                self.assertNotIn("synthetic-private", str(error.exception))
                self.assertEqual(len(calls), 1)

    def test_partial_json_keeps_body_on_each_side_of_metadata_but_not_metadata_text(self):
        content = ('{"text":"앞부분 본문입니다.","reasoning":{"text":"synthetic-private-reasoning"},'
                   '"tool_calls":[{"arguments":{"text":"synthetic-private-tool"}}],'
                   '"paragraphs":[{"text":"뒷부분 본문입니다."},{"text":"미완성 끝 문장')
        output, calls = self.run_engine(lambda _: response(content, finish="length"))
        self.assert_draft(output, "response_truncated", "invalid_response")
        self.assertIn("앞부분 본문입니다.", output["text"])
        self.assertIn("뒷부분 본문입니다.", output["text"])
        self.assertTrue(output["text"].endswith("미완성 끝 문장"))
        self.assertNotIn("synthetic-private", output["text"])
        self.assertEqual(len(calls), 1)

    def test_partial_json_does_not_treat_escaped_key_like_prose_as_structure(self):
        content = ('{"text":"본문에서 \\\"reasoning\\\": {\\\"text\\\": \\\"예시\\\"}를 설명합니다.",'
                   '"reasoning":{"text":"synthetic-private-reasoning"}')
        output, calls = self.run_engine(lambda _: response(content, finish="length"))
        self.assert_draft(output, "response_truncated")
        self.assertIn('"reasoning": {"text": "예시"}', output["text"])
        self.assertNotIn("synthetic-private", output["text"])
        self.assertEqual(len(calls), 1)

    def test_prose_bracket_prefixes_are_not_discarded_as_failed_json(self):
        for content in ("[불명확] 남아 있는 본문입니다.", "{표기} 설명이 남아 있습니다."):
            with self.subTest(content=content):
                output, calls = self.run_engine(lambda _: response(content))
                self.assert_draft(output, "invalid_response")
                self.assertEqual(output["text"], content)
                self.assertEqual(len(calls), 1)

    def test_json_with_unescaped_newline_and_tab_preserves_readable_text(self):
        content = '{"text":"첫 문장입니다.\n두 번째 문장입니다.\t끝 부분입니다."}'
        output, calls = self.run_engine(lambda _: response(content))
        self.assert_draft(output, "invalid_response")
        self.assertIn("첫 문장입니다.", output["text"])
        self.assertIn("두 번째 문장입니다.", output["text"])
        self.assertIn("끝 부분입니다.", output["text"])
        self.assertEqual(len(calls), 1)

    def test_json_and_code_are_preserved_when_they_are_inside_a_body_field(self):
        for body in ('{"photosynthesis":true}', '```python\nprint("수업 예시")\n```',
                     '["분석", "설명"]'):
            with self.subTest(body=body):
                output, _ = self.run_engine(lambda _: response(json.dumps({"text": body}, ensure_ascii=False)))
                self.assert_draft(output, "invalid_response")
                self.assertEqual(output["text"], body)

    def test_complete_json_with_length_finish_is_preserved_as_warning_draft(self):
        content = json.dumps({"paragraphs": [paragraph()]}, ensure_ascii=False)
        output, calls = self.run_engine(lambda _: response(content, finish="length"))
        self.assert_draft(output, "response_truncated")
        self.assertIn(paragraph()["text"], output["text"])
        self.assertEqual(len(calls), 1)

    def test_unknown_duplicate_missing_and_reordered_source_ids_do_not_discard_body(self):
        raw = [source(identifier="private-first"), source(identifier="private-second", start=1)]
        variants = (["S000001", "outside-source"], ["S000001", "S000001"], ["S000001"],
                    ["S000002", "S000001"], [], "S000001")
        for ids in variants:
            with self.subTest(ids=ids):
                content = json.dumps({"paragraphs": [paragraph(ids=ids)]}, ensure_ascii=False)
                output, calls = self.run_engine(lambda _: response(content), raw)
                self.assert_draft(output, "invalid_response")
                self.assertIn(paragraph()["text"], output["text"])
                for identifier in ("S000001", "S000002", "outside-source", "private-first", "private-second"):
                    self.assertNotIn(identifier, output["text"])
                self.assertEqual(len(calls), 1)

    def test_malformed_edits_and_heading_types_preserve_readable_body(self):
        changes = ({"edits": {}}, {"edits": [{"uncertain": "false"}]}, {"heading": ["잘못된 제목"]},
                   {"edits": [{"original": "라이트", "replacement": "빛", "uncertain": "unknown"}]},
                   {"extra": {"reasoning": "must-not-be-shown"}}, {"source_ids": None})
        for change in changes:
            with self.subTest(change=change):
                item = paragraph(); item.update(change)
                output, _ = self.run_engine(lambda _: response(json.dumps({"paragraphs": [item]}, ensure_ascii=False)))
                self.assert_draft(output, "invalid_response")
                self.assertIn(paragraph()["text"], output["text"])
                self.assertNotIn("must-not-be-shown", output["text"])

    def test_empty_preferred_keys_do_not_hide_other_usable_body_fields(self):
        for document in ({"text": "", "markdown": "마크다운에 실제 본문이 있습니다."},
                         {"paragraphs": [], "text": "빈 문단 목록 뒤에도 본문이 있습니다."},
                         {"text": {}, "body": "다른 본문 필드에 내용이 있습니다."}):
            with self.subTest(document=document):
                output, _ = self.run_engine(lambda _: response(json.dumps(document, ensure_ascii=False)))
                self.assert_draft(output, "invalid_response")
                self.assertIn("본문", output["text"])

    def test_unknown_generated_body_schema_is_salvaged_without_metadata(self):
        document = {"lecture_summary": {"introduction": "첫 번째 수업 설명입니다.",
                                        "conclusion": "마지막 수업 설명입니다."},
                    "reasoning": "synthetic-private-reasoning", "source_ids": ["synthetic-private-source"],
                    "tool_calls": [{"function": {"arguments": "synthetic-private-tool"}}]}
        output, calls = self.run_engine(lambda _: response(json.dumps(document, ensure_ascii=False)))
        self.assert_draft(output, "invalid_response")
        self.assertIn("첫 번째 수업 설명입니다.", output["text"])
        self.assertIn("마지막 수업 설명입니다.", output["text"])
        self.assertNotIn("synthetic-private", output["text"])
        self.assertEqual(len(calls), 1)

    def test_huge_multibyte_output_is_bounded_without_discarding_everything(self):
        content = "가" * 400_000
        output, calls = self.run_engine(lambda _: response(content))
        self.assert_draft(output, "invalid_response", "content_limited")
        self.assertGreater(len(output["text"]), 300_000)
        self.assertLess(len(output["text"]), len(content))
        self.assertLessEqual(len(output["text"]), study_notes.MAX_DOCUMENT_TEXT_CHARS)
        self.assertLessEqual(len(json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode()),
                             study_notes.MAX_DOCUMENT_BYTES)
        self.assertEqual(len(calls), 1)

    def test_controls_and_surrogate_output_are_safely_limited(self):
        content = json.dumps({"text": "앞 문장\x00\x01\ud800 뒤 문장"}, ensure_ascii=True)
        output, calls = self.run_engine(lambda _: response(content))
        self.assert_draft(output, "invalid_response", "content_limited")
        self.assertIn("앞 문장", output["text"])
        self.assertIn("뒤 문장", output["text"])
        self.assertNotIn("\x00", output["text"])
        self.assertNotIn("\x01", output["text"])
        output["text"].encode("utf-8")
        self.assertEqual(len(calls), 1)

    def test_known_masks_restore_but_unknown_masks_are_marked_without_guessing(self):
        raw = [source("실험 값은 35이고 메일은 synthetic.learner@example.invalid입니다.", "secret-row", 93.25)]
        raw[0].update(username="private-student", title="private-title", audio="private-path")

        def handler(request):
            data = payload(request)
            masks = study_notes._PLACEHOLDER.findall(data["source_markdown"])
            self.assertGreaterEqual(len(masks), 2)
            return response("# 복원된 설명\n\n" + " / ".join(masks) + " / __PRIVATE_999999__")

        output, calls = self.run_engine(handler, raw)
        self.assert_draft(output, "invalid_response", "placeholder_unresolved")
        self.assertIn("35", output["text"])
        self.assertIn("synthetic.learner@example.invalid", output["text"])
        self.assertIn("[가려진 값 확인 필요]", output["text"])
        self.assertNotIn("__PRIVATE_", output["text"])
        sent = calls[0].content.decode()
        for private in ("secret-row", "private-student", "private-title", "private-path", "93.25", "synthetic.learner@example.invalid"):
            self.assertNotIn(private, sent)
        self.assertEqual(len(calls), 1)

    def test_first_batch_success_second_network_failure_preserves_first_only_and_no_retry(self):
        raw = [source(identifier=f"local-{index}", start=index) for index in range(65)]
        calls = []

        def handler(request):
            data = payload(request); calls.append(data)
            if len(calls) == 2:
                raise httpx.ConnectError("synthetic-private-provider-error", request=request)
            return response(json.dumps({"paragraphs": [paragraph("앞 요청에서 생성한 본문입니다.", data["target_source_ids"])]}, ensure_ascii=False))

        output, actual = self.run_engine(handler, raw)
        self.assert_draft(output, "gateway_unavailable", "incomplete_batches")
        self.assertIn("앞 요청에서 생성한 본문입니다.", output["text"])
        self.assertNotIn("synthetic-private-provider-error", output["text"])
        self.assertEqual(len(actual), 2)
        self.assertEqual([len(call["target_source_ids"]) for call in calls], [64, 1])
        self.assertEqual(calls[0]["source_markdown"], calls[1]["source_markdown"])

    def test_bad_format_first_batch_and_valid_second_batch_both_survive(self):
        raw = [source(identifier=f"local-{index}", start=index) for index in range(65)]
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                return response("첫 번째 요청의 자유 형식 본문입니다.")
            return response(json.dumps({"paragraphs": [paragraph("두 번째 요청의 정형 본문입니다.", payload(request)["target_source_ids"])]}, ensure_ascii=False))

        output, calls = self.run_engine(handler, raw)
        self.assert_draft(output, "invalid_response")
        self.assertIn("첫 번째 요청의 자유 형식 본문입니다.", output["text"])
        self.assertIn("두 번째 요청의 정형 본문입니다.", output["text"])
        self.assertEqual(len(calls), 2)

    def test_no_response_and_http_errors_still_fail_without_any_retry(self):
        for kind, expected in (("network", "gateway_unavailable"), (401, "authentication_failed"),
                               (402, "credit_exhausted"), (429, "rate_limited"), (503, "gateway_unavailable")):
            with self.subTest(kind=kind):
                calls = []

                def handler(request):
                    calls.append(request)
                    if kind == "network":
                        raise httpx.ReadTimeout("synthetic-private-network-message", request=request)
                    return httpx.Response(kind, json={"text": "synthetic-private-http-error"})

                with self.assertRaises(StudyNoteError) as error:
                    self.run_engine(handler)
                self.assertEqual(error.exception.code, expected)
                self.assertNotIn("synthetic-private", str(error.exception))
                self.assertEqual(len(calls), 1)

    def test_no_usable_body_or_refusal_metadata_alone_remains_failure(self):
        cases = [response(""), response("   "), response("{}"), response('{"paragraphs":[]}'),
                 response(None, refusal="synthetic-private-refusal"),
                 response(None, reasoning_content="synthetic-private-reasoning"),
                 response(None, tool_calls=[{"function": {"arguments": '{"text":"synthetic-private-tool"}'}}]),
                 response([{"type": "reasoning", "text": "synthetic-private-reasoning"}]),
                 response(json.dumps({"reasoning": "synthetic-private-reasoning", "role": "assistant",
                                      "model": "synthetic-private-model", "source_ids": ["synthetic-private-source"],
                                      "tool_calls": [{"arguments": "synthetic-private-tool"}]}))]
        for reply in cases:
            with self.subTest(reply=reply):
                calls = []

                def handler(request):
                    calls.append(request)
                    return reply

                with self.assertRaises(StudyNoteError) as error:
                    self.run_engine(handler)
                self.assertIn(error.exception.code, ("invalid_response", "model_refused"))
                self.assertNotIn("synthetic-private", str(error.exception))
                self.assertEqual(len(calls), 1)

    def test_explicit_refused_message_body_can_be_saved_but_refusal_metadata_cannot(self):
        output, _ = self.run_engine(lambda _: response("이미 작성된 설명은 남아 있습니다.", refusal="synthetic-private-refusal"))
        self.assert_draft(output, "model_refused")
        self.assertIn("이미 작성된 설명은 남아 있습니다.", output["text"])
        self.assertNotIn("synthetic-private-refusal", output["text"])

    def test_text_blocks_salvage_excludes_nontext_blocks_and_hidden_reasoning(self):
        content = [{"type": "text", "text": "허용된 설명 첫 부분입니다."},
                   {"type": "reasoning", "text": "synthetic-private-reasoning"},
                   {"type": "output_text", "text": "허용된 설명 끝 부분입니다."},
                   {"type": "image_url", "image_url": {"url": "https://synthetic.invalid/private"}}]
        output, calls = self.run_engine(lambda _: response(content, reasoning_content="synthetic-private-hidden"))
        self.assert_draft(output, "invalid_response")
        self.assertIn("허용된 설명 첫 부분입니다.", output["text"])
        self.assertIn("허용된 설명 끝 부분입니다.", output["text"])
        self.assertNotIn("synthetic-private", output["text"])
        self.assertNotIn("https://", output["text"])
        self.assertEqual(len(calls), 1)

    def test_malformed_content_block_types_do_not_discard_other_received_text(self):
        for wrong_type in ([], {}, None, 1, True):
            with self.subTest(wrong_type=wrong_type):
                content = [{"type": "text", "text": "앞에서 생성된 본문입니다."},
                           {"type": wrong_type, "text": "synthetic-private-invalid-block"},
                           {"type": "output_text", "text": "뒤에서 생성된 본문입니다."}]
                output, calls = self.run_engine(lambda _: response(content))
                self.assert_draft(output, "invalid_response")
                self.assertIn("앞에서 생성된 본문입니다.", output["text"])
                self.assertIn("뒤에서 생성된 본문입니다.", output["text"])
                self.assertNotIn("synthetic-private", output["text"])
                self.assertEqual(len(calls), 1)

    def test_later_batch_with_malformed_blocks_keeps_previous_and_current_bodies(self):
        raw = [source(identifier=f"local-{index}", start=index) for index in range(65)]
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                return response(json.dumps({"paragraphs": [paragraph("이전 배치 본문입니다.", payload(request)["target_source_ids"])]}, ensure_ascii=False))
            return response([{"type": {}, "text": "synthetic-private-invalid-block"},
                             {"type": "text", "text": "현재 배치 본문입니다."}])

        output, calls = self.run_engine(handler, raw)
        self.assert_draft(output, "invalid_response")
        self.assertIn("이전 배치 본문입니다.", output["text"])
        self.assertIn("현재 배치 본문입니다.", output["text"])
        self.assertNotIn("synthetic-private", output["text"])
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
