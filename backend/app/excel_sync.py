"""Import Candidate Attendance Report Excel files downloaded from Kaushal.

This is the reliable online sync path when the cloud browser gets 401 from Kaushal.
Administrators open Kaushal in their own browser, download Excel, and upload here.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from .calculator import now_iso, parse_date, validate_rules
from .candidate_sync import filter_dates, group_punches_by_date, reconcile_punch_date
from .db import get_json, transaction, upsert_batch
from .portal_parse import parse_candidate_punch_excel
from .reporting import recalculate_all

logger = logging.getLogger("attendance.excel_sync")


def import_punch_excels(
    paths: list[Path],
    *,
    mode: str = "full",
    date_from: str | None = None,
    date_to: str | None = None,
    batch_code: str | None = None,
) -> dict:
    from .sync_service import _persist_date, _upsert_summary

    if not paths:
        raise ValueError("Choose at least one Excel file downloaded from Kaushal.")

    all_punches: list[dict] = []
    warnings: list[str] = []
    for path in paths:
        rows, notes = parse_candidate_punch_excel(path)
        warnings.extend(notes)
        if not rows:
            warnings.append(f"{path.name}: no punch rows were found.")
            continue
        all_punches.extend(rows)

    if not all_punches:
        raise ValueError("No attendance punch rows were found in the uploaded Excel file(s).")

    with transaction() as conn:
        batch = dict(get_json(conn, "batch") or {})
        if batch_code:
            batch["batch_code"] = batch_code
        rules = validate_rules(dict(get_json(conn, "rules") or {}))
        stored = conn.execute(
            "SELECT * FROM batches WHERE batch_code = ?",
            (batch.get("batch_code"),),
        ).fetchone()
        if stored:
            for key in (
                "state",
                "pia",
                "sanction_order",
                "training_center",
                "batch_code",
                "batch_start_date",
                "expected_end_date",
                "training_start",
                "training_end",
                "lunch_start",
                "lunch_end",
            ):
                if stored[key]:
                    if key in {"training_start", "training_end", "lunch_start", "lunch_end"}:
                        rules[key] = stored[key]
                    else:
                        batch[key] = stored[key]
        batch_id = upsert_batch(conn, batch)
        candidates = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM candidates
                WHERE batch_id = ? OR batch_registration_number = ?
                ORDER BY sr_no, id
                """,
                (batch_id, batch.get("batch_code")),
            )
        ]
        if not candidates:
            raise ValueError("Import the master candidate workbook before uploading attendance Excel.")

        batch_start = parse_date(batch.get("batch_start_date"))
        kept_punches, skipped_before = _drop_before_batch_start(all_punches, batch_start)
        if skipped_before:
            start_label = batch_start.isoformat() if batch_start else "batch start"
            warnings.append(
                f"Ignored {skipped_before} punch row(s) dated before batch start {start_label}."
            )
        if not kept_punches:
            raise ValueError(
                "Every punch row was before the batch start date, so nothing was imported. "
                f"Batch start is {batch_start.isoformat() if batch_start else 'not set'}."
            )

        cursor = conn.execute(
            """
            INSERT INTO sync_history (
                started_at, status, mode, date_from, date_to, progress_step, message
            ) VALUES (?, 'running', ?, ?, ?, 'extracting_records', 'Importing Kaushal Excel')
            """,
            (now_iso(), f"excel-{mode}", date_from, date_to),
        )
        sync_id = int(cursor.lastrowid)

        by_date = group_punches_by_date(kept_punches)
        discovered = sorted(by_date.keys())
        existing = {
            row["attendance_date"]: dict(row)
            for row in conn.execute("SELECT * FROM attendance_dates WHERE batch_id = ?", (batch_id,))
        }
        selected = filter_dates(
            discovered,
            mode=mode,
            date_from=date_from,
            date_to=date_to,
            existing=existing,
        )
        # Never import calendar days before the configured batch start.
        if batch_start is not None:
            start_iso = batch_start.isoformat()
            before_selected = [day for day in selected if day < start_iso]
            selected = [day for day in selected if day >= start_iso]
            if before_selected:
                warnings.append(
                    f"Skipped date(s) before batch start {start_iso}: "
                    + ", ".join(before_selected[:20])
                    + ("…" if len(before_selected) > 20 else "")
                )
        conn.execute(
            "UPDATE sync_history SET dates_discovered = ?, progress_total = ? WHERE id = ?",
            (len(discovered), len(selected), sync_id),
        )

        saved = 0
        processed = 0
        for day in selected:
            attendance_day = parse_date(day)
            if attendance_day is None:
                warnings.append(f"Skipped unreadable date {day}.")
                continue
            summary, result = reconcile_punch_date(
                attendance_day=attendance_day,
                day_punches=by_date.get(day, []),
                candidates=candidates,
                rules=rules,
            )
            _upsert_summary(conn, batch_id, summary)
            written = _persist_date(
                conn,
                batch_id,
                summary,
                result,
                sync_id,
                replace=True,
            )
            saved += written
            processed += 1

        recalculate_all(conn, rules)
        message = (
            f"Imported {processed} date(s), {saved} record(s) from {len(paths)} Excel file(s)."
            + (f" Ignored {skipped_before} row(s) before batch start." if skipped_before else "")
        )
        if warnings:
            log = "\n".join(warnings[-40:])
            conn.execute(
                "UPDATE sync_history SET error_log = ? WHERE id = ?",
                (log[-30000:], sync_id),
            )
        conn.execute(
            """
            UPDATE sync_history SET
                status = 'completed',
                finished_at = ?,
                progress_step = 'completed',
                message = ?,
                dates_processed = ?,
                records_saved = ?
            WHERE id = ?
            """,
            (now_iso(), message, processed, saved, sync_id),
        )

    return {
        "sync_id": sync_id,
        "dates_discovered": len(discovered),
        "dates_processed": processed,
        "records_saved": saved,
        "files": len(paths),
        "skipped_before_batch_start": skipped_before,
        "batch_start_date": batch_start.isoformat() if batch_start else None,
        "warnings": warnings[:30],
        "message": message,
    }


def _drop_before_batch_start(punches: list[dict], batch_start) -> tuple[list[dict], int]:
    """Keep only punches on/after batch start. Rows with no readable date are kept for later warnings."""
    if batch_start is None:
        return punches, 0
    kept: list[dict] = []
    skipped = 0
    start_iso = batch_start.isoformat()
    for row in punches:
        day = row.get("attendance_date") or row.get("consume_date")
        parsed = parse_date(day) if day else None
        if parsed is not None and parsed.isoformat() < start_iso:
            skipped += 1
            continue
        kept.append(row)
    return kept, skipped
