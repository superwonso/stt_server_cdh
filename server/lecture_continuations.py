"""Owner-scoped links between independent recordings, never audio append.

Creation helpers run inside the caller's BEGIN IMMEDIATE transaction. A
deleted parent unlinks the child; its hashed creation intent remains private
so a lost creation response cannot later be replayed with a different parent.
"""
from __future__ import annotations

import hashlib
import secrets

from fastapi import HTTPException

MAX_CHAIN_LECTURES = 64
MAX_DIRECT_CHILDREN = 1000


def _parent_hash(parent_id: str) -> str:
    return hashlib.sha256(parent_id.encode("ascii")).hexdigest()


def matches_creation(connection, child_id: str, parent_id: str | None) -> bool:
    """Called only after the existing child has passed owner/body checks."""
    row = connection.execute(
        "SELECT parent_request_hash FROM lecture_continuations WHERE child_lecture_id=?", (child_id,),
    ).fetchone()
    if parent_id is None:
        return row is None
    return row is not None and secrets.compare_digest(row["parent_request_hash"], _parent_hash(parent_id))


def link_new_lecture(connection, *, child_id: str, parent_id: str | None, username: str) -> None:
    """Validate and link a just-inserted child atomically, without touching parent data."""
    if parent_id is None:
        return
    child = connection.execute(
        "SELECT id FROM lectures WHERE id=? AND username=? AND deleting=0 AND trashed_at IS NULL",
        (child_id, username),
    ).fetchone()
    parent = connection.execute(
        "SELECT id FROM lectures WHERE id=? AND username=? AND deleting=0 AND trashed_at IS NULL",
        (parent_id, username),
    ).fetchone()
    if parent is None or child is None:
        raise HTTPException(404, "이어 녹음할 수업을 찾을 수 없습니다.")
    if child_id == parent_id:
        raise HTTPException(409, "같은 수업을 이어 녹음의 부모로 지정할 수 없습니다.")

    # Newly created children cannot normally form cycles. Still bound traversal
    # and reject corrupt/cross-owner ancestry instead of trusting stored links.
    visited = {child_id}
    ancestor_id = parent_id
    while ancestor_id is not None:
        if ancestor_id in visited or len(visited) >= MAX_CHAIN_LECTURES:
            raise HTTPException(409, "이어 녹음 연결이 너무 길거나 올바르지 않습니다. 새 수업으로 시작해 주세요.")
        visited.add(ancestor_id)
        ancestor = connection.execute(
            "SELECT l.username,c.parent_lecture_id FROM lectures l "
            "LEFT JOIN lecture_continuations c ON c.child_lecture_id=l.id WHERE l.id=?", (ancestor_id,),
        ).fetchone()
        if ancestor is None or ancestor["username"] != username:
            raise HTTPException(409, "이어 녹음 연결을 확인하지 못했습니다.")
        ancestor_id = ancestor["parent_lecture_id"]

    count = connection.execute(
        "SELECT COUNT(*) FROM lecture_continuations WHERE parent_lecture_id=?", (parent_id,),
    ).fetchone()[0]
    if count >= MAX_DIRECT_CHILDREN:
        raise HTTPException(409, "이 수업에 연결된 녹음이 너무 많습니다. 새 수업으로 시작해 주세요.")
    connection.execute(
        "INSERT INTO lecture_continuations(child_lecture_id,parent_lecture_id,parent_request_hash) VALUES(?,?,?)",
        (child_id, parent_id, _parent_hash(parent_id)),
    )


def fields_for(connection, lecture) -> dict:
    """Public projection: live, same-owner direct links only; never stored hashes."""
    username, lecture_id = lecture["username"], lecture["id"]
    parent = connection.execute(
        "SELECT p.id FROM lecture_continuations c "
        "JOIN lectures l ON l.id=c.child_lecture_id JOIN lectures p ON p.id=c.parent_lecture_id "
        "WHERE l.id=? AND l.username=? AND p.username=? "
        "AND l.deleting=0 AND l.trashed_at IS NULL AND p.deleting=0 AND p.trashed_at IS NULL",
        (lecture_id, username, username),
    ).fetchone()
    children = connection.execute(
        "SELECT l.id FROM lecture_continuations c "
        "JOIN lectures l ON l.id=c.child_lecture_id JOIN lectures p ON p.id=c.parent_lecture_id "
        "WHERE p.id=? AND p.username=? AND l.username=? "
        "AND l.deleting=0 AND l.trashed_at IS NULL AND p.deleting=0 AND p.trashed_at IS NULL "
        "ORDER BY l.created_at,l.id LIMIT ?",
        (lecture_id, username, username, MAX_DIRECT_CHILDREN + 1),
    ).fetchall()
    if len(children) > MAX_DIRECT_CHILDREN:
        raise HTTPException(503, "저장된 녹음 연결을 확인하지 못했습니다.")
    return {"continuation_of": parent["id"] if parent is not None else None,
            "continuations": [row["id"] for row in children]}
