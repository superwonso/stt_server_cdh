"""Synthetic local tests: never contact GitHub, Pages, or a real tunnel."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import httpx
from server import windows_publication as publication
from server.platform_files import atomic_write_private, ensure_private_directory
from server.win_model_process import process_identity, process_lock
from scripts.runtime_config import runtime_config

URL = 'https://synthetic-publisher-test.trycloudflare.com'

class PublicationNetworkTests(unittest.TestCase):
    def test_fixed_cdn_rejects_redirect_oversize_and_wrong_schema(self):
        real_client = httpx.Client
        for response in (httpx.Response(302, headers={'location':'https://foreign.example'}),
                         httpx.Response(200, stream=httpx.ByteStream(b'x'*4097)),
                         httpx.Response(200, stream=httpx.ByteStream(b'{"state":"online"}'))):
            requests = []
            def handler(request): requests.append(request); return response
            def factory(**kwargs):
                self.assertFalse(kwargs['trust_env']); self.assertFalse(kwargs['follow_redirects'])
                return real_client(transport=httpx.MockTransport(handler), **kwargs)
            with mock.patch('httpx.Client', side_effect=factory):
                self.assertIsNone(publication.fetch_config())
            self.assertEqual(str(requests[0].url).split('?')[0], publication.CONFIG_URL)
            self.assertIn('check=',str(requests[0].url))

    def test_cli_environment_excludes_tokens_and_service_settings(self):
        with mock.patch.dict(os.environ, {'GH_TOKEN':'synthetic','GITHUB_TOKEN':'synthetic',
                'GH_HOST':'foreign.example','GH_DEBUG':'api','GH_CONFIG_DIR':'foreign-profile',
                'HTTPS_PROXY':'synthetic','STT_ENV_FILE':'synthetic'}):
            environment=publication.gh_environment()
        self.assertEqual(environment['GH_HOST'],'github.com')
        for key in ('GH_TOKEN','GITHUB_TOKEN','GH_DEBUG','GH_CONFIG_DIR','HTTPS_PROXY','STT_ENV_FILE'):
            self.assertNotIn(key,environment)

@unittest.skipUnless(os.name=='nt','Windows private lock/process tests')
class PublicationStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        self.directory=self.root/'publication'
        self.tunnel_directory=self.root/'tunnel'
        ensure_private_directory(self.directory); ensure_private_directory(self.tunnel_directory)
        self.document=runtime_config(URL)
        self.owner={'instance':'a'*32,'pid':1234,'created':'12345','exe':str(Path(sys._base_executable).resolve())}
        self.record={**self.owner,'api_url':URL}
        self.tunnel=mock.Mock()
        self.tunnel.directory=self.tunnel_directory
        self.tunnel.lock_file=self.tunnel_directory/'tunnel-control.lock'
        self.tunnel.config_file=self.tunnel_directory/'runtime-config.json'
        self.tunnel.read_record.return_value=self.record
        self.tunnel.desired.return_value=self.document
        self.tunnel.status.return_value={'running':True,'healthy':True,'api_url':URL}
        self.tunnel.running.return_value=True
        atomic_write_private(self.tunnel.config_file,json.dumps(self.document).encode())
        self.publisher=publication.WindowsPublisher(self.directory,gh=Path(sys._base_executable).resolve(),tunnel=self.tunnel)

    def tearDown(self): self.temporary.cleanup()

    def save_desired(self, document):
        atomic_write_private(self.publisher.desired_file,json.dumps(document).encode())

    def save_confirmed(self, document, owner=None):
        value={'version':1,'config':document,'owner':self.owner if owner is None else owner}
        atomic_write_private(self.publisher.confirmed_file,json.dumps(value).encode())

    def test_initialization_and_absent_readers_do_not_write_or_launch(self):
        missing=self.root/'does-not-exist'
        publisher=publication.WindowsPublisher(missing,tunnel=self.tunnel)
        self.assertIsNone(publisher.read_desired()); self.assertIsNone(publisher.read_confirmation())
        self.assertIsNone(publisher.read_confirmed()); self.assertFalse(missing.exists())

    def test_publish_uses_fixed_repository_stdin_and_confirms_exact_document(self):
        def fetch(**kwargs):
            # CDN wait cannot retain either lifecycle lock.
            with process_lock(self.publisher.lock_file):
                with process_lock(self.tunnel.lock_file): pass
            return self.document
        with mock.patch.object(publication,'run_gh') as gh, mock.patch.object(publication,'fetch_config',side_effect=fetch):
            result=self.publisher.publish(wait_timeout=1)
        self.assertEqual(result['state'],'confirmed')
        self.assertEqual(gh.call_count,2)
        self.assertEqual(gh.call_args_list[0].args[1],['variable','set','CLASSROOM_API_CONFIG','--repo','superwonso/stt_server_cdh'])
        self.assertEqual(json.loads(gh.call_args_list[0].args[2]),self.document)
        self.assertEqual(gh.call_args_list[1].args[1],['workflow','run','pages.yml','--repo','superwonso/stt_server_cdh','--ref','main'])
        self.assertEqual(self.publisher.read_desired(),self.document)
        self.assertEqual(self.publisher.read_confirmation(),{'version':1,'config':self.document,'owner':self.owner})
        self.tunnel.start.assert_not_called(); self.tunnel.stop.assert_not_called()

    def test_partial_dispatch_failure_preserves_previous_state_without_confirmation(self):
        old=runtime_config('OFFLINE'); self.save_desired(old)
        with mock.patch.object(publication,'run_gh',side_effect=[None,publication.PublicationError('synthetic')]),mock.patch.object(publication,'fetch_config') as fetch:
            with self.assertRaises(publication.PublicationError): self.publisher.publish(wait_timeout=1)
        self.assertEqual(self.publisher.read_desired(),old)
        self.assertIsNone(self.publisher.read_confirmed()); fetch.assert_not_called()

    def test_older_candidate_cannot_replace_newer_desired_before_github(self):
        newer=runtime_config(URL)
        older=runtime_config(URL,now=datetime.now(timezone.utc)-timedelta(hours=1))
        self.save_desired(newer)
        with mock.patch.object(publication,'run_gh') as gh:
            with self.assertRaises(publication.PublicationError): self.publisher._request(older)
            gh.assert_not_called()
        self.assertEqual(self.publisher.read_desired(),newer)

    def test_newer_desired_during_fetch_prevents_stale_confirmation(self):
        self.save_desired(self.document)
        def supersede(**kwargs):
            self.save_desired(runtime_config('OFFLINE'))
            return self.document
        with mock.patch.object(publication,'fetch_config',side_effect=supersede):
            with self.assertRaises(publication.PublicationError):
                self.publisher.wait(self.document,owner=self.owner,wait_timeout=1)
        self.assertIsNone(self.publisher.read_confirmation())

    def test_foreign_launch_cannot_be_confirmed_after_exact_cdn_response(self):
        self.save_desired(self.document)
        self.tunnel.read_record.return_value={**self.record,'created':'99999'}
        with mock.patch.object(publication,'fetch_config',return_value=self.document):
            with self.assertRaises(publication.PublicationError):
                self.publisher.wait(self.document,owner=self.owner,wait_timeout=1)
        self.assertIsNone(self.publisher.read_confirmation())

    def test_renew_requires_previous_confirmation_and_same_launch(self):
        self.save_desired(self.document)
        with mock.patch.object(publication,'run_gh') as gh:
            with self.assertRaises(publication.PublicationError): self.publisher.renew(wait_timeout=1)
            self.save_confirmed(self.document,dict(self.owner,instance='b'*32))
            with self.assertRaises(publication.PublicationError): self.publisher.renew(wait_timeout=1)
            gh.assert_not_called()
        self.tunnel.start.assert_not_called(); self.tunnel.stop.assert_not_called()

    def test_confirmed_renew_updates_24h_lease_without_restarting_tunnel(self):
        old=runtime_config(URL,now=datetime.now(timezone.utc)-timedelta(hours=20))
        self.save_desired(old); self.save_confirmed(old)
        with mock.patch.object(publication,'run_gh') as gh,mock.patch.object(publication,'fetch_config',side_effect=lambda **kwargs:self.publisher.read_desired()):
            result=self.publisher.renew(wait_timeout=1)
        self.assertEqual(gh.call_count,2)
        self.assertGreater(result['config']['expiresAt'],old['expiresAt'])
        self.assertEqual(self.publisher.read_confirmed(),result['config'])
        self.tunnel.start.assert_not_called(); self.tunnel.stop.assert_not_called()

    def test_unhealthy_or_expired_candidate_never_calls_github(self):
        with mock.patch.object(publication,'run_gh') as gh:
            self.tunnel.status.return_value['healthy']=False
            with self.assertRaises(publication.PublicationError): self.publisher.publish(wait_timeout=1)
            expired=runtime_config(URL,now=datetime.now(timezone.utc)-timedelta(days=2))
            with self.assertRaises(publication.PublicationError): self.publisher._request(expired)
            gh.assert_not_called()

    def test_offline_is_explicit_confirmed_without_tunnel_restart(self):
        with mock.patch.object(publication,'run_gh'),mock.patch.object(publication,'fetch_config',side_effect=lambda **kwargs:self.publisher.read_desired()):
            result=self.publisher.publish(offline=True,wait_timeout=1)
        self.assertEqual(result['config']['state'],'offline')
        self.assertIsNone(self.publisher.read_confirmation()['owner'])
        self.tunnel.status.assert_not_called(); self.tunnel.start.assert_not_called(); self.tunnel.stop.assert_not_called()

    def test_expired_cdn_never_confirms_and_wait_is_bounded(self):
        self.save_desired(self.document)
        expired=runtime_config(URL,now=datetime.now(timezone.utc)-timedelta(days=2))
        with mock.patch.object(publication,'fetch_config',return_value=expired):
            with self.assertRaises(publication.PublicationError):
                self.publisher.wait(self.document,owner=self.owner,wait_timeout=1)
        self.assertIsNone(self.publisher.read_confirmed())

    def test_invalid_deadline_never_launches_or_publishes(self):
        with mock.patch.object(publication,'run_gh') as gh:
            with self.assertRaises(publication.PublicationError): self.publisher.publish(wait_timeout=0)
            with self.assertRaises(publication.PublicationError): self.publisher.renew(wait_timeout=601)
            gh.assert_not_called()
        self.assertIsNone(self.publisher.read_desired())

    def test_native_profile_resolves_when_api_environment_has_no_appdata(self):
        expected=publication.roaming_appdata()
        with mock.patch.dict(os.environ,{},clear=True):
            environment=publication.gh_environment()
        self.assertTrue(Path(expected).is_absolute())
        self.assertEqual(environment['APPDATA'],expected)
        self.assertNotIn('GH_CONFIG_DIR',environment)

    def test_native_child_timeout_cancel_and_diagnostics_are_private(self):
        binary=Path(sys._base_executable).resolve()
        output,errors=io.StringIO(),io.StringIO()
        with redirect_stdout(output),redirect_stderr(errors):
            publication.run_gh(binary,['-c',"import sys; sys.stdout.write('synthetic-diagnostic'); sys.stderr.write('synthetic-diagnostic')"],b'{}',timeout=2)
            original=subprocess.Popen
            children=[]
            def observe(*args,**kwargs):
                process=original(*args,**kwargs); children.append(process); return process
            with mock.patch('subprocess.Popen',side_effect=observe):
                with self.assertRaises(publication.PublicationError):
                    publication.run_gh(binary,['-c','import time; time.sleep(30)'],timeout=1)
                event=threading.Event(); timer=threading.Timer(.1,event.set); timer.start()
                try:
                    with self.assertRaises(publication.PublicationCancelled):
                        publication.run_gh(binary,['-c','import time; time.sleep(30)'],timeout=10,cancel_event=event)
                finally: timer.cancel()
            for child in children:
                self.assertIsNotNone(child.poll()); self.assertIsNone(process_identity(child.pid))
        self.assertEqual(output.getvalue(),''); self.assertEqual(errors.getvalue(),'')

if __name__=='__main__': unittest.main()