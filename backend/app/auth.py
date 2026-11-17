"""Local administrator sessions. Government portal passwords are never stored."""

from __future__ import annotations

import hashlib
import secrets
import threading
from datetime import timedelta

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .calculator import now_iso, now_ist

router = APIRouter(prefix="/api/auth")

COOKIE = "kk_admin"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "Admin@123"
SESSION_HOURS = 12
_local = threading.local()


class LoginBody(BaseModel):
    username: str
    password: str


def cookie_settings(request: Request) -> dict:
    """Use a cross-site cookie when the Vercel site talks to this API."""
    origin = request.headers.get("origin") or ""
    host = (request.headers.get("host") or "").split(":")[0]
    origin_host = urlparse(origin).hostname or ""
    cross_site = bool(origin_host and host and origin_host != host)
    return {
        "httponly": True,
        "samesite": "none" if cross_site else "lax",
        "secure": cross_site or request.url.scheme == "https",
        "path": "/",
        "max_age": SESSION_HOURS * 3600,
    }


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${digest}"


def password_matches(password: str, stored: str) -> bool:
    salt, digest = stored.split("$", 1)
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return secrets.compare_digest(check, digest)


def actor() -> dict | None:
    return getattr(_local, "user", None)


def bind_user(request: Request):
    _local.user = session_user(request.cookies.get(COOKIE))
    return _local.user


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    from .db import read_conn

    with read_conn() as conn:
        row = conn.execute(
            """
            SELECT a.id, a.username, a.role, a.centre_id, a.active_batch_code, c.name AS centre_name
            FROM admin_sessions s
            JOIN administrators a ON a.id = s.admin_id
            LEFT JOIN training_centres c ON c.id = a.centre_id
            WHERE s.token = ? AND s.expires_at > ?
            """,
            (token, now_iso()),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"] or "admin",
        "centre_id": row["centre_id"],
        "centre_name": row["centre_name"],
        "active_batch_code": row["active_batch_code"] or "",
    }


def public_account(user: dict | None) -> dict | None:
    if not user:
        return None
    return {
        "username": user["username"],
        "role": user.get("role") or "admin",
        "centre_id": user.get("centre_id"),
        "centre_name": user.get("centre_name"),
    }


def session_admin(token: str | None) -> str | None:
    user = session_user(token)
    return user["username"] if user else None


def ensure_admin(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS administrators (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token TEXT PRIMARY KEY,
            admin_id INTEGER NOT NULL REFERENCES administrators(id),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(administrators)")}
    if "role" not in columns:
        conn.execute("ALTER TABLE administrators ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
    if "centre_id" not in columns:
        conn.execute("ALTER TABLE administrators ADD COLUMN centre_id INTEGER")
    if "active_batch_code" not in columns:
        conn.execute("ALTER TABLE administrators ADD COLUMN active_batch_code TEXT")
    count = conn.execute("SELECT COUNT(*) AS n FROM administrators").fetchone()["n"]
    if count == 0:
        conn.execute(
            "INSERT INTO administrators (username, password_hash, created_at) VALUES (?, ?, ?)",
            (DEFAULT_ADMIN_USERNAME, hash_password(DEFAULT_ADMIN_PASSWORD), now_iso()),
        )


@router.post("/login")
def login(body: LoginBody, request: Request):
    from .db import read_conn, transaction

    username = body.username.strip()
    with read_conn() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash FROM administrators WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
    if row is None or not password_matches(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="The administrator username or password is incorrect.")
    token = secrets.token_urlsafe(32)
    created = now_ist()
    expires = created + timedelta(hours=SESSION_HOURS)
    with transaction() as conn:
        conn.execute(
            "INSERT INTO admin_sessions (token, admin_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, row["id"], created.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
        )
    account = {"username": row["username"], "role": "admin", "centre_id": None, "centre_name": None}
    with read_conn() as conn:
        stored = session_user_from_row(conn, row["id"])
        if stored:
            account = public_account(stored)
    response = JSONResponse(account)
    response.set_cookie(COOKIE, token, **cookie_settings(request))
    return response


def session_user_from_row(conn, admin_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT a.id, a.username, a.role, a.centre_id, a.active_batch_code, c.name AS centre_name
        FROM administrators a
        LEFT JOIN training_centres c ON c.id = a.centre_id
        WHERE a.id = ?
        """,
        (admin_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"] or "admin",
        "centre_id": row["centre_id"],
        "centre_name": row["centre_name"],
        "active_batch_code": row["active_batch_code"] or "",
    }


@router.get("/me")
def me(request: Request):
    user = session_user(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in as the administrator.")
    return public_account(user)


@router.post("/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        from .db import transaction

        with transaction() as conn:
            conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
    response = JSONResponse({"ok": True})
    settings = cookie_settings(request)
    response.delete_cookie(
        COOKIE,
        path=settings["path"],
        samesite=settings["samesite"],
        secure=settings["secure"],
    )
    return response
