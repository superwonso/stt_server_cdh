"""Run the real renewal-only shell branch in a wholly fake temporary project."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
URL = "https://fake-renewal-test.trycloudflare.com"


class TunnelRenewalScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / ".data"
        for name in (".data", ".tools", ".venv/bin", "scripts", "bin"):
            (self.root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.start = self.root / "scripts/start-tunnel.sh"
        shutil.copy2(ROOT / "scripts/start-tunnel.sh", self.start)
        self.write(".data/pages-desired-config.json", json.dumps({"state": "online", "apiUrl": URL}))
        self.write(".data/tunnel-url.txt", URL + "\n")
        self.write(".data/tunnel.pid", "1234\n")
        self.write(".data/server.pid", "2345\n")
        self.write(".tools/cloudflared", """#!/usr/bin/env bash
printf '%s\\n' "$*" >> .data/cloudflared-calls
[[ "$*" == --version ]] || exit 91
""", executable=True)
        # Process ownership and schema validation are unit-tested in Python.
        # This stand-in exercises only shell control flow without real PIDs.
        self.write(".venv/bin/python", """#!/usr/bin/python3
import json, pathlib, sys
d = pathlib.Path('.data')
try:
    value = json.loads((d / 'pages-desired-config.json').read_text())
    if '--check-current' not in sys.argv or (d / 'unowned').exists(): raise ValueError()
    if value['state'] != 'online' or value['apiUrl'] + '\\n' != (d / 'tunnel-url.txt').read_text(): raise ValueError()
    if not (d / 'tunnel.pid').is_file() or not (d / 'server.pid').is_file(): raise ValueError()
    print(value['apiUrl'])
except Exception:
    sys.exit(1)
""", executable=True)
        self.write("bin/curl", """#!/usr/bin/python3
import json, pathlib, sys
d = pathlib.Path('.data')
local = sys.argv[-1].startswith('http://127.0.0.1:')
if (d / ('fail-local' if local else 'fail-external')).exists(): sys.exit(1)
if not local and (d / 'offline-after-health').exists():
    (d / 'pages-desired-config.json').write_text(json.dumps({'state':'offline','apiUrl':''}))
if not local and (d / 'changed-after-health').exists():
    new = 'https://changed-test.trycloudflare.com'
    (d / 'pages-desired-config.json').write_text(json.dumps({'state':'online','apiUrl':new}))
    (d / 'tunnel-url.txt').write_text(new + '\\n')
print('200', end='')
""", executable=True)
        self.write("scripts/publish-api-url.sh", """#!/usr/bin/env bash
set -eu
IFS= read -r public_url
printf '%s %s\\n' "$*" "$public_url" >> .data/publications
if [[ "$*" == *--no-wait* ]]; then
    # The lifecycle lock must still belong to start-tunnel here.
    if flock -n .data/tunnel-start.lock -c true; then exit 81; fi
else
    # CDN waiting must not keep an operator's later OFFLINE request blocked.
    flock -n .data/tunnel-start.lock -c true || exit 82
fi
""", executable=True)
        self.env = {"PATH": f"{self.root / 'bin'}:/usr/bin:/bin", "LANG": "C.UTF-8"}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, body, *, executable=False):
        target = self.root / name
        target.write_text(textwrap.dedent(body), encoding="utf-8")
        target.chmod(0o700 if executable else 0o600)

    def run_renewal(self):
        return subprocess.run([str(self.start), "--renew-only", "--port", "8765"],
                              cwd=self.root, env=self.env, capture_output=True, text=True, timeout=3)

    def test_success_only_republishes_and_releases_lifecycle_lock_before_cdn_wait(self):
        before = {name: (self.data / name).read_bytes() for name in ("tunnel.pid", "server.pid", "tunnel-url.txt")}
        result = self.run_renewal()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.data / "cloudflared-calls").read_text().splitlines(), ["--version"])
        self.assertEqual((self.data / "publications").read_text().splitlines(), [
            "--stdin --no-wait " + URL, "--wait-only --stdin " + URL])
        self.assertEqual(before, {name: (self.data / name).read_bytes() for name in before})
        self.assertFalse((self.data / "tunnel.log").exists())
        self.assertNotIn(URL, result.stdout + result.stderr)

    def test_missing_pid_offline_unowned_and_changed_url_never_publish_or_start(self):
        for failure in ("missing-pid", "offline", "unowned", "changed-url"):
            with self.subTest(failure=failure):
                self.write(".data/pages-desired-config.json", json.dumps({"state": "online", "apiUrl": URL}))
                self.write(".data/tunnel-url.txt", URL + "\n")
                self.write(".data/tunnel.pid", "1234\n")
                (self.data / "unowned").unlink(missing_ok=True)
                if failure == "missing-pid": (self.data / "tunnel.pid").unlink()
                if failure == "offline": self.write(".data/pages-desired-config.json", json.dumps({"state": "offline", "apiUrl": ""}))
                if failure == "unowned": self.write(".data/unowned", "1")
                if failure == "changed-url": self.write(".data/tunnel-url.txt", "https://other-test.trycloudflare.com\n")
                before = {path.name: path.read_bytes() for path in self.data.glob("*.pid")}
                result = self.run_renewal()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.data / "publications").exists())
                self.assertEqual(before, {path.name: path.read_bytes() for path in self.data.glob("*.pid")})
        self.assertTrue(all(line == "--version" for line in (self.data / "cloudflared-calls").read_text().splitlines()))

    def test_health_failures_or_state_changes_before_publication_do_not_publish(self):
        for failure in ("fail-local", "fail-external", "offline-after-health", "changed-after-health"):
            with self.subTest(failure=failure):
                self.write(".data/pages-desired-config.json", json.dumps({"state": "online", "apiUrl": URL}))
                self.write(".data/tunnel-url.txt", URL + "\n")
                self.write(".data/" + failure, "1")
                result = self.run_renewal()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.data / "publications").exists())
                (self.data / failure).unlink()

    def test_symlinked_lifecycle_lock_is_rejected_without_touching_target(self):
        target = self.root / "untouched"
        target.write_text("keep")
        (self.data / "tunnel-start.lock").symlink_to(target)
        result = self.run_renewal()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), "keep")
        self.assertFalse((self.data / "publications").exists())


if __name__ == "__main__":
    unittest.main()
