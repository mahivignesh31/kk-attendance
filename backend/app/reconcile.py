"""Reconcile portal summary counts with candidate-level rows.

A candidate is stored once per date. Categories are not added together.
Absence is recorded only from an absent list, or when every other reported
count is zero and those empty lists were confirmed. Hours come only from
complete check-in and check-out pairs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .calculator import HourResult, SessionSpan, calculate_hours, parse_clock, rules_times
from .matching import Match, clean_text, match_candidate, normalize_name

COUNT_FIELDS = {
    "present": "present",
    "half_day": "half_day",
    "absent": "absent",
    "rf": "rf",
    "ojt": "ojt",
}


@dataclass
class Issue:
    issue_type: str
    severity: str
    message: str
    candidate_pk: Optional[int] = None
    portal_value: Optional[str] = None
    local_value: Optional[str] = None


@dataclass
class SessionDraft:
    in_time: Optional[str]
    out_time: Optional[str]
    device_type: Optional[str]
    in_device_id: Optional[str]
    out_device_id: Optional[str]
    portal_attendance_id: Optional[str]
    invalid: bool = False


@dataclass
class RecordDraft:
    candidate_pk: int
    official_status: str
    official_categories: list[str]
    calculated_status: str
    in_time: Optional[str]
    out_time: Optional[str]
    elapsed_minutes: Optional[int]
    lunch_deduction_minutes: Optional[int]
    qualifying_minutes: Optional[int]
    device_type: Optional[str]
    in_device_id: Optional[str]
    out_device_id: Optional[str]
    portal_name: Optional[str]
    sessions: list[SessionDraft] = field(default_factory=list)
    raw: list[dict] = field(default_factory=list)
    data_source: str = "kaushal_portal"


@dataclass
class ReconcileResult:
    records: list[RecordDraft]
    issues: list[Issue]
    complete: bool


def reconcile_date(
    *,
    attendance_day: date,
    summary: dict,
    detail_rows: list[dict],
    candidates: list[dict],
    rules: dict,
    opened_categories: set[str],
    failed_categories: set[str],
) -> ReconcileResult:
    issues: list[Issue] = []
    clocks = rules_times(rules)
    clip_start = None if rules.get("count_time_outside_schedule", True) else clocks["training_start"]
    clip_end = None if rules.get("count_time_outside_schedule", True) else clocks["training_end"]

    grouped: dict[tuple, list[dict]] = {}
    matches: dict[tuple, Match] = {}
    for row in detail_rows:
        match = match_candidate(row, candidates)
        if match.candidate is not None:
            key = ("pk", int(match.candidate["id"]))
        elif row.get("candidate_id"):
            key = ("cid", row["candidate_id"])
        else:
            key = ("row", row.get("sr_no"), normalize_name(row.get("name")))
        grouped.setdefault(key, []).append(row)
        matches[key] = match

    extracted_counts = {category: set() for category in COUNT_FIELDS}
    records: list[RecordDraft] = []

    for key, rows in grouped.items():
        match = matches[key]
        categories = []
        for row in rows:
            category = row.get("category")
            if category in COUNT_FIELDS and category not in categories:
                categories.append(category)
            identity = row.get("candidate_id") or row.get("name") or key
            extracted_counts.setdefault(category, set()).add(identity)
        if match.warning:
            issues.append(
                Issue(
                    "candidate_match",
                    "warning",
                    match.warning,
                    int(match.candidate["id"]) if match.candidate else None,
                    json.dumps(_public_row(rows[0]), ensure_ascii=False),
                    match.candidate.get("name") if match.candidate else None,
                )
            )
        if match.candidate is None:
            issues.append(
                Issue(
                    "unmatched_portal_candidate",
                    "warning",
                    match.warning or "A portal attendance row could not be matched to the master list.",
                    None,
                    json.dumps([_public_row(row) for row in rows], ensure_ascii=False),
                    None,
                )
            )
            continue
        if len(categories) > 1:
            issues.append(
                Issue(
                    "duplicate_category",
                    "warning",
                    f"{match.candidate.get('name')} appears in {', '.join(categories)} on {attendance_day.isoformat()}. Counted once.",
                    int(match.candidate["id"]),
                    ",".join(categories),
                    match.candidate.get("candidate_id"),
                )
            )
        draft = _build_record(
            candidate=match.candidate,
            rows=rows,
            categories=categories,
            day=attendance_day,
            rules=rules,
            clip_start=clip_start,
            clip_end=clip_end,
            issues=issues,
        )
        records.append(draft)

    count_mismatch = False
    for category, field_name in COUNT_FIELDS.items():
        expected = summary.get(field_name)
        if expected is None:
            continue
        found = len(extracted_counts.get(category, set()))
        if category in failed_categories:
            count_mismatch = True
            issues.append(
                Issue(
                    "detail_not_opened",
                    "warning",
                    f"{category.replace('_', ' ').title()} shows {expected} on {attendance_day.isoformat()}, but the candidate list could not be opened.",
                    None,
                    str(expected),
                    "not opened",
                )
            )
            continue
        if category not in opened_categories and expected == 0:
            continue
        if category not in opened_categories and expected > 0:
            count_mismatch = True
            issues.append(
                Issue(
                    "detail_not_opened",
                    "warning",
                    f"{category.replace('_', ' ').title()} shows {expected} on {attendance_day.isoformat()}, but that list was not read.",
                    None,
                    str(expected),
                    "not read",
                )
            )
            continue
        if found != expected:
            count_mismatch = True
            issues.append(
                Issue(
                    "summary_mismatch",
                    "warning",
                    f"{attendance_day.isoformat()} {category.replace('_', ' ')} summary is {expected}, while {found} candidate rows were read.",
                    None,
                    str(expected),
                    str(found),
                )
            )

    complete = not count_mismatch and not failed_categories
    return ReconcileResult(records=records, issues=issues, complete=complete)


def _build_record(
    *,
    candidate: dict,
    rows: list[dict],
    categories: list[str],
    day: date,
    rules: dict,
    clip_start,
    clip_end,
    issues: list[Issue],
) -> RecordDraft:
    clocks = rules_times(rules)
    non_absent = [row for row in rows if row.get("category") != "absent"]
    absent_rows = [row for row in rows if row.get("category") == "absent"]
    for row in absent_rows:
        if row.get("in_time") or row.get("out_time"):
            issues.append(
                Issue(
                    "absent_row_has_times",
                    "warning",
                    f"{candidate.get('name')} is listed absent on {day.isoformat()} but the absent row also contains a time. The time was not used as attendance.",
                    int(candidate["id"]),
                    json.dumps(_public_row(row), ensure_ascii=False),
                    "ignored",
                )
            )

    sessions: list[SessionDraft] = []
    seen_sessions: set[tuple] = set()
    source_rows = non_absent if non_absent else []
    if not source_rows and not absent_rows:
        source_rows = rows
    for row in source_rows:
        if row.get("category") == "absent":
            continue
        in_time = row.get("in_time")
        out_time = row.get("out_time")
        invalid = bool(row.get("in_invalid") or row.get("out_invalid"))
        if invalid:
            issues.append(
                Issue(
                    "invalid_timestamp",
                    "warning",
                    f"{candidate.get('name')} has an unreadable time on {day.isoformat()}. No check-out was inferred.",
                    int(candidate["id"]),
                    json.dumps(_public_row(row), ensure_ascii=False),
                    None,
                )
            )
        key = (in_time, out_time, row.get("in_device_id"), row.get("out_device_id"), row.get("device_type"))
        if key in seen_sessions:
            continue
        seen_sessions.add(key)
        if not in_time and not out_time and not invalid:
            continue
        sessions.append(
            SessionDraft(
                in_time=in_time,
                out_time=out_time,
                device_type=row.get("device_type"),
                in_device_id=row.get("in_device_id"),
                out_device_id=row.get("out_device_id"),
                portal_attendance_id=row.get("attendance_id"),
                invalid=invalid,
            )
        )

    only_absent = bool(absent_rows) and not non_absent
    if only_absent:
        result = HourResult(0, 0, 0, "absent", False, False)
        sessions = []
    elif not sessions:
        result = HourResult(None, None, None, "incomplete", False, True)
    else:
        spans = []
        for session in sessions:
            in_clock, in_invalid = parse_clock(session.in_time)
            out_clock, out_invalid = parse_clock(session.out_time)
            spans.append(
                SessionSpan(
                    in_clock,
                    out_clock,
                    invalid=session.invalid or in_invalid or out_invalid,
                )
            )
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
        if result.missing_checkout and result.calculated_status == "absent" and result.qualifying_minutes is None:
            issues.append(
                Issue(
                    "single_punch",
                    "info",
                    f"{candidate.get('name')} on {day.isoformat()} has a check-in without a check-out. Counted as absent.",
                    int(candidate["id"]),
                    ",".join(categories),
                    result.calculated_status,
                )
            )
        elif result.missing_checkout:
            issues.append(
                Issue(
                    "partial_missing_checkout",
                    "info",
                    f"{candidate.get('name')} on {day.isoformat()} has an extra check-in without a check-out. Hours use complete sessions only.",
                    int(candidate["id"]),
                    ",".join(categories),
                    result.calculated_status,
                )
            )

    official = "mixed" if len(categories) > 1 else (categories[0] if categories else "unknown")
    if official == "half_day" and result.calculated_status == "full_day":
        issues.append(
            Issue(
                "hours_differ_from_official",
                "info",
                f"{candidate.get('name')} is Half Day on the portal for {day.isoformat()}, while local hours reach the full-day threshold. Both statuses are kept.",
                int(candidate["id"]),
                "half_day",
                "full_day",
            )
        )
    elif official == "present" and result.calculated_status in {"half_day", "insufficient", "absent"}:
        issues.append(
            Issue(
                "hours_differ_from_official",
                "info",
                f"{candidate.get('name')} is Present on the portal for {day.isoformat()}, while local hours calculate as {result.calculated_status.replace('_', ' ')}.",
                int(candidate["id"]),
                "present",
                result.calculated_status,
            )
        )

    valid_ins = [session.in_time for session in sessions if session.in_time and session.out_time]
    valid_outs = [session.out_time for session in sessions if session.in_time and session.out_time]
    first = sessions[0] if sessions else None
    return RecordDraft(
        candidate_pk=int(candidate["id"]),
        official_status=official,
        official_categories=categories,
        calculated_status=result.calculated_status,
        in_time=min(valid_ins) if valid_ins else (first.in_time if first else None),
        out_time=max(valid_outs) if valid_outs else (first.out_time if first else None),
        elapsed_minutes=result.elapsed_minutes,
        lunch_deduction_minutes=result.lunch_deduction_minutes,
        qualifying_minutes=result.qualifying_minutes,
        device_type=first.device_type if first else None,
        in_device_id=first.in_device_id if first else None,
        out_device_id=first.out_device_id if first else None,
        portal_name=next((row.get("name") for row in rows if row.get("name")), candidate.get("name")),
        sessions=sessions,
        raw=[_public_row(row) for row in rows],
    )


def _public_row(row: dict) -> dict:
    hidden = {"in_invalid", "out_invalid"}
    return {key: value for key, value in row.items() if key not in hidden}
