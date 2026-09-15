"""Native Windows security and descriptor behavior; synthetic data only."""
from __future__ import annotations

import ctypes
import io
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
import wave
from pathlib import Path

from server import platform_files as files
from server.recordings import RecordingConflict, RecordingStore
from server.recording_snapshot import RecordingSnapshot, SnapshotStream
from server.recordings import _header


@unittest.skipUnless(os.name == "nt", "Native Windows kernel APIs")
class WindowsFilePrimitiveTests(unittest.TestCase):
    """Public synthetic fixtures test kernel I/O independently of private ACLs.

These do not claim private-storage ACL validation. In particular they can run
inside restricted agent tokens that cannot reopen an owner-only directory.
"""
    def setUp(self):
        with tempfile.NamedTemporaryFile(prefix="yeobaek-public-fixture-", delete=False) as output:
            output.write(b"synthetic")
            self.path = Path(output.name)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_read_at_and_open_file_deletion(self):
        descriptor = files.open_file(self.path, os.O_RDONLY)
        try:
            self.path.unlink()
            self.assertEqual(files.read_at(descriptor, 9, 0), b"synthetic")
        finally:
            os.close(descriptor)

    def test_cross_process_lock_and_release(self):
        descriptor = files.open_file(self.path, os.O_RDWR)
        code = (
            "import os,sys; from pathlib import Path; from server.platform_files import open_file,file_lock; "
            "fd=open_file(Path(sys.argv[1]),os.O_RDWR)\n"
            "try:\n with file_lock(fd,blocking=False): pass\n"
            "except BlockingIOError: sys.exit(23)\n"
            "finally: os.close(fd)\n"
        )
        try:
            with files.file_lock(descriptor):
                result = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 23)
            result = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0)
        finally:
            os.close(descriptor)

    def test_snapshot_retains_exact_prefix_after_append_and_delete(self):
        initial = b"\x01\x00" * 100
        final_tail = b"\x02\x00" * 17
        self.path.write_bytes(_header(len(initial)) + initial)
        descriptor = files.open_file(self.path, os.O_RDONLY)
        info = {"bytes": 44 + len(initial), "stat": os.fstat(descriptor)}
        snapshot = RecordingSnapshot.capture(info, 1000)
        with self.path.open("r+b") as output:
            output.seek(0, os.SEEK_END)
            output.write(final_tail)
            output.seek(0)
            output.write(_header(len(initial) + len(final_tail)))
        self.path.unlink()
        stream = SnapshotStream(descriptor, snapshot)
        payload = b"".join(stream.iter_bytes(0, snapshot.total_bytes))
        with wave.open(io.BytesIO(payload), "rb") as source:
            self.assertEqual(source.getnframes(), 100)
            self.assertEqual(source.readframes(100), initial)


@unittest.skipUnless(os.name == "nt", "Native Windows kernel APIs")
class WindowsPrivateFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="yeobaek-native-test-")
        self.root = Path(self.temporary.name) / "private"
        files.ensure_private_directory(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_private_creation_and_atomic_replacement_preserve_acl(self):
        target = self.root / "synthetic.token"
        files.atomic_write_private(target, b"synthetic-one")
        files.validate_private_path(target)
        descriptor = files.open_file(target, os.O_RDONLY, private=True)
        try:
            self.assertEqual(os.read(descriptor, 100), b"synthetic-one")
        finally:
            os.close(descriptor)
        files.atomic_write_private(target, b"synthetic-two")
        self.assertEqual(target.read_bytes(), b"synthetic-two")
        files.validate_private_path(target)
        self.assertFalse(tuple(self.root.glob(".*.tmp")))

    def test_open_descriptor_survives_unlink(self):
        target = self.root / "synthetic.bin"
        files.atomic_write_private(target, b"audio")
        descriptor = files.open_file(target, os.O_RDONLY, private=True)
        try:
            target.unlink()
            self.assertEqual(os.read(descriptor, 100), b"audio")
        finally:
            os.close(descriptor)

    def test_multiply_linked_private_file_is_rejected(self):
        target = self.root / "one.bin"
        files.atomic_write_private(target, b"unchanged")
        os.link(target, self.root / "two.bin")
        with self.assertRaises(PermissionError):
            files.open_file(target, os.O_RDWR | os.O_TRUNC, private=True)
        self.assertEqual(target.read_bytes(), b"unchanged")

    def test_permissive_acl_is_rejected_without_rewriting(self):
        target = self.root / "unsafe.bin"
        descriptor = files.P()
        sid = files.current_user_sid()
        self.assertTrue(files.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"O:{sid}D:P(A;;FA;;;{sid})(A;;GR;;;WD)", 1, ctypes.byref(descriptor), None))
        try:
            attributes = files.SECURITY_ATTRIBUTES(ctypes.sizeof(files.SECURITY_ATTRIBUTES), descriptor, False)
            handle = files.kernel.CreateFileW(str(target), 0xC0020080, 7, ctypes.byref(attributes), 1, 0, None)
            self.assertNotEqual(handle, files.P(-1).value)
            files.kernel.CloseHandle(handle)
        finally:
            files.kernel.LocalFree(descriptor)
        with self.assertRaises(PermissionError):
            files.validate_private_path(target)
        with self.assertRaises(PermissionError):
            files.atomic_write_private(target, b"must-not-write")
        self.assertEqual(target.stat().st_size, 0)

    def test_junction_parent_is_rejected(self):
        junction = Path(self.temporary.name) / "junction"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(self.root)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            self.skipTest("Creating a test junction is unavailable")
        try:
            with self.assertRaises(PermissionError):
                files.ensure_private_directory(junction / "child")
            self.assertFalse((self.root / "child").exists())
        finally:
            junction.rmdir()

    def test_lock_excludes_another_process_and_releases_after_close(self):
        target = self.root / "operation.lock"
        descriptor = files.open_file(target, os.O_RDWR | os.O_CREAT, private=True)
        code = (
            "import os,sys; from pathlib import Path; from server.platform_files import open_file,file_lock; "
            "fd=open_file(Path(sys.argv[1]),os.O_RDWR,private=True)\n"
            "try:\n with file_lock(fd,blocking=False): pass\n"
            "except BlockingIOError: sys.exit(23)\n"
            "finally: os.close(fd)\n"
        )
        try:
            with files.file_lock(descriptor):
                result = subprocess.run([sys.executable, "-c", code, str(target)], capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 23, "Child must observe the native exclusive lock")
            result = subprocess.run([sys.executable, "-c", code, str(target)], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, "The lock must be reusable after release")
        finally:
            os.close(descriptor)

    def test_wav_retry_final_tail_snapshot_and_delete(self):
        store = RecordingStore(self.root / "recordings", ("synthetic-user",),
                               max_total_bytes=1024 * 1024, min_free_bytes=0, max_seconds=10)
        lecture_id = str(uuid.uuid4())
        first, tail = b"\x01\x00" * 100, b"\x02\x00" * 17
        arguments = dict(start_seconds=0, overlap_seconds=0, pcm=first, strict_contiguous=True)
        store.write_chunk("synthetic-user", lecture_id, **arguments)
        store.write_chunk("synthetic-user", lecture_id, **arguments)
        recording = store.open_info("synthetic-user", lecture_id)
        snapshot = RecordingSnapshot.capture(recording, store.max_frames)
        stream = SnapshotStream(recording["descriptor"], snapshot)
        store.write_chunk("synthetic-user", lecture_id, start_seconds=100 / 16000,
                          overlap_seconds=0, pcm=tail, strict_contiguous=True)
        prefix = b"".join(stream.iter_bytes(0, snapshot.total_bytes))
        with wave.open(io.BytesIO(prefix), "rb") as source:
            self.assertEqual(source.readframes(source.getnframes()), first)
        final = store.open_info("synthetic-user", lecture_id)
        self.assertEqual(final["bytes"], 44 + len(first) + len(tail))
        final_stream = SnapshotStream(final["descriptor"], RecordingSnapshot.capture(final, store.max_frames))
        store.delete("synthetic-user", lecture_id)
        payload = b"".join(final_stream.iter_bytes(0, final["bytes"]))
        with wave.open(io.BytesIO(payload), "rb") as source:
            self.assertEqual(source.readframes(source.getnframes()), first + tail)
        self.assertFalse(store.available("synthetic-user", lecture_id))

    def test_conflicting_retry_is_rejected_without_mutating_wav(self):
        store = RecordingStore(self.root / "recordings", ("synthetic-user",),
                               max_total_bytes=1024 * 1024, min_free_bytes=0, max_seconds=10)
        lecture_id = str(uuid.uuid4())
        store.write_chunk("synthetic-user", lecture_id, start_seconds=0, overlap_seconds=0, pcm=b"\x01\x00")
        before = store.path("synthetic-user", lecture_id).read_bytes()
        with self.assertRaises(RecordingConflict):
            store.write_chunk("synthetic-user", lecture_id, start_seconds=0, overlap_seconds=0, pcm=b"\x02\x00")
        self.assertEqual(store.path("synthetic-user", lecture_id).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
