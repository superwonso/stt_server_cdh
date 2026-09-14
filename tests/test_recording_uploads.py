from __future__ import annotations

import hashlib
import io
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from server.app import create_app
from server.clova_transcriber import ClovaTranscriptionError
from server.db import Database
from server.recordings import RecordingCapacityError
from server.security import digest
from server.settings import Settings


def wav(samples):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return output.getvalue()


class FakeEngine:
    configured = True

    def __init__(self):
        self.calls = 0
        self.failure = False
        self.block = False
        self.entered, self.release = threading.Event(), threading.Event()

    def status(self):
        return {"model_state": "ready", "model": "synthetic", "device": "cpu"}

    def transcribe(self, samples, language, overlap=0, final=True, **kwargs):
        self.calls += 1
        self.entered.set()
        if self.block and not self.release.wait(5):
            raise RuntimeError("synthetic timeout")
        if self.failure:
            raise ClovaTranscriptionError("invalid_response")
        return [{"start": 0, "end": len(samples) / 16000, "text": "Synthetic English."}]

    def close_session(self, *args):
        return None


class RecordingUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stt-raw-upload-test-")
        self.root = Path(self.temp.name)
        self.engine = FakeEngine()
        self.settings = Settings(data_dir=self.root / "data", model_cache_dir=self.root / "models",
            accounts=("synthetic-alpha", "synthetic-beta"), site_origins=("https://synthetic.github.io",),
            max_import_seconds=60, max_recordings_bytes=16 * 1024 * 1024,
            recording_free_reserve_bytes=0, max_pending_chunks=1)
        self.app = create_app(self.settings, self.engine, clova_transcriber=self.engine)
        self.client = TestClient(self.app)
        self.db, self.store = self.app.state.database, self.app.state.recording_store
        self.auth = {"Authorization": "Bearer synthetic-token"}
        with self.db.connect() as connection:
            connection.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                (digest("synthetic-token"), "synthetic-alpha", time.time() + 3600, time.time()))
            connection.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                (digest("synthetic-other"), "synthetic-beta", time.time() + 3600, time.time()))
        response = self.client.post("/lectures", headers=self.auth,
            json={"title": "Synthetic recording", "language": "en", "asr_provider": "clova"})
        self.assertEqual(response.status_code, 201, response.text)
        self.lecture = response.json()["id"]
        self.first = np.arange(1600, dtype="<i2")
        self.second = np.concatenate((self.first[-800:], np.arange(2000, 3600, dtype="<i2")))
        self.ids = [str(uuid.uuid4()) for _ in range(4)]

    def tearDown(self):
        self.engine.release.set()
        self.app.state.archive_manager.request_shutdown()
        self.app.state.archive_manager.stop(timeout=1)
        self.app.state.stop_import_worker()
        self.client.close()
        self.temp.cleanup()

    def upload(self, samples=None, *, index=0, start=0, overlap=0, final=False,
               asr=False, auth=None, lecture=None, content=None):
        return self.client.post(f"/lectures/{lecture or self.lecture}/{'chunks' if asr else 'recording-chunks'}",
            headers=(self.auth if auth is None else auth) | {
                "Content-Type": "audio/wav", "X-Chunk-Id": self.ids[index],
                "X-Start-Seconds": str(start), "X-Overlap-Seconds": str(overlap),
                "X-Final-Chunk": str(final).lower()},
            content=wav(self.first if samples is None else samples) if content is None else content)

    def result(self, index=0, auth=None):
        return self.client.get(f"/lectures/{self.lecture}/recording-chunks/{self.ids[index]}/result",
                               headers=self.auth if auth is None else auth)

    def rows(self, table):
        with self.db.connect() as connection:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]

    def audio(self):
        return self.store.path("synthetic-alpha", self.lecture).read_bytes()

    def finish_raw(self):
        self.assertEqual(self.upload().status_code, 200)
        response = self.upload(self.second, index=1, start=.05, overlap=.05, final=True)
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_receipt_contract_has_no_asr_result_and_durable_replay(self):
        self.assertEqual(self.result().json(), {"status": "unknown", "chunk_id": self.ids[0]})
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "status": "stored", "chunk_id": self.ids[0], "payload_sha256": hashlib.sha256(wav(self.first)).hexdigest(),
            "start_seconds": 0, "duration_seconds": .1, "overlap_seconds": 0,
            "final_chunk": False, "recording_audio_finalized": False, "recording_stored_seconds": .1})
        original = self.audio()
        self.assertEqual(self.upload().json(), response.json())
        self.assertEqual(self.result().json(), response.json())
        self.assertEqual(self.audio(), original)
        self.assertEqual(len(self.rows("recording_chunks")), 1)
        self.assertEqual(self.rows("chunks"), [])
        self.assertEqual(self.rows("segments"), [])
        self.assertEqual(self.engine.calls, 0)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_overlap_removed_and_final_audio_downloadable_without_transcript_final(self):
        response = self.finish_raw()
        self.assertTrue(response.json()["recording_audio_finalized"])
        lecture = self.client.get(f"/lectures/{self.lecture}", headers=self.auth).json()
        self.assertFalse(lecture["recording_finalized"])
        self.assertTrue(lecture["recording_audio_finalized"])
        self.assertEqual(lecture["recording_stored_seconds"], .2)
        ticket = self.client.post(f"/lectures/{self.lecture}/recording-download-ticket", headers=self.auth)
        self.assertEqual(ticket.status_code, 200, ticket.text)
        download = self.client.get(ticket.json()["path"])
        self.assertEqual(download.status_code, 200)
        with wave.open(io.BytesIO(download.content), "rb") as stream:
            actual = stream.readframes(stream.getnframes())
        self.assertEqual(actual, np.concatenate((self.first, self.second[800:])).astype("<i2").tobytes())
        self.assertEqual(self.engine.calls, 0)
        self.assertEqual(self.rows("segments"), [])

    def test_clova_failure_does_not_prevent_later_raw_upload(self):
        self.assertEqual(self.upload().status_code, 200)
        self.engine.failure = True
        self.assertEqual(self.upload(asr=True).status_code, 424)
        self.assertEqual(self.upload(self.second, index=1, start=.05, overlap=.05, final=True).status_code, 200)
        self.assertEqual(self.engine.calls, 1)
        self.assertEqual(self.rows("chunks"), [])
        self.assertEqual(len(self.rows("recording_chunks")), 2)
        self.assertFalse(self.rows("lectures")[0]["recording_finalized"])

    def test_raw_upload_progresses_while_asr_is_blocked(self):
        self.assertEqual(self.upload().status_code, 200)
        self.engine.block = True
        with ThreadPoolExecutor(max_workers=2) as executor:
            running = executor.submit(self.upload, asr=True)
            self.assertTrue(self.engine.entered.wait(2))
            response = self.upload(self.second, index=1, start=.05, overlap=.05, final=True)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.rows("chunks")[0]["status"], "pending")
            self.engine.release.set()
            self.assertEqual(running.result(timeout=5).status_code, 200)
        self.assertEqual(len(self.rows("segments")), 1)

    def test_asr_can_process_saved_chunks_after_raw_final_without_rewriting(self):
        self.finish_raw()
        before = self.audio()
        with mock.patch.object(self.store, "write_chunk", side_effect=AssertionError("must not rewrite raw audio")):
            first = self.upload(asr=True)
            final = self.upload(self.second, index=1, start=.05, overlap=.05, final=True, asr=True)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(final.status_code, 200, final.text)
        self.assertTrue(final.json()["recording_finalized"])
        self.assertEqual(self.audio(), before)
        self.assertEqual(self.engine.calls, 2)
        self.assertEqual(self.upload(asr=True).status_code, 200)
        self.assertEqual(self.engine.calls, 2)

    def test_missing_or_changed_raw_receipt_rejects_asr_before_provider(self):
        self.finish_raw()
        for kwargs in ({"index": 2}, {"content": wav(self.first + 1)}, {"overlap": .01}):
            with self.subTest(kwargs=tuple(kwargs)):
                self.assertEqual(self.upload(asr=True, **kwargs).status_code, 409)
        self.assertEqual(self.engine.calls, 0)

    def test_new_raw_ids_and_changed_replays_rejected_after_final(self):
        self.finish_raw()
        before = self.audio()
        self.assertEqual(self.upload(index=2).status_code, 409)
        self.assertEqual(self.upload(content=wav(self.first + 1)).status_code, 409)
        self.assertEqual(self.upload(final=True).status_code, 409)
        self.assertEqual(self.audio(), before)

    def test_gaps_and_incorrect_retained_overlap_never_create_silence(self):
        self.assertEqual(self.upload().status_code, 200)
        before = self.audio()
        self.assertEqual(self.upload(self.second, index=1, start=.06, overlap=.05).status_code, 409)
        corrupt = self.second.copy()
        corrupt[0] += 1
        self.assertEqual(self.upload(corrupt, index=1, start=.05, overlap=.05).status_code, 409)
        self.assertEqual(self.audio(), before)
        self.assertEqual(len(self.rows("recording_chunks")), 1)

    def test_duplicate_audio_with_new_id_does_not_add_a_second_receipt(self):
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(self.upload(index=1).status_code, 409)
        self.assertEqual(len(self.rows("recording_chunks")), 1)

    def test_full_overlap_final_has_no_duplicate_samples(self):
        self.assertEqual(self.upload().status_code, 200)
        before = self.audio()
        response = self.upload(self.first[-800:], index=1, start=.05, overlap=.05, final=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.audio(), before)
        self.assertEqual(response.json()["recording_stored_seconds"], .1)

    def test_legacy_asr_prefix_can_be_backfilled_without_rewriting_content(self):
        self.assertEqual(self.upload(asr=True).status_code, 200)
        original = self.audio()
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(self.audio(), original)
        self.assertEqual(self.upload(self.second, index=1, start=.05, overlap=.05, final=True).status_code, 200)

    def test_legacy_finalized_asr_receipt_cannot_claim_a_missing_recording(self):
        self.assertEqual(self.upload(asr=True, final=True).status_code, 200)
        self.store.delete("synthetic-alpha", self.lecture)
        with mock.patch.object(self.store, "write_chunk", side_effect=AssertionError("must not recreate archived audio")):
            response = self.upload(final=True)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.rows("recording_chunks"), [])
        self.assertFalse(self.store.available("synthetic-alpha", self.lecture))

    def test_missing_source_invalidates_raw_receipt_and_blocks_asr_before_call(self):
        self.finish_raw()
        self.store.delete("synthetic-alpha", self.lecture)
        self.assertEqual(self.result().status_code, 503)
        self.assertEqual(self.upload().status_code, 503)
        self.assertEqual(self.upload(asr=True).status_code, 503)
        self.assertEqual(self.engine.calls, 0)
        self.assertEqual(len(self.rows("recording_chunks")), 2)
        self.assertEqual(self.rows("chunks"), [])

    def test_missing_source_blocks_completed_asr_post_and_get_replay_without_rebilling(self):
        self.finish_raw()
        self.assertEqual(self.upload(asr=True).status_code, 200)
        before_chunks, before_segments = self.rows("chunks"), self.rows("segments")
        self.assertEqual(self.engine.calls, 1)
        self.store.delete("synthetic-alpha", self.lecture)
        self.assertEqual(self.upload(asr=True).status_code, 503)
        response = self.client.get(f"/lectures/{self.lecture}/chunks/{self.ids[0]}/result", headers=self.auth | {
            "X-Chunk-Payload-Sha256": hashlib.sha256(wav(self.first)).hexdigest(),
            "X-Start-Seconds": "0", "X-Overlap-Seconds": "0", "X-Final-Chunk": "false"})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.engine.calls, 1)
        self.assertEqual(self.rows("chunks"), before_chunks)
        self.assertEqual(self.rows("segments"), before_segments)

    def test_owner_auth_trash_and_delete_fail_closed(self):
        for auth, status in (({}, 401), ({"Authorization": "Bearer synthetic-other"}, 404)):
            self.assertEqual(self.upload(auth=auth).status_code, status)
            self.assertEqual(self.result(auth=auth).status_code, status)
        with self.db.connect() as connection:
            connection.execute("UPDATE lectures SET trashed_at='synthetic-time' WHERE id=?", (self.lecture,))
        self.assertEqual(self.upload().status_code, 404)
        self.assertEqual(self.result().status_code, 404)
        with self.db.connect() as connection:
            connection.execute("UPDATE lectures SET trashed_at=NULL,deleting=1 WHERE id=?", (self.lecture,))
        self.assertEqual(self.upload().status_code, 404)
        self.assertEqual(self.rows("recording_chunks"), [])

    def test_quota_failure_keeps_prior_audio_and_allows_same_id_retry(self):
        self.assertEqual(self.upload().status_code, 200)
        before = self.audio()
        with mock.patch.object(self.store, "ensure_capacity", side_effect=RecordingCapacityError("synthetic full")):
            response = self.upload(self.second, index=1, start=.05, overlap=.05, final=True)
        self.assertEqual(response.status_code, 507)
        self.assertEqual(self.audio(), before)
        self.assertEqual(len(self.rows("recording_chunks")), 1)
        self.assertEqual(self.upload(self.second, index=1, start=.05, overlap=.05, final=True).status_code, 200)

    def test_failed_receipt_commit_replays_fsynced_bytes_exactly(self):
        original_connect = self.db.connect
        fail = [True]
        @contextmanager
        def fail_receipt_commit():
            with original_connect() as connection:
                yield connection
                if (fail[0] and connection.in_transaction and connection.execute(
                        "SELECT 1 FROM recording_chunks WHERE chunk_id=?", (self.ids[0],)).fetchone()):
                    fail[0] = False
                    raise sqlite3.OperationalError("synthetic receipt rollback")
        with mock.patch.object(self.db, "connect", fail_receipt_commit):
            response = self.upload()
        self.assertEqual(response.status_code, 503, response.text)
        before = self.audio()
        self.assertEqual(self.rows("recording_chunks"), [])
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(self.audio(), before)

    def test_upload_input_bounds_and_unavailable_auth_never_call_asr(self):
        for kwargs in ({"content": b""}, {"samples": np.zeros(799, dtype="<i2")},
                       {"start": float("nan")}, {"start": 1 / 32000}, {"overlap": 3.01}):
            self.assertEqual(self.upload(**kwargs).status_code, 422)
        self.assertEqual(self.engine.calls, 0)
        self.assertEqual(self.rows("recording_chunks"), [])

    def test_capability_and_cors_are_authenticated_and_no_store(self):
        response = self.client.get("/status", headers=self.auth)
        self.assertTrue(response.json()["capabilities"]["recording_audio_upload"])
        self.assertEqual(self.client.get("/status").status_code, 401)
        response = self.client.options(f"/lectures/{self.lecture}/recording-chunks", headers={
            "Origin": "https://synthetic.github.io", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,x-chunk-id,x-start-seconds,x-overlap-seconds,x-final-chunk"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_manual_finalize_cannot_close_unfinished_raw_upload(self):
        self.assertEqual(self.upload().status_code, 200)
        response = self.client.post(f"/lectures/{self.lecture}/recording-finalize", headers=self.auth)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.engine.calls, 0)
        self.assertFalse(self.rows("lectures")[0]["recording_finalized"])

    def enable_fake_drive(self):
        from test_drive_archive import FakeDrive
        self.client.close()
        self.drive = FakeDrive()
        self.app = create_app(replace(self.settings, google_drive_enabled=True), self.engine,
            clova_transcriber=self.engine, drive_storage=self.drive)
        self.client = TestClient(self.app)
        self.db, self.store = self.app.state.database, self.app.state.recording_store

    def test_raw_final_moves_to_fake_drive_then_asr_retries_without_recreating_local_wav(self):
        self.enable_fake_drive()
        self.finish_raw()
        original = self.audio()
        result = self.app.state.archive_manager.run_once(delete_local=True)
        self.assertEqual(result, {"migrated_count": 1, "deleted_local_count": 1, "failed_count": 0})
        self.assertFalse(self.store.available("synthetic-alpha", self.lecture))
        self.assertFalse(self.rows("lectures")[0]["recording_finalized"])
        ticket = self.client.post(f"/lectures/{self.lecture}/recording-download-ticket", headers=self.auth)
        self.assertEqual(ticket.status_code, 200, ticket.text)
        self.assertEqual(self.client.get(ticket.json()["path"]).content, original)
        self.assertEqual(self.engine.calls, 0)
        with mock.patch.object(self.store, "write_chunk", side_effect=AssertionError("must not recreate archived audio")):
            self.assertEqual(self.upload(asr=True).status_code, 200)
            response = self.upload(self.second, index=1, start=.05, overlap=.05, final=True, asr=True)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.upload().status_code, 200)
        self.assertTrue(response.json()["recording_finalized"])
        self.assertFalse(self.store.available("synthetic-alpha", self.lecture))
        self.assertEqual(self.engine.calls, 2)

    def test_qwen_manual_finalize_cannot_silently_lose_archived_raw_tail(self):
        self.enable_fake_drive()
        with self.db.connect() as connection:
            connection.execute("UPDATE lectures SET asr_provider='qwen' WHERE id=?", (self.lecture,))
        self.finish_raw()
        result = self.app.state.archive_manager.run_once(delete_local=True)
        self.assertEqual(result, {"migrated_count": 1, "deleted_local_count": 1, "failed_count": 0})
        response = self.client.post(f"/lectures/{self.lecture}/recording-finalize", headers=self.auth)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(self.rows("lectures")[0]["recording_finalized"])
        self.assertEqual(self.engine.calls, 0)
        self.assertEqual(self.rows("segments"), [])

    def test_legacy_asr_receipt_can_backfill_only_with_verified_fake_drive_copy(self):
        self.enable_fake_drive()
        self.assertEqual(self.upload(asr=True, final=True).status_code, 200)
        result = self.app.state.archive_manager.run_once(delete_local=True)
        self.assertEqual(result, {"migrated_count": 1, "deleted_local_count": 1, "failed_count": 0})
        response = self.upload(final=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["recording_audio_finalized"])
        self.assertFalse(self.store.available("synthetic-alpha", self.lecture))
        with self.db.connect() as connection:
            connection.execute("UPDATE recording_archives SET state='attention' WHERE lecture_id=?", (self.lecture,))
        self.assertEqual(self.result().status_code, 503)


class RecordingUploadMigrationTests(unittest.TestCase):
    def test_v21_migration_is_additive_and_cascades_only_on_lecture_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Database(Path(temporary) / "data.sqlite3", ("synthetic-alpha", "synthetic-beta"))
            db.initialize()
            with db.connect() as connection:
                connection.execute("DROP TABLE recording_chunks")
                connection.execute("ALTER TABLE lectures DROP COLUMN audio_finalized")
                connection.execute("PRAGMA user_version=21")
                connection.execute("INSERT INTO lectures(id,username,title,created_at,recording_finalized) "
                    "VALUES('synthetic-lecture','synthetic-alpha','unchanged','now',1)")
                before = dict(connection.execute("SELECT * FROM lectures").fetchone())
            db.initialize()
            db.initialize()
            with db.connect() as connection:
                after = dict(connection.execute("SELECT * FROM lectures").fetchone())
                self.assertEqual(after.pop("audio_finalized"), 0)
                self.assertEqual(after, before)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 22)
                connection.execute("INSERT INTO recording_chunks VALUES(?,?,?,?,?,?,?,?)",
                    ("synthetic-lecture", "synthetic-chunk", "a" * 64, 0, 800, 0, 1, "now"))
                connection.execute("DELETE FROM lectures WHERE id='synthetic-lecture'")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM recording_chunks").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
