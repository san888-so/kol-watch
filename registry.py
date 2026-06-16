"""Parse the KOL Google Sheet -> kol + channel rows.

"All" tab layout (0-based columns), header on row 0, data from row 1:
  0 Name  1 Tier  2 Exclusive  3 E-Mail  4 CTA Link  5 Remark
  6 Main Platform  7 Ploy Recheck  8 All Check
  9 TT Check  10 Tiktok Link
  11 FB Check  12 Facebook Link
  13 YT Check  14 Youtube Link
  15 X Check   16 X Link
  17 IG Check  18 IG Link
  19 Lemon8 Check  20 Lemon8 Link

The Exclusive column value drives competitor-scan eligibility:
  "Exclusive (All Compet)"   -> scan for competitors AND brand
  "Non - Exclusive"          -> scan for brand only
"""
import os
import sys
import re
from urllib.parse import urlparse, parse_qs, unquote

sys.path.append(os.path.join(os.path.dirname(__file__), "vendor"))
import gsync  # noqa: E402  (vendored: public-CSV reader)

import db  # noqa: E402

COL_NAME = 0
COL_TIER = 1
COL_EXCL = 2
PLATFORM_COLS = {
    "tiktok": 10,
    "facebook": 12,
    "youtube": 14,
    "x": 16,
    "instagram": 18,
    "lemon8": 20,
}

# A value is a usable profile URL only if it is http(s) and the host looks right.
PLATFORM_HOST = {
    "facebook": ("facebook.com", "fb.com", "fb.watch"),
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
    "x": ("x.com", "twitter.com"),
    "instagram": ("instagram.com",),
    "lemon8": ("lemon8-app.com", "lemon8.com"),
}

_URL_RE = re.compile(r"https?://[^\s]+", re.I)


def _clean(v):
    return (v or "").strip()


def _extract_url(raw):
    m = _URL_RE.search(raw)
    if not m:
        return ""
    url = m.group(0).rstrip(" ,);").strip()
    # Unwrap Facebook link shim: l.facebook.com/l.php?u=<real-url>
    try:
        p = urlparse(url)
        if p.netloc.endswith("facebook.com") and p.path == "/l.php":
            inner = parse_qs(p.query).get("u", [""])[0]
            if inner:
                return unquote(inner)
    except Exception:
        pass
    return url


def _host_ok(platform, url):
    u = url.lower()
    return any(h in u for h in PLATFORM_HOST[platform])


def sync_from_sheet(sheet_id, gid=None):
    """Read sheet -> merge into kol/channel (non-destructive).

    Existing channels keep their incremental cursor (last_seen_post_id)
    so a re-sync never triggers a full re-fetch. Only the unresolved
    report is rebuilt. Returns a summary with new vs existing counts.
    """
    gid = gid or db.get_setting("sheet_gid", "401523688") or "0"
    rows = gsync.read_csv(sheet_id, gid=gid)
    db.clear_unresolved()

    existing = {(c["kol_id"], c["platform"], c["profile_url"])
                for c in db.list_channels()}
    kol_count = 0
    channel_count = 0
    new_channels = 0
    unresolved = 0

    for i, r in enumerate(rows):
        if i == 0:  # header
            continue
        name = _clean(r[COL_NAME]) if len(r) > COL_NAME else ""
        if not name:
            continue
        tier = _clean(r[COL_TIER]) if len(r) > COL_TIER else ""
        excl = _clean(r[COL_EXCL]) if len(r) > COL_EXCL else ""
        kol_id = db.upsert_kol(name, tier, excl)
        kol_count += 1

        for platform, col in PLATFORM_COLS.items():
            raw = _clean(r[col]) if len(r) > col else ""
            if not raw or raw == "-":
                continue
            url = _extract_url(raw)
            if not url:
                db.add_unresolved(name, platform, raw, "not a URL")
                unresolved += 1
                continue
            if not _host_ok(platform, url):
                db.add_unresolved(name, platform, raw,
                                  f"URL host does not match {platform}")
                unresolved += 1
                continue
            if (kol_id, platform, url) not in existing:
                new_channels += 1
            db.upsert_channel(kol_id, platform, url)
            channel_count += 1

    return {
        "kols": kol_count,
        "channels": channel_count,
        "new_channels": new_channels,
        "unresolved": unresolved,
    }
