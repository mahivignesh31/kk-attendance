"""Excel exports with frozen headers, filters, and consistent date formats."""

from __future__ import annotations

from io import BytesIO

import pandas as pd
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .calculator import OFFICIAL_LABELS, STATUS_LABELS, hours_decimal

PURPLE = "5B2A86"
GOLD = "C4A35A"
THIN = Border(
    left=Side(style="thin", color="E6E0EF"),
    right=Side(style="thin", color="E6E0EF"),
    top=Side(style="thin", color="E6E0EF"),
    bottom=Side(style="thin", color="E6E0EF"),
)
STATUS_FILLS = {
    "Present": "DCFCE7",
    "Half Day": "FEF3C7",
    "Absent": "FEE2E2",
    "Insufficient Hours": "FEE2E2",
    "Incomplete Record": "FFEDD5",
    "Not Yet Verified": "F1F5F9",
    "Before Enrollment": "F8FAFC",
    "Sunday": "F8FAFC",
    "Future Date": "F8FAFC",
}


def workbook_response(sheets: list[tuple[str, list[dict]]], color_status: bool = False) -> BytesIO:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, rows in sheets:
            frame = pd.DataFrame(rows)
            if frame.empty:
                frame = pd.DataFrame([{"Message": "No rows for this export."}])
            frame.to_excel(writer, index=False, sheet_name=name[:31])
            style_worksheet(writer.book[name[:31]], color_status=color_status)
    buffer.seek(0)
    return buffer


def style_worksheet(worksheet: Worksheet, color_status: bool = False) -> None:
    worksheet.freeze_panes = "A2"
    if worksheet.dimensions:
        worksheet.auto_filter.ref = worksheet.dimensions
    worksheet.auto_filter.ref = worksheet.dimensions
    worksheet.row_dimensions[1].height = 24
    worksheet.page_setup.orientation = "landscape"
    worksheet.page_setup.fitToPage = True
    worksheet.page_setup.fitToWidth = 1
    worksheet.page_setup.fitToHeight = 0
    worksheet.page_setup.paperSize = worksheet.PAPERSIZE_A4
    worksheet.sheet_properties.pageSetUpPr.fitToPage = True
    worksheet.oddHeader.left.text = "KKERS Ongole · DDU-GKY attendance"
    worksheet.oddFooter.left.text = "Local dashboard export. Official portal statuses are unchanged."
    worksheet.oddFooter.right.text = "Page &P of &N"
    for cell in worksheet[1]:
        cell.font = Font(bold=True, color="FFFFFF", name="Calibri", size=11)
        cell.fill = PatternFill("solid", fgColor=PURPLE)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    header_index = {cell.value: cell.column for cell in worksheet[1]}
    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name="Calibri", size=10, color="1F2937")
            cell.alignment = Alignment(vertical="center")
            cell.border = THIN
            header = worksheet.cell(1, cell.column).value or ""
            if header in {"Date", "Enrollment Date", "OJT Start Date", "Attendance Date"} and cell.value:
                cell.number_format = "YYYY-MM-DD"
            if header in {"In Time", "Out Time"} and cell.value:
                cell.number_format = "HH:MM:SS"
            if header in {"Attendance %", "Verified Attendance %", "Coverage %", "Qualifying Hours"} and isinstance(cell.value, (int, float)):
                cell.number_format = "0.0"
            if color_status and header not in {"Candidate Name", "Candidate Id"} and cell.value in STATUS_FILLS:
                cell.fill = PatternFill("solid", fgColor=STATUS_FILLS[str(cell.value)])
                cell.alignment = Alignment(horizontal="center", vertical="center")
            if header == "Calculated Status" and cell.value in STATUS_FILLS:
                cell.fill = PatternFill("solid", fgColor=STATUS_FILLS[str(cell.value)])
    for column in worksheet.columns:
        letter = get_column_letter(column[0].column)
        length = max(len(str(cell.value)) if cell.value is not None else 0 for cell in column)
        worksheet.column_dimensions[letter].width = min(42, max(12, length + 2))
    if header_index:
        worksheet.auto_filter.ref = worksheet.dimensions
    worksheet.sheet_view.showGridLines = False
    worksheet.oddHeader.center.text = ""
    # A thin gold row is too invasive for data; the purple header carries the identity.
    _ = GOLD


def status_label(value: str | None) -> str:
    if not value:
        return ""
    return STATUS_LABELS.get(value, value)


def official_label(value: str | None) -> str:
    if not value:
        return ""
    return OFFICIAL_LABELS.get(value, value)


def hours_cell(minutes) -> float | None:
    return hours_decimal(minutes) if minutes is not None else None
