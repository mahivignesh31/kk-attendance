"""Dashboard, matrix, profile, and export datasets."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

from .calculator import (
    OFFICIAL_LABELS,
    STATUS_LABELS,
    SessionSpan,
    calculate_hours,
    cell_status,
    calculate_attendance,
    enrollment_window,
    format_minutes,
    grace_days_to_demote,
    sop_limits,
    hours_decimal,
    total_training_days,
    now_iso,
    parse_clock,
    parse_date,
    rules_times,
    scheduled_qualifying_minutes,
    score_working_day,
    summarize_candidate,
    today_ist,
    validate_rules,
)
from .db import get_json, set_json, upsert_batch

DISTRIBUTION = (
    (0, 50, "Below 50%"),
    (50, 75, "50% to 75%"),
    (75, 90, "75% to 90%"),
    (90, 100.01, "90% to 100%"),
)


def _centre_count(conn: sqlite3.Connection) -> int:
    from .auth import actor

    user = actor() or {}
    if user.get("role") == "centre":
        return 1 if user.get("centre_id") else 0
    return conn.execute("SELECT COUNT(*) AS count FROM training_centres").fetchone()["count"]


def _active_batch_count(conn: sqlite3.Connection) -> int:
    from .auth import actor

    user = actor() or {}
    if user.get("role") == "centre" and user.get("centre_id"):
        return conn.execute(
            "SELECT COUNT(*) AS count FROM batches WHERE centre_id = ? AND COALESCE(status, 'active') = 'active'",
            (user["centre_id"],),
        ).fetchone()["count"]
    return conn.execute(
        "SELECT COUNT(*) AS count FROM batches WHERE COALESCE(status, 'active') = 'active'"
    ).fetchone()["count"]


def _batch_from_row(row) -> dict:
    return {
        "state": row["state"] or "",
        "pia": row["pia"] or "",
        "sanction_order": row["sanction_order"] or "",
        "training_center": row["training_center"] or "",
        "batch_code": row["batch_code"] or "",
        "batch_start_date": row["batch_start_date"] or "",
        "expected_end_date": row["expected_end_date"] or "",
    }


def _centre_batch(conn: sqlite3.Connection):
    from .auth import actor

    user = actor() or {}
    if user.get("role") != "centre" or not user.get("centre_id"):
        return None
    centre_id = user["centre_id"]
    row = None
    code = user.get("active_batch_code") or ""
    if code:
        row = conn.execute(
            "SELECT * FROM batches WHERE centre_id = ? AND batch_code = ?",
            (centre_id, code),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM batches WHERE centre_id = ? ORDER BY batch_code LIMIT 1",
            (centre_id,),
        ).fetchone()
    if row is None:
        centre = conn.execute("SELECT * FROM training_centres WHERE id = ?", (centre_id,)).fetchone()
        return {
            "state": centre["state"] if centre else "",
            "pia": centre["pia"] if centre else "",
            "sanction_order": centre["sanction_order"] if centre else "",
            "training_center": centre["name"] if centre else "",
            "batch_code": "",
            "batch_start_date": "",
            "expected_end_date": "",
        }
    return _batch_from_row(row)


def load_context(conn: sqlite3.Connection) -> dict:
    scoped = _centre_batch(conn)
    batch = scoped if scoped is not None else (get_json(conn, "batch") or {})
    rules = validate_rules(get_json(conn, "rules") or {})
    batch_row = conn.execute(
        "SELECT id FROM batches WHERE batch_code = ?",
        (batch.get("batch_code"),),
    ).fetchone()
    batch_id = batch_row["id"] if batch_row else None
    if batch_id:
        candidates = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM candidates
                WHERE batch_id = ? OR batch_registration_number = ?
                ORDER BY sr_no, name, id
                """,
                (batch_id, batch.get("batch_code")),
            )
        ]
        dates = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM attendance_dates WHERE batch_id = ? ORDER BY attendance_date",
                (batch_id,),
            )
        ]
        record_sql = "SELECT * FROM attendance_records WHERE batch_id = ?"
        if scoped is None:
            record_sql = "SELECT * FROM attendance_records WHERE batch_id = ? OR batch_id IS NULL"
        records = [dict(row) for row in conn.execute(record_sql, (batch_id,))]
    elif scoped is not None:
        candidates, dates, records = [], [], []
    else:
        candidates = [dict(row) for row in conn.execute("SELECT * FROM candidates ORDER BY sr_no, name, id")]
        dates = [dict(row) for row in conn.execute("SELECT * FROM attendance_dates ORDER BY attendance_date")]
        records = [dict(row) for row in conn.execute("SELECT * FROM attendance_records")]
    return {
        "batch": batch,
        "rules": rules,
        "candidates": candidates,
        "dates": dates,
        "records": records,
        "today": today_ist(),
        "batch_start": parse_date(batch.get("batch_start_date")),
    }


def _portal_days(dates: list[dict]) -> list[date]:
    days = []
    for row in dates:
        parsed = parse_date(row.get("attendance_date"))
        if parsed:
            days.append(parsed)
    return sorted(set(days))


def _records_index(records: list[dict]) -> dict[tuple[int, date], dict]:
    indexed = {}
    for record in records:
        day = parse_date(record.get("attendance_date"))
        if day is None:
            continue
        indexed[(int(record["candidate_pk"]), day)] = record
    return indexed


def _batch_end(context: dict) -> date | None:
    return parse_date((context.get("batch") or {}).get("expected_end_date"))


def _total_days(context: dict) -> list[date]:
    days = total_training_days(context.get("batch_start"), context["today"], _batch_end(context))
    if days:
        return days
    return [
        day
        for day in _portal_days(context["dates"])
        if day <= context["today"] and day.weekday() != 6
    ]


def candidate_stats(context: dict) -> list[dict]:
    days = _total_days(context) if context["records"] or context["dates"] else []
    indexed = _records_index(context["records"])
    rows = []
    for candidate in context["candidates"]:
        enrollment, removal = enrollment_window(candidate, context["batch_start"])
        by_day = {}
        for day in days:
            record = indexed.get((int(candidate["id"]), day))
            if record:
                by_day[day] = record
        totals = summarize_candidate(
            days,
            by_day,
            today=context["today"],
            rules=context["rules"],
            enrollment=enrollment,
            removal=removal,
            batch_end=_batch_end(context),
        )
        rows.append(
            {
                "id": candidate["id"],
                "sr_no": candidate.get("sr_no"),
                "candidate_id": candidate["candidate_id"],
                "attendance_id": candidate.get("attendance_id"),
                "name": candidate["name"],
                "batch_registration_number": candidate.get("batch_registration_number"),
                "candidate_status": candidate.get("candidate_status"),
                "add_remove_date": candidate.get("add_remove_date"),
                "ojt_start_date": candidate.get("ojt_start_date"),
                "enrollment_date": enrollment.isoformat() if enrollment else None,
                "removal_date": removal.isoformat() if removal else None,
                **totals.as_dict(),
            }
        )
    return rows


def dashboard(conn: sqlite3.Connection, selected: str | None = None) -> dict:
    context = load_context(conn)
    stats = candidate_stats(context)
    days = _portal_days(context["dates"])
    date_rows = {row["attendance_date"]: row for row in context["dates"]}
    selected_day = parse_date(selected) if selected else None
    if selected_day is None:
        elapsed = [day for day in days if day <= context["today"]]
        selected_day = elapsed[-1] if elapsed else (days[-1] if days else None)
    selected_iso = selected_day.isoformat() if selected_day else None
    selected_row = date_rows.get(selected_iso) if selected_iso else None
    indexed = _records_index(context["records"])
    counts = {key: 0 for key in ("full_day", "half_day", "absent", "insufficient", "incomplete", "not_verified")}
    detail_ready = bool(selected_row and selected_row.get("detail_synced"))
    if selected_day and detail_ready:
        for candidate in context["candidates"]:
            enrollment, removal = enrollment_window(candidate, context["batch_start"])
            record = indexed.get((int(candidate["id"]), selected_day))
            cell = cell_status(
                selected_day,
                record,
                today=context["today"],
                rules=context["rules"],
                enrollment=enrollment,
                removal=removal,
            )
            status = cell["status"]
            if status == "insufficient":
                counts["insufficient"] += 1
                counts["absent"] += 1
            elif status in counts:
                counts[status] += 1
            elif status in {"before_enrollment", "sunday", "future", "not_eligible"}:
                continue
            else:
                counts["not_verified"] += 1
    average = calculate_attendance(
        sum(row.get("full_days") or 0 for row in stats),
        sum(row.get("half_days") or 0 for row in stats),
        sum(row.get("eligible_days") or 0 for row in stats),
    ) if stats else None
    verified_days = len(_total_days(context))
    last_sync = conn.execute(
        """
        SELECT * FROM sync_history
        WHERE status = 'completed'
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    open_issues = conn.execute(
        "SELECT COUNT(*) AS count FROM reconciliation_issues WHERE resolved = 0"
    ).fetchone()["count"]
    threshold = float(context["rules"]["low_attendance_threshold"])
    below = [
        row for row in stats if row["verified_percent"] is not None and row["verified_percent"] < threshold
    ]
    below.sort(key=lambda item: (item["verified_percent"], item["name"]))
    from .auth import actor, public_account

    return {
        "user": public_account(actor()),
        "batch": context["batch"],
        "rules": {**context["rules"], "scheduled_hours": round(scheduled_qualifying_minutes(context["rules"]) / 60, 2)},
        "selected_date": selected_iso,
        "selected_detail_synced": detail_ready,
        "portal_dates": [day.isoformat() for day in days],
        "cards": {
            "total_candidates": len(context["candidates"]),
            "training_centres": _centre_count(conn),
            "active_batches": _active_batch_count(conn),
            "below_threshold": len(below),
            "full_day": counts["full_day"] if detail_ready else None,
            "half_day": counts["half_day"] if detail_ready else None,
            "absent": counts["absent"] if detail_ready else None,
            "insufficient": counts["insufficient"] if detail_ready else None,
            "incomplete": counts["incomplete"] if detail_ready else None,
            "not_verified": counts["not_verified"] if detail_ready else None,
            "average_verified_percent": average,
            "verified_training_days": verified_days,
            "last_sync": dict(last_sync) if last_sync else None,
            "portal_summary": _portal_summary(selected_row),
        },
        "daily": _daily_series(context, stats, indexed),
        "monthly": _monthly_series(context, indexed),
        "distribution": _distribution(stats),
        "below_threshold": below,
        "open_issues": open_issues,
        "generated_at": now_iso(),
    }


def _portal_summary(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "enrolled": row.get("enrolled_count"),
        "present": row.get("present_count"),
        "half_day": row.get("half_day_count"),
        "absent": row.get("absent_count"),
        "rf": row.get("rf_attendance_count"),
        "ojt": row.get("ojt_attendance_count"),
        "logbook": row.get("logbook_status"),
        "detail_synced": bool(row.get("detail_synced")),
    }


def _daily_series(context: dict, stats: list[dict], indexed: dict) -> list[dict]:
    series = []
    candidates = context["candidates"]
    for row in context["dates"]:
        day = parse_date(row["attendance_date"])
        if day is None or day > context["today"]:
            continue
        full = half = absent = verified = 0
        if row.get("detail_synced") or day <= context["today"]:
            for candidate in candidates:
                enrollment, removal = enrollment_window(candidate, context["batch_start"])
                record = indexed.get((int(candidate["id"]), day))
                outcome = score_working_day(
                    day,
                    record,
                    today=context["today"],
                    enrollment=enrollment,
                    removal=removal,
                    batch_end=_batch_end(context),
                )
                if not outcome["include"]:
                    continue
                verified += 1
                if outcome["status"] == "full_day":
                    full += 1
                elif outcome["status"] == "half_day":
                    half += 1
                else:
                    absent += 1
        series.append(
            {
                "date": day.isoformat(),
                "label": day.strftime("%d %b"),
                "working_days": verified,
                "verified_percent": calculate_attendance(full, half, verified) if verified else None,
                "full_day": full if row.get("detail_synced") else None,
                "half_day": half if row.get("detail_synced") else None,
                "absent": absent if row.get("detail_synced") else None,
                "portal_present": row.get("present_count"),
                "portal_half_day": row.get("half_day_count"),
                "portal_absent": row.get("absent_count"),
                "detail_synced": bool(row.get("detail_synced")),
            }
        )
    return series


def _monthly_series(context: dict, indexed: dict) -> list[dict]:
    buckets: dict[str, dict] = {}
    for day in _total_days(context):
        for candidate in context["candidates"]:
            enrollment, removal = enrollment_window(candidate, context["batch_start"])
            outcome = score_working_day(
                day,
                indexed.get((int(candidate["id"]), day)),
                today=context["today"],
                enrollment=enrollment,
                removal=removal,
                batch_end=_batch_end(context),
            )
            if not outcome["include"]:
                continue
            bucket = buckets.setdefault(day.strftime("%Y-%m"), {"full": 0, "half": 0, "days": 0})
            bucket["days"] += 1
            if outcome["status"] == "full_day":
                bucket["full"] += 1
            elif outcome["status"] == "half_day":
                bucket["half"] += 1
    return [
        {
            "month": month,
            "label": datetime.strptime(month, "%Y-%m").strftime("%b %Y"),
            "verified_percent": calculate_attendance(bucket["full"], bucket["half"], bucket["days"]),
        }
        for month, bucket in sorted(buckets.items())
    ]


def _distribution(stats: list[dict]) -> list[dict]:
    counts = {label: 0 for _, _, label in DISTRIBUTION}
    for row in stats:
        percent = row["verified_percent"]
        if percent is None:
            continue
        for low, high, label in DISTRIBUTION:
            if low <= percent < high or (high > 100 and percent == 100):
                counts[label] += 1
                break
    return [{"bucket": label, "count": counts[label]} for _, _, label in DISTRIBUTION]


def matrix(conn: sqlite3.Connection, date_from: str | None = None, date_to: str | None = None) -> dict:
    context = load_context(conn)
    start = parse_date(date_from)
    end = parse_date(date_to)
    by_date = {row["attendance_date"]: row for row in context["dates"]}
    days = []
    for day in _total_days(context):
        if start and day < start:
            continue
        if end and day > end:
            continue
        days.append((day, by_date.get(day.isoformat()) or {}))
    indexed = _records_index(context["records"])
    stats = {row["id"]: row for row in candidate_stats(context)}
    matrix_rows = []
    for candidate in context["candidates"]:
        enrollment, removal = enrollment_window(candidate, context["batch_start"])
        cells = {}
        for day, _row in days:
            record = indexed.get((int(candidate["id"]), day))
            cells[day.isoformat()] = cell_status(
                day,
                record,
                today=context["today"],
                rules=context["rules"],
                enrollment=enrollment,
                removal=removal,
            )
        summary = stats[candidate["id"]]
        matrix_rows.append(
            {
                "id": candidate["id"],
                "name": candidate["name"],
                "candidate_id": candidate["candidate_id"],
                "candidate_status": candidate.get("candidate_status"),
                "verified_percent": summary["verified_percent"],
                "coverage_percent": summary["coverage_percent"],
                "full_days": summary["full_days"],
                "half_days": summary["half_days"],
                "absent_days": summary["absent_days"],
                "cells": cells,
            }
        )
    return {
        "dates": [
            {
                "date": day.isoformat(),
                "label": day.strftime("%d %b"),
                "weekday": day.strftime("%a"),
                "sunday": day.weekday() == 6,
                "detail_synced": bool(row.get("detail_synced")),
                "present": row.get("present_count"),
                "half_day": row.get("half_day_count"),
                "absent": row.get("absent_count"),
            }
            for day, row in days
        ],
        "rows": matrix_rows,
        "candidate_count": len(matrix_rows),
    }


def candidate_profile(conn: sqlite3.Connection, candidate_pk: int) -> dict | None:
    context = load_context(conn)
    candidate = next((item for item in context["candidates"] if int(item["id"]) == int(candidate_pk)), None)
    if candidate is None:
        return None
    stats = next(item for item in candidate_stats(context) if int(item["id"]) == int(candidate_pk))
    enrollment, removal = enrollment_window(candidate, context["batch_start"])
    indexed = _records_index(context["records"])
    timeline = []
    monthly: dict[str, dict] = {}
    for day in _total_days(context):
        record = indexed.get((int(candidate_pk), day))
        cell = cell_status(
            day,
            record,
            today=context["today"],
            rules=context["rules"],
            enrollment=enrollment,
            removal=removal,
        )
        entry = {
            "date": day.isoformat(),
            "label": day.strftime("%d %b %Y"),
            "month": day.strftime("%Y-%m"),
            **cell,
            "in_time": (record or {}).get("in_time"),
            "out_time": (record or {}).get("out_time"),
            "elapsed_minutes": (record or {}).get("elapsed_minutes"),
            "lunch_deduction_minutes": (record or {}).get("lunch_deduction_minutes"),
            "qualifying_minutes": (record or {}).get("qualifying_minutes"),
            "qualifying_label": format_minutes((record or {}).get("qualifying_minutes")),
            "official_categories": _categories(record),
            "last_synced_at": (record or {}).get("last_synced_at"),
            "data_source": (record or {}).get("data_source"),
        }
        timeline.append(entry)
        if cell["counts_toward_percent"]:
            bucket = monthly.setdefault(
                entry["month"],
                {"month": entry["month"], "label": day.strftime("%b %Y"), "full": 0, "half": 0, "days": 0, "minutes": 0},
            )
            bucket["days"] += 1
            if cell["status"] == "full_day":
                bucket["full"] += 1
            elif cell["status"] == "half_day":
                bucket["half"] += 1
            if entry["qualifying_minutes"]:
                bucket["minutes"] += entry["qualifying_minutes"]
    monthly_rows = []
    for bucket in monthly.values():
        monthly_rows.append(
            {
                "month": bucket["month"],
                "label": bucket["label"],
                "verified_percent": calculate_attendance(bucket["full"], bucket["half"], bucket["days"]) if bucket["days"] else None,
                "verified_days": bucket["days"],
                "qualifying_hours": hours_decimal(bucket["minutes"]),
            }
        )
    sessions = [
        dict(row)
        for row in conn.execute(
            """
            SELECT s.*, r.attendance_date
            FROM attendance_sessions s
            JOIN attendance_records r ON r.id = s.record_id
            WHERE r.candidate_pk = ?
            ORDER BY r.attendance_date, s.session_index
            """,
            (candidate_pk,),
        )
    ]
    return {"candidate": stats, "timeline": timeline, "monthly": monthly_rows, "sessions": sessions}


def attendance_cell(conn: sqlite3.Connection, candidate_pk: int, attendance_date: str) -> dict | None:
    day = parse_date(attendance_date)
    if day is None:
        return None
    context = load_context(conn)
    candidate = next((item for item in context["candidates"] if int(item["id"]) == int(candidate_pk)), None)
    if candidate is None:
        return None
    record = conn.execute(
        "SELECT * FROM attendance_records WHERE candidate_pk = ? AND attendance_date = ?",
        (candidate_pk, day.isoformat()),
    ).fetchone()
    record_dict = dict(record) if record else None
    sessions = []
    if record_dict:
        sessions = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM attendance_sessions WHERE record_id = ? ORDER BY session_index",
                (record_dict["id"],),
            )
        ]
        record_dict["official_categories"] = _categories(record_dict)
    enrollment, removal = enrollment_window(candidate, context["batch_start"])
    cell = cell_status(
        day,
        record_dict,
        today=context["today"],
        rules=context["rules"],
        enrollment=enrollment,
        removal=removal,
    )
    date_row = conn.execute(
        "SELECT * FROM attendance_dates WHERE attendance_date = ?",
        (day.isoformat(),),
    ).fetchone()
    return {
        "candidate": {
            "id": candidate["id"],
            "name": candidate["name"],
            "candidate_id": candidate["candidate_id"],
            "attendance_id": candidate.get("attendance_id"),
        },
        "date": day.isoformat(),
        "cell": cell,
        "record": _public_record(record_dict),
        "sessions": sessions,
        "date_summary": dict(date_row) if date_row else None,
        "labels": {"calculated": STATUS_LABELS, "official": OFFICIAL_LABELS},
    }


def _categories(record: dict | None) -> list:
    if not record or not record.get("official_categories"):
        return []
    try:
        value = json.loads(record["official_categories"])
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _public_record(record: dict | None) -> dict | None:
    if record is None:
        return None
    hidden = {"raw_json"}
    payload = {key: value for key, value in record.items() if key not in hidden}
    payload["elapsed_label"] = format_minutes(record.get("elapsed_minutes"))
    payload["lunch_label"] = format_minutes(record.get("lunch_deduction_minutes"))
    payload["qualifying_label"] = format_minutes(record.get("qualifying_minutes"))
    payload["qualifying_hours"] = hours_decimal(record.get("qualifying_minutes"))
    payload["calculated_label"] = STATUS_LABELS.get(record.get("calculated_status"), record.get("calculated_status"))
    payload["official_label"] = OFFICIAL_LABELS.get(record.get("official_status"), record.get("official_status"))
    return payload


def save_settings(conn: sqlite3.Connection, batch: dict, rules: dict) -> dict:
    cleaned_rules = validate_rules(rules)
    required = ("state", "pia", "sanction_order", "training_center")
    cleaned_batch = {key: (batch or {}).get(key, "").strip() for key in (*required, "batch_code", "batch_start_date", "expected_end_date")}
    if any(not cleaned_batch[key] for key in required):
        raise ValueError("State, PIA, sanction order, and training center are required.")
    start = parse_date(cleaned_batch.get("batch_start_date"))
    cleaned_batch["batch_start_date"] = start.isoformat() if start else ""
    cleaned_batch["expected_end_date"] = ""
    if cleaned_batch["batch_code"]:
        upsert_batch(conn, cleaned_batch)
    set_json(conn, "batch", cleaned_batch)
    set_json(conn, "rules", cleaned_rules)
    recalculate_all(conn, cleaned_rules)
    return {"batch": cleaned_batch, "rules": cleaned_rules}


def recalculate_all(conn: sqlite3.Connection, rules: dict | None = None) -> int:
    rules = validate_rules(rules or get_json(conn, "rules") or {})
    clocks = rules_times(rules)
    clip_start = None if rules.get("count_time_outside_schedule", True) else clocks["training_start"]
    clip_end = None if rules.get("count_time_outside_schedule", True) else clocks["training_end"]
    updated = 0
    pending: list[tuple] = []
    records = conn.execute("SELECT * FROM attendance_records").fetchall()
    for record in records:
        sessions = conn.execute(
            "SELECT * FROM attendance_sessions WHERE record_id = ? ORDER BY session_index",
            (record["id"],),
        ).fetchall()
        if record["calculated_status"] == "absent" and not sessions:
            continue
        if not sessions:
            continue
        spans = []
        for session in sessions:
            in_clock, in_invalid = parse_clock(session["in_time"])
            out_clock, out_invalid = parse_clock(session["out_time"])
            spans.append(SessionSpan(in_clock, out_clock, invalid=bool(session["invalid"]) or in_invalid or out_invalid))
        day = parse_date(record["attendance_date"])
        if day is None:
            continue
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
        conn.execute(
            """
            UPDATE attendance_records
            SET calculated_status = ?, elapsed_minutes = ?, lunch_deduction_minutes = ?, qualifying_minutes = ?
            WHERE id = ?
            """,
            (
                result.calculated_status,
                result.elapsed_minutes,
                result.lunch_deduction_minutes,
                result.qualifying_minutes,
                record["id"],
            ),
        )
        updated += 1
        pending.append((int(record["candidate_pk"]), day, result.calculated_status, result.grace_band, int(record["id"])))
    by_candidate: dict[int, list] = {}
    for candidate_pk, day, status, grace, record_id in pending:
        by_candidate.setdefault(candidate_pk, []).append((day, status, grace, record_id))
    for rows in by_candidate.values():
        _full_limit, _absent_after, occasions = sop_limits(scheduled_qualifying_minutes(rules))
        demoted = grace_days_to_demote(
            ((day, grace and status == "full_day") for day, status, grace, _record_id in rows),
            occasions,
        )
        for day, _status, _grace, record_id in rows:
            if day in demoted:
                conn.execute("UPDATE attendance_records SET calculated_status = 'half_day' WHERE id = ?", (record_id,))
    return updated


def daily_export_rows(conn: sqlite3.Connection, attendance_date: str) -> list[dict]:
    context = load_context(conn)
    day = parse_date(attendance_date)
    if day is None:
        raise ValueError("Choose a valid attendance date.")
    indexed = _records_index(context["records"])
    stats = {row["id"]: row for row in candidate_stats(context)}
    rows = []
    for candidate in context["candidates"]:
        enrollment, removal = enrollment_window(candidate, context["batch_start"])
        record = indexed.get((int(candidate["id"]), day))
        cell = cell_status(day, record, today=context["today"], rules=context["rules"], enrollment=enrollment, removal=removal)
        summary = stats[candidate["id"]]
        rows.append(_export_line(candidate, day, record, cell, summary))
    return rows


def monthly_export_rows(conn: sqlite3.Connection, month: str) -> list[dict]:
    if len(month) != 7 or month[4] != "-":
        raise ValueError("Month must look like YYYY-MM.")
    context = load_context(conn)
    indexed = _records_index(context["records"])
    stats = {row["id"]: row for row in candidate_stats(context)}
    rows = []
    for row in context["dates"]:
        day = parse_date(row["attendance_date"])
        if day is None or day.strftime("%Y-%m") != month:
            continue
        for candidate in context["candidates"]:
            enrollment, removal = enrollment_window(candidate, context["batch_start"])
            record = indexed.get((int(candidate["id"]), day))
            cell = cell_status(day, record, today=context["today"], rules=context["rules"], enrollment=enrollment, removal=removal)
            rows.append(_export_line(candidate, day, record, cell, stats[candidate["id"]]))
    return rows


def candidate_export_sheets(conn: sqlite3.Connection) -> list[tuple[str, list[dict]]]:
    context = load_context(conn)
    stats = candidate_stats(context)
    summary = []
    for row in stats:
        summary.append(
            {
                "Candidate Id": row["candidate_id"],
                "Candidate Name": row["name"],
                "Attendance Id": row.get("attendance_id") or "",
                "Batch": row.get("batch_registration_number") or "",
                "Status": row.get("candidate_status") or "",
                "Enrollment Date": row.get("enrollment_date") or "",
                "OJT Start Date": row.get("ojt_start_date") or "",
                "Full Days": row["full_days"],
                "Half Days": row["half_days"],
                "Absent Days": row["absent_days"],
                "Total Working Days": row["eligible_days"],
                "Attendance Credits": row.get("attendance_credits"),
                "Insufficient Hours Days": row["insufficient_days"],
                "Incomplete Records": row["incomplete_days"],
                "Not Yet Verified": row["unverified_days"],
                "Qualifying Hours": row["qualifying_hours"] if row["qualifying_hours"] is not None else "",
                "Attendance %": row["verified_percent"] if row["verified_percent"] is not None else "",
            }
        )
    detail = []
    indexed = _records_index(context["records"])
    for candidate in context["candidates"]:
        enrollment, removal = enrollment_window(candidate, context["batch_start"])
        stat = next(item for item in stats if item["id"] == candidate["id"])
        for row in context["dates"]:
            day = parse_date(row["attendance_date"])
            if day is None:
                continue
            record = indexed.get((int(candidate["id"]), day))
            cell = cell_status(day, record, today=context["today"], rules=context["rules"], enrollment=enrollment, removal=removal)
            detail.append(_export_line(candidate, day, record, cell, stat))
    return [("Candidate Summary", summary), ("Daily Detail", detail)]


def matrix_export_rows(conn: sqlite3.Connection) -> list[dict]:
    payload = matrix(conn)
    rows = []
    for item in payload["rows"]:
        line = {
            "Candidate Id": item["candidate_id"],
            "Candidate Name": item["name"],
        }
        for column in payload["dates"]:
            status = item["cells"][column["date"]]["status"]
            line[column["label"] + " " + column["date"][2:]] = STATUS_LABELS.get(status, status)
        line["Attendance %"] = item["verified_percent"] if item["verified_percent"] is not None else ""
        rows.append(line)
    return rows


def low_attendance_rows(conn: sqlite3.Connection) -> list[dict]:
    context = load_context(conn)
    threshold = float(context["rules"]["low_attendance_threshold"])
    rows = []
    for item in candidate_stats(context):
        if item["verified_percent"] is None or item["verified_percent"] >= threshold:
            continue
        rows.append(
            {
                "Candidate Id": item["candidate_id"],
                "Candidate Name": item["name"],
                "Attendance %": item["verified_percent"],
                "Full Days": item["full_days"],
                "Half Days": item["half_days"],
                "Absent Days": item["absent_days"],
                "Qualifying Hours": item["qualifying_hours"] if item["qualifying_hours"] is not None else "",
                "Threshold %": threshold,
            }
        )
    return rows


def _export_line(candidate: dict, day: date, record: dict | None, cell: dict, summary: dict) -> dict:
    return {
        "Candidate Id": candidate["candidate_id"],
        "Candidate Name": candidate["name"],
        "Date": day.isoformat(),
        "In Time": (record or {}).get("in_time") or "",
        "Out Time": (record or {}).get("out_time") or "",
        "Qualifying Hours": hours_decimal((record or {}).get("qualifying_minutes"))
        if record and record.get("qualifying_minutes") is not None
        else "",
        "Official Portal Status": OFFICIAL_LABELS.get((record or {}).get("official_status"), "") if record else "",
        "Calculated Status": STATUS_LABELS.get(cell["status"], cell["status"]),
        "Attendance %": summary["verified_percent"] if summary["verified_percent"] is not None else "",
    }
