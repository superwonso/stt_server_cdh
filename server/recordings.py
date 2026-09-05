from __future__ import annotations

import os
import stat
import struct
import threading
import uuid
from pathlib import Path


SAMPLE_RATE = 16_000
BYTES_PER_FRAME = 2
WAV_HEADER_BYTES = 44
_WAV_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")
_ZERO_BLOCK = b"\0" * (1024 * 1024)


class RecordingError(RuntimeError):
    """Base error for private recording storage."""


class RecordingConflict(RecordingError):
    """A chunk does not match the already stored recording timeline."""


class RecordingCapacityError(RecordingError):
    """The configured quota or free-space reserve would be exceeded."""


class RecordingCorruptError(RecordingError):
    """An app-owned recording is not the expected PCM WAV format."""


def _header(data_bytes: int) -> bytes:
    return _WAV_HEADER.pack(
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        SAMPLE_RATE,
        SAMPLE_RATE * BYTES_PER_FRAME,
        BYTES_PER_FRAME,
        16,
        b"data",
        data_bytes,
    )


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short recording write")
        view = view[written:]


def _valid_header(value: bytes, data_bytes: int, *, allow_stale_sizes: bool = False) -> bool:
    if len(value) != WAV_HEADER_BYTES:
        return False
    try:
        fields = _WAV_HEADER.unpack(value)
    except struct.error:
        return False
    expected = _WAV_HEADER.unpack(_header(data_bytes))
    if allow_stale_sizes:
        return fields[:1] + fields[2:12] == expected[:1] + expected[2:12]
    return fields == expected


class RecordingStore:
    """Store only normalized mono PCM audio, never uploaded video/container data."""

    def __init__(
        self,
        root: Path,
        accounts: tuple[str, ...],
        *,
        max_total_bytes: int,
        min_free_bytes: int,
        max_seconds: int,
        max_gap_seconds: int = 60,
        create_directories: bool = True,
    ):
        self.root = Path(root)
        self.accounts = frozenset(accounts)
        self.max_total_bytes = max_total_bytes
        self.min_free_bytes = min_free_bytes
        self.max_frames = max_seconds * SAMPLE_RATE
        self.max_gap_frames = max_gap_seconds * SAMPLE_RATE
        self.lock = threading.RLock()
        if create_directories:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
            for account in accounts:
                directory = self.root / account
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory.chmod(0o700)

    def path(self, username: str, lecture_id: str) -> Path:
        if username not in self.accounts:
            raise RecordingError("unknown recording owner")
        try:
            normalized = str(uuid.UUID(lecture_id))
        except (ValueError, AttributeError) as error:
            raise RecordingError("invalid recording identifier") from error
        return self.root / username / f"{normalized}.wav"

    @staticmethod
    def _open_flags(write: bool = False) -> int:
        flags = os.O_RDWR | os.O_CREAT if write else os.O_RDONLY
        return flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)

    def _inspect_descriptor(self, descriptor: int, *, repair_stale_header: bool = False) -> tuple[int, int]:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise RecordingCorruptError("recording is not a regular file")
        size = details.st_size
        if size < WAV_HEADER_BYTES or (size - WAV_HEADER_BYTES) % BYTES_PER_FRAME:
            raise RecordingCorruptError("recording has an invalid size")
        data_bytes = size - WAV_HEADER_BYTES
        os.lseek(descriptor, 0, os.SEEK_SET)
        value = os.read(descriptor, WAV_HEADER_BYTES)
        if not _valid_header(value, data_bytes, allow_stale_sizes=repair_stale_header):
            raise RecordingCorruptError("recording has an invalid WAV header")
        if repair_stale_header and not _valid_header(value, data_bytes):
            repaired = _header(data_bytes)
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                _write_all(descriptor, repaired)
                os.fsync(descriptor)
            except BaseException:
                # A short header write must not turn a recoverable stale size
                # into an unreadable recording.
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    _write_all(descriptor, value)
                    os.fsync(descriptor)
                except OSError:
                    pass
                raise
        return size, data_bytes // BYTES_PER_FRAME

    def _used_bytes(self) -> int:
        total = 0
        for account in self.accounts:
            directory = self.root / account
            try:
                candidates = tuple(directory.iterdir())
            except FileNotFoundError:
                continue
            for candidate in candidates:
                try:
                    details = candidate.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(details.st_mode) and candidate.suffix == ".wav":
                    total += details.st_size
        return total

    def ensure_capacity(self, additional_bytes: int, *, other_reserved_bytes: int = 0) -> None:
        if additional_bytes < 0 or other_reserved_bytes < 0:
            raise ValueError("capacity reservations must not be negative")
        if self._used_bytes() + additional_bytes > self.max_total_bytes:
            raise RecordingCapacityError("recording quota exceeded")
        free = os.statvfs(self.root)
        free_bytes = free.f_bavail * free.f_frsize
        if additional_bytes + other_reserved_bytes + self.min_free_bytes > free_bytes:
            raise RecordingCapacityError("recording free-space reserve would be exceeded")

    def write_chunk(
        self,
        username: str,
        lecture_id: str,
        *,
        start_seconds: float,
        overlap_seconds: float,
        pcm: bytes,
    ) -> bool:
        if len(pcm) % BYTES_PER_FRAME:
            raise RecordingConflict("PCM data is not frame-aligned")
        start_exact = start_seconds * SAMPLE_RATE
        overlap_exact = overlap_seconds * SAMPLE_RATE
        start_frame = round(start_exact)
        overlap_frames = round(overlap_exact)
        if abs(start_exact - start_frame) > 0.01 or abs(overlap_exact - overlap_frames) > 0.01:
            raise RecordingConflict("recording timestamps are not sample-aligned")
        total_frames = len(pcm) // BYTES_PER_FRAME
        if not 0 <= overlap_frames <= total_frames:
            raise RecordingConflict("recording overlap exceeds the chunk")
        fresh_pcm = pcm[overlap_frames * BYTES_PER_FRAME :]
        if not fresh_pcm:
            return self.available(username, lecture_id)
        target_frame = start_frame + overlap_frames
        end_frame = target_frame + len(fresh_pcm) // BYTES_PER_FRAME
        if target_frame < 0 or end_frame > self.max_frames:
            raise RecordingConflict("recording timeline exceeds the duration limit")

        path = self.path(username, lecture_id)
        with self.lock:
            try:
                path.lstat()
                existed = True
            except FileNotFoundError:
                existed = False
            try:
                descriptor = os.open(path, self._open_flags(write=True), 0o600)
            except OSError as error:
                raise RecordingCorruptError("recording cannot be opened safely") from error
            original_size = 0
            original_header = b""
            try:
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode):
                    raise RecordingCorruptError("recording is not a regular file")
                original_size = details.st_size
                if details.st_size == 0:
                    _write_all(descriptor, _header(0))
                    os.fsync(descriptor)
                    current_frames = 0
                else:
                    _, current_frames = self._inspect_descriptor(descriptor, repair_stale_header=True)
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    original_header = os.read(descriptor, WAV_HEADER_BYTES)
                if target_frame > current_frames + self.max_gap_frames:
                    raise RecordingConflict("recording chunk is too far from the stored timeline")

                overlap_with_file = max(0, min(len(fresh_pcm), (current_frames - target_frame) * BYTES_PER_FRAME))
                if overlap_with_file:
                    os.lseek(descriptor, WAV_HEADER_BYTES + target_frame * BYTES_PER_FRAME, os.SEEK_SET)
                    previous = os.read(descriptor, overlap_with_file)
                    if previous != fresh_pcm[:overlap_with_file]:
                        raise RecordingConflict("recording chunk conflicts with stored audio")

                additional_frames = max(0, end_frame - current_frames)
                additional_bytes = additional_frames * BYTES_PER_FRAME
                if additional_bytes:
                    self.ensure_capacity(additional_bytes)
                if target_frame > current_frames:
                    gap_bytes = (target_frame - current_frames) * BYTES_PER_FRAME
                    os.lseek(descriptor, 0, os.SEEK_END)
                    while gap_bytes:
                        block = _ZERO_BLOCK[: min(len(_ZERO_BLOCK), gap_bytes)]
                        _write_all(descriptor, block)
                        gap_bytes -= len(block)
                unwritten = fresh_pcm[overlap_with_file:]
                if unwritten:
                    os.lseek(
                        descriptor,
                        WAV_HEADER_BYTES + (target_frame * BYTES_PER_FRAME) + overlap_with_file,
                        os.SEEK_SET,
                    )
                    _write_all(descriptor, unwritten)
                    os.fsync(descriptor)
                final_size = os.fstat(descriptor).st_size
                data_bytes = final_size - WAV_HEADER_BYTES
                os.lseek(descriptor, 0, os.SEEK_SET)
                _write_all(descriptor, _header(data_bytes))
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o600)
            except BaseException:
                if not existed:
                    try:
                        os.close(descriptor)
                    finally:
                        descriptor = -1
                        path.unlink(missing_ok=True)
                else:
                    # Keep a failed internal write from poisoning all future
                    # retries. A DB failure after this method returns is
                    # intentionally handled by byte-exact idempotent replay.
                    try:
                        os.ftruncate(descriptor, original_size)
                        if original_header:
                            os.lseek(descriptor, 0, os.SEEK_SET)
                            _write_all(descriptor, original_header)
                        os.fsync(descriptor)
                    except OSError:
                        pass
                raise
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        return True

    def open_info(self, username: str, lecture_id: str) -> dict | None:
        """Return a validated open descriptor so a later path swap is harmless."""

        path = self.path(username, lecture_id)
        with self.lock:
            try:
                descriptor = os.open(path, self._open_flags())
            except FileNotFoundError:
                return None
            except OSError as error:
                raise RecordingCorruptError("recording cannot be opened safely") from error
            try:
                try:
                    size, frames = self._inspect_descriptor(descriptor)
                except OSError as error:
                    raise RecordingCorruptError("recording cannot be read safely") from error
            except BaseException:
                os.close(descriptor)
                raise
            if frames == 0:
                os.close(descriptor)
                return None
            return {
                "path": path,
                "descriptor": descriptor,
                "stat": os.fstat(descriptor),
                "bytes": size,
                "duration_seconds": frames / SAMPLE_RATE,
            }

    def info(self, username: str, lecture_id: str) -> dict | None:
        recording = self.open_info(username, lecture_id)
        if recording is None:
            return None
        try:
            return {
                "path": recording["path"],
                "bytes": recording["bytes"],
                "duration_seconds": recording["duration_seconds"],
            }
        finally:
            os.close(recording["descriptor"])

    def available(self, username: str, lecture_id: str) -> bool:
        try:
            return self.info(username, lecture_id) is not None
        except RecordingError:
            return False

    def delete(self, username: str, lecture_id: str) -> None:
        path = self.path(username, lecture_id)
        with self.lock:
            try:
                details = path.lstat()
            except FileNotFoundError:
                return
            if stat.S_ISDIR(details.st_mode):
                raise RecordingCorruptError("recording is not a regular file")
            # unlink() removes a corrupt file, FIFO, or symlink itself and
            # never follows its target. Download remains fail-closed, while a
            # damaged app-owned path cannot make a private lesson undeletable.
            path.unlink()

    def remove_orphans(self, expected: dict[str, set[str]]) -> None:
        """Remove only UUID WAV files in app-owned account directories."""

        with self.lock:
            for username in self.accounts:
                directory = self.root / username
                for candidate in directory.glob("*.wav"):
                    try:
                        lecture_id = str(uuid.UUID(candidate.stem))
                    except ValueError:
                        continue
                    if lecture_id not in expected.get(username, set()):
                        self.delete(username, lecture_id)
