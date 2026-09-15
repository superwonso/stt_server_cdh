import json
import unittest
from types import SimpleNamespace
from server.llm_result import (draft_document, draft_from_response, validate_draft_document,
                               safe_model_content, RESULT_WARNING)
from server.correction_result import completed_correction_result
from server.postprocessor import PostprocessingError


class ReceivedResultTests(unittest.TestCase):
    def response(self, text, finish="stop"):
        return {"choices":[{"finish_reason":finish,"message":{"role":"assistant","content":text}}]}

    def test_structural_and_content_failures_keep_text(self):
        for value in [
            {"overview":"시험관을 가열한다.","overview_source_ids":["unknown"]},
            {"paragraphs":[{"text":"결과는 1000이다.","source_ids":["invented"]}]},
            {"segments":[{"id":"unknown","text":"받은 번역"}]},
        ]:
            with self.subTest(value=list(value)):
                doc=draft_document(value)
                self.assertTrue(doc["text"])
                self.assertNotIn("unknown",doc["text"])
                self.assertNotIn("invented",doc["text"])
                self.assertEqual(validate_draft_document(doc),doc)

    def test_truncated_fenced_bom_and_plain_bodies(self):
        cases=['{"overview":"앞부분을 받았습니다.', '\ufeff{"text":"본문"}',
               '```json\n{"answer":"답변"}\n```', "형식에 맞추지 않은 실제 본문"]
        for content in cases:
            self.assertTrue(draft_from_response(self.response(content,"length"))["text"])

    def test_multiple_choice_bodies_are_preserved(self):
        value={"choices":[{"message":{"content":json.dumps({"text":"첫째"})}},
                          {"message":{"content":json.dumps({"text":"둘째"})}}]}
        doc=draft_from_response(value)
        self.assertIn("첫째",doc["text"]);self.assertIn("둘째",doc["text"])

    def test_diagnostics_and_hidden_reasoning_are_not_results(self):
        for value in [{"error":{"text":"secret-error"}},
                      {"metadata":{"content":"secret-metadata"}},
                      {"reasoning_content":"secret-reason"},
                      {"type":"reasoning","text":"secret-type"}]:
            with self.assertRaises(ValueError):draft_document(value)
        for value in [{"error":{"text":"secret"}},{"choices":[{"message":{"reasoning_content":"secret"}}]}]:
            with self.assertRaises(ValueError):draft_from_response(value)
        self.assertEqual(draft_document("<think>secret-reason</think>보이는 본문")["text"],"보이는 본문")

    def test_partial_metadata_and_typed_reasoning_are_excluded(self):
        for value in [
            '{"paragraphs":[{"text":"비공개","type":"reasoning"},{"text":"보이는 본문"}',
            '{"paragraphs":[{"type":"reasoning","text":"비공개"},{"text":"보이는 본문"}',
            '{"error":{"text":"비공개"},"text":"보이는 본문"',
        ]:
            body=draft_document(value)["text"]
            self.assertNotIn("비공개",body);self.assertIn("보이는 본문",body)

    def test_partial_unknown_subtrees_never_supply_answer_body(self):
        for field in ("api_key", "thinking", "authorization", "debug", "unknown"):
            text = '{"' + field + '":{"text":"synthetic-secret"},"answer":"visible"'
            self.assertEqual(draft_document(text)["text"], "visible")
        self.assertEqual(draft_document('{"paragraphs":[{"role":"analysis","text":"secret"},{"text":"visible"}')["text"], "visible")

    def test_correction_auxiliary_terms_cannot_expose_reasoning(self):
        output = SimpleNamespace(segments=[{"id":"real","text":"본문"}],
                                 uncertain_terms=["<think>secret</think>", "정상 용어"])
        result = completed_correction_result(output,[{"id":"real","start":1,"end":2,"text":"원문"}])
        self.assertNotIn("secret", str(result))
        self.assertEqual(result["uncertain_terms"], ["정상 용어", RESULT_WARNING])

    def test_only_local_known_protected_values_are_restored(self):
        doc=draft_document("__PRIVATE_000001__ __KOREAN_000002__ __PRIVATE_000003__",
                           replacements={"__PRIVATE_000001__":"합성 값","__KOREAN_000002__":"한국어"})
        self.assertEqual(doc["text"],"합성 값 한국어 [보호된 내용]")
        self.assertIn("placeholder_unresolved",doc["warnings"])

    def test_saved_warning_and_size_envelope_is_strict(self):
        for document in [{"format":"draft","text":"text","warnings":["provider-secret"]},
                         {"format":"draft","text":"text","warnings":["validation_failed"],"source_ids":["fake"]},
                         {"format":"draft","text":"","warnings":["validation_failed"]}]:
            with self.assertRaises(ValueError):validate_draft_document(document)
        doc=draft_document("가"*100,max_chars=100,max_bytes=180)
        self.assertLessEqual(len(json.dumps(doc,ensure_ascii=False,separators=(",",":")).encode()),180)
        self.assertIn("content_limited",doc["warnings"])

    def test_correction_unmapped_text_survives_without_source_time(self):
        result=completed_correction_result(SimpleNamespace(segments=[{"id":"wrong","text":"받은 교정본문"}],uncertain_terms=[]),
            [{"id":"real","start":1,"end":2,"text":"원문"}])
        self.assertEqual(result["segments"],[])
        self.assertEqual(result["text"],"받은 교정본문")
        self.assertEqual(result["uncertain_terms"],[RESULT_WARNING])

    def test_correction_invalid_metadata_retains_mapped_body(self):
        result=completed_correction_result(SimpleNamespace(segments=[{"id":"real","text":"받은 교정본문"}],uncertain_terms=None),
            [{"id":"real","start":1,"end":2,"text":"원문"}])
        self.assertEqual(result["segments"][0]["start"],1)
        self.assertEqual(result["warnings"],["validation_failed"])

    def test_correction_absent_answer_is_not_a_success(self):
        with self.assertRaises(PostprocessingError):
            completed_correction_result(SimpleNamespace(segments=[],uncertain_terms=[]),
                                         [{"id":"real","start":1,"end":2,"text":"원문"}])
