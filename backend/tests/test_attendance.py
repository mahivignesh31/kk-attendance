"""Accuracy tests for hours, matching, portal parsing, and the master workbook."""

from datetime import date, timedelta

import pytest

from app.calculator import (
    SessionSpan,
    calculate_attendance,
    calculate_hours,
    grace_days_to_demote,
    parse_clock,
    sop_status,
    summarize_candidate,
    validate_rules,
)
from app.config import CANDIDATE_WORKBOOK, DEFAULT_RULES
from app.matching import match_candidate
from app.candidate_sync import build_date_payload, classify_day_rows, filter_dates, group_punches_by_date
from app.portal_parse import parse_candidate_punch_table, parse_detail_table, parse_summary_table
from app.reconcile import reconcile_date

RULES = validate_rules(DEFAULT_RULES)
DAY = date(2026, 11, 16)


def hours(start, end, *extra, clip=False):
    spans = []
    for begin, finish in ((start, end), *extra):
        in_clock, in_invalid = parse_clock(begin)
        if finish:
            out_clock, out_invalid = parse_clock(finish)
        else:
            out_clock, out_invalid = None, False
        spans.append(SessionSpan(in_clock, out_clock, invalid=in_invalid or out_invalid))
    return calculate_hours(
        DAY,
        spans,
        lunch_start=parse_clock(RULES["lunch_start"])[0],
        lunch_end=parse_clock(RULES["lunch_end"])[0],
        full_day_hours=RULES["full_day_hours"],
        half_day_hours=RULES["half_day_hours"],
        clip_start=parse_clock(RULES["training_start"])[0] if clip else None,
        clip_end=parse_clock(RULES["training_end"])[0] if clip else None,
        schedule_start=parse_clock(RULES["training_start"])[0],
        schedule_end=parse_clock(RULES["training_end"])[0],
    )


def test_full_schedule_follows_the_sop_and_does_not_subtract_lunch():
    result = hours("09:00", "18:00")
    assert (result.elapsed_minutes, result.lunch_deduction_minutes, result.qualifying_minutes) == (540, 0, 540)
    assert result.calculated_status == "full_day"
    assert result.grace_band is False
    assert RULES["scheduled_hours"] == 9


def test_koduri_example_duration_is_not_invented():
    result = hours("08:25:34", "18:14:38")
    assert result.elapsed_minutes == 589
    assert result.lunch_deduction_minutes == 0
    assert result.qualifying_minutes == 589
    assert result.calculated_status == "full_day"
    assert result.grace_band is False


def test_sop_late_and_early_limits():
    condoned = hours("09:10", "18:00")
    assert condoned.calculated_status == "full_day"
    assert condoned.grace_band is False
    grace = hours("09:20", "17:40")
    assert grace.calculated_status == "full_day"
    assert grace.grace_band is True
    half = hours("10:30", "18:00")
    assert half.calculated_status == "half_day"
    assert half.grace_band is False
    left_early = hours("09:00", "15:00")
    assert left_early.calculated_status == "half_day"
    assert hours("09:00", "12:00").calculated_status == "absent"
    assert hours("14:00", "18:00").calculated_status == "absent"


def test_overlapping_sessions_are_counted_once():
    merged = hours("09:00", "13:30", ("13:00", "18:00"))
    assert (merged.elapsed_minutes, merged.lunch_deduction_minutes, merged.qualifying_minutes) == (540, 0, 540)
    assert merged.calculated_status == "full_day"


def test_double_shift_uses_the_shorter_sop_table():
    assert sop_status(10, 0, 180) == ("full_day", False)
    assert sop_status(20, 0, 180) == ("full_day", True)
    assert sop_status(45, 0, 180)[0] == "half_day"
    assert sop_status(130, 0, 180)[0] == "absent"


def test_the_third_grace_day_in_fifteen_days_becomes_a_half_day():
    start = date(2026, 11, 2)
    flagged = [(start + timedelta(days=offset), True) for offset in (0, 1, 2)]
    assert grace_days_to_demote(flagged) == {start + timedelta(days=2)}
    assert grace_days_to_demote(flagged[:2]) == set()


def test_one_punch_is_a_full_day():
    missing = hours("09:00", None)
    assert missing.calculated_status == "full_day"
    assert missing.missing_checkout is True
    assert missing.qualifying_minutes is None
    assert missing.grace_band is False
    assert hours("09:00", None, ("10:00", None)).calculated_status == "full_day"
    midnight_out = hours("08:37:13", "00:00:00")
    assert midnight_out.calculated_status == "full_day"
    assert midnight_out.missing_checkout is True
    backwards = hours("18:00", "09:00")
    assert backwards.calculated_status == "incomplete"
    assert backwards.invalid_time is True
    same_minute = hours("08:20:58", "08:20:58")
    assert same_minute.calculated_status == "absent"
    assert same_minute.missing_checkout is False


def _friday() -> date:
    cursor = date(2026, 10, 16)
    while cursor.weekday() != 4:
        cursor += timedelta(days=1)
    return cursor


def test_percentage_uses_every_day_through_today():
    friday = _friday()
    saturday = friday + timedelta(days=1)
    sunday = friday + timedelta(days=2)
    monday = friday + timedelta(days=3)
    records = {
        friday: {"calculated_status": "full_day", "qualifying_minutes": 480},
        saturday: {"calculated_status": "half_day", "qualifying_minutes": 240},
        sunday: {"calculated_status": "full_day", "qualifying_minutes": 480},
        monday: {"calculated_status": "not_verified"},
    }
    totals = summarize_candidate(
        [friday, saturday, sunday, monday, monday + timedelta(days=10)],
        records,
        today=monday,
        rules=RULES,
        enrollment=saturday,
        removal=None,
    )
    assert totals.half_days == 1
    assert totals.full_days == 0
    assert totals.absent_days == 1
    assert totals.eligible_days == 2
    assert totals.verified_percent == 25.0


def test_a_day_with_no_attendance_mark_is_absent():
    friday = date(2026, 11, 6)
    saturday = friday + timedelta(days=1)
    monday = friday + timedelta(days=3)
    records = {
        friday: {"calculated_status": "full_day", "qualifying_minutes": 480},
        saturday: {"calculated_status": "insufficient", "qualifying_minutes": 90},
    }
    totals = summarize_candidate(
        [friday, saturday, monday],
        records,
        today=monday + timedelta(days=1),
        rules=RULES,
        enrollment=friday,
        removal=None,
    )
    assert totals.verified_percent == 33.33
    assert totals.absent_days == 2
    assert totals.insufficient_days == 1
    assert totals.unverified_days == 0


def test_portal_absence_lowers_the_percentage_even_when_hours_look_complete():
    friday = _friday()
    saturday = friday + timedelta(days=1)
    records = {
        friday: {"calculated_status": "full_day", "official_status": "present", "qualifying_minutes": 480},
        saturday: {"calculated_status": "full_day", "official_status": "absent", "qualifying_minutes": 480},
    }
    totals = summarize_candidate(
        [friday, saturday],
        records,
        today=saturday + timedelta(days=1),
        rules=RULES,
        enrollment=friday,
        removal=None,
    )
    assert totals.full_days == 1
    assert totals.absent_days == 1
    assert totals.verified_percent == 50.0


def test_four_full_and_one_half_out_of_nine_days_is_50_percent():
    start = date(2026, 11, 6)
    days = [start + timedelta(days=offset) for offset in range(11)]
    eligible = [day for day in days if day.weekday() != 6][:9]
    records = {}
    for day in eligible[:4]:
        records[day] = {"calculated_status": "full_day", "qualifying_minutes": 480}
    records[eligible[4]] = {"calculated_status": "half_day", "qualifying_minutes": 240}
    for day in eligible[5:]:
        records[day] = {"calculated_status": "absent", "official_status": "absent", "qualifying_minutes": 0}
    totals = summarize_candidate(
        eligible,
        records,
        today=eligible[-1],
        rules=RULES,
        enrollment=eligible[0],
        removal=None,
    )
    assert totals.eligible_days == 9
    assert totals.full_days == 4
    assert totals.half_days == 1
    assert totals.absent_days == 4
    assert totals.verified_percent == 50.0


def test_attendance_formula_rounds_to_two_decimals():
    assert calculate_attendance(19, 1, 28) == 69.64
    assert calculate_attendance(20, 0, 28) == 71.43
    assert calculate_attendance(25, 0, 28) == 89.29
    assert calculate_attendance(15, 5, 25) == 70.00
    assert calculate_attendance(0, 0, 28) == 0.00
    assert calculate_attendance(28, 0, 28) == 100.00
    assert calculate_attendance(0, 0, 0) == 0.00


def test_batch_percentage_uses_the_same_formula():
    weighted = calculate_attendance(19 + 10, 1, 28 + 10)
    plain_average = round((69.64 + 100) / 2, 2)
    assert weighted == 77.63
    assert weighted != plain_average


def test_portal_present_is_reflected_even_when_the_sop_hours_are_short():
    day = date(2026, 11, 6)
    totals = summarize_candidate(
        [day],
        {day: {"calculated_status": "absent", "official_status": "present", "official_categories": ["present"], "in_time": "08:20:58", "out_time": "08:20:58"}},
        today=day + timedelta(days=1),
        rules=RULES,
        enrollment=day,
        removal=None,
    )
    assert totals.full_days == 1
    assert totals.absent_days == 0
    assert totals.verified_percent == 100.0
    mixed = summarize_candidate(
        [day],
        {day: {"calculated_status": "half_day", "official_status": "mixed", "official_categories": ["present", "rf"]}},
        today=day + timedelta(days=1),
        rules=RULES,
        enrollment=day,
        removal=None,
    )
    assert mixed.full_days == 1
    assert mixed.verified_percent == 100.0
    residential = summarize_candidate(
        [day],
        {day: {"calculated_status": "full_day", "official_status": "rf", "official_categories": ["rf"], "qualifying_minutes": 480}},
        today=day + timedelta(days=1),
        rules=RULES,
        enrollment=day,
        removal=None,
    )
    assert residential.full_days == 0
    assert residential.absent_days == 1
    assert residential.verified_percent == 0.0
    on_job = summarize_candidate(
        [day],
        {day: {"calculated_status": "full_day", "official_status": "ojt", "official_categories": ["ojt"], "qualifying_minutes": 480}},
        today=day + timedelta(days=1),
        rules=RULES,
        enrollment=day,
        removal=None,
    )
    assert on_job.full_days == 0
    assert on_job.absent_days == 1
    assert on_job.verified_percent == 0.0


def test_official_present_counts_when_the_checkout_is_missing():
    day = date(2026, 11, 6)
    totals = summarize_candidate(
        [day],
        {day: {"calculated_status": "incomplete", "official_status": "present", "in_time": "08:37:13", "out_time": "00:00:00"}},
        today=day + timedelta(days=1),
        rules=RULES,
        enrollment=day,
        removal=None,
    )
    assert totals.full_days == 1
    assert totals.verified_percent == 100.0


def test_today_is_left_out_until_attendance_is_finalized():
    day = date(2026, 11, 6)
    totals = summarize_candidate(
        [day],
        {},
        today=day,
        rules=RULES,
        enrollment=day,
        removal=None,
    )
    assert totals.eligible_days == 0
    assert totals.verified_percent is None


def test_names_do_not_merge_on_similarity():
    candidates = [
        {"id": 1, "candidate_id": "2628772980", "attendance_id": "46334806", "name": "Koduri Sathireddy"},
        {"id": 2, "candidate_id": "2628752093", "attendance_id": "09372544", "name": "Badugu Mamatha"},
    ]
    exact = match_candidate({"candidate_id": "2628772980", "name": "KODURI  SATHIREDDY"}, candidates)
    assert exact.method == "candidate_id"
    assert exact.warning is None
    similar = match_candidate({"candidate_id": "", "attendance_id": "", "name": "Koduri Sathi Reddy"}, candidates)
    assert similar.candidate is None
    fallback = match_candidate({"name": "Koduri Sathireddy"}, candidates)
    assert fallback.method == "name_fallback"
    assert fallback.warning
    padded = match_candidate({"attendance_id": "9372544", "name": "Someone Else"}, candidates)
    assert padded.candidate is None


def test_summary_parser_does_not_invent_dates():
    headers = [
        "Sr No", "Date", "Number of Candidates Enrolled", "Present", "Half Day",
        "Absent", "RF Attendance", "OJT Attendance", "LogBook Status",
    ]
    rows, warnings = parse_summary_table(
        headers,
        [["1", "16/10/2026", "35", "30", "2", "3", "0", "0", "Completed"], ["2", "", "35", "1", "0", "0", "0", "0", ""]],
    )
    assert [row["date"] for row in rows] == ["2026-10-16"]
    assert rows[0]["present"] == 30
    assert rows[0]["half_day"] == 2
    assert warnings


def test_reconcile_uses_timestamps_and_does_not_double_count():
    candidates = [
        {"id": 19, "candidate_id": "2628772980", "attendance_id": "46334806", "name": "Koduri Sathireddy"},
        {"id": 6, "candidate_id": "2628752093", "attendance_id": "09372544", "name": "Badugu Mamatha"},
        {"id": 7, "candidate_id": "2628752463", "attendance_id": "36443683", "name": "Gangireddy Kalyani"},
    ]
    headers = [
        "Sr No", "Candidate Id", "Candidate Name", "Attendance Id", "Device Type",
        "Attendance Date", "In Time", "In Device Id", "Out Time", "Out Device Id",
    ]
    present_rows, _ = parse_detail_table(
        headers,
        [
            ["19", "2628772980", "Koduri Sathireddy", "46334806", "Biometric", "2026-11-16", "08:25:34", "IN1", "18:14:38", "OUT1"],
            ["6", "2628752093", "Badugu Mamatha", "09372544", "Biometric", "2026-11-16", "09:10:00", "IN1", "", ""],
        ],
        "present",
    )
    absent_rows, _ = parse_detail_table(
        headers,
        [["7", "2628752463", "Gangireddy Kalyani", "36443683", "", "2026-11-16", "", "", "", ""]],
        "absent",
    )
    result = reconcile_date(
        attendance_day=DAY,
        summary={"present": 2, "half_day": 0, "absent": 1, "rf": 0, "ojt": 0},
        detail_rows=present_rows + absent_rows,
        candidates=candidates,
        rules=RULES,
        opened_categories={"present", "half_day", "absent", "rf", "ojt"},
        failed_categories=set(),
    )
    by_id = {row.candidate_pk: row for row in result.records}
    assert result.complete is True
    assert by_id[19].calculated_status == "full_day"
    assert by_id[19].qualifying_minutes == 589
    assert by_id[19].official_status == "present"
    assert by_id[6].calculated_status == "full_day"
    assert by_id[6].out_time is None
    assert by_id[7].calculated_status == "absent"
    assert by_id[7].qualifying_minutes == 0
    assert len(result.records) == 3


def test_summary_mismatch_blocks_a_complete_reconcile():
    candidates = [{"id": 1, "candidate_id": "2628772980", "attendance_id": "1", "name": "Koduri Sathireddy"}]
    result = reconcile_date(
        attendance_day=DAY,
        summary={"present": 5, "half_day": 0, "absent": 0, "rf": 0, "ojt": 0},
        detail_rows=[{
            "category": "present",
            "candidate_id": "2628772980",
            "name": "Koduri Sathireddy",
            "attendance_id": "1",
            "in_time": "09:00:00",
            "out_time": "18:00:00",
            "in_invalid": False,
            "out_invalid": False,
        }],
        candidates=candidates,
        rules=RULES,
        opened_categories={"present"},
        failed_categories=set(),
    )
    assert result.complete is False
    assert any(issue.issue_type == "summary_mismatch" for issue in result.issues)
    assert result.records[0].calculated_status == "full_day"


def test_master_workbook_imports_all_35_without_losing_leading_zeros(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTENDANCE_DB", str(tmp_path / "candidates.db"))
    from app.db import init_db, read_conn
    from app.importer import import_workbook

    init_db()
    first = import_workbook(CANDIDATE_WORKBOOK)
    assert first["inserted"] == 35
    assert first["total_candidates"] == 35
    assert first["errors"] == []
    with read_conn() as conn:
        mamatha = conn.execute(
            "SELECT attendance_id, name FROM candidates WHERE candidate_id = ?",
            ("2628752093",),
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) AS count FROM candidates").fetchone()["count"]
        attendance_rows = conn.execute("SELECT COUNT(*) AS count FROM attendance_records").fetchone()["count"]
    assert mamatha["name"] == "Badugu Mamatha"
    assert mamatha["attendance_id"] == "09372544"
    assert total == 35
    assert attendance_rows == 0
    second = import_workbook(CANDIDATE_WORKBOOK)
    assert second["inserted"] == 0
    assert second["updated"] == 35


def test_recalculate_changes_local_status_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTENDANCE_DB", str(tmp_path / "rules.db"))
    from app.db import init_db, transaction
    from app.reporting import recalculate_all

    init_db()
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO candidates (
                candidate_id, attendance_id, name, sr_no, candidate_status, add_remove_date,
                created_at, updated_at
            ) VALUES ('2628772980', '46334806', 'Koduri Sathireddy', 19, 'Active', '2026-10-15', 'now', 'now')
            """
        )
        candidate_pk = conn.execute("SELECT id FROM candidates").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO attendance_records (
                candidate_pk, attendance_date, official_status, official_categories, calculated_status,
                in_time, out_time, data_source, last_synced_at
            ) VALUES (?, '2026-11-16', 'present', '["present"]', 'full_day', '09:00:00', '15:00:00', 'kaushal_portal', 'now')
            """,
            (candidate_pk,),
        )
        record_id = conn.execute("SELECT id FROM attendance_records").fetchone()["id"]
        conn.execute(
            "INSERT INTO attendance_sessions (record_id, session_index, in_time, out_time, invalid) VALUES (?, 0, '09:00:00', '15:00:00', 0)",
            (record_id,),
        )
        recalculate_all(conn, RULES)
        stored = conn.execute(
            "SELECT calculated_status, official_status, qualifying_minutes FROM attendance_records"
        ).fetchone()
    assert stored["official_status"] == "present"
    assert stored["calculated_status"] == "half_day"
    assert stored["qualifying_minutes"] == 360


def test_excel_import_ignores_dates_before_batch_start():
    from app.excel_sync import _drop_before_batch_start

    punches = [
        {"attendance_date": "2026-10-15", "candidate_id": "1"},
        {"attendance_date": "2026-10-16", "candidate_id": "1"},
        {"attendance_date": "2026-10-17", "candidate_id": "1"},
    ]
    kept, skipped = _drop_before_batch_start(punches, date(2026, 10, 16))
    assert skipped == 1
    assert [row["attendance_date"] for row in kept] == ["2026-10-16", "2026-10-17"]


def test_parse_candidate_punch_excel_skips_title_row(tmp_path):
    from openpyxl import Workbook

    from app.portal_parse import parse_candidate_punch_excel

    path = tmp_path / "Candidate_Attendance_Report.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Candidate Attendance Report"])
    ws.append(
        [
            "Sr. No.",
            "Candidate ID",
            "Attendance ID",
            "Batch Reg No.",
            "Consume Date",
            "Log Date",
            "In Time",
            "In Device ID",
            "Out Time",
            "Out Device ID",
            "Latitude",
            "Longitude",
            "Location",
        ]
    )
    ws.append(
        [
            1,
            "2628750937",
            "96055455",
            "BT26D023667",
            "16-10-2026",
            "16-10-2026",
            "16-10-2026 10:31:01",
            "146037",
            "16-10-2026 18:07:46",
            "146154",
            "15.5029",
            "80.0455",
            "KKERS",
        ]
    )
    wb.save(path)

    punches, warnings = parse_candidate_punch_excel(path)
    assert warnings == []
    assert len(punches) == 1
    assert punches[0]["candidate_id"] == "2628750937"
    assert punches[0]["attendance_date"] == "2026-10-16"
    assert punches[0]["in_time"] == "10:31:01"
    assert punches[0]["out_time"] == "18:07:46"


def test_candidate_id_punch_table_builds_present_and_absent():
    headers = [
        "Sr. No.",
        "Candidate ID",
        "Attendance ID",
        "Batch Reg No.",
        "Consume Date",
        "Log Date",
        "In Time",
        "In Device ID",
        "Out Time",
        "Out Device ID",
        "Latitude",
        "Longitude",
        "Location",
    ]
    rows = [
        [
            "1",
            "2628076517",
            "16190001",
            "BT26D023667",
            "16-10-2026",
            "16-10-2026",
            "16-10-2026 08:24:10",
            "146037",
            "16-10-2026 18:00:05",
            "146037",
            "15.5",
            "80.0",
            "KKEF",
        ],
        [
            "2",
            "2628076517",
            "16180856",
            "BT26D023667",
            "15-10-2026",
            "15-10-2026",
            "15-10-2026 16:27:52",
            "146037",
            "15-10-2026 18:19:35",
            "146037",
            "15.5",
            "80.0",
            "KKEF",
        ],
    ]
    punches, warnings = parse_candidate_punch_table(headers, rows)
    assert warnings == []
    assert punches[0]["attendance_date"] == "2026-10-16"
    by_date = group_punches_by_date(punches)
    assert set(by_date) == {"2026-10-15", "2026-10-16"}
    category, result = classify_day_rows(by_date["2026-10-16"], day=date(2026, 10, 16), rules=RULES)
    assert category == "present"
    assert result.calculated_status == "full_day"
    candidates = [
        {"id": 1, "candidate_id": "2628076517", "name": "Test One", "sr_no": 1},
        {"id": 2, "candidate_id": "9999999999", "name": "Test Two", "sr_no": 2},
    ]
    summary, details, opened, failed = build_date_payload(
        attendance_day=date(2026, 10, 16),
        day_punches=by_date["2026-10-16"],
        candidates=candidates,
        rules=RULES,
    )
    assert summary["present"] == 1
    assert summary["absent"] == 1
    assert summary["enrolled"] == 2
    assert failed == set()
    assert "present" in opened
    assert any(row["category"] == "absent" and row["candidate_id"] == "9999999999" for row in details)
    assert filter_dates(
        ["2026-10-15", "2026-10-16"],
        mode="incremental",
        date_from=None,
        date_to=None,
        existing={"2026-10-15": {"detail_synced": 1}},
    ) == ["2026-10-16"]
    assert filter_dates(
        ["2026-10-16"],
        mode="range",
        date_from="2026-10-15",
        date_to="2026-10-17",
        existing={},
    ) == ["2026-10-15", "2026-10-16", "2026-10-17"]
    assert filter_dates(
        [],
        mode="range",
        date_from="2026-11-01",
        date_to="2026-11-01",
        existing={},
    ) == ["2026-11-01"]


def test_api_serves_candidates_exports_and_does_not_seed_attendance(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.setenv("ATTENDANCE_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("ATTENDANCE_SKIP_SEED", "1")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["writes_to_portal"] is False
        locked = client.get("/api/dashboard")
        assert locked.status_code == 401
        login = client.post("/api/auth/login", json={"username": "admin", "password": "Admin@123"})
        assert login.status_code == 200
        with CANDIDATE_WORKBOOK.open("rb") as handle:
            imported = client.post(
                "/api/candidates/import",
                files={"file": ("Candidate_Details.xlsx", handle, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
        assert imported.status_code == 200, imported.text
        assert imported.json()["total_candidates"] == 35
        payload = client.get("/api/dashboard").json()
        assert payload["cards"]["total_candidates"] == 35
        assert payload["cards"]["full_day"] is None
        assert payload["portal_dates"] == []
        matrix = client.get("/api/matrix").json()
        assert matrix["candidate_count"] == 35
        assert matrix["dates"] == []
        names = [row["name"] for row in matrix["rows"]]
        assert "Koduri Sathireddy" in names
        exported = client.get("/api/export/daily", params={"date": "2026-10-16"})
        assert exported.status_code == 200
        assert exported.content[:2] == b"PK"
        rejected = client.post("/api/admin/reset", json={"confirmation": "delete the portal"})
        assert rejected.status_code == 400
        settings = client.get("/api/settings").json()
        settings["rules"]["half_day_hours"] = 8
        settings["rules"]["full_day_hours"] = 6
        invalid = client.put("/api/settings", json=settings)
        assert invalid.status_code == 400
