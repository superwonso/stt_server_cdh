from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import numpy as np
import uvicorn
from fastapi.testclient import TestClient

from server.model_protocol import (
    MAX_CONTEXT_BYTES, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, MAX_SAMPLES,
    ModelUnavailableError, ProtocolError, dump_json, load_json, make_request,
    make_result, read_request, read_result, safe_status,
)
from server.model_server import _log_model_failure, create_model_app
from server.remote_transcriber import RemoteTranscriber, validate_socket_path


def settings(**overrides):
    return SimpleNamespace(**{**dict(model="synthetic-qwen", device="cpu", model_warmup=False,
                                     local_model_socket=None, local_model_timeout_seconds=2), **overrides})


def request(samples=None, **overrides):
    value = make_request(np.zeros(16000, dtype=np.float32) if samples is None else samples,
                         "ko", 0, True, start_seconds=0, boundary_context=None,
                         boundary_requested=False, request_id=str(uuid.uuid4()))
    value.update(overrides)
    return value


def until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Synthetic model check did not finish")
        time.sleep(.005)


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.warmups = 0
        self.active = 0
        self.maximum_active = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.warmup_entered = threading.Event()
        self.warmup_release = threading.Event()
        self.block = False
        self.block_warmup = False
        self.fail = False
        self.invalid = False
        self.thread_ids = []

    def warmup(self):
        self.warmups += 1
        self.thread_ids.append(threading.get_ident())
        self.warmup_entered.set()
        if self.block_warmup and not self.warmup_release.wait(4):
            raise RuntimeError("Synthetic warmup wait expired")
        if self.fail:
            raise RuntimeError("private-path and credentials must not escape")

    def transcribe(self, samples, language, overlap_seconds=0, final_chunk=True, *,
                   start_seconds=0, boundary_context=None, boundary_output=None):
        self.active += 1
        self.thread_ids.append(threading.get_ident())
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            self.calls.append((samples.copy(), language, overlap_seconds, final_chunk,
                               start_seconds, copy.deepcopy(boundary_context)))
            self.entered.set()
            if self.block and not self.release.wait(4):
                raise RuntimeError("Synthetic inference wait expired")
            if self.fail:
                raise RuntimeError("private transcription and account must not escape")
            if boundary_output is not None:
                boundary_output.update({"version": 1, "audio_end": start_seconds + len(samples) / 16000,
                                        "tokens": [{"text": "끝!", "start": start_seconds,
                                                    "end": start_seconds + .01, "emitted": True}]})
            if boundary_context is not None:
                boundary_context["private_mutation"] = "must stay in worker copy"
            if self.invalid:
                return [{"start": 0, "end": 1, "text": "부분 결과"}, {"start": 0, "end": float("nan"), "text": "bad"}]
            return [{"start": 0.0, "end": len(samples) / 16000, "text": " 끝! \n"}]
        finally:
            self.active -= 1


@contextmanager
def real_server(engine, *, warmup=False):
    with tempfile.TemporaryDirectory(prefix="stt-model-test-") as directory:
        runtime = Path(directory)
        runtime.chmod(0o700)
        path = runtime / "model.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(8)
        app = create_model_app(settings(model_warmup=warmup), engine)
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical", lifespan="on"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        until(lambda: server.started)
        remote = RemoteTranscriber(settings(local_model_socket=path))
        try:
            yield remote, app, path
        finally:
            engine.release.set()
            engine.warmup_release.set()
            remote.close()
            server.should_exit = True
            thread.join(5)
            listener.close()
            if thread.is_alive():
                raise AssertionError("Synthetic model server did not stop")


@contextmanager
def mock_remote(handler):
    if os.name == "nt":
        # These tests exercise HTTP response validation independently of the
        # filesystem transport. Native ACL/loopback integration has its own
        # tests; do not try to fabricate a POSIX socket on Windows.
        path = Path(__file__).resolve().parent / "synthetic.sock"
        remote = RemoteTranscriber(settings(local_model_socket=path))
        remote._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://local-model")
        with patch("server.remote_transcriber.validate_socket_path", return_value=path):
            try:
                yield remote
            finally:
                remote.close()
        return
    with tempfile.TemporaryDirectory(prefix="stt-model-mock-") as directory:
        path = Path(directory) / "model.sock"
        path.parent.chmod(0o700)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        path.chmod(0o600)
        remote = RemoteTranscriber(settings(local_model_socket=path))
        remote._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://local-model")
        try:
            yield remote
        finally:
            remote.close()
            sock.close()


class ModelProtocolTests(unittest.TestCase):
    def test_float32_pcm_is_byte_exact_including_negative_zero_and_small_values(self):
        samples = np.array([-1, 1, -0.0, 0, .12345679, 1e-38], dtype=np.float32)
        decoded = read_request(request(samples))
        self.assertEqual(decoded["samples"].tobytes(), samples.tobytes())
        self.assertIsNot(decoded["samples"], samples)

    def test_context_copy_preserves_content_without_cross_request_mutation(self):
        context = {"version": 1, "tokens": [{"text": "합성 문맥", "start": 0, "end": .5, "emitted": True}]}
        value = make_request(np.zeros(16000, np.float32), None, 1, True, start_seconds=8,
                             boundary_context=context, boundary_requested=True, request_id=str(uuid.uuid4()))
        original = copy.deepcopy(context)
        context["tokens"][0]["text"] = "caller changed"
        self.assertEqual(value["boundary_context"], original)
        decoded = read_request(value)
        decoded["boundary_context"]["tokens"].clear()
        self.assertEqual(value["boundary_context"], original)

    def test_input_bounds_dtype_shape_and_finiteness(self):
        for samples in (np.array([], np.float32), np.zeros(MAX_SAMPLES + 1, np.float32),
                        np.zeros((2, 2), np.float32), np.zeros(2, np.float64),
                        np.array([np.nan], np.float32), np.array([np.inf], np.float32),
                        np.array([1.001], np.float32), [0.0]):
            with self.subTest(shape=getattr(samples, "shape", None)), self.assertRaises(ProtocolError):
                request(samples)
        self.assertEqual(len(read_request(request(np.zeros(MAX_SAMPLES, np.float32)))["samples"]), MAX_SAMPLES)

    def test_strict_metadata_unknown_fields_and_timeline(self):
        bad = ({"sample_rate": True}, {"sample_rate": 48000}, {"sample_count": True},
               {"version": True}, {"language": []}, {"language": "ja"}, {"final_chunk": 1},
               {"boundary_requested": "true"}, {"start_seconds": -1}, {"start_seconds": float("inf")},
               {"start_seconds": True}, {"overlap_seconds": 4}, {"overlap_seconds": 1.1},
               {"overlap_seconds": 1, "final_chunk": False}, {"encoding": "pcm16"},
               {"pcm": "not base64"}, {"sample_count": 15999}, {"request_id": "foreign"}, {"extra": "private"})
        for values in bad:
            with self.subTest(values=values), self.assertRaises(ProtocolError):
                read_request(request(**values))

    def test_context_bytes_depth_types_and_nan_are_bounded(self):
        deep = {}
        for _ in range(14):
            deep = {"next": deep}
        for context in ([], {"big": "a" * MAX_CONTEXT_BYTES}, deep, {"bad": float("nan")},
                        {"too_many": [0] * 8192}, {"not_json": object()}):
            with self.subTest(type=type(context)), self.assertRaises(ProtocolError):
                read_request(request(boundary_context=context, boundary_requested=True))

    def test_encoded_pcm_nan_is_rejected_even_with_matching_length(self):
        bad = np.zeros(16000, np.float32)
        bad[5] = float("nan")
        with self.assertRaises(ProtocolError):
            read_request(request(pcm=base64.b64encode(bad.tobytes()).decode()))

    def test_json_duplicate_keys_nan_and_size_are_rejected(self):
        for data in (b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff', b'[' * 2000, b'a' * (MAX_REQUEST_BYTES + 1)):
            with self.assertRaises(ProtocolError):
                load_json(data, MAX_REQUEST_BYTES)

    def test_result_validation_is_all_or_nothing_and_preserves_exact_text(self):
        rid = str(uuid.uuid4())
        segment = {"start": 0, "end": .1, "text": " 공백!\n"}
        value = make_result(rid, [segment], {}, 16000)
        result, output = read_result(value, rid, 16000)
        self.assertEqual(result, [segment])
        self.assertEqual(output, {})
        for bad in ({"start": -1}, {"end": 2}, {"end": float("nan")}, {"text": ""},
                    {"text": "x" * 65537}, {"text": "\ud800"}, {"account": "should-not-exist"}):
            damaged = {**value, "segments": [segment, {**segment, **bad}]}
            with self.subTest(bad=list(bad)), self.assertRaises(ProtocolError):
                read_result(damaged, rid, 16000)
        with self.assertRaises(ProtocolError):
            read_result(value, str(uuid.uuid4()), 16000)

    def test_result_count_and_total_size_are_bounded(self):
        rid = str(uuid.uuid4())
        with self.assertRaises(ProtocolError):
            make_result(rid, [{"start": 0, "end": 1, "text": "x"}] * 4097, {}, 16000)
        with self.assertRaises(ProtocolError):
            make_result(rid, [{"start": 0, "end": 1, "text": "x" * 60000}] * 5, {}, 16000)

    def test_safe_exception_and_status_never_reflect_arbitrary_keys_or_paths(self):
        self.assertEqual(ModelUnavailableError("private password").code, "model_unavailable")
        self.assertNotIn("private", str(ModelUnavailableError("private password")))
        result = safe_status({"model_state": [], "model": "/private/models/public-name", "key": "private"})
        self.assertEqual(result["model"], "public-name")
        self.assertNotIn("key", result)


class ModelServerTests(unittest.TestCase):
    def test_failure_log_only_contains_allowlisted_origin_and_sanitized_class(self):
        for filename, error_type, stage, expected_origin, expected_kind in (
            (r"C:\synthetic-path-secret\transcriber.py", RuntimeError,
             "infer", "transcriber.py", "RuntimeError"),
            ("/synthetic-path-secret/private-recording-secret.py",
             type("synthetic-class-secret\n", (Exception,), {}),
             "synthetic-stage-secret", "unknown", "Exception"),
        ):
            with self.subTest(origin=expected_origin):
                namespace = {"error_type": error_type}
                exec(compile(
                    "def fail():\n"
                    "    try:\n"
                    "        raise ValueError('synthetic-chain-secret')\n"
                    "    except ValueError as cause:\n"
                    "        raise error_type('synthetic-message-secret') from cause\n",
                    filename, "exec"), namespace)
                with self.assertLogs("server.model_server", level="ERROR") as logs:
                    try:
                        namespace["fail"]()
                    except Exception as error:
                        _log_model_failure(stage, error)
                self.assertEqual(len(logs.records), 1)
                record = logs.records[0]
                self.assertEqual(record.args, (
                    "infer" if expected_origin != "unknown" else "unknown",
                    expected_kind, expected_origin, 5 if expected_origin != "unknown" else 0,
                ))
                self.assertIsNone(record.exc_info)
                self.assertIsNone(record.stack_info)
                self.assertNotIn("secret", "\n".join(logs.output))
                self.assertNotIn("Traceback", "\n".join(logs.output))

    def test_warmup_and_infer_failure_logs_preserve_error_state_without_private_text(self):
        def fail():
            try:
                raise ValueError("synthetic-chain-secret")
            except ValueError as cause:
                raise RuntimeError("synthetic-message-secret") from cause

        for stage in ("warmup", "infer"):
            with self.subTest(stage=stage):
                engine = FakeEngine()
                if stage == "warmup":
                    engine.warmup = fail
                else:
                    engine.transcribe = lambda *args, **kwargs: fail()
                with self.assertLogs("server.model_server", level="ERROR") as logs:
                    with TestClient(create_model_app(settings(model_warmup=stage == "warmup"), engine)) as client:
                        if stage == "warmup":
                            until(lambda: client.get("/status").json()["model_state"] == "error")
                        response = client.post("/transcribe", json=request())
                        self.assertEqual(response.status_code, 503)
                        self.assertEqual(response.json()["code"], "model_unavailable")
                        self.assertEqual(client.get("/health").json(), {"status": "ok", "model_state": "error"})
                        self.assertNotIn("secret", response.text)
                self.assertEqual(len(logs.records), 1)
                self.assertEqual(logs.records[0].args[:3], (stage, "RuntimeError", "model_server.py"))
                self.assertGreater(logs.records[0].args[3], 0)
                self.assertIsNone(logs.records[0].exc_info)
                self.assertNotIn("secret", "\n".join(logs.output))

    def test_protocol_and_worker_failure_logs_retain_fixed_response_codes(self):
        for stage, error_type, code in (
            ("infer_protocol", ProtocolError, "model_protocol_error"),
            ("worker", KeyboardInterrupt, "model_unavailable"),
        ):
            with self.subTest(stage=stage):
                def fail(*args, **kwargs):
                    try:
                        raise ValueError("synthetic-chain-secret")
                    except ValueError as cause:
                        error = error_type()
                        error.args = ("synthetic-message-secret",)
                        raise error from cause

                engine = FakeEngine()
                engine.transcribe = fail
                with self.assertLogs("server.model_server", level="ERROR") as logs:
                    with TestClient(create_model_app(settings(), engine)) as client:
                        response = client.post("/transcribe", json=request())
                        self.assertEqual(response.status_code, 503)
                        self.assertEqual(response.json()["code"], code)
                        self.assertEqual(client.get("/status").json()["model_state"], "error")
                        self.assertEqual(client.post("/transcribe", json=request()).status_code, 503)
                        self.assertNotIn("secret", response.text)
                self.assertEqual(len(logs.records), 1)
                self.assertEqual(logs.records[0].args[:3], (stage, error_type.__name__, "model_server.py"))
                self.assertGreater(logs.records[0].args[3], 0)
                self.assertIsNone(logs.records[0].exc_info)
                self.assertNotIn("secret", "\n".join(logs.output))

    def test_factory_never_loads_application_settings_or_auth_routes(self):
        with patch("server.settings.Settings.from_env", side_effect=AssertionError("application env forbidden")):
            with TestClient(create_model_app(settings(), FakeEngine())) as client:
                self.assertEqual(client.get("/health").json(), {"status": "ok", "model_state": "unloaded"})
                self.assertEqual(client.post("/auth/login", json={}).status_code, 404)
                self.assertEqual(client.get("/openapi.json").status_code, 404)

    def test_background_warmup_does_not_block_health_and_inference(self):
        engine = FakeEngine()
        engine.block_warmup = True
        with TestClient(create_model_app(settings(model_warmup=True), engine)) as client:
            self.assertTrue(engine.warmup_entered.wait(1))
            started = time.monotonic()
            self.assertEqual(client.get("/health").json()["model_state"], "loading")
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(client.post("/transcribe", json=request()).status_code, 503)
            self.assertEqual(len(engine.calls), 0)
            engine.warmup_release.set()
            until(lambda: client.get("/status").json()["model_state"] == "ready")
            self.assertEqual(client.post("/transcribe", json=request()).status_code, 200)

    def test_warmup_failure_retains_health_and_never_leaks_exception(self):
        engine = FakeEngine()
        engine.fail = True
        with TestClient(create_model_app(settings(model_warmup=True), engine)) as client:
            until(lambda: client.get("/status").json()["model_state"] == "error")
            response = client.post("/transcribe", json=request())
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private", response.text)
            self.assertEqual(client.get("/health").status_code, 200)

    def test_request_body_and_typed_shape_rejection_do_not_call_model(self):
        engine = FakeEngine()
        with TestClient(create_model_app(settings(), engine)) as client:
            for payload in (b'x' * (MAX_REQUEST_BYTES + 1), b'{"version":NaN}', b'{"unexpected":1}'):
                response = client.post("/transcribe", content=payload, headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 422)
            self.assertEqual(client.post("/transcribe", content=b'{}').status_code, 422)
            self.assertEqual(engine.calls, [])

    def test_invalid_engine_output_is_not_partially_returned(self):
        engine = FakeEngine()
        engine.invalid = True
        with TestClient(create_model_app(settings(), engine)) as client:
            response = client.post("/transcribe", json=request())
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["code"], "model_protocol_error")
            self.assertNotIn("segments", response.json())

    def test_cancelled_handler_does_not_release_gpu_while_worker_runs(self):
        async def scenario():
            engine = FakeEngine()
            engine.block = True
            app = create_model_app(settings(model_warmup=True), engine)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    while (await client.get("/status")).json()["model_state"] != "ready":
                        await asyncio.sleep(.005)
                    operation = asyncio.create_task(client.post("/transcribe", json=request()))
                    await asyncio.to_thread(engine.entered.wait, 1)
                    self.assertTrue(engine.entered.is_set())
                    operation.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await operation
                    blocked = await client.post("/transcribe", json=request())
                    self.assertEqual(blocked.status_code, 429)
                    self.assertEqual(len(engine.calls), 1)
                    self.assertEqual((await client.get("/health")).status_code, 200)
                    engine.release.set()
                    while engine.active:
                        await asyncio.sleep(.005)
                    self.assertEqual((await client.post("/transcribe", json=request())).status_code, 200)
                    self.assertEqual(engine.maximum_active, 1)
        asyncio.run(scenario())


class RemoteModelTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX UDS integration; native loopback has separate tests")
    def test_real_uds_exact_pcm_context_and_independent_requests(self):
        engine = FakeEngine()
        with real_server(engine) as (remote, _, path):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            samples = np.array([-.75, .75, -0.0, 1e-38] * 4000, np.float32)
            context = {"version": 1, "audio_end": 8, "tokens": []}
            original = copy.deepcopy(context)
            output = {"old": "replace only on complete response"}
            with patch.dict(os.environ, {"HTTP_PROXY": "http://invalid-proxy.invalid:1", "HTTPS_PROXY": "http://invalid-proxy.invalid:1"}):
                result = remote.transcribe(samples, None, .5, False, start_seconds=7.5,
                                           boundary_context=context, boundary_output=output)
            self.assertEqual(engine.calls[0][0].tobytes(), samples.tobytes())
            self.assertEqual(engine.calls[0][1:], (None, .5, False, 7.5, original))
            self.assertEqual(context, original)
            self.assertEqual(output["audio_end"], 8.5)
            self.assertNotIn("old", output)
            self.assertEqual(result[0]["text"], " 끝! \n")
            remote.transcribe(samples, "en")
            self.assertEqual(engine.calls[-1][5], None)
            self.assertEqual(len(engine.calls), 2)

    @unittest.skipIf(os.name == "nt", "POSIX UDS integration; native loopback has separate tests")
    def test_real_uds_busy_is_serial_and_status_is_nonblocking(self):
        engine = FakeEngine()
        engine.block = True
        with real_server(engine, warmup=True) as (remote, _, _):
            until(lambda: remote.status()["model_state"] == "ready")
            result = []
            worker = threading.Thread(target=lambda: result.append(remote.transcribe(np.zeros(16000, np.float32), "ko")))
            worker.start()
            self.assertTrue(engine.entered.wait(1))
            started = time.monotonic()
            status = remote.status()
            self.assertLess(time.monotonic() - started, .1)
            self.assertEqual(status["model_state"], "ready")
            with self.assertRaises(ModelUnavailableError) as failure:
                remote.transcribe(np.zeros(16000, np.float32), "ko")
            self.assertEqual(failure.exception.code, "model_busy")
            engine.release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(engine.maximum_active, 1)
            self.assertEqual(len(engine.calls), 1)

    @unittest.skipIf(os.name == "nt", "POSIX UDS integration; native loopback has separate tests")
    def test_real_model_timeout_preserves_output_and_worker_exclusivity(self):
        engine = FakeEngine()
        engine.block = True
        with real_server(engine, warmup=True) as (remote, _, _):
            until(lambda: remote.status()["model_state"] == "ready")
            remote._timeout = .05
            output = {"unchanged": True}
            with self.assertRaises(ModelUnavailableError) as failure:
                remote.transcribe(np.zeros(16000, np.float32), "ko", boundary_output=output)
            self.assertEqual(failure.exception.code, "model_timeout")
            self.assertEqual(output, {"unchanged": True})
            with self.assertRaises(ModelUnavailableError):
                remote.transcribe(np.zeros(16000, np.float32), "ko")
            self.assertEqual(len(engine.calls), 1)

    def test_missing_socket_warmup_and_status_do_not_block_api_startup(self):
        remote = RemoteTranscriber(settings(local_model_socket=Path("/tmp/not-existing-model-test/runtime.sock")))
        started = time.monotonic()
        self.assertIsNone(remote.warmup())
        self.assertEqual(remote.status()["model_state"], "offline")
        self.assertLess(time.monotonic() - started, .1)
        with self.assertRaises(ModelUnavailableError):
            remote.transcribe(np.zeros(16000, np.float32), "ko")
        remote.close()

    @unittest.skipIf(os.name == "nt", "POSIX permissions; Windows ACL/reparse checks have separate tests")
    def test_socket_and_directory_permission_and_symlink_checks_fail_closed(self):
        engine = FakeEngine()
        with real_server(engine) as (_, _, path):
            self.assertEqual(validate_socket_path(path), path)
            path.chmod(0o666)
            with self.assertRaises(ModelUnavailableError):
                validate_socket_path(path)
            path.chmod(0o600)
            path.parent.chmod(0o755)
            with self.assertRaises(ModelUnavailableError):
                validate_socket_path(path)
            path.parent.chmod(0o700)
            link = path.with_name("link.sock")
            link.symlink_to(path)
            with self.assertRaises(ModelUnavailableError):
                validate_socket_path(link)
            with patch("server.remote_transcriber.os.getuid", return_value=os.getuid() + 1):
                with self.assertRaises(ModelUnavailableError):
                    validate_socket_path(path)
            regular = path.with_name("not-socket")
            regular.touch(mode=0o600)
            with self.assertRaises(ModelUnavailableError):
                validate_socket_path(regular)

    @unittest.skipIf(os.name == "nt", "POSIX UDS integration; native loopback has separate tests")
    def test_closed_adapter_never_reopens_or_uses_gpu(self):
        engine = FakeEngine()
        with real_server(engine) as (remote, _, _):
            remote.close()
            with self.assertRaises(ModelUnavailableError):
                remote.transcribe(np.zeros(16000, np.float32), "ko")
            self.assertEqual(remote.gpu_resources(), {"available": False})
            self.assertEqual(engine.calls, [])

    @unittest.skipIf(os.name == "nt", "POSIX UDS fixture; native signed error codes have separate tests")
    def test_error_codes_and_malformed_statuses_remain_safe(self):
        with tempfile.TemporaryDirectory(prefix="stt-model-mock-") as directory:
            path = Path(directory) / "model.sock"
            path.parent.chmod(0o700)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(path))
            path.chmod(0o600)
            try:
                for code, status in (("model_loading", 503), ("model_busy", 429), ("model_protocol_error", 422)):
                    remote = RemoteTranscriber(settings(local_model_socket=path))
                    remote._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
                        status, stream=httpx.ByteStream(json.dumps({"code": code, "detail": "private"}).encode()),
                        headers={"Content-Type": "application/json"})), base_url="http://local-model")
                    with self.assertRaises(ModelUnavailableError) as failure:
                        remote.transcribe(np.zeros(16000, np.float32), "ko")
                    self.assertEqual(failure.exception.code, code)
                    self.assertNotIn("private", str(failure.exception))
                    remote.close()
            finally:
                sock.close()

    @unittest.skipIf(os.name == "nt", "POSIX UDS integration; native loopback has separate tests")
    def test_invalid_response_preserves_caller_context_and_emits_no_partial_segments(self):
        engine = FakeEngine()
        engine.invalid = True
        with real_server(engine) as (remote, _, _):
            output = {"old": "retained"}
            with self.assertRaises(ModelUnavailableError) as failure:
                remote.transcribe(np.zeros(16000, np.float32), "ko", boundary_output=output)
            self.assertEqual(failure.exception.code, "model_protocol_error")
            self.assertEqual(output, {"old": "retained"})

    def test_status_thread_start_failure_releases_refresh_lock(self):
        remote = RemoteTranscriber(settings())
        with patch("server.remote_transcriber.threading.Thread.start", side_effect=RuntimeError("private")):
            self.assertEqual(remote.status()["model_state"], "offline")
        self.assertFalse(remote._status_lock.locked())
        remote.close()

    def test_malformed_success_never_replaces_boundary_output(self):
        for damage in ("request_id", "text", "extra", "context", "missing_context", "nan"):
            def reply(incoming):
                value = json.loads(incoming.content)
                result = {"version": 1, "request_id": value["request_id"],
                          "segments": [{"start": 0, "end": 1, "text": "valid first"}], "boundary_output": {}}
                if damage == "request_id":
                    result["request_id"] = str(uuid.uuid4())
                elif damage == "text":
                    result["segments"].append({"start": 0, "end": 1, "text": None})
                elif damage == "extra":
                    result["secret"] = "must not reflect"
                elif damage == "context":
                    result["boundary_output"] = {"huge": "x" * MAX_CONTEXT_BYTES}
                elif damage == "missing_context":
                    result["boundary_output"] = None
                else:
                    result["segments"][0]["end"] = float("nan")
                return httpx.Response(200, stream=httpx.ByteStream(json.dumps(result).encode()),
                                      headers={"Content-Type": "application/json"})
            with self.subTest(damage=damage), mock_remote(reply) as remote:
                output = {"committed": "keep"}
                with self.assertRaises(ModelUnavailableError) as failure:
                    remote.transcribe(np.zeros(16000, np.float32), "ko", boundary_output=output)
                self.assertEqual(failure.exception.code, "model_protocol_error")
                self.assertEqual(output, {"committed": "keep"})

    def test_response_length_stream_budget_compression_and_mime_are_rejected(self):
        class Oversized(httpx.SyncByteStream):
            reads = 0
            closed = False
            def __iter__(self):
                for _ in range(100):
                    self.reads += 1
                    yield b"x" * 65536
            def close(self):
                self.closed = True
        for scenario in ("length", "stream", "compressed", "mime"):
            stream = Oversized()
            headers = {"Content-Type": "application/json"}
            if scenario == "length":
                headers["Content-Length"] = str(MAX_RESPONSE_BYTES + 1)
            elif scenario == "compressed":
                headers["Content-Encoding"] = "gzip"
            elif scenario == "mime":
                headers["Content-Type"] = "text/html"
            with self.subTest(scenario=scenario), mock_remote(lambda _: httpx.Response(200, stream=stream, headers=headers)) as remote:
                with self.assertRaises(ModelUnavailableError) as failure:
                    remote.transcribe(np.zeros(16000, np.float32), "ko")
                self.assertEqual(failure.exception.code, "model_protocol_error")
                self.assertLessEqual(stream.reads, 17)
                self.assertTrue(stream.closed)
                if scenario in ("length", "compressed"):
                    self.assertEqual(stream.reads, 0)

    def test_bad_status_and_late_status_after_close_do_not_publish_ready(self):
        def reply(_):
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps({"model_state": [], "private": "hidden"}).encode()),
                                  headers={"Content-Type": "application/json"})
        with mock_remote(reply) as remote:
            remote.status()
            until(lambda: not remote._status_lock.locked())
            self.assertEqual(remote.status()["model_state"], "offline")
            self.assertNotIn("private", remote.status())
        with mock_remote(reply) as remote:
            entered, release = threading.Event(), threading.Event()
            def delayed(*args, **kwargs):
                entered.set()
                release.wait(2)
                return {"model_state": "ready", "engine": "test", "model": "test", "device": "cpu", "gpu": {"available": False}}
            remote._request = delayed
            remote.status()
            self.assertTrue(entered.wait(1))
            remote.close()
            release.set()
            until(lambda: not remote._status_lock.locked())
            self.assertEqual(remote.status()["model_state"], "offline")

    @unittest.skipIf(os.name == "nt", "POSIX UDS integration; native loopback has separate tests")
    def test_replacing_api_adapter_does_not_reload_model_or_lose_model_availability(self):
        engine = FakeEngine()
        with real_server(engine, warmup=True) as (remote, _, path):
            until(lambda: remote.status()["model_state"] == "ready")
            remote.transcribe(np.zeros(16000, np.float32), "ko")
            remote.close()
            replacement = RemoteTranscriber(settings(local_model_socket=path))
            try:
                replacement.transcribe(np.zeros(16000, np.float32), "en")
                self.assertEqual(engine.warmups, 1)
                self.assertEqual(len(engine.calls), 2)
                self.assertEqual(len(set(engine.thread_ids)), 1, "warmup and every request reuse one GPU thread")
            finally:
                replacement.close()


if __name__ == "__main__":
    unittest.main()
