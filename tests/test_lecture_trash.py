"""Reversible trash tests: temporary synthetic DB/WAV, fake providers only."""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.security import digest
from server.settings import Settings
from tests.test_summary_api import FakeSummarizer


class FakeTranscriber:
    def __init__(self):
        self.calls = 0

    def status(self):
        return {"model_state":"ready","model":"synthetic","device":"cpu"}

    def transcribe(self, *args, **kwargs):
        self.calls += 1
        return [{"start":0,"end":1,"text":"합성 받아쓰기"}]


class LectureTrashTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(data_dir=root / "data",model_cache_dir=root / "models",
                                 recording_free_reserve_bytes=0,
                                 site_origins=("https://student.github.io",))
        self.engine, self.summarizer = FakeTranscriber(), FakeSummarizer()
        self.app = create_app(self.settings,self.engine,summarizer=self.summarizer)
        self.db, self.store = self.app.state.database, self.app.state.recording_store
        self.archive = self.app.state.archive_manager
        self.service = self.app.state.summary_service
        self.start_patch = patch.object(self.service,"start")
        self.start_patch.start()
        self.app.state.stop_import_worker()
        self.client = TestClient(self.app)
        self.tokens = {"user-alpha":"synthetic-trash-alpha","user-beta":"synthetic-trash-beta"}
        with self.db.connect() as connection:
            for owner, token in self.tokens.items():
                connection.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                                   (digest(token),owner,time.time()+3600,time.time()))

    def tearDown(self):
        self.service.stop()
        self.app.state.translation_service.stop()
        self.app.state.stop_import_worker()
        self.app.state.stop_correction_worker()
        self.start_patch.stop()
        self.client.close()
        self.temporary.cleanup()

    def headers(self, owner="user-alpha"):
        return {"Authorization":f"Bearer {self.tokens[owner]}"}

    def lecture(self, *, owner="user-alpha", finalized=True, audio=True):
        identifier, chunk_id, segment_id = [str(uuid.uuid4()) for _ in range(3)]
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO lectures(id,username,title,language,created_at,recording_finalized) "
                "VALUES(?,?,'synthetic original','ko','2000-01-01T00:00:00Z',?)",(identifier,owner,int(finalized)))
            connection.execute(
                "INSERT INTO chunks(lecture_id,chunk_id,payload_hash,start_seconds,status) "
                "VALUES(?,?,?,0,'done')",(identifier,chunk_id,"a"*64))
            connection.execute("INSERT INTO segments VALUES(?,?,?,0,1,'빛을 이용해 양분을 만드는 과정이다.')",
                               (segment_id,identifier,chunk_id))
            connection.execute("INSERT INTO lecture_metadata VALUES(?,'synthetic display','synthetic course','2026-2',1,'now')",
                               (identifier,))
            connection.execute("INSERT INTO lecture_bookmarks VALUES(?,?,0.5,'synthetic bookmark','now')",
                               (str(uuid.uuid4()),identifier))
        if audio:
            self.store.write_chunk(owner,identifier,start_seconds=0,overlap_seconds=0,
                                   pcm=bytes(range(256))*125)
        return identifier

    def state(self, identifier):
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM lectures WHERE id=?",(identifier,)).fetchone()
            return dict(row) if row is not None else None

    def snapshot(self, identifier):
        with self.db.connect() as connection:
            return {table:[dict(row) for row in connection.execute(
                f"SELECT * FROM {table} WHERE lecture_id=? ORDER BY rowid",(identifier,))]
                for table in ("chunks","segments","lecture_metadata","lecture_bookmarks",
                              "transcript_corrections","lecture_summaries","lecture_translations","recording_archives")}

    def trash(self, identifier, owner="user-alpha"):
        return self.client.post(f"/lectures/{identifier}/trash",headers=self.headers(owner))

    def restore(self, identifier, owner="user-alpha"):
        return self.client.post(f"/lectures/{identifier}/restore",headers=self.headers(owner))

    def purge(self, identifier, owner="user-alpha"):
        return self.client.delete(f"/lectures/{identifier}/permanent",headers=self.headers(owner))

    def test_trash_restore_preserves_original_metadata_all_results_and_exact_wav(self):
        identifier = self.lecture()
        before = self.snapshot(identifier)
        path = self.store.info("user-alpha",identifier)["path"]
        before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        with patch.object(self.archive,"trash_for_deletion",side_effect=AssertionError("unexpected remote deletion")):
            response = self.trash(identifier)
            self.assertEqual(response.status_code,200,response.text)
            first = response.json()
            self.assertEqual(first["status"],"trashed")
            self.assertEqual(self.trash(identifier).json(),first,"retries preserve the original trash timestamp")
            self.assertEqual(self.snapshot(identifier),before)
            self.assertEqual(self.client.get("/lectures",headers=self.headers()).json(),[])
            rows = self.client.get("/library/trash",headers=self.headers()).json()
            self.assertEqual(rows,[{"lecture_id":identifier,"display_title":"synthetic display",
                "course":"synthetic course","semester":"2026-2","created_at":"2000-01-01T00:00:00Z",
                "trashed_at":first["trashed_at"],"deleting":False}])
            self.assertEqual(self.restore(identifier).json(),{"status":"restored","lecture_id":identifier})
            self.assertEqual(self.restore(identifier).status_code,200)
        self.assertIsNone(self.state(identifier)["trashed_at"])
        self.assertEqual(self.snapshot(identifier),before)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before_hash)
        self.assertEqual(self.client.get(f"/lectures/{identifier}",headers=self.headers()).status_code,200)

    def test_legacy_delete_is_reversible_and_cannot_bypass_trash(self):
        identifier = self.lecture()
        self.assertEqual(self.purge(identifier).status_code,409)
        response = self.client.delete(f"/lectures/{identifier}",headers=self.headers())
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["status"],"trashed")
        self.assertIsNotNone(self.state(identifier))
        self.assertTrue(self.store.available("user-alpha",identifier))
        self.assertEqual(self.restore(identifier).status_code,200)

    def test_other_owner_and_missing_ids_reveal_no_trash_or_restore_existence(self):
        identifier = self.lecture()
        before = self.snapshot(identifier)
        for target in (identifier,str(uuid.uuid4())):
            self.assertEqual(self.trash(target,"user-beta").status_code,404)
            self.assertEqual(self.restore(target,"user-beta").status_code,404)
            self.assertEqual(self.purge(target,"user-beta").json(),{"status":"deleted"})
        self.assertEqual(self.trash(identifier).status_code,200)
        self.assertEqual(self.client.get("/library/trash",headers=self.headers("user-beta")).json(),[])
        self.assertEqual(self.snapshot(identifier),before)

    def test_trashed_lesson_is_hidden_from_every_normal_read_and_write_path(self):
        identifier = self.lecture()
        with self.db.connect() as connection:
            chunk_id = connection.execute("SELECT chunk_id FROM chunks WHERE lecture_id=?",(identifier,)).fetchone()[0]
            bookmark_id = connection.execute("SELECT id FROM lecture_bookmarks WHERE lecture_id=?",(identifier,)).fetchone()[0]
        self.assertEqual(self.trash(identifier).status_code,200)
        before = self.snapshot(identifier)
        for suffix in ("","/summary","/translation","/correction","/metadata","/bookmarks","/recording-clip"):
            response = self.client.get(f"/lectures/{identifier}{suffix}",headers=self.headers())
            self.assertEqual(response.status_code,404,(suffix,response.text))
        for suffix in ("summary","translation","correction","recording-finalize","recording-download-ticket"):
            response = self.client.post(f"/lectures/{identifier}/{suffix}",headers=self.headers())
            self.assertEqual(response.status_code,404,(suffix,response.text))
        self.assertEqual(self.client.patch(f"/lectures/{identifier}/metadata",headers=self.headers(),
                         json={"revision":1,"display_title":"hidden change"}).status_code,404)
        self.assertEqual(self.client.post(f"/lectures/{identifier}/bookmarks",headers=self.headers(),
                         json={"id":str(uuid.uuid4()),"start_seconds":0.5}).status_code,404)
        self.assertEqual(self.client.delete(f"/lectures/{identifier}/bookmarks/{bookmark_id}",headers=self.headers()).status_code,404)
        response = self.client.get(f"/lectures/{identifier}/chunks/{chunk_id}/result",headers={**self.headers(),
            "X-Chunk-Payload-SHA256":"a"*64,"X-Start-Seconds":"0","X-Overlap-Seconds":"0","X-Final-Chunk":"true"})
        self.assertEqual(response.status_code,404,response.text)
        response = self.client.post(f"/lectures/{identifier}/chunks",headers={**self.headers(),
            "X-Chunk-Id":str(uuid.uuid4()),"X-Start-Seconds":"0","X-Overlap-Seconds":"0",
            "X-Final-Chunk":"true","Content-Type":"audio/wav"},content=b"not-sent-to-ASR")
        self.assertEqual(response.status_code,404,response.text)
        response = self.client.post("/lectures",headers={**self.headers(),"X-Lecture-Id":identifier},
                                    json={"title":"synthetic original","language":"ko","asr_provider":"qwen"})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.engine.calls,0)
        self.assertEqual(self.snapshot(identifier),before)
        self.assertEqual(self.client.get("/library/options",headers=self.headers()).json(),{"courses":[],"semesters":[]})
        result = self.client.get("/library/search",params={"q":"synthetic"},headers=self.headers())
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()["items"],[])

    def test_incomplete_and_active_work_are_not_trashed_or_silently_cancelled(self):
        identifier = self.lecture(finalized=False)
        self.assertEqual(self.trash(identifier).status_code,409)
        self.assertIsNone(self.state(identifier)["trashed_at"])
        for table in ("chunks","imports","transcript_corrections","lecture_summaries","lecture_translations","lecture_study_notes"):
            for status in (("pending",) if table=="chunks" else ("uploading","queued","processing") if table=="imports" else ("queued","processing")):
                with self.subTest(table=table,status=status):
                    identifier = self.lecture(audio=False)
                    with self.db.connect() as connection:
                        if table=="chunks":
                            connection.execute("UPDATE chunks SET status='pending' WHERE lecture_id=?",(identifier,))
                        elif table=="imports":
                            connection.execute("INSERT INTO imports(id,username,lecture_id,title,filename,file_fingerprint,total_bytes,status,created_at,updated_at) "
                                "VALUES(?,'user-alpha',?,'synthetic','file.wav',?,8,?,'now','now')",
                                (str(uuid.uuid4()),identifier,"a"*64,status))
                        elif table=="transcript_corrections":
                            connection.execute("INSERT INTO transcript_corrections(lecture_id,raw_revision,status,model,created_at,updated_at) "
                                "VALUES(?,?,?,'synthetic','now','now')",(identifier,"a"*64,status))
                        elif table=="lecture_study_notes":
                            connection.execute("INSERT INTO lecture_study_notes(lecture_id,username,job_id,raw_revision,status,model,created_at,updated_at) "
                                "VALUES(?,'user-alpha',?,?,?,'synthetic','now','now')",(identifier,str(uuid.uuid4()),"a"*64,status))
                        else:
                            connection.execute(f"INSERT INTO {table}(lecture_id,job_id,raw_revision,status,model,created_at,updated_at) "
                                "VALUES(?,?,?,?,'synthetic','now','now')",(identifier,str(uuid.uuid4()),"a"*64,status))
                    self.assertEqual(self.trash(identifier).status_code,409)
                    self.assertIsNone(self.state(identifier)["trashed_at"])
                    with self.db.connect() as connection:
                        self.assertEqual(connection.execute(f"SELECT status FROM {table} WHERE lecture_id=?",(identifier,)).fetchone()[0],status)
                        connection.execute(f"DELETE FROM {table} WHERE lecture_id=?",(identifier,))

    def test_issued_download_tickets_are_revoked_even_after_restore(self):
        identifier = self.lecture()
        issued = self.client.post(f"/lectures/{identifier}/recording-download-ticket",headers=self.headers())
        self.assertEqual(issued.status_code,200,issued.text)
        self.assertEqual(self.trash(identifier).status_code,200)
        self.assertEqual(self.client.get(issued.json()["path"]).status_code,404)
        self.assertEqual(self.restore(identifier).status_code,200)
        self.assertEqual(self.client.get(issued.json()["path"]).status_code,404)

    def test_ticket_mint_rechecks_a_trash_that_wins_during_recording_lookup(self):
        identifier = self.lecture()
        storage = self.archive.storage
        def lookup(*args):
            result = storage(*args)
            self.assertEqual(self.trash(identifier).status_code,200)
            return result
        with patch.object(self.archive,"storage",side_effect=lookup):
            issued = self.client.post(f"/lectures/{identifier}/recording-download-ticket",headers=self.headers())
        self.assertEqual(issued.status_code,404,issued.text)

    def test_download_and_clip_recheck_after_local_audio_open_and_close_the_descriptor(self):
        for clip in (False,True):
            identifier = self.lecture()
            ticket = self.client.post(f"/lectures/{identifier}/recording-download-ticket",headers=self.headers()).json()
            opened = []
            open_info = self.store.open_info
            def opening(*args):
                result = open_info(*args)
                opened.append(result["descriptor"])
                self.assertEqual(self.trash(identifier).status_code,200)
                return result
            with patch.object(self.store,"open_info",side_effect=opening):
                result = self.client.get(f"/lectures/{identifier}/recording-clip" if clip else ticket["path"],headers=self.headers())
            self.assertEqual(result.status_code,404,result.text)
            for descriptor in opened:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_completed_import_is_hidden_without_removing_its_restorable_metadata(self):
        identifier, import_id = self.lecture(), str(uuid.uuid4())
        with self.db.connect() as connection:
            connection.execute("INSERT INTO imports(id,username,lecture_id,title,language,filename,file_fingerprint,total_bytes,uploaded_bytes,status,raw_deleted,created_at,updated_at) "
                "VALUES(?,'user-alpha',?,'synthetic original','ko','fixture.wav',?,8,8,'completed',1,'now','now')",
                (import_id,identifier,"a"*64))
        self.assertEqual(self.trash(identifier).status_code,200)
        for method,suffix in (("get",""),("post","/complete"),("post","/cancel")):
            result = getattr(self.client,method)(f"/imports/{import_id}{suffix}",headers=self.headers())
            self.assertEqual(result.status_code,404,result.text)
        self.assertEqual(self.client.get("/imports",headers=self.headers()).json(),[])
        response = self.client.post("/imports",headers={**self.headers(),"X-Import-Id":import_id},
            json={"title":"synthetic original","language":"ko","filename":"fixture.wav","file_fingerprint":"a"*64,"size":8})
        self.assertEqual(response.status_code,404,response.text)
        self.assertEqual(self.restore(identifier).status_code,200)
        self.assertEqual(self.client.get(f"/imports/{import_id}",headers=self.headers()).status_code,200)

    def test_explicit_purge_failure_preserves_data_and_retries_without_restore(self):
        identifier = self.lecture()
        self.assertEqual(self.trash(identifier).status_code,200)
        before = self.snapshot(identifier)
        with patch.object(self.archive,"trash_for_deletion",return_value=False):
            failed = self.purge(identifier)
        self.assertEqual(failed.status_code,503,failed.text)
        self.assertEqual(self.state(identifier)["deleting"],1)
        self.assertEqual(self.snapshot(identifier),before)
        self.assertTrue(self.store.available("user-alpha",identifier))
        self.assertEqual(self.restore(identifier).status_code,409)
        self.assertTrue(self.client.get("/library/trash",headers=self.headers()).json()[0]["deleting"])
        with patch.object(self.archive,"trash_for_deletion",return_value=True):
            self.assertEqual(self.purge(identifier).json(),{"status":"deleted"})
        self.assertIsNone(self.state(identifier))
        self.assertFalse(self.store.available("user-alpha",identifier))
        self.assertEqual(self.purge(identifier).json(),{"status":"deleted"})
        self.assertTrue(all(not rows for rows in self.snapshot(identifier).values()))

    def test_restore_wins_before_purge_but_cannot_undo_a_started_remote_purge(self):
        identifier = self.lecture()
        self.trash(identifier)
        self.assertEqual(self.restore(identifier).status_code,200)
        self.assertEqual(self.purge(identifier).status_code,409)
        self.trash(identifier)
        entered, release = threading.Event(), threading.Event()
        def deleting(_):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("synthetic gate timeout")
            return True
        with patch.object(self.archive,"trash_for_deletion",side_effect=deleting):
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(self.purge,identifier)
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(self.restore(identifier).status_code,409)
                finally:
                    release.set()
                self.assertEqual(pending.result(timeout=5).status_code,200)

    def test_claimed_summary_blocks_trash_and_trash_blocks_new_provider_work(self):
        identifier = self.lecture(audio=False)
        self.assertEqual(self.client.post(f"/lectures/{identifier}/summary",headers=self.headers()).status_code,202)
        entered, release = threading.Event(), threading.Event()
        self.summarizer.during = lambda: (entered.set(),release.wait(5))
        with ThreadPoolExecutor(max_workers=1) as executor:
            worker = executor.submit(self.service.process_next)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.trash(identifier).status_code,409)
            finally:
                release.set()
            self.assertTrue(worker.result(timeout=5))
        before = self.snapshot(identifier)
        self.assertEqual(self.trash(identifier).status_code,200)
        self.assertEqual(self.client.post(f"/lectures/{identifier}/summary",headers=self.headers()).status_code,404)
        self.assertFalse(self.service.process_next())
        self.assertEqual(len(self.summarizer.calls),1)
        self.assertEqual(self.restore(identifier).status_code,200)
        self.assertEqual(self.snapshot(identifier),before)

    def test_trashed_rows_are_not_claimed_or_given_late_ai_results(self):
        identifier = self.lecture(audio=False)
        self.client.post(f"/lectures/{identifier}/summary",headers=self.headers())
        def force_trash():
            with self.db.connect() as connection:
                connection.execute("UPDATE lectures SET trashed_at='2026-01-01T00:00:00Z' WHERE id=?",(identifier,))
        self.summarizer.during = force_trash
        self.assertTrue(self.service.process_next())
        with self.db.connect() as connection:
            row = connection.execute("SELECT summary_json,status FROM lecture_summaries WHERE lecture_id=?",(identifier,)).fetchone()
            self.assertIsNone(row["summary_json"])
            connection.execute("UPDATE lecture_summaries SET status='queued' WHERE lecture_id=?",(identifier,))
        self.assertFalse(self.service.process_next())
        self.assertEqual(len(self.summarizer.calls),1)

    def test_restart_preserves_arbitrarily_old_trash_and_never_schedules_age_deletion(self):
        identifier = self.lecture()
        self.trash(identifier)
        with self.db.connect() as connection:
            connection.execute("UPDATE lectures SET trashed_at='2000-01-01T00:00:00Z' WHERE id=?",(identifier,))
        before = self.snapshot(identifier)
        restarted = create_app(self.settings,FakeTranscriber())
        with TestClient(restarted) as client:
            rows = client.get("/library/trash",headers=self.headers()).json()
            self.assertEqual(rows[0]["lecture_id"],identifier)
            self.assertEqual(rows[0]["trashed_at"],"2000-01-01T00:00:00Z")
            self.assertEqual(client.get(f"/lectures/{identifier}",headers=self.headers()).status_code,404)
        self.assertEqual(self.snapshot(identifier),before)
        self.assertTrue(self.store.available("user-alpha",identifier))

    def test_expired_revoked_and_paused_access_cannot_mutate_trash(self):
        identifier = self.lecture()
        for state in ("expired","revoked","paused"):
            with self.db.connect() as connection:
                connection.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)",
                    (digest(self.tokens["user-alpha"]),"user-alpha",time.time()+3600,time.time()))
                connection.execute("UPDATE operational_state SET access_enabled=1")
                if state=="expired":
                    connection.execute("UPDATE sessions SET expires_at=0 WHERE username='user-alpha'")
                elif state=="revoked":
                    connection.execute("DELETE FROM sessions WHERE username='user-alpha'")
                else:
                    connection.execute("UPDATE operational_state SET access_enabled=0")
            status = 503 if state=="paused" else 401
            for method in (self.trash,self.restore,self.purge):
                self.assertEqual(method(identifier).status_code,status,state)
            self.assertIsNone(self.state(identifier)["trashed_at"])

    def test_stale_import_startup_cleanup_cannot_permanently_delete_reversible_trash(self):
        identifier = self.lecture()
        self.trash(identifier)
        before = self.snapshot(identifier)
        with self.db.connect() as connection:
            # Simulate old/corrupt persisted job metadata resurfacing at
            # startup. Normal trash already rejects every active import.
            connection.execute("INSERT INTO imports(id,username,lecture_id,title,filename,file_fingerprint,total_bytes,status,created_at,updated_at) "
                "VALUES(?,'user-alpha',?,'synthetic','fixture.wav',?,8,'uploading',"
                "'2000-01-01T00:00:00Z','2000-01-01T00:00:00Z')",
                (str(uuid.uuid4()),identifier,"a"*64))
        restarted = create_app(self.settings,FakeTranscriber())
        with patch.object(restarted.state.archive_manager,"trash_for_deletion",return_value=True) as remote_delete:
            with TestClient(restarted) as client:
                self.assertEqual(client.get("/library/trash",headers=self.headers()).status_code,200)
        remote_delete.assert_not_called()
        self.assertEqual(self.state(identifier)["deleting"],0)
        self.assertIsNotNone(self.state(identifier)["trashed_at"])
        self.assertEqual(self.snapshot(identifier),before)
        self.assertTrue(self.store.available("user-alpha",identifier))


if __name__ == "__main__":
    unittest.main()
