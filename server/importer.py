from __future__ import annotations

import io
import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import av
import numpy as np


SAMPLE_RATE = 16_000
TARGET_SAMPLES = 8 * SAMPLE_RATE
MAX_CHUNK_SAMPLES = 15 * SAMPLE_RATE
OVERLAP_SAMPLES = 3 * SAMPLE_RATE
PAUSE_SAMPLES = round(0.24 * SAMPLE_RATE)
PAUSE_RMS = 0.006
MIN_AUDIO_SAMPLES = round(0.05 * SAMPLE_RATE)
MAX_SOURCE_CHANNELS = 16
MIN_SOURCE_SAMPLE_RATE = 1_000
MAX_SOURCE_SAMPLE_RATE = 384_000
MAX_SOURCE_FRAME_SAMPLES = 262_144

# Do not let an uploaded playlist or adaptive-media manifest make FFmpeg open
# another local path or a network URL.  The top-level upload is already open as
# a Python file object; ordinary self-contained media never needs this callback.
def _reject_nested_io(url: str, flags: int, options: dict):
    raise PermissionError("external media references are not allowed")


ALLOWED_CONTAINERS = frozenset({
    "aac",
    "aiff",
    "ape",
    "asf",
    "au",
    "flac",
    "matroska",
    "mov",
    "mp3",
    "ogg",
    "opus",
    "wav",
    "webm",
})


class ImportMediaError(ValueError):
    """The uploaded object is not a supported, self-contained audio file."""


class ImportDurationError(ValueError):
    """The decoded audio is longer than the configured limit."""


class ImportInterrupted(RuntimeError):
    """The worker was asked to stop at a safe chunk boundary."""


@dataclass(frozen=True)
class AudioChunk:
    index: int
    payload: bytes
    start_seconds: float
    duration_seconds: float
    overlap_seconds: float
    final: bool


def encode_wav(samples: np.ndarray) -> bytes:
    """Encode one bounded 16 kHz mono int16 chunk for the existing API path."""

    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return output.getvalue()


class PauseAwareChunker:
    """Streaming counterpart of web/audio.js's 8--15 s overlap chunker."""

    def __init__(self):
        self._buffer = np.empty(MAX_CHUNK_SAMPLES, dtype="<i2")
        self._used = 0
        self._overlap = 0
        self._start = 0
        self._index = 0
        self._emitted = False

    @property
    def total_fresh_samples(self) -> int:
        return self._start + self._used

    def push(self, samples: np.ndarray) -> Iterator[AudioChunk]:
        source = np.asarray(samples, dtype="<i2").reshape(-1)
        offset = 0
        while offset < len(source):
            count = min(len(source) - offset, MAX_CHUNK_SAMPLES - self._used)
            self._buffer[self._used : self._used + count] = source[offset : offset + count]
            self._used += count
            offset += count
            if self._used == MAX_CHUNK_SAMPLES or self._ends_in_pause():
                yield self._emit(False)

    def finish(self) -> list[AudioChunk]:
        if self.total_fresh_samples < MIN_AUDIO_SAMPLES:
            raise ImportMediaError("audio is shorter than 0.05 seconds")
        if not self._used:
            return []
        return [self._emit(True)]

    def _ends_in_pause(self) -> bool:
        fresh = self._used - self._overlap
        if fresh < TARGET_SAMPLES or fresh < PAUSE_SAMPLES:
            return False
        tail = self._buffer[self._used - PAUSE_SAMPLES : self._used].astype(np.float64)
        rms = math.sqrt(float(np.mean(tail * tail))) / 32768.0
        return rms <= PAUSE_RMS

    def _emit(self, final: bool) -> AudioChunk:
        count = self._used
        chunk = AudioChunk(
            index=self._index,
            payload=encode_wav(self._buffer[:count]),
            start_seconds=self._start / SAMPLE_RATE,
            duration_seconds=count / SAMPLE_RATE,
            overlap_seconds=self._overlap / SAMPLE_RATE,
            final=final,
        )
        self._index += 1
        self._emitted = True
        if final:
            self._used = 0
            self._overlap = 0
        else:
            retained = min(OVERLAP_SAMPLES, count)
            self._buffer[:retained] = self._buffer[count - retained : count]
            self._start += count - retained
            self._used = retained
            self._overlap = retained
        return chunk


def iter_audio_chunks(
    path: Path,
    *,
    max_seconds: float,
    interrupted: Callable[[], bool] = lambda: False,
    on_duration: Callable[[float], None] = lambda value: None,
) -> Iterator[AudioChunk]:
    """Decode and resample a private upload without materializing full PCM.

    Only the first audio stream is decoded.  Video, subtitles, and metadata are
    ignored.  Decoded duration, rather than untrusted container metadata, is the
    authoritative limit.
    """

    max_samples = round(max_seconds * SAMPLE_RATE)
    if max_samples < MIN_AUDIO_SAMPLES:
        raise ValueError("max_seconds is too small")
    chunker = PauseAwareChunker()
    decoded_samples = 0
    try:
        with Path(path).open("rb") as source:
            with av.open(
                source,
                mode="r",
                io_open=_reject_nested_io,
                container_options={"protocol_whitelist": "file,pipe"},
            ) as container:
                formats = frozenset((container.format.name or "").split(","))
                if not formats.intersection(ALLOWED_CONTAINERS):
                    raise ImportMediaError("unsupported media container")
                if not container.streams.audio:
                    raise ImportMediaError("media has no audio stream")
                stream = container.streams.audio[0]
                source_rate = int(stream.codec_context.sample_rate or 0)
                source_channels = int(stream.codec_context.channels or 0)
                if source_rate and not MIN_SOURCE_SAMPLE_RATE <= source_rate <= MAX_SOURCE_SAMPLE_RATE:
                    raise ImportMediaError("unsupported source sample rate")
                if source_channels and not 1 <= source_channels <= MAX_SOURCE_CHANNELS:
                    raise ImportMediaError("unsupported source channel count")
                if stream.duration is not None and stream.time_base is not None:
                    declared_seconds = float(stream.duration * stream.time_base)
                    if math.isfinite(declared_seconds) and declared_seconds > max_seconds + 1:
                        raise ImportDurationError("audio exceeds duration limit")
                    if math.isfinite(declared_seconds) and declared_seconds > 0:
                        on_duration(min(declared_seconds, max_seconds))
                resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

                def accept(frame) -> Iterator[AudioChunk]:
                    nonlocal decoded_samples
                    if frame.samples < 0 or frame.samples > MAX_CHUNK_SAMPLES:
                        raise ImportMediaError("decoded audio frame is too large")
                    if decoded_samples + frame.samples > max_samples:
                        raise ImportDurationError("audio exceeds duration limit")
                    array = frame.to_ndarray().reshape(-1)
                    if len(array) != frame.samples:
                        raise ImportMediaError("decoded mono audio shape is invalid")
                    decoded_samples += len(array)
                    yield from chunker.push(array)

                for frame in container.decode(stream):
                    if interrupted():
                        raise ImportInterrupted("import processing interrupted")
                    frame_rate = int(frame.sample_rate or source_rate or 0)
                    frame_channels = len(frame.layout.channels) if frame.layout is not None else source_channels
                    if frame.samples < 0 or frame.samples > MAX_SOURCE_FRAME_SAMPLES:
                        raise ImportMediaError("source audio frame is too large")
                    if not MIN_SOURCE_SAMPLE_RATE <= frame_rate <= MAX_SOURCE_SAMPLE_RATE:
                        raise ImportMediaError("unsupported source sample rate")
                    if not 1 <= frame_channels <= MAX_SOURCE_CHANNELS:
                        raise ImportMediaError("unsupported source channel count")
                    # Validate the duration represented by this source frame
                    # before libswresample can allocate its upsampled output.
                    # A forged 1 Hz frame, for example, must not expand into
                    # billions of 16 kHz samples before the post-resample cap.
                    if frame.samples * SAMPLE_RATE > (MAX_CHUNK_SAMPLES - 2048) * frame_rate:
                        raise ImportMediaError("source audio frame duration is too large")
                    for converted in resampler.resample(frame):
                        yield from accept(converted)
                for converted in resampler.resample(None):
                    yield from accept(converted)
                if decoded_samples == 0:
                    raise ImportMediaError("media has no decodable audio")
                if interrupted():
                    raise ImportInterrupted("import processing interrupted")
                yield from chunker.finish()
    except (ImportDurationError, ImportInterrupted, ImportMediaError):
        raise
    except (av.FFmpegError, EOFError, OSError, ValueError) as error:
        raise ImportMediaError("media could not be decoded") from error
