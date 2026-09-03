from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np
import av

from server.importer import (
    ImportDurationError,
    ImportMediaError,
    PauseAwareChunker,
    iter_audio_chunks,
)


def write_wav(path: Path, seconds: float, *, rate: int = 48_000, channels: int = 2):
    samples = np.full(round(seconds * rate) * channels, 1200, dtype="<i2")
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(samples.tobytes())


def write_m4a(path: Path, seconds: float):
    rate = 48_000
    frame_samples = 1024
    frames = max(1, round(seconds * rate / frame_samples))
    with av.open(str(path), "w", format="mp4") as container:
        stream = container.add_stream("aac", rate=rate)
        stream.layout = "stereo"
        for _ in range(frames):
            values = np.full((2, frame_samples), 0.03, dtype=np.float32)
            frame = av.AudioFrame.from_ndarray(values, format="fltp", layout="stereo")
            frame.sample_rate = rate
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


class ImportDecoderTests(unittest.TestCase):
    def test_pyav_streams_resampled_audio_into_bounded_overlapped_wavs(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stereo-48k.wav"
            write_wav(path, 31)
            chunks = list(iter_audio_chunks(path, max_seconds=4 * 60 * 60))

        self.assertGreaterEqual(len(chunks), 3)
        self.assertTrue(chunks[-1].final)
        self.assertTrue(all(not chunk.final for chunk in chunks[:-1]))
        self.assertEqual(chunks[0].start_seconds, 0)
        self.assertTrue(all(chunk.duration_seconds <= 15 for chunk in chunks))
        self.assertTrue(all(len(chunk.payload) <= 480_044 for chunk in chunks))
        for previous, current in zip(chunks, chunks[1:]):
            self.assertAlmostEqual(current.start_seconds, previous.start_seconds + previous.duration_seconds - 3)
            self.assertEqual(current.overlap_seconds, 3)
        self.assertAlmostEqual(chunks[-1].start_seconds + chunks[-1].duration_seconds, 31, places=3)

    def test_empty_audio_and_decoded_duration_limit_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            empty = Path(temporary) / "empty.wav"
            with wave.open(str(empty), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16_000)
            with self.assertRaises(ImportMediaError):
                list(iter_audio_chunks(empty, max_seconds=1))

            long = Path(temporary) / "long.wav"
            write_wav(long, 0.2, rate=16_000, channels=1)
            with self.assertRaises(ImportDurationError):
                list(iter_audio_chunks(long, max_seconds=0.1))

    def test_pyav_decodes_aac_in_mp4_without_a_system_ffmpeg_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lecture.m4a"
            write_m4a(path, 2)
            chunks = list(iter_audio_chunks(path, max_seconds=10))
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].final)
        self.assertGreater(chunks[0].duration_seconds, 1.9)
        self.assertLess(chunks[0].duration_seconds, 2.1)

    def test_chunker_checks_interruption_without_retaining_full_recording(self):
        chunker = PauseAwareChunker()
        chunks = []
        block = np.full(16_000, 1000, dtype="<i2")
        for _ in range(31):
            chunks.extend(chunker.push(block))
        chunks.extend(chunker.finish())
        self.assertGreaterEqual(len(chunks), 3)
        self.assertLessEqual(chunker._buffer.nbytes, 15 * 16_000 * 2)


if __name__ == "__main__":
    unittest.main()
