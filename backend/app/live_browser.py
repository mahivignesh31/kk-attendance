"""Always-on Chromium for online Kaushal login (no Playwright during login).

Login runs in a normal Chromium window (no remote-debugging). After the admin
clicks “I have logged in”, Chromium restarts with the same profile + CDP so
sync can read reports. Cookies survive in the profile directory.
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
_debug_enabled = False


def cdp_endpoint() -> str:
    return CDP_URL


def find_chromium() -> str:
    override = os.environ.get("ATTENDANCE_CHROMIUM")
    if override and Path(override).exists():
        return override
    patterns = [
        "/ms-playwright/chromium-*/chrome-linux/chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
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


def ensure_login_browser(url: str | None = None, *, fresh: bool = False) -> None:
    """Open normal Chromium for login (no CDP / no automation flags)."""
    if not remote_display_enabled():
        return
    target = url or PORTAL_LOGIN_URL
    if is_running() and not _debug_enabled and not fresh:
        return
    stop()
    _start(target, debug=False)


def enable_debugging(url: str | None = None) -> None:
    """Restart Chromium with CDP using the same profile (keeps login cookies)."""
    if not remote_display_enabled():
        return
    target = url or PORTAL_LOGIN_URL
    stop()
    time.sleep(0.8)
    _start(target, debug=True)
    deadline = time.time() + 45
    while time.time() < deadline:
        if cdp_ready(timeout=0.5):
            return
        time.sleep(0.3)
    raise RuntimeError("Could not enable the live browser for reading attendance.")


def ensure_started(url: str | None = None) -> None:
    """Boot helper: show the Kaushal login page in a normal window."""
    ensure_login_browser(url)


def _start(url: str, *, debug: bool) -> None:
    global _proc, _debug_enabled
    display = os.environ.get("DISPLAY", ":99")
    profile = browser_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    _clear_profile_locks(profile)
    chromium = find_chromium()
    env = os.environ.copy()
    env["DISPLAY"] = display
    args = [
        chromium,
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-sandbox",
        "--window-size=1440,900",
        "--window-position=0,0",
        "--start-maximized",
        # Look like a normal desktop Chrome, not a bot.
        "--disable-blink-features=AutomationControlled",
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    ]
    if debug:
        args.extend(
            [
                f"--remote-debugging-port={CDP_PORT}",
                "--remote-debugging-address=127.0.0.1",
            ]
        )
    args.append(url)
    logger.info("Starting live Chromium debug=%s url=%s", debug, url)
    _proc = subprocess.Popen(
        args,
        env=env,
        stdout=open("/tmp/live-chromium.out", "ab", buffering=0),
        stderr=open("/tmp/live-chromium.err", "ab", buffering=0),
        start_new_session=True,
    )
    _debug_enabled = debug
    deadline = time.time() + 30
    while time.time() < deadline:
        if _proc.poll() is not None:
            break
        if debug and cdp_ready(timeout=0.4):
            logger.info("Live Chromium CDP ready on %s", CDP_URL)
            return
        if not debug and _proc.poll() is None and time.time() > deadline - 28:
            # Process stayed up — good enough for login window.
            time.sleep(1.2)
            if _proc.poll() is None:
                return
        time.sleep(0.3)
    if _proc.poll() is not None:
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
    global _proc, _debug_enabled
    if _proc is not None and _proc.poll() is None:
        try:
            os.killpg(os.getpgid(_proc.pid), signal.SIGTERM)
        except OSError:
            try:
                _proc.terminate()
            except OSError:
                pass
        try:
            _proc.wait(timeout=6)
        except Exception:
            try:
                os.killpg(os.getpgid(_proc.pid), signal.SIGKILL)
            except OSError:
                pass
    # Also clear any orphan chromium using our profile.
    profile = str(browser_profile_dir())
    try:
        listed = subprocess.run(["pgrep", "-fl", profile], capture_output=True, text=True, check=False)
        if listed.stdout:
            for line in listed.stdout.splitlines():
                pid_text, _, command = line.partition(" ")
                if "chrom" not in command.lower():
                    continue
                try:
                    os.kill(int(pid_text), signal.SIGTERM)
                except (ValueError, OSError):
                    continue
            time.sleep(0.5)
    except Exception:
        pass
    _proc = None
    _debug_enabled = False
    _clear_profile_locks(browser_profile_dir())
