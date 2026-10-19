"""Durable live-ASR deadlines; raw audio and file imports are separate lanes.

The database is the commit fence. Cancellation is cooperative and never releases
the model's GPU lock or kills a shared process. No transcript is stored here.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from fastapi import HTTPException

log = logging.getLogger("classroom")
TIMEOUT_SECONDS = 180


def migrate(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS recording_controls (
            lecture_id TEXT PRIMARY KEY REFERENCES lectures(id) ON DELETE CASCADE,
            capture_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('active','paused','stopped','finished')),
            revision INTEGER NOT NULL DEFAULT 1,
            started_at REAL NOT NULL,
            last_text_at REAL,
            deadline_at REAL,
            remaining_seconds REAL NOT NULL DEFAULT 180,
            reason TEXT CHECK(reason IS NULL OR reason IN ('manual','no_text_timeout')),
            stopped_at REAL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS recording_controls_deadline ON recording_controls(state, deadline_at)")


@dataclass(eq=False)
class RecordingRequest:
    lecture_id: str
    username: str
    capture_id: str | None
    cancel_event: threading.Event
    started_at: float


class RecordingControl:
    def __init__(self, database, close_clova, *, clock=time.time):
        self.database = database
        self.close_clova = close_clova
        self.clock = clock
        self._lock = threading.Lock()
        self._requests: set[RecordingRequest] = set()
        self._shutdown = threading.Event()
        self._thread = None

    def start(self):
        self.expire_due()
        if self._thread is None or not self._thread.is_alive():
            self._shutdown.clear()
            worker = threading.Thread(target=self._run, name="recording-watchdog", daemon=True)
            try:
                worker.start()
            except BaseException:
                self._shutdown.set()
                self._thread = None
                raise
            self._thread = worker

    def request_shutdown(self):
        self._shutdown.set()

    def stop(self, timeout=1.0):
        self.request_shutdown()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        return self._thread is None or not self._thread.is_alive()

    def _run(self):
        while not self._shutdown.wait(1):
            try:
                self.expire_due()
            except Exception:
                # No account, text, filenames, or upstream exception bodies.
                log.warning("Recording watchdog check could not complete")

    @staticmethod
    def _owned(connection, lecture_id, username):
        lecture = connection.execute(
            "SELECT id, recording_finalized, asr_provider FROM lectures "
            "WHERE id=? AND username=? AND deleting=0 AND trashed_at IS NULL",
            (lecture_id, username),
        ).fetchone()
        if lecture is None:
            raise HTTPException(404, "수업을 찾을 수 없습니다.")
        return lecture

    @staticmethod
    def _row(connection, lecture_id):
        row = connection.execute("SELECT * FROM recording_controls WHERE lecture_id=?", (lecture_id,)).fetchone()
        return None if row is None else dict(row)

    def _public(self, row):
        keys = ("capture_id", "revision", "started_at", "last_text_at", "deadline_at", "reason")
        return {"state": "unregistered" if row is None else row["state"],
                **{key: None if row is None else row[key] for key in keys},
                "revision": 0 if row is None else row["revision"],
                "deadline_at": None if row is None or row["state"] == "paused" else row["deadline_at"],
                "server_now": self.clock()}

    def _reject(self, row, code="recording_stopped"):
        message = ("중단된 받아쓰기입니다. 원본을 보관했으며 파일 가져오기 또는 새 수업에서 다시 처리하세요."
                   if code == "recording_stopped" else "녹음 세션이 바뀌었습니다. 현재 녹음 상태를 다시 확인하세요.")
        return HTTPException(409, {"code": code, "message": message,
                                   "recording_control": self._public(row)},
                             headers={"Cache-Control": "no-store", "X-Local-Model-Retryable": "0"})

    def status(self, lecture_id, username):
        # Verify ownership before running any account-specific observation.
        with self.database.connect() as connection:
            self._owned(connection, lecture_id, username)
        self.expire_due()
        with self.database.connect() as connection:
            self._owned(connection, lecture_id, username)
            return self._public(self._row(connection, lecture_id))

    def command(self, lecture_id, username, capture_id, action, reason="manual"):
        self.expire_due()
        try:
            return self._command(lecture_id, username, capture_id, action, reason)
        finally:
            self.expire_due()

    def _command(self, lecture_id, username, capture_id, action, reason):
        cancelled = False
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lecture = self._owned(connection, lecture_id, username)
            if connection.execute("SELECT 1 FROM imports WHERE lecture_id=? LIMIT 1", (lecture_id,)).fetchone():
                raise HTTPException(409, "파일 변환 작업은 녹음 제어로 변경할 수 없습니다.")
            row = self._row(connection, lecture_id)
            now = self.clock()
            if row is not None and row["capture_id"] != capture_id:
                raise self._reject(row, "recording_capture_mismatch")
            if row is not None and row["state"] in {"active", "paused"}:
                self.check(connection, lecture_id, capture_id)
            if row is None:
                if action not in {"start", "stop"}:
                    raise HTTPException(409, "먼저 녹음 세션을 등록하세요.")
                if lecture["recording_finalized"]:
                    raise HTTPException(409, "이미 마친 수업에는 새 녹음을 시작할 수 없습니다.")
                state = "stopped" if action == "stop" else "active"
                connection.execute(
                    "INSERT INTO recording_controls(lecture_id,capture_id,state,started_at,deadline_at,reason,stopped_at) "
                    "VALUES(?,?,?,?,?,?,?)", (lecture_id, capture_id, state, now,
                                              None if state == "stopped" else now + TIMEOUT_SECONDS,
                                              reason if state == "stopped" else None,
                                              now if state == "stopped" else None))
                cancelled = state == "stopped"
            elif row["state"] in {"stopped", "finished"}:
                if action not in {"stop", "finish"} and not (row["state"] == "finished" and action == "start"):
                    raise self._reject(row)
                cancelled = row["state"] == "stopped" and action == "stop"
            elif action == "stop":
                connection.execute("UPDATE recording_controls SET state='stopped',reason=?,stopped_at=?,"
                                   "deadline_at=NULL,revision=revision+1 WHERE lecture_id=?", (reason, now, lecture_id))
                cancelled = True
            elif action == "pause" and row["state"] == "active":
                connection.execute("UPDATE recording_controls SET state='paused',remaining_seconds=?,"
                                   "revision=revision+1 WHERE lecture_id=?",
                                   (max(0, row["deadline_at"] - now), lecture_id))
            elif action == "resume" and row["state"] == "paused":
                connection.execute("UPDATE recording_controls SET state='active',deadline_at=?,"
                                   "revision=revision+1 WHERE lecture_id=?", (now + row["remaining_seconds"], lecture_id))
            elif action == "finish":
                if not lecture["recording_finalized"] or connection.execute(
                        "SELECT 1 FROM chunks WHERE lecture_id=? AND status='pending' LIMIT 1", (lecture_id,)).fetchone():
                    raise HTTPException(409, "마지막 음성 처리가 끝난 뒤 녹음을 마칠 수 있습니다.")
                self.mark_finished(connection, lecture_id)
            row = self._row(connection, lecture_id)
        if cancelled:
            self._cancel(lecture_id, capture_id, username, lecture["asr_provider"])
        return self._public(row)

    def expire_due(self):
        now = self.clock()
        with self._lock:
            active_requests = {(request.lecture_id, request.capture_id)
                               for request in self._requests if request.capture_id is not None}
            overdue_requests = {(request.lecture_id, request.capture_id)
                                for request in self._requests
                                if request.capture_id is not None and now - request.started_at >= TIMEOUT_SECONDS}
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = list(connection.execute(
                "SELECT r.lecture_id,r.capture_id,l.username,l.asr_provider FROM recording_controls r "
                "JOIN lectures l ON l.id=r.lecture_id "
                "WHERE r.state='active' AND r.deadline_at<=?", (now,),
            ).fetchall())
            connection.execute("UPDATE recording_controls SET state='stopped',reason='no_text_timeout',"
                               "stopped_at=?,deadline_at=NULL,revision=revision+1 "
                               "WHERE state='active' AND deadline_at<=?", (now, now))
            # Pausing exempts microphone-idle time, not unresolved provider
            # work. Keep the old text deadline across short failed retries.
            for lecture_id, capture_id in active_requests:
                row = connection.execute(
                    "SELECT r.*,l.username,l.asr_provider FROM recording_controls r "
                    "JOIN lectures l ON l.id=r.lecture_id WHERE r.lecture_id=? AND r.capture_id=? "
                    "AND r.state IN ('active','paused')", (lecture_id, capture_id),
                ).fetchone()
                if row is not None and ((lecture_id, capture_id) in overdue_requests
                        or (row["state"] == "paused" and self._processing_deadline(row) <= now)):
                    connection.execute("UPDATE recording_controls SET state='stopped',reason='no_text_timeout',"
                                       "stopped_at=?,deadline_at=NULL,revision=revision+1 WHERE lecture_id=?",
                                       (now, lecture_id))
                    expired.append(row)
        for row in expired:
            self._cancel(row["lecture_id"], row["capture_id"], row["username"], row["asr_provider"])
        return len(expired)

    def _cancel(self, lecture_id, capture_id, username, provider):
        with self._lock:
            for request in self._requests:
                if (request.lecture_id == lecture_id and request.username == username
                        and request.capture_id == capture_id):
                    request.cancel_event.set()
        if provider != "clova":
            return
        try:
            self.close_clova(username, lecture_id)
        except Exception:
            log.warning("Recording provider cancellation could not be confirmed")

    def check(self, connection, lecture_id, capture_id, *, allow_finished=False):
        row = self._row(connection, lecture_id)
        if row is None:
            if capture_id is not None:
                raise self._reject(row, "recording_capture_mismatch")
            return
        if row["capture_id"] != capture_id:
            raise self._reject(row, "recording_capture_mismatch")
        if row["state"] == "stopped" or (row["state"] == "finished" and not allow_finished):
            raise self._reject(row)
        if row["state"] == "active" and row["deadline_at"] <= self.clock():
            # This transaction rolls back. expire_due() will persist the
            # tombstone after it unwinds; no late text may renew the deadline.
            raise self._reject({**row, "state": "stopped", "reason": "no_text_timeout", "deadline_at": None})
        if row["state"] in {"active", "paused"}:
            now = self.clock()
            with self._lock:
                requests = [request for request in self._requests if request.lecture_id == lecture_id
                            and request.capture_id == capture_id and request.capture_id is not None]
            if (any(now - request.started_at >= TIMEOUT_SECONDS for request in requests)
                    or (requests and row["state"] == "paused" and self._processing_deadline(row) <= now)):
                raise self._reject({**row, "state": "stopped", "reason": "no_text_timeout", "deadline_at": None})

    @staticmethod
    def _processing_deadline(row):
        return (row["deadline_at"] if row["deadline_at"] is not None
                else (row["last_text_at"] or row["started_at"]) + TIMEOUT_SECONDS)

    @contextmanager
    def request(self, lecture_id, username, capture_id):
        self.expire_due()
        request = RecordingRequest(lecture_id, username, capture_id, threading.Event(), self.clock())
        with self._lock:
            self._requests.add(request)
        try:
            with self.database.connect() as connection:
                self._owned(connection, lecture_id, username)
                self.check(connection, lecture_id, capture_id, allow_finished=True)
            yield request
        finally:
            try:
                # Keep admission visible while persisting an elapsed paused
                # request deadline, including a late result fenced at commit.
                self.expire_due()
            finally:
                with self._lock:
                    self._requests.discard(request)

    def check_current(self, lecture_id, capture_id):
        self.expire_due()
        with self.database.connect() as connection:
            self.check(connection, lecture_id, capture_id, allow_finished=True)

    def raise_cancelled(self, lecture_id, capture_id):
        self.check_current(lecture_id, capture_id)
        raise HTTPException(409, {"code": "transcription_cancelled", "message": "이 음성의 처리를 중단했습니다."},
                            headers={"X-Local-Model-Retryable": "0"})

    def text_committed(self, connection, lecture_id, segments):
        row = self._row(connection, lecture_id)
        if row is not None:
            # Raw-file fsync may have taken time since the initial transaction
            # fence. Do not extend a deadline that elapsed during that work.
            self.check(connection, lecture_id, row["capture_id"])
        if not any(segment.get("text", "").strip() for segment in segments):
            return
        now = self.clock()
        connection.execute("UPDATE recording_controls SET last_text_at=?,remaining_seconds=?,"
                           "deadline_at=?,revision=revision+1 "
                           "WHERE lecture_id=? AND state IN ('active','paused')",
                           (now, TIMEOUT_SECONDS, now + TIMEOUT_SECONDS, lecture_id))

    @staticmethod
    def mark_finished(connection, lecture_id):
        connection.execute("UPDATE recording_controls SET state='finished',deadline_at=NULL,revision=revision+1 "
                           "WHERE lecture_id=? AND state IN ('active','paused')", (lecture_id,))
