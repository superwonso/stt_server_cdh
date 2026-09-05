from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest import mock

import httpx

from scripts.google_drive import (
    DRIVE_FILE_SCOPE,
    ArchiveFunctions,
    AuthorizationGrant,
    GoogleDriveSetupError,
    OAuthClientConfiguration,
    _atomic_private_write,
    _open_browser_without_output,
    authorize,
    build_authorization_url,
    exchange_authorization_grant,
    load_oauth_client,
    main,
    parse_oauth_callback,
    server_is_running,
)
from server.drive_storage import GoogleDriveStorage
from server.drive_archive import plan_existing_recordings
from server.db import Database
from server.recordings import RecordingStore
from server.settings import Settings


CLIENT_ID = "test-public-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "test-private-client-secret"
REFRESH_TOKEN = "test-private-refresh-token-value"


def client_document() -> dict[str, object]:
    return {
        "installed": {
            "client_id": CLIENT_ID,
            "project_id": "test-project",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": CLIENT_SECRET,
            "redirect_uris": ["http://localhost"],
        }
    }


class ServerProcessDetectionTests(unittest.TestCase):
    @staticmethod
    def _seed_project_server(process_root: Path, pid: int) -> None:
        process_directory = process_root / str(pid)
        process_directory.mkdir()
        (process_directory / "cwd").symlink_to(
            Path(__file__).resolve().parents[1], target_is_directory=True
        )
        (process_directory / "cmdline").write_bytes(
            b"python\0-m\0uvicorn\0server.app:create_app\0--factory\0"
        )

    def test_missing_pid_file_still_finds_exact_project_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            data_dir.mkdir()
            process_root = root / "proc"
            process_root.mkdir()
            self._seed_project_server(process_root, 4242)

            with mock.patch("scripts.google_drive.os.kill", return_value=None):
                running = server_is_running(
                    SimpleNamespace(data_dir=data_dir), process_root=process_root
                )

        self.assertTrue(running)

    def test_stale_pid_file_falls_back_to_process_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "server.pid").write_text("9999\n", encoding="ascii")
            process_root = root / "proc"
            process_root.mkdir()
            self._seed_project_server(process_root, 4242)

            def probe(pid: int, signal: int) -> None:
                self.assertEqual(signal, 0)
                if pid == 9999:
                    raise ProcessLookupError

            with mock.patch("scripts.google_drive.os.kill", side_effect=probe):
                running = server_is_running(
                    SimpleNamespace(data_dir=data_dir), process_root=process_root
                )

        self.assertTrue(running)

    def test_similar_command_outside_project_is_not_mistaken_for_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            data_dir.mkdir()
            process_root = root / "proc"
            process_root.mkdir()
            process_directory = process_root / "4242"
            process_directory.mkdir()
            unrelated = root / "unrelated"
            unrelated.mkdir()
            (process_directory / "cwd").symlink_to(
                unrelated, target_is_directory=True
            )
            (process_directory / "cmdline").write_bytes(
                b"python\0-m\0uvicorn\0server.app:create_app\0--factory\0"
            )

            with mock.patch("scripts.google_drive.os.kill", return_value=None):
                running = server_is_running(
                    SimpleNamespace(data_dir=data_dir), process_root=process_root
                )

        self.assertFalse(running)


class OAuthSetupTests(unittest.TestCase):
    def test_failed_wsl_browser_launcher_cannot_echo_private_url(self):
        private_url = "https://accounts.google.com/o/oauth2/auth?state=private-state"
        completed = SimpleNamespace(returncode=1)
        with (
            mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "test-wsl"}),
            mock.patch("scripts.google_drive.Path.is_file", return_value=True),
            mock.patch("scripts.google_drive.subprocess.run", return_value=completed) as run,
        ):
            opened = _open_browser_without_output(private_url)

        self.assertFalse(opened)
        arguments, keywords = run.call_args
        self.assertIn(private_url, arguments[0])
        self.assertIs(keywords["stdout"], subprocess.DEVNULL)
        self.assertIs(keywords["stderr"], subprocess.DEVNULL)

    def test_desktop_client_is_private_and_secret_repr_is_redacted(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "google-drive"
            directory.mkdir(mode=0o755)
            path = directory / "oauth-client.json"
            path.write_text(json.dumps(client_document()), encoding="utf-8")
            path.chmod(0o600)

            configuration = load_oauth_client(path)

            self.assertEqual(configuration.client_id, CLIENT_ID)
            self.assertEqual(configuration.client_secret, CLIENT_SECRET)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(CLIENT_ID, repr(configuration))
            self.assertNotIn(CLIENT_SECRET, repr(configuration))

    def test_web_client_and_symlink_are_rejected_without_reflecting_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret_path = root / "outside.json"
            secret_path.write_text(json.dumps(client_document()), encoding="utf-8")
            symlink = root / "oauth-client.json"
            symlink.symlink_to(secret_path)
            with self.assertRaises(GoogleDriveSetupError) as raised:
                load_oauth_client(symlink)
            self.assertNotIn(CLIENT_SECRET, str(raised.exception))

            web_path = root / "web.json"
            web_path.write_text(
                json.dumps({"web": client_document()["installed"]}),
                encoding="utf-8",
            )
            with self.assertRaises(GoogleDriveSetupError) as raised:
                load_oauth_client(web_path)
            self.assertNotIn(CLIENT_SECRET, str(raised.exception))

    def test_authorization_url_uses_offline_drive_file_scope_and_pkce(self):
        configuration = OAuthClientConfiguration(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            authorization_endpoint="https://accounts.google.com/o/oauth2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
        )
        url = build_authorization_url(
            configuration,
            redirect_uri="http://127.0.0.1:12345/",
            state="private-state",
            code_challenge="pkce-challenge",
        )
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "accounts.google.com")
        self.assertEqual(query["scope"], [DRIVE_FILE_SCOPE])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["prompt"], ["select_account consent"])
        self.assertNotIn("include_granted_scopes", query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["state"], ["private-state"])

    def test_exchange_uses_pinned_endpoint_and_returns_minimal_authorized_user(self):
        observed: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed["url"] = str(request.url)
            observed["body"] = request.content.decode("ascii")
            return httpx.Response(
                200,
                json={
                    "access_token": "must-not-be-persisted",
                    "expires_in": 3600,
                    "refresh_token": REFRESH_TOKEN,
                    "scope": DRIVE_FILE_SCOPE,
                    "token_type": "Bearer",
                },
            )

        configuration = OAuthClientConfiguration(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            authorization_endpoint="https://accounts.google.com/o/oauth2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
        )
        grant = AuthorizationGrant(
            code="private-authorization-code",
            redirect_uri="http://127.0.0.1:12345/",
            code_verifier="private-code-verifier",
        )
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = exchange_authorization_grant(configuration, grant, client=client)

        self.assertEqual(observed["url"], "https://oauth2.googleapis.com/token")
        self.assertIn("grant_type=authorization_code", observed["body"])
        self.assertEqual(result["type"], "authorized_user")
        self.assertEqual(result["refresh_token"], REFRESH_TOKEN)
        self.assertEqual(result["scopes"], [DRIVE_FILE_SCOPE])
        self.assertNotIn("access_token", result)

    def test_authorize_atomically_stores_token_with_private_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "google-drive"
            directory.mkdir(mode=0o700)
            client_path = directory / "oauth-client.json"
            token_path = directory / "token.json"
            client_path.write_text(json.dumps(client_document()), encoding="utf-8")
            client_path.chmod(0o600)

            grant = AuthorizationGrant(
                code="private-code",
                redirect_uri="http://127.0.0.1:12345/",
                code_verifier="private-verifier",
            )
            collect = mock.Mock(return_value=grant)
            exchange = mock.Mock(
                return_value={
                    "type": "authorized_user",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "refresh_token": REFRESH_TOKEN,
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "scopes": [DRIVE_FILE_SCOPE],
                }
            )
            authorize(
                client_path,
                token_path,
                collect_grant=collect,
                exchange_grant=exchange,
            )

            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(client_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual((directory / "token.lock").stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(token_path.read_text())["refresh_token"], REFRESH_TOKEN)
            self.assertFalse(tuple(directory.glob("*.tmp")))
            # The archive backend must be able to consume the CLI's token
            # without an initial access token; it refreshes lazily.
            storage = GoogleDriveStorage.from_token_file(token_path)
            storage.close()

    def test_atomic_private_write_rejects_symlink_instead_of_following_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / "private"
            private.mkdir()
            outside = root / "outside"
            outside.write_bytes(b"preserve")
            target = private / "token.json"
            target.symlink_to(outside)
            with self.assertRaises(GoogleDriveSetupError):
                _atomic_private_write(target, b"new-secret")
            self.assertEqual(outside.read_bytes(), b"preserve")

    def test_existing_nonprivate_directory_is_rejected_without_chmod(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "google-drive"
            directory.mkdir(mode=0o755)
            client_path = directory / "oauth-client.json"
            client_path.write_text(json.dumps(client_document()), encoding="utf-8")
            client_path.chmod(0o600)
            with self.assertRaisesRegex(GoogleDriveSetupError, "0700"):
                authorize(
                    client_path,
                    directory / "token.json",
                    collect_grant=mock.Mock(),
                    exchange_grant=mock.Mock(),
                )
            self.assertEqual(directory.stat().st_mode & 0o777, 0o755)

    def test_token_scope_must_be_present_and_exact(self):
        configuration = OAuthClientConfiguration(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            authorization_endpoint="https://accounts.google.com/o/oauth2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
        )
        grant = AuthorizationGrant(
            code="private-code",
            redirect_uri="http://127.0.0.1:12345/",
            code_verifier="private-verifier",
        )
        for scope in (None, "openid " + DRIVE_FILE_SCOPE):
            payload = {"refresh_token": REFRESH_TOKEN}
            if scope is not None:
                payload["scope"] = scope
            transport = httpx.MockTransport(lambda _request, value=payload: httpx.Response(200, json=value))
            with self.subTest(scope=scope), httpx.Client(transport=transport) as client:
                with self.assertRaisesRegex(GoogleDriveSetupError, "Drive file scope"):
                    exchange_authorization_grant(configuration, grant, client=client)

    def test_loopback_callback_ignores_wrong_state_then_accepts_root_callback(self):
        state = "expected-private-state"
        invalid = parse_oauth_callback("/?state=wrong&error=access_denied", state)
        self.assertEqual(invalid.status, 400)
        self.assertFalse(invalid.terminal)
        self.assertIsNone(invalid.error)

        valid = parse_oauth_callback(f"/?state={state}&code=private-code", state)
        self.assertEqual(valid.status, 200)
        self.assertTrue(valid.terminal)
        self.assertEqual(valid.code, "private-code")
        self.assertNotIn("private-code", repr(valid))


class ArchiveCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = SimpleNamespace(data_dir=Path(self.temporary.name))

    @staticmethod
    def archive(**overrides: object) -> ArchiveFunctions:
        status = mock.Mock(
            return_value={
                "configured": True,
                "connected": True,
                "pending_count": 1,
                "uploading_count": 2,
                "ready_count": 3,
                "organization_pending_count": 8,
                "attention_count": 6,
                "retrying_count": 4,
                "deleting_count": 7,
                "local_count": 5,
                "local_bytes": 600,
                "private_username": "must-not-be-printed",
                "drive_file_id": "must-not-be-printed",
            }
        )
        plan = mock.Mock(
            return_value={
                "candidate_count": 7,
                "candidate_bytes": 800,
                "already_ready_count": 9,
                "organization_pending_count": 2,
                "attention_count": 1,
                "private_path": "must-not-be-printed",
            }
        )
        enqueue = mock.Mock(return_value={"enqueued_count": 6, "attention_count": 0})
        run = mock.Mock(
            return_value={
                "migrated_count": 5,
                "deleted_local_count": 5,
                "failed_count": 0,
                "remaining_count": 0,
                "remote_session": "must-not-be-printed",
            }
        )
        values = {"status": status, "plan": plan, "enqueue": enqueue, "run": run}
        values.update(overrides)
        return ArchiveFunctions(
            archive_status=values["status"],
            plan_existing_recordings=values["plan"],
            enqueue_existing_recordings=values["enqueue"],
            run_archive_until_idle=values["run"],
        )

    def run_cli(self, arguments: list[str], archive: ArchiveFunctions) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                result = main(
                    arguments,
                    settings_loader=lambda: self.settings,
                    archive=archive,
                    server_running_check=lambda _settings: False,
                )
            except SystemExit as error:
                result = int(error.code)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_status_prints_only_aggregate_allowlist(self):
        archive = self.archive()
        result, output, errors = self.run_cli(["status"], archive)
        self.assertEqual(result, 0)
        self.assertFalse(errors)
        for expected in (
            "configured: yes",
            "connected: yes",
            "pending: 1",
            "folder organization pending: 8",
            "needs attention: 6",
            "deleting: 7",
            "local recordings: 5",
            "local bytes: 600",
        ):
            self.assertIn(expected, output)
        self.assertNotIn("must-not-be-printed", output)
        self.assertNotIn("private_username", output)
        self.assertNotIn("drive_file_id", output)

    def test_dry_run_never_enqueues_uploads_or_deletes(self):
        archive = self.archive()
        result, output, errors = self.run_cli(["migrate", "--dry-run", "--limit", "2"], archive)
        self.assertEqual(result, 0)
        self.assertFalse(errors)
        self.assertIn("migration candidates: 7", output)
        self.assertIn("candidate bytes: 800", output)
        self.assertIn("folder organization pending: 2", output)
        self.assertIn("dry-run", output)
        archive.plan_existing_recordings.assert_called_once_with(self.settings, limit=2)
        archive.enqueue_existing_recordings.assert_not_called()
        archive.run_archive_until_idle.assert_not_called()
        self.assertNotIn("must-not-be-printed", output)

    def test_migrate_deletes_only_through_verified_archive_controller_by_default(self):
        archive = self.archive()
        result, output, errors = self.run_cli(["migrate", "--limit", "4"], archive)
        self.assertEqual(result, 0)
        self.assertFalse(errors)
        archive.enqueue_existing_recordings.assert_called_once_with(self.settings, limit=4)
        archive.run_archive_until_idle.assert_called_once_with(
            self.settings,
            delete_local=True,
            limit=4,
        )
        self.assertIn("migrated: 5", output)
        self.assertIn("local copies deleted: 5", output)
        self.assertNotIn("must-not-be-printed", output)

    def test_migrate_requires_enabled_and_verified_drive_before_enqueue(self):
        status = mock.Mock(
            return_value={
                "configured": False,
                "connected": False,
                "private_error": "must-not-be-printed",
            }
        )
        archive = self.archive(status=status)
        result, output, errors = self.run_cli(["migrate"], archive)
        self.assertEqual(result, 2)
        self.assertFalse(output)
        self.assertIn("authorized and enabled", errors)
        self.assertNotIn("must-not-be-printed", errors)
        archive.enqueue_existing_recordings.assert_not_called()
        archive.run_archive_until_idle.assert_not_called()

    def test_keep_local_is_explicit_and_preserves_local_copy(self):
        archive = self.archive()
        result, _output, errors = self.run_cli(["migrate", "--keep-local"], archive)
        self.assertEqual(result, 0)
        self.assertFalse(errors)
        archive.run_archive_until_idle.assert_called_once_with(
            self.settings,
            delete_local=False,
            limit=None,
        )

    def test_partial_migration_returns_nonzero_without_leaking_details(self):
        run = mock.Mock(
            return_value={
                "migrated_count": 1,
                "deleted_local_count": 1,
                "failed_count": 1,
                "remaining_count": 2,
                "private_detail": "must-not-be-printed",
            }
        )
        archive = self.archive(run=run)
        result, output, errors = self.run_cli(["migrate"], archive)
        self.assertEqual(result, 1)
        self.assertFalse(errors)
        self.assertIn("failed: 1", output)
        self.assertIn("remaining: 2", output)
        self.assertNotIn("must-not-be-printed", output)

    def test_archive_exception_does_not_reflect_private_details(self):
        private = "private-account/title/path/drive-id"
        archive = self.archive(status=mock.Mock(side_effect=RuntimeError(private)))
        result, output, errors = self.run_cli(["status"], archive)
        self.assertEqual(result, 2)
        self.assertFalse(output)
        self.assertNotIn(private, errors)
        self.assertIn("safe to check status and retry", errors)

    def test_live_managed_server_blocks_mutating_migration_but_not_dry_run(self):
        archive = self.archive()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(
                    ["migrate"],
                    settings_loader=lambda: self.settings,
                    archive=archive,
                    server_running_check=lambda _settings: True,
                )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Stop the managed local API server", stderr.getvalue())
        archive.enqueue_existing_recordings.assert_not_called()
        archive.run_archive_until_idle.assert_not_called()

        result, output, errors = self.run_cli(["migrate", "--dry-run"], archive)
        self.assertEqual(result, 0)
        self.assertIn("dry-run", output)
        self.assertFalse(errors)

    def test_auth_uses_private_default_paths_and_warns_about_testing_expiry(self):
        authorizer = mock.Mock()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main(
                ["auth", "--no-browser", "--timeout", "60"],
                settings_loader=lambda: self.settings,
                authorizer=authorizer,
                server_running_check=lambda _settings: False,
            )
        self.assertEqual(result, 0)
        authorizer.assert_called_once_with(
            self.settings.data_dir / "google-drive" / "oauth-client.json",
            self.settings.data_dir / "google-drive" / "token.json",
            timeout_seconds=60,
            open_browser=False,
        )
        output = stdout.getvalue()
        self.assertIn("Testing", output)
        self.assertIn("7일", output)
        self.assertNotIn(CLIENT_SECRET, output)

    def test_live_managed_server_blocks_oauth_replacement(self):
        authorizer = mock.Mock()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(
                ["auth"],
                settings_loader=lambda: self.settings,
                authorizer=authorizer,
                server_running_check=lambda _settings: True,
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("before replacing Google authorization", stderr.getvalue())
        authorizer.assert_not_called()


class ArchiveCliIntegrationTests(unittest.TestCase):
    def test_dry_run_counts_ready_root_file_as_folder_organization_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts = ("user-alpha", "user-beta")
            settings = Settings(
                data_dir=root / "data",
                model_cache_dir=root / "models",
                accounts=accounts,
                recording_free_reserve_bytes=0,
            )
            database = Database(settings.database_path, accounts)
            database.initialize()
            lecture_id = str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO lectures"
                    "(id, username, title, language, created_at, recording_finalized) "
                    "VALUES (?, ?, 'private title', 'ko', '2026-01-01T00:00:00Z', 1)",
                    (lecture_id, accounts[0]),
                )
                connection.execute(
                    "INSERT INTO recording_archives"
                    "(lecture_id, state, object_key, drive_file_id, source_bytes, "
                    "source_sha256, source_md5, uploaded_bytes, local_deleted, "
                    "folder_layout_version, updated_at) "
                    "VALUES (?, 'ready', ?, 'opaqueDriveFile_1', 44, ?, ?, 44, 0, 0, ?)",
                    (
                        lecture_id,
                        "a" * 64,
                        "b" * 64,
                        "c" * 32,
                        "2026-01-01T00:01:00Z",
                    ),
                )

            report = plan_existing_recordings(settings)

            self.assertEqual(report["candidate_count"], 0)
            self.assertEqual(report["already_ready_count"], 0)
            self.assertEqual(report["organization_pending_count"], 1)

    def test_real_dry_run_missing_database_creates_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(
                data_dir=root / "missing-data",
                model_cache_dir=root / "models",
                accounts=("user-alpha", "user-beta"),
            )
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(
                    ["migrate", "--dry-run"],
                    settings_loader=lambda: settings,
                    server_running_check=lambda _settings: False,
                )

            self.assertEqual(raised.exception.code, 2)
            self.assertFalse(settings.data_dir.exists())
            self.assertFalse(settings.database_path.exists())
            self.assertNotIn(str(settings.data_dir), stderr.getvalue())

    def test_real_dry_run_finds_finalized_wav_without_enqueue_or_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts = ("user-alpha", "user-beta")
            settings = Settings(
                data_dir=root / "data",
                model_cache_dir=root / "models",
                accounts=accounts,
                recording_free_reserve_bytes=0,
            )
            database = Database(settings.database_path, accounts)
            database.initialize()
            lecture_id = str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO lectures"
                    "(id, username, title, language, created_at, recording_finalized) "
                    "VALUES (?, ?, 'private title', 'ko', '2026-01-01T00:00:00Z', 1)",
                    (lecture_id, accounts[0]),
                )
            recordings = RecordingStore(
                settings.data_dir / "recordings",
                accounts,
                max_total_bytes=1024 * 1024,
                min_free_bytes=0,
                max_seconds=60,
            )
            recordings.write_chunk(
                accounts[0],
                lecture_id,
                start_seconds=0,
                overlap_seconds=0,
                pcm=b"\0\0" * 160,
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = main(
                    ["migrate", "--dry-run"],
                    settings_loader=lambda: settings,
                    server_running_check=lambda _settings: False,
                )

            self.assertEqual(result, 0)
            output = stdout.getvalue()
            self.assertIn("migration candidates: 1", output)
            self.assertIn("candidate bytes: 364", output)
            self.assertNotIn(accounts[0], output)
            self.assertNotIn("private title", output)
            self.assertTrue(recordings.available(accounts[0], lecture_id))
            with database.connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM recording_archives").fetchone()[0],
                    0,
                )

    def test_real_dry_run_reads_legacy_schema_without_upgrading_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts = ("user-alpha", "user-beta")
            settings = Settings(
                data_dir=root / "data",
                model_cache_dir=root / "models",
                accounts=accounts,
                recording_free_reserve_bytes=0,
            )
            database = Database(settings.database_path, accounts)
            database.initialize()
            lecture_id = str(uuid.uuid4())
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO lectures"
                    "(id, username, title, language, created_at, recording_finalized) "
                    "VALUES (?, ?, 'private title', 'ko', '2026-01-01T00:00:00Z', 1)",
                    (lecture_id, accounts[0]),
                )
                connection.execute("DROP TABLE drive_archive_binding")
                connection.execute("DROP TABLE recording_archives")
                connection.execute("PRAGMA user_version = 6")
            recordings = RecordingStore(
                settings.data_dir / "recordings",
                accounts,
                max_total_bytes=1024 * 1024,
                min_free_bytes=0,
                max_seconds=60,
            )
            recordings.write_chunk(
                accounts[0],
                lecture_id,
                start_seconds=0,
                overlap_seconds=0,
                pcm=b"\0\0" * 160,
            )
            with sqlite3.connect(settings.database_path) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            database_before = settings.database_path.read_bytes()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = main(
                    ["migrate", "--dry-run"],
                    settings_loader=lambda: settings,
                    server_running_check=lambda _settings: False,
                )

            self.assertEqual(result, 0)
            self.assertIn("migration candidates: 1", stdout.getvalue())
            self.assertEqual(settings.database_path.read_bytes(), database_before)
            with sqlite3.connect(settings.database_path) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertNotIn("recording_archives", tables)
            self.assertNotIn("drive_archive_binding", tables)
            self.assertTrue(recordings.available(accounts[0], lecture_id))


if __name__ == "__main__":
    unittest.main()
