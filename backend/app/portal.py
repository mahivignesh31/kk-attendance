"""Read-only browser session for the Kaushal candidate attendance report.

The administrator signs in manually. This module never types credentials, never
solves CAPTCHA or MFA, and never creates or edits official attendance. The only
form control it submits is the report filter Submit button.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

from .calculator import parse_date
from .config import BROWSER_PROFILE, PORTAL_LOGIN_URL, PORTAL_REPORT_URL, SCREENSHOT_DIR
from .portal_parse import detail_key, norm_header, parse_detail_table, parse_summary_table

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

    def alive(self) -> bool:
        try:
            return self.page is not None and not self.page.is_closed()
        except Exception:
            return False

    def open_browser(self, force: bool = False) -> None:
        if self.alive() and not force:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PortalError("Playwright is not installed. Run `python -m playwright install chromium`.") from exc
        self._discard_browser()
        BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
        self._release_profile_lock()
        self.playwright = sync_playwright().start()
        try:
            headless = os.environ.get("ATTENDANCE_HEADLESS", "").strip().lower() in {"1", "true", "yes"}
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE),
                headless=headless,
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1440, "height": 900},
                accept_downloads=False,
                args=["--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage"],
            )
        except Exception as exc:
            self._discard_browser()
            raise PortalError(
                "Could not open the portal browser. Close any leftover Chromium window from the last sync, then try again."
            ) from exc
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.main_page = self.page
        self._detail_popup = None
        self.page.set_default_timeout(20000)
        self.page.set_default_navigation_timeout(60000)
        try:
            self.page.bring_to_front()
        except Exception:
            logger.info("Could not bring the portal window to the front")

    def _discard_browser(self) -> None:
        context = self.context
        playwright = self.playwright
        self.context = None
        self.page = None
        self.main_page = None
        self.playwright = None
        self._detail_popup = None
        if context is not None:
            try:
                context.close()
            except Exception:
                logger.info("Previous browser context was already closed")
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                logger.info("Previous Playwright driver was already stopped")

    def _release_profile_lock(self) -> None:
        import os
        import signal
        import subprocess

        profile = str(BROWSER_PROFILE)
        try:
            listed = subprocess.run(["pgrep", "-fl", profile], capture_output=True, text=True, check=False)
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
                (BROWSER_PROFILE / name).unlink(missing_ok=True)
            except OSError:
                logger.info("Could not remove the browser profile lock %s", name)

    def prepare_report(self, batch: dict, sync_id: int) -> list[str]:
        self.batch = batch
        self.sync_id = sync_id
        self.open_browser()
        self.manager.update(sync_id, progress_step="connecting", message="Opening the Kaushal portal")
        if self._summary_ready():
            return self._verify_batch_banner()
        if not self._on_attendance_report():
            self._goto(PORTAL_REPORT_URL)
            self._wait_for_app()
        if self._login_form_visible() or not self._authenticated():
            if not self._login_form_visible():
                self._goto(PORTAL_LOGIN_URL)
                self._wait_for_app()
            self._wait_for_manual_login()
            if not self._summary_ready() and not self._on_attendance_report():
                self._goto(PORTAL_REPORT_URL)
                self._wait_for_app()
        if self._summary_ready():
            return self._verify_batch_banner()
        self.manager.update(sync_id, progress_step="loading_report", message="Selecting the batch")
        if not self._filters_visible():
            self._open_report_menu()
            self._wait_for_app()
        if self._summary_ready():
            return self._verify_batch_banner()
        try:
            self._apply_filters()
            if not self._summary_ready():
                self._click_submit()
            self._wait_for_table("summary", timeout=25)
        except PortalError:
            self.manager.update(
                sync_id,
                message="Click Submit on the Kaushal report. Reading starts when the date table is visible.",
            )
            self._wait_for_submitted_table()
        if not self._summary_ready():
            self._wait_for_submitted_table()
        return self._verify_batch_banner()

    def discover_dates(self) -> tuple[list[dict], list[str]]:
        self.manager.update(self.sync_id, progress_step="discovering_dates", message="Discovering Dates")
        return self._collect("summary")

    def extract_date(self, summary: dict) -> tuple[list[dict], set[str], set[str], list[str]]:
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

    def _wait_for_manual_login(self) -> None:
        self.manager.update(
            self.sync_id,
            status="awaiting_login",
            progress_step="awaiting_login",
            message="Please log in to the official DDU-GKY Kaushal portal. Attendance synchronization will continue after successful authentication.",
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
