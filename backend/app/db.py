"""SQLite storage for candidates, portal attendance, and sync history."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from .calculator import now_iso, validate_rules
from .config import DEFAULT_BATCH, DEFAULT_RULES, DATA_DIR, db_path

_write_lock = threading.Lock()

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS application_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    state TEXT,
    pia TEXT,
    sanction_order TEXT,
    training_center TEXT,
    batch_code TEXT UNIQUE,
    batch_start_date TEXT,
    expected_end_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE,
    attendance_id TEXT,
    name TEXT NOT NULL,
    sr_no INTEGER,
    batch_registration_number TEXT,
    candidate_status TEXT,
    add_remove_date TEXT,
    ojt_start_date TEXT,
    batch_id INTEGER REFERENCES batches(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendance_dates (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER REFERENCES batches(id),
    attendance_date TEXT NOT NULL,
    enrolled_count INTEGER,
    present_count INTEGER,
    half_day_count INTEGER,
    absent_count INTEGER,
    rf_attendance_count INTEGER,
    ojt_attendance_count INTEGER,
    logbook_status TEXT,
    portal_date_text TEXT,
    detail_synced INTEGER NOT NULL DEFAULT 0,
    portal_synced_at TEXT,
    UNIQUE(batch_id, attendance_date)
);

CREATE TABLE IF NOT EXISTS attendance_records (
    id INTEGER PRIMARY KEY,
    candidate_pk INTEGER NOT NULL REFERENCES candidates(id),
    attendance_date TEXT NOT NULL,
    batch_id INTEGER,
    official_status TEXT,
    official_categories TEXT,
    calculated_status TEXT NOT NULL,
    in_time TEXT,
    out_time TEXT,
    elapsed_minutes INTEGER,
    lunch_deduction_minutes INTEGER,
    qualifying_minutes INTEGER,
    device_type TEXT,
    in_device_id TEXT,
    out_device_id TEXT,
    portal_candidate_name TEXT,
    data_source TEXT,
    last_synced_at TEXT,
    raw_json TEXT,
    UNIQUE(candidate_pk, attendance_date)
);

CREATE TABLE IF NOT EXISTS attendance_sessions (
    id INTEGER PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES attendance_records(id) ON DELETE CASCADE,
    session_index INTEGER NOT NULL,
    in_time TEXT,
    out_time TEXT,
    device_type TEXT,
    in_device_id TEXT,
    out_device_id TEXT,
    portal_attendance_id TEXT,
    invalid INTEGER NOT NULL DEFAULT 0,
    UNIQUE(record_id, session_index)
);

CREATE TABLE IF NOT EXISTS sync_history (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    mode TEXT,
    date_from TEXT,
    date_to TEXT,
    progress_step TEXT,
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    dates_discovered INTEGER NOT NULL DEFAULT 0,
    dates_processed INTEGER NOT NULL DEFAULT 0,
    records_saved INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    error_log TEXT
);

CREATE TABLE IF NOT EXISTS reconciliation_issues (
    id INTEGER PRIMARY KEY,
    sync_id INTEGER,
    attendance_date TEXT,
    candidate_pk INTEGER,
    issue_type TEXT NOT NULL,
    severity TEXT,
    message TEXT NOT NULL,
    portal_value TEXT,
    local_value TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_attendance_id ON candidates(attendance_id);
CREATE INDEX IF NOT EXISTS idx_records_date ON attendance_records(attendance_date);
CREATE INDEX IF NOT EXISTS idx_issues_open ON reconciliation_issues(resolved, attendance_date);
"""


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path or db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    with transaction() as conn:
        conn.executescript(SCHEMA)
        migrate_centres(conn)
        from .auth import clear_all_attendance, ensure_admin, ensure_centre_accounts

        ensure_admin(conn)
        ensure_defaults(conn)
        # One-time: clear punches and set centre passwords to {centrename}@123.
        flag = get_json(conn, "centre_password_reset_v2")
        if not flag:
            clear_all_attendance(conn)
            ensure_centre_accounts(conn, reset_passwords=True)
            set_json(conn, "centre_password_reset_v2", {"done_at": now_iso()})
        else:
            # Create logins for any new centres without resetting existing passwords.
            ensure_centre_accounts(conn, reset_passwords=False)


def migrate_centres(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS training_centres (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE,
            state TEXT,
            district TEXT,
            address TEXT,
            pia TEXT,
            sanction_order TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(batches)")}
    additions = {
        "centre_id": "INTEGER",
        "job_role": "TEXT",
        "ojt_start_date": "TEXT",
        "training_start": "TEXT",
        "training_end": "TEXT",
        "lunch_start": "TEXT",
        "lunch_end": "TEXT",
        "status": "TEXT DEFAULT 'active'",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE batches ADD COLUMN {name} {definition}")
    for row in conn.execute("SELECT * FROM batches").fetchall():
        if row["centre_id"]:
            continue
        name = (row["training_center"] or "Training centre").strip()
        code = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-") or "CENTRE"
        existing = conn.execute("SELECT id FROM training_centres WHERE code = ?", (code,)).fetchone()
        now = now_iso()
        if existing:
            centre_id = int(existing["id"])
        else:
            cursor = conn.execute(
                """
                INSERT INTO training_centres (
                    name, code, state, district, address, pia, sanction_order, status, created_at, updated_at
                ) VALUES (?, ?, ?, '', '', ?, ?, 'active', ?, ?)
                """,
                (name, code, row["state"], row["pia"], row["sanction_order"], now, now),
            )
            centre_id = int(cursor.lastrowid)
        conn.execute(
            """
            UPDATE batches
            SET centre_id = ?, status = COALESCE(status, 'active'),
                training_start = COALESCE(training_start, '09:00'),
                training_end = COALESCE(training_end, '18:00'),
                lunch_start = COALESCE(lunch_start, '13:00'),
                lunch_end = COALESCE(lunch_end, '14:00')
            WHERE id = ?
            """,
            (centre_id, row["id"]),
        )


def ensure_defaults(conn: sqlite3.Connection) -> None:
    if get_json(conn, "batch") is None:
        centre = dict(DEFAULT_BATCH)
        centre["batch_code"] = ""
        centre["batch_start_date"] = ""
        centre["expected_end_date"] = ""
        set_json(conn, "batch", centre)
    if get_json(conn, "rules") is None:
        set_json(conn, "rules", validate_rules(DEFAULT_RULES))


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    with _write_lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


@contextmanager
def read_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def get_json(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM application_settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return json.loads(row["value"])


def set_json(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        """
        INSERT INTO application_settings(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json.dumps(value), now_iso()),
    )


def upsert_batch(conn: sqlite3.Connection, batch: dict) -> int:
    now = now_iso()
    conn.execute(
        """
        INSERT INTO batches (
            state, pia, sanction_order, training_center, batch_code,
            batch_start_date, expected_end_date, centre_id, job_role, ojt_start_date,
            training_start, training_end, lunch_start, lunch_end, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(batch_code) DO UPDATE SET
            state = excluded.state,
            pia = excluded.pia,
            sanction_order = excluded.sanction_order,
            training_center = excluded.training_center,
            batch_start_date = excluded.batch_start_date,
            expected_end_date = excluded.expected_end_date,
            centre_id = COALESCE(excluded.centre_id, batches.centre_id),
            job_role = COALESCE(excluded.job_role, batches.job_role),
            ojt_start_date = COALESCE(excluded.ojt_start_date, batches.ojt_start_date),
            training_start = COALESCE(excluded.training_start, batches.training_start),
            training_end = COALESCE(excluded.training_end, batches.training_end),
            lunch_start = COALESCE(excluded.lunch_start, batches.lunch_start),
            lunch_end = COALESCE(excluded.lunch_end, batches.lunch_end),
            status = COALESCE(excluded.status, batches.status),
            updated_at = excluded.updated_at
        """,
        (
            batch.get("state"),
            batch.get("pia"),
            batch.get("sanction_order"),
            batch.get("training_center"),
            batch.get("batch_code"),
            batch.get("batch_start_date"),
            batch.get("expected_end_date"),
            batch.get("centre_id"),
            batch.get("job_role"),
            batch.get("ojt_start_date"),
            batch.get("training_start") or "09:00",
            batch.get("training_end") or "18:00",
            batch.get("lunch_start") or "13:00",
            batch.get("lunch_end") or "14:00",
            batch.get("status") or "active",
            now,
            now,
        ),
    )
    row = conn.execute("SELECT id FROM batches WHERE batch_code = ?", (batch.get("batch_code"),)).fetchone()
    return int(row["id"])


def mark_interrupted_syncs() -> None:
    with transaction() as conn:
        conn.execute(
            """
            UPDATE sync_history
            SET status = 'interrupted',
                finished_at = ?,
                message = 'The server stopped before this sync finished. Start it again to continue.'
            WHERE status IN ('running', 'awaiting_login', 'cancelling')
            """,
            (now_iso(),),
        )
