"""Independent recording links: temporary DB/WAV only, no real providers."""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from server.app import create_app
from server import lecture_continuations
from server.security import digest
from server.settings import Settings


class FakeTranscriber:
    def status(self):
        return {"model_state": "ready", "model": "synthetic", "device": "cpu"}


class ContinuationApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-continuation-test-")
        directory = Path(self.temporary.name)
        self.settings = Settings(data_dir=directory / "data", model_cache_dir=directory / "models",
                                 recording_free_reserve_bytes=0, site_origins=("https://student.github.io",),
                                 admin_username="user-alpha")
        self.app = create_app(self.settings, FakeTranscriber())
        self.database = self.app.state.database
        self.client = TestClient(self.app)
        self.tokens = {"user-alpha": "synthetic-continuation-alpha", "user-beta": "synthetic-continuation-beta"}
        with self.database.connect() as connection:
            for username, token in self.tokens.items():
                connection.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                                   (digest(token), username, time.time() + 3600, time.time()))

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def headers(self, username="user-alpha"):
        return {"Authorization": f"Bearer {self.tokens[username]}"}

    def lecture(self, *, username="user-alpha", finalized=False):
        identifier = str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lectures(id,username,title,language,created_at,recording_finalized) "
                               "VALUES(?,?,'synthetic parent','ko','2026-09-09T00:00:00Z',?)",
                               (identifier, username, int(finalized)))
        return identifier

    def create(self, parent=None, *, identifier=None, username="user-alpha", **body):
        request_body = {"title": "synthetic child", "language": "ko", "asr_provider": "qwen"}
        if parent is not None:
            request_body["continuation_of"] = parent
        request_body.update(body)
        return self.client.post("/lectures", headers={**self.headers(username), "X-Lecture-Id": identifier or str(uuid.uuid4())},
                                json=request_body)

    def get(self, identifier, username="user-alpha"):
        return self.client.get(f"/lectures/{identifier}", headers=self.headers(username))

    def count(self):
        with self.database.connect() as connection:
            return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                         for table in ("lectures", "lecture_continuations"))

    def test_capability_and_ordinary_creation_remain_backwards_compatible(self):
        self.assertEqual(self.client.get("/status").status_code, 401)
        status = self.client.get("/status", headers=self.headers()).json()
        self.assertIs(status["capabilities"]["lecture_continuations"], True)
        response = self.create()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNone(response.json()["continuation_of"])
        self.assertEqual(response.json()["continuations"], [])
        self.assertNotIn("continuation_creation_verified", response.json())
        self.assertEqual(self.count(), (1, 0))

    def test_finalized_parent_links_without_changing_original_wav_raw_ai_or_drive_metadata(self):
        parent = self.lecture(finalized=True)
        chunk_id, segment_id = str(uuid.uuid4()), str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute("INSERT INTO chunks(lecture_id,chunk_id,payload_hash,start_seconds,status) VALUES(?,?,'synthetic',0,'done')",
                               (parent, chunk_id))
            connection.execute("INSERT INTO segments VALUES(?,?,?,0,1,'합성 원문')", (segment_id, parent, chunk_id))
            connection.execute("INSERT INTO lecture_metadata VALUES(?,'표시 이름','분류','학기',1,'now')", (parent,))
            connection.execute("INSERT INTO lecture_summaries(lecture_id,job_id,raw_revision,status,model,created_at,updated_at) "
                               "VALUES(?,?,?,'failed','synthetic','now','now')", (parent, str(uuid.uuid4()), "a" * 64))
            connection.execute("INSERT INTO recording_archives(lecture_id,state,object_key,updated_at) VALUES(?,'pending',?,'now')",
                               (parent, "b" * 64))
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('lectures','lecture_continuations')")]
            before = {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')] for table in tables}
            original = tuple(connection.execute("SELECT * FROM lectures WHERE id=?", (parent,)).fetchone())
        store = self.app.state.recording_store
        store.write_chunk("user-alpha", parent, start_seconds=0, overlap_seconds=0, pcm=b"\x01\x00" * 16000)
        wav_path = store.path("user-alpha", parent)
        wav_hash = hashlib.sha256(wav_path.read_bytes()).digest()
        response = self.create(parent)
        self.assertEqual(response.status_code, 201, response.text)
        child = response.json()["id"]
        self.assertNotEqual(child, parent)
        self.assertFalse(response.json()["recording_finalized"])
        self.assertEqual(response.json()["continuation_of"], parent)
        self.assertIs(response.json()["continuation_creation_verified"], True)
        self.assertEqual(self.get(parent).json()["continuations"], [child])
        self.assertEqual(self.get(child).json()["segments"], [])
        self.assertFalse(store.path("user-alpha", child).exists())
        self.assertEqual(hashlib.sha256(wav_path.read_bytes()).digest(), wav_hash)
        with self.database.connect() as connection:
            self.assertEqual(tuple(connection.execute("SELECT * FROM lectures WHERE id=?", (parent,)).fetchone()), original)
            self.assertEqual({table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')] for table in tables}, before)

    def test_unfinalized_parent_with_pending_audio_is_not_finalized_or_mutated(self):
        parent, pending = self.lecture(), str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute("INSERT INTO chunks(lecture_id,chunk_id,payload_hash,start_seconds,final_chunk,status) VALUES(?,?,'synthetic',24,0,'pending')", (parent, pending))
            before = tuple(connection.execute("SELECT * FROM chunks WHERE lecture_id=?", (parent,)).fetchone())
        result = self.create(parent)
        self.assertEqual(result.status_code, 201, result.text)
        with self.database.connect() as connection:
            self.assertEqual(tuple(connection.execute("SELECT * FROM chunks WHERE lecture_id=?", (parent,)).fetchone()), before)
            self.assertEqual(connection.execute("SELECT recording_finalized FROM lectures WHERE id=?", (parent,)).fetchone()[0], 0)

    def test_lost_creation_ack_and_concurrent_same_uuid_do_not_duplicate(self):
        parent, child = self.lecture(), str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.create(parent, identifier=child), range(2)))
        for response in responses:
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["id"], child)
            self.assertEqual(response.json()["continuation_of"], parent)
        self.assertEqual(self.create(parent, identifier=child).status_code, 201)
        self.assertEqual(self.count(), (2, 1))

    def test_same_uuid_rejects_changed_parent_or_ordinary_creation_both_directions(self):
        parent, other = self.lecture(), self.lecture()
        child = self.create(parent).json()["id"]
        for supplied in (None, other):
            self.assertEqual(self.create(supplied, identifier=child).status_code, 409)
        ordinary = self.create().json()["id"]
        self.assertEqual(self.create(parent, identifier=ordinary).status_code, 409)
        for changes in ({"title": "changed"}, {"language": "en"}, {"asr_provider": "clova"}):
            self.assertEqual(self.create(parent, identifier=child, **changes).status_code, 409)

    def test_concurrent_same_uuid_with_different_parent_has_one_winner(self):
        parents, child = [self.lecture(), self.lecture()], str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda parent: self.create(parent, identifier=child), parents))
        self.assertEqual(sorted(response.status_code for response in responses), [201, 409])
        winner = next(response.json()["continuation_of"] for response in responses if response.status_code == 201)
        self.assertEqual(self.get(child).json()["continuation_of"], winner)
        self.assertEqual(self.count(), (3, 1))

    def test_existing_child_id_cannot_be_replayed_by_other_owner_or_admin(self):
        parent = self.lecture(username="user-beta")
        child = self.create(parent, username="user-beta").json()["id"]
        response = self.create(parent, identifier=child)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(parent, response.text)
        self.assertNotIn(child, response.text)
        self.assertEqual(self.count(), (2, 1))

    def test_parent_owner_access_and_uuid_validation_leave_no_orphans(self):
        parent = self.lecture(username="user-beta")
        for supplied in (parent, str(uuid.uuid4())):
            self.assertEqual(self.create(supplied).status_code, 404)
        self.assertEqual(self.client.post("/lectures", json={"title": "synthetic", "continuation_of": parent}).status_code, 401)
        for invalid in ("not-a-uuid", 123, {}, "../private"):
            self.assertEqual(self.create(invalid).status_code, 422)
        response = self.client.post("/lectures", json={"title": "synthetic", "continuation_of": parent}, headers=self.headers("user-beta"))
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/lectures", json={"title": "synthetic", "continuation_of": parent},
                                    headers={**self.headers("user-beta"), "X-Lecture-Id": ""})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.count(), (1, 0))

    def test_self_link_and_trashed_deleting_parent_fail_without_child(self):
        parent = self.lecture()
        self.assertEqual(self.create(parent, identifier=parent, title="synthetic parent").status_code, 409)
        for column, value in (("trashed_at", "now"), ("deleting", 1)):
            with self.database.connect() as connection:
                connection.execute(f"UPDATE lectures SET {column}=? WHERE id=?", (value, parent))
            self.assertEqual(self.create(parent).status_code, 404)
            with self.database.connect() as connection:
                connection.execute("UPDATE lectures SET trashed_at=NULL,deleting=0 WHERE id=?", (parent,))
        self.assertEqual(self.count(), (1, 0))

    def test_link_insertion_failure_rolls_back_child_insert(self):
        parent = self.lecture()
        with patch.object(lecture_continuations, "link_new_lecture", side_effect=HTTPException(409, "synthetic failure")):
            self.assertEqual(self.create(parent).status_code, 409)
        self.assertEqual(self.count(), (1, 0))

    def test_visibility_only_own_active_direct_links_and_no_private_hashes(self):
        parent = self.lecture()
        child = self.create(parent).json()["id"]
        grandchild = self.create(child).json()["id"]
        listed = self.client.get("/lectures", headers=self.headers()).json()
        self.assertEqual(len(listed), 3)
        self.assertEqual(self.get(parent).json()["continuations"], [child])
        self.assertEqual(self.get(child).json()["continuations"], [grandchild])
        self.assertNotIn("parent_request_hash", json.dumps(listed))
        self.assertNotIn("username", json.dumps(listed))
        self.assertNotIn("continuation_creation_verified", json.dumps(listed))
        self.assertNotIn("continuation_creation_verified", self.get(child).json())
        for identifier in (parent, child, grandchild):
            self.assertEqual(self.get(identifier, "user-beta").status_code, 404)
        self.assertEqual(self.client.get("/lectures", headers=self.headers("user-beta")).json(), [])

    def test_trash_restore_hides_links_and_purge_preserves_independent_children(self):
        parent = self.lecture(finalized=True)
        child = self.create(parent).json()["id"]
        self.assertEqual(self.client.post(f"/lectures/{parent}/trash", headers=self.headers()).status_code, 200)
        self.assertIsNone(self.get(child).json()["continuation_of"])
        self.assertEqual(self.get(parent).status_code, 404)
        replay = self.create(parent, identifier=child)
        self.assertEqual(replay.status_code, 201)
        self.assertIsNone(replay.json()["continuation_of"])
        self.assertIs(replay.json()["continuation_creation_verified"], True)
        self.assertNotIn(parent, replay.text)
        self.assertEqual(self.client.post(f"/lectures/{parent}/restore", headers=self.headers()).status_code, 200)
        self.assertEqual(self.get(child).json()["continuation_of"], parent)
        self.assertEqual(self.client.post(f"/lectures/{parent}/trash", headers=self.headers()).status_code, 200)
        response = self.client.delete(f"/lectures/{parent}/permanent", headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.get(child).status_code, 200)
        self.assertIsNone(self.get(child).json()["continuation_of"])
        replay = self.create(parent, identifier=child)
        self.assertEqual(replay.status_code, 201)
        self.assertIsNone(replay.json()["continuation_of"])
        self.assertIs(replay.json()["continuation_creation_verified"], True)
        self.assertNotIn(parent, replay.text)
        self.assertEqual(self.create(identifier=child).status_code, 409)
        self.assertEqual(self.create(str(uuid.uuid4()), identifier=child).status_code, 409)
        self.assertEqual(self.count(), (1, 1))

    def test_hidden_child_not_listed_and_restore_keeps_link(self):
        parent = self.lecture()
        child = self.create(parent).json()["id"]
        with self.database.connect() as connection:
            connection.execute("UPDATE lectures SET recording_finalized=1 WHERE id=?", (child,))
        self.assertEqual(self.client.post(f"/lectures/{child}/trash", headers=self.headers()).status_code, 200)
        self.assertEqual(self.get(parent).json()["continuations"], [])
        self.assertEqual(self.create(parent, identifier=child).status_code, 409)
        self.assertEqual(self.client.post(f"/lectures/{child}/restore", headers=self.headers()).status_code, 200)
        self.assertEqual(self.get(parent).json()["continuations"], [child])

    def test_corrupt_cross_owner_links_are_hidden_and_cannot_be_extended(self):
        parent, child = self.lecture(username="user-beta"), self.lecture()
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lecture_continuations VALUES(?,?,?)", (child, parent, "a" * 64))
        self.assertIsNone(self.get(child).json()["continuation_of"])
        self.assertEqual(self.get(parent, "user-beta").json()["continuations"], [])
        self.assertEqual(self.create(child).status_code, 409)
        self.assertEqual(self.count(), (2, 1))

    def test_corrupt_cycle_and_depth_are_bounded(self):
        parent = self.lecture()
        child = self.create(parent).json()["id"]
        with patch.object(lecture_continuations, "MAX_CHAIN_LECTURES", 2):
            self.assertEqual(self.create(child).status_code, 409)
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lecture_continuations VALUES(?,?,?)", (parent, child, "a" * 64))
        self.assertEqual(self.create(parent).status_code, 409)
        self.assertEqual(self.count(), (2, 2))

    def test_direct_child_limit_and_replay_bypass_new_creation_limit(self):
        parent = self.lecture()
        child = self.create(parent).json()["id"]
        with patch.object(lecture_continuations, "MAX_DIRECT_CHILDREN", 1):
            self.assertEqual(self.create(parent).status_code, 409)
            self.assertEqual(self.create(parent, identifier=child).status_code, 201)
        self.assertEqual(self.count(), (2, 1))

    def test_service_pause_and_expired_session_cannot_create(self):
        parent = self.lecture()
        response = self.client.post("/admin/access", headers=self.headers(), json={"enabled": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.create(parent).status_code, 503)
        with self.database.connect() as connection:
            connection.execute("UPDATE sessions SET expires_at=0")
        self.assertEqual(self.create(parent).status_code, 401)
        self.assertEqual(self.count(), (1, 0))

    def test_replay_preserves_parent_and_normalizes_uuid_case(self):
        parent, child = self.lecture(), str(uuid.uuid4())
        result = self.create(parent.upper(), identifier=child.upper())
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["id"], child)
        self.assertEqual(result.json()["continuation_of"], parent)
        self.assertEqual(self.create(parent, identifier=child).status_code, 201)


if __name__ == "__main__":
    unittest.main()
