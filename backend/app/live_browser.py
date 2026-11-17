"""Always-on Chromium for online Kaushal login (no Playwright during login).

Playwright's controlled browser shows “automated test software” and gets blocked.
This module starts a normal Chromium on the remote display. After the admin logs
in through noVNC, sync attaches with CDP only for reading reports.
"""

from __future__ import annotations

import glob
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen

from .config import PORTAL_LOGIN_URL, browser_profile_dir, remote_display_enabled

logger = logging.getLogger("attendance.live_browser")

CDP_PORT = int(os.environ.get("ATTENDANCE_CDP_PORT", "9222"))
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

_proc: Optional[subprocess.Popen] = None


def cdp_endpoint() -> str:
    return CDP_URL


def find_chromium() -> str:
    override = os.environ.get("ATTENDANCE_CHROMIUM")
    if override and Path(override).exists():
        return override
    patterns = [
        "/ms-playwright/chromium-*/chrome-linux/chrome",
        "/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        # Prefer full chrome over headless_shell for interactive login.
        preferred = [path for path in matches if "headless_shell" not in path]
        if preferred:
            return preferred[-1]
        if matches:
            return matches[-1]
    raise RuntimeError("Chromium was not found for the live portal window.")


def cdp_ready(timeout: float = 1.0) -> bool:
    try:
        with urlopen(f"{CDP_URL}/json/version", timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def is_running() -> bool:
    global _proc
    if cdp_ready():
        return True
    if _proc is not None and _proc.poll() is None:
        return True
    return False


def open_url(url: str) -> None:
    """Open a URL in the live Chromium without attaching Playwright."""
    if not cdp_ready():
        return
    try:
        from urllib.parse import quote
        from urllib.request import Request

        target = f"{CDP_URL}/json/new?{quote(url, safe='')}"
        with urlopen(Request(target, method="PUT"), timeout=8) as response:
            response.read()
    except Exception as exc:
        logger.info("Could not open %s in live Chromium: %s", url, exc)


def ensure_started(url: str | None = None) -> None:
    """Start a normal Chromium on DISPLAY for noVNC login (idempotent)."""
    if not remote_display_enabled():
        return
    target = url or PORTAL_LOGIN_URL
    if cdp_ready():
        open_url(target)
        return
    _start(target)


def _start(url: str) -> None:
    global _proc
    display = os.environ.get("DISPLAY", ":99")
    profile = browser_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    _clear_profile_locks(profile)
    chromium = find_chromium()
    env = os.environ.copy()
    env["DISPLAY"] = display
    # No --enable-automation: keep this a normal browser for CAPTCHA/login.
    args = [
        chromium,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1440,900",
        "--window-position=0,0",
        "--start-maximized",
        url,
    ]
    logger.info("Starting live Chromium for Kaushal login on %s", display)
    _proc = subprocess.Popen(
        args,
        env=env,
        stdout=open("/tmp/live-chromium.out", "ab", buffering=0),
        stderr=open("/tmp/live-chromium.err", "ab", buffering=0),
        start_new_session=True,
    )
    deadline = time.time() + 45
    while time.time() < deadline:
        if cdp_ready(timeout=0.5):
            logger.info("Live Chromium CDP is ready on %s", CDP_URL)
            return
        if _proc.poll() is not None:
            break
        time.sleep(0.4)
    raise RuntimeError(
        "The live Kaushal browser did not start. Refresh the Sync page and try again."
    )


def _clear_profile_locks(profile: Path) -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile / name).unlink(missing_ok=True)
        except OSError:
            pass


def stop() -> None:
    global _proc
    if _proc is not None and _proc.poll() is None:
        try:
            os.killpg(os.getpgid(_proc.pid), signal.SIGTERM)
        except OSError:
            try:
                _proc.terminate()
            except OSError:
                pass
        try:
            _proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(_proc.pid), signal.SIGKILL)
            except OSError:
                pass
    _proc = None
