from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from server.db import Database

TEST_ACCOUNTS = ("user-alpha", "user-beta")


class DatabaseTests(unittest.TestCase):
    def test_v11_adds_translation_without_rewriting_existing_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            lecture_id = str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute("INSERT INTO lectures(id,username,title,created_at) VALUES (?,?,'test-only','now')",
                                   (lecture_id, TEST_ACCOUNTS[0]))
                before = tuple(connection.execute("SELECT * FROM lectures WHERE id=?", (lecture_id,)).fetchone())
                connection.execute("DROP TABLE lecture_translations")
                connection.execute("PRAGMA user_version=11")
            database.initialize()
            with database.connect() as connection:
                self.assertEqual(tuple(connection.execute("SELECT * FROM lectures WHERE id=?", (lecture_id,)).fetchone()), before)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 12)
                connection.execute(
                    "INSERT INTO lecture_translations(lecture_id,job_id,raw_revision,status,model,created_at,updated_at) "
                    "VALUES (?,?,'revision','queued','test-model','now','now')", (lecture_id, str(uuid.uuid4())),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE lecture_translations SET status='completed' WHERE lecture_id=?", (lecture_id,))
                connection.execute("DELETE FROM lectures WHERE id=?", (lecture_id,))
                self.assertEqual(connection.execute("SELECT count(*) FROM lecture_translations").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v10_adds_summary_storage_without_changing_private_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            lecture_id = str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute("INSERT INTO lectures(id,username,title,created_at) VALUES (?,?,'private test','now')",
                                   (lecture_id, TEST_ACCOUNTS[0]))
                before = tuple(connection.execute("SELECT * FROM lectures WHERE id=?", (lecture_id,)).fetchone())
                connection.execute("DROP TABLE lecture_summaries")
                connection.execute("PRAGMA user_version=10")
            database.initialize()
            with database.connect() as connection:
                self.assertEqual(tuple(connection.execute("SELECT * FROM lectures WHERE id=?", (lecture_id,)).fetchone()), before)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 12)
                connection.execute(
                    "INSERT INTO lecture_summaries(lecture_id,job_id,raw_revision,status,model,created_at,updated_at) "
                    "VALUES (?,?,'revision','queued','test-model','now','now')", (lecture_id, str(uuid.uuid4())),
                )
                connection.execute("DELETE FROM lectures WHERE id=?", (lecture_id,))
                self.assertEqual(connection.execute("SELECT count(*) FROM lecture_summaries").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v9_adds_nullable_private_boundary_without_rewriting_existing_chunks(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            lecture_id, chunk_id = str(uuid.uuid4()), str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO lectures(id,username,title,created_at) VALUES (?,?,'test','2026-09-05Z')",
                    (lecture_id, TEST_ACCOUNTS[0]),
                )
                connection.execute(
                    "INSERT INTO chunks(lecture_id,chunk_id,payload_hash,start_seconds,status) "
                    "VALUES (?,?,'test-payload',0,'done')", (lecture_id, chunk_id),
                )
                connection.execute("ALTER TABLE chunks DROP COLUMN qwen_boundary_json")
                connection.execute("PRAGMA user_version = 9")
            database.initialize()
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT status,payload_hash,qwen_boundary_json FROM chunks "
                    "WHERE lecture_id=? AND chunk_id=?", (lecture_id, chunk_id),
                ).fetchone()
                self.assertEqual(tuple(row), ("done", "test-payload", None))
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 12)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE chunks SET qwen_boundary_json=?", ("x" * 65537,))

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
                    "SELECT c.overlap_seconds, c.final_chunk, l.recording_finalized, l.asr_provider "
                    "FROM chunks c JOIN lectures l ON l.id = c.lecture_id "
                    "WHERE c.lecture_id = ? AND c.chunk_id = ?",
                    (lecture_id, chunk_id),
                ).fetchone()
            self.assertEqual(
                dict(migrated),
                {
                    "overlap_seconds": 0.0,
                    "final_chunk": 1,
                    "recording_finalized": 1,
                    "asr_provider": "qwen",
                },
            )
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
                    "INSERT INTO lectures(id, username, title, language, created_at) "
                    "VALUES (?, 'user-alpha', 'current', 'ko', '2026-01-01T00:00:00Z')",
                    (lecture_id,),
                )
                connection.execute(
                    "INSERT INTO chunks(lecture_id, chunk_id, payload_hash, start_seconds, overlap_seconds, "
                    "final_chunk, status) VALUES (?, ?, 'payload', 0, 0, 0, 'done')",
                    (lecture_id, chunk_id),
                )
                connection.execute("PRAGMA user_version = 3")
            database.initialize()
            with database.connect() as connection:
                state = connection.execute(
                    "SELECT c.final_chunk, l.recording_finalized "
                    "FROM chunks c JOIN lectures l ON l.id = c.lecture_id "
                    "WHERE c.lecture_id = ? AND c.chunk_id = ?",
                    (lecture_id, chunk_id),
                ).fetchone()
                schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(tuple(state), (0, 0))
            self.assertEqual(schema_version, 12)

    def test_recording_archive_schema_keeps_remote_state_private_and_owned(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(
                Path(temporary) / "data" / "classroom.sqlite3",
                TEST_ACCOUNTS,
            )
            database.initialize()
            lecture_id = str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO lectures(id, username, title, language, created_at, recording_finalized) "
                    "VALUES (?, 'user-alpha', 'archive', 'ko', '2026-01-01T00:00:00Z', 1)",
                    (lecture_id,),
                )
                connection.execute(
                    "INSERT INTO recording_archives(lecture_id, state, object_key, updated_at) "
                    "VALUES (?, 'pending', ?, '2026-01-01T00:00:00Z')",
                    (lecture_id, "a" * 64),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE recording_archives SET state = 'ready' WHERE lecture_id = ?",
                        (lecture_id,),
                    )
            with database.connect() as connection:
                connection.execute("DELETE FROM lectures WHERE id = ?", (lecture_id,))
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM recording_archives WHERE lecture_id = ?",
                        (lecture_id,),
                    ).fetchone()
                )

    def test_drive_binding_is_singleton_and_opaque(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(
                Path(temporary) / "data" / "classroom.sqlite3",
                TEST_ACCOUNTS,
            )
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO drive_archive_binding"
                    "(singleton, binding_key, folder_id, updated_at) "
                    "VALUES (1, ?, 'opaqueFolder_1', '2026-01-01T00:00:00Z')",
                    ("a" * 64,),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO drive_archive_binding"
                        "(singleton, binding_key, folder_id, updated_at) "
                        "VALUES (2, ?, 'opaqueFolder_2', '2026-01-01T00:00:00Z')",
                        ("b" * 64,),
                    )
                row = connection.execute(
                    "SELECT binding_key, folder_id FROM drive_archive_binding"
                ).fetchone()
            self.assertEqual(tuple(row), ("a" * 64, "opaqueFolder_1"))

    def test_drive_user_folder_binding_is_owned_unique_and_opaque(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(
                Path(temporary) / "data" / "classroom.sqlite3",
                TEST_ACCOUNTS,
            )
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO drive_archive_user_folders"
                    "(username, folder_key, folder_id, updated_at) VALUES (?, ?, ?, ?)",
                    ("user-alpha", "a" * 64, "opaqueUserFolder_1", "2026-01-01T00:00:00Z"),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO drive_archive_user_folders"
                        "(username, folder_key, folder_id, updated_at) VALUES (?, ?, ?, ?)",
                        ("user-beta", "b" * 64, "opaqueUserFolder_1", "2026-01-01T00:00:00Z"),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO drive_archive_user_folders"
                        "(username, folder_key, folder_id, updated_at) VALUES (?, ?, ?, ?)",
                        ("not-configured", "c" * 64, "opaqueUserFolder_3", "2026-01-01T00:00:00Z"),
                    )

    def test_v8_ready_archive_upgrades_with_unconfirmed_folder_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data" / "classroom.sqlite3"
            database = Database(path, TEST_ACCOUNTS)
            database.initialize()
            lecture_id = str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO lectures(id, username, title, language, created_at, "
                    "recording_finalized) VALUES (?, 'user-alpha', 'archive', 'ko', "
                    "'2026-01-01T00:00:00Z', 1)",
                    (lecture_id,),
                )
                connection.execute("ALTER TABLE recording_archives RENAME TO archives_v9")
                connection.execute(
                    "CREATE TABLE recording_archives ("
                    "lecture_id TEXT PRIMARY KEY REFERENCES lectures(id) ON DELETE CASCADE, "
                    "state TEXT NOT NULL, object_key TEXT NOT NULL UNIQUE, "
                    "drive_file_id TEXT UNIQUE, upload_session_uri TEXT, source_bytes INTEGER, "
                    "source_sha256 TEXT, source_md5 TEXT, uploaded_bytes INTEGER NOT NULL DEFAULT 0, "
                    "local_deleted INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, "
                    "next_attempt_at REAL NOT NULL DEFAULT 0, last_error_code TEXT, updated_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO recording_archives"
                    "(lecture_id, state, object_key, drive_file_id, source_bytes, source_sha256, "
                    "source_md5, uploaded_bytes, local_deleted, updated_at) "
                    "VALUES (?, 'ready', ?, 'opaqueDriveFile_1', 44, ?, ?, 44, 0, ?)",
                    (
                        lecture_id,
                        "d" * 64,
                        "e" * 64,
                        "f" * 32,
                        "2026-01-01T00:01:00Z",
                    ),
                )
                connection.execute("DROP TABLE archives_v9")
                connection.execute("DROP TABLE drive_archive_user_folders")
                connection.execute("PRAGMA user_version = 8")

            database.initialize()

            with database.connect() as connection:
                row = connection.execute(
                    "SELECT state, drive_file_id, local_deleted, folder_layout_version "
                    "FROM recording_archives WHERE lecture_id = ?",
                    (lecture_id,),
                ).fetchone()
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                folders_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'drive_archive_user_folders'"
                ).fetchone()
            self.assertEqual(tuple(row), ("ready", "opaqueDriveFile_1", 0, 0))
            self.assertEqual(version, 12)
            self.assertIsNotNone(folders_table)

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

    def test_three_account_database_initializes_and_keeps_exact_set_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data" / "classroom.sqlite3"
            accounts = ("user-alpha", "user-beta", "user-gamma")
            Database(path, accounts).initialize()
            Database(path, accounts).initialize()
            with sqlite3.connect(path) as connection:
                users = tuple(
                    row[0] for row in connection.execute("SELECT username FROM users ORDER BY username")
                )
            self.assertEqual(users, accounts)
            with self.assertRaisesRegex(RuntimeError, "do not match"):
                Database(path, TEST_ACCOUNTS).initialize()

    def test_database_account_count_is_bounded(self):
        path = Path("unused.sqlite3")
        invalid = [
            ("only-one",),
            tuple(f"private-{position}" for position in range(11)),
            ("same", "same"),
        ]
        for accounts in invalid:
            with self.subTest(count=len(accounts)), self.assertRaises(ValueError):
                Database(path, accounts)

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
                (
                    "lesson-id",
                    "user-alpha",
                    "private title",
                    "ko",
                    "2026-01-01T00:00:00Z",
                    0,
                    0,
                    "qwen",
                ),
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
