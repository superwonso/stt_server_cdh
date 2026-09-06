"""Local-only personal notes and edits; immutable raw/AI, append-only history."""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_REVISION = 2_147_483_647
MAX_TEXT = 5000
MAX_NOTES = 100
MAX_NOTE_CHARS = 100_000
MAX_EDITS = 1000
MAX_EDIT_CHARS = 250_000
MAX_HISTORY = 2000
MAX_HISTORY_CHARS = 2_000_000
MAX_SOURCE_SEGMENTS = 50_000
MAX_SOURCE_CHARS = 250_000
MAX_SOURCE_TEXT = 24_000
MAX_SECONDS = 14400


class ManualChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    revision: int = Field(strict=True, ge=0, le=MAX_REVISION)
    raw_revision: str = Field(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    action: Literal["note_upsert", "note_delete", "segment_edit"]
    note_id: uuid.UUID | None = None
    segment_id: str | None = Field(default=None, strict=True, min_length=1, max_length=128)
    start_seconds: float | None = Field(default=None, strict=True, ge=0, le=MAX_SECONDS, allow_inf_nan=False)
    text: str | None = Field(default=None, strict=True, max_length=MAX_TEXT)

    @field_validator("segment_id")
    @classmethod
    def valid_segment_id(cls, value):
        if value is not None and any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("invalid segment")
        return value

    @field_validator("text")
    @classmethod
    def valid_text(cls, value):
        if value is not None:
            if any((ord(character) < 32 and character not in "\n\r\t") or ord(character) == 127 for character in value):
                raise ValueError("invalid note text")
            value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
            if not value:
                raise ValueError("use null to restore the original text")
        return value

    @model_validator(mode="after")
    def appropriate_fields(self):
        if self.action == "segment_edit":
            if self.segment_id is None or self.note_id is not None or "text" not in self.model_fields_set or self.start_seconds is not None:
                raise ValueError("invalid segment edit")
        elif self.action == "note_upsert":
            if self.note_id is None or self.text is None:
                raise ValueError("invalid note")
        elif self.note_id is None or self.text is not None or self.segment_id is not None or self.start_seconds is not None:
            raise ValueError("invalid note deletion")
        return self


def _conflict():
    return HTTPException(409, "원문이나 내 필기가 변경되었습니다. 다시 불러온 뒤 수정해 주세요.")


def _limit(message):
    return HTTPException(413, message)


def _owned(connection, lecture_id, username):
    row = connection.execute(
        "SELECT id,recording_finalized FROM lectures WHERE id=? AND username=? "
        "AND deleting=0 AND trashed_at IS NULL", (lecture_id, username),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "수업을 찾을 수 없습니다.")
    if not row["recording_finalized"]:
        raise HTTPException(409, "수업 종료와 마지막 저장이 끝난 뒤 내 필기를 사용할 수 있습니다.")


def _snapshot(connection, lecture_id, raw_segments, transcript_revision):
    sizes = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(length(text)),0),COALESCE(MAX(length(text)),0),"
        "COALESCE(MAX(length(id)),0) FROM segments WHERE lecture_id=?", (lecture_id,),
    ).fetchone()
    if (sizes[0] > MAX_SOURCE_SEGMENTS or sizes[1] > MAX_SOURCE_CHARS
            or sizes[2] > MAX_SOURCE_TEXT or sizes[3] > 128):
        raise _limit("내 필기로 읽을 수 있는 원문 분량을 초과했습니다.")
    segments = raw_segments(connection, lecture_id)
    for row in segments:
        if (not isinstance(row.get("id"), str) or not isinstance(row.get("text"), str)
                or any(type(row.get(key)) not in (int, float) or not math.isfinite(row[key]) for key in ("start", "end"))
                or not 0 <= row["start"] <= row["end"] <= MAX_SECONDS):
            raise HTTPException(503, "수업 원문 구간을 안전하게 확인하지 못했습니다.")
    return segments, transcript_revision(segments)


def _state(connection, lecture_id, raw_revision):
    row = connection.execute("SELECT * FROM lecture_manual_state WHERE lecture_id=?", (lecture_id,)).fetchone()
    if row is not None and row["raw_revision"] != raw_revision:
        raise _conflict()
    return row["revision"] if row is not None else 0


def _request_hash(body):
    return hashlib.sha256(json.dumps(body.model_dump(mode="json", exclude_unset=True),
                                   sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _ack(row):
    return {"id": row["request_id"], "revision": row["revision"], "action": row["action"],
            "note_id": row["note_id"], "segment_id": row["segment_id"]}


def _history_item(row):
    return {key: row[key] for key in ("revision", "action", "note_id", "segment_id", "text", "start_seconds", "created_at")}


def install(app, settings, database, *, identity, owned_lecture, limiter, raw_segments, transcript_revision):
    max_seconds = min(MAX_SECONDS, settings.max_import_seconds)

    def allow(operation, username, maximum=120):
        if not limiter.allow((operation, username), maximum, 60):
            raise HTTPException(429, "내 필기 요청이 많습니다. 잠시 후 다시 시도하세요.", headers={"Retry-After": "60"})

    def owned_snapshot(connection, lecture_id, username):
        _owned(connection, lecture_id, username)
        return _snapshot(connection, lecture_id, raw_segments, transcript_revision)

    @app.get("/lectures/{lecture_id}/manual")
    def get_manual(lecture_id: str, user: dict = Depends(identity)):
        owned_lecture(lecture_id, user["username"])
        allow("manual-read", user["username"])
        with database.connect() as connection:
            connection.execute("BEGIN")
            segments, raw_revision = owned_snapshot(connection, lecture_id, user["username"])
            revision = _state(connection, lecture_id, raw_revision)
            sizes = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(text)),0) FROM lecture_manual_notes WHERE lecture_id=?", (lecture_id,),
            ).fetchone()
            edit_sizes = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(text)),0) FROM lecture_manual_edits WHERE lecture_id=?", (lecture_id,),
            ).fetchone()
            if sizes[0] > MAX_NOTES or sizes[1] > MAX_NOTE_CHARS or edit_sizes[0] > MAX_EDITS or edit_sizes[1] > MAX_EDIT_CHARS:
                raise _limit("저장된 내 필기 분량을 안전하게 읽을 수 있는 범위를 초과했습니다.")
            notes = [dict(row) for row in connection.execute(
                "SELECT id,segment_id,start_seconds,text,created_at,updated_at FROM lecture_manual_notes "
                "WHERE lecture_id=? ORDER BY start_seconds,created_at,id", (lecture_id,),
            )]
            edits = [dict(row) for row in connection.execute(
                "SELECT e.segment_id,e.text,e.created_at,e.updated_at FROM lecture_manual_edits e "
                "JOIN segments s ON s.id=e.segment_id WHERE e.lecture_id=? AND s.lecture_id=? "
                "ORDER BY s.start,s.end,s.id", (lecture_id, lecture_id),
            )]
            source_ids = {row["id"] for row in segments}
            if len(edits) != edit_sizes[0] or any(note["segment_id"] is not None and note["segment_id"] not in source_ids for note in notes):
                raise HTTPException(503, "내 필기의 원문 연결을 안전하게 확인하지 못했습니다.")
            return {"lecture_id": lecture_id, "raw_revision": raw_revision, "revision": revision, "notes": notes, "edits": edits}

    @app.post("/lectures/{lecture_id}/manual")
    def change_manual(lecture_id: str, body: ManualChange, user: dict = Depends(identity)):
        owned_lecture(lecture_id, user["username"])
        request_id, request_hash = str(body.id), _request_hash(body)
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _owned(connection, lecture_id, user["username"])
            existing = connection.execute(
                "SELECT * FROM lecture_manual_history WHERE lecture_id=? AND request_id=?", (lecture_id, request_id),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise HTTPException(409, "같은 변경 ID로 다른 내용을 저장할 수 없습니다.")
                return _ack(existing)
            allow("manual-write", user["username"], 60)
            segments, raw_revision = _snapshot(connection, lecture_id, raw_segments, transcript_revision)
            revision = _state(connection, lecture_id, raw_revision)
            if revision != body.revision or body.raw_revision != raw_revision or revision >= MAX_REVISION:
                raise _conflict()
            source = {row["id"]: row for row in segments}
            note_id, segment_id, start, text = str(body.note_id) if body.note_id else None, body.segment_id, None, body.text
            old = None
            if body.action.startswith("note_"):
                old = connection.execute("SELECT * FROM lecture_manual_notes WHERE id=? AND lecture_id=?", (note_id, lecture_id)).fetchone()
                if old is None:
                    # A known note UUID from another lecture must never be moved
                    # or rebound, including when a caller guesses another owner.
                    occupied = connection.execute(
                        "SELECT 1 FROM lecture_manual_history WHERE note_id=? AND lecture_id!=? LIMIT 1",
                        (note_id, lecture_id),
                    ).fetchone() or connection.execute("SELECT 1 FROM lecture_manual_notes WHERE id=?", (note_id,)).fetchone()
                    if occupied is not None or body.action == "note_delete":
                        raise HTTPException(404, "필기를 찾을 수 없습니다.")
                if body.action == "note_delete":
                    segment_id, start, text = old["segment_id"], old["start_seconds"], None
                else:
                    segment_id = body.segment_id if "segment_id" in body.model_fields_set else old["segment_id"] if old else None
                    if segment_id is not None:
                        if segment_id not in source:
                            raise HTTPException(404, "원문 구간을 찾을 수 없습니다.")
                        start = source[segment_id]["start"]
                        if body.start_seconds is not None and body.start_seconds != start:
                            raise HTTPException(422, "연결한 원문 구간의 시작 시각과 일치해야 합니다.")
                    else:
                        start = body.start_seconds if body.start_seconds is not None else old["start_seconds"] if old else 0.0
                    if start > max_seconds:
                        raise HTTPException(422, "필기 시각이 수업의 허용 길이를 초과했습니다.")
                    sizes = connection.execute("SELECT COUNT(*),COALESCE(SUM(length(text)),0) FROM lecture_manual_notes WHERE lecture_id=?", (lecture_id,)).fetchone()
                    if sizes[0] + int(old is None) > MAX_NOTES or sizes[1] - (len(old["text"]) if old else 0) + len(text) > MAX_NOTE_CHARS:
                        raise _limit("필기는 수업당 100개·총 10만 자까지 저장할 수 있습니다.")
            else:
                if segment_id not in source:
                    raise HTTPException(404, "원문 구간을 찾을 수 없습니다.")
                raw = source[segment_id]
                start = raw["start"]
                if text is not None and len(text) > min(MAX_TEXT, max(1000, len(raw["text"]) * 4 + 500)):
                    raise _limit("직접 정정한 문장이 원문 대비 허용 길이를 초과했습니다.")
                old = connection.execute("SELECT text FROM lecture_manual_edits WHERE lecture_id=? AND segment_id=?", (lecture_id, segment_id)).fetchone()
                sizes = connection.execute("SELECT COUNT(*),COALESCE(SUM(length(text)),0) FROM lecture_manual_edits WHERE lecture_id=?", (lecture_id,)).fetchone()
                next_count = sizes[0] + int(old is None and text is not None) - int(old is not None and text is None)
                if next_count > MAX_EDITS or sizes[1] - (len(old["text"]) if old else 0) + len(text or "") > MAX_EDIT_CHARS:
                    raise _limit("직접 정정은 수업당 1000개·총 25만 자까지 저장할 수 있습니다.")
            history_size = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(text)),0) FROM lecture_manual_history WHERE lecture_id=?", (lecture_id,),
            ).fetchone()
            if history_size[0] >= MAX_HISTORY or history_size[1] + len(text or "") > MAX_HISTORY_CHARS:
                raise HTTPException(409, "수업의 수정 이력 저장 한도(2000건·총 200만 자)에 도달했습니다. 기존 이력은 삭제하지 않습니다.")
            now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
            if body.action == "note_upsert":
                connection.execute(
                    "INSERT INTO lecture_manual_notes(id,lecture_id,segment_id,start_seconds,text,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET segment_id=excluded.segment_id,"
                    "start_seconds=excluded.start_seconds,text=excluded.text,updated_at=excluded.updated_at",
                    (note_id, lecture_id, segment_id, start, text, now, now),
                )
            elif body.action == "note_delete":
                connection.execute("DELETE FROM lecture_manual_notes WHERE id=? AND lecture_id=?", (note_id, lecture_id))
            elif text is None:
                connection.execute("DELETE FROM lecture_manual_edits WHERE lecture_id=? AND segment_id=?", (lecture_id, segment_id))
            else:
                connection.execute(
                    "INSERT INTO lecture_manual_edits(segment_id,lecture_id,text,created_at,updated_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(segment_id) DO UPDATE SET text=excluded.text,updated_at=excluded.updated_at",
                    (segment_id, lecture_id, text, now, now),
                )
            revision += 1
            connection.execute(
                "INSERT INTO lecture_manual_state(lecture_id,raw_revision,revision) VALUES(?,?,?) "
                "ON CONFLICT(lecture_id) DO UPDATE SET revision=excluded.revision", (lecture_id, raw_revision, revision),
            )
            connection.execute(
                "INSERT INTO lecture_manual_history(lecture_id,revision,request_id,request_hash,raw_revision,action,"
                "note_id,segment_id,start_seconds,text,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (lecture_id, revision, request_id, request_hash, raw_revision, body.action, note_id, segment_id, start, text, now),
            )
            return {"id": request_id, "revision": revision, "action": body.action, "note_id": note_id, "segment_id": segment_id}

    @app.get("/lectures/{lecture_id}/manual/history")
    def history(lecture_id: str, segment_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
                note_id: uuid.UUID | None = None,
                at_revision: Annotated[int | None, Query(ge=0, le=MAX_REVISION)] = None,
                offset: Annotated[int, Query(ge=0, le=MAX_HISTORY)] = 0,
                limit: Annotated[int, Query(ge=1, le=20)] = 20, user: dict = Depends(identity)):
        owned_lecture(lecture_id, user["username"])
        allow("manual-history", user["username"])
        if segment_id is not None and note_id is not None:
            raise HTTPException(422, "문장 또는 필기 하나의 이력을 선택하세요.")
        with database.connect() as connection:
            connection.execute("BEGIN")
            segments, raw_revision = owned_snapshot(connection, lecture_id, user["username"])
            revision = _state(connection, lecture_id, raw_revision)
            anchor = revision if at_revision is None else at_revision
            if anchor > revision:
                raise _conflict()
            if segment_id is not None and segment_id not in {row["id"] for row in segments}:
                raise HTTPException(404, "원문 구간을 찾을 수 없습니다.")
            if note_id is not None and connection.execute(
                "SELECT 1 FROM lecture_manual_history WHERE lecture_id=? AND note_id=? LIMIT 1", (lecture_id, str(note_id)),
            ).fetchone() is None:
                raise HTTPException(404, "필기를 찾을 수 없습니다.")
            rows = connection.execute(
                "SELECT revision,action,note_id,segment_id,text,start_seconds,created_at FROM lecture_manual_history "
                "WHERE lecture_id=? AND revision<=? AND (? IS NULL OR (segment_id=? AND action='segment_edit')) "
                "AND (? IS NULL OR note_id=?) ORDER BY revision DESC LIMIT ? OFFSET ?",
                (lecture_id, anchor, segment_id, segment_id, str(note_id) if note_id else None, str(note_id) if note_id else None, limit + 1, offset),
            ).fetchall()
            return {"lecture_id": lecture_id, "raw_revision": raw_revision, "revision": revision,
                    "items": [_history_item(row) for row in rows[:limit]], "offset": offset, "at_revision": anchor,
                    "limit": limit, "has_more": len(rows) > limit}
