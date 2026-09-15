"""Authenticated, bounded loopback transport; credentials never travel on the wire.

Only native server clients without browser headers are admitted. HMAC covers
request method/path/body and every response, so a reused port cannot impersonate
the model. Runtime files remain private to the OS account.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path

from .model_protocol import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, ModelUnavailableError
from .platform_files import validate_private_path

AUTH_HEADER = "x-yeobaek-model-auth"
RESPONSE_HEADER = "x-yeobaek-model-response"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")


def read_endpoint(runtime):
    try:
        runtime = Path(runtime)
        if not runtime.is_absolute() or ".." in runtime.parts:
            raise ValueError
        validate_private_path(runtime, directory=True)
        endpoint, credential = runtime / "endpoint.json", runtime / "auth.token"
        for path in (endpoint, credential):
            validate_private_path(path)
            if path.stat().st_size > 8192:
                raise ValueError
        value = json.loads(endpoint.read_text(encoding="utf-8"))
        token = credential.read_text(encoding="ascii")
        if (set(value) != {"version", "host", "port", "instance", "token_sha256"}
                or type(value["version"]) is not int or value["version"] != 1 or value["host"] != "127.0.0.1"
                or type(value["port"]) is not int or not 1024 <= value["port"] <= 65535
                or not isinstance(value["instance"], str) or not HEX32.fullmatch(value["instance"])
                or not HEX64.fullmatch(token)
                or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), value["token_sha256"])):
            raise ValueError
        return value, token
    except (OSError, ValueError, TypeError, KeyError):
        raise ModelUnavailableError() from None


def request_auth(token, instance, method, path, body=b"", *, now=None, nonce=None):
    stamp = str(int(time.time() if now is None else now))
    nonce = nonce or secrets.token_hex(16)
    digest = hashlib.sha256(body or b"").hexdigest()
    message = "\n".join((instance, method, path, stamp, nonce, digest))
    signature = hmac.new(token.encode("ascii"), message.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{stamp}:{nonce}:{digest}:{signature}", nonce


def response_auth(token, instance, nonce, status, body):
    message = f"{instance}\n{nonce}\n{status}\n{hashlib.sha256(body).hexdigest()}"
    return hmac.new(token.encode("ascii"), message.encode("ascii"), hashlib.sha256).hexdigest()


def verify_response(token, endpoint, nonce, response, body):
    actual = response.headers.get(RESPONSE_HEADER, "")
    expected = response_auth(token, endpoint["instance"], nonce, response.status_code, body)
    if not isinstance(actual, str) or not HEX64.fullmatch(actual) or not hmac.compare_digest(actual, expected):
        raise ModelUnavailableError("model_protocol_error")


class LoopbackSecurityMiddleware:
    def __init__(self, app, *, token, instance, port):
        if not HEX64.fullmatch(token) or not HEX32.fullmatch(instance):
            raise ValueError("Invalid local model authentication configuration")
        self.app, self.token, self.instance = app, token, instance
        self.host = f"127.0.0.1:{port}".encode("ascii")
        self.seen = {}
        self.reading = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        async def reject(status=403, *, nonce=None):
            payload = b'{"code":"model_busy"}' if status == 429 else b'{"code":"model_unavailable"}'
            signed = ([(RESPONSE_HEADER.encode(), response_auth(
                self.token, self.instance, nonce, status, payload).encode())] if nonce else [])
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                                    (b"x-content-type-options", b"nosniff"),
                                    (b"content-length", str(len(payload)).encode()), *signed]})
            await send({"type": "http.response.body", "body": payload})

        headers = scope.get("headers", [])
        names = [key.lower() for key, _ in headers]
        values = {key.lower(): value for key, value in headers}
        if (not scope.get("client") or scope["client"][0] != "127.0.0.1"
                or values.get(b"host") != self.host
                or any(name in {b"origin", b"referer", b"forwarded", b"x-forwarded-for", b"x-forwarded-host"}
                       or name.startswith(b"sec-fetch-") for name in names)
                or names.count(b"host") != 1 or scope.get("query_string")
                or scope["method"] not in {"GET", "POST"}):
            return await reject()
        if names.count(AUTH_HEADER.encode()) != 1:
            return await reject(401)
        try:
            raw = values[AUTH_HEADER.encode()].decode("ascii")
            stamp, nonce, digest, signature = raw.split(":")
            now = time.time()
            if (not stamp.isdigit() or len(stamp) > 12 or abs(now - int(stamp)) > 60
                    or not HEX32.fullmatch(nonce) or not HEX64.fullmatch(digest) or not HEX64.fullmatch(signature)):
                return await reject(401)
            message = "\n".join((self.instance, scope["method"], scope["path"], stamp, nonce, digest))
            expected = hmac.new(self.token.encode(), message.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return await reject(401)
            self.seen = {key: expiry for key, expiry in self.seen.items() if expiry > now}
            if nonce in self.seen or len(self.seen) >= 4096:
                return await reject(401)
            self.seen[nonce] = now + 121
        except (ValueError, UnicodeError):
            return await reject(401)

        async def read():
            body = bytearray()
            while True:
                part = await receive()
                if part["type"] != "http.request":
                    raise ValueError
                body.extend(part.get("body", b""))
                if len(body) > (MAX_REQUEST_BYTES if scope["method"] == "POST" else 0):
                    raise ValueError
                if not part.get("more_body", False):
                    return bytes(body)
        reading_slot = scope["method"] == "POST"
        if reading_slot and self.reading >= 2:
            return await reject(429, nonce=nonce)
        if reading_slot:
            self.reading += 1
        try:
            try:
                body = await asyncio.wait_for(read(), timeout=5)
                if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), digest):
                    return await reject(401)
            except (ValueError, asyncio.TimeoutError):
                return await reject(413)
        finally:
            if reading_slot:
                self.reading -= 1
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        started, parts, size = None, [], 0

        async def capture(message):
            nonlocal started, size
            if message["type"] == "http.response.start":
                started = dict(message)
            elif message["type"] == "http.response.body":
                payload = message.get("body", b"")
                size += len(payload)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError("Local model response exceeded limit")
                parts.append(payload)
                if not message.get("more_body", False):
                    payload = b"".join(parts)
                    signed = response_auth(self.token, self.instance, nonce, started["status"], payload)
                    started["headers"] = [(key, value) for key, value in started.get("headers", [])
                                          if key.lower() not in {b"content-length", RESPONSE_HEADER.encode()}]
                    started["headers"].extend([(b"content-length", str(len(payload)).encode()),
                                                (RESPONSE_HEADER.encode(), signed.encode()),
                                                (b"cache-control", b"no-store")])
                    await send(started)
                    await send({"type": "http.response.body", "body": payload})
        await self.app(scope, replay, capture)
