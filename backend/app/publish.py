"""Push locally synced attendance to the online API so centre logins can view it."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

from .calculator import now_iso
from .db import transaction
from .matching import clean_id

logger = logging.getLogger("attendance.publish")

DEFAULT_ONLINE_API = "https://kk-attendance-production.up.railway.app"


def online_api_url() -> str:
    return (os.environ.get("ATTENDANCE_ONLINE_API_URL") or DEFAULT_ONLINE_API).rstrip("/")


def online_admin_credentials() -> tuple[str, str]:
    user = os.environ.get("ATTENDANCE_ONLINE_ADMIN_USER") or "admin"
    password = os.environ.get("ATTENDANCE_ONLINE_ADMIN_PASSWORD") or "Admin@123"
    return user, password


def export_batch_attendance(conn, batch_code: str) -> dict:
    """Export one batch's candidates, dates, records, and sessions for online publish."""
    batch = conn.execute("SELECT * FROM batches WHERE batch_code = ?", (batch_code,)).fetchone()
    if batch is None:
        raise ValueError(f"Batch {batch_code} was not found.")
    batch_id = int(batch["id"])
    candidates = [
        dict(row)
        for row in conn.execute(
            """
            SELECT candidate_id, attendance_id, name, sr_no, candidate_status, add_remove_date,
                   batch_registration_number, batch_id
            FROM candidates
            WHERE batch_id = ? OR batch_registration_number = ?
            ORDER BY sr_no, id
            """,
            (batch_id, batch_code),
        )
    ]
    dates = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM attendance_dates WHERE batch_id = ? ORDER BY attendance_date",
            (batch_id,),
        )
    ]
    records = [
        dict(row)
        for row in conn.execute(
            """
            SELECT r.*, c.candidate_id AS master_candidate_id
            FROM attendance_records r
            JOIN candidates c ON c.id = r.candidate_pk
            WHERE r.batch_id = ?
            ORDER BY r.attendance_date, c.sr_no, r.id
            """,
            (batch_id,),
        )
    ]
    record_ids = [int(row["id"]) for row in records]
    sessions: list[dict] = []
    if record_ids:
        marks = ",".join("?" * len(record_ids))
        sessions = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM attendance_sessions WHERE record_id IN ({marks}) ORDER BY record_id, session_index",
                record_ids,
            )
        ]
    # Attach sessions to each record by local record id for the wire format.
    by_record: dict[int, list[dict]] = {}
    for session in sessions:
        by_record.setdefault(int(session["record_id"]), []).append(
            {
                "session_index": session["session_index"],
                "in_time": session["in_time"],
                "out_time": session["out_time"],
                "device_type": session["device_type"],
                "in_device_id": session["in_device_id"],
                "out_device_id": session["out_device_id"],
                "portal_attendance_id": session["portal_attendance_id"],
                "invalid": int(session["invalid"] or 0),
            }
        )
    packed_records = []
    for row in records:
        packed_records.append(
            {
                "candidate_id": clean_id(row.get("master_candidate_id")),
                "attendance_date": row["attendance_date"],
                "official_status": row["official_status"],
                "official_categories": row["official_categories"],
                "calculated_status": row["calculated_status"],
                "in_time": row["in_time"],
                "out_time": row["out_time"],
                "elapsed_minutes": row["elapsed_minutes"],
                "lunch_deduction_minutes": row["lunch_deduction_minutes"],
                "qualifying_minutes": row["qualifying_minutes"],
                "device_type": row["device_type"],
                "in_device_id": row["in_device_id"],
                "out_device_id": row["out_device_id"],
                "portal_candidate_name": row["portal_candidate_name"],
                "data_source": row["data_source"] or "published",
                "last_synced_at": row["last_synced_at"],
                "raw_json": row["raw_json"],
                "sessions": by_record.get(int(row["id"]), []),
            }
        )
    packed_dates = [
        {
            "attendance_date": row["attendance_date"],
            "enrolled_count": row["enrolled_count"],
            "present_count": row["present_count"],
            "half_day_count": row["half_day_count"],
            "absent_count": row["absent_count"],
            "rf_attendance_count": row["rf_attendance_count"],
            "ojt_attendance_count": row["ojt_attendance_count"],
            "logbook_status": row["logbook_status"],
            "portal_date_text": row["portal_date_text"],
            "detail_synced": int(row["detail_synced"] or 0),
            "portal_synced_at": row["portal_synced_at"],
        }
        for row in dates
    ]
    return {
        "exported_at": now_iso(),
        "batch": {
            "batch_code": batch["batch_code"],
            "state": batch["state"],
            "pia": batch["pia"],
            "sanction_order": batch["sanction_order"],
            "training_center": batch["training_center"],
            "batch_start_date": batch["batch_start_date"],
            "expected_end_date": batch["expected_end_date"],
            "centre_id": batch["centre_id"],
            "job_role": batch["job_role"],
            "training_start": batch["training_start"],
            "training_end": batch["training_end"],
            "lunch_start": batch["lunch_start"],
            "lunch_end": batch["lunch_end"],
            "status": batch["status"],
        },
        "candidates": candidates,
        "dates": packed_dates,
        "records": packed_records,
    }


def import_batch_attendance(conn, payload: dict) -> dict:
    """Upsert a published batch attendance payload into this database."""
    from .db import upsert_batch
    from .reporting import recalculate_all
    from .db import get_json

    batch_info = dict(payload.get("batch") or {})
    code = (batch_info.get("batch_code") or "").strip()
    if not code:
        raise ValueError("Published payload has no batch_code.")
    existing = conn.execute("SELECT * FROM batches WHERE batch_code = ?", (code,)).fetchone()
    if existing is None:
        raise ValueError(
            f"Online batch {code} does not exist yet. Create the batch and upload candidates online first, then publish again."
        )
    # Keep online centre_id / names; refresh schedule fields from local publish when present.
    merged = dict(existing)
    for key in (
        "state",
        "pia",
        "sanction_order",
        "training_center",
        "batch_start_date",
        "expected_end_date",
        "job_role",
        "training_start",
        "training_end",
        "lunch_start",
        "lunch_end",
        "status",
    ):
        if batch_info.get(key):
            merged[key] = batch_info[key]
    batch_id = upsert_batch(conn, merged)

    online_candidates = {
        clean_id(row["candidate_id"]): dict(row)
        for row in conn.execute(
            """
            SELECT * FROM candidates
            WHERE batch_id = ? OR batch_registration_number = ?
            """,
            (batch_id, code),
        )
        if clean_id(row["candidate_id"])
    }
    if not online_candidates:
        raise ValueError(f"Online batch {code} has no candidates. Import the master workbook online first.")

    dates = payload.get("dates") or []
    for row in dates:
        conn.execute(
            """
            INSERT INTO attendance_dates (
                batch_id, attendance_date, enrolled_count, present_count, half_day_count,
                absent_count, rf_attendance_count, ojt_attendance_count, logbook_status,
                portal_date_text, detail_synced, portal_synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(batch_id, attendance_date) DO UPDATE SET
                enrolled_count = excluded.enrolled_count,
                present_count = excluded.present_count,
                half_day_count = excluded.half_day_count,
                absent_count = excluded.absent_count,
                rf_attendance_count = excluded.rf_attendance_count,
                ojt_attendance_count = excluded.ojt_attendance_count,
                logbook_status = excluded.logbook_status,
                portal_date_text = excluded.portal_date_text,
                detail_synced = excluded.detail_synced,
                portal_synced_at = excluded.portal_synced_at
            """,
            (
                batch_id,
                row["attendance_date"],
                row.get("enrolled_count"),
                row.get("present_count"),
                row.get("half_day_count"),
                row.get("absent_count"),
                row.get("rf_attendance_count"),
                row.get("ojt_attendance_count"),
                row.get("logbook_status"),
                row.get("portal_date_text"),
                int(row.get("detail_synced") or 0),
                row.get("portal_synced_at") or now_iso(),
            ),
        )

    written = 0
    skipped = 0
    for row in payload.get("records") or []:
        cid = clean_id(row.get("candidate_id"))
        candidate = online_candidates.get(cid)
        if candidate is None:
            skipped += 1
            continue
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
                int(candidate["id"]),
                row["attendance_date"],
                batch_id,
                row.get("official_status"),
                row.get("official_categories"),
                row.get("calculated_status") or "absent",
                row.get("in_time"),
                row.get("out_time"),
                row.get("elapsed_minutes"),
                row.get("lunch_deduction_minutes"),
                row.get("qualifying_minutes"),
                row.get("device_type"),
                row.get("in_device_id"),
                row.get("out_device_id"),
                row.get("portal_candidate_name"),
                row.get("data_source") or "published",
                row.get("last_synced_at") or now_iso(),
                row.get("raw_json"),
            ),
        )
        stored = conn.execute(
            "SELECT id FROM attendance_records WHERE candidate_pk = ? AND attendance_date = ?",
            (int(candidate["id"]), row["attendance_date"]),
        ).fetchone()
        conn.execute("DELETE FROM attendance_sessions WHERE record_id = ?", (stored["id"],))
        for index, session in enumerate(row.get("sessions") or []):
            conn.execute(
                """
                INSERT INTO attendance_sessions (
                    record_id, session_index, in_time, out_time, device_type,
                    in_device_id, out_device_id, portal_attendance_id, invalid
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored["id"],
                    session.get("session_index", index),
                    session.get("in_time"),
                    session.get("out_time"),
                    session.get("device_type"),
                    session.get("in_device_id"),
                    session.get("out_device_id"),
                    session.get("portal_attendance_id"),
                    int(session.get("invalid") or 0),
                ),
            )
        written += 1

    rules = get_json(conn, "rules") or {}
    recalculate_all(conn, rules)
    return {
        "ok": True,
        "batch_code": code,
        "dates": len(dates),
        "records_written": written,
        "records_skipped": skipped,
        "message": f"Published {written} attendance record(s) for {code} to online centres.",
    }


def _http_json(method: str, url: str, payload: Optional[dict] = None, cookie: str | None = None) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
            set_cookie = response.headers.get("Set-Cookie") or ""
            parsed = json.loads(body) if body else {}
            if set_cookie:
                parsed["_set_cookie"] = set_cookie.split(";", 1)[0]
            return parsed
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            message = parsed.get("detail") or detail
        except json.JSONDecodeError:
            message = detail or str(exc)
        raise ValueError(f"Online API error ({exc.code}): {message}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach online API: {exc.reason}") from exc


def publish_batch_to_online(conn, batch_code: str) -> dict:
    """Export from this DB and POST into the configured online Railway API."""
    payload = export_batch_attendance(conn, batch_code)
    base = online_api_url()
    user, password = online_admin_credentials()
    login = _http_json(
        "POST",
        f"{base}/api/auth/login",
        {"username": user, "password": password},
    )
    cookie = login.get("_set_cookie")
    if not cookie:
        raise ValueError("Online admin login did not return a session cookie.")
    result = _http_json(
        "POST",
        f"{base}/api/admin/import-attendance",
        payload,
        cookie=cookie,
    )
    return {
        "ok": True,
        "online_api": base,
        "batch_code": batch_code,
        "dates": len(payload.get("dates") or []),
        "records": len(payload.get("records") or []),
        "online": result,
        "message": result.get("message")
        or f"Published {batch_code} to online centres ({base}).",
    }
