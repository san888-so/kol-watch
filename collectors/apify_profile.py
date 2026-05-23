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
    if not pid and url:
        # Some FB posts have no id field — derive a stable unique one from
        # the URL. The identity lives in story_fbid/pfbid or a numeric path
        # segment; fall back to hashing the whole URL (query included).
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


def collect(channel, limit=10):
    platform = channel["platform"]
    url = channel["profile_url"]
    prov = _provider()

    if platform == "tiktok":
        user = _tiktok_user(url)
        if not user:
            raise RuntimeError("cannot parse tiktok username")
        # apidojo~tiktok-scraper: ~$0.30/1k vs clockworks ~$1.70/1k (5.6x
        # cheaper) and returns everything we need (id/title/hashtags/postPage).
        payload = {
            "startUrls": [{"url": f"https://www.tiktok.com/@{user}"}],
            "maxItems": limit,
        }
        raw = prov._call_actor_sync(ACTORS["tiktok_cheap"], payload)
        out = []
        for r in raw[:limit]:
            tags = [h for h in (r.get("hashtags") or []) if h]
            out.append({
                "post_id": str(r.get("id") or ""),
                "url": r.get("postPage") or url,
                "caption": r.get("title") or "",
                "hashtags": tags,
                "posted_at": r.get("uploadedAtFormatted") or "",
                "pinned": False,
            })
        return out

    if platform == "instagram":
        user = _ig_user(url)
        payload = {
            "directUrls": [url], "resultsType": "posts",
            "resultsLimit": limit, "addParentData": False,
        }
        raw = prov._call_actor_sync(ACTORS["instagram_general"], payload)
        return [_shape(normalize_ig_post(r, "apify")) for r in raw[:limit]]

    if platform == "facebook":
        payload = {"startUrls": [{"url": url}], "resultsLimit": limit}
        raw = prov._call_actor_sync(ACTORS["facebook_posts"], payload)
        return [_shape(normalize_fb_post(r, "apify")) for r in raw[:limit]]

    if platform == "x":
        user = _x_user(url)
        if not user:
            raise RuntimeError("cannot parse x username")
        payload = {
            "searchTerms": [f"from:{user}"], "maxItems": limit, "sort": "Latest",
        }
        raw = prov._call_actor_sync(ACTORS["x_tweets"], payload)
        return [_shape(normalize_x_tweet(r, "apify")) for r in raw[:limit]]

    raise RuntimeError(f"apify_profile: unsupported platform {platform}")
