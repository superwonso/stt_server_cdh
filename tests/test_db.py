from __future__ import annotations

import sqlite3
import os
from contextlib import closing
import tempfile
import unittest
import uuid
from pathlib import Path

from server.db import Database
from server import platform_files

TEST_ACCOUNTS = ("user-alpha", "user-beta")


class DatabaseTests(unittest.TestCase):
    def test_v20_continuations_migration_preserves_every_existing_row(self):
        with tempfile.TemporaryDirectory(prefix="stt-test-v21-migration-") as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            lecture_id = str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute("INSERT INTO lectures(id,username,title,created_at,recording_finalized) "
                                   "VALUES(?,?,'synthetic untouched','now',1)", (lecture_id, TEST_ACCOUNTS[0]))
                connection.execute("INSERT INTO lecture_study_notes(lecture_id,username,job_id,raw_revision,status,model,"
                                   "created_at,updated_at,error_code,error) VALUES(?,?,?,?,'failed','synthetic','now','now','interrupted','safe')",
                                   (lecture_id, TEST_ACCOUNTS[0], str(uuid.uuid4()), "a" * 64))
                connection.execute("DROP TABLE lecture_continuations")
                connection.execute("PRAGMA user_version=20")
                tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                before = {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                          for table in tables}
            database.initialize()
            database.initialize()
            with database.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
                self.assertEqual({table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                                  for table in tables}, before)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_continuations").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_continuations_parent_purge_unlinks_child_and_child_purge_cascades_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            parent, child = str(uuid.uuid4()), str(uuid.uuid4())
            with database.connect() as connection:
                connection.executemany("INSERT INTO lectures(id,username,title,created_at) VALUES(?,?,'synthetic','now')",
                                       [(parent, TEST_ACCOUNTS[0]), (child, TEST_ACCOUNTS[0])])
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO lecture_continuations VALUES(?,?,?)", (parent, parent, "a" * 64))
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO lecture_continuations VALUES(?,?,?)", (child, parent, "not-a-hash"))
                connection.execute("INSERT INTO lecture_continuations VALUES(?,?,?)", (child, parent, "a" * 64))
                connection.execute("DELETE FROM lectures WHERE id=?", (parent,))
                self.assertEqual(connection.execute("SELECT id FROM lectures").fetchone()[0], child)
                self.assertEqual(tuple(connection.execute("SELECT * FROM lecture_continuations").fetchone()), (child, None, "a" * 64))
                connection.execute("DELETE FROM lectures WHERE id=?", (child,))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_continuations").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v19_study_notes_are_additive_and_preserve_every_existing_table(self):
        with tempfile.TemporaryDirectory(prefix="stt-test-v20-migration-") as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            lecture_id = str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute("INSERT INTO lectures(id,username,title,created_at,recording_finalized) "
                                   "VALUES(?,?,'synthetic preserved title','now',1)", (lecture_id, TEST_ACCOUNTS[0]))
                connection.execute("UPDATE users SET password_hash='synthetic-password-hash'")
                connection.execute("INSERT INTO sessions VALUES('synthetic-token-hash',?,9999999999,1)", (TEST_ACCOUNTS[0],))
                connection.execute("INSERT INTO lecture_metadata VALUES(?,'synthetic display','course','term',1,'now')", (lecture_id,))
                for table in ("lecture_summaries", "lecture_translations"):
                    connection.execute(f"INSERT INTO {table}(lecture_id,job_id,raw_revision,status,model,created_at,updated_at) "
                                       "VALUES(?,?,?,'failed','synthetic-old-model','now','now')",
                                       (lecture_id, str(uuid.uuid4()), "a" * 64))
                connection.execute("DROP TABLE lecture_study_notes")
                connection.execute("PRAGMA user_version=19")
                tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                before = {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                          for table in tables}
            database.initialize()
            database.initialize()
            with database.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
                self.assertEqual({table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                                  for table in tables}, before)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_study_notes").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_study_note_owner_queue_constraints_and_purge_cascade(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            first, second, other = [str(uuid.uuid4()) for _ in range(3)]
            with database.connect() as connection:
                for identifier, username in ((first, TEST_ACCOUNTS[0]), (second, TEST_ACCOUNTS[0]), (other, TEST_ACCOUNTS[1])):
                    connection.execute("INSERT INTO lectures(id,username,title,created_at) VALUES(?,?,'synthetic','now')", (identifier, username))

                def insert(identifier, username):
                    connection.execute("INSERT INTO lecture_study_notes(lecture_id,username,job_id,raw_revision,status,model,created_at,updated_at) "
                                       "VALUES(?,?,?,?,'queued','synthetic-model','now','now')",
                                       (identifier, username, str(uuid.uuid4()), "a" * 64))

                insert(first, TEST_ACCOUNTS[0])
                with self.assertRaises(sqlite3.IntegrityError):
                    insert(second, TEST_ACCOUNTS[0])
                insert(other, TEST_ACCOUNTS[1])
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE lecture_study_notes SET attempts=2 WHERE lecture_id=?", (first,))
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE lecture_study_notes SET status='completed' WHERE lecture_id=?", (first,))
                connection.execute("UPDATE lecture_study_notes SET status='failed' WHERE lecture_id=?", (first,))
                insert(second, TEST_ACCOUNTS[0])
                connection.execute("UPDATE lectures SET trashed_at='now' WHERE id=?", (first,))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_study_notes").fetchone()[0], 3)
                connection.execute("DELETE FROM lectures WHERE id=?", (first,))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_study_notes WHERE lecture_id=?", (first,)).fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_study_notes").fetchone()[0], 2)

    def test_v18_recovery_preserves_data_and_audit_ids_with_sequence(self):
        with tempfile.TemporaryDirectory(prefix="stt-test-v19-migration-") as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            lecture_id = str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute("INSERT INTO lectures(id,username,title,created_at) VALUES(?,?,'synthetic','now')",
                                   (lecture_id, TEST_ACCOUNTS[0]))
                connection.execute("UPDATE users SET password_hash='synthetic-hash',setup_hash='synthetic-setup',setup_expires=9999999999")
                connection.execute("INSERT INTO sessions VALUES('synthetic-session',?,9999999999,1)",(TEST_ACCOUNTS[0],))
                connection.execute("DROP TABLE account_password_resets")
                connection.execute("DROP TABLE admin_audit")
                connection.execute("""CREATE TABLE admin_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('access_changed','sessions_revoked','tunnel_restarted')),
                    result TEXT NOT NULL CHECK(result IN ('success','failed','accepted')),
                    target TEXT NOT NULL CHECK(length(target) BETWEEN 1 AND 64))""")
                connection.execute("CREATE INDEX admin_audit_recent ON admin_audit(timestamp DESC,id DESC)")
                connection.execute("INSERT INTO admin_audit VALUES(7,'old-time','sessions_revoked','success',?)",(TEST_ACCOUNTS[0],))
                connection.execute("INSERT INTO admin_audit VALUES(999,'old-time','access_changed','success','service')")
                connection.execute("DELETE FROM admin_audit WHERE id=999")
                connection.execute("PRAGMA user_version=18")
                before = {table:[tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                          for table in ("users","sessions","lectures","admin_audit")}
            database.initialize()
            database.initialize()
            with database.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0],23)
                self.assertEqual({table:[tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                                  for table in before},before)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM account_password_resets").fetchone()[0],0)
                for action in ("password_reset_issued","password_reset_revoked","password_reset_completed"):
                    cursor = connection.execute("INSERT INTO admin_audit(timestamp,action,result,target) VALUES('new',?,'success',?)",
                                                (action,TEST_ACCOUNTS[0]))
                    self.assertGreater(cursor.lastrowid,999)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO admin_audit(timestamp,action,result,target) VALUES('new','unsafe-action','success','service')")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(),[])
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0],"ok")

    def test_v17_questions_are_additive_private_and_cascade_only_on_permanent_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            lecture_id, question_id = str(uuid.uuid4()), str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute("INSERT INTO lectures(id,username,title,created_at,trashed_at) VALUES (?,?,'original','now','trashed')",
                                   (lecture_id, TEST_ACCOUNTS[0]))
                connection.execute("INSERT INTO sessions VALUES ('test-token-hash',?,9999999999,1)", (TEST_ACCOUNTS[0],))
                connection.execute("INSERT INTO lecture_manual_state VALUES (?, ?,1)", (lecture_id, "a" * 64))
                connection.execute("DROP TABLE lecture_questions")
                connection.execute("PRAGMA user_version=17")
                before = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                          for table in ("users", "sessions", "lectures", "lecture_manual_state")}
            database.initialize()
            database.initialize()
            with database.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
                self.assertEqual({table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                                  for table in before}, before)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_questions").fetchone()[0], 0)
                connection.execute(
                    "INSERT INTO lecture_questions(id,lecture_id,username,question,request_hash,raw_revision,model,"
                    "selected_ids_json,evidence_sha256,scope,total_segments,selected_count,status,created_at,updated_at) "
                    "VALUES(?,?,?,'private question',?,?,'fake','[]',?,'none',0,0,'queued','now','now')",
                    (question_id, lecture_id, TEST_ACCOUNTS[0], "a" * 64, "b" * 64, "c" * 64),
                )
                connection.execute("UPDATE lectures SET trashed_at=NULL WHERE id=?", (lecture_id,))
                self.assertEqual(connection.execute("SELECT question FROM lecture_questions").fetchone()[0], "private question")
                connection.execute("DELETE FROM lectures WHERE id=?", (lecture_id,))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_questions").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v16_manual_tables_are_empty_additions_and_purge_cascades_private_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            lecture_id, chunk_id, segment_id, note_id = (str(uuid.uuid4()) for _ in range(4))
            tables = ("lecture_manual_history", "lecture_manual_notes", "lecture_manual_edits", "lecture_manual_state")
            with database.connect() as connection:
                connection.execute("INSERT INTO lectures(id,username,title,created_at,trashed_at) VALUES (?,?,'original','now','trashed')",
                                   (lecture_id, TEST_ACCOUNTS[0]))
                connection.execute("INSERT INTO chunks(lecture_id,chunk_id,payload_hash,start_seconds,status) VALUES (?,?,'hash',0,'done')",
                                   (lecture_id, chunk_id))
                connection.execute("INSERT INTO segments VALUES (?,?,?,0,1,'raw')", (segment_id, lecture_id, chunk_id))
                for table in tables:
                    connection.execute(f"DROP TABLE {table}")
                connection.execute("PRAGMA user_version=16")
                before = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                          for table in ("users", "lectures", "chunks", "segments")}
            database.initialize()
            database.initialize()
            with database.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
                self.assertEqual({table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                                  for table in before}, before)
                for table in tables:
                    self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
                connection.execute("INSERT INTO lecture_manual_state VALUES (?, ?,1)", (lecture_id, "a" * 64))
                connection.execute("INSERT INTO lecture_manual_notes VALUES (?,?,?,0,'note','now','now')", (note_id, lecture_id, segment_id))
                connection.execute("INSERT INTO lecture_manual_edits VALUES (?,?,'edit','now','now')", (segment_id, lecture_id))
                connection.execute("INSERT INTO lecture_manual_history VALUES (?,1,?,?,?,'segment_edit',NULL,?,0,'edit','now')",
                                   (lecture_id, str(uuid.uuid4()), "b" * 64, "a" * 64, segment_id))
                connection.execute("DELETE FROM lectures WHERE id=?", (lecture_id,))
                for table in tables:
                    self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v15_trash_migration_is_additive_and_never_trashes_existing_lessons(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            identifiers = [str(uuid.uuid4()) for _ in range(2)]
            with database.connect() as connection:
                connection.executemany(
                    "INSERT INTO lectures(id,username,title,created_at,deleting) VALUES(?,?,'old','2000-01-01',?)",
                    [(identifiers[0],TEST_ACCOUNTS[0],0),(identifiers[1],TEST_ACCOUNTS[1],1)],
                )
                connection.execute("DROP INDEX lectures_user_trash")
                connection.execute("ALTER TABLE lectures DROP COLUMN trashed_at")
                connection.execute("PRAGMA user_version=15")
                before = [dict(row) for row in connection.execute("SELECT * FROM lectures ORDER BY id")]
                users = [dict(row) for row in connection.execute("SELECT * FROM users ORDER BY username")]
            database.initialize()
            database.initialize()
            with database.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0],23)
                after = [dict(row) for row in connection.execute("SELECT * FROM lectures ORDER BY id")]
                self.assertTrue(all(row.pop("trashed_at") is None for row in after))
                self.assertEqual(after,before)
                self.assertEqual([dict(row) for row in connection.execute("SELECT * FROM users ORDER BY username")],users)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(),[])

    def test_v14_adds_private_metadata_without_rewriting_original_titles(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            lecture_id = str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute("INSERT INTO lectures(id,username,title,created_at) VALUES (?,?,'original','now')",
                                   (lecture_id, TEST_ACCOUNTS[0]))
                connection.execute("DROP TABLE lecture_metadata")
                connection.execute("PRAGMA user_version=14")
                before = tuple(connection.execute("SELECT * FROM lectures").fetchone())
                users_before = [tuple(row) for row in connection.execute("SELECT * FROM users ORDER BY username")]
            database.initialize()
            database.initialize()
            with database.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
                self.assertEqual(tuple(connection.execute("SELECT * FROM lectures").fetchone()), before)
                self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM users ORDER BY username")], users_before)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_metadata").fetchone()[0], 0)
                for title, course, semester, revision in (("", "", "", 1), ("x" * 121, "", "", 1),
                                                        (None, "x" * 81, "", 1), (None, "", "x" * 41, 1),
                                                        (None, "", "", 0), (None, "", "", 1.5)):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute("INSERT INTO lecture_metadata VALUES (?,?,?,?,?,'now')",
                                           (lecture_id, title, course, semester, revision))
                connection.execute("INSERT INTO lecture_metadata VALUES (?,'display','course','semester',1,'now')", (lecture_id,))
                self.assertEqual(connection.execute("SELECT title FROM lectures").fetchone()[0], "original")
                connection.execute("DELETE FROM lectures WHERE id=?", (lecture_id,))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_metadata").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v13_adds_bookmarks_preserving_drive_metrics_and_cascade(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            lecture_id, bookmark_id = str(uuid.uuid4()), str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute("INSERT INTO lectures(id,username,title,created_at) VALUES (?,?,'fixture','now')",
                                   (lecture_id, TEST_ACCOUNTS[0]))
                connection.execute("UPDATE drive_archive_statistics SET last_verified_upload_at='2026-09-06T00:00:00Z'")
                connection.execute("DROP TABLE lecture_bookmarks")
                connection.execute("PRAGMA user_version=13")
                before = tuple(connection.execute("SELECT * FROM lectures").fetchone())
            database.initialize()
            database.initialize()
            with database.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
                self.assertEqual(tuple(connection.execute("SELECT * FROM lectures").fetchone()), before)
                self.assertEqual(connection.execute("SELECT last_verified_upload_at FROM drive_archive_statistics").fetchone()[0],
                                 "2026-09-06T00:00:00Z")
                for start, label in ((-1, "x"), (14401, "x"), (1, "x" * 121)):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute("INSERT INTO lecture_bookmarks VALUES (?,?,?,?,'now')",
                                           (bookmark_id, lecture_id, start, label))
                connection.execute("INSERT INTO lecture_bookmarks VALUES (?,?,0,'fixture','now')", (bookmark_id, lecture_id))
                connection.execute("DELETE FROM lectures WHERE id=?", (lecture_id,))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM lecture_bookmarks").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v12_drive_metrics_migrate_without_inventing_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "data" / "classroom.sqlite3", TEST_ACCOUNTS)
            database.initialize()
            lecture_id = str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO lectures(id,username,title,created_at) VALUES (?,?,'fixture','now')",
                    (lecture_id, TEST_ACCOUNTS[0]),
                )
                connection.execute(
                    "INSERT INTO recording_archives(lecture_id,state,object_key,updated_at) "
                    "VALUES (?,'pending',?,'2026-01-01T00:00:00Z')", (lecture_id, "a" * 64),
                )
                for column in ("queued_at", "queued_bytes", "verified_at"):
                    connection.execute(f"ALTER TABLE recording_archives DROP COLUMN {column}")
                connection.execute("DROP TABLE drive_archive_statistics")
                connection.execute("PRAGMA user_version=12")
                before = dict(connection.execute("SELECT * FROM recording_archives").fetchone())
            database.initialize()
            database.initialize()
            with database.connect() as connection:
                after = dict(connection.execute("SELECT * FROM recording_archives").fetchone())
                self.assertEqual({key: after[key] for key in before}, before)
                self.assertEqual(tuple(after[key] for key in ("queued_at", "queued_bytes", "verified_at")), (None,) * 3)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
                self.assertEqual(tuple(connection.execute("SELECT * FROM drive_archive_statistics").fetchone()), (1, None))
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE recording_archives SET queued_bytes=43")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

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
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
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
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
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
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE chunks SET qwen_boundary_json=?", ("x" * 65537,))

    def test_pre_fingerprint_import_table_migrates_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data" / "classroom.sqlite3"
            if os.name == "nt":
                platform_files.ensure_private_directory(path.parent)
            else:
                path.parent.mkdir()
            with closing(sqlite3.connect(path)) as connection, connection:
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
            with closing(sqlite3.connect(path)) as connection, connection:
                row = connection.execute(
                    "SELECT file_fingerprint, raw_deleted FROM imports "
                    "WHERE id = '11111111-1111-4111-8111-111111111111'"
                ).fetchone()
            self.assertEqual(row, ("0" * 64, 0))

    def test_legacy_chunks_migrate_as_final_and_keep_retry_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "classroom.sqlite3"
            if os.name == "nt":
                platform_files.ensure_private_directory(path.parent)
            else:
                path.parent.mkdir()
            lecture_id = str(uuid.uuid4())
            chunk_id = str(uuid.uuid4())
            with closing(sqlite3.connect(path)) as connection, connection:
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
            platform_files.validate_private_path(path.parent, directory=True)
            platform_files.validate_private_path(path)

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
            self.assertEqual(schema_version, 23)

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
            self.assertEqual(version, 23)
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
            with closing(sqlite3.connect(path)) as connection, connection:
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
            if os.name == "nt":
                platform_files.ensure_private_directory(path.parent)
            else:
                path.parent.mkdir()
            with closing(sqlite3.connect(path)) as connection, connection:
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
                    None,
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
