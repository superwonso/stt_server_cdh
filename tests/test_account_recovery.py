from __future__ import annotations

import secrets
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import account_recovery
from server.app import create_app
from server.db import Database
from server.security import PASSWORD_HASHER, digest, password_matches
from server.settings import Settings

ACCOUNTS = ("user-alpha", "user-beta", "user-gamma")
ADMIN_PASSWORD = "synthetic administrator password"
OLD_PASSWORD = "synthetic original password"
NEW_PASSWORD = "새암호4"


class FakeTranscriber:
    def status(self):
        return {"model_state": "unloaded", "engine": "fake", "model": "fake", "device": "cpu"}


class RecoveryApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_hash = PASSWORD_HASHER.hash(ADMIN_PASSWORD)
        cls.old_hash = PASSWORD_HASHER.hash(OLD_PASSWORD)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-test-recovery-")
        self.directory = Path(self.temporary.name)
        self.app = create_app(Settings(data_dir=self.directory / "data", model_cache_dir=self.directory / "models",
                                       accounts=ACCOUNTS, admin_username=ACCOUNTS[0],
                                       site_origins=("https://student.github.io",), recording_free_reserve_bytes=0),
                              FakeTranscriber())
        self.database = self.app.state.database
        self.client = TestClient(self.app)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.client.close)
        self.tokens = {name: secrets.token_urlsafe(32) for name in ACCOUNTS}
        with self.database.connect() as connection:
            for index, name in enumerate(ACCOUNTS):
                connection.execute("UPDATE users SET password_hash=?,setup_hash=?,setup_expires=? WHERE username=?",
                                   (self.admin_hash if index == 0 else self.old_hash, digest("old setup"), time.time()+3600, name))
                connection.execute("INSERT INTO sessions VALUES(?,?,?,?)", (digest(self.tokens[name]),name,time.time()+3600,time.time()))
        result = self.client.get("/admin/overview", headers=self.headers())
        self.assertEqual(result.status_code, 200)
        self.ids = {entry["label"]: entry["account_id"] for entry in result.json()["accounts"]}

    def headers(self, name=ACCOUNTS[0]):
        return {"Authorization": "Bearer " + self.tokens[name]}

    def issue(self, name=ACCOUNTS[1], **overrides):
        return self.client.post("/admin/password-resets", headers=self.headers(),
                                json={"account_id":self.ids[name], "current_password":ADMIN_PASSWORD} | overrides)

    def revoke(self, name=ACCOUNTS[1], **overrides):
        return self.client.post("/admin/password-resets/revoke", headers=self.headers(),
                                json={"account_id":self.ids[name], "current_password":ADMIN_PASSWORD} | overrides)

    def complete(self, code, name=ACCOUNTS[1], **overrides):
        return self.client.post("/auth/reset-password", json={"username":name,"reset_code":code,
                                "password":NEW_PASSWORD,"password_confirm":NEW_PASSWORD} | overrides)

    def snapshot(self, tables=("users","sessions","account_password_resets")):
        with self.database.connect() as connection:
            return {table:[tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in tables}

    def test_issue_stores_only_hash_expires_in_thirty_minutes_and_does_not_logout(self):
        before = self.snapshot(("users","sessions"))
        started = time.time()
        response = self.issue()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.headers["cache-control"],"no-store")
        value = response.json()
        self.assertEqual(set(value),{"username","reset_code","expires_at"})
        self.assertEqual(value["username"],ACCOUNTS[1])
        self.assertRegex(value["reset_code"],r"^[a-zA-Z0-9_-]{43}$")
        self.assertGreaterEqual(value["expires_at"],started+1800)
        self.assertLessEqual(value["expires_at"],time.time()+1800)
        self.assertEqual(self.snapshot(("users","sessions")),before)
        stored = self.snapshot(("account_password_resets","admin_audit"))
        self.assertNotIn(value["reset_code"],repr(stored))
        self.assertNotIn(ADMIN_PASSWORD,repr(stored))
        self.assertEqual(stored["account_password_resets"][0][1],digest(value["reset_code"]))
        self.assertEqual(stored["account_password_resets"][0][2],digest(self.old_hash))
        self.assertEqual(stored["admin_audit"][0][2],"password_reset_issued")
        self.assertEqual(self.client.get("/auth/me",headers=self.headers(ACCOUNTS[1])).status_code,200)

    def test_success_invalidates_every_target_session_and_setup_but_not_other_accounts(self):
        with self.database.connect() as connection:
            connection.execute("INSERT INTO sessions VALUES(?,?,?,?)",(digest("another target token"),ACCOUNTS[1],time.time()+3600,time.time()))
        before = self.snapshot()
        code = self.issue().json()["reset_code"]
        response = self.complete(code)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json(),{"status":"password_reset"})
        self.assertEqual(response.headers["cache-control"],"no-store")
        after = self.snapshot()
        target = next(row for row in after["users"] if row[0] == ACCOUNTS[1])
        self.assertTrue(password_matches(target[1],NEW_PASSWORD))
        self.assertEqual(target[2:],(None,None))
        self.assertFalse(any(row[1] == ACCOUNTS[1] for row in after["sessions"]))
        self.assertEqual(after["account_password_resets"],[])
        for table,column in (("users",0),("sessions",1)):
            self.assertEqual([row for row in before[table] if row[column] != ACCOUNTS[1]],
                             [row for row in after[table] if row[column] != ACCOUNTS[1]])
        self.assertEqual(self.client.get("/auth/me",headers=self.headers(ACCOUNTS[1])).status_code,401)
        self.assertEqual(self.client.post("/auth/login",json={"username":ACCOUNTS[1],"password":OLD_PASSWORD}).status_code,401)
        self.assertEqual(self.client.post("/auth/login",json={"username":ACCOUNTS[1],"password":NEW_PASSWORD}).status_code,200)

    def test_code_is_one_time_and_failed_retry_preserves_new_password(self):
        code = self.issue().json()["reset_code"]
        self.assertEqual(self.complete(code).status_code,200)
        before = self.snapshot()
        self.assertEqual(self.complete(code,password="other password",password_confirm="other password").status_code,400)
        self.assertEqual(self.snapshot(),before)

    def test_reissue_invalidates_old_code_without_changing_password_or_sessions(self):
        old = self.issue().json()["reset_code"]
        new = self.issue().json()["reset_code"]
        self.assertNotEqual(old,new)
        before = self.snapshot()
        self.assertEqual(self.complete(old).status_code,400)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.complete(new).status_code,200)

    def test_revoke_is_idempotent_and_never_changes_existing_credentials(self):
        code = self.issue().json()["reset_code"]
        before = self.snapshot(("users","sessions"))
        for _ in range(2):
            response = self.revoke()
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json(),{"status":"revoked"})
        self.assertEqual(self.snapshot(("users","sessions")),before)
        self.assertEqual(self.complete(code).status_code,400)

    def test_expired_code_is_rejected_without_mutation(self):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            code = account_recovery.issue_password_reset(connection,ACCOUNTS[1],now=time.time()-1801)["reset_code"]
        before = self.snapshot()
        self.assertEqual(self.complete(code).status_code,400)
        self.assertEqual(self.snapshot(),before)

    def test_code_is_bound_to_current_password_hash(self):
        code = self.issue().json()["reset_code"]
        with self.database.connect() as connection:
            connection.execute("UPDATE users SET password_hash=? WHERE username=?",(self.admin_hash,ACCOUNTS[1]))
        before = self.snapshot()
        self.assertEqual(self.complete(code).status_code,400)
        self.assertEqual(self.snapshot(),before)

    def test_unknown_account_wrong_code_wrong_owner_have_same_safe_response(self):
        code = self.issue().json()["reset_code"]
        before = self.snapshot()
        responses = [self.complete(code,name="unknown-synthetic-user"),self.complete("invalid-secret-code-12345"),
                     self.complete(code,name=ACCOUNTS[2])]
        self.assertEqual([r.status_code for r in responses],[400,400,400])
        self.assertEqual(len({r.text for r in responses}),1)
        self.assertNotIn(code,responses[0].text)
        self.assertEqual(self.snapshot(),before)

    def test_anonymous_and_normal_user_cannot_issue_or_revoke(self):
        body = {"account_id":self.ids[ACCOUNTS[1]],"current_password":ADMIN_PASSWORD}
        for path in ("/admin/password-resets","/admin/password-resets/revoke"):
            self.assertEqual(self.client.post(path,json=body).status_code,401)
            self.assertEqual(self.client.post(path,json=body,headers=self.headers(ACCOUNTS[2])).status_code,403)
        self.assertEqual(self.snapshot(("account_password_resets",))["account_password_resets"],[])

    def test_admin_self_and_inactive_targets_rejected(self):
        self.assertEqual(self.issue(name=ACCOUNTS[0]).status_code,409)
        self.assertEqual(self.revoke(name=ACCOUNTS[0]).status_code,409)
        with self.database.connect() as connection:
            connection.execute("UPDATE users SET password_hash=NULL WHERE username=?",(ACCOUNTS[2],))
        before = self.snapshot()
        self.assertEqual(self.issue(name=ACCOUNTS[2]).status_code,409)
        self.assertEqual(self.revoke(name=ACCOUNTS[2]).status_code,409)
        self.assertEqual(self.snapshot(),before)

    def test_reauthentication_and_unknown_opaque_id_are_safe(self):
        before = self.snapshot()
        self.assertEqual(self.issue(current_password="not-the-admin-password").status_code,403)
        self.assertEqual(self.issue(account_id="unknown-opaque-id").status_code,404)
        # Unicode is bounded input, not a compare_digest crash or reflection.
        self.assertEqual(self.issue(account_id="알수없는계정").status_code,404)
        self.assertEqual(self.snapshot(),before)

    def test_confirm_validation_four_character_minimum_and_no_reflection(self):
        code = self.issue().json()["reset_code"]
        before = self.snapshot()
        for changes in ({"password_confirm":"다른암호"},{"password":"123","password_confirm":"123"},
                        {"password":"s"*129,"password_confirm":"s"*129}, {"unexpected_secret":"do-not-echo"}):
            response = self.complete(code,**changes)
            self.assertEqual(response.status_code,422)
            for secret in (code,"do-not-echo", "s"*129):
                self.assertNotIn(secret,response.text)
            self.assertEqual(response.headers["cache-control"],"no-store")
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.complete(code,password="네글자요",password_confirm="네글자요").status_code,200)

    def test_reset_token_does_not_activate_or_login_or_authorize_requests(self):
        code = self.issue().json()["reset_code"]
        self.assertEqual(self.client.post("/auth/activate",json={"username":ACCOUNTS[1],"setup_code":code,"password":NEW_PASSWORD}).status_code,400)
        self.assertEqual(self.client.post("/auth/login",json={"username":ACCOUNTS[1],"password":code}).status_code,401)
        self.assertEqual(self.client.get("/auth/me",headers={"Authorization":"Bearer "+code}).status_code,401)

    def test_admin_and_public_attempts_rate_limited_separately(self):
        with mock.patch("server.account_recovery.password_matches",return_value=False) as verify:
            for _ in range(10):
                self.assertEqual(self.issue(current_password="bad").status_code,403)
            response = self.revoke(current_password="bad")
            self.assertEqual(response.status_code,429)
            self.assertEqual(verify.call_count,10)
        for _ in range(10):
            self.assertEqual(self.complete("invalid-code-123456789").status_code,400)
        response = self.complete("invalid-code-123456789")
        self.assertEqual(response.status_code,429)
        self.assertIn("retry-after",response.headers)

    def test_untrusted_origin_rejected_before_mutation(self):
        before = self.snapshot()
        response = self.client.post("/admin/password-resets",headers=self.headers()|{"Origin":"https://foreign.invalid"},
                                    json={"account_id":self.ids[ACCOUNTS[1]],"current_password":ADMIN_PASSWORD})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.snapshot(),before)

    def test_recovery_works_while_data_access_paused(self):
        with self.database.connect() as connection:
            connection.execute("UPDATE operational_state SET access_enabled=0 WHERE singleton=1")
        code = self.issue().json()["reset_code"]
        self.assertEqual(self.complete(code).status_code,200)

    def test_audit_failure_rolls_back_issue_and_complete(self):
        with self.database.connect() as connection:
            connection.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON admin_audit BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.issue()
        self.assertEqual(self.snapshot(),before)
        with self.database.connect() as connection:
            connection.execute("DROP TRIGGER fail_audit")
        code = self.issue().json()["reset_code"]
        with self.database.connect() as connection:
            connection.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON admin_audit BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.complete(code)
        self.assertEqual(self.snapshot(),before)

    def test_two_concurrent_completions_only_one_can_commit(self):
        code = self.issue().json()["reset_code"]
        barrier = threading.Barrier(2)
        actual_hash = PASSWORD_HASHER.hash
        def coordinated_hash(value):
            encoded = actual_hash(value)
            barrier.wait(timeout=10)
            return encoded
        with mock.patch("server.account_recovery.PASSWORD_HASHER",mock.Mock(hash=coordinated_hash)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _:self.complete(code),range(2)))
        self.assertEqual(sorted(r.status_code for r in responses),[200,400])
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM admin_audit WHERE action='password_reset_completed'").fetchone()[0],1)

    def test_reissue_while_password_hashing_rejects_old_completion(self):
        old_code = self.issue().json()["reset_code"]
        actual_hash = PASSWORD_HASHER.hash
        def reissue_during_hash(value):
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self.new_code = account_recovery.issue_password_reset(connection,ACCOUNTS[1])["reset_code"]
            return actual_hash(value)
        before = self.snapshot(("users","sessions"))
        with mock.patch("server.account_recovery.PASSWORD_HASHER",mock.Mock(hash=reissue_during_hash)):
            self.assertEqual(self.complete(old_code).status_code,400)
        self.assertEqual(self.snapshot(("users","sessions")),before)
        self.assertEqual(self.complete(self.new_code).status_code,200)

    def test_admin_session_revoked_during_password_check_prevents_issue(self):
        def revoke_during_check(encoded,password):
            with self.database.connect() as connection:
                connection.execute("DELETE FROM sessions WHERE username=?",(ACCOUNTS[0],))
            return True
        with mock.patch("server.account_recovery.password_matches",side_effect=revoke_during_check):
            self.assertEqual(self.issue().status_code,401)
        self.assertEqual(self.snapshot(("account_password_resets",))["account_password_resets"],[])

    def test_admin_password_changed_during_password_check_prevents_revoke(self):
        self.issue()
        before = self.snapshot(("account_password_resets",))
        def change_during_check(encoded,password):
            with self.database.connect() as connection:
                connection.execute("UPDATE users SET password_hash=? WHERE username=?",(self.old_hash,ACCOUNTS[0]))
            return True
        with mock.patch("server.account_recovery.password_matches",side_effect=change_during_check):
            self.assertEqual(self.revoke().status_code,401)
        self.assertEqual(self.snapshot(("account_password_resets",)),before)

    def test_old_password_login_cannot_mint_session_after_reset_commit(self):
        code = self.issue().json()["reset_code"]
        def reset_after_verification(encoded,password):
            self.assertTrue(password_matches(encoded,password))
            self.assertEqual(self.complete(code).status_code,200)
            return True
        with mock.patch("server.app.password_matches",side_effect=reset_after_verification):
            response = self.client.post("/auth/login",json={"username":ACCOUNTS[1],"password":OLD_PASSWORD})
        self.assertEqual(response.status_code,401)
        self.assertFalse(any(row[1] == ACCOUNTS[1] for row in self.snapshot()["sessions"]))

    def test_activation_session_is_bound_to_just_committed_password(self):
        setup = secrets.token_urlsafe(32)
        with self.database.connect() as connection:
            connection.execute("UPDATE users SET password_hash=NULL,setup_hash=?,setup_expires=? WHERE username=?",
                               (digest(setup),time.time()+3600,ACCOUNTS[2]))
        # new_secret() inside issue_session runs after activation committed.
        # Simulate an authorized local reset during that exact gap.
        def change_before_session():
            with self.database.connect() as connection:
                connection.execute("UPDATE users SET password_hash=? WHERE username=?",(self.admin_hash,ACCOUNTS[2]))
                connection.execute("DELETE FROM sessions WHERE username=?",(ACCOUNTS[2],))
            return "synthetic-new-session-token"
        with mock.patch("server.app.new_secret",side_effect=change_before_session):
            response = self.client.post("/auth/activate",json={"username":ACCOUNTS[2],"setup_code":setup,"password":NEW_PASSWORD})
        self.assertEqual(response.status_code,401)
        self.assertFalse(any(row[1] == ACCOUNTS[2] for row in self.snapshot()["sessions"]))

    def recording(self):
        lecture_id,chunk_id,segment_id = (str(uuid.uuid4()) for _ in range(3))
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lectures(id,username,title,created_at,recording_finalized) VALUES(?,?,'synthetic lecture','now',1)",
                               (lecture_id,ACCOUNTS[1]))
            connection.execute("INSERT INTO chunks(lecture_id,chunk_id,payload_hash,start_seconds,status) VALUES(?,?,'hash',0,'done')",(lecture_id,chunk_id))
            connection.execute("INSERT INTO segments VALUES(?,?,?,0,1,'synthetic transcript')",(segment_id,lecture_id,chunk_id))
        store = self.app.state.recording_store
        store.write_chunk(ACCOUNTS[1],lecture_id,start_seconds=0,overlap_seconds=0,pcm=b"\0\0"*16000)
        return lecture_id,store.path(ACCOUNTS[1],lecture_id)

    def test_completion_preserves_transcript_audio_and_removes_ticket_and_presence(self):
        lecture_id,path = self.recording()
        before = self.snapshot(("lectures","chunks","segments"))
        audio = path.read_bytes()
        self.assertEqual(self.client.post("/presence",headers=self.headers(ACCOUNTS[1]),json={"activity":"recording"}).status_code,200)
        ticket = self.client.post(f"/lectures/{lecture_id}/recording-download-ticket",headers=self.headers(ACCOUNTS[1]))
        self.assertEqual(ticket.status_code,200,ticket.text)
        code = self.issue().json()["reset_code"]
        self.assertEqual(self.complete(code).status_code,200)
        self.assertEqual(self.client.get(ticket.json()["path"]).status_code,404)
        self.assertEqual(self.snapshot(("lectures","chunks","segments")),before)
        self.assertEqual(path.read_bytes(),audio)
        overview = self.client.get("/admin/overview",headers=self.headers()).json()
        target = next(row for row in overview["accounts"] if row["label"] == ACCOUNTS[1])
        self.assertFalse(target["online"])

    def test_slow_pre_reset_ticket_request_cannot_mint_new_ticket(self):
        lecture_id,_ = self.recording()
        code = self.issue().json()["reset_code"]
        real_available = self.app.state.recording_store.available
        already_reset = False
        def reset_during_preflight(*args,**kwargs):
            nonlocal already_reset
            if not already_reset:
                already_reset = True
                self.assertEqual(self.complete(code).status_code,200)
            return real_available(*args,**kwargs)
        with mock.patch.object(self.app.state.recording_store,"available",side_effect=reset_during_preflight):
            response = self.client.post(f"/lectures/{lecture_id}/recording-download-ticket",headers=self.headers(ACCOUNTS[1]))
        self.assertEqual(response.status_code,401,response.text)


class RecoveryPrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-test-reset-primitive-")
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name)/"data"/"test.sqlite3",ACCOUNTS)
        self.database.initialize()
        with self.database.connect() as connection:
            connection.execute("UPDATE users SET password_hash='synthetic-password-hash' WHERE username=?",(ACCOUNTS[0],))

    def test_primitives_require_caller_transaction_and_active_account(self):
        with self.database.connect() as connection:
            for helper in (account_recovery.issue_password_reset,account_recovery.revoke_password_reset):
                with self.assertRaises(account_recovery.RecoveryError) as raised:
                    helper(connection,ACCOUNTS[0])
                self.assertEqual(raised.exception.code,"reset_unavailable")
            connection.execute("BEGIN IMMEDIATE")
            for name in (ACCOUNTS[1],"not-configured"):
                with self.assertRaises(account_recovery.RecoveryError) as raised:
                    account_recovery.issue_password_reset(connection,name)
                self.assertEqual(raised.exception.code,"account_not_active")
                self.assertNotIn(name,str(raised.exception))
            result = account_recovery.issue_password_reset(connection,ACCOUNTS[0],now=100)
            self.assertEqual(result["expires_at"],1900)
            # Local operator authority deliberately permits administrator self.
            self.assertEqual(result["username"],ACCOUNTS[0])
            account_recovery.revoke_password_reset(connection,ACCOUNTS[0])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM account_password_resets").fetchone()[0],0)

    def test_invalid_clock_values_and_transaction_rollback_do_not_persist(self):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for now in (True,float("nan"),float("inf"),"secret clock text"):
                with self.assertRaises(account_recovery.RecoveryError):
                    account_recovery.issue_password_reset(connection,ACCOUNTS[0],now=now)
        with self.assertRaisesRegex(RuntimeError,"synthetic abort"):
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                account_recovery.issue_password_reset(connection,ACCOUNTS[0])
                raise RuntimeError("synthetic abort")
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM account_password_resets").fetchone()[0],0)


if __name__ == "__main__":
    unittest.main()
