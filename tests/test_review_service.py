"""Synthetic reminder API, concurrency, ownership, and migration regressions."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from server.db import Database
from server.review_service import ReviewService, install_review
from server import review_schedule as schedule


CLASS_ROWS = [
    {"subject": "창업과공동체", "day": 2, "start": "13:30", "end": "16:15", "room": "S4115"},
    {"subject": "글로벌문화", "day": 2, "start": "16:30", "end": "17:45", "room": "S4509"},
    {"subject": "글로벌문화", "day": 4, "start": "16:30", "end": "17:45", "room": "S4509"},
]
CURVE = {"sameDay": True, "nextDay": True, "eve": False, "curve": True}


class ReviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-reminder-")
        self.database = Database(Path(self.temporary.name) / "private" / "test.sqlite3", ("alpha", "beta"))
        self.database.initialize()
        self.days = ["2026-09-29"]
        self.times = [1000000.0]
        self.app = FastAPI()

        def identity(authorization: str | None = Header(default=None)):
            identities = {"Bearer synthetic-alpha": {"username": "alpha"}, "Bearer synthetic-beta": {"username": "beta"},
                          "Bearer synthetic-readonly": {"username": "alpha", "read_only": True}}
            if authorization not in identities:
                raise HTTPException(401, "unauthorized")
            return identities[authorization]

        self.service = install_review(self.app, self.database, identity=identity,
                                      today=lambda: self.days[0], clock=lambda: self.times[0])
        self.client = TestClient(self.app)
        self.state = self.get()

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def headers(self, owner="alpha"):
        return {"Authorization": "Bearer synthetic-" + owner}

    def get(self, owner="alpha"):
        response = self.client.get("/review/state", headers=self.headers(owner))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def request(self, action, payload=None, *, revision=None, owner="alpha", request_id=None):
        return {"request_id": request_id or str(uuid.uuid4()), "revision": self.state["revision"] if revision is None else revision,
                "action": action, "payload": payload or {}}

    def send(self, body, owner="alpha"):
        return self.client.post("/review/actions", json=body, headers=self.headers(owner))

    def act(self, action, payload=None, *, expected=200, owner="alpha", revision=None):
        response = self.send(self.request(action, payload, revision=revision), owner)
        self.assertEqual(response.status_code, expected, response.text)
        if expected == 200 and owner == "alpha":
            self.state = response.json()
        return response.json()

    def item(self, title="합성 복습", subject="합성 과목", learned="2026-09-28", offsets=None):
        previous = {row["id"] for row in self.state["items"]}
        self.act("item.add", {"title": title, "subject": subject, "learned": learned, "offsets": offsets or [1, 3, 7]})
        return next(row["id"] for row in self.state["items"] if row["id"] not in previous)

    def timetable(self, start="2026-09-29", mode=None):
        return self.act("timetable.save", {"classes": CLASS_ROWS, "from": start, "until": "2026-12-31", "mode": mode or CURVE})

    def row(self, identifier):
        return next(row for row in self.state["items"] if row["id"] == identifier)

    def test_defaults_holidays_kst_and_recognition(self):
        self.assertEqual(self.state["revision"], 0)
        self.assertEqual(self.state["today"], "2026-09-29")
        self.assertEqual(self.state["settings"]["offsets"], [1, 3, 7, 14, 30])
        self.assertIsNone(self.state["timetable"])
        self.assertEqual(self.state["recognition"], {"enabled": False})
        self.assertIn("2026-10-05", self.state["holidays"])
        self.assertNotIn("2026-05-01", self.state["holidays"])
        self.service.recognition = lambda: {"enabled": True}
        self.assertTrue(self.get()["recognition"]["enabled"])

    def test_timetable_read_does_not_create_missing_profile(self):
        with self.database.connect() as connection:
            before = connection.execute("SELECT COUNT(*) FROM review_profiles WHERE username='beta'").fetchone()[0]
        self.assertEqual(before, 0)
        response = self.client.get("/review/timetable", headers=self.headers("beta"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"timetable": None})
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_profiles WHERE username='beta'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_items WHERE username='beta'").fetchone()[0], 0)

    def test_timetable_read_uses_only_owner_and_returns_no_review_metadata(self):
        self.timetable()
        self.item()
        response = self.client.get("/review/timetable", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"timetable": {"classes": CLASS_ROWS, "from": "2026-09-29", "until": "2026-12-31"}})
        other = self.client.get("/review/timetable?username=alpha", headers=self.headers("beta"))
        self.assertEqual(other.status_code, 200)
        self.assertEqual(other.json(), {"timetable": None})
        self.act("timetable.disable")
        self.assertEqual(self.client.get("/review/timetable", headers=self.headers()).json(), {"timetable": None})

    def test_timetable_read_is_side_effect_free_even_when_generation_is_due(self):
        self.timetable()
        self.item()
        self.days[0] = "2026-10-01"
        tables = ("review_profiles", "review_items", "review_sources", "review_requests", "review_undo")

        def snapshot():
            with self.database.connect() as connection:
                return {table: [tuple(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")] for table in tables}

        before = snapshot()
        with patch.object(self.service, "_load", side_effect=AssertionError("must not create a profile")), patch.object(self.service, "_generate", side_effect=AssertionError("must not generate items")):
            for owner in ("alpha", "readonly"):
                response = self.client.get("/review/timetable", headers=self.headers(owner))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["timetable"]["classes"], CLASS_ROWS)
        self.assertEqual(snapshot(), before)
        # The fixture really has pending generation; only the existing full
        # state route advances the cursor and adds the next class.
        after_generation = self.get()
        self.assertGreater(len(after_generation["items"]), len(self.state["items"]))

    def test_timetable_read_keeps_authentication_and_data_access_guards(self):
        self.assertEqual(self.client.get("/review/timetable").status_code, 401)
        with self.database.connect() as connection:
            connection.execute("UPDATE operational_state SET access_enabled=0")
        self.assertEqual(self.client.get("/review/timetable", headers=self.headers()).status_code, 503)
        self.assertEqual(self.client.get("/review/timetable", headers=self.headers("readonly")).status_code, 503)
        self.assertEqual(self.client.get("/review/timetable").status_code, 401)

    def test_add_review_undo_and_reset_preserve_history(self):
        identifier = self.item()
        self.act("item.review", {"id": identifier})
        self.assertEqual(self.row(identifier)["reviews"], ["2026-09-29"])
        self.assertEqual(schedule.next_due(self.row(identifier)), "2026-10-01")
        self.act("undo", {"token": self.state["undo"]["token"]})
        self.assertEqual(self.row(identifier)["reviews"], [])
        self.act("item.reset", {"id": identifier})
        self.assertEqual(self.row(identifier)["base"], "2026-09-29")
        self.assertEqual(self.row(identifier)["history"][-1]["type"], "reset")

    def test_lost_response_request_is_exactly_once_and_undo_replayed(self):
        body = self.request("item.add", {"title": "synthetic"})
        first, second = self.send(body), self.send(body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.get()["items"]), 1)
        self.state = first.json()
        self.act("settings.save", {"sort": "subject"})
        replay = self.send(body).json()
        self.assertEqual(replay["revision"], self.state["revision"])
        self.assertNotIn("undo", replay)
        self.assertEqual(len(replay["items"]), 1)

    def test_request_id_reuse_with_different_body_rejected(self):
        body = self.request("item.add", {"title": "synthetic"})
        self.assertEqual(self.send(body).status_code, 200)
        body["payload"]["title"] = "different"
        self.assertEqual(self.send(body).status_code, 409)
        self.assertEqual(len(self.get()["items"]), 1)

    def test_stale_revision_preserves_newer_device_changes(self):
        stale = self.request("settings.save", {"sort": "subject"})
        self.item()
        self.assertEqual(self.send(stale).status_code, 409)
        self.assertEqual(self.get()["settings"]["sort"], "due")

    def test_two_simultaneous_same_requests_write_once(self):
        body = self.request("item.add", {"title": "synthetic simultaneous"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            states = list(pool.map(lambda _: self.service.action("alpha", body), range(2)))
        self.assertEqual(states[0], states[1])
        self.assertEqual(len(self.get()["items"]), 1)

    def test_other_account_cannot_read_change_or_undo(self):
        identifier = self.item()
        token = self.state["undo"]["token"]
        beta = self.get("beta")
        self.assertEqual(beta["items"], [])
        for action in ("item.review", "item.delete", "item.reset"):
            self.assertEqual(self.send(self.request(action, {"id": identifier}, revision=beta["revision"]), "beta").status_code, 404)
        self.assertEqual(self.send(self.request("undo", {"token": token}, revision=0), "beta").status_code, 409)
        self.assertEqual(len(self.get()["items"]), 1)

    def test_auth_and_readonly_sessions_cannot_generate_or_mutate(self):
        self.assertEqual(self.client.get("/review/state").status_code, 401)
        self.assertEqual(self.client.post("/review/actions", json=self.request("item.add", {"title": "x"})).status_code, 401)
        self.assertEqual(self.client.get("/review/state", headers=self.headers("readonly")).status_code, 403)
        self.assertEqual(self.send(self.request("item.add", {"title": "x"}), "readonly").status_code, 403)
        self.assertEqual(self.get()["revision"], 0)

    def test_disabled_service_blocks_generation_and_actions(self):
        with self.database.connect() as connection:
            connection.execute("UPDATE operational_state SET access_enabled=0")
        self.assertEqual(self.client.get("/review/state", headers=self.headers()).status_code, 503)
        self.assertEqual(self.send(self.request("item.add", {"title": "x"})).status_code, 503)

    def test_undo_expires_and_stale_tokens_never_restore_prior_device_state(self):
        identifier = self.item()
        token = self.state["undo"]["token"]
        self.times[0] += 8
        self.act("undo", {"token": token}, expected=409)
        self.act("item.review", {"id": identifier})
        token = self.state["undo"]["token"]
        self.act("settings.save", {"sort": "subject"})
        self.act("undo", {"token": token}, expected=409)
        self.assertEqual(self.row(identifier)["reviews"], ["2026-09-29"])

    def test_all_item_undo_operations_restore_exact_item(self):
        identifier = self.item()
        for action, payload in (("item.skip", {}), ("item.reset", {}), ("item.move", {"date": "2026-10-02"}), ("item.delete", {})):
            before = copy.deepcopy(self.row(identifier))
            self.act(action, {"id": identifier, **payload})
            self.act("undo", {"token": self.state["undo"]["token"]})
            self.assertEqual(self.row(identifier), before)

    def test_skip_unskip_does_not_generate_duplicate(self):
        self.timetable()
        item = self.state["items"][0]
        self.act("item.skip", {"id": item["id"]})
        self.assertIsNone(schedule.next_due(self.row(item["id"])))
        self.assertEqual(len(self.get()["items"]), 2)
        self.act("item.unskip", {"id": item["id"]})
        self.assertEqual(schedule.next_due(self.row(item["id"])), "2026-09-29")

    def test_generation_is_atomic_concurrent_and_deleted_source_is_not_recreated(self):
        self.days[0] = "2026-09-28"
        self.timetable()
        self.assertEqual(len(self.state["items"]), 0)
        revision = self.state["revision"]
        self.days[0] = "2026-09-29"
        with ThreadPoolExecutor(max_workers=4) as pool:
            states = list(pool.map(lambda _: self.service.state("alpha"), range(4)))
        self.assertTrue(all(len(result["items"]) == 2 and result["revision"] == revision + 1 for result in states))
        self.state = states[0]
        removed = self.state["items"][0]
        self.act("item.delete", {"id": removed["id"]})
        self.assertIn(removed["source_key"], self.state["source_keys"])
        self.act("timetable.disable")
        self.timetable()
        self.assertEqual(len(self.state["items"]), 1)
        self.assertNotIn(removed["source_key"], {item["source_key"] for item in self.state["items"]})
        self.assertIn(removed["source_key"], self.get()["source_keys"])
        self.assertEqual(self.get("beta")["source_keys"], [])

    def test_generated_day_change_invalidates_old_undo(self):
        self.days[0] = "2026-09-28"
        self.timetable()
        self.item(learned="2026-09-27")
        token = self.state["undo"]["token"]
        self.days[0] = "2026-09-29"
        self.state = self.get()
        self.act("undo", {"token": token}, expected=409)

    def test_timetable_edit_never_silently_backfills_and_preserves_items_on_disable(self):
        self.timetable()
        existing = {row["id"] for row in self.state["items"]}
        self.timetable(start="2026-09-01")
        self.assertEqual({row["id"] for row in self.state["items"]}, existing)
        self.act("timetable.disable")
        self.assertIsNone(self.state["timetable"])
        self.assertEqual({row["id"] for row in self.state["items"]}, existing)

    def test_default_timetable_mode_is_eve_only(self):
        self.act("timetable.save", {"classes": CLASS_ROWS, "from": "2026-09-29"})
        self.assertEqual(self.state["timetable"]["mode"], {"sameDay": False, "nextDay": False, "eve": True, "curve": False})

    def test_catchup_selection_revalidated_and_atomic_zero_writes_on_bad_key(self):
        self.timetable()
        rows = schedule.catchup_preview(self.state["timetable"], "2026-09-01", self.state["today"], self.state["settings"], self.state["holidays"], self.state["source_keys"])
        keys = [row["source_key"] for row in rows if row["selected"]]
        before = self.get()
        self.act("timetable.catchup", {"sem_start": "2026-09-01", "source_keys": keys + ["not-an-occurrence"], "per_day": 5}, expected=409)
        self.assertEqual(self.get(), before)
        self.act("timetable.catchup", {"sem_start": "2026-09-01", "source_keys": keys, "per_day": 5})
        self.assertEqual(len(self.state["items"]), len(keys) + 2)
        self.assertEqual(self.state["timetable"]["from"], "2026-09-01")
        self.assertEqual(self.state["timetable"]["through"], "2026-09-29")
        self.assertEqual(sum(item["base"] == "2026-09-29" for item in self.state["items"] if item["catchup"]), min(5, len(keys)))

    def test_repeated_catchup_adds_only_explicit_holiday_and_preserves_three_prior_records(self):
        self.act("timetable.save", {"classes": [CLASS_ROWS[-1]], "from": self.days[0], "mode": CURVE})

        def preview():
            return schedule.catchup_preview(self.state["timetable"], "2026-09-01", self.days[0],
                                            self.state["settings"], self.state["holidays"], self.state["source_keys"])

        selected = [row["source_key"] for row in preview() if row["selected"]]
        self.assertEqual(len(selected), 3)
        self.act("timetable.catchup", {"sem_start": "2026-09-01", "source_keys": selected, "per_day": 0})
        self.assertEqual(len(self.state["items"]), 3)
        self.assertEqual(self.state["timetable"]["from"], "2026-09-01")
        self.assertEqual(self.state["timetable"]["through"], self.days[0])
        self.act("item.review", {"id": self.state["items"][0]["id"]})
        originals = copy.deepcopy(self.state["items"])
        self.assertEqual(len(schedule.due_items(originals, self.days[0])), 2)
        again = preview()
        self.assertEqual(len(again), 4)
        self.assertTrue(all(row["existing"] and not row["selected"] for row in again[:3]))
        holiday = again[-1]
        self.assertEqual((holiday["date"], holiday["holiday"], holiday["existing"], holiday["selected"]),
                         ("2026-09-24", "추석", False, False))
        payload = {"sem_start": "2026-09-01", "source_keys": [holiday["source_key"]], "per_day": 0}
        before = self.get()
        self.act("timetable.catchup", {**payload, "source_keys": [selected[0], holiday["source_key"]]}, expected=409)
        self.assertEqual(self.get(), before, "a stale mixed selection must not partially insert its new holiday")
        body = self.request("timetable.catchup", payload)
        response = self.send(body)
        self.assertEqual(response.status_code, 200)
        self.state = response.json()
        self.assertEqual(len(self.state["items"]), 4)
        self.assertEqual([self.row(row["id"]) for row in originals], originals)
        added = next(row for row in self.state["items"] if row["source_key"] == holiday["source_key"])
        self.assertEqual((added["learned"], added["reviews"]), ("2026-09-24", []))
        self.assertEqual(len(schedule.due_items(self.state["items"], self.days[0])), 3)
        self.assertEqual(sum(len(group["items"]) for group in schedule.group_due_items(self.state["items"], self.days[0])), 3)
        self.assertEqual(self.send(body).json()["items"], self.state["items"], "a lost-response replay creates nothing twice")
        before = self.get()
        self.act("timetable.catchup", payload, expected=409)
        self.assertEqual(self.get(), before)
        self.act("item.review", {"id": added["id"]})
        self.assertEqual(len(schedule.due_items(self.state["items"], self.days[0])), 2)
        self.act("undo", {"token": self.state["undo"]["token"]})
        self.assertEqual(self.state["items"], before["items"])
        self.act("item.delete", {"id": added["id"]})
        undo = self.state["undo"]["token"]
        self.assertEqual(self.state["items"], originals)
        tombstone = next(row for row in preview() if row["source_key"] == holiday["source_key"])
        self.assertTrue(tombstone["existing"])
        self.assertFalse(tombstone["selected"])
        self.act("timetable.catchup", payload, expected=409)
        self.act("undo", {"token": undo})
        self.assertEqual(self.state["items"], before["items"])
        self.assertEqual(len(self.get()["items"]), 4)

    def test_today_catchup_and_automatic_generation_race_creates_one_source_once(self):
        self.days[0] = "2026-09-28"
        self.act("timetable.save", {"classes": [CLASS_ROWS[0]], "from": "2026-09-29", "mode": CURVE})
        self.assertEqual(self.state["items"], [])
        self.days[0] = "2026-09-29"
        rows = schedule.catchup_preview(self.state["timetable"], self.days[0], self.days[0],
                                        self.state["settings"], self.state["holidays"], self.state["source_keys"])
        self.assertEqual(len(rows), 1)
        payload = {"sem_start": self.days[0], "source_keys": [rows[0]["source_key"]], "per_day": 0}
        body = self.request("timetable.catchup", payload)
        start = Barrier(2)

        def generate():
            start.wait(timeout=10)
            return self.service.state("alpha")

        def catchup():
            start.wait(timeout=10)
            return self.send(body)

        with ThreadPoolExecutor(max_workers=2) as pool:
            generation = pool.submit(generate)
            action = pool.submit(catchup)
            generated, response = generation.result(timeout=15), action.result(timeout=15)
        self.assertEqual(len(generated["items"]), 1)
        self.assertIn(response.status_code, (200, 409))
        if response.status_code == 409:
            self.assertEqual(response.json()["detail"], "stale_revision")
        self.state = self.get()
        self.assertEqual(len(self.state["items"]), 1)
        self.assertEqual(self.state["source_keys"], payload["source_keys"])
        self.assertEqual(self.state["timetable"]["through"], self.days[0])
        self.assertEqual(len(schedule.due_items(self.state["items"], self.days[0])), 1)
        before = copy.deepcopy(self.state)
        self.act("timetable.catchup", payload, expected=409)
        self.assertEqual(self.get(), before)

    def test_catchup_can_explicitly_include_holiday_and_use_early_learning_date(self):
        self.timetable(mode={"sameDay": False, "nextDay": False, "eve": True, "curve": False})
        rows = schedule.catchup_preview(self.state["timetable"], "2026-09-01", self.state["today"], self.state["settings"], self.state["holidays"])
        holiday = next(row for row in rows if row["date"] == "2026-09-24")
        self.assertFalse(holiday["selected"])
        self.act("timetable.catchup", {"sem_start": "2026-09-01", "source_keys": [holiday["source_key"]]})
        item = next(item for item in self.state["items"] if item["catchup"])
        self.assertEqual(item["offsets"], [0])
        self.act("item.move", {"id": item["id"], "date": "2026-09-24"})
        self.assertEqual(self.row(item["id"])["reviews"], ["2026-09-24"])
        self.act("item.edit_review", {"id": item["id"], "stage": 0, "date": "2026-09-23"}, expected=400)
        self.act("item.remove_review", {"id": item["id"]})
        self.assertEqual(self.row(item["id"])["reviews"], [])

    def test_mode_apply_preserves_reviews_and_only_changes_unfinished_timetable(self):
        manual = self.item()
        self.timetable()
        item = next(row for row in self.state["items"] if row["source"] == "timetable")
        self.act("item.review", {"id": item["id"]})
        self.act("timetable.mode", {"mode": {"sameDay": False, "nextDay": False, "eve": True, "curve": False}, "apply_existing": True})
        self.assertEqual(self.row(item["id"])["reviews"], ["2026-09-29"])
        self.assertEqual(self.row(manual)["offsets"], [1, 3, 7])

    def test_group_review_undo_is_atomic_and_foreign_or_future_ids_reject_everything(self):
        identifiers = [self.item(title=str(index)) for index in range(7)]
        other_ids = [self.item(title="다른 과목 " + str(index), subject="별도 합성 과목") for index in range(2)]
        before = self.get()
        self.act("item.group_review", {"ids": identifiers + [str(uuid.uuid4())]}, expected=404)
        self.assertEqual(self.get(), before)
        self.act("item.group_review", {"ids": identifiers})
        self.assertEqual(sum(row["reviews"] == ["2026-09-29"] for row in self.state["items"]), 7)
        self.assertTrue(all(self.row(item_id)["reviews"] == [] for item_id in other_ids))
        self.act("undo", {"token": self.state["undo"]["token"]})
        self.assertEqual(self.state["items"], before["items"])
        self.assertEqual(len(self.state["items"]), 9)

    def test_eve_mode_apply_clears_previous_move_for_unreviewed_catchup(self):
        self.timetable()
        rows = schedule.catchup_preview(self.state["timetable"], "2026-09-01", self.state["today"], self.state["settings"], self.state["holidays"])
        source = next(row["source_key"] for row in rows if row["subject"] == "글로벌문화" and row["selected"])
        self.act("timetable.catchup", {"sem_start": "2026-09-01", "source_keys": [source], "per_day": 5})
        identifier = next(row["id"] for row in self.state["items"] if row["catchup"])
        self.act("item.move", {"id": identifier, "date": "2026-10-10"})
        before = copy.deepcopy(self.row(identifier))
        self.act("timetable.mode", {"mode": {"sameDay": False, "nextDay": False, "eve": True, "curve": False}, "apply_existing": True})
        current = self.row(identifier)
        self.assertEqual(current["base"], "2026-09-30")
        self.assertIsNone(current["moved"])
        self.assertEqual(schedule.next_due(current), "2026-09-30")
        for key in ("id", "learned", "reviews", "history", "source_key"):
            self.assertEqual(current[key], before[key])

    def test_past_completion_edit_last_removal_and_earliest_date(self):
        identifier = self.item(learned="2026-09-20")
        self.act("item.move", {"id": identifier, "date": "2026-09-19"}, expected=400)
        self.act("item.move", {"id": identifier, "date": "2026-09-21"})
        self.assertEqual(schedule.next_due(self.row(identifier)), "2026-09-23")
        self.act("item.move", {"id": identifier, "date": "2026-09-25"})
        self.assertEqual(schedule.next_due(self.row(identifier)), "2026-09-29")
        self.act("item.edit_review", {"id": identifier, "stage": 0, "date": "2026-09-26"}, expected=400)
        self.act("item.edit_review", {"id": identifier, "stage": 0, "date": "2026-09-20"})
        self.act("item.remove_review", {"id": identifier})
        self.assertEqual(self.row(identifier)["reviews"], ["2026-09-20"])

    def test_routine_check_guards_day_and_owner_stop_keeps_history(self):
        self.act("routine.add", {"name": "합성 읽기", "days": [2, 4], "start": "2026-09-29", "count": 6, "numbered": True})
        identifier = self.state["routines"][0]["id"]
        self.act("routine.check", {"id": identifier, "date": "2026-10-01", "checked": True}, expected=400)
        self.act("routine.check", {"id": identifier, "date": "2026-09-28", "checked": True}, expected=400)
        self.act("routine.check", {"id": identifier, "date": "2026-09-29", "checked": True})
        self.assertEqual(self.state["routines"][0]["done"], ["2026-09-29"])
        self.act("routine.stop", {"id": identifier})
        self.assertEqual(self.state["routines"][0]["end"], "2026-09-28")
        self.assertEqual(self.state["routines"][0]["done"], ["2026-09-29"])
        self.assertEqual(self.send(self.request("routine.delete", {"id": identifier}, revision=0), "beta").status_code, 404)

    def test_exam_rounds_preserve_marks_and_validate_current_session(self):
        self.timetable()
        self.act("exam.save", {"subject": "글로벌문화", "kind": "mid", "date": "2026-10-06", "rounds": 3, "lead": 7})
        exam = self.state["exams"][0]
        sessions = schedule.exam_sessions(exam, "2026-09-01", self.state["timetable"], self.state["holidays"], items=self.state["items"], today=self.days[0])
        key = sessions[0]["key"]
        self.act("exam.check", {"id": exam["id"], "session_key": key, "round": 0, "checked": True})
        marks = copy.deepcopy(self.state["exams"][0]["done"])
        self.act("exam.save", {"subject": "글로벌문화", "kind": "mid", "date": "2026-10-07", "rounds": 2, "lead": 5})
        self.assertEqual(self.state["exams"][0]["done"], marks)
        self.act("exam.save", {"subject": "글로벌문화", "kind": "final", "date": "2026-10-07"}, expected=400)
        self.act("exam.check", {"id": exam["id"], "session_key": "invalid", "round": 0, "checked": True}, expected=400)
        future = next(row for row in sessions if row["date"] > self.days[0])
        self.act("exam.check", {"id": exam["id"], "session_key": future["key"], "round": 0, "checked": True}, expected=400)

    def test_strict_validation_rejects_unbounded_or_coerced_payloads_without_mutation(self):
        cases = [
            ("item.add", {"title": "x" * 201}), ("item.add", {"title": "x", "owner": "beta"}),
            ("item.add", {"title": "x", "offsets": [True]}), ("item.add", {"title": "x", "offsets": [2, 1]}),
            ("item.add", {"title": "x", "learned": "2026-02-30"}), ("item.add", {"title": "x", "learned": "1999-12-31"}),
            ("settings.save", {"offsets": list(range(13))}), ("settings.save", {"sort": []}),
            ("routine.add", {"name": "x", "start": "2026-09-01", "days": [True]}),
            ("routine.add", {"name": "x", "start": "2026-09-01", "count": -1}),
            ("timetable.save", {"from": "2026-09-29", "classes": [{"subject": "x", "day": 2, "start": "24:00"}]}),
            ("timetable.save", {"from": "2026-09-29", "classes": [{"subject": "x", "day": 2, "start": "13:00", "end": "12:00"}]}),
            ("timetable.save", {"from": "2026-09-29", "classes": CLASS_ROWS * 21}),
            ("timetable.save", {"from": "2026-09-29", "classes": CLASS_ROWS, "mode": dict.fromkeys(CURVE, False)}),
        ]
        for action, payload in cases:
            with self.subTest(action=action, payload_keys=list(payload)):
                self.act(action, payload, expected=400)
                self.assertEqual(self.get()["revision"], 0)
        body = self.request("settings.save", {"sort": "subject"})
        body["revision"] = True
        self.assertEqual(self.send(body).status_code, 400)

    def test_item_limit_rolls_back_profile_and_request_receipt(self):
        with patch("server.review_service.MAX_ITEMS", 1):
            self.item()
            revision = self.state["revision"]
            self.act("item.add", {"title": "over limit"}, expected=409)
            self.assertEqual(self.get()["revision"], revision)
            self.assertEqual(len(self.get()["items"]), 1)

    def test_generation_limit_keeps_existing_items_accessible_and_cursor_retryable(self):
        self.days[0] = "2026-09-28"
        self.timetable()
        identifier = self.item(learned="2026-09-27")
        revision = self.state["revision"]
        self.days[0] = "2026-09-29"
        with patch("server.review_service.MAX_ITEMS", 1):
            result = self.get()
            self.assertEqual(result["generation_error"], "item_limit")
            self.assertEqual(result["revision"], revision)
            self.assertEqual(result["timetable"]["through"], "2026-09-28")
            self.assertEqual([row["id"] for row in result["items"]], [identifier])
        result = self.get()
        self.assertNotIn("generation_error", result)
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(result["revision"], revision + 1)

    def test_exam_range_rejected_before_saving_unrenderable_state(self):
        self.timetable()
        self.act("exam.save", {"subject": "글로벌문화", "kind": "mid", "date": "2100-01-01"}, expected=400)
        self.act("exam.save", {"subject": "글로벌문화", "kind": "mid", "date": "2026-10-06"})
        self.act("settings.save", {"sem_start": "2000-01-01"}, expected=400)
        self.assertEqual(self.get()["settings"]["sem_start"], "2026-09-01")

    def test_manual_only_exam_uses_same_optional_semester_fallback_as_ui(self):
        identifier = self.item(learned="2026-09-29")
        self.assertIsNone(self.state["settings"]["sem_start"])
        self.act("exam.save", {"subject": "합성 과목", "kind": "mid", "date": "2100-01-01"}, expected=400)
        self.act("exam.save", {"subject": "합성 과목", "kind": "mid", "date": "2026-10-06"})
        exam = self.state["exams"][0]
        self.act("exam.check", {"id": exam["id"], "session_key": identifier, "round": 0, "checked": True})
        self.assertEqual(self.state["exams"][0]["done"][identifier], [True, False, False])
        self.act("exam.save", {"subject": "합성 과목", "kind": "mid", "date": "2026-08-01"})
        self.assertEqual(self.state["exams"][0]["done"][identifier], [True, False, False])

    def test_recognized_unknown_start_and_room_are_valid_without_dropping_fields(self):
        row = {"subject": "합성", "day": 2, "start": "", "end": "17:45", "room": "r" * 120}
        self.act("timetable.save", {"classes": [row], "from": "2026-09-29"})
        self.assertEqual(self.state["timetable"]["classes"], [row])

    def test_early_review_and_multiline_memo_are_supported(self):
        self.act("item.add", {"title": "synthetic", "memo": "line one\nline two", "learned": "2026-09-29", "offsets": [1, 3]})
        identifier = self.state["items"][0]["id"]
        self.assertEqual(self.row(identifier)["memo"], "line one\nline two")
        self.act("item.review", {"id": identifier})
        self.assertEqual(self.row(identifier)["reviews"], ["2026-09-29"])

    def test_bulk_storage_failure_rolls_back_items_profile_tombstones_and_receipt(self):
        with self.database.connect() as connection:
            connection.execute("CREATE TRIGGER synthetic_fail BEFORE INSERT ON review_items WHEN (SELECT COUNT(*) FROM review_items)>0 BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        body = self.request("timetable.save", {"classes": CLASS_ROWS, "from": "2026-09-29", "mode": CURVE})
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.action("alpha", body)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_items").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_sources").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_requests").fetchone()[0], 0)
        self.assertIsNone(self.get()["timetable"])
        self.assertEqual(self.get()["revision"], 0)

    def test_schema24_additive_reinitialization_preserves_legacy_values(self):
        with self.database.connect() as connection:
            connection.execute("UPDATE users SET password_hash='synthetic-preserved',setup_hash='synthetic-setup',setup_expires=123 WHERE username='alpha'")
            before = [tuple(row) for row in connection.execute("SELECT * FROM users ORDER BY username")]
            connection.execute("PRAGMA user_version=23")
        self.database.initialize()
        with self.database.connect() as connection:
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM users ORDER BY username")], before)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 25)
            self.assertIsNone(connection.execute("PRAGMA foreign_key_check").fetchone())

    def test_account_deletion_cascades_every_reminder_table_only_for_owner(self):
        self.item()
        self.timetable()
        self.act("routine.add", {"name": "synthetic", "start": "2026-09-29"})
        self.act("exam.save", {"subject": "글로벌문화", "kind": "mid", "date": "2026-10-06"})
        self.item(title="undo snapshot")
        beta = self.service.action("beta", self.request("item.add", {"title": "beta synthetic"}, revision=0))
        with self.database.connect() as connection:
            connection.execute("INSERT INTO review_parse_usage VALUES('alpha','2026-09-29',1)")
            connection.execute("INSERT INTO review_parse_leases VALUES('alpha','synthetic',123)")
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'review_%' AND name!='review_holidays'")]
            connection.execute("DELETE FROM users WHERE username='alpha'")
            for table in tables:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM " + table + " WHERE username='alpha'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_items WHERE username='beta'").fetchone()[0], len(beta["items"]))
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM review_holidays").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
