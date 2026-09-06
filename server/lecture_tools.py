"""Owner-only bounded WAV previews and non-destructive lecture bookmarks."""
from __future__ import annotations

import math
import os
import threading
import uuid
from datetime import UTC, datetime

from fastapi import Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .recordings import (
    BYTES_PER_FRAME, SAMPLE_RATE, WAV_HEADER_BYTES, _header, _valid_header,
)

MAX_CLIP_SECONDS = 120
MAX_BOOKMARKS = 500
_READ_BYTES = 256 * 1024


class BookmarkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    start_seconds: float = Field(strict=True, ge=0, allow_inf_nan=False)
    label: str = Field(default="", max_length=120, strict=True)


def _unavailable() -> HTTPException:
    return HTTPException(503, "녹음 구간을 안전하게 확인하지 못했습니다. 잠시 후 다시 시도하세요.",
                         headers={"Retry-After": "5"})


def _range_error() -> HTTPException:
    return HTTPException(416, "요청한 녹음 구간을 재생할 수 없습니다.")


def _query_seconds(value: str, maximum: float, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise _range_error() from None
    if not math.isfinite(result) or result < 0 or result > maximum or (positive and result <= 0):
        raise _range_error()
    return result


def _frames(total: int) -> int:
    if type(total) is not int or total <= WAV_HEADER_BYTES or total > 2**32 - 1:
        raise _unavailable()
    data_bytes = total - WAV_HEADER_BYTES
    if data_bytes % BYTES_PER_FRAME:
        raise _unavailable()
    return data_bytes // BYTES_PER_FRAME


def _clip_range(total: int, start: float, duration: float) -> tuple[int, int]:
    frames = _frames(total)
    first = int(start * SAMPLE_RATE)
    count = min(int(duration * SAMPLE_RATE), frames - first)
    if first >= frames or count <= 0:
        raise _range_error()
    return first, count


def _read_descriptor(descriptor: int, offset: int, length: int) -> bytes:
    os.lseek(descriptor, offset, os.SEEK_SET)
    result = bytearray()
    while len(result) < length:
        block = os.read(descriptor, min(_READ_BYTES, length - len(result)))
        if not block:
            raise _unavailable()
        result.extend(block)
    return bytes(result)


def _read_remote(archive_manager, lecture_id: str, total: int, first: int, last: int) -> bytes:
    """The archive manager checks binding/checksums; independently bound the response."""
    stream = archive_manager.open_download(lecture_id, start=first, end=last)
    try:
        expected = last - first
        if (
            type(stream.status_code) is not int or stream.status_code != 206
            or type(stream.content_length) is not int or stream.content_length != expected
            or stream.content_range != f"bytes {first}-{last - 1}/{total}"
        ):
            raise _unavailable()
        result = bytearray()
        for block in stream.iter_bytes():
            if not isinstance(block, bytes) or len(block) > expected - len(result):
                raise _unavailable()
            result.extend(block)
        if len(result) != expected:
            raise _unavailable()
        return bytes(result)
    finally:
        stream.close()


def install(app, settings, database, recording_store, archive_manager, *,
            identity, owned_lecture, limiter):
    """Register routes; callers provide the same data-access auth as lecture APIs."""
    clip_capacity = threading.BoundedSemaphore(2)

    def owned_now(connection, lecture_id: str, username: str, *, clip: bool = False):
        row = connection.execute(
            "SELECT recording_finalized,deleting,trashed_at FROM lectures WHERE id=? AND username=?",
            (lecture_id, username),
        ).fetchone()
        if row is None or row["trashed_at"] is not None:
            raise HTTPException(404, "수업을 찾을 수 없습니다.")
        if row["deleting"]:
            if clip:
                raise _unavailable()
            raise HTTPException(404, "수업을 찾을 수 없습니다.")
        if clip and not row["recording_finalized"]:
            raise HTTPException(409, "녹음 종료와 마지막 저장이 끝난 뒤 재생할 수 있습니다.")
        return row

    def allow(operation: str, username: str, maximum: int):
        if not limiter.allow((operation, username), maximum, 60):
            raise HTTPException(429, "요청이 많습니다. 잠시 후 다시 시도하세요.",
                                headers={"Retry-After": "60"})

    @app.get("/lectures/{lecture_id}/recording-clip")
    def recording_clip(lecture_id: str, start: str = "0", duration: str = "60", user: dict = Depends(identity)):
        username = user["username"]
        # Include own deleting rows only so we can distinguish an unavailable
        # recording without ever exposing another account's lecture.
        owned_lecture(lecture_id, username, include_deleting=True)
        with database.connect() as connection:
            owned_now(connection, lecture_id, username, clip=True)
        first_seconds = _query_seconds(start, settings.max_import_seconds)
        duration_seconds = _query_seconds(duration, MAX_CLIP_SECONDS, positive=True)
        allow("recording-clip", username, 30)
        if not clip_capacity.acquire(blocking=False):
            raise HTTPException(429, "다른 녹음 구간을 읽고 있습니다. 잠시 후 다시 시도하세요.",
                                headers={"Retry-After": "2"})
        try:
            total = archive_manager.remote_size(lecture_id)
            if total is not None:
                # A verified remote locator takes precedence even when a
                # local cleanup copy exists: matching that copy would require
                # hashing the WHOLE WAV on every clip. No unverified fallback.
                first_frame, count = _clip_range(total, first_seconds, duration_seconds)
                first = WAV_HEADER_BYTES + first_frame * BYTES_PER_FRAME
                last = first + count * BYTES_PER_FRAME
                if first_frame == 0:
                    payload = _read_remote(archive_manager, lecture_id, total, 0, last)
                    source_header, pcm = payload[:WAV_HEADER_BYTES], payload[WAV_HEADER_BYTES:]
                else:
                    source_header = _read_remote(archive_manager, lecture_id, total, 0, WAV_HEADER_BYTES)
                    if not _valid_header(source_header, total - WAV_HEADER_BYTES):
                        raise _unavailable()
                    pcm = _read_remote(archive_manager, lecture_id, total, first, last)
                if not _valid_header(source_header, total - WAV_HEADER_BYTES):
                    raise _unavailable()
            else:
                # A malformed/incomplete remote locator must not downgrade to
                # trusting a duplicate local file.
                with database.connect() as connection:
                    remote = connection.execute(
                        "SELECT 1 FROM recording_archives WHERE lecture_id=? AND drive_file_id IS NOT NULL",
                        (lecture_id,),
                    ).fetchone()
                if remote is not None:
                    raise _unavailable()
                recording = recording_store.open_info(username, lecture_id)
                if recording is None:
                    raise HTTPException(404, "이 수업에는 저장된 녹음이 없습니다.")
                descriptor = recording["descriptor"]
                try:
                    total = recording["bytes"]
                    first_frame, count = _clip_range(total, first_seconds, duration_seconds)
                    pcm = _read_descriptor(descriptor, WAV_HEADER_BYTES + first_frame * BYTES_PER_FRAME,
                                           count * BYTES_PER_FRAME)
                    before, after = recording["stat"], os.fstat(descriptor)
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns
                    ):
                        raise _unavailable()
                finally:
                    os.close(descriptor)
            with database.connect() as connection:
                owned_now(connection, lecture_id, username, clip=True)
            return Response(
                _header(len(pcm)) + pcm, media_type="audio/wav",
                headers={
                    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": 'inline; filename="lecture-clip.wav"',
                    "X-Clip-Start-Seconds": str(first_frame / SAMPLE_RATE),
                    "X-Clip-Duration-Seconds": str(count / SAMPLE_RATE),
                },
            )
        except HTTPException:
            raise
        except Exception:
            # No provider body, locator, file path, title or private text in errors.
            raise _unavailable() from None
        finally:
            clip_capacity.release()

    def bookmark_result(row):
        return {key: row[key] for key in ("id", "start_seconds", "label", "created_at")}

    @app.get("/lectures/{lecture_id}/bookmarks")
    def list_bookmarks(lecture_id: str, user: dict = Depends(identity)):
        owned_lecture(lecture_id, user["username"])
        allow("bookmarks-read", user["username"], 120)
        with database.connect() as connection:
            owned_now(connection, lecture_id, user["username"])
            rows = connection.execute(
                "SELECT id,start_seconds,label,created_at FROM lecture_bookmarks WHERE lecture_id=? "
                "ORDER BY start_seconds,created_at,id LIMIT ?", (lecture_id, MAX_BOOKMARKS),
            ).fetchall()
        return {"bookmarks": [bookmark_result(row) for row in rows]}

    @app.post("/lectures/{lecture_id}/bookmarks")
    def add_bookmark(lecture_id: str, body: BookmarkBody, user: dict = Depends(identity)):
        owned_lecture(lecture_id, user["username"])
        if body.start_seconds > min(settings.max_import_seconds, 14400) or any(ord(c) < 32 for c in body.label):
            raise HTTPException(422, "북마크 시간이나 이름을 확인하세요.")
        allow("bookmarks-write", user["username"], 60)
        bookmark_id = str(body.id)
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owned_now(connection, lecture_id, user["username"])
            existing = connection.execute("SELECT * FROM lecture_bookmarks WHERE id=?", (bookmark_id,)).fetchone()
            if existing is not None:
                if (existing["lecture_id"], existing["start_seconds"], existing["label"]) != (
                    lecture_id, body.start_seconds, body.label
                ):
                    raise HTTPException(409, "같은 북마크 식별자로 다른 내용을 저장할 수 없습니다.")
                return {"bookmark": bookmark_result(existing)}
            count = connection.execute("SELECT COUNT(*) FROM lecture_bookmarks WHERE lecture_id=?", (lecture_id,)).fetchone()[0]
            if count >= MAX_BOOKMARKS:
                raise HTTPException(409, "수업당 북마크는 최대 500개까지 저장할 수 있습니다.")
            created_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            connection.execute(
                "INSERT INTO lecture_bookmarks(id,lecture_id,start_seconds,label,created_at) VALUES (?,?,?,?,?)",
                (bookmark_id, lecture_id, body.start_seconds, body.label, created_at),
            )
        return {"bookmark": {"id": bookmark_id, "start_seconds": body.start_seconds,
                             "label": body.label, "created_at": created_at}}

    @app.delete("/lectures/{lecture_id}/bookmarks/{bookmark_id}")
    def delete_bookmark(lecture_id: str, bookmark_id: str, user: dict = Depends(identity)):
        owned_lecture(lecture_id, user["username"])
        try:
            normalized = str(uuid.UUID(bookmark_id))
        except ValueError:
            raise HTTPException(404, "북마크를 찾을 수 없습니다.") from None
        allow("bookmarks-write", user["username"], 60)
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owned_now(connection, lecture_id, user["username"])
            changed = connection.execute(
                "DELETE FROM lecture_bookmarks WHERE id=? AND lecture_id=?", (normalized, lecture_id),
            ).rowcount
            if changed == 0:
                raise HTTPException(404, "북마크를 찾을 수 없습니다.")
        return {"deleted": True}
