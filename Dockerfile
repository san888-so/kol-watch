# Playwright's official Python image already ships with Chromium + deps,
# which the Lemon8 collector needs. Pin a known-good tag.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Bangkok \
    KOLWATCH_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium

COPY . .

# /data is the persistent volume on Railway (mount it in the dashboard)
RUN mkdir -p /data

CMD ["python3", "main.py"]
