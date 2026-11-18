"""Read-only browser session for the Kaushal candidate attendance report.

The administrator signs in manually. This module never types credentials, never
solves CAPTCHA or MFA, and never creates or edits official attendance. The only
form control it submits is the report Submit button (batch filters or Candidate ID).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

from .calculator import parse_date
from .config import (
    BROWSER_PROFILE,
    PORTAL_CANDIDATE_DETAILS_URL,
    PORTAL_LOGIN_URL,
    PORTAL_REPORT_URL,
    SCREENSHOT_DIR,
    browser_profile_dir,
)
from .portal_parse import (
    detail_key,
    norm_header,
    parse_candidate_punch_excel,
    parse_candidate_punch_table,
    parse_detail_table,
    parse_summary_table,
)

logger = logging.getLogger("attendance.portal")

CATEGORY_COLUMNS = {
    "present": ("Present",),
    "half_day": ("Half Day", "Halfday"),
    "absent": ("Absent",),
    "rf": ("RF Attendance", "RF"),
    "ojt": ("OJT Attendance", "OJT"),
}

FILTERS = (
    (("State",), "state"),
    (("PIA", "Project Implementing Agency"), "pia"),
    (("Sanction Order", "Sanction"), "sanction_order"),
    (("Training Center", "Training Centre", "TC Name"), "training_center"),
    (("Batch", "Batch Code"), "batch_code"),
)

NEXT_SELECTORS = (
    "button[aria-label='Next page']",
    "button[aria-label='Next Page']",
    "button[aria-label='Next']",
    ".mat-mdc-paginator-navigation-next",
    ".mat-paginator-navigation-next",
    "li.pagination-next a",
    "a[aria-label='Next']",
    "button.pagination-next",
)

EXTRACT_TABLES_JS = """
() => {
  const norm = (value) => (value || "").replace(/\\s+/g, " ").trim();
  const tables = [];
  for (const table of document.querySelectorAll("table")) {
    let headerCells = Array.from(table.querySelectorAll("thead th, thead td"));
    if (!headerCells.length) {
      const first = table.querySelector("tr");
      if (first && first.querySelector("th")) headerCells = Array.from(first.querySelectorAll("th"));
    }
    const headers = headerCells.map((cell) => norm(cell.innerText));
    const bodyRows = Array.from(table.querySelectorAll("tbody tr"));
    const source = bodyRows.length ? bodyRows : Array.from(table.querySelectorAll("tr")).slice(headers.length ? 1 : 0);
    const rows = [];
    for (const tr of source) {
      if (tr.querySelector("th") && !tr.querySelector("td")) continue;
      const cells = Array.from(tr.querySelectorAll("td")).map((td) => norm(td.innerText));
      if (cells.some(Boolean)) rows.push(cells);
    }
    if (headers.length && rows.length) tables.push({ headers, rows });
  }
  return tables;
}
"""

LOADER_JS = """
() => {
  const selectors = [".loader", ".spinner", "mat-progress-spinner", ".ngx-spinner-overlay", ".loading", ".block-ui-wrapper"];
  return selectors.some((selector) => {
    const node = document.querySelector(selector);
    if (!node) return false;
    const style = getComputedStyle(node);
    return style.display !== "none" && style.visibility !== "hidden" && node.getClientRects().length > 0;
  });
}
"""

SCROLL_JS = """
() => {
  const table = document.querySelector("table");
  let node = table ? table.parentElement : null;
  while (node && node !== document.body) {
    const style = getComputedStyle(node);
    if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 20) {
      const before = node.scrollTop;
      node.scrollTop = Math.min(node.scrollTop + 1200, node.scrollHeight);
      return { scrolled: node.scrollTop !== before };
    }
    node = node.parentElement;
  }
  const before = window.scrollY;
  window.scrollBy(0, 1400);
  return { scrolled: window.scrollY !== before };
}
"""

AUTH_JS = """
() => {
  const password = Array.from(document.querySelectorAll("input[type='password']")).some((node) => {
    const style = getComputedStyle(node);
    return style.display !== "none" && style.visibility !== "hidden" && node.getClientRects().length > 0;
  });
  const text = document.body ? document.body.innerText.slice(0, 4000) : "";
  return { passwordVisible: password, href: location.href, text };
}
"""


class PortalError(Exception):
    pass


class SyncCancelled(Exception):
    pass


class PortalClient:
    def __init__(self, manager):
        self.manager = manager
        self.playwright = None
        self.context = None
        self.page = None
        self.batch = {}
        self.sync_id = None
        self._cdp_browser = None
        self._remote_mode = False

    def alive(self) -> bool:
        try:
            return self.page is not None and not self.page.is_closed()
        except Exception:
            return False

    def open_browser(self, force: bool = False) -> None:
        """Online: attach to the live server Chrome. Local: user Chrome CDP or headed Playwright."""
        from .config import remote_display_enabled

        if remote_display_enabled():
            self._open_remote_cdp(force=force)
            return
        if self.alive() and not force:
            return
        if self._try_connect_user_chrome(force=force):
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PortalError("Playwright is not installed. Run `python -m playwright install chromium`.") from exc
        self._discard_browser()
        profile = browser_profile_dir()
        profile.mkdir(parents=True, exist_ok=True)
        self._release_profile_lock()
        self.playwright = sync_playwright().start()
        try:
            headless = os.environ.get("ATTENDANCE_HEADLESS", "").strip().lower() in {"1", "true", "yes"}
            launch_args = [
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--window-size=1440,900",
                "--window-position=0,0",
            ]
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=headless,
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1440, "height": 900},
                accept_downloads=True,
                args=launch_args,
            )
        except Exception as exc:
            self._discard_browser()
            raise PortalError(
                "Could not open a browser. Close leftover Chromium, or run scripts/open-kaushal-chrome.sh, then try again."
            ) from exc
        self._remote_mode = False
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.main_page = self.page
        self._detail_popup = None
        self.page.set_default_timeout(20000)
        self.page.set_default_navigation_timeout(60000)
        try:
            self.page.bring_to_front()
        except Exception:
            logger.info("Could not bring the portal window to the front")

    def _open_remote_cdp(self, force: bool = False) -> None:
        """Attach to the online live Chromium after the admin has logged in."""
        from .live_browser import cdp_endpoint, cdp_ready, enable_debugging

        if self.alive() and not force:
            return
        if not cdp_ready(timeout=1.0):
            enable_debugging(PORTAL_LOGIN_URL)
        self._disconnect_playwright()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PortalError("Playwright is not installed.") from exc
        try:
            self.playwright = sync_playwright().start()
            self._cdp_browser = self.playwright.chromium.connect_over_cdp(cdp_endpoint())
            contexts = self._cdp_browser.contexts
            self.context = contexts[0] if contexts else self._cdp_browser.new_context(
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                accept_downloads=True,
            )
            pages = [p for p in self.context.pages if not p.is_closed()]
            self.page = pages[0] if pages else self.context.new_page()
            self.main_page = self.page
            self._detail_popup = None
            self._remote_mode = True
            self.page.set_default_timeout(20000)
            self.page.set_default_navigation_timeout(60000)
        except Exception as exc:
            self._disconnect_playwright()
            raise PortalError(
                "Could not attach to the live Kaushal window. Click Reload Kaushal login, then try again."
            ) from exc

    def _try_connect_user_chrome(self, force: bool = False) -> bool:
        """Read Kaushal from the user’s normal Chrome (remote debugging port)."""
        from urllib.request import urlopen

        from .config import user_chrome_cdp_url

        endpoint = user_chrome_cdp_url()
        try:
            with urlopen(f"{endpoint}/json/version", timeout=1.0) as response:
                if response.status != 200:
                    return False
        except Exception:
            return False
        if self.alive() and not force:
            return True
        self._disconnect_playwright()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False
        try:
            self.playwright = sync_playwright().start()
            self._cdp_browser = self.playwright.chromium.connect_over_cdp(endpoint)
            contexts = self._cdp_browser.contexts
            self.context = contexts[0] if contexts else self._cdp_browser.new_context(
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                accept_downloads=True,
            )
            pages = [p for p in self.context.pages if not p.is_closed()]
            self.page = pages[0] if pages else self.context.new_page()
            self.main_page = self.page
            self._detail_popup = None
            self._remote_mode = True  # do not kill the user’s Chrome on disconnect
            self.page.set_default_timeout(20000)
            self.page.set_default_navigation_timeout(60000)
            logger.info("Attached to user Chrome at %s", endpoint)
            return True
        except Exception as exc:
            self._disconnect_playwright()
            logger.info("Could not attach to user Chrome: %s", exc)
            return False

    def _disconnect_playwright(self) -> None:
        """Detach automation only — keep the live Chromium running for login."""
        was_remote = self._remote_mode
        context = self.context
        playwright = self.playwright
        cdp_browser = self._cdp_browser
        self.context = None
        self.page = None
        self.main_page = None
        self.playwright = None
        self._cdp_browser = None
        self._detail_popup = None
        self._remote_mode = False
        if not was_remote and context is not None:
            try:
                context.close()
            except Exception:
                logger.info("Previous browser context was already closed")
        if cdp_browser is not None:
            try:
                cdp_browser.close()
            except Exception:
                logger.info("CDP browser detach finished")
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                logger.info("Previous Playwright driver was already stopped")

    def _discard_browser(self) -> None:
        from .config import remote_display_enabled

        if remote_display_enabled() or self._remote_mode:
            # Never kill the live Chromium used for online login.
            self._disconnect_playwright()
            return
        self._disconnect_playwright()

    def _release_profile_lock(self) -> None:
        import os
        import signal
        import subprocess

        profile = browser_profile_dir()
        profile_text = str(profile)
        try:
            listed = subprocess.run(["pgrep", "-fl", profile_text], capture_output=True, text=True, check=False)
        except Exception:
            listed = None
        killed = False
        if listed and listed.stdout:
            for line in listed.stdout.splitlines():
                pid_text, _, command = line.partition(" ")
                if "chrom" not in command.lower():
                    continue
                try:
                    pid = int(pid_text)
                except ValueError:
                    continue
                if pid == os.getpid():
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed = True
                except OSError:
                    continue
        if killed:
            time.sleep(0.6)
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (profile / name).unlink(missing_ok=True)
            except OSError:
                logger.info("Could not remove the browser profile lock %s", name)

    def prepare_report(self, batch: dict, sync_id: int) -> list[str]:
        """Open Candidate ID report — online live window or local Chrome."""
        from .config import remote_display_enabled
        from .live_browser import enable_debugging, ensure_login_browser

        self.batch = batch
        self.sync_id = sync_id
        if remote_display_enabled():
            self.manager.update(
                sync_id,
                progress_step="connecting",
                message="Opening live Kaushal window — sign in below on this site",
            )
            ensure_login_browser(PORTAL_LOGIN_URL, fresh=True)
            self._wait_for_remote_login_click()
            self.manager.update(sync_id, message="Connecting to your logged-in session")
            enable_debugging(PORTAL_LOGIN_URL)
            self.open_browser()
            if self._login_form_visible() or not self._authenticated():
                self._disconnect_playwright()
                ensure_login_browser(PORTAL_LOGIN_URL, fresh=True)
                self.manager.update(
                    sync_id,
                    status="awaiting_login",
                    progress_step="awaiting_login",
                    message="Finish login in the live window, then click “I have logged in” again.",
                )
                self._wait_for_remote_login_click()
                enable_debugging(PORTAL_LOGIN_URL)
                self.open_browser()
                if self._login_form_visible() or not self._authenticated():
                    raise PortalError(
                        "Kaushal login was not detected. Sign in fully in the live window, then click “I have logged in”."
                    )
        else:
            self.manager.update(sync_id, progress_step="connecting", message="Connecting to Chrome")
            self.open_browser()
            if not self._on_candidate_details():
                self._goto(PORTAL_CANDIDATE_DETAILS_URL)
                self._wait_for_app()
            if self._login_form_visible() or not self._authenticated():
                if not self._login_form_visible():
                    self._goto(PORTAL_LOGIN_URL)
                    self._wait_for_app()
                self._wait_for_manual_login()
        if not self._on_candidate_details():
            self._goto(PORTAL_CANDIDATE_DETAILS_URL)
            self._wait_for_app()
        if not self._on_candidate_details():
            self._open_candidate_details_menu()
            self._wait_for_app()
        if not self._candidate_id_form_visible():
            self._goto(PORTAL_CANDIDATE_DETAILS_URL)
            self._wait_for_app()
        if not self._candidate_id_form_visible():
            self._shot("candidate-form-missing")
            raise PortalError(
                "The Candidate ID attendance report form was not found. Open REPORT → CANDIDATE ATTENDANCE REPORT in the live window."
            )
        self.manager.update(
            sync_id,
            progress_step="loading_report",
            message="Candidate ID report ready — reading each candidate’s punch history",
        )
        return []

    def _wait_for_remote_login_click(self) -> None:
        self.manager.update(
            self.sync_id,
            status="awaiting_login",
            progress_step="awaiting_login",
            message="In the live window on this page: sign into Kaushal, then click “I have logged in”.",
        )
        deadline = time.time() + 20 * 60
        while time.time() < deadline:
            self._check_cancel()
            if self.manager.login_event.is_set():
                self.manager.login_event.clear()
                self.manager.update(self.sync_id, status="running", message="Connecting to your logged-in session")
                return
            time.sleep(0.4)
        raise PortalError("Login was not completed within 20 minutes. No attendance was changed on the portal.")

    def fetch_candidate_punches(self, candidate_id: str) -> tuple[list[dict], list[str]]:
        """Submit one KP Id and return every punch row for that person."""
        self._guard_candidate_session()
        cid = (candidate_id or "").strip()
        if not cid:
            return [], ["Skipped a candidate with a blank KP Id."]
        self._fill_candidate_id(cid)
        self._click_submit()
        self._wait_until_settled()
        # Prefer Excel when the portal offers it — one file has the full history.
        excel_rows, excel_warnings = self._try_download_candidate_excel(cid)
        if excel_rows is not None:
            return excel_rows, excel_warnings
        if not self._wait_for_candidate_table(timeout=30):
            return [], [f"No punch rows returned for KP Id {cid}."]
        return self._collect_candidate_punches()

    def _wait_for_candidate_table(self, timeout: int) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._check_cancel()
            if not self._loader_visible():
                table = self._read_table("candidate_punches")
                if table and table["rows"]:
                    return True
                # Empty result page (no punches) — stop waiting once the form is idle.
                try:
                    body = (self.page.inner_text("body") or "").lower()
                except Exception:
                    body = ""
                if any(token in body for token in ("no record", "no data", "0 of 0", "showing 0")):
                    return False
            self.page.wait_for_timeout(120)
        table = self._read_table("candidate_punches")
        return bool(table and table["rows"])

    def discover_dates(self) -> tuple[list[dict], list[str]]:
        """Legacy batch summary path (kept for reference / emergency fallback)."""
        self.manager.update(self.sync_id, progress_step="discovering_dates", message="Discovering Dates")
        return self._collect("summary")

    def extract_date(self, summary: dict) -> tuple[list[dict], set[str], set[str], list[str]]:
        """Legacy per-date Present/Half/Absent drill-down (unused by the fast sync)."""
        self._guard_session()
        self._ensure_summary()
        details: list[dict] = []
        opened: set[str] = set()
        failed: set[str] = set()
        warnings: list[str] = []
        for category, titles in CATEGORY_COLUMNS.items():
            count = summary.get(category)
            if count in (None, 0):
                if count == 0:
                    opened.add(category)
                continue
            try:
                rows, row_warnings = self._read_category(summary, titles, category, count)
                details.extend(rows)
                warnings.extend(row_warnings)
                opened.add(category)
            except PortalError as exc:
                failed.add(category)
                warnings.append(str(exc))
                logger.warning("Category %s failed for %s: %s", category, summary.get("date"), exc)
            finally:
                self._ensure_summary()
        return details, opened, failed, warnings

    def _on_candidate_details(self) -> bool:
        if self._candidate_id_form_visible():
            return True
        try:
            href = (self.page.evaluate("() => location.href") or "").lower()
        except Exception:
            href = ""
        if "candidate-attendance-details" in href:
            return True
        try:
            text = (self.page.inner_text("body") or "")[:8000].lower()
        except Exception:
            return False
        return (
            "enter candidate id" in text
            or "enter kp id" in text
            or (("candidate id" in text or "kp id" in text) and "attendance id" in text and "or" in text)
        )

    def _candidate_id_form_visible(self) -> bool:
        kp_pattern = re.compile(r"(kp\s*id|kaushal\s*panjee\s*id|candidate\s*id)", re.I)
        try:
            if self.page.get_by_placeholder(kp_pattern).count():
                return True
            if self.page.get_by_label(kp_pattern).count():
                return True
        except Exception:
            pass
        try:
            text = norm_header(self.page.inner_text("body")[:4000])
        except Exception:
            return False
        return ("candidate id" in text or "kp id" in text) and "attendance id" in text

    def _open_candidate_details_menu(self) -> None:
        for pattern in (r"DDUGKY\s*REPORT", r"DDU-?\s*GKY\s*REPORT", r"^\s*REPORT\s*$"):
            if self._click_text(pattern):
                self.page.wait_for_timeout(500)
                break
        self._click_text(r"CANDIDATE\s+ATTENDANCE\s+REPORT")
        self._wait_for_app()

    def _clear_attendance_id_fields(self) -> None:
        """Leave Attendance Id blank so the portal OR form uses KP Id only."""
        patterns = (
            re.compile(r"attendance\s*id", re.I),
            re.compile(r"^\s*Attendance\s+ID\s*$", re.I),
        )
        for pattern in patterns:
            try:
                for locator in (
                    self.page.get_by_placeholder(pattern),
                    self.page.get_by_label(pattern),
                ):
                    if locator.count():
                        locator.first.fill("")
            except Exception:
                pass
        try:
            label = self.page.get_by_text(re.compile(r"^\s*Attendance\s+ID\s*$", re.I))
            if label.count():
                nearby = label.first.locator(
                    "xpath=following::input[not(@type='hidden')][1] | ../following::input[not(@type='hidden')][1]"
                )
                if nearby.count():
                    nearby.first.fill("")
        except Exception:
            pass

    def _fill_candidate_id(self, candidate_id: str) -> None:
        """Type KP Id into Candidate/KP Id only — never into Attendance Id."""
        kp_pattern = re.compile(r"(kp\s*id|kaushal\s*panjee\s*id|candidate\s*id)", re.I)
        field = None
        for locator in (
            self.page.get_by_placeholder(kp_pattern),
            self.page.get_by_label(kp_pattern),
        ):
            try:
                if locator.count() == 0:
                    continue
                candidate = locator.first
                if candidate.is_visible():
                    field = candidate
                    break
            except Exception:
                continue
        if field is None:
            label = self.page.get_by_text(re.compile(r"^\s*(KP\s*Id|Kaushal\s*Panjee\s*Id|Candidate\s+ID)\s*$", re.I))
            if label.count():
                nearby = label.first.locator(
                    "xpath=following::input[not(@type='hidden')][1] | ../following::input[not(@type='hidden')][1]"
                )
                if nearby.count():
                    field = nearby.first
        if field is None:
            raise PortalError("The KP Id / Candidate ID input was not found. Attendance Id is not used.")
        # Clear Attendance Id first so an OR search cannot use it.
        self._clear_attendance_id_fields()
        field.click()
        field.fill("")
        field.fill(candidate_id)
        self._clear_attendance_id_fields()

    def _try_download_candidate_excel(self, candidate_id: str) -> tuple[Optional[list[dict]], list[str]]:
        button = self.page.get_by_role("button", name=re.compile(r"download\s*excel", re.I))
        if button.count() == 0:
            button = self.page.locator("button, a").filter(has_text=re.compile(r"download\s*excel", re.I))
        if button.count() == 0:
            return None, []
        try:
            with self.page.expect_download(timeout=12000) as download_info:
                button.first.click()
            download = download_info.value
            suffix = Path(download.suggested_filename or "punches.xlsx").suffix or ".xlsx"
            handle, temp_path = tempfile.mkstemp(prefix=f"kk-{candidate_id}-", suffix=suffix)
            os.close(handle)
            download.save_as(temp_path)
            try:
                rows, warnings = parse_candidate_punch_excel(temp_path)
            finally:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            if not rows:
                return None, warnings
            for row in rows:
                if not row.get("candidate_id"):
                    row["candidate_id"] = candidate_id
            return rows, warnings
        except Exception as exc:
            logger.info("Excel download skipped for %s: %s", candidate_id, exc)
            return None, []

    def _collect_candidate_punches(self) -> tuple[list[dict], list[str]]:
        found: dict = {}
        warnings: list[str] = []
        seen_signatures: set[str] = set()
        scrolls_without_growth = 0
        for _ in range(120):
            self._check_cancel()
            self._wait_until_settled()
            table = self._read_table("candidate_punches")
            if table is None:
                break
            signature = f"{table['headers']}|{len(table['rows'])}|{table['rows'][:1]}|{table['rows'][-1:]}"
            parsed, parse_warnings = parse_candidate_punch_table(table["headers"], table["rows"])
            warnings.extend(parse_warnings)
            before = len(found)
            for item in parsed:
                found[detail_key(item)] = item
            grew = len(found) > before
            if self._scroll_more():
                if grew:
                    scrolls_without_growth = 0
                else:
                    scrolls_without_growth += 1
                if scrolls_without_growth < 1:
                    continue
            scrolls_without_growth = 0
            if signature in seen_signatures:
                break
            seen_signatures.add(signature)
            if not self._click_next():
                break
            self.page.wait_for_timeout(60)
        return list(found.values()), warnings

    def _guard_candidate_session(self) -> None:
        if self._authenticated() and self._candidate_id_form_visible():
            return
        if not self._authenticated():
            self._wait_for_manual_login()
        if not self._candidate_id_form_visible():
            self._goto(PORTAL_CANDIDATE_DETAILS_URL)
            self._wait_for_app()
        if not self._candidate_id_form_visible():
            raise PortalError("Lost the Candidate ID report page. Open it again and restart sync.")

    def _wait_for_manual_login(self) -> None:
        from .config import remote_display_enabled

        self.manager.update(
            self.sync_id,
            status="awaiting_login",
            progress_step="awaiting_login",
            message=(
                "In the live window on this page: sign into Kaushal, then click “I have logged in”."
                if remote_display_enabled()
                else "In Google Chrome: sign into Kaushal, then click “I have logged in” here."
            ),
        )
        deadline = time.time() + 20 * 60
        while time.time() < deadline:
            self._check_cancel()
            if self.manager.login_event.is_set():
                self.manager.login_event.clear()
            if self._authenticated():
                self.manager.update(self.sync_id, status="running", message="Loading Attendance Report")
                return
            self.page.wait_for_timeout(700)
        raise PortalError("Login was not completed within 20 minutes. No attendance was changed on the portal.")

    def _wait_for_submitted_table(self) -> None:
        """The administrator fills the filters and clicks Submit. Reading starts only then."""
        self.manager.update(
            self.sync_id,
            status="running",
            progress_step="loading_report",
            message="You are signed in. Open the attendance report for the batch on screen and click Submit. Reading starts when the date table is visible.",
        )
        deadline = time.time() + 20 * 60
        while time.time() < deadline:
            self._check_cancel()
            if self._login_form_visible():
                self._wait_for_manual_login()
                continue
            if self._summary_ready():
                self.page.wait_for_timeout(250)
                if self._summary_ready():
                    return
            self.page.wait_for_timeout(400)
        self._shot("report-not-found")
        raise PortalError(
            "The attendance date table did not appear. Fill the filters, click Submit, leave that table on screen, and start the sync again."
        )

    def _authenticated(self) -> bool:
        try:
            info = self.page.evaluate(AUTH_JS)
        except Exception:
            return False
        if info.get("passwordVisible"):
            return False
        text = (info.get("text") or "").lower()
        if "login to your account" in text or "enter captcha" in text:
            return False
        if any(token in text for token in ("logout", "sign out", "candidate attendance", "sanction order", "training center")):
            return True
        href = (info.get("href") or "").lower()
        if any(token in href for token in ("login", "signin", "sign-in")):
            return False
        return False

    def _on_attendance_report(self) -> bool:
        if self._summary_ready():
            return True
        try:
            text = (self.page.inner_text("body") or "")[:8000].lower()
        except Exception:
            return False
        return "candidate attendance report" in text

    def _filters_visible(self) -> bool:
        try:
            text = norm_header(self.page.inner_text("body")[:5000])
        except Exception:
            return False
        return "state" in text and ("batch" in text or "sanction" in text)

    def _login_form_visible(self) -> bool:
        try:
            info = self.page.evaluate(AUTH_JS)
        except Exception:
            return False
        if info.get("passwordVisible"):
            return True
        text = (info.get("text") or "").lower()
        return "login to your account" in text or "enter captcha" in text

    def _summary_ready(self) -> bool:
        table = self._read_table("summary")
        return bool(table and table.get("rows"))

    def _open_report_menu(self) -> None:
        # The public site also has a "DDU-GKY" item. Only the in-app report labels count.
        for pattern in (r"DDUGKY\s*REPORT", r"DDU-?\s*GKY\s*REPORT"):
            if self._click_text(pattern):
                self.page.wait_for_timeout(600)
                break
        self._click_text(r"CANDIDATE\s+ATTENDANCE\s+REPORT")
        self._wait_for_app()

    def _apply_filters(self) -> list[str]:
        warnings = []
        for labels, key in FILTERS:
            value = (self.batch.get(key) or "").strip()
            if not value:
                raise PortalError(f"The {labels[0]} filter has no configured value.")
            self._select_filter(labels, value)
            try:
                self.page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                self.page.wait_for_timeout(800)
        return warnings

    def _select_filter(self, labels: tuple[str, ...], value: str) -> None:
        last_error = "The filter did not show the expected value."
        for attempt in range(2):
            self._check_cancel()
            try:
                self._select_once(labels, value)
                if self._selection_visible(labels, value):
                    return
                last_error = f"{labels[0]} did not stay selected as {value}."
            except PortalError as exc:
                last_error = str(exc)
                if "dropdown was not found" in last_error:
                    raise PortalError(f"Could not select {labels[0]} '{value}'. {last_error}") from exc
            except Exception as exc:
                last_error = str(exc)
            self.page.keyboard.press("Escape")
            if attempt == 0:
                self.page.wait_for_timeout(250)
        self._shot("filter-failed")
        raise PortalError(f"Could not select {labels[0]} '{value}'. {last_error}")

    def _select_once(self, labels: tuple[str, ...], value: str) -> None:
        scope = self.page
        for label in labels:
            exact = re.compile(rf"^\s*{re.escape(label)}\s*\*?\s*:?\s*$", re.I)
            nodes = scope.get_by_text(exact)
            if nodes.count() == 0:
                continue
            node = nodes.first
            combo = node.locator(
                "xpath=following::*[self::mat-select or self::select or self::ng-select or self::input or @role='combobox'][1]"
            )
            if combo.count() == 0:
                continue
            tag = combo.evaluate("element => element.tagName.toLowerCase()")
            role = (combo.get_attribute("role") or "").lower()
            if tag == "select":
                combo.select_option(label=value)
                return
            combo.click()
            if tag == "input" or role == "combobox":
                try:
                    combo.fill(value)
                except Exception:
                    self.page.keyboard.type(value, delay=25)
            else:
                self.page.keyboard.type(value, delay=25)
            self._click_option(value)
            return
        raise PortalError(f"The {labels[0]} dropdown was not found on the report.")

    def _click_option(self, value: str) -> None:
        pattern = re.compile(re.escape(value), re.I)
        option = self.page.get_by_role("option", name=pattern)
        if option.count() == 0:
            option = self.page.locator("mat-option, .ng-option, [role='option'], li").filter(has_text=pattern)
        if option.count() == 0:
            raise PortalError(f"The option '{value}' was not in the open list.")
        option.first.click()
        self.page.wait_for_timeout(400)

    def _selection_visible(self, labels: tuple[str, ...], value: str) -> bool:
        self.page.keyboard.press("Escape")
        try:
            text = self.page.inner_text("body")
        except Exception:
            return False
        folded = re.sub(r"\s+", " ", text).lower()
        return value.lower() in folded

    def _click_submit(self) -> None:
        button = self.page.get_by_role("button", name=re.compile(r"^\s*submit\s*$", re.I))
        if button.count() == 0:
            button = self.page.locator("button, input[type='submit']").filter(has_text=re.compile(r"^\s*submit\s*$", re.I))
        if button.count() == 0:
            raise PortalError("The report Submit button was not found.")
        button.first.click()

    def _verify_batch_banner(self) -> list[str]:
        notes = []
        try:
            text = re.sub(r"\s+", " ", self.page.inner_text("body"))
        except Exception:
            text = ""
        batch_code = (self.batch.get("batch_code") or "").lower()
        if batch_code and batch_code not in text.lower():
            notes.append(
                f"Batch {self.batch.get('batch_code')} was not printed on the report page. Rows were read from the date table left on screen after Submit."
            )
        for key, label in (("batch_start_date", "start"), ("expected_end_date", "end")):
            parsed = parse_date(self.batch.get(key))
            if parsed is None:
                continue
            shown = parsed.strftime("%d/%m/%Y")
            if shown not in text and parsed.isoformat() not in text:
                notes.append(f"The configured batch {label} date {shown} was not visible on the report. Rows were still read from the table that was displayed.")
        return notes

    def _collect(self, kind: str, category: Optional[str] = None, expected: Optional[int] = None) -> tuple[list[dict], list[str]]:
        found: dict = {}
        warnings: list[str] = []
        seen_signatures: set[str] = set()
        scrolls_without_growth = 0
        for _ in range(240):
            self._check_cancel()
            self._wait_until_settled()
            table = self._read_table(kind)
            if table is None:
                break
            signature = f"{table['headers']}|{len(table['rows'])}|{table['rows'][:1]}|{table['rows'][-1:]}"
            if kind == "summary":
                parsed, parse_warnings = parse_summary_table(table["headers"], table["rows"])
            else:
                parsed, parse_warnings = parse_detail_table(table["headers"], table["rows"], category or "present")
            warnings.extend(parse_warnings)
            before = len(found)
            for item in parsed:
                key = item["date"] if kind == "summary" else detail_key(item)
                found[key] = item
            if expected and len(found) >= expected:
                break
            grew = len(found) > before
            if self._scroll_more():
                if grew:
                    scrolls_without_growth = 0
                else:
                    scrolls_without_growth += 1
                if scrolls_without_growth < 1:
                    continue
            scrolls_without_growth = 0
            if signature in seen_signatures:
                break
            seen_signatures.add(signature)
            if not self._click_next():
                break
            self.page.wait_for_timeout(60)
        return list(found.values()), warnings

    def _read_category(self, summary: dict, titles: tuple[str, ...], category: str, expected: Optional[int] = None) -> tuple[list[dict], list[str]]:
        self.manager.update(
            self.sync_id,
            progress_step="extracting_records",
            message="Extracting Candidate Records",
        )
        opened = self._click_summary_count(summary.get("portal_date") or summary["date"], titles)
        if not opened:
            raise PortalError(
                f"{summary['date']} {category.replace('_', ' ')} count is {summary.get(category)}, but the number was not clickable."
            )
        self._wait_for_table("detail", timeout=60)
        try:
            expected_count = int(expected) if expected is not None else None
        except (TypeError, ValueError):
            expected_count = None
        rows, warnings = self._collect("detail", category, expected=expected_count)
        for row in rows:
            row_day = row.get("attendance_date")
            if row_day and row_day != summary["date"]:
                warnings.append(
                    f"{summary['date']} {category} row is dated {row_day} on the portal. It was kept with the date list that was opened."
                )
        self._return_from_detail()
        return rows, warnings

    def _click_summary_count(self, portal_date: str, titles: tuple[str, ...]) -> bool:
        frame, table = self._summary_table()
        if table is None:
            raise PortalError("The attendance summary table is not on screen.")
        headers = [norm_header(value) for value in table.locator("thead th, thead td").all_inner_texts()]
        if not headers:
            first = table.locator("tr").first
            headers = [norm_header(value) for value in first.locator("th").all_inner_texts()]
        date_index = _index(headers, ("date",))
        column_index = _index(headers, tuple(norm_header(title) for title in titles))
        if date_index is None or column_index is None:
            raise PortalError("The summary table columns could not be matched to Date and the attendance count.")
        rows = table.locator("tbody tr")
        target_row = None
        for index in range(rows.count()):
            cells = rows.nth(index).locator("td")
            if cells.count() <= max(date_index, column_index):
                continue
            cell_date = re.sub(r"\s+", " ", cells.nth(date_index).inner_text()).strip()
            if cell_date == portal_date or _same_day(cell_date, portal_date):
                target_row = rows.nth(index)
                break
        if target_row is None:
            raise PortalError(f"The summary row for {portal_date} was not visible. Pagination may have moved.")
        cell = target_row.locator("td").nth(column_index)
        clickable = cell.locator("a, button, [role='link'], [role='button']")
        target = clickable.first if clickable.count() else cell
        self._detail_popup = None
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeout

            with self.context.expect_page(timeout=350) as caught:
                target.click()
            popup = caught.value
            popup.set_default_timeout(20000)
            self._detail_popup = popup
            self.page = popup
        except PlaywrightTimeout:
            self.page = self.main_page
        except Exception as exc:
            raise PortalError(f"The attendance count for {portal_date} could not be opened. {exc}") from exc
        return True

    def _return_from_detail(self) -> None:
        popup = self._detail_popup
        self._detail_popup = None
        if popup is not None:
            try:
                popup.close()
            except Exception:
                logger.exception("Could not close the detail tab")
            if self.main_page and not self.main_page.is_closed():
                self.page = self.main_page
            else:
                self.page = next((page for page in self.context.pages if not page.is_closed()), self.page)
                self.main_page = self.page
            return
        self.page = self.main_page if self.main_page and not self.main_page.is_closed() else self.page
        for name in ("Back", "Close"):
            button = self.page.get_by_role("button", name=re.compile(rf"^\s*{name}\s*$", re.I))
            if button.count() and button.first.is_visible():
                button.first.click()
                self.page.wait_for_timeout(40)
                if self._summary_table()[1] is not None:
                    return
        try:
            self.page.go_back(wait_until="domcontentloaded")
        except Exception:
            logger.info("Browser back was not available while returning to the summary")

    def _ensure_summary(self) -> None:
        if self._detail_popup is not None:
            self._return_from_detail()
        elif self.main_page and not self.main_page.is_closed():
            self.page = self.main_page
        if self._summary_ready():
            return
        self._return_from_detail()
        if self._summary_ready():
            return
        self._wait_for_submitted_table()

    def _guard_session(self) -> None:
        if self._authenticated():
            return
        self._wait_for_manual_login()
        self._ensure_summary()

    def _summary_table(self):
        for frame in self.page.frames:
            tables = frame.locator("table")
            try:
                count = tables.count()
            except Exception:
                continue
            for index in range(count):
                table = tables.nth(index)
                try:
                    header_text = norm_header(table.locator("thead").inner_text(timeout=1500))
                except Exception:
                    try:
                        header_text = norm_header(table.locator("tr").first.inner_text(timeout=1500))
                    except Exception:
                        continue
                if "date" in header_text and "present" in header_text:
                    return frame, table
        return None, None

    def _read_table(self, kind: str) -> Optional[dict]:
        best = None
        for frame in self.page.frames:
            try:
                tables = frame.evaluate(EXTRACT_TABLES_JS)
            except Exception:
                continue
            for table in tables or []:
                headers = [norm_header(header) for header in table.get("headers") or []]
                rows = table.get("rows") or []
                if kind == "summary" and "date" in headers and "present" in headers:
                    score = 100 + len(rows)
                elif kind == "candidate_punches" and (
                    ("consume date" in headers or "log date" in headers or "in time" in headers)
                    and ("candidate id" in headers or "kp id" in headers)
                ):
                    score = 120 + len(rows)
                elif kind == "detail" and ("candidate id" in headers or "in time" in headers):
                    score = 80 + len(rows)
                else:
                    continue
                if best is None or score > best[0]:
                    best = (score, {"headers": table["headers"], "rows": rows})
        return None if best is None else best[1]

    def _wait_for_table(self, kind: str, timeout: int) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._check_cancel()
            if not self._loader_visible():
                table = self._read_table(kind)
                if table and table["rows"]:
                    self.page.wait_for_timeout(40)
                    again = self._read_table(kind)
                    if again and len(again["rows"]) >= len(table["rows"]):
                        return again
            self.page.wait_for_timeout(80)
        self._shot(f"{kind}-timeout")
        raise PortalError(f"The {kind} attendance table did not finish loading.")

    def _loader_visible(self) -> bool:
        try:
            return bool(self.page.evaluate(LOADER_JS))
        except Exception:
            return False

    def _scroll_more(self) -> bool:
        try:
            result = self.page.evaluate(SCROLL_JS)
        except Exception:
            return False
        if result and result.get("scrolled"):
            self.page.wait_for_timeout(40)
            return True
        return False

    def _click_next(self) -> bool:
        for selector in NEXT_SELECTORS:
            locator = self.page.locator(selector)
            if locator.count() == 0:
                continue
            target = locator.last
            try:
                if not target.is_visible():
                    continue
            except Exception:
                continue
            disabled = target.get_attribute("disabled")
            aria = (target.get_attribute("aria-disabled") or "").lower()
            class_name = target.get_attribute("class") or ""
            if disabled is not None or aria == "true" or "disabled" in class_name.lower():
                continue
            target.click()
            return True
        return False

    def _wait_until_settled(self) -> None:
        for _ in range(4):
            if not self._loader_visible():
                return
            self.page.wait_for_timeout(80)

    def _goto(self, url: str) -> None:
        last_error: Exception | None = None
        for attempt in range(2):
            self._check_cancel()
            try:
                if not self.alive():
                    self.open_browser(force=True)
                self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                return
            except SyncCancelled:
                raise
            except Exception as exc:
                last_error = exc
                text = str(exc)
                if "closed" not in text.lower() and "targetclosed" not in text.lower():
                    raise
                logger.warning("Portal browser closed during navigation, attempt %s", attempt + 1)
                self.open_browser(force=True)
        raise PortalError(
            "The Chromium window closed before the Kaushal portal finished loading. Start the sync again and leave that window open."
        ) from last_error

    def _wait_for_app(self) -> None:
        deadline = time.time() + 40
        while time.time() < deadline:
            self._check_cancel()
            try:
                text = self.page.inner_text("body")
            except Exception:
                text = ""
            if len(text.strip()) > 40 and not self._loader_visible():
                return
            self.page.wait_for_timeout(400)

    def _click_text(self, pattern: str) -> bool:
        locator = self.page.get_by_text(re.compile(pattern, re.I))
        if locator.count() == 0:
            return False
        try:
            locator.first.click(timeout=5000)
            return True
        except Exception:
            logger.info("Menu text %s was visible but not clickable", pattern)
            return False

    def _shot(self, name: str) -> None:
        if self.sync_id is None or self.page is None:
            return
        folder = SCREENSHOT_DIR / str(self.sync_id)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            self.page.screenshot(path=str(folder / f"{name}.png"), full_page=True)
        except Exception:
            logger.exception("Screenshot %s failed", name)

    def _check_cancel(self) -> None:
        if self.manager.cancel_event.is_set():
            raise SyncCancelled("Sync cancelled by the administrator.")


def _index(headers: list[str], options: tuple[str, ...]) -> Optional[int]:
    for option in options:
        if option in headers:
            return headers.index(option)
    return None


def _same_day(left: str, right: str) -> bool:
    first = parse_date(left)
    second = parse_date(right)
    return first is not None and first == second
