"""Paths and default batch rules for the local attendance monitor."""

from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
DATA_DIR = BACKEND_DIR / "data"
CANDIDATE_WORKBOOK = DATA_DIR / "Candidate_Details.xlsx"
BROWSER_PROFILE = DATA_DIR / "browser-profile"
SCREENSHOT_DIR = DATA_DIR / "sync-screenshots"
LOG_PATH = DATA_DIR / "sync.log"

PORTAL_HOME = "https://kaushal.dord.gov.in/"
PORTAL_LOGIN_URL = "https://kaushal.dord.gov.in/"
PORTAL_REPORT_URL = "https://kaushal.dord.gov.in/#/landing-page/candidate-attendance-report"
# Faster sync path: one Candidate ID submit returns that person's full punch history.
PORTAL_CANDIDATE_DETAILS_URL = "https://kaushal.dord.gov.in/#/landing-page/candidate-attendance-details"

RESET_PHRASE = "RESET LOCAL ATTENDANCE"

DEFAULT_BATCH = {
    "state": "ANDHRA PRADESH",
    "pia": "KK Educational and Rural Development Society",
    "sanction_order": "SO26D021933",
    "training_center": "KKERS-ONGOLE",
    "batch_code": "BT26D023667",
    "batch_start_date": "2026-10-16",
    "expected_end_date": "2027-01-23",
}

DEFAULT_RULES = {
    "training_start": "09:00",
    "training_end": "18:00",
    "lunch_start": "13:00",
    "lunch_end": "14:00",
    "full_day_hours": 6,
    "half_day_hours": 3,
    "exclude_sundays": True,
    "low_attendance_threshold": 75,
    "count_time_outside_schedule": True,
}


def db_path() -> Path:
    return Path(os.environ.get("ATTENDANCE_DB", str(DATA_DIR / "attendance.db")))


def candidate_workbook() -> Path:
    override = os.environ.get("ATTENDANCE_WORKBOOK")
    return Path(override) if override else CANDIDATE_WORKBOOK


def skip_seed() -> bool:
    return os.environ.get("ATTENDANCE_SKIP_SEED") == "1"
