"""Synthetic-only Windows lease scheduling; no network, gh or service access."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from scripts.runtime_config import runtime_config
from server import windows_lease_renewal as native
from server.lease_renewal import LeaseRenewer, create_lease_renewer
from server.platform_files import atomic_write_private, ensure_private_directory

URL = "https://synthetic-renewal-only.trycloudflare.com"


class NativeLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "private-production-synthetic"
        self.data, self.env = self.root / "data", self.root / "config" / "service.env"
        for path in (self.root, self.data, self.env.parent):
            ensure_private_directory(path)
        atomic_write_private(self.env, b"# synthetic fixture, no credentials\n")
        for name, value in (("SERVICE_ROOT", self.root), ("DATA_DIR", self.data), ("ENV_FILE", self.env)):
            patched = mock.patch.object(native, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.now, self.mono = 1700000000.0, 1000.0
        self.api = mock.Mock()
        self.api.record.return_value = {"pid": os.getpid()}
        self.api.matching.return_value = True
        self.tunnel = mock.Mock()
        self.record = {"instance": "a" * 32, "pid": 1234, "created": "123456",
                       "exe": "synthetic-cloudflared.exe", "api_url": URL}
        self.tunnel.read_record.return_value = self.record
        self.tunnel.running.return_value = True
        self.publisher = mock.Mock()
        self.set_publication(age=18 * 3600)
        self.adapter = native.WindowsLeaseAdapter(api=self.api, tunnel=self.tunnel, publisher=self.publisher)
        self.publisher.renew.side_effect = self.succeed

    def document(self, age=0, value=URL):
        return runtime_config(value, datetime.fromtimestamp(self.now - age, timezone.utc))

    def set_publication(self, age=0):
        value = self.document(age)
        self.publisher.read_desired.return_value = value
        self.tunnel.desired.return_value = value
        self.publisher.read_confirmation.return_value = {
            "version": 1, "config": value,
            "owner": {key: self.record[key] for key in native.OWNER_KEYS},
        }

    def succeed(self, **kwargs):
        self.set_publication()

    def worker(self):
        worker = LeaseRenewer(data_dir=self.data, controller=self.adapter, runner=self.adapter.run,
                              lease_reader=self.adapter.read_lease, clock=lambda: self.now,
                              monotonic=lambda: self.mono)
        self.addCleanup(worker.stop)
        return worker

    def advance(self, seconds):
        self.now += seconds
        self.mono += seconds

    def test_exact_threshold_keeps_existing_loop_and_cached_status(self):
        self.set_publication(age=18 * 3600 - 1)
        worker = self.worker()
        worker.check_once()
        self.publisher.renew.assert_not_called()
        self.assertEqual(worker.status()["state"], "waiting")
        self.advance(1)
        worker.check_once()
        self.publisher.renew.assert_called_once()
        self.assertEqual(worker.status()["expires_in_seconds"], 86400)
        self.assertEqual(worker.status()["last_success_at"], self.now)
        reads = self.publisher.read_desired.call_count
        for _ in range(5):
            worker.status()
        self.assertEqual(self.publisher.read_desired.call_count, reads)

    def test_missing_offline_and_first_unconfirmed_never_publish(self):
        for desired, confirmation, error in (
            (None, None, "desired_missing"),
            (self.document(value="OFFLINE"), None, "offline"),
            (self.document(age=23 * 3600), None, "publication_unconfirmed"),
        ):
            with self.subTest(error=error):
                self.publisher.read_desired.return_value = desired
                self.publisher.read_confirmation.return_value = confirmation
                worker = self.worker()
                worker.check_once()
                self.assertEqual(worker.status()["error_code"], error)
        self.publisher.renew.assert_not_called()

    def test_new_launch_cannot_reuse_previous_confirmation(self):
        self.record = {**self.record, "instance": "b" * 32}
        self.tunnel.read_record.return_value = self.record
        worker = self.worker()
        worker.check_once()
        self.assertEqual(worker.status()["error_code"], "publication_unconfirmed")
        self.publisher.renew.assert_not_called()

    def test_foreign_api_pid_dead_tunnel_or_changed_url_blocks_renewal(self):
        changes = [(self.api.record, {"pid": os.getpid() + 1000}),
                   (self.tunnel.running, False),
                   (self.tunnel.desired, self.document(value="OFFLINE"))]
        for method, value in changes:
            previous = method.return_value
            with self.subTest(change=method._mock_name):
                method.return_value = value
                worker = self.worker()
                worker.check_once()
                self.assertIn(worker.status()["error_code"], {"process_not_owned", "url_changed"})
                method.return_value = previous
        self.publisher.renew.assert_not_called()

    def test_failed_dispatch_is_retried_after_restart_from_confirmed_expiry(self):
        def fail_after_dispatch(**kwargs):
            self.publisher.read_desired.return_value = self.document()
            raise RuntimeError("synthetic failure must not escape")
        self.publisher.renew.side_effect = fail_after_dispatch
        worker = self.worker()
        worker.check_once()
        self.assertEqual(worker.status()["error_code"], "publication_failed")
        self.assertEqual(worker.status()["expires_in_seconds"], 6 * 3600)
        worker.check_once()
        self.publisher.renew.assert_called_once()
        self.advance(299)
        worker.check_once()
        self.publisher.renew.assert_called_once()
        self.advance(1)
        self.publisher.renew.side_effect = self.succeed
        replacement = self.worker()
        replacement.check_once()
        self.assertEqual(self.publisher.renew.call_count, 2)
        self.assertEqual(replacement.status()["last_success_at"], self.now)

    def test_wall_clock_jump_does_not_skip_monotonic_failure_backoff(self):
        self.publisher.renew.side_effect = RuntimeError("synthetic")
        worker = self.worker()
        worker.check_once()
        self.now += 10000
        worker.check_once()
        self.publisher.renew.assert_called_once()
        self.assertEqual(worker.status()["error_code"], "retry_wait")

    def test_shutdown_cancels_direct_publisher_without_a_child_or_late_success(self):
        entered, ended = threading.Event(), threading.Event()
        def cancellable(**kwargs):
            entered.set()
            kwargs["cancel_event"].wait(2)
            ended.set()
        self.publisher.renew.side_effect = cancellable
        worker = self.worker()
        with mock.patch("server.lease_renewal.subprocess.Popen") as spawn:
            worker.start()
            self.assertTrue(entered.wait(2))
            self.assertTrue(worker.stop(timeout=2))
            self.assertTrue(ended.is_set())
            self.assertIsNone(worker.status()["last_success_at"])
            self.assertEqual(worker.status()["state"], "stopped")
            spawn.assert_not_called()

    def test_process_or_url_change_during_publication_is_not_success(self):
        def changed(**kwargs):
            self.succeed()
            self.tunnel.running.return_value = False
        self.publisher.renew.side_effect = changed
        worker = self.worker()
        worker.check_once()
        self.assertEqual(worker.status()["error_code"], "process_not_owned")
        self.assertIsNone(worker.status()["last_success_at"])

    def test_expired_confirmed_same_tunnel_can_recover(self):
        self.set_publication(age=25 * 3600)
        worker = self.worker()
        worker.check_once()
        self.publisher.renew.assert_called_once()
        self.assertEqual(worker.status()["expires_in_seconds"], 86400)

    def test_wrong_profile_never_reads_publisher_or_calls_it(self):
        worker = LeaseRenewer(data_dir=self.root / "local-test", controller=self.adapter,
                              runner=self.adapter.run, lease_reader=self.adapter.read_lease)
        worker.check_once()
        self.assertEqual(worker.status()["error_code"], "unsafe_profile")
        self.publisher.read_desired.assert_not_called()
        self.publisher.renew.assert_not_called()

    def test_native_factory_requires_explicit_opt_in_fixed_env_and_production_path(self):
        cases = [({}, self.data, True),
                 ({"AUTO_RENEW_API_URL": "1", "STT_ENV_FILE": str(self.env)}, self.data, False),
                 ({"AUTO_RENEW_API_URL": "1", "STT_ENV_FILE": "unrelated.env"}, self.data, True),
                 ({"AUTO_RENEW_API_URL": "1", "STT_ENV_FILE": str(self.env)}, self.root / "local", True)]
        for environment, path, enabled in cases:
            with self.subTest(environment_count=len(environment), enabled=enabled), \
                 mock.patch.dict(os.environ, environment, clear=True), \
                 mock.patch.object(native, "WindowsLeaseAdapter") as factory:
                worker = native.create_windows_lease_renewer(data_dir=path, enabled=enabled)
                worker.start()
                worker.check_once()
                self.assertFalse(worker.status()["enabled"])
                factory.assert_not_called()
        with mock.patch.dict(os.environ, {"AUTO_RENEW_API_URL": "1", "STT_ENV_FILE": str(self.env)}, clear=True), \
             mock.patch.object(native, "WindowsLeaseAdapter", return_value=self.adapter):
            worker = native.create_windows_lease_renewer(data_dir=self.data)
            self.assertTrue(worker.status()["enabled"])
            self.publisher.renew.assert_not_called()
            self.publisher.read_desired.assert_not_called()

    def test_controller_command_cannot_be_changed_and_no_environment_is_forwarded(self):
        self.assertEqual(self.adapter._safe_environment(), {})
        event = threading.Event()
        self.assertEqual(self.adapter.run(("unrelated",), self.root, {}, 100, event), 130)
        event.set()
        self.assertEqual(self.adapter.run(native.NATIVE_RENEW_COMMAND, self.root, {}, 100, event), 130)
        self.publisher.renew.assert_not_called()


if __name__ == "__main__":
    unittest.main()