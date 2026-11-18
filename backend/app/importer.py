"""Import the master candidate workbook."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from .calculator import parse_date
from .db import get_json, transaction, upsert_batch
from .matching import clean_id, clean_text, normalize_name

HEADER_MAP = {
    "sr no": "sr_no",
    "s no": "sr_no",
    "sno": "sr_no",
    "candidate id": "candidate_id",
    "kp id": "candidate_id",
    "kpid": "candidate_id",
    "kaushal panjee id": "candidate_id",
    "kaushal panjee id kpid": "candidate_id",
    "candidate name": "name",
    "attendance id": "attendance_id",
    "batch registration number": "batch_registration_number",
    "candidate status": "candidate_status",
    "candidate add/remove date": "add_remove_date",
    "candidate add remove date": "add_remove_date",
    "ojt start date": "ojt_start_date",
}


def _header_key(value: object) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", " ", clean_text(value).lower()).strip()


def import_workbook(path: Path, batch: Optional[dict] = None) -> dict:
    frame = pd.read_excel(path, dtype=str, engine="openpyxl")
    renamed = {}
    for column in frame.columns:
        key = HEADER_MAP.get(_header_key(column))
        if key and key not in renamed.values():
            renamed[column] = key
    working = frame.rename(columns=renamed)
    missing = [name for name in ("candidate_id", "name") if name not in working.columns]
    if missing:
        labels = {"candidate_id": "KP Id", "name": "Candidate Name"}
        raise ValueError("The workbook is missing required columns: " + ", ".join(labels[name] for name in missing))

    errors: list[str] = []
    warnings: list[str] = []
    prepared: list[dict] = []
    seen_ids: set[str] = set()
    for index, row in working.iterrows():
        candidate_id = clean_id(row.get("candidate_id"))
        name = clean_text(row.get("name"))
        line = int(index) + 2
        if not candidate_id:
            errors.append(f"Row {line} has no KP Id and was skipped.")
            continue
        if not name:
            errors.append(f"Row {line} ({candidate_id}) has no Candidate Name and was skipped.")
            continue
        if candidate_id in seen_ids:
            errors.append(f"Row {line} repeats KP Id {candidate_id} and was skipped.")
            continue
        # Attendance Id is stored for display only — never used as a match key.
        attendance_id = clean_id(row.get("attendance_id")) if "attendance_id" in working.columns else ""
        seen_ids.add(candidate_id)
        sr_no = _optional_int(row.get("sr_no")) if "sr_no" in working.columns else None
        prepared.append(
            {
                "sr_no": sr_no,
                "candidate_id": candidate_id,
                "name": name,
                "attendance_id": attendance_id or None,
                "batch_registration_number": clean_text(row.get("batch_registration_number")) or None,
                "candidate_status": clean_text(row.get("candidate_status")) or None,
                "add_remove_date": _date_or_none(row.get("add_remove_date")),
                "ojt_start_date": _date_or_none(row.get("ojt_start_date")),
            }
        )

    if not prepared:
        raise ValueError("No candidate rows could be imported. " + " ".join(errors[:5]))

    batch_codes = {item["batch_registration_number"] for item in prepared if item["batch_registration_number"]}
    with transaction() as conn:
        stored = get_json(conn, "batch") or {}
        batch = {**stored, **(batch or {})}
        if len(batch_codes) == 1:
            only = next(iter(batch_codes))
            if batch.get("batch_code") and batch.get("batch_code") != only:
                warnings.append(
                    f"The workbook batch {only} differs from the configured batch {batch.get('batch_code')}."
                )
            batch["batch_code"] = batch.get("batch_code") or only
        elif len(batch_codes) > 1:
            warnings.append("The workbook contains more than one batch registration number. Candidates were still stored.")
        from .auth import actor
        from .db import set_json

        if (actor() or {}).get("role") != "centre":
            set_json(conn, "batch", batch)
        batch_id = upsert_batch(conn, batch)
        inserted = 0
        updated = 0
        for item in prepared:
            existing = conn.execute(
                "SELECT id, name FROM candidates WHERE candidate_id = ?",
                (item["candidate_id"],),
            ).fetchone()
            if existing and normalize_name(existing["name"]) != normalize_name(item["name"]):
                warnings.append(
                    f"Candidate {item['candidate_id']} name changed from '{existing['name']}' to '{item['name']}'."
                )
            if item["attendance_id"]:
                attendance_owner = conn.execute(
                    "SELECT candidate_id FROM candidates WHERE attendance_id = ? AND candidate_id != ?",
                    (item["attendance_id"], item["candidate_id"]),
                ).fetchone()
                if attendance_owner:
                    warnings.append(
                        f"Attendance Id {item['attendance_id']} is also on {attendance_owner['candidate_id']}. {item['candidate_id']} was still stored by KP Id."
                    )
                    item["attendance_id"] = None
            before = conn.execute("SELECT id FROM candidates WHERE candidate_id = ?", (item["candidate_id"],)).fetchone()
            conn.execute(
                """
                INSERT INTO candidates (
                    candidate_id, attendance_id, name, sr_no, batch_registration_number,
                    candidate_status, add_remove_date, ojt_start_date, batch_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(candidate_id) DO UPDATE SET
                    attendance_id = excluded.attendance_id,
                    name = excluded.name,
                    sr_no = excluded.sr_no,
                    batch_registration_number = excluded.batch_registration_number,
                    candidate_status = excluded.candidate_status,
                    add_remove_date = excluded.add_remove_date,
                    ojt_start_date = excluded.ojt_start_date,
                    batch_id = excluded.batch_id,
                    updated_at = datetime('now')
                """,
                (
                    item["candidate_id"],
                    item["attendance_id"],
                    item["name"],
                    item["sr_no"],
                    item["batch_registration_number"],
                    item["candidate_status"],
                    item["add_remove_date"],
                    item["ojt_start_date"],
                    batch_id,
                ),
            )
            if before:
                updated += 1
            else:
                inserted += 1
        total = conn.execute("SELECT COUNT(*) AS count FROM candidates").fetchone()["count"]
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": len([message for message in errors if "skipped" in message or "belongs" in message]),
        "total_candidates": total,
        "errors": errors,
        "warnings": warnings,
    }


def _optional_int(value: object) -> Optional[int]:
    text = clean_text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _date_or_none(value: object) -> Optional[str]:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else None
