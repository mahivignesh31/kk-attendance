FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ATTENDANCE_HEADLESS=0 \
    ATTENDANCE_REMOTE_DISPLAY=1 \
    ATTENDANCE_BLOCK_CLOUD_SYNC=0 \
    ATTENDANCE_DB=/data/attendance.db \
    ATTENDANCE_BROWSER_PROFILE=/data/browser-profile \
    DISPLAY=:99 \
    PORT=8000 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
        xvfb \
        x11vnc \
        novnc \
        websockify \
        fluxbox \
        nginx \
        gettext-base \
        fonts-liberation \
        fonts-noto-core \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY frontend /app/frontend
COPY deploy /app/deploy
COPY scripts/start-online.sh /app/scripts/start-online.sh
RUN chmod +x /app/scripts/start-online.sh

WORKDIR /app/frontend
RUN npm install && npm run build

WORKDIR /app/backend
EXPOSE 8000

CMD ["/app/scripts/start-online.sh"]
