"""Pure KST calendar scheduling. No I/O, accounts, provider calls or persistence.

Callers supply validated persisted objects and holiday data. Returned mutations
are copies; IDs, transactions, concurrency and undo tokens belong to the API.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import math
import re
import unicodedata

DEFAULT_OFFSETS = [1, 3, 7, 14, 30]
MODE_KEYS = ("sameDay", "nextDay", "eve", "curve")
DEFAULT_MODE = {"sameDay": False, "nextDay": False, "eve": True, "curve": False}
DOW = "일월화수목금토"
KST = timezone(timedelta(hours=9))
MAX_RANGE_DAYS = 3660


def valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _date(value):
    if not valid_date(value):
        raise ValueError("올바른 날짜를 골라 주세요.")
    return date.fromisoformat(value)


def kst_today(now=None):
    instant = now or datetime.now(timezone.utc)
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        raise ValueError("시간대가 있는 시각이 필요합니다.")
    return instant.astimezone(KST).date().isoformat()


def add_days(value, days):
    if type(days) is not int:
        raise ValueError("날짜 간격은 정수여야 합니다.")
    return (_date(value) + timedelta(days=days)).isoformat()


def diff_days(start, end):
    return (_date(end) - _date(start)).days


def day_of_week(value):
    return (_date(value).weekday() + 1) % 7


def _dates(start, end):
    span = diff_days(start, end)
    if span > MAX_RANGE_DAYS:
        raise ValueError("날짜 범위는 10년 이내로 나누어 주세요.")
    return [add_days(start, n) for n in range(max(0, span + 1))]


def valid_offsets(values):
    return (isinstance(values, list) and 1 <= len(values) <= 12
            and all(type(n) is int and 0 <= n <= 365 and (i == 0 or n > values[i - 1]) for i, n in enumerate(values)))


def gaps(item):
    values = item["offsets"]
    if not valid_offsets(values):
        raise ValueError("복습 간격을 확인해 주세요.")
    return [n - (values[i - 1] if i else 0) for i, n in enumerate(values)]


def is_done(item):
    return len(item.get("reviews", [])) >= len(item["offsets"])


def last_event(item):
    return item["reviews"][-1] if item.get("reviews") else item["base"]


def next_due(item):
    if item.get("skipped") or is_done(item):
        return None
    k = len(item.get("reviews", []))
    moved = item.get("moved")
    if moved and moved.get("stage") == k:
        _date(moved["date"])
        return moved["date"]
    return add_days(last_event(item), gaps(item)[k])


def projected(item, today):
    _date(today)
    due = next_due(item)
    if due is None:
        return []
    stage = len(item.get("reviews", []))
    result = [{"stage": stage, "date": due, "overdue": due < today}]
    current = max(due, today)
    for index in range(stage + 1, len(item["offsets"])):
        current = add_days(current, gaps(item)[index])
        result.append({"stage": index, "date": current, "overdue": False})
    return result


def retention(item, today):
    origin = item["learned"] if item.get("catchup") and not item.get("reviews") else last_event(item)
    return math.exp(-max(0, diff_days(origin, today)) / (2.5 ** len(item.get("reviews", []))))


def min_review_date(item, stage=None):
    stage = len(item.get("reviews", [])) if stage is None else stage
    return item["reviews"][stage - 1] if stage else item["learned"] if item.get("catchup") else item["base"]


def _history(item, day, kind, stage=None):
    event = {"date": day, "type": kind}
    if stage is not None:
        event["stage"] = stage
    item.setdefault("history", []).append(event)


def complete_review(item, today):
    _date(today)
    if next_due(item) is None:
        raise ValueError("이 기록은 복습할 회차가 남아 있지 않아요.")
    if today < min_review_date(item):
        raise ValueError("앞 회차나 공부한 날보다 앞 날짜로 기록할 수 없어요.")
    result = deepcopy(item)
    stage = len(result.setdefault("reviews", []))
    result["reviews"].append(today)
    result["moved"] = None
    _history(result, today, "review", stage)
    return result


def reset_item(item, today):
    _date(today)
    result = deepcopy(item)
    result.update(base=today, reviews=[], moved=None, catchup=False, resets=item.get("resets", 0) + 1)
    _history(result, today, "reset")
    return result


def skip_item(item, today):
    _date(today)
    result = deepcopy(item)
    if not result.get("skipped"):
        result["skipped"] = today
        _history(result, today, "skip")
    return result


def unskip_item(item, today):
    _date(today)
    result = deepcopy(item)
    if result.get("skipped"):
        result["skipped"] = None
        _history(result, today, "unskip")
    return result


def place_review(item, day, today):
    _date(day); _date(today)
    due = next_due(item)
    if due is None:
        raise ValueError("이 기록은 옮길 복습이 남아 있지 않아요.")
    if day < min_review_date(item):
        raise ValueError("앞 회차나 공부한 날보다 앞 날짜로 기록할 수 없어요.")
    if day < today:
        return complete_review(item, day)
    result = deepcopy(item)
    if day != due:
        result["moved"] = {"stage": len(item.get("reviews", [])), "date": day}
    return result


def edit_review_date(item, stage, day, today):
    _date(day); _date(today)
    if type(stage) is not int or not 0 <= stage < len(item.get("reviews", [])):
        raise ValueError("끝낸 복습 회차를 골라 주세요.")
    lower = min_review_date(item, stage)
    upper = min(today, item["reviews"][stage + 1]) if stage + 1 < len(item["reviews"]) else today
    if not lower <= day <= upper:
        raise ValueError("앞뒤 복습 날짜와 오늘 사이에서 골라 주세요.")
    result = deepcopy(item)
    old = result["reviews"][stage]
    result["reviews"][stage] = day
    for event in reversed(result.get("history", [])):
        if event.get("type") == "review" and event.get("stage") == stage and event.get("date") == old:
            event["date"] = day
            break
    return result


def undo_last_review(item):
    result = deepcopy(item)
    if not result.get("reviews"):
        raise ValueError("지울 복습 기록이 없어요.")
    stage = len(result["reviews"]) - 1
    result["reviews"].pop()
    for index in range(len(result.get("history", [])) - 1, -1, -1):
        event = result["history"][index]
        if event.get("type") == "review" and event.get("stage") == stage:
            result["history"].pop(index)
            break
    result["moved"] = None
    return result


def holiday_map(holidays):
    if isinstance(holidays, dict):
        if "holidays" in holidays:
            holidays = holidays["holidays"]
        else:
            return holidays
    return {row["date"]: row["name"] for row in holidays or []}


def _mode(timetable):
    mode = (timetable or {}).get("mode") or DEFAULT_MODE
    if not all(type(mode.get(key)) is bool for key in MODE_KEYS) or not any(mode[key] for key in MODE_KEYS):
        raise ValueError("평소 복습 방식을 하나 이상 골라 주세요.")
    return mode


def next_class_date(subject, day, timetable, holidays):
    _date(day)
    if not timetable:
        return None
    excluded = holiday_map(holidays)
    for n in range(1, 22):
        candidate = add_days(day, n)
        if timetable.get("until") and candidate > timetable["until"]:
            return None
        if candidate not in excluded and any(c["subject"] == subject and c["day"] == day_of_week(candidate) for c in timetable["classes"]):
            return candidate
    return None


def _curve(settings):
    offsets = (settings or {}).get("offsets", DEFAULT_OFFSETS)
    if not valid_offsets(offsets):
        raise ValueError("복습 간격을 확인해 주세요.")
    return [n for n in offsets if n > 1]


def offsets_for(subject, day, timetable, settings, holidays):
    mode = _mode(timetable); values = []
    if mode["sameDay"]: values.append(0)
    if mode["nextDay"]: values.append(1)
    if mode["eve"]:
        following = next_class_date(subject, day, timetable, holidays)
        if following: values.append(diff_days(day, following) - 1)
    if mode["curve"]: values.extend(_curve(settings))
    return sorted(set(values))[:12] or [1]


def catchup_offsets(timetable, settings):
    mode = _mode(timetable)
    return sorted(set([0] + ([1] if mode["nextDay"] else []) + (_curve(settings) if mode["curve"] else [])))[:12]


def eve_target(subject, today, timetable, holidays):
    following = next_class_date(subject, today, timetable, holidays)
    return max(today, add_days(following, -1)) if following else today


def source_key(day, class_row):
    return f"{day}|{class_row['subject']}|{class_row['day']}|{class_row.get('start', '')}"


def class_item(class_row, day, timetable, settings, holidays):
    parsed = _date(day)
    clock = "–".join([class_row.get("start", ""), class_row.get("end", "")]) if class_row.get("start") and class_row.get("end") else class_row.get("start", "")
    return {"title": f"{parsed.month}/{parsed.day}({DOW[day_of_week(day)]}) {class_row['subject']} 수업",
            "subject": class_row["subject"], "memo": " · ".join(part for part in (clock, class_row.get("room", "")) if part),
            "learned": day, "base": day, "offsets": offsets_for(class_row["subject"], day, timetable, settings, holidays),
            "reviews": [], "history": [{"date": day, "type": "learn"}], "resets": 0,
            "source": "timetable", "source_key": source_key(day, class_row), "catchup": False, "skipped": None, "moved": None}


def _classes(day, timetable, exams):
    return sorted((row for row in timetable["classes"] if row["day"] == day_of_week(day)
                   and not any(exam["subject"] == row["subject"] and exam["date"] == day for exam in exams)),
                  key=lambda row: (row.get("start", ""), row["subject"]))


def timetable_preview(timetable, today, settings, holidays, exams=(), existing_keys=()):
    if not timetable:
        return {"items": [], "through": None}
    end = min(today, timetable.get("until") or today)
    through = timetable.get("through") or add_days(timetable["from"], -1)
    start = max(timetable["from"], add_days(through, 1))
    excluded, seen, result = holiday_map(holidays), set(existing_keys), []
    for day in _dates(start, end):
        if day in excluded: continue
        for row in _classes(day, timetable, exams):
            key = source_key(day, row)
            if key not in seen:
                result.append(class_item(row, day, timetable, settings, holidays)); seen.add(key)
    return {"items": result, "through": max(through, end) if end >= timetable["from"] else through}


def edited_through(previous, new_from):
    baseline = add_days(new_from, -1)
    return max(previous, baseline) if previous else baseline


def default_sem_start(start):
    _date(start)
    return start[:8] + "01"


def week_no(day, sem_start):
    monday = add_days(sem_start, -((day_of_week(sem_start) + 6) % 7))
    return diff_days(monday, day) // 7 + 1


def catchup_preview(timetable, sem_start, today, settings, holidays, existing_keys=(), exams=()):
    if not timetable: return []
    end = min(add_days(timetable["from"], -1), today, timetable.get("until") or today)
    excluded, seen, rows = holiday_map(holidays), set(existing_keys), []
    for day in _dates(sem_start, end):
        for entry in _classes(day, timetable, exams):
            key = source_key(day, entry); holiday = excluded.get(day, "")
            rows.append({**deepcopy(entry), "date": day, "source_key": key, "holiday": holiday,
                         "existing": key in seen, "selected": key not in seen and not holiday, "week": week_no(day, sem_start)})
            seen.add(key)
    return rows


def catchup_plan(rows, timetable, today, settings, holidays, per_day=5):
    if per_day not in (3, 5, 8, 12, 0, None) or isinstance(per_day, bool):
        raise ValueError("하루에 불러올 개수를 골라 주세요.")
    candidates = sorted((r for r in rows if r.get("selected") and not r.get("existing")), key=lambda r: (r["date"], r.get("start", ""), r["subject"]))
    result, seen = [], set()
    for row in candidates:
        key = source_key(row["date"], row)
        if key in seen: continue
        item = class_item(row, row["date"], timetable, settings, holidays)
        item.update(catchup=True, offsets=catchup_offsets(timetable, settings),
                    base=eve_target(row["subject"], today, timetable, holidays) if _mode(timetable)["eve"] else add_days(today, len(result) // per_day if per_day else 0))
        result.append(item); seen.add(key)
    return result


def apply_mode(item, timetable, settings, holidays, today):
    result = deepcopy(item)
    if item.get("source") != "timetable" or item.get("skipped") or is_done(item): return result
    result["offsets"] = catchup_offsets(timetable, settings) if item.get("catchup") else offsets_for(item["subject"], item["learned"], timetable, settings, holidays)
    if item.get("catchup") and not item.get("reviews") and _mode(timetable)["eve"]:
        result["base"] = eve_target(item["subject"], today, timetable, holidays)
        result["moved"] = None
    return result


def exam_range(exam, sem_start, exams=()):
    start, mid = sem_start, None
    if exam["kind"] == "final":
        previous = [e for e in exams if e["subject"] == exam["subject"] and e["kind"] == "mid" and e["date"] < exam["date"]]
        if previous:
            mid = max(previous, key=lambda e: e["date"]); start = add_days(mid["date"], 1)
    return {"start": start, "end": add_days(exam["date"], -1), "mid": deepcopy(mid)}


def validate_exam_order(exam, exams=()):
    _date(exam["date"])
    for other in exams:
        if other["subject"] != exam["subject"] or other["kind"] == exam["kind"]: continue
        mid, final = (exam, other) if exam["kind"] == "mid" else (other, exam)
        if final["date"] <= mid["date"]:
            raise ValueError("기말 날짜는 중간 날짜보다 뒤여야 합니다.")
    return True


def exam_sessions(exam, sem_start, timetable, holidays, exams=(), items=(), today=None):
    today = today or kst_today(); interval = exam_range(exam, sem_start, exams)
    by_key = {item.get("source_key"): item for item in items if item.get("source_key")}
    result, seen, excluded = [], set(), holiday_map(holidays)
    def append(key, day, title, skipped, start="", source=None):
        if key in seen: return
        seen.add(key)
        result.append({"key": key, "date": day, "title": title, "subject": exam["subject"], "skipped": bool(skipped),
                       "future": day > today, "week": week_no(day, sem_start), "start": start, "source_key": source})
    if timetable:
        for day in _dates(interval["start"], interval["end"]):
            if day in excluded or (timetable.get("until") and day > timetable["until"]): continue
            for row in _classes(day, timetable, exams):
                if row["subject"] != exam["subject"]: continue
                key = source_key(day, row); item = by_key.get(key)
                title = item["title"] if item else class_item(row, day, timetable, {"offsets": DEFAULT_OFFSETS}, holidays)["title"]
                append(key, day, title, item and item.get("skipped"), row.get("start", ""), key)
    for item in items:
        if item.get("source", "manual") == "manual" and item["subject"] == exam["subject"] and interval["start"] <= item["learned"] <= interval["end"]:
            append(item.get("source_key") or item["id"], item["learned"], item["title"], item.get("skipped"))
    return sorted(result, key=lambda r: (r["date"], r["start"], r["title"], r["key"]))


def exam_stats(exam, sessions, today):
    live = [deepcopy(row) for row in sessions if not row["skipped"]]
    rounds, marks = exam.get("rounds", 3), exam.get("done", {})
    per = [sum(1 for row in live if not row["future"] and index < len(marks.get(row["key"], [])) and marks[row["key"]][index] == 1) for index in range(rounds)]
    total, done = len(live) * rounds, sum(per)
    remaining = total - done
    cram_start, days_left = add_days(exam["date"], -exam.get("lead", 7)), diff_days(today, exam["date"])
    phase = "over" if today > exam["date"] else "today" if today == exam["date"] else "cram" if today >= cram_start else "before"
    next_key, next_round = None, None
    for index in range(rounds):
        if per[index] >= len(live): continue
        candidate = next((row for row in live if not row["future"] and (index >= len(marks.get(row["key"], [])) or marks[row["key"]][index] != 1)), None)
        if candidate: next_key, next_round = candidate["key"], index
        break
    return {"all": deepcopy(sessions), "live": live, "per": per, "total": total, "done": done, "remaining": remaining,
            "cram_start": cram_start, "days_left": days_left, "phase": phase, "target": math.ceil(remaining / max(1, days_left)) if phase == "cram" else 0,
            "next_key": next_key, "next_round": next_round, "checkable": sum(not row["future"] for row in live) * rounds}


def routine_n(routine, day):
    _date(day); _date(routine["start"])
    days = set(routine.get("days") or range(7))
    if day < routine["start"] or (routine.get("end") and day > routine["end"]) or day_of_week(day) not in days: return 0
    span = diff_days(routine["start"], day)
    full, remainder = divmod(span + 1, 7)
    number = full * len(days) + sum((day_of_week(routine["start"]) + n) % 7 in days for n in range(remainder))
    return 0 if routine.get("count") and number > routine["count"] else number


def routine_title(routine, number):
    return routine["name"] + (f" ({routine.get('num_start', 1) - 1 + number})" if routine.get("numbered") else "")


def routine_days_label(routine):
    days = set(routine.get("days") or range(7))
    if len(days) == 7: return "매일"
    if days == {1, 2, 3, 4, 5}: return "평일"
    if days == {0, 6}: return "주말"
    return "·".join(DOW[n] for n in (1, 2, 3, 4, 5, 6, 0) if n in days)


def _done_dates(routine):
    done = routine.get("done", [])
    return set(key for key, value in done.items() if value) if isinstance(done, dict) else set(done)


def _routine_date(routine, number):
    days = set(routine.get("days") or range(7))
    full, remaining = divmod(number - 1, len(days))
    start = add_days(routine["start"], full * 7)
    occurrences = [add_days(start, n) for n in range(7) if day_of_week(add_days(start, n)) in days]
    return occurrences[remaining]


def _previous_run(routine, day):
    last = min(day, routine.get("end") or day)
    if routine.get("count"):
        last = min(last, _routine_date(routine, routine["count"]))
    for n in range(7):
        candidate = add_days(last, -n)
        if candidate >= routine["start"] and routine_n(routine, candidate):
            return candidate
    return None


def routine_stats(routine, today):
    done = {day for day in _done_dates(routine) if valid_date(day) and day <= today and routine_n(routine, day)}
    monday = add_days(today, -((day_of_week(today) + 6) % 7))
    week = [add_days(monday, n) for n in range(7) if routine_n(routine, add_days(monday, n))]
    current = add_days(today, -1) if routine_n(routine, today) and today not in done else today
    streak = 0
    # Skip gaps algebraically at most six days per completed run; total work is
    # bounded by the persisted done dates, never by years since the start.
    while current >= routine["start"]:
        current = _previous_run(routine, current)
        if current is None: break
        if current not in done: break
        streak += 1; current = add_days(current, -1)
    future_start = max(today, routine["start"])
    ended = not any(routine_n(routine, add_days(future_start, n)) for n in range(7))
    return {"week_total": len(week), "week_done": sum(day in done for day in week), "streak": streak, "total": len(done), "ended": ended}


def routines_on(routines, day):
    return [{"routine": deepcopy(row), "n": routine_n(row, day)} for row in routines if routine_n(row, day)]


def toggle_routine(routine, day, today, on):
    if type(on) is not bool or day > today or not routine_n(routine, day):
        raise ValueError("실행일과 지난 날짜 또는 오늘만 체크할 수 있어요.")
    result = deepcopy(routine); done = _done_dates(routine)
    if on: done.add(day)
    else: done.discard(day)
    result["done"] = sorted(done)
    return result


def due_items(items, today, sort="due"):
    result = [deepcopy(item) for item in items if next_due(item) is not None and next_due(item) <= today]
    if sort == "subject": return sorted(result, key=lambda item: (item.get("subject", ""), item["learned"], item.get("title", ""), item.get("id", "")))
    return sorted(result, key=lambda item: (next_due(item), item.get("title", ""), item.get("id", "")))


def group_due_items(items, today):
    result = []
    for item in due_items(items, today, "subject"):
        subject = item.get("subject", "")
        if not result or result[-1]["subject"] != subject: result.append({"subject": subject, "items": [], "overdue": 0})
        result[-1]["items"].append(item); result[-1]["overdue"] += int(next_due(item) < today)
    return result


def norm_time(value):
    if not isinstance(value, str): return ""
    match = re.fullmatch(r"([0-9]{1,2})(?::([0-9]{2})|\.([0-9]{2})|시(?:\s*([0-9]{1,2})분?)?)", value.strip())
    if not match: return ""
    hour, minute = int(match[1]), int(next((part for part in match.groups()[1:] if part is not None), "0"))
    return f"{hour:02d}:{minute:02d}" if hour <= 23 and minute <= 59 else ""


def clean_classes(rows):
    if not isinstance(rows, list): return []
    aliases = {name: index for index, names in enumerate((("일", "일요일", "sun", "sunday"), ("월", "월요일", "mon", "monday"), ("화", "화요일", "tue", "tuesday"), ("수", "수요일", "wed", "wednesday"), ("목", "목요일", "thu", "thursday"), ("금", "금요일", "fri", "friday"), ("토", "토요일", "sat", "saturday"))) for name in names}
    def clean(value):
        return "".join(c for c in unicodedata.normalize("NFC", value).strip() if unicodedata.category(c) not in {"Cc", "Cf", "Cs"}) if isinstance(value, str) else ""
    result = []
    for row in rows:
        if not isinstance(row, dict): continue
        subject, room = clean(row.get("subject")), clean(row.get("room", ""))
        day = row.get("day")
        if isinstance(day, str):
            key = day.strip().lower(); day = int(key) if re.fullmatch(r"[0-6]", key) else aliases.get(key)
        if not subject or len(subject) > 40 or len(room) > 120 or type(day) is not int or not 0 <= day <= 6: continue
        start, end = norm_time(row.get("start")), norm_time(row.get("end"))
        if end and start and end <= start: end = ""
        result.append({"subject": subject, "day": day, "start": start, "end": end, "room": room})
        if len(result) == 60: break
    return result
