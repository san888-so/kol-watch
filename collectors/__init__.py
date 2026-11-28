"""Collector dispatch: platform -> collect(channel, limit) -> [posts].

Each post: {post_id, url, caption, hashtags(list), posted_at}.
Collectors raise on hard failure; the scheduler catches and marks the
channel blocked so it surfaces in the digest (never silently 'clean').
"""
from . import youtube_rss, apify_profile, brightdata_profile, lemon8
import db
import relay

_APIFY = {"tiktok", "instagram", "facebook", "x", "threads"}
_QUOTA = ("hard limit", "platform-feature-disabled", "monthly usage")


# Facebook/IG are fetched as "posts since the last check", so zero posts
# just means nothing new; a gone/private page raises in apify_profile.
_WINDOWED = {"facebook", "instagram"}


def collect(channel, limit=10, windowed=True):
    posts = _collect(channel, limit, windowed)
    # Zero posts almost always means a renamed/deleted/private account or a
    # wrong link — surface it as "เช็กไม่ได้" instead of a silent "ok".
    if not posts and not (windowed and channel["platform"] in _WINDOWED):
        raise RuntimeError("ดึงได้ 0 โพสต์ — บัญชีอาจเปลี่ยนชื่อ ถูกปิด เป็นส่วนตัว "
                           "หรือลิงก์ในชีตผิด")
    for p in posts:
        if not p.get("posted_at"):
            p["posted_at"] = date_from_id(channel["platform"], p.get("post_id"))
    return posts


def date_from_id(platform, post_id):
    """X and TikTok ids carry their creation time. Some scrapers return no
    date (5,106 X posts were saved undated by Nov 2026 and so missing from
    every per-month count) — derive it from the id instead."""
    from datetime import datetime, timezone
    pid = str(post_id or "")
    if not pid.isdigit() or len(pid) < 15:
        return ""
    try:
        if platform == "x":
            ts = ((int(pid) >> 22) + 1288834974657) / 1000
        elif platform == "tiktok":
            ts = int(pid) >> 32
        else:
            return ""
        if not 1.2e9 < ts < 2.2e9:
            return ""
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()
    except Exception:
        return ""


def _collect(channel, limit=10, windowed=True):
    platform = channel["platform"]
    # Pages only a logged-in browser can read: posts are pushed in from
    # sanmini (relay/fb_relay.py) instead of fetched here — see relay.py.
    if relay.is_relay(channel):
        return relay.collect(channel)
    if platform == "youtube":
        # Same quota cost for 3 or 15 items (1 API unit / free RSS), and a
        # channel posting Shorts can add 4+ videos between scans.
        return youtube_rss.collect(channel, limit=max(limit, 15))
    if platform == "lemon8":
        return lemon8.collect(channel, limit=limit)
    if platform in _APIFY:
        try:
            return apify_profile.collect(channel, limit=limit, windowed=windowed)
        except Exception as e:
            # IMPORTANT cost guard: when Apify is over quota, do NOT
            # auto-cascade every channel to Brightdata (BD is far pricier
            # and that cascade silently drained the BD balance). Only fall
            # back if the operator explicitly opted in.
            msg = str(e).lower()
            is_quota = any(q in msg for q in _QUOTA)
            bd_on = (db.get_setting("enable_bd_fallback", "0") or "0") == "1"
            if is_quota and bd_on:
                return brightdata_profile.collect(channel, limit=limit)
            raise
    raise RuntimeError(f"no collector for platform {platform}")
