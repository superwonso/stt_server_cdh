from __future__ import annotations

import unittest
import tempfile
from unittest import mock
from pathlib import Path

from dotenv import dotenv_values

from server.db import Database
from server.manage import (
    account_at_position,
    account_status_lines,
    add_account,
    configure_admin,
    configure_clova,
    update_private_env,
)
from server.settings import (
    CLOVA_SPEECH_GRPC_TARGET,
    PROJECT_DIR,
    Settings,
    account_usernames,
    api_origin,
    mindlogic_gateway_base_url,
    url_origin,
)


class UrlValidationTests(unittest.TestCase):
    def test_private_account_allowlist_is_parsed_and_normalized(self):
        self.assertEqual(
            account_usernames(" user-alpha , user-beta "),
            ("user-alpha", "user-beta"),
        )
        self.assertEqual(
            account_usernames(" user-alpha , user-beta, user-gamma "),
            ("user-alpha", "user-beta", "user-gamma"),
        )
        ten_accounts = tuple(f"private-{position}" for position in range(10))
        self.assertEqual(account_usernames(",".join(ten_accounts)), ten_accounts)

    def test_invalid_account_configuration_fails_without_reflecting_values(self):
        invalid = [
            None,
            "",
            "only-one",
            "one,,two",
            "one,two,",
            "same,same",
            "Uppercase,valid",
            "../escape,valid",
            ".hidden,valid",
            "a" * 33 + ",valid",
            ",".join(f"private-{position}" for position in range(11)),
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                account_usernames(value)
            if value:
                self.assertNotIn(value, str(raised.exception))

    def test_public_env_example_does_not_contain_working_account_ids(self):
        content = (PROJECT_DIR / "server" / "env.example").read_text(encoding="utf-8")
        account_line = next(line for line in content.splitlines() if line.startswith("ACCOUNT_USERNAMES="))
        self.assertEqual(account_line, "ACCOUNT_USERNAMES=")
        admin_line = next(line for line in content.splitlines() if line.startswith("ADMIN_USERNAME="))
        self.assertEqual(admin_line, "ADMIN_USERNAME=")

    def test_runtime_configuration_requires_private_account_ids(self):
        with mock.patch("server.settings.load_dotenv"), mock.patch.dict(
            "os.environ", {}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "ACCOUNT_USERNAMES"):
                Settings.from_env()

    def test_quick_tunnel_and_loopback_api_origins_are_allowed(self):
        self.assertEqual(
            api_origin("https://gentle-classroom-voice.trycloudflare.com/"),
            "https://gentle-classroom-voice.trycloudflare.com",
        )
        self.assertEqual(api_origin("http://localhost:8765"), "http://localhost:8765")
        self.assertEqual(api_origin("https://127.0.0.1:8765/"), "https://127.0.0.1:8765")
        self.assertEqual(api_origin("http://[::1]:8765"), "http://[::1]:8765")

    def test_other_api_destinations_are_rejected(self):
        invalid = [
            "https://attacker.example",
            "https://trycloudflare.com",
            "https://nested.evil.trycloudflare.com",
            "https://safe.trycloudflare.com.evil.example",
            "http://safe.trycloudflare.com",
            "https://safe.trycloudflare.com:8443",
            "https://safe.trycloudflare.com/path",
            "https://safe.trycloudflare.com/?redirect=evil",
            "https://user:password@safe.trycloudflare.com",
            "https://safe.trycloudflare.com:invalid",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                api_origin(value)

    def test_pages_site_origin_remains_independent_of_api_restriction(self):
        self.assertEqual(
            url_origin("https://student.github.io/classroom/"),
            "https://student.github.io",
        )

    def test_managed_env_file_is_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path = Path(temporary) / "server" / ".env"
            env_path.parent.mkdir(parents=True)
            env_path.write_text(
                "API_URL='https://old.trycloudflare.com'\n"
                "ACCOUNT_USERNAMES='user-alpha,user-beta'\n",
                encoding="utf-8",
            )
            update_private_env(env_path, "https://student.github.io")
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            content = env_path.read_text(encoding="utf-8")
            self.assertIn("SITE_ORIGINS='https://student.github.io'", content)
            self.assertIn("ACCOUNT_USERNAMES='user-alpha,user-beta'", content)
            self.assertNotIn("API_URL", content)

    def test_admin_configuration_uses_only_activated_account_and_private_env(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "classroom.sqlite3", ("user-alpha", "user-beta"))
            database.initialize()
            env_path = root / "server" / ".env"
            with database.connect() as connection:
                connection.execute(
                    "UPDATE users SET password_hash = 'test-hash' WHERE username = 'user-beta'"
                )
            configure_admin(database, env_path)
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            self.assertIn("ADMIN_USERNAME='user-beta'", env_path.read_text(encoding="utf-8"))

            with database.connect() as connection:
                connection.execute(
                    "UPDATE users SET password_hash = 'test-hash' WHERE username = 'user-alpha'"
                )
            with self.assertRaisesRegex(ValueError, "hidden account selection"):
                configure_admin(database, env_path)
            configure_admin(database, env_path, selected_username="user-alpha")
            content = env_path.read_text(encoding="utf-8")
            self.assertIn("ADMIN_USERNAME='user-alpha'", content)
            self.assertNotIn("ADMIN_USERNAME='user-beta'", content)
            with self.assertRaisesRegex(ValueError, "activated configured account"):
                configure_admin(database, env_path, selected_username="not-an-account")
            with database.connect() as connection:
                connection.execute("UPDATE users SET password_hash = NULL")
            with self.assertRaisesRegex(ValueError, "requires one activated account"):
                configure_admin(database, env_path)

    def test_account_position_supports_all_configured_accounts_without_ids(self):
        accounts = ("user-alpha", "user-beta", "user-gamma")
        self.assertEqual(account_at_position(accounts, "first"), accounts[0])
        self.assertEqual(account_at_position(accounts, "3"), accounts[2])
        self.assertEqual(account_at_position(accounts, "third"), accounts[2])
        for invalid in ("0", "4", "eleventh", "private-secret"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError) as raised:
                account_at_position(accounts, invalid)
            self.assertNotIn(invalid, str(raised.exception))

    def test_add_account_updates_private_env_and_creates_only_an_inactive_user(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_accounts = ("user-alpha", "user-beta")
            expanded_accounts = (*original_accounts, "user-gamma")
            database = Database(root / "data" / "classroom.sqlite3", original_accounts)
            database.initialize()
            env_path = root / "server" / ".env"
            env_path.parent.mkdir(parents=True)
            env_path.write_text(
                "ACCOUNT_USERNAMES='user-alpha,user-beta'\n"
                "ADMIN_USERNAME='user-alpha'\n"
                "MINDLOGIC_API_KEY='test-private-key'\n",
                encoding="utf-8",
            )
            with database.connect() as connection:
                connection.execute(
                    "UPDATE users SET password_hash = 'existing-password-hash' "
                    "WHERE username = 'user-alpha'"
                )
                connection.execute(
                    "UPDATE users SET setup_hash = 'existing-setup-hash', setup_expires = 12345 "
                    "WHERE username = 'user-beta'"
                )
                connection.execute(
                    "INSERT INTO sessions(token_hash, username, expires_at, created_at) "
                    "VALUES ('existing-session', 'user-alpha', 99999, 11111)"
                )
                connection.execute(
                    "INSERT INTO lectures(id, username, title, language, created_at) "
                    "VALUES ('existing-lecture', 'user-beta', 'private title', 'ko', "
                    "'2026-01-01T00:00:00Z')"
                )
                original_users = [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT username, password_hash, setup_hash, setup_expires "
                        "FROM users ORDER BY username"
                    )
                ]
                original_owned_data = (
                    tuple(
                        connection.execute(
                            "SELECT token_hash, username, expires_at, created_at FROM sessions"
                        ).fetchone()
                    ),
                    tuple(
                        connection.execute(
                            "SELECT id, username, title, language, created_at FROM lectures"
                        ).fetchone()
                    ),
                )
            add_account(database, env_path, selected_username="user-gamma")

            self.assertEqual(database.accounts, expanded_accounts)
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            private_env = dotenv_values(env_path, interpolate=False)
            self.assertEqual(
                private_env["ACCOUNT_USERNAMES"],
                "user-alpha,user-beta,user-gamma",
            )
            self.assertEqual(private_env["ADMIN_USERNAME"], "user-alpha")
            self.assertEqual(private_env["MINDLOGIC_API_KEY"], "test-private-key")
            with database.connect() as connection:
                users = [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT username, password_hash, setup_hash, setup_expires "
                        "FROM users ORDER BY username"
                    )
                ]
                preserved_owned_data = (
                    tuple(
                        connection.execute(
                            "SELECT token_hash, username, expires_at, created_at FROM sessions"
                        ).fetchone()
                    ),
                    tuple(
                        connection.execute(
                            "SELECT id, username, title, language, created_at FROM lectures"
                        ).fetchone()
                    ),
                )
            self.assertEqual(users[:2], original_users)
            self.assertEqual(users[2], ("user-gamma", None, None, None))
            self.assertEqual(preserved_owned_data, original_owned_data)
            Database(database.path, expanded_accounts).initialize()
            with self.assertRaisesRegex(RuntimeError, "do not match"):
                Database(database.path, original_accounts).initialize()

    def test_add_account_rolls_back_sqlite_when_private_env_update_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_accounts = ("user-alpha", "user-beta")
            database = Database(root / "data" / "classroom.sqlite3", original_accounts)
            database.initialize()
            env_path = root / "server" / ".env"
            env_path.parent.mkdir(parents=True)
            original_env = b"ACCOUNT_USERNAMES='user-alpha,user-beta'\nPRIVATE='preserved'\n"
            env_path.write_bytes(original_env)

            with mock.patch(
                "server.manage._set_private_env_key",
                side_effect=OSError("simulated private write failure"),
            ):
                with self.assertRaises(OSError):
                    add_account(database, env_path, selected_username="user-gamma")

            self.assertEqual(database.accounts, original_accounts)
            self.assertEqual(env_path.read_bytes(), original_env)
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            with database.connect() as connection:
                users = tuple(
                    row[0] for row in connection.execute("SELECT username FROM users ORDER BY username")
                )
            self.assertEqual(users, original_accounts)

    def test_add_account_fails_closed_on_env_mismatch_and_invalid_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(
                root / "data" / "classroom.sqlite3",
                ("user-alpha", "user-beta"),
            )
            database.initialize()
            env_path = root / "server" / ".env"
            env_path.parent.mkdir(parents=True)
            env_path.write_text(
                "ACCOUNT_USERNAMES='user-beta,user-alpha'\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "do not match"):
                add_account(database, env_path, selected_username="user-gamma")
            with database.connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                    2,
                )

            invalid = "MUST-NOT-BE-REFLECTED"
            with self.assertRaises(ValueError) as raised:
                add_account(database, env_path, selected_username=invalid)
            self.assertNotIn(invalid, str(raised.exception))

    def test_three_account_status_uses_positions_and_never_prints_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            accounts = ("user-alpha", "user-beta", "user-gamma")
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", accounts)
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "UPDATE users SET password_hash = 'activated' WHERE username = ?",
                    (accounts[0],),
                )
                connection.execute(
                    "UPDATE users SET setup_hash = 'valid', setup_expires = 200 "
                    "WHERE username = ?",
                    (accounts[1],),
                )
                connection.execute(
                    "UPDATE users SET setup_hash = 'expired', setup_expires = 50 "
                    "WHERE username = ?",
                    (accounts[2],),
                )
            lines = account_status_lines(database, now=100)
            self.assertEqual(
                lines,
                (
                    "account-1: active=True, invite=none",
                    "account-2: active=False, invite=valid",
                    "account-3: active=False, invite=expired",
                ),
            )
            output = "\n".join(lines)
            for username in accounts:
                self.assertNotIn(username, output)

    def test_audio_body_limit_cannot_be_configured_below_the_static_part_contract(self):
        with mock.patch("server.settings.load_dotenv"), mock.patch.dict(
            "os.environ",
            {"MAX_UPLOAD_BYTES": "64000", "ACCOUNT_USERNAMES": "user-alpha,user-beta"},
            clear=True,
        ):
            self.assertEqual(Settings.from_env().max_upload_bytes, 480 * 1024)

    def test_mindlogic_bearer_is_pinned_to_the_official_gateway(self):
        self.assertEqual(
            mindlogic_gateway_base_url(
                "https://factchat-cloud.mindlogic.ai/v1/gateway/"
            ),
            "https://factchat-cloud.mindlogic.ai/v1/gateway",
        )
        invalid = [
            "http://factchat-cloud.mindlogic.ai/v1/gateway",
            "https://attacker.example/v1/gateway",
            "https://factchat-cloud.mindlogic.ai/v1/gateway/../other",
            "https://factchat-cloud.mindlogic.ai/v1/gateway?next=evil",
            "https://user:pass@factchat-cloud.mindlogic.ai/v1/gateway",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                mindlogic_gateway_base_url(value)

    def test_api_key_is_not_represented_and_public_example_keeps_it_blank(self):
        secret = "test-only-secret-that-must-not-appear"
        clova_secret = "test-only-clova-secret-that-must-not-appear"
        settings = Settings(
            data_dir=Path("/tmp/test-data"),
            model_cache_dir=Path("/tmp/test-models"),
            mindlogic_api_key=secret,
            clova_speech_secret_key=clova_secret,
            admin_username="user-alpha",
        )
        self.assertNotIn(secret, repr(settings))
        self.assertNotIn(clova_secret, repr(settings))
        self.assertNotIn("admin_username", repr(settings))
        content = (PROJECT_DIR / "server" / "env.example").read_text(encoding="utf-8")
        self.assertIn("MINDLOGIC_API_KEY=\n", content)
        self.assertIn("CLOVA_SPEECH_SECRET_KEY=\n", content)
        self.assertNotIn(clova_secret, content)

    def test_clova_secret_is_private_env_only_and_target_is_not_overridable(self):
        secret = "test-only-clova-domain-secret"
        with mock.patch("server.settings.load_dotenv"), mock.patch.dict(
            "os.environ",
            {
                "ACCOUNT_USERNAMES": "user-alpha,user-beta",
                "CLOVA_SPEECH_SECRET_KEY": secret,
                "CLOVA_STREAM_RESPONSE_TIMEOUT_SECONDS": "1",
                "CLOVA_STREAM_MAX_AGE_SECONDS": "999",
                "CLOVA_STREAM_IDLE_SECONDS": "2",
                "CLOVA_EPD_GAP_MS": "10",
                "CLOVA_EPD_DURATION_MS": "999999",
                "CLOVA_SPEECH_GRPC_TARGET": "attacker.example:443",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.clova_speech_secret_key, secret)
        self.assertEqual(settings.clova_stream_response_timeout_seconds, 10.0)
        self.assertEqual(settings.clova_stream_max_age_seconds, 270.0)
        self.assertEqual(settings.clova_stream_idle_seconds, 5.0)
        self.assertEqual(settings.clova_epd_gap_ms, 250)
        self.assertEqual(settings.clova_epd_duration_ms, 30_000)
        self.assertEqual(CLOVA_SPEECH_GRPC_TARGET, "clovaspeech-gw.ncloud.com:50051")
        self.assertNotIn(secret, repr(settings))
        self.assertNotIn("attacker.example", repr(settings))

    def test_invalid_clova_secret_fails_without_reflecting_it(self):
        invalid = "MUST NOT LEAK THIS PRIVATE VALUE"
        with self.assertRaises(ValueError) as raised:
            Settings(
                data_dir=Path("/tmp/test-data"),
                model_cache_dir=Path("/tmp/test-models"),
                clova_speech_secret_key=invalid,
            )
        self.assertNotIn(invalid, str(raised.exception))

    def test_configure_clova_preserves_private_env_and_round_trips_secret(self):
        secret = "test-only-valid-clova-domain-secret"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_path = root / "server" / ".env"
            env_path.parent.mkdir(parents=True)
            env_path.write_text(
                "ACCOUNT_USERNAMES='user-alpha,user-beta'\n"
                "ADMIN_USERNAME='user-alpha'\n"
                "MINDLOGIC_API_KEY='preserve-existing-private-value'\n",
                encoding="utf-8",
            )

            configure_clova(env_path, secret_key=secret)

            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            private_env = dotenv_values(env_path, interpolate=False)
            self.assertEqual(
                private_env,
                {
                    "ACCOUNT_USERNAMES": "user-alpha,user-beta",
                    "ADMIN_USERNAME": "user-alpha",
                    "MINDLOGIC_API_KEY": "preserve-existing-private-value",
                    "CLOVA_SPEECH_SECRET_KEY": secret,
                },
            )
            with mock.patch("server.settings.PROJECT_DIR", root), mock.patch.dict(
                "os.environ", {}, clear=True
            ):
                loaded = Settings.from_env()
            self.assertEqual(loaded.clova_speech_secret_key, secret)
            self.assertNotIn(secret, repr(loaded))

    def test_configure_clova_rejects_invalid_secret_without_file_or_error_leak(self):
        invalid_values = (
            "too-short",
            "private secret with spaces must never be reflected",
        )
        with tempfile.TemporaryDirectory() as temporary:
            env_path = Path(temporary) / "server" / ".env"
            env_path.parent.mkdir(parents=True)
            original = b"ACCOUNT_USERNAMES='user-alpha,user-beta'\nPRIVATE='preserved'\n"
            env_path.write_bytes(original)
            for invalid in invalid_values:
                with self.subTest(invalid=invalid), self.assertRaises(ValueError) as raised:
                    configure_clova(env_path, secret_key=invalid)
                self.assertNotIn(invalid, str(raised.exception))
                self.assertEqual(env_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
