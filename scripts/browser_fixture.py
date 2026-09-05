#!/usr/bin/env python3
"""Loopback-only, synthetic fixture for validate_browser.mjs; never reads .env.

An explicitly supplied, new /tmp/stt-browser-check.* child directory owns all
test accounts, sessions, recordings, and metadata. No production app factory,
ASR model, cloud archive, or external language-model client is used.
"""
from __future__ import annotations

import argparse
import functools
import json
import secrets
import socket
import sys
import threading
import wave
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import uvicorn

from server.app import create_app
from server.security import PASSWORD_HASHER
from server.settings import Settings
from server.summarizer import LectureSummary
from server.translator import LectureTranslation


class SyntheticASR:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"model_state": "ready", "model": "synthetic-browser-check", "device": "cpu"}

    def transcribe(self, samples, language, overlap_seconds=0, final_chunk=True):
        duration = len(samples) / 16000
        self.calls.append({"samples": len(samples), "overlap": overlap_seconds,
                           "final": final_chunk, "peak": float(abs(samples).max()) if len(samples) else 0})
        if duration <= overlap_seconds:
            return []
        return [{"start": overlap_seconds, "end": duration,
                 "text": "Plants use light to produce nutrients."}]


class DisabledCloudASR:
    configured = False

    def close(self):
        pass


class DisabledCorrection:
    configured = False
    model = "synthetic-disabled"

    def close(self):
        pass


class SyntheticSummary:
    configured = True

    def __init__(self):
        self.calls = 0

    def summarize(self, *, language, segments, interrupted):
        self.calls += 1
        identifier = segments[0]["id"]
        return LectureSummary(
            overview="식물은 빛을 이용해 양분을 만듭니다.",
            overview_source_ids=[identifier],
            sections=[{"heading": "핵심 개념", "bullets": [
                {"text": "빛을 이용해 양분을 만듭니다.", "source_ids": [identifier]}]}],
            review_questions=[{"question": "식물은 무엇을 이용하나요?", "source_ids": [identifier]}],
        )


class SyntheticTranslation:
    configured = True

    def __init__(self):
        self.calls = 0

    def translate(self, *, language, segments, interrupted):
        self.calls += 1
        return LectureTranslation([{**segment, "text": "식물은 빛을 이용해 양분을 만듭니다."}
                                   for segment in segments])


class QuietWebHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.partition("?")[0] == "/pcm-worklet.js":
            self.server.validation_worklet_requests += 1
        super().do_GET()

    def log_message(self, *_args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if (directory.parent.parent != Path("/tmp")
            or not directory.parent.name.startswith("stt-browser-check.")
            or directory.exists()):
        parser.error("Use a new child directory under /tmp/stt-browser-check.*")
    directory.mkdir(mode=0o700)
    suffix = secrets.token_hex(4)
    accounts = (f"checka-{suffix}", f"checkb-{suffix}")
    password = secrets.token_urlsafe(24)
    handler = functools.partial(QuietWebHandler, directory=str(PROJECT_DIR / "web"))
    web = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    web.validation_worklet_requests = 0
    site_origin = f"http://127.0.0.1:{web.server_port}"
    settings = Settings(
        data_dir=directory / "data", model_cache_dir=directory / "unused-models",
        accounts=accounts, site_origins=(site_origin,), model_warmup=False,
        device="cpu", google_drive_enabled=False, recording_free_reserve_bytes=0,
    )
    asr, summary, translation = SyntheticASR(), SyntheticSummary(), SyntheticTranslation()
    app = create_app(settings, asr, DisabledCorrection(), clova_transcriber=DisabledCloudASR(),
                     summarizer=summary, translator=translation)
    with app.state.database.connect() as connection:
        connection.execute("UPDATE users SET password_hash=?", (PASSWORD_HASHER.hash(password),))

    @app.get("/__validation__/state")
    def state():
        # Only this deliberately isolated fixture exposes synthetic statistics.
        with app.state.database.connect() as connection:
            lectures = [dict(row) for row in connection.execute(
                "SELECT id,recording_finalized FROM lectures ORDER BY created_at")]
            chunks = [dict(row) for row in connection.execute(
                "SELECT lecture_id,chunk_id,start_seconds,overlap_seconds,final_chunk,status "
                "FROM chunks ORDER BY start_seconds")]
            for lecture in lectures:
                owner = connection.execute("SELECT username FROM lectures WHERE id=?", (lecture["id"],)).fetchone()[0]
                info = app.state.recording_store.info(owner, lecture["id"])
                lecture["recording_seconds"] = info["duration_seconds"] if info else 0
                lecture["segment_count"] = connection.execute(
                    "SELECT count(*) FROM segments WHERE lecture_id=?", (lecture["id"],)).fetchone()[0]
        return {"lectures": lectures, "chunks": chunks, "asr_calls": asr.calls,
                "worklet_loads": web.validation_worklet_requests,
                "summary_calls": summary.calls, "translation_calls": translation.calls}

    fake_audio = directory / "synthetic-silence.wav"
    with wave.open(str(fake_audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(bytes(48000 * 2 * 90))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    api_origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    metadata = {"api_origin": api_origin, "site_origin": site_origin, "accounts": accounts,
                "password": password, "fake_audio": str(fake_audio), "directory": str(directory)}
    metadata_path = directory / "fixture.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    metadata_path.chmod(0o600)
    threading.Thread(target=web.serve_forever, name="synthetic-browser-web", daemon=True).start()
    print(json.dumps({"fixture": str(metadata_path)}), flush=True)
    try:
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning"))
        server.run(sockets=[listener])
    finally:
        web.shutdown()
        web.server_close()
        listener.close()


if __name__ == "__main__":
    main()
