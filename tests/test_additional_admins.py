from dataclasses import replace
from pathlib import Path
import secrets
import tempfile
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from server.app import create_app
from server.security import PASSWORD_HASHER, digest
from server.settings import Settings


ACCOUNTS = ('user-alpha', 'user-beta', 'user-gamma')
PASSWORD = 'synthetic additional administrator password'


class AdditionalAdministratorSettingsTests(unittest.TestCase):
    def settings(self, **kwargs):
        return Settings(data_dir=Path('/unused-data'), model_cache_dir=Path('/unused-models'),
                        accounts=ACCOUNTS, **kwargs)

    def test_additional_admin_does_not_replace_primary_or_expose_names_in_repr(self):
        settings = self.settings(admin_username=ACCOUNTS[0], additional_admin_usernames=(ACCOUNTS[1],))
        self.assertEqual(settings.administrator_usernames, ACCOUNTS[:2])
        self.assertTrue(settings.is_admin(ACCOUNTS[0]))
        self.assertTrue(settings.is_admin(ACCOUNTS[1]))
        for value in (ACCOUNTS[2], 'unknown', '관리자', None, True):
            self.assertFalse(settings.is_admin(value))
        for value in ACCOUNTS:
            self.assertNotIn(value, repr(settings))

    def test_default_grants_nobody_and_primary_overlap_is_deduplicated(self):
        self.assertFalse(self.settings().is_admin(ACCOUNTS[0]))
        settings = self.settings(admin_username=ACCOUNTS[0], additional_admin_usernames=ACCOUNTS[:2])
        self.assertEqual(settings.administrator_usernames, ACCOUNTS[:2])

    def test_invalid_allowlists_fail_closed_without_echoing_values(self):
        for value in ('user-beta', ['user-beta'], ('unknown-private-id',), ('',),
                      ('user-beta', 'user-beta'), (True,), (['user-beta'],)):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    self.settings(additional_admin_usernames=value)
                self.assertNotIn('unknown-private-id', str(caught.exception))

    def test_env_parsing_keeps_legacy_and_rejects_trailing_empty_member(self):
        env={'ACCOUNT_USERNAMES':','.join(ACCOUNTS), 'ADMIN_USERNAME':ACCOUNTS[0],
             'ADDITIONAL_ADMIN_USERNAMES':'  user-beta  '}
        with mock.patch('server.settings.load_dotenv'), mock.patch.dict('os.environ', env, clear=True):
            self.assertEqual(Settings.from_env().administrator_usernames, ACCOUNTS[:2])
        env['ADDITIONAL_ADMIN_USERNAMES']='user-beta,'
        with mock.patch('server.settings.load_dotenv'), mock.patch.dict('os.environ', env, clear=True):
            with self.assertRaises(ValueError):Settings.from_env()


class AdditionalAdministratorApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = PASSWORD_HASHER.hash(PASSWORD)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root=Path(self.directory.name)
        self.settings=Settings(data_dir=root/'data', model_cache_dir=root/'models', accounts=ACCOUNTS,
                               admin_username=ACCOUNTS[0], additional_admin_usernames=(ACCOUNTS[1],),
                               site_origins=('https://student.github.io',))
        self.app=create_app(self.settings, transcriber=mock.Mock(status=lambda:{'model_state':'ready'}))
        self.client=TestClient(self.app);self.addCleanup(self.client.close)
        self.tokens={name:secrets.token_urlsafe(32) for name in ACCOUNTS}
        with self.app.state.database.connect() as db:
            for name,token in self.tokens.items():
                db.execute('UPDATE users SET password_hash=? WHERE username=?',(self.password_hash,name))
                db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(digest(token),name,time.time()+3600,time.time()))

    def headers(self, name):
        return {'Authorization':'Bearer '+self.tokens[name]}

    def test_both_admins_allowed_student_and_anonymous_denied_without_account_mutation(self):
        with self.app.state.database.connect() as db:
            before=[tuple(row) for row in db.execute('SELECT * FROM users ORDER BY username')]
        for name in ACCOUNTS[:2]:
            self.assertEqual(self.client.get('/admin/overview',headers=self.headers(name)).status_code,200)
        self.assertEqual(self.client.get('/admin/overview',headers=self.headers(ACCOUNTS[2])).status_code,403)
        self.assertEqual(self.client.get('/admin/overview').status_code,401)
        with self.app.state.database.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM users ORDER BY username')],before)

    def test_additional_admin_must_reauthenticate_for_password_recovery_and_cannot_target_self(self):
        owner=ACCOUNTS[1];headers=self.headers(owner)
        overview=self.client.get('/admin/overview',headers=headers).json()
        ids={row['label']:row['account_id'] for row in overview['accounts']}
        payload={'account_id':ids[ACCOUNTS[2]],'current_password':'wrong synthetic password'}
        self.assertEqual(self.client.post('/admin/password-resets',headers=headers,json=payload).status_code,403)
        payload['current_password']=PASSWORD
        self.assertEqual(self.client.post('/admin/password-resets',headers=headers,json=payload).status_code,200)
        payload['account_id']=ids[owner]
        self.assertEqual(self.client.post('/admin/password-resets',headers=headers,json=payload).status_code,409)

    def test_existing_session_loses_admin_permission_after_config_removal(self):
        app=create_app(replace(self.settings,additional_admin_usernames=()),transcriber=mock.Mock(status=lambda:{'model_state':'ready'}))
        with TestClient(app) as client:
            self.assertEqual(client.get('/admin/overview',headers=self.headers(ACCOUNTS[1])).status_code,403)
            self.assertEqual(client.get('/auth/me',headers=self.headers(ACCOUNTS[1])).status_code,200)
            self.assertEqual(client.get('/admin/overview',headers=self.headers(ACCOUNTS[0])).status_code,200)
