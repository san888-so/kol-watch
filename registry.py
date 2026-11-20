"""Parse the KOL Google Sheet -> kol + channel rows.

"All" tab layout (0-based columns), header on row 0, data from row 1:
  0 Name  1 Tier  2 Exclusive  3 E-Mail  4 CTA Link  5 Remark
     (Remark containing "ยกเลิกสัญญา" = contract cancelled -> not scanned)
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
import json
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
COL_REMARK = 5
# A row whose Remark contains this text is a cancelled contract: the KOL is
# kept in the db (old posts/hits stay readable) but is no longer scanned.
CANCEL_MARK = "ยกเลิกสัญญา"
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


_BARE_RE = re.compile(
    r"(?:www\.)?(?:[a-z0-9-]+\.)*(?:facebook\.com|fb\.com|youtube\.com|youtu\.be|"
    r"tiktok\.com|x\.com|twitter\.com|instagram\.com|lemon8-app\.com)/[^\s]+", re.I)


def _extract_url(raw):
    m = _URL_RE.search(raw)
    if not m:
        # Links pasted without https:// (e.g. "instagram.com/name")
        b = _BARE_RE.search(raw)
        return ("https://" + b.group(0).rstrip(" ,);").strip()) if b else ""
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


def _skip_channels():
    """Channels the team does not want checked, kept out of the scan without
    deleting the link from the Sheet (the team still uses those links).
    Setting `skip_channels`: JSON {"KOL name as in the Sheet": ["lemon8",
    "instagram", ...]}. A skipped link is switched off on the next sync."""
    try:
        raw = json.loads(db.get_setting("skip_channels", "{}") or "{}")
    except Exception:
        return {}
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, str):
                v = [v]
            if isinstance(k, str) and isinstance(v, list):
                out[" ".join(k.split()).lower()] = {
                    str(x).strip().lower() for x in v if str(x).strip()}
    return out


def _extra_channels():
    """Links the Sheet cannot hold: it has ONE column per platform, but a
    KOL can run a page AND a personal profile, or rename an account.
    Setting `extra_channels`: JSON {"KOL name as in the Sheet":
    {"facebook": ["https://..."], "x": "https://..."}}. These links are
    scanned on top of the Sheet's and survive every re-sync."""
    try:
        raw = json.loads(db.get_setting("extra_channels", "{}") or "{}")
    except Exception:
        return {}
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if not (isinstance(k, str) and isinstance(v, dict)):
                continue
            links = {}
            for p, urls in v.items():
                if isinstance(urls, str):
                    urls = [urls]
                if p in PLATFORM_COLS and isinstance(urls, list):
                    links[p] = [str(u).strip() for u in urls if str(u).strip()]
            out[" ".join(k.split()).lower()] = links
    return out


def _exclusive_override():
    import json
    try:
        v = json.loads(db.get_setting("exclusive_override", "[]") or "[]")
        return set(v) if isinstance(v, list) else set()
    except Exception:
        return set()


def sync_from_sheet(sheet_id, gid=None):
    """Read sheet -> merge into kol/channel (non-destructive).

    Existing channels keep their incremental cursor (last_seen_post_id)
    so a re-sync never triggers a full re-fetch. Only the unresolved
    report is rebuilt. Returns a summary with new vs existing counts.
    """
    gid = gid or db.get_setting("sheet_gid", "401523688") or "0"
    rows = gsync.read_csv(sheet_id, gid=gid)
    skip = _skip_channels()
    extra = _extra_channels()
    skipped = 0
    db.clear_unresolved()

    existing = {(c["kol_id"], c["platform"], c["profile_url"])
                for c in db.list_channels(active_only=False)}
    synced_channel_ids = set()
    kols_before = db.count_kols()
    seen_names = set()
    cancelled = 0
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
        remark = _clean(r[COL_REMARK]) if len(r) > COL_REMARK else ""
        is_cancelled = CANCEL_MARK in remark
        # KOLs the team confirmed Exclusive (tracking sheet) even if the
        # master sheet still says otherwise — survives every re-sync.
        if name in _exclusive_override():
            excl = "Exclusive (All Compet)"
        kol_id = db.upsert_kol(name, tier, excl, active=0 if is_cancelled else 1)
        seen_names.add(name)
        kol_count += 1
        if is_cancelled:
            cancelled += 1
            continue  # keep its old channels, but don't add or scan any

        key = " ".join(name.split()).lower()
        skip_here = skip.get(key, set())
        links = []
        for platform, col in PLATFORM_COLS.items():
            raw = _clean(r[col]) if len(r) > col else ""
            if raw and raw != "-":
                links.append((platform, raw))
        for platform, urls in extra.get(key, {}).items():
            links += [(platform, u) for u in urls]
        for platform, raw in links:
            if platform in skip_here:  # team said: don't check this one
                skipped += 1
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
            synced_channel_ids.add(db.upsert_channel(kol_id, platform, url))
            channel_count += 1

    # KOLs that disappeared from the Sheet are switched off too — but only
    # when the Sheet read looks complete, so a half-loaded / wrong tab can
    # never silently turn the whole watch off.
    removed = []
    old_links = 0
    if kol_count and kol_count >= kols_before / 2:
        removed = db.deactivate_kols_not_in(seen_names)
        # A link that was changed in the Sheet left the old URL behind and
        # both were scanned (paid twice, or the wrong account). Only links
        # that are in the Sheet right now stay on.
        old_links = db.set_active_channels(synced_channel_ids)

    return {
        "kols": kol_count,
        "channels": channel_count,
        "new_channels": new_channels,
        "unresolved": unresolved,
        "cancelled": cancelled,
        "removed": len(removed),
        "old_links_off": old_links,
        "skipped_by_setting": skipped,
    }
