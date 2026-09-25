"""Course grouping and explicit review regressions; temporary DBs and fake AI only."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.security import digest
from server.settings import Settings
from server.study_notes import StudyNoteDocument, StudyNoteError


class FakeTranscriber:
    def status(self):
        return {"model_state": "ready", "model": "synthetic-local", "device": "cpu"}


class FakeReviewEngine:
    configured = True
    model = "synthetic-course-review"

    def __init__(self):
        self.calls = []
        self.during = None
        self.fail_calls = set()
        self.closed = False

    def create_unified(self, *, language, segments, supporting_sources, interrupted):
        self.calls.append({"language": language, "segments": copy.deepcopy(segments),
                           "supporting_sources": copy.deepcopy(supporting_sources)})
        if self.during:
            self.during(segments, supporting_sources, interrupted)
        if len(self.calls) in self.fail_calls:
            raise StudyNoteError("gateway_unavailable")
        return StudyNoteDocument([{"heading": "합성 상세 설명", "text": row["text"],
                                   "source_ids": [row["id"]], "edits": []} for row in segments])

    def close(self):
        self.closed = True


class CourseReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-course-review-test-")
        directory = Path(self.temporary.name)
        settings = Settings(data_dir=directory / "data", model_cache_dir=directory / "models",
                            site_origins=("https://student.github.io",), admin_username="user-alpha")
        self.engine = FakeReviewEngine()
        self.app = create_app(settings, FakeTranscriber(), course_review_maker=self.engine)
        self.database = self.app.state.database
        self.service = self.app.state.course_review_service
        self.start_patch = patch.object(self.service, "start")
        self.start = self.start_patch.start()
        self.client = TestClient(self.app)
        self.tokens = {"user-alpha": "synthetic-review-alpha", "user-beta": "synthetic-review-beta"}
        with self.database.connect() as connection:
            for username, token in self.tokens.items():
                connection.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                                   (digest(token), username, time.time() + 3600, time.time()))

    def tearDown(self):
        self.service.stop()
        self.start_patch.stop()
        self.client.close()
        self.temporary.cleanup()

    def headers(self, username="user-alpha"):
        return {"Authorization": "Bearer " + self.tokens[username]}

    def course(self, username="user-alpha", name=None):
        identifier = str(uuid.uuid4())
        body = {"id": identifier, "name": name or "합성 강의 " + identifier, "semester": "2026 가을"}
        response = self.client.post("/courses", json=body, headers=self.headers(username))
        self.assertEqual(response.status_code, 201, response.text)
        return identifier

    def lecture(self, course_id=None, *, username="user-alpha", finalized=True, name="합성 회차",
                date="2026-09-10T00:00:00Z", texts=("  첫 원문의 공백도 유지합니다.  ", "두 번째 원문 전체입니다.")):
        identifier, chunk = str(uuid.uuid4()), str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lectures(id,username,title,language,created_at,recording_finalized) "
                               "VALUES(?,?,'synthetic lecture','ko','2026-09-01T00:00:00Z',?)",
                               (identifier, username, int(finalized)))
            connection.execute("INSERT INTO chunks(lecture_id,chunk_id,payload_hash,start_seconds,status) "
                               "VALUES(?,?,'synthetic',0,'done')", (identifier, chunk))
            for index, text in enumerate(texts):
                connection.execute("INSERT INTO segments VALUES(?,?,?,?,?,?)",
                                   (str(uuid.uuid4()), identifier, chunk, index * 5, index * 5 + 4, text))
        if course_id:
            response = self.client.put(f"/lectures/{identifier}/course-session", headers=self.headers(username),
                                       json={"revision": 0, "course_id": course_id, "session_name": name, "session_at": date})
            self.assertEqual(response.status_code, 200, response.text)
        return identifier

    def material(self, *, lecture_id=None, course_id=None):
        identifier = str(uuid.uuid4())
        document = {"units": [{"index": 1, "markdown": "합성 보조 자료 전체", "warnings": []}]}
        with self.database.connect() as connection:
            connection.execute("INSERT INTO study_materials(id,username,lecture_id,course_id,filename,kind,size_bytes,uploaded_bytes,sha256,storage_name,status,document_json,created_at,updated_at) "
                               "VALUES(?,'user-alpha',?,?,'synthetic.pdf','pdf',1,1,?,?,'ready',?,'now','now')",
                               (identifier, lecture_id, course_id, "a" * 64, identifier + ".pdf", json.dumps(document)))
        return identifier

    def post(self, course, *, identifier=None, selected=None, username="user-alpha"):
        body = {"id": identifier or str(uuid.uuid4())}
        if selected is not None:
            body["lecture_ids"] = selected
        return self.client.post(f"/courses/{course}/reviews", json=body, headers=self.headers(username))

    def queued(self, course, **kwargs):
        response = self.post(course, **kwargs)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "queued")
        return response.json()["id"]

    def get(self, course, review, username="user-alpha"):
        return self.client.get(f"/courses/{course}/reviews/{review}", headers=self.headers(username))

    def row(self, review):
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM course_review_jobs WHERE id=?", (review,)).fetchone()
            return dict(row) if row else None

    def raw(self, lecture):
        with self.database.connect() as connection:
            return self.service.raw_segments(connection, lecture)

    def assert_preserved(self, document, lectures):
        self.assertEqual(document["format"], "course_review")
        self.assertEqual([item["lecture_id"] for item in document["sessions"]], lectures)
        for item in document["sessions"]:
            note = item["document"]
            self.assertEqual(note["format"], "unified_study_note")
            self.assertEqual([row for section in note["sections"] for row in section["originals"]], self.raw(item["lecture_id"]))
            self.assertTrue(note["coverage"]["complete"])
            self.assertIs(note["coverage"]["semantic_verified"], False)

    def test_overall_overview_is_derived_from_validated_session_points(self):
        course = self.course()
        lecture = self.lecture(course)
        identifier = self.queued(course)
        self.service.process_next()
        document = json.loads(self.row(identifier)['document_json'])
        document['sessions'][0]['document']['overview'] = [{'text':'합성 개요 핵심', 'source_ids':[self.raw(lecture)[0]['id']]}]
        with self.database.connect() as connection:
            connection.execute('UPDATE course_review_jobs SET document_json=? WHERE id=?',(json.dumps(document),identifier))
        result = self.get(course,identifier).json()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['document']['overview'][0]['lecture_id'],lecture)
        self.assertEqual(result['document']['overview'][0]['text'],'합성 개요 핵심')
        self.assertIn('## 전체 수업 핵심 흐름',result['markdown'])
        self.assertIn('합성 개요 핵심',result['markdown'])
        self.assert_preserved(result['document'],[lecture])

    def test_completed_same_source_is_reused_after_browser_loses_request_id(self):
        course = self.course()
        self.lecture(course)
        identifier = self.queued(course)
        self.service.process_next()
        calls = len(self.engine.calls)
        retried = self.post(course)
        self.assertEqual(retried.status_code, 202)
        self.assertEqual(retried.json()['id'], identifier)
        self.assertEqual(retried.json()['status'], 'completed')
        self.assertEqual(len(self.engine.calls), calls)
        self.assertFalse(self.service.process_next())
        with self.database.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM course_review_jobs').fetchone()[0], 1)

    def test_every_course_review_route_is_owner_scoped_without_admin_bypass(self):
        foreign = self.course("user-beta")
        lecture = self.lecture(foreign, username="user-beta")
        review = self.queued(foreign, username="user-beta")
        urls = [f"/courses/{foreign}", f"/courses/{foreign}/reviews", f"/courses/{foreign}/reviews/{review}",
                f"/lectures/{lecture}/course-session"]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 401)
            self.assertEqual(self.client.get(url, headers=self.headers()).status_code, 404)
        self.assertEqual(self.post(foreign).status_code, 404)
        self.assertEqual(self.client.patch(f"/courses/{foreign}", json={"revision": 1, "name": "stolen"}, headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.put(f"/lectures/{lecture}/course-session", json={"revision": 1, "course_id": None}, headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.get("/courses", headers=self.headers()).json()["total"], 0)
        self.assertTrue(self.service.process_next())
        self.assertEqual(self.get(foreign, review, "user-beta").json()["status"], "completed")
        self.assertEqual(self.get(foreign, review).status_code, 404)

    def test_course_create_idempotency_normalized_duplicate_and_revision_compare_and_swap(self):
        identifier = self.course(name="Intro Theory")
        body = {"id": identifier, "name": "Intro Theory", "semester": "2026 가을"}
        self.assertEqual(self.client.post("/courses", json=body, headers=self.headers()).status_code, 201)
        body["name"] = "Different"
        self.assertEqual(self.client.post("/courses", json=body, headers=self.headers()).status_code, 409)
        body.update(id=str(uuid.uuid4()), name="ＩＮＴＲＯ   theory")
        self.assertEqual(self.client.post("/courses", json=body, headers=self.headers()).status_code, 409)
        updated = self.client.patch(f"/courses/{identifier}", json={"revision": 1, "name": "변경한 강의"}, headers=self.headers())
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["revision"], 2)
        self.assertEqual(updated.json()["semester"], "2026 가을")
        stale = self.client.patch(f"/courses/{identifier}", json={"revision": 1, "name": "덮어쓰기"}, headers=self.headers())
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.client.get(f"/courses/{identifier}", headers=self.headers()).json()["course"]["name"], "변경한 강의")

    def test_session_partial_updates_preserve_fields_and_require_current_revision_and_owned_course(self):
        course = self.course()
        lecture = self.lecture(course, name="기존 회차", date="2026-09-10T09:00:00+09:00")
        url = f"/lectures/{lecture}/course-session"
        response = self.client.put(url, headers=self.headers(), json={"revision": 1, "session_name": "새 회차"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["course_id"], course)
        self.assertEqual(response.json()["session_at"], "2026-09-10T00:00:00+00:00")
        self.assertEqual(response.json()["session_revision"], 2)
        self.assertEqual(self.client.put(url, headers=self.headers(), json={"revision": 1, "course_id": None}).status_code, 409)
        foreign = self.course("user-beta")
        self.assertEqual(self.client.put(url, headers=self.headers(), json={"revision": 2, "course_id": foreign}).status_code, 404)
        self.assertEqual(self.client.get(url, headers=self.headers()).json()["course_id"], course)
        detached = self.client.put(url, headers=self.headers(), json={"revision": 2, "course_id": None}).json()
        self.assertIsNone(detached["course_id"])
        self.assertEqual(detached["session_name"], "새 회차")

    def test_review_request_is_idempotent_through_processing_completion_and_keeps_exact_scope(self):
        course = self.course()
        one, two = self.lecture(course), self.lecture(course, date="2026-09-11T00:00:00Z")
        review = self.queued(course, selected=[two, one])
        saved = self.row(review)
        repeat = self.post(course, identifier=review, selected=[one, two])
        self.assertEqual(repeat.status_code, 202)
        self.assertEqual(self.row(review), saved)
        self.assertEqual(self.post(course, identifier=review, selected=[one]).status_code, 409)
        self.assertEqual(self.post(course, identifier=review).status_code, 409)
        self.assertEqual(self.post(course).status_code, 409)
        def repeat_processing(*_):
            self.assertEqual(self.post(course, identifier=review, selected=[one, two]).json()["status"], "processing")
        self.engine.during = repeat_processing
        self.assertTrue(self.service.process_next())
        self.assertEqual(len(self.engine.calls), 2)
        before = self.row(review)
        self.assertEqual(self.post(course, identifier=review, selected=[two, one]).json()["status"], "completed")
        self.assertEqual(self.row(review), before)
        self.assertFalse(self.service.process_next())
        self.assertEqual(len(self.engine.calls), 2)

    def test_completed_bundle_keeps_every_ordered_session_source_and_course_and_session_materials(self):
        course = self.course()
        later = self.lecture(course, date="2026-09-12T00:00:00Z", name="두 번째")
        earlier = self.lecture(course, date="2026-09-10T00:00:00Z", name="첫 번째")
        shared, specific = self.material(course_id=course), self.material(lecture_id=earlier)
        originals = {lecture: self.raw(lecture) for lecture in [earlier, later]}
        review = self.queued(course)
        self.assertTrue(self.service.process_next())
        before = self.row(review)
        result = self.get(course, review).json()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["stale"])
        self.assert_preserved(result["document"], [earlier, later])
        self.assertEqual({source["id"] for source in self.engine.calls[0]["supporting_sources"]}, {shared + ":1", specific + ":1"})
        self.assertEqual([source["id"] for source in self.engine.calls[1]["supporting_sources"]], [shared + ":1"])
        self.assertIn("# 강의 복습 정리본", result["markdown"])
        self.assertIn("## 첫 번째", result["markdown"])
        self.assertIn("## 두 번째", result["markdown"])
        self.assertEqual(self.row(review), before)
        self.assertEqual({lecture: self.raw(lecture) for lecture in originals}, originals)

    def test_unfinished_empty_or_foreign_selection_is_explicitly_rejected_without_silent_skipping(self):
        course = self.course()
        self.assertEqual(self.post(course).status_code, 409)
        ready = self.lecture(course)
        self.lecture(course, finalized=False)
        self.assertEqual(self.post(course).status_code, 409)
        self.assertEqual(self.post(course, selected=[ready, ready]).status_code, 422)
        self.assertEqual(self.post(course, selected=[str(uuid.uuid4())]).status_code, 404)
        self.assertEqual(self.post(course, selected=[]).status_code, 422)
        review = self.queued(course, selected=[ready])
        self.assertTrue(self.service.process_next())
        self.assert_preserved(self.get(course, review).json()["document"], [ready])

    def test_invalid_or_oversized_raw_source_is_a_safe_input_error_without_a_provider_call(self):
        for text, expected in [(None, 422), ("x" * 24001, 413)]:
            course = self.course()
            self.lecture(course, texts=() if text is None else (text,))
            response = self.post(course)
            self.assertEqual(response.status_code, expected)
        self.assertEqual(self.engine.calls, [])

    def test_raw_change_before_or_during_generation_never_saves_changed_results(self):
        for during in (False, True):
            course = self.course()
            lecture = self.lecture(course)
            review = self.queued(course)
            def change(*_):
                with self.database.connect() as connection:
                    connection.execute("UPDATE segments SET text='변경된 원문' WHERE lecture_id=?", (lecture,))
            before = len(self.engine.calls)
            if during:
                self.engine.during = change
            else:
                change()
            self.assertTrue(self.service.process_next())
            row = self.row(review)
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["error_code"], "source_changed")
            self.assertIsNone(row["document_json"])
            self.assertEqual(len(self.engine.calls), before + int(during))
            self.engine.during = None

    def test_material_change_before_or_during_generation_never_saves_changed_results(self):
        for during in (False, True):
            course = self.course()
            self.lecture(course)
            material = self.material(course_id=course)
            review = self.queued(course)
            def change(*_):
                with self.database.connect() as connection:
                    connection.execute("UPDATE study_materials SET revision=revision+1 WHERE id=?", (material,))
            before = len(self.engine.calls)
            if during:
                self.engine.during = change
            else:
                change()
            self.assertTrue(self.service.process_next())
            self.assertEqual(self.row(review)["error_code"], "source_changed")
            self.assertIsNone(self.row(review)["document_json"])
            self.assertEqual(len(self.engine.calls), before + int(during))
            self.engine.during = None

    def test_access_pause_before_claim_and_during_work_prevents_publication(self):
        course = self.course()
        self.lecture(course)
        review = self.queued(course)
        with self.database.connect() as connection:
            connection.execute("UPDATE operational_state SET access_enabled=0")
        self.assertFalse(self.service.process_next())
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.get(course, review).status_code, 503)
        with self.database.connect() as connection:
            connection.execute("UPDATE operational_state SET access_enabled=1")
        def pause(_segments, _materials, interrupted):
            with self.database.connect() as connection:
                connection.execute("UPDATE operational_state SET access_enabled=0")
            self.assertTrue(interrupted())
        self.engine.during = pause
        self.assertTrue(self.service.process_next())
        self.assertEqual(self.row(review)["status"], "failed")
        self.assertIsNone(self.row(review)["document_json"])
        self.assertEqual(len(self.engine.calls), 1)

    def test_membership_removal_trash_or_unfinalized_source_during_call_discards_result(self):
        for mutation in ("membership", "trash", "unfinished"):
            course = self.course()
            lecture = self.lecture(course)
            review = self.queued(course)
            def change(_segments, _materials, interrupted):
                with self.database.connect() as connection:
                    if mutation == "membership":
                        connection.execute("UPDATE course_sessions SET course_id=NULL,revision=revision+1 WHERE lecture_id=?", (lecture,))
                    elif mutation == "trash":
                        connection.execute("UPDATE lectures SET trashed_at='synthetic-trash' WHERE id=?", (lecture,))
                    else:
                        connection.execute("UPDATE lectures SET recording_finalized=0 WHERE id=?", (lecture,))
                self.assertTrue(interrupted())
            self.engine.during = change
            self.assertTrue(self.service.process_next())
            self.assertEqual(self.row(review)["error_code"], "source_changed")
            self.assertIsNone(self.row(review)["document_json"])

    def test_restart_terminates_started_requests_without_replay_but_never_started_queue_can_run(self):
        for status, attempts in [("processing", 1), ("queued", 1), ("queued", 0)]:
            course = self.course()
            self.lecture(course)
            review = self.queued(course)
            with self.database.connect() as connection:
                connection.execute("UPDATE course_review_jobs SET status=?,attempts=? WHERE id=?", (status, attempts, review))
            before = len(self.engine.calls)
            self.service.recover()
            if attempts:
                self.assertEqual(self.row(review)["error_code"], "interrupted")
                self.assertFalse(self.service.process_next())
                self.assertEqual(len(self.engine.calls), before)
            else:
                self.assertEqual(self.row(review)["status"], "queued")
                self.assertTrue(self.service.process_next())
                self.assertEqual(self.row(review)["status"], "completed")
                self.assertEqual(len(self.engine.calls), before + 1)

    def test_final_commit_failure_or_lost_ack_settles_once_without_repeating_ai(self):
        for lost_ack in (False, True):
            course = self.course()
            self.lecture(course)
            review = self.queued(course)
            original_connect = self.database.connect
            class ConnectionProxy:
                def __init__(self, connection):
                    self.connection, self.final_write = connection, False
                def execute(self, sql, *args):
                    if sql.startswith("UPDATE course_review_jobs SET status='completed'"):
                        self.final_write = True
                    return self.connection.execute(sql, *args)
                def __getattr__(self, name):
                    return getattr(self.connection, name)
            @contextmanager
            def fail_final_commit():
                with original_connect() as connection:
                    proxy = ConnectionProxy(connection)
                    yield proxy
                    if proxy.final_write and not lost_ack:
                        raise sqlite3.OperationalError("synthetic-private-save-failure")
                if proxy.final_write and lost_ack:
                    raise sqlite3.OperationalError("synthetic-private-commit-ack-lost")
            before = len(self.engine.calls)
            with patch.object(self.database, "connect", fail_final_commit):
                with self.assertRaises(sqlite3.OperationalError):
                    self.service.process_next()
            self.assertIsNotNone(self.service._unsettled)
            self.assertFalse(self.service.process_next())
            self.assertIsNone(self.service._unsettled)
            self.assertEqual(len(self.engine.calls), before + 1)
            self.assertEqual(self.row(review)["status"], "completed" if lost_ack else "failed")
            self.assertNotIn("synthetic-private", json.dumps(self.row(review)))

    def test_engine_mutation_cannot_change_the_saved_raw_or_material_snapshot(self):
        course = self.course()
        lecture = self.lecture(course)
        material = self.material(course_id=course)
        original = self.raw(lecture)
        def mutate(segments, materials, _interrupted):
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
            segments[0]["id"] = "foreign"
            segments[0]["text"] = "forged transcript"
            materials[0]["id"] = "foreign-material"
            materials[0]["text"] = "forged evidence"
        self.engine.during = mutate
        review = self.queued(course)
        self.assertTrue(self.service.process_next())
        result = self.get(course, review).json()
        self.assertEqual(result["status"], "completed")
        self.assert_preserved(result["document"], [lecture])
        saved_materials = result["document"]["sessions"][0]["document"]["supporting_sources"]
        self.assertEqual(saved_materials[0]["id"], material + ":1")
        self.assertEqual(saved_materials[0]["text"], "합성 보조 자료 전체")
        self.assertEqual(self.raw(lecture), original)

    def test_completed_review_survives_new_unfinished_session_as_stale_without_regeneration(self):
        course = self.course()
        original = self.lecture(course)
        review = self.queued(course)
        self.service.process_next()
        before = self.row(review)
        self.lecture(course, finalized=False, date="2026-09-11T00:00:00Z")
        result = self.get(course, review).json()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["stale"])
        self.assert_preserved(result["document"], [original])
        self.assertEqual(self.row(review), before)
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.post(course).status_code, 409)
        self.assertEqual(self.post(course, identifier=review).json()["status"], "completed")
        self.assertEqual(len(self.engine.calls), 1)

    def test_completed_review_material_change_marks_stale_but_source_change_hides_invalid_saved_artifact(self):
        course = self.course()
        lecture = self.lecture(course)
        material = self.material(course_id=course)
        review = self.queued(course)
        self.service.process_next()
        before = self.row(review)
        with self.database.connect() as connection:
            connection.execute("UPDATE study_materials SET revision=revision+1 WHERE id=?", (material,))
        result = self.get(course, review).json()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["stale"])
        with self.database.connect() as connection:
            connection.execute("UPDATE segments SET text='다른 원문' WHERE lecture_id=?", (lecture,))
        result = self.get(course, review).json()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["stale"])
        self.assertIsNone(result["document"])
        self.assertIsNone(result["markdown"])
        self.assertEqual(self.row(review), before)
        self.assertEqual(len(self.engine.calls), 1)

    def test_later_provider_failure_keeps_earlier_work_and_every_remaining_original(self):
        course = self.course()
        lectures = [self.lecture(course, date=f"2026-09-{10+index}T00:00:00Z") for index in range(3)]
        self.engine.fail_calls = {2}
        review = self.queued(course)
        self.service.process_next()
        result = self.get(course, review).json()
        self.assertEqual(result["status"], "completed")
        self.assert_preserved(result["document"], lectures)
        self.assertIn("incomplete_batches", result["document"]["warnings"])
        self.assertEqual(len(self.engine.calls), 3)
        self.assertFalse(self.service.process_next())
        self.assertEqual(len(self.engine.calls), 3)

    def test_active_review_blocks_lecture_trash_and_shutdown_cancels_before_result_save(self):
        course = self.course()
        lecture = self.lecture(course)
        review = self.queued(course)
        self.assertEqual(self.client.delete(f"/lectures/{lecture}", headers=self.headers()).status_code, 409)
        def stop(_segments, _materials, interrupted):
            self.assertEqual(self.client.delete(f"/lectures/{lecture}", headers=self.headers()).status_code, 409)
            self.service.request_shutdown()
            self.assertTrue(interrupted())
        self.engine.during = stop
        self.assertTrue(self.service.process_next())
        self.assertEqual(self.row(review)["status"], "failed")
        self.assertIsNone(self.row(review)["document_json"])
        self.assertFalse(self.service.process_next())
        self.assertEqual(len(self.engine.calls), 1)


if __name__ == "__main__":
    unittest.main()
