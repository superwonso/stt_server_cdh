"""Owner-only lecture questions with at-most-one provider attempt per UUID.

The durable processing transition is deliberately irreversible: a lost answer,
shutdown, or failed save never silently queues another billable request.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from .postprocessor import PostprocessingError
from .question_answerer import QuestionAnsweringError, select_evidence, answer_result_document, validate_answer_result_document

HISTORY_LIMIT = 100
OWNER_QUEUE_LIMIT = 3
GLOBAL_QUEUE_LIMIT = 10
_ERRORS = {
    "not_configured": "운영자가 수업 질문 API를 설정해야 합니다.",
    "interrupted_unknown": "처리 도중 연결이 종료되어 결과를 확인하지 못했습니다. 자동으로 다시 요청하지 않습니다.",
    "question_failed": "질문 답변을 완료하지 못했습니다. 자동으로 다시 요청하지 않습니다.",
    "question_save_failed": "답변 결과를 저장하지 못했습니다. 자동으로 다시 요청하지 않습니다.",
    "source_changed": "원문 또는 수업 상태가 변경되어 답변을 저장하지 않았습니다.",
    "invalid_saved_answer": "저장된 답변과 원문 출처를 확인하지 못했습니다.",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


def _uuid(value):
    try:
        result = str(uuid.UUID(value))
        if result != value.lower():
            raise ValueError("noncanonical")
        return result
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "질문 요청 ID가 올바르지 않습니다.") from None


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=36, max_length=36)
    question: StrictStr = Field(min_length=1, max_length=1000)


class QuestionService:
    def __init__(self, settings, database, engine, limiter):
        self.settings, self.database, self.engine, self.limiter = settings, database, engine, limiter
        self.shutdown = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.thread = None
        # Only one local worker claims a job. Retain its ID until the terminal
        # transaction commits, so a failed save is cleaned up before new work.
        self._unsettled = None

    @property
    def configured(self):
        return bool(getattr(self.engine, "configured", False))

    @property
    def model(self):
        return getattr(self.engine, "model", self.settings.summary_model)

    @staticmethod
    def _lecture(connection, lecture_id, username):
        lecture = connection.execute(
            "SELECT * FROM lectures WHERE id=? AND username=? AND deleting=0 AND trashed_at IS NULL",
            (lecture_id, username),
        ).fetchone()
        if lecture is None:
            raise HTTPException(404, "수업을 찾을 수 없습니다.")
        return lecture

    @staticmethod
    def _access(connection):
        row = connection.execute("SELECT access_enabled FROM operational_state WHERE singleton=1").fetchone()
        return bool(row and row[0])

    def install(self, app, *, identity, owned_lecture, raw_segments, transcript_revision):
        self.raw_segments, self.revision = raw_segments, transcript_revision

        @app.get("/lectures/{lecture_id}/questions")
        def list_questions(lecture_id: str, offset: int = Query(0, ge=0, le=100),
                           limit: int = Query(20, ge=1, le=20), user: dict = Depends(identity)):
            owned_lecture(lecture_id, user["username"])
            with self.database.connect() as connection:
                # One snapshot covers ownership, source and the bounded page.
                connection.execute("BEGIN")
                lecture = self._lecture(connection, lecture_id, user["username"])
                total = connection.execute(
                    "SELECT COUNT(*) FROM lecture_questions WHERE lecture_id=? AND username=?",
                    (lecture_id, user["username"]),
                ).fetchone()[0]
                rows = connection.execute(
                    "SELECT * FROM lecture_questions WHERE lecture_id=? AND username=? "
                    "ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                    (lecture_id, user["username"], limit, offset),
                ).fetchall()
                raw = self.raw_segments(connection, lecture_id) if any(r["status"] == "completed" for r in rows) else None
                results = [self.result(row, connection, lecture=lecture, raw=raw) for row in rows]
            return {"configured": self.configured, "model": self.model, "questions": results,
                    "offset": offset, "limit": limit, "total": total,
                    "has_more": offset + len(results) < total, "history_limit": HISTORY_LIMIT}

        @app.get("/lectures/{lecture_id}/questions/{question_id}")
        def get_question(lecture_id: str, question_id: str, user: dict = Depends(identity)):
            owned_lecture(lecture_id, user["username"])
            question_id = _uuid(question_id)
            with self.database.connect() as connection:
                connection.execute("BEGIN")
                lecture = self._lecture(connection, lecture_id, user["username"])
                row = self._owned_job(connection, question_id, lecture_id, user["username"])
                result = self.result(row, connection, lecture=lecture)
            return {"question": result}

        @app.post("/lectures/{lecture_id}/questions", status_code=202)
        def create_question(lecture_id: str, request: QuestionRequest, user: dict = Depends(identity)):
            owned_lecture(lecture_id, user["username"])
            question_id = _uuid(request.id)
            question = request.question.strip()
            if not question or any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in question):
                raise HTTPException(422, "질문 내용을 확인해 주세요.")
            request_hash = _hash({"id": question_id, "lecture_id": lecture_id, "question": question})
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                lecture = self._lecture(connection, lecture_id, user["username"])
                if not self._access(connection):
                    raise HTTPException(503, "현재 수업 서비스가 일시 중지되었습니다.")
                existing = connection.execute("SELECT * FROM lecture_questions WHERE id=?", (question_id,)).fetchone()
                if existing is not None:
                    if existing["username"] != user["username"] or existing["lecture_id"] != lecture_id:
                        raise HTTPException(404, "질문을 찾을 수 없습니다.")
                    if existing["request_hash"] != request_hash or existing["question"] != question:
                        raise HTTPException(409, "같은 요청 ID에 다른 질문을 사용할 수 없습니다.")
                    return {"question": self.result(existing, connection, lecture=lecture)}
                if not self.configured:
                    raise HTTPException(503, _ERRORS["not_configured"])
                if not lecture["recording_finalized"]:
                    raise HTTPException(409, "수업 녹음과 받아쓰기 저장이 끝난 뒤 질문할 수 있습니다.")
                count = connection.execute("SELECT COUNT(*) FROM lecture_questions WHERE lecture_id=?", (lecture_id,)).fetchone()[0]
                if count >= HISTORY_LIMIT:
                    raise HTTPException(409, "이 수업의 질문 기록 한도에 도달했습니다. 기존 질문은 계속 볼 수 있습니다.")
                global_active, owner_active = connection.execute(
                    "SELECT COUNT(*),COALESCE(SUM(username=?),0) FROM lecture_questions "
                    "WHERE status IN ('queued','processing')", (user["username"],),
                ).fetchone()
                if global_active >= GLOBAL_QUEUE_LIMIT or owner_active >= OWNER_QUEUE_LIMIT:
                    raise HTTPException(429, "대기 중인 질문이 많습니다. 먼저 요청한 질문이 끝난 뒤 다시 시도하세요.")
                raw = self.raw_segments(connection, lecture_id)
                if not raw or not any(s["text"].strip() for s in raw):
                    raise HTTPException(409, "질문에 사용할 받아쓰기 내용이 없습니다.")
                if len(raw) > 50000 or any(len(s["text"]) > 24000 for s in raw) or sum(len(s["text"]) for s in raw) > 250000:
                    raise HTTPException(413, "질문에 사용할 원문 분량의 상한을 초과했습니다.")
                try:
                    evidence = select_evidence(question, raw)
                    selected = evidence["segments"]
                    self._validate_selection(evidence, raw)
                except Exception:
                    raise HTTPException(422, "질문에 사용할 원문 출처를 확인하지 못했습니다.") from None
                if not self.limiter.allow(("lecture-question", user["username"]), 20, 3600):
                    raise HTTPException(429, "질문 요청이 많습니다. 잠시 후 다시 시도하세요.")
                now = _now()
                connection.execute(
                    "INSERT INTO lecture_questions(id,lecture_id,username,question,request_hash,raw_revision,model,"
                    "selected_ids_json,evidence_sha256,scope,total_segments,selected_count,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)",
                    (question_id, lecture_id, user["username"], question, request_hash, self.revision(raw), self.model,
                     _encode([s["id"] for s in selected]), _hash(selected), evidence["scope"],
                     evidence["total_segments"], len(selected), now, now),
                )
                row = self._owned_job(connection, question_id, lecture_id, user["username"])
                result = self.result(row, connection, lecture=lecture)
            self.start()
            self.wake.set()
            return {"question": result}

        @app.delete("/lectures/{lecture_id}/questions/{question_id}")
        def cancel_question(lecture_id: str, question_id: str, user: dict = Depends(identity)):
            owned_lecture(lecture_id, user["username"])
            question_id = _uuid(question_id)
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                lecture = self._lecture(connection, lecture_id, user["username"])
                row = self._owned_job(connection, question_id, lecture_id, user["username"])
                if row["status"] in ("queued", "processing"):
                    connection.execute(
                        "UPDATE lecture_questions SET cancel_requested=1,status=CASE WHEN status='queued' "
                        "THEN 'cancelled' ELSE status END,updated_at=? WHERE id=?", (_now(), question_id),
                    )
                    row = self._owned_job(connection, question_id, lecture_id, user["username"])
                result = self.result(row, connection, lecture=lecture)
            self.wake.set()
            return {"question": result}

    @staticmethod
    def _owned_job(connection, question_id, lecture_id, username):
        row = connection.execute("SELECT * FROM lecture_questions WHERE id=? AND lecture_id=? AND username=?",
                                 (question_id, lecture_id, username)).fetchone()
        if row is None:
            raise HTTPException(404, "질문을 찾을 수 없습니다.")
        return row

    @staticmethod
    def _validate_selection(evidence, raw):
        selected = evidence["segments"]
        if (evidence["scope"] not in ("full", "retrieved", "none") or evidence["total_segments"] != len(raw)
                or not isinstance(selected, list) or len(selected) > 128
                or sum(len(s["text"]) for s in selected) > 24000):
            raise ValueError("invalid selection")
        indices = {s["id"]: i for i, s in enumerate(raw)}
        positions = [indices[s["id"]] for s in selected]
        if positions != sorted(set(positions)) or any(s != raw[i] for s, i in zip(selected, positions)):
            raise ValueError("invalid selection")
        if ((evidence["scope"] == "none") != (not selected)
                or (evidence["scope"] == "full" and selected != raw)):
            raise ValueError("invalid scope")

    def _evidence(self, row, raw):
        if len(raw) > 50000 or sum(len(s["text"]) for s in raw) > 250000 or self.revision(raw) != row["raw_revision"]:
            raise ValueError("source changed")
        ids = json.loads(row["selected_ids_json"])
        if not isinstance(ids, list) or len(ids) > 128 or len(ids) != row["selected_count"]:
            raise ValueError("invalid IDs")
        by_id = {s["id"]: s for s in raw}
        selected = [by_id[identifier] for identifier in ids]
        evidence = {"segments": selected, "scope": row["scope"], "total_segments": row["total_segments"]}
        self._validate_selection(evidence, raw)
        if _hash(selected) != row["evidence_sha256"]:
            raise ValueError("invalid evidence seal")
        return selected

    def result(self, row, connection, *, lecture=None, raw=None):
        result = {key: row[key] for key in (
            "id", "lecture_id", "question", "status", "model", "scope", "total_segments", "selected_count",
            "created_at", "updated_at", "completed_at", "error_code", "error",
        )}
        result["cancel_requested"] = bool(row["cancel_requested"])
        result["document"] = None
        if row["status"] == "completed":
            try:
                lecture = lecture if lecture is not None else self._lecture(connection, row["lecture_id"], row["username"])
                if not lecture["recording_finalized"] or lecture["username"] != row["username"]:
                    raise ValueError("inactive source")
                raw = raw if raw is not None else self.raw_segments(connection, row["lecture_id"])
                selected = self._evidence(row, raw)
                document = json.loads(row["document_json"])
                result["document"] = validate_answer_result_document(document, selected)
            except Exception:
                result.update(status="failed", error_code="invalid_saved_answer", error=_ERRORS["invalid_saved_answer"])
        return result

    def _terminal(self, connection, identifier, code, *, cancelled=False):
        if code in _ERRORS:
            message = _ERRORS[code]
        else:
            # Known provider codes have fixed feature-specific messages. Never
            # persist exception strings or a caller-provided unknown code.
            safe_error = QuestionAnsweringError(code)
            code, message = safe_error.code, str(safe_error)
        connection.execute(
            "UPDATE lecture_questions SET status=?,document_json=NULL,completed_at=NULL,error_code=?,error=?,updated_at=? "
            "WHERE id=? AND status IN ('queued','processing')",
            ("cancelled" if cancelled else "failed", None if cancelled else code,
             None if cancelled else message, _now(), identifier),
        )

    def recover(self):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT id,status,cancel_requested,attempts FROM lecture_questions WHERE status IN ('queued','processing')").fetchall()
            for row in rows:
                if row["cancel_requested"]:
                    self._terminal(connection, row["id"], "interrupted_unknown", cancelled=True)
                elif row["status"] == "processing" or row["attempts"]:
                    self._terminal(connection, row["id"], "interrupted_unknown")
                elif not self.configured:
                    self._terminal(connection, row["id"], "not_configured")
            # Do not revive jobs from a trash/deletion or an ownership change.
            connection.execute(
                "UPDATE lecture_questions SET status='failed',error_code='source_changed',error=?,updated_at=? "
                "WHERE status='queued' AND NOT EXISTS (SELECT 1 FROM lectures l WHERE l.id=lecture_id "
                "AND l.username=lecture_questions.username AND l.deleting=0 AND l.trashed_at IS NULL AND l.recording_finalized=1)",
                (_ERRORS["source_changed"], _now()),
            )

    def _interrupted(self, job):
        if self.shutdown.is_set():
            return True
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT q.cancel_requested FROM lecture_questions q JOIN lectures l ON l.id=q.lecture_id "
                "WHERE q.id=? AND q.status='processing' AND q.username=? AND l.username=q.username "
                "AND q.lecture_id=? AND q.request_hash=? "
                "AND l.deleting=0 AND l.trashed_at IS NULL AND l.recording_finalized=1",
                (job["id"], job["username"], job["lecture_id"], job["request_hash"]),
            ).fetchone()
            return row is None or bool(row[0]) or not self._access(connection)

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
                previous = self._unsettled
                row = connection.execute(
                    "SELECT id FROM lecture_questions WHERE id=? AND username=? AND lecture_id=? AND request_hash=?",
                    (previous["id"], previous["username"], previous["lecture_id"], previous["request_hash"]),
                ).fetchone()
                if row is not None:
                    self._terminal(connection, row["id"], "question_save_failed")
            self._unsettled = None
        if self.shutdown.is_set() or not self.configured:
            return False
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._access(connection):
                return False
            row = connection.execute(
                "SELECT q.* FROM lecture_questions q JOIN lectures l ON l.id=q.lecture_id "
                "WHERE q.status='queued' AND q.attempts=0 AND l.username=q.username AND l.deleting=0 "
                "AND l.trashed_at IS NULL AND l.recording_finalized=1 AND NOT EXISTS "
                "(SELECT 1 FROM lecture_questions p WHERE p.username=q.username AND p.status='processing') "
                "ORDER BY q.created_at,q.id LIMIT 1"
            ).fetchone()
            if row is None:
                return False
            job = dict(row)
            try:
                selected = self._evidence(job, self.raw_segments(connection, job["lecture_id"]))
                if job["model"] != self.model:
                    raise ValueError("model changed")
            except Exception:
                self._terminal(connection, job["id"], "source_changed")
                return True
            if job["cancel_requested"]:
                self._terminal(connection, job["id"], "interrupted_unknown", cancelled=True)
                return True
            connection.execute("UPDATE lecture_questions SET status='processing',attempts=1,updated_at=? WHERE id=?",
                               (_now(), job["id"]))
            self._unsettled = job
        document, failure_code = None, "question_failed"
        try:
            if not self._interrupted(job):
                output = self.engine.answer(job["question"], copy.deepcopy(selected), lambda: self._interrupted(job))
                document = answer_result_document(output, selected)
        except PostprocessingError as error:
            # The code is normalized now and its message reconstructed at the
            # terminal write. This never adds a retry of the paid request.
            failure_code = QuestionAnsweringError(error.code).code
            document = None
        except Exception:
            # Never retain raw provider errors, bodies, keys or question text.
            document = None
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM lecture_questions WHERE id=? AND status='processing' AND username=? "
                "AND lecture_id=? AND request_hash=?",
                (job["id"], job["username"], job["lecture_id"], job["request_hash"]),
            ).fetchone()
            if current is not None:
                lecture = connection.execute(
                    "SELECT * FROM lectures WHERE id=? AND username=? AND deleting=0 AND trashed_at IS NULL AND recording_finalized=1",
                    (job["lecture_id"], job["username"]),
                ).fetchone()
                if current["cancel_requested"]:
                    self._terminal(connection, job["id"], "interrupted_unknown", cancelled=True)
                elif self.shutdown.is_set() or not self._access(connection):
                    self._terminal(connection, job["id"], "interrupted_unknown")
                else:
                    try:
                        if lecture is None or any(current[key] != job[key] for key in (
                            "lecture_id", "question", "request_hash", "raw_revision", "model", "selected_ids_json", "evidence_sha256"
                        )):
                            raise ValueError("job changed")
                        self._evidence(current, self.raw_segments(connection, job["lecture_id"]))
                    except Exception:
                        self._terminal(connection, job["id"], "source_changed")
                    else:
                        if document is None:
                            self._terminal(connection, job["id"], failure_code)
                        else:
                            now = _now()
                            connection.execute(
                                "UPDATE lecture_questions SET status='completed',document_json=?,error_code=NULL,error=NULL,"
                                "updated_at=?,completed_at=? WHERE id=? AND status='processing'",
                                (_encode(document), now, now, job["id"]),
                            )
        self._unsettled = None
        return True

    def start(self):
        with self.lock:
            if self.shutdown.is_set() or not self.configured or (self.thread is not None and self.thread.is_alive()):
                return
            self.thread = threading.Thread(target=self._run, name="lecture-question", daemon=True)
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
                pass  # Next iteration settles the claimed UUID, never resends it.
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
