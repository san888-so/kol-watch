"""Google Sheets sync.

Read: public CSV export (no auth needed, sheet must allow "anyone with link")
Write: POST JSON to Apps Script Web App webhook (user deploys it once)
"""
import csv
import io
import json
import re
import urllib.request
from datetime import datetime, timezone

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"

# Column indexes in the sheet (0-based)
COL = {
    "content_code": 0,
    "kol_name": 1,
    "content": 2,
    "platform": 3,
    "content_type": 4,
    "post_url": 5,
    "screen_capture": 6,
    "post_date": 7,
    "post_time": 8,
    "data_collection_period": 9,
    "impression": 10,
    "reach": 11,
    "views": 12,
    "total_engagement": 13,
    "likes": 14,
    "comments": 15,
    "shares": 16,
    "engagement_rate": 17,
}
# Columns we WRITE to (skip Video Metric 18+)
WRITE_COLS = list(range(3, 18))

DATA_HEADER_ROW = 2  # 0-based; row 3 in 1-based = first data row


def extract_sheet_id(sheet_id_or_url):
    if not sheet_id_or_url:
        return ""
    s = sheet_id_or_url.strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", s)
    return m.group(1) if m else s


def read_csv(sheet_id, gid="0"):
    sid = extract_sheet_id(sheet_id)
    url = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        text = r.read().decode("utf-8")
    return list(csv.reader(io.StringIO(text)))


def list_post_rows(rows):
    """Return list of {row_index, kol_name, post_url} for rows with a Post URL."""
    out = []
    for i, r in enumerate(rows):
        if i <= DATA_HEADER_ROW:
            continue
        if len(r) <= COL["post_url"]:
            continue
        url = (r[COL["post_url"]] or "").strip()
        if not url:
            continue
        out.append({
            "row_index": i,            # 0-based index into rows
            "row_number": i + 1,        # 1-based for sheets
            "kol_name": (r[COL["kol_name"]] if len(r) > COL["kol_name"] else "").strip(),
            "post_url": url,
        })
    return out


def data_collection_period(days_since):
    if days_since is None or days_since < 0:
        return ""
    if days_since <= 1:
        return "ภายใน 24hr หลังโพส"
    if days_since <= 3:
        return "3 Days หลังโพส"
    if days_since <= 7:
        return "7 Days หลังโพส"
    return ">7 Day"


def days_since(post_date):
    if not post_date:
        return None
    try:
        d = datetime.strptime(post_date, "%Y-%m-%d").date()
    except Exception:
        return None
    today = datetime.now(timezone.utc).date()
    return (today - d).days


def build_row_update(post, snap):
    """Return dict of {column_index: value} with only non-empty values."""
    out = {}

    def put(col_key, val):
        if val is None or val == "" or val == 0 and col_key not in ("impression",):
            # allow 0 only for impressions; otherwise skip zeros
            if val == 0 and col_key in ("likes", "comments", "shares", "views",
                                         "total_engagement"):
                return  # don't overwrite empty cells with 0
            if val is None or val == "":
                return
        out[COL[col_key]] = val

    # Basic
    if post.get("kol_name"):
        out[COL["kol_name"]] = post["kol_name"]
    if post.get("platform"):
        out[COL["platform"]] = post["platform"]
    if post.get("content_type"):
        out[COL["content_type"]] = post["content_type"]
    if post.get("post_date"):
        out[COL["post_date"]] = post["post_date"]
    if post.get("post_time"):
        # strip minute (per sheet header instruction)
        pt = post["post_time"]
        m = re.match(r"^(\d{1,2})", pt)
        out[COL["post_time"]] = (m.group(1) + ":00") if m else pt

    period = data_collection_period(days_since(post.get("post_date")))
    if period:
        out[COL["data_collection_period"]] = period

    if not snap:
        return out

    # Metrics — only write non-null
    if snap.get("impressions") is not None:
        out[COL["impression"]] = snap["impressions"]
    if snap.get("reach") is not None:
        out[COL["reach"]] = snap["reach"]
    if snap.get("views"):
        out[COL["views"]] = snap["views"]
    if snap.get("total_engagement"):
        out[COL["total_engagement"]] = snap["total_engagement"]
    if snap.get("likes"):
        out[COL["likes"]] = snap["likes"]
    if snap.get("comments"):
        out[COL["comments"]] = snap["comments"]
    if snap.get("shares"):
        out[COL["shares"]] = snap["shares"]
    if snap.get("engagement_rate") is not None:
        # send as decimal so the sheet's "%" format displays correctly (5.25% = 0.0525)
        out[COL["engagement_rate"]] = round(snap["engagement_rate"] / 100, 4)

    return out


def post_webhook(webhook_url, payload):
    """POST JSON to Apps Script Web App. Returns parsed response or raises."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def normalize_url(u):
    return (u or "").strip().rstrip("/").lower()


def read_trigger(sheet_id):
    """Return value of cell A1 ('' if blank or read failure)."""
    try:
        rows = read_csv(sheet_id)
    except Exception:
        return ""
    if not rows or not rows[0]:
        return ""
    return (rows[0][0] or "").strip()


def set_status(webhook_url, status=None, eta=None, last_done=None):
    payload = {"action": "set_status"}
    if status is not None: payload["status"] = status
    if eta is not None: payload["eta"] = eta
    if last_done is not None: payload["last_done"] = last_done
    try:
        return post_webhook(webhook_url, payload)
    except Exception:
        return {"ok": False}


def consume_trigger(webhook_url):
    try:
        return post_webhook(webhook_url, {"action": "consume_trigger"})
    except Exception:
        return {"ok": False}
