"""Real Windows startup-job checks, without GPU models or private data fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from server.model_process import ModelProcessError
from server.win_model_launch import OwnedLaunch
from server.win_model_process import ProcessHandle, process_identity

ROOT = Path(__file__).resolve().parents[1]
CHILD = ('import json,os,time; from server.win_model_process import process_identity; '
         'print(json.dumps(process_identity(os.getpid())),flush=True); time.sleep(30)')


@unittest.skipUnless(os.name == "nt", "Windows job objects")
class WindowsStartupJobTests(unittest.TestCase):
    def launch(self):
        return OwnedLaunch([sys.executable, "-c", CHILD], cwd=ROOT, env=os.environ.copy(), stdout=subprocess.PIPE)

    def test_failed_registration_stops_venv_launcher_and_real_child(self):
        with self.launch() as launch:
            registered = json.loads(launch.process.stdout.readline())
            self.assertIsNotNone(process_identity(launch.process.pid))
        self.assertIsNotNone(launch.process.poll())
        self.assertIsNone(process_identity(registered["pid"]))
        launch.process.stdout.close()
        launch.process.stderr.close()

    def test_verified_registration_detaches_for_independent_lifetime(self):
        registered = None
        try:
            with self.launch() as launch:
                registered = json.loads(launch.process.stdout.readline())
                launch.commit(registered)
            self.assertIsNone(launch.process.poll())
            self.assertIsNotNone(process_identity(registered["pid"]))
        finally:
            if registered is not None:
                with ProcessHandle(registered["pid"], terminate=True) as handle:
                    handle.terminate(registered)
                    self.assertTrue(handle.wait(3))
                launch.process.wait(timeout=3)
                launch.process.stdout.close()
                launch.process.stderr.close()

    def test_foreign_process_cannot_be_adopted_or_killed_on_start_failure(self):
        other = subprocess.Popen([process_identity(os.getpid())["exe"], "-c", "import time; time.sleep(30)"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with self.assertRaises(ModelProcessError):
                with self.launch() as launch:
                    registered = json.loads(launch.process.stdout.readline())
                    launch.commit(process_identity(other.pid))
            self.assertIsNone(other.poll())
            self.assertIsNone(process_identity(registered["pid"]))
            launch.process.stdout.close()
            launch.process.stderr.close()
        finally:
            other.terminate()
            other.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
