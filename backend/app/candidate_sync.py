"""Build per-date attendance payloads from KP Id punch history.

The KP Id / Candidate ID report has In/Out times only (no Present/Half/Absent lists).
Attendance Id is never used for matching.
Classroom status is taken from local SOP hours, then stored as portal-style
present / half_day / absent so classroom % stays consistent.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Optional

from .calculator import HourResult, SessionSpan, calculate_hours, parse_clock, rules_times
from .matching import clean_id, match_candidate
from .reconcile import reconcile_date

STATUS_TO_CATEGORY = {
    "full_day": "present",
    "half_day": "half_day",
    "absent": "absent",
    "insufficient": "absent",
    "incomplete": "absent",
}


def group_punches_by_date(punches: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in punches:
        day = row.get("attendance_date") or row.get("consume_date")
        if not day:
            continue
        grouped[day].append(row)
    return dict(grouped)


def classify_day_rows(
    rows: list[dict],
    *,
    day: date,
    rules: dict,
) -> tuple[str, HourResult]:
    clocks = rules_times(rules)
    clip_start = None if rules.get("count_time_outside_schedule", True) else clocks["training_start"]
    clip_end = None if rules.get("count_time_outside_schedule", True) else clocks["training_end"]
    spans: list[SessionSpan] = []
    for row in rows:
        in_clock, in_invalid = parse_clock(row.get("in_time"))
        out_clock, out_invalid = parse_clock(row.get("out_time"))
        invalid = bool(row.get("in_invalid") or row.get("out_invalid") or in_invalid or out_invalid)
        if not in_clock and not out_clock and not invalid:
            continue
        spans.append(SessionSpan(in_clock, out_clock, invalid=invalid))
    if not spans:
        result = HourResult(0, 0, 0, "absent", False, False)
    else:
        result = calculate_hours(
            day,
            spans,
            lunch_start=clocks["lunch_start"],
            lunch_end=clocks["lunch_end"],
            full_day_hours=float(rules["full_day_hours"]),
            half_day_hours=float(rules["half_day_hours"]),
            clip_start=clip_start,
            clip_end=clip_end,
            schedule_start=clocks["training_start"],
            schedule_end=clocks["training_end"],
        )
    category = STATUS_TO_CATEGORY.get(result.calculated_status, "absent")
    return category, result


def build_date_payload(
    *,
    attendance_day: date,
    day_punches: list[dict],
    candidates: list[dict],
    rules: dict,
) -> tuple[dict, list[dict], set[str], set[str]]:
    """Return summary, detail_rows, opened_categories, failed_categories for reconcile_date."""
    by_candidate: dict[str, list[dict]] = defaultdict(list)
    for row in day_punches:
        cid = clean_id(row.get("candidate_id"))
        if not cid:
            match = match_candidate(row, candidates)
            if match.candidate is not None:
                cid = clean_id(match.candidate.get("candidate_id"))
        if cid:
            by_candidate[cid].append(row)

    detail_rows: list[dict] = []
    counts = {"present": 0, "half_day": 0, "absent": 0, "rf": 0, "ojt": 0}
    seen_pks: set[int] = set()

    for cid, rows in by_candidate.items():
        category, _result = classify_day_rows(rows, day=attendance_day, rules=rules)
        counts[category] = counts.get(category, 0) + 1
        match = match_candidate({"candidate_id": cid, "name": rows[0].get("name")}, candidates)
        if match.candidate is not None:
            seen_pks.add(int(match.candidate["id"]))
        for row in rows:
            detail = dict(row)
            detail["category"] = category
            detail["attendance_date"] = attendance_day.isoformat()
            if match.candidate is not None and not detail.get("name"):
                detail["name"] = match.candidate.get("name") or ""
            detail_rows.append(detail)

    # Enrolled people with no punch that day → Absent (matches unmarked-day rule explicitly).
    for candidate in candidates:
        pk = int(candidate["id"])
        if pk in seen_pks:
            continue
        counts["absent"] += 1
        detail_rows.append(
            {
                "category": "absent",
                "sr_no": candidate.get("sr_no"),
                "candidate_id": clean_id(candidate.get("candidate_id")),
                "name": candidate.get("name") or "",
                "attendance_id": None,
                "device_type": None,
                "attendance_date": attendance_day.isoformat(),
                "portal_attendance_date": attendance_day.isoformat(),
                "in_time": None,
                "out_time": None,
                "in_invalid": False,
                "out_invalid": False,
                "in_device_id": None,
                "out_device_id": None,
            }
        )

    enrolled = len(candidates)
    summary = {
        "date": attendance_day.isoformat(),
        "portal_date": attendance_day.strftime("%d-%m-%Y"),
        "enrolled": enrolled,
        "present": counts["present"],
        "half_day": counts["half_day"],
        "absent": counts["absent"],
        "rf": 0,
        "ojt": 0,
        "logbook": None,
    }
    opened = {"present", "half_day", "absent", "rf", "ojt"}
    return summary, detail_rows, opened, set()


def reconcile_punch_date(
    *,
    attendance_day: date,
    day_punches: list[dict],
    candidates: list[dict],
    rules: dict,
):
    summary, detail_rows, opened, failed = build_date_payload(
        attendance_day=attendance_day,
        day_punches=day_punches,
        candidates=candidates,
        rules=rules,
    )
    result = reconcile_date(
        attendance_day=attendance_day,
        summary=summary,
        detail_rows=detail_rows,
        candidates=candidates,
        rules=rules,
        opened_categories=opened,
        failed_categories=failed,
    )
    return summary, result


def filter_dates(
    dates: list[str],
    *,
    mode: str,
    date_from: Optional[str],
    date_to: Optional[str],
    existing: dict[str, dict],
) -> list[str]:
    from datetime import timedelta

    from .calculator import parse_date, today_ist

    # Never sync today — attendance for the current IST day is incomplete.
    yesterday = (today_ist() - timedelta(days=1)).isoformat()

    selected = sorted(day for day in dates if day <= yesterday)
    if mode == "range":
        # Cross-check one date or a range: include every calendar day in the
        # window, even when nobody punched (those days save as all-absent).
        start = parse_date(date_from) if date_from else None
        end = parse_date(date_to) if date_to else start
        if start is None or end is None:
            if date_from:
                selected = [day for day in selected if day >= date_from]
            if date_to:
                selected = [day for day in selected if day <= min(date_to, yesterday)]
            return [day for day in selected if day <= yesterday]
        if end < start:
            start, end = end, start
        last = parse_date(yesterday)
        if last is not None and end > last:
            end = last
        if last is not None and start > last:
            return []
        days: list[str] = []
        cursor = start
        while cursor <= end:
            days.append(cursor.isoformat())
            cursor += timedelta(days=1)
        return days
    if mode == "full":
        return selected
    # incremental: new dates, or dates not yet detail-synced
    return [day for day in selected if not (existing.get(day) or {}).get("detail_synced")]