"""Owner-only, separately persisted lecture study notes.

Only explicit POSTs create work. A claimed job is never automatically replayed
after interruption or an uncertain save; prior ASR and AI products stay intact.
"""
from __future__ import annotations

import copy
import json
import threading
import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException

from .postprocessor import PostprocessingError
from .material_service import material_snapshot
from .study_notes import (
    StudyNoteError, coerce_study_note_document, study_note_markdown, validate_study_note_document,
    validate_study_note_source, coerce_unified_study_note_document, validate_supporting_sources,
)


_ERRORS = {
    "not_configured": "운영자가 수업 정리본 API를 설정해야 합니다.",
    "interrupted": "수업 정리본 처리가 중단되어 결과를 확인하지 못했습니다. 자동으로 다시 요청하지 않습니다.",
    "study_note_failed": "수업 정리본을 완료하지 못했습니다. 원문은 그대로 보관되며 자동으로 다시 요청하지 않습니다.",
    "study_note_save_failed": "수업 정리본 결과를 저장하지 못했습니다. 자동으로 다시 요청하지 않습니다.",
    "source_changed": "원문 또는 수업 상태가 변경되어 정리본을 저장하지 않았습니다.",
    "invalid_saved_study_note": "저장된 수업 정리본과 원문 출처를 확인하지 못했습니다.",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe_error(code):
    if isinstance(code, str) and code in _ERRORS:
        return code, _ERRORS[code]
    safe = StudyNoteError(code)
    if safe.code != code:
        return "study_note_failed", _ERRORS["study_note_failed"]
    return safe.code, str(safe)


class StudyNoteService:
    def __init__(self, settings, database, engine, limiter):
        self.settings, self.database, self.engine, self.limiter = settings, database, engine, limiter
        self.shutdown = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.thread = None
        self._unsettled = None

    @property
    def configured(self):
        return bool(getattr(self.engine, "configured", False))

    @property
    def model(self):
        return getattr(self.engine, "model", self.settings.translation_model)

    @staticmethod
    def _lecture(connection, lecture_id, username):
        row = connection.execute(
            "SELECT * FROM lectures WHERE id=? AND username=? AND deleting=0 AND trashed_at IS NULL",
            (lecture_id, username),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "수업을 찾을 수 없습니다.")
        return row

    @staticmethod
    def _access(connection):
        row = connection.execute("SELECT access_enabled FROM operational_state WHERE singleton=1").fetchone()
        return bool(row and row[0])

    def envelope(self, result):
        return {"configured": self.configured, "model": self.model, "study_note": result}

    def install(self, app, *, identity, owned_lecture, raw_segments, transcript_revision):
        self.raw_segments, self.revision = raw_segments, transcript_revision

        @app.get("/lectures/{lecture_id}/study-note")
        def get_study_note(lecture_id: str, user: dict = Depends(identity)):
            owned_lecture(lecture_id, user["username"])
            with self.database.connect() as connection:
                connection.execute("BEGIN")
                lecture = self._lecture(connection, lecture_id, user["username"])
                row = connection.execute(
                    "SELECT * FROM lecture_study_notes WHERE lecture_id=? AND username=?",
                    (lecture_id, user["username"]),
                ).fetchone()
                result = self.result(row, connection, lecture=lecture)
            return self.envelope(result)

        @app.post("/lectures/{lecture_id}/study-note", status_code=202)
        def create_study_note(lecture_id: str, user: dict = Depends(identity)):
            owned_lecture(lecture_id, user["username"])
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                lecture = self._lecture(connection, lecture_id, user["username"])
                if not self._access(connection):
                    raise HTTPException(503, "현재 수업 서비스가 일시 중지되었습니다.")
                if not lecture["recording_finalized"]:
                    raise HTTPException(409, "수업 녹음과 받아쓰기 저장이 끝난 뒤 정리본을 만들 수 있습니다.")
                existing = connection.execute(
                    "SELECT * FROM lecture_study_notes WHERE lecture_id=?", (lecture_id,),
                ).fetchone()
                if existing is not None and existing["username"] != user["username"]:
                    raise HTTPException(404, "수업 정리본을 찾을 수 없습니다.")
                if existing is not None and existing["status"] in ("queued", "processing"):
                    result = self.result(existing, connection, lecture=lecture)
                else:
                    raw = self.raw_segments(connection, lecture_id)
                    revision = self.revision(raw)
                    materials = material_snapshot(connection, user["username"], [lecture_id])
                    if (existing is not None and existing["status"] == "completed"
                            and existing["raw_revision"] == revision and existing["model"] == self.model
                            and existing["format_version"] == 2 and existing["material_revision"] == materials["revision"]):
                        result = self.result(existing, connection, lecture=lecture, raw=raw)
                        if result["status"] == "completed":
                            return self.envelope(result)
                    if not self.configured:
                        raise HTTPException(503, _ERRORS["not_configured"])
                    if not raw or not any(s["text"].strip() for s in raw):
                        raise HTTPException(409, "정리본을 만들 받아쓰기 내용이 없습니다.")
                    try:
                        validate_study_note_source(raw)
                        validate_supporting_sources(materials["sources"])
                    except StudyNoteError as error:
                        safe = StudyNoteError(error.code)
                        raise HTTPException(413 if safe.code == "source_too_large" else 422, str(safe)) from None
                    if connection.execute(
                        "SELECT 1 FROM lecture_study_notes WHERE username=? AND status IN ('queued','processing')",
                        (user["username"],),
                    ).fetchone() is not None:
                        raise HTTPException(409, "진행 중인 수업 정리본이 끝난 뒤 다시 시도하세요.")
                    if not self.limiter.allow(("lecture-study-note", user["username"]), 6, 3600):
                        raise HTTPException(429, "정리본 요청이 많습니다. 잠시 후 다시 시도하세요.")
                    now = _now()
                    connection.execute(
                        "INSERT INTO lecture_study_notes(lecture_id,username,job_id,raw_revision,status,model,created_at,updated_at,material_revision,source_manifest_json,format_version) "
                        "VALUES(?,?,?,?,'queued',?,?,?,?,?,2) ON CONFLICT(lecture_id) DO UPDATE SET "
                        "job_id=excluded.job_id,raw_revision=excluded.raw_revision,status='queued',model=excluded.model,"
                        "document_json=NULL,error_code=NULL,error=NULL,attempts=0,created_at=excluded.created_at,"
                        "updated_at=excluded.updated_at,completed_at=NULL,material_revision=excluded.material_revision,"
                        "source_manifest_json=excluded.source_manifest_json,format_version=excluded.format_version",
                        (lecture_id, user["username"], str(uuid.uuid4()), revision, self.model, now, now,
                         materials["revision"], json.dumps(materials["manifest"], ensure_ascii=False)),
                    )
                    row = connection.execute("SELECT * FROM lecture_study_notes WHERE lecture_id=?", (lecture_id,)).fetchone()
                    result = self.result(row, connection, lecture=lecture)
            # Also wake an existing queued job after a previous thread-start
            # failure, without creating a replacement or spending rate budget.
            self.start()
            self.wake.set()
            return self.envelope(result)

    def result(self, row, connection, *, lecture=None, raw=None):
        if row is None:
            return None
        result = {key: row[key] for key in (
            "lecture_id", "status", "model", "error_code", "error", "created_at", "updated_at", "completed_at",
        )}
        result.update(document=None, markdown=None, stale=False, format_version=row["format_version"])
        if row["status"] != "completed" and (
                row["status"] == "failed" or row["error_code"] is not None or row["error"] is not None):
            result["error_code"], result["error"] = _safe_error(row["error_code"])
        if row["status"] == "completed":
            try:
                lecture = lecture if lecture is not None else self._lecture(connection, row["lecture_id"], row["username"])
                if not lecture["recording_finalized"] or lecture["username"] != row["username"]:
                    raise ValueError("inactive source")
                raw = raw if raw is not None else self.raw_segments(connection, row["lecture_id"])
                if self.revision(raw) != row["raw_revision"]:
                    raise ValueError("source changed")
                document = validate_study_note_document(json.loads(row["document_json"]), raw)
                markdown = study_note_markdown(document, raw)
                if not isinstance(markdown, str):
                    raise ValueError("invalid markdown")
                try:
                    latest_materials = material_snapshot(connection, row["username"], [row["lecture_id"]])
                    stale = row["format_version"] < 2 or latest_materials["revision"] != row["material_revision"]
                except HTTPException:
                    stale = True
                result.update(document=document, markdown=markdown, stale=stale)
            except Exception:
                result.update(status="failed", error_code="invalid_saved_study_note", error=_ERRORS["invalid_saved_study_note"])
        return result

    def _terminal(self, connection, job, code):
        code, message = _safe_error(code)
        connection.execute(
            "UPDATE lecture_study_notes SET status='failed',document_json=NULL,completed_at=NULL,"
            "error_code=?,error=?,updated_at=? WHERE job_id=? AND lecture_id=? AND username=? "
            "AND status IN ('queued','processing')",
            (code, message, _now(), job["job_id"], job["lecture_id"], job["username"]),
        )

    def recover(self):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT * FROM lecture_study_notes WHERE status IN ('queued','processing')").fetchall()
            for row in rows:
                if row["status"] == "processing" or row["attempts"]:
                    self._terminal(connection, row, "interrupted")
                elif not self.configured:
                    self._terminal(connection, row, "not_configured")
                elif connection.execute(
                    "SELECT 1 FROM lectures WHERE id=? AND username=? AND deleting=0 "
                    "AND trashed_at IS NULL AND recording_finalized=1", (row["lecture_id"], row["username"]),
                ).fetchone() is None:
                    self._terminal(connection, row, "source_changed")

    def _interrupted(self, job):
        if self.shutdown.is_set():
            return True
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM lecture_study_notes n JOIN lectures l ON l.id=n.lecture_id "
                "WHERE n.job_id=? AND n.lecture_id=? AND n.username=? AND l.username=n.username "
                "AND n.status='processing' AND n.raw_revision=? AND n.model=? AND l.deleting=0 "
                "AND l.trashed_at IS NULL AND l.recording_finalized=1",
                (job["job_id"], job["lecture_id"], job["username"], job["raw_revision"], job["model"]),
            ).fetchone()
            if row is None or not self._access(connection):
                return True
            if job["format_version"] == 2:
                try:
                    return material_snapshot(connection, job["username"], [job["lecture_id"]])["revision"] != job["material_revision"]
                except (HTTPException, ValueError):
                    return True
            return False

    def process_next(self):
        if not self.process_lock.acquire(blocking=False):
            return False
        try:
            return self._process_next()
        finally:
            self.process_lock.release()

    def _process_next(self):
        if self._unsettled is not None:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._terminal(connection, self._unsettled, "study_note_save_failed")
            self._unsettled = None
        if self.shutdown.is_set() or not self.configured:
            return False
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._access(connection):
                return False
            row = connection.execute(
                "SELECT n.*,l.language FROM lecture_study_notes n JOIN lectures l ON l.id=n.lecture_id "
                "WHERE n.status='queued' AND n.attempts=0 AND l.username=n.username AND l.deleting=0 "
                "AND l.trashed_at IS NULL AND l.recording_finalized=1 ORDER BY n.created_at,n.lecture_id LIMIT 1",
            ).fetchone()
            if row is None:
                return False
            job = dict(row)
            raw = self.raw_segments(connection, job["lecture_id"])
            try:
                materials = material_snapshot(connection, job["username"], [job["lecture_id"]]) if job["format_version"] == 2 else {"sources": [], "revision": ""}
            except HTTPException:
                self._terminal(connection, job, "source_changed")
                return True
            if (self.revision(raw) != job["raw_revision"] or job["model"] != self.model
                    or materials["revision"] != job["material_revision"]):
                self._terminal(connection, job, "source_changed")
                return True
            connection.execute("UPDATE lecture_study_notes SET status='processing',attempts=1,updated_at=? WHERE job_id=?",
                               (_now(), job["job_id"]))
            self._unsettled = job
        document, failure = None, "study_note_failed"
        try:
            if not self._interrupted(job):
                arguments = {"language": job["language"], "segments": copy.deepcopy(raw),
                             "interrupted": lambda: self._interrupted(job)}
                if job["format_version"] == 2:
                    if hasattr(self.engine, "create_unified"):
                        output = self.engine.create_unified(**arguments, supporting_sources=copy.deepcopy(materials["sources"]))
                    else:
                        output = self.engine.create(**arguments)
                    document = coerce_unified_study_note_document(output.to_dict(), raw, supporting_sources=materials["sources"])
                else:
                    output = self.engine.create(**arguments)
                    document = coerce_study_note_document(output.to_dict(), raw)
                # Verify the promised download before publishing completion;
                # usable drafts are saved with warnings, but a failed rendering
                # or missing body must not be advertised as a downloadable note.
                if not isinstance(study_note_markdown(document, raw), str):
                    raise ValueError("invalid markdown")
        except PostprocessingError as error:
            document, failure = None, StudyNoteError(error.code).code
        except Exception:
            document = None
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM lecture_study_notes WHERE job_id=? AND lecture_id=? AND username=? AND status='processing'",
                (job["job_id"], job["lecture_id"], job["username"]),
            ).fetchone()
            if current is not None:
                lecture = connection.execute(
                    "SELECT 1 FROM lectures WHERE id=? AND username=? AND deleting=0 "
                    "AND trashed_at IS NULL AND recording_finalized=1", (job["lecture_id"], job["username"]),
                ).fetchone()
                try:
                    material_changed = job["format_version"] == 2 and material_snapshot(connection, job["username"], [job["lecture_id"]])["revision"] != job["material_revision"]
                except HTTPException:
                    material_changed = True
                if self.shutdown.is_set() or not self._access(connection):
                    self._terminal(connection, job, "interrupted")
                elif (lecture is None or material_changed or any(current[key] != job[key] for key in ("raw_revision", "model", "material_revision"))
                      or self.revision(self.raw_segments(connection, job["lecture_id"])) != job["raw_revision"]):
                    self._terminal(connection, job, "source_changed")
                elif document is None:
                    self._terminal(connection, job, failure)
                else:
                    now = _now()
                    connection.execute(
                        "UPDATE lecture_study_notes SET status='completed',document_json=?,error_code=NULL,error=NULL,"
                        "updated_at=?,completed_at=? WHERE job_id=? AND status='processing'",
                        (json.dumps(document, ensure_ascii=False, allow_nan=False), now, now, job["job_id"]),
                    )
        self._unsettled = None
        return True

    def start(self):
        with self.lock:
            if self.shutdown.is_set() or not self.configured or (self.thread is not None and self.thread.is_alive()):
                return
            self.thread = threading.Thread(target=self._run, name="lecture-study-note", daemon=True)
            try:
                self.thread.start()
            except Exception:
                self.thread = None
                raise

    def _run(self):
        while not self.shutdown.is_set():
            try:
                if self.process_next():
                    continue
            except Exception:
                pass  # Settle only the exact claimed job before any new work.
            self.wake.wait(1)
            self.wake.clear()

    def request_shutdown(self):
        self.shutdown.set()
        self.wake.set()

    def stop(self, timeout=5):
        self.request_shutdown()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=max(0, timeout))
        stopped = self.thread is None or not self.thread.is_alive()
        if stopped and hasattr(self.engine, "close"):
            self.engine.close()
        return stopped
