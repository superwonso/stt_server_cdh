"""Synthetic activity metadata: no model, real account or network."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from server.activity import ActivityMiddleware, ActivityTracker, timestamp
from server.db import Database


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-activity-")
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "private" / "test.sqlite3", ("alpha", "beta"))
        self.database.initialize()
        self.now = [1900000000.0]
        self.tracker = ActivityTracker(self.database, clock=lambda: self.now[0])
        self.user = {"username": "alpha", "token_hash": "synthetic-private-session-one"}
        self.other_session = {"username": "alpha", "token_hash": "synthetic-private-session-two"}
        with self.database.connect() as connection:
            for user in (self.user, self.other_session):
                connection.execute("INSERT INTO sessions(token_hash,username,created_at,expires_at) VALUES(?,?,?,?)",
                                   (user["token_hash"], user["username"], self.now[0], self.now[0] + 86400))

    def account(self, jobs=None):
        observations, events = self.tracker.snapshot()
        return self.tracker.account("alpha", observations, events, jobs or {})

    def test_multiple_tabs_keep_active_recording_and_separate_input_from_heartbeat(self):
        self.tracker.observe(self.user, "reminder", "a" * 32, 0)
        first = self.account()["last_activity_at"]
        self.now[0] += 20
        self.tracker.observe(self.user, "reminder", "a" * 32, 20)
        self.tracker.observe(self.user, "viewing", "b" * 32, 600)
        current = self.account()
        self.assertEqual(current["activity"], "reminder")
        self.assertFalse(current["idle"])
        self.assertEqual(current["last_activity_at"], first)
        self.assertNotEqual(current["last_seen_at"], first)
        self.tracker.observe(self.user, "recording", "c" * 32, 600)
        self.assertEqual(self.account()["activity"], "recording")
        self.now[0] += 100
        self.tracker.observe(self.user, "paused", "c" * 32, 700)
        self.tracker.observe(self.user, "reminder", "a" * 32, 700)
        self.assertTrue(self.account()["idle"])
        self.assertEqual(self.account()["activity"], "away")
        jobs = self.account({"study_notes": 1, "imports": 1})
        self.assertFalse(jobs["idle"])
        self.assertEqual(jobs["activities"], ["transcribing", "study_notes"])

    def test_legacy_hidden_grace_idle_threshold_and_session_boundaries(self):
        self.tracker.observe(self.user, "away")
        self.assertEqual(self.account()["activity"], "viewing")
        for age in (100, 200, 299):
            self.now[0] = 1900000000 + age
            self.tracker.observe(self.user, "away")
            self.assertFalse(self.account()["idle"])
        self.now[0] += 1
        self.tracker.observe(self.user, "away")
        self.assertTrue(self.account()["idle"])
        self.tracker.observe(self.other_session, "reminder", "d" * 32, 0)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (self.user["token_hash"],))
        self.tracker.remove(session=self.user["token_hash"])
        self.assertEqual(self.account()["activities"], ["reminder"])
        with self.assertRaises(HTTPException) as raised:
            self.tracker.observe(self.user, "recording", "f" * 32, 0)
        self.assertEqual(raised.exception.status_code, 401)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE username='alpha'")
        self.assertFalse(self.account()["online"])
        self.tracker.record(self.other_session, "study_notes", "requested")
        self.assertNotEqual(self.account()["last_action"], "study_notes")

    def test_ttl_restart_and_max_tabs_do_not_persist_presence_or_credentials(self):
        for index in range(8):
            self.tracker.observe(self.user, "viewing", f"{index:032x}", 0)
        with self.assertRaises(HTTPException) as raised:
            self.tracker.observe(self.user, "viewing", "f" * 32, 0)
        self.assertEqual(raised.exception.status_code, 429)
        self.tracker.observe(self.other_session, "reminder", "e" * 32, 0)
        self.assertTrue(self.account()["online"])
        self.now[0] += 120
        self.assertTrue(self.account()["online"])
        self.now[0] += 0.001
        self.assertFalse(self.account()["online"])
        self.assertEqual(self.account()["last_action"], "reminder")
        self.tracker = ActivityTracker(self.database, clock=lambda: self.now[0])
        self.assertFalse(self.account()["online"])
        events = self.tracker.snapshot()[1]
        self.assertEqual(len(events), 2, "periodic heartbeat and tabs do not flood the durable ledger")
        serialized = json.dumps(events)
        for secret in (self.user["token_hash"], self.other_session["token_hash"], "e" * 32):
            self.assertNotIn(secret, serialized)
        with self.database.connect() as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(user_activity)")]
            self.assertEqual(columns, ["id", "username", "timestamp", "action", "result"])
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 27)

    def test_retention_is_seven_days_and_500_entries_without_touching_admin_audit(self):
        with self.database.connect() as connection:
            connection.execute("INSERT INTO admin_audit(timestamp,action,result,target) VALUES(?,?,?,?)",
                               (timestamp(self.now[0] - 9 * 86400), "access_changed", "success", "service"))
            connection.execute("INSERT INTO user_activity(username,timestamp,action,result) VALUES(?,?,?,?)",
                               ("alpha", timestamp(self.now[0] - 8 * 86400), "viewing", "observed"))
            connection.executemany("INSERT INTO user_activity(username,timestamp,action,result) VALUES(?,?,?,?)",
                                   [("alpha", timestamp(self.now[0] - index), "reminder_changed", "completed") for index in range(501)])
        events = self.tracker.snapshot()[1]
        self.assertEqual(len(events), 500)
        self.assertTrue(all(row["action"] == "reminder_changed" for row in events))
        self.assertEqual(events[0]["timestamp"], timestamp(self.now[0]))
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM admin_audit").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM user_activity").fetchone()[0], 500)
            for column, value in (("action", "PRIVATE-CONTENT"), ("result", "PRIVATE-CONTENT")):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(f"UPDATE user_activity SET {column}=?", (value,))
        self.now[0] += 7 * 86400 + 1
        self.assertEqual(self.tracker.snapshot()[1], [])

    def test_metadata_storage_failure_never_leaks_exception_or_changes_success_response(self):
        app = FastAPI()
        app.add_middleware(ActivityMiddleware, tracker=self.tracker)

        @app.post("/lectures")
        def create(request: Request):
            request.state.activity_actor = self.user
            return {"status": "created"}

        with TestClient(app) as client:
            with patch.object(self.database, "connect", side_effect=RuntimeError("PRIVATE-KEY-AND-CONTENT")):
                with self.assertLogs("classroom.activity", level="WARNING") as logs:
                    response = client.post("/lectures", json={"title": "PRIVATE-TITLE"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "created"})
        self.assertNotIn("PRIVATE", " ".join(logs.output))

    def test_route_classification_uses_only_authenticated_success_and_resolved_template(self):
        app = FastAPI()
        app.add_middleware(ActivityMiddleware, tracker=self.tracker)

        @app.post("/lectures/{lecture_id}/study-note", status_code=202)
        def request_note(lecture_id: str, request: Request):
            if request.headers.get("Authorization"):
                request.state.activity_actor = self.user
            if lecture_id == "rejected":
                raise HTTPException(403, "denied")
            return {"private": "PRIVATE-RESPONSE-CONTENT"}

        @app.post("/unclassified/{content}")
        def unclassified(content: str, request: Request):
            request.state.activity_actor = self.user
            return {"status": "ok"}

        @app.put("/study-materials/{material_id}/content")
        def upload_part(material_id: str, request: Request):
            request.state.activity_actor = self.user
            return {"uploaded_bytes": 1024}

        with TestClient(app) as client:
            client.post("/lectures/anonymous/study-note")
            client.post("/lectures/rejected/study-note", headers={"Authorization": "synthetic"})
            client.post("/unclassified/PRIVATE-PATH")
            client.put("/study-materials/PRIVATE-MATERIAL-ID/content", content=b"synthetic-part")
            self.assertEqual(self.tracker.snapshot()[1], [], "a saved upload part must not be reported as a completed conversion")
            client.post("/lectures/PRIVATE-LECTURE-ID/study-note?secret=PRIVATE-QUERY",
                        json={"secret": "PRIVATE-BODY"}, headers={"Authorization": "synthetic"})
        events = self.tracker.snapshot()[1]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["action"], events[0]["result"]), ("study_notes", "requested"))
        self.assertNotIn("PRIVATE", json.dumps(events))


if __name__ == "__main__":
    unittest.main()
