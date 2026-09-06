"""Question API regressions; temporary DB and synthetic local provider only."""
from __future__ import annotations

import copy
import json
import tempfile
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


class FakeTranscriber:
    def status(self):
        return {"model_state": "ready", "model": "test-local", "device": "cpu"}


class FakeAnswerer:
    configured = True
    model = "solar-pro4"

    def __init__(self):
        self.calls = []
        self.during = None
        self.error = None
        self.invalid = False
        self.closed = False

    def answer(self, question, segments, interrupted=None):
        self.calls.append({"question": question, "segments": copy.deepcopy(segments)})
        if self.during:
            self.during(interrupted)
        if self.error:
            raise self.error
        if not segments:
            return {"answerability": "insufficient_evidence", "paragraphs": []}
        return {"answerability": "answered", "paragraphs": [{
            "text": segments[0]["text"],
            "source_ids": ["outside-source" if self.invalid else segments[0]["id"]],
        }]}

    def close(self):
        self.closed = True


class QuestionFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.settings = Settings(data_dir=directory / "data", model_cache_dir=directory / "models",
                                 site_origins=("https://student.github.io",))
        self.engine = FakeAnswerer()
        self.app = create_app(self.settings, FakeTranscriber(), question_answerer=self.engine)
        self.database = self.app.state.database
        self.service = self.app.state.question_service
        self.start_patch = patch.object(self.service, "start")
        self.start = self.start_patch.start()
        self.client = TestClient(self.app)
        self.tokens = {"user-alpha": "question-alpha-token", "user-beta": "question-beta-token"}
        with self.database.connect() as connection:
            for username, token in self.tokens.items():
                connection.execute("INSERT INTO sessions(token_hash,username,expires_at,created_at) VALUES(?,?,?,?)",
                                   (digest(token), username, time.time() + 3600, time.time()))

    def tearDown(self):
        self.service.stop()
        self.start_patch.stop()
        self.client.close()
        self.temporary.cleanup()

    def headers(self, username="user-alpha"):
        return {"Authorization": f"Bearer {self.tokens[username]}"}

    def lecture(self, *, username="user-alpha", finalized=True, text="빛을 이용해 양분을 만드는 과정이다."):
        lecture_id, chunk_id, segment_id = (str(uuid.uuid4()) for _ in range(3))
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lectures(id,username,title,language,created_at,recording_finalized) "
                               "VALUES(?,?,'private-title-not-sent','ko','2026-09-06T00:00:00Z',?)",
                               (lecture_id, username, int(finalized)))
            connection.execute("INSERT INTO chunks(lecture_id,chunk_id,payload_hash,start_seconds,status) "
                               "VALUES(?,?,'test-hash',0,'done')", (lecture_id, chunk_id))
            if text is not None:
                connection.execute("INSERT INTO segments(id,lecture_id,chunk_id,start,end,text) VALUES(?,?,?,0,1,?)",
                                   (segment_id, lecture_id, chunk_id, text))
        return lecture_id, segment_id

    def post(self, lecture_id, *, identifier=None, question="양분을 만드는 과정은 무엇인가요?", username="user-alpha"):
        return self.client.post(f"/lectures/{lecture_id}/questions", headers=self.headers(username),
                                json={"id": identifier or str(uuid.uuid4()), "question": question})

    def get(self, lecture_id, identifier=None, *, username="user-alpha", **params):
        path = f"/lectures/{lecture_id}/questions" + (f"/{identifier}" if identifier else "")
        return self.client.get(path, headers=self.headers(username), params=params)

    def cancel(self, lecture_id, identifier, *, username="user-alpha"):
        return self.client.delete(f"/lectures/{lecture_id}/questions/{identifier}", headers=self.headers(username))

    def queued(self, lecture_id, **kwargs):
        response = self.post(lecture_id, **kwargs)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["question"]["status"], "queued")
        return response.json()["question"]

    def row(self, identifier):
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM lecture_questions WHERE id=?", (identifier,)).fetchone()
            return dict(row) if row else None


class QuestionApiTests(QuestionFixture, unittest.TestCase):
    def test_owner_isolation_all_routes_and_uuid_collision_are_hidden(self):
        lecture_id, segment_id = self.lecture()
        job = self.queued(lecture_id)
        foreign, _ = self.lecture(username="user-beta")
        for response in (self.get(lecture_id, username="user-beta"),
                         self.get(lecture_id, job["id"], username="user-beta"),
                         self.cancel(lecture_id, job["id"], username="user-beta"),
                         self.post(lecture_id, username="user-beta"),
                         self.post(foreign, identifier=job["id"], username="user-beta"),
                         self.get(foreign, job["id"], username="user-beta")):
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(job["question"], response.text)
        self.assertTrue(self.service.process_next())
        document = self.get(lecture_id, job["id"]).json()["question"]["document"]
        self.assertEqual(document["paragraphs"][0]["source_ids"], [segment_id])
        self.assertEqual(self.get(lecture_id, job["id"], username="user-beta").status_code, 404)

    def test_duplicate_uuid_is_same_job_in_every_state_and_conflicting_body_is_rejected(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        for _ in range(2):
            self.assertEqual(self.post(lecture_id, identifier=job["id"]).json()["question"], job)
        self.assertEqual(self.post(lecture_id, identifier=job["id"], question="다른 질문").status_code, 409)
        self.engine.during = lambda _: self.assertEqual(self.post(lecture_id, identifier=job["id"]).json()["question"]["status"], "processing")
        self.service.process_next()
        result = self.get(lecture_id, job["id"]).json()["question"]
        self.assertEqual(result["status"], "completed")
        self.engine.configured = False
        self.assertEqual(self.post(lecture_id, identifier=job["id"]).json()["question"], result)
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.row(job["id"])["attempts"], 1)

    def test_concurrent_duplicate_creates_only_one_durable_row(self):
        lecture_id, _ = self.lecture()
        identifier = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(lambda _: self.post(lecture_id, identifier=identifier), range(4)))
        self.assertTrue(all(r.status_code == 202 for r in responses))
        self.assertEqual(self.get(lecture_id).json()["total"], 1)
        self.service.process_next()
        self.assertEqual(len(self.engine.calls), 1)

    def test_empty_unfinished_and_bounded_source_rejections_make_no_job(self):
        for options, expected in (({"finalized": False}, 409), ({"text": None}, 409),
                                  ({"text": "  "}, 409), ({"text": "가" * 24001}, 413)):
            lecture_id, _ = self.lecture(**options)
            self.assertEqual(self.post(lecture_id).status_code, expected)
            self.assertEqual(self.get(lecture_id).json()["questions"], [])
        self.assertEqual(self.engine.calls, [])
        self.start.assert_not_called()

    def test_input_uuid_question_control_and_extra_fields_are_rejected(self):
        lecture_id, _ = self.lecture()
        for body in ({"id": "invalid", "question": "질문"}, {"id": str(uuid.uuid4()), "question": " "},
                     {"id": str(uuid.uuid4()), "question": "질문" * 501},
                     {"id": str(uuid.uuid4()), "question": 123},
                     {"id": str(uuid.uuid4()), "question": "질문\u0000"},
                     {"id": str(uuid.uuid4()), "question": "질문", "username": "user-beta"}):
            response = self.client.post(f"/lectures/{lecture_id}/questions", json=body, headers=self.headers())
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.get(lecture_id, "not-a-uuid").status_code, 422)

    def test_canonical_whitespace_and_uppercase_uuid_replay_without_new_work(self):
        lecture_id, _ = self.lecture()
        identifier = str(uuid.uuid4())
        job = self.queued(lecture_id, identifier=identifier.upper(), question="  질문입니다. \n")
        self.assertEqual(job["id"], identifier)
        self.assertEqual(job["question"], "질문입니다.")
        self.assertEqual(self.post(lecture_id, identifier=identifier, question="질문입니다.").json()["question"], job)

    def test_queue_and_rate_limits_do_not_reject_existing_uuid_replay(self):
        lecture_id, _ = self.lecture()
        jobs = [self.queued(lecture_id) for _ in range(3)]
        self.assertEqual(self.post(lecture_id).status_code, 429)
        self.assertEqual(self.post(lecture_id, identifier=jobs[0]["id"]).status_code, 202)
        for job in jobs:
            self.cancel(lecture_id, job["id"])
        with patch.object(self.service.limiter, "allow", return_value=False):
            self.assertEqual(self.post(lecture_id).status_code, 429)
            self.assertEqual(self.post(lecture_id, identifier=jobs[0]["id"]).status_code, 202)

    def test_cancel_queued_and_terminal_is_idempotent_and_never_erases_history(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        first = self.cancel(lecture_id, job["id"]).json()["question"]
        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(self.cancel(lecture_id, job["id"]).json()["question"], first)
        self.assertEqual(self.post(lecture_id, identifier=job["id"]).json()["question"], first)
        self.assertFalse(self.service.process_next())
        self.assertEqual(self.get(lecture_id).json()["total"], 1)
        self.assertEqual(self.engine.calls, [])

    def test_global_queue_bound_is_shared_across_accounts_without_leaking_jobs(self):
        first, _ = self.lecture()
        second, _ = self.lecture(username="user-beta")
        with patch("server.question_service.GLOBAL_QUEUE_LIMIT", 2):
            self.queued(first)
            self.queued(second, username="user-beta")
            self.assertEqual(self.post(second, username="user-beta").status_code, 429)
            self.assertEqual(self.get(second, username="user-beta").json()["total"], 1)

    def test_read_and_post_require_valid_session_and_data_access(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        self.assertEqual(self.client.get(f"/lectures/{lecture_id}/questions").status_code, 401)
        with self.database.connect() as connection:
            connection.execute("UPDATE sessions SET expires_at=0")
        for response in (self.get(lecture_id), self.get(lecture_id, job["id"]),
                         self.post(lecture_id), self.cancel(lecture_id, job["id"])):
            self.assertEqual(response.status_code, 401)
        with self.database.connect() as connection:
            connection.execute("UPDATE sessions SET expires_at=?", (time.time() + 3600,))
            connection.execute("UPDATE operational_state SET access_enabled=0")
        for response in (self.get(lecture_id), self.post(lecture_id), self.cancel(lecture_id, job["id"])):
            self.assertEqual(response.status_code, 503)
        self.assertFalse(self.service.process_next())
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sessions")
        self.assertEqual(self.get(lecture_id).status_code, 401)

    def test_trash_gate_restore_keeps_completed_answer_and_permanent_delete_cascades(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        trash = lambda: self.client.post(f"/lectures/{lecture_id}/trash", headers=self.headers())
        self.assertEqual(trash().status_code, 409)
        self.service.process_next()
        before = self.get(lecture_id, job["id"]).json()
        self.assertEqual(trash().status_code, 200)
        for response in (self.get(lecture_id), self.get(lecture_id, job["id"]),
                         self.post(lecture_id), self.cancel(lecture_id, job["id"])):
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.post(f"/lectures/{lecture_id}/restore", headers=self.headers()).status_code, 200)
        self.assertEqual(self.get(lecture_id, job["id"]).json(), before)
        self.assertEqual(trash().status_code, 200)
        self.assertEqual(self.client.delete(f"/lectures/{lecture_id}/permanent", headers=self.headers()).status_code, 200)
        self.assertIsNone(self.row(job["id"]))

    def test_history_limit_and_bounded_pages_keep_old_questions_accessible(self):
        lecture_id, _ = self.lecture()
        job = self.queued(lecture_id)
        self.cancel(lecture_id, job["id"])
        original = self.row(job["id"])
        with self.database.connect() as connection:
            keys = list(original)
            for index in range(99):
                row = dict(original, id=str(uuid.uuid4()), question=f"합성 질문 {index}", created_at=f"2026-09-06T00:{index:02}:00Z")
                connection.execute(f"INSERT INTO lecture_questions({','.join(keys)}) VALUES({','.join('?' for _ in keys)})", [row[k] for k in keys])
        self.assertEqual(self.post(lecture_id).status_code, 409)
        seen = set()
        for offset in range(0, 100, 20):
            result = self.get(lecture_id, offset=offset).json()
            self.assertEqual(result["total"], 100)
            self.assertEqual(result["history_limit"], 100)
            self.assertEqual(len(result["questions"]), 20)
            self.assertEqual(result["has_more"], offset < 80)
            seen.update(q["id"] for q in result["questions"])
        self.assertEqual(len(seen), 100)
        self.assertEqual(self.get(lecture_id, job["id"]).status_code, 200)
        for params in ({"offset": -1}, {"offset": 101}, {"limit": 0}, {"limit": 21}):
            self.assertEqual(self.get(lecture_id, **params).status_code, 422)

    def test_unconfigured_read_is_safe_and_does_not_start_work(self):
        lecture_id, _ = self.lecture()
        self.engine.configured = False
        self.assertFalse(self.get(lecture_id).json()["configured"])
        self.assertEqual(self.post(lecture_id).status_code, 503)
        self.assertFalse(self.service.process_next())
        self.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
