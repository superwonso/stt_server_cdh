"""Synthetic CPU cancellation; never load a model or access production."""
import json
import threading
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
from fastapi.testclient import TestClient

from server.model_cancellation import (CancellationRegistry, CancellableAligner,
    RequestStoppingCriteria, cancellation_scope, check_cancelled)
from server.model_protocol import (ModelRequestCancelledError, ModelUnavailableError, ProtocolError,
    dump_json, make_cancel_request, make_cancelled_result, make_result, read_cancel_request,
    read_cancelled_result)
from server.model_server import create_model_app
from server.remote_transcriber import RemoteTranscriber
from test_model_transport import FakeEngine, request, settings, until
import test_transcriber as transcriber_fixture


class CancellationTests(unittest.TestCase):
    def test_protocol_is_exact_bound_and_cannot_supply_partial_success(self):
        rid = str(uuid.uuid4())
        self.assertEqual(read_cancel_request(make_cancel_request(rid)), rid)
        result = make_cancelled_result(rid)
        error = read_cancelled_result(result, rid)
        self.assertIsInstance(error, ModelRequestCancelledError)
        self.assertNotIsInstance(error, ModelUnavailableError)
        self.assertFalse(error.retryable)
        error.args = ('synthetic private text',)
        self.assertNotIn('private', str(error) + repr(error))
        for changed in ({'version': True}, {'version': 2}, {'retryable': 0}, {'retryable': True},
                        {'request_id': str(uuid.uuid4())}, {'segments': []}, {'partial_text': 'synthetic'}):
            with self.subTest(changed=changed), self.assertRaises(ProtocolError):
                read_cancelled_result({**result, **changed}, rid)
        for value in ({'version': 1, 'request_id': 'not-uuid'}, {**make_cancel_request(rid), 'all': True}):
            with self.assertRaises(ProtocolError):
                read_cancel_request(value)

    def test_precancel_is_bounded_idempotent_and_never_cancels_another_request(self):
        now = [10.0]
        registry = CancellationRegistry(limit=1, ttl=2, now=lambda: now[0])
        first, other = str(uuid.uuid4()), str(uuid.uuid4())
        self.assertTrue(registry.cancel(first))
        self.assertTrue(registry.cancel(first))
        self.assertFalse(registry.cancel(other))
        event = registry.register(first)
        other_event = registry.register(other)
        self.assertTrue(event.is_set())
        self.assertFalse(other_event.is_set())
        self.assertIsNone(registry.register(first))
        registry.finish(first, threading.Event())
        self.assertIsNone(registry.register(first))
        registry.finish(first, event)
        now[0] += 3
        self.assertFalse(registry.register(first).is_set())

    def test_scope_and_aligner_check_before_and_after_but_do_not_mask_fatal_error(self):
        event = threading.Event()
        calls = []
        class Aligner:
            def align(self):
                calls.append('forward')
                event.set()
                return ['never publish']
        wrapped = CancellableAligner(Aligner())
        with self.assertRaises(ModelRequestCancelledError), cancellation_scope(event):
            criteria = RequestStoppingCriteria()
            self.assertFalse(criteria(None, None))
            wrapped.align()
        self.assertEqual(calls, ['forward'])
        with self.assertRaises(ModelRequestCancelledError), cancellation_scope(event):
            wrapped.align()
        self.assertEqual(calls, ['forward'])
        check_cancelled()  # Context was reset; unrelated file requests continue.
        self.assertFalse(RequestStoppingCriteria()(None, None))

    def test_local_cancel_keeps_ready_and_never_commits_boundary_then_next_request_works(self):
        event = threading.Event()
        model = transcriber_fixture.FakeQwen()
        original = model.transcribe
        def stopped(**kwargs):
            result = original(**kwargs)
            event.set()
            return result
        model.transcribe = stopped
        engine = transcriber_fixture.TranscriberTests().engine(model)
        boundary = {'previous': 'must not become success'}
        with patch('server.transcriber.contains_speech', return_value=True), self.assertRaises(ModelRequestCancelledError):
            engine.transcribe(np.zeros(16000, np.float32), 'ko', boundary_output=boundary, cancel_event=event)
        self.assertEqual(boundary, {})
        self.assertEqual(engine.status()['model_state'], 'ready')
        model.transcribe = original
        with patch('server.transcriber.contains_speech', return_value=True):
            self.assertTrue(engine.transcribe(np.zeros(16000, np.float32), 'ko'))
        model.error = RuntimeError('synthetic fatal')
        with patch('server.transcriber.contains_speech', return_value=True), self.assertRaises(RuntimeError):
            engine.transcribe(np.zeros(16000, np.float32), 'ko')
        self.assertEqual(engine.status()['model_state'], 'error')

    def test_private_cancel_interrupts_one_worker_keeps_lock_until_exit_and_next_request_works(self):
        engine = FakeEngine()
        engine.supports_cancellation = True
        released, allow_exit = threading.Event(), threading.Event()
        original = engine.transcribe
        def cancellable(*args, cancel_event=None, **kwargs):
            engine.entered.set()
            if not cancel_event.wait(3):
                raise AssertionError('synthetic cancellation missing')
            released.set()
            if not allow_exit.wait(3):
                raise AssertionError('synthetic cleanup missing')
            check_cancelled(cancel_event)
        engine.transcribe = cancellable
        value = request()
        result = []
        with TestClient(create_model_app(settings(model_warmup=True), engine)) as client:
            until(lambda: client.get('/health').json()['model_state'] == 'ready')
            thread = threading.Thread(target=lambda: result.append(client.post('/transcribe', json=value)))
            thread.start()
            try:
                self.assertTrue(engine.entered.wait(2))
                unknown = str(uuid.uuid4())
                self.assertEqual(client.post(f'/requests/{unknown}/cancel', json=make_cancel_request(unknown)).status_code, 200)
                self.assertFalse(released.is_set())
                rid = value['request_id']
                ack = client.post(f'/requests/{rid}/cancel', json=make_cancel_request(rid))
                self.assertEqual(ack.json(), {**make_cancel_request(rid), 'cancel_requested': True})
                self.assertTrue(released.wait(2))
                self.assertEqual(client.post('/transcribe', json=request()).status_code, 429)
                allow_exit.set()
                thread.join(3)
                self.assertEqual(result[0].status_code, 409)
                self.assertEqual(result[0].json(), make_cancelled_result(rid))
                self.assertEqual(client.get('/health').json()['model_state'], 'ready')
                engine.transcribe = original
                engine.supports_cancellation = False
                self.assertEqual(client.post('/transcribe', json=request()).status_code, 200)
                value = request()
                client.post(f"/requests/{value['request_id']}/cancel", json=make_cancel_request(value['request_id']))
                calls = len(engine.calls)
                self.assertEqual(client.post('/transcribe', json=value).status_code, 409)
                self.assertEqual(len(engine.calls), calls)
            finally:
                allow_exit.set()
                thread.join(4)
        self.assertFalse(thread.is_alive())

    def test_remote_lost_cancel_ack_retries_only_cancel_and_keeps_other_results_private(self):
        event, started, cancelled = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def handler(req):
            value = json.loads(req.content)
            calls.append(req.url.path)
            if req.url.path == '/transcribe':
                started.set()
                if not cancelled.wait(3):
                    raise AssertionError('synthetic cancel not received')
                return httpx.Response(409, json=make_cancelled_result(value['request_id']))
            cancelled.set()
            if len([path for path in calls if path.endswith('/cancel')]) == 1:
                raise httpx.ReadTimeout('synthetic lost ACK')
            return httpx.Response(200, json={**value, 'cancel_requested': True})
        remote = RemoteTranscriber(settings(local_model_socket='synthetic.sock'))
        remote._client = httpx.Client(transport=httpx.MockTransport(handler), base_url='http://local-model')
        remote._cancel_client = httpx.Client(transport=httpx.MockTransport(handler), base_url='http://local-model')
        outcome = []
        boundary = {'previous': 'untouched'}
        def work():
            try:
                remote.transcribe(np.zeros(16000, np.float32), 'ko', boundary_output=boundary, cancel_event=event)
            except BaseException as error:
                outcome.append(error)
        with patch('server.remote_transcriber.validate_socket_path', return_value='synthetic.sock'):
            thread = threading.Thread(target=work)
            thread.start()
            self.assertTrue(started.wait(2))
            event.set()
            thread.join(4)
            until(lambda: len(calls) >= 3)
            remote.close()
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome[0], ModelRequestCancelledError)
        self.assertEqual(calls.count('/transcribe'), 1)
        self.assertEqual(boundary, {'previous': 'untouched'})

    def test_disconnected_http_client_cancels_worker_without_a_control_request(self):
        engine = FakeEngine()
        engine.supports_cancellation = True
        def cancellable(*args, cancel_event=None, **kwargs):
            self.assertTrue(cancel_event.wait(3))
            check_cancelled(cancel_event)
        engine.transcribe = cancellable
        with TestClient(create_model_app(settings(model_warmup=True), engine)) as client:
            until(lambda: client.get('/health').json()['model_state'] == 'ready')
            with patch('server.model_server.Request.is_disconnected', new=AsyncMock(return_value=True)):
                result = client.post('/transcribe', json=request())
            self.assertEqual(result.status_code, 409)
            self.assertEqual(result.json()['code'], 'transcription_cancelled')
            self.assertEqual(client.get('/health').json()['model_state'], 'ready')

    def test_a_fatal_kernel_error_after_cancel_still_latches_model_error(self):
        engine = FakeEngine()
        engine.supports_cancellation = True
        def fatal(*args, cancel_event=None, **kwargs):
            cancel_event.set()
            raise RuntimeError('synthetic private detail must not escape')
        engine.transcribe = fatal
        with TestClient(create_model_app(settings(model_warmup=True), engine)) as client:
            until(lambda: client.get('/health').json()['model_state'] == 'ready')
            with self.assertLogs('server.model_server', level='ERROR') as logged:
                result = client.post('/transcribe', json=request())
            self.assertEqual(result.status_code, 503)
            self.assertEqual(client.get('/health').json()['model_state'], 'error')
            self.assertNotIn('synthetic private', result.text + str(logged.output))

    def test_remote_http_timeout_sends_bounded_best_effort_cancel_not_audio_replay(self):
        remote = RemoteTranscriber(settings())
        calls = []
        def fail(method, path, **kwargs):
            calls.append(path)
            raise ModelUnavailableError('model_timeout')
        remote._request = fail
        with self.assertRaises(ModelUnavailableError):
            remote.transcribe(np.zeros(16000, np.float32), 'ko')
        self.assertEqual(calls.count('/transcribe'), 1)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(set(calls[1:])), 1)


if __name__ == '__main__':
    unittest.main()
