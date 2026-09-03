from __future__ import annotations

import argparse
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from dotenv import set_key, unset_key

from .db import Database
from .security import digest, new_secret
from .settings import LOCAL_API_HOSTS, PROJECT_DIR, Settings, api_origin, url_origin


def update_private_env(env_path: Path, site_origin: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.touch(exist_ok=True, mode=0o600)
    env_path.chmod(0o600)
    set_key(str(env_path), "SITE_ORIGINS", site_origin)
    # Older builds stored the ephemeral tunnel here even though the API never
    # consumed it. Remove that misleading coupling; devices receive the live
    # address separately from the invitation link.
    if any(
        line.partition("=")[0].strip().removeprefix("export ").strip() == "API_URL"
        for line in env_path.read_text(encoding="utf-8").splitlines()
    ):
        unset_key(str(env_path), "API_URL")
    # python-dotenv rewrites through a temporary file; enforce the final mode.
    env_path.chmod(0o600)


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
        "보안을 위해 초대 링크는 서버 주소를 포함하지 않습니다. 아래 서버 주소를 별도로 전달하고,",
        "받는 사람이 Pages의 '내 서버'에 먼저 입력한 다음 본인 초대 링크를 열게 하세요.",
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
    parser = argparse.ArgumentParser(description="Prepare the two private classroom accounts.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    initialize = subcommands.add_parser("init", help="Create fresh invitations for accounts not yet activated")
    initialize.add_argument("--site-url", required=True, help="Full HTTPS GitHub Pages site URL")
    initialize.add_argument("--api-url", required=True, help="Public HTTPS address of this PC's API")
    subcommands.add_parser("status", help="Show account activation state without revealing secrets")
    arguments = parser.parse_args()
    try:
        settings = Settings.from_env()
        database = Database(settings.database_path, settings.accounts)
        database.initialize()
        if arguments.command == "status":
            now = time.time()
            with database.connect() as connection:
                accounts = connection.execute(
                    "SELECT username, password_hash IS NOT NULL AS active, "
                    "setup_hash IS NOT NULL AS invited, setup_expires FROM users "
                    "WHERE username IN (?, ?) ORDER BY username",
                    settings.accounts,
                ).fetchall()
            for account in accounts:
                invite_state = "none"
                if account["invited"]:
                    invite_state = "valid" if (account["setup_expires"] or 0) > now else "expired"
                print(f"{account['username']}: active={bool(account['active'])}, invite={invite_state}")
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
