"""Private fixture DB/WAV and fake range streams only; no external APIs."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server.app import create_app
from server.drive_storage import DriveIntegrityError
from server.recordings import SAMPLE_RATE, _header
from server.security import digest
from server.settings import Settings


class FakeTranscriber:
    def status(self):
        return {"model_state": "ready", "model": "fixture", "device": "cpu"}


class FakeRange:
    def __init__(self, content, start, end, *, problem=None):
        self.status_code = 200 if problem == "status" else 206
        self.content_length = end - start + (1 if problem == "length" else 0)
        self.content_range = f"bytes {start}-{end - 1}/{len(content)}" if problem != "range" else "PRIVATE-range"
        self.payload = content[start:end]
        if problem == "short":
            self.payload = self.payload[:-1]
        if problem == "long":
            self.payload += b"extra"
        self.problem = problem
        self.closed = False

    def iter_bytes(self):
        if self.problem == "read":
            raise RuntimeError("PRIVATE-provider-error")
        for offset in range(0, len(self.payload), 65536):
            yield self.payload[offset:offset + 65536]

    def close(self):
        self.closed = True


class LectureToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(data_dir=root / "data", model_cache_dir=root / "models",
                                 accounts=("user-alpha", "user-beta"), admin_username="user-alpha",
                                 max_import_seconds=600, recording_free_reserve_bytes=0,
                                 site_origins=("https://student.github.io",))
        self.app = create_app(self.settings, FakeTranscriber())
        self.database = self.app.state.database
        self.store = self.app.state.recording_store
        self.archive = self.app.state.archive_manager
        self.client = TestClient(self.app)
        self.tokens = {"user-alpha": "fixture-alpha-token", "user-beta": "fixture-beta-token"}
        with self.database.connect() as connection:
            for username, token in self.tokens.items():
                connection.execute(
                    "INSERT INTO sessions(token_hash,username,expires_at,created_at) VALUES (?,?,?,?)",
                    (digest(token), username, time.time() + 3600, time.time()),
                )

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def headers(self, username="user-alpha"):
        return {"Authorization": f"Bearer {self.tokens[username]}"}

    def lecture(self, username="user-alpha", *, finalized=True, seconds=3):
        lecture_id = str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO lectures(id,username,title,language,created_at,recording_finalized) "
                "VALUES (?,?,'PRIVATE-title','ko','2026-09-06T00:00:00Z',?)",
                (lecture_id, username, int(finalized)),
            )
        pcm = (b"\x01\x02\x03\x04" * (SAMPLE_RATE // 2)) * seconds
        if pcm:
            self.store.write_chunk(username, lecture_id, start_seconds=0, overlap_seconds=0, pcm=pcm)
        return lecture_id, pcm

    def clip(self, lecture_id, start=0, duration=60, username="user-alpha"):
        return self.client.get(f"/lectures/{lecture_id}/recording-clip",
                               params={"start": start, "duration": duration}, headers=self.headers(username))

    def mark(self, lecture_id, *, start=1, label="중요", bookmark_id=None, username="user-alpha"):
        return self.client.post(f"/lectures/{lecture_id}/bookmarks", headers=self.headers(username),
                                json={"id": bookmark_id or str(uuid.uuid4()), "start_seconds": start, "label": label})

    def fake_remote(self, lecture_id, content, *, problem=None, during=None):
        streams, ranges = [], []

        def open_range(identifier, *, start, end):
            self.assertEqual(identifier, lecture_id)
            ranges.append((start, end))
            if during:
                during()
            stream = FakeRange(content, start, end, problem=problem)
            streams.append(stream)
            return stream

        return (mock.patch.object(self.archive, "remote_size", return_value=len(content)),
                mock.patch.object(self.archive, "open_download", side_effect=open_range), streams, ranges)

    def test_local_clip_returns_exact_frame_range_and_safe_headers(self):
        lecture_id, pcm = self.lecture()
        response = self.clip(lecture_id, 0.5, 1.25)
        expected = pcm[16000:56000]
        self.assertEqual(response.status_code, 200, response.text[:100] if response.status_code != 200 else "")
        self.assertEqual(response.content, _header(len(expected)) + expected)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-clip-start-seconds"], "0.5")
        self.assertEqual(response.headers["x-clip-duration-seconds"], "1.25")
        self.assertNotIn("PRIVATE", str(response.headers))

    def test_near_end_truncates_duration_and_fractional_start_is_frame_aligned(self):
        lecture_id, pcm = self.lecture()
        response = self.clip(lecture_id, 2.50001, 60)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, _header(16000) + pcm[80000:])
        self.assertEqual(response.headers["x-clip-start-seconds"], "2.5")
        self.assertEqual(response.headers["x-clip-duration-seconds"], "0.5")

    def test_clip_rejects_nonfinite_negative_or_outside_bounds(self):
        lecture_id, _ = self.lecture()
        for start, duration in ((-1, 60), ("NaN", 60), ("inf", 60), ("bad", 60),
                                (0, "NaN"), (0, 0), (0, -1), (0, 121), (601, 1), (3, 1), (0, 0.000001)):
            with self.subTest(start=start, duration=duration):
                response = self.clip(lecture_id, start, duration)
                self.assertEqual(response.status_code, 416, response.text)

    def test_maximum_clip_reads_only_header_and_requested_pcm_not_entire_local_file(self):
        lecture_id, pcm = self.lecture(seconds=180)
        reads = []
        real_read = os.read
        def read(descriptor, length):
            reads.append(length)
            return real_read(descriptor, length)
        with mock.patch("server.lecture_tools.os.read", side_effect=read), \
                mock.patch.object(self.archive, "local_download_matches_archive", side_effect=AssertionError("No full hash")):
            response = self.clip(lecture_id, 10, 120)
        self.assertEqual(response.status_code, 200)
        expected = pcm[320000:4160000]
        self.assertEqual(response.content, _header(len(expected)) + expected)
        self.assertEqual(len(response.content), 44 + 120 * 32000)
        self.assertEqual(sum(reads), 44 + 120 * 32000)
        self.assertLess(sum(reads), len(pcm))
        self.assertLessEqual(max(reads), 256 * 1024)

    def test_local_descriptor_is_closed_on_success_and_error(self):
        lecture_id, _ = self.lecture()
        opened = []
        real_open = self.store.open_info
        def recording(*args):
            result = real_open(*args)
            opened.append(result["descriptor"])
            return result
        with mock.patch.object(self.store, "open_info", side_effect=recording):
            self.assertEqual(self.clip(lecture_id).status_code, 200)
            with mock.patch("server.lecture_tools._read_descriptor", side_effect=OSError("PRIVATE-path")):
                response = self.clip(lecture_id)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("PRIVATE", response.text)
        for descriptor in opened:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_remote_clip_prefers_verified_range_and_never_reads_local_duplicate(self):
        lecture_id, pcm = self.lecture()
        patches = self.fake_remote(lecture_id, _header(len(pcm)) + pcm)
        with patches[0], patches[1], mock.patch.object(self.store, "open_info", side_effect=AssertionError("No local copy")):
            response = self.clip(lecture_id, 1, 1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, _header(32000) + pcm[32000:64000])
        self.assertEqual(patches[3], [(0, 44), (32044, 64044)])
        self.assertTrue(all(stream.closed for stream in patches[2]))

    def test_remote_clip_at_zero_uses_one_bounded_range(self):
        lecture_id, pcm = self.lecture()
        patches = self.fake_remote(lecture_id, _header(len(pcm)) + pcm)
        with patches[0], patches[1]:
            response = self.clip(lecture_id, 0, 1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(patches[3], [(0, 32044)])
        self.assertTrue(all(stream.closed for stream in patches[2]))

    def test_remote_corruption_missing_data_and_protocol_errors_fail_closed(self):
        lecture_id, pcm = self.lecture()
        for problem in ("status", "length", "range", "short", "long", "read"):
            with self.subTest(problem=problem):
                patches = self.fake_remote(lecture_id, _header(len(pcm)) + pcm, problem=problem)
                with patches[0], patches[1], mock.patch.object(self.store, "open_info", side_effect=AssertionError("No fallback")):
                    response = self.clip(lecture_id, 1, 1)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("PRIVATE", response.text)
                self.assertTrue(all(stream.closed for stream in patches[2]))
        patches = self.fake_remote(lecture_id, b"INVALID" + b"\0" * 37 + pcm)
        with patches[0], patches[1]:
            self.assertEqual(self.clip(lecture_id, 1, 1).status_code, 503)
        self.assertEqual(patches[3], [(0, 44)])

    def test_remote_metadata_verification_failure_has_no_unverified_local_fallback(self):
        lecture_id, pcm = self.lecture()
        with mock.patch.object(self.archive, "remote_size", return_value=len(pcm) + 44), \
                mock.patch.object(self.archive, "open_download", side_effect=DriveIntegrityError("mismatch", "PRIVATE-locator")), \
                mock.patch.object(self.store, "open_info", side_effect=AssertionError("No fallback")):
            response = self.clip(lecture_id)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("PRIVATE", response.text)

    def test_deleted_during_remote_read_does_not_return_audio(self):
        lecture_id, pcm = self.lecture()
        def deleting():
            with self.database.connect() as connection:
                connection.execute("UPDATE lectures SET deleting=1 WHERE id=?", (lecture_id,))
        patches = self.fake_remote(lecture_id, _header(len(pcm)) + pcm, during=deleting)
        with patches[0], patches[1]:
            self.assertEqual(self.clip(lecture_id).status_code, 503)
        self.assertTrue(all(stream.closed for stream in patches[2]))

    def test_unauthenticated_and_other_owner_including_administrator_are_rejected(self):
        lecture_id, _ = self.lecture("user-beta")
        bookmark_id = str(uuid.uuid4())
        for suffix in ("recording-clip", "bookmarks"):
            self.assertEqual(self.client.get(f"/lectures/{lecture_id}/{suffix}").status_code, 401)
            self.assertEqual(self.client.get(f"/lectures/{lecture_id}/{suffix}", headers=self.headers()).status_code, 404)
        self.assertEqual(self.mark(lecture_id, bookmark_id=bookmark_id).status_code, 404)
        self.assertEqual(self.client.delete(f"/lectures/{lecture_id}/bookmarks/{bookmark_id}", headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.post(f"/lectures/{lecture_id}/bookmarks", json={"id": bookmark_id,"start_seconds": 0,"label":"x"}).status_code, 401)

    def test_data_access_pause_and_nonfinalized_clip_are_rejected(self):
        lecture_id, _ = self.lecture(finalized=False)
        self.assertEqual(self.clip(lecture_id).status_code, 409)
        self.assertEqual(self.mark(lecture_id, start=30).status_code, 200)
        with self.database.connect() as connection:
            connection.execute("UPDATE operational_state SET access_enabled=0")
        self.assertEqual(self.mark(lecture_id).status_code, 503)
        self.assertEqual(self.clip(lecture_id).status_code, 503)

    def test_live_bookmarks_preserve_text_and_idempotent_body_and_time_order(self):
        lecture_id, _ = self.lecture(finalized=False)
        bookmark_id = str(uuid.uuid4())
        response = self.mark(lecture_id, start=90.5, label="<원문 대조>", bookmark_id=bookmark_id)
        self.assertEqual(response.status_code, 200, response.text)
        first = response.json()
        self.assertEqual(set(first["bookmark"]), {"id", "start_seconds", "label", "created_at"})
        self.assertEqual(self.mark(lecture_id, start=90.5, label="<원문 대조>", bookmark_id=bookmark_id).json(), first)
        self.assertEqual(self.mark(lecture_id, start=91, label="<원문 대조>", bookmark_id=bookmark_id).status_code, 409)
        self.assertEqual(self.mark(lecture_id, start=90.5, label="다른 이름", bookmark_id=bookmark_id).status_code, 409)
        self.mark(lecture_id, start=5)
        result = self.client.get(f"/lectures/{lecture_id}/bookmarks", headers=self.headers())
        self.assertEqual([item["start_seconds"] for item in result.json()["bookmarks"]], [5, 90.5])
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT recording_finalized FROM lectures WHERE id=?", (lecture_id,)).fetchone()[0], 0)
            for table in ("segments", "lecture_summaries", "lecture_translations", "transcript_corrections"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_bookmark_validation_is_bounded_and_errors_do_not_echo_private_labels(self):
        lecture_id, _ = self.lecture()
        for start, label, bookmark_id in ((-1, "x", str(uuid.uuid4())), (601, "x", str(uuid.uuid4())),
                                        (True, "x", str(uuid.uuid4())), ("5", "x", str(uuid.uuid4())),
                                        (0, "PRIVATE" * 30, str(uuid.uuid4())), (0, "PRIVATE\n", str(uuid.uuid4())),
                                        (0, "PRIVATE", "invalid-id")):
            response = self.mark(lecture_id, start=start, label=label, bookmark_id=bookmark_id)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn("PRIVATE", response.text)
        for value in ("NaN", "Infinity"):
            raw = '{"id":"' + str(uuid.uuid4()) + '","start_seconds":' + value + ',"label":"PRIVATE"}'
            response = self.client.post(f"/lectures/{lecture_id}/bookmarks", content=raw,
                                        headers=self.headers() | {"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("PRIVATE", response.text)

    def test_bookmark_id_cannot_move_across_lectures_or_owners(self):
        first, _ = self.lecture()
        second, _ = self.lecture()
        third, _ = self.lecture("user-beta")
        bookmark_id = str(uuid.uuid4())
        self.assertEqual(self.mark(first, bookmark_id=bookmark_id).status_code, 200)
        self.assertEqual(self.mark(second, bookmark_id=bookmark_id).status_code, 409)
        self.assertEqual(self.mark(third, bookmark_id=bookmark_id, username="user-beta").status_code, 409)
        self.assertEqual(self.client.delete(f"/lectures/{second}/bookmarks/{bookmark_id}", headers=self.headers()).status_code, 404)

    def test_bookmark_count_cap_is_atomic_and_existing_retry_still_succeeds(self):
        lecture_id, _ = self.lecture(finalized=False)
        existing_id = str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.executemany(
                "INSERT INTO lecture_bookmarks VALUES(?,?,?,?,'2026-09-06T00:00:00Z')",
                [(existing_id if index == 0 else str(uuid.uuid4()), lecture_id, 1, "중요") for index in range(499)],
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.mark(lecture_id), range(2)))
        self.assertEqual(sorted(response.status_code for response in results), [200, 409])
        self.assertEqual(self.mark(lecture_id, bookmark_id=existing_id).status_code, 200)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_bookmarks").fetchone()[0], 500)

    def test_bookmark_delete_is_bound_to_owner_and_lecture_and_cascades(self):
        lecture_id, _ = self.lecture()
        item = self.mark(lecture_id).json()["bookmark"]
        endpoint = f"/lectures/{lecture_id}/bookmarks/{item['id']}"
        self.assertEqual(self.client.delete(endpoint, headers=self.headers("user-beta")).status_code, 404)
        self.assertEqual(self.client.delete(endpoint, headers=self.headers()).json(), {"deleted": True})
        self.assertEqual(self.client.delete(endpoint, headers=self.headers()).status_code, 404)
        self.mark(lecture_id)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM lectures WHERE id=?", (lecture_id,))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_bookmarks").fetchone()[0], 0)

    def test_clip_and_bookmark_rate_limits_remain_separate(self):
        lecture_id, _ = self.lecture()
        bookmark_id = str(uuid.uuid4())
        for _ in range(30):
            self.assertEqual(self.clip(lecture_id, 0, 0.001).status_code, 200)
        self.assertEqual(self.clip(lecture_id, 0, 0.001).status_code, 429)
        for _ in range(60):
            self.assertEqual(self.mark(lecture_id, bookmark_id=bookmark_id).status_code, 200)
        self.assertEqual(self.mark(lecture_id, bookmark_id=bookmark_id).status_code, 429)
        self.assertEqual(self.client.get(f"/lectures/{lecture_id}/bookmarks", headers=self.headers()).status_code, 200)

    def test_clip_cors_headers_and_rejected_origin_keep_private_data_closed(self):
        lecture_id, _ = self.lecture()
        endpoint = f"/lectures/{lecture_id}/recording-clip"
        allowed = self.client.get(endpoint, headers=self.headers() | {"Origin": "https://student.github.io"})
        self.assertEqual(allowed.status_code, 200)
        exposed = allowed.headers["access-control-expose-headers"].lower()
        self.assertIn("x-clip-start-seconds", exposed)
        self.assertIn("x-clip-duration-seconds", exposed)
        with mock.patch.object(self.archive, "remote_size", side_effect=AssertionError("No source access")):
            self.assertEqual(self.client.get(endpoint, headers=self.headers() | {"Origin": "https://other.example"}).status_code, 403)
            with self.database.connect() as connection:
                connection.execute("DELETE FROM sessions")
            self.assertEqual(self.client.get(endpoint, headers=self.headers()).status_code, 401)

    def test_incomplete_remote_locator_never_trusts_local_duplicate(self):
        lecture_id, _ = self.lecture()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO recording_archives(lecture_id,state,object_key,drive_file_id,updated_at) "
                "VALUES (?,'attention',?,'fixture-remote','2026-09-06T00:00:00Z')", (lecture_id, "c" * 64),
            )
        with mock.patch.object(self.store, "open_info", side_effect=AssertionError("No unverified local fallback")):
            self.assertEqual(self.clip(lecture_id).status_code, 503)

    def test_concurrent_clips_have_a_global_bounded_capacity(self):
        lecture_id, pcm = self.lecture()
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        count_lock = threading.Lock()
        starts = 0
        streams = []
        content = _header(len(pcm)) + pcm

        def slow(identifier, *, start, end):
            nonlocal starts
            with count_lock:
                index = starts
                starts += 1
            if index < 2:
                entered[index].set()
            self.assertTrue(release.wait(5))
            stream = FakeRange(content, start, end)
            streams.append(stream)
            return stream

        try:
            with mock.patch.object(self.archive, "remote_size", return_value=len(content)), \
                    mock.patch.object(self.archive, "open_download", side_effect=slow), \
                    ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.clip, lecture_id, 0, 1) for _ in range(2)]
                self.assertTrue(all(event.wait(2) for event in entered))
                third = self.clip(lecture_id, 0, 1)
                self.assertEqual(third.status_code, 429)
                release.set()
                self.assertEqual([future.result(timeout=2).status_code for future in futures], [200, 200])
        finally:
            release.set()
        self.assertEqual(starts, 2)
        self.assertTrue(all(stream.closed for stream in streams))

    def test_bookmark_owner_is_rechecked_after_admission_before_write(self):
        lecture_id, _ = self.lecture()
        def changed_owner(key, limit, window):
            if key[0] == "bookmarks-write":
                with self.database.connect() as connection:
                    connection.execute("UPDATE lectures SET username='user-beta' WHERE id=?", (lecture_id,))
            return True
        with mock.patch("server.security.RateLimiter.allow", side_effect=changed_owner):
            self.assertEqual(self.mark(lecture_id).status_code, 404)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_bookmarks").fetchone()[0], 0)
