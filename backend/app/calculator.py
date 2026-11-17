"""Local attendance-hour rules.

These calculations belong to this dashboard. They are not an official DDU-GKY
attendance policy. Official portal statuses are stored separately and are never
replaced by a calculated status.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

STATUS_LABELS = {
    "full_day": "Present",
    "half_day": "Half Day",
    "absent": "Absent",
    "insufficient": "Insufficient Hours",
    "incomplete": "Incomplete Record",
    "not_verified": "Not Yet Verified",
    "before_enrollment": "Before Enrollment",
    "sunday": "Sunday",
    "future": "Future Date",
    "not_eligible": "Not Eligible",
}

OFFICIAL_LABELS = {
    "present": "Present",
    "half_day": "Half Day",
    "absent": "Absent",
    "rf": "RF Attendance",
    "ojt": "OJT Attendance",
    "mixed": "Multiple Categories",
    "unknown": "Unknown",
}

VERIFIED_STATUSES = {"full_day", "half_day", "absent", "insufficient"}
STATUS_WEIGHT = {"full_day": 1.0, "half_day": 0.5, "absent": 0.0, "insufficient": 0.0}

INACTIVE_WORDS = (
    "removed",
    "remove",
    "left",
    "drop",
    "dropped",
    "inactive",
    "cancel",
    "cancelled",
    "canceled",
    "terminated",
)

_EMPTY_TOKENS = {"", "-", "--", "na", "n/a", "null", "none", "nan", "nat"}
_TIME_FORMATS = ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p")
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d-%b-%Y",
    "%m/%d/%Y",
)


def now_ist() -> datetime:
    return datetime.now(IST)


def now_iso() -> str:
    return now_ist().isoformat(timespec="seconds")


def today_ist() -> date:
    return now_ist().date()


def parse_date(value: object) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if text.lower() in _EMPTY_TOKENS:
        return None
    head = text.replace("T", " ").split()[0].strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    try:
        serial = float(head)
    except ValueError:
        return None
    if 20000 < serial < 80000:
        return date(1899, 12, 30) + timedelta(days=int(serial))
    return None


def parse_clock(value: object) -> tuple[Optional[time], bool]:
    """Return (clock, invalid). Blank values are missing, not invalid."""
    if value is None:
        return None, False
    if isinstance(value, datetime):
        return value.time().replace(microsecond=0), False
    if isinstance(value, time):
        return value.replace(microsecond=0), False
    text = " ".join(str(value).strip().split())
    if text.lower() in _EMPTY_TOKENS:
        return None, False
    text = text.replace(".", ":").replace("T", " ")
    parts = text.split()
    meridiem = ""
    clock = parts[-1]
    if parts[-1].lower() in {"am", "pm"}:
        if len(parts) < 2:
            return None, True
        meridiem = parts[-1]
        clock = parts[-2]
    elif len(parts) >= 2 and re.search(r"\d", parts[-2]) and not re.search(r"\d", parts[-1]):
        return None, True
    candidate = f"{clock} {meridiem}".strip().upper()
    for fmt in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(candidate, fmt).time()
            return parsed.replace(microsecond=0), False
        except ValueError:
            continue
    return None, True


def parse_time_value(value: object) -> Optional[time]:
    clock, invalid = parse_clock(value)
    if invalid:
        raise ValueError(f"Invalid time: {value}")
    if clock is None:
        raise ValueError("Time is required")
    return clock


def format_clock(value: Optional[str]) -> Optional[str]:
    clock, invalid = parse_clock(value)
    if invalid or clock is None:
        return None
    return clock.strftime("%H:%M:%S")


def format_minutes(minutes: Optional[int]) -> Optional[str]:
    if minutes is None:
        return None
    sign = "-" if minutes < 0 else ""
    minutes = abs(int(minutes))
    hours, mins = divmod(minutes, 60)
    return f"{sign}{hours}h {mins:02d}m"


def hours_decimal(minutes: Optional[int]) -> Optional[float]:
    if minutes is None:
        return None
    return round(minutes / 60, 2)


@dataclass(frozen=True)
class SessionSpan:
    in_time: Optional[time]
    out_time: Optional[time]
    invalid: bool = False


@dataclass(frozen=True)
class HourResult:
    elapsed_minutes: Optional[int]
    lunch_deduction_minutes: Optional[int]
    qualifying_minutes: Optional[int]
    calculated_status: str
    invalid_time: bool
    missing_checkout: bool
    grace_band: bool = False


def _as_time(value: str) -> time:
    clock = parse_time_value(value)
    assert clock is not None
    return clock


def merge_intervals(intervals: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    cleaned = [(start, end) for start, end in intervals if end > start]
    cleaned.sort(key=lambda item: (item[0], item[1]))
    merged: list[list[datetime]] = []
    for start, end in cleaned:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return [(start, end) for start, end in merged]


def _clock_minutes(start: time, end: time) -> int:
    span = datetime.combine(date(2000, 1, 1), end) - datetime.combine(date(2000, 1, 1), start)
    return int(span.total_seconds() // 60)


def sop_limits(shift_minutes: int) -> tuple[int, int, int]:
    """Return full-day minutes, absent-after minutes, and grace occasions in 15 days.

    SF 4.3E / SF 6.3B. A one-shift batch is longer than 4 hours. A double-shift
    batch is 4 hours or less. Fifteen minutes at either edge is condoned and is
    not one of these occasions. Lunch is not part of this table.
    """
    if shift_minutes > 240:
        return 60, 240, 2
    return 30, 120, 4


def sop_status(late_minutes: int, early_minutes: int, shift_minutes: int = 540) -> tuple[str, bool]:
    """Classify one day from lateness at the start and early leaving at the end."""
    deviation = max(late_minutes, early_minutes)
    full_limit, absent_after, _occasions = sop_limits(shift_minutes)
    if deviation <= 15:
        return "full_day", False
    if deviation <= full_limit:
        return "full_day", True
    if deviation <= absent_after:
        return "half_day", False
    return "absent", False


def grace_days_to_demote(days: Iterable[tuple[date, bool]], limit: int = 2) -> set[date]:
    """An occasion past the permitted count inside any 15 calendar days becomes a half day."""
    flagged = sorted(day for day, grace in days if grace)
    demoted: set[date] = set()
    for day in flagged:
        window_start = day - timedelta(days=14)
        if sum(1 for earlier in flagged if window_start <= earlier <= day) > limit:
            demoted.add(day)
    return demoted


def calculate_hours(
    day: date,
    sessions: Iterable[SessionSpan],
    *,
    lunch_start: time,
    lunch_end: time,
    full_day_hours: float,
    half_day_hours: float,
    clip_start: Optional[time] = None,
    clip_end: Optional[time] = None,
    schedule_start: Optional[time] = None,
    schedule_end: Optional[time] = None,
) -> HourResult:
    """One-shift SOP status from training start/end (lunch not subtracted).

    0–15 min late/early → full day (condoned, no grace)
    16–60 min → full day + 1 grace
    61–240 min → half day
    > 4 hours → absent
    Only one punch / out 00:00:00 → full day
    No punch on a working day → absent (handled by callers)
    """
    del lunch_start, lunch_end, full_day_hours, half_day_hours, clip_start, clip_end
    invalid = False
    missing = False
    lone_in: list[time] = []
    intervals: list[tuple[datetime, datetime]] = []
    points: list[datetime] = []

    for session in sessions:
        if session.invalid:
            invalid = True
            continue
        out_time = session.out_time
        if out_time == time(0, 0):
            out_time = None
        if session.in_time and not out_time:
            missing = True
            lone_in.append(session.in_time)
            continue
        if out_time and not session.in_time:
            missing = True
            continue
        if not session.in_time or not out_time:
            continue
        start = datetime.combine(day, session.in_time)
        end = datetime.combine(day, out_time)
        if end < start:
            invalid = True
            continue
        if end == start:
            points.append(start)
            continue
        intervals.append((start, end))

    if not intervals and not points:
        if lone_in or missing:
            return HourResult(None, 0, None, "full_day", invalid, True, False)
        return HourResult(None, 0, None, "incomplete", invalid, False, False)

    merged = merge_intervals(intervals)
    elapsed_seconds = sum(int((end - start).total_seconds()) for start, end in merged)
    elapsed_minutes = elapsed_seconds // 60
    starts = [start for start, _end in merged] + points
    ends = [end for _start, end in merged] + points
    shift_minutes = 540
    if schedule_start and schedule_end:
        shift_minutes = max(0, _clock_minutes(schedule_start, schedule_end))
    if schedule_start is None or schedule_end is None:
        status, grace = "full_day", False
    else:
        first_in = min(starts)
        last_out = max(ends)
        late = max(0, int((first_in - datetime.combine(day, schedule_start)).total_seconds()) // 60)
        early = max(0, int((datetime.combine(day, schedule_end) - last_out).total_seconds()) // 60)
        status, grace = sop_status(late, early, shift_minutes)
    return HourResult(elapsed_minutes, 0, elapsed_minutes, status, invalid, missing, grace)


def rules_times(rules: dict) -> dict[str, time]:
    return {
        "training_start": _as_time(rules["training_start"]),
        "training_end": _as_time(rules["training_end"]),
        "lunch_start": _as_time(rules["lunch_start"]),
        "lunch_end": _as_time(rules["lunch_end"]),
    }


def scheduled_qualifying_minutes(rules: dict) -> int:
    clocks = rules_times(rules)
    start = datetime.combine(date(2000, 1, 1), clocks["training_start"])
    end = datetime.combine(date(2000, 1, 1), clocks["training_end"])
    if end <= start:
        return 0
    return int((end - start).total_seconds()) // 60


def validate_rules(rules: dict) -> dict:
    cleaned = dict(DEFAULT_RULES_FALLBACK)
    cleaned.update(rules or {})
    clocks = rules_times(cleaned)
    if clocks["training_end"] <= clocks["training_start"]:
        raise ValueError("Training end must be after training start.")
    if clocks["lunch_end"] <= clocks["lunch_start"]:
        raise ValueError("Lunch end must be after lunch start.")
    if not (clocks["training_start"] <= clocks["lunch_start"] < clocks["lunch_end"] <= clocks["training_end"]):
        raise ValueError("Lunch break must sit inside the training schedule.")
    full_hours = float(cleaned["full_day_hours"])
    half_hours = float(cleaned["half_day_hours"])
    if half_hours <= 0 or full_hours <= half_hours:
        raise ValueError("Full-day hours must be greater than half-day hours, and both must be positive.")
    threshold = float(cleaned["low_attendance_threshold"])
    if threshold < 0 or threshold > 100:
        raise ValueError("Low attendance threshold must be between 0 and 100.")
    cleaned["full_day_hours"] = full_hours
    cleaned["half_day_hours"] = half_hours
    cleaned["low_attendance_threshold"] = threshold
    cleaned["exclude_sundays"] = bool(cleaned["exclude_sundays"])
    cleaned["count_time_outside_schedule"] = bool(cleaned["count_time_outside_schedule"])
    for key in ("training_start", "training_end", "lunch_start", "lunch_end"):
        cleaned[key] = clocks[key].strftime("%H:%M")
    cleaned["scheduled_hours"] = round(scheduled_qualifying_minutes(cleaned) / 60, 2)
    return cleaned


# Local import avoided at class-body time; fallback mirrors config defaults.
DEFAULT_RULES_FALLBACK = {
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


def is_inactive_status(status: Optional[str]) -> bool:
    text = (status or "").casefold()
    return any(word in text for word in INACTIVE_WORDS)


def enrollment_window(
    candidate: dict,
    batch_start: Optional[date],
) -> tuple[Optional[date], Optional[date]]:
    """Return (first eligible date, last eligible date).

    For an active candidate, Candidate Add/Remove Date is the addition date.
    For an inactive candidate, that same column is treated as the removal date
    and eligibility starts at the batch start.
    """
    marker = parse_date(candidate.get("add_remove_date"))
    if is_inactive_status(candidate.get("candidate_status")):
        return batch_start, marker
    return marker or batch_start, None


EXCLUDED_TRAINING_DAYS = {date(2026, 10, 19)}


def total_training_days(start: Optional[date], today: date, end: Optional[date] = None) -> list[date]:
    """Working days from the batch start through today, or through the batch end when sooner.

    Sundays and configured non-working days are excluded. Future dates are excluded.
    """
    if start is None or start > today:
        return []
    last = today if end is None or end > today else end
    if last < start:
        return []
    days = [start + timedelta(days=offset) for offset in range((last - start).days + 1)]
    return [day for day in days if day.weekday() != 6 and day not in EXCLUDED_TRAINING_DAYS]


def calculate_attendance(full_days: float, half_days: float, total_working_days: float) -> float:
    """Attendance % = ((full days + half days × 0.5) / total working days) × 100.

    This is the dashboard rule. It is rounded to 2 decimal places. It is not claimed
    as an official Kaushal Bharat formula.
    """
    full = _non_negative(full_days)
    half = _non_negative(half_days)
    total = _non_negative(total_working_days)
    if total <= 0:
        return 0.0
    credits = min(full + half * Decimal("0.5"), total)
    return _percentage(credits, total)


def _non_negative(value: float) -> Decimal:
    try:
        number = Decimal(str(value))
    except Exception:
        return Decimal(0)
    if number.is_nan() or number < 0:
        return Decimal(0)
    return number


def _percentage(credits: Decimal, total: Decimal) -> float:
    percentage = (credits / total) * Decimal(100)
    return float(percentage.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_eligible_day(
    day: date,
    *,
    today: date,
    rules: dict,
    enrollment: Optional[date],
    removal: Optional[date],
) -> bool:
    if day > today:
        return False
    if rules.get("exclude_sundays", True) and day.weekday() == 6:
        return False
    if enrollment and day < enrollment:
        return False
    if removal and day > removal:
        return False
    return True


@dataclass
class AttendanceTotals:
    full_days: int = 0
    half_days: int = 0
    absent_days: int = 0
    insufficient_days: int = 0
    incomplete_days: int = 0
    unverified_days: int = 0
    eligible_days: int = 0
    verified_days: int = 0
    qualifying_minutes: int = 0
    attendance_credits: float = 0.0
    verified_percent: Optional[float] = None
    coverage_percent: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "full_days": self.full_days,
            "half_days": self.half_days,
            "absent_days": self.absent_days,
            "insufficient_days": self.insufficient_days,
            "incomplete_days": self.incomplete_days,
            "unverified_days": self.unverified_days,
            "eligible_days": self.eligible_days,
            "verified_days": self.verified_days,
            "qualifying_minutes": self.qualifying_minutes,
            "qualifying_hours": hours_decimal(self.qualifying_minutes),
            "qualifying_label": format_minutes(self.qualifying_minutes),
            "attendance_credits": self.attendance_credits,
            "verified_percent": self.verified_percent,
            "coverage_percent": self.coverage_percent,
        }


def summarize_candidate(
    portal_days: Iterable[date],
    records_by_day: dict[date, dict],
    *,
    today: date,
    rules: dict,
    enrollment: Optional[date],
    removal: Optional[date],
    batch_end: Optional[date] = None,
) -> AttendanceTotals:
    """One attendance percentage for every screen.

    Attendance % = ((full days + half days × 0.5) / total working days) × 100.
    Sundays, configured holidays, and future dates are excluded. A day before
    enrollment is absent. A completed working day with no mark is absent.
    Today is left out until that day's attendance is finalized.
    """
    del rules
    totals = AttendanceTotals()
    for day in portal_days:
        record = records_by_day.get(day)
        outcome = score_working_day(
            day,
            record,
            today=today,
            enrollment=enrollment,
            removal=removal,
            batch_end=batch_end,
        )
        if not outcome["include"]:
            if outcome["status"] == "incomplete":
                totals.incomplete_days += 1
            elif outcome["status"] == "not_verified":
                totals.unverified_days += 1
            continue
        totals.eligible_days += 1
        totals.verified_days += 1
        credit = outcome["credit"]
        totals.attendance_credits += credit
        status = outcome["status"]
        if status == "full_day":
            totals.full_days += 1
        elif status == "half_day":
            totals.half_days += 1
        elif status == "insufficient":
            totals.insufficient_days += 1
            totals.absent_days += 1
        else:
            totals.absent_days += 1
        minutes = (record or {}).get("qualifying_minutes")
        if isinstance(minutes, int):
            totals.qualifying_minutes += max(0, minutes)
    totals.verified_percent = (
        calculate_attendance(totals.full_days, totals.half_days, totals.eligible_days)
        if totals.eligible_days
        else None
    )
    totals.attendance_credits = float(totals.full_days + Decimal(totals.half_days) * Decimal("0.5"))
    if totals.eligible_days:
        totals.coverage_percent = float(
            (Decimal(totals.verified_days) / Decimal(totals.eligible_days) * Decimal(100)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
    else:
        totals.coverage_percent = 0.0
    return totals


def _calendar_block(
    day: date,
    *,
    today: date,
    enrollment: Optional[date],
    removal: Optional[date],
    batch_end: Optional[date],
) -> Optional[str]:
    if day > today:
        return "future"
    if batch_end and day > batch_end:
        return "not_eligible"
    if day.weekday() == 6:
        return "sunday"
    if day in EXCLUDED_TRAINING_DAYS:
        return "not_eligible"
    del enrollment
    if removal and day > removal:
        return "not_eligible"
    return None


def _finalized(record: Optional[dict]) -> bool:
    if not record:
        return False
    official = record.get("official_status")
    status = record.get("calculated_status")
    if official in {"present", "half_day", "absent"}:
        return True
    return status in VERIFIED_STATUSES and not _unearned_full_day(record)


def _unearned_full_day(record: dict) -> bool:
    """A single punch is a full day under the SOP, so it is not removed here."""
    del record
    return False


def _category_set(record: dict) -> set[str]:
    raw = record.get("official_categories")
    if isinstance(raw, (list, tuple, set)):
        return {str(item) for item in raw}
    if not isinstance(raw, str) or not raw.strip():
        return set()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if isinstance(parsed, list):
        return {str(item) for item in parsed}
    return set()


def _portal_credit(record: dict) -> Optional[tuple[str, float]]:
    """Classroom percentage only. Present, Half day, and Absent are the classroom marks.

    RF is residential and OJT is on-the-job. Neither adds a classroom day. A portal
    Present mark still counts when RF or OJT is listed on the same date.
    """
    official = record.get("official_status") or ""
    marks = _category_set(record)
    if not marks and official not in {"", "mixed", "unknown"}:
        marks = {official}
    if not marks:
        return None
    if "present" in marks:
        return "full_day", 1.0
    if "half_day" in marks:
        return "half_day", 0.5
    if "absent" in marks:
        return "absent", 0.0
    if marks & {"rf", "ojt"}:
        return "absent", 0.0
    return None


def _credit(record: Optional[dict]) -> tuple[str, Optional[float]]:
    if not record:
        return "absent", 0.0
    portal = _portal_credit(record)
    if portal is not None:
        return portal
    status = record.get("calculated_status") or ""
    if status == "full_day" and _unearned_full_day(record):
        return "incomplete", None
    if status == "full_day":
        return "full_day", 1.0
    if status == "half_day":
        return "half_day", 0.5
    if status == "absent":
        return "absent", 0.0
    if status == "insufficient":
        return "insufficient", 0.0
    if status == "incomplete":
        return "incomplete", None
    return "absent", 0.0


def score_working_day(
    day: date,
    record: Optional[dict],
    *,
    today: date,
    enrollment: Optional[date],
    removal: Optional[date],
    batch_end: Optional[date] = None,
) -> dict:
    blocked = _calendar_block(day, today=today, enrollment=enrollment, removal=removal, batch_end=batch_end)
    if blocked:
        return {"include": False, "status": blocked, "credit": 0.0}
    if enrollment and day < enrollment:
        return {"include": True, "status": "absent", "credit": 0.0}
    if day == today and not _finalized(record):
        return {"include": False, "status": "not_verified", "credit": 0.0}
    status, credit = _credit(record)
    if credit is None:
        return {"include": False, "status": status, "credit": 0.0}
    return {"include": True, "status": status, "credit": credit}


def cell_status(
    day: date,
    record: Optional[dict],
    *,
    today: date,
    rules: dict,
    enrollment: Optional[date],
    removal: Optional[date],
    batch_end: Optional[date] = None,
) -> dict:
    del rules
    outcome = score_working_day(
        day,
        record,
        today=today,
        enrollment=enrollment,
        removal=removal,
        batch_end=batch_end,
    )
    status = outcome["status"]
    eligible = outcome["include"]
    return {
        "status": status,
        "label": STATUS_LABELS.get(status, status),
        "counts_toward_percent": bool(eligible and status in VERIFIED_STATUSES),
        "eligible": eligible,
        "official_status": (record or {}).get("official_status"),
        "official_label": OFFICIAL_LABELS.get((record or {}).get("official_status") or "", None),
        "qualifying_minutes": (record or {}).get("qualifying_minutes"),
        "has_record": record is not None,
        "warning": bool(
            record
            and record.get("official_status") not in (None, "", "unknown")
            and record.get("calculated_status")
            and _official_disagrees(record.get("official_status"), record.get("calculated_status"))
        ),
    }


def _official_disagrees(official: Optional[str], calculated: Optional[str]) -> bool:
    if official in {None, "", "unknown", "mixed"}:
        return official == "mixed"
    expected = {
        "present": {"full_day"},
        "half_day": {"half_day"},
        "absent": {"absent", "insufficient"},
        "rf": set(),
        "ojt": set(),
    }.get(official, set())
    if not expected:
        return False
    return calculated not in expected

