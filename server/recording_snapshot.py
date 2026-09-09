"""Read-only, bounded streaming of an app-owned append-only PCM prefix.

No descriptor is retained by a ticket. The private inode/size identity is
checked when opening a download. Later appends only modify the on-disk header
and suffix, so a synthesized header and pread() preserve the captured WAV.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass

from .recordings import BYTES_PER_FRAME, SAMPLE_RATE, WAV_HEADER_BYTES, RecordingCorruptError, _header

READ_BLOCK_BYTES = 64 * 1024


@dataclass(frozen=True)
class RecordingSnapshot:
    device: int
    inode: int
    total_bytes: int

    @classmethod
    def capture(cls, recording: dict, max_frames: int) -> "RecordingSnapshot":
        total = recording["bytes"]
        if (type(total) is not int or total <= WAV_HEADER_BYTES
                or (total - WAV_HEADER_BYTES) % BYTES_PER_FRAME
                or total - WAV_HEADER_BYTES > max_frames * BYTES_PER_FRAME
                or total > 0xFFFFFFFF + 8):
            raise RecordingCorruptError("recording snapshot size is invalid")
        details = recording["stat"]
        if details.st_size != total:
            raise RecordingCorruptError("recording snapshot size changed")
        return cls(details.st_dev, details.st_ino, total)

    @property
    def duration_seconds(self) -> float:
        return (self.total_bytes - WAV_HEADER_BYTES) / (SAMPLE_RATE * BYTES_PER_FRAME)

    def matches(self, recording: dict) -> bool:
        details = recording["stat"]
        return (details.st_dev == self.device and details.st_ino == self.inode
                and recording["bytes"] >= self.total_bytes)


class SnapshotStream:
    def __init__(self, descriptor: int, snapshot: RecordingSnapshot, *, on_close=None):
        self.descriptor = descriptor
        self.snapshot = snapshot
        self.header = _header(snapshot.total_bytes - WAV_HEADER_BYTES)
        self._on_close = on_close
        self._close_lock = threading.Lock()

    def close(self) -> None:
        with self._close_lock:
            descriptor, self.descriptor = self.descriptor, -1
            callback, self._on_close = self._on_close, None
        try:
            if descriptor >= 0:
                os.close(descriptor)
        finally:
            if callback is not None:
                callback()

    def iter_bytes(self, start: int, end: int):
        try:
            if not 0 <= start < end <= self.snapshot.total_bytes:
                raise RecordingCorruptError("recording snapshot range is invalid")
            position = start
            while position < end:
                # Appends are fine; truncation is not. A later read failure
                # aborts the transport rather than silently declaring success.
                with self._close_lock:
                    if os.fstat(self.descriptor).st_size < self.snapshot.total_bytes:
                        raise RecordingCorruptError("recording snapshot was truncated")
                    if position < WAV_HEADER_BYTES:
                        next_position = min(end, WAV_HEADER_BYTES)
                        value = self.header[position:next_position]
                    else:
                        next_position = min(end, position + READ_BLOCK_BYTES)
                        value = os.pread(self.descriptor, next_position - position, position)
                        if len(value) != next_position - position:
                            raise RecordingCorruptError("recording snapshot read was short")
                position = next_position
                yield value
        finally:
            self.close()
