"""HTTP API for the local attendance dashboard."""

from __future__ import annotations

import secrets
import sqlite3
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .auth import actor, bind_user, hash_password, public_account
from .calculator import now_iso, validate_rules
from .config import (
    DEFAULT_BATCH,
    PORTAL_REPORT_URL,
    RESET_PHRASE,
    cloud_portal_sync_blocked,
    remote_display_enabled,
)
from .db import get_json, read_conn, set_json, transaction, upsert_batch
from .exporter import workbook_response
from .importer import import_workbook
from .reporting import (
    attendance_cell,
    candidate_export_sheets,
    candidate_profile,
    candidate_stats,
    daily_export_rows,
    dashboard,
    load_context,
    low_attendance_rows,
    matrix,
    matrix_export_rows,
    monthly_export_rows,
    save_settings,
)
from .sync_service import manager

router = APIRouter(prefix="/api", dependencies=[Depends(bind_user)])


def _user() -> dict:
    return actor() or {}


def _require_admin() -> None:
    if _user().get("role") == "centre":
        raise HTTPException(status_code=403, detail="Only the administrator can do that.")


def _own_centre(centre_id: int) -> None:
    user = _user()
    if user.get("role") == "centre" and user.get("centre_id") != centre_id:
        raise HTTPException(status_code=404, detail="Training centre was not found.")


class SettingsBody(BaseModel):
    batch: dict
    rules: dict


class SyncBody(BaseModel):
    mode: str = "incremental"
    date_from: str | None = None
    date_to: str | None = None


class ResetBody(BaseModel):
    confirmation: str = Field(min_length=1)


def _xlsx(buffer, filename: str) -> StreamingResponse:
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/health")
def health():
    return {
        "status": "ok",
        "portal": PORTAL_REPORT_URL,
        "writes_to_portal": False,
        "remote_browser": False,
        "cloud_sync_blocked": cloud_portal_sync_blocked(),
        "chrome_sync": True,
    }


@router.get("/bootstrap")
def bootstrap():
    with read_conn() as conn:
        context = load_context(conn)
        count = len(context["candidates"])
        issues = conn.execute("SELECT COUNT(*) AS count FROM reconciliation_issues WHERE resolved = 0").fetchone()["count"]
        current = conn.execute("SELECT * FROM sync_history ORDER BY id DESC LIMIT 1").fetchone()
        user = _user()
        if user.get("role") == "centre" and user.get("centre_id"):
            batches = conn.execute(
                """
                SELECT batch_code, batch_start_date, (
                    SELECT COUNT(*) FROM candidates c
                    WHERE c.batch_id = batches.id OR c.batch_registration_number = batches.batch_code
                ) AS candidate_count
                FROM batches
                WHERE centre_id = ?
                ORDER BY batch_start_date, batch_code
                """,
                (user["centre_id"],),
            ).fetchall()
        else:
            batches = conn.execute(
                """
                SELECT batch_code, batch_start_date, (
                    SELECT COUNT(*) FROM candidates c
                    WHERE c.batch_id = batches.id OR c.batch_registration_number = batches.batch_code
                ) AS candidate_count
                FROM batches
                ORDER BY batch_start_date, batch_code
                """
            ).fetchall()
    return {
        "user": public_account(user),
        "batch": context["batch"],
        "batches": [dict(row) for row in batches],
        "rules": context["rules"],
        "candidate_count": count,
        "open_issues": issues,
        "sync": dict(current) if current else None,
        "portal_url": PORTAL_REPORT_URL,
        "remote_browser": False,
        "remote_browser_path": None,
        "cloud_sync_blocked": cloud_portal_sync_blocked(),
        "chrome_sync": True,
    }


@router.get("/dashboard")
def dashboard_route(date: str | None = None):
    with read_conn() as conn:
        return dashboard(conn, date)


@router.get("/matrix")
def matrix_route(date_from: str | None = None, date_to: str | None = None):
    with read_conn() as conn:
        return matrix(conn, date_from, date_to)


@router.get("/candidates")
def candidates_route():
    with read_conn() as conn:
        return {"candidates": candidate_stats(load_context(conn))}


@router.post("/candidates/delete-all")
def delete_all_candidates():
    user = _user()
    with transaction() as conn:
        if user.get("role") == "centre" and user.get("centre_id"):
            rows = conn.execute(
                "SELECT id, batch_code FROM batches WHERE centre_id = ?",
                (user["centre_id"],),
            ).fetchall()
            ids = [row["id"] for row in rows]
            codes = [row["batch_code"] for row in rows if row["batch_code"]]
            if not ids:
                return {"ok": True, "deleted": 0, "portal_changed": False}
            id_marks = ",".join("?" * len(ids))
            code_marks = ",".join("?" * len(codes)) if codes else "NULL"
            where = f"batch_id IN ({id_marks})"
            params: list = list(ids)
            if codes:
                where += f" OR batch_registration_number IN ({code_marks})"
                params.extend(codes)
            deleted = conn.execute(f"SELECT COUNT(*) AS n FROM candidates WHERE {where}", params).fetchone()["n"]
            record_ids = [
                row["id"]
                for row in conn.execute(
                    f"SELECT id FROM attendance_records WHERE batch_id IN ({id_marks})",
                    ids,
                )
            ]
            if record_ids:
                marks = ",".join("?" * len(record_ids))
                conn.execute(f"DELETE FROM attendance_sessions WHERE record_id IN ({marks})", record_ids)
            conn.execute(f"DELETE FROM attendance_records WHERE batch_id IN ({id_marks})", ids)
            conn.execute(f"DELETE FROM candidates WHERE {where}", params)
        else:
            deleted = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
            conn.execute("DELETE FROM attendance_sessions")
            conn.execute("DELETE FROM attendance_records")
            conn.execute("DELETE FROM reconciliation_issues")
            conn.execute("DELETE FROM candidates")
    return {"ok": True, "deleted": deleted, "portal_changed": False}


@router.get("/candidates/{candidate_pk}")
def candidate_route(candidate_pk: int):
    with read_conn() as conn:
        profile = candidate_profile(conn, candidate_pk)
    if profile is None:
        raise HTTPException(status_code=404, detail="Candidate was not found.")
    return profile


@router.get("/attendance/cell")
def cell_route(candidate_pk: int, date: str):
    with read_conn() as conn:
        payload = attendance_cell(conn, candidate_pk, date)
    if payload is None:
        raise HTTPException(status_code=404, detail="Attendance cell was not found.")
    return payload


@router.get("/settings")
def settings_route():
    with read_conn() as conn:
        context = load_context(conn)
    return {"batch": context["batch"], "rules": context["rules"]}


@router.put("/settings")
def update_settings(body: SettingsBody):
    try:
        with transaction() as conn:
            return save_settings(conn, body.batch, body.rules)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/batches")
def list_batches():
    with read_conn() as conn:
        user = _user()
        if user.get("role") == "centre":
            from .reporting import load_context

            active = (load_context(conn)["batch"] or {}).get("batch_code")
            rows = conn.execute(
                """
                SELECT b.*, (
                    SELECT COUNT(*) FROM candidates c
                    WHERE c.batch_id = b.id OR c.batch_registration_number = b.batch_code
                ) AS candidate_count
                FROM batches b
                WHERE b.centre_id = ?
                ORDER BY b.batch_code
                """,
                (user.get("centre_id"),),
            ).fetchall()
        else:
            active = (get_json(conn, "batch") or {}).get("batch_code")
            rows = conn.execute(
                """
                SELECT b.*, (
                    SELECT COUNT(*) FROM candidates c
                    WHERE c.batch_id = b.id OR c.batch_registration_number = b.batch_code
                ) AS candidate_count
                FROM batches b
                ORDER BY b.batch_code
                """
            ).fetchall()
    return {
        "active_batch_code": active,
        "batches": [{**dict(row), "active": row["batch_code"] == active} for row in rows],
    }


class CentreBody(BaseModel):
    name: str
    code: str
    state: str = ""
    district: str = ""
    address: str = ""
    pia: str = ""
    sanction_order: str = ""
    status: str = "active"


class BatchBody(BaseModel):
    batch_code: str
    job_role: str = ""
    sanction_order: str = ""
    batch_start_date: str = ""
    ojt_start_date: str = ""
    training_start: str = "09:00"
    training_end: str = "18:00"
    lunch_start: str = "13:00"
    lunch_end: str = "14:00"
    status: str = "active"


def _centre_cards(conn):
    rows = conn.execute("SELECT * FROM training_centres ORDER BY name").fetchall()
    cards = []
    for row in rows:
        centre = dict(row)
        stats = conn.execute(
            """
            SELECT COUNT(DISTINCT b.id) AS batches,
                   COUNT(DISTINCT c.id) AS candidates
            FROM batches b
            LEFT JOIN candidates c ON c.batch_id = b.id OR c.batch_registration_number = b.batch_code
            WHERE b.centre_id = ?
            """,
            (row["id"],),
        ).fetchone()
        centre["batch_count"] = stats["batches"] or 0
        centre["candidate_count"] = stats["candidates"] or 0
        cards.append(centre)
    return cards


@router.get("/centres")
def list_centres():
    user = _user()
    with read_conn() as conn:
        cards = _centre_cards(conn)
    if user.get("role") == "centre":
        cards = [card for card in cards if card["id"] == user.get("centre_id")]
    return {"centres": cards, "role": user.get("role") or "admin"}


@router.post("/centres")
def create_centre(body: CentreBody):
    _require_admin()
    name = body.name.strip()
    code = body.code.strip()
    if not name or not code:
        raise HTTPException(status_code=400, detail="Training centre name and code are required.")
    now = now_iso()
    with transaction() as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO training_centres (
                    name, code, state, district, address, pia, sanction_order, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, code, body.state.strip(), body.district.strip(), body.address.strip(), body.pia.strip(), body.sanction_order.strip(), body.status.strip() or "active", now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=400, detail="That training centre code is already used.") from exc
        row = conn.execute("SELECT * FROM training_centres WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {"centre": dict(row)}


@router.put("/centres/{centre_id}")
def update_centre(centre_id: int, body: CentreBody):
    _require_admin()
    now = now_iso()
    with transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE training_centres
            SET name = ?, code = ?, state = ?, district = ?, address = ?, pia = ?, sanction_order = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (body.name.strip(), body.code.strip(), body.state.strip(), body.district.strip(), body.address.strip(), body.pia.strip(), body.sanction_order.strip(), body.status.strip() or "active", now, centre_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Training centre was not found.")
        row = conn.execute("SELECT * FROM training_centres WHERE id = ?", (centre_id,)).fetchone()
    return {"centre": dict(row)}


@router.get("/centres/{centre_id}")
def centre_detail(centre_id: int):
    _own_centre(centre_id)
    with read_conn() as conn:
        centre = conn.execute("SELECT * FROM training_centres WHERE id = ?", (centre_id,)).fetchone()
        if centre is None:
            raise HTTPException(status_code=404, detail="Training centre was not found.")
        login = conn.execute(
            "SELECT username FROM administrators WHERE centre_id = ? AND role = 'centre'",
            (centre_id,),
        ).fetchone()
        batches = conn.execute(
            """
            SELECT b.*, (
                SELECT COUNT(*) FROM candidates c
                WHERE c.batch_id = b.id OR c.batch_registration_number = b.batch_code
            ) AS candidate_count
            FROM batches b
            WHERE b.centre_id = ?
            ORDER BY b.batch_start_date, b.batch_code
            """,
            (centre_id,),
        ).fetchall()
    return {
        "centre": dict(centre),
        "batches": [dict(row) for row in batches],
        "login_username": login["username"] if login else None,
        "role": _user().get("role") or "admin",
    }


@router.post("/centres/{centre_id}/login")
def create_centre_login(centre_id: int):
    _require_admin()
    import re

    with transaction() as conn:
        centre = conn.execute("SELECT * FROM training_centres WHERE id = ?", (centre_id,)).fetchone()
        if centre is None:
            raise HTTPException(status_code=404, detail="Training centre was not found.")
        code_slug = re.sub(r"[^a-z0-9]+", "", (centre["code"] or "").lower())
        name_slug = re.sub(r"[^a-z0-9]+", "", (centre["name"] or "").lower())
        base = code_slug if len(code_slug) >= 4 else name_slug
        base = base or f"centre{centre_id}"
        username = base
        owner = conn.execute(
            "SELECT id FROM administrators WHERE centre_id = ? AND role = 'centre'",
            (centre_id,),
        ).fetchone()
        clash = conn.execute(
            "SELECT id, centre_id FROM administrators WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if clash and (owner is None or clash["id"] != owner["id"]):
            username = f"{base}{centre_id}"
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        password = "Centre-" + "".join(secrets.choice(alphabet) for _ in range(6))
        hashed = hash_password(password)
        if owner:
            conn.execute(
                "UPDATE administrators SET username = ?, password_hash = ? WHERE id = ?",
                (username, hashed, owner["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO administrators (username, password_hash, role, centre_id, created_at)
                VALUES (?, ?, 'centre', ?, ?)
                """,
                (username, hashed, centre_id, now_iso()),
            )
    return {"username": username, "password": password, "centre_name": centre["name"]}


@router.post("/centres/{centre_id}/batches")
def create_centre_batch(centre_id: int, body: BatchBody):
    from .calculator import parse_date

    _own_centre(centre_id)
    code = body.batch_code.strip()
    start = parse_date(body.batch_start_date)
    if not code or start is None:
        raise HTTPException(status_code=400, detail="Batch number and a valid start date are required.")
    with transaction() as conn:
        centre = conn.execute("SELECT * FROM training_centres WHERE id = ?", (centre_id,)).fetchone()
        if centre is None:
            raise HTTPException(status_code=404, detail="Training centre was not found.")
        batch = {
            "state": centre["state"],
            "pia": centre["pia"],
            "sanction_order": body.sanction_order.strip() or centre["sanction_order"],
            "training_center": centre["name"],
            "batch_code": code,
            "batch_start_date": start.isoformat(),
            "expected_end_date": "",
            "centre_id": centre_id,
            "job_role": body.job_role.strip(),
            "ojt_start_date": parse_date(body.ojt_start_date).isoformat() if parse_date(body.ojt_start_date) else "",
            "training_start": body.training_start,
            "training_end": body.training_end,
            "lunch_start": body.lunch_start,
            "lunch_end": body.lunch_end,
            "status": body.status or "active",
        }
        try:
            batch_id = upsert_batch(conn, batch)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=400, detail="That batch number already exists.") from exc
        user = _user()
        if user.get("role") == "centre":
            conn.execute(
                "UPDATE administrators SET active_batch_code = ? WHERE id = ?",
                (code, user["id"]),
            )
            user["active_batch_code"] = code
        else:
            set_json(conn, "batch", batch)
    return {"batch_id": batch_id, "batch": batch}


class BatchActivate(BaseModel):
    batch_code: str


@router.post("/batches/activate")
def activate_batch(body: BatchActivate):
    code = body.batch_code.strip()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM batches WHERE batch_code = ?", (code,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="That batch is not on this centre.")
        user = _user()
        if user.get("role") == "centre" and row["centre_id"] != user.get("centre_id"):
            raise HTTPException(status_code=404, detail="That batch is not on this centre.")
        if user.get("role") == "centre":
            conn.execute(
                "UPDATE administrators SET active_batch_code = ? WHERE id = ?",
                (row["batch_code"], user["id"]),
            )
            user["active_batch_code"] = row["batch_code"]
            return {
                "batch": {
                    "state": row["state"],
                    "pia": row["pia"],
                    "sanction_order": row["sanction_order"],
                    "training_center": row["training_center"],
                    "batch_code": row["batch_code"],
                    "batch_start_date": row["batch_start_date"],
                    "expected_end_date": row["expected_end_date"],
                }
            }
        current = get_json(conn, "batch") or {}
        current.update(
            {
                "state": row["state"] or current.get("state"),
                "pia": row["pia"] or current.get("pia"),
                "sanction_order": row["sanction_order"] or current.get("sanction_order"),
                "training_center": row["training_center"] or current.get("training_center"),
                "batch_code": row["batch_code"],
                "batch_start_date": row["batch_start_date"],
                "expected_end_date": row["expected_end_date"],
            }
        )
        set_json(conn, "batch", current)
    return {"batch": current}


@router.post("/batches")
async def create_batch(
    batch_code: str = Form(...),
    batch_start_date: str = Form(...),
    file: UploadFile = File(...),
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".xlsx", ".xlsm"}:
        raise HTTPException(status_code=400, detail="Upload an Excel .xlsx workbook.")
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="The uploaded workbook is empty.")
    with transaction() as conn:
        current = get_json(conn, "batch") or DEFAULT_BATCH
    batch = {
        "state": current.get("state") or DEFAULT_BATCH["state"],
        "pia": current.get("pia") or DEFAULT_BATCH["pia"],
        "sanction_order": current.get("sanction_order") or DEFAULT_BATCH["sanction_order"],
        "training_center": current.get("training_center") or DEFAULT_BATCH["training_center"],
        "batch_code": batch_code.strip(),
        "batch_start_date": batch_start_date.strip(),
        "expected_end_date": "",
    }
    from .calculator import parse_date

    if not batch["batch_code"] or parse_date(batch["batch_start_date"]) is None:
        raise HTTPException(status_code=400, detail="Batch code and a valid start date are required.")
    batch["batch_start_date"] = parse_date(batch["batch_start_date"]).isoformat()
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as handle:
        handle.write(contents)
        temporary = Path(handle.name)
    try:
        try:
            result = import_workbook(temporary, batch)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        temporary.unlink(missing_ok=True)
    result["batch_code"] = batch["batch_code"]
    return result


@router.post("/candidates/import")
async def import_candidates(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".xlsx", ".xlsm"}:
        raise HTTPException(status_code=400, detail="Upload an Excel .xlsx workbook.")
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="The uploaded workbook is empty.")
    batch_override = None
    if _user().get("role") == "centre":
        with read_conn() as conn:
            from .reporting import load_context

            batch_override = load_context(conn)["batch"]
        if not batch_override.get("batch_code"):
            raise HTTPException(status_code=400, detail="Create a batch before uploading candidates.")
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as handle:
        handle.write(contents)
        temporary = Path(handle.name)
    try:
        try:
            return import_workbook(temporary, batch_override)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        temporary.unlink(missing_ok=True)


@router.get("/sync/current")
def sync_current():
    with read_conn() as conn:
        row = conn.execute("SELECT * FROM sync_history ORDER BY id DESC LIMIT 1").fetchone()
    return {"sync": dict(row) if row else None}


@router.get("/sync/history")
def sync_history():
    with read_conn() as conn:
        rows = conn.execute("SELECT * FROM sync_history ORDER BY id DESC LIMIT 50").fetchall()
    return {"history": [dict(row) for row in rows]}


@router.post("/sync/start")
def sync_start(body: SyncBody):
    if cloud_portal_sync_blocked():
        raise HTTPException(
            status_code=400,
            detail=(
                "Portal sync uses your normal Chrome on this Mac. "
                "Open http://127.0.0.1:8000 locally, run scripts/open-kaushal-chrome.sh, "
                "sign into Kaushal, then start sync. This online site is for viewing reports."
            ),
        )
    if body.mode not in {"incremental", "range", "full"}:
        raise HTTPException(status_code=400, detail="Sync mode must be incremental, range, or full.")
    date_from = body.date_from
    date_to = body.date_to
    if body.mode == "range" and date_from and not date_to:
        date_to = date_from
    if body.mode == "range" and (not date_from or not date_to):
        raise HTTPException(status_code=400, detail="Choose one date, or a start and end date.")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=400, detail="The range start is after the range end.")
    with read_conn() as conn:
        from .reporting import load_context

        batch_code = (load_context(conn)["batch"] or {}).get("batch_code") or None
    if _user().get("role") == "centre" and not batch_code:
        raise HTTPException(status_code=400, detail="Create a batch before syncing attendance.")
    try:
        sync_id = manager.start(body.mode, date_from, date_to, batch_code)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"sync_id": sync_id}


@router.post("/sync/cancel")
def sync_cancel():
    manager.cancel()
    return {"ok": True}


@router.post("/sync/login-done")
def sync_login_done():
    manager.notify_login()
    return {"ok": True}


@router.post("/sync/reopen-browser")
def sync_reopen_browser():
    """Reset the live Chromium to the Kaushal login page (clears stuck 401 tabs)."""
    from .config import remote_display_enabled
    from .live_browser import ensure_login_browser, stop

    if not remote_display_enabled():
        raise HTTPException(status_code=400, detail="Live browser is only available on the online server.")
    stop()
    ensure_login_browser(fresh=True)
    return {"ok": True, "remote_browser_path": "/novnc/vnc.html?autoconnect=1&resize=scale&reconnect=1&path=novnc/"}


@router.get("/reconciliation")
def reconciliation(resolved: int = 0):
    with read_conn() as conn:
        rows = conn.execute(
            """
            SELECT i.*, c.name AS candidate_name, c.candidate_id
            FROM reconciliation_issues i
            LEFT JOIN candidates c ON c.id = i.candidate_pk
            WHERE i.resolved = ?
            ORDER BY i.id DESC
            LIMIT 300
            """,
            (1 if resolved else 0,),
        ).fetchall()
    return {"issues": [dict(row) for row in rows]}


@router.post("/reconciliation/{issue_id}/resolve")
def resolve_issue(issue_id: int):
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE reconciliation_issues SET resolved = 1 WHERE id = ? AND resolved = 0",
            (issue_id,),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Issue was not found.")
    return {"ok": True}


@router.post("/admin/reset")
def reset_local(body: ResetBody):
    _require_admin()
    if body.confirmation != RESET_PHRASE:
        raise HTTPException(status_code=400, detail=f"Type {RESET_PHRASE} to reset the local database.")
    with transaction() as conn:
        conn.execute("DELETE FROM attendance_sessions")
        conn.execute("DELETE FROM attendance_records")
        conn.execute("DELETE FROM attendance_dates")
        conn.execute("DELETE FROM reconciliation_issues")
        conn.execute("DELETE FROM sync_history")
        conn.execute("DELETE FROM candidates")
        centre = get_json(conn, "batch") or DEFAULT_BATCH
        centre_batch = {
            "state": centre.get("state") or DEFAULT_BATCH["state"],
            "pia": centre.get("pia") or DEFAULT_BATCH["pia"],
            "sanction_order": centre.get("sanction_order") or DEFAULT_BATCH["sanction_order"],
            "training_center": centre.get("training_center") or DEFAULT_BATCH["training_center"],
            "batch_code": centre.get("batch_code") or DEFAULT_BATCH["batch_code"],
            "batch_start_date": centre.get("batch_start_date") or DEFAULT_BATCH["batch_start_date"],
            "expected_end_date": centre.get("expected_end_date") or DEFAULT_BATCH["expected_end_date"],
        }
        set_json(conn, "batch", centre_batch)
        upsert_batch(conn, centre_batch)
    return {"ok": True, "portal_changed": False, "candidates_cleared": True}


@router.get("/export/daily")
def export_daily(date: str):
    try:
        with read_conn() as conn:
            rows = daily_export_rows(conn, date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _xlsx(workbook_response([("Daily Attendance", rows)]), f"daily-attendance-{date}.xlsx")


@router.get("/export/monthly")
def export_monthly(month: str):
    try:
        with read_conn() as conn:
            rows = monthly_export_rows(conn, month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _xlsx(workbook_response([("Monthly Attendance", rows)]), f"monthly-attendance-{month}.xlsx")


@router.get("/export/candidates")
def export_candidates():
    with read_conn() as conn:
        sheets = candidate_export_sheets(conn)
    return _xlsx(workbook_response(sheets), "candidate-wise-attendance.xlsx")


@router.get("/export/matrix")
def export_matrix():
    with read_conn() as conn:
        rows = matrix_export_rows(conn)
    return _xlsx(workbook_response([("Attendance Matrix", rows)], color_status=True), "attendance-matrix.xlsx")


@router.get("/export/low-attendance")
def export_low():
    with read_conn() as conn:
        rows = low_attendance_rows(conn)
        threshold = int(float((get_json(conn, "rules") or {}).get("low_attendance_threshold", 75)))
    return _xlsx(workbook_response([("Low Attendance", rows)]), f"low-attendance-below-{threshold}.xlsx")


def rules_preview(rules: dict) -> dict:
    return validate_rules(rules)
