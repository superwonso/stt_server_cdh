"""Owner-scoped lecture organization and bounded, read-only transcript search.

Creation titles and ASR text remain immutable here. Metadata edits use a
separate revision; search never invokes a model or modifies saved AI results.
"""
from __future__ import annotations

import heapq
import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_REVISION = 2_147_483_647
MAX_CORRECTION_JSON = 8 * 1024 * 1024
MAX_SOURCE_SEGMENTS = 50_000
MAX_SOURCE_CHARS = 250_000
MAX_SEGMENT_CHARS = 24_000
SEARCH_SECONDS = 2.0
MAX_OPTIONS = 500
SNIPPET_CHARS = 240


def _clean(value: str, maximum: int, *, empty: bool = True) -> str:
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError("invalid library text")
    value = value.strip()
    if not empty and not value:
        raise ValueError("empty library text")
    return value


class MetadataPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(strict=True, ge=0, le=MAX_REVISION)
    display_title: str | None = None
    course: str | None = None
    semester: str | None = None

    @field_validator("display_title", "course", "semester", mode="before")
    @classmethod
    def validate_text(cls, value, info):
        return _clean(value, {"display_title": 120, "course": 80, "semester": 40}[info.field_name],
                      empty=info.field_name != "display_title")

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set - {"revision"}:
            raise ValueError("no metadata fields supplied")
        return self


def _metadata(lecture, row=None) -> dict:
    return {
        "lecture_id": lecture["id"],
        "display_title": row["display_title"] if row is not None and row["display_title"] is not None else lecture["title"],
        "course": row["course"] if row is not None else "",
        "semester": row["semester"] if row is not None else "",
        "revision": row["revision"] if row is not None else 0,
    }


def metadata_for(connection, lecture_row) -> dict:
    """Projection helper for an already owner-authorized lecture, not an auth API."""
    row = connection.execute(
        "SELECT m.* FROM lecture_metadata m JOIN lectures l ON l.id=m.lecture_id "
        "WHERE l.id=? AND l.username=? AND l.deleting=0 AND l.trashed_at IS NULL",
        (lecture_row["id"], lecture_row["username"]),
    ).fetchone()
    metadata = _metadata(lecture_row, row)
    return {key: metadata[key] for key in ("display_title", "course", "semester")} | {
        "metadata_revision": metadata["revision"],
    }


def _owned(connection, lecture_id, username):
    row = connection.execute(
        "SELECT id,username,title,created_at,recording_finalized FROM lectures "
        "WHERE id=? AND username=? AND deleting=0 AND trashed_at IS NULL", (lecture_id, username),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "수업을 찾을 수 없습니다.")
    return row


def _pattern(query: str) -> str:
    return "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _fold(value: str) -> str:
    # Match SQLite LIKE's ASCII case insensitivity, without changing Korean
    # text or turning SQL wildcard characters into search operators.
    return value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def _snippet(text: str, query: str) -> str:
    position = _fold(text).find(_fold(query))
    first = max(0, position - 60)
    last = first + SNIPPET_CHARS - 2
    result = ("…" if first else "") + text[first:last] + ("…" if last < len(text) else "")
    return " ".join(result.split())[:SNIPPET_CHARS]


def _too_broad():
    return HTTPException(503, "검색 범위가 넓어 시간이 초과되었습니다. 과목·학기나 검색어로 범위를 좁혀 주세요.",
                         headers={"Retry-After": "2"})


@contextmanager
def _search_budget(connection):
    deadline = time.monotonic() + SEARCH_SECONDS

    def expired():
        return time.monotonic() >= deadline

    def check():
        if expired():
            raise _too_broad()

    connection.set_progress_handler(lambda: int(expired()), 1000)
    try:
        yield check
    except sqlite3.OperationalError as error:
        if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            raise _too_broad() from None
        raise
    finally:
        connection.set_progress_handler(None, 0)


def _corrected_segments(connection, lecture, raw_segments, transcript_revision, check, query):
    """Validate one bounded completed correction without touching its source."""
    row = connection.execute(
        "SELECT raw_revision, "
        "CASE WHEN length(CAST(corrected_segments AS BLOB))<=? THEN corrected_segments ELSE NULL END AS payload "
        "FROM transcript_corrections WHERE lecture_id=? AND status='completed' "
        "AND corrected_text LIKE ? ESCAPE '\\'",
        (MAX_CORRECTION_JSON, lecture["id"], _pattern(query)),
    ).fetchone()
    if row is None:
        return [], False
    if not lecture["recording_finalized"] or row["payload"] is None:
        return [], True
    sizes = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(length(text)),0),COALESCE(MAX(length(text)),0) "
        "FROM segments WHERE lecture_id=?", (lecture["id"],),
    ).fetchone()
    if not 0 < sizes[0] <= MAX_SOURCE_SEGMENTS or sizes[1] > MAX_SOURCE_CHARS or sizes[2] > MAX_SEGMENT_CHARS:
        return [], True
    check()
    try:
        document = json.loads(row["payload"])
        if not isinstance(document, list) or len(document) != sizes[0]:
            return [], True
        source = raw_segments(connection, lecture["id"])
        if transcript_revision(source) != row["raw_revision"]:
            return [], True
        for index, (raw, item) in enumerate(zip(source, document, strict=True)):
            if index % 256 == 0:
                check()
            if (not isinstance(item, dict) or set(item) != {"id", "start", "end", "text"}
                    or item["id"] != raw["id"] or not isinstance(item["text"], str) or not item["text"].strip()
                    or len(item["text"]) > max(1000, len(raw["text"]) * 4 + 500)):
                return [], True
            for key in ("start", "end"):
                value = item[key]
                if type(value) not in (int, float) or not math.isfinite(value) or value != raw[key]:
                    return [], True
        return document, False
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return [], True


def install(app, settings, database, *, identity, owned_lecture, limiter, raw_segments, transcript_revision):
    del settings  # No environment, model or credential access in this service.
    search_capacity = threading.BoundedSemaphore(2)

    def allow(operation, username, maximum):
        if not limiter.allow((operation, username), maximum, 60):
            raise HTTPException(429, "요청이 많습니다. 잠시 후 다시 시도하세요.", headers={"Retry-After": "60"})

    @app.get("/lectures/{lecture_id}/metadata")
    def get_metadata(lecture_id: str, user: dict = Depends(identity)):
        owned_lecture(lecture_id, user["username"])
        allow("library-metadata-read", user["username"], 120)
        with database.connect() as connection:
            connection.execute("BEGIN")
            lecture = _owned(connection, lecture_id, user["username"])
            row = connection.execute("SELECT * FROM lecture_metadata WHERE lecture_id=?", (lecture_id,)).fetchone()
            return _metadata(lecture, row)

    @app.patch("/lectures/{lecture_id}/metadata")
    def patch_metadata(lecture_id: str, body: MetadataPatch, user: dict = Depends(identity)):
        owned_lecture(lecture_id, user["username"])
        allow("library-metadata-write", user["username"], 60)
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lecture = _owned(connection, lecture_id, user["username"])
            if not lecture["recording_finalized"]:
                raise HTTPException(409, "수업 종료와 마지막 저장이 끝난 뒤 이름과 분류를 바꿀 수 있습니다.")
            row = connection.execute("SELECT * FROM lecture_metadata WHERE lecture_id=?", (lecture_id,)).fetchone()
            current = _metadata(lecture, row)
            if current["revision"] != body.revision or current["revision"] >= MAX_REVISION:
                raise HTTPException(409, "다른 화면에서 수업 정보가 변경되었습니다. 다시 불러온 뒤 수정해 주세요.")
            values = {**current, **body.model_dump(exclude_unset=True)}
            values["revision"] = current["revision"] + 1
            connection.execute(
                "INSERT INTO lecture_metadata(lecture_id,display_title,course,semester,revision,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(lecture_id) DO UPDATE SET "
                "display_title=excluded.display_title,course=excluded.course,semester=excluded.semester,"
                "revision=excluded.revision,updated_at=excluded.updated_at",
                (lecture_id, values["display_title"] if values["display_title"] != lecture["title"] else None,
                 values["course"], values["semester"], values["revision"], datetime.now(UTC).isoformat()),
            )
            return values

    @app.get("/library/options")
    def options(user: dict = Depends(identity)):
        allow("library-options", user["username"], 60)
        with database.connect() as connection, _search_budget(connection):
            connection.execute("BEGIN")
            result = {}
            for column, plural in (("course", "courses"), ("semester", "semesters")):
                rows = connection.execute(
                    f"SELECT DISTINCT m.{column} FROM lecture_metadata m JOIN lectures l ON l.id=m.lecture_id "
                    f"WHERE l.username=? AND l.deleting=0 AND l.trashed_at IS NULL "
                    f"AND m.{column}!='' ORDER BY m.{column} LIMIT ?",
                    (user["username"], MAX_OPTIONS + 1),
                ).fetchall()
                if len(rows) > MAX_OPTIONS:
                    raise HTTPException(503, "수업 분류가 너무 많습니다. 사용하지 않는 분류를 정리해 주세요.")
                result[plural] = [row[0] for row in rows]
            return result

    @app.get("/library/search")
    def search(q: Annotated[str, Query(max_length=120)] = "",
               course: Annotated[str, Query(max_length=80)] = "",
               semester: Annotated[str, Query(max_length=40)] = "",
               source: Literal["all", "raw", "corrected"] = "all",
               offset: Annotated[int, Query(ge=0, le=10000)] = 0,
               limit: Annotated[int, Query(ge=1, le=50)] = 20,
               user: dict = Depends(identity)):
        try:
            q, course, semester = _clean(q, 120), _clean(course, 80), _clean(semester, 40)
        except ValueError:
            raise HTTPException(422, "검색어와 분류에 올바른 문자를 입력하세요.") from None
        allow("library-search", user["username"], 60)
        if not search_capacity.acquire(blocking=False):
            raise HTTPException(429, "다른 수업 검색을 처리하고 있습니다. 잠시 후 다시 시도하세요.",
                                headers={"Retry-After": "2"})
        try:
            with database.connect() as connection, _search_budget(connection) as check:
                connection.execute("BEGIN")
                rows = connection.execute(
                    "SELECT l.id,l.title,l.created_at,l.recording_finalized,m.display_title,"
                    "COALESCE(m.course,'') AS course,COALESCE(m.semester,'') AS semester "
                    "FROM lectures l LEFT JOIN lecture_metadata m ON m.lecture_id=l.id "
                    "WHERE l.username=? AND l.deleting=0 AND l.trashed_at IS NULL "
                    "AND (?='' OR COALESCE(m.course,'')=?) "
                    "AND (?='' OR COALESCE(m.semester,'')=?) ORDER BY l.created_at DESC,l.id DESC",
                    (user["username"], course, course, semester, semester),
                )
                items, skipped, partial_corrected = [], 0, False
                pattern = _pattern(q)

                def take(lecture, match):
                    nonlocal skipped
                    if skipped < offset:
                        skipped += 1
                        return
                    items.append({"lecture_id": lecture["id"], "display_title": lecture["display_title"] or lecture["title"],
                                  "course": lecture["course"], "semester": lecture["semester"],
                                  "created_at": lecture["created_at"], **match})

                for lecture in rows:
                    check()
                    title = lecture["display_title"] or lecture["title"]
                    if not q or _fold(q) in _fold(title):
                        take(lecture, {"source": "title", "segment_id": None, "start": None, "end": None,
                                       "snippet": _snippet(title, q)})
                        if len(items) > limit:
                            break
                    if not q:
                        continue
                    raw_matches = ()
                    if source in ("all", "raw"):
                        raw_matches = connection.execute(
                            "SELECT s.id,s.start,s.end,substr(s.text,MAX(1,instr(lower(s.text),lower(?))-60),?) AS text "
                            "FROM segments s JOIN chunks c ON c.lecture_id=s.lecture_id AND c.chunk_id=s.chunk_id "
                            "WHERE s.lecture_id=? AND c.status='done' AND s.text LIKE ? ESCAPE '\\' "
                            "ORDER BY s.start,s.end,s.id", (q, SNIPPET_CHARS, lecture["id"], pattern),
                        )
                    corrected = []
                    if source in ("all", "corrected"):
                        corrected, incomplete = _corrected_segments(connection, lecture, raw_segments, transcript_revision, check, q)
                        partial_corrected = partial_corrected or incomplete
                    candidates = heapq.merge(
                        ((row["start"], row["end"], row["id"], 0, row["text"]) for row in raw_matches),
                        ((row["start"], row["end"], row["id"], 1, row["text"]) for row in corrected
                         if _fold(q) in _fold(row["text"])),
                    )
                    for start, end, identifier, kind, text in candidates:
                        check()
                        take(lecture, {"source": "raw" if kind == 0 else "corrected", "segment_id": identifier,
                                       "start": start, "end": end, "snippet": _snippet(text, q)})
                        if len(items) > limit:
                            break
                    if len(items) > limit:
                        break
                return {"items": items[:limit], "offset": offset, "limit": limit,
                        "has_more": len(items) > limit, "partial_corrected": partial_corrected}
        finally:
            search_capacity.release()
