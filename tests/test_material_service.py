"""Private material API/worker regressions; synthetic bytes and a fake parser only."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from server.app import create_app
from server.material_service import (
    MaterialBody, MaterialDownloadResponse, MAX_PART, material_snapshot,
)
from server.security import digest
from server.settings import Settings


class FakeTranscriber:
    def status(self):
        return {"model_state": "ready", "model": "synthetic-local", "device": "cpu"}


class FakeConverter:
    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.error = None
        self.wrong_hash = False

    def __call__(self, path, kind, *, cancel):
        content = path.read_bytes()
        self.calls.append((kind, content))
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("synthetic test gate timeout")
        if cancel():
            raise RuntimeError("synthetic cancellation")
        if self.error:
            raise self.error
        unit = {"index": 1, "source_id": "page:1" if kind == "pdf" else "slide:1",
                "text": "합성 자료의 전체 본문", "markdown": "합성 자료의 전체 본문", "warnings": []}
        return {"kind": kind, "sha256": "0" * 64 if self.wrong_hash else hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content), "unit_count": 1, "units": [unit],
                "markdown": "# 합성 자료\n\n" + unit["markdown"] + "\n", "warnings": [],
                "extraction_version": "material-v1"}


class MaterialServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-material-service-")
        directory = Path(self.temporary.name)
        settings = Settings(data_dir=directory / "data", model_cache_dir=directory / "models",
                            site_origins=("https://student.github.io",), admin_username="user-alpha")
        self.app = create_app(settings, FakeTranscriber())
        self.database = self.app.state.database
        self.service = self.app.state.material_service
        self.converter = FakeConverter()
        self.service.converter = self.converter
        # No lifespan: no model, provider, archive, or background note service starts.
        self.client = TestClient(self.app)
        self.tokens = {"user-alpha": "synthetic-material-alpha", "user-beta": "synthetic-material-beta"}
        with self.database.connect() as connection:
            for username, token in self.tokens.items():
                connection.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                                   (digest(token), username, time.time() + 3600, time.time()))
        self.lecture_id = self.lecture()

    def tearDown(self):
        self.converter.release.set()
        self.service.stop(timeout=5)
        self.client.close()
        self.temporary.cleanup()

    def headers(self, username="user-alpha"):
        return {"Authorization": "Bearer " + self.tokens[username]}

    def lecture(self, username="user-alpha"):
        identifier = str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lectures(id,username,title,language,created_at,recording_finalized) "
                               "VALUES(?,?,'synthetic lecture','ko','2026-09-25T00:00:00Z',1)",
                               (identifier, username))
        return identifier

    def course(self, username="user-alpha"):
        identifier = str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute("INSERT INTO course_groups(id,username,name,normalized_name,created_at,updated_at) "
                               "VALUES(?,?,?,?,'now','now')", (identifier, username, identifier, identifier))
        return identifier

    def row(self, identifier):
        with self.database.connect() as connection:
            found = connection.execute("SELECT * FROM study_materials WHERE id=?", (identifier,)).fetchone()
            return dict(found) if found else None

    def reserve(self, content=b"%PDF-1.4\nsynthetic bytes\n", *, kind="pdf", username="user-alpha",
                lecture_id=None, course_id=None, identifier=None, expected_hash=None):
        body = {"id": identifier or str(uuid.uuid4()), "filename": "합성 자료." + kind,
                "size_bytes": len(content), "sha256": expected_hash or hashlib.sha256(content).hexdigest()}
        scope = f"courses/{course_id}" if course_id else f"lectures/{lecture_id or self.lecture_id}"
        response = self.client.post(f"/{scope}/materials", json=body, headers=self.headers(username))
        self.assertEqual(response.status_code, 201, response.text)
        return body, response.json()

    def upload(self, identifier, content, offset=0, username="user-alpha", part_hash=None):
        return self.client.put(f"/study-materials/{identifier}/content", content=content,
                               headers={**self.headers(username), "X-Upload-Offset": str(offset),
                                        "X-Part-SHA256": part_hash or hashlib.sha256(content).hexdigest()})

    def uploaded(self, content=b"%PDF-1.4\nsynthetic bytes\n", **kwargs):
        body, _ = self.reserve(content, **kwargs)
        response = self.upload(body["id"], content, username=kwargs.get("username", "user-alpha"))
        self.assertEqual(response.status_code, 200, response.text)
        return body["id"]

    def convert(self, identifier, username="user-alpha"):
        return self.client.post(f"/study-materials/{identifier}/convert", headers=self.headers(username))

    def settled(self, identifier):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = self.row(identifier)
            with self.service.lock:
                done = not self.service.futures
            if done and row["error_code"] != "converting":
                return row
            time.sleep(0.01)
        self.fail("Synthetic material conversion did not settle")

    def assert_slots_available(self):
        self.assertTrue(self.service.capacity.acquire(blocking=False))
        self.assertTrue(self.service.capacity.acquire(blocking=False))
        self.assertFalse(self.service.capacity.acquire(blocking=False))
        self.service.capacity.release()
        self.service.capacity.release()

    def test_authentication_and_every_material_route_are_owner_scoped(self):
        foreign_lecture = self.lecture("user-beta")
        identifier = self.uploaded(username="user-beta", lecture_id=foreign_lecture)
        foreign_course = self.course("user-beta")
        for url in (f"/lectures/{foreign_lecture}/materials", f"/courses/{foreign_course}/materials"):
            self.assertEqual(self.client.get(url, headers=self.headers()).status_code, 404)
            self.assertEqual(self.client.post(url, json={"id": str(uuid.uuid4()), "filename": "x.pdf",
                             "size_bytes": 1, "sha256": "a" * 64}, headers=self.headers()).status_code, 404)
        for suffix in ("", "/original", "/markdown"):
            url = f"/study-materials/{identifier}{suffix}"
            self.assertEqual(self.client.get(url).status_code, 401)
            self.assertEqual(self.client.get(url, headers=self.headers()).status_code, 404)
        self.assertEqual(self.upload(identifier, b"x").status_code, 404)
        self.assertEqual(self.convert(identifier).status_code, 404)
        self.assertEqual(self.client.delete(f"/study-materials/{identifier}", headers=self.headers()).status_code, 404)
        self.assertEqual(self.row(identifier)["uploaded_bytes"], self.row(identifier)["size_bytes"])
        self.assertEqual(self.converter.calls, [])

    def test_reservation_replay_is_idempotent_and_changed_manifest_cannot_overwrite(self):
        content = b"original synthetic content"
        body, initial = self.reserve(content)
        identifier = body["id"]
        self.assertEqual(self.upload(identifier, content).status_code, 200)
        path = self.service._path(self.row(identifier))
        url = f"/lectures/{self.lecture_id}/materials"
        response = self.client.post(url, json=body, headers=self.headers())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["uploaded_bytes"], len(content))
        for change in ({"filename": "other.pdf"}, {"size_bytes": len(content) + 1},
                       {"sha256": "0" * 64}, {"filename": "other.pptx"}):
            with self.subTest(change=change):
                self.assertEqual(self.client.post(url, json={**body, **change}, headers=self.headers()).status_code, 409)
        other = self.lecture()
        self.assertEqual(self.client.post(f"/lectures/{other}/materials", json=body, headers=self.headers()).status_code, 409)
        beta = self.lecture("user-beta")
        self.assertEqual(self.client.post(f"/lectures/{beta}/materials", json=body, headers=self.headers("user-beta")).status_code, 409)
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.row(identifier)["revision"], initial["revision"])

    def test_invalid_filename_type_and_size_are_rejected_before_any_reservation(self):
        base = {"id": str(uuid.uuid4()), "filename": "x.pdf", "size_bytes": 1, "sha256": "a" * 64}
        for change in ({"filename": "../x.pdf"}, {"filename": "x\\x.pdf"}, {"filename": " x.pdf"},
                       {"filename": "x.exe"}, {"filename": "x\n.pdf"}, {"size_bytes": True},
                       {"size_bytes": 0}, {"sha256": "A" * 64}, {"extra": "no"}):
            with self.subTest(change=change):
                response = self.client.post(f"/lectures/{self.lecture_id}/materials", json={**base, **change}, headers=self.headers())
                self.assertEqual(response.status_code, 422)
        self.assertIsNone(self.row(base["id"]))

    def test_parts_replay_exactly_and_reject_offsets_hashes_overlaps_and_changed_bytes(self):
        content = b"first-last"
        body, _ = self.reserve(content)
        identifier = body["id"]
        self.assertEqual(self.upload(identifier, content[:6]).status_code, 200)
        self.assertEqual(self.upload(identifier, content[:6]).status_code, 200)
        for part, offset, expected in ((b"wrong!", 0, 409), (b"xx", 5, 409), (b"x", 7, 409),
                                       (b"far too long", 6, 409), (b"", 6, 422)):
            self.assertEqual(self.upload(identifier, part, offset).status_code, expected)
        self.assertEqual(self.upload(identifier, content[6:], 6, part_hash="0" * 64).status_code, 422)
        self.assertEqual(self.client.get(f"/study-materials/{identifier}/original", headers=self.headers()).status_code, 409)
        self.assertEqual(self.convert(identifier).status_code, 409)
        self.assertEqual(self.upload(identifier, content[6:], 6).status_code, 200)
        self.assertEqual(self.service._path(self.row(identifier)).read_bytes(), content)
        self.assertEqual(self.row(identifier)["uploaded_bytes"], len(content))
        with self.assertRaises(HTTPException) as raised:
            self.service.write_part(identifier, "user-alpha", b"x" * (MAX_PART + 1), 0,
                                    hashlib.sha256(b"x" * (MAX_PART + 1)).hexdigest())
        self.assertEqual(raised.exception.status_code, 422)
        self.assert_slots_available()

    def test_pdf_and_pptx_fake_conversion_downloads_preserve_original_and_full_markdown(self):
        for kind, content in (("pdf", b"%PDF-synthetic\nlast-page"), ("pptx", b"PK\x03\x04synthetic-final-slide")):
            with self.subTest(kind=kind):
                identifier = self.uploaded(content, kind=kind)
                self.assertEqual(self.client.get(f"/study-materials/{identifier}/markdown", headers=self.headers()).status_code, 409)
                self.assertEqual(self.convert(identifier).status_code, 202)
                row = self.settled(identifier)
                self.assertEqual(row["status"], "ready")
                self.assertEqual(row["revision"], 2)
                info = self.client.get(f"/study-materials/{identifier}", headers=self.headers()).json()
                self.assertEqual(info["unit_count"], 1)
                self.assertEqual(info["document"]["units"][0]["index"], 1)
                response = self.client.get(f"/study-materials/{identifier}/markdown", headers=self.headers())
                self.assertEqual(response.text, info["document"]["markdown"])
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                original = self.client.get(f"/study-materials/{identifier}/original", headers=self.headers())
                self.assertEqual(original.content, content)
                self.assertEqual(original.headers["content-type"], "application/octet-stream")
                self.assertIn(f'lecture-material.{kind}', original.headers["content-disposition"])
                self.assertEqual(self.upload(identifier, content).status_code, 200)
                self.assertEqual(self.upload(identifier, b"x" * len(content)).status_code, 409)
                self.assertEqual(self.convert(identifier).json()["status"], "ready")
        self.assertEqual(len(self.converter.calls), 2)

    def test_final_input_hash_mismatch_never_invokes_converter(self):
        content = b"received but differs from declared final hash"
        identifier = self.uploaded(content, expected_hash="a" * 64)
        self.assertEqual(self.convert(identifier).status_code, 409)
        self.assertEqual(self.converter.calls, [])
        self.assertEqual(self.row(identifier)["error_code"], "awaiting_upload")
        self.assertEqual(self.service._path(self.row(identifier)).read_bytes(), content)
        self.assert_slots_available()

    def test_changed_converter_input_hash_and_raw_errors_are_never_published(self):
        identifier = self.uploaded()
        self.converter.wrong_hash = True
        self.assertEqual(self.convert(identifier).status_code, 202)
        self.assertEqual(self.settled(identifier)["status"], "failed")
        self.converter.wrong_hash = False
        self.converter.error = RuntimeError("synthetic-secret-and-private-path-MUST-NOT-LEAK")
        self.assertEqual(self.convert(identifier).status_code, 202)
        self.settled(identifier)
        response = self.client.get(f"/study-materials/{identifier}", headers=self.headers())
        self.assertNotIn("MUST-NOT-LEAK", response.text)
        self.assertEqual(response.json()["error_code"], "conversion_failed")
        self.assertIsNone(response.json()["document"])
        self.assertEqual(self.client.get(f"/study-materials/{identifier}/original", headers=self.headers()).status_code, 200)

    def test_admission_commit_failure_releases_slot_without_submitting_or_marking_converting(self):
        identifier = self.uploaded()
        original_connect = self.database.connect
        @contextmanager
        def failed_commit():
            with original_connect() as connection:
                yield connection
                raise sqlite3.OperationalError("synthetic commit failure")
        with patch.object(self.database, "connect", failed_commit), patch.object(self.service.executor, "submit") as submit:
            with self.assertRaises(sqlite3.OperationalError):
                self.service.start_conversion(identifier, "user-alpha")
            submit.assert_not_called()
        self.assertEqual(self.row(identifier)["error_code"], "awaiting_upload")
        self.assert_slots_available()

    def test_part_commit_failure_repairs_uncommitted_tail_without_duplicate_bytes(self):
        content = b"synthetic upload after rollback"
        body, _ = self.reserve(content)
        identifier = body["id"]
        original_connect = self.database.connect
        @contextmanager
        def failed_commit():
            with original_connect() as connection:
                yield connection
                raise sqlite3.OperationalError("synthetic upload commit failure")
        with patch.object(self.database, "connect", failed_commit):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.write_part(identifier, "user-alpha", content, 0, hashlib.sha256(content).hexdigest())
        self.assertEqual(self.row(identifier)["uploaded_bytes"], 0)
        self.assertEqual(self.service._path(self.row(identifier)).read_bytes(), content)
        self.assertEqual(self.upload(identifier, content).status_code, 200)
        self.assertEqual(self.service._path(self.row(identifier)).read_bytes(), content)
        self.assertEqual(self.row(identifier)["uploaded_bytes"], len(content))

    def test_failed_result_and_failure_commits_settle_on_reads_without_reconverting(self):
        for endpoint in ("detail", "list"):
            with self.subTest(endpoint=endpoint):
                identifier = self.uploaded()
                with self.database.connect() as connection:
                    connection.execute("UPDATE study_materials SET error_code='converting' WHERE id=?", (identifier,))
                job = self.row(identifier)
                original_connect = self.database.connect
                failures = []
                @contextmanager
                def failed_commit():
                    with original_connect() as connection:
                        yield connection
                        failures.append(True)
                        raise sqlite3.OperationalError("synthetic terminal commit unavailable")
                previous_calls = len(self.converter.calls)
                with patch.object(self.database, "connect", failed_commit):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.service._convert(job)
                self.assertEqual(len(failures), 2)
                self.assertEqual(len(self.converter.calls), previous_calls + 1)
                self.assertIn(identifier, self.service._unsettled)
                self.assertEqual(self.row(identifier)["error_code"], "converting")
                url = f"/study-materials/{identifier}" if endpoint == "detail" else f"/lectures/{self.lecture_id}/materials"
                response = self.client.get(url, headers=self.headers())
                self.assertEqual(response.status_code, 200, response.text)
                row = self.row(identifier)
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["error_code"], "conversion_failed")
                self.assertIsNone(row["document_json"])
                self.assertNotIn(identifier, self.service._unsettled)
                self.assertEqual(len(self.converter.calls), previous_calls + 1)
                self.assertTrue(self.service._path(row).is_file())

    def test_material_list_returns_small_counts_without_extracted_document_body(self):
        identifier = self.uploaded()
        self.assertEqual(self.convert(identifier).status_code, 202)
        self.settled(identifier)
        with self.database.connect() as connection:
            document = json.loads(self.row(identifier)["document_json"])
            document["markdown"] = "synthetic-full-document-marker" * 10000
            document["units"][0]["text"] = document["markdown"]
            connection.execute("UPDATE study_materials SET document_json=? WHERE id=?", (json.dumps(document), identifier))
        response = self.client.get(f"/lectures/{self.lecture_id}/materials", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.content), 2000)
        self.assertNotIn("synthetic-full-document-marker", response.text)
        self.assertEqual(response.json()["materials"][0]["unit_count"], 1)
        self.assertNotIn("document", response.json()["materials"][0])

    def test_executor_submission_failure_is_safe_and_releases_slot(self):
        identifier = self.uploaded()
        with patch.object(self.service.executor, "submit", side_effect=RuntimeError("synthetic-secret-submit")):
            response = self.convert(identifier)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("synthetic-secret-submit", response.text)
        self.assertEqual(self.row(identifier)["error_code"], "interrupted")
        self.assert_slots_available()

    def test_two_slot_admission_rejects_third_and_duplicate_convert_does_not_submit_twice(self):
        identifiers = [self.uploaded() for _ in range(3)]
        futures = [Future(), Future()]
        with patch.object(self.service.executor, "submit", side_effect=futures) as submit:
            for identifier in identifiers[:2]:
                self.assertEqual(self.convert(identifier).status_code, 202)
            self.assertEqual(self.convert(identifiers[0]).status_code, 202)
            self.assertEqual(self.convert(identifiers[2]).status_code, 429)
            self.assertEqual(submit.call_count, 2)
        self.assertEqual(self.row(identifiers[2])["error_code"], "awaiting_upload")
        for future in futures:
            future.set_result(None)
        self.assert_slots_available()

    def test_stale_conversion_revision_cannot_publish_document_or_delete_original(self):
        identifier = self.uploaded()
        self.converter.release.clear()
        self.assertEqual(self.convert(identifier).status_code, 202)
        self.assertTrue(self.converter.entered.wait(3))
        self.assertEqual(self.client.delete(f"/study-materials/{identifier}", headers=self.headers()).status_code, 409)
        with self.database.connect() as connection:
            connection.execute("UPDATE study_materials SET revision=revision+1 WHERE id=?", (identifier,))
        self.converter.release.set()
        row = self.settled(identifier)
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["document_json"])
        self.assertTrue(self.service._path(row).is_file())

    def test_shutdown_cancels_active_conversion_and_blocks_new_admission(self):
        identifier, pending = self.uploaded(), self.uploaded()
        self.converter.release.clear()
        self.assertEqual(self.convert(identifier).status_code, 202)
        self.assertTrue(self.converter.entered.wait(3))
        self.service.request_shutdown()
        self.assertEqual(self.convert(pending).status_code, 429)
        self.converter.release.set()
        row = self.settled(identifier)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "interrupted")
        self.assertIsNone(row["document_json"])
        self.assertEqual(self.row(pending)["error_code"], "awaiting_upload")
        self.assert_slots_available()

    def test_startup_recovery_marks_interrupted_only_and_does_not_replay_conversion(self):
        interrupted, pending, ready = self.uploaded(), self.uploaded(), self.uploaded()
        self.assertEqual(self.convert(ready).status_code, 202)
        self.settled(ready)
        with self.database.connect() as connection:
            connection.execute("UPDATE study_materials SET error_code='converting' WHERE id=?", (interrupted,))
        calls = len(self.converter.calls)
        self.service.recover()
        self.assertEqual(self.row(interrupted)["error_code"], "interrupted")
        self.assertEqual(self.row(interrupted)["status"], "failed")
        self.assertEqual(self.row(pending)["error_code"], "awaiting_upload")
        self.assertEqual(self.row(ready)["status"], "ready")
        self.assertEqual(len(self.converter.calls), calls)

    def test_database_foreign_keys_prevent_cross_owner_materials_and_course_membership(self):
        foreign_lecture, foreign_course = self.lecture("user-beta"), self.course("user-beta")
        identifier = self.uploaded()
        for statement, values in (
            ("UPDATE study_materials SET lecture_id=? WHERE id=?", (foreign_lecture, identifier)),
            ("UPDATE study_materials SET lecture_id=NULL,course_id=? WHERE id=?", (foreign_course, identifier)),
            ("UPDATE study_materials SET course_id=? WHERE id=?", (self.course(), identifier)),
            ("INSERT INTO course_sessions(lecture_id,username,course_id,updated_at) VALUES(?,'user-alpha',?,'now')",
             (self.lecture_id, foreign_course)),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.database.connect() as connection:
                    connection.execute(statement, values)
        with self.database.connect() as connection:
            self.assertIsNone(connection.execute("PRAGMA foreign_key_check").fetchone())
        self.assertEqual(self.row(identifier)["lecture_id"], self.lecture_id)

    def test_snapshot_includes_current_course_and_lecture_and_preserves_ocr_warning(self):
        course = self.course()
        course_material = self.uploaded(course_id=course)
        lecture_material = self.uploaded()
        unrelated = self.uploaded(lecture_id=self.lecture())
        for identifier in (course_material, lecture_material, unrelated):
            self.assertEqual(self.convert(identifier).status_code, 202)
            self.settled(identifier)
        with self.database.connect() as connection:
            connection.execute("INSERT INTO course_sessions(lecture_id,username,course_id,updated_at) VALUES(?,'user-alpha',?,'now')", (self.lecture_id, course))
            document = json.loads(self.row(course_material)["document_json"])
            document["units"][0].update(text="", markdown="", warnings=["ocr_required"])
            connection.execute("UPDATE study_materials SET document_json=? WHERE id=?", (json.dumps(document), course_material))
        with self.database.connect() as connection:
            result = material_snapshot(connection, "user-alpha", [self.lecture_id])
            self.assertEqual({row["id"] for row in result["manifest"]}, {course_material, lecture_material})
            self.assertTrue(any("추출 주의" in unit["text"] for unit in result["sources"]))
            self.assertTrue(all(unit["text"] for unit in result["sources"]))
            connection.execute("UPDATE course_sessions SET course_id=NULL WHERE lecture_id=?", (self.lecture_id,))
            changed = material_snapshot(connection, "user-alpha", [self.lecture_id])
        self.assertNotEqual(result["revision"], changed["revision"])
        self.assertEqual([row["id"] for row in changed["manifest"]], [lecture_material])

    def test_snapshot_rejects_pending_material_without_silently_skipping_it(self):
        self.reserve()
        with self.database.connect() as connection:
            with self.assertRaises(HTTPException) as raised:
                material_snapshot(connection, "user-alpha", [self.lecture_id])
        self.assertEqual(raised.exception.status_code, 409)

    def test_completed_original_can_be_deleted_without_affecting_other_material(self):
        identifier, other = self.uploaded(), self.uploaded()
        path, other_path = self.service._path(self.row(identifier)), self.service._path(self.row(other))
        other_content = other_path.read_bytes()
        response = self.client.delete(f"/study-materials/{identifier}", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(path.exists())
        self.assertIsNone(self.row(identifier))
        self.assertEqual(other_path.read_bytes(), other_content)


class MaterialDescriptorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-material-descriptor-")
        self.path = Path(self.temporary.name) / "synthetic-original.pdf"
        self.path.write_bytes(b"synthetic-original" * 10000)

    def tearDown(self):
        self.temporary.cleanup()

    def assert_closed(self, descriptor):
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_descriptor_closes_after_complete_and_partial_stream(self):
        for partial in (False, True):
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            response = MaterialDownloadResponse(descriptor)
            stream = response._chunks()
            if partial:
                self.assertTrue(next(stream))
                stream.close()
            else:
                self.assertEqual(b"".join(stream), self.path.read_bytes())
            self.assert_closed(descriptor)
            response.close()  # Idempotent, including disconnect cleanup.

    def test_descriptor_closes_if_response_construction_fails(self):
        descriptor = os.open(self.path, os.O_RDONLY)
        with patch("server.material_service.StreamingResponse.__init__", side_effect=RuntimeError("synthetic constructor failure")):
            with self.assertRaises(RuntimeError):
                MaterialDownloadResponse(descriptor)
        self.assert_closed(descriptor)

    def test_descriptor_closes_when_client_disconnects_before_body_iteration(self):
        descriptor = os.open(self.path, os.O_RDONLY)
        response = MaterialDownloadResponse(descriptor)
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            raise RuntimeError("synthetic disconnect before body")
        with self.assertRaises(RuntimeError):
            asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
        self.assert_closed(descriptor)


if __name__ == "__main__":
    unittest.main()
