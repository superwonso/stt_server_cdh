"""Owned native Windows quick tunnel for the production API on 127.0.0.1:8765.

This module never publishes GitHub/Pages configuration or reads service secrets.
Its private runtime-config.json is a verified 24-hour publication candidate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import secrets
import socket
import ssl

from scripts.runtime_config import runtime_config, validate_document, validate_value, parse_timestamp
from .model_process import PROJECT_DIR, ModelProcessError, command_hash
from .platform_files import atomic_write_private, current_user_sid, ensure_private_directory, open_file
from .win_model_launch import OwnedLaunch
from .windows_dns import clear_startup_dns_cache
from .win_model_process import ProcessHandle, process_identity, process_lock, runtime_path

SERVICE_DIR = PROJECT_DIR.parent
DEFAULT_RUNTIME = SERVICE_DIR / 'production' / 'tunnel'
DEFAULT_BINARY = SERVICE_DIR / 'tools' / 'cloudflared' / 'cloudflared.exe'
TARGET = 'http://127.0.0.1:8765'
HEX32 = re.compile(r'[0-9a-f]{32}\Z')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')
DNS_CACHE_RETRY_DELAYS = (10.0, 30.0, 60.0)


START_CODES = frozenset({
    'operation_failed', 'local_health_failed', 'existing_tunnel_unhealthy',
    'startup_no_url', 'startup_timeout', 'process_exited', 'multiple_tunnel_origins',
})
HEALTH_CODES = frozenset({
    'ok', 'dns_lookup_failed', 'tls_failed', 'network_failed', 'health_timeout',
    'invalid_health', 'health_body_too_large', 'health_failed',
})


def safe_health_code(value):
    # Only local classifications and bounded HTTP status numbers may reach UI.
    if isinstance(value, str) and (value in HEALTH_CODES
            or re.fullmatch(r'http_status_[1-5][0-9]{2}', value)):
        return value
    return 'health_failed'


def safe_diagnostic_code(value):
    if isinstance(value, str):
        for prefix in ('local_', 'public_'):
            if value.startswith(prefix) and safe_health_code(value[len(prefix):]) == value[len(prefix):]:
                return value
    return None


class TunnelStartError(ModelProcessError):
    def __init__(self, code, diagnostic=None):
        self.code = code if isinstance(code, str) and code in START_CODES else 'operation_failed'
        self.diagnostic = safe_diagnostic_code(diagnostic)
        super().__init__('Windows tunnel startup failed [' + self.code + '].')


def health_exception_code(error):
    """Inspect bounded exception types, never provider/DNS exception text."""
    import httpx
    pending, seen, kinds = [error], set(), set()
    while pending and len(seen) < 16:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            kinds.add('dns_lookup_failed')
        if isinstance(current, ssl.SSLError):
            kinds.add('tls_failed')
        if isinstance(current, (httpx.TimeoutException, TimeoutError)):
            kinds.add('health_timeout')
        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                pending.append(linked)
    for code in ('dns_lookup_failed', 'tls_failed', 'health_timeout'):
        if code in kinds:
            return code
    return 'network_failed'


def read_private(path, limit=8192):
    fd = open_file(path, os.O_RDONLY, private=True)
    with os.fdopen(fd, 'rb') as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ModelProcessError('Private tunnel state exceeds its allowed size.')
    return data


def quick_url(log):
    """Match a whole origin, never a trusted-looking prefix of another URL."""
    candidates = set()
    for candidate in re.findall(r'https://[^\s\"\'<>|]+', log):
        try:
            state, origin = validate_value(candidate)
        except ValueError:
            continue
        if state == 'online' and candidate == origin:
            candidates.add(origin)
    if len(candidates) > 1:
        raise TunnelStartError('multiple_tunnel_origins')
    return next(iter(candidates), None)


def _health_attempt(origin, deadline):
    import httpx
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 'health_timeout'
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=min(3, remaining)) as client:
            with client.stream('GET', origin + '/health', headers={'Accept-Encoding': 'identity', 'Cache-Control': 'no-cache'}) as response:
                if response.status_code != 200:
                    return safe_health_code('http_status_' + str(response.status_code))
                body = bytearray()
                for piece in response.iter_raw():
                    body.extend(piece)
                    if len(body) > 4096:
                        return 'health_body_too_large'
                    if time.monotonic() > deadline:
                        return 'health_timeout'
                return 'ok' if json.loads(body) == {'status': 'ok'} else 'invalid_health'
    except (httpx.HTTPError, OSError) as error:
        return health_exception_code(error)
    except (ValueError, RecursionError):
        return 'invalid_health'


def check_health_result(origin):
    """Return fixed diagnostics after normal TLS and exact health validation."""
    if origin != TARGET:
        state, normalized = validate_value(origin)
        if state != 'online' or normalized != origin:
            raise ModelProcessError('The public health origin is not canonical.')
    return _health_attempt(origin, time.monotonic() + 5)


def check_health(origin, *, diagnostics=None):
    # Existing status/publication callers retain the bool contract. Startup can
    # collect the exact result of its own probe without a second network call.
    code = check_health_result(origin)
    if diagnostics is not None:
        diagnostics['code'] = code
    return code == 'ok'


def clean_environment(directory):
    # Exclude inherited TUNNEL_*, credentials, proxy settings and service .env.
    home, temporary = directory / 'home', directory / 'tmp'
    for path in (home, temporary):
        ensure_private_directory(path)
    windows = os.environ.get('SystemRoot', r'C:\Windows')
    return {'SystemRoot': windows, 'WINDIR': windows,
            'PATH': str(Path(windows) / 'System32'),
            'USERPROFILE': str(home), 'HOME': str(home),
            'APPDATA': str(home), 'LOCALAPPDATA': str(home),
            'TEMP': str(temporary), 'TMP': str(temporary)}


class WindowsTunnelController:
    def __init__(self, directory=DEFAULT_RUNTIME, *, binary=DEFAULT_BINARY, command_prefix=None):
        self.directory = runtime_path(directory)
        self.binary = Path(binary)
        if (not self.binary.is_absolute() or '..' in self.binary.parts
                or str(self.binary).startswith('\\\\') or self.binary.resolve() != self.binary):
            raise ModelProcessError('Cloudflared must have a local, non-reparse absolute path.')
        self.command_prefix = list(command_prefix or [str(self.binary)])
        self.record_file = self.directory / 'tunnel.pid.json'
        self.config_file = self.directory / 'runtime-config.json'
        self.lock_file = self.directory / 'tunnel-control.lock'
        self._spawned = None

    def command(self, instance):
        if not HEX32.fullmatch(instance):
            raise ModelProcessError('Invalid tunnel launch identity.')
        return [*self.command_prefix, 'tunnel', '--config', str(self.directory / f'cloudflared-{instance}.yml'),
                '--url', TARGET, '--http-host-header', '127.0.0.1:8765',
                '--no-autoupdate', '--metrics', '127.0.0.1:0']

    def read_record(self):
        try:
            value = json.loads(read_private(self.record_file))
        except FileNotFoundError:
            return None
        except ValueError:
            raise ModelProcessError('Invalid tunnel process record.') from None
        expected = {'version', 'pid', 'created', 'exe', 'sid', 'project', 'runtime',
                    'instance', 'command_hash', 'binary_sha256', 'target', 'api_url'}
        try:
            if (not isinstance(value, dict) or set(value) != expected or type(value['version']) is not int
                    or value['version'] != 1 or type(value['pid']) is not int or value['pid'] <= 0
                    or not isinstance(value['created'], str) or not value['created'].isdigit()
                    or value['exe'] != os.path.normcase(os.path.realpath(self.command_prefix[0]))
                    or value['sid'] != current_user_sid() or value['project'] != str(PROJECT_DIR)
                    or value['runtime'] != str(self.directory) or value['target'] != TARGET
                    or not isinstance(value['instance'], str) or not HEX32.fullmatch(value['instance'])
                    or not isinstance(value['binary_sha256'], str) or not HEX64.fullmatch(value['binary_sha256'])
                    or value['command_hash'] != command_hash(self.command(value['instance']))
                    or validate_value(value['api_url']) != ('online', value['api_url'])):
                raise ValueError
        except (TypeError, ValueError, KeyError):
            raise ModelProcessError('Tunnel record does not match this project and executable; preserved.') from None
        return value

    def verify_handle(self, handle, record):
        import psutil
        identity = handle.identity()
        if identity is None:
            return False
        if any(identity[key] != record[key] for key in identity):
            raise ModelProcessError('Tunnel PID belongs to a different process; preserved.')
        try:
            actual = psutil.Process(record['pid']).cmdline()
            expected = self.command(record['instance'])
            if not actual or os.path.normcase(os.path.realpath(actual[0])) != os.path.normcase(os.path.realpath(expected[0])) or actual[1:] != expected[1:]:
                raise ModelProcessError('Live tunnel command does not match this launch; preserved.')
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            raise ModelProcessError('Live tunnel command could not be verified; preserved.') from None
        after = handle.identity()
        if after is None:
            return False
        if after != identity:
            raise ModelProcessError('Tunnel identity changed during verification.')
        return True

    def running(self, record):
        try:
            with ProcessHandle(record['pid']) as handle:
                return self.verify_handle(handle, record)
        except ProcessLookupError:
            return False

    def desired(self):
        try:
            return validate_document(json.loads(read_private(self.config_file, 4096)))
        except FileNotFoundError:
            return None

    def status(self, *, check=False):
        if not self.directory.exists():
            return {'running': False, 'state': 'stopped', 'publication_managed_externally': True}
        runtime_path(self.directory)
        record, document = self.read_record(), self.desired()
        if record is None:
            if document is not None and document['state'] != 'offline':
                raise ModelProcessError('Online configuration has no owned process record; preserved.')
            return {'running': False, 'state': 'stopped', 'publication_managed_externally': True, 'desired_config': document}
        live = self.running(record)
        if document is None or document['apiUrl'] != record['api_url'] or document['state'] != 'online':
            raise ModelProcessError('Tunnel publication candidate does not match the process record; preserved.')
        result = {'running': live, 'pid': record['pid'], 'state': 'running' if live else 'stopped',
                  'api_url': record['api_url'], 'target': TARGET, 'publication_managed_externally': True,
                  'lease_expired': parse_timestamp(document['expiresAt']) <= datetime.now(timezone.utc),
                  'desired_config': document, 'config_path': str(self.config_file)}
        if check:
            result['healthy'] = bool(live and check_health(TARGET) and check_health(record['api_url']))
        return result

    def _retire(self, record):
        if self.read_record() != record or self.running(record):
            raise ModelProcessError('Tunnel state changed or is still running; preserved.')
        atomic_write_private(self.config_file, json.dumps(runtime_config('OFFLINE')).encode())
        self.record_file.unlink()

    def _binary_digest(self):
        digest = hashlib.sha256()
        fd = open_file(self.binary, os.O_RDONLY)
        with os.fdopen(fd, 'rb') as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()

    def start(self, *, timeout=90):
        if not 1 <= timeout <= 300:
            raise ModelProcessError('Tunnel timeout must be 1 to 300 seconds.')
        runtime_path(self.directory, create=True)
        last_failure = None

        def probe(origin):
            nonlocal last_failure
            diagnostics = {}
            healthy = check_health(origin, diagnostics=diagnostics)
            if not healthy:
                scope = 'local_' if origin == TARGET else 'public_'
                last_failure = scope + safe_health_code(diagnostics.get('code'))
            return healthy

        with process_lock(self.lock_file):
            record = self.read_record()
            if record is not None:
                if self.running(record):
                    self.status()
                    if not probe(TARGET) or not probe(record['api_url']):
                        raise TunnelStartError('existing_tunnel_unhealthy', last_failure)
                    atomic_write_private(self.config_file, json.dumps(runtime_config(record['api_url'])).encode())
                    return self.status()
                self._retire(record)
            self.status()
            if not probe(TARGET):
                raise TunnelStartError('local_health_failed', last_failure)
            digest = self._binary_digest()
            instance = secrets.token_hex(16)
            log_file = self.directory / f'cloudflared-{instance}.log'
            # An explicit empty configuration prevents loading ~/.cloudflared credentials/routes.
            atomic_write_private(self.directory / f'cloudflared-{instance}.yml', b'{}\n')
            environment = clean_environment(self.directory)
            fd = open_file(log_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, private=True)
            registered = None
            try:
                try:
                    with OwnedLaunch(self.command(instance), cwd=PROJECT_DIR, env=environment, stdout=fd) as launch:
                        identity = process_identity(launch.process.pid)
                        if identity is None:
                            raise TunnelStartError('process_exited')
                        deadline = time.monotonic() + timeout
                        saw_url = False
                        dns_cache_clear_attempts = 0
                        first_dns_failure_at = None
                        while time.monotonic() < deadline:
                            if launch.process.poll() is not None:
                                raise TunnelStartError('process_exited', last_failure)
                            url = quick_url(read_private(log_file, 2 * 1024 * 1024).decode('utf-8', errors='replace'))
                            if url:
                                saw_url = True
                            public_healthy = False
                            if url and time.monotonic() < deadline:
                                public_healthy = probe(url)
                                if (not public_healthy and os.name == 'nt'
                                        and last_failure == 'public_dns_lookup_failed'):
                                    observed = time.monotonic()
                                    if first_dns_failure_at is None:
                                        first_dns_failure_at = observed
                                    # Retry only at bounded offsets from the first
                                    # genuine DNS failure while ordinary HTTPS
                                    # probes continue. Existing tunnels never enter
                                    # here; no blocking grace-period sleep is used.
                                    if (dns_cache_clear_attempts < len(DNS_CACHE_RETRY_DELAYS)
                                            and observed - first_dns_failure_at >= DNS_CACHE_RETRY_DELAYS[dns_cache_clear_attempts]):
                                        # Consume the attempt before calling even
                                        # when the helper fails or times out.
                                        dns_cache_clear_attempts += 1
                                        budget = min(2.0, deadline - observed)
                                        if budget > 0:
                                            clear_startup_dns_cache(timeout=budget)
                                        # Cache clearing is never readiness proof.
                                        # The next loop must verify normal TLS and
                                        # exact health JSON from the same origin.
                            if public_healthy and probe(TARGET):
                                registered = {'version': 1, **identity, 'sid': current_user_sid(),
                                    'project': str(PROJECT_DIR), 'runtime': str(self.directory), 'instance': instance,
                                    'command_hash': command_hash(self.command(instance)), 'binary_sha256': digest,
                                    'target': TARGET, 'api_url': url}
                                with ProcessHandle(identity['pid']) as handle:
                                    if not self.verify_handle(handle, registered):
                                        raise TunnelStartError('process_exited', last_failure)
                                atomic_write_private(self.record_file, json.dumps(registered).encode())
                                atomic_write_private(self.config_file, json.dumps(runtime_config(url)).encode())
                                launch.commit(registered)
                                self._spawned = launch.process
                                break
                            time.sleep(.2)
                        else:
                            raise TunnelStartError('startup_timeout' if saw_url else 'startup_no_url', last_failure)
                except BaseException:
                    # The armed job has already stopped only the failed launch.
                    # If registration was partially written, retire only that exact record.
                    if registered is not None and self.read_record() == registered and not self.running(registered):
                        self._retire(registered)
                    raise
            finally:
                os.close(fd)
            return self.status()

    def stop(self, *, timeout=10):
        if not 1 <= timeout <= 60:
            raise ModelProcessError('Stop timeout must be 1 to 60 seconds.')
        if not self.directory.exists():
            return self.status()
        runtime_path(self.directory)
        with process_lock(self.lock_file):
            record = self.read_record()
            if record is None:
                return self.status()
            try:
                with ProcessHandle(record['pid'], terminate=True) as handle:
                    if self.verify_handle(handle, record):
                        handle.terminate(record)
                        if not handle.wait(timeout):
                            raise ModelProcessError('Tunnel exit is not confirmed; ownership files preserved.')
            except ProcessLookupError:
                pass
            self._retire(record)
            if self._spawned is not None:
                self._spawned.wait(timeout=3)
                self._spawned = None
            return self.status()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'status', 'stop'])
    parser.add_argument('--runtime', type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument('--cloudflared', type=Path, default=DEFAULT_BINARY)
    parser.add_argument('--timeout', type=int, default=90)
    parser.add_argument('--check-health', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if os.name != 'nt':
        parser.error('Native Windows is required.')
    try:
        controller = WindowsTunnelController(args.runtime, binary=args.cloudflared)
        if args.action == 'start':
            result = controller.start(timeout=args.timeout)
        elif args.action == 'stop':
            result = controller.stop(timeout=min(args.timeout, 60))
        else:
            result = controller.status(check=args.check_health)
        if args.json:
            print(json.dumps(result))
        else:
            print('Windows tunnel: ' + result['state'])
            if result.get('api_url'):
                print(result['api_url'])
            print('GitHub/Pages publication was not performed.')
        return 0
    except TunnelStartError as error:
        # Even typed errors are normalized again here; never reflect str(error).
        code = error.code if isinstance(error.code, str) and error.code in START_CODES else 'operation_failed'
        diagnostic = safe_diagnostic_code(error.diagnostic)
        codes = code + ('; ' + diagnostic if diagnostic else '')
        print('Windows tunnel operation failed [' + codes + ']. Private state and logs were preserved; unrelated processes and publication were not changed.', file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError):
        print('Windows tunnel operation failed [operation_failed]. Private state and logs were preserved; unrelated processes and publication were not changed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
