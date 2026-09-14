"""Owner-scoped, ASR-independent receipts for lossless recording uploads.

WAV bytes are fsynced before committing a receipt. A lost DB acknowledgement
can therefore be replayed against byte-identical audio without appending twice.
Neither this module nor its raw-only route invokes a recognition provider.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone

from fastapi import HTTPException

from .recordings import RecordingCapacityError, RecordingConflict, RecordingCorruptError, SAMPLE_RATE


def sample_time(value: float) -> int:
    exact = value * SAMPLE_RATE
    if not math.isfinite(exact) or exact < 0:
        raise HTTPException(422, "녹음 조각의 시간이 올바르지 않습니다.")
    rounded = round(exact)
    if abs(exact - rounded) > 0.01:
        raise HTTPException(422, "녹음 조각의 시간은 오디오 샘플 경계와 맞아야 합니다.")
    return rounded


def matching_receipt(connection, lecture_id, chunk_id, payload_hash, start_seconds,
                     duration, overlap_seconds, final_chunk):
    row = connection.execute(
        "SELECT * FROM recording_chunks WHERE lecture_id=? AND chunk_id=?",
        (lecture_id, chunk_id),
    ).fetchone()
    if row is None:
        return None
    if (row["payload_hash"] != payload_hash
            or row["start_samples"] != sample_time(start_seconds)
            or row["duration_samples"] != sample_time(duration)
            or row["overlap_samples"] != sample_time(overlap_seconds)
            or bool(row["final_chunk"]) != final_chunk):
        raise HTTPException(409, "같은 음성 ID로 다른 녹음 조각을 보낼 수 없습니다.")
    return row


def require_asr_receipt(connection, lecture, chunk_id, payload_hash, start_seconds,
                        duration, overlap_seconds, final_chunk):
    receipt = matching_receipt(connection, lecture["id"], chunk_id, payload_hash,
                               start_seconds, duration, overlap_seconds, final_chunk)
    raw_started = connection.execute(
        "SELECT 1 FROM recording_chunks WHERE lecture_id=? LIMIT 1", (lecture["id"],),
    ).fetchone() is not None
    if (lecture["audio_finalized"] or raw_started) and receipt is None:
        raise HTTPException(409, "원본 녹음을 먼저 서버에 보관한 뒤 전사를 요청해 주세요.")
    return receipt


def stored_seconds(connection, lecture_id):
    value = connection.execute(
        "SELECT MAX(start_samples+duration_samples) FROM recording_chunks WHERE lecture_id=?",
        (lecture_id,),
    ).fetchone()[0]
    return (value or 0) / SAMPLE_RATE


class RecordingUploadStore:
    def __init__(self, database, recordings):
        self.database = database
        self.recordings = recordings

    @staticmethod
    def _owned(connection, lecture_id, username):
        row = connection.execute(
            "SELECT id,username,recording_finalized,audio_finalized FROM lectures "
            "WHERE id=? AND username=? AND deleting=0 AND trashed_at IS NULL",
            (lecture_id, username),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "수업을 찾을 수 없습니다.")
        return row

    def source_frames(self, connection, lecture, required_end):
        """Check existing storage evidence; never trust a historical ACK alone.

        The caller holds the recording lock before its SQLite transaction.
        Only a validated local WAV or a previously checksum-verified archive
        can substantiate an ACK. No Drive network operation is performed here.
        """
        try:
            info = self.recordings.info(lecture["username"], lecture["id"])
        except (RecordingCorruptError, OSError):
            info = None
        if info is not None:
            frames = sample_time(info["duration_seconds"])
            if required_end <= frames <= self.recordings.max_frames:
                return frames
        archive = connection.execute(
            "SELECT state,drive_file_id,source_bytes,source_sha256,source_md5 "
            "FROM recording_archives WHERE lecture_id=?", (lecture["id"],),
        ).fetchone()
        if (archive is not None and archive["state"] == "ready"
                and archive["drive_file_id"] and isinstance(archive["source_bytes"], int)
                and archive["source_bytes"] >= 44 and (archive["source_bytes"] - 44) % 2 == 0
                and re.fullmatch(r"[0-9a-f]{64}", archive["source_sha256"] or "")
                and re.fullmatch(r"[0-9a-f]{32}", archive["source_md5"] or "")):
            frames = (archive["source_bytes"] - 44) // 2
            if required_end <= frames <= self.recordings.max_frames:
                return frames
        raise HTTPException(503, "서버나 보관 저장소에서 원본 녹음을 확인하지 못했습니다. "
                            "기기에 남은 음성을 지우지 말고 보관해 주세요.", headers={"Retry-After": "5"})

    def check_source_for_chunk(self, lecture_id, username, chunk_id):
        with self.recordings.lock, self.database.connect() as connection:
            connection.execute("BEGIN")
            lecture = self._owned(connection, lecture_id, username)
            row = connection.execute(
                "SELECT start_samples,duration_samples FROM recording_chunks WHERE lecture_id=? AND chunk_id=?",
                (lecture_id, chunk_id),
            ).fetchone()
            if row is not None:
                self.source_frames(connection, lecture, row["start_samples"] + row["duration_samples"])

    def _response(self, connection, lecture, row):
        frames = self.source_frames(connection, lecture, row["start_samples"] + row["duration_samples"])
        return {
            "status": "stored", "chunk_id": row["chunk_id"],
            "payload_sha256": row["payload_hash"],
            "start_seconds": row["start_samples"] / SAMPLE_RATE,
            "duration_seconds": row["duration_samples"] / SAMPLE_RATE,
            "overlap_seconds": row["overlap_samples"] / SAMPLE_RATE,
            "final_chunk": bool(row["final_chunk"]),
            "recording_audio_finalized": bool(lecture["audio_finalized"] or lecture["recording_finalized"]),
            "recording_stored_seconds": frames / SAMPLE_RATE,
        }

    def result(self, lecture_id, username, chunk_id):
        with self.recordings.lock, self.database.connect() as connection:
            connection.execute("BEGIN")
            lecture = self._owned(connection, lecture_id, username)
            row = connection.execute(
                "SELECT * FROM recording_chunks WHERE lecture_id=? AND chunk_id=?",
                (lecture_id, chunk_id),
            ).fetchone()
            return ({"status": "unknown", "chunk_id": chunk_id} if row is None
                    else self._response(connection, lecture, row))

    def store(self, lecture_id, user, chunk_id, payload_hash, start_seconds,
              duration, overlap_seconds, final_chunk, pcm):
        start = sample_time(start_seconds)
        frames = sample_time(duration)
        overlap = sample_time(overlap_seconds)
        if not 800 <= frames <= 240000 or len(pcm) != frames * 2 or not 0 <= overlap <= min(48000, frames):
            raise HTTPException(422, "녹음 조각의 길이나 겹침 시간이 올바르지 않습니다.")
        if not final_chunk and overlap == frames:
            raise HTTPException(422, "마지막 조각이 아니면 새 음성이 필요합니다.")
        with self.recordings.lock, self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lecture = self._owned(connection, lecture_id, user["username"])
            live = connection.execute(
                "SELECT 1 FROM sessions WHERE token_hash=? AND username=? AND expires_at>?",
                (user["token_hash"], user["username"], time.time()),
            ).fetchone()
            if live is None:
                raise HTTPException(401, "로그인이 만료되었습니다. 다시 로그인하세요.",
                                    headers={"WWW-Authenticate": "Bearer"})
            existing = matching_receipt(connection, lecture_id, chunk_id, payload_hash,
                                        start_seconds, duration, overlap_seconds, final_chunk)
            if existing is not None:
                return self._response(connection, lecture, existing)
            if lecture["audio_finalized"]:
                raise HTTPException(409, "이미 원본 저장이 끝난 수업에는 새 음성을 추가할 수 없습니다.")
            if connection.execute("SELECT 1 FROM imports WHERE lecture_id=?", (lecture_id,)).fetchone():
                raise HTTPException(409, "파일 가져오기 수업에는 별도 녹음 조각을 추가할 수 없습니다.")
            previous_end = connection.execute(
                "SELECT MAX(start_samples+duration_samples) FROM recording_chunks WHERE lecture_id=?",
                (lecture_id,),
            ).fetchone()[0]
            if previous_end is not None and start + overlap != previous_end:
                raise HTTPException(409, "원본 녹음 조각은 이전 저장 조각에 이어서 보내야 합니다.")
            asr = connection.execute(
                "SELECT * FROM chunks WHERE lecture_id=? AND chunk_id=?", (lecture_id, chunk_id),
            ).fetchone()
            if asr is not None and (
                asr["payload_hash"] != payload_hash or asr["start_seconds"] != start_seconds
                or asr["overlap_seconds"] != overlap_seconds or bool(asr["final_chunk"]) != final_chunk
            ):
                raise HTTPException(409, "전사 요청과 원본 녹음의 내용이 다릅니다.")
            if lecture["recording_finalized"]:
                # A legacy ASR upload may have finalized/archived first. Only
                # its exact committed receipt can establish the matching raw ACK;
                # never recreate a local WAV after Drive moved it away.
                if asr is None or asr["status"] != "done":
                    raise HTTPException(409, "이미 종료된 수업에는 새 음성을 추가할 수 없습니다.")
                self.source_frames(connection, lecture, start + frames)
            else:
                try:
                    self.recordings.write_chunk(user["username"], lecture_id,
                        start_seconds=start_seconds, overlap_seconds=overlap_seconds,
                        pcm=pcm, strict_contiguous=True)
                    if final_chunk:
                        info = self.recordings.info(user["username"], lecture_id)
                        if info is None or sample_time(info["duration_seconds"]) != start + frames:
                            raise RecordingConflict("final raw chunk does not end at the saved recording boundary")
                except RecordingConflict:
                    raise HTTPException(409, "원본 녹음의 시간 순서 또는 겹친 음성이 기존 파일과 다릅니다.") from None
                except RecordingCapacityError:
                    raise HTTPException(507, "녹음을 보관할 서버 저장 공간이 부족합니다.") from None
                except (RecordingCorruptError, OSError):
                    raise HTTPException(503, "서버에 원본 녹음을 안전하게 저장하지 못했습니다.") from None
            connection.execute(
                "INSERT INTO recording_chunks(lecture_id,chunk_id,payload_hash,start_samples,duration_samples,"
                "overlap_samples,final_chunk,stored_at) VALUES(?,?,?,?,?,?,?,?)",
                (lecture_id, chunk_id, payload_hash, start, frames, overlap, int(final_chunk),
                 datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
            )
            if final_chunk:
                connection.execute("UPDATE lectures SET audio_finalized=1 WHERE id=?", (lecture_id,))
                if asr is not None and asr["status"] == "done":
                    connection.execute("UPDATE lectures SET recording_finalized=1 WHERE id=?", (lecture_id,))
            lecture = self._owned(connection, lecture_id, user["username"])
            row = matching_receipt(connection, lecture_id, chunk_id, payload_hash,
                                   start_seconds, duration, overlap_seconds, final_chunk)
            return self._response(connection, lecture, row)
