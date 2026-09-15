"""Offline native Drive credential/lock and process-detection regression tests."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
if os.name == "nt":
    import psutil

from scripts.google_drive import GoogleDriveSetupError, PROJECT_ROOT, server_is_running
from server import platform_files as files
from server.drive_storage import DRIVE_FILE_SCOPE, GOOGLE_TOKEN_URL, GoogleDriveStorage


@unittest.skipUnless(os.name == "nt", "Native Windows credential and process adapters")
class WindowsDriveTests(unittest.TestCase):
    def test_offline_token_refresh_replaces_private_file_under_shared_lock(self):
        with tempfile.TemporaryDirectory(prefix="yeobaek-drive-test-") as temporary:
            root = Path(temporary) / "drive"
            files.ensure_private_directory(root)
            token = root / "token.json"
            files.atomic_write_private(token, json.dumps({
                "type": "authorized_user", "client_id": "synthetic-client.apps.googleusercontent.com",
                "client_secret": "synthetic-secret", "refresh_token": "synthetic-refresh",
                "token_uri": GOOGLE_TOKEN_URL, "scopes": [DRIVE_FILE_SCOPE],
            }).encode())
            refresh_count = 0

            def handler(request):
                nonlocal refresh_count
                if str(request.url) == GOOGLE_TOKEN_URL:
                    refresh_count += 1
                    descriptor = files.open_file(root / "token.lock", os.O_RDWR, private=True)
                    try:
                        with self.assertRaises(BlockingIOError):
                            with files.file_lock(descriptor, blocking=False):
                                pass
                    finally:
                        os.close(descriptor)
                    return httpx.Response(200, json={"access_token": "synthetic-access", "expires_in": 3600,
                                                    "scope": DRIVE_FILE_SCOPE, "token_type": "Bearer"})
                return httpx.Response(200, json={"files": []})

            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                storage = GoogleDriveStorage.from_token_file(token, client=client)
                self.assertTrue(storage.verify_connection())
                self.assertTrue(storage.verify_connection())
                self.assertEqual(refresh_count, 1)
            files.validate_private_path(token)
            files.validate_private_path(root / "token.lock")

    def test_process_scan_finds_exact_project_without_pid_file(self):
        process = mock.Mock()
        process.info = {"pid": 98765, "name": "python.exe"}
        process.cwd.return_value = str(PROJECT_ROOT)
        process.cmdline.return_value = ["python.exe", "-m", "uvicorn", "server.app:create_app", "--factory"]
        with mock.patch("psutil.process_iter", return_value=[process]), mock.patch("os.kill") as kill:
            self.assertTrue(server_is_running(SimpleNamespace(data_dir=Path("unused"))))
            kill.assert_not_called()
        process.cwd.return_value = str(PROJECT_ROOT.parent)
        with mock.patch("psutil.process_iter", return_value=[process]):
            self.assertFalse(server_is_running(SimpleNamespace(data_dir=Path("unused"))))

    def native_process(self, *, arguments=None, cwd=None, executable=None):
        process = mock.Mock()
        process.info = {"pid": 98765, "name": "python.exe"}
        process.cwd.return_value = str(cwd or PROJECT_ROOT)
        process.cmdline.return_value = arguments or [sys.executable, "-m", "server.windows_service", "run", "--launch-id", "a" * 32]
        process.exe.return_value = executable or sys.executable
        return process

    def test_native_service_detected_without_reading_pid_or_touching_process(self):
        for executable in (sys.executable, getattr(sys, "_base_executable", sys.executable)):
            process = self.native_process(executable=executable)
            with mock.patch("psutil.process_iter", return_value=[process]), mock.patch("os.kill") as kill:
                self.assertTrue(server_is_running(SimpleNamespace(data_dir=Path("missing-private-state"))))
            kill.assert_not_called()
            process.terminate.assert_not_called()
            process.kill.assert_not_called()
        process = self.native_process(arguments=[sys.executable, "-m", "server.windows_service", "run"])
        with mock.patch("psutil.process_iter", return_value=[process]):
            self.assertTrue(server_is_running(SimpleNamespace(data_dir=Path("unused"))))

    def test_native_model_other_project_and_control_cli_are_not_api(self):
        commands = [
            [sys.executable, "-m", "server.model_process", "run"],
            [sys.executable, "-m", "server.windows_local", "run-api"],
            [sys.executable, "-m", "server.windows_service", "start"],
            [sys.executable, "-m", "server.windows_service", "status"],
            [sys.executable, "-c", "server.windows_service", "run"],
            [sys.executable, "-m", "server.windows_service", "run", "--launch-id", "not-a-launch-id"],
            ["python.exe", "-m", "server.windows_service", "run"],
        ]
        for arguments in commands:
            with self.subTest(arguments=arguments):
                process = self.native_process(arguments=arguments)
                with mock.patch("psutil.process_iter", return_value=[process]):
                    self.assertFalse(server_is_running(SimpleNamespace(data_dir=Path("unused"))))
        for process in (self.native_process(cwd=PROJECT_ROOT.parent),
                        self.native_process(executable=str(PROJECT_ROOT / "foreign" / "python.exe"))):
            with mock.patch("psutil.process_iter", return_value=[process]):
                self.assertFalse(server_is_running(SimpleNamespace(data_dir=Path("unused"))))

    def test_native_image_and_scan_access_denied_refuse_credential_replacement(self):
        process = self.native_process()
        process.exe.side_effect = psutil.AccessDenied(98765)
        with mock.patch("psutil.process_iter", return_value=[process]):
            with self.assertRaises(GoogleDriveSetupError):
                server_is_running(SimpleNamespace(data_dir=Path("unused")))
        with mock.patch("psutil.process_iter", side_effect=psutil.AccessDenied(98765)):
            with self.assertRaises(GoogleDriveSetupError):
                server_is_running(SimpleNamespace(data_dir=Path("unused")))

    def test_disappeared_native_process_does_not_block_next_candidate(self):
        exited = self.native_process()
        exited.exe.side_effect = psutil.NoSuchProcess(98765)
        live = self.native_process()
        with mock.patch("psutil.process_iter", return_value=[exited, live]):
            self.assertTrue(server_is_running(SimpleNamespace(data_dir=Path("unused"))))

    def test_inaccessible_candidate_refuses_credential_replacement(self):
        process = mock.Mock()
        process.info = {"pid": 98765, "name": "python.exe"}
        process.cmdline.side_effect = psutil.AccessDenied(98765)
        with mock.patch("psutil.process_iter", return_value=[process]):
            with self.assertRaises(GoogleDriveSetupError):
                server_is_running(SimpleNamespace(data_dir=Path("unused")))


if __name__ == "__main__":
    unittest.main()
