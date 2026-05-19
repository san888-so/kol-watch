"""Brightdata profile-mode fallback (used only when Apify is unavailable).

BD profile datasets return one record per profile with a list of recent
posts under a key like top_posts_data / top_videos / posts. Schemas vary
per platform, so we extract caption/url/id defensively across common
field names rather than assuming one shape.
"""
import os
import re
import sys

_PROV = os.path.join(os.path.dirname(__file__), "..", "vendor")
if _PROV not in sys.path:
    sys.path.append(_PROV)

from providers.brightdata import BrightDataProvider, DATASETS  # noqa: E402
from providers.config import load_config  # noqa: E402

_DATASET = {
    "tiktok": "tiktok_profile_by_url",
    "instagram": "instagram_posts_by_url",
    "facebook": "facebook_posts_by_profile",
    "x": "x_posts_by_url",
}
_POSTS_KEYS = ("top_posts_data", "top_videos", "posts", "latest_posts", "data")
_CAPTION_KEYS = ("description", "desc", "caption", "text", "title", "content", "message")
_URL_KEYS = ("url", "post_url", "video_url", "web_video_url", "link", "permalink")
_ID_KEYS = ("id", "post_id", "video_id", "pk", "shortcode")


def _bd():
    cfg = load_config()
    tok = cfg.get("brightdata_token")
    if not tok:
        raise RuntimeError("brightdata_token not configured")
    return BrightDataProvider(token=tok, max_wait_sec=300)


def _first(d, keys, default=""):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return default


def collect(channel, limit=10):
    platform = channel["platform"]
    ds_key = _DATASET.get(platform)
    if not ds_key:
        raise RuntimeError(f"no brightdata profile dataset for {platform}")
    bd = _bd()
    sid = bd._trigger(DATASETS[ds_key], [{"url": channel["profile_url"]}])
    bd._wait_snapshot(sid)
    raw = bd._fetch_snapshot(sid)
    if not raw:
        return []

    # Flatten: records may be the posts themselves, or a profile with a posts list.
    posts = []
    for rec in raw:
        nested = None
        for k in _POSTS_KEYS:
            if isinstance(rec.get(k), list) and rec[k]:
                nested = rec[k]
                break
        posts.extend(nested if nested else [rec])

    out = []
    for p in posts[:limit]:
        if not isinstance(p, dict):
            continue
        cap = str(_first(p, _CAPTION_KEYS))
        out.append({
            "post_id": str(_first(p, _ID_KEYS) or cap[:32]),
            "url": _first(p, _URL_KEYS) or channel["profile_url"],
            "caption": cap,
            "hashtags": re.findall(r"#([A-Za-z0-9_฀-๿]+)", cap),
            "posted_at": str(_first(p, ("create_time", "timestamp", "date_posted", "taken_at"))),
            "pinned": False,
        })
    return out
