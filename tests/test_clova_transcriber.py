from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import unittest
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
            return [transcript("앞", 0, [("앞", 0.1, 0.2)], seq_id=seq)]

        def rotated(seq, _start, _end):
            return [
                transcript(
                    "앞 뒤",
                    0,
                    [("앞", 0.1, 0.25), (" ", 0.25, 0.55), ("뒤", 0.7, 0.9)],
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
        engine.close_session("owner-a", "same")
        engine.close_session("owner-a", "same")
        self.assertTrue(factory.channels[0].closed.is_set())
        self.assertFalse(factory.channels[1].closed.is_set())
        self.assertEqual(engine.status()["active_sessions"], 1)
        engine.close()
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
                transcript("앞", 0, [("앞", 0.1, 0.2)], seq_id=seq),
                DISCONNECT,
            ]

        def recovered(seq, _start, _end):
            return [
                transcript(
                    "앞 뒤",
                    0,
                    [("앞", 0.1, 0.2), (" ", 0.2, 0.5), ("뒤", 0.7, 0.9)],
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
