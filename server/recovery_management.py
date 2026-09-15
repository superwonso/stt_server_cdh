"""Explicit local-operator recovery; never initialize or migrate a database.

OS access to the private database is the authority for this emergency path,
including an administrator who cannot log in. Web issuance uses its separate
administrator reauthentication boundary. Tokens are never printed or returned.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from .db import Database
from .platform_files import open_file, validate_private_path
from .settings import url_origin


def create_password_reset_file(
    database: Database,
    *,
    selected_username: str,
    site_url: str,
    allowed_origins: tuple[str, ...],
    output_path: Path,
) -> dict:
    """Issue one reset and an exclusive 0600 file in the existing private dir.

    All existing sessions/passwords/lessons remain unchanged. A previous reset
    for this account is replaced only if the file write and DB commit succeed.
    This does not enable the feature in an older, currently running API process.
    """
    from .account_recovery import issue_password_reset

    if selected_username not in database.accounts:
        raise ValueError("Select a configured account")
    origin = url_origin(site_url)
    parsed = urlsplit(site_url)
    if origin not in allowed_origins or parsed.query or parsed.fragment:
        raise ValueError("Use the configured website origin without query or fragment")
    site = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))
    if not parsed.path.endswith(".html") and not site.endswith("/"):
        site += "/"
    if not database.path.is_file() or database.path.is_symlink():
        raise ValueError("An existing private database is required")
    root = database.path.parent
    if (root.is_symlink() or root.absolute() != root.resolve()
            or output_path.parent.absolute() != root.resolve()
            or output_path.is_symlink() or output_path.exists()):
        raise ValueError("Use a new file directly inside the private data directory")

    created = False
    try:
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Do not migrate a live v18 database as a side effect of invoking a
            # local recovery command before the requested deployment/restart.
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version < 19:
                raise ValueError("Apply the recovery server update before using this command")
            configured, _ = database._inspect_users(connection)
            if configured != set(database.accounts):
                raise ValueError("Private account configuration does not match the database")
            if os.name == "nt":
                validate_private_path(root, directory=True)
                descriptor = open_file(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, private=True)
            else:
                descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                reset = issue_password_reset(connection, selected_username)
                fragment = urlencode({"username": selected_username, "reset_code": reset["reset_code"]})
                expires = datetime.fromtimestamp(reset["expires_at"], timezone.utc).isoformat()
                output.write(
                    "개인 비밀번호 복구 링크 · 30분 동안 한 번만 사용 가능\n"
                    "기존 연락 수단으로 본인을 확인한 뒤 본인에게만 전달하세요.\n"
                    "발급만으로 기존 비밀번호나 로그인이 바뀌지는 않습니다.\n"
                    "재설정 완료 시 기존 로그인은 해제되며 수업과 녹음은 보존됩니다.\n"
                    "이 파일과 링크를 GitHub, 공개 문서, 단체 대화방에 올리지 마세요.\n"
                    f"유효 기한(UTC): {expires}\n\n{site}#{fragment}\n"
                )
                output.flush()
                os.fsync(output.fileno())
            connection.execute(
                "INSERT INTO admin_audit(timestamp, action, result, target) VALUES (?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                 "password_reset_issued", "success", selected_username),
            )
            connection.execute(
                "DELETE FROM admin_audit WHERE id NOT IN "
                "(SELECT id FROM admin_audit ORDER BY timestamp DESC, id DESC LIMIT 500)"
            )
    except BaseException:
        if created:
            output_path.unlink(missing_ok=True)
        raise
    return {"output_path": str(output_path), "expires_at": reset["expires_at"]}
