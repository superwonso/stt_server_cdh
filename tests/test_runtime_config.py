from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "runtime_config.py"
SPEC = importlib.util.spec_from_file_location("runtime_config", SCRIPT)
runtime_config = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runtime_config)


class RuntimeConfigTests(unittest.TestCase):
    def test_online_document_has_only_public_bounded_fields(self):
        now = datetime(2026, 9, 3, 1, 2, 3, tzinfo=timezone.utc)
        result = runtime_config.runtime_config(
            "https://gentle-classroom-voice.trycloudflare.com", now
        )
        self.assertEqual(
            result,
            {
                "version": 1,
                "state": "online",
                "apiUrl": "https://gentle-classroom-voice.trycloudflare.com",
                "publishedAt": "2026-09-03T01:02:03Z",
                "expiresAt": "2026-09-04T01:02:03Z",
            },
        )

    def test_offline_document_contains_no_endpoint(self):
        now = datetime(2026, 9, 3, 1, 2, 3, tzinfo=timezone.utc)
        result = runtime_config.runtime_config("OFFLINE", now)
        self.assertEqual(result["state"], "offline")
        self.assertEqual(result["apiUrl"], "")
        self.assertEqual(result["expiresAt"], result["publishedAt"])
        self.assertEqual(set(result), runtime_config.CONFIG_KEYS)

    def test_only_an_exact_quick_tunnel_origin_is_accepted(self):
        accepted = [
            "https://a.trycloudflare.com",
            "https://gentle-classroom-voice.trycloudflare.com",
            "https://gentle-classroom-voice.trycloudflare.com/",
        ]
        for value in accepted:
            with self.subTest(value=value):
                self.assertEqual(runtime_config.validate_value(value)[0], "online")

        rejected = [
            "offline",
            " OFFLINE",
            "https://trycloudflare.com",
            "https://nested.evil.trycloudflare.com",
            "https://safe.trycloudflare.com.evil.example",
            "http://safe.trycloudflare.com",
            "https://safe.trycloudflare.com:8443",
            "https://safe.trycloudflare.com:443",
            "https://safe.trycloudflare.com/path",
            "https://safe.trycloudflare.com/?query=yes",
            "https://user:password@safe.trycloudflare.com",
            "https://safe.trycloudflare.com\nINJECTED=value",
        ]
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    runtime_config.validate_value(value)

    def test_render_reads_only_the_runtime_variable(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "config.json"
            environment = os.environ.copy()
            desired = runtime_config.runtime_config("https://safe.trycloudflare.com")
            environment["CLASSROOM_API_CONFIG"] = json.dumps(desired)
            environment["ACCOUNT_USERNAMES"] = "must-not-appear,also-private"
            environment["PASSWORD"] = "must-not-appear"
            subprocess.run(
                [str(SCRIPT), "render", str(output)],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = output.read_text(encoding="utf-8")
            document = json.loads(payload)
            self.assertEqual(set(document), runtime_config.CONFIG_KEYS)
            self.assertEqual(document, desired)
            self.assertNotIn("must-not-appear", payload)

    def test_match_requires_the_exact_atomic_desired_state(self):
        expected = "https://safe.trycloudflare.com"
        now = datetime.now(timezone.utc)
        document = runtime_config.runtime_config(expected, now)
        self.assertTrue(runtime_config.matches_config(document, document.copy()))
        document["username"] = "private"
        self.assertFalse(runtime_config.matches_config(document, runtime_config.runtime_config(expected, now)))
        del document["username"]
        renewed = runtime_config.runtime_config(expected, now + timedelta(seconds=1))
        self.assertFalse(runtime_config.matches_config(document, renewed))

    def test_redeployment_preserves_the_original_expiry(self):
        now = datetime.now(timezone.utc) - timedelta(days=2)
        desired = runtime_config.runtime_config("https://safe.trycloudflare.com", now)
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.json"
            second = Path(temporary) / "second.json"
            environment = os.environ.copy()
            environment["CLASSROOM_API_CONFIG"] = json.dumps(desired)
            for output in (first, second):
                subprocess.run(
                    [str(SCRIPT), "render", str(output)],
                    cwd=ROOT,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            self.assertEqual(json.loads(first.read_text()), desired)
            self.assertEqual(json.loads(second.read_text()), desired)


if __name__ == "__main__":
    unittest.main()
