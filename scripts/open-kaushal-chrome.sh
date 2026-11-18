#!/bin/zsh
# Open Google Chrome so attendance sync can read Kaushal from it.
# Newer Chrome requires a non-default --user-data-dir for remote debugging.

set -e
PORT="${ATTENDANCE_CDP_PORT:-9222}"
URL="${1:-https://kaushal.dord.gov.in/}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE="${ATTENDANCE_CHROME_PROFILE:-$HOME/Library/Application Support/kk-attendance-chrome}"

if [[ ! -x "$CHROME" ]]; then
  echo "Google Chrome was not found at: $CHROME"
  exit 1
fi

if curl -fsS "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
  echo "Chrome is already open for sync on port ${PORT}."
  echo "Log into Kaushal in that Chrome window, then start sync in the app."
  open "$URL" 2>/dev/null || true
  exit 0
fi

echo "Closing Chrome so it can reopen for sync (required once)…"
osascript -e 'quit app "Google Chrome"' >/dev/null 2>&1 || true
killall -9 "Google Chrome" 2>/dev/null || true
sleep 2

echo "Opening your Chrome for Kaushal sync…"
mkdir -p "$PROFILE"
"$CHROME" \
  --remote-debugging-port="${PORT}" \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  --restore-last-session=false \
  "$URL" >/tmp/kk-chrome-sync.log 2>&1 &
sleep 3

if curl -fsS "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
  echo "Ready. In Chrome: sign into Kaushal."
  echo "Then in the attendance app: Sync Attendance → start → click “I have logged in”."
else
  echo "Chrome did not open with debugging. Quit Chrome completely and run this script again."
  echo "See /tmp/kk-chrome-sync.log for details."
  exit 1
fi
