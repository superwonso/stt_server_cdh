"""Bounded administrator-only inventory metrics, not a billing ledger.

Every count uses the same retained-lecture creation cohort. No transcript,
question, password, file name, audio, or Drive identifier is read, and no
external provider is contacted.
"""
from __future__ import annotations

import copy
import math
import sqlite3
import threading
import time
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Query

KST = ZoneInfo("Asia/Seoul")
QUERY_SECONDS = 2.0
CACHE_SECONDS = 10.0
MAX_RECORDING_SECONDS = 14_400
SCOPE = {
    "basis": "retained_lectures",
    "date_field": "lecture_created_at",
    "includes_trashed": True,
    "excludes_permanently_deleted": True,
    "excludes_deleting": True,
    "ai_counts": "latest_saved_state_except_question_rows",
    "imports": "linked_retained_lectures",
    "duration": "finalized_archive_or_completed_import_metadata",
}
FEATURE_TABLES = (
    ("correction", "transcript_corrections"), ("summary", "lecture_summaries"),
    ("translation", "lecture_translations"), ("question", "lecture_questions"),
    ("study_note", "lecture_study_notes"),
)


def _utcnow():
    return datetime.now(UTC)


def _stamp(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def period_bounds(period: str, now: datetime | None = None) -> tuple[datetime | None, datetime]:
    current = now if now is not None else _utcnow()
    if period not in {"today", "month", "all"} or current.tzinfo is None:
        raise ValueError("invalid usage period")
    local = current.astimezone(KST)
    start = None if period == "all" else local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        start = start.replace(day=1)
    return start.astimezone(UTC) if start else None, current.astimezone(UTC)


def _created_epoch(value):
    # Avoid SQLite's millisecond date rounding at the Korean midnight boundary.
    # All app timestamps are timezone-aware; malformed legacy values fail closed.
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _empty_statuses():
    return {"completed": 0, "failed": 0, "pending": 0, "cancelled": 0}


def _empty_metrics():
    return {
        "lectures": {"total": 0, "active": 0, "trashed": 0},
        "recording": {"known_seconds": 0.0, "qwen_seconds": 0.0, "clova_seconds": 0.0,
                      "known_lectures": 0, "unknown_lectures": 0},
        "imports": _empty_statuses(),
        "ai": {feature: _empty_statuses() for feature, _ in FEATURE_TABLES},
    }


def _unavailable():
    return HTTPException(503, "사용량 집계를 확인하지 못했습니다. 잠시 후 다시 시도하세요.",
                         headers={"Retry-After": "10"})


class UsageReader:
    def __init__(self, database, account_ids):
        self.database = database
        self.account_ids = dict(account_ids)
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, dict]] = {}

    def read(self, period: str, *, now: datetime | None = None):
        current = now if now is not None else _utcnow()
        start, end = period_bounds(period, current)
        key = (period, current.astimezone(KST).date().isoformat())
        if not self._lock.acquire(blocking=False):
            # Never queue an unbounded set of administrator SQL workers.
            raise _unavailable()
        try:
            monotonic = time.monotonic()
            cached = self._cache.get(key)
            if cached is not None and monotonic < cached[0]:
                return copy.deepcopy(cached[1])
            document = self._collect(period, start, end)
            # At most one current-day entry per fixed period. Errors never
            # substitute old numbers or cache an artificial zero response.
            self._cache = {old_key: value for old_key, value in self._cache.items()
                           if old_key[1] == key[1] and value[0] > monotonic}
            self._cache[key] = (time.monotonic() + CACHE_SECONDS, document)
            return copy.deepcopy(document)
        finally:
            self._lock.release()

    def _collect(self, period, start, end):
        accounts = {name: {"account_id": opaque, "label": name, **_empty_metrics()}
                    for name, opaque in self.account_ids.items()}
        placeholders = ",".join("?" for _ in accounts)
        date_clause = " AND usage_epoch(l.created_at)>=?" if start else ""
        cohort = (
            "WITH cohort AS (SELECT l.id,l.username,l.asr_provider,l.recording_finalized,l.trashed_at "
            "FROM lectures l WHERE l.deleting=0 AND l.username IN (" + placeholders + ") "
            "AND usage_epoch(l.created_at)<?" + date_clause + ") "
        )
        parameters = (*accounts, end.timestamp(), *((start.timestamp(),) if start else ()))
        deadline = time.monotonic() + QUERY_SECONDS

        def check():
            if time.monotonic() >= deadline:
                raise _unavailable()

        try:
            with self.database.connect() as connection:
                connection.execute("PRAGMA busy_timeout=100")
                connection.execute("PRAGMA query_only=ON")
                connection.create_function("usage_epoch", 1, _created_epoch, deterministic=True)
                connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                try:
                    connection.execute("BEGIN")
                    # source_bytes is captured after RecordingStore.info's
                    # canonical 44-byte / 16kHz mono PCM validation. queued_bytes
                    # is only a stat snapshot, so deliberately not used here.
                    recording_sql = cohort.rstrip() + ", imported AS (" \
                        "SELECT i.lecture_id,CASE WHEN COUNT(*)=SUM(typeof(i.duration_seconds) IN ('integer','real') " \
                        "AND i.duration_seconds BETWEEN 0 AND 14400) " \
                        "AND MIN(i.duration_seconds)=MAX(i.duration_seconds) THEN MAX(i.duration_seconds) END AS seconds " \
                        "FROM imports i JOIN cohort c ON c.id=i.lecture_id AND c.username=i.username " \
                        "WHERE i.status='completed' GROUP BY i.lecture_id), measured AS (" \
                        "SELECT c.*,CASE WHEN c.recording_finalized=1 THEN CASE " \
                        "WHEN typeof(a.source_bytes) IN ('integer','real') " \
                        "AND a.source_bytes BETWEEN 44 AND 460800044 " \
                        "AND CAST(a.source_bytes AS INTEGER)=a.source_bytes " \
                        "AND (a.source_bytes-44)%2=0 THEN (a.source_bytes-44)/32000.0 " \
                        "ELSE i.seconds END END AS seconds FROM cohort c " \
                        "LEFT JOIN recording_archives a ON a.lecture_id=c.id " \
                        "LEFT JOIN imported i ON i.lecture_id=c.id) " \
                        "SELECT username,COUNT(*) AS total,SUM(trashed_at IS NOT NULL) AS trashed," \
                        "SUM(seconds IS NOT NULL) AS known," \
                        "COALESCE(SUM(CASE WHEN asr_provider='qwen' THEN seconds END),0) AS qwen," \
                        "COALESCE(SUM(CASE WHEN asr_provider='clova' THEN seconds END),0) AS clova " \
                        "FROM measured GROUP BY username"
                    for row in connection.execute(recording_sql, parameters):
                        check()
                        value = accounts[row["username"]]
                        value["lectures"] = {"total": row["total"], "active": row["total"]-row["trashed"], "trashed": row["trashed"]}
                        qwen, clova = round(row["qwen"], 3), round(row["clova"], 3)
                        if not all(math.isfinite(seconds) and seconds >= 0 for seconds in (qwen, clova)):
                            raise _unavailable()
                        value["recording"] = {"known_seconds": round(qwen+clova, 3), "qwen_seconds": qwen,
                                              "clova_seconds": clova, "known_lectures": row["known"],
                                              "unknown_lectures": row["total"]-row["known"]}
                    check()
                    for row in connection.execute(cohort +
                            "SELECT c.username,i.status,COUNT(*) AS amount FROM imports i JOIN cohort c "
                            "ON c.id=i.lecture_id AND c.username=i.username GROUP BY c.username,i.status", parameters):
                        status = "pending" if row["status"] in {"uploading", "queued", "processing"} else row["status"]
                        accounts[row["username"]]["imports"][status] += row["amount"]
                    check()
                    branches = []
                    for feature, table in FEATURE_TABLES:
                        owner = " AND t.username=c.username" if feature in {"question", "study_note"} else ""
                        branches.append("SELECT c.username,'" + feature + "' AS feature,t.status,COUNT(*) AS amount "
                                        "FROM " + table + " t JOIN cohort c ON c.id=t.lecture_id" + owner +
                                        " GROUP BY c.username,t.status")
                    for row in connection.execute(cohort + " UNION ALL ".join(branches), parameters):
                        status = "pending" if row["status"] in {"queued", "processing"} else row["status"]
                        accounts[row["username"]]["ai"][row["feature"]][status] += row["amount"]
                    check()
                finally:
                    connection.set_progress_handler(None, 0)
        except sqlite3.Error:
            # Do not reflect SQL/database paths or return partial aggregates.
            raise _unavailable() from None
        totals = _empty_metrics()
        for account in accounts.values():
            for group in ("lectures", "recording", "imports"):
                for field, value in account[group].items():
                    totals[group][field] += value
            for feature, statuses in account["ai"].items():
                for status, value in statuses.items():
                    totals["ai"][feature][status] += value
        for field in ("known_seconds", "qwen_seconds", "clova_seconds"):
            totals["recording"][field] = round(totals["recording"][field], 3)
        return {"period": period, "timezone": "Asia/Seoul", "start_at": _stamp(start) if start else None,
                "end_at": _stamp(end), "generated_at": _stamp(end), "scope": dict(SCOPE),
                "accounts": list(accounts.values()), "totals": totals,
                "billing": {"available": False, "reason": "not_recorded"}}


def install(app, database, *, admin_identity, account_ids, limiter):
    reader = UsageReader(database, account_ids)

    @app.get("/admin/usage")
    def usage(period: Literal["today", "month", "all"] = Query(default="month"), user: dict = Depends(admin_identity)):
        if not limiter.allow(("admin-usage", user["username"]), 30, 60):
            raise HTTPException(429, "사용량 조회가 너무 많습니다. 잠시 후 다시 시도하세요.", headers={"Retry-After": "60"})
        return reader.read(period)

    return reader
