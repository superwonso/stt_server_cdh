"""Pure configuration rendering and fail-closed native maintenance entrypoint."""
from __future__ import annotations

import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest import mock

from dotenv import dotenv_values
from server.manage import _management_env_path, _render_env_changes, main


class PrivateEnvironmentRenderingTests(unittest.TestCase):
    def test_multiline_values_comments_and_literal_escapes_survive_selected_changes(self):
        original = "# preserve comment\nMULTILINE='first\nsecond'\nLITERAL='keep \\\\ slash'\nSITE_ORIGINS='old'\nAPI_URL='old-public'\n"
        updated = _render_env_changes(original, {"SITE_ORIGINS": "http://127.0.0.1:18765", "API_URL": None})
        before = dotenv_values(stream=io.StringIO(original), interpolate=False)
        after = dotenv_values(stream=io.StringIO(updated), interpolate=False)
        self.assertEqual(after["MULTILINE"], before["MULTILINE"])
        self.assertEqual(after["LITERAL"], before["LITERAL"])
        self.assertEqual(after["SITE_ORIGINS"], "http://127.0.0.1:18765")
        self.assertNotIn("API_URL", after)
        self.assertIn("# preserve comment", updated)

    def test_new_key_preserves_non_newline_ending_and_roundtrips_quotes(self):
        sample = "synthetic ' quote \\ slash"
        updated = _render_env_changes("UNCHANGED='yes'", {"SYNTHETIC": sample})
        parsed = dotenv_values(stream=io.StringIO(updated), interpolate=False)
        self.assertEqual(parsed, {"UNCHANGED": "yes", "SYNTHETIC": sample})

    def test_malformed_configuration_is_rejected_without_reflecting_content(self):
        with self.assertRaises(ValueError) as raised:
            _render_env_changes("SYNTHETIC_PRIVATE='unfinished", {"OTHER": "value"})
        self.assertNotIn("SYNTHETIC_PRIVATE", str(raised.exception))


@unittest.skipUnless(os.name == "nt", "Windows explicit private environment policy")
class WindowsManagementGuardTests(unittest.TestCase):
    def test_missing_or_relative_environment_path_is_rejected(self):
        for configured in ("", "relative.env"):
            with self.subTest(path=configured), mock.patch.dict(os.environ, {"STT_ENV_FILE": configured}):
                with self.assertRaises(ValueError):
                    _management_env_path()

    def test_running_api_blocks_mutation_before_database_or_hidden_prompt(self):
        with (mock.patch.dict(os.environ, {"STT_ENV_FILE": str(Path.cwd() / "synthetic.env")}),
              mock.patch("sys.argv", ["manage", "configure-clova"]),
              mock.patch("scripts.google_drive.server_is_running", return_value=True),
              mock.patch("server.manage.Database") as database,
              mock.patch("server.manage.getpass.getpass") as hidden_prompt,
              contextlib.redirect_stderr(io.StringIO())):
            with self.assertRaises(SystemExit) as raised:
                main()
            self.assertEqual(raised.exception.code, 2)
            database.assert_not_called()
            hidden_prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
