"""Explicitly enabled renewal of an already-published owned Windows tunnel.

Uses the existing LeaseRenewer thread, timing, status and retry policy. This
adapter cannot launch/stop a tunnel or publish its first public connection.
Only the native publisher receives cancellation; API credentials never leave
this process or enter the publisher's child environment.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

from scripts.runtime_config import parse_timestamp, validate_document
from .lease_renewal import DesiredLease, LeaseRenewer, LeaseStateError
from .model_process import PROJECT_DIR
from .platform_files import validate_private_path

SERVICE_ROOT = PROJECT_DIR.parent / "production"
DATA_DIR = SERVICE_ROOT / "data"
ENV_FILE = SERVICE_ROOT / "config" / "service.env"
NATIVE_RENEW_COMMAND = ("native-windows-pages-renew",)
OWNER_KEYS = ("instance", "pid", "created", "exe")


class WindowsLeaseAdapter:
    def __init__(self, *, api=None, tunnel=None, publisher=None):
        if api is None:
            from .windows_service import ServiceAPIController
            api = ServiceAPIController()
        if tunnel is None:
            from .windows_tunnel import WindowsTunnelController
            tunnel = WindowsTunnelController()
        self.api, self.tunnel = api, tunnel
        self._publisher = publisher

    @property
    def publisher(self):
        if self._publisher is None:
            from .windows_publication import WindowsPublisher
            self._publisher = WindowsPublisher()
        return self._publisher

    def renewal_processes_owned(self):
        # This adapter is an API worker, never a second scheduler launched from
        # another shell. The publisher repeats ownership under the tunnel lock.
        record = self.api.record()
        if record is None or record["pid"] != os.getpid() or not self.api.matching(record):
            return False
        tunnel_record = self.tunnel.read_record()
        return tunnel_record is not None and self.tunnel.running(tunnel_record)

    def read_lease(self, data_dir, *, now):
        if Path(data_dir) != DATA_DIR:
            raise LeaseStateError("unsafe_profile")
        for path in (SERVICE_ROOT, DATA_DIR, ENV_FILE.parent):
            validate_private_path(path, directory=True)
        validate_private_path(ENV_FILE)
        desired = self.publisher.read_desired()
        if desired is None:
            raise LeaseStateError("desired_missing")
        current = datetime.fromtimestamp(now, timezone.utc)
        desired = validate_document(desired, now=current)
        if desired["state"] != "online":
            raise LeaseStateError("offline")
        confirmation = self.publisher.read_confirmation()
        if confirmation is None:
            raise LeaseStateError("publication_unconfirmed")
        confirmed = validate_document(confirmation["config"], now=current)
        tunnel_record = self.tunnel.read_record()
        candidate = self.tunnel.desired()
        if (tunnel_record is None or candidate is None or candidate["state"] != "online"
                or desired["apiUrl"] != tunnel_record["api_url"]
                or candidate["apiUrl"] != desired["apiUrl"]):
            raise LeaseStateError("url_changed")
        if (confirmed["state"] != "online" or confirmed["apiUrl"] != desired["apiUrl"]
                or confirmation.get("owner") != {key: tunnel_record[key] for key in OWNER_KEYS}
                or parse_timestamp(desired["publishedAt"]) < parse_timestamp(confirmed["publishedAt"])):
            raise LeaseStateError("publication_unconfirmed")
        if not self.renewal_processes_owned():
            raise LeaseStateError("process_not_owned")
        # A previous process may have dispatched a new desired document and
        # crashed before CDN confirmation. Scheduling from the last confirmed
        # lease prevents that unconfirmed local timestamp delaying a retry.
        return DesiredLease(confirmed["state"], confirmed["apiUrl"],
                            parse_timestamp(confirmed["publishedAt"]).timestamp(),
                            parse_timestamp(confirmed["expiresAt"]).timestamp())

    def renewal_command(self):
        return NATIVE_RENEW_COMMAND

    def _safe_environment(self):
        return {}  # No shell/child is launched by this adapter.

    def run(self, command, cwd, environment, timeout, cancelled):
        if command != NATIVE_RENEW_COMMAND or cancelled.is_set():
            return 130
        try:
            # The publisher rechecks the existing online confirmation, current
            # owned processes and both health endpoints under its lifecycle lock.
            self.publisher.renew(wait_timeout=min(timeout, 180), cancel_event=cancelled)
            return 130 if cancelled.is_set() else 0
        except Exception:
            return 130 if cancelled.is_set() else 1


def create_windows_lease_renewer(*, data_dir, enabled=True, port=8765):
    # No private files, process state or publisher are read/created unless the
    # exact production profile explicitly opted in. Tests/local remain inert.
    opted_in = os.getenv("AUTO_RENEW_API_URL", "0").strip().lower() in {"1", "true", "yes"}
    safe = (enabled and opted_in and port == 8765 and Path(data_dir) == DATA_DIR
            and os.getenv("STT_ENV_FILE", "") == str(ENV_FILE))
    if not safe:
        return LeaseRenewer(data_dir=data_dir, enabled=False)
    adapter = WindowsLeaseAdapter()
    return LeaseRenewer(data_dir=data_dir, enabled=True, controller=adapter,
                        runner=adapter.run, lease_reader=adapter.read_lease)