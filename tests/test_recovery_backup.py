from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from contextlib import closing
from unittest import mock

from server.db import Database
from server import platform_files
from server.recovery_backup import (
    AGE_RELATIVE, MAX_DATABASE_BYTES, BackupError, BackupScheduler, RecoveryBackupManager,
    _database_info, _hash_file, _run, verify_recovery_archive,
)
from server.settings import PROJECT_DIR, Settings

# Explicit test-only override allows an already provisioned native age binary.
# The default remains the project's pinned Linux/native tool location.
AGE = Path(os.environ["STT_TEST_AGE_BINARY"]) if os.environ.get("STT_TEST_AGE_BINARY") else PROJECT_DIR / AGE_RELATIVE
if not AGE.is_absolute():
    raise ValueError("STT_TEST_AGE_BINARY must be an absolute path")
RESTORE_TEMP_ROOT = Path(tempfile.gettempdir()) if os.name == "nt" else Path("/tmp")
ACCOUNTS = ("user-alpha", "user-beta")


def private_write(path: Path, content: bytes):
    platform_files.ensure_private_directory(path.parent)
    platform_files.atomic_write_private(path, content)


def seed_question_jobs(database):
    lecture_id = str(uuid.uuid4())
    with database.connect() as connection:
        connection.execute("INSERT INTO lectures(id,username,title,created_at,recording_finalized) "
                           "VALUES (?,?,'Synthetic questions','2026-09-06',1)", (lecture_id, ACCOUNTS[0]))
        for status in ("queued", "processing", "completed", "failed", "cancelled"):
            connection.execute(
                "INSERT INTO lecture_questions(id,lecture_id,username,question,request_hash,raw_revision,model,"
                "selected_ids_json,evidence_sha256,scope,total_segments,selected_count,status,attempts,document_json,"
                "created_at,updated_at,completed_at) VALUES(?,?,?,'synthetic private question',?,?,'synthetic-model',"
                "'[]',?,'none',0,0,?,?,?,'2026-09-06','2026-09-06',?)",
                (str(uuid.uuid4()), lecture_id, ACCOUNTS[0], "a" * 64, "b" * 64, "c" * 64, status,
                 int(status in ("processing", "completed")),
                 '{"answerability":"insufficient_evidence","paragraphs":[]}' if status == "completed" else None,
                 "2026-09-06" if status == "completed" else None),
            )


def seed_material_and_review_jobs(database):
    """Two synthetic owners; active and terminal rows, never service execution."""
    with database.connect() as connection:
        for owner, active_review, upload_code in zip(ACCOUNTS, ("queued", "processing"), ("awaiting_upload", "converting")):
            course_id = str(uuid.uuid4())
            connection.execute("INSERT INTO course_groups(id,username,name,normalized_name,created_at,updated_at) "
                               "VALUES(?,?,'synthetic private course','synthetic private course','now','now')", (course_id, owner))
            for status in (active_review, "completed", "failed"):
                connection.execute(
                    "INSERT INTO course_review_jobs(id,username,course_id,model,status,source_revision,source_manifest_json,"
                    "document_json,error_code,created_at,updated_at,completed_at) "
                    "VALUES(?,?,?,'synthetic private model',?,?,'[]',?,?,'now','now',?)",
                    (str(uuid.uuid4()), owner, course_id, status, "a" * 64,
                     '{"private":"synthetic private review document"}' if status == "completed" else None,
                     "interrupted" if status == "failed" else None, "now" if status == "completed" else None))
            for status, code in (("processing", upload_code), ("ready", None), ("failed", "invalid_file")):
                material_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO study_materials(id,username,course_id,filename,kind,size_bytes,uploaded_bytes,sha256,storage_name,"
                    "status,document_json,error_code,created_at,updated_at) "
                    "VALUES(?,?,?,'synthetic private material.pdf','pdf',3,?,?,?, ?,?,?,'now','now')",
                    (material_id, owner, course_id, 0 if code == "awaiting_upload" else 3, "b" * 64, material_id + ".pdf", status,
                     '{"unit_count":1,"warnings":[],"markdown":"synthetic private material document"}' if status == "ready" else None, code))


class RecoveryQuestionInfoTests(unittest.TestCase):
    def test_study_note_backup_warnings_are_readonly_and_legacy_optional(self):
        with tempfile.TemporaryDirectory(prefix="stt-study-note-backup-test-") as temporary:
            database = Database(Path(temporary) / "private" / "database.sqlite3", ACCOUNTS)
            database.initialize()
            with database.connect() as connection:
                for owner,status in zip(ACCOUNTS,("queued","processing")):
                    identifier = str(uuid.uuid4())
                    connection.execute("INSERT INTO lectures(id,username,title,created_at,recording_finalized) "
                                       "VALUES(?,?,'Synthetic','2026-09-06',1)",(identifier,owner))
                    connection.execute("INSERT INTO lecture_study_notes(lecture_id,username,job_id,raw_revision,status,model,"
                                       "created_at,updated_at) VALUES(?,?,?,?,?,'synthetic','now','now')",
                                       (identifier,owner,str(uuid.uuid4()),"a"*64,status))
                before = [tuple(row) for row in connection.execute("SELECT * FROM lecture_study_notes ORDER BY lecture_id")]
            result = _database_info(database.path)
            self.assertEqual(result["schema_version"],23)
            self.assertEqual(result["unfinished_jobs"],2)
            with database.connect() as connection:
                self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM lecture_study_notes ORDER BY lecture_id")],before)
                connection.execute("DROP TABLE lecture_study_notes")
                connection.execute("PRAGMA user_version=19")
            self.assertEqual(_database_info(database.path)["unfinished_jobs"],0)

    def test_material_and_course_review_counts_are_readonly_and_exclude_terminal_jobs(self):
        with tempfile.TemporaryDirectory(prefix="stt-material-backup-info-") as temporary:
            database = Database(Path(temporary) / "private" / "database.sqlite3", ACCOUNTS)
            database.initialize()
            seed_material_and_review_jobs(database)
            with database.connect() as connection:
                before = {table: [tuple(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY id")]
                          for table in ("course_groups", "course_review_jobs", "study_materials")}
            result = _database_info(database.path)
            self.assertEqual(result["unfinished_jobs"], 4)
            self.assertEqual(result["material_files_omitted"], 6)
            self.assertEqual(result["unfinalized_lectures"], 0)
            for private in ("synthetic private course", "synthetic private model", "synthetic private review document",
                            "synthetic private material.pdf", "synthetic private material document"):
                self.assertNotIn(private, json.dumps(result))
            with database.connect() as connection:
                after = {table: [tuple(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY id")]
                         for table in before}
            self.assertEqual(after, before)

    def test_legacy_database_without_material_or_course_tables_reports_zero_new_jobs(self):
        with tempfile.TemporaryDirectory(prefix="stt-material-legacy-backup-") as temporary:
            database = Database(Path(temporary) / "private" / "database.sqlite3", ACCOUNTS)
            database.initialize()
            with database.connect() as connection:
                for table in ("course_review_sources", "course_review_jobs", "study_materials", "course_sessions", "course_groups"):
                    connection.execute("DROP TABLE " + table)
                connection.execute("PRAGMA user_version=22")
            result = _database_info(database.path)
            self.assertEqual(result["schema_version"], 22)
            self.assertEqual(result["unfinished_jobs"], 0)
            self.assertEqual(result["material_files_omitted"], 0)

    def test_unfinished_question_warning_counts_only_queued_and_processing_without_changes(self):
        with tempfile.TemporaryDirectory(prefix="stt-question-backup-test-") as temporary:
            database = Database(Path(temporary) / "private" / "database.sqlite3", ACCOUNTS)
            database.initialize()
            seed_question_jobs(database)
            with database.connect() as connection:
                before = [tuple(row) for row in connection.execute("SELECT * FROM lecture_questions ORDER BY id")]
            result = _database_info(database.path)
            self.assertEqual(result["unfinished_jobs"], 2)
            self.assertEqual(result["unfinalized_lectures"], 0)
            with database.connect() as connection:
                self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM lecture_questions ORDER BY id")], before)

    def test_legacy_backup_without_question_table_is_still_readable(self):
        with tempfile.TemporaryDirectory(prefix="stt-question-backup-test-") as temporary:
            database = Database(Path(temporary) / "private" / "database.sqlite3", ACCOUNTS)
            database.initialize()
            with database.connect() as connection:
                connection.execute("DROP TABLE lecture_questions")
                connection.execute("PRAGMA user_version=17")
            result = _database_info(database.path)
            self.assertEqual(result["schema_version"], 17)
            self.assertEqual(result["unfinished_jobs"], 0)


@unittest.skipUnless(AGE.is_file(), "The pinned age CLI is required for encrypted backup integration tests")
class RecoveryBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-backup-test-")
        self.root = Path(self.temporary.name) / "private-root"
        platform_files.ensure_private_directory(self.root)
        self.settings = Settings(data_dir=self.root / "private", model_cache_dir=self.root / "models",
                                 accounts=ACCOUNTS, google_drive_enabled=True)
        self.database = Database(self.settings.database_path, ACCOUNTS)
        self.database.initialize()
        self.environment = self.root / "server" / ".env"
        environment = mock.patch.dict(os.environ, {"STT_ENV_FILE": str(self.environment)})
        environment.start()
        self.addCleanup(environment.stop)
        private_write(self.environment, b"ACCOUNT_USERNAMES=user-alpha,user-beta\nGOOGLE_DRIVE_RECORDINGS=1\nAPI_KEY=synthetic-fixture-only\n")
        drive = self.settings.data_dir / "google-drive"
        private_write(drive / "identity.key", bytes(range(32)))
        private_write(drive / "oauth-client.json", b'{"installed":{"client_id":"synthetic-client"}}')
        private_write(drive / "token.json", b'{"refresh_token":"synthetic-token"}')
        self.destination = self.root / "ciphertext-destination"
        self.destination.mkdir(mode=0o700)
        self.copies = []
        self.manager = RecoveryBackupManager(self.settings, project_dir=self.root, age_binary=AGE,
                                             copy_runner=self.copy)
        self.manager.initialize()
        self.verified_directories = []

    def tearDown(self):
        for directory in self.verified_directories:
            shutil.rmtree(directory)
        self.temporary.cleanup()

    def copy(self, source, pending, cancel, deadline):
        self.assertTrue(source.read_bytes().startswith(b"age-encryption.org/v1\n"))
        self.copies.append(dict(pending))
        target = self.destination / pending["name"]
        if not target.exists():
            with target.open("xb") as output, source.open("rb") as stream:
                shutil.copyfileobj(stream, output)
        self.assertEqual(_hash_file(target, 600 * 1024 * 1024), (pending["bytes"], pending["sha256"]))

    def exported(self):
        result = self.manager.export()
        return self.destination / (result["bundle_id"] + ".age")

    def verify(self, archive, identity=None):
        result = self.manager.restore_check(archive, identity or self.manager.config_dir / "identity.txt")
        self.verified_directories.append(result["directory"])
        return result

    def encrypt_tar(self, members):
        plaintext = self.root / "malicious.tar"
        with tarfile.open(plaintext, "w", format=tarfile.USTAR_FORMAT) as archive:
            for name, content, kind in members:
                info = tarfile.TarInfo(name)
                info.type = kind
                info.size = len(content)
                if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                    info.linkname = "settings.env"
                archive.addfile(info, io.BytesIO(content))
        encrypted = self.root / "malicious.age"
        recipient = json.loads((self.manager.config_dir / "config.json").read_bytes())["recipient"]
        with encrypted.open("wb") as output:
            subprocess.run([str(AGE), "--encrypt", "-r", recipient, str(plaintext)],
                           stdout=output, stderr=subprocess.DEVNULL, check=True, timeout=15)
        return encrypted

    def test_roundtrip_includes_online_database_and_allowlist_only(self):
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lectures(id,username,title,language,created_at) VALUES ('test-lecture',?,'Synthetic lesson','ko','2026-01-01')", (ACCOUNTS[0],))
        private_write(self.settings.data_dir / "recordings" / "synthetic.wav", b"synthetic audio excluded")
        private_write(self.settings.data_dir / "old-backup.sqlite3", b"excluded backup")
        archive = self.exported()
        result = self.verify(archive)
        directory = Path(result["directory"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["file_count"], 6)
        self.assertEqual(result["warnings"]["local_wav_files_omitted"], 1)
        self.assertEqual(result["warnings"]["unfinalized_lectures"], 1)
        platform_files.validate_private_path(directory, directory=True)
        self.assertEqual((directory / "settings.env").read_bytes(), self.environment.read_bytes())
        self.assertNotIn(str(self.root), (directory / "manifest.json").read_text())
        self.assertFalse(any(path.suffix == ".wav" for path in directory.iterdir()))
        for path in directory.iterdir():
            platform_files.validate_private_path(path)
        with closing(sqlite3.connect(directory / "database.sqlite3")) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lectures").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertFalse(any(self.manager.config_dir.glob("build-*")))

    def test_init_never_overwrites_identity_and_scheduler_never_needs_it(self):
        key = self.manager.config_dir / "identity.txt"
        initial = key.read_bytes()
        platform_files.validate_private_path(key)
        with self.assertRaisesRegex(BackupError, "already_initialized"):
            self.manager.initialize()
        self.assertEqual(key.read_bytes(), initial)
        outside = self.root / "separate-identity.txt"
        key.rename(outside)
        result = self.verify(self.exported(), outside)
        self.assertTrue(result["verified"], "encryption only reads the public recipient")

    def test_question_jobs_survive_encrypted_roundtrip_with_unfinished_warning(self):
        seed_question_jobs(self.database)
        with self.database.connect() as connection:
            before = [tuple(row) for row in connection.execute("SELECT * FROM lecture_questions ORDER BY id")]
        result = self.verify(self.exported())
        self.assertEqual(result["warnings"]["unfinished_jobs"], 2)
        directory = Path(result["directory"])
        manifest_text = (directory / "manifest.json").read_text()
        self.assertNotIn("synthetic private question", manifest_text)
        with closing(sqlite3.connect(directory / "database.sqlite3")) as connection, connection:
            self.assertEqual(connection.execute("SELECT * FROM lecture_questions ORDER BY id").fetchall(), before)

    def test_material_and_course_jobs_survive_encrypted_roundtrip_with_explicit_original_omission(self):
        seed_material_and_review_jobs(self.database)
        tables = ("course_groups", "course_review_jobs", "study_materials")
        with self.database.connect() as connection:
            before = {table: [tuple(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY id")]
                      for table in tables}
            names = [row[0] for row in connection.execute("SELECT storage_name FROM study_materials")]
        original_paths = [self.settings.data_dir / "study-materials" / name for name in names]
        for path in original_paths:
            private_write(path, b"synthetic original material excluded")
        original_bytes = [path.read_bytes() for path in original_paths]
        result = self.verify(self.exported())
        self.assertEqual(result["warnings"]["unfinished_jobs"], 4)
        self.assertEqual(result["warnings"]["material_files_omitted"], 6)
        self.assertEqual(result["file_count"], 6)
        directory = Path(result["directory"])
        self.assertFalse(any(path.suffix.lower() in {".pdf", ".pptx"} for path in directory.rglob("*")))
        manifest = (directory / "manifest.json").read_text()
        for private in (*names, *ACCOUNTS, "synthetic private course", "synthetic private model",
                        "synthetic private review document", "synthetic private material document", str(self.root)):
            self.assertNotIn(private, manifest)
        with closing(sqlite3.connect(directory / "database.sqlite3")) as connection:
            after = {table: connection.execute("SELECT * FROM " + table + " ORDER BY id").fetchall() for table in tables}
            self.assertEqual(after, before)
        self.assertEqual([path.read_bytes() for path in original_paths], original_bytes)

    def test_legacy_manifest_without_material_omission_field_remains_readable(self):
        result = self.verify(self.exported())
        directory = Path(result["directory"])
        manifest = json.loads((directory / "manifest.json").read_bytes())
        self.assertEqual(manifest["warnings"].pop("material_files_omitted"), 0)
        members = [(path.name, json.dumps(manifest).encode() if path.name == "manifest.json" else path.read_bytes(), tarfile.REGTYPE)
                   for path in directory.iterdir()]
        restored = self.verify(self.encrypt_tar(members))
        self.assertTrue(restored["verified"])
        self.assertEqual(set(restored["warnings"]), {"unfinished_jobs", "unfinalized_lectures", "local_wav_files_omitted"})

    def test_material_omission_manifest_count_rejects_negative_boolean_and_private_extra_fields(self):
        result = self.verify(self.exported())
        directory = Path(result["directory"])
        original = json.loads((directory / "manifest.json").read_bytes())
        for changed in ({"material_files_omitted": -1}, {"material_files_omitted": True},
                        {"material_files_omitted": "6"}, {"material_files_omitted": 10 ** 9 + 1},
                        {"material_owner": "synthetic private account"}):
            with self.subTest(changed=changed):
                manifest = {**original, "warnings": {**original["warnings"], **changed}}
                members = [(path.name, json.dumps(manifest).encode() if path.name == "manifest.json" else path.read_bytes(), tarfile.REGTYPE)
                           for path in directory.iterdir()]
                with self.assertRaisesRegex(BackupError, "invalid_manifest"):
                    self.verify(self.encrypt_tar(members))

    def test_wrong_key_and_tamper_never_expose_a_partially_decrypted_restore(self):
        archive = self.exported()
        wrong = self.root / "wrong-identity.txt"
        with wrong.open("wb") as output:
            platform_files.set_private_file(output.fileno())
            subprocess.run([str(AGE.with_name("age-keygen.exe" if os.name == "nt" else "age-keygen"))], stdout=output, stderr=subprocess.DEVNULL, check=True)
        before = set(RESTORE_TEMP_ROOT.glob("stt-recovery-check-*"))
        with self.assertRaises(BackupError):
            self.verify(archive, wrong)
        changed = bytearray(archive.read_bytes()); changed[-1] ^= 1
        damaged = self.root / "damaged.age"; damaged.write_bytes(changed)
        with self.assertRaises(BackupError):
            self.verify(damaged)
        self.assertEqual(set(RESTORE_TEMP_ROOT.glob("stt-recovery-check-*")), before)

    def test_tar_rejects_traversal_absolute_links_duplicate_unknown_and_extended_headers(self):
        cases = [
            [("../outside", b"x", tarfile.REGTYPE)],
            [("/tmp/outside", b"x", tarfile.REGTYPE)],
            [("settings.env", b"", tarfile.SYMTYPE)],
            [("settings.env", b"", tarfile.LNKTYPE)],
            [("settings.env", b"a", tarfile.REGTYPE), ("settings.env", b"b", tarfile.REGTYPE)],
            [("recording.wav", b"audio", tarfile.REGTYPE)],
            [("pax", b"", tarfile.XHDTYPE)],
            [("longname", b"", tarfile.GNUTYPE_LONGNAME)],
        ]
        for members in cases:
            with self.subTest(kind=members[0][2], name=members[0][0]):
                with self.assertRaises(BackupError):
                    self.verify(self.encrypt_tar(members))
        self.assertFalse((self.root / "outside").exists())

    def test_tar_size_bomb_is_rejected_before_extracting_payload(self):
        header = tarfile.TarInfo("database.sqlite3"); header.size = MAX_DATABASE_BYTES + 1
        raw = self.root / "bomb.tar"; raw.write_bytes(header.tobuf(format=tarfile.USTAR_FORMAT) + bytes(1024))
        encrypted = self.root / "bomb.age"
        recipient = self.manager._config()["recipient"]
        with encrypted.open("wb") as output:
            subprocess.run([str(AGE), "-r", recipient, str(raw)], stdout=output, stderr=subprocess.DEVNULL, check=True)
        with self.assertRaises(BackupError):
            self.verify(encrypted)

    def test_manifest_tamper_and_account_mismatch_fail_closed(self):
        result = self.verify(self.exported())
        directory = Path(result["directory"])
        members = [(path.name, path.read_bytes(), tarfile.REGTYPE) for path in directory.iterdir()]
        altered = [(name, content + b"\n" if name == "settings.env" else content, kind) for name, content, kind in members]
        with self.assertRaisesRegex(BackupError, "manifest_hash_mismatch"):
            self.verify(self.encrypt_tar(altered))
        self.environment.write_bytes(b"ACCOUNT_USERNAMES=user-alpha,user-gamma\n")
        with self.assertRaisesRegex(BackupError, "account_mismatch"):
            self.manager.export()

    def test_restore_checks_accounts_even_when_changed_environment_has_a_matching_manifest_hash(self):
        result = self.verify(self.exported())
        directory = Path(result["directory"])
        replacement = b"ACCOUNT_USERNAMES=user-alpha,user-gamma\n"
        manifest = json.loads((directory / "manifest.json").read_bytes())
        manifest["files"]["settings.env"] = {"size": len(replacement), "sha256": hashlib.sha256(replacement).hexdigest()}
        members = [(path.name, replacement if path.name == "settings.env" else
                    json.dumps(manifest).encode() if path.name == "manifest.json" else path.read_bytes(), tarfile.REGTYPE)
                   for path in directory.iterdir()]
        with self.assertRaisesRegex(BackupError, "account_mismatch"):
            self.verify(self.encrypt_tar(members))

    def test_restore_rechecks_database_foreign_keys_after_manifest_hash_validation(self):
        result = self.verify(self.exported())
        directory = Path(result["directory"])
        with closing(sqlite3.connect(directory / "database.sqlite3")) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("INSERT INTO lectures(id,username,title,language,created_at) VALUES ('bad','no-such-user','Synthetic','ko','2026-01-01')")
        manifest = json.loads((directory / "manifest.json").read_bytes())
        content = (directory / "database.sqlite3").read_bytes()
        manifest["files"]["database.sqlite3"] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        members = [(path.name, json.dumps(manifest).encode() if path.name == "manifest.json" else path.read_bytes(), tarfile.REGTYPE)
                   for path in directory.iterdir()]
        with self.assertRaisesRegex(BackupError, "database_foreign_keys"):
            self.verify(self.encrypt_tar(members))

    def test_configuration_changed_during_snapshot_is_not_published(self):
        from server import recovery_backup as module
        original = module._database_info
        calls = 0
        def changed(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs); calls += 1
            if calls == 2:
                private_write(self.environment, self.environment.read_bytes() + b"CHANGED=1\n")
            return result
        with mock.patch.object(module, "_database_info", side_effect=changed):
            with self.assertRaisesRegex(BackupError, "configuration_changed"):
                self.manager.export()
        self.assertEqual(self.copies, [])

    def test_database_foreign_key_violation_is_rejected_without_fixing_source(self):
        with closing(sqlite3.connect(self.settings.database_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("INSERT INTO lectures(id,username,title,language,created_at) VALUES ('bad','no-such-user','Synthetic','ko','2026-01-01')")
        with self.assertRaisesRegex(BackupError, "database_foreign_keys"):
            self.manager.export()
        with closing(sqlite3.connect(self.settings.database_path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lectures").fetchone()[0], 1)

    def test_failed_destination_retries_identical_bundle_and_keeps_last_success(self):
        previous = self.exported(); previous_bytes = previous.read_bytes()
        attempts = []
        def failed(source, pending, cancel, deadline):
            attempts.append(dict(pending)); raise OSError("synthetic failure containing private text")
        self.manager.copy_runner = failed
        with mock.patch("server.recovery_backup.time.sleep"):
            with self.assertRaisesRegex(BackupError, "destination_unavailable"):
                self.manager.export()
        self.assertEqual(len(attempts), 3)
        self.assertEqual(attempts, [attempts[0]] * 3)
        self.assertEqual(previous.read_bytes(), previous_bytes)
        status = self.manager.status()
        self.assertTrue(status["pending_copy"])
        self.assertNotIn("private text", json.dumps(status))
        self.manager.copy_runner = self.copy
        retried = self.manager.export()
        self.assertEqual(retried["bundle_id"] + ".age", attempts[0]["name"])
        self.assertEqual(previous.read_bytes(), previous_bytes)

    def test_copy_response_loss_can_retry_without_overwriting_existing_ciphertext(self):
        attempts = 0
        def response_lost(source, pending, cancel, deadline):
            nonlocal attempts
            self.copy(source, pending, cancel, deadline); attempts += 1
            if attempts == 1:
                raise OSError("lost synthetic response")
        self.manager.copy_runner = response_lost
        with mock.patch("server.recovery_backup.time.sleep"):
            self.manager.export()
        self.assertEqual(attempts, 2)
        self.assertEqual(len(list(self.destination.iterdir())), 1)
        self.assertEqual(self.copies[0], self.copies[1])

    def test_existing_destination_with_different_bytes_is_never_overwritten(self):
        collision = []
        def occupied(source, pending, cancel, deadline):
            path = self.destination / pending["name"]
            if not collision:
                path.write_bytes(b"synthetic-existing-file-do-not-overwrite"); collision.append(path)
            self.copy(source, pending, cancel, deadline)
        self.manager.copy_runner = occupied
        with mock.patch("server.recovery_backup.time.sleep"), self.assertRaises(BackupError):
            self.manager.export()
        self.assertEqual(collision[0].read_bytes(), b"synthetic-existing-file-do-not-overwrite")
        self.assertTrue(self.manager.status()["pending_copy"])

    def test_symlink_sources_and_changed_pending_ciphertext_are_rejected(self):
        original = self.environment.read_bytes()
        target = self.root / "elsewhere.env"; private_write(target, original)
        self.environment.unlink()
        try:
            self.environment.symlink_to(target)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows file symlink fixture requires Developer Mode or symlink privilege")
            raise
        with self.assertRaisesRegex(BackupError, "unsafe_path"):
            self.manager.export()
        self.environment.unlink(); private_write(self.environment, original)

    def test_changed_pending_ciphertext_is_rejected(self):
        self.manager.copy_runner = lambda *_: (_ for _ in ()).throw(OSError("fake outage"))
        with mock.patch("server.recovery_backup.time.sleep"), self.assertRaises(BackupError):
            self.manager.export()
        pending = self.manager._state()["pending"]
        path = self.manager.config_dir / "staging" / pending["name"]
        path.write_bytes(path.read_bytes() + b"tamper")
        with self.assertRaisesRegex(BackupError, "pending_bundle_changed"):
            self.manager.export()

    def test_windows_copy_adapter_only_passes_ciphertext_and_accepts_verified_metadata(self):
        # Exercise the actual adapter argv / result contract without Windows
        # access or writing the real operator-selected drive.
        from server import recovery_backup as module
        commands = []
        def fake_run(arguments, output, cancel, deadline):
            commands.append(arguments)
            if arguments[0] == "wslpath":
                leaf = Path(arguments[-1]).name
                output.write(("\\\\wsl.localhost\\Synthetic\\fixture\\" + leaf + "\n").encode())
            else:
                expected = {"verified": True, "bytes": int(arguments[arguments.index("-ExpectedBytes") + 1]),
                            "sha256": arguments[arguments.index("-ExpectedSha256") + 1]}
                output.write(json.dumps(expected).encode())
        self.manager.copy_runner = self.manager._copy_to_windows
        # Build real ciphertext first; _run is mocked only after encryption.
        built = self.manager._build(self.manager._config()["recipient"], None, time.monotonic() + 30)
        with mock.patch.object(module, "_run", side_effect=fake_run):
            self.manager._copy_to_windows(self.manager.config_dir / "staging" / built["name"], built, None, time.monotonic() + 30)
        self.assertEqual(len(commands), 1 if os.name == "nt" else 3)
        self.assertIn("-File", commands[-1]); self.assertIn("-ExpectedSha256", commands[-1])
        command_text = json.dumps(commands)
        self.assertNotIn("identity.txt", command_text); self.assertNotIn("token.json", command_text)
        self.assertNotIn("settings.env", command_text); self.assertNotIn("/mnt/d", command_text)

    def test_singleton_export_lock_rejects_concurrent_job(self):
        with self.manager._exclusive():
            with self.assertRaisesRegex(BackupError, "already_running"):
                self.manager.export()

    def test_disabled_scheduler_does_not_read_or_create_private_configuration(self):
        manager = RecoveryBackupManager(self.settings, project_dir=self.root,
                                        config_dir=self.root / "missing-config", age_binary=AGE)
        scheduler = BackupScheduler(manager)
        self.assertEqual(scheduler.status(), {"configured": False, "enabled": False, "running": False})
        self.assertFalse(scheduler.start()); self.assertTrue(scheduler.stop())
        self.assertFalse(manager.config_dir.exists())

    def test_status_whitelists_fields_and_never_returns_paths_keys_or_accounts(self):
        private_write(self.manager.config_dir / "state.json", json.dumps({"failure_count": 2,
                      "last_success_at": "/synthetic-private/path", "last_error_code": "synthetic-secret",
                      "accounts": list(ACCOUNTS)}).encode())
        text = json.dumps(self.manager.status())
        self.assertNotIn("synthetic", text); self.assertNotIn("user-alpha", text)
        self.assertNotIn("recipient", text); self.assertNotIn("directory", text)


class BackupSchedulerTests(unittest.TestCase):
    def test_failed_thread_start_preserves_original_error_and_remains_safe_to_stop(self):
        manager = mock.Mock()
        manager.status.return_value = {"enabled": True}
        scheduler = BackupScheduler(manager)
        failure = RuntimeError("synthetic thread allocation failure")
        with mock.patch("server.recovery_backup.threading.Thread.start", side_effect=failure):
            with self.assertRaises(RuntimeError) as captured:
                scheduler.start()
        self.assertIs(captured.exception, failure)
        self.assertIsNone(scheduler.thread)
        self.assertTrue(scheduler.stop_event.is_set())
        self.assertTrue(scheduler.stop(timeout=0))
        self.assertTrue(scheduler.stop(timeout=0))
        manager.export.assert_not_called()

    def test_explicit_retry_after_thread_start_failure_can_start_one_worker(self):
        manager = mock.Mock()
        manager.status.return_value = {"enabled": True, "last_success_at": int(time.time())}
        scheduler = BackupScheduler(manager)
        self.addCleanup(scheduler.stop)
        with mock.patch("server.recovery_backup.threading.Thread.start", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                scheduler.start()
        self.assertTrue(scheduler.start())
        self.assertFalse(scheduler.start())
        self.assertTrue(scheduler.stop())
        manager.export.assert_not_called()

    def test_worker_stops_cooperatively_and_cannot_be_cloned(self):
        started = threading.Event()
        class Manager:
            calls = 0
            def status(self):
                return {"enabled": True}
            def export(self, cancelled):
                self.calls += 1; started.set(); cancelled.wait(5)
                raise BackupError("cancelled")
        manager = Manager(); scheduler = BackupScheduler(manager)
        self.assertTrue(scheduler.start()); self.assertTrue(started.wait(2))
        self.assertFalse(scheduler.start()); scheduler.request_shutdown()
        self.assertTrue(scheduler.stop_event.is_set()); self.assertTrue(scheduler.stop())
        self.assertEqual(manager.calls, 1)

    def test_recent_success_waits_instead_of_creating_another_daily_bundle(self):
        manager = mock.Mock()
        manager.status.return_value = {"enabled": True, "last_success_at": int(time.time())}
        scheduler = BackupScheduler(manager)
        self.assertTrue(scheduler.start()); self.assertTrue(scheduler.stop())
        manager.export.assert_not_called()

    def test_external_process_timeout_is_bounded_and_does_not_print_stderr(self):
        with self.assertRaisesRegex(BackupError, "timeout"):
            _run([sys.executable, "-c", "import time; time.sleep(10)"], subprocess.DEVNULL,
                 threading.Event(), time.monotonic() + 0.1)

    def test_windows_copy_source_has_fixed_destination_and_no_overwrite_fallback(self):
        source = (PROJECT_DIR / "scripts" / "copy-backup-to-d.ps1").read_text()
        self.assertIn("$destination = 'D:\\STT-Backups'", source)
        self.assertIn("[IO.FileMode]::CreateNew", source)
        self.assertIn("Get-VerifiedHash $partial", source)
        self.assertIn("[IO.File]::Move($partial, $final)", source)
        self.assertNotIn("Copy-Item", source)
        self.assertNotIn("Move-Item", source)
        self.assertNotIn("/mnt/d", source)


if __name__ == "__main__":
    unittest.main()
