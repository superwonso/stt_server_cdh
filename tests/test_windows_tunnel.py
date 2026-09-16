"""Synthetic-only tunnel lifecycle and security tests; never starts cloudflared."""
from datetime import timedelta
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import socket
import ssl
import time
import unittest
from unittest import mock

import httpx
from server import windows_tunnel as tunnel
from server.platform_files import atomic_write_private, ensure_private_directory
from server.win_model_process import process_identity
from scripts.runtime_config import parse_timestamp, runtime_config


class TunnelParsingTests(unittest.TestCase):
    def test_only_complete_canonical_quick_origin_is_accepted(self):
        url='https://synthetic-owned-test.trycloudflare.com'
        self.assertEqual(tunnel.quick_url('prefix | '+url+' | suffix'),url)
        for invalid in ('https://safe.trycloudflare.com.evil.test','https://safe.trycloudflare.com:443',
                        'https://user@safe.trycloudflare.com','https://safe.trycloudflare.com/path',
                        'https://safe.trycloudflare.com?x=1','https://safe.trycloudflare.com#fragment',
                        'http://safe.trycloudflare.com','https://a.b.trycloudflare.com',
                        'https://-a.trycloudflare.com','https://'+'a'*64+'.trycloudflare.com'):
            with self.subTest(url=invalid): self.assertIsNone(tunnel.quick_url(invalid))
        with self.assertRaises(tunnel.ModelProcessError):
            tunnel.quick_url(url+' https://different.trycloudflare.com')

    def test_health_rejects_redirect_large_body_wrong_shape_and_error(self):
        real_client=httpx.Client
        for response in (httpx.Response(302,headers={'location':'https://evil.test'}),
                         httpx.Response(200,stream=httpx.ByteStream(b'x'*4097)),
                         httpx.Response(200,stream=httpx.ByteStream(b'{"status":"other"}')),httpx.Response(500)):
            requests=[]
            def handler(request): requests.append(request); return response
            def factory(**kwargs):
                self.assertFalse(kwargs['trust_env']); self.assertFalse(kwargs['follow_redirects'])
                return real_client(transport=httpx.MockTransport(handler),**kwargs)
            with mock.patch('httpx.Client',side_effect=factory):
                self.assertFalse(tunnel.check_health(tunnel.TARGET))
            self.assertEqual(len(requests),1)
        with mock.patch('httpx.Client') as client:
            with self.assertRaises(ValueError): tunnel.check_health('https://not-cloudflare.example')
            client.assert_not_called()

    def test_document_keeps_exact_public_schema_and_24_hour_lease(self):
        value=runtime_config('https://synthetic-test.trycloudflare.com')
        self.assertEqual(set(value),{'version','state','apiUrl','publishedAt','expiresAt'})
        self.assertEqual(parse_timestamp(value['expiresAt'])-parse_timestamp(value['publishedAt']),timedelta(hours=24))


class HealthDiagnosticTests(unittest.TestCase):
    def probe(self, response=None, failure=None):
        real_client = httpx.Client
        def handler(request):
            if failure is not None:
                outer = httpx.ConnectError('synthetic-private-address-and-secret', request=request)
                raise outer from failure
            return httpx.Response(response.status_code, headers=response.headers,
                                  stream=httpx.ByteStream(response.content))
        def client(**kwargs):
            self.assertFalse(kwargs['trust_env'])
            self.assertFalse(kwargs['follow_redirects'])
            self.assertNotIn('verify', kwargs)  # Normal certificate verification remains enabled.
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        diagnostics = {}
        with mock.patch('httpx.Client', side_effect=client):
            healthy = tunnel.check_health(tunnel.TARGET, diagnostics=diagnostics)
        return healthy, diagnostics

    def test_exception_types_distinguish_dns_tls_timeout_and_network_without_text(self):
        for failure, code in (
            (socket.gaierror(11001, 'synthetic-private-host'), 'dns_lookup_failed'),
            (ssl.SSLCertVerificationError(1, 'synthetic-private-certificate'), 'tls_failed'),
            (httpx.ConnectTimeout('synthetic-private-timeout'), 'health_timeout'),
            (OSError(10061, 'synthetic-private-address'), 'network_failed'),
        ):
            with self.subTest(code=code):
                healthy, diagnostics = self.probe(failure=failure)
                self.assertFalse(healthy)
                self.assertEqual(diagnostics, {'code': code})
        cyclic = RuntimeError('not a DNS error despite mentioning getaddrinfo 11001')
        cyclic.__cause__ = cyclic
        self.assertEqual(tunnel.health_exception_code(cyclic), 'network_failed')

    def test_http_status_body_limit_and_exact_health_have_distinct_safe_codes(self):
        for response, code in (
            (httpx.Response(503, text='synthetic-private-server-body'), 'http_status_503'),
            (httpx.Response(302, headers={'Location': 'https://private.example'}), 'http_status_302'),
            (httpx.Response(200, content=b'{malformed-synthetic-private-body'), 'invalid_health'),
            (httpx.Response(200, json={'status': 'ok', 'secret': 'synthetic'}), 'invalid_health'),
            (httpx.Response(200, content=b'x' * 4097), 'health_body_too_large'),
            (httpx.Response(200, json={'status': 'ok'}), 'ok'),
        ):
            with self.subTest(code=code):
                healthy, diagnostics = self.probe(response=response)
                self.assertEqual(healthy, code == 'ok')
                self.assertEqual(diagnostics, {'code': code})

    def test_ordinary_public_health_poll_does_not_clear_dns_cache(self):
        with mock.patch.object(tunnel, '_health_attempt', return_value='dns_lookup_failed'), \
             mock.patch.object(tunnel, 'clear_startup_dns_cache') as clear:
            self.assertFalse(tunnel.check_health('https://synthetic-poll-only.trycloudflare.com'))
        clear.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows-only CLI')
    def test_cli_emits_only_fixed_codes_even_for_untrusted_error_attributes(self):
        cases = [
            (tunnel.TunnelStartError('startup_timeout', 'public_dns_lookup_failed'),
             '[startup_timeout; public_dns_lookup_failed]'),
            (tunnel.TunnelStartError('startup_no_url'), '[startup_no_url]'),
            (tunnel.TunnelStartError('process_exited'), '[process_exited]'),
            (tunnel.ModelProcessError('synthetic-private-secret https://private.example'), '[operation_failed]'),
            (OSError('synthetic-private-secret C:/private/service.env'), '[operation_failed]'),
        ]
        changed = tunnel.TunnelStartError('startup_timeout')
        changed.code, changed.diagnostic = 'synthetic-private-secret', 'public_synthetic-private-secret'
        cases.append((changed, '[operation_failed]'))
        for error, code in cases:
            with self.subTest(code=code), mock.patch.object(tunnel, 'WindowsTunnelController') as controller:
                controller.return_value.start.side_effect = error
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    self.assertEqual(tunnel.main(['start']), 1)
                message = stderr.getvalue()
                self.assertIn(code, message)
                for private in ('synthetic-private', 'private.example', 'service.env'):
                    self.assertNotIn(private, message)


@unittest.skipUnless(os.name=='nt','native Windows process handle tests')
class TunnelProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)/'owned'
        self.binary=Path(sys._base_executable).resolve()
        self.prefix=[str(self.binary),'-c',"import time; print('https://synthetic-owned-test.trycloudflare.com',flush=True); time.sleep(30)"]
        self.controller=tunnel.WindowsTunnelController(self.root,binary=self.binary,command_prefix=self.prefix)
        # These native lifecycle fixtures never mutate the machine DNS cache.
        patcher = mock.patch.object(tunnel, 'clear_startup_dns_cache', return_value=True)
        self.dns_clear = patcher.start()
        self.addCleanup(patcher.stop)
        # Most lifecycle cases need immediate synthetic recovery; dedicated
        # timing cases below retain a measured nonzero propagation grace.
        delay = mock.patch.object(tunnel, 'DNS_CACHE_RETRY_DELAYS', (0.0, 0.0, 0.0))
        delay.start()
        self.addCleanup(delay.stop)

    def tearDown(self):
        # Test resources only; each test must terminate its original process handles.
        self.temporary.cleanup()

    def test_start_verify_repeat_and_stop_only_own_native_process(self):
        calls=[]
        def healthy(origin, **kwargs): calls.append(origin); return True
        with mock.patch.object(tunnel,'check_health',side_effect=healthy):
            result=self.controller.start(timeout=3)
            try:
                self.assertTrue(result['running'])
                self.assertTrue(result['publication_managed_externally'])
                record=self.controller.read_record()
                self.assertEqual(record['target'],'http://127.0.0.1:8765')
                command=self.controller.command(record['instance'])
                self.assertEqual(command[command.index('--http-host-header')+1],'127.0.0.1:8765')
                self.assertEqual(command[command.index('--metrics')+1],'127.0.0.1:0')
                self.assertIn(record['instance'],self.controller.command(record['instance'])[5])
                repeated=self.controller.start(timeout=3)
                self.assertEqual(repeated['pid'],result['pid'])
                self.assertEqual(calls[0],tunnel.TARGET)
            finally:
                stopped=self.controller.stop(timeout=5)
        self.assertFalse(stopped['running'])
        self.assertEqual(stopped['desired_config']['state'],'offline')
        self.assertIsNone(process_identity(result['pid']))
        self.assertTrue(list(self.root.glob('cloudflared-*.log')))

    def test_stop_then_restart_ignores_previous_log_and_uses_a_new_identity(self):
        with mock.patch.object(tunnel, 'check_health', return_value=True):
            first = self.controller.start(timeout=3)
            original = self.controller.read_record()
            self.controller.stop(timeout=5)
            self.assertEqual(self.controller.desired()['state'], 'offline')
            self.assertIsNone(self.controller.read_record())
            self.controller.command_prefix = [str(self.binary), '-c',
                "import time; print('https://synthetic-next-launch.trycloudflare.com',flush=True); time.sleep(30)"]
            try:
                second = self.controller.start(timeout=3)
                self.assertNotEqual(self.controller.read_record()['instance'], original['instance'])
                self.assertNotEqual(second['api_url'], first['api_url'])
                self.assertEqual(len(list(self.root.glob('cloudflared-*.log'))), 2)
            finally:
                self.controller.stop(timeout=5)

    def test_fresh_start_clears_once_only_after_dns_error_then_checks_https_again(self):
        order = []
        def clear(*, timeout):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 2)
            order.append('clear')
            return True
        def healthy(origin, *, diagnostics=None):
            if origin == tunnel.TARGET:
                return True
            ready = 'clear' in order
            order.append('https_ok' if ready else 'https_dns_failed')
            diagnostics['code'] = 'ok' if ready else 'dns_lookup_failed'
            return ready
        self.dns_clear.side_effect = clear
        with mock.patch.object(tunnel, 'check_health', side_effect=healthy):
            result = self.controller.start(timeout=3)
            try:
                self.assertTrue(result['running'])
                self.assertEqual(order, ['https_dns_failed', 'clear', 'https_ok'])
                self.controller.start(timeout=3)
                self.assertEqual(order[-1], 'https_ok')
                self.dns_clear.assert_called_once()
            finally:
                self.controller.stop(timeout=5)

    def test_dns_recovery_honors_three_offsets_while_https_probes_continue(self):
        failures, clears = [], []
        offsets = (.15, .35, .55)
        def clear(*, timeout):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 2)
            clears.append(time.monotonic())
            return True
        def healthy(origin, *, diagnostics=None):
            if origin == tunnel.TARGET:
                return True
            if len(clears) < 3:
                failures.append(time.monotonic())
                diagnostics['code'] = 'dns_lookup_failed'
                return False
            diagnostics['code'] = 'ok'
            return True
        self.dns_clear.side_effect = clear
        with mock.patch.object(tunnel, 'DNS_CACHE_RETRY_DELAYS', offsets), \
             mock.patch.object(tunnel, 'check_health', side_effect=healthy):
            try:
                result = self.controller.start(timeout=3)
                self.assertTrue(result['running'])
                self.assertGreaterEqual(len(failures), 4)
                self.assertEqual(len(clears), 3)
                for actual, minimum in zip(clears, offsets):
                    self.assertGreaterEqual(actual - failures[0], minimum)
            finally:
                self.controller.stop(timeout=5)

    def test_startup_deadline_before_grace_never_clears_cache(self):
        def healthy(origin, *, diagnostics=None):
            diagnostics['code'] = 'ok' if origin == tunnel.TARGET else 'dns_lookup_failed'
            return origin == tunnel.TARGET
        with mock.patch.object(tunnel, 'DNS_CACHE_RETRY_DELAYS', (10.0, 30.0, 60.0)), \
             mock.patch.object(tunnel, 'check_health', side_effect=healthy) as health:
            with self.assertRaises(tunnel.TunnelStartError) as raised:
                self.controller.start(timeout=1)
        self.assertEqual(raised.exception.diagnostic, 'public_dns_lookup_failed')
        self.assertGreater(len(health.call_args_list), 2)
        self.dns_clear.assert_not_called()
        self.assertIsNone(self.controller.read_record())

    def test_existing_running_tunnel_dns_failure_does_not_clear_cache(self):
        def healthy(origin, *, diagnostics=None):
            diagnostics['code'] = 'ok' if origin == tunnel.TARGET else 'dns_lookup_failed'
            return origin == tunnel.TARGET
        with mock.patch.object(tunnel, 'check_health', return_value=True):
            self.controller.start(timeout=3)
        try:
            with mock.patch.object(tunnel, 'check_health', side_effect=healthy):
                with self.assertRaises(tunnel.TunnelStartError) as raised:
                    self.controller.start(timeout=1)
            self.assertEqual(raised.exception.code, 'existing_tunnel_unhealthy')
            self.dns_clear.assert_not_called()
        finally:
            self.controller.stop(timeout=5)

    def test_later_tls_error_does_not_consume_remaining_dns_retries(self):
        def healthy(origin, *, diagnostics=None):
            if origin == tunnel.TARGET:
                return True
            diagnostics['code'] = 'tls_failed' if self.dns_clear.called else 'dns_lookup_failed'
            return False
        with mock.patch.object(tunnel, 'check_health', side_effect=healthy):
            with self.assertRaises(tunnel.TunnelStartError) as raised:
                self.controller.start(timeout=1)
        self.assertEqual(raised.exception.diagnostic, 'public_tls_failed')
        self.dns_clear.assert_called_once()
        self.assertIsNone(self.controller.read_record())

    def test_non_dns_public_failures_never_clear_cache(self):
        for code in ('tls_failed', 'http_status_503', 'invalid_health'):
            with self.subTest(code=code):
                def healthy(origin, *, diagnostics=None):
                    diagnostics['code'] = 'ok' if origin == tunnel.TARGET else code
                    return origin == tunnel.TARGET
                with mock.patch.object(tunnel, 'check_health', side_effect=healthy):
                    with self.assertRaises(tunnel.TunnelStartError) as raised:
                        self.controller.start(timeout=1)
                self.assertEqual(raised.exception.diagnostic, 'public_' + code)
                self.dns_clear.assert_not_called()

    def test_unready_api_never_launches(self):
        with mock.patch.object(tunnel,'check_health',return_value=False),mock.patch.object(tunnel,'OwnedLaunch') as launch:
            with self.assertRaises(tunnel.ModelProcessError): self.controller.start(timeout=1)
        launch.assert_not_called()
        self.dns_clear.assert_not_called()
        self.assertIsNone(self.controller.read_record())

    def test_failed_public_health_rolls_back_new_job_only(self):
        pids=[]
        original=tunnel.OwnedLaunch
        class ObservedLaunch(original):
            def __enter__(self):
                result=super().__enter__(); pids.append(self.process.pid); return result
        with mock.patch.object(tunnel,'check_health',side_effect=lambda origin,**kwargs:origin==tunnel.TARGET),mock.patch.object(tunnel,'OwnedLaunch',ObservedLaunch):
            with self.assertRaises(tunnel.ModelProcessError): self.controller.start(timeout=1)
        self.assertEqual(len(pids),1)
        self.assertIsNone(process_identity(pids[0]))
        self.assertIsNone(self.controller.read_record())
        self.assertIsNone(self.controller.desired())

    def test_startup_dns_failure_reports_last_probe_and_rolls_back_own_process(self):
        self.dns_clear.return_value = False
        pids = []
        original = tunnel.OwnedLaunch
        class ObservedLaunch(original):
            def __enter__(self):
                result = super().__enter__()
                pids.append(self.process.pid)
                return result
        def probe(origin, *, diagnostics=None):
            if diagnostics is not None:
                diagnostics['code'] = 'ok' if origin == tunnel.TARGET else 'dns_lookup_failed'
            return origin == tunnel.TARGET
        with mock.patch.object(tunnel, 'check_health', side_effect=probe), \
             mock.patch.object(tunnel, 'OwnedLaunch', ObservedLaunch):
            with self.assertRaises(tunnel.TunnelStartError) as raised:
                self.controller.start(timeout=1)
        self.assertEqual(raised.exception.code, 'startup_timeout')
        self.assertEqual(raised.exception.diagnostic, 'public_dns_lookup_failed')
        self.assertEqual(self.dns_clear.call_count, 3)
        self.assertEqual(len(pids), 1)
        self.assertIsNone(process_identity(pids[0]))
        self.assertIsNone(self.controller.read_record())

    def test_no_url_and_early_exit_have_separate_startup_codes(self):
        for program, code in (
            ("import time; print('synthetic-no-url',flush=True); time.sleep(30)", 'startup_no_url'),
            ("raise SystemExit(7)", 'process_exited'),
        ):
            with self.subTest(code=code):
                controller = tunnel.WindowsTunnelController(self.root, binary=self.binary,
                    command_prefix=[str(self.binary), '-c', program])
                with mock.patch.object(tunnel, 'check_health', return_value=True):
                    with self.assertRaises(tunnel.TunnelStartError) as raised:
                        controller.start(timeout=1)
                self.assertEqual(raised.exception.code, code)
                self.assertIsNone(controller.read_record())
                self.dns_clear.assert_not_called()

    def test_foreign_live_command_is_not_terminated_or_cleaned(self):
        ensure_private_directory(self.root)
        foreign=subprocess.Popen([str(self.binary),'-c','import time; time.sleep(31)'],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            instance='a'*32
            record={'version':1,**process_identity(foreign.pid),'sid':tunnel.current_user_sid(),
                'project':str(tunnel.PROJECT_DIR),'runtime':str(self.root),'instance':instance,
                'command_hash':tunnel.command_hash(self.controller.command(instance)),'binary_sha256':'b'*64,
                'target':tunnel.TARGET,'api_url':'https://synthetic-owned-test.trycloudflare.com'}
            atomic_write_private(self.controller.record_file,json.dumps(record).encode())
            atomic_write_private(self.controller.config_file,json.dumps(runtime_config(record['api_url'])).encode())
            with self.assertRaises(tunnel.ModelProcessError): self.controller.stop(timeout=1)
            self.assertIsNone(foreign.poll())
            self.assertEqual(self.controller.read_record(),record)
            changed=dict(record,created=str(int(record['created'])+1))
            atomic_write_private(self.controller.record_file,json.dumps(changed).encode())
            with self.assertRaises(tunnel.ModelProcessError): self.controller.stop(timeout=1)
            self.assertIsNone(foreign.poll())
        finally:
            foreign.terminate(); foreign.wait(timeout=5)

    def test_child_environment_does_not_inherit_tokens_proxies_or_profile(self):
        ensure_private_directory(self.root)
        with mock.patch.dict(os.environ,{'TUNNEL_TOKEN':'synthetic','HTTPS_PROXY':'synthetic','STT_ENV_FILE':'synthetic','CLOVA_SPEECH_SECRET_KEY':'synthetic','USERPROFILE':'unsafe-profile'}):
            result=tunnel.clean_environment(self.root)
        self.assertEqual(result['USERPROFILE'],str(self.root/'home'))
        for key in ('TUNNEL_TOKEN','HTTPS_PROXY','STT_ENV_FILE','CLOVA_SPEECH_SECRET_KEY'):
            self.assertNotIn(key,result)


if __name__=='__main__': unittest.main()
