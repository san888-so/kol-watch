# KOL Competitor-Keyword Watch

Monitors a list of KOLs across Facebook, YouTube, TikTok, X, Instagram and
Lemon8 for posts mentioning competitor brands (e.g. Moomoo, Dime, Liberator —
Thai + English, evasion-aware). When a match is found it pushes a LINE alert
with the post link and the matched keyword so the team can ask for removal.

Scans run twice a day (default 08:00 and 16:00) plus an on-demand button.

## Features

- KOL list synced from a public Google Sheet (re-sync button)
- Free YouTube collection via RSS (no API key); TikTok/X/IG/FB via Apify
  with a Brightdata fallback; Lemon8 via Playwright
- Incremental — only new posts since the last check are scanned
- Thai/English keyword matcher with leetspeak + spacing evasion handling
- LINE push per hit + end-of-run digest
- Web dashboard: hits with follow-up status, all collected posts, CSV export,
  blocked channels, and unreadable Sheet links
- Login-protected

## Setup

```bash
pip install -r requirements.txt
python3 -m playwright install chromium   # for Lemon8
cp .env.example .env                      # fill in tokens + login
export $(grep -v '^#' .env | xargs)
python3 main.py
```

Open http://127.0.0.1:5070 and log in.

## Configuration

| Env var | Purpose |
|---|---|
| `APIFY_TOKEN` | Apify API token (TikTok/X/IG/FB) |
| `BRIGHTDATA_TOKEN` | Brightdata token (fallback) |
| `KOLWATCH_USER` / `KOLWATCH_PASS` | dashboard login |
| `KOLWATCH_PORT` | server port (default 5070) |

In the dashboard **Settings**: Google Sheet ID, scan times, LINE channel
access token + target id, posts-per-check, and the editable watchlist.

The Google Sheet must be link-readable. Expected columns (5 leading blank
columns, header on row 1): KOL name, Tier, Exclusivity, primary link, then
Facebook, Youtube, Tiktok, X, IG, Lemon8.

## Deploy to Railway (always-on, no laptop required)

Repo includes a `Dockerfile` (Playwright + Chromium baked in) and reads
config from env. Steps in the Railway dashboard:

1. **New Project → Deploy from GitHub repo** → pick this repo.
2. **Variables** — add:
   - `APIFY_TOKEN`, `BRIGHTDATA_TOKEN`
   - `KOLWATCH_USER`, `KOLWATCH_PASS` (your dashboard login)
   - `TZ=Asia/Bangkok` (so the 08:00/16:00 cron runs in Thai time)
3. **Settings → Networking → Generate Domain** to get a public `https://…up.railway.app` URL.
4. **Settings → Volumes → New Volume** mounted at `/data` — keeps the
   SQLite DB across redeploys (set/keep `KOLWATCH_DATA_DIR=/data`, the
   Dockerfile already sets this).
5. Deploy. Open the URL, log in, fill in **Settings**: Google Sheet ID,
   LINE token + target id, then press **Sync จาก Sheet**.

The container binds `0.0.0.0:$PORT` automatically when Railway sets
`PORT`; local runs keep using `127.0.0.1:5070`.

## Notes

- `data/` (SQLite: collected posts + state) is gitignored — local only.
- Tokens come from env vars or `~/.tiktok_scraper/providers.json`; never
  commit them.
- Change `KOLWATCH_PASS` for any real deployment.
