from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from server.db import Database

TEST_ACCOUNTS = ("user-alpha", "user-beta")


class DatabaseTests(unittest.TestCase):
    def test_pre_fingerprint_import_table_migrates_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data" / "classroom.sqlite3"
            path.parent.mkdir()
            with sqlite3.connect(path) as connection:
                connection.execute("""
                    CREATE TABLE imports (
                        id TEXT PRIMARY KEY, username TEXT NOT NULL, lecture_id TEXT,
                        title TEXT NOT NULL, language TEXT, filename TEXT NOT NULL,
                        total_bytes INTEGER NOT NULL, uploaded_bytes INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                        processed_seconds REAL NOT NULL DEFAULT 0, duration_seconds REAL,
                        error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    )
                """)
                connection.execute(
                    "INSERT INTO imports VALUES "
                    "('11111111-1111-4111-8111-111111111111', 'user-alpha', NULL, 'old', 'ko', "
                    "'old.wav', 1, 0, 'failed', 0, 0, NULL, NULL, '2026-01-01Z', '2026-01-01Z')"
                )
            Database(path, TEST_ACCOUNTS).initialize()
            with sqlite3.connect(path) as connection:
                row = connection.execute(
                    "SELECT file_fingerprint, raw_deleted FROM imports "
                    "WHERE id = '11111111-1111-4111-8111-111111111111'"
                ).fetchone()
            self.assertEqual(row, ("0" * 64, 0))

    def test_legacy_chunks_migrate_as_final_and_keep_retry_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "classroom.sqlite3"
            path.parent.mkdir()
            lecture_id = str(uuid.uuid4())
            chunk_id = str(uuid.uuid4())
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                    CREATE TABLE users (
                        username TEXT PRIMARY KEY,
                        password_hash TEXT,
                        setup_hash TEXT,
                        setup_expires REAL
                    );
                    CREATE TABLE lectures (
                        id TEXT PRIMARY KEY,
                        username TEXT NOT NULL REFERENCES users(username),
                        title TEXT NOT NULL,
                        language TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE chunks (
                        lecture_id TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
                        chunk_id TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        start_seconds REAL NOT NULL,
                        status TEXT NOT NULL,
                        processing_seconds REAL,
                        PRIMARY KEY (lecture_id, chunk_id)
                    );
                """)
                connection.execute("INSERT INTO users(username) VALUES ('user-alpha')")
                connection.execute("INSERT INTO users(username) VALUES ('user-beta')")
                connection.execute(
                    "INSERT INTO lectures VALUES (?, 'user-alpha', 'legacy', 'ko', '2026-01-01T00:00:00Z')",
                    (lecture_id,),
                )
                connection.execute(
                    "INSERT INTO chunks VALUES (?, ?, 'payload', 8.0, 'done', 1.25)",
                    (lecture_id, chunk_id),
                )

            database = Database(path, TEST_ACCOUNTS)
            database.initialize()
            with database.connect() as connection:
                migrated = connection.execute(
                    "SELECT overlap_seconds, final_chunk FROM chunks WHERE lecture_id = ? AND chunk_id = ?",
                    (lecture_id, chunk_id),
                ).fetchone()
            self.assertEqual(dict(migrated), {"overlap_seconds": 0.0, "final_chunk": 1})
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_reinitializing_does_not_change_a_nonfinal_chunk(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            lecture_id = str(uuid.uuid4())
            chunk_id = str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO lectures VALUES (?, 'user-alpha', 'current', 'ko', '2026-01-01T00:00:00Z')",
                    (lecture_id,),
                )
                connection.execute(
                    "INSERT INTO chunks(lecture_id, chunk_id, payload_hash, start_seconds, overlap_seconds, "
                    "final_chunk, status) VALUES (?, ?, 'payload', 0, 0, 0, 'done')",
                    (lecture_id, chunk_id),
                )
            database.initialize()
            with database.connect() as connection:
                final_chunk = connection.execute(
                    "SELECT final_chunk FROM chunks WHERE lecture_id = ? AND chunk_id = ?",
                    (lecture_id, chunk_id),
                ).fetchone()[0]
            self.assertEqual(final_chunk, 0)

    def test_accounts_are_data_not_hardcoded_in_the_users_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            with database.connect() as connection:
                accounts = tuple(
                    row[0] for row in connection.execute("SELECT username FROM users ORDER BY username")
                )
                schema = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
                ).fetchone()[0]
            self.assertEqual(accounts, TEST_ACCOUNTS)
            for account in TEST_ACCOUNTS:
                self.assertNotIn(account, schema)

    def test_legacy_account_check_is_removed_without_losing_private_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data" / "classroom.sqlite3"
            path.parent.mkdir()
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                    CREATE TABLE users (
                        username TEXT PRIMARY KEY
                            CHECK (username IN ('user-alpha', 'user-beta')),
                        password_hash TEXT,
                        setup_hash TEXT,
                        setup_expires REAL
                    );
                    CREATE TABLE sessions (
                        token_hash TEXT PRIMARY KEY,
                        username TEXT NOT NULL REFERENCES users(username),
                        expires_at REAL NOT NULL,
                        created_at REAL NOT NULL
                    );
                    CREATE TABLE lectures (
                        id TEXT PRIMARY KEY,
                        username TEXT NOT NULL REFERENCES users(username),
                        title TEXT NOT NULL,
                        language TEXT,
                        created_at TEXT NOT NULL
                    );
                """)
                connection.execute(
                    "INSERT INTO users VALUES ('user-alpha', 'password-hash', NULL, NULL)"
                )
                connection.execute(
                    "INSERT INTO users VALUES ('user-beta', NULL, 'setup-hash', 4102444800)"
                )
                connection.execute(
                    "INSERT INTO sessions VALUES ('token-hash', 'user-alpha', 4102444800, 1)"
                )
                connection.execute(
                    "INSERT INTO lectures VALUES "
                    "('lesson-id', 'user-alpha', 'private title', 'ko', '2026-01-01T00:00:00Z')"
                )

            database = Database(path, TEST_ACCOUNTS)
            database.initialize()
            database.initialize()
            with database.connect() as connection:
                users = [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT username, password_hash, setup_hash, setup_expires "
                        "FROM users ORDER BY username"
                    )
                ]
                session = tuple(connection.execute("SELECT * FROM sessions").fetchone())
                lecture = tuple(connection.execute("SELECT * FROM lectures").fetchone())
                schema = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
                ).fetchone()[0]
                foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(
                users,
                [
                    ("user-alpha", "password-hash", None, None),
                    ("user-beta", None, "setup-hash", 4102444800.0),
                ],
            )
            self.assertEqual(session, ("token-hash", "user-alpha", 4102444800.0, 1.0))
            self.assertEqual(
                lecture,
                ("lesson-id", "user-alpha", "private title", "ko", "2026-01-01T00:00:00Z"),
            )
            self.assertNotIn("CHECK", schema.upper())
            self.assertEqual(foreign_key_errors, [])

    def test_existing_database_rejects_a_different_account_configuration_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data" / "classroom.sqlite3"
            original = Database(path, ("legacy-one", "legacy-two"))
            original.initialize()
            before = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "do not match"):
                Database(path, TEST_ACCOUNTS).initialize()
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
