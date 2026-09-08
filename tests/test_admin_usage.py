from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient

from server import admin_usage
from server.admin_usage import SCOPE, UsageReader, period_bounds
from server.app import create_app
from server.db import Database
from server.security import digest
from server.settings import Settings

ACCOUNTS = ("user-alpha", "user-beta")
IDS = {ACCOUNTS[0]: "synthetic-account-alpha", ACCOUNTS[1]: "synthetic-account-beta"}
NOW = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)  # Korean noon.
TODAY = "2026-09-08T01:00:00Z"


class UsageReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-test-usage-")
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name)/"data"/"test.sqlite3", ACCOUNTS)
        self.database.initialize()
        self.reader = UsageReader(self.database, IDS)

    def lecture(self, *, owner=ACCOUNTS[0], at=TODAY, provider="qwen", finalized=True, trashed=False, deleting=False):
        identifier = str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute("INSERT INTO lectures(id,username,title,created_at,asr_provider,recording_finalized,trashed_at,deleting) "
                               "VALUES(?,?,'must-not-read-private-title',?,?,?,?,?)",
                               (identifier,owner,at,provider,int(finalized),TODAY if trashed else None,int(deleting)))
        return identifier

    def archive(self, identifier, *, seconds=None, source_bytes=None, queued_bytes=None):
        size = 44+int(seconds*32000) if seconds is not None else source_bytes
        with self.database.connect() as connection:
            connection.execute("INSERT INTO recording_archives(lecture_id,state,object_key,source_bytes,queued_bytes,updated_at) "
                               "VALUES(?,'pending',?,?,?,'now')",
                               (identifier,uuid.uuid4().hex*2,size,queued_bytes))

    def imported(self, identifier, *, owner=ACCOUNTS[0], seconds=5, status="completed", at="2000-01-01T00:00:00Z"):
        with self.database.connect() as connection:
            connection.execute("INSERT INTO imports(id,username,lecture_id,title,filename,file_fingerprint,total_bytes,uploaded_bytes,status,"
                               "duration_seconds,created_at,updated_at) VALUES(?,?,?,'private','private.wav',?,100,100,?,?,?,'now')",
                               (str(uuid.uuid4()),owner,identifier,"a"*64,status,seconds,at))

    def ai(self, identifier, feature, status="completed", *, owner=ACCOUNTS[0]):
        common = (identifier,"a"*64,status,"synthetic-model")
        with self.database.connect() as connection:
            if feature == "correction":
                connection.execute("INSERT INTO transcript_corrections(lecture_id,raw_revision,status,model,corrected_text,corrected_segments,"
                                   "uncertain_terms,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,'2000-01-01','now',?)",
                                   (*common,*(("must-not-read-private-answer","[]","[]") if status == "completed" else (None,None,None)),
                                    "now" if status == "completed" else None))
            elif feature in {"summary","translation"}:
                table,field = (("lecture_summaries","summary_json") if feature == "summary" else ("lecture_translations","translation_json"))
                connection.execute(f"INSERT INTO {table}(lecture_id,raw_revision,status,model,job_id,{field},created_at,updated_at,completed_at) "
                                   "VALUES(?,?,?,?,?,?,'2000-01-01','now',?)",
                                   (*common,str(uuid.uuid4()),'{"private":"answer"}' if status == "completed" else None,
                                    "now" if status == "completed" else None))
            else:
                connection.execute("INSERT INTO lecture_questions(id,lecture_id,username,question,request_hash,raw_revision,model,selected_ids_json,"
                                   "evidence_sha256,scope,total_segments,selected_count,status,document_json,created_at,updated_at,completed_at) "
                                   "VALUES(?,?,?,'must-not-read-private-question',?,?,'fake','[]',?,'none',0,0,?,?,'2000-01-01','now',?)",
                                   (str(uuid.uuid4()),identifier,owner,"a"*64,"b"*64,"c"*64,status,
                                    '{"private":"answer"}' if status == "completed" else None,
                                    "now" if status == "completed" else None))

    def read(self, period="month", now=NOW):
        return self.reader.read(period,now=now)

    def account(self, result, owner=ACCOUNTS[0]):
        return next(row for row in result["accounts"] if row["label"] == owner)

    def assert_balanced(self, result):
        for row in [*result["accounts"],result["totals"]]:
            self.assertEqual(row["lectures"]["active"]+row["lectures"]["trashed"],row["lectures"]["total"])
            self.assertEqual(row["recording"]["known_lectures"]+row["recording"]["unknown_lectures"],row["lectures"]["total"])
            self.assertAlmostEqual(row["recording"]["qwen_seconds"]+row["recording"]["clova_seconds"],row["recording"]["known_seconds"],places=3)
        for group in ("lectures","recording","imports"):
            for name,value in result["totals"][group].items():
                self.assertAlmostEqual(value,sum(row[group][name] for row in result["accounts"]),places=3)
        for feature,statuses in result["totals"]["ai"].items():
            for status,value in statuses.items():
                self.assertEqual(value,sum(row["ai"][feature][status] for row in result["accounts"]))

    def test_empty_users_are_zero_but_billing_explicitly_unavailable(self):
        result = self.read()
        self.assertEqual(len(result["accounts"]),2)
        self.assertEqual(result["totals"]["lectures"]["total"],0)
        self.assertEqual(result["billing"],{"available":False,"reason":"not_recorded"})
        self.assertEqual(result["scope"],SCOPE)
        self.assertEqual(result["start_at"],"2026-08-31T15:00:00Z")
        self.assertEqual(result["end_at"],"2026-09-08T03:00:00Z")
        self.assertEqual(result["generated_at"],result["end_at"])
        self.assert_balanced(result)

    def test_korean_midnight_microseconds_and_explicit_offsets(self):
        self.lecture(at="2026-09-07T14:59:59.999999Z")
        self.lecture(at="2026-09-07T15:00:00.000000Z")
        self.lecture(at="2026-09-08T00:00:00+09:00")
        self.lecture(at="2026-09-08T03:00:00Z")  # Exclusive as-of cutoff.
        self.lecture(at="2026-09-08T12:00:00Z")
        self.assertEqual(self.read("today")["totals"]["lectures"]["total"],2)
        self.assertEqual(self.read("month")["totals"]["lectures"]["total"],3)

    def test_month_boundary_all_period_and_invalid_dates(self):
        self.lecture(at="2026-08-31T14:59:59.999999Z")
        self.lecture(at="2026-08-31T15:00:00Z")
        self.lecture(at="malformed-private-value")
        self.lecture(at="2026-09-08T01:00:00")  # No authoritative timezone.
        self.assertEqual(self.read("month")["totals"]["lectures"]["total"],1)
        all_result = self.read("all")
        self.assertEqual(all_result["totals"]["lectures"]["total"],2)
        self.assertIsNone(all_result["start_at"])

    def test_trash_included_deleting_and_permanent_rows_excluded(self):
        self.lecture()
        self.lecture(trashed=True)
        self.lecture(trashed=True,deleting=True)
        removed = self.lecture()
        with self.database.connect() as connection:
            connection.execute("DELETE FROM lectures WHERE id=?",(removed,))
        result = self.read()
        self.assertEqual(result["totals"]["lectures"],{"total":2,"active":1,"trashed":1})
        self.assert_balanced(result)

    def test_duration_provider_split_one_lecture_once_and_import_fallback(self):
        qwen = self.lecture()
        clova = self.lecture(provider="clova",owner=ACCOUNTS[1],trashed=True)
        imported = self.lecture()
        unknown = self.lecture()
        self.archive(qwen,seconds=12.125)
        self.imported(qwen,seconds=99)  # Archive takes priority, never sum both.
        self.archive(clova,seconds=30)
        self.imported(imported,seconds=8.5)
        self.archive(unknown,queued_bytes=320044)  # Unverified stat is not a measured duration.
        result = self.read()
        self.assertEqual(result["totals"]["recording"],{"known_seconds":50.625,"qwen_seconds":20.625,"clova_seconds":30,
                                                       "known_lectures":3,"unknown_lectures":1})
        self.assert_balanced(result)

    def test_unfinalized_and_invalid_size_metadata_remain_unknown(self):
        live = self.lecture(finalized=False)
        self.archive(live,seconds=9)
        for size in (45,44.5,float("inf"),460800046):
            self.archive(self.lecture(),source_bytes=size)
        self.archive(self.lecture(),source_bytes=44)  # Valid zero-frame WAV.
        result = self.read()
        self.assertEqual(result["totals"]["recording"]["known_lectures"],1)
        self.assertEqual(result["totals"]["recording"]["unknown_lectures"],5)
        self.assertEqual(result["totals"]["recording"]["known_seconds"],0)
        self.assert_balanced(result)

    def test_bad_incomplete_conflicting_and_other_owner_import_durations_not_used(self):
        for seconds in (-1,float("inf"),14401,None):
            self.imported(self.lecture(),seconds=seconds)
        self.imported(self.lecture(),status="failed",seconds=10)
        self.imported(self.lecture(),owner=ACCOUNTS[1],seconds=10)
        conflict = self.lecture()
        self.imported(conflict,seconds=2)
        self.imported(conflict,seconds=3)
        result = self.read()
        self.assertEqual(result["totals"]["recording"]["known_lectures"],0)
        self.assertEqual(result["totals"]["recording"]["unknown_lectures"],7)
        self.assertEqual(result["totals"]["imports"]["completed"],6)

    def test_import_counts_follow_lecture_creation_not_job_creation_and_exclude_orphans(self):
        inside,outside = self.lecture(),self.lecture(at="2020-01-01T00:00:00Z")
        self.imported(inside,at="2000-01-01T00:00:00Z")
        self.imported(inside,status="failed")
        self.imported(inside,status="queued")
        self.imported(inside,status="cancelled")
        self.imported(outside,at=TODAY)
        self.imported(None,at=TODAY)
        self.assertEqual(self.read()["totals"]["imports"],{"completed":1,"failed":1,"pending":1,"cancelled":1})

    def test_ai_saved_state_not_calls_and_no_multi_join_multiplication(self):
        one = self.lecture()
        two = self.lecture(owner=ACCOUNTS[1],trashed=True)
        old = self.lecture(at="2020-01-01T00:00:00Z")
        self.archive(one,seconds=4)
        self.imported(one)
        self.imported(one)
        for feature in ("correction","summary","translation"):
            self.ai(one,feature)
        self.ai(two,"correction","failed")
        self.ai(two,"summary","queued")
        self.ai(two,"translation","processing")
        for status in ("completed","completed","failed","cancelled","queued"):
            self.ai(one,"question",status)
        self.ai(old,"question")
        self.ai(one,"question",owner=ACCOUNTS[1])  # Corrupt cross-owner row must not count.
        result = self.read()
        self.assertEqual(result["totals"]["lectures"]["total"],2)
        self.assertEqual(result["totals"]["recording"]["known_seconds"],4)
        self.assertEqual(result["totals"]["ai"]["question"],{"completed":2,"failed":1,"pending":1,"cancelled":1})
        self.assertEqual(result["totals"]["ai"]["correction"],{"completed":1,"failed":1,"pending":0,"cancelled":0})
        self.assertEqual(result["totals"]["ai"]["summary"]["pending"],1)
        self.assertEqual(result["totals"]["ai"]["translation"]["pending"],1)
        self.assert_balanced(result)

    def test_aggregate_queries_cannot_read_any_private_columns_or_files(self):
        one = self.lecture()
        self.imported(one)
        self.ai(one,"question")
        allowed = {
            "lectures":{"id","username","created_at","deleting","trashed_at","recording_finalized","asr_provider"},
            "recording_archives":{"lecture_id","source_bytes"},
            "imports":{"lecture_id","username","duration_seconds","status"},
            "transcript_corrections":{"lecture_id","status"},"lecture_summaries":{"lecture_id","status"},
            "lecture_translations":{"lecture_id","status"},"lecture_questions":{"lecture_id","username","status"},
        }
        original = self.database.connect
        accessed = []
        @contextmanager
        def checked_connection():
            with original() as connection:
                def authorize(action,table,column,_db,_source):
                    if action == sqlite3.SQLITE_READ and column:
                        accessed.append((table,column))
                        return sqlite3.SQLITE_OK if column in allowed.get(table,set()) else sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                connection.set_authorizer(authorize)
                yield connection
        with mock.patch.object(self.database,"connect",checked_connection):
            result = self.read()
        self.assertTrue(accessed)
        self.assertNotIn("must-not-read",str(result))
        self.assertNotIn("private.wav",str(result))
        self.assert_balanced(result)

    def test_cache_is_period_specific_bounded_and_returns_independent_values(self):
        self.lecture()
        with mock.patch("server.admin_usage.time.monotonic",return_value=100):
            first = self.read("today")
            first["totals"]["lectures"]["total"] = 999
            self.lecture()
            cached = self.read("today")
            self.assertEqual(cached["totals"]["lectures"]["total"],1)
            self.assertEqual(self.read("all")["totals"]["lectures"]["total"],2)
        with mock.patch("server.admin_usage.time.monotonic",return_value=111):
            self.assertEqual(self.read("today")["totals"]["lectures"]["total"],2)
        self.assertLessEqual(len(self.reader._cache),3)

    def test_cache_never_crosses_korean_day_or_month_midnight(self):
        before = datetime(2026,9,30,14,59,59,tzinfo=UTC)
        after = before+timedelta(seconds=2)
        self.lecture(at="2026-09-30T14:30:00Z")
        with mock.patch("server.admin_usage.time.monotonic",return_value=100):
            self.assertEqual(self.read("today",before)["totals"]["lectures"]["total"],1)
            self.assertEqual(self.read("month",before)["totals"]["lectures"]["total"],1)
            self.assertEqual(self.read("today",after)["totals"]["lectures"]["total"],0)
            self.assertEqual(self.read("month",after)["totals"]["lectures"]["total"],0)

    def test_timeout_or_database_failure_is_safe_and_never_cached_as_zero(self):
        self.lecture()
        with mock.patch("server.admin_usage.QUERY_SECONDS",-1):
            with self.assertRaises(HTTPException) as raised:
                self.read()
        self.assertEqual(raised.exception.status_code,503)
        self.assertEqual(self.reader._cache,{})
        with mock.patch.object(self.database,"connect",side_effect=sqlite3.OperationalError("private database diagnostic")):
            with self.assertRaises(HTTPException) as raised:
                self.read()
        self.assertEqual(raised.exception.status_code,503)
        self.assertNotIn("private",str(raised.exception))
        self.assertEqual(self.read()["totals"]["lectures"]["total"],1)

    def test_parallel_collection_rejects_without_starting_more_work(self):
        with self.reader._lock:
            with self.assertRaises(HTTPException) as raised:
                self.read()
        self.assertEqual(raised.exception.status_code,503)
        self.assertEqual(self.reader._cache,{})

    def test_queries_share_one_read_snapshot_during_concurrent_updates(self):
        one = self.lecture()
        self.ai(one,"summary","failed")
        original = self.database.connect
        changed = False
        class Proxy:
            def __init__(self, connection):
                self.connection = connection
            def __getattr__(self, name):
                return getattr(self.connection,name)
            def execute(self, sql, *args):
                nonlocal changed
                result = self.connection.execute(sql,*args)
                if "FROM measured GROUP BY" in sql and not changed:
                    changed = True
                    with original() as writer:
                        writer.execute("UPDATE lecture_summaries SET status='completed',summary_json='{}',completed_at='now' WHERE lecture_id=?",(one,))
                return result
        @contextmanager
        def updating_connection():
            with original() as connection:
                yield Proxy(connection)
        with mock.patch.object(self.database,"connect",updating_connection):
            result = self.read()
        self.assertTrue(changed)
        self.assertEqual(result["totals"]["ai"]["summary"]["failed"],1)
        self.assertEqual(UsageReader(self.database,IDS).read("month",now=NOW)["totals"]["ai"]["summary"]["completed"],1)


class UsagePeriodTests(unittest.TestCase):
    def test_leap_year_and_year_rollover_use_korean_calendar(self):
        start,end = period_bounds("month",datetime(2024,2,29,15,0,tzinfo=UTC))
        self.assertEqual(start,datetime(2024,2,29,15,0,tzinfo=UTC))
        start,_ = period_bounds("today",datetime(2026,12,31,15,0,tzinfo=UTC))
        self.assertEqual(start,datetime(2026,12,31,15,0,tzinfo=UTC))
        for period,now in (("bad",NOW),("today",datetime(2026,9,8))):
            with self.assertRaises(ValueError):
                period_bounds(period,now)


class UsageApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stt-test-usage-api-")
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        class FakeTranscriber:
            def status(self):
                return {"model_state":"unloaded","engine":"fake","model":"fake","device":"cpu"}
        self.app = create_app(Settings(data_dir=directory/"data",model_cache_dir=directory/"models",
                                       accounts=ACCOUNTS,admin_username=ACCOUNTS[0],
                                       site_origins=("https://student.github.io",)),FakeTranscriber())
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {}
        with self.app.state.database.connect() as connection:
            for name in ACCOUNTS:
                token = "synthetic-usage-"+name
                self.headers[name] = {"Authorization":"Bearer "+token}
                connection.execute("INSERT INTO sessions VALUES(?,?,?,?)",(digest(token),name,time.time()+3600,time.time()))

    def test_admin_only_default_month_no_store_and_unknown_period(self):
        self.assertEqual(self.client.get("/admin/usage").status_code,401)
        response = self.client.get("/admin/usage",headers=self.headers[ACCOUNTS[1]])
        self.assertEqual(response.status_code,403)
        self.assertNotIn(ACCOUNTS[0],response.text)
        response = self.client.get("/admin/usage",headers=self.headers[ACCOUNTS[0]])
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["period"],"month")
        self.assertEqual(response.headers["cache-control"],"no-store")
        self.assertEqual(self.client.get("/admin/usage?period=private-query",headers=self.headers[ACCOUNTS[0]]).status_code,422)

    def test_cors_and_cached_results_still_require_live_admin_session(self):
        headers = self.headers[ACCOUNTS[0]]|{"Origin":"https://student.github.io"}
        response = self.client.get("/admin/usage?period=today",headers=headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.headers["access-control-allow-origin"],"https://student.github.io")
        self.assertEqual(self.client.get("/admin/usage",headers=headers|{"Origin":"https://foreign.invalid"}).status_code,403)
        with self.app.state.database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE username=?",(ACCOUNTS[0],))
        self.assertEqual(self.client.get("/admin/usage?period=today",headers=headers).status_code,401)

    def test_rate_limit_applies_to_cache_hits(self):
        for _ in range(30):
            self.assertEqual(self.client.get("/admin/usage",headers=self.headers[ACCOUNTS[0]]).status_code,200)
        response = self.client.get("/admin/usage",headers=self.headers[ACCOUNTS[0]])
        self.assertEqual(response.status_code,429)
        self.assertEqual(response.headers["retry-after"],"60")
        self.assertEqual(response.headers["cache-control"],"no-store")

    def test_unavailable_is_not_an_empty_success_response(self):
        with mock.patch("server.admin_usage.UsageReader._collect",side_effect=HTTPException(503,"집계를 확인하지 못했습니다.")):
            response = self.client.get("/admin/usage",headers=self.headers[ACCOUNTS[0]])
        self.assertEqual(response.status_code,503)
        self.assertNotIn("accounts",response.json())


if __name__ == "__main__":
    unittest.main()
