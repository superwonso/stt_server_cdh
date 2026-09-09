"""Read-only partial recording downloads using synthetic temporary WAVs only."""
from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request

from server.app import CloseableStreamingResponse, create_app
from server.recording_snapshot import READ_BLOCK_BYTES, RecordingSnapshot, SnapshotStream
from server.recordings import RecordingCorruptError, _header
from server.security import digest
from server.settings import Settings


class FakeTranscriber:
    def __init__(self):
        self.calls = 0

    def status(self):
        return {"model_state": "ready", "model": "synthetic", "device": "cpu"}

    def transcribe(self, samples, language, overlap, final):
        self.calls += 1
        return []


class RecordingSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-recording-snapshot-test-")
        directory = Path(self.temporary.name)
        settings = Settings(data_dir=directory / "data", model_cache_dir=directory / "models",
                            recording_free_reserve_bytes=0, admin_username="user-alpha",
                            site_origins=("https://student.github.io",))
        self.app = create_app(settings, FakeTranscriber())
        self.database = self.app.state.database
        self.store = self.app.state.recording_store
        self.archive = self.app.state.archive_manager
        self.client = TestClient(self.app)
        self.tokens = {"user-alpha": "synthetic-snapshot-alpha", "user-beta": "synthetic-snapshot-beta"}
        with self.database.connect() as connection:
            for username, token in self.tokens.items():
                connection.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                                   (digest(token), username, time.time() + 3600, time.time()))

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def headers(self, username="user-alpha"):
        return {"Authorization": f"Bearer {self.tokens[username]}"}

    def lecture(self, *, username="user-alpha", finalized=False, seconds=2):
        identifier = str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lectures(id,username,title,language,created_at,recording_finalized) "
                               "VALUES(?,?,'PRIVATE synthetic title','ko','now',?)", (identifier, username, int(finalized)))
        pcm = b"\x01\x02" * (seconds * 16000)
        if pcm:
            self.store.write_chunk(username, identifier, start_seconds=0, overlap_seconds=0, pcm=pcm)
        return identifier, pcm

    def ticket(self, identifier, username="user-alpha"):
        return self.client.post(f"/lectures/{identifier}/recording-snapshot-ticket", headers=self.headers(username))

    def granted(self, identifier):
        response = self.ticket(identifier)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def snapshot_stream(self, identifier):
        info = self.store.open_info("user-alpha", identifier)
        snapshot = RecordingSnapshot.capture(info, self.store.max_frames)
        return SnapshotStream(info["descriptor"], snapshot)

    def hold_download(self, path):
        # Model a response stalled before ASGI can stream it: the real route
        # has acquired a slot and FD but its closing callback has not run yet.
        endpoint = next(route.endpoint for route in self.app.routes if route.path == "/recording-snapshots/{ticket}")
        return endpoint(path.rsplit("/", 1)[1], Request({"type": "http", "method": "GET", "headers": []}))

    def test_two_slow_downloads_bound_fds_without_spending_busy_retries_or_asr_capacity(self):
        identifier, _ = self.lecture()
        path = self.granted(identifier)["path"]
        held = [self.hold_download(path), self.hold_download(path)]
        try:
            with patch.object(self.store, "open_info", side_effect=AssertionError("Full pool must not open an FD")):
                for _ in range(4):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 429)
                    self.assertEqual(response.headers["retry-after"], "5")
                self.assertEqual(self.client.get("/recording-snapshots/" + "unknown" * 7).status_code, 404)
            pcm = b"\x03\x04" * 16000
            response = self.client.post(f"/lectures/{identifier}/chunks", content=_header(len(pcm)) + pcm,
                                        headers={**self.headers(), "Content-Type": "audio/wav",
                                                 "X-Chunk-Id": str(uuid.uuid4()), "X-Start-Seconds": "2",
                                                 "X-Final-Chunk": "false"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.app.state.transcriber.calls, 1)
        finally:
            for response in held:
                response._close_stream()
                response._close_stream()
        # Only the two admitted downloads spent retries; rejected requests did not.
        for _ in range(14):
            self.assertEqual(self.client.get(path, headers={"Range": "bytes=0-0"}).status_code, 206)
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_admission_releases_on_bad_range_corruption_and_session_change(self):
        identifier, _ = self.lecture()
        other, _ = self.lecture()
        held = self.hold_download(self.granted(other)["path"])
        path = self.granted(identifier)["path"]
        try:
            for _ in range(3):
                self.assertEqual(self.client.get(path, headers={"Range": "bytes=9-2"}).status_code, 416)
            with patch.object(self.store, "open_info", side_effect=RecordingCorruptError("synthetic")):
                for _ in range(3):
                    self.assertEqual(self.client.get(path).status_code, 503)
            original_open = self.store.open_info
            def expire_during_read(*args):
                recording = original_open(*args)
                with self.database.connect() as connection:
                    connection.execute("UPDATE sessions SET expires_at=0 WHERE username='user-alpha'")
                return recording
            with patch.object(self.store, "open_info", side_effect=expire_during_read):
                self.assertEqual(self.client.get(path).status_code, 404)
            with self.database.connect() as connection:
                connection.execute("UPDATE sessions SET expires_at=? WHERE username='user-alpha'", (time.time() + 3600,))
            self.assertEqual(self.client.get(path).status_code, 200)
        finally:
            held._close_stream()

    def test_asgi_disconnect_releases_one_slot_exactly_once(self):
        identifier, _ = self.lecture()
        path = self.granted(identifier)["path"]
        first, second = self.hold_download(path), self.hold_download(path)
        third = None
        try:
            async def exercise():
                async def receive():
                    return {"type": "http.disconnect"}
                async def send(message):
                    if message["type"] == "http.response.start":
                        await asyncio.sleep(0.05)
                await first({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
            asyncio.run(exercise())
            first._close_stream()
            third = self.hold_download(path)
            self.assertEqual(self.client.get(path).status_code, 429)
        finally:
            first._close_stream()
            second._close_stream()
            if third is not None:
                third._close_stream()
        self.assertEqual(self.client.get(path).status_code, 200)

    def test_owner_only_capability_and_exact_readonly_saved_prefix(self):
        identifier, pcm = self.lecture()
        with self.database.connect() as connection:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            before = {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                      for table in tables}
        path = self.store.path("user-alpha", identifier)
        original = path.read_bytes()
        status = self.client.get("/status", headers=self.headers()).json()
        self.assertIs(status["capabilities"]["recording_partial_download"], True)
        self.assertEqual(self.client.post(f"/lectures/{identifier}/recording-snapshot-ticket").status_code, 401)
        self.assertEqual(self.ticket(identifier, "user-beta").status_code, 404)
        with patch.object(self.archive, "queue", side_effect=AssertionError("Must not queue Drive")), \
                patch.object(self.archive, "open_download", side_effect=AssertionError("Must not use Drive")), \
                patch.object(self.store, "write_chunk", side_effect=AssertionError("Must not write")):
            granted = self.granted(identifier)
            response = self.client.get(granted["path"])
        self.assertEqual(set(granted), {"path", "expires_in", "bytes", "duration_seconds", "scope"})
        self.assertEqual(granted["scope"], "server_saved_prefix")
        self.assertEqual(granted["expires_in"], 60)
        self.assertEqual(granted["duration_seconds"], 2)
        self.assertEqual(granted["bytes"], 44 + len(pcm))
        self.assertNotIn(identifier, granted["path"])
        self.assertNotIn(self.tokens["user-alpha"], granted["path"])
        self.assertEqual(response.status_code, 200, response.text[:50])
        self.assertEqual(response.content, _header(len(pcm)) + pcm)
        self.assertEqual(response.headers["content-length"], str(len(original)))
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertNotIn("PRIVATE", str(response.headers))
        self.assertEqual(path.read_bytes(), original)
        with self.database.connect() as connection:
            self.assertEqual({table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                              for table in tables}, before)

    def test_admin_has_no_other_owner_bypass(self):
        identifier, _ = self.lecture(username="user-beta")
        self.assertEqual(self.ticket(identifier).status_code, 404)
        self.assertEqual(self.ticket(identifier, "user-beta").status_code, 200)

    def test_append_between_ticket_and_get_keeps_original_header_and_length(self):
        identifier, pcm = self.lecture()
        granted = self.granted(identifier)
        self.store.write_chunk("user-alpha", identifier, start_seconds=2, overlap_seconds=0, pcm=b"\x03\x04" * 16000)
        response = self.client.get(granted["path"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, _header(len(pcm)) + pcm)
        self.assertEqual(self.store.info("user-alpha", identifier)["duration_seconds"], 3)

    def test_ranges_resume_same_snapshot_despite_append(self):
        identifier, pcm = self.lecture()
        granted = self.granted(identifier)
        first = self.client.get(granted["path"], headers={"Range": "bytes=0-99"})
        self.store.write_chunk("user-alpha", identifier, start_seconds=2, overlap_seconds=0, pcm=b"\x03\x04" * 16000)
        second = self.client.get(granted["path"], headers={"Range": "bytes=100-"})
        self.assertEqual((first.status_code, second.status_code), (206, 206))
        self.assertEqual(first.content + second.content, _header(len(pcm)) + pcm)
        self.assertEqual(first.headers["content-range"], f"bytes 0-99/{granted['bytes']}")
        suffix = self.client.get(granted["path"], headers={"Range": "bytes=-7"})
        self.assertEqual(suffix.content, pcm[-7:])

    def test_bad_ranges_are_bounded_and_head_is_not_supported(self):
        identifier, _ = self.lecture()
        granted = self.granted(identifier)
        for value in ("bytes=99999999-", "bytes=5-2", "bytes=0-1,3-4", "bytes=-0", "units=0-10"):
            response = self.client.get(granted["path"], headers={"Range": value})
            self.assertEqual(response.status_code, 416)
            self.assertEqual(response.headers["content-range"], f"bytes */{granted['bytes']}")
        self.assertEqual(self.client.head(granted["path"]).status_code, 405)

    def test_ticket_routes_are_not_interchangeable_and_normal_complete_route_unchanged(self):
        identifier, _ = self.lecture()
        partial = self.granted(identifier)["path"]
        self.assertEqual(self.client.get(partial.replace("recording-snapshots", "recording-downloads")).status_code, 404)
        self.assertEqual(self.client.get(partial).status_code, 200)
        with self.database.connect() as connection:
            connection.execute("UPDATE lectures SET recording_finalized=1 WHERE id=?", (identifier,))
        self.assertEqual(self.ticket(identifier).status_code, 409)
        complete = self.client.post(f"/lectures/{identifier}/recording-download-ticket", headers=self.headers()).json()["path"]
        self.assertEqual(self.client.get(complete.replace("recording-downloads", "recording-snapshots")).status_code, 404)
        self.assertEqual(self.client.get(complete).status_code, 200)
        self.assertEqual(self.client.get(partial).status_code, 404)

    def test_expiry_reissue_reuse_limit_logout_and_session_expiry(self):
        identifier, _ = self.lecture()
        first = self.granted(identifier)["path"]
        second = self.granted(identifier)["path"]
        self.assertEqual(self.client.get(first).status_code, 404)
        for _ in range(16):
            self.assertEqual(self.client.get(second, headers={"Range": "bytes=0-0"}).status_code, 206)
        self.assertEqual(self.client.get(second).status_code, 404)
        third = self.granted(identifier)["path"]
        actual_clock = time.monotonic
        with patch("server.app.time.monotonic", side_effect=lambda: actual_clock() + 61):
            self.assertEqual(self.client.get(third).status_code, 404)
        fourth = self.granted(identifier)["path"]
        self.assertEqual(self.client.post("/auth/logout", headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get(fourth).status_code, 404)

    def test_revoked_or_expired_session_after_capture_cannot_mint_ticket(self):
        identifier, _ = self.lecture()
        original_open = self.store.open_info
        descriptors = []
        def revoke_after_read(*args):
            recording = original_open(*args)
            descriptors.append(recording["descriptor"])
            with self.database.connect() as connection:
                connection.execute("DELETE FROM sessions WHERE username='user-alpha'")
            return recording
        with patch.object(self.store, "open_info", side_effect=revoke_after_read):
            self.assertEqual(self.ticket(identifier).status_code, 401)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_expired_session_before_download_and_during_open_is_rejected(self):
        identifier, _ = self.lecture()
        path = self.granted(identifier)["path"]
        original_open = self.store.open_info
        def expire_during_open(*args):
            recording = original_open(*args)
            with self.database.connect() as connection:
                connection.execute("UPDATE sessions SET expires_at=0")
            return recording
        with patch.object(self.store, "open_info", side_effect=expire_during_open):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_pause_owner_change_trash_and_delete_revoke_access(self):
        identifier, _ = self.lecture()
        path = self.granted(identifier)["path"]
        self.assertEqual(self.client.post("/admin/access", headers=self.headers(), json={"enabled": False}).status_code, 200)
        self.assertEqual(self.client.get(path).status_code, 503)
        self.assertEqual(self.ticket(identifier).status_code, 503)
        self.assertEqual(self.client.post("/admin/access", headers=self.headers(), json={"enabled": True}).status_code, 200)
        for sql in ("UPDATE lectures SET username='user-beta' WHERE id=?", "UPDATE lectures SET username='user-alpha',trashed_at='now' WHERE id=?", "DELETE FROM lectures WHERE id=?"):
            with self.database.connect() as connection:
                connection.execute(sql, (identifier,))
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_no_audio_header_only_corrupt_symlink_and_too_long_fail_closed(self):
        identifier, _ = self.lecture(seconds=0)
        self.assertEqual(self.ticket(identifier).status_code, 404)
        path = self.store.path("user-alpha", identifier)
        for payload, status in ((_header(0), 404), (b"corrupt", 503), (_header(32000) + b"\0" * 16000, 503)):
            path.write_bytes(payload)
            self.assertEqual(self.ticket(identifier).status_code, status)
            self.assertEqual(path.read_bytes(), payload)
        path.unlink()
        other, _ = self.lecture()
        path.symlink_to(self.store.path("user-alpha", other))
        self.assertEqual(self.ticket(identifier).status_code, 503)
        with patch.object(self.store, "max_frames", 1):
            self.assertEqual(self.ticket(other).status_code, 503)

    def test_replacement_truncation_and_missing_prefix_cannot_fall_back_to_drive(self):
        for change in ("replace", "truncate", "missing"):
            identifier, pcm = self.lecture()
            granted = self.granted(identifier)
            path = self.store.path("user-alpha", identifier)
            if change == "replace":
                path.rename(path.with_suffix(".synthetic-original"))
                path.write_bytes(_header(len(pcm)) + pcm)
            elif change == "truncate":
                path.write_bytes(_header(len(pcm) // 2) + pcm[:len(pcm) // 2])
            else:
                path.unlink()
            with patch.object(self.archive, "open_download", side_effect=AssertionError("No Drive fallback")):
                self.assertEqual(self.client.get(granted["path"]).status_code, 503)

    def test_ticket_metadata_does_not_retain_descriptors(self):
        identifier, _ = self.lecture()
        original_open = self.store.open_info
        descriptors = []
        def track(*args):
            recording = original_open(*args)
            descriptors.append(recording["descriptor"])
            return recording
        with patch.object(self.store, "open_info", side_effect=track):
            granted = self.granted(identifier)
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertEqual(self.client.get(granted["path"]).status_code, 200)

    def test_invalid_range_and_late_owner_change_close_download_descriptor(self):
        identifier, _ = self.lecture()
        granted = self.granted(identifier)
        original_open = self.store.open_info
        descriptors = []
        def track(*args):
            recording = original_open(*args)
            descriptors.append(recording["descriptor"])
            return recording
        with patch.object(self.store, "open_info", side_effect=track):
            self.assertEqual(self.client.get(granted["path"], headers={"Range": "bytes=9-2"}).status_code, 416)
        with self.assertRaises(OSError):
            os.fstat(descriptors[-1])
        def change_owner(*args):
            recording = track(*args)
            with self.database.connect() as connection:
                connection.execute("UPDATE lectures SET username='user-beta' WHERE id=?", (identifier,))
            return recording
        with patch.object(self.store, "open_info", side_effect=change_owner):
            self.assertEqual(self.client.get(granted["path"]).status_code, 404)
        with self.assertRaises(OSError):
            os.fstat(descriptors[-1])

    def test_failed_later_append_rollback_preserves_captured_prefix(self):
        identifier, pcm = self.lecture()
        stream = self.snapshot_stream(identifier)
        iterator = stream.iter_bytes(0, stream.snapshot.total_bytes)
        header = next(iterator)
        with patch("server.recordings.os.fsync", side_effect=OSError("synthetic fsync failure")):
            with self.assertRaises(OSError):
                self.store.write_chunk("user-alpha", identifier, start_seconds=2, overlap_seconds=0,
                                       pcm=b"\x03\x04" * 16000)
        self.assertEqual(header + b"".join(iterator), _header(len(pcm)) + pcm)
        self.assertEqual(self.store.path("user-alpha", identifier).read_bytes(), _header(len(pcm)) + pcm)

    def test_stream_does_not_hold_recording_lock_and_never_reads_whole_file(self):
        identifier, pcm = self.lecture(seconds=8)
        stream = self.snapshot_stream(identifier)
        descriptor = stream.descriptor
        iterator = stream.iter_bytes(0, stream.snapshot.total_bytes)
        header = next(iterator)
        finished, errors = threading.Event(), []
        def append():
            try:
                self.store.write_chunk("user-alpha", identifier, start_seconds=8, overlap_seconds=0, pcm=b"\x03\x04" * 16000)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        thread = threading.Thread(target=append)
        thread.start()
        self.assertTrue(finished.wait(2), "Snapshot streaming must release the writer lock")
        thread.join(2)
        self.assertEqual(errors, [])
        original_pread, reads = os.pread, []
        def read_bounded(fd, count, offset):
            self.assertLessEqual(count, READ_BLOCK_BYTES)
            self.assertGreaterEqual(offset, 44)
            reads.append((count, offset))
            return original_pread(fd, count, offset)
        with patch("server.recording_snapshot.os.pread", side_effect=read_bounded):
            result = header + b"".join(iterator)
        self.assertGreater(len(reads), 1)
        self.assertEqual(result, _header(len(pcm)) + pcm)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_truncated_or_short_reads_abort_stream_and_close_descriptor(self):
        identifier, _ = self.lecture()
        stream = self.snapshot_stream(identifier)
        descriptor = stream.descriptor
        with patch("server.recording_snapshot.os.pread", return_value=b"x"):
            with self.assertRaises(RecordingCorruptError):
                list(stream.iter_bytes(44, stream.snapshot.total_bytes))
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        stream = self.snapshot_stream(identifier)
        descriptor = stream.descriptor
        iterator = stream.iter_bytes(0, stream.snapshot.total_bytes)
        next(iterator)
        self.store.path("user-alpha", identifier).write_bytes(_header(0))
        with self.assertRaises(RecordingCorruptError):
            next(iterator)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_unstarted_asgi_disconnect_and_send_failure_close_descriptors(self):
        identifier, _ = self.lecture()
        for early in (True, False):
            stream = self.snapshot_stream(identifier)
            descriptor = stream.descriptor
            response = CloseableStreamingResponse(stream.iter_bytes(0, stream.snapshot.total_bytes), close=stream.close)
            async def exercise():
                async def receive():
                    return {"type": "http.disconnect"}
                async def send(message):
                    if early and message["type"] == "http.response.start":
                        await asyncio.sleep(0.05)
                    if not early and message["type"] == "http.response.body":
                        raise OSError("synthetic disconnected client")
                if early:
                    await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
                else:
                    with self.assertRaises(Exception):
                        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
            asyncio.run(exercise())
            with self.assertRaises(OSError):
                os.fstat(descriptor)


if __name__ == "__main__":
    unittest.main()
