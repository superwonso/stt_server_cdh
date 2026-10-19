"""Loopback-only recording-control QA with a temporary DB and synthetic ASR.

Never reads an environment file, production database, or real provider keys.
"""
from __future__ import annotations

import argparse
import json
import math
import socket
import struct
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn
from fastapi import HTTPException, Request
from fastapi.staticfiles import StaticFiles

from server.app import create_app
from server.security import PASSWORD_HASHER
from server.settings import Settings


class SyntheticASR:
    supports_cancellation = True

    def __init__(self):
        self.blocked = True
        self.calls = 0
        self.cancelled = 0

    def status(self):
        return {"model_state": "ready", "model": "synthetic", "device": "cpu"}

    def transcribe(self, samples, language, overlap_seconds=0, final_chunk=True, *, cancel_event=None):
        self.calls += 1
        deadline = time.monotonic() + 90
        while self.blocked and time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.wait(.02):
                self.cancelled += 1
                # Return a late result deliberately: the API must discard it.
                return [{"start": 0, "end": len(samples) / 16000, "text": "취소 뒤 합성 결과"}]
            time.sleep(.02)
        return [{"start": overlap_seconds, "end": len(samples) / 16000, "text": "정상 합성 결과"}]


class DisabledCloud:
    configured = False

    def close(self):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", required=True, type=Path)
    args = parser.parse_args()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="recording-control-browser-") as temporary:
        directory = Path(temporary)
        audio = directory / "synthetic.wav"
        with wave.open(str(audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"".join(struct.pack("<h", int(4000 * math.sin(2 * math.pi * 220 * n / 16000))) for n in range(16000 * 20)))
        settings = Settings(data_dir=directory / "data", model_cache_dir=directory / "models", site_origins=(origin,))
        engine = SyntheticASR()
        app = create_app(settings, engine, clova_transcriber=DisabledCloud())
        with app.state.database.connect() as db:
            db.execute("UPDATE users SET password_hash=?", (PASSWORD_HASHER.hash("synthetic-test-password"),))
        app.mount("/app", StaticFiles(directory=str(ROOT / "web"), html=True), name="fixture-web")
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))

        def guard(request):
            if request.headers.get("x-fixture") != "synthetic-recording-control-only":
                raise HTTPException(403)

        @app.get("/_fixture/state")
        def state(request: Request):
            guard(request)
            with app.state.database.connect() as db:
                segments = db.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
            return {"calls": engine.calls, "cancelled": engine.cancelled, "segments": segments}

        @app.post("/_fixture/expire")
        def expire(request: Request):
            guard(request)
            control = app.state.recording_control
            base = control.clock
            control.clock = lambda: base() + 181
            control.expire_due()
            return {"advanced_seconds": 181}

        @app.post("/_fixture/unblock")
        def unblock(request: Request):
            guard(request)
            engine.blocked = False
            return {"unblocked": True}

        @app.post("/_fixture/stop")
        def stop(request: Request):
            guard(request)
            server.should_exit = True
            return {"stopping": True}

        timer = threading.Timer(1200, lambda: setattr(server, "should_exit", True))
        timer.daemon = True
        timer.start()
        args.state_file.write_text(json.dumps({"origin": origin, "fake_audio": str(audio)}), encoding="utf-8")
        try:
            server.run(sockets=[listener])
        finally:
            timer.cancel()
            listener.close()


if __name__ == "__main__":
    main()
