from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, path: Path, accounts: tuple[str, ...]):
        if not 2 <= len(accounts) <= 10 or len(set(accounts)) != len(accounts):
            raise ValueError("Database requires 2-10 distinct account IDs")
        self.path = Path(path)
        self.accounts = accounts

    @staticmethod
    def _inspect_users(connection: sqlite3.Connection) -> tuple[set[str] | None, str]:
        table = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        if table is None:
            return None, ""
        columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(users)"))
        if columns != ("username", "password_hash", "setup_hash", "setup_expires"):
            raise RuntimeError("The existing users table has an unsupported schema")
        accounts = {row[0] for row in connection.execute("SELECT username FROM users")}
        return accounts, table[0] or ""

    @staticmethod
    def _remove_legacy_account_check(
        connection: sqlite3.Connection,
        schema_sql: str,
        schema_version: int,
    ) -> None:
        if "CHECK" not in schema_sql.upper():
            return
        if schema_version != 0:
            raise RuntimeError("The users table has an unexpected account constraint")
        shadow = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users_without_account_check'"
        ).fetchone()
        if shadow is not None:
            raise RuntimeError("An unfinished users table migration needs manual review")

        # Foreign-key mode can only be changed outside a transaction. Rebuild
        # the parent table in one explicit transaction so existing password,
        # invitation, session, lecture, and import ownership data is retained.
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE users_without_account_check (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT,
                    setup_hash TEXT,
                    setup_expires REAL
                )
            """)
            connection.execute(
                "INSERT INTO users_without_account_check "
                "(username, password_hash, setup_hash, setup_expires) "
                "SELECT username, password_hash, setup_hash, setup_expires FROM users"
            )
            connection.execute("DROP TABLE users")
            connection.execute("ALTER TABLE users_without_account_check RENAME TO users")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Foreign-key validation failed during users table migration")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("Could not restore SQLite foreign-key enforcement")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        # The database contains password hashes and private transcripts.  Keep
        # both it and its SQLite WAL files inaccessible to other local users.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self.path.touch(exist_ok=True, mode=0o600)
        self.path.chmod(0o600)
        with self.connect() as connection:
            existing_accounts, users_schema = self._inspect_users(connection)
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            configured_accounts = set(self.accounts)
            if existing_accounts and existing_accounts != configured_accounts:
                # Check before any schema or data write. A typo or copied
                # placeholder must never add accounts to a private database.
                raise RuntimeError(
                    "Configured account IDs do not match the existing database"
                )
            if existing_accounts is not None:
                self._remove_legacy_account_check(connection, users_schema, schema_version)
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT,
                    setup_hash TEXT,
                    setup_expires REAL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL REFERENCES users(username),
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(username);
                CREATE TABLE IF NOT EXISTS lectures (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL REFERENCES users(username),
                    title TEXT NOT NULL,
                    language TEXT CHECK (language IS NULL OR language IN ('ko', 'en')),
                    created_at TEXT NOT NULL,
                    deleting INTEGER NOT NULL DEFAULT 0 CHECK (deleting IN (0, 1)),
                    recording_finalized INTEGER NOT NULL DEFAULT 0
                        CHECK (recording_finalized IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS lectures_user ON lectures(username, created_at);
                CREATE TABLE IF NOT EXISTS chunks (
                    lecture_id TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
                    chunk_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    start_seconds REAL NOT NULL,
                    overlap_seconds REAL NOT NULL DEFAULT 0,
                    final_chunk INTEGER NOT NULL DEFAULT 1 CHECK (final_chunk IN (0, 1)),
                    status TEXT NOT NULL CHECK (status IN ('pending', 'done')),
                    processing_seconds REAL,
                    PRIMARY KEY (lecture_id, chunk_id)
                );
                CREATE TABLE IF NOT EXISTS segments (
                    id TEXT PRIMARY KEY,
                    lecture_id TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    start REAL NOT NULL,
                    end REAL NOT NULL,
                    text TEXT NOT NULL,
                    FOREIGN KEY (lecture_id, chunk_id)
                        REFERENCES chunks(lecture_id, chunk_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS segments_lecture ON segments(lecture_id, start);
                CREATE TABLE IF NOT EXISTS imports (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL REFERENCES users(username),
                    lecture_id TEXT REFERENCES lectures(id) ON DELETE SET NULL,
                    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
                    language TEXT CHECK (language IS NULL OR language IN ('ko', 'en')),
                    filename TEXT NOT NULL CHECK (length(filename) BETWEEN 1 AND 255),
                    file_fingerprint TEXT NOT NULL CHECK (
                        length(file_fingerprint) = 64 AND file_fingerprint = lower(file_fingerprint)
                    ),
                    total_bytes INTEGER NOT NULL CHECK (total_bytes BETWEEN 1 AND 1073741824),
                    uploaded_bytes INTEGER NOT NULL DEFAULT 0
                        CHECK (uploaded_bytes BETWEEN 0 AND total_bytes),
                    status TEXT NOT NULL CHECK (
                        status IN ('uploading', 'queued', 'processing', 'completed', 'failed', 'cancelled')
                    ),
                    raw_deleted INTEGER NOT NULL DEFAULT 0 CHECK (raw_deleted IN (0, 1)),
                    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
                    processed_seconds REAL NOT NULL DEFAULT 0 CHECK (processed_seconds >= 0),
                    duration_seconds REAL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS imports_user_recent ON imports(username, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS imports_one_active_user ON imports(username)
                    WHERE status IN ('uploading', 'queued', 'processing');
                CREATE TABLE IF NOT EXISTS transcript_corrections (
                    lecture_id TEXT PRIMARY KEY REFERENCES lectures(id) ON DELETE CASCADE,
                    raw_revision TEXT NOT NULL CHECK (
                        length(raw_revision) = 64 AND raw_revision = lower(raw_revision)
                    ),
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'processing', 'completed', 'failed')
                    ),
                    model TEXT NOT NULL,
                    corrected_text TEXT,
                    corrected_segments TEXT,
                    uncertain_terms TEXT,
                    error_code TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    CHECK (
                        (status = 'completed' AND corrected_text IS NOT NULL
                            AND corrected_segments IS NOT NULL AND uncertain_terms IS NOT NULL
                            AND error_code IS NULL AND error IS NULL AND completed_at IS NOT NULL)
                        OR
                        (status != 'completed' AND corrected_text IS NULL
                            AND corrected_segments IS NULL AND uncertain_terms IS NULL
                            AND completed_at IS NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS transcript_corrections_queue
                    ON transcript_corrections(status, created_at);
                CREATE TABLE IF NOT EXISTS operational_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    access_enabled INTEGER NOT NULL CHECK (access_enabled IN (0, 1)),
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO operational_state(singleton, access_enabled, updated_at)
                    VALUES (1, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
                CREATE TABLE IF NOT EXISTS admin_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (
                        action IN ('access_changed', 'sessions_revoked', 'tunnel_restarted')
                    ),
                    result TEXT NOT NULL CHECK (result IN ('success', 'failed', 'accepted')),
                    target TEXT NOT NULL CHECK (length(target) BETWEEN 1 AND 64)
                );
                CREATE INDEX IF NOT EXISTS admin_audit_recent
                    ON admin_audit(timestamp DESC, id DESC);
            """)
            chunk_columns = {row[1] for row in connection.execute("PRAGMA table_info(chunks)")}
            lecture_columns = {row[1] for row in connection.execute("PRAGMA table_info(lectures)")}
            if "deleting" not in lecture_columns:
                connection.execute(
                    "ALTER TABLE lectures ADD COLUMN deleting INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (deleting IN (0, 1))"
                )
            if "recording_finalized" not in lecture_columns:
                connection.execute(
                    "ALTER TABLE lectures ADD COLUMN recording_finalized INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (recording_finalized IN (0, 1))"
                )
            if "overlap_seconds" not in chunk_columns:
                connection.execute("ALTER TABLE chunks ADD COLUMN overlap_seconds REAL NOT NULL DEFAULT 0")
            if "final_chunk" not in chunk_columns:
                # Every chunk produced by the pre-overlap protocol was final.
                # Using 1 preserves idempotent retries made after an upgrade;
                # the API's omitted-header default is also `true`.
                connection.execute(
                    "ALTER TABLE chunks ADD COLUMN final_chunk INTEGER NOT NULL DEFAULT 1 "
                    "CHECK (final_chunk IN (0, 1))"
                )
            import_columns = {row[1] for row in connection.execute("PRAGMA table_info(imports)")}
            if "file_fingerprint" not in import_columns:
                # This table existed briefly during development before reload
                # recovery gained a bounded content fingerprint. Existing
                # unfinished jobs receive a value that cannot accidentally
                # match a real uploaded file and will fail closed at complete.
                connection.execute(
                    "ALTER TABLE imports ADD COLUMN file_fingerprint TEXT NOT NULL DEFAULT "
                    "'0000000000000000000000000000000000000000000000000000000000000000'"
                )
            if "raw_deleted" not in import_columns:
                # A missing raw file is reconciled by startup/periodic cleanup.
                # Defaulting to false avoids claiming deletion before checking it.
                connection.execute(
                    "ALTER TABLE imports ADD COLUMN raw_deleted INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (raw_deleted IN (0, 1))"
                )
            if schema_version < 4:
                # Before retained WAV recordings existed, completed text-only
                # lectures had no lecture-level final flag and all old chunks
                # were migrated as final. Promote only that unambiguous legacy
                # shape. A current/incomplete recording has a non-final or
                # pending chunk (or no completed chunk) and remains untouched.
                connection.execute(
                    "UPDATE lectures SET recording_finalized = 1 "
                    "WHERE recording_finalized = 0 "
                    "AND EXISTS (SELECT 1 FROM chunks c "
                    "WHERE c.lecture_id = lectures.id AND c.status = 'done') "
                    "AND NOT EXISTS (SELECT 1 FROM chunks c "
                    "WHERE c.lecture_id = lectures.id "
                    "AND (c.status != 'done' OR c.final_chunk = 0))"
                )
            existing_accounts = {
                row[0] for row in connection.execute("SELECT username FROM users").fetchall()
            }
            if existing_accounts and existing_accounts != configured_accounts:
                raise RuntimeError(
                    "Configured account IDs do not match the existing database"
                )
            if not existing_accounts:
                connection.executemany(
                    "INSERT INTO users(username) VALUES (?)",
                    [(name,) for name in self.accounts],
                )
            if schema_version < 5:
                connection.execute("PRAGMA user_version = 5")
