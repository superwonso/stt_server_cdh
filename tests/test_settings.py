from __future__ import annotations

import unittest
import tempfile
from unittest import mock
from pathlib import Path

from server.manage import update_private_env
from server.settings import PROJECT_DIR, Settings, account_usernames, api_origin, url_origin


class UrlValidationTests(unittest.TestCase):
    def test_two_private_account_ids_are_parsed_and_normalized(self):
        self.assertEqual(
            account_usernames(" user-alpha , user-beta "),
            ("user-alpha", "user-beta"),
        )

    def test_invalid_account_configuration_fails_without_reflecting_values(self):
        invalid = [
            None,
            "",
            "only-one",
            "one,two,three",
            "one,,two",
            "one,two,",
            "same,same",
            "Uppercase,valid",
            "../escape,valid",
            ".hidden,valid",
            "a" * 33 + ",valid",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                account_usernames(value)
            if value:
                self.assertNotIn(value, str(raised.exception))

    def test_public_env_example_does_not_contain_working_account_ids(self):
        content = (PROJECT_DIR / "server" / "env.example").read_text(encoding="utf-8")
        account_line = next(line for line in content.splitlines() if line.startswith("ACCOUNT_USERNAMES="))
        self.assertEqual(account_line, "ACCOUNT_USERNAMES=")

    def test_runtime_configuration_requires_private_account_ids(self):
        with mock.patch("server.settings.load_dotenv"), mock.patch.dict(
            "os.environ", {}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "ACCOUNT_USERNAMES"):
                Settings.from_env()

    def test_quick_tunnel_and_loopback_api_origins_are_allowed(self):
        self.assertEqual(
            api_origin("https://gentle-classroom-voice.trycloudflare.com/"),
            "https://gentle-classroom-voice.trycloudflare.com",
        )
        self.assertEqual(api_origin("http://localhost:8765"), "http://localhost:8765")
        self.assertEqual(api_origin("https://127.0.0.1:8765/"), "https://127.0.0.1:8765")
        self.assertEqual(api_origin("http://[::1]:8765"), "http://[::1]:8765")

    def test_other_api_destinations_are_rejected(self):
        invalid = [
            "https://attacker.example",
            "https://trycloudflare.com",
            "https://nested.evil.trycloudflare.com",
            "https://safe.trycloudflare.com.evil.example",
            "http://safe.trycloudflare.com",
            "https://safe.trycloudflare.com:8443",
            "https://safe.trycloudflare.com/path",
            "https://safe.trycloudflare.com/?redirect=evil",
            "https://user:password@safe.trycloudflare.com",
            "https://safe.trycloudflare.com:invalid",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                api_origin(value)

    def test_pages_site_origin_remains_independent_of_api_restriction(self):
        self.assertEqual(
            url_origin("https://student.github.io/classroom/"),
            "https://student.github.io",
        )

    def test_managed_env_file_is_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path = Path(temporary) / "server" / ".env"
            env_path.parent.mkdir(parents=True)
            env_path.write_text(
                "API_URL='https://old.trycloudflare.com'\n"
                "ACCOUNT_USERNAMES='user-alpha,user-beta'\n",
                encoding="utf-8",
            )
            update_private_env(env_path, "https://student.github.io")
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            content = env_path.read_text(encoding="utf-8")
            self.assertIn("SITE_ORIGINS='https://student.github.io'", content)
            self.assertIn("ACCOUNT_USERNAMES='user-alpha,user-beta'", content)
            self.assertNotIn("API_URL", content)

    def test_audio_body_limit_cannot_be_configured_below_the_static_part_contract(self):
        with mock.patch.dict(
            "os.environ",
            {"MAX_UPLOAD_BYTES": "64000", "ACCOUNT_USERNAMES": "user-alpha,user-beta"},
        ):
            self.assertEqual(Settings.from_env().max_upload_bytes, 480 * 1024)


if __name__ == "__main__":
    unittest.main()
