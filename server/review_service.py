"""Authenticated reminder actions, atomic generation, and short-lived undo.

Pure calendar rules live in review_schedule. This module alone persists them:
all queries include the authenticated owner, all changes serialize with SQLite,
and a stale client can never overwrite a newer device's changes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
import time
import uuid
from datetime import date, datetime, timezone

from fastapi import Depends, HTTPException

from . import review_schedule as schedule

MAX_ITEMS = 5000
MAX_HISTORY = 256
MAX_ROUTINES = 200
MAX_EXAMS = 200
DEFAULT_SETTINGS = {"offsets": [1, 3, 7, 14, 30], "sem_start": None, "sort": "due"}
DEFAULT_MODE = {"sameDay": False, "nextDay": False, "eve": True, "curve": False}
ENTITY_TABLES = {"items": "review_items", "routines": "review_routines", "exams": "review_exams"}


def reject(code="invalid_input", status=400):
    raise HTTPException(status, code)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def text(value, maximum, *, required=False, multiline=False):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and not (multiline and c in "\n\t\r") for c in value):
        reject()
    value = value.strip()
    if required and not value:
        reject()
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        reject()
    return value


def boolean(value):
    if type(value) is not bool:
        reject()
    return value


def day(value, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or len(value) != 10:
        reject()
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        reject()
    if parsed.isoformat() != value or not "2000-01-01" <= value <= "2100-12-31":
        reject("date_out_of_range")
    return value


def identifier(value):
    if not isinstance(value, str):
        reject()
    try:
        if str(uuid.UUID(value)) != value:
            reject()
    except ValueError:
        reject()
    return value


def fields(payload, allowed, required=()):
    if not isinstance(payload, dict) or set(payload) - set(allowed) or set(required) - set(payload):
        reject()


def offsets(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        reject()
    result = [integer(v, 0, 365) for v in value]
    if result != sorted(set(result)):
        reject()
    return result


def mode(value):
    fields(value, DEFAULT_MODE, DEFAULT_MODE)
    result = {key: boolean(value[key]) for key in DEFAULT_MODE}
    if not any(result.values()):
        reject("mode_required")
    return result


class ReviewService:
    def __init__(self, database, *, today=None, clock=None, holidays=None, recognition=None):
        self.database = database
        self.today = today or schedule.kst_today
        self.clock = clock or time.time
        if holidays is None:
            with database.connect() as connection:
                self.holidays = dict(connection.execute("SELECT date,name FROM review_holidays"))
        else:
            self.holidays = dict(holidays)
        self.recognition = recognition

    def _timestamp(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _access(connection):
        row = connection.execute("SELECT access_enabled FROM operational_state WHERE singleton=1").fetchone()
        if row is None or not row[0]:
            reject("service_unavailable", 503)

    def _load(self, connection, username):
        profile = connection.execute("SELECT * FROM review_profiles WHERE username=?", (username,)).fetchone()
        if profile is None:
            connection.execute("INSERT INTO review_profiles(username,settings_json,updated_at) VALUES(?,?,?)",
                               (username, encoded(DEFAULT_SETTINGS), self._timestamp()))
            state = {"revision": 0, "settings": copy.deepcopy(DEFAULT_SETTINGS), "timetable": None}
        else:
            state = {"revision": profile["revision"], "settings": json.loads(profile["settings_json"]),
                     "timetable": json.loads(profile["timetable_json"]) if profile["timetable_json"] else None}
        for key, table in ENTITY_TABLES.items():
            state[key] = [json.loads(row[0]) for row in connection.execute(
                "SELECT document_json FROM " + table + " WHERE username=? ORDER BY id", (username,))]
        state["source_keys"] = sorted(self._source_keys(connection, username))
        return state

    @staticmethod
    def _source_keys(connection, username):
        return {row[0] for row in connection.execute("SELECT source_key FROM review_sources WHERE username=?", (username,))}

    def _new_item(self, item):
        item = copy.deepcopy(item)
        item.update(id=str(uuid.uuid4()), created_at=self._timestamp(), updated_at=self._timestamp())
        item.setdefault("memo", "")
        item.setdefault("subject", "")
        item.setdefault("reviews", [])
        item.setdefault("moved", None)
        item.setdefault("history", [{"date": item["learned"], "type": "learn"}])
        item.setdefault("resets", 0)
        item.setdefault("source", "timetable")
        item.setdefault("catchup", False)
        item.setdefault("skipped", None)
        item.setdefault("source_key", None)
        return item

    def _generate(self, connection, username, state, today):
        if state["timetable"] is None:
            return False
        preview = schedule.timetable_preview(state["timetable"], today, state["settings"], self.holidays,
                                             exams=state["exams"], existing_keys=self._source_keys(connection, username))
        if len(state["items"]) + len(preview["items"]) > MAX_ITEMS:
            reject("item_limit", 409)
        changed = preview["through"] != state["timetable"].get("through") or bool(preview["items"])
        state["items"].extend(self._new_item(item) for item in preview["items"])
        state["timetable"]["through"] = preview["through"]
        return changed

    def _persist(self, connection, username, before, state):
        if len(state["items"]) > MAX_ITEMS or len(state["routines"]) > MAX_ROUTINES or len(state["exams"]) > MAX_EXAMS:
            reject("record_limit", 409)
        for item in state["items"]:
            if len(item["history"]) > MAX_HISTORY:
                reject("history_limit", 409)
        state["revision"] = before["revision"] + 1
        changed = connection.execute(
            "UPDATE review_profiles SET revision=?,settings_json=?,timetable_json=?,updated_at=? WHERE username=? AND revision=?",
            (state["revision"], encoded(state["settings"]), encoded(state["timetable"]) if state["timetable"] else None,
             self._timestamp(), username, before["revision"]))
        if changed.rowcount != 1:
            reject("stale_revision", 409)
        for key, table in ENTITY_TABLES.items():
            old = {row["id"]: row for row in before[key]}
            new = {row["id"]: row for row in state[key]}
            for removed in old.keys() - new.keys():
                connection.execute("DELETE FROM " + table + " WHERE username=? AND id=?", (username, removed))
            for item_id, item in new.items():
                if old.get(item_id) == item:
                    continue
                if key == "items":
                    connection.execute("INSERT INTO review_items(username,id,source_key,document_json) VALUES(?,?,?,?) "
                                       "ON CONFLICT(username,id) DO UPDATE SET source_key=excluded.source_key,document_json=excluded.document_json",
                                       (username, item_id, item["source_key"], encoded(item)))
                    if item["source_key"]:
                        connection.execute("INSERT OR IGNORE INTO review_sources(username,source_key) VALUES(?,?)", (username, item["source_key"]))
                elif key == "exams":
                    connection.execute("INSERT INTO review_exams(username,id,subject,kind,document_json) VALUES(?,?,?,?,?) "
                                       "ON CONFLICT(username,id) DO UPDATE SET subject=excluded.subject,kind=excluded.kind,document_json=excluded.document_json",
                                       (username, item_id, item["subject"], item["kind"], encoded(item)))
                else:
                    connection.execute("INSERT INTO review_routines(username,id,document_json) VALUES(?,?,?) "
                                       "ON CONFLICT(username,id) DO UPDATE SET document_json=excluded.document_json", (username, item_id, encoded(item)))
        if connection.execute("SELECT COUNT(*) FROM review_sources WHERE username=?", (username,)).fetchone()[0] > 100000:
            reject("source_limit", 409)

    def _output(self, state, today, undo=None):
        result = copy.deepcopy(state)
        for key in ENTITY_TABLES:
            result[key].sort(key=lambda item: item["id"])
        # Include the durable ledger, including deleted generated items, so a
        # catch-up preview does not offer a source the server must reject.
        result["source_keys"] = sorted(set(state["source_keys"]) | {item["source_key"] for item in state["items"] if item["source_key"]})
        capabilities = self.recognition() if callable(self.recognition) else self.recognition
        result.update(today=today, holidays=dict(self.holidays), recognition=capabilities or {"enabled": False})
        if undo:
            result["undo"] = {"token": undo["token"], "expires_at": datetime.fromtimestamp(undo["expires_at"], timezone.utc).isoformat().replace("+00:00", "Z")}
        return result

    def timetable(self, username):
        """Read course choices without creating a profile or generating items."""
        with self.database.connect() as connection:
            connection.execute("PRAGMA query_only=ON")
            self._access(connection)
            profile = connection.execute(
                "SELECT timetable_json FROM review_profiles WHERE username=?", (username,)
            ).fetchone()
            if profile is None or profile["timetable_json"] is None:
                return {"timetable": None}
            stored = json.loads(profile["timetable_json"])
            return {"timetable": {
                "classes": [{key: row[key] for key in ("subject", "day", "start", "end", "room")} for row in stored["classes"]],
                "from": stored["from"],
                "until": stored["until"],
            }}

    def state(self, username):
        today = day(self.today())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._access(connection)
            state = self._load(connection, username)
            before = copy.deepcopy(state)
            try:
                generated = self._generate(connection, username, state, today)
            except ValueError:
                result = self._output(before, today)
                result["generation_error"] = "schedule_range_limit"
                return result
            except HTTPException as exc:
                if exc.detail != "item_limit":
                    raise
                # Capacity must not prevent opening existing records to free
                # space. The cursor and revision stay unchanged for a retry.
                result = self._output(before, today)
                result["generation_error"] = "item_limit"
                return result
            if generated:
                self._persist(connection, username, before, state)
                connection.execute("DELETE FROM review_undo WHERE username=?", (username,))
            return self._output(state, today)

    def action(self, username, body):
        fields(body, {"request_id", "revision", "action", "payload"}, {"request_id", "revision", "action", "payload"})
        request_id = identifier(body["request_id"])
        integer(body["revision"], 0, 2**53 - 2)
        if not isinstance(body["action"], str) or len(body["action"]) > 40 or not isinstance(body["payload"], dict):
            reject()
        try:
            serialized = encoded(body)
        except (TypeError, ValueError, RecursionError):
            reject()
        if len(serialized.encode("utf-8")) > 256 * 1024:
            reject("input_limit", 413)
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
        today = day(self.today())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._access(connection)
            state = self._load(connection, username)
            receipt = connection.execute("SELECT * FROM review_requests WHERE username=? AND request_id=?", (username, request_id)).fetchone()
            if receipt:
                if not secrets.compare_digest(receipt["fingerprint"], fingerprint):
                    reject("request_id_conflict", 409)
                undo = connection.execute("SELECT * FROM review_undo WHERE username=? AND token=? AND revision=? AND expires_at>?",
                                          (username, receipt["undo_token"], state["revision"], self.clock())).fetchone()
                return self._output(state, today, undo)
            if body["revision"] != state["revision"]:
                reject("stale_revision", 409)
            before = copy.deepcopy(state)
            try:
                self._apply(connection, username, state, body["action"], body["payload"], today)
                semester = state["settings"]["sem_start"] or (state["timetable"] or {}).get("from") or today
                for exam in state["exams"]:
                    self._exam_period(exam, semester, state["exams"])
            except (ValueError, KeyError, TypeError, OverflowError):
                reject("invalid_action")
            changed = state != before
            undo = None
            if changed:
                self._persist(connection, username, before, state)
                connection.execute("DELETE FROM review_undo WHERE username=?", (username,))
                if body["action"].startswith("item."):
                    old = {item["id"]: item for item in before["items"]}
                    new = {item["id"]: item for item in state["items"]}
                    snapshot = {key: old.get(key) for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
                    undo = {"token": secrets.token_urlsafe(32), "expires_at": self.clock() + 8}
                    connection.execute("INSERT INTO review_undo(username,token,revision,expires_at,snapshot_json) VALUES(?,?,?,?,?)",
                                       (username, undo["token"], state["revision"], undo["expires_at"], encoded(snapshot)))
            connection.execute("INSERT INTO review_requests(username,request_id,fingerprint,result_revision,undo_token,created_at) VALUES(?,?,?,?,?,?)",
                               (username, request_id, fingerprint, state["revision"], undo["token"] if undo else None, self.clock()))
            connection.execute("DELETE FROM review_requests WHERE username=? AND request_id NOT IN "
                               "(SELECT request_id FROM review_requests WHERE username=? ORDER BY created_at DESC,rowid DESC LIMIT 512)", (username, username))
            return self._output(state, today, undo)

    @staticmethod
    def _owned(state, key, item_id):
        identifier(item_id)
        for item in state[key]:
            if item["id"] == item_id:
                return item
        reject("not_found", 404)

    def _apply(self, connection, username, state, action, payload, today):
        if action == "undo":
            fields(payload, {"token"}, {"token"})
            token = text(payload["token"], 100, required=True)
            undo = connection.execute("SELECT * FROM review_undo WHERE username=? AND token=? AND revision=? AND expires_at>?",
                                      (username, token, state["revision"], self.clock())).fetchone()
            if undo is None:
                reject("undo_unavailable", 409)
            snapshot = json.loads(undo["snapshot_json"])
            state["items"] = [item for item in state["items"] if item["id"] not in snapshot]
            state["items"].extend(item for item in snapshot.values() if item is not None)
        elif action.startswith("item."):
            self._item(state, action[5:], payload, today)
        elif action == "settings.save":
            fields(payload, {"offsets", "sem_start", "sort"})
            if "offsets" in payload:
                state["settings"]["offsets"] = offsets(payload["offsets"])
            if "sem_start" in payload:
                state["settings"]["sem_start"] = day(payload["sem_start"], nullable=True)
            if "sort" in payload:
                if payload["sort"] not in {"due", "subject"}:
                    reject()
                state["settings"]["sort"] = payload["sort"]
        elif action.startswith("timetable."):
            self._timetable(connection, username, state, action[10:], payload, today)
        elif action.startswith("routine."):
            self._routine(state, action[8:], payload, today)
        elif action.startswith("exam."):
            self._exam(state, action[5:], payload, today)
        else:
            reject("unknown_action")

    def _item(self, state, action, payload, today):
        if action == "add":
            fields(payload, {"title", "subject", "memo", "learned", "offsets"}, {"title"})
            learned = day(payload.get("learned", today))
            if learned > today:
                reject("future_learning")
            item = self._new_item({"title": text(payload["title"], 200, required=True), "subject": text(payload.get("subject", ""), 40),
                                   "memo": text(payload.get("memo", ""), 200, multiline=True), "learned": learned, "base": learned,
                                   "offsets": offsets(payload.get("offsets", state["settings"]["offsets"])), "source": "manual"})
            state["items"].append(item)
            return
        if action == "group_review":
            fields(payload, {"ids"}, {"ids"})
            ids = payload["ids"]
            if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or len(set(ids)) != len(ids):
                reject()
            items = [self._owned(state, "items", value) for value in ids]
            if len({item["subject"] for item in items}) != 1:
                reject("group_subject_mismatch")
            items.sort(key=lambda item: (schedule.next_due(item) or "9999-12-31", item["learned"], item["id"]))
            for item in items:
                if schedule.next_due(item) is None or schedule.next_due(item) > today:
                    reject("review_not_due")
            for item in items:
                self._item(state, "review", {"id": item["id"]}, today)
            return
        allowed = {"id", "date"} if action == "move" else {"id", "stage", "date"} if action == "edit_review" else {"id"}
        fields(payload, allowed, allowed)
        item = self._owned(state, "items", payload["id"])
        if action == "delete":
            state["items"].remove(item)
            return
        if action == "review":
            if schedule.next_due(item) is None:
                reject("review_not_due")
            changed = schedule.complete_review(item, today)
        elif action == "reset":
            changed = schedule.reset_item(item, today)
        elif action == "skip":
            changed = schedule.skip_item(item, today)
        elif action == "unskip":
            changed = schedule.unskip_item(item, today)
        elif action == "move":
            changed = schedule.place_review(item, day(payload["date"]), today)
        elif action == "edit_review":
            changed = schedule.edit_review_date(item, integer(payload["stage"], 0, 11), day(payload["date"]), today)
        elif action == "remove_review":
            changed = schedule.undo_last_review(item)
        else:
            reject("unknown_action")
        if changed != item:
            changed["updated_at"] = self._timestamp()
            state["items"][state["items"].index(item)] = changed

    def _timetable(self, connection, username, state, action, payload, today):
        old = state["timetable"]
        if action == "disable":
            fields(payload, set())
            state["timetable"] = None
            return
        if action == "save":
            fields(payload, {"classes", "from", "until", "mode"}, {"classes", "from"})
            classes = payload["classes"]
            if not isinstance(classes, list) or not 1 <= len(classes) <= 60:
                reject()
            cleaned, seen = [], set()
            for row in classes:
                fields(row, {"subject", "day", "start", "end", "room"}, {"subject", "day"})
                subject = text(row["subject"], 40, required=True)
                if "|" in subject:
                    reject("invalid_subject")
                result = {"subject": subject, "day": integer(row["day"], 0, 6), "room": text(row.get("room", ""), 120)}
                for key in ("start", "end"):
                    value = row.get(key, "")
                    if not isinstance(value, str) or value and (len(value) != 5 or value[2] != ":" or not value[:2].isascii() or not value[:2].isdigit() or not value[3:].isascii() or not value[3:].isdigit() or int(value[:2]) > 23 or int(value[3:]) > 59):
                        reject("invalid_time")
                    result[key] = value
                if result["start"] and result["end"] and result["end"] <= result["start"]:
                    reject("invalid_time")
                key = (subject, result["day"], result["start"])
                if key in seen:
                    reject("duplicate_class")
                seen.add(key)
                cleaned.append(result)
            start = day(payload["from"])
            until = day(payload.get("until"), nullable=True)
            if until and until < start:
                reject("invalid_period")
            through = max(old.get("through") or "", schedule.add_days(start, -1)) if old else schedule.add_days(start, -1)
            state["timetable"] = {"classes": cleaned, "from": start, "until": until, "through": through,
                                  "mode": mode(payload.get("mode", old["mode"] if old else DEFAULT_MODE))}
            if not state["settings"]["sem_start"]:
                state["settings"]["sem_start"] = min(start[:8] + "01", start)
            self._generate(connection, username, state, today)
            return
        if old is None:
            reject("timetable_required", 409)
        if action == "mode":
            fields(payload, {"mode", "apply_existing"}, {"mode"})
            old["mode"] = mode(payload["mode"])
            if boolean(payload.get("apply_existing", False)):
                for item in state["items"]:
                    if item["source"] != "timetable" or item["skipped"] or len(item["reviews"]) >= len(item["offsets"]):
                        continue
                    item["offsets"] = (schedule.catchup_offsets(old, state["settings"]) if item["catchup"] else
                                       schedule.offsets_for(item["subject"], item["learned"], old, state["settings"], self.holidays))
                    if old["mode"]["eve"] and item["catchup"] and not item["reviews"]:
                        item["base"] = schedule.eve_target(item["subject"], today, old, self.holidays)
                        item["moved"] = None
                    item["updated_at"] = self._timestamp()
            return
        if action == "catchup":
            fields(payload, {"sem_start", "source_keys", "per_day"}, {"sem_start", "source_keys"})
            start = day(payload["sem_start"])
            keys = payload["source_keys"]
            if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_ITEMS or any(not isinstance(key, str) or len(key) > 120 for key in keys) or len(set(keys)) != len(keys):
                reject()
            per_day = payload.get("per_day", 5)
            if type(per_day) is not int or per_day not in {0, 3, 5, 8, 12}:
                reject()
            rows = schedule.catchup_preview(old, start, today, state["settings"], self.holidays,
                                             existing_keys=self._source_keys(connection, username), exams=state["exams"])
            by_key = {row["source_key"]: row for row in rows}
            if any(key not in by_key or by_key[key]["existing"] for key in keys):
                reject("catchup_selection_changed", 409)
            wanted = set(keys)
            for row in rows:
                row["selected"] = row["source_key"] in wanted
            planned = schedule.catchup_plan(rows, old, today, state["settings"], self.holidays, per_day=per_day)
            if len(planned) != len(keys) or len(state["items"]) + len(planned) > MAX_ITEMS:
                reject("item_limit", 409)
            state["items"].extend(self._new_item(item) for item in planned)
            state["settings"]["sem_start"] = start
            old["from"] = min(old["from"], start)
            return
        reject("unknown_action")

    def _routine(self, state, action, payload, today):
        if action == "add":
            fields(payload, {"name", "tag", "days", "start", "end", "count", "numbered", "num_start"}, {"name", "start"})
            days = payload.get("days", [])
            if not isinstance(days, list) or len(days) > 7 or any(type(v) is not int or not 0 <= v <= 6 for v in days) or len(set(days)) != len(days):
                reject()
            start, end = day(payload["start"]), day(payload.get("end"), nullable=True)
            if end and end < start:
                reject("invalid_period")
            state["routines"].append({"id": str(uuid.uuid4()), "name": text(payload["name"], 60, required=True), "tag": text(payload.get("tag", ""), 40),
                                      "days": sorted(days), "start": start, "end": end, "count": integer(payload.get("count", 0), 0, 10000),
                                      "numbered": boolean(payload.get("numbered", False)), "num_start": integer(payload.get("num_start", 1), 1, 1000000),
                                      "done": [], "created_at": self._timestamp(), "updated_at": self._timestamp()})
            return
        allowed = {"id", "date", "checked"} if action == "check" else {"id"}
        fields(payload, allowed, allowed)
        routine = self._owned(state, "routines", payload["id"])
        if action == "delete":
            state["routines"].remove(routine)
        elif action == "stop":
            routine["end"] = schedule.add_days(today, -1)
        elif action == "check":
            target, checked = day(payload["date"]), boolean(payload["checked"])
            if target > today or not schedule.routine_n(routine, target):
                reject("routine_not_available")
            values = set(routine["done"])
            values.add(target) if checked else values.discard(target)
            if len(values) > 10000:
                reject("history_limit", 409)
            routine["done"] = sorted(values)
        else:
            reject("unknown_action")
        routine["updated_at"] = self._timestamp()

    def _exam(self, state, action, payload, today):
        if action == "save":
            fields(payload, {"subject", "kind", "date", "rounds", "lead"}, {"subject", "kind", "date"})
            subject = text(payload["subject"], 40, required=True)
            known = {item["subject"] for item in state["items"]}
            if state["timetable"]:
                known.update(row["subject"] for row in state["timetable"]["classes"])
            if subject not in known or payload["kind"] not in {"mid", "final"}:
                reject("invalid_subject_or_kind")
            exam_date, rounds = day(payload["date"]), integer(payload.get("rounds", 3), 1, 5)
            lead = integer(payload.get("lead", 7), 3, 14)
            if lead not in {3, 5, 7, 10, 14}:
                reject()
            for other in state["exams"]:
                if other["subject"] == subject and other["kind"] != payload["kind"]:
                    if (payload["kind"] == "final" and exam_date <= other["date"]) or (payload["kind"] == "mid" and exam_date >= other["date"]):
                        reject("exam_order")
            exam = next((row for row in state["exams"] if row["subject"] == subject and row["kind"] == payload["kind"]), None)
            if exam is None:
                exam = {"id": str(uuid.uuid4()), "subject": subject, "kind": payload["kind"], "done": {}, "created_at": self._timestamp()}
                state["exams"].append(exam)
            exam.update(date=exam_date, rounds=rounds, lead=lead, updated_at=self._timestamp())
            return
        allowed = {"id", "session_key", "round", "checked"} if action == "check" else {"id"}
        fields(payload, allowed, allowed)
        exam = self._owned(state, "exams", payload["id"])
        if action == "delete":
            state["exams"].remove(exam)
            return
        if action != "check":
            reject("unknown_action")
        key, checked = text(payload["session_key"], 120, required=True), boolean(payload["checked"])
        round_index = integer(payload["round"], 0, exam["rounds"] - 1)
        semester = state["settings"]["sem_start"] or (state["timetable"] or {}).get("from") or today
        sessions = schedule.exam_sessions(exam, semester, state["timetable"], self.holidays,
                                          exams=state["exams"], items=state["items"], today=today)
        session = next((row for row in sessions if row["key"] == key), None)
        if session is None or session["skipped"] or session["date"] > today:
            reject("exam_session_unavailable")
        values = list(exam["done"].get(key, []))
        values.extend([False] * max(0, exam["rounds"] - len(values)))
        values[round_index] = checked
        exam["done"][key] = values
        if len(exam["done"]) > MAX_ITEMS:
            reject("history_limit", 409)
        exam["updated_at"] = self._timestamp()

    @staticmethod
    def _exam_period(exam, semester, exams):
        period = schedule.exam_range(exam, semester, exams)
        span = schedule.diff_days(period["start"], period["end"])
        if span > 3660:
            reject("exam_range_limit")


def install_review(app, database, *, identity, recognition=None, **options):
    service = ReviewService(database, recognition=recognition, **options)

    @app.get("/review/timetable")
    def review_timetable(user: dict = Depends(identity)):
        # A read-only session may inspect its own classes; no profile, cursor,
        # generated item, revision, or undo record is changed on this route.
        return service.timetable(user["username"])

    @app.get("/review/state")
    def review_state(user: dict = Depends(identity)):
        if user.get("read_only"):
            reject("read_only", 403)
        return service.state(user["username"])

    @app.post("/review/actions")
    def review_action(body: dict, user: dict = Depends(identity)):
        if user.get("read_only"):
            reject("read_only", 403)
        return service.action(user["username"], body)

    app.state.review_service = service
    return service
