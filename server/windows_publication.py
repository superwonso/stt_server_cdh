"""Publish only the fixed repository's public Pages configuration from Windows.

No authentication, secrets, service environment, or database is handled here.
GitHub CLI diagnostics are captured and discarded; errors expose only the stage.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from scripts.runtime_config import matches_config, parse_timestamp, runtime_config, validate_document
from .model_process import PROJECT_DIR, ModelProcessError
from .platform_files import atomic_write_private
from .win_model_process import process_lock, runtime_path
from .windows_tunnel import WindowsTunnelController, read_private, SERVICE_DIR

REPOSITORY = 'superwonso/stt_server_cdh'
WORKFLOW = 'pages.yml'
VARIABLE = 'CLASSROOM_API_CONFIG'
CONFIG_URL = 'https://superwonso.github.io/stt_server_cdh/config.json'
DEFAULT_RUNTIME = SERVICE_DIR / 'production' / 'publication'
DEFAULT_GH = SERVICE_DIR / 'tools' / 'gh' / 'gh.exe'
OWNER_KEYS = ('instance', 'pid', 'created', 'exe')

class PublicationError(ModelProcessError):
    pass

class PublicationCancelled(PublicationError):
    pass

def cancelled(event):
    if event is not None and event.is_set():
        raise PublicationCancelled('Publication was cancelled.')

def roaming_appdata():
    """Resolve the current user's standard gh profile without reading its files.

    SHGetKnownFolderPath handles redirected profiles and a filtered API process
    environment. The caller is the current user; no impersonation or elevation.
    """
    import ctypes
    from ctypes import wintypes
    import uuid
    class GUID(ctypes.Structure):
        _fields_ = [('first', wintypes.DWORD), ('second', wintypes.WORD),
                    ('third', wintypes.WORD), ('last', ctypes.c_ubyte * 8)]
    identifier = GUID.from_buffer_copy(uuid.UUID('3eb685db-65f9-4cf6-a03a-e3ef65729f3d').bytes_le)
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    ole = ctypes.WinDLL('ole32', use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    pointer = ctypes.c_void_p()
    try:
        if shell.SHGetKnownFolderPath(ctypes.byref(identifier), 0x4000, None, ctypes.byref(pointer)) != 0 or not pointer.value:
            raise PublicationError('The current Windows GitHub CLI profile path could not be resolved.')
        return ctypes.wstring_at(pointer)
    finally:
        if pointer.value:
            ole.CoTaskMemFree(pointer)

def validate_wait_timeout(timeout):
    if not 1 <= timeout <= 600:
        raise PublicationError('Pages timeout must be 1 to 600 seconds.')

def gh_environment():
    # gh itself reads the normal user's existing login. Never inherit API tokens,
    # proxy/debug/credential commands, service secrets, or an alternate GH host.
    result = {key: os.environ[key] for key in
              ('SystemRoot', 'WINDIR', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
               'HOMEDRIVE', 'HOMEPATH', 'TEMP', 'TMP') if key in os.environ}
    if os.name == 'nt':
        result['APPDATA'] = roaming_appdata()
    windows = result.get('SystemRoot', r'C:\Windows')
    result.update({'SystemRoot': windows, 'WINDIR': windows,
                   'PATH': str(Path(windows) / 'System32'), 'GH_HOST': 'github.com',
                   'GH_PROMPT_DISABLED': '1', 'GH_PAGER': '', 'PAGER': '',
                   'NO_COLOR': '1', 'GH_NO_UPDATE_NOTIFIER': '1', 'GH_TELEMETRY': '0'})
    return result

def run_gh(binary, arguments, payload=None, *, timeout=30, cancel_event=None):
    """Bound native gh lifetime; kill only the retained Popen handle on cancel."""
    cancelled(cancel_event)
    if not 1 <= timeout <= 120:
        raise PublicationError('GitHub timeout must be 1 to 120 seconds.')
    command = [str(binary), *arguments]
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    process = subprocess.Popen(command, cwd=PROJECT_DIR, env=gh_environment(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=flags, close_fds=True)
    deadline = time.monotonic() + timeout
    first = True
    try:
        while True:
            cancelled(cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PublicationError('GitHub command exceeded its deadline.')
            try:
                # Captured diagnostics are deliberately never emitted or saved.
                process.communicate(input=payload if first else None, timeout=min(.2, remaining))
                break
            except subprocess.TimeoutExpired:
                first = False
        cancelled(cancel_event)
        if process.returncode != 0:
            raise PublicationError('GitHub command failed; check the existing device login and repository permissions.')
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            raise PublicationError('Owned GitHub command exit could not be confirmed.') from None
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

def fetch_config(*, timeout=3):
    import httpx
    try:
        deadline = time.monotonic() + min(timeout, 8)
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=min(timeout, 3)) as client:
            with client.stream('GET', CONFIG_URL, params={'check': secrets.token_hex(12)},
                    headers={'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'}) as response:
                if response.status_code != 200:
                    return None
                body = bytearray()
                for chunk in response.iter_raw():
                    body.extend(chunk)
                    if len(body) > 4096 or time.monotonic() > deadline:
                        return None
                return validate_document(json.loads(body))
    except (httpx.HTTPError, OSError, TypeError, ValueError):
        return None

def public_document(document):
    value = validate_document(document)
    if value['state'] == 'online' and parse_timestamp(value['expiresAt']) <= datetime.now(timezone.utc):
        raise PublicationError('An expired online lease cannot be published or confirmed.')
    return value

class WindowsPublisher:
    def __init__(self, directory=DEFAULT_RUNTIME, *, gh=DEFAULT_GH, tunnel=None):
        self.directory = runtime_path(directory)
        self.gh = Path(gh)
        if (not self.gh.is_absolute() or '..' in self.gh.parts
                or str(self.gh).startswith('\\\\') or self.gh.resolve() != self.gh):
            raise PublicationError('GitHub CLI must have a local non-reparse absolute path.')
        self.tunnel = tunnel if tunnel is not None else WindowsTunnelController()
        self.desired_file = self.directory / 'pages-desired-config.json'
        self.confirmed_file = self.directory / 'pages-confirmed-config.json'
        self.lock_file = self.directory / 'pages-publish.lock'

    def read_desired(self):
        try:
            return validate_document(json.loads(read_private(self.desired_file, 4096)))
        except FileNotFoundError:
            return None

    def read_confirmation(self):
        try:
            value = json.loads(read_private(self.confirmed_file, 8192))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or set(value) != {'version', 'config', 'owner'} or type(value['version']) is not int or value['version'] != 1:
            raise PublicationError('Invalid Pages confirmation record.')
        config = validate_document(value['config'])
        owner = value['owner']
        if config['state'] == 'offline':
            if owner is not None:
                raise PublicationError('Offline confirmation cannot own a tunnel.')
        elif (not isinstance(owner, dict) or set(owner) != set(OWNER_KEYS)
              or type(owner['pid']) is not int or owner['pid'] <= 0
              or not isinstance(owner['instance'], str) or len(owner['instance']) != 32
              or any(c not in '0123456789abcdef' for c in owner['instance'])
              or not isinstance(owner['created'], str) or not owner['created'].isdigit()
              or not isinstance(owner['exe'], str) or not Path(owner['exe']).is_absolute()):
            raise PublicationError('Invalid confirmed tunnel ownership.')
        return value

    def read_confirmed(self):
        value = self.read_confirmation()
        return value['config'] if value is not None else None

    def _owner(self):
        record = self.tunnel.read_record()
        if record is None:
            raise PublicationError('No owned tunnel is registered.')
        return {key: record[key] for key in OWNER_KEYS}

    def _ready_owner(self, url):
        status = self.tunnel.status(check=True)
        if not status.get('running') or not status.get('healthy') or status.get('api_url') != url:
            raise PublicationError('This owned tunnel and the production API are not healthy.')
        return self._owner()

    def _request(self, document, *, cancel_event=None):
        runtime_path(self.directory, create=True)
        with process_lock(self.lock_file):
            document = public_document(document)
            previous = self.read_desired()
            if previous is not None and parse_timestamp(document['publishedAt']) < parse_timestamp(previous['publishedAt']):
                raise PublicationError('An older publication candidate cannot replace a newer requested state.')
            cancelled(cancel_event)
            payload = json.dumps(document, separators=(',', ':')).encode('utf-8')
            try:
                run_gh(self.gh, ['variable', 'set', VARIABLE, '--repo', REPOSITORY], payload,
                       cancel_event=cancel_event)
            except PublicationError as error:
                raise PublicationError('GitHub public variable update did not complete.') from error
            try:
                run_gh(self.gh, ['workflow', 'run', WORKFLOW, '--repo', REPOSITORY, '--ref', 'main'],
                       cancel_event=cancel_event)
            except PublicationError as error:
                # The variable may already be updated. Never claim confirmed delivery.
                raise PublicationError('Pages dispatch did not complete; the public variable may already be updated.') from error
            atomic_write_private(self.desired_file, payload)
        return document

    def _assert_current(self, document):
        if self.read_desired() != document:
            raise PublicationError('A newer publication request superseded this wait.')

    def _confirm(self, document, owner):
        # Order always matches publish/renew: tunnel lock, then publication lock.
        runtime_path(self.tunnel.directory, create=True)
        with process_lock(self.tunnel.lock_file):
            with process_lock(self.lock_file):
                self._assert_current(document)
                public_document(document)
                if document['state'] == 'online':
                    if owner is None or self._owner() != owner:
                        raise PublicationError('Tunnel ownership changed while waiting for Pages.')
                    record = self.tunnel.read_record()
                    if record['api_url'] != document['apiUrl'] or not self.tunnel.running(record):
                        raise PublicationError('The requested tunnel stopped before Pages confirmation.')
                confirmation = {'version': 1, 'config': document, 'owner': owner}
                atomic_write_private(self.confirmed_file, json.dumps(confirmation).encode('utf-8'))

    def wait(self, document=None, *, owner=None, wait_timeout=180, cancel_event=None):
        validate_wait_timeout(wait_timeout)
        document = public_document(document if document is not None else self.read_desired())
        if document['state'] == 'online' and owner is None:
            owner = self._ready_owner(document['apiUrl'])
        deadline = time.monotonic() + wait_timeout
        while time.monotonic() < deadline:
            cancelled(cancel_event)
            self._assert_current(document)
            fetched = fetch_config(timeout=min(3, max(.05, deadline-time.monotonic())))
            self._assert_current(document)
            if matches_config(fetched, document):
                self._confirm(document, owner)
                return {'state': 'confirmed', 'config': document}
            remaining = deadline - time.monotonic()
            if remaining > 0:
                if cancel_event is not None:
                    cancel_event.wait(min(2, remaining))
                else:
                    time.sleep(min(2, remaining))
        raise PublicationError('Pages did not serve the exact requested configuration before the deadline.')

    def publish(self, *, config_file=None, offline=False, wait_timeout=180, cancel_event=None):
        validate_wait_timeout(wait_timeout)
        cancelled(cancel_event)
        if offline and config_file is not None:
            raise PublicationError('Choose a candidate configuration or explicit offline state.')
        runtime_path(self.tunnel.directory, create=True)
        with process_lock(self.tunnel.lock_file):
            if offline:
                document, owner = runtime_config('OFFLINE'), None
            else:
                path = Path(config_file) if config_file is not None else self.tunnel.config_file
                document = public_document(json.loads(read_private(path, 4096)))
                if document['state'] != 'online' or document != self.tunnel.desired():
                    raise PublicationError('The candidate must match the current owned tunnel configuration.')
                owner = self._ready_owner(document['apiUrl'])
            self._request(document, cancel_event=cancel_event)
        return self.wait(document, owner=owner, wait_timeout=wait_timeout, cancel_event=cancel_event)

    def renew(self, *, wait_timeout=180, cancel_event=None):
        validate_wait_timeout(wait_timeout)
        cancelled(cancel_event)
        runtime_path(self.tunnel.directory, create=True)
        with process_lock(self.tunnel.lock_file):
            desired, confirmed = self.read_desired(), self.read_confirmation()
            if desired is None or confirmed is None or desired['state'] != 'online' or confirmed['config']['state'] != 'online' or desired['apiUrl'] != confirmed['config']['apiUrl']:
                raise PublicationError('Automatic renewal requires a previously confirmed online publication.')
            owner = self._ready_owner(desired['apiUrl'])
            if confirmed['owner'] != owner:
                raise PublicationError('Automatic renewal cannot adopt a different tunnel launch.')
            document = runtime_config(desired['apiUrl'])
            self._request(document, cancel_event=cancel_event)
        return self.wait(document, owner=owner, wait_timeout=wait_timeout, cancel_event=cancel_event)

    def status(self):
        return {'desired': self.read_desired(), 'confirmed': self.read_confirmation()}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('publish', 'renew', 'wait', 'status'))
    parser.add_argument('--config', type=Path)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--wait-timeout', type=int, default=180)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if os.name != 'nt':
        parser.error('Native Windows is required.')
    if args.action != 'publish' and (args.config is not None or args.offline):
        parser.error('--config and --offline apply only to publish.')
    try:
        publisher = WindowsPublisher()
        if args.action == 'publish':
            result = publisher.publish(config_file=args.config, offline=args.offline, wait_timeout=args.wait_timeout)
        elif args.action in ('renew', 'wait'):
            result = getattr(publisher, args.action)(wait_timeout=args.wait_timeout)
        else:
            result = publisher.status()
        print(json.dumps(result) if args.json else 'Windows Pages publication: ' + result.get('state', 'status read'))
        return 0
    except PublicationError as error:
        print('Pages publication was not confirmed: ' + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, RuntimeError):
        print('Pages publication was not confirmed. Inspect the private desired/confirmed state; GitHub may already have accepted the public update.', file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())