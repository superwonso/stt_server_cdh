from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from server.tunnel_control import TunnelControlSetupError, TunnelController


class FakeRuntime:
    def __init__(self) -> None:
        self.states = {"server": "owned", "tunnel": "owned"}
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str], float]] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.fail_name = ""

    def probe(self, kind: str) -> str:
        return self.states[kind]

    def run(self, command, cwd, environment, timeout) -> int:
        copied = (tuple(command), cwd, dict(environment), timeout)
        self.calls.append(copied)
        self.entered.set()
        if self.block and not self.release.wait(3):
            return 99
        name = Path(command[0]).name
        if name == self.fail_name:
            return 17
        if name == "stop.sh":
            self.states["tunnel"] = "stopped"
        elif name == "start-tunnel.sh":
            self.states["tunnel"] = "owned"
        return 0


class TunnelControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        scripts = self.root / "scripts"
        tools = self.root / ".tools"
        scripts.mkdir()
        tools.mkdir()
        for name in ("start-tunnel.sh", "stop.sh", "status.sh", "publish-api-url.sh"):
            path = scripts / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o700)
        self.cloudflared = tools / "cloudflared"
        self.cloudflared.write_text("test executable\n", encoding="utf-8")
        self.cloudflared.chmod(0o700)
        self.runtime = FakeRuntime()

    def tearDown(self):
        self.runtime.release.set()
        self.temporary.cleanup()

    def controller(self, *, grace_waiter=lambda seconds: None, **changes):
        values = {
            "project_root": self.root,
            "process_probe": self.runtime.probe,
            "script_runner": self.runtime.run,
            "grace_waiter": grace_waiter,
            "cloudflared_path": self.cloudflared,
        }
        values.update(changes)
        return TunnelController(**values)

    def test_restart_returns_before_grace_and_uses_only_fixed_argv(self):
        grace_entered = threading.Event()
        grace_release = threading.Event()

        def grace(seconds):
            self.assertEqual(seconds, 12.0)
            grace_entered.set()
            self.assertTrue(grace_release.wait(2))

        controller = self.controller(grace_waiter=grace)
        before = time.monotonic()
        result = controller.restart()
        elapsed = time.monotonic() - before

        self.assertTrue(result["accepted"])
        self.assertEqual(result["state"], "starting")
        self.assertEqual(result["operation"], "restarting")
        self.assertTrue(result["remote_recovery_possible"])
        self.assertFalse(result["same_public_url_guaranteed"])
        self.assertFalse(result["restart_available"])
        self.assertLess(elapsed, 0.5)
        self.assertTrue(grace_entered.wait(1))
        self.assertEqual(self.runtime.calls, [])

        grace_release.set()
        completed = controller.wait_for_operation()
        self.assertEqual(completed["state"], "online")
        self.assertEqual(completed["operation"], "restart_succeeded")
        self.assertTrue(completed["restart_available"])
        self.assertEqual(len(self.runtime.calls), 2)
        stop, start = self.runtime.calls
        self.assertEqual(
            stop[0],
            (str(self.root / "scripts" / "stop.sh"), "--tunnel-only", "--timeout", "20"),
        )
        self.assertEqual(
            start[0],
            (
                str(self.root / "scripts" / "start-tunnel.sh"),
                "--port",
                "8765",
                "--cloudflared",
                str(self.cloudflared),
                "--timeout",
                "60",
            ),
        )
        for command, cwd, environment, timeout in self.runtime.calls:
            self.assertEqual(cwd, self.root)
            self.assertEqual(timeout, 360.0)
            self.assertEqual(environment["PATH"], "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
            self.assertNotIn("BASH_ENV", environment)
            self.assertNotIn("GH_BIN", environment)
            self.assertNotIn("CLOUDFLARED_BIN", environment)
            self.assertNotIn(";", "".join(command))

    def test_stop_explicitly_reports_that_remote_recovery_is_impossible(self):
        controller = self.controller()
        accepted = controller.stop()
        self.assertTrue(accepted["accepted"])
        self.assertFalse(accepted["remote_recovery_possible"])
        self.assertTrue(accepted["local_start_required"])
        self.assertFalse(accepted["restart_available"])
        self.assertIn("원격으로 다시 켤 수 없어", accepted["message"])

        completed = controller.wait_for_operation()
        self.assertEqual(completed["state"], "offline")
        self.assertEqual(completed["operation"], "stop_succeeded")
        self.assertFalse(completed["remote_recovery_possible"])
        self.assertTrue(completed["local_start_required"])
        self.assertEqual([Path(call[0][0]).name for call in self.runtime.calls], ["stop.sh"])

    def test_simultaneous_requests_have_one_worker_and_no_second_script(self):
        self.runtime.block = True
        controller = self.controller()
        first = controller.restart()
        self.assertTrue(first["accepted"])
        self.assertTrue(self.runtime.entered.wait(1))

        second = controller.restart()
        third = controller.stop()
        self.assertFalse(second["accepted"])
        self.assertFalse(third["accepted"])
        self.assertIn("이미 진행 중", second["message"])
        self.assertEqual(len(self.runtime.calls), 1)

        self.runtime.release.set()
        controller.wait_for_operation()
        self.assertEqual(len(self.runtime.calls), 2)

    def test_server_and_tunnel_pid_ownership_fail_closed(self):
        controller = self.controller()
        self.runtime.states["server"] = "conflict"
        self.assertFalse(controller.status()["restart_available"])
        denied = controller.restart()
        self.assertFalse(denied["accepted"])
        self.assertEqual(denied["state"], "online")
        self.assertIn("API 프로세스의 소유권", denied["message"])
        self.assertEqual(self.runtime.calls, [])

        self.runtime.states["server"] = "owned"
        self.runtime.states["tunnel"] = "conflict"
        self.assertFalse(controller.status()["restart_available"])
        denied = controller.restart()
        self.assertFalse(denied["accepted"])
        self.assertEqual(denied["state"], "error")
        self.assertFalse(denied["remote_recovery_possible"])
        self.assertEqual(self.runtime.calls, [])

    def test_restart_can_start_from_stopped_without_running_stop_first(self):
        self.runtime.states["tunnel"] = "stopped"
        controller = self.controller()
        accepted = controller.restart()
        self.assertTrue(accepted["accepted"])
        completed = controller.wait_for_operation()
        self.assertEqual(completed["state"], "online")
        self.assertEqual([Path(call[0][0]).name for call in self.runtime.calls], ["start-tunnel.sh"])

    def test_script_failure_is_redacted_and_allows_a_later_retry(self):
        self.runtime.fail_name = "start-tunnel.sh"
        controller = self.controller()
        controller.restart()
        failed = controller.wait_for_operation()
        self.assertEqual(failed["state"], "error")
        self.assertEqual(failed["operation"], "restart_failed")
        self.assertNotIn("17", str(failed))
        self.assertNotIn("error_code", failed)
        self.assertFalse(failed["remote_recovery_possible"])
        self.assertTrue(failed["local_start_required"])
        self.assertTrue(failed["restart_available"])

        self.runtime.fail_name = ""
        retried = controller.restart()
        self.assertTrue(retried["accepted"])
        self.assertEqual(controller.wait_for_operation()["operation"], "restart_succeeded")

    def test_unexpected_worker_error_reaches_terminal_state_and_does_not_deadlock(self):
        controller = self.controller()
        with mock.patch.object(controller, "_worker_body", side_effect=RuntimeError("private detail")):
            accepted = controller.restart()
            failed = controller.wait_for_operation()

        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["state"], "starting")
        self.assertEqual(accepted["operation"], "restarting")
        self.assertEqual(failed["state"], "error")
        self.assertEqual(failed["operation"], "restart_failed")
        self.assertNotIn("private detail", str(failed))
        self.assertTrue(controller.restart()["accepted"])
        controller.wait_for_operation()

    def test_fixed_scripts_must_not_be_symlinked_or_group_writable(self):
        status = self.root / "scripts" / "status.sh"
        status.chmod(0o720)
        with self.assertRaises(TunnelControlSetupError):
            self.controller()

        status.chmod(0o700)
        status.unlink()
        status.symlink_to(self.root / "scripts" / "stop.sh")
        with self.assertRaises(TunnelControlSetupError):
            self.controller()

    def test_invalid_probe_result_and_cloudflared_absence_fail_closed(self):
        self.runtime.states["tunnel"] = "surprising"
        controller = self.controller()
        self.assertEqual(controller.status()["state"], "error")
        self.assertFalse(controller.restart()["accepted"])

        self.runtime.states["tunnel"] = "stopped"
        missing = self.root / ".tools" / "missing-cloudflared"
        controller = self.controller(cloudflared_path=missing)
        self.assertFalse(controller.status()["restart_available"])
        denied = controller.restart()
        self.assertFalse(denied["accepted"])
        self.assertIn("cloudflared", denied["message"])

    def test_default_tunnel_probe_requires_exact_owned_process(self):
        controller = self.controller()
        pid = 4321
        command = [
            str(self.cloudflared),
            "tunnel",
            "--url",
            "http://127.0.0.1:8765",
            "--no-autoupdate",
        ]
        with (
            mock.patch.object(controller, "_read_pid", return_value=("candidate", pid)),
            mock.patch.object(controller, "_process_is_live", return_value=True),
            mock.patch.object(
                controller,
                "_process_details",
                return_value=(self.root, command, self.cloudflared),
            ) as details,
        ):
            self.assertEqual(controller._default_process_probe("tunnel"), "owned")
            details.return_value = (self.root, command, self.root / "different-binary")
            self.assertEqual(controller._default_process_probe("tunnel"), "conflict")
            details.return_value = (
                self.root,
                [str(self.cloudflared), "tunnel", "--url", "http://127.0.0.1:9999", "--no-autoupdate"],
                self.cloudflared,
            )
            self.assertEqual(controller._default_process_probe("tunnel"), "conflict")
            details.return_value = (self.root.parent, command, self.cloudflared)
            self.assertEqual(controller._default_process_probe("tunnel"), "conflict")

    def test_default_server_probe_requires_the_fixed_loopback_binding(self):
        controller = self.controller()
        pid = os.getpid()
        command = [
            str(self.root / ".venv" / "bin" / "python"),
            "-m",
            "uvicorn",
            "server.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--workers",
            "1",
        ]
        with (
            mock.patch.object(controller, "_read_pid", return_value=("candidate", pid)),
            mock.patch.object(controller, "_process_is_live", return_value=True),
            mock.patch.object(
                controller,
                "_process_details",
                return_value=(self.root, command, self.root / "python"),
            ) as details,
        ):
            self.assertEqual(controller._default_process_probe("server"), "owned")
            wrong_port = command.copy()
            wrong_port[wrong_port.index("8765")] = "9999"
            details.return_value = (self.root, wrong_port, self.root / "python")
            self.assertEqual(controller._default_process_probe("server"), "conflict")
            public_host = command.copy()
            public_host[public_host.index("127.0.0.1")] = "0.0.0.0"
            details.return_value = (self.root, public_host, self.root / "python")
            self.assertEqual(controller._default_process_probe("server"), "conflict")

    def test_pid_file_must_be_private_regular_and_not_a_symlink(self):
        controller = self.controller()
        data = self.root / ".data"
        data.mkdir()
        pid_file = data / "tunnel.pid"
        pid_file.write_text("4321\n", encoding="ascii")
        pid_file.chmod(0o600)
        self.assertEqual(controller._read_pid("tunnel"), ("candidate", 4321))

        pid_file.chmod(0o620)
        self.assertEqual(controller._read_pid("tunnel"), ("conflict", None))
        pid_file.unlink()
        pid_file.symlink_to(data / "missing")
        self.assertEqual(controller._read_pid("tunnel"), ("conflict", None))


if __name__ == "__main__":
    unittest.main()
