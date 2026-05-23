"""Collector dispatch: platform -> collect(channel, limit) -> [posts].

Each post: {post_id, url, caption, hashtags(list), posted_at}.
Collectors raise on hard failure; the scheduler catches and marks the
channel blocked so it surfaces in the digest (never silently 'clean').
"""
from . import youtube_rss, apify_profile, brightdata_profile, lemon8
import db

_APIFY = {"tiktok", "instagram", "facebook", "x"}
_QUOTA = ("hard limit", "platform-feature-disabled", "monthly usage")


def collect(channel, limit=10):
    platform = channel["platform"]
    if platform == "youtube":
        return youtube_rss.collect(channel)
    if platform == "lemon8":
        return lemon8.collect(channel, limit=limit)
    if platform in _APIFY:
        try:
            return apify_profile.collect(channel, limit=limit)
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
