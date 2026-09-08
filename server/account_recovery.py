"""Administrator-issued recovery, separate from first-account activation.

The two CLI primitives require a caller-owned IMMEDIATE transaction and local
operator authority. They never initialize a database, print secrets, or touch
an existing password/session. Only the one-time completion route does that.
"""
from __future__ import annotations

import math
import secrets
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from .security import PASSWORD_HASHER, digest, new_secret, password_matches

RESET_LIFETIME_SECONDS = 30 * 60
RESET_INVALID = "재설정 코드가 올바르지 않거나 만료되었습니다. 관리자에게 새 코드를 요청하세요."
REAUTH_INVALID = "관리자 인증을 다시 확인하세요."


class RecoveryError(ValueError):
    def __init__(self, code: str):
        self.code = code if code in {"account_not_active", "reset_unavailable"} else "reset_unavailable"
        super().__init__("활성 계정에서만 비밀번호 복구를 사용할 수 있습니다."
                         if self.code == "account_not_active" else "비밀번호 복구를 처리할 수 없습니다.")


def _require_transaction(connection):
    if not connection.in_transaction:
        raise RecoveryError("reset_unavailable")


def issue_password_reset(connection, username: str, *, now: float | None = None) -> dict:
    """Replace this account's outstanding reset; caller commits atomically."""
    _require_transaction(connection)
    current = time.time() if now is None else now
    if isinstance(current, bool) or not isinstance(current, (int, float)) or not math.isfinite(current):
        raise RecoveryError("reset_unavailable")
    user = connection.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
    if user is None or not user["password_hash"]:
        raise RecoveryError("account_not_active")
    code = new_secret()
    expires_at = current + RESET_LIFETIME_SECONDS
    connection.execute(
        "INSERT INTO account_password_resets(username,token_hash,password_fingerprint,created_at,expires_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET token_hash=excluded.token_hash, "
        "password_fingerprint=excluded.password_fingerprint,created_at=excluded.created_at,expires_at=excluded.expires_at",
        (username, digest(code), digest(user["password_hash"]), current, expires_at),
    )
    return {"reset_code": code, "username": username, "expires_at": expires_at}


def revoke_password_reset(connection, username: str) -> None:
    _require_transaction(connection)
    connection.execute("DELETE FROM account_password_resets WHERE username = ?", (username,))


class AdministratorResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    current_password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class CompleteResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    reset_code: Annotated[str, StringConstraints(min_length=20, max_length=128)]
    password: Annotated[str, StringConstraints(min_length=4, max_length=128)]
    password_confirm: Annotated[str, StringConstraints(min_length=4, max_length=128)]


def install(app, database, *, admin_identity, account_ids, administrator, auth_limit,
            purge_tickets, purge_presence, audit) -> None:
    accounts = frozenset(account_ids)

    def reauthenticate(body, request, user):
        auth_limit(request, user["username"], "password-reset-admin")
        with database.connect() as connection:
            row = connection.execute("SELECT password_hash FROM users WHERE username = ?",
                                     (user["username"],)).fetchone()
        encoded = row["password_hash"] if row else None
        if not password_matches(encoded, body.current_password):
            raise HTTPException(403, REAUTH_INVALID)
        target = next((name for name, opaque in account_ids.items()
                       if secrets.compare_digest(opaque.encode(), body.account_id.encode())), None)
        if target is None:
            raise HTTPException(404, "계정을 찾을 수 없습니다.")
        if target == user["username"]:
            raise HTTPException(409, "관리자 본인의 비밀번호 복구는 서버의 로컬 관리 명령을 사용하세요.")
        return target, encoded

    def recheck_administrator(connection, user, encoded):
        # Password hashing is intentionally outside the write lock, but neither
        # a revoked session nor a concurrently changed password grants a write.
        row = connection.execute(
            "SELECT u.password_hash FROM sessions s JOIN users u ON u.username=s.username "
            "WHERE s.token_hash=? AND s.username=? AND s.expires_at>?",
            (user["token_hash"], user["username"], time.time()),
        ).fetchone()
        if (not administrator or user["username"] != administrator or row is None
                or not row["password_hash"] or not secrets.compare_digest(row["password_hash"], encoded)):
            raise HTTPException(401, REAUTH_INVALID)

    @app.post("/admin/password-resets")
    def issue(body: AdministratorResetBody, request: Request, user: dict = Depends(admin_identity)):
        target, encoded = reauthenticate(body, request, user)
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recheck_administrator(connection, user, encoded)
            try:
                result = issue_password_reset(connection, target)
            except RecoveryError as error:
                raise HTTPException(409, "활성 계정에서만 비밀번호 복구를 사용할 수 있습니다.") from error
            audit(connection, "password_reset_issued", "success", target)
        return result

    @app.post("/admin/password-resets/revoke")
    def revoke(body: AdministratorResetBody, request: Request, user: dict = Depends(admin_identity)):
        target, encoded = reauthenticate(body, request, user)
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recheck_administrator(connection, user, encoded)
            active = connection.execute("SELECT password_hash FROM users WHERE username=?", (target,)).fetchone()
            if active is None or not active["password_hash"]:
                raise HTTPException(409, "활성 계정에서만 비밀번호 복구를 사용할 수 있습니다.")
            revoke_password_reset(connection, target)
            audit(connection, "password_reset_revoked", "success", target)
        return {"status": "revoked"}

    def valid_reset(connection, body):
        row = connection.execute(
            "SELECT u.password_hash,r.token_hash,r.password_fingerprint,r.expires_at "
            "FROM users u JOIN account_password_resets r ON r.username=u.username WHERE u.username=?",
            (body.username,),
        ).fetchone()
        if (body.username not in accounts or row is None or not row["password_hash"]
                or row["expires_at"] <= time.time()
                or not secrets.compare_digest(row["token_hash"], digest(body.reset_code))
                or not secrets.compare_digest(row["password_fingerprint"], digest(row["password_hash"]))):
            raise HTTPException(400, RESET_INVALID)
        return row["password_hash"]

    @app.post("/auth/reset-password")
    def complete(body: CompleteResetBody, request: Request):
        auth_limit(request, body.username, "password-reset")
        if body.password != body.password_confirm:
            raise HTTPException(422, "새 비밀번호와 확인 값이 일치하지 않습니다.")
        # Reject invalid/expired credentials before expensive Argon2 work.
        with database.connect() as connection:
            valid_reset(connection, body)
        encoded = PASSWORD_HASHER.hash(body.password)
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            valid_reset(connection, body)
            connection.execute(
                "UPDATE users SET password_hash=?,setup_hash=NULL,setup_expires=NULL WHERE username=?",
                (encoded, body.username),
            )
            connection.execute("DELETE FROM sessions WHERE username=?", (body.username,))
            revoke_password_reset(connection, body.username)
            audit(connection, "password_reset_completed", "success", body.username)
        # Never hold a DB write transaction while acquiring either app lock.
        # Download redemption independently checks the now-revoked session.
        purge_tickets(body.username)
        purge_presence(body.username)
        return {"status": "password_reset"}
