#!/bin/sh
# Online sync: virtual display + noVNC so the administrator can log into Kaushal in the browser.
set -eu

export DISPLAY="${DISPLAY:-:99}"
export ATTENDANCE_HEADLESS="${ATTENDANCE_HEADLESS:-0}"
export ATTENDANCE_REMOTE_DISPLAY="${ATTENDANCE_REMOTE_DISPLAY:-1}"
export ATTENDANCE_BROWSER_PROFILE="${ATTENDANCE_BROWSER_PROFILE:-/data/browser-profile}"
export ATTENDANCE_DB="${ATTENDANCE_DB:-/data/attendance.db}"
PORT="${PORT:-8000}"

mkdir -p /data /data/browser-profile /tmp

if ! pgrep -x Xvfb >/dev/null 2>&1; then
  Xvfb "$DISPLAY" -screen 0 1440x900x24 -ac +extension RANDR +render -noreset >/tmp/xvfb.log 2>&1 &
  sleep 1
fi

if ! pgrep -x fluxbox >/dev/null 2>&1; then
  fluxbox >/tmp/fluxbox.log 2>&1 &
fi

if ! pgrep -x x11vnc >/dev/null 2>&1; then
  x11vnc -display "$DISPLAY" -forever -shared -rfbport 5900 -nopw -xkb -repeat >/tmp/x11vnc.log 2>&1 &
fi

if ! pgrep -f "websockify.*6080" >/dev/null 2>&1; then
  websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/websockify.log 2>&1 &
fi

export PORT
envsubst '${PORT}' < /app/deploy/nginx.conf.template > /tmp/nginx.conf

cd /app/backend
uvicorn app.main:app --host 127.0.0.1 --port 8000 &
UVICORN_PID=$!

cleanup() {
  kill "$UVICORN_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Wait for API before handing traffic to nginx
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS "http://127.0.0.1:8000/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Normal Chromium for live Kaushal login (no remote-debugging until after login).
python - <<'PY' >/tmp/live-browser-boot.log 2>&1 &
from app.live_browser import ensure_login_browser
ensure_login_browser()
PY

exec nginx -g 'daemon off;' -c /tmp/nginx.conf
