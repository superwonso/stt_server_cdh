"""Offline Windows backup file handling; no age, Drive, or operational data."""
from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import platform_files as files
from server import recovery_backup as backup
from server.db import Database
from server.settings import Settings


@unittest.skipUnless(os.name == "nt", "Native Windows backup ACL and file handling")
class WindowsBackupFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="yeobaek-backup-fixture-")
        self.root = Path(self.temporary.name) / "private"
        files.ensure_private_directory(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_private_temporary_directory_does_not_inherit_public_parent_acl(self):
        # The outer fixture has ordinary inherited permissions. The new child
        # must be private from creation; existing parent ACLs are not rewritten.
        generated = files.make_private_temporary_directory(
            prefix="synthetic-private-", directory=Path(self.temporary.name))
        try:
            files.validate_private_path(generated, directory=True)
            files.atomic_write_private(generated / "synthetic", b"fixture")
        finally:
            shutil.rmtree(generated)

    def test_failed_authentication_does_not_parse_and_removes_private_plaintext(self):
        identity = self.root / "synthetic.identity"
        archive = self.root / "synthetic.age"
        files.atomic_write_private(identity, b"synthetic identity")
        files.atomic_write_private(archive, b"synthetic ciphertext")
        destinations = []

        def failed_decrypt(arguments, output, cancel, deadline):
            directory = Path(output.name).parent
            destinations.append(directory)
            files.validate_private_path(directory, directory=True)
            files.validate_private_path(Path(output.name))
            output.write(b"partial unauthenticated plaintext")
            output.flush()
            raise backup.BackupError("external_tool_failed")

        with mock.patch.object(backup, "_run", side_effect=failed_decrypt) as run, mock.patch.object(
                backup.tarfile.TarInfo, "frombuf", side_effect=AssertionError("Must authenticate before parsing")):
            with self.assertRaises(backup.BackupError) as raised:
                backup.verify_recovery_archive(archive, identity)
        self.assertEqual(raised.exception.code, "external_tool_failed")
        run.assert_called_once()
        self.assertEqual(len(destinations), 1)
        self.assertFalse(destinations[0].exists())
        self.assertEqual(identity.read_bytes(), b"synthetic identity")
        self.assertEqual(archive.read_bytes(), b"synthetic ciphertext")

    def test_build_snapshots_synthetic_database_in_private_workspace_and_cleans_it(self):
        accounts = ("synthetic-alpha", "synthetic-beta")
        settings = Settings(data_dir=self.root / "data", model_cache_dir=self.root / "models", accounts=accounts)
        database = Database(settings.database_path, accounts)
        database.initialize()
        environment = self.root / "settings.env"
        env_bytes = b"ACCOUNT_USERNAMES=synthetic-alpha,synthetic-beta\n"
        files.atomic_write_private(environment, env_bytes)
        manager = backup.RecoveryBackupManager(settings, project_dir=self.root)
        files.ensure_private_directory(manager.config_dir)
        workspaces = []

        def fake_encrypt(arguments, output, cancel, deadline):
            # This verifies storage plumbing only, not age encryption.
            payload = Path(arguments[-1])
            workspaces.append(payload.parent)
            files.validate_private_path(payload.parent, directory=True)
            files.validate_private_path(payload)
            with tarfile.open(payload, "r:") as archive:
                self.assertEqual(set(archive.getnames()), {"database.sqlite3", "settings.env", "manifest.json"})
            output.write(b"synthetic ciphertext for file handling only")

        with mock.patch.dict(os.environ, {"STT_ENV_FILE": str(environment)}), mock.patch.object(
                backup, "_run", side_effect=fake_encrypt):
            pending = manager._build("synthetic-recipient", None, time.monotonic() + 30)
        ciphertext = manager.config_dir / "staging" / pending["name"]
        files.validate_private_path(ciphertext)
        self.assertEqual(ciphertext.stat().st_size, pending["bytes"])
        self.assertEqual(len(workspaces), 1)
        self.assertFalse(workspaces[0].exists())
        self.assertEqual(environment.read_bytes(), env_bytes)
        self.assertEqual(backup._database_info(database.path)["accounts"], accounts)

    def test_omitted_wav_count_does_not_traverse_a_junction(self):
        recordings = self.root / "recordings"
        recordings.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "synthetic.wav").write_bytes(b"synthetic")
        junction = recordings / "junction"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            self.skipTest("Creating a test junction is unavailable")
        try:
            self.assertEqual(backup._local_wav_count(recordings), 0)
            self.assertEqual((outside / "synthetic.wav").read_bytes(), b"synthetic")
        finally:
            junction.rmdir()

    def test_backup_operation_lock_excludes_another_manager_and_releases(self):
        settings = Settings(data_dir=self.root / "data", model_cache_dir=self.root / "models",
                            accounts=("synthetic-alpha", "synthetic-beta"))
        first = backup.RecoveryBackupManager(settings, project_dir=self.root)
        second = backup.RecoveryBackupManager(settings, project_dir=self.root)
        files.ensure_private_directory(first.config_dir)
        with first._exclusive():
            with self.assertRaises(backup.BackupError) as raised:
                with second._exclusive():
                    self.fail("A second manager must not enter")
            self.assertEqual(raised.exception.code, "already_running")
        with second._exclusive():
            self.assertTrue(second._running)
        self.assertFalse(second._running)


if __name__ == "__main__":
    unittest.main()
