#!/bin/zsh
# Open YOUR normal Google Chrome so attendance sync can read Kaushal from it.
# Chrome must be started with remote debugging (one-time for this window).

set -e
PORT="${ATTENDANCE_CDP_PORT:-9222}"
URL="${1:-https://kaushal.dord.gov.in/}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

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
sleep 2

echo "Opening your Chrome for Kaushal sync…"
"$CHROME" --remote-debugging-port="${PORT}" --restore-last-session=false "$URL" >/tmp/kk-chrome-sync.log 2>&1 &
sleep 2

if curl -fsS "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
  echo "Ready. In Chrome: sign into Kaushal."
  echo "Then in the attendance app: Sync Attendance → start → click “I have logged in”."
else
  echo "Chrome did not open with debugging. Quit Chrome completely and run this script again."
  exit 1
fi
