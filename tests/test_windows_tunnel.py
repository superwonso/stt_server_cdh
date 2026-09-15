"""Synthetic-only tunnel lifecycle and security tests; never starts cloudflared."""
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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


@unittest.skipUnless(os.name=='nt','native Windows process handle tests')
class TunnelProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)/'owned'
        self.binary=Path(sys._base_executable).resolve()
        self.prefix=[str(self.binary),'-c',"import time; print('https://synthetic-owned-test.trycloudflare.com',flush=True); time.sleep(30)"]
        self.controller=tunnel.WindowsTunnelController(self.root,binary=self.binary,command_prefix=self.prefix)

    def tearDown(self):
        # Test resources only; each test must terminate its original process handles.
        self.temporary.cleanup()

    def test_start_verify_repeat_and_stop_only_own_native_process(self):
        calls=[]
        def healthy(origin): calls.append(origin); return True
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

    def test_unready_api_never_launches(self):
        with mock.patch.object(tunnel,'check_health',return_value=False),mock.patch.object(tunnel,'OwnedLaunch') as launch:
            with self.assertRaises(tunnel.ModelProcessError): self.controller.start(timeout=1)
        launch.assert_not_called()
        self.assertIsNone(self.controller.read_record())

    def test_failed_public_health_rolls_back_new_job_only(self):
        pids=[]
        original=tunnel.OwnedLaunch
        class ObservedLaunch(original):
            def __enter__(self):
                result=super().__enter__(); pids.append(self.process.pid); return result
        with mock.patch.object(tunnel,'check_health',side_effect=lambda origin:origin==tunnel.TARGET),mock.patch.object(tunnel,'OwnedLaunch',ObservedLaunch):
            with self.assertRaises(tunnel.ModelProcessError): self.controller.start(timeout=1)
        self.assertEqual(len(pids),1)
        self.assertIsNone(process_identity(pids[0]))
        self.assertIsNone(self.controller.read_record())
        self.assertIsNone(self.controller.desired())

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
