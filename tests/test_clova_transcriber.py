from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from server import clova_nest_pb2 as nest_pb2
from server.clova_transcriber import (
    MAX_DATA_BYTES,
    MAX_RESPONSE_BYTES,
    ClovaStreamingTranscriber,
    ClovaTranscriptionError,
)


BLOCK = object()
DISCONNECT = object()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def transcript(
    text: str,
    position: int,
    alignments: list[tuple[str, float, float]],
    *,
    seq_id: int,
    ep_flag: bool = True,
    **extra,
) -> dict:
    value = {
        "text": text,
        "position": position,
        "periodPositions": [],
        "periodAlignIndices": [],
        "epFlag": ep_flag,
        "seqId": seq_id if ep_flag else 0,
        "epdType": "endPoint" if ep_flag else "gap",
        "startTimestamp": round(alignments[0][1] * 1000) if alignments else 0,
        "endTimestamp": round(alignments[-1][2] * 1000) if alignments else 0,
        "confidence": 0.99,
        "alignInfos": [
            {
                "word": word,
                "start": round(start * 1000),
                "end": round(end * 1000),
                "confidence": 0.99,
            }
            for word, start, end in alignments
        ],
    }
    value.update(extra)
    return {"responseType": ["transcription"], "transcription": value}


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class FakeChannel:
    def __init__(self):
        self.closed = threading.Event()
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.closed.set()


class FakeStub:
    def __init__(self, channel: FakeChannel, plans, config=...):
        self.channel = channel
        self.plans = list(plans)
        self.config_response = config
        self.requests = []
        self.data_groups = []
        self.metadata = []
        self.sent_samples = 0

    def recognize(self, requests, metadata):
        self.metadata.append(metadata)
        group = []
        group_start = 0.0
        for request in requests:
            self.requests.append(request)
            if request.type == nest_pb2.CONFIG:
                if self.config_response is BLOCK:
                    self.channel.closed.wait(2)
                    return
                if isinstance(self.config_response, Exception):
                    raise self.config_response
                value = self.config_response
                if value is ...:
                    value = {"responseType": ["config"], "config": {"status": "Success"}}
                yield nest_pb2.NestResponse(contents=json.dumps(value))
                continue

            if request.type != nest_pb2.DATA:
                raise AssertionError("unexpected request type")
            if not group:
                group_start = self.sent_samples / 16000
            group.append(request)
            self.sent_samples += len(request.data.chunk) // 2
            details = json.loads(request.data.extra_contents)
            if not details["epFlag"]:
                continue
            self.data_groups.append(group)
            group = []
            if not self.plans:
                raise AssertionError("missing fake response plan")
            plan = self.plans.pop(0)
            if plan is BLOCK:
                self.channel.closed.wait(2)
                return
            if isinstance(plan, Exception):
                raise plan
            seq_id = details["seqId"]
            group_end = self.sent_samples / 16000
            responses = plan(seq_id, group_start, group_end) if callable(plan) else plan
            for response in responses:
                if response is DISCONNECT:
                    raise RuntimeError("private provider disconnect detail")
                if isinstance(response, str):
                    contents = response
                else:
                    contents = json.dumps(response, ensure_ascii=False, allow_nan=True)
                yield nest_pb2.NestResponse(contents=contents)


class FakeFactory:
    def __init__(self, session_plans, *, configs=None):
        self.session_plans = list(session_plans)
        self.configs = list(configs or [])
        self.stubs = []
        self.channels = []

    def __call__(self):
        index = len(self.stubs)
        if index >= len(self.session_plans):
            raise AssertionError("unexpected session")
        channel = FakeChannel()
        config = self.configs[index] if index < len(self.configs) else ...
        stub = FakeStub(channel, self.session_plans[index], config=config)
        self.channels.append(channel)
        self.stubs.append(stub)
        return stub, channel


def settings(**updates):
    values = {
        "clova_speech_secret_key": "unit-test-secret",
        "clova_stream_response_timeout_seconds": 0.3,
        "clova_stream_max_age_seconds": 240,
        "clova_stream_idle_seconds": 60,
        "clova_epd_gap_ms": 1800,
        "clova_epd_duration_ms": 18000,
    }
    values.update(updates)
    return SimpleNamespace(**values)


class ClovaConcurrencyTests(unittest.TestCase):
    @staticmethod
    def call(engine, lecture="lecture-a", *, start=0, final=False):
        return engine.transcribe(
            np.zeros(16_000, dtype=np.float32),
            "ko",
            final_chunk=final,
            lecture_id=lecture,
            username="owner",
            start_seconds=start,
            payload_hash=digest(f"{lecture}:{start}"),
        )

    def wait_for_turns(self, engine, count):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with engine._state_lock:
                actual = sum(len(turns) for turns in engine._owner_queues.values())
            if actual == count:
                return
            time.sleep(0.005)
        self.fail(f"expected {count} registered operation turns, got {actual}")

    def test_another_lecture_completes_while_the_first_waits_for_ack(self):
        entered, release = threading.Event(), threading.Event()

        def slow(seq, _start, _end):
            entered.set()
            self.assertTrue(release.wait(3))
            return [transcript("가", 0, [("가", 0.1, 0.2)], seq_id=seq)]

        def fast(seq, _start, _end):
            return [transcript("나", 0, [("나", 0.1, 0.2)], seq_id=seq)]

        factory = FakeFactory([[slow], [fast]])
        engine = ClovaStreamingTranscriber(
            settings(clova_stream_response_timeout_seconds=3), stub_factory=factory
        )
        self.addCleanup(engine.close)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.call, engine)
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(self.call, engine, "lecture-b")
                self.assertEqual(second.result(timeout=1)[0]["text"], "나")
                self.assertFalse(first.done())
            finally:
                release.set()
            self.assertEqual(first.result(timeout=1)[0]["text"], "가")
        self.assertEqual(engine._owner_queues, {})

    def test_same_lecture_waits_and_submits_chunks_in_registration_order(self):
        entered, release = threading.Event(), threading.Event()

        def plan(word, position, wait=False):
            def respond(seq, start, _end):
                if wait:
                    entered.set()
                    self.assertTrue(release.wait(3))
                return [transcript(
                    word, position, [(word, start + 0.1, start + 0.2)], seq_id=seq
                )]
            return respond

        factory = FakeFactory([[plan("가", 0, True), plan("나", 1), plan("다", 2)]])
        engine = ClovaStreamingTranscriber(
            settings(clova_stream_response_timeout_seconds=3), stub_factory=factory
        )
        self.addCleanup(engine.close)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.call, engine)
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(self.call, engine, start=1)
                self.wait_for_turns(engine, 2)
                third = pool.submit(self.call, engine, start=2, final=True)
                self.wait_for_turns(engine, 3)
                self.assertEqual(len(factory.stubs[0].data_groups), 1)
                self.assertFalse(second.done())
                self.assertFalse(third.done())
            finally:
                release.set()
            self.assertEqual(
                [future.result(timeout=1)[0]["text"] for future in (first, second, third)],
                ["가", "나", "다"],
            )
        self.assertEqual(len(factory.stubs), 1)
        self.assertEqual(engine._owner_queues, {})

    def test_another_lecture_can_open_while_a_transport_is_still_being_created(self):
        entered, release = threading.Event(), threading.Event()

        def response(seq, _start, _end):
            return [transcript("가", 0, [("가", 0.1, 0.2)], seq_id=seq)]

        factory = FakeFactory([[response], [response]])
        factory_lock = threading.Lock()

        def first_transport_waits():
            with factory_lock:
                is_first = not factory.stubs
                product = factory()
            if is_first:
                entered.set()
                self.assertTrue(release.wait(3))
            return product

        engine = ClovaStreamingTranscriber(
            settings(clova_stream_response_timeout_seconds=3),
            stub_factory=first_transport_waits,
        )
        self.addCleanup(engine.close)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.call, engine)
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(self.call, engine, "lecture-b")
                self.assertEqual(second.result(timeout=1)[0]["text"], "가")
                self.assertFalse(first.done())
            finally:
                release.set()
            self.assertEqual(first.result(timeout=1)[0]["text"], "가")
        self.assertEqual(engine._opening_reservations, set())

    def test_busy_session_survives_status_and_other_lecture_idle_cleanup(self):
        entered, release = threading.Event(), threading.Event()
        clock = FakeClock()

        def slow(seq, _start, _end):
            entered.set()
            self.assertTrue(release.wait(3))
            return [transcript("가", 0, [("가", 0.1, 0.2)], seq_id=seq)]

        def fast(seq, _start, _end):
            return [transcript("나", 0, [("나", 0.1, 0.2)], seq_id=seq)]

        factory = FakeFactory([[slow], [fast]])
        engine = ClovaStreamingTranscriber(
            settings(clova_stream_response_timeout_seconds=3, clova_stream_idle_seconds=1),
            stub_factory=factory, clock=clock,
        )
        self.addCleanup(engine.close)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.call, engine)
            try:
                self.assertTrue(entered.wait(1))
                clock.value = 2
                self.assertEqual(engine.status()["active_sessions"], 1)
                self.assertFalse(factory.channels[0].closed.is_set())
                second = pool.submit(self.call, engine, "lecture-b")
                self.assertEqual(second.result(timeout=1)[0]["text"], "나")
                self.assertFalse(factory.channels[0].closed.is_set())
            finally:
                release.set()
            self.assertEqual(first.result(timeout=1)[0]["text"], "가")

    def test_session_capacity_never_evicts_another_active_lecture(self):
        entered, release = threading.Event(), threading.Event()

        def slow(seq, _start, _end):
            entered.set()
            self.assertTrue(release.wait(3))
            return [transcript("가", 0, [("가", 0.1, 0.2)], seq_id=seq)]

        factory = FakeFactory([[slow]])
        engine = ClovaStreamingTranscriber(
            settings(clova_stream_response_timeout_seconds=3), stub_factory=factory
        )
        self.addCleanup(engine.close)
        with patch("server.clova_transcriber.MAX_ACTIVE_SESSIONS", 1):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.call, engine)
                try:
                    self.assertTrue(entered.wait(1))
                    with self.assertRaises(ClovaTranscriptionError) as raised:
                        self.call(engine, "lecture-b")
                    self.assertEqual(raised.exception.code, "session_capacity")
                    self.assertTrue(raised.exception.retryable)
                    self.assertFalse(factory.channels[0].closed.is_set())
                    self.assertEqual(engine.status()["active_sessions"], 1)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=1)[0]["text"], "가")

    def test_transport_creation_reserves_the_last_session_slot(self):
        entered, release = threading.Event(), threading.Event()

        def response(seq, _start, _end):
            return [transcript("가", 0, [("가", 0.1, 0.2)], seq_id=seq)]

        factory = FakeFactory([[response]])

        def blocked_factory():
            entered.set()
            self.assertTrue(release.wait(3))
            return factory()

        engine = ClovaStreamingTranscriber(
            settings(clova_stream_response_timeout_seconds=3), stub_factory=blocked_factory
        )
        self.addCleanup(engine.close)
        with patch("server.clova_transcriber.MAX_ACTIVE_SESSIONS", 1):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.call, engine)
                try:
                    self.assertTrue(entered.wait(1))
                    self.assertEqual(engine.status()["active_sessions"], 1)
                    with self.assertRaises(ClovaTranscriptionError) as raised:
                        self.call(engine, "lecture-b")
                    self.assertEqual(raised.exception.code, "session_capacity")
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=1)[0]["text"], "가")
        self.assertEqual(len(factory.stubs), 1)
        self.assertEqual(engine._opening_reservations, set())

    def test_cancelled_turns_remain_bounded_until_their_owner_queue_unwinds(self):
        entered, release = threading.Event(), threading.Event()

        def response(seq, _start, _end):
            return [transcript("가", 0, [("가", 0.1, 0.2)], seq_id=seq)]

        factory = FakeFactory([[response], [response]])

        def blocked_factory():
            if not factory.stubs:
                entered.set()
                self.assertTrue(release.wait(3))
            return factory()

        engine = ClovaStreamingTranscriber(
            settings(clova_stream_response_timeout_seconds=3), stub_factory=blocked_factory
        )
        self.addCleanup(engine.close)
        with patch("server.clova_transcriber.MAX_PENDING_OPERATIONS", 2):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.call, engine)
                try:
                    self.assertTrue(entered.wait(1))
                    second = pool.submit(self.call, engine, start=1)
                    self.wait_for_turns(engine, 2)
                    engine.close_session("owner", "lecture-a")
                    with self.assertRaises(ClovaTranscriptionError) as raised:
                        self.call(engine, start=2)
                    self.assertEqual(raised.exception.code, "session_capacity")
                finally:
                    release.set()
                for future in (first, second):
                    with self.assertRaises(ClovaTranscriptionError):
                        future.result(timeout=1)
        self.assertEqual(factory.stubs[0].requests, [])
        self.assertEqual(engine._owner_queues, {})
        self.assertEqual(engine._opening_reservations, set())
        self.assertEqual(self.call(engine, final=True)[0]["text"], "가")
        self.assertEqual(engine._owner_queues, {})

    def test_parallel_lectures_keep_unique_ack_ids_and_release_all_turns(self):
        count = 8
        all_sent = threading.Barrier(count)

        def response(seq, _start, _end):
            all_sent.wait(timeout=2)
            return [transcript("가", 0, [("가", 0.1, 0.2)], seq_id=seq)]

        factory = FakeFactory([[response] for _ in range(count)])
        factory_lock = threading.Lock()

        def locked_factory():
            with factory_lock:
                return factory()

        engine = ClovaStreamingTranscriber(
            settings(clova_stream_response_timeout_seconds=3), stub_factory=locked_factory
        )
        self.addCleanup(engine.close)
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [
                pool.submit(self.call, engine, f"lecture-{index}", final=True)
                for index in range(count)
            ]
            for future in futures:
                self.assertEqual(future.result(timeout=3)[0]["text"], "가")
        sequences = [
            json.loads(stub.data_groups[0][-1].data.extra_contents)["seqId"]
            for stub in factory.stubs
        ]
        self.assertEqual(len(set(sequences)), count)
        self.assertEqual(engine._owner_queues, {})
        self.assertEqual(engine._operation_tokens, {})
        self.assertEqual(engine._opening_reservations, set())
        self.assertEqual(engine.status()["active_sessions"], 0)

    def test_reopening_a_cancelled_lecture_waits_for_older_turns_to_unwind(self):
        entered, release = threading.Event(), threading.Event()

        def response(seq, _start, _end):
            return [transcript("가", 0, [("가", 0.1, 0.2)], seq_id=seq)]

        factory = FakeFactory([[response], [response]])

        def blocked_factory():
            product = factory()
            if len(factory.stubs) == 1:
                entered.set()
                self.assertTrue(release.wait(3))
            return product

        engine = ClovaStreamingTranscriber(
            settings(clova_stream_response_timeout_seconds=3), stub_factory=blocked_factory
        )
        self.addCleanup(engine.close)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.call, engine)
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(self.call, engine, start=1)
                self.wait_for_turns(engine, 2)
                engine.close_session("owner", "lecture-a")
                reopened = pool.submit(self.call, engine, final=True)
                self.wait_for_turns(engine, 3)
                self.assertFalse(reopened.done())
                self.assertEqual(len(factory.stubs), 1)
            finally:
                release.set()
            for future in (first, second):
                with self.assertRaises(ClovaTranscriptionError):
                    future.result(timeout=1)
            self.assertEqual(reopened.result(timeout=1)[0]["text"], "가")
        self.assertEqual(factory.stubs[0].requests, [])
        self.assertEqual(len(factory.stubs[1].data_groups), 1)
        self.assertEqual(engine._owner_queues, {})
        self.assertEqual(engine._operation_tokens, {})
        self.assertEqual(engine._opening_reservations, set())


class ClovaStreamingTranscriberTests(unittest.TestCase):
    def test_documented_english_alignment_preserves_spaces_from_response_text(self):
        def plan(seq, _start, _end):
            # NAVER's documented success example omits the two internal spaces
            # from alignInfos and includes a trailing-space alignment which is
            # absent from transcription.text.
            return [transcript(
                "This is text.",
                0,
                [
                    ("This", 0.190, 0.340),
                    ("is", 0.341, 0.447),
                    ("text", 0.448, 0.580),
                    (".", 0.581, 0.700),
                    (" ", 0.701, 0.840),
                ],
                seq_id=seq,
            )]

        factory = FakeFactory([[plan]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        result = engine.transcribe(
            np.zeros(16_000, dtype=np.float32),
            "en",
            final_chunk=True,
            lecture_id="lecture-spaced-english",
            username="owner-a",
            start_seconds=0,
            payload_hash=digest("spaced-english"),
        )

        self.assertEqual(result, [{"start": 0.19, "end": 0.7, "text": "This is text."}])

    def test_overlap_filter_uses_the_exact_original_text_substring(self):
        def plan(seq, _start, _end):
            return [transcript(
                "이전 문장 새 문장",
                0,
                [
                    ("이", 0.10, 0.20),
                    ("전", 0.20, 0.30),
                    ("문", 0.45, 0.55),
                    ("장", 0.55, 0.65),
                    ("새", 1.10, 1.20),
                    ("문", 1.35, 1.45),
                    ("장", 1.45, 1.55),
                ],
                seq_id=seq,
            )]

        factory = FakeFactory([[plan]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        result = engine.transcribe(
            np.zeros(32_000, dtype=np.float32),
            "ko",
            overlap_seconds=1.0,
            final_chunk=True,
            lecture_id="lecture-spaced-overlap",
            username="owner-a",
            start_seconds=5.0,
            payload_hash=digest("spaced-overlap"),
        )

        self.assertEqual(result, [{"start": 1.1, "end": 1.55, "text": "새 문장"}])

    def test_config_precedes_bounded_data_and_last_data_carries_ack(self):
        def plan(seq, _start, _end):
            return [transcript("안녕", 0, [("안", 0.1, 0.2), ("녕", 0.2, 0.3)], seq_id=seq)]

        factory = FakeFactory([[plan]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        result = engine.transcribe(
            np.zeros(20_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture-a",
            username="owner-a",
            start_seconds=0,
            payload_hash=digest("one"),
        )
        self.assertEqual(result, [{"start": 0.1, "end": 0.3, "text": "안녕"}])
        stub = factory.stubs[0]
        self.assertEqual(stub.requests[0].type, nest_pb2.CONFIG)
        config = json.loads(stub.requests[0].config.config)
        self.assertEqual(config["transcription"]["language"], "ko")
        self.assertEqual(config["semanticEpd"]["gapThreshold"], 1800)
        self.assertEqual(config["semanticEpd"]["durationThreshold"], 18000)
        self.assertFalse(config["semanticEpd"]["skipEmptyText"])
        self.assertEqual(stub.metadata, [(('authorization', 'Bearer unit-test-secret'),)])
        data = stub.data_groups[0]
        self.assertEqual([len(item.data.chunk) for item in data], [MAX_DATA_BYTES, 8000])
        self.assertTrue(all(len(item.data.chunk) <= MAX_DATA_BYTES for item in data))
        first_extra = json.loads(data[0].data.extra_contents)
        last_extra = json.loads(data[-1].data.extra_contents)
        self.assertEqual(first_extra, {"epFlag": False})
        self.assertNotIn("seqId", first_extra)
        self.assertTrue(last_extra["epFlag"])
        self.assertGreater(last_extra["seqId"], 0)
        self.assertEqual(engine.status()["active_sessions"], 1)
        status_text = json.dumps(engine.status())
        for private in ("unit-test-secret", "owner-a", "lecture-a", "ncloud.com"):
            self.assertNotIn(private, status_text)
        engine.close()
        self.assertTrue(factory.channels[0].closed.is_set())

    def test_production_transport_pins_tls_target_and_disables_environment_proxy(self):
        def plan(seq, _start, _end):
            return [transcript("가", 0, [("가", 0.1, 0.2)], seq_id=seq)]

        channel = FakeChannel()
        stub = FakeStub(channel, [plan])
        with (
            patch("server.clova_transcriber.grpc.secure_channel", return_value=channel) as secure,
            patch("server.clova_transcriber.nest_pb2_grpc.NestServiceStub", return_value=stub) as make_stub,
        ):
            engine = ClovaStreamingTranscriber(settings())
            engine.transcribe(
                np.zeros(16_000, dtype=np.float32), "ko", final_chunk=True,
                lecture_id="lecture", username="owner", start_seconds=0,
                payload_hash=digest("pinned"),
            )
        self.assertEqual(secure.call_args.args[0], "clovaspeech-gw.ncloud.com:50051")
        options = dict(secure.call_args.kwargs["options"])
        self.assertEqual(options["grpc.enable_http_proxy"], 0)
        self.assertEqual(options["grpc.max_send_message_length"], MAX_DATA_BYTES + 4096)
        make_stub.assert_called_once_with(channel)
        self.assertTrue(channel.closed.is_set())

    def test_pcm16_conversion_exactly_inverts_wav_decode_scaling(self):
        def plan(seq, _start, _end):
            return [transcript("", 0, [], seq_id=seq)]

        original = np.array(
            [-32768, -32767, -1, 0, 1, 32766, 32767],
            dtype="<i2",
        )
        decoded = original.astype(np.float32) / 32768.0
        factory = FakeFactory([[plan]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        self.assertEqual(
            engine.transcribe(
                decoded,
                "ko",
                final_chunk=True,
                lecture_id="lecture",
                username="owner",
                start_seconds=0,
                payload_hash=digest("pcm-fidelity"),
            ),
            [],
        )
        wire = b"".join(
            request.data.chunk for request in factory.stubs[0].data_groups[0]
        )
        np.testing.assert_array_equal(np.frombuffer(wire, dtype="<i2"), original)

    def test_healthy_session_keeps_context_and_strips_repeated_browser_overlap(self):
        def first(seq, _start, _end):
            return [transcript("앞", 0, [("앞", 0.1, 0.2)], seq_id=seq)]

        def second(seq, _start, _end):
            return [transcript("뒤", 1, [("뒤", 1.1, 1.3)], seq_id=seq)]

        factory = FakeFactory([[first, second]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        engine.transcribe(
            np.zeros(16_000, dtype=np.float32), "ko", 0, False,
            lecture_id="lecture", username="owner", start_seconds=0, payload_hash=digest("first"),
        )
        result = engine.transcribe(
            np.zeros(24_000, dtype=np.float32), "ko", 0.5, False,
            lecture_id="lecture", username="owner", start_seconds=0.5, payload_hash=digest("second"),
        )
        self.assertEqual(len(factory.stubs), 1)
        second_data_bytes = sum(len(value.data.chunk) for value in factory.stubs[0].data_groups[1])
        self.assertEqual(second_data_bytes, 32_000)  # only one fresh second, not 1.5 s
        self.assertEqual(result, [{"start": 0.6, "end": 0.8, "text": "뒤"}])
        engine.close()

    def test_healthy_session_recovers_text_finalized_after_the_chunk_boundary(self):
        def first(seq, _start, _end):
            return [
                transcript(
                    "비교하",
                    0,
                    [("비", 8.0, 8.4), ("교", 8.4, 8.8), ("하", 8.8, 9.6)],
                    seq_id=seq,
                )
            ]

        def second(seq, _start, _end):
            return [
                transcript(
                    "는 것이다.",
                    3,
                    [
                        ("는", 9.62, 9.75),
                        ("것", 9.76, 9.94),
                        ("이", 10.05, 10.15),
                        ("다", 10.15, 10.34),
                        (".", 10.34, 10.36),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first, second]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        first_result = engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("healthy-boundary-first"),
        )
        second_result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("healthy-boundary-second"),
        )

        self.assertEqual(len(factory.stubs), 1)
        self.assertEqual(second_result, [{"start": 2.62, "end": 3.36, "text": "는 것이다."}])
        self.assertEqual(
            first_result[0]["text"] + second_result[0]["text"],
            "비교하는 것이다.",
        )
        engine.close()

    def test_healthy_session_keeps_a_new_repeated_phrase_at_the_boundary(self):
        def first(seq, _start, _end):
            return [
                transcript(
                    "정말",
                    0,
                    [("정", 9.40, 9.50), ("말", 9.50, 9.60)],
                    seq_id=seq,
                )
            ]

        def second(seq, _start, _end):
            return [
                transcript(
                    "정말 맞다",
                    2,
                    [
                        ("정", 9.59, 9.67),
                        ("말", 9.67, 9.78),
                        ("맞", 10.04, 10.16),
                        ("다", 10.16, 10.30),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first, second]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        first_result = engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("healthy-repeat-first"),
        )
        second_result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("healthy-repeat-second"),
        )

        self.assertEqual(len(factory.stubs), 1)
        self.assertEqual(first_result[0]["text"], "정말")
        self.assertEqual(
            second_result,
            [{"start": 2.59, "end": 3.3, "text": "정말 맞다"}],
        )
        engine.close()

    def test_overlap_only_final_closes_without_duplicate_provider_data(self):
        def first(seq, _start, _end):
            return [transcript("앞", 0, [("앞", 0.1, 0.2)], seq_id=seq)]

        factory = FakeFactory([[first]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        engine.transcribe(
            np.zeros(16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("first"),
        )
        kwargs = dict(
            lecture_id="lecture",
            username="owner",
            start_seconds=0.5,
            payload_hash=digest("overlap-only"),
        )
        result = engine.transcribe(
            np.zeros(8_000, dtype=np.float32),
            "ko",
            overlap_seconds=0.5,
            final_chunk=True,
            **kwargs,
        )
        self.assertEqual(result, [])
        self.assertEqual(len(factory.stubs), 1)
        self.assertEqual(len(factory.stubs[0].data_groups), 1)
        self.assertTrue(factory.channels[0].closed.is_set())
        self.assertEqual(engine.status()["active_sessions"], 0)
        replay = engine.transcribe(
            np.zeros(8_000, dtype=np.float32),
            "ko",
            overlap_seconds=0.5,
            final_chunk=True,
            **kwargs,
        )
        self.assertEqual(replay, [])
        self.assertEqual(len(factory.stubs), 1)

    def test_rotation_seeds_overlap_and_filters_it_by_alignment_timestamp(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [transcript("앞", 0, [("앞", 0.8, 0.9)], seq_id=seq)]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "앞 뒤",
                    0,
                    [("앞", 0.2, 0.4), (" ", 0.4, 0.55), ("뒤", 0.7, 0.9)],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        engine.transcribe(
            np.zeros(16_000, dtype=np.float32), "ko", 0, False,
            lecture_id="lecture", username="owner", start_seconds=0, payload_hash=digest("first"),
        )
        clock.value = 241
        result = engine.transcribe(
            np.zeros(24_000, dtype=np.float32), "ko", 0.5, False,
            lecture_id="lecture", username="owner", start_seconds=0.5, payload_hash=digest("second"),
        )
        self.assertEqual(len(factory.stubs), 2)
        self.assertTrue(factory.channels[0].closed.is_set())
        sent = sum(len(value.data.chunk) for value in factory.stubs[1].data_groups[0])
        self.assertEqual(sent, 48_000)  # fresh session receives overlap as context
        self.assertEqual(result, [{"start": 0.7, "end": 0.9, "text": "뒤"}])
        engine.close()

    def test_rotation_recovers_unemitted_context_suffix_exactly_once(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "비교하",
                    0,
                    [("비", 8.0, 8.4), ("교", 8.4, 8.8), ("하", 8.8, 9.6)],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "비교하는 것이다.",
                    0,
                    [
                        ("비", 0.50, 0.80),
                        ("교", 0.80, 1.10),
                        ("하", 1.10, 2.55),
                        ("는", 2.56, 2.70),
                        ("것", 2.75, 2.92),
                        ("이", 3.05, 3.15),
                        ("다", 3.15, 3.40),
                        (".", 3.40, 3.45),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        first_result = engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("boundary-first"),
        )
        clock.value = 241
        second_kwargs = dict(
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("boundary-second"),
        )
        second_result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            **second_kwargs,
        )

        self.assertEqual(first_result[0]["text"], "비교하")
        self.assertEqual(second_result[0]["text"], "는 것이다.")
        self.assertGreaterEqual(second_result[0]["start"], 2.56)
        self.assertLess(second_result[0]["start"], 3.0)
        self.assertEqual(
            first_result[0]["text"] + second_result[0]["text"],
            "비교하는 것이다.",
        )
        self.assertEqual(len(factory.stubs), 2)
        self.assertTrue(factory.channels[0].closed.is_set())
        self.assertTrue(factory.channels[1].closed.is_set())
        self.assertNotIn(("owner", "lecture"), engine._continuity)

        replay = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            **second_kwargs,
        )
        self.assertEqual(replay, second_result)
        self.assertEqual(len(factory.stubs), 2)
        self.assertNotIn(("owner", "lecture"), engine._continuity)
        engine.close()

    def test_rotation_text_anchor_removes_replay_despite_timestamp_drift(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "경계",
                    0,
                    [("경", 9.70, 9.82), ("계", 9.82, 9.95)],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "경계 새 내용",
                    0,
                    [
                        ("경", 2.70, 3.05),
                        ("계", 3.05, 3.16),
                        ("새", 3.17, 3.30),
                        ("내", 3.31, 3.45),
                        ("용", 3.45, 3.60),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("drift-first"),
        )
        clock.value = 241
        result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("drift-second"),
        )

        self.assertEqual(result, [{"start": 3.17, "end": 3.6, "text": "새 내용"}])
        engine.close()

    def test_rotation_matches_replay_through_the_rest_of_the_overlap(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "비교하는 것",
                    0,
                    [
                        ("비", 8.30, 8.42),
                        ("교", 8.44, 8.55),
                        ("하", 8.56, 8.65),
                        ("는", 8.65, 8.70),
                        ("것", 8.70, 8.74),
                    ],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "교하는 것이다.",
                    0,
                    [
                        ("교", 2.44, 2.57),
                        ("하", 2.57, 2.68),
                        ("는", 2.68, 2.79),
                        ("것", 2.80, 2.92),
                        ("이", 3.05, 3.14),
                        ("다", 3.14, 3.28),
                        (".", 3.28, 3.30),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        engine.transcribe(
            np.zeros(round(9.1 * 16_000), dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("long-shift-first"),
        )
        clock.value = 241
        result = engine.transcribe(
            np.zeros(round(6.5 * 16_000), dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=6.1,
            payload_hash=digest("long-shift-second"),
        )

        self.assertEqual(result, [{"start": 3.05, "end": 3.3, "text": "이다."}])
        engine.close()

    def test_rotation_promotes_unemitted_context_without_a_text_anchor(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "비교하",
                    0,
                    [("비", 8.0, 8.4), ("교", 8.4, 8.8), ("하", 8.8, 9.6)],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "는 것이다.",
                    0,
                    [
                        ("는", 2.56, 2.70),
                        ("것", 2.75, 2.92),
                        ("이", 3.05, 3.15),
                        ("다", 3.15, 3.40),
                        (".", 3.40, 3.45),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("anchorless-first"),
        )
        clock.value = 241
        result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("anchorless-second"),
        )

        self.assertEqual(
            result,
            [{"start": 2.56, "end": 3.45, "text": "는 것이다."}],
        )
        engine.close()

    def test_rotation_recovers_unemitted_suffix_with_negative_timestamp_drift(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "비교하",
                    0,
                    [("비", 8.2, 8.5), ("교", 8.5, 8.9), ("하", 8.9, 9.8)],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "는 것이다.",
                    0,
                    [
                        ("는", 2.62, 2.74),
                        ("것", 2.75, 2.88),
                        ("이", 3.04, 3.14),
                        ("다", 3.14, 3.31),
                        (".", 3.31, 3.33),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("negative-drift-first"),
        )
        clock.value = 241
        result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("negative-drift-second"),
        )

        self.assertEqual(
            result,
            [{"start": 2.62, "end": 3.33, "text": "는 것이다."}],
        )
        engine.close()

    def test_rotation_does_not_drop_new_one_character_prefix_collision(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [transcript("가", 0, [("가", 9.4, 9.6)], seq_id=seq)]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "가나다",
                    0,
                    [
                        ("가", 2.61, 2.72),
                        ("나", 2.73, 2.86),
                        ("다", 3.02, 3.18),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("one-character-first"),
        )
        clock.value = 241
        result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("one-character-second"),
        )

        self.assertEqual(result, [{"start": 2.61, "end": 3.18, "text": "가나다"}])
        engine.close()

    def test_rotation_keeps_a_new_repeated_phrase_inside_timestamp_dead_zone(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "정말",
                    0,
                    [("정", 9.40, 9.50), ("말", 9.50, 9.60)],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "정말 맞다",
                    0,
                    [
                        ("정", 2.59, 2.67),
                        ("말", 2.67, 2.78),
                        ("맞", 3.04, 3.16),
                        ("다", 3.16, 3.30),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        first_result = engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("rotation-repeat-first"),
        )
        clock.value = 241
        second_result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("rotation-repeat-second"),
        )

        self.assertEqual(first_result[0]["text"], "정말")
        self.assertEqual(
            second_result,
            [{"start": 2.59, "end": 3.3, "text": "정말 맞다"}],
        )
        engine.close()

    def test_rotation_keeps_a_new_repeat_after_a_certain_replay_prefix(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "정말 정말",
                    0,
                    [
                        ("정", 8.80, 8.90),
                        ("말", 8.90, 9.00),
                        ("정", 9.30, 9.45),
                        ("말", 9.45, 9.60),
                    ],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "정말 정말 맞다",
                    0,
                    [
                        # This first phrase is replayed overlap, while the
                        # identical phrase at the timestamp frontier is new.
                        ("정", 2.30, 2.40),
                        ("말", 2.40, 2.50),
                        ("정", 2.61, 2.70),
                        ("말", 2.70, 2.80),
                        ("맞", 3.05, 3.15),
                        ("다", 3.15, 3.28),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        first_result = engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("periodic-repeat-first"),
        )
        clock.value = 241
        second_result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("periodic-repeat-second"),
        )

        self.assertEqual(first_result[0]["text"], "정말 정말")
        self.assertEqual(
            second_result,
            [{"start": 2.61, "end": 3.28, "text": "정말 맞다"}],
        )
        engine.close()

    def test_rotation_keeps_a_new_repeat_after_a_crossing_replay_word(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "정말 정말",
                    0,
                    [("정말", 8.80, 9.00), ("정말", 9.30, 9.60)],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "정말 정말 맞다",
                    0,
                    [
                        # The replayed word crosses the 2.60-second frontier,
                        # so none of it is temporally certain. The next equal
                        # word is nevertheless a new utterance and must remain.
                        ("정말", 2.30, 2.70),
                        ("정말", 2.72, 2.92),
                        ("맞다", 3.05, 3.28),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        first_result = engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("crossing-repeat-first"),
        )
        clock.value = 241
        second_result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("crossing-repeat-second"),
        )

        self.assertEqual(first_result[0]["text"], "정말 정말")
        self.assertEqual(
            second_result,
            [{"start": 2.72, "end": 3.28, "text": "정말 맞다"}],
        )
        engine.close()

    def test_rotation_does_not_extend_certain_replay_into_a_new_repeat(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "정말 정말",
                    0,
                    [
                        ("정", 8.80, 8.90),
                        ("말", 8.90, 9.00),
                        ("정", 9.30, 9.45),
                        ("말", 9.45, 9.60),
                    ],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "정말 정말 맞다",
                    0,
                    [
                        # Negative timestamp drift puts the first character of
                        # the new repeat before the 2.60-second frontier. Thus
                        # temporal certainty is three characters, between the
                        # two valid two- and four-character text overlaps.
                        ("정", 2.20, 2.46),
                        ("말", 2.46, 2.50),
                        ("정", 2.50, 2.58),
                        ("말", 2.61, 2.75),
                        ("맞", 3.05, 3.15),
                        ("다", 3.15, 3.28),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        first_result = engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("partial-certain-repeat-first"),
        )
        clock.value = 241
        second_result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("partial-certain-repeat-second"),
        )

        self.assertEqual(first_result[0]["text"], "정말 정말")
        self.assertEqual(
            second_result,
            [{"start": 2.5, "end": 3.28, "text": "정말 맞다"}],
        )
        engine.close()

    def test_rotation_lexical_fallback_keeps_a_new_punctuated_repeat(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "정말! 정말!",
                    0,
                    [
                        ("정", 8.80, 8.89),
                        ("말", 8.89, 8.98),
                        ("!", 8.98, 9.00),
                        ("정", 9.30, 9.43),
                        ("말", 9.43, 9.58),
                        ("!", 9.58, 9.60),
                    ],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "정말? 정말? 맞다",
                    0,
                    [
                        # Different punctuation forces lexical fallback. Only
                        # this first phrase is replay; the equal phrase at the
                        # timestamp frontier is newly spoken.
                        ("정", 2.30, 2.39),
                        ("말", 2.39, 2.48),
                        ("?", 2.48, 2.50),
                        ("정", 2.61, 2.70),
                        ("말", 2.70, 2.78),
                        ("?", 2.78, 2.80),
                        ("맞", 3.05, 3.15),
                        ("다", 3.15, 3.28),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        first_result = engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("lexical-repeat-first"),
        )
        clock.value = 241
        second_result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("lexical-repeat-second"),
        )

        self.assertEqual(first_result[0]["text"], "정말! 정말!")
        self.assertEqual(
            second_result,
            [{"start": 2.61, "end": 3.28, "text": "정말? 맞다"}],
        )
        engine.close()

    def test_rotation_lexical_fallback_does_not_extend_certain_replay(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "정말! 정말!",
                    0,
                    [
                        ("정", 8.80, 8.89),
                        ("말", 8.89, 8.98),
                        ("!", 8.98, 9.00),
                        ("정", 9.30, 9.43),
                        ("말", 9.43, 9.58),
                        ("!", 9.58, 9.60),
                    ],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "정말? 정말? 맞다",
                    0,
                    [
                        # Exact matching fails on punctuation. After removing
                        # it, temporal certainty is three lexical characters,
                        # between the two- and four-character overlaps.
                        ("정", 2.20, 2.46),
                        ("말", 2.46, 2.49),
                        ("?", 2.49, 2.50),
                        ("정", 2.50, 2.58),
                        ("말", 2.61, 2.70),
                        ("?", 2.70, 2.72),
                        ("맞", 3.05, 3.15),
                        ("다", 3.15, 3.28),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        first_result = engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("partial-certain-lexical-first"),
        )
        clock.value = 241
        second_result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("partial-certain-lexical-second"),
        )

        self.assertEqual(first_result[0]["text"], "정말! 정말!")
        self.assertEqual(
            second_result,
            [{"start": 2.5, "end": 3.28, "text": "정말? 맞다"}],
        )
        engine.close()

    def test_rotation_does_not_count_punctuation_as_a_second_anchor_character(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "가?",
                    0,
                    [("가", 9.40, 9.58), ("?", 9.58, 9.61)],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "가? 나",
                    0,
                    [
                        ("가", 2.61, 2.70),
                        ("?", 2.70, 2.72),
                        ("나", 3.04, 3.18),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("short-punctuated-anchor-first"),
        )
        clock.value = 241
        result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("short-punctuated-anchor-second"),
        )

        self.assertEqual(result, [{"start": 2.61, "end": 3.18, "text": "가? 나"}])
        engine.close()

    def test_rotation_never_splits_an_nfkc_expansion_while_deduplicating(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "株式",
                    0,
                    [("株", 9.40, 9.50), ("式", 9.50, 9.60)],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "㍿ 새",
                    0,
                    [("㍿", 2.60, 3.02), ("새", 3.08, 3.20)],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("nfkc-first"),
        )
        clock.value = 241
        result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("nfkc-second"),
        )

        self.assertEqual(result, [{"start": 2.6, "end": 3.2, "text": "㍿ 새"}])
        engine.close()

    def test_rotation_does_not_leave_an_orphaned_combining_mark(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "ab",
                    0,
                    [("a", 9.40, 9.50), ("b", 9.50, 9.60)],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "ab\u0301 new",
                    0,
                    [
                        ("a", 2.40, 2.50),
                        ("b\u0301", 2.50, 2.72),
                        ("new", 3.08, 3.22),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "en",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("combining-first"),
        )
        clock.value = 241
        result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "en",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("combining-second"),
        )

        self.assertEqual(result, [{"start": 3.08, "end": 3.22, "text": "new"}])
        engine.close()

    def test_rotation_after_silence_does_not_apply_stale_text_anchor(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [transcript("가", 0, [("가", 1.0, 1.2)], seq_id=seq)]

        def silent(seq, _start, _end):
            return [transcript("", 1, [], seq_id=seq)]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "가나다",
                    0,
                    [
                        ("가", 0.05, 0.18),
                        ("나", 0.19, 0.31),
                        ("다", 0.32, 0.45),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first, silent], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("before-silence"),
        )
        quiet = engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("silence"),
        )
        self.assertEqual(quiet, [])
        clock.value = 241
        result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=14,
            payload_hash=digest("after-silence"),
        )

        self.assertEqual(result, [{"start": 0.05, "end": 0.45, "text": "가나다"}])
        engine.close()

    def test_rotation_preserves_new_punctuation_after_replay_anchor(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "안녕",
                    0,
                    [("안", 9.70, 9.82), ("녕", 9.82, 9.95)],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "안녕? 왜",
                    0,
                    [
                        ("안", 2.70, 3.05),
                        ("녕", 3.05, 3.16),
                        ("?", 3.16, 3.17),
                        ("왜", 3.18, 3.30),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("punctuation-first"),
        )
        clock.value = 241
        result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("punctuation-second"),
        )

        self.assertEqual(result, [{"start": 3.16, "end": 3.3, "text": "? 왜"}])
        engine.close()

    def test_rotation_does_not_repeat_punctuation_already_in_anchor(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "안녕?",
                    0,
                    [
                        ("안", 9.70, 9.82),
                        ("녕", 9.82, 9.95),
                        ("?", 9.95, 10.0),
                    ],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "안녕? 왜",
                    0,
                    [
                        ("안", 2.70, 3.05),
                        ("녕", 3.05, 3.12),
                        ("?", 3.12, 3.14),
                        ("왜", 3.15, 3.30),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("existing-punctuation-first"),
        )
        clock.value = 241
        result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("existing-punctuation-second"),
        )

        self.assertEqual(result, [{"start": 3.15, "end": 3.3, "text": "왜"}])
        engine.close()

    def test_rotation_deduplicates_words_when_provider_repunctuates_anchor(self):
        clock = FakeClock()

        def first(seq, _start, _end):
            return [
                transcript(
                    "안녕!",
                    0,
                    [
                        ("안", 9.70, 9.82),
                        ("녕", 9.82, 9.95),
                        ("!", 9.95, 10.0),
                    ],
                    seq_id=seq,
                )
            ]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "안녕? 왜",
                    0,
                    [
                        ("안", 2.70, 3.05),
                        ("녕", 3.05, 3.12),
                        ("?", 3.12, 3.14),
                        ("왜", 3.15, 3.30),
                    ],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [rotated]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory, clock=clock)
        engine.transcribe(
            np.zeros(10 * 16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("repunctuation-first"),
        )
        clock.value = 241
        result = engine.transcribe(
            np.zeros(5 * 16_000, dtype=np.float32),
            "ko",
            overlap_seconds=3,
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=7,
            payload_hash=digest("repunctuation-second"),
        )

        self.assertEqual(result, [{"start": 3.15, "end": 3.3, "text": "왜"}])
        engine.close()

    def test_rotation_age_reserves_the_configured_response_timeout(self):
        engine = ClovaStreamingTranscriber(
            settings(
                clova_stream_max_age_seconds=270,
                clova_stream_response_timeout_seconds=45,
            ),
            stub_factory=FakeFactory([]),
        )
        self.assertEqual(engine._max_age, 240)

    def test_final_alignment_rounding_past_pcm_end_is_clamped_not_dropped(self):
        def plan(seq, _start, _end):
            return [transcript("끝", 0, [("끝", 0.95, 1.05)], seq_id=seq)]

        factory = FakeFactory([[plan]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        result = engine.transcribe(
            np.zeros(16_000, dtype=np.float32),
            "ko",
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("rounded-tail"),
        )
        self.assertEqual(result, [{"start": 0.95, "end": 1.0, "text": "끝"}])

    def test_timeline_discontinuity_rotates_at_the_previous_flushed_boundary(self):
        def one(seq, _start, _end):
            return [transcript("한", 0, [("한", 0.1, 0.2)], seq_id=seq)]

        def two(seq, _start, _end):
            return [transcript("문 새", 0, [("문", 0.1, 0.2), (" ", 0.2, 0.6), ("새", 0.7, 0.8)], seq_id=seq)]

        factory = FakeFactory([[one], [two]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        engine.transcribe(
            np.zeros(16_000, dtype=np.float32), "ko", 0, False,
            lecture_id="lecture", username="owner", start_seconds=0, payload_hash=digest("one"),
        )
        result = engine.transcribe(
            np.zeros(24_000, dtype=np.float32), "ko", 0.5, False,
            lecture_id="lecture", username="owner", start_seconds=10, payload_hash=digest("two"),
        )
        self.assertEqual(len(factory.stubs), 2)
        self.assertTrue(factory.channels[0].closed.is_set())
        self.assertEqual(result[0]["text"], "새")
        engine.close()

    def test_positions_replace_uncommitted_tail_instead_of_blind_append(self):
        def plan(seq, _start, _end):
            return [
                transcript("오자", 0, [("오", 0.1, 0.2), ("자", 0.2, 0.3)], seq_id=0, ep_flag=False),
                transcript("정정", 0, [("정", 0.2, 0.3), ("정", 0.3, 0.4)], seq_id=seq),
            ]

        factory = FakeFactory([[plan]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        result = engine.transcribe(
            np.zeros(16_000, dtype=np.float32), "ko", final_chunk=True,
            lecture_id="lecture", username="owner", start_seconds=0, payload_hash=digest("positions"),
        )
        self.assertEqual(result, [{"start": 0.2, "end": 0.4, "text": "정정"}])

    def test_silent_ack_may_omit_optional_alignments(self):
        def plan(seq, _start, _end):
            response = transcript("", 0, [], seq_id=seq)
            response["transcription"].pop("alignInfos")
            return [response]

        factory = FakeFactory([[plan]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        result = engine.transcribe(
            np.zeros(16_000, dtype=np.float32), "ko", final_chunk=True,
            lecture_id="silent", username="owner", start_seconds=0,
            payload_hash=digest("silent-without-alignments"),
        )
        self.assertEqual(result, [])

    def test_documented_control_response_does_not_complete_the_audio_batch(self):
        def plan(seq, _start, _end):
            return [
                {
                    "responseType": ["semanticEpd"],
                    "semanticEpd": {"status": "Success"},
                },
                transcript("가", 0, [("가", 0.1, 0.2)], seq_id=seq),
            ]

        factory = FakeFactory([[plan]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        result = engine.transcribe(
            np.zeros(16_000, dtype=np.float32),
            "ko",
            final_chunk=True,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("control-before-ack"),
        )
        self.assertEqual(result, [{"start": 0.1, "end": 0.2, "text": "가"}])

    def test_final_close_and_idempotent_cache_are_owner_scoped(self):
        def plan(word):
            return lambda seq, _start, _end: [
                transcript(word, 0, [(word, 0.1, 0.2)], seq_id=seq)
            ]

        factory = FakeFactory([[plan("가")], [plan("나")]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        kwargs = dict(
            lecture_id="shared-id", username="owner-a", start_seconds=0,
            payload_hash=digest("payload"),
        )
        first = engine.transcribe(np.zeros(16_000, dtype=np.float32), "ko", final_chunk=True, **kwargs)
        self.assertTrue(factory.channels[0].closed.is_set())
        self.assertEqual(engine.status()["active_sessions"], 0)
        replay = engine.transcribe(np.zeros(16_000, dtype=np.float32), "ko", final_chunk=True, **kwargs)
        self.assertEqual(replay, first)
        self.assertEqual(len(factory.stubs), 1)
        with self.assertRaises(ClovaTranscriptionError) as raised:
            engine.transcribe(
                np.zeros(16_000, dtype=np.float32), "ko", final_chunk=True,
                **{**kwargs, "payload_hash": digest("changed")},
            )
        self.assertEqual(raised.exception.code, "chunk_conflict")
        other = engine.transcribe(
            np.zeros(16_000, dtype=np.float32), "ko", final_chunk=True,
            **{**kwargs, "username": "owner-b"},
        )
        self.assertEqual(other[0]["text"], "나")
        self.assertEqual(len(factory.stubs), 2)

    def test_close_session_is_owner_isolated_and_idempotent(self):
        plan = lambda word: lambda seq, _start, _end: [
            transcript(word, 0, [(word, 0.1, 0.2)], seq_id=seq)
        ]
        factory = FakeFactory([[plan("가")], [plan("나")]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        for owner, word in (("owner-a", "a"), ("owner-b", "b")):
            engine.transcribe(
                np.zeros(16_000, dtype=np.float32), "ko", final_chunk=False,
                lecture_id="same", username=owner, start_seconds=0, payload_hash=digest(word),
            )
        self.assertIn(("owner-a", "same"), engine._continuity)
        self.assertIn(("owner-b", "same"), engine._continuity)
        engine.close_session("owner-a", "same")
        engine.close_session("owner-a", "same")
        self.assertNotIn(("owner-a", "same"), engine._continuity)
        self.assertIn(("owner-b", "same"), engine._continuity)
        self.assertTrue(factory.channels[0].closed.is_set())
        self.assertFalse(factory.channels[1].closed.is_set())
        self.assertEqual(engine.status()["active_sessions"], 1)
        engine.close()
        self.assertEqual(engine._continuity, {})
        self.assertTrue(factory.channels[1].closed.is_set())

    def test_close_session_purges_only_that_lectures_cached_transcripts(self):
        def plan(word):
            return lambda seq, _start, _end: [
                transcript(word, 0, [(word, 0.1, 0.2)], seq_id=seq)
            ]

        factory = FakeFactory([[plan("가")], [plan("나")], [plan("새")]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        common = dict(start_seconds=0, final_chunk=True, language="ko")
        owner_a = dict(
            lecture_id="lecture-a",
            username="owner",
            payload_hash=digest("a"),
        )
        owner_b = dict(
            lecture_id="lecture-b",
            username="owner",
            payload_hash=digest("b"),
        )
        engine.transcribe(np.zeros(16_000, dtype=np.float32), **common, **owner_a)
        engine.transcribe(np.zeros(16_000, dtype=np.float32), **common, **owner_b)
        self.assertEqual(engine.status()["cached_chunks"], 2)
        engine.close_session("owner", "lecture-a")
        engine.close_session("owner", "lecture-a")
        self.assertEqual(engine.status()["cached_chunks"], 1)
        # The other lecture remains an exactly-once cache hit.
        engine.transcribe(np.zeros(16_000, dtype=np.float32), **common, **owner_b)
        self.assertEqual(len(factory.stubs), 2)
        # The deleted lecture cannot return its old cached text and is sent to
        # a fresh provider session if the caller explicitly retries it.
        result = engine.transcribe(np.zeros(16_000, dtype=np.float32), **common, **owner_a)
        self.assertEqual(result[0]["text"], "새")
        self.assertEqual(len(factory.stubs), 3)

    def test_close_session_interrupts_an_inflight_ack_wait(self):
        factory = FakeFactory([[BLOCK]])
        engine = ClovaStreamingTranscriber(
            settings(clova_stream_response_timeout_seconds=2),
            stub_factory=factory,
        )
        errors = []

        def run_transcription():
            try:
                engine.transcribe(
                    np.zeros(16_000, dtype=np.float32),
                    "ko",
                    final_chunk=False,
                    lecture_id="lecture",
                    username="owner",
                    start_seconds=0,
                    payload_hash=digest("blocked"),
                )
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run_transcription)
        worker.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if factory.stubs and factory.stubs[0].data_groups:
                break
            time.sleep(0.005)
        self.assertTrue(factory.stubs and factory.stubs[0].data_groups)

        def run_queued_transcription():
            try:
                engine.transcribe(
                    np.zeros(24_000, dtype=np.float32),
                    "ko",
                    overlap_seconds=0.5,
                    final_chunk=False,
                    lecture_id="lecture",
                    username="owner",
                    start_seconds=0.5,
                    payload_hash=digest("queued"),
                )
            except Exception as error:
                errors.append(error)

        queued_worker = threading.Thread(target=run_queued_transcription)
        queued_worker.start()
        pending = 0
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with engine._state_lock:
                pending = len(engine._operation_tokens.get(("owner", "lecture"), ()))
            if pending == 2:
                break
            time.sleep(0.005)
        self.assertEqual(pending, 2)
        started = time.monotonic()
        engine.close_session("owner", "lecture")
        self.assertLess(time.monotonic() - started, 0.5)
        worker.join(1)
        queued_worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(queued_worker.is_alive())
        self.assertEqual(len(errors), 2)
        for error in errors:
            self.assertIsInstance(error, ClovaTranscriptionError)
            self.assertEqual(error.code, "provider_unavailable")
            self.assertTrue(error.retryable)
        self.assertEqual(len(factory.stubs), 1)
        self.assertTrue(factory.channels[0].closed.is_set())
        self.assertEqual(engine.status()["active_sessions"], 0)

    def test_close_session_during_transport_creation_sends_no_config_or_audio(self):
        entered = threading.Event()
        release = threading.Event()
        channel = FakeChannel()

        def plan(seq, _start, _end):
            return [transcript("가", 0, [("가", 0.1, 0.2)], seq_id=seq)]

        stub = FakeStub(channel, [plan])

        def blocking_factory():
            entered.set()
            release.wait(1)
            return stub, channel

        engine = ClovaStreamingTranscriber(settings(), stub_factory=blocking_factory)
        errors = []

        def run_transcription():
            try:
                engine.transcribe(
                    np.zeros(16_000, dtype=np.float32),
                    "ko",
                    final_chunk=False,
                    lecture_id="lecture",
                    username="owner",
                    start_seconds=0,
                    payload_hash=digest("transport-race"),
                )
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run_transcription)
        worker.start()
        self.assertTrue(entered.wait(1))
        engine.close_session("owner", "lecture")
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ClovaTranscriptionError)
        self.assertEqual(errors[0].code, "provider_unavailable")
        self.assertEqual(stub.requests, [])
        self.assertEqual(stub.metadata, [])
        self.assertTrue(channel.closed.is_set())

    def test_disconnect_between_acknowledged_chunks_rotates_before_new_audio(self):
        def first(seq, _start, _end):
            return [
                transcript("앞", 0, [("앞", 0.8, 0.9)], seq_id=seq),
                DISCONNECT,
            ]

        def recovered(seq, _start, _end):
            return [
                transcript(
                    "앞 뒤",
                    0,
                    [("앞", 0.2, 0.4), (" ", 0.4, 0.5), ("뒤", 0.7, 0.9)],
                    seq_id=seq,
                )
            ]

        factory = FakeFactory([[first], [recovered]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        engine.transcribe(
            np.zeros(16_000, dtype=np.float32),
            "ko",
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0,
            payload_hash=digest("first-before-disconnect"),
        )
        session = None
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with engine._state_lock:
                session = engine._sessions.get(("owner", "lecture"))
            if session is not None:
                with session.condition:
                    if session.error is not None:
                        break
            time.sleep(0.005)
        self.assertIsNotNone(session)
        with session.condition:
            self.assertIsNotNone(session.error)
        # Aggregate status performs lazy hygiene and must not count a reader
        # that has already failed as an active native stream.
        self.assertEqual(engine.status()["active_sessions"], 0)
        self.assertTrue(factory.channels[0].closed.is_set())
        result = engine.transcribe(
            np.zeros(24_000, dtype=np.float32),
            "ko",
            overlap_seconds=0.5,
            final_chunk=False,
            lecture_id="lecture",
            username="owner",
            start_seconds=0.5,
            payload_hash=digest("after-disconnect"),
        )
        self.assertEqual(result, [{"start": 0.7, "end": 0.9, "text": "뒤"}])
        self.assertEqual(len(factory.stubs), 2)
        sent = sum(
            len(request.data.chunk) for request in factory.stubs[1].data_groups[0]
        )
        self.assertEqual(sent, 48_000)
        engine.close()

    def test_idle_cleanup_and_session_count_are_bounded(self):
        clock = FakeClock()
        plan = lambda word: lambda seq, _start, _end: [
            transcript(word, 0, [(word, 0.1, 0.2)], seq_id=seq)
        ]
        factory = FakeFactory([[plan("가")], [plan("나")], [plan("다")]])
        engine = ClovaStreamingTranscriber(
            settings(clova_stream_idle_seconds=2), stub_factory=factory, clock=clock
        )
        engine.transcribe(
            np.zeros(16_000, dtype=np.float32), "ko", final_chunk=False,
            lecture_id="a", username="owner", start_seconds=0, payload_hash=digest("a"),
        )
        clock.value = 3
        engine.transcribe(
            np.zeros(16_000, dtype=np.float32), "ko", final_chunk=False,
            lecture_id="b", username="owner", start_seconds=0, payload_hash=digest("b"),
        )
        self.assertTrue(factory.channels[0].closed.is_set())
        with patch("server.clova_transcriber.MAX_ACTIVE_SESSIONS", 1):
            engine.transcribe(
                np.zeros(16_000, dtype=np.float32), "ko", final_chunk=False,
                lecture_id="c", username="owner", start_seconds=0, payload_hash=digest("c"),
            )
        self.assertTrue(factory.channels[1].closed.is_set())
        self.assertEqual(engine.status()["active_sessions"], 1)
        engine.close()

    def test_cache_count_is_bounded(self):
        plan = lambda word: lambda seq, _start, _end: [
            transcript(word, 0, [(word, 0.1, 0.2)], seq_id=seq)
        ]
        factory = FakeFactory([[plan("가")], [plan("나")], [plan("다")]])
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        with patch("server.clova_transcriber.MAX_CACHE_ENTRIES", 2):
            for index in range(3):
                engine.transcribe(
                    np.zeros(16_000, dtype=np.float32), "ko", final_chunk=True,
                    lecture_id=f"lecture-{index}", username="owner", start_seconds=0,
                    payload_hash=digest(str(index)),
                )
        self.assertEqual(engine.status()["cached_chunks"], 2)

    def test_config_must_report_documented_success_before_any_data(self):
        factory = FakeFactory(
            [[]],
            configs=[{"responseType": ["config"], "config": {"status": "Failure private body"}}],
        )
        engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
        with self.assertRaises(ClovaTranscriptionError) as raised:
            engine.transcribe(
                np.zeros(16_000, dtype=np.float32), "ko", final_chunk=True,
                lecture_id="lecture", username="owner", start_seconds=0, payload_hash=digest("x"),
            )
        self.assertEqual(raised.exception.code, "provider_rejected")
        self.assertEqual(len(factory.stubs[0].data_groups), 0)
        self.assertTrue(factory.channels[0].closed.is_set())
        self.assertNotIn("private", str(raised.exception))

    def test_malformed_oversize_nan_and_out_of_range_responses_fail_closed(self):
        valid = transcript("가", 0, [("가", 0.1, 0.2)], seq_id=1)
        cases = {
            "malformed": ["not-json"],
            "oversize": ["x" * (MAX_RESPONSE_BYTES + 1)],
            "nan": [{
                **copy.deepcopy(valid),
                "transcription": {**copy.deepcopy(valid["transcription"]), "confidence": float("nan")},
            }],
            "timestamp": [{
                **copy.deepcopy(valid),
                "transcription": {
                    **copy.deepcopy(valid["transcription"]),
                    "alignInfos": [{"word": "가", "start": 100, "end": 99_000, "confidence": 1}],
                },
            }],
            "position": [{
                **copy.deepcopy(valid),
                "transcription": {**copy.deepcopy(valid["transcription"]), "position": 9},
            }],
            "alignment_text": [{
                **copy.deepcopy(valid),
                "transcription": {
                    **copy.deepcopy(valid["transcription"]),
                    "alignInfos": [
                        {"word": "나", "start": 100, "end": 200, "confidence": 1}
                    ],
                },
            }],
        }
        for name, response in cases.items():
            with self.subTest(name=name):
                def plan(seq, _start, _end, response=response):
                    values = copy.deepcopy(response)
                    if values and isinstance(values[0], dict):
                        values[0].get("transcription", {})["seqId"] = seq
                    return values

                factory = FakeFactory([[plan]])
                engine = ClovaStreamingTranscriber(settings(), stub_factory=factory)
                with self.assertRaises(ClovaTranscriptionError) as raised:
                    engine.transcribe(
                        np.zeros(16_000, dtype=np.float32), "ko", final_chunk=True,
                        lecture_id="lecture", username="owner", start_seconds=0,
                        payload_hash=digest(name),
                    )
                self.assertEqual(raised.exception.code, "invalid_response")
                self.assertFalse(raised.exception.retryable)
                self.assertTrue(factory.channels[0].closed.is_set())
                self.assertEqual(engine.status()["active_sessions"], 0)

    def test_timeout_and_iterator_errors_are_sanitized_and_close_session(self):
        factory = FakeFactory([[BLOCK]])
        engine = ClovaStreamingTranscriber(
            settings(clova_stream_response_timeout_seconds=0.05), stub_factory=factory
        )
        started = time.monotonic()
        with self.assertRaises(ClovaTranscriptionError) as raised:
            engine.transcribe(
                np.zeros(16_000, dtype=np.float32), "ko", final_chunk=False,
                lecture_id="lecture", username="owner", start_seconds=0, payload_hash=digest("timeout"),
            )
        self.assertEqual(raised.exception.code, "provider_timeout")
        self.assertTrue(raised.exception.retryable)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(factory.channels[0].closed.is_set())
        self.assertEqual(engine.status()["active_sessions"], 0)

        private_detail = "Bearer unit-test-secret and provider response body"
        error_factory = FakeFactory([[RuntimeError(private_detail)]])
        error_engine = ClovaStreamingTranscriber(settings(), stub_factory=error_factory)
        with self.assertRaises(ClovaTranscriptionError) as sanitized:
            error_engine.transcribe(
                np.zeros(16_000, dtype=np.float32), "ko", final_chunk=False,
                lecture_id="lecture", username="owner", start_seconds=0, payload_hash=digest("error"),
            )
        self.assertEqual(sanitized.exception.code, "provider_unavailable")
        self.assertTrue(sanitized.exception.retryable)
        self.assertNotIn("Bearer", str(sanitized.exception))
        self.assertNotIn("body", repr(sanitized.exception))
        self.assertTrue(error_factory.channels[0].closed.is_set())

    def test_unconfigured_and_invalid_samples_never_open_a_channel(self):
        factory = FakeFactory([])
        engine = ClovaStreamingTranscriber(
            settings(clova_speech_secret_key=None), stub_factory=factory
        )
        with self.assertRaises(ClovaTranscriptionError) as missing:
            engine.transcribe(
                np.zeros(16_000, dtype=np.float32), "ko", final_chunk=True,
                lecture_id="lecture", username="owner", start_seconds=0, payload_hash=digest("x"),
            )
        self.assertEqual(missing.exception.code, "not_configured")
        self.assertEqual(factory.stubs, [])

        configured_factory = FakeFactory([])
        configured = ClovaStreamingTranscriber(settings(), stub_factory=configured_factory)
        bad = np.zeros(16_000, dtype=np.float32)
        bad[0] = np.nan
        with self.assertRaises(ClovaTranscriptionError) as invalid:
            configured.transcribe(
                bad, "ko", final_chunk=True,
                lecture_id="lecture", username="owner", start_seconds=0, payload_hash=digest("bad"),
            )
        self.assertEqual(invalid.exception.code, "invalid_input")
        self.assertEqual(configured_factory.stubs, [])

    def test_secret_validation_matches_server_settings_constraints(self):
        for secret in ("short", "valid secret value", "x" * 513):
            with self.subTest(secret_length=len(secret)):
                engine = ClovaStreamingTranscriber(
                    settings(clova_speech_secret_key=secret),
                    stub_factory=FakeFactory([]),
                )
                self.assertFalse(engine.configured)
                self.assertEqual(engine.status()["model_state"], "unconfigured")


if __name__ == "__main__":
    unittest.main()
