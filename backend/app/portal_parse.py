"""Parse attendance tables copied from the Kaushal portal.

Parsing accepts only cells that are present. Missing dates are not created.
"""

from __future__ import annotations

import re
from typing import Optional

from .calculator import parse_clock, parse_date
from .matching import clean_id, clean_text

SUMMARY_HEADERS = {
    "sr no": "sr_no",
    "s no": "sr_no",
    "sno": "sr_no",
    "date": "date",
    "attendance date": "date",
    "number of candidates enrolled": "enrolled",
    "no of candidates enrolled": "enrolled",
    "candidates enrolled": "enrolled",
    "enrolled": "enrolled",
    "present": "present",
    "half day": "half_day",
    "halfday": "half_day",
    "absent": "absent",
    "rf attendance": "rf",
    "rf": "rf",
    "ojt attendance": "ojt",
    "ojt": "ojt",
    "logbook status": "logbook",
    "log book status": "logbook",
}

DETAIL_HEADERS = {
    "sr no": "sr_no",
    "s no": "sr_no",
    "candidate id": "candidate_id",
    "candidate name": "name",
    "attendance id": "attendance_id",
    "device type": "device_type",
    "attendance date": "attendance_date",
    "date": "attendance_date",
    "in time": "in_time",
    "in device id": "in_device_id",
    "out time": "out_time",
    "out device id": "out_device_id",
}

# Candidate Attendance Report (by Candidate ID) — punch history table.
CANDIDATE_PUNCH_HEADERS = {
    "sr no": "sr_no",
    "s no": "sr_no",
    "candidate id": "candidate_id",
    "attendance id": "attendance_id",
    "batch reg no": "batch_reg_no",
    "batch registration number": "batch_reg_no",
    "consume date": "consume_date",
    "log date": "log_date",
    "attendance date": "consume_date",
    "date": "consume_date",
    "in time": "in_time",
    "in device id": "in_device_id",
    "out time": "out_time",
    "out device id": "out_device_id",
    "latitude": "latitude",
    "longitude": "longitude",
    "location": "location",
    "center name": "location",
    "centre name": "location",
    "location center name": "location",
}


def norm_header(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_text(value).lower()).strip()


def header_map(headers: list[object], catalog: dict[str, str]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    used: set[str] = set()
    for index, header in enumerate(headers):
        key = catalog.get(norm_header(header))
        if key and key not in used:
            mapping[index] = key
            used.add(key)
    return mapping


def parse_count(value: object) -> Optional[int]:
    text = clean_text(value).replace(",", "")
    if not text:
        return None
    match = re.search(r"-?\d+", text)
    if not match:
        return None
    return int(match.group())


def _project(headers: list[object], row: list[object], mapping: dict[int, str]) -> dict:
    projected = {field: "" for field in mapping.values()}
    for index, field in mapping.items():
        if index < len(row):
            projected[field] = clean_text(row[index])
    return projected


def parse_summary_table(headers: list[object], rows: list[list[object]]) -> tuple[list[dict], list[str]]:
    mapping = header_map(headers, SUMMARY_HEADERS)
    warnings: list[str] = []
    if "date" not in mapping.values() or "present" not in mapping.values():
        return [], ["Summary table is missing Date or Present."]
    parsed: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not any(clean_text(cell) for cell in row):
            continue
        projected = _project(headers, row, mapping)
        day = parse_date(projected.get("date"))
        if day is None:
            warnings.append(f"Skipped a summary row with an unreadable date: {projected.get('date') or 'blank'}.")
            continue
        iso = day.isoformat()
        if iso in seen:
            warnings.append(f"Duplicate summary row for {iso} was ignored.")
            continue
        seen.add(iso)
        parsed.append(
            {
                "date": iso,
                "portal_date": projected.get("date") or iso,
                "sr_no": parse_count(projected.get("sr_no")),
                "enrolled": parse_count(projected.get("enrolled")),
                "present": parse_count(projected.get("present")),
                "half_day": parse_count(projected.get("half_day")),
                "absent": parse_count(projected.get("absent")),
                "rf": parse_count(projected.get("rf")),
                "ojt": parse_count(projected.get("ojt")),
                "logbook": projected.get("logbook") or None,
            }
        )
    return parsed, warnings


def parse_detail_table(
    headers: list[object],
    rows: list[list[object]],
    category: str,
) -> tuple[list[dict], list[str]]:
    mapping = header_map(headers, DETAIL_HEADERS)
    warnings: list[str] = []
    if "candidate_id" not in mapping.values() and "attendance_id" not in mapping.values() and "name" not in mapping.values():
        return [], [f"{category} detail table has no candidate identifier column."]
    parsed: list[dict] = []
    for row in rows:
        if not any(clean_text(cell) for cell in row):
            continue
        projected = _project(headers, row, mapping)
        in_clock, in_invalid = parse_clock(projected.get("in_time"))
        out_clock, out_invalid = parse_clock(projected.get("out_time"))
        day = parse_date(projected.get("attendance_date"))
        parsed.append(
            {
                "category": category,
                "sr_no": parse_count(projected.get("sr_no")),
                "candidate_id": clean_id(projected.get("candidate_id")),
                "name": clean_text(projected.get("name")),
                "attendance_id": clean_id(projected.get("attendance_id")),
                "device_type": projected.get("device_type") or None,
                "attendance_date": day.isoformat() if day else None,
                "portal_attendance_date": projected.get("attendance_date") or None,
                "in_time": in_clock.strftime("%H:%M:%S") if in_clock else None,
                "out_time": out_clock.strftime("%H:%M:%S") if out_clock else None,
                "in_invalid": in_invalid,
                "out_invalid": out_invalid,
                "in_device_id": projected.get("in_device_id") or None,
                "out_device_id": projected.get("out_device_id") or None,
            }
        )
    return parsed, warnings


def detail_key(row: dict) -> tuple:
    return (
        row.get("candidate_id") or "",
        row.get("attendance_id") or "",
        row.get("name") or "",
        row.get("in_time") or "",
        row.get("out_time") or "",
        row.get("in_device_id") or "",
        row.get("out_device_id") or "",
        row.get("category") or "",
        row.get("sr_no") if row.get("sr_no") is not None else "",
    )


def parse_candidate_punch_table(
    headers: list[object],
    rows: list[list[object]],
) -> tuple[list[dict], list[str]]:
    """Parse the Candidate ID attendance report (Consume Date + In/Out punches)."""
    mapping = header_map(headers, CANDIDATE_PUNCH_HEADERS)
    warnings: list[str] = []
    fields = set(mapping.values())
    if "candidate_id" not in fields and "attendance_id" not in fields:
        return [], ["Candidate punch table has no Candidate ID or Attendance ID column."]
    if "consume_date" not in fields and "in_time" not in fields:
        return [], ["Candidate punch table has no Consume Date or In Time column."]
    parsed: list[dict] = []
    for row in rows:
        if not any(clean_text(cell) for cell in row):
            continue
        projected = _project(headers, row, mapping)
        day = (
            parse_date(projected.get("consume_date"))
            or parse_date(projected.get("log_date"))
            or parse_date(projected.get("in_time"))
        )
        in_clock, in_invalid = parse_clock(projected.get("in_time"))
        out_clock, out_invalid = parse_clock(projected.get("out_time"))
        if day is None and not in_clock and not out_clock:
            warnings.append(
                f"Skipped a punch row with no readable date or time: {projected.get('consume_date') or 'blank'}."
            )
            continue
        parsed.append(
            {
                "sr_no": parse_count(projected.get("sr_no")),
                "candidate_id": clean_id(projected.get("candidate_id")),
                "attendance_id": clean_id(projected.get("attendance_id")),
                "batch_reg_no": clean_text(projected.get("batch_reg_no")) or None,
                "attendance_date": day.isoformat() if day else None,
                "portal_attendance_date": projected.get("consume_date") or projected.get("log_date") or None,
                "consume_date": day.isoformat() if day else None,
                "log_date": (parse_date(projected.get("log_date")).isoformat() if parse_date(projected.get("log_date")) else None),
                "in_time": in_clock.strftime("%H:%M:%S") if in_clock else None,
                "out_time": out_clock.strftime("%H:%M:%S") if out_clock else None,
                "in_invalid": in_invalid,
                "out_invalid": out_invalid,
                "in_device_id": projected.get("in_device_id") or None,
                "out_device_id": projected.get("out_device_id") or None,
                "latitude": projected.get("latitude") or None,
                "longitude": projected.get("longitude") or None,
                "location": projected.get("location") or None,
                "device_type": None,
                "name": "",
            }
        )
    return parsed, warnings


def parse_candidate_punch_excel(path: object) -> tuple[list[dict], list[str]]:
    """Parse a portal 'Download Excel' file from the Candidate ID report."""
    import pandas as pd

    frame = pd.read_excel(path, dtype=str)
    frame = frame.fillna("")
    headers = [str(column) for column in frame.columns]
    rows = frame.astype(str).values.tolist()
    return parse_candidate_punch_table(headers, rows)
