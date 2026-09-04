from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from dotenv import dotenv_values, set_key, unset_key

from .db import Database
from .security import digest, new_secret
from .settings import (
    LOCAL_API_HOSTS,
    PROJECT_DIR,
    Settings,
    account_usernames,
    api_origin,
    url_origin,
)


class AdminSelectionRequired(ValueError):
    """More than one activated account requires a private TTY selection."""


POSITION_NAMES = (
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
)


def account_at_position(accounts: tuple[str, ...], position: str) -> str:
    """Resolve a private allowlist position without reflecting invalid input."""
    normalized = position.strip().lower()
    try:
        index = POSITION_NAMES.index(normalized)
    except ValueError:
        try:
            number = int(normalized, 10)
        except ValueError:
            raise ValueError("Account position must be a number from 1 to 10") from None
        index = number - 1
    if not 0 <= index < len(accounts):
        raise ValueError("Account position is outside the configured account list")
    return accounts[index]


def _ensure_private_env(env_path: Path) -> None:
    if env_path.is_symlink():
        raise OSError("The private environment file must not be a symbolic link")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.touch(exist_ok=True, mode=0o600)
    env_path.chmod(0o600)


def _set_private_env_key(env_path: Path, key: str, value: str) -> None:
    result = set_key(str(env_path), key, value)
    if not result[0]:
        raise OSError("Could not update the private environment file")
    # python-dotenv rewrites through a temporary file; enforce the final mode.
    env_path.chmod(0o600)


def update_private_env(env_path: Path, site_origin: str) -> None:
    _ensure_private_env(env_path)
    _set_private_env_key(env_path, "SITE_ORIGINS", site_origin)
    # Older builds stored the ephemeral tunnel here even though the API never
    # consumed it. Remove that misleading coupling; devices receive the live
    # address separately from the invitation link.
    if any(
        line.partition("=")[0].strip().removeprefix("export ").strip() == "API_URL"
        for line in env_path.read_text(encoding="utf-8").splitlines()
    ):
        unset_key(str(env_path), "API_URL")
    env_path.chmod(0o600)


def configure_admin(
    database: Database,
    env_path: Path,
    *,
    selected_username: str | None = None,
) -> None:
    """Select an activated account without echoing its private ID."""
    with database.connect() as connection:
        activated = connection.execute(
            "SELECT username FROM users WHERE password_hash IS NOT NULL ORDER BY username"
        ).fetchall()
    activated_usernames = tuple(row["username"] for row in activated)
    if selected_username is None:
        if not activated_usernames:
            raise ValueError("Administrator setup requires one activated account")
        if len(activated_usernames) > 1:
            raise AdminSelectionRequired(
                "Administrator setup requires a hidden account selection when "
                "more than one account is activated"
            )
        username = activated_usernames[0]
    else:
        username = selected_username.strip()
        if username not in activated_usernames:
            raise ValueError("The selected administrator must be an activated configured account")
    if username not in database.accounts:
        raise RuntimeError("The activated administrator is not a configured account")
    _ensure_private_env(env_path)
    _set_private_env_key(env_path, "ADMIN_USERNAME", username)


def add_account(
    database: Database,
    env_path: Path,
    *,
    selected_username: str,
) -> None:
    """Add exactly one inactive account and extend the private allowlist.

    Normal startup still requires an exact set match between the environment
    and SQLite.  This deliberately narrow maintenance path is the only place
    that may grow both sets together.
    """
    username = selected_username.strip()
    candidate_accounts = (*database.accounts, username)
    try:
        # This validates the ID, distinctness, normalization, and upper bound
        # without ever including a rejected value in an exception.
        validated_accounts = account_usernames(",".join(candidate_accounts))
    except ValueError:
        raise ValueError(
            "The new account ID must be a distinct normalized lowercase ID, "
            "and the deployment may contain at most 10 accounts"
        ) from None

    _ensure_private_env(env_path)
    original_env = env_path.read_bytes()
    try:
        private_accounts = account_usernames(
            dotenv_values(env_path, interpolate=False).get("ACCOUNT_USERNAMES")
        )
    except (ValueError, UnicodeError):
        raise RuntimeError(
            "The private environment account list is missing or invalid"
        ) from None
    if private_accounts != database.accounts:
        raise RuntimeError(
            "The private environment and database account lists do not match"
        )

    env_updated = False
    try:
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_accounts, _ = database._inspect_users(connection)
            if existing_accounts != set(database.accounts):
                raise RuntimeError(
                    "Configured account IDs do not match the existing database"
                )
            connection.execute(
                "INSERT INTO users(username, password_hash, setup_hash, setup_expires) "
                "VALUES (?, NULL, NULL, NULL)",
                (username,),
            )
            env_updated = True
            _set_private_env_key(
                env_path,
                "ACCOUNT_USERNAMES",
                ",".join(validated_accounts),
            )
    except BaseException:
        if env_updated:
            try:
                env_path.write_bytes(original_env)
                env_path.chmod(0o600)
            except OSError as restore_error:
                raise RuntimeError(
                    "Account setup could not restore the private environment; "
                    "manual review is required before restarting"
                ) from restore_error
        raise
    database.accounts = validated_accounts


def account_status_lines(database: Database, *, now: float | None = None) -> tuple[str, ...]:
    """Return positional activation states without disclosing account IDs."""
    current_time = time.time() if now is None else now
    placeholders = ", ".join("?" for _ in database.accounts)
    with database.connect() as connection:
        account_rows = connection.execute(
            "SELECT username, password_hash IS NOT NULL AS active, "
            "setup_hash IS NOT NULL AS invited, setup_expires FROM users "
            f"WHERE username IN ({placeholders})",
            database.accounts,
        ).fetchall()
    accounts = {row["username"]: row for row in account_rows}
    if set(accounts) != set(database.accounts):
        raise RuntimeError("Account status does not match the configured allowlist")
    lines = []
    for position, username in enumerate(database.accounts, start=1):
        account = accounts[username]
        invite_state = "none"
        if account["invited"]:
            invite_state = (
                "valid" if (account["setup_expires"] or 0) > current_time else "expired"
            )
        lines.append(
            f"account-{position}: active={bool(account['active'])}, invite={invite_state}"
        )
    return tuple(lines)


def create_invitations(
    database: Database,
    site_url: str,
    api_url: str,
    output_path: Path,
) -> int:
    url_origin(site_url)
    validated_api_origin = api_origin(api_url)
    site = urlsplit(site_url)
    api = urlsplit(validated_api_origin)
    if site.query or site.fragment:
        raise ValueError("Site URL must not include a query or fragment")
    if api.hostname in LOCAL_API_HOSTS and site.hostname not in LOCAL_API_HOSTS:
        raise ValueError("A loopback API URL is allowed only with a local development site")
    site_url = urlunsplit((site.scheme, site.netloc, site.path or "/", "", ""))
    if not site_url.endswith("/") and not site.path.endswith(".html"):
        site_url += "/"
    api_url = validated_api_origin
    expires = time.time() + 7 * 86400
    lines = [
        "개인 초대 링크 · 7일 동안 한 번만 사용 가능",
        "각 사용자에게 본인 링크만 직접 전달하세요. 비밀번호는 링크를 받은 사람이 설정합니다.",
        "보안을 위해 초대 링크는 서버 주소를 포함하지 않습니다. Pages가 현재 주소를 자동으로 찾으므로",
        "자동 연결 표시를 확인한 뒤 본인 초대 링크를 열게 하세요. 아래 주소는 관리자 확인용입니다.",
        f"현재 서버 주소: {api_url}",
        "이 파일과 server/.env, .data 폴더를 GitHub에 올리지 마세요.",
        "",
    ]
    count = 0
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for username in database.accounts:
            account = connection.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
            if account["password_hash"]:
                lines.append(f"{username}: 이미 활성화됨 (현재 비밀번호 유지)")
                continue
            code = new_secret()
            connection.execute(
                "UPDATE users SET setup_hash = ?, setup_expires = ? WHERE username = ? AND password_hash IS NULL",
                (digest(code), expires, username),
            )
            # Do not put an API origin in the fragment. A forged Pages-looking
            # link could otherwise redirect the setup code and new password to
            # an attacker's own Quick Tunnel.
            fragment = urlencode({"username": username, "setup_code": code})
            lines.extend([username, f"{site_url}#{fragment}", ""])
            count += 1
        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_path.parent.chmod(0o700)
        output_path.touch(exist_ok=True, mode=0o600)
        output_path.chmod(0o600)
        # Never print tokens or invitation URLs to terminal output.
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        output_path.chmod(0o600)
    return count


def main():
    parser = argparse.ArgumentParser(description="Prepare private classroom accounts.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    initialize = subcommands.add_parser("init", help="Create fresh invitations for accounts not yet activated")
    initialize.add_argument("--site-url", required=True, help="Full HTTPS GitHub Pages site URL")
    initialize.add_argument("--api-url", required=True, help="Public HTTPS address of this PC's API")
    subcommands.add_parser("status", help="Show account activation state without revealing secrets")
    configure = subcommands.add_parser(
        "configure-admin",
        help="Privately select an activated account as administrator",
    )
    configure.add_argument(
        "--position",
        help=(
            "Select by private ACCOUNT_USERNAMES position (1-10 or first-tenth) "
            "without putting an ID in shell history"
        ),
    )
    subcommands.add_parser(
        "add-account",
        help="Privately add one inactive account (the ID is entered with echo disabled)",
    )
    arguments = parser.parse_args()
    try:
        settings = Settings.from_env()
        database = Database(settings.database_path, settings.accounts)
        database.initialize()
        if arguments.command == "add-account":
            if not sys.stdin.isatty():
                raise ValueError(
                    "Account addition requires an interactive WSL terminal so the new ID "
                    "can be entered with echo disabled"
                )
            selected = getpass.getpass("새 계정 ID (입력 내용은 보이지 않음): ")
            add_account(
                database,
                PROJECT_DIR / "server" / ".env",
                selected_username=selected,
            )
            print(
                "Added one inactive account to private server/.env and SQLite. "
                "Create its one-time invitation, then restart the local server."
            )
            return
        if arguments.command == "configure-admin":
            if arguments.position:
                selected = account_at_position(settings.accounts, arguments.position)
                configure_admin(
                    database,
                    PROJECT_DIR / "server" / ".env",
                    selected_username=selected,
                )
            else:
                try:
                    configure_admin(database, PROJECT_DIR / "server" / ".env")
                except AdminSelectionRequired:
                    if not sys.stdin.isatty():
                        raise ValueError(
                            "Multiple accounts are activated; run this command in an interactive "
                            "WSL terminal to select the administrator without echo, or pass "
                            "--position with its private ACCOUNT_USERNAMES position"
                        ) from None
                    selected = getpass.getpass("관리자로 지정할 활성 계정 ID (입력 내용은 보이지 않음): ")
                    configure_admin(
                        database,
                        PROJECT_DIR / "server" / ".env",
                        selected_username=selected,
                    )
            print("Configured the administrator in private server/.env. Restart the local server to apply it.")
            return
        if arguments.command == "status":
            for line in account_status_lines(database):
                print(line)
            return
        origin = url_origin(arguments.site_url)
        validated_api_origin = api_origin(arguments.api_url)
        count = create_invitations(
            database,
            arguments.site_url,
            validated_api_origin,
            settings.data_dir / "invitations.txt",
        )
        env_path = PROJECT_DIR / "server" / ".env"
        update_private_env(env_path, origin)
    except (ValueError, OSError, RuntimeError) as error:
        parser.error(str(error))
    print(f"Prepared {count} unused account invitation(s). Open .data/invitations.txt locally.")
    print("Updated server/.env. Restart the local server to apply the site origin.")


if __name__ == "__main__":
    main()
