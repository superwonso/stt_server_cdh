import copy
import json
import unittest

from server.llm_protocol import ProtocolError, gateway_schema, parse_json_document


def envelope(content='{"ok":true}', finish="stop", **message):
    return {"choices": [{"finish_reason": finish, "message": {"content": content, **message}}]}


class LlmProtocolTests(unittest.TestCase):
    def test_complete_json_and_legacy_missing_finish_reason(self):
        self.assertEqual(parse_json_document(envelope()), {"ok": True})
        value = envelope()
        del value["choices"][0]["finish_reason"]
        self.assertEqual(parse_json_document(value), {"ok": True})

    def test_truncation_never_accepts_even_syntactically_complete_document(self):
        for content in ('{"ok":true}', '{"ok":', None):
            with self.subTest(content=content), self.assertRaises(ProtocolError) as error:
                parse_json_document(envelope(content, "length"))
            self.assertEqual(error.exception.code, "response_truncated")

    def test_refusal_takes_precedence_and_never_discloses_its_body(self):
        for value in (envelope(refusal="private-provider-body"), envelope(finish="content_filter"),
                      envelope(finish="length", refusal="private-provider-body")):
            with self.assertRaises(ProtocolError) as error:
                parse_json_document(value)
            self.assertEqual(error.exception.code, "model_refused")
            self.assertNotIn("private-provider", str(error.exception))

    def test_invalid_json_is_not_repaired(self):
        for content in ('```json\n{"ok":true}\n```', '{"ok":true} trailing', '{"ok":true,"ok":false}',
                        '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '[1]', 'null', '', [], None):
            with self.subTest(content=content), self.assertRaises(ProtocolError) as error:
                parse_json_document(envelope(content))
            self.assertEqual(error.exception.code, "invalid_response")

    def test_incorrect_envelopes_and_tool_calls_rejected(self):
        for value in (None, [], {}, {"choices": []}, {"choices": [None]},
                      {"choices": [envelope()["choices"][0]] * 2},
                      envelope(finish="tool_calls"), envelope(tool_calls=[{"secret": "not-output"}]),
                      envelope(function_call={"name": "not-output"}), envelope(finish=[]),
                      envelope(finish="length",tool_calls=[{"name":"not-output"}]),
                      envelope(finish="length",function_call={"name":"not-output"})):
            with self.subTest(kind=type(value)), self.assertRaises(ProtocolError) as error:
                parse_json_document(value)
            self.assertEqual(error.exception.code, "invalid_response")

    def test_deep_json_is_a_redacted_protocol_error(self):
        with self.assertRaises(ProtocolError):
            parse_json_document(envelope('{"x":' * 2000 + '0' + '}' * 2000))
        literal = '[{' * 2000 + '"\\\\' + '}]' * 2000
        self.assertEqual(parse_json_document(envelope(json.dumps({"text":literal}))), {"text":literal})

    def test_content_size_guard_is_retained_before_json_decoding(self):
        with self.assertRaises(ProtocolError):
            parse_json_document(envelope(json.dumps({"text":"a"*2_000_000})))

    def test_gateway_schema_preserves_local_schema_and_strict_identity_contract(self):
        source = {"type":"object", "properties":{
            "segments":{"type":"array", "minItems":2,"maxItems":2,"uniqueItems":True,
                        "items":{"type":"object","properties":{
                            "id":{"type":"string","enum":["S000001","S000002"]},
                            "text":{"type":"string","minLength":1,"maxLength":100}},
                            "required":["id","text"],"additionalProperties":False}},
            "maxLength":{"type":"string","maxLength":42}},
            "required":["segments","maxLength"],"additionalProperties":False}
        original = copy.deepcopy(source)
        wire = gateway_schema(source)
        self.assertEqual(source, original)
        rows = wire["properties"]["segments"]
        self.assertNotIn("minItems", rows)
        self.assertNotIn("maxItems", rows)
        self.assertNotIn("uniqueItems", rows)
        self.assertEqual(rows["items"]["properties"]["id"]["enum"], ["S000001","S000002"])
        self.assertEqual(wire["properties"]["maxLength"], {"type":"string"})
        self.assertEqual(wire["required"], ["segments","maxLength"])
        self.assertFalse(wire["additionalProperties"])

    def test_gateway_schema_only_transforms_schema_nodes_not_enum_data(self):
        source = {"anyOf":[{"$defs":{"A":{"type":"array","maxItems":1}},
                            "properties":{"data":{"enum":[{"maxLength":42}]}}}]}
        wire = gateway_schema(source)
        self.assertNotIn("maxItems", wire["anyOf"][0]["$defs"]["A"])
        self.assertEqual(wire["anyOf"][0]["properties"]["data"]["enum"], [{"maxLength":42}])


if __name__ == '__main__':
    unittest.main()
