"""Local DDU-GKY attendance monitor."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .auth import COOKIE, router as auth_router, session_admin
from .config import DATA_DIR, LOG_PATH, PROJECT_DIR
from .db import init_db, mark_interrupted_syncs

PUBLIC_API = {"/api/health", "/api/auth/login", "/api/auth/me", "/api/auth/logout"}

logger = logging.getLogger("attendance")


def configure_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    init_db()
    mark_interrupted_syncs()
    yield


app = FastAPI(title="DDU-GKY Attendance Monitor", lifespan=lifespan)


@app.middleware("http")
async def require_admin(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or not path.startswith("/api") or path in PUBLIC_API:
        return await call_next(request)
    if session_admin(request.cookies.get(COOKIE)):
        return await call_next(request)
    return JSONResponse({"detail": "Sign in as the administrator."}, status_code=401)


app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(router)

DIST = PROJECT_DIR / "frontend" / "dist"
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/")
@app.get("/{full_path:path}")
def frontend(full_path: str = ""):
    index = DIST / "index.html"
    if full_path.startswith("api"):
        raise HTTPException(status_code=404, detail="Not found")
    if index.exists():
        candidate = (DIST / full_path).resolve() if full_path else index
        if full_path and str(candidate).startswith(str(DIST.resolve())) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)
    if full_path:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "service": "DDU-GKY Attendance Monitor",
        "ui": "http://127.0.0.1:5173",
        "docs": "/docs",
    }
