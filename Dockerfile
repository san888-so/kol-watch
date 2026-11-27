# Playwright's official Python image already ships with Chromium + deps,
# which the Lemon8 collector needs. Pin a known-good tag.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Bangkok \
    KOLWATCH_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
# tini as PID 1 (27 Nov 2026): Chromium (Lemon8) leaves helper processes
# that get re-parented to PID 1. Python never reaps them, so zombies piled
# up until the container hit its pid limit ("can't start new thread") and
# the web server + scans died. tini reaps them.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium

COPY . .

# /data is the persistent volume on Railway (mount it in the dashboard)
RUN mkdir -p /data

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "main.py"]
