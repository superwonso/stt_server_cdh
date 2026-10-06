from __future__ import annotations

import copy
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from server.db import Database
from server.model_control import (ModelControl, ModelControlError, WindowsModelBackend,
                                  create_model_control, sanitized_status)
from server.model_process import ModelProcessError
from server.settings import Settings
from server.win_model_process import WindowsModelController


class FakeBackend:
    def __init__(self, state="error"):
        self.state = state
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = 0
        self.fail = False

    def inspect(self):
        return {"state": self.state, "record": {"instance": "synthetic"}}

    def restart(self, snapshot, cancelled):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(3):
            raise AssertionError("Synthetic worker did not finish")
        if self.fail:
            raise RuntimeError("SYNTHETIC-SECRET-PATH-AND-TOKEN")
        self.state = "ready"


class ModelControlTests(unittest.TestCase):
    def test_reads_and_healthy_loading_unknown_states_never_restart(self):
        for state in ("ready", "loading", "unknown"):
            with self.subTest(state=state):
                backend = FakeBackend(state)
                control = ModelControl(backend)
                audit = mock.Mock()
                self.assertFalse(control.status()["restart_available"])
                with self.assertRaises(ModelControlError):
                    control.request_restart(audit=audit, authorize=lambda: True)
                audit.assert_not_called()
                self.assertEqual(backend.calls, 0)
        control = ModelControl()
        self.assertFalse(control.status()["supported"])
        with self.assertRaises(ModelControlError) as raised:
            control.request_restart(audit=mock.Mock(), authorize=lambda: True)
        self.assertEqual(raised.exception.code, "unsupported")

    def test_single_worker_duplicate_block_and_success_audits(self):
        backend = FakeBackend()
        control = ModelControl(backend)
        records = []
        try:
            result = control.request_restart(audit=records.append, authorize=lambda: True)
            self.assertEqual(result["operation"], "restarting")
            self.assertFalse(result["restart_available"])
            self.assertTrue(backend.entered.wait(2))
            with self.assertRaises(ModelControlError) as raised:
                control.request_restart(audit=records.append, authorize=lambda: True)
            self.assertEqual(raised.exception.code, "busy")
            self.assertEqual(records, ["accepted"])
        finally:
            backend.release.set()
            self.assertTrue(control.stop(3))
        self.assertEqual(records, ["accepted", "success"])
        self.assertEqual(backend.calls, 1)
        self.assertEqual(control.status()["operation"], "restart_succeeded")
        self.assertEqual(control.status()["state"], "ready")
        backend.state = "error"
        self.assertTrue(control.status()["restart_available"])
        self.assertIn("오류", control.status()["message"])

    def test_failure_has_safe_status_and_audit_without_exception_contents(self):
        backend = FakeBackend()
        backend.fail = True
        backend.release.set()
        control = ModelControl(backend)
        records = []
        control.request_restart(audit=records.append, authorize=lambda: True)
        self.assertTrue(control.stop(3))
        result = control.status()
        self.assertEqual(result["operation"], "restart_failed")
        self.assertNotIn("SECRET", str(result))
        self.assertEqual(records, ["accepted", "failed"])

    def test_revoked_request_or_shutdown_before_execution_cannot_start_model(self):
        backend = FakeBackend()
        control = ModelControl(backend)
        records = []
        control.request_restart(audit=records.append, authorize=mock.Mock(side_effect=[True, False]))
        self.assertTrue(control.stop(3))
        self.assertEqual(backend.calls, 0)
        self.assertEqual(records, ["accepted", "failed"])
        control.request_shutdown()
        self.assertFalse(control.status()["restart_available"])
        with self.assertRaises(ModelControlError):
            control.request_restart(audit=records.append, authorize=lambda: True)
        self.assertEqual(backend.calls, 0)

    def test_audit_failure_prevents_start_and_hook_fields_are_closed(self):
        backend = FakeBackend()
        control = ModelControl(backend)
        with self.assertRaises(sqlite3.OperationalError):
            control.request_restart(audit=mock.Mock(side_effect=sqlite3.OperationalError("private")), authorize=lambda: True)
        self.assertEqual(backend.calls, 0)
        self.assertEqual(control.status()["operation"], "idle")
        result = sanitized_status({"supported": True, "restart_available": True, "state": "ready",
                                   "operation": "idle", "message": "PRIVATE", "token": "PRIVATE"})
        self.assertFalse(result["restart_available"])
        self.assertNotIn("PRIVATE", str(result))
        self.assertEqual(set(result), {"supported", "restart_available", "state", "operation", "message"})

    def test_local_fixture_linux_and_unverified_profile_are_unsupported(self):
        settings = Settings(data_dir=Path("synthetic-data"), model_cache_dir=Path("synthetic-models"))
        with mock.patch("server.model_control.WindowsModelBackend") as backend:
            self.assertFalse(create_model_control(settings).status()["supported"])
            with mock.patch("server.model_control.os.name", "posix"):
                self.assertFalse(create_model_control(settings, enabled=True).status()["supported"])
            backend.assert_not_called()
        with mock.patch.dict(os.environ, {"STT_ENV_FILE": "unverified-profile"}):
            self.assertFalse(create_model_control(settings, enabled=True).status()["supported"])


class WindowsRecoveryGuardTests(unittest.TestCase):
    def backend(self):
        backend = object.__new__(WindowsModelBackend)
        backend.controller = mock.Mock()
        backend.port = 18775
        return backend

    def test_execution_state_and_record_changes_never_reach_stop_or_start(self):
        for changed in ({"state": "ready", "record": {"instance": "old"}},
                        {"state": "error", "record": {"instance": "new"}}):
            backend = self.backend()
            backend.inspect = mock.Mock(return_value=changed)
            with self.assertRaises(ModelControlError):
                backend.restart({"state": "error", "record": {"instance": "old"}}, threading.Event())
            backend.controller.stop.assert_not_called()
            backend.controller.start.assert_not_called()

    def test_error_restart_uses_locked_expected_identity_and_credential_free_model_environment(self):
        backend = self.backend()
        record = {"instance": "old"}
        snapshot = {"state": "error", "record": record}
        backend.inspect = mock.Mock(side_effect=[snapshot, {"state": "ready", "record": {"instance": "new"}}])
        with mock.patch.dict(os.environ, {"MINDLOGIC_API_KEY": "SYNTHETIC-API-SECRET", "CLOVA_SECRET": "SYNTHETIC-API-SECRET"}):
            backend.restart(copy.deepcopy(snapshot), threading.Event())
        backend.controller.stop.assert_called_once_with(timeout=20, expected_record=record, require_error=True)
        kwargs = backend.controller.start.call_args.kwargs
        self.assertIsNone(kwargs["expected_record"])
        self.assertTrue(kwargs["warmup"])
        self.assertFalse(kwargs["wait_ready"])
        self.assertNotIn("SYNTHETIC-API-SECRET", str(kwargs))
        self.assertEqual(kwargs["inherited"]["ASR_MODEL"], "Qwen3-ASR-1.7B")

    def test_offline_restart_never_stops_and_pins_stale_record(self):
        backend = self.backend()
        snapshot = {"state": "offline", "record": {"instance": "dead"}}
        backend.inspect = mock.Mock(side_effect=[snapshot, {"state": "ready", "record": None}])
        backend.restart(snapshot, threading.Event())
        backend.controller.stop.assert_not_called()
        self.assertEqual(backend.controller.start.call_args.kwargs["expected_record"], snapshot["record"])

    def test_wrong_port_and_record_races_fail_closed(self):
        backend = self.backend()
        backend.controller.directory.exists.return_value = True
        backend.controller.record.return_value = {"port": 18765}
        with self.assertRaises(ModelControlError):
            backend.inspect()
        backend.controller.status.assert_not_called()
        backend.controller.record.side_effect = [{"port": 18775, "instance": "old"}, {"port": 18775, "instance": "new"}]
        with self.assertRaises(ModelControlError):
            backend.inspect()

    def controller(self):
        controller = object.__new__(WindowsModelController)
        controller.directory = mock.Mock()
        controller.directory.exists.return_value = True
        controller.lock_file = Path("synthetic-lock")
        controller.record = mock.Mock()
        controller.request = mock.Mock()
        controller.health = mock.Mock(return_value={"model_state": "ready"})
        return controller

    def test_stop_rechecks_error_after_taking_owned_handle_and_never_signals_ready(self):
        controller = self.controller()
        record = {"pid": 1234, "created": "5678", "exe": "synthetic-python"}
        controller.record.return_value = record
        handle = mock.MagicMock()
        handle.__enter__.return_value = handle
        handle.identity.return_value = dict(record)
        with mock.patch("server.win_model_process.process_lock", return_value=nullcontext()), \
             mock.patch("server.win_model_process.ProcessHandle", return_value=handle):
            with self.assertRaises(ModelProcessError):
                controller.stop(expected_record=record, require_error=True)
        controller.request.assert_not_called()
        handle.terminate.assert_not_called()
        controller.health.assert_called_once_with(record)

    def test_reused_pid_or_replaced_record_cannot_be_stopped(self):
        controller = self.controller()
        expected = {"pid": 1234, "created": "5678", "exe": "synthetic-python"}
        controller.record.return_value = {**expected, "created": "new"}
        with mock.patch("server.win_model_process.process_lock", return_value=nullcontext()), \
             mock.patch("server.win_model_process.ProcessHandle") as handle:
            with self.assertRaises(ModelProcessError):
                controller.stop(expected_record=expected, require_error=True)
            handle.assert_not_called()
        controller.record.return_value = expected
        handle = mock.MagicMock()
        handle.__enter__.return_value = handle
        handle.identity.return_value = {**expected, "created": "new"}
        with mock.patch("server.win_model_process.process_lock", return_value=nullcontext()), \
             mock.patch("server.win_model_process.ProcessHandle", return_value=handle):
            with self.assertRaises(ModelProcessError):
                controller.stop(expected_record=expected, require_error=True)
        controller.request.assert_not_called()
        controller.health.assert_not_called()
        handle.terminate.assert_not_called()

    def test_start_refuses_launch_that_appeared_between_stop_and_start(self):
        controller = self.controller()
        controller.record.return_value = {"instance": "another-launch"}
        with mock.patch("server.win_model_process.runtime_path"), \
             mock.patch("server.win_model_process.process_lock", return_value=nullcontext()), \
             mock.patch("server.win_model_process.model_environment") as environment:
            with self.assertRaises(ModelProcessError):
                controller.start(expected_record=None)
            environment.assert_not_called()


class ModelAuditMigrationTests(unittest.TestCase):
    def test_schema26_preserves_all_values_ids_and_deleted_sequence_highwater(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-model-audit-") as temporary:
            database = Database(Path(temporary) / "private" / "classroom.db", ("user-alpha", "user-beta"))
            database.initialize()
            with database.connect() as connection:
                sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='admin_audit'").fetchone()[0]
                connection.execute("DROP TABLE admin_audit")
                connection.execute(sql.replace("'model_restarted',", ""))
                connection.execute("CREATE INDEX admin_audit_recent ON admin_audit(timestamp DESC,id DESC)")
                connection.execute("INSERT INTO admin_audit VALUES(7,'synthetic-time','access_changed','success','service')")
                connection.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='admin_audit'")
                connection.execute("UPDATE users SET password_hash='synthetic-unchanged' WHERE username='user-alpha'")
                connection.execute("INSERT INTO sessions VALUES('synthetic-token-hash','user-alpha',99,1)")
                connection.execute("PRAGMA user_version=25")
                tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                before = {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                          for table in tables}
            database.initialize()
            database.initialize()
            with database.connect() as connection:
                after = {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                         for table in tables}
                # Rebuilding the table can change sqlite_sequence row order only.
                self.assertEqual(sorted(after.pop("sqlite_sequence")), sorted(before.pop("sqlite_sequence")))
                self.assertEqual(after, before)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 26)
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                cursor = connection.execute("INSERT INTO admin_audit(timestamp,action,result,target) "
                                            "VALUES('synthetic-time','model_restarted','accepted','model')")
                self.assertEqual(cursor.lastrowid, 101)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO admin_audit(timestamp,action,result,target) "
                                       "VALUES('synthetic-time','untrusted-action','accepted','model')")
