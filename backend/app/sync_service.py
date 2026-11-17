"""Background synchronization. Portal data is only written into the local database."""

from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import timedelta

from .calculator import now_iso, parse_date
from .candidate_sync import filter_dates, group_punches_by_date, reconcile_punch_date
from .db import transaction, upsert_batch
from .matching import clean_id
from .portal import PortalClient, PortalError, SyncCancelled

logger = logging.getLogger("attendance.sync")

PROGRESS_FIELDS = {
    "status",
    "finished_at",
    "progress_step",
    "progress_current",
    "progress_total",
    "dates_discovered",
    "dates_processed",
    "records_saved",
    "message",
    "error_log",
}


class SyncManager:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.login_event = threading.Event()
        self.portal = PortalClient(self)
        self._lock = threading.Lock()
        self._busy = False
        self._jobs: queue.Queue = queue.Queue()
        self._browser_thread = threading.Thread(target=self._browser_loop, name="kaushal-browser", daemon=True)
        self._browser_thread.start()

    def start(self, mode: str, date_from: str | None, date_to: str | None, batch_code: str | None = None) -> int:
        with self._lock:
            if self._busy:
                raise RuntimeError("A sync is already running.")
            self._busy = True
        self.cancel_event.clear()
        self.login_event.clear()
        try:
            with transaction() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO sync_history (
                        started_at, status, mode, date_from, date_to, progress_step, message
                    ) VALUES (?, 'running', ?, ?, ?, 'connecting', 'Connecting to Portal')
                    """,
                    (now_iso(), mode, date_from, date_to),
                )
                sync_id = int(cursor.lastrowid)
            self._jobs.put((sync_id, mode, date_from, date_to, batch_code))
        except Exception:
            with self._lock:
                self._busy = False
            raise
        return sync_id

    def _browser_loop(self) -> None:
        """Playwright can only be used on the thread that started it. This thread never exits."""
        while True:
            sync_id, mode, date_from, date_to, batch_code = self._jobs.get()
            try:
                self._run(sync_id, mode, date_from, date_to, batch_code)
            except Exception:
                logger.exception("Sync %s ended outside the normal handler", sync_id)
            finally:
                with self._lock:
                    self._busy = False

    def cancel(self) -> None:
        self.cancel_event.set()
        self.login_event.set()

    def notify_login(self) -> None:
        self.login_event.set()

    def update(self, sync_id: int, **fields) -> None:
        assignments = []
        values = []
        for key, value in fields.items():
            if key not in PROGRESS_FIELDS:
                continue
            assignments.append(f"{key} = ?")
            values.append(value)
        if not assignments:
            return
        values.append(sync_id)
        with transaction() as conn:
            conn.execute(f"UPDATE sync_history SET {', '.join(assignments)} WHERE id = ?", values)

    def log(self, sync_id: int, message: str) -> None:
        logger.info("sync %s: %s", sync_id, message)
        with transaction() as conn:
            current = conn.execute("SELECT error_log FROM sync_history WHERE id = ?", (sync_id,)).fetchone()
            text = ((current["error_log"] or "") + "\n" + message).strip()
            conn.execute("UPDATE sync_history SET error_log = ? WHERE id = ?", (text[-30000:], sync_id))

    def _run(self, sync_id: int, mode: str, date_from: str | None, date_to: str | None, batch_code: str | None = None) -> None:
        saved = 0
        processed = 0
        try:
            with transaction() as conn:
                from .db import get_json

                batch = get_json(conn, "batch") or {}
                if batch_code:
                    batch["batch_code"] = batch_code
                rules = dict(get_json(conn, "rules") or {})
                stored = conn.execute(
                    "SELECT * FROM batches WHERE batch_code = ?",
                    (batch.get("batch_code"),),
                ).fetchone()
                if stored:
                    for key in ("state", "pia", "sanction_order", "training_center", "batch_code", "batch_start_date", "expected_end_date", "training_start", "training_end", "lunch_start", "lunch_end"):
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
                raise PortalError("Import the master candidate workbook before syncing.")
            warnings = self.portal.prepare_report(batch, sync_id)
            for warning in warnings:
                self.log(sync_id, warning)

            # Fast path: one Candidate ID submit returns that person's full punch history.
            all_punches: list[dict] = []
            candidate_failures = 0
            total_candidates = len(candidates)
            self.update(
                sync_id,
                progress_step="extracting_records",
                progress_current=0,
                progress_total=total_candidates,
                status="running",
                message=f"Reading candidate 0 of {total_candidates}",
            )
            for index, candidate in enumerate(candidates, start=1):
                if self.cancel_event.is_set():
                    raise SyncCancelled("Sync cancelled by the administrator.")
                cid = clean_id(candidate.get("candidate_id"))
                name = candidate.get("name") or cid or f"#{candidate.get('id')}"
                self.update(
                    sync_id,
                    progress_step="extracting_records",
                    progress_current=index,
                    progress_total=total_candidates,
                    message=f"Reading {name} ({index} of {total_candidates})",
                )
                if not cid:
                    self.log(sync_id, f"{name}: skipped — blank Candidate ID on the master list.")
                    candidate_failures += 1
                    continue
                try:
                    rows, notes = self.portal.fetch_candidate_punches(cid)
                    for note in notes:
                        self.log(sync_id, note)
                    for row in rows:
                        if not row.get("candidate_id"):
                            row["candidate_id"] = cid
                        if not row.get("name"):
                            row["name"] = candidate.get("name") or ""
                    all_punches.extend(rows)
                except SyncCancelled:
                    raise
                except Exception as exc:
                    candidate_failures += 1
                    self.log(sync_id, f"{name} ({cid}): {exc}")
                    logger.exception("Candidate %s failed", cid)

            by_date = group_punches_by_date(all_punches)
            discovered_dates = sorted(by_date.keys())
            self.update(sync_id, dates_discovered=len(discovered_dates), message="Grouping punch dates")
            with transaction() as conn:
                existing = {
                    row["attendance_date"]: dict(row)
                    for row in conn.execute("SELECT * FROM attendance_dates WHERE batch_id = ?", (batch_id,))
                }
            selected_dates = filter_dates(
                discovered_dates,
                mode=mode,
                date_from=date_from,
                date_to=date_to,
                existing=existing,
            )
            if mode == "range" and date_from and date_to:
                missing = _dates_not_on_portal(
                    [{"date": day} for day in discovered_dates],
                    date_from,
                    date_to,
                )
                if missing:
                    self.log(
                        sync_id,
                        "No punches were found for these dates, so they were not created: "
                        + ", ".join(missing[:40])
                        + ("…" if len(missing) > 40 else ""),
                    )
            total = len(selected_dates)
            self.update(sync_id, progress_total=total, status="running", message="Calculating attendance")
            if total == 0:
                self.update(
                    sync_id,
                    status="completed" if candidate_failures < total_candidates else "failed",
                    finished_at=now_iso(),
                    progress_step="completed",
                    message="Sync Completed",
                    records_saved=0,
                    dates_processed=0,
                )
                self.log(
                    sync_id,
                    "No attendance dates needed saving."
                    if not candidate_failures
                    else f"Finished with {candidate_failures} candidate read error(s) and no dates to save.",
                )
                return

            failures = 0
            for index, day in enumerate(selected_dates, start=1):
                if self.cancel_event.is_set():
                    raise SyncCancelled("Sync cancelled by the administrator.")
                self.update(
                    sync_id,
                    progress_step="processing_dates",
                    progress_current=index,
                    progress_total=total,
                    message=f"Processing Date {index} of {total}",
                )
                try:
                    attendance_day = parse_date(day)
                    if attendance_day is None:
                        raise PortalError(f"Unreadable attendance date {day}")
                    self.update(sync_id, progress_step="reconciling", message="Calculating attendance")
                    summary, result = reconcile_punch_date(
                        attendance_day=attendance_day,
                        day_punches=by_date.get(day, []),
                        candidates=candidates,
                        rules=rules,
                    )
                    with transaction() as conn:
                        _upsert_summary(conn, batch_id, summary)
                        written = _persist_date(
                            conn,
                            batch_id,
                            summary,
                            result,
                            sync_id,
                            replace=mode in {"full", "range"},
                        )
                    saved += written
                    processed += 1
                    self.update(sync_id, dates_processed=processed, records_saved=saved)
                except SyncCancelled:
                    raise
                except Exception as exc:
                    failures += 1
                    self.log(sync_id, f"{day}: {exc}")
                    logger.exception("Date %s failed", day)
            if self.cancel_event.is_set():
                raise SyncCancelled("Sync cancelled by the administrator.")
            with transaction() as conn:
                from .reporting import recalculate_all

                recalculate_all(conn, rules)
            status = "completed" if failures < max(total, 1) else "failed"
            if failures and failures == total:
                status = "failed"
            if candidate_failures and status == "completed":
                self.log(sync_id, f"{candidate_failures} candidate(s) could not be read; other records were saved.")
            self.update(
                sync_id,
                status=status,
                finished_at=now_iso(),
                progress_step="completed" if status == "completed" else "failed",
                message="Sync Completed" if status == "completed" else "Sync finished with errors. See the log.",
                dates_processed=processed,
                records_saved=saved,
            )
        except SyncCancelled as exc:
            self.update(sync_id, status="cancelled", finished_at=now_iso(), message=str(exc), records_saved=saved)
        except Exception as exc:
            logger.exception("Sync %s failed", sync_id)
            message = _friendly_sync_error(exc)
            self.log(sync_id, message)
            self.update(
                sync_id,
                status="failed",
                finished_at=now_iso(),
                progress_step="failed",
                message=message,
                records_saved=saved,
            )


def _friendly_sync_error(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if "cannot switch to a different thread" in lowered or "has been closed" in lowered or "targetclosed" in lowered:
        return "The Chromium window closed before the portal finished loading. Start the sync again and leave that window open."
    return text


def _should_process(summary: dict, existing: dict | None, mode: str, date_from: str | None, date_to: str | None) -> bool:
    if mode == "range":
        if date_from and summary["date"] < date_from:
            return False
        if date_to and summary["date"] > date_to:
            return False
        return True
    if mode == "full":
        return True
    if existing is None or not existing.get("detail_synced"):
        return True
    comparisons = (
        ("enrolled_count", "enrolled"),
        ("present_count", "present"),
        ("half_day_count", "half_day"),
        ("absent_count", "absent"),
        ("rf_attendance_count", "rf"),
        ("ojt_attendance_count", "ojt"),
    )
    for column, key in comparisons:
        if existing.get(column) != summary.get(key):
            return True
    old_log = existing.get("logbook_status") or None
    new_log = summary.get("logbook") or None
    return old_log != new_log


def _dates_not_on_portal(discovered: list[dict], date_from: str, date_to: str) -> list[str]:
    present = {row["date"] for row in discovered}
    start = parse_date(date_from)
    end = parse_date(date_to)
    if start is None or end is None:
        return []
    missing = []
    cursor = start
    while cursor <= end:
        iso = cursor.isoformat()
        if iso not in present:
            missing.append(iso)
        cursor += timedelta(days=1)
    return missing


def _upsert_summary(conn, batch_id: int, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO attendance_dates (
            batch_id, attendance_date, enrolled_count, present_count, half_day_count,
            absent_count, rf_attendance_count, ojt_attendance_count, logbook_status,
            portal_date_text, detail_synced, portal_synced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        ON CONFLICT(batch_id, attendance_date) DO UPDATE SET
            enrolled_count = excluded.enrolled_count,
            present_count = excluded.present_count,
            half_day_count = excluded.half_day_count,
            absent_count = excluded.absent_count,
            rf_attendance_count = excluded.rf_attendance_count,
            ojt_attendance_count = excluded.ojt_attendance_count,
            logbook_status = excluded.logbook_status,
            portal_date_text = excluded.portal_date_text,
            portal_synced_at = excluded.portal_synced_at
        """,
        (
            batch_id,
            row["date"],
            row.get("enrolled"),
            row.get("present"),
            row.get("half_day"),
            row.get("absent"),
            row.get("rf"),
            row.get("ojt"),
            row.get("logbook"),
            row.get("portal_date"),
            now_iso(),
        ),
    )


def _persist_date(conn, batch_id: int, summary: dict, result, sync_id: int, replace: bool) -> int:
    unmatched = any(issue.issue_type == "unmatched_portal_candidate" for issue in result.issues)
    complete = bool(result.complete) and not unmatched
    if replace and complete:
        keep = [record.candidate_pk for record in result.records]
        counts_empty = all((summary.get(key) or 0) == 0 for key in ("present", "half_day", "absent", "rf", "ojt"))
        if keep:
            marks = ",".join("?" for _ in keep)
            conn.execute(
                f"""
                DELETE FROM attendance_records
                WHERE batch_id = ? AND attendance_date = ? AND candidate_pk NOT IN ({marks})
                """,
                [batch_id, summary["date"], *keep],
            )
        elif counts_empty:
            conn.execute(
                "DELETE FROM attendance_records WHERE batch_id = ? AND attendance_date = ?",
                (batch_id, summary["date"]),
            )
    now = now_iso()
    for record in result.records:
        conn.execute(
            """
            INSERT INTO attendance_records (
                candidate_pk, attendance_date, batch_id, official_status, official_categories,
                calculated_status, in_time, out_time, elapsed_minutes, lunch_deduction_minutes,
                qualifying_minutes, device_type, in_device_id, out_device_id, portal_candidate_name,
                data_source, last_synced_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_pk, attendance_date) DO UPDATE SET
                batch_id = excluded.batch_id,
                official_status = excluded.official_status,
                official_categories = excluded.official_categories,
                calculated_status = excluded.calculated_status,
                in_time = excluded.in_time,
                out_time = excluded.out_time,
                elapsed_minutes = excluded.elapsed_minutes,
                lunch_deduction_minutes = excluded.lunch_deduction_minutes,
                qualifying_minutes = excluded.qualifying_minutes,
                device_type = excluded.device_type,
                in_device_id = excluded.in_device_id,
                out_device_id = excluded.out_device_id,
                portal_candidate_name = excluded.portal_candidate_name,
                data_source = excluded.data_source,
                last_synced_at = excluded.last_synced_at,
                raw_json = excluded.raw_json
            """,
            (
                record.candidate_pk,
                summary["date"],
                batch_id,
                record.official_status,
                json.dumps(record.official_categories),
                record.calculated_status,
                record.in_time,
                record.out_time,
                record.elapsed_minutes,
                record.lunch_deduction_minutes,
                record.qualifying_minutes,
                record.device_type,
                record.in_device_id,
                record.out_device_id,
                record.portal_name,
                record.data_source,
                now,
                json.dumps(record.raw),
            ),
        )
        stored = conn.execute(
            "SELECT id FROM attendance_records WHERE candidate_pk = ? AND attendance_date = ?",
            (record.candidate_pk, summary["date"]),
        ).fetchone()
        conn.execute("DELETE FROM attendance_sessions WHERE record_id = ?", (stored["id"],))
        for index, session in enumerate(record.sessions):
            conn.execute(
                """
                INSERT INTO attendance_sessions (
                    record_id, session_index, in_time, out_time, device_type,
                    in_device_id, out_device_id, portal_attendance_id, invalid
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored["id"],
                    index,
                    session.in_time,
                    session.out_time,
                    session.device_type,
                    session.in_device_id,
                    session.out_device_id,
                    session.portal_attendance_id,
                    1 if session.invalid else 0,
                ),
            )
    conn.execute(
        "DELETE FROM reconciliation_issues WHERE attendance_date = ? AND resolved = 0",
        (summary["date"],),
    )
    conn.execute(
        """
        UPDATE attendance_dates
        SET detail_synced = 1, portal_synced_at = ?
        WHERE batch_id = ? AND attendance_date = ?
        """,
        (now, batch_id, summary["date"]),
    )
    return len(result.records)


manager = SyncManager()
