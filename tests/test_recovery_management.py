from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from server.db import Database
from server.recovery_management import create_password_reset_file
from server.security import digest
from server.settings import Settings


class RecoveryManagementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "data" / "classroom.sqlite3", ("user-alpha", "user-beta"))
        self.database.initialize()
        self.output = self.database.path.parent / "private-reset.txt"
        with self.database.connect() as connection:
            connection.execute("UPDATE users SET password_hash='test-only-existing-hash', setup_hash='old-setup', setup_expires=?", (time.time()+500,))
            connection.execute("INSERT INTO sessions VALUES ('existing-session','user-alpha',?,?)", (time.time()+3600,time.time()))
            connection.execute("INSERT INTO lectures(id,username,title,language,created_at) VALUES ('test-lecture','user-alpha','test title','ko','2026-09-08T00:00:00Z')")

    def issue(self, **updates):
        values = dict(selected_username="user-alpha", site_url="https://student.github.io/classroom/",
                      allowed_origins=("https://student.github.io",), output_path=self.output)
        values.update(updates)
        return create_password_reset_file(self.database, **values)

    def rows(self, table):
        with self.database.connect() as connection:
            return [tuple(row) for row in connection.execute('SELECT * FROM "'+table+'"')]

    def test_local_operator_can_issue_admin_recovery_without_changing_credentials_or_data(self):
        before = {table:self.rows(table) for table in ("users", "sessions", "lectures")}
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = self.issue()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(set(result), {"output_path", "expires_at"})
        if os.name == "nt":
            from server.platform_files import validate_private_path
            validate_private_path(self.output)
        else:
            self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        link = next(line for line in self.output.read_text(encoding="utf-8").splitlines() if line.startswith("https://"))
        url = urlsplit(link); values = parse_qs(url.fragment)
        self.assertEqual((url.scheme, url.netloc, url.path, url.query), ("https", "student.github.io", "/classroom/", ""))
        self.assertEqual(set(values), {"username", "reset_code"})
        self.assertEqual(values["username"], ["user-alpha"])
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM account_password_resets WHERE username='user-alpha'").fetchone()
        self.assertEqual(row["token_hash"], digest(values["reset_code"][0]))
        self.assertNotIn(values["reset_code"][0], str(dict(row)))
        self.assertGreater(result["expires_at"], time.time()+1700)
        self.assertLessEqual(result["expires_at"], time.time()+1800)
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows)
        self.assertEqual(self.rows("admin_audit")[-1][2:4], ("password_reset_issued", "success"))

    def test_existing_output_refuses_without_rotating_current_reset(self):
        self.issue()
        token_rows, contents = self.rows("account_password_resets"), self.output.read_bytes()
        with self.assertRaises(ValueError):
            self.issue()
        self.assertEqual(self.rows("account_password_resets"), token_rows)
        self.assertEqual(self.output.read_bytes(), contents)

    def test_bad_site_and_output_paths_do_not_issue_tokens(self):
        outside = self.root / "outside.txt"
        for updates in (
            {"site_url":"https://attacker.invalid/"},
            {"site_url":"https://student.github.io/classroom/?api=https://attacker.invalid"},
            {"site_url":"https://student.github.io/#reset_code=untrusted"},
            {"site_url":"https://user:password@student.github.io/classroom/"},
            {"output_path":outside},
            {"selected_username":"not-configured"},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.issue(**updates)
        self.assertEqual(self.rows("account_password_resets"), [])

    def test_symlink_output_does_not_issue_tokens(self):
        outside = self.root / "outside.txt"
        try:
            self.output.symlink_to(outside)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege is unavailable")
            raise
        with self.assertRaises(ValueError):
            self.issue()
        self.assertFalse(outside.exists())
        self.assertEqual(self.rows("account_password_resets"), [])

    def test_old_schema_is_not_migrated_and_does_not_create_file(self):
        with self.database.connect() as connection:
            connection.execute("DROP TABLE account_password_resets")
            connection.execute("PRAGMA user_version=18")
        before = self.rows("users")
        with self.assertRaisesRegex(ValueError, "Apply the recovery server update"):
            self.issue()
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='account_password_resets'").fetchone())
        self.assertFalse(self.output.exists())
        self.assertEqual(self.rows("users"), before)

    def test_missing_database_is_not_created(self):
        missing = Database(self.root / "missing" / "database.sqlite3", self.database.accounts)
        with self.assertRaises(ValueError):
            create_password_reset_file(missing, selected_username="user-alpha", site_url="https://student.github.io/",
                                       allowed_origins=("https://student.github.io",), output_path=missing.path.parent/"reset.txt")
        self.assertFalse(missing.path.exists())

    def test_account_mismatch_does_not_modify_existing_database(self):
        self.database.accounts = ("user-alpha", "user-gamma")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.issue()
        self.assertEqual(self.rows("account_password_resets"), [])
        self.assertFalse(self.output.exists())

    def test_write_failure_rolls_back_previous_reset_and_removes_own_incomplete_file(self):
        self.issue()
        previous = self.rows("account_password_resets")
        next_output = self.output.with_name("new-private-reset.txt")
        with mock.patch("server.recovery_management.os.fsync", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.issue(output_path=next_output)
        self.assertEqual(self.rows("account_password_resets"), previous)
        self.assertFalse(next_output.exists())
        self.assertTrue(self.output.exists())

    def test_unactivated_account_cannot_replace_its_invitation(self):
        from server.account_recovery import RecoveryError
        with self.database.connect() as connection:
            connection.execute("UPDATE users SET password_hash=NULL WHERE username='user-beta'")
        previous = self.rows("users")
        with self.assertRaises(RecoveryError):
            self.issue(selected_username="user-beta")
        self.assertEqual(self.rows("users"), previous)
        self.assertFalse(self.output.exists())

    def test_cli_uses_private_file_and_never_initializes_or_prints_credentials(self):
        from server import manage
        settings = Settings(data_dir=self.database.path.parent, model_cache_dir=self.root/"models",
                            accounts=self.database.accounts, admin_username="user-alpha",
                            site_origins=("https://student.github.io",))
        stdout = io.StringIO()
        with mock.patch("sys.argv", ["manage", "issue-password-reset", "--position", "first", "--site-url", "https://student.github.io/classroom/"]), \
             mock.patch("server.manage.Settings.from_env", return_value=settings), \
             mock.patch.object(Database, "initialize", side_effect=AssertionError("CLI must never migrate")), \
             contextlib.redirect_stdout(stdout):
            manage.main()
        output = stdout.getvalue()
        self.assertNotIn("user-alpha", output)
        self.assertNotIn("test-only-existing-hash", output)
        self.assertNotIn("https://", output)
        self.assertNotIn("reset_code=", output)
        self.assertEqual(len(list(settings.data_dir.glob("password-reset-*.txt"))), 1)


if __name__ == "__main__":
    unittest.main()
