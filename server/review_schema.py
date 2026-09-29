"""Additive, owner-scoped reminder storage (schema version 24)."""
import json
from datetime import date
from pathlib import Path


def migrate_review_schema(connection):
    # execute (not executescript) keeps this migration inside the caller's
    # transaction. No existing table, account, or lecture value is rewritten.
    statements = (
        """CREATE TABLE IF NOT EXISTS review_profiles (
            username TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
            revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
            settings_json TEXT NOT NULL, timetable_json TEXT,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS review_items (
            id TEXT NOT NULL CHECK(length(id)=36),
            username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            source_key TEXT, document_json TEXT NOT NULL,
            PRIMARY KEY(username,id), UNIQUE(username,source_key)
        )""",
        """CREATE TABLE IF NOT EXISTS review_sources (
            username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            source_key TEXT NOT NULL CHECK(length(source_key) BETWEEN 1 AND 120),
            PRIMARY KEY(username,source_key)
        )""",
        """CREATE TABLE IF NOT EXISTS review_routines (
            id TEXT NOT NULL CHECK(length(id)=36),
            username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            document_json TEXT NOT NULL, PRIMARY KEY(username,id)
        )""",
        """CREATE TABLE IF NOT EXISTS review_exams (
            id TEXT NOT NULL CHECK(length(id)=36),
            username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            subject TEXT NOT NULL CHECK(length(subject) BETWEEN 1 AND 40),
            kind TEXT NOT NULL CHECK(kind IN ('mid','final')),
            document_json TEXT NOT NULL,
            PRIMARY KEY(username,id), UNIQUE(username,subject,kind)
        )""",
        """CREATE TABLE IF NOT EXISTS review_requests (
            username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            request_id TEXT NOT NULL CHECK(length(request_id)=36),
            fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64),
            result_revision INTEGER NOT NULL,
            undo_token TEXT, created_at REAL NOT NULL,
            PRIMARY KEY(username,request_id)
        )""",
        """CREATE TABLE IF NOT EXISTS review_undo (
            username TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
            token TEXT NOT NULL, revision INTEGER NOT NULL,
            expires_at REAL NOT NULL, snapshot_json TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS review_parse_usage (
            username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            day TEXT NOT NULL, count INTEGER NOT NULL CHECK(count >= 0),
            PRIMARY KEY(username,day)
        )""",
        """CREATE TABLE IF NOT EXISTS review_parse_leases (
            username TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
            lease_id TEXT NOT NULL, expires_at REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS review_holidays (
            date TEXT PRIMARY KEY, year INTEGER NOT NULL,
            name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100)
        )""",
    )
    for statement in statements:
        connection.execute(statement)
    directory = Path(__file__).resolve().parents[1] / "data" / "review-holidays"
    for path in sorted(directory.glob("[0-9][0-9][0-9][0-9].json")):
        if path.stat().st_size > 128 * 1024:
            raise ValueError("holiday_data_limit")
        document = json.loads(path.read_text(encoding="utf-8"))
        year, rows = int(path.stem), document.get("holidays")
        if document.get("year") != year or not isinstance(rows, list) or len(rows) > 366:
            raise ValueError("invalid_holiday_data")
        values, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid_holiday_data")
            value, name = row.get("date"), row.get("name")
            if not isinstance(value, str) or len(value) != 10 or date.fromisoformat(value).isoformat() != value or not value.startswith(str(year) + "-"):
                raise ValueError("invalid_holiday_data")
            if value in seen or not isinstance(name, str) or not 1 <= len(name.strip()) <= 100 or any(ord(c) < 32 for c in name):
                raise ValueError("invalid_holiday_data")
            seen.add(value)
            values.append((value, year, name.strip()))
        # A fully validated replacement affects only its named year. Missing
        # files never erase older years already stored by an earlier release.
        connection.execute("DELETE FROM review_holidays WHERE year=?", (year,))
        connection.executemany("INSERT INTO review_holidays(date,year,name) VALUES(?,?,?)", values)
