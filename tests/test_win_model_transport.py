"""Native loopback protocol tests use synthetic PCM/text, never service data."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import numpy as np
import uvicorn
from fastapi.testclient import TestClient

from server.model_process import ModelProcessError
from server.model_protocol import (MAX_REQUEST_BYTES, ModelUnavailableError, dump_json,
                                   make_request, read_result)
from server.model_server import create_model_app
from server.remote_transcriber import RemoteTranscriber
from server.win_model_transport import (AUTH_HEADER, RESPONSE_HEADER, LoopbackSecurityMiddleware,
                                       request_auth, verify_response)
from tests.test_model_transport import FakeEngine, settings, until

TOKEN = "a" * 64
INSTANCE = "b" * 32
PORT = 18765
ENDPOINT = {"version": 1, "host": "127.0.0.1", "port": PORT, "instance": INSTANCE,
            "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest()}


def protected_app(engine, *, warmup=False, shutdown=None, port=PORT):
    app = create_model_app(settings(model_warmup=warmup), engine, shutdown=shutdown)
    app.add_middleware(LoopbackSecurityMiddleware, token=TOKEN, instance=INSTANCE, port=port)
    return app


class LoopbackAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.shutdowns = []
        self.client = TestClient(protected_app(self.engine, shutdown=lambda: self.shutdowns.append(True)),
                                 base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 51234))
        self.client.__enter__()

    def tearDown(self):
        self.engine.release.set()
        self.client.__exit__(None, None, None)

    def signed(self, method, path, *, body=b"", **kwargs):
        auth, nonce = request_auth(TOKEN, INSTANCE, method, path, body)
        headers = {AUTH_HEADER: auth, "Content-Type": "application/json"}
        headers.update(kwargs.pop("headers", {}))
        response = self.client.request(method, path, content=body, headers=headers, **kwargs)
        return response, nonce

    def test_no_endpoint_accepts_an_unauthenticated_request(self):
        for method, path in (("GET", "/health"), ("GET", "/status"), ("POST", "/transcribe"), ("POST", "/shutdown")):
            with self.subTest(path=path):
                response = self.client.request(method, path)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn(TOKEN, response.text)
                self.assertNotIn("access-control-allow-origin", response.headers)
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.shutdowns, [])

    def test_authorized_health_and_responses_are_mutually_authenticated(self):
        response, nonce = self.signed("GET", "/health")
        self.assertEqual(response.status_code, 200)
        verify_response(TOKEN, ENDPOINT, nonce, response, response.content)
        self.assertEqual(response.json()["status"], "ok")
        self.assertNotIn(TOKEN, response.text + str(response.headers))
        for content in (b'{"status":"forged"}', response.content + b" "):
            with self.assertRaises(ModelUnavailableError):
                verify_response(TOKEN, ENDPOINT, nonce, response, content)
        with self.assertRaises(ModelUnavailableError):
            verify_response(TOKEN, ENDPOINT, nonce, httpx.Response(200), response.content)
        malformed_header = httpx.Response(200, headers=[(RESPONSE_HEADER.encode(), b"\xff" * 64)])
        with self.assertRaises(ModelUnavailableError):
            verify_response(TOKEN, ENDPOINT, nonce, malformed_header, response.content)

    def test_browser_headers_and_dns_rebinding_hosts_are_denied_even_with_valid_signature(self):
        for headers in ({"Origin": "http://127.0.0.1:8765"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "same-origin"}, {"Referer": "http://127.0.0.1/"},
                        {"Host": "localhost:18765"}, {"Host": "attacker.invalid:18765"},
                        {"X-Forwarded-For": "127.0.0.1"}, {"Forwarded": "for=127.0.0.1"}):
            with self.subTest(header=next(iter(headers))):
                response, _ = self.signed("GET", "/health", headers=headers)
                self.assertEqual(response.status_code, 403)
        auth, _ = request_auth(TOKEN, INSTANCE, "GET", "/health")
        with TestClient(protected_app(FakeEngine()), base_url=f"http://127.0.0.1:{PORT}",
                        client=("192.0.2.1", 50000)) as client:
            self.assertEqual(client.get("/health", headers={AUTH_HEADER: auth}).status_code, 403)

    def test_replayed_expired_wrong_key_and_previous_instance_requests_are_denied(self):
        auth, _ = request_auth(TOKEN, INSTANCE, "GET", "/health")
        self.assertEqual(self.client.get("/health", headers={AUTH_HEADER: auth}).status_code, 200)
        self.assertEqual(self.client.get("/health", headers={AUTH_HEADER: auth}).status_code, 401)
        for token, instance, stamp in (("c" * 64, INSTANCE, time.time()),
                                       (TOKEN, "d" * 32, time.time()), (TOKEN, INSTANCE, time.time() - 120)):
            invalid, _ = request_auth(token, instance, "GET", "/health", now=stamp)
            self.assertEqual(self.client.get("/health", headers={AUTH_HEADER: invalid}).status_code, 401)

    def test_body_tampering_and_over_limit_requests_never_reach_inference(self):
        auth, _ = request_auth(TOKEN, INSTANCE, "POST", "/transcribe", b"{}")
        response = self.client.post("/transcribe", content=b'{"changed":true}',
                                    headers={AUTH_HEADER: auth, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 401)
        response, _ = self.signed("POST", "/transcribe", body=b"x" * (MAX_REQUEST_BYTES + 1))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.engine.calls, [])

    def test_synthetic_pcm_context_and_last_chunk_pass_without_mutation(self):
        import uuid
        samples = np.array([-.75, .75, -0.0, 1e-38] * 4000, np.float32)
        context = {"version": 1, "audio_end": 8, "tokens": []}
        request_id = str(uuid.uuid4())
        value = make_request(samples, "ko", .5, True, start_seconds=7.5,
                             boundary_context=context, boundary_requested=True, request_id=request_id)
        body = dump_json(value, MAX_REQUEST_BYTES)
        response, nonce = self.signed("POST", "/transcribe", body=body)
        self.assertEqual(response.status_code, 200)
        verify_response(TOKEN, ENDPOINT, nonce, response, response.content)
        result, output = read_result(response.json(), request_id, len(samples))
        self.assertEqual(self.engine.calls[0][0].tobytes(), samples.tobytes())
        self.assertTrue(self.engine.calls[0][3])
        self.assertEqual(context, {"version": 1, "audio_end": 8, "tokens": []})
        self.assertEqual(output["audio_end"], 8.5)
        self.assertEqual(result[0]["text"], " 끝! \n")

    def test_shutdown_is_explicit_and_authenticated(self):
        response, nonce = self.signed("POST", "/shutdown")
        self.assertEqual(response.status_code, 200)
        verify_response(TOKEN, ENDPOINT, nonce, response, response.content)
        self.assertEqual(self.shutdowns, [True])


@contextmanager
def live_loopback(engine, *, warmup=False):
    """Exercise real TCP plus HMAC; native ACL file integration is separate."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    endpoint = {**ENDPOINT, "port": port}
    app = protected_app(engine, warmup=warmup, port=port)
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical", proxy_headers=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    until(lambda: server.started)
    runtime = Path(__file__).resolve().parent / "synthetic-runtime-does-not-exist"
    with patch("server.win_model_transport.read_endpoint", return_value=(endpoint, TOKEN)):
        remote = RemoteTranscriber(settings(local_model_runtime=runtime))
        try:
            yield remote, server, runtime
        finally:
            engine.release.set()
            engine.warmup_release.set()
            remote.close()
            server.should_exit = True
            thread.join(5)
            listener.close()
            if thread.is_alive():
                raise AssertionError("Synthetic model server did not stop")


class LiveLoopbackTests(unittest.TestCase):
    def test_actual_listener_uses_only_ipv4_loopback_and_server_signed_responses(self):
        engine = FakeEngine()
        with live_loopback(engine) as (remote, _, _):
            with patch.dict(os.environ, {"HTTP_PROXY": "http://invalid.invalid:1"}):
                result = remote.transcribe(np.zeros(16000, np.float32), "en")
            self.assertEqual(result[0]["text"], " 끝! \n")
            self.assertEqual(len(engine.calls), 1)

    def test_busy_timeout_and_retry_keep_exactly_one_gpu_operation(self):
        engine = FakeEngine()
        engine.block = True
        with live_loopback(engine, warmup=True) as (remote, _, _):
            until(lambda: remote.status()["model_state"] == "ready")
            remote._timeout = .05
            output = {"committed": True}
            with self.assertRaises(ModelUnavailableError) as failure:
                remote.transcribe(np.zeros(16000, np.float32), "ko", boundary_output=output)
            self.assertEqual(failure.exception.code, "model_timeout")
            self.assertEqual(output, {"committed": True})
            with self.assertRaises(ModelUnavailableError) as failure:
                remote.transcribe(np.zeros(16000, np.float32), "ko")
            self.assertEqual(failure.exception.code, "model_busy")
            self.assertEqual(len(engine.calls), 1)
            self.assertEqual(engine.maximum_active, 1)
            engine.release.set()

    def test_replacing_api_adapter_keeps_model_warmup_and_single_worker(self):
        engine = FakeEngine()
        with live_loopback(engine, warmup=True) as (remote, _, runtime):
            until(lambda: remote.status()["model_state"] == "ready")
            remote.transcribe(np.zeros(16000, np.float32), "ko")
            remote.close()
            replacement = RemoteTranscriber(settings(local_model_runtime=runtime))
            try:
                replacement.transcribe(np.zeros(16000, np.float32), "en")
                self.assertEqual(engine.warmups, 1)
                self.assertEqual(len(engine.calls), 2)
                self.assertEqual(len(set(engine.thread_ids)), 1)
            finally:
                replacement.close()

    def test_malformed_result_preserves_committed_context_and_closed_client_stays_closed(self):
        engine = FakeEngine()
        engine.invalid = True
        with live_loopback(engine) as (remote, _, _):
            output = {"keep": True}
            with self.assertRaises(ModelUnavailableError) as failure:
                remote.transcribe(np.zeros(16000, np.float32), "ko", boundary_output=output)
            self.assertEqual(failure.exception.code, "model_protocol_error")
            self.assertEqual(output, {"keep": True})
            remote.close()
            with self.assertRaises(ModelUnavailableError):
                remote.transcribe(np.zeros(16000, np.float32), "ko")
            self.assertEqual(len(engine.calls), 1)


@unittest.skipUnless(os.name == "nt", "Windows kernel process handles")
class WindowsProcessHandleTests(unittest.TestCase):
    def test_process_identity_and_wrong_creation_time_cannot_kill_another_process(self):
        from server.win_model_process import ProcessHandle, process_identity
        executable = process_identity(os.getpid())["exe"]
        child = subprocess.Popen([executable, "-c", "import time; time.sleep(30)"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with ProcessHandle(child.pid, terminate=True) as handle:
                identity = handle.identity()
                self.assertEqual(identity["pid"], child.pid)
                self.assertFalse(handle.wait())
                with self.assertRaises(ModelProcessError):
                    handle.terminate({**identity, "created": str(int(identity["created"]) + 1)})
                self.assertIsNone(child.poll())
                handle.terminate(identity)
                self.assertTrue(handle.wait(3))
            child.wait(timeout=3)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)

    def test_handle_remains_bound_to_original_process_after_exit(self):
        from server.win_model_process import ProcessHandle, process_identity
        child = subprocess.Popen([process_identity(os.getpid())["exe"], "-c", "import time; time.sleep(.3)"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        with ProcessHandle(child.pid, terminate=True) as handle:
            self.assertIsNotNone(handle.identity())
            child.wait(timeout=3)
            self.assertTrue(handle.wait(1))
            self.assertIsNone(handle.identity())
            handle.terminate({"pid": child.pid, "created": "unrelated", "exe": "unrelated"})


if __name__ == "__main__":
    unittest.main()
