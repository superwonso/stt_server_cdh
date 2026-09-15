"""Fail-closed Windows environment loading; no production files are read."""
from __future__ import annotations
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
from server.settings import Settings

@unittest.skipUnless(os.name == "nt", "Windows private environment policy")
class WindowsEnvironmentPolicyTests(unittest.TestCase):
    def test_missing_explicit_environment_refuses_fallback(self):
        missing = Path.cwd() / ("synthetic-missing-" + uuid.uuid4().hex + ".env")
        with (mock.patch.dict(os.environ, {"STT_ENV_FILE": str(missing), "ACCOUNT_USERNAMES": "test-a,test-b"}),
              mock.patch("server.settings.load_dotenv") as loader):
            with self.assertRaises(ValueError):
                Settings.from_env()
            loader.assert_not_called()

    def test_relative_explicit_path_is_rejected_even_when_missing(self):
        with (mock.patch.dict(os.environ, {"STT_ENV_FILE": "relative-synthetic.env", "ACCOUNT_USERNAMES": "test-a,test-b"}),
              mock.patch("server.settings.load_dotenv") as loader):
            with self.assertRaisesRegex(ValueError, "absolute"):
                Settings.from_env()
            loader.assert_not_called()

    def test_nonprivate_fixture_is_never_sent_to_dotenv_parser(self):
        with tempfile.NamedTemporaryFile(prefix="yeobaek-public-env-fixture-", delete=False) as output:
            output.write(b"SYNTHETIC_ONLY=yes\n")
            path = Path(output.name)
        try:
            with (mock.patch.dict(os.environ, {"STT_ENV_FILE": str(path)}),
                  mock.patch("server.settings.load_dotenv") as loader):
                with self.assertRaises(ValueError):
                    Settings.from_env()
                loader.assert_not_called()
        finally:
            path.unlink()

if __name__ == "__main__":
    unittest.main()
