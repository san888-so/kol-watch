"""Collector dispatch: platform -> collect(channel, limit) -> [posts].

Each post: {post_id, url, caption, hashtags(list), posted_at}.
Collectors raise on hard failure; the scheduler catches and marks the
channel blocked so it surfaces in the digest (never silently 'clean').
"""
from . import youtube_rss, apify_profile, brightdata_profile, lemon8

_APIFY = {"tiktok", "instagram", "facebook", "x"}


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
            # Apify down / over quota -> fall back to Brightdata
            if "hard limit" in str(e) or "platform-feature-disabled" in str(e):
                return brightdata_profile.collect(channel, limit=limit)
            raise
    raise RuntimeError(f"no collector for platform {platform}")
