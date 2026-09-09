"""Strict JSON decoding and the shared conservative NOVA schema subset.

This module never logs provider bodies, source text, identifiers, or credentials.
Schema size constraints omitted on the wire remain mandatory in each feature's
local validator; removing a wire keyword does not accept incomplete output.
"""
from __future__ import annotations

import copy
import json
import math
from typing import Any


class ProtocolError(ValueError):
    def __init__(self, code: str = "invalid_response"):
        self.code = code if code in {"invalid_response", "response_truncated", "model_refused"} else "invalid_response"
        super().__init__(self.code)


def gateway_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Use the same wire subset already required by the NOVA question client.

Only schema nodes are visited: field names such as 'maxLength' in properties or
data in enums/defaults are not themselves schema keywords and must survive.
"""
    result = copy.deepcopy(schema)

    def visit(node):
        if not isinstance(node, dict):
            return
        for key in ("minLength", "maxLength", "minItems", "maxItems", "uniqueItems"):
            node.pop(key, None)
        for key in ("properties", "$defs", "definitions", "patternProperties"):
            values = node.get(key)
            if isinstance(values, dict):
                for child in values.values():
                    visit(child)
        for key in ("items", "additionalProperties", "contains", "not", "if", "then", "else"):
            child = node.get(key)
            if isinstance(child, list):
                for item in child:
                    visit(item)
            else:
                visit(child)
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            for child in node.get(key, []) if isinstance(node.get(key), list) else []:
                visit(child)

    visit(result)
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError()
        result[key] = value
    return result


def _reject_constant(_):
    raise ProtocolError()


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ProtocolError()
    return value


def _check_nesting(content: str):
    # Some libraries raise Python's global recursion limit. Do not depend on
    # that process-wide setting to reject hostile deeply nested JSON safely.
    depth, quoted, escaped = 0, False, False
    for char in content:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            depth += 1
            if depth > 64:
                raise ProtocolError()
        elif char in "}]":
            depth -= 1
            if depth < 0:
                raise ProtocolError()


def parse_json_document(response: Any) -> dict[str, Any]:
    """Reject truncated/refused/multiple results before parsing any content.

Missing finish_reason is retained for the existing gateway compatibility
contract. Code fences, trailing prose, duplicate keys and partial JSON are not
repaired. Feature-specific exact IDs, sizes, numbers and citations are checked
by the caller after this syntactic validation.
"""
    try:
        choices = response["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProtocolError()
        choice = choices[0]
        message = choice["message"]
        if not isinstance(message, dict):
            raise ProtocolError()
        # Refusal takes precedence; never split/retry a refused request.
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise ProtocolError("model_refused")
        if message.get("tool_calls") or message.get("function_call"):
            raise ProtocolError()
        if choice.get("finish_reason") == "length":
            raise ProtocolError("response_truncated")
        if choice.get("finish_reason") not in {None, "stop"}:
            raise ProtocolError()
        content = message["content"]
        if not isinstance(content, str) or len(content) > 2_000_000:
            raise ProtocolError()
        _check_nesting(content)
        document = json.loads(content, object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant, parse_float=_finite_float)
        if not isinstance(document, dict):
            raise ProtocolError()
        return document
    except ProtocolError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, RecursionError):
        raise ProtocolError() from None
