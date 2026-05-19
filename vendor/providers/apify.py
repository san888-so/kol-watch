"""Apify provider — call public actors via REST API.

ไม่แตะ actors ที่ทีมเก่าใช้: เราเรียก public actors (clockworks/tiktok-scraper ฯลฯ)
ด้วย API token ใหม่ของเราเอง → consume credit จาก account เราอย่างเดียว

Reference: https://docs.apify.com/api/v2#/reference/actors/run-actor-synchronously
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import requests

from providers.base import (
    ProviderBase,
    normalize_fb_post,
    normalize_ig_post,
    normalize_tiktok_post,
    normalize_x_tweet,
    normalize_youtube_video,
)


# Actors to use — เลือก popular + reliable + ราคาดี
ACTORS = {
    "tiktok_general": "clockworks~tiktok-scraper",        # $1.70/1K, ครอบคลุมที่สุด
    "tiktok_cheap": "apidojo~tiktok-scraper",             # $0.30/1K
    "tiktok_hashtag": "clockworks~tiktok-hashtag-scraper",
    "tiktok_comments": "clockworks~tiktok-comments-scraper",
    "tiktok_profile": "clockworks~tiktok-profile-scraper",

    "instagram_general": "apify~instagram-scraper",
    "instagram_hashtag": "apify~instagram-hashtag-scraper",
    "instagram_profile": "apify~instagram-profile-scraper",

    "facebook_posts": "apify~facebook-posts-scraper",
    "facebook_search": "danek~facebook-search-ppr",

    "x_tweets": "apidojo~tweet-scraper",                   # $0.40/1K
    "x_tweets_cheap": "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest",

    "youtube_general": "streamers~youtube-scraper",
}

API_BASE = "https://api.apify.com/v2"


class ApifyProvider(ProviderBase):
    name = "apify"

    def __init__(self, token: str, timeout_sec: int = 300):
        self.token = token
        self.timeout = timeout_sec
        self.session = requests.Session()

    def is_configured(self) -> bool:
        return bool(self.token) and self.token.startswith("apify_api")

    def _call_actor_sync(self, actor_id: str, payload: dict,
                         on_progress: Optional[Callable] = None) -> list:
        """Run actor synchronously and return dataset items.

        Uses run-sync-get-dataset-items endpoint — blocks until done or timeout.
        """
        url = f"{API_BASE}/acts/{actor_id}/run-sync-get-dataset-items"
        params = {"token": self.token, "timeout": self.timeout}
        if on_progress:
            on_progress("starting", 0, 0)
        try:
            r = self.session.post(url, json=payload, params=params,
                                  timeout=self.timeout + 30)
        except requests.exceptions.Timeout:
            raise RuntimeError(f"Apify actor {actor_id} timeout")
        if r.status_code != 200 and r.status_code != 201:
            raise RuntimeError(
                f"Apify {actor_id} HTTP {r.status_code}: {r.text[:200]}"
            )
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"Apify {actor_id} non-JSON response")
        if not isinstance(data, list):
            raise RuntimeError(f"Apify {actor_id} unexpected response: {data}")
        if on_progress:
            on_progress("done", len(data), len(data))
        return data

    # --- TikTok ---

    def fetch_tiktok_hashtag(self, hashtag: str, limit: int = 50,
                             on_progress=None) -> list:
        tag = hashtag.lstrip("#").strip()
        actor = ACTORS["tiktok_general"]
        payload = {
            "hashtags": [tag],
            "resultsPerPage": limit,
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
            "shouldDownloadSubtitles": False,
            "shouldDownloadAvatars": False,
            "shouldDownloadMusicCovers": False,
            "shouldDownloadSlideshowImages": False,
            "scrapeRelatedVideos": False,
            "excludePinnedPosts": False,
            "profileSorting": "latest",
        }
        raw = self._call_actor_sync(actor, payload, on_progress=on_progress)
        return [normalize_tiktok_post(r, "apify:" + actor) for r in raw[:limit]]

    def fetch_tiktok_profile(self, username: str, on_progress=None) -> dict:
        """Return follower_count, channel_topic (bio), last5_posts + aggregates."""
        actor = ACTORS["tiktok_profile"]
        payload = {
            "profiles": [username.lstrip("@")],
            "resultsPerPage": 5,
            "profileScrapeSections": ["videos"],
            "profileSorting": "latest",
        }
        raw = self._call_actor_sync(actor, payload, on_progress=on_progress)
        if not raw:
            return {}
        author = (raw[0].get("authorMeta") or {})
        last5 = []
        for r in raw[:5]:
            last5.append({
                "play_count": int(r.get("playCount") or 0),
                "like_count": int(r.get("diggCount") or 0),
                "share_count": int(r.get("shareCount") or 0),
                "save_count": int(r.get("collectCount") or 0),
                "comment_count": int(r.get("commentCount") or 0),
                "create_time": r.get("createTime"),
            })
        n = len(last5)
        avg_view = sum(p["play_count"] for p in last5) / n if n else 0
        avg_eng = sum(
            p["like_count"] + p["share_count"] + p["save_count"] + p["comment_count"]
            for p in last5
        ) / n if n else 0
        er5 = (avg_eng / avg_view * 100) if avg_view else 0
        return {
            "follower_count": int(author.get("fans", 0) or 0),
            "following_count": int(author.get("following", 0) or 0),
            "heart_count": int(author.get("heart", 0) or 0),
            "channel_topic": author.get("signature") or "",
            "bio_link": author.get("bioLink") or "",
            "is_verified": bool(author.get("verified")),
            "is_private": bool(author.get("privateAccount")),
            "last5_posts": last5,
            "last5_avg_view": round(avg_view, 1),
            "last5_avg_engagement": round(avg_eng, 1),
            "last5_engagement_rate": round(er5, 3),
        }

    def fetch_tiktok_comments(self, video_url: str, limit: int = 20,
                              on_progress=None) -> list:
        actor = ACTORS["tiktok_comments"]
        payload = {
            "postURLs": [video_url],
            "commentsPerPost": limit,
        }
        raw = self._call_actor_sync(actor, payload, on_progress=on_progress)
        out = []
        for c in raw[:limit]:
            out.append({
                "text": c.get("text") or "",
                "like_count": int(c.get("diggCount") or c.get("likes") or 0),
                "user": (c.get("user") or {}).get("uniqueId") or c.get("username") or "",
                "create_time": c.get("createTime") or c.get("create_time"),
            })
        return out

    # --- Instagram ---

    def fetch_instagram_hashtag(self, hashtag: str, limit: int = 50,
                                on_progress=None) -> list:
        tag = hashtag.lstrip("#").strip()
        actor = ACTORS["instagram_hashtag"]
        payload = {
            "hashtags": [tag],
            "resultsLimit": limit,
        }
        raw = self._call_actor_sync(actor, payload, on_progress=on_progress)
        return [normalize_ig_post(r, "apify:" + actor) for r in raw[:limit]]

    def fetch_instagram_profile(self, username: str, on_progress=None) -> dict:
        actor = ACTORS["instagram_profile"]
        payload = {"usernames": [username.lstrip("@")]}
        raw = self._call_actor_sync(actor, payload, on_progress=on_progress)
        if not raw:
            return {}
        p = raw[0]
        # extract last 5 from latestPosts/posts
        posts = p.get("latestPosts") or p.get("posts") or []
        last5 = []
        for r in posts[:5]:
            last5.append({
                "play_count": int(r.get("videoViewCount") or 0),
                "like_count": int(r.get("likesCount") or 0),
                "share_count": 0,
                "save_count": 0,
                "comment_count": int(r.get("commentsCount") or 0),
                "create_time": r.get("timestamp"),
            })
        n = len(last5)
        avg_view = sum(p["play_count"] for p in last5) / n if n else 0
        avg_eng = sum(p["like_count"] + p["comment_count"] for p in last5) / n if n else 0
        er5 = (avg_eng / avg_view * 100) if avg_view else 0
        return {
            "follower_count": int(p.get("followersCount") or 0),
            "following_count": int(p.get("followsCount") or 0),
            "channel_topic": p.get("biography") or "",
            "bio_link": p.get("externalUrl") or "",
            "is_verified": bool(p.get("verified")),
            "is_private": bool(p.get("private")),
            "last5_posts": last5,
            "last5_avg_view": round(avg_view, 1),
            "last5_avg_engagement": round(avg_eng, 1),
            "last5_engagement_rate": round(er5, 3),
        }

    def fetch_instagram_comments(self, post_url: str, limit: int = 15,
                                  on_progress=None) -> list:
        actor = "apify~instagram-comment-scraper"
        payload = {"directUrls": [post_url], "resultsLimit": limit}
        try:
            raw = self._call_actor_sync(actor, payload, on_progress=on_progress)
        except Exception:
            return []
        out = []
        for c in raw[:limit]:
            out.append({
                "text": c.get("text") or "",
                "like_count": int(c.get("likesCount") or 0),
                "user": (c.get("ownerUsername") or c.get("username") or ""),
                "create_time": c.get("timestamp"),
            })
        return out

    # --- Facebook ---

    def fetch_facebook_hashtag(self, hashtag: str, limit: int = 50,
                               on_progress=None) -> list:
        tag = hashtag.lstrip("#").strip()
        actor = ACTORS["facebook_search"]
        payload = {
            "query": f"#{tag}",
            "search_type": "posts",
            "max_posts": limit,
        }
        raw = self._call_actor_sync(actor, payload, on_progress=on_progress)
        return [normalize_fb_post(r, "apify:" + actor) for r in raw[:limit]]

    def fetch_facebook_profile(self, profile_url: str, on_progress=None) -> dict:
        """Fetch page/profile metadata + last 5 posts via apify/facebook-posts-scraper."""
        if not profile_url:
            return {}
        actor = ACTORS["facebook_posts"]
        payload = {
            "startUrls": [{"url": profile_url}],
            "resultsLimit": 5,
        }
        try:
            raw = self._call_actor_sync(actor, payload, on_progress=on_progress)
        except Exception:
            return {}
        if not raw:
            return {}

        # first item may include page-level info; otherwise infer from posts
        first = raw[0] if raw else {}
        page_meta = first.get("pageMeta") or first.get("page") or {}

        # Build last 5 posts (FB posts-scraper schema: likes, shares, viewsCount, ...)
        last5 = []
        for r in raw[:5]:
            like = int(
                r.get("likes")
                or r.get("reactionLikeCount")
                or r.get("reactions_count")
                or 0
            )
            shares = int(r.get("shares") or r.get("reshare_count") or 0)
            views = int(r.get("viewsCount") or r.get("video_view_count") or 0)
            comments = int(r.get("comments_count") or r.get("commentsCount") or 0)
            last5.append({
                "play_count": views,
                "like_count": like,
                "share_count": shares,
                "save_count": 0,
                "comment_count": comments,
                "create_time": r.get("timestamp") or r.get("time"),
            })

        n = len(last5)
        nonzero_views = [p["play_count"] for p in last5 if p["play_count"] > 0]
        avg_view = sum(nonzero_views) / len(nonzero_views) if nonzero_views else 0
        avg_eng = sum(
            p["like_count"] + p["share_count"] + p["comment_count"]
            for p in last5
        ) / n if n else 0
        er5 = (avg_eng / avg_view * 100) if avg_view else 0

        # Author/page metadata — try multiple field paths
        user = first.get("user") or first.get("author") or {}
        follower = int(
            page_meta.get("followers_count")
            or page_meta.get("fan_count")
            or user.get("followers_count")
            or user.get("likes_count")
            or 0
        )
        bio = (
            page_meta.get("about")
            or page_meta.get("bio")
            or page_meta.get("description")
            or user.get("about")
            or ""
        )

        return {
            "follower_count": follower,
            "following_count": 0,
            "channel_topic": bio,
            "is_verified": bool(page_meta.get("is_verified") or user.get("is_verified")),
            "is_private": False,
            "last5_posts": last5,
            "last5_avg_view": round(avg_view, 1),
            "last5_avg_engagement": round(avg_eng, 1),
            "last5_engagement_rate": round(er5, 3),
        }

    # --- X (Twitter) ---

    def fetch_x_hashtag(self, hashtag: str, limit: int = 50,
                       on_progress=None) -> list:
        tag = hashtag.lstrip("#").strip()
        actor = ACTORS["x_tweets"]  # apidojo — reliable for hashtag search
        payload = {
            "searchTerms": [f"#{tag}"],
            "maxItems": limit,
            "sort": "Latest",
        }
        raw = self._call_actor_sync(actor, payload, on_progress=on_progress)
        return [normalize_x_tweet(r, "apify:" + actor) for r in raw[:limit]]

    def fetch_x_profile(self, username: str, on_progress=None) -> dict:
        """Fetch X user's recent 5 tweets + profile stats."""
        actor = ACTORS["x_tweets"]
        payload = {
            "searchTerms": [f"from:{username.lstrip('@')}"],
            "maxItems": 5,
            "sort": "Latest",
        }
        try:
            raw = self._call_actor_sync(actor, payload, on_progress=on_progress)
        except Exception:
            return {}
        if not raw:
            return {}
        # Author info from first tweet
        author = (raw[0].get("author") or raw[0].get("user") or {})
        last5 = []
        for r in raw[:5]:
            last5.append({
                "play_count": int(r.get("viewCount") or 0),
                "like_count": int(r.get("likeCount") or r.get("favoriteCount") or 0),
                "share_count": int(r.get("retweetCount") or 0),
                "save_count": int(r.get("bookmarkCount") or 0),
                "comment_count": int(r.get("replyCount") or 0),
                "create_time": r.get("createdAt") or r.get("created_at"),
            })
        n = len(last5)
        nonzero_v = [p["play_count"] for p in last5 if p["play_count"] > 0]
        avg_view = sum(nonzero_v) / len(nonzero_v) if nonzero_v else 0
        avg_eng = sum(
            p["like_count"] + p["share_count"] + p["comment_count"]
            for p in last5
        ) / n if n else 0
        er5 = (avg_eng / avg_view * 100) if avg_view else 0
        return {
            "follower_count": int(author.get("followers") or author.get("followersCount") or 0),
            "following_count": int(author.get("following") or author.get("friendsCount") or 0),
            "channel_topic": author.get("description") or author.get("bio") or "",
            "is_verified": bool(author.get("verified") or author.get("isVerified")),
            "last5_posts": last5,
            "last5_avg_view": round(avg_view, 1),
            "last5_avg_engagement": round(avg_eng, 1),
            "last5_engagement_rate": round(er5, 3),
        }

    # --- YouTube ---

    def fetch_youtube_keyword(self, keyword: str, limit: int = 50,
                              on_progress=None) -> list:
        actor = ACTORS["youtube_general"]
        payload = {
            "searchKeywords": keyword,
            "maxResults": limit,
        }
        raw = self._call_actor_sync(actor, payload, on_progress=on_progress)
        return [normalize_youtube_video(r, "apify:" + actor) for r in raw[:limit]]

    def fetch_youtube_channel(self, channel: str, on_progress=None) -> dict:
        """Fetch YouTube channel's recent 5 videos + sub count."""
        actor = ACTORS["youtube_general"]
        # YouTube actor accepts startUrls with channel URL
        channel_url = channel if channel.startswith("http") else f"https://www.youtube.com/{channel}"
        payload = {
            "startUrls": [{"url": channel_url}],
            "maxResults": 5,
        }
        try:
            raw = self._call_actor_sync(actor, payload, on_progress=on_progress)
        except Exception:
            return {}
        if not raw:
            return {}
        first = raw[0]
        last5 = []
        for r in raw[:5]:
            last5.append({
                "play_count": int(r.get("viewCount") or r.get("views") or 0),
                "like_count": int(r.get("likes") or 0),
                "share_count": 0,
                "save_count": 0,
                "comment_count": int(r.get("commentsCount") or r.get("commentCount") or 0),
                "create_time": r.get("date") or r.get("uploadDate"),
            })
        n = len(last5)
        nonzero_v = [p["play_count"] for p in last5 if p["play_count"] > 0]
        avg_view = sum(nonzero_v) / len(nonzero_v) if nonzero_v else 0
        avg_eng = sum(
            p["like_count"] + p["comment_count"] for p in last5
        ) / n if n else 0
        er5 = (avg_eng / avg_view * 100) if avg_view else 0
        return {
            "follower_count": int(first.get("numberOfSubscribers") or 0),
            "following_count": 0,
            "channel_topic": first.get("channelDescription") or "",
            "last5_posts": last5,
            "last5_avg_view": round(avg_view, 1),
            "last5_avg_engagement": round(avg_eng, 1),
            "last5_engagement_rate": round(er5, 3),
        }
