from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts.runtime_config import runtime_config
from server.lease_renewal import LeaseRenewer, LeaseStateError, create_lease_renewer, read_desired_lease


URL = "https://fake-lease-example.trycloudflare.com"


class FakeClock:
    def __init__(self):
        self.wall = 1700000000.0
        self.mono = 1000.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class FakeController:
    def __init__(self, root):
        self.root = root
        self.owned = True

    def renewal_processes_owned(self):
        return self.owned

    def renewal_command(self):
        return (str(self.root / "scripts" / "start-tunnel.sh"), "--renew-only", "--port", "8765",
                "--cloudflared", str(self.root / ".tools" / "cloudflared"))

    def _safe_environment(self):
        return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


@unittest.skipIf(os.name == "nt", "Linux private lease files, chmod and process-group renewal")
class LeaseRenewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / ".data"
        self.data.mkdir(mode=0o700)
        self.clock = FakeClock()
        self.controller = FakeController(self.root)
        self.calls = []
        self.workers = []
        self.write("tunnel-url.txt", URL + "\n")
        self.write("server.pid", "1234\n")
        self.write("tunnel.pid", "2345\n")
        self.desired(age=18 * 3600)

    def tearDown(self):
        for worker in self.workers:
            worker.stop(timeout=2)
        self.temp.cleanup()

    def write(self, name, value):
        path = self.data / name
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)

    def desired(self, age=0, url=URL):
        value = runtime_config(url, datetime.fromtimestamp(self.clock.wall - age, timezone.utc))
        self.write("pages-desired-config.json", json.dumps(value))
        return value

    def run_ok(self, command, cwd, environment, timeout, cancelled):
        self.calls.append((command, cwd, environment, timeout))
        self.desired()
        return 0

    def worker(self, **changes):
        options = dict(project_root=self.root, data_dir=self.data, controller=self.controller,
                       clock=lambda: self.clock.wall, monotonic=lambda: self.clock.mono, runner=self.run_ok)
        options.update(changes)
        worker = LeaseRenewer(**options)
        self.workers.append(worker)
        return worker

    def test_exact_six_hour_threshold_renews_same_url_once(self):
        self.desired(age=18 * 3600 - 1)
        worker = self.worker()
        worker.check_once()
        self.assertEqual(self.calls, [])
        self.clock.advance(1)
        worker.check_once()
        self.assertEqual(len(self.calls), 1)
        self.assertIn("--renew-only", self.calls[0][0])
        self.assertNotIn(URL, str(self.calls[0]))
        self.assertEqual(worker.status()["last_success_at"], self.clock.wall)
        self.assertEqual(worker.status()["expires_in_seconds"], 24 * 3600)
        worker.check_once()
        self.assertEqual(len(self.calls), 1)

    def test_expired_existing_online_lease_can_be_republished(self):
        self.desired(age=25 * 3600)
        worker = self.worker()
        worker.check_once()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(worker.status()["state"], "waiting")

    def test_absent_or_explicitly_offline_desired_state_never_starts_tunnel(self):
        worker = self.worker()
        self.desired(url="OFFLINE")
        worker.check_once()
        self.assertEqual(worker.status()["state"], "offline")
        (self.data / "pages-desired-config.json").unlink()
        worker.check_once()
        self.assertEqual(worker.status()["error_code"], "desired_missing")
        self.assertEqual(self.calls, [])

    def test_unowned_process_missing_pid_and_changed_url_are_fail_closed(self):
        worker = self.worker()
        self.controller.owned = False
        worker.check_once()
        self.assertEqual(worker.status()["error_code"], "process_not_owned")
        self.controller.owned = True
        (self.data / "server.pid").unlink()
        worker.check_once()
        self.write("server.pid", "1234\n")
        self.write("tunnel-url.txt", "https://other-example.trycloudflare.com\n")
        worker.check_once()
        self.assertEqual(worker.status()["error_code"], "url_changed")
        self.assertEqual(self.calls, [])

    def test_future_publication_and_invalid_lifetime_do_not_extend_any_lease(self):
        worker = self.worker()
        self.desired(age=-301)
        worker.check_once()
        self.assertEqual(worker.status()["error_code"], "desired_invalid")
        value = self.desired()
        value["expiresAt"] = "2099-01-01T00:00:00Z"
        self.write("pages-desired-config.json", json.dumps(value))
        worker.check_once()
        self.assertEqual(self.calls, [])

    def test_regular_private_bounded_files_and_directory_are_required(self):
        path = self.data / "pages-desired-config.json"
        for mode in (0o644, 0o660):
            path.chmod(mode)
            with self.assertRaises(LeaseStateError):
                read_desired_lease(self.data, now=self.clock.wall)
        path.chmod(0o600)
        self.data.chmod(0o755)
        with self.assertRaises(LeaseStateError):
            read_desired_lease(self.data, now=self.clock.wall)
        self.data.chmod(0o700)
        path.unlink()
        path.symlink_to(self.data / "server.pid")
        with self.assertRaises(LeaseStateError):
            read_desired_lease(self.data, now=self.clock.wall)
        path.unlink()
        self.write("pages-desired-config.json", "x" * 4097)
        with self.assertRaises(LeaseStateError):
            read_desired_lease(self.data, now=self.clock.wall)
        path.unlink()
        os.mkfifo(path, 0o600)
        started = time.monotonic()
        with self.assertRaises(LeaseStateError):
            read_desired_lease(self.data, now=self.clock.wall)
        self.assertLess(time.monotonic() - started, 0.2)

    def test_symlinked_data_directory_and_duplicate_json_keys_are_rejected(self):
        alias = self.root / "linked-data"
        alias.symlink_to(self.data, target_is_directory=True)
        with self.assertRaises(LeaseStateError):
            read_desired_lease(alias, now=self.clock.wall)
        value = json.dumps(self.desired())
        self.write("pages-desired-config.json", value[:-1] + ',"state":"online"}')
        with self.assertRaises(LeaseStateError):
            read_desired_lease(self.data, now=self.clock.wall)

    def test_failures_back_off_at_least_five_monotonic_minutes(self):
        def fail(*args):
            self.calls.append(args)
            return 1  # Includes a failed local/external health probe in the script.
        worker = self.worker(runner=fail)
        worker.check_once()
        self.clock.wall += 10000  # wall-clock jumps do not accelerate the retry
        worker.check_once()
        self.clock.mono += 299
        worker.check_once()
        self.assertEqual(len(self.calls), 1)
        self.clock.mono += 1
        worker.check_once()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(worker.status()["last_success_at"], None)

    def test_failed_cdn_confirmation_retries_even_after_desired_file_was_renewed(self):
        def unconfirmed(*args):
            self.run_ok(*args)
            return 1
        worker = self.worker(runner=unconfirmed)
        worker.check_once()
        self.assertEqual(worker.status()["error_code"], "publication_failed")
        self.clock.advance(300)
        worker._runner = self.run_ok
        worker.check_once()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(worker.status()["last_success_at"], self.clock.wall)

    def test_zero_exit_without_fresh_publisher_record_is_not_success(self):
        worker = self.worker(runner=lambda *args: 0)
        before = (self.data / "pages-desired-config.json").read_bytes()
        worker.check_once()
        self.assertEqual(worker.status()["error_code"], "publication_failed")
        self.assertEqual(worker.status()["last_success_at"], None)
        self.assertEqual((self.data / "pages-desired-config.json").read_bytes(), before)

    def test_offline_stop_and_url_change_during_publication_are_not_recorded_as_success(self):
        def stop_during(*args):
            self.desired(url="OFFLINE")
            return 0
        worker = self.worker(runner=stop_during)
        worker.check_once()
        self.assertEqual(worker.status()["state"], "offline")
        self.assertEqual(worker.status()["last_success_at"], None)
        self.desired(age=18 * 3600)
        self.clock.advance(300)
        def change_during(*args):
            other = "https://other-example.trycloudflare.com"
            self.desired(url=other)
            self.write("tunnel-url.txt", other + "\n")
            return 0
        worker._runner = change_during
        worker.check_once()
        self.assertEqual(worker.status()["error_code"], "url_or_process_changed")
        self.assertEqual(worker.status()["last_success_at"], None)

    def test_status_is_nonblocking_and_redacted_while_one_worker_renews(self):
        entered = threading.Event()
        def blocked(command, cwd, environment, timeout, cancelled):
            self.calls.append(command)
            entered.set()
            cancelled.wait(2)
            return 130
        worker = self.worker(runner=blocked)
        worker.start()
        worker.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        status = worker.status()
        worker.check_once()
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(status["renewing"])
        self.assertEqual(set(status), {"enabled", "state", "expires_in_seconds", "last_attempt_at",
                                       "last_success_at", "renewing", "error_code"})
        self.assertNotIn("trycloudflare", str(status))
        self.assertTrue(worker.stop(timeout=1))
        self.assertFalse(worker.status()["renewing"])
        worker.start()
        self.assertEqual(len(self.calls), 1)

    def test_shutdown_before_execution_never_starts_a_subprocess(self):
        worker = self.worker()
        worker.request_shutdown()
        worker.check_once()
        worker.start()
        self.assertEqual(self.calls, [])

    def test_actual_runner_has_fixed_no_shell_argv_and_kills_the_whole_group_on_cancel(self):
        worker = self.worker(runner=None)
        cancelled = threading.Event()
        process = mock.Mock(pid=43210)
        def wait(timeout):
            if not cancelled.is_set():
                cancelled.set()
                raise subprocess.TimeoutExpired("fixed", timeout)
            return 0
        process.wait.side_effect = wait
        with (mock.patch("server.lease_renewal.subprocess.Popen", return_value=process) as popen,
              mock.patch("server.lease_renewal.os.killpg") as kill):
            result = worker._run_script(self.controller.renewal_command(), self.root,
                                       self.controller._safe_environment(), 10, cancelled)
        self.assertEqual(result, 130)
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(popen.call_args.kwargs[stream], subprocess.DEVNULL)
        self.assertEqual(kill.call_args_list, [mock.call(43210, signal.SIGTERM), mock.call(43210, signal.SIGKILL)])
        self.assertIsNone(worker._process)

    def test_shutdown_signal_reaches_a_registered_process_immediately(self):
        worker = self.worker()
        worker._process = mock.Mock(pid=43210)
        with mock.patch("server.lease_renewal.os.killpg") as kill:
            worker.request_shutdown()
            kill.assert_called_once_with(43210, signal.SIGTERM)
        worker._process = None

    def test_cancellation_also_kills_descendants_when_the_leader_exits_first(self):
        worker = self.worker()
        cancelled = threading.Event()
        process = mock.Mock(pid=43210)
        def wait(timeout):
            cancelled.set()
            return -signal.SIGTERM
        process.wait.side_effect = wait
        with (mock.patch("server.lease_renewal.subprocess.Popen", return_value=process),
              mock.patch("server.lease_renewal.os.killpg") as kill):
            worker._run_script(self.controller.renewal_command(), self.root,
                               self.controller._safe_environment(), 10, cancelled)
        kill.assert_called_once_with(43210, signal.SIGKILL)
        self.assertIsNone(worker._process)

    def test_production_factory_is_disabled_for_isolated_database_and_reads_no_live_state(self):
        with (mock.patch("server.lease_renewal.TunnelController") as controller,
              mock.patch("server.lease_renewal._private_file") as reader):
            worker = create_lease_renewer(data_dir=self.data)
            worker.start()
            worker.check_once()
            self.assertFalse(worker.status()["enabled"])
            controller.assert_not_called()
            reader.assert_not_called()

    def test_unexpected_runner_exception_is_redacted_and_can_be_retried(self):
        def fail(*args):
            raise RuntimeError("fake-key fake-user fake-url")
        worker = self.worker(runner=fail)
        worker.check_once()
        self.assertEqual(worker.status()["error_code"], "renewal_failed")
        self.assertNotIn("fake-key", str(worker.status()))
        self.clock.advance(300)
        worker._runner = self.run_ok
        worker.check_once()
        self.assertEqual(worker.status()["last_success_at"], self.clock.wall)

    def test_runner_timeout_cancels_its_group_without_changing_tunnel_state_files(self):
        worker = self.worker()
        before = (self.data / "pages-desired-config.json").read_bytes()
        process = mock.Mock(pid=43210)
        process.wait.return_value = 0
        with (mock.patch("server.lease_renewal.subprocess.Popen", return_value=process),
              mock.patch("server.lease_renewal.time.monotonic", side_effect=[10.0, 12.0]),
              mock.patch("server.lease_renewal.os.killpg") as kill):
            result = worker._run_script(self.controller.renewal_command(), self.root,
                                       self.controller._safe_environment(), 1, threading.Event())
        self.assertEqual(result, 124)
        self.assertEqual(kill.call_args_list, [mock.call(43210, signal.SIGTERM), mock.call(43210, signal.SIGKILL)])
        self.assertEqual((self.data / "pages-desired-config.json").read_bytes(), before)

    def test_worker_start_failure_is_reported_without_breaking_api_startup(self):
        worker = self.worker()
        with mock.patch("server.lease_renewal.threading.Thread.start", side_effect=RuntimeError("fake detail")):
            worker.start()
        self.assertEqual(worker.status()["error_code"], "worker_start_failed")
        self.assertEqual(self.calls, [])

    def test_confirmation_exception_after_desired_write_still_retries(self):
        def fail_after_write(*args):
            self.run_ok(*args)
            raise RuntimeError("fake CDN failure")
        worker = self.worker(runner=fail_after_write)
        worker.check_once()
        self.clock.advance(300)
        worker._runner = self.run_ok
        worker.check_once()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(worker.status()["state"], "waiting")


class PortableLeaseBoundaryTests(unittest.TestCase):
    def test_lease_json_is_validated_without_a_platform_file_fixture(self):
        now = 1700000000.0
        document = runtime_config(URL, datetime.fromtimestamp(now, timezone.utc))
        with mock.patch("server.lease_renewal._private_file", return_value=json.dumps(document).encode()):
            lease = read_desired_lease(Path("unused-synthetic"), now=now)
        self.assertEqual(lease.api_url, URL)
        self.assertEqual(lease.expires_at - lease.published_at, 86400)
        for payload in (b'{"version":1,"version":1}', b'[]', b'null', b'{invalid'):
            with self.subTest(payload_type=type(payload).__name__), \
                 mock.patch("server.lease_renewal._private_file", return_value=payload):
                with self.assertRaises(LeaseStateError):
                    read_desired_lease(Path("unused-synthetic"), now=now)

    def test_isolated_factory_never_reads_production_or_constructs_control(self):
        with mock.patch("server.lease_renewal.TunnelController") as controller, \
             mock.patch("server.lease_renewal._private_file") as reader:
            worker = create_lease_renewer(data_dir=Path(tempfile.gettempdir()) / "synthetic-isolated")
            worker.start()
            worker.check_once()
            self.assertFalse(worker.status()["enabled"])
            controller.assert_not_called()
            reader.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Native Windows boundary")
    def test_windows_factory_is_disabled_even_with_default_data_path(self):
        from server.lease_renewal import PROJECT_ROOT
        with mock.patch("server.lease_renewal.TunnelController") as controller, \
             mock.patch("server.lease_renewal._private_file") as reader:
            worker = create_lease_renewer(data_dir=PROJECT_ROOT / ".data")
            worker.start()
            worker.check_once()
            self.assertFalse(worker.status()["enabled"])
            controller.assert_not_called()
            reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
