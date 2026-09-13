"""Exercise cloned lifecycle scripts against a disposable fake HTTP API/model.

The real repository launchers, settings, ports, PIDs and providers are never run.
All model commands resolve to a synthetic module inside the temporary clone.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(Path("/proc/self/cmdline").exists(), "Linux process ownership integration")
class ModelProcessScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stt-model-shell-")
        self.root = Path(self.temp.name)
        self.data = self.root / ".data"
        for name in ("scripts", "server", ".venv/bin", ".data"):
            (self.root / name).mkdir(parents=True, mode=0o700)
        for name in ("start-server.sh", "stop.sh", "start-model-server.sh", "stop-model-server.sh", "status.sh"):
            shutil.copy2(ROOT / "scripts" / name, self.root / "scripts" / name)
        self.write("server/.env", "# synthetic fixture only\nMODEL_WARMUP=1\n")
        self.write("server/__init__.py", "")
        self.write(".venv/bin/python", """#!/usr/bin/python3
import os, sys
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
""", executable=True)
        self.write("uvicorn.py", """
import http.server, json, os, pathlib, signal, sys
root = pathlib.Path.cwd()
args = sys.argv[1:]
port = int(args[args.index('--port') + 1])
state = {'pid': os.getpid(), 'args': args,
         'warmup': os.environ.get('MODEL_WARMUP'),
         'socket': os.environ.get('LOCAL_MODEL_SOCKET')}
(root / '.data/api-started.json').write_text(json.dumps(state))
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == '/health' else 404)
        self.send_header('Content-Length', '15')
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *args): pass
class Server(http.server.HTTPServer):
    allow_reuse_address = True
def stop(*args): raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
with Server(('127.0.0.1', port), Handler) as server:
    server.serve_forever(poll_interval=0.05)
""")
        self.write("server/model_process.py", """
import json, pathlib, sys
root = pathlib.Path.cwd()
data = root / '.data'
action = sys.argv[1]
with (data / 'model-calls.jsonl').open('a') as target:
    target.write(json.dumps({'action': action, 'args': sys.argv[2:]}) + '\\n')
marker = data / 'model-running'
if action == 'start':
    if (data / 'model-start-fails').exists(): raise SystemExit(9)
    marker.write_text('synthetic running')
elif action == 'stop':
    marker.unlink(missing_ok=True)
elif action != 'status':
    raise SystemExit(91)
print('Synthetic model: ' + ('running' if marker.exists() else 'stopped'))
""")
        # Even an accidentally expanded stop mode cannot publish outside this clone.
        self.write("scripts/publish-api-url.sh", """#!/usr/bin/env bash
printf '%s\\n' "$*" >> .data/unexpected-publication
exit 92
""", executable=True)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        self.socket = self.root / ".data/model-server/model.sock"
        self.env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PORT": str(self.port)}

    def write(self, path, body, *, executable=False):
        target = self.root / path
        target.write_text(textwrap.dedent(body), encoding="utf-8")
        target.chmod(0o700 if executable else 0o600)

    def tearDown(self):
        try:
            state = self.data / "api-started.json"
            if state.exists():
                pid = json.loads(state.read_text())["pid"]
                proc = Path(f"/proc/{pid}")
                try:
                    owned = (proc / "cwd").resolve(strict=True) == self.root
                except FileNotFoundError:
                    owned = False
                if owned:
                    command = (proc / "cmdline").read_bytes().split(b"\0")
                    self.assertIn(b"uvicorn", command)
                    self.assertIn(b"server.app:create_app", command)
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + 3
                    while self.api_alive() and time.monotonic() < deadline:
                        time.sleep(.025)
                    if self.api_alive():
                        os.kill(pid, signal.SIGKILL)
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
            self.assertFalse((self.data / "unexpected-publication").exists(), "no external publishing path may be entered")
        finally:
            self.temp.cleanup()

    def run_script(self, name, *args, timeout=12, env=None):
        return subprocess.run([str(self.root / "scripts" / name), *args], cwd=self.root,
                              env=self.env | (env or {}), capture_output=True, text=True, timeout=timeout)

    def start(self, *args, **kwargs):
        result = self.run_script("start-server.sh", "--timeout", "5", *args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.api_alive())
        return result

    def api_alive(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=.2) as connection:
                connection.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
                return b" 200 " in connection.recv(1024)
        except OSError:
            return False

    def model_calls(self):
        path = self.data / "model-calls.jsonl"
        return [] if not path.exists() else [json.loads(line) for line in path.read_text().splitlines()]

    def test_api_only_never_starts_model_and_forces_remote_no_warmup_environment(self):
        custom = self.root / ".data/custom-model/model.sock"
        self.start("--api-only", "--warmup", "--model-socket", str(custom), env={"MODEL_WARMUP": "1"})
        state = json.loads((self.data / "api-started.json").read_text())
        self.assertEqual(state["warmup"], "0")
        self.assertEqual(state["socket"], str(custom))
        self.assertIn("--no-access-log", state["args"])
        self.assertEqual(state["args"][state["args"].index("--workers") + 1], "1")
        self.assertEqual(self.model_calls(), [])

    def test_model_start_failure_leaves_api_and_its_pid_running(self):
        self.write(".data/model-start-fails", "1")
        result = self.start()
        self.assertIn("API", result.stderr)
        calls = self.model_calls()
        self.assertEqual([call["action"] for call in calls], ["start"])
        self.assertIn("--warmup", calls[0]["args"])
        self.assertIn(str(self.socket), calls[0]["args"])
        self.assertTrue((self.data / "server.pid").exists())
        self.assertTrue(self.api_alive())

    def test_repeated_api_start_retains_pid_and_explicitly_starts_selected_model(self):
        self.start("--api-only")
        pid = (self.data / "server.pid").read_bytes()
        self.start("--no-warmup")
        self.assertEqual((self.data / "server.pid").read_bytes(), pid)
        self.assertEqual([call["action"] for call in self.model_calls()], ["start"])
        self.assertIn("--no-warmup", self.model_calls()[0]["args"])
        self.assertTrue((self.data / "model-running").exists())

    def test_server_only_stop_retains_model_marker_and_never_calls_model_stop(self):
        self.start()
        calls = self.model_calls()
        result = self.run_script("stop.sh", "--server-only", "--timeout", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.api_alive())
        self.assertFalse((self.data / "server.pid").exists())
        self.assertTrue((self.data / "model-running").exists())
        self.assertEqual(self.model_calls(), calls)

    def test_model_only_stop_and_status_leave_api_pid_and_recording_service_alive(self):
        self.start()
        pid = (self.data / "server.pid").read_bytes()
        stopped = self.run_script("stop.sh", "--model-only", "--timeout", "2")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertTrue(self.api_alive())
        self.assertEqual((self.data / "server.pid").read_bytes(), pid)
        self.assertFalse((self.data / "model-running").exists())
        self.assertEqual([call["action"] for call in self.model_calls()], ["start", "stop"])
        status = self.run_script("status.sh")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual([call["action"] for call in self.model_calls()], ["start", "stop", "status"])
        self.assertTrue(self.api_alive())
        self.assertEqual((self.data / "server.pid").read_bytes(), pid)

    def test_changed_socket_on_running_api_does_not_spawn_a_second_model(self):
        self.start("--api-only")
        pid = (self.data / "server.pid").read_bytes()
        self.start("--model-socket", str(self.root / ".data/another-model/model.sock"))
        self.assertEqual(self.model_calls(), [])
        self.assertEqual((self.data / "server.pid").read_bytes(), pid)
        self.assertTrue(self.api_alive())

    def test_existing_listener_is_not_replaced_or_sent_a_signal(self):
        # A genuine loopback listener not owned by the cloned API must survive.
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.port))
            listener.listen(1)
            # The production collision check intentionally runs forty probes
            # before refusing. Allow subprocess startup overhead under full CI
            # load as well as its ten seconds of bounded retry sleeps.
            result = self.run_script("start-server.sh", "--api-only", "--timeout", "1", timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.data / "api-started.json").exists())
            self.assertFalse((self.data / "server.pid").exists())
            self.assertEqual(self.model_calls(), [])
            with socket.create_connection(("127.0.0.1", self.port), timeout=.2):
                connection, _address = listener.accept()
                connection.close()


if __name__ == "__main__":
    unittest.main()
