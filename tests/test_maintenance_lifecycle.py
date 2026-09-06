"""Maintenance integration checks using temporary databases and inert workers.

No production configuration, processes, credentials, network or model is used.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import app as app_module
from server.lease_renewal import LeaseRenewer
from server.recovery_backup import BackupScheduler, RecoveryBackupManager
from server.settings import Settings
from server.summary_service import SummaryService
from server.translation_service import TranslationService


class FakeEngine:
    configured = True

    def __init__(self):
        self.close = mock.Mock()

    def status(self):
        return {"model_state": "ready", "engine": "synthetic", "model": "fixture", "device": "cpu"}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class ServiceProbe:
    configured = False
    enabled = False

    def __init__(self, name, events, clock, *, stuck=False, fail_start=False):
        self.name, self.events, self.clock = name, events, clock
        self.stuck, self.fail_start = stuck, fail_start
        self.signalled = False

    def install(self, *args, **kwargs):
        pass

    def recover(self):
        self.events.append(("recover", self.name))

    def start(self):
        self.events.append(("start", self.name))
        if self.fail_start:
            raise RuntimeError("synthetic worker start failure")

    def request_shutdown(self):
        self.signalled = True
        self.events.append(("signal", self.name))

    def stop(self, timeout):
        self.events.append(("join", self.name, timeout))
        if self.stuck:
            self.clock.now += timeout
        return not self.stuck

    def close(self):
        self.events.append(("close", self.name))

    def admin_snapshot(self):
        return {"configured": False}

    def status(self):
        return {"enabled": False, "state": "disabled"}


class MaintenanceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-maintenance-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.settings = Settings(data_dir=self.directory / "data", model_cache_dir=self.directory / "models",
                                 accounts=("user-alpha", "user-beta"), admin_username="user-alpha", device="cpu")

    @contextmanager
    def factory(self, *, production=True, stuck=False, fail_start=None, auto_renew="1"):
        events, threads, clock = [], [], FakeClock()
        services = {name: ServiceProbe(name, events, clock, stuck=stuck, fail_start=fail_start == name)
                    for name in ("summary", "translation", "archive", "lease", "backup")}
        engines = {name: FakeEngine() for name in ("local", "clova", "correction", "summary", "translation")}

        class InertThread:
            def __init__(thread, *, target, name, daemon):
                self.assertTrue(daemon)
                thread.name, thread.target, thread.alive = name, target, False
                thread.closure = inspect.getclosurevars(target).nonlocals
                threads.append(thread)

            def start(thread):
                thread.alive = True
                events.append(("start", thread.name))

            def is_alive(thread):
                return thread.alive

            def join(thread, timeout):
                # These assertions also cover the import/correction events,
                # whose request helpers are closures rather than services.
                self.assertTrue(thread.closure["import_worker_shutdown" if "import" in thread.name
                                               else "correction_worker_shutdown"].is_set())
                for name in ("summary", "translation", "archive", "lease") + (("backup",) if production else ()):
                    self.assertTrue(services[name].signalled, f"{name} was not signalled before the first join")
                events.append(("join", thread.name, timeout))
                if stuck:
                    clock.now += timeout
                else:
                    thread.alive = False

        with ExitStack() as stack:
            from_env = stack.enter_context(mock.patch.object(app_module.Settings, "from_env", return_value=self.settings))
            stack.enter_context(mock.patch.dict("os.environ", {"AUTO_RENEW_API_URL": auto_renew}))
            stack.enter_context(mock.patch.object(app_module.threading, "Thread", InertThread))
            stack.enter_context(mock.patch.object(app_module, "time", SimpleNamespace(monotonic=clock.monotonic,
                                                                                     time=time.time)))
            for constructor, name in (("SummaryService", "summary"), ("TranslationService", "translation"),
                                      ("DriveArchiveManager", "archive")):
                stack.enter_context(mock.patch.object(app_module, constructor, return_value=services[name]))
            lease_factory = stack.enter_context(mock.patch.object(app_module, "create_lease_renewer",
                                                                 return_value=services["lease"]))
            manager_factory = stack.enter_context(mock.patch.object(app_module, "RecoveryBackupManager"))
            scheduler_factory = stack.enter_context(mock.patch.object(app_module, "BackupScheduler",
                                                                      return_value=services["backup"]))
            app = app_module.create_app(None if production else self.settings, transcriber=engines["local"],
                                        clova_transcriber=engines["clova"], postprocessor=engines["correction"],
                                        summarizer=engines["summary"], translator=engines["translation"],
                                        tunnel_status=lambda: {"state": "offline"},
                                        tunnel_restart=lambda: {"state": "offline"})
            yield SimpleNamespace(app=app, events=events, threads=threads, services=services, engines=engines,
                                  clock=clock, from_env=from_env, lease_factory=lease_factory,
                                  manager_factory=manager_factory, scheduler_factory=scheduler_factory)

    def run_lifespan(self, app, *, fail_body=False):
        async def run():
            async with app.router.lifespan_context(app):
                if fail_body:
                    raise ValueError("synthetic application failure")
        asyncio.run(run())

    def test_explicit_settings_never_construct_backup_or_enable_address_publication(self):
        with self.factory(production=False, auto_renew="true") as fixture:
            fixture.from_env.assert_not_called()
            fixture.manager_factory.assert_not_called()
            fixture.scheduler_factory.assert_not_called()
            fixture.lease_factory.assert_called_once_with(data_dir=self.settings.data_dir, enabled=False)
            self.assertIsNone(fixture.app.state.backup_scheduler)
            self.run_lifespan(fixture.app)
            self.assertNotIn(("start", "backup"), fixture.events)

    def test_production_factory_registers_one_scheduler_and_one_lease_worker(self):
        with self.factory() as fixture:
            fixture.from_env.assert_called_once_with()
            fixture.manager_factory.assert_called_once_with(self.settings)
            fixture.scheduler_factory.assert_called_once_with(fixture.manager_factory.return_value)
            fixture.lease_factory.assert_called_once_with(data_dir=self.settings.data_dir, enabled=True)
            self.run_lifespan(fixture.app)
            self.assertEqual(fixture.events.count(("start", "backup")), 1)
            self.assertEqual(fixture.events.count(("start", "lease")), 1)
            self.assertTrue(all(not thread.alive for thread in fixture.threads))

    def test_explicit_production_opt_out_disables_lease_but_keeps_backup_independent(self):
        with self.factory(auto_renew="0") as fixture:
            fixture.lease_factory.assert_called_once_with(data_dir=self.settings.data_dir, enabled=False)
            fixture.manager_factory.assert_called_once_with(self.settings)

    def test_all_workers_are_signalled_before_any_join_and_idle_resources_close(self):
        with self.factory() as fixture:
            self.run_lifespan(fixture.app)
            first_join = next(index for index, event in enumerate(fixture.events) if event[0] == "join")
            for name in fixture.services:
                self.assertLess(fixture.events.index(("signal", name)), first_join)
            fixture.engines["clova"].close.assert_called_once_with()
            fixture.engines["correction"].close.assert_called_once_with()
            self.assertIn(("close", "archive"), fixture.events)

    def test_stuck_workers_share_eighteen_seconds_instead_of_adding_join_budgets(self):
        with self.factory(stuck=True) as fixture:
            with self.assertLogs("classroom", level="WARNING"):
                self.run_lifespan(fixture.app)
            waits = {event[1]: event[2] for event in fixture.events if event[0] == "join"}
            self.assertEqual(waits, {"transcript-correction-worker": 8, "summary": 5, "translation": 5,
                                     "recording-import-worker": 0, "archive": 0, "lease": 0, "backup": 0})
            self.assertEqual(fixture.clock.now, 1018)
            fixture.engines["correction"].close.assert_not_called()
            self.assertNotIn(("close", "archive"), fixture.events)

    def test_worker_start_failure_cleans_already_started_workers(self):
        for failing in ("summary", "translation", "archive", "lease", "backup"):
            with self.subTest(failing=failing), self.factory(fail_start=failing) as fixture:
                with self.assertRaisesRegex(RuntimeError, "synthetic worker start failure"):
                    self.run_lifespan(fixture.app)
                self.assertTrue(all(service.signalled for service in fixture.services.values()))
                self.assertTrue(all(not thread.alive for thread in fixture.threads))
                fixture.engines["clova"].close.assert_called_once_with()

    def test_application_body_failure_still_stops_all_maintenance(self):
        with self.factory() as fixture:
            with self.assertRaisesRegex(ValueError, "synthetic application failure"):
                self.run_lifespan(fixture.app, fail_body=True)
            self.assertTrue(all(service.signalled for service in fixture.services.values()))
            self.assertTrue(all(not thread.alive for thread in fixture.threads))

    def test_real_summary_and_translation_start_failure_preserves_cleanup_continuation(self):
        # Real service and Thread objects, with only native startup faulted:
        # the failed worker must not mask the original exception in finally.
        for service_class in (SummaryService, TranslationService):
            with self.subTest(service=service_class.__name__):
                engine = FakeEngine()
                service = service_class(self.settings, None, engine, None)
                later_cleanup = mock.Mock()
                failure = RuntimeError("synthetic native thread start failure")
                with mock.patch("threading.Thread.start", side_effect=failure):
                    with self.assertRaises(RuntimeError) as captured:
                        try:
                            service.start()
                        finally:
                            self.assertTrue(service.stop(timeout=0))
                            later_cleanup()
                self.assertIs(captured.exception, failure)
                self.assertIsNone(service.thread)
                self.assertTrue(service.shutdown.is_set())
                engine.close.assert_called_once_with()
                later_cleanup.assert_called_once_with()

    def test_unconfigured_status_is_safe_and_does_not_create_private_backup_files(self):
        # The real managers are used for status only, confined to this fixture.
        manager = RecoveryBackupManager(self.settings, project_dir=self.directory)
        scheduler = BackupScheduler(manager)
        lease = LeaseRenewer(project_root=self.directory, data_dir=self.settings.data_dir, enabled=False)
        with self.factory(production=False) as fixture:
            fixture.services["lease"].status = lease.status
            endpoint = next(route.endpoint for route in fixture.app.routes if route.path == "/admin/overview")
            value = endpoint(user={"username": "user-alpha"})
            self.assertEqual(value["backup"], {"configured": False, "enabled": False, "running": False})
            self.assertEqual(value["api_address"]["state"], "disabled")
            self.assertEqual(scheduler.status(), value["backup"])
            public = json.dumps({"backup": value["backup"], "api_address": value["api_address"]})
            for private in (str(self.directory), "user-alpha", "user-beta", "identity", "recipient", "https://"):
                self.assertNotIn(private, public)
            with mock.patch("server.recovery_backup.threading.Thread") as thread:
                self.assertFalse(scheduler.start())
                lease.start()
                thread.assert_not_called()
            self.assertFalse((self.settings.data_dir / "backup").exists())


if __name__ == "__main__":
    unittest.main()
