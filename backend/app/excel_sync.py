"""Import Candidate Attendance Report Excel files downloaded from Kaushal.

This is the reliable online sync path when the cloud browser gets 401 from Kaushal.
Administrators open Kaushal in their own browser, download Excel, and upload here.

Each Excel row carries Batch Reg No. Imports are routed to that batch so selecting
the wrong batch on screen cannot wipe another centre's attendance.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from .calculator import now_iso, parse_date, validate_rules
from .candidate_sync import filter_dates, group_punches_by_date, reconcile_punch_date
from .db import get_json, transaction, upsert_batch
from .matching import clean_id, clean_text
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
        active = dict(get_json(conn, "batch") or {})
        if batch_code:
            active["batch_code"] = batch_code
        active_code = clean_text(active.get("batch_code") or "") or None
        global_rules = validate_rules(dict(get_json(conn, "rules") or {}))

        by_batch = _group_punches_by_batch(all_punches, fallback_batch=active_code)
        if not by_batch:
            raise ValueError("Excel rows had no Batch Reg No. and no batch is selected on screen.")

        unknown = [code for code in by_batch if not conn.execute("SELECT id FROM batches WHERE batch_code = ?", (code,)).fetchone()]
        for code in unknown:
            warnings.append(f"No local batch {code} — create that batch and import its master workbook first. Those Excel rows were skipped.")
            by_batch.pop(code, None)
        if not by_batch:
            raise ValueError(
                "Excel Batch Reg No. did not match any batch in this app. "
                "Select or create the correct batch, then upload again."
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

        total_discovered = 0
        total_processed = 0
        total_saved = 0
        total_skipped_before = 0
        total_unmatched = 0
        batch_messages: list[str] = []

        for code, punches in sorted(by_batch.items()):
            batch_row = conn.execute("SELECT * FROM batches WHERE batch_code = ?", (code,)).fetchone()
            batch = {key: batch_row[key] for key in batch_row.keys()}
            rules = dict(global_rules)
            for key in ("training_start", "training_end", "lunch_start", "lunch_end"):
                if batch.get(key):
                    rules[key] = batch[key]
            rules = validate_rules(rules)
            batch_id = upsert_batch(conn, batch)
            candidates = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM candidates
                    WHERE batch_id = ? OR batch_registration_number = ?
                    ORDER BY sr_no, id
                    """,
                    (batch_id, code),
                )
            ]
            if not candidates:
                warnings.append(f"{code}: no candidates — import the master workbook for this batch first.")
                continue

            candidate_ids = {clean_id(row.get("candidate_id")) for row in candidates if clean_id(row.get("candidate_id"))}
            matched_punches = []
            unmatched = 0
            for row in punches:
                cid = clean_id(row.get("candidate_id"))
                if cid and cid in candidate_ids:
                    matched_punches.append(row)
                else:
                    unmatched += 1
            total_unmatched += unmatched
            if unmatched:
                warnings.append(
                    f"{code}: {unmatched} punch row(s) did not match any candidate on this batch and were skipped."
                )
            if not matched_punches:
                warnings.append(
                    f"{code}: Excel punches did not match this batch's candidates, so nothing was written "
                    "(existing attendance was left unchanged)."
                )
                continue

            batch_start = parse_date(batch.get("batch_start_date"))
            kept_punches, skipped_before = _drop_before_batch_start(matched_punches, batch_start)
            total_skipped_before += skipped_before
            if skipped_before:
                start_label = batch_start.isoformat() if batch_start else "batch start"
                warnings.append(
                    f"{code}: ignored {skipped_before} punch row(s) dated before batch start {start_label}."
                )
            if not kept_punches:
                warnings.append(f"{code}: every matched punch was before the batch start date.")
                continue

            by_date = group_punches_by_date(kept_punches)
            discovered = sorted(by_date.keys())
            total_discovered += len(discovered)
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
            if batch_start is not None:
                start_iso = batch_start.isoformat()
                selected = [day for day in selected if day >= start_iso]

            saved = 0
            processed = 0
            for day in selected:
                attendance_day = parse_date(day)
                if attendance_day is None:
                    warnings.append(f"{code}: skipped unreadable date {day}.")
                    continue
                day_punches = by_date.get(day, [])
                if not day_punches:
                    continue
                summary, result = reconcile_punch_date(
                    attendance_day=attendance_day,
                    day_punches=day_punches,
                    candidates=candidates,
                    rules=rules,
                )
                matched_records = [row for row in result.records if _record_has_punch(row)]
                if not matched_records:
                    warnings.append(
                        f"{code} {day}: punches did not attach to candidates; existing marks for that day were kept."
                    )
                    continue
                # Only replace/write when at least one punch matched a candidate.
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

            total_processed += processed
            total_saved += saved
            batch_messages.append(f"{code}: {processed} date(s), {saved} record(s)")

        recalculate_all(conn, global_rules)
        if total_processed == 0 and total_saved == 0:
            message = (
                "No attendance was written. Excel Batch Reg No. or Candidate IDs did not match the "
                "selected centre batches. Existing stored attendance was left unchanged."
            )
            status = "failed"
        else:
            message = (
                f"Imported {total_processed} date(s), {total_saved} record(s) from {len(paths)} Excel file(s). "
                + "; ".join(batch_messages)
            )
            if total_skipped_before:
                message += f" Ignored {total_skipped_before} row(s) before batch start."
            if total_unmatched:
                message += f" Skipped {total_unmatched} unmatched candidate row(s)."
            status = "completed"

        if warnings:
            log = "\n".join(warnings[-60:])
            conn.execute(
                "UPDATE sync_history SET error_log = ? WHERE id = ?",
                (log[-30000:], sync_id),
            )
        conn.execute(
            """
            UPDATE sync_history SET
                status = ?,
                finished_at = ?,
                progress_step = ?,
                message = ?,
                dates_discovered = ?,
                dates_processed = ?,
                records_saved = ?,
                progress_total = ?
            WHERE id = ?
            """,
            (
                status,
                now_iso(),
                status,
                message,
                total_discovered,
                total_processed,
                total_saved,
                total_processed,
                sync_id,
            ),
        )

    if status == "failed":
        raise ValueError(message)

    return {
        "sync_id": sync_id,
        "dates_discovered": total_discovered,
        "dates_processed": total_processed,
        "records_saved": total_saved,
        "files": len(paths),
        "skipped_before_batch_start": total_skipped_before,
        "unmatched_candidate_rows": total_unmatched,
        "batches": batch_messages,
        "warnings": warnings[:40],
        "message": message,
    }


def _group_punches_by_batch(punches: list[dict], *, fallback_batch: str | None) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in punches:
        code = clean_text(row.get("batch_reg_no") or "") or fallback_batch
        if not code:
            continue
        grouped[code].append(row)
    return dict(grouped)


def _record_has_punch(record) -> bool:
    if getattr(record, "in_time", None) or getattr(record, "out_time", None):
        return True
    for session in getattr(record, "sessions", None) or []:
        if getattr(session, "in_time", None) or getattr(session, "out_time", None):
            return True
    return False


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
