"""Profile-mode collectors via Apify actors (TikTok / IG / Facebook / X).

Reuses tiktok_scraper/providers: ApifyProvider for the HTTP call + the
normalize_*_post helpers which keep caption/hashtags/url/id (the aggregate
fetch_*_profile methods drop those, so we go through the actor directly).
"""
import hashlib
import os
import re
import sys

_PROV = os.path.join(os.path.dirname(__file__), "..", "vendor")
if _PROV not in sys.path:
    sys.path.append(_PROV)

from providers.apify import ApifyProvider, ACTORS  # noqa: E402
from providers.brightdata import BrightDataProvider  # noqa: E402
from providers.config import load_config  # noqa: E402
from providers.base import (  # noqa: E402
    normalize_tiktok_post, normalize_ig_post,
    normalize_fb_post, normalize_x_tweet,
)


def _provider():
    cfg = load_config()
    token = cfg.get("apify_token")
    if not token:
        raise RuntimeError("apify_token not configured (~/.tiktok_scraper/providers.json)")
    return ApifyProvider(token=token)


def _tiktok_user(url):
    m = re.search(r"tiktok\.com/@([^/?#]+)", url)
    return m.group(1) if m else ""


def _ig_user(url):
    m = re.search(r"instagram\.com/([^/?#]+)", url)
    return m.group(1) if m else ""


def _x_user(url):
    m = re.search(r"(?:x|twitter)\.com/([^/?#]+)", url)
    u = m.group(1) if m else ""
    return "" if u in ("i", "home", "search") else u


def _shape(item):
    pid = str(item.get("video_id") or "").strip()
    url = item.get("url") or ""
    caption = item.get("caption") or ""
    if not pid and url:
        # Some FB posts have no id field, so derive one. Facebook re-issues
        # the pfbid in the URL on every fetch (same post, new link each day),
        # so the URL is NOT a stable identity. Prefer the caption text; only
        # fall back to the URL for posts with little or no caption.
        cap_key = re.sub(r"\s+", " ", caption).strip()
        if len(cap_key) >= 20:
            key = "cap:" + cap_key
        else:
            m = re.search(r"(?:story_fbid=|/posts/|/videos/|/reel/|fbid=)(pfbid\w+|\d+)", url)
            key = m.group(1) if m else url
        pid = "fb-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return {
        "post_id": pid,
        "url": url,
        "caption": item.get("caption") or "",
        "hashtags": item.get("hashtags") or [],
        "posted_at": item.get("posted_date") or "",
        # Pinned posts sit on top out of chronological order — the
        # scheduler must not treat a seen pinned post as "reached old feed".
        "pinned": bool(item.get("is_pinned")),
    }


def _tiktok_subtitle(r):
    subs = [s for s in (r.get("subtitleInformation") or []) if s.get("url")]
    for pick in (lambda s: (s.get("language_code") or "").startswith("th"),
                 lambda s: s.get("is_original_caption"),
                 lambda s: (s.get("language_code") or "").startswith("en")):
        for s in subs:
            if pick(s):
                return s["url"]
    return ""


def _window_hours(channel):
    """FB/IG are asked only for posts newer than this many hours: the time
    since this channel's last successful check + 2h overlap (min 4h, max
    3 days). Their actors return posts out of date order and pinned posts
    first, so "the latest N" missed new posts; a date window doesn't, and
    since both bill per returned post it is also cheaper."""
    from datetime import datetime, timezone
    # last_ok, not last_checked: a failed run also stamps last_checked, and
    # shrinking the window after failures would drop the posts in the gap.
    lc = channel.get("last_ok") or channel.get("last_checked") or ""
    try:
        d = datetime.fromisoformat(lc)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        h = (datetime.now(timezone.utc) - d).total_seconds() / 3600 + 2
    except ValueError:
        h = 72
    return int(min(max(h, 4), 72)) + 1


def _apify_errors(raw):
    """Split actor output into real items; 'no_items' = nothing new (fine),
    any other error item = page gone/private -> raise."""
    items = []
    for r in raw:
        err = r.get("error")
        if not err:
            items.append(r)
        elif err != "no_items":
            raise RuntimeError(f"{err}: {r.get('errorDescription') or ''}"[:300])
    return items


def collect(channel, limit=10, windowed=True):
    platform = channel["platform"]
    url = channel["profile_url"]
    prov = _provider()

    if platform == "tiktok":
        user = _tiktok_user(url)
        if not user:
            raise RuntimeError("cannot parse tiktok username")
        # apidojo~tiktok-scraper: ~$0.30/1k vs clockworks ~$1.70/1k (5.6x
        # cheaper) and returns everything we need (id/title/hashtags/postPage).
        # A profile shows up to 3 pinned clips first. Fetching only `limit`
        # items meant 32/74 channels returned nothing but old pinned clips
        # and their new clips were never seen (Nov 2026 audit) — so fetch
        # 3 extra and drop the pinned ones (flagged isPinned).
        payload = {
            "startUrls": [{"url": f"https://www.tiktok.com/@{user}"}],
            "maxItems": limit + 3,
        }
        raw = prov._call_actor_sync(ACTORS["tiktok_cheap"], payload)
        raw = [r for r in raw if not r.get("noResults")]
        if not raw:
            raise RuntimeError("TikTok ไม่พบบัญชี/ไม่มีคลิป (ชื่อบัญชีอาจเปลี่ยน ถูกปิด หรือลิงก์ในชีตผิด)")
        fresh = [r for r in raw if not r.get("isPinned")]
        out = []
        for r in fresh[:limit]:
            tags = [h for h in (r.get("hashtags") or []) if h]
            out.append({
                "post_id": str(r.get("id") or ""),
                "url": r.get("postPage") or url,
                "caption": r.get("title") or "",
                "hashtags": tags,
                "posted_at": r.get("uploadedAtFormatted") or "",
                "pinned": False,
                # TikTok's auto-subtitles = what is SAID in the clip. The AI
                # agent on sanmini reads them (a KOL can plug a broker by
                # voice without writing it in the caption).
                "subtitle_url": _tiktok_subtitle(r),
            })
        return out

    if platform == "instagram":
        user = _ig_user(url)
        if not user:
            raise RuntimeError("cannot parse instagram username")
        # Sheet links often carry ?igsh=/?fbclid=/&utm_* tails, which the
        # actor's directUrls pattern rejects (HTTP 400) — send a clean URL.
        payload = {
            "directUrls": [f"https://www.instagram.com/{user}/"],
            "resultsType": "posts",
            "resultsLimit": limit, "addParentData": False,
        }
        if windowed:
            payload["resultsLimit"] = max(limit, 25)
            payload["onlyPostsNewerThan"] = f"{_window_hours(channel)} hours"
        raw = _apify_errors(prov._call_actor_sync(ACTORS["instagram_general"], payload))
        return [_shape(normalize_ig_post(r, "apify")) for r in raw[:payload["resultsLimit"]]]

    if platform == "facebook":
        payload = {"startUrls": [{"url": url}], "resultsLimit": limit}
        if windowed:
            payload["resultsLimit"] = max(limit, 25)
            payload["onlyPostsNewerThan"] = f"{_window_hours(channel)} hours"
        raw = _apify_errors(prov._call_actor_sync(ACTORS["facebook_posts"], payload))
        return [_shape(normalize_fb_post(r, "apify")) for r in raw[:payload["resultsLimit"]]]

    if platform == "x":
        user = _x_user(url)
        if not user:
            raise RuntimeError("cannot parse x username")
        # Busy accounts tweet more than `limit` times between scans; take
        # a bigger page so nothing between two scans is skipped (tweets are
        # cheap, and already-seen ones are dropped by the scan).
        n = max(limit, 10) if windowed else limit
        payload = {
            "searchTerms": [f"from:{user}"], "maxItems": n, "sort": "Latest",
        }
        raw = prov._call_actor_sync(ACTORS["x_tweets"], payload)
        return [_shape(normalize_x_tweet(r, "apify")) for r in raw[:n]]

    raise RuntimeError(f"apify_profile: unsupported platform {platform}")
