from __future__ import annotations

import os
import json
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PUBLISH = ROOT / "scripts" / "publish-api-url.sh"


class PublishApiUrlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.directory = directory
        self.log = directory / "gh.log"
        self.mock_gh = directory / "gh"
        self.mock_gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -eu
                {
                    printf 'ARGS'
                    printf ' <%s>' "$@"
                    printf '\\n'
                    if [[ "${1:-}" == variable ]]; then
                        printf 'BODY '
                        IFS= read -r body || true
                        printf '%s\\n' "$body"
                    fi
                } >>"$MOCK_GH_LOG"
                if [[ "${MOCK_GH_FAIL:-0}" == 1 ]]; then
                    printf 'credential=fake-secret-that-must-not-escape\\n' >&2
                    exit 1
                fi
                """
            ),
            encoding="utf-8",
        )
        self.mock_gh.chmod(0o755)
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "GH_BIN": str(self.mock_gh),
                "MOCK_GH_LOG": str(self.log),
                "PAGES_PUBLISH_LOCK": str(directory / "publish.lock"),
                "PAGES_DESIRED_FILE": str(directory / "desired.json"),
                "PYTHON_BIN": sys.executable,
            }
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_publish(self, *arguments: str, input_value: str | None = None, **environment):
        values = self.environment | environment
        return subprocess.run(
            [str(PUBLISH), *arguments],
            cwd=ROOT,
            env=values,
            input=input_value,
            capture_output=True,
            text=True,
        )

    def test_online_url_uses_stdin_for_both_script_and_github_cli(self):
        url = "https://gentle-classroom-voice.trycloudflare.com"
        result = self.run_publish("--stdin", "--no-wait", input_value=url + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(url, result.stdout + result.stderr)
        calls = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 3)
        self.assertNotIn(url, calls[0])
        body = json.loads(calls[1].removeprefix("BODY "))
        self.assertEqual(body["apiUrl"], url)
        self.assertEqual(body["state"], "online")
        self.assertIn("<workflow> <run> <pages.yml>", calls[2])
        self.assertIn("<--repo> <superwonso/stt_server_cdh>", calls[2])
        desired_file = Path(self.environment["PAGES_DESIRED_FILE"])
        self.assertEqual(stat.S_IMODE(desired_file.stat().st_mode), 0o600)

    def test_offline_dispatch_contains_no_endpoint(self):
        result = self.run_publish("--offline", "--no-wait")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text(encoding="utf-8")
        body_line = next(line for line in calls.splitlines() if line.startswith("BODY "))
        body = json.loads(body_line.removeprefix("BODY "))
        self.assertEqual(body["state"], "offline")
        self.assertEqual(body["apiUrl"], "")
        self.assertNotIn("trycloudflare", calls)

    def test_malformed_url_is_rejected_before_github_is_called(self):
        result = self.run_publish(
            "https://nested.evil.trycloudflare.com\ncredential=fake-secret",
            "--no-wait",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertNotIn("fake-secret", result.stdout + result.stderr)

    def test_github_diagnostics_cannot_echo_credentials(self):
        result = self.run_publish(
            "--offline", "--no-wait", MOCK_GH_FAIL="1"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("fake-secret", result.stdout + result.stderr)
        self.assertIn("GitHub", result.stderr)

    def test_wait_only_checks_the_exact_local_request_without_mutating_github(self):
        url = "https://gentle-classroom-voice.trycloudflare.com"
        dispatched = self.run_publish("--stdin", "--no-wait", input_value=url + "\n")
        self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
        before = self.log.read_text(encoding="utf-8")

        fake_curl = self.directory / "curl"
        fake_curl.write_text(
            "#!/usr/bin/env bash\ncat \"$PAGES_DESIRED_FILE\"\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)
        result = self.run_publish(
            "--wait-only",
            "--stdin",
            input_value=url + "\n",
            PATH=f"{self.directory}:{self.environment['PATH']}",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("준비됐습니다", result.stdout)
        self.assertEqual(self.log.read_text(encoding="utf-8"), before)

    def test_cdn_wait_does_not_block_a_newer_offline_dispatch(self):
        fake_curl = self.directory / "curl"
        fake_curl.write_text("#!/usr/bin/env bash\nsleep 1\nexit 1\n", encoding="utf-8")
        fake_curl.chmod(0o755)
        environment = self.environment | {
            "PATH": f"{self.directory}:{self.environment['PATH']}",
            "PAGES_PUBLISH_TIMEOUT": "6",
        }
        url = "https://gentle-classroom-voice.trycloudflare.com"
        online = subprocess.Popen(
            [str(PUBLISH), "--stdin"],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert online.stdin is not None
        online.stdin.write(url + "\n")
        online.stdin.close()
        online.stdin = None
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if self.log.exists() and "<workflow> <run>" in self.log.read_text(encoding="utf-8"):
                    break
                time.sleep(0.02)
            else:
                self.fail("online publication did not reach workflow dispatch")

            offline = subprocess.run(
                [str(PUBLISH), "--offline", "--no-wait"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=2,
            )
            self.assertEqual(offline.returncode, 0, offline.stderr)
            stdout, stderr = online.communicate(timeout=3)
            self.assertNotEqual(online.returncode, 0, stdout)
            self.assertIn("더 새로운", stderr)
        finally:
            if online.poll() is None:
                online.terminate()
                online.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
