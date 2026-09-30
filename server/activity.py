"""Bounded operational activity, without content, URLs or credential storage.

Browser observations are untrusted hints and disappear on restart. Session
hashes are used only as in-memory keys and to check the existing sessions table;
they are never written to the activity ledger or returned to administrators.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
import threading
import time

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

log = logging.getLogger("classroom.activity")
TTL_SECONDS = 120
IDLE_SECONDS = 300
MAX_TABS_PER_SESSION = 8
RETENTION_SECONDS = 7 * 86400
MAX_EVENTS = 500
PRESENCE_ACTIONS = frozenset({
    "viewing", "recording", "uploading", "transcribing", "correcting",
    "reminder", "admin", "paused", "summarizing", "translating", "questioning",
    "study_notes", "course_review", "materials",
})
ACTIONS = PRESENCE_ACTIONS | {
    "lecture_created", "recording_saved", "recording_finished", "file_import",
    "import_cancelled", "timetable_recognition", "reminder_changed", "note_saved",
}
RESULTS = frozenset({"observed", "requested", "completed"})
TASKS = ("recording", "uploading", "transcribing", "correcting", "summarizing",
         "translating", "questioning", "study_notes", "course_review", "materials")
JOB_ACTIONS = {
    "transcription": "transcribing", "imports": "transcribing",
    "corrections": "correcting", "summaries": "summarizing",
    "translations": "translating", "questions": "questioning",
    "study_notes": "study_notes", "course_reviews": "course_review", "materials": "materials",
}
ROUTE_ACTIONS = {
    ("POST", "/lectures"): ("lecture_created", "completed"),
    # Audio chunks/parts are intentionally absent: an ongoing recording must
    # not replace all recent human actions with repetitive transfer events.
    ("POST", "/lectures/{lecture_id}/recording-finalize"): ("recording_finished", "completed"),
    ("POST", "/imports"): ("uploading", "requested"),
    ("POST", "/imports/{import_id}/complete"): ("file_import", "requested"),
    ("POST", "/imports/{import_id}/cancel"): ("import_cancelled", "requested"),
    ("POST", "/lectures/{lecture_id}/correction"): ("correcting", "requested"),
    ("POST", "/lectures/{lecture_id}/summary"): ("summarizing", "requested"),
    ("POST", "/lectures/{lecture_id}/translation"): ("translating", "requested"),
    ("POST", "/lectures/{lecture_id}/questions"): ("questioning", "requested"),
    ("POST", "/lectures/{lecture_id}/study-note"): ("study_notes", "requested"),
    ("POST", "/courses/{course_id}/reviews"): ("course_review", "requested"),
    # Material PUTs save only one part, not a completed conversion.
    ("POST", "/study-materials/{material_id}/convert"): ("materials", "requested"),
    ("POST", "/review/actions"): ("reminder_changed", "completed"),
    ("POST", "/review/timetable/parse"): ("timetable_recognition", "completed"),
    ("POST", "/lectures/{lecture_id}/manual"): ("note_saved", "completed"),
}


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def migrate_activity_schema(connection):
    # These SQL fragments contain only source-code allowlists, never input.
    actions = ",".join("'" + action + "'" for action in sorted(ACTIONS))
    connection.execute(f"""CREATE TABLE IF NOT EXISTS user_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
        timestamp TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ({actions})),
        result TEXT NOT NULL CHECK(result IN ('observed','requested','completed'))
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS user_activity_recent ON user_activity(timestamp DESC,id DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS user_activity_owner ON user_activity(username,timestamp DESC,id DESC)")
    prune_activity(connection, time.time())


def prune_activity(connection, now):
    connection.execute("DELETE FROM user_activity WHERE timestamp < ?", (timestamp(now - RETENTION_SECONDS),))
    connection.execute("DELETE FROM user_activity WHERE id NOT IN "
                       "(SELECT id FROM user_activity ORDER BY timestamp DESC,id DESC LIMIT ?)", (MAX_EVENTS,))


@dataclass(frozen=True)
class Observation:
    username: str
    activity: str
    seen: float
    interaction: float


class ActivityTracker:
    def __init__(self, database, *, clock=None):
        self.database = database
        self.clock = clock or time.time
        self.lock = threading.Lock()
        self.observations = {}

    def _live_sessions(self, connection, now):
        return {(row["username"], row["token_hash"]) for row in connection.execute(
            "SELECT username,token_hash FROM sessions WHERE expires_at > ?", (now,))}

    def _prune(self, live, now):
        for key, row in list(self.observations.items()):
            if now - row.seen > TTL_SECONDS or (row.username, key[0]) not in live:
                self.observations.pop(key, None)

    def observe(self, user, activity, tab_id=None, idle_seconds=None):
        if activity not in PRESENCE_ACTIONS | {"idle", "away"}:
            raise ValueError("invalid activity")
        if tab_id is not None and not re.fullmatch(r"[0-9a-f]{32}", tab_id):
            raise ValueError("invalid tab")
        if idle_seconds is not None and (type(idle_seconds) is not int or not 0 <= idle_seconds <= 86400):
            raise ValueError("invalid idle duration")
        now = self.clock()
        key = (user["token_hash"], tab_id or "legacy")
        with self.lock:
            with self.database.connect() as connection:
                live = self._live_sessions(connection, now)
            self._prune(live, now)
            if (user["username"], user["token_hash"]) not in live:
                raise HTTPException(401, "로그인이 만료되었습니다. 다시 로그인하세요.", headers={"WWW-Authenticate": "Bearer"})
            if key not in self.observations and sum(k[0] == key[0] for k in self.observations) >= MAX_TABS_PER_SESSION:
                raise HTTPException(429, "동시에 표시할 수 있는 탭 수를 넘었습니다.", headers={"Retry-After": "120"})
            previous = self.observations.get(key)
            if idle_seconds is not None:
                interaction = now - idle_seconds
            elif activity == "away":
                # Old clients reported away on hide. Preserve the last input
                # estimate, allowing a full five-minute grace for a first hint.
                interaction = previous.interaction if previous else now
            elif activity == "idle":
                interaction = min(previous.interaction, now - IDLE_SECONDS) if previous else now - IDLE_SECONDS
            else:
                interaction = now
            normalized = "viewing" if activity in {"idle", "away"} else activity
            self.observations[key] = Observation(user["username"], normalized, now, interaction)
            changed = previous is None or previous.activity != normalized
        if changed and activity in PRESENCE_ACTIONS and now - interaction < IDLE_SECONDS:
            self.record(user, activity, "observed")

    def remove(self, *, username=None, session=None):
        with self.lock:
            for key, row in list(self.observations.items()):
                if (username is not None and row.username == username) or (session is not None and key[0] == session):
                    self.observations.pop(key, None)

    def record(self, user, action, result):
        if action not in ACTIONS or result not in RESULTS:
            return
        try:
            now = self.clock()
            with self.database.connect() as connection:
                # Activity logging must never hold up a real user action behind
                # a long SQLite writer. Dropped metadata is safer than failure.
                connection.execute("PRAGMA busy_timeout = 100")
                connection.execute("BEGIN IMMEDIATE")
                if (user["username"], user["token_hash"]) not in self._live_sessions(connection, now):
                    return
                prune_activity(connection, now)
                # Deduplicate heartbeat transitions and rapid part retries,
                # without reading any request ID, audio or lesson identifier.
                delay = 30 if result == "observed" else 10
                recent = connection.execute("SELECT 1 FROM user_activity WHERE username=? AND action=? AND result=? "
                                            "AND timestamp >= ? LIMIT 1",
                                            (user["username"], action, result, timestamp(now - delay))).fetchone()
                if not recent:
                    connection.execute("INSERT INTO user_activity(username,timestamp,action,result) VALUES(?,?,?,?)",
                                       (user["username"], timestamp(now), action, result))
                    prune_activity(connection, now)
        except Exception:
            # No exception text, account, headers or request content in logs.
            log.warning("Activity metadata could not be saved")

    def snapshot(self):
        now = self.clock()
        with self.lock:
            with self.database.connect() as connection:
                live = self._live_sessions(connection, now)
            self._prune(live, now)
            snapshot = list(self.observations.values())
        try:
            with self.database.connect() as connection:
                connection.execute("PRAGMA busy_timeout = 100")
                prune_activity(connection, now)
                recent = [dict(row) for row in connection.execute(
                    "SELECT username,timestamp,action,result FROM user_activity ORDER BY timestamp DESC,id DESC LIMIT ?", (MAX_EVENTS,))]
        except Exception:
            log.warning("Activity metadata could not be read")
            recent = []
        return snapshot, recent

    def account(self, username, observations, recent, jobs):
        now = self.clock()
        rows = [row for row in observations if row.username == username]
        server_tasks = list(dict.fromkeys(value for key, value in JOB_ACTIONS.items() if jobs.get(key, 0) > 0))
        client_tasks = [task for task in TASKS if any(row.activity == task for row in rows)]
        pages = list(dict.fromkeys(row.activity for row in sorted(rows, key=lambda r: r.interaction, reverse=True)
                                  if now - row.interaction < IDLE_SECONDS and row.activity not in TASKS))
        activities = list(dict.fromkeys(server_tasks + client_tasks + pages))
        online = bool(rows)
        idle = online and not server_tasks and not client_tasks and all(now - row.interaction >= IDLE_SECONDS for row in rows)
        action = next((row for row in recent if row["username"] == username), None)
        interaction = max((row.interaction for row in rows), default=None)
        last_activity = timestamp(interaction) if interaction is not None else None
        if action and (last_activity is None or action["timestamp"] > last_activity):
            last_activity = action["timestamp"]
        return {"online": online, "activity": activities[0] if activities else "away" if idle else "offline",
                "activities": activities, "idle": idle,
                "last_seen_at": timestamp(max(row.seen for row in rows)) if rows else None,
                "last_activity_at": last_activity,
                "last_action": action["action"] if action else None,
                "last_action_at": action["timestamp"] if action else None}


class ActivityMiddleware:
    """Observe successful known operations, never request bodies or raw paths."""
    def __init__(self, app, tracker):
        self.app = app
        self.tracker = tracker

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = None

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await self.app(scope, receive, capture)
        event = ROUTE_ACTIONS.get((scope.get("method"), getattr(scope.get("route"), "path", None)))
        actor = scope.get("state", {}).get("activity_actor")
        if event and actor and status is not None and 200 <= status < 300:
            try:
                await run_in_threadpool(self.tracker.record, actor, *event)
            except Exception:
                log.warning("Activity metadata could not be saved")
