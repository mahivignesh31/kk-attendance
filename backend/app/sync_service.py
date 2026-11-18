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

    def start(
        self,
        mode: str,
        date_from: str | None,
        date_to: str | None,
        batch_code: str | None = None,
        batch_codes: list[str] | None = None,
    ) -> int:
        codes = [code for code in (batch_codes or []) if code]
        if not codes and batch_code:
            codes = [batch_code]
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
            self._jobs.put((sync_id, mode, date_from, date_to, codes or None))
        except Exception:
            with self._lock:
                self._busy = False
            raise
        return sync_id

    def _browser_loop(self) -> None:
        """Playwright can only be used on the thread that started it. This thread never exits."""
        while True:
            job = self._jobs.get()
            if job and job[0] == "__open_live__":
                try:
                    from .config import PORTAL_CANDIDATE_DETAILS_URL

                    self.portal.open_browser(force=True)
                    try:
                        self.portal._goto(PORTAL_CANDIDATE_DETAILS_URL)
                    except Exception:
                        logger.info("Chromium live window opened — sign into Kaushal if the report is not visible yet")
                except Exception:
                    logger.exception("Could not open the Chromium live window")
                continue
            sync_id, mode, date_from, date_to, batch_codes = job
            try:
                self._run(sync_id, mode, date_from, date_to, batch_codes)
            except Exception:
                logger.exception("Sync %s ended outside the normal handler", sync_id)
            finally:
                with self._lock:
                    self._busy = False

    def request_open_live_window(self) -> None:
        """Queue Chromium open on the Playwright browser thread (never from a request thread)."""
        self._jobs.put(("__open_live__", None, None, None, None))

    def cancel(self) -> None:
        self.cancel_event.set()
        self.login_event.set()
        with transaction() as conn:
            row = conn.execute(
                """
                SELECT id FROM sync_history
                WHERE status IN ('running', 'awaiting_login', 'cancelling')
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE sync_history
                    SET status = 'cancelling',
                        progress_step = 'cancelling',
                        message = 'Stopping sync…'
                    WHERE id = ?
                    """,
                    (row["id"],),
                )

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

    def _run(
        self,
        sync_id: int,
        mode: str,
        date_from: str | None,
        date_to: str | None,
        batch_codes: list[str] | None = None,
    ) -> None:
        saved = 0
        processed = 0
        try:
            with transaction() as conn:
                from .db import get_json

                active = get_json(conn, "batch") or {}
                rules_base = dict(get_json(conn, "rules") or {})
                if batch_codes:
                    codes = list(batch_codes)
                else:
                    code = (active.get("batch_code") or "").strip()
                    if not code:
                        raise PortalError("Select a training centre (or batch) before syncing.")
                    codes = [code]
                batches: list[dict] = []
                for code in codes:
                    stored = conn.execute("SELECT * FROM batches WHERE batch_code = ?", (code,)).fetchone()
                    if stored is None:
                        raise PortalError(f"Batch {code} was not found.")
                    batches.append(dict(stored))
            if not batches:
                raise PortalError("No batches to sync for this training centre.")

            first = batches[0]
            open_batch = {
                "state": first.get("state"),
                "pia": first.get("pia"),
                "sanction_order": first.get("sanction_order"),
                "training_center": first.get("training_center"),
                "batch_code": first.get("batch_code"),
                "batch_start_date": first.get("batch_start_date"),
                "expected_end_date": first.get("expected_end_date"),
            }
            warnings = self.portal.prepare_report(open_batch, sync_id)
            for warning in warnings:
                self.log(sync_id, warning)

            batch_failures = 0
            candidate_failures = 0
            date_failures = 0
            dates_discovered = 0
            for batch_index, stored in enumerate(batches, start=1):
                if self.cancel_event.is_set():
                    raise SyncCancelled("Sync cancelled by the administrator.")
                code = stored["batch_code"]
                self.update(
                    sync_id,
                    status="running",
                    progress_step="extracting_records",
                    message=f"Syncing {code} ({batch_index} of {len(batches)})",
                )
                self.log(sync_id, f"Starting batch {code} ({batch_index}/{len(batches)})")
                try:
                    batch_saved, batch_processed, batch_discovered, cand_fail, day_fail = self._sync_one_batch(
                        sync_id=sync_id,
                        mode=mode,
                        date_from=date_from,
                        date_to=date_to,
                        stored=stored,
                        rules_base=rules_base,
                        batch_label=f"{code} ({batch_index}/{len(batches)})",
                    )
                except SyncCancelled:
                    raise
                except Exception as exc:
                    batch_failures += 1
                    self.log(sync_id, f"{code}: {_friendly_sync_error(exc)}")
                    logger.exception("Batch %s failed", code)
                    continue
                saved += batch_saved
                processed += batch_processed
                dates_discovered += batch_discovered
                candidate_failures += cand_fail
                date_failures += day_fail
                self.update(
                    sync_id,
                    dates_discovered=dates_discovered,
                    dates_processed=processed,
                    records_saved=saved,
                )

            if self.cancel_event.is_set():
                raise SyncCancelled("Sync cancelled by the administrator.")
            with transaction() as conn:
                from .reporting import recalculate_all

                recalculate_all(conn, rules_base)

            total_batches = len(batches)
            status = "completed"
            if batch_failures == total_batches:
                status = "failed"
            elif batch_failures or (date_failures and processed == 0 and saved == 0):
                status = "failed" if saved == 0 and processed == 0 else "completed"
            if candidate_failures and status == "completed":
                self.log(
                    sync_id,
                    f"{candidate_failures} candidate(s) could not be read; other records were saved.",
                )
            if batch_failures and status == "completed":
                self.log(sync_id, f"{batch_failures} batch(es) failed; other batches were saved.")
            centre_note = f" · {total_batches} batch(es)" if total_batches > 1 else ""
            self.update(
                sync_id,
                status=status,
                finished_at=now_iso(),
                progress_step="completed" if status == "completed" else "failed",
                message=(
                    f"Sync Completed{centre_note}"
                    if status == "completed"
                    else "Sync finished with errors. See the log."
                ),
                dates_discovered=dates_discovered,
                dates_processed=processed,
                records_saved=saved,
            )
        except SyncCancelled as exc:
            self.update(
                sync_id,
                status="cancelled",
                finished_at=now_iso(),
                progress_step="cancelled",
                message=f"Sync stopped. Saved {saved} records before cancel.",
                records_saved=saved,
            )
        except Exception as exc:
            logger.exception("Sync %s failed", sync_id)
            message = _friendly_sync_error(exc)
            self.log(sync_id, message)
            self.update(
                sync_id,
                status="failed",
                finished_at=now_iso(),
                progress_step="failed",
                message=f"Sync failed: {message}",
                records_saved=saved,
            )

    def _sync_one_batch(
        self,
        *,
        sync_id: int,
        mode: str,
        date_from: str | None,
        date_to: str | None,
        stored: dict,
        rules_base: dict,
        batch_label: str,
    ) -> tuple[int, int, int, int, int]:
        """Sync one batch after the portal session is open. Returns saved, processed, discovered, candidate_failures, date_failures."""
        rules = dict(rules_base)
        batch = {
            "state": stored.get("state"),
            "pia": stored.get("pia"),
            "sanction_order": stored.get("sanction_order"),
            "training_center": stored.get("training_center"),
            "batch_code": stored.get("batch_code"),
            "batch_start_date": stored.get("batch_start_date"),
            "expected_end_date": stored.get("expected_end_date"),
        }
        for key in ("training_start", "training_end", "lunch_start", "lunch_end"):
            if stored.get(key):
                rules[key] = stored[key]

        with transaction() as conn:
            from .db import set_json

            set_json(conn, "batch", batch)
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
            raise PortalError(f"{batch_label}: import the master candidate workbook before syncing.")

        all_punches: list[dict] = []
        candidate_failures = 0
        total_candidates = len(candidates)
        self.update(
            sync_id,
            progress_step="extracting_records",
            progress_current=0,
            progress_total=total_candidates,
            status="running",
            message=f"{batch_label}: reading candidate 0 of {total_candidates}",
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
                message=f"{batch_label} · reading {name} ({index} of {total_candidates})",
            )
            if not cid:
                self.log(sync_id, f"{batch_label} · {name}: skipped — blank KP Id on the master list.")
                candidate_failures += 1
                continue
            try:
                rows, notes = self.portal.fetch_candidate_punches(cid)
                for note in notes:
                    self.log(sync_id, f"{batch_label}: {note}")
                for row in rows:
                    if not row.get("candidate_id"):
                        row["candidate_id"] = cid
                    row["attendance_id"] = None
                    if not row.get("name"):
                        row["name"] = candidate.get("name") or ""
                all_punches.extend(rows)
            except SyncCancelled:
                raise
            except Exception as exc:
                candidate_failures += 1
                self.log(sync_id, f"{batch_label} · {name} (KP Id {cid}): {exc}")
                logger.exception("Candidate KP Id %s failed", cid)

        by_date = group_punches_by_date(all_punches)
        discovered_dates = sorted(by_date.keys())
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
                    f"{batch_label}: no punches for "
                    + ", ".join(missing[:40])
                    + ("…" if len(missing) > 40 else ""),
                )

        saved = 0
        processed = 0
        failures = 0
        total = len(selected_dates)
        self.update(
            sync_id,
            progress_step="reconciling",
            progress_current=0,
            progress_total=total,
            status="running",
            message=f"{batch_label} · calculating attendance (0 of {total})",
        )
        if total == 0:
            self.log(
                sync_id,
                f"{batch_label}: no attendance dates needed saving."
                if not candidate_failures
                else f"{batch_label}: finished with {candidate_failures} candidate read error(s) and no dates to save.",
            )
            return saved, processed, len(discovered_dates), candidate_failures, failures

        # Fetch stays sequential on one Chrome page; parallelize only DB reconcile/save.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _save_one_day(day: str) -> tuple[str, int]:
            if self.cancel_event.is_set():
                raise SyncCancelled("Sync cancelled by the administrator.")
            attendance_day = parse_date(day)
            if attendance_day is None:
                raise PortalError(f"Unreadable attendance date {day}")
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
            return day, written

        workers = min(4, total)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_save_one_day, day): day for day in selected_dates}
            for future in as_completed(futures):
                if self.cancel_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    raise SyncCancelled("Sync cancelled by the administrator.")
                day = futures[future]
                try:
                    _, written = future.result()
                    saved += written
                    processed += 1
                except SyncCancelled:
                    raise
                except Exception as exc:
                    failures += 1
                    self.log(sync_id, f"{batch_label} · {day}: {exc}")
                    logger.exception("Date %s failed", day)
                self.update(
                    sync_id,
                    progress_step="saving",
                    progress_current=processed + failures,
                    progress_total=total,
                    records_saved=saved,
                    message=f"{batch_label} · saving dates ({processed + failures} of {total}) · saved {saved}",
                )
        return saved, processed, len(discovered_dates), candidate_failures, failures


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
