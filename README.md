# KKERS DDU-GKY attendance monitor

Local dashboard for batch **BT26D023667** at **KKERS-ONGOLE**. It reads the official Kaushal candidate attendance report after an administrator signs in, stores the rows in SQLite, and calculates qualifying hours from the check-in and check-out times.

The app does not submit, edit, or delete attendance on the portal. It does not store portal passwords, and it does not bypass CAPTCHA or MFA.

## What you need

- Python 3.11
- Node.js 18 or newer
- Google Chrome is not required. The sync installs its own Chromium through Playwright.

The master candidate file is already at `backend/data/Candidate_Details.xlsx` (35 candidates).

## Install and start

From this folder:

```bash
chmod +x start.sh
./start.sh
```

The first run creates `backend/.venv`, installs Python packages, installs Chromium, and installs the frontend packages.

Then open [http://127.0.0.1:5173](http://127.0.0.1:5173).

The API is at [http://127.0.0.1:8000](http://127.0.0.1:8000). API docs are at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

To start the two processes yourself:

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```bash
cd frontend
npm install
npm run dev
```

## Sync from the portal

1. Open **Sync**.
2. Choose new or changed dates, a date range, or a full historical resync.
3. Click **SYNC ATTENDANCE FROM KAUSHAL PORTAL**.
4. In the Chromium window, sign in with the authorized account and complete any CAPTCHA or MFA.
5. Leave the window open. The sync continues when the report is available.

The sync opens:

`https://kaushal.dord.gov.in/#/landing-page/candidate-attendance-report`

It selects Andhra Pradesh, KK Educational and Rural Development Society, sanction order SO26D021933, training center KKERS-ONGOLE, and batch BT26D023667. Those values can be changed under Settings. After Submit, it reads every date row the portal actually shows, including later pages, then opens Present, Half Day, Absent, RF Attendance, and OJT Attendance where those counts are available.

Dates the portal does not list are not created. A missing check-out is not filled in. A candidate is not marked present from the summary count alone.

The browser profile stays in `backend/data/browser-profile`. That folder holds the signed-in session. Do not share it.

## Attendance rules

Training is 9:00–18:00 with lunch 13:00–14:00, so a full schedule is 8 qualifying hours. These thresholds are local dashboard rules, not an official DDU-GKY policy:

- 6 hours or more: Full day
- 3 hours up to, but not including, 6: Half day
- Under 3 hours: Insufficient hours, counted as absent in the percentage
- Check-in without check-out: Absent
- No portal row: Not yet verified

Overlapping punches are merged before lunch is removed, so the same minutes are not counted twice. Only the part of a session that falls inside lunch is deducted.

Official portal status and the calculated status are both stored. The matrix shows the calculated status.

Verified attendance percentage:

`(full days + 0.5 × half days) / verified eligible days × 100`

Eligible days are portal dates on or after enrollment, on or before today, excluding Sundays. Future dates, incomplete records, and dates that were not verified are not treated as absences. Coverage shows what share of eligible portal dates has a verified result.

## Exports and reset

The dashboard downloads daily, monthly, candidate-wise, matrix, and low-attendance workbooks. Headers are frozen and filtered.

Reset local attendance asks for the phrase `RESET LOCAL ATTENDANCE`. It clears local punches, dates, sync history, and reconciliation notes. It does not remove the master candidates and it does not touch the portal.

## Tests

```bash
cd backend
source .venv/bin/activate
pytest
```

## Data

SQLite database: `backend/data/attendance.db`

Sync log: `backend/data/sync.log`

Screenshots taken when a portal step fails: `backend/data/sync-screenshots/`
