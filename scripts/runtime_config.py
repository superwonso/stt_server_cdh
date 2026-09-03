#!/usr/bin/env python3
"""Validate and render the public, secret-free Pages runtime configuration."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


VERSION = 1
OFFLINE = "OFFLINE"
ONLINE_LIFETIME = timedelta(hours=24)
QUICK_TUNNEL_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
CONFIG_KEYS = frozenset({"version", "state", "apiUrl", "publishedAt", "expiresAt"})


def validate_value(value: str) -> tuple[str, str]:
    """Return the public state and URL represented by operator input."""
    if not isinstance(value, str):
        raise ValueError("runtime API state must be text")
    if value == OFFLINE or value == "":
        return "offline", ""
    if any(character.isspace() for character in value):
        raise ValueError("runtime API URL must not contain whitespace")

    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("runtime API URL has an invalid port") from error
    hostname = parsed.hostname or ""
    suffix = ".trycloudflare.com"
    label = hostname.removesuffix(suffix)
    valid = (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and hostname.endswith(suffix)
        and "." not in label
        and QUICK_TUNNEL_LABEL.fullmatch(label) is not None
        and port is None
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    )
    if not valid:
        raise ValueError(
            "runtime API URL must be an HTTPS single-label *.trycloudflare.com origin"
        )
    return "online", f"https://{hostname}"


def utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def runtime_config(value: str, now: datetime | None = None) -> dict[str, object]:
    state, api_url = validate_value(value)
    published = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expires = published + ONLINE_LIFETIME if state == "online" else published
    return {
        "version": VERSION,
        "state": state,
        "apiUrl": api_url,
        "publishedAt": utc_timestamp(published),
        "expiresAt": utc_timestamp(expires),
    }


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise ValueError("runtime timestamp is invalid")
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def validate_document(document: object, now: datetime | None = None) -> dict[str, object]:
    """Validate the exact public schema without renewing its original lease."""
    if not isinstance(document, dict) or frozenset(document) != CONFIG_KEYS:
        raise ValueError("runtime configuration has unexpected fields")
    if type(document.get("version")) is not int or document["version"] != VERSION:
        raise ValueError("runtime configuration version is invalid")
    if document.get("state") not in {"online", "offline"}:
        raise ValueError("runtime configuration state is invalid")
    if not isinstance(document.get("apiUrl"), str):
        raise ValueError("runtime configuration URL is invalid")

    published = parse_timestamp(document.get("publishedAt"))
    expires = parse_timestamp(document.get("expiresAt"))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if published > current + timedelta(minutes=5):
        raise ValueError("runtime publication time is too far in the future")

    if document["state"] == "online":
        state, normalized = validate_value(document["apiUrl"])
        if state != "online" or normalized != document["apiUrl"]:
            raise ValueError("runtime configuration URL is not canonical")
        if expires != published + ONLINE_LIFETIME:
            raise ValueError("runtime configuration lease is invalid")
    elif document["apiUrl"] != "" or expires != published:
        raise ValueError("offline runtime configuration is invalid")
    return document


def parse_document(payload: bytes) -> dict[str, object]:
    if len(payload) > 4096:
        raise ValueError("runtime configuration is too large")
    document = json.loads(payload)
    return validate_document(document)


def matches_config(document: object, expected: object) -> bool:
    """Require the deployed document to equal the atomic desired state."""
    try:
        actual = validate_document(document)
        desired = validate_document(expected)
        if desired["state"] == "online" and parse_timestamp(desired["expiresAt"]) <= datetime.now(timezone.utc):
            return False
        return actual == desired
    except (TypeError, ValueError):
        return False


def matches_value(document: object, expected: str) -> bool:
    try:
        desired = validate_document(document)
        state, api_url = validate_value(expected)
        return desired["state"] == state and desired["apiUrl"] == api_url
    except (TypeError, ValueError):
        return False


def read_document() -> dict[str, object]:
    payload = sys.stdin.buffer.read(4097)
    return parse_document(payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate")
    validate.add_argument("value")
    subcommands.add_parser("desired")
    render = subcommands.add_parser("render")
    render.add_argument("output", type=Path)
    subcommands.add_parser("matches")
    subcommands.add_parser("matches-value")
    arguments = parser.parse_args()

    try:
        if arguments.command == "validate":
            validate_value(arguments.value)
            return 0
        if arguments.command == "desired":
            value = sys.stdin.buffer.read(513)
            if len(value) > 512:
                raise ValueError("runtime API URL is too large")
            document = runtime_config(value.decode("utf-8"))
            print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
            return 0
        if arguments.command == "render":
            desired = os.environ.get("CLASSROOM_API_CONFIG", "")
            document = parse_document(desired.encode("utf-8")) if desired else runtime_config("")
            arguments.output.write_text(
                json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            return 0
        if arguments.command == "matches-value":
            document = read_document()
            return 0 if matches_value(document, os.environ.get("CLASSROOM_API_EXPECTED_VALUE", "")) else 1
        document = read_document()
        expected = parse_document(os.environ.get("CLASSROOM_API_EXPECTED", "").encode("utf-8"))
        return 0 if matches_config(document, expected) else 1
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        print(f"runtime config error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
