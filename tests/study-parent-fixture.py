"""Loopback-only browser fixture: temporary accounts/DB, fake ASR and AI."""
import argparse
import json
import socket
import sys
import tempfile
import threading
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--state-file", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
import uvicorn
from fastapi import HTTPException, Request
from fastapi.staticfiles import StaticFiles
from server.app import create_app
from server.security import PASSWORD_HASHER
from server.settings import Settings


class FakeTranscriber:
    supports_boundary_context = True

    def status(self):
        return {"model_state": "ready", "model": "synthetic", "device": "cpu"}

    def transcribe(self, samples, language, overlap_seconds=0, final_chunk=True,
                   *, start_seconds=0, boundary_context=None, boundary_output=None):
        if boundary_output is not None:
            boundary_output.update({"version": 1, "audio_end": start_seconds + len(samples) / 16000, "tokens": []})
        return [{"start": overlap_seconds, "end": len(samples) / 16000, "text": "합성 브라우저 확인 음성"}]


class FakeGenerator:
    configured = True
    model = "gpt-6-luna"

    async def generate(self, *, text, count):
        return {"questions": [{"chapter": "합성 단원", "sub": "", "number": 1,
                               "question": "합성 자료의 답은?", "answer": "합성 답", "type": "short",
                               "source_excerpt": "합성 자료", "source_page": None}]}, False


sock = socket.socket()
sock.bind(("127.0.0.1", 0))
sock.listen()
port = sock.getsockname()[1]
with tempfile.TemporaryDirectory(prefix="study-parent-browser-") as temporary:
    settings = Settings(data_dir=Path(temporary) / "data", model_cache_dir=Path(temporary) / "models",
                        site_origins=(f"http://127.0.0.1:{port}",), model_warmup=False)
    app = create_app(settings, FakeTranscriber(), study_generator=FakeGenerator())
    with app.state.database.connect() as connection:
        connection.execute("UPDATE users SET password_hash=?", (PASSWORD_HASHER.hash("synthetic-test-password"),))
    for base in ("/stt_server_cdh", "/app"):
        app.mount(base + "/study", StaticFiles(directory=str(root / "study" / "out"), html=True))
        app.mount(base, StaticFiles(directory=str(root / "web"), html=True))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                          log_level="error", access_log=False, lifespan="off"))

    @app.post("/_fixture/stop")
    def stop(request: Request):
        if request.headers.get("x-fixture") != "synthetic-study-only":
            raise HTTPException(403)
        server.should_exit = True
        return {"stopping": True}

    timer = threading.Timer(900, lambda: setattr(server, "should_exit", True))
    timer.daemon = True
    timer.start()
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    args.state_file.write_text(json.dumps({"port": port}), encoding="utf-8")
    try:
        server.run(sockets=[sock])
    finally:
        app.state.stop_import_worker()
        timer.cancel()
        sock.close()
