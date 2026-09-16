"""Synthetic cache-recovery tests. Never execute a real ipconfig /flushdns."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from server import windows_dns


class WindowsDnsTests(unittest.TestCase):
    def test_non_windows_is_a_noop(self):
        with patch.object(windows_dns, "_IS_WINDOWS", False), patch.object(
            windows_dns.subprocess, "run"
        ) as run, patch.object(windows_dns, "_system_ipconfig") as binary:
            self.assertFalse(windows_dns.clear_startup_dns_cache())
        run.assert_not_called()
        binary.assert_not_called()

    def test_invalid_timeouts_do_not_query_or_launch(self):
        with patch.object(windows_dns, "_IS_WINDOWS", True), patch.object(
            windows_dns.subprocess, "run"
        ) as run, patch.object(windows_dns, "_system_ipconfig") as binary:
            for value in (0, -1, float("nan"), float("inf"), True, None, "2", 10 ** 1000):
                with self.subTest(value_type=type(value).__name__):
                    self.assertFalse(windows_dns.clear_startup_dns_cache(timeout=value))
        run.assert_not_called()
        binary.assert_not_called()

    def test_extra_arguments_are_rejected_before_any_launch(self):
        with patch.object(windows_dns.subprocess, "run") as run:
            with self.assertRaises(TypeError):
                windows_dns.clear_startup_dns_cache("/release")
            with self.assertRaises(TypeError):
                windows_dns.clear_startup_dns_cache(command="/renew")
            with self.assertRaises(TypeError):
                windows_dns.clear_startup_dns_cache(hostname="example.invalid")
        run.assert_not_called()

    def test_direct_system_command_has_fixed_argument_and_trusted_cwd_environment(self):
        executable = Path.cwd() / "synthetic-Windows" / "System32" / "ipconfig.exe"
        with patch.object(windows_dns, "_IS_WINDOWS", True), patch.object(
            windows_dns, "_system_ipconfig", return_value=executable
        ), patch.dict(os.environ, {
            "SystemRoot": "untrusted-root", "WINDIR": "untrusted-dir",
            "PATH": "untrusted-path", "SYNTHETIC_SERVICE_SECRET": "fake-secret",
            "HTTPS_PROXY": "https://invalid.example", "USERPROFILE": "private-profile",
        }, clear=True), patch.object(windows_dns.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertTrue(windows_dns.clear_startup_dns_cache(timeout=.4))
        args, kwargs = run.call_args
        self.assertEqual(args[0], [str(executable), "/flushdns"])
        self.assertEqual(kwargs["cwd"], str(executable.parent))
        self.assertEqual(kwargs["env"], {"SystemRoot": str(executable.parent.parent), "WINDIR": str(executable.parent.parent)})
        self.assertEqual(kwargs["timeout"], .4)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertFalse(kwargs["check"])

    def test_timeout_is_capped(self):
        with patch.object(windows_dns, "_IS_WINDOWS", True), patch.object(
            windows_dns, "_system_ipconfig", return_value=Path.cwd() / "System32" / "ipconfig.exe"
        ), patch.object(windows_dns.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertTrue(windows_dns.clear_startup_dns_cache(timeout=500))
        self.assertEqual(run.call_args.kwargs["timeout"], 5.0)

    def test_missing_unsafe_or_failed_system_lookup_never_launches(self):
        with patch.object(windows_dns, "_IS_WINDOWS", True), patch.object(windows_dns.subprocess, "run") as run:
            with patch.object(windows_dns, "_system_ipconfig", return_value=None):
                self.assertFalse(windows_dns.clear_startup_dns_cache())
            for failure in (OSError("synthetic"), ValueError("synthetic"), AttributeError("synthetic")):
                with patch.object(windows_dns, "_system_ipconfig", side_effect=failure):
                    self.assertFalse(windows_dns.clear_startup_dns_cache())
        run.assert_not_called()

    def test_execution_failures_and_timeouts_return_false_without_output(self):
        with patch.object(windows_dns, "_IS_WINDOWS", True), patch.object(
            windows_dns, "_system_ipconfig", return_value=Path.cwd() / "System32" / "ipconfig.exe"
        ):
            for code in (1, 2, -1):
                with patch.object(windows_dns.subprocess, "run", return_value=Mock(returncode=code)):
                    self.assertFalse(windows_dns.clear_startup_dns_cache())
            for failure in (OSError("synthetic"), subprocess.TimeoutExpired(["synthetic"], 1)):
                with patch.object(windows_dns.subprocess, "run", side_effect=failure):
                    self.assertFalse(windows_dns.clear_startup_dns_cache())

    def fake_library(self, directory, *, returned_length=None):
        library = Mock()
        def query(buffer, size):
            self.assertEqual(size, 32768)
            buffer.value = str(directory)
            return len(str(directory)) if returned_length is None else returned_length
        library.GetSystemDirectoryW.side_effect = query
        return library

    def test_system_directory_comes_from_system_dll_api_not_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "System32"
            directory.mkdir()
            executable = directory / "ipconfig.exe"
            executable.write_bytes(b"synthetic-not-executed")
            library = self.fake_library(directory)
            with patch.object(ctypes, "WinDLL", return_value=library, create=True) as load, patch.dict(
                os.environ, {"SystemRoot": "untrusted-root", "WINDIR": "untrusted-dir", "PATH": "untrusted-path"}
            ):
                self.assertEqual(windows_dns._system_ipconfig(), executable)
            load.assert_called_once_with("kernel32.dll", winmode=0x00000800)
            self.assertEqual(library.GetSystemDirectoryW.argtypes, [ctypes.c_wchar_p, ctypes.c_uint32])
            self.assertEqual(library.GetSystemDirectoryW.restype, ctypes.c_uint32)

    def test_failed_truncated_and_nonabsolute_directory_results_are_rejected(self):
        directory = Path.cwd() / "System32"
        for path, length in ((directory, 0), (directory, 32768), (directory, 1),
                             ("relative/System32", None), (Path.cwd() / "Other", None),
                             ("\\\\invalid-server\\System32", None)):
            with self.subTest(length=length):
                library = self.fake_library(path, returned_length=length)
                with patch.object(ctypes, "WinDLL", return_value=library, create=True):
                    self.assertIsNone(windows_dns._system_ipconfig())

    def test_reparse_alias_is_not_used(self):
        directory = Path.cwd() / "System32"
        library = self.fake_library(directory)
        with patch.object(ctypes, "WinDLL", return_value=library, create=True), patch.object(
            Path, "resolve", return_value=Path.cwd() / "different" / "ipconfig.exe"
        ):
            self.assertIsNone(windows_dns._system_ipconfig())

    @unittest.skipUnless(os.name == "nt", "Read-only Windows system API validation")
    def test_actual_system_binary_location_without_executing_it(self):
        with patch.object(windows_dns.subprocess, "run") as run:
            executable = windows_dns._system_ipconfig()
        self.assertIsNotNone(executable)
        self.assertTrue(executable.is_absolute())
        self.assertEqual(executable.name, "ipconfig.exe")
        self.assertEqual(executable.parent.name.casefold(), "system32")
        run.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows disposable child lifetime integration")
    def test_timeout_kills_and_reaps_the_exact_child_without_flushing_dns(self):
        import _winapi
        executable = windows_dns._system_ipconfig()  # Location lookup only.
        observed = []
        original_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "synthetic_dns_child.py"
            marker = root / "started.pid"
            helper.write_text(
                "import os, pathlib, time\n"
                "pathlib.Path(__file__).with_name('started.pid').write_text(str(os.getpid()))\n"
                "time.sleep(30)\n", encoding="utf-8",
            )
            def substitute_synthetic_child(command, **kwargs):
                self.assertEqual(command, [str(executable), "/flushdns"])
                # Never start ipconfig in this suite. Use the base interpreter
                # directly so this synthetic worker has the same PID as Popen.
                process = original_popen([sys._base_executable, "-I", "-S", str(helper)], **kwargs)
                observed.append(process)
                return process
            before = time.monotonic()
            try:
                with patch.object(windows_dns.subprocess, "Popen", side_effect=substitute_synthetic_child):
                    self.assertFalse(windows_dns.clear_startup_dns_cache(timeout=1.5))
                self.assertLess(time.monotonic() - before, 5)
                self.assertTrue(marker.exists(), "synthetic child must start before timeout")
                self.assertEqual(len(observed), 1)
                process = observed[0]
                self.assertEqual(int(marker.read_text()), process.pid)
                self.assertIsNotNone(process.returncode)
                self.assertEqual(_winapi.WaitForSingleObject(process._handle, 0), 0)
                self.assertNotEqual(_winapi.GetExitCodeProcess(process._handle), 259)
                self.assertEqual(process.wait(timeout=0), process.returncode)
            finally:
                for process in observed:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    process._handle.Close()


if __name__ == "__main__":
    unittest.main()
