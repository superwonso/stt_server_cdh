"""Reversible, owner-scoped app trash; remote media stays in place until purge."""
from datetime import UTC, datetime

from fastapi import Depends, HTTPException


def _owned(connection, lecture_id, username):
    row = connection.execute(
        "SELECT id,username,recording_finalized,deleting,trashed_at "
        "FROM lectures WHERE id=? AND username=?", (lecture_id, username),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "수업을 찾을 수 없습니다.")
    return row


def _require_idle(connection, lecture_id):
    # Check in the same write transaction as the state change. Each worker
    # claims its job inside BEGIN IMMEDIATE too, so a claim and trash cannot
    # both succeed. Queued AI jobs are preserved, not silently cancelled or
    # unexpectedly restarted by restoring a lesson.
    from .course_review import active_lecture_review
    if active_lecture_review(connection, lecture_id):
        raise HTTPException(409, "이 수업을 사용하는 강의 복습이 끝난 뒤 휴지통으로 옮기세요.")
    for table, states in (
        ("chunks", "'pending'"),
        ("imports", "'uploading','queued','processing'"),
        ("transcript_corrections", "'queued','processing'"),
        ("lecture_summaries", "'queued','processing'"),
        ("lecture_translations", "'queued','processing'"),
        ("lecture_questions", "'queued','processing'"),
        ("lecture_study_notes", "'queued','processing'"),
        ("study_materials", "'processing'"),
    ):
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE lecture_id=? AND status IN ({states}) LIMIT 1",
            (lecture_id,),
        ).fetchone() is not None:
            raise HTTPException(409, "진행 중인 음성 처리·후보정·요약·번역·수업 질문·정리본이 끝난 뒤 휴지통으로 옮기세요.")


def install(app, database, *, identity, import_fs_lock, recording_lock, purge_tickets, limiter):
    def allow(username):
        if not limiter.allow(("lecture-trash", username), 60, 60):
            raise HTTPException(429, "요청이 많습니다. 잠시 후 다시 시도하세요.",
                                headers={"Retry-After": "60"})

    @app.get("/library/trash")
    def list_trash(user: dict = Depends(identity)):
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT l.id AS lecture_id,COALESCE(m.display_title,l.title) AS display_title,"
                "COALESCE(m.course,'') AS course,COALESCE(m.semester,'') AS semester,"
                "l.created_at,l.trashed_at,l.deleting FROM lectures l "
                "LEFT JOIN lecture_metadata m ON m.lecture_id=l.id "
                "WHERE l.username=? AND l.trashed_at IS NOT NULL "
                "ORDER BY l.trashed_at DESC,l.id DESC LIMIT 1000", (user["username"],),
            ).fetchall()
        return [{**dict(row), "deleting": bool(row["deleting"])} for row in rows]

    @app.delete("/lectures/{lecture_id}")
    @app.post("/lectures/{lecture_id}/trash")
    def trash_lecture(lecture_id: str, user: dict = Depends(identity)):
        allow(user["username"])
        # Preserve the existing filesystem -> SQLite order. No provider calls,
        # upload removal, or remote archive lock is used by reversible trash.
        with import_fs_lock, recording_lock:
            with database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                lecture = _owned(connection, lecture_id, user["username"])
                if lecture["deleting"]:
                    raise HTTPException(409, "이미 영구 삭제 중인 수업은 복원 가능한 휴지통으로 옮길 수 없습니다.")
                trashed_at = lecture["trashed_at"]
                if trashed_at is None:
                    if not lecture["recording_finalized"]:
                        raise HTTPException(409, "수업 녹음과 마지막 저장이 끝난 뒤 휴지통으로 옮기세요.")
                    _require_idle(connection, lecture_id)
                    trashed_at = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
                    connection.execute(
                        "UPDATE lectures SET trashed_at=? WHERE id=? AND username=? AND deleting=0",
                        (trashed_at, lecture_id, user["username"]),
                    )
        # Commit first. Ticket creation rechecks visibility under its own lock;
        # it cannot mint a usable grant after this purge has completed.
        purge_tickets(lecture_id)
        return {"status": "trashed", "lecture_id": lecture_id, "trashed_at": trashed_at}

    @app.post("/lectures/{lecture_id}/restore")
    def restore_lecture(lecture_id: str, user: dict = Depends(identity)):
        allow(user["username"])
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lecture = _owned(connection, lecture_id, user["username"])
            if lecture["deleting"]:
                raise HTTPException(409, "영구 삭제가 시작된 수업은 복원할 수 없습니다.")
            if lecture["trashed_at"] is not None:
                connection.execute(
                    "UPDATE lectures SET trashed_at=NULL WHERE id=? AND username=? AND deleting=0",
                    (lecture_id, user["username"]),
                )
        return {"status": "restored", "lecture_id": lecture_id}
