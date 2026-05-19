"""Base provider interface — ทุก provider implement methods เหล่านี้.

Schema return: list of dicts ที่ map เข้า normalize_item ของเราได้
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional


class ProviderBase(ABC):
    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """True ถ้า API token ครบ + ใช้งานได้."""
        ...

    @abstractmethod
    def fetch_tiktok_hashtag(self, hashtag: str, limit: int = 50,
                             on_progress=None) -> list:
        """Scrape TikTok hashtag → list of post dicts (normalized)."""
        ...

    def fetch_instagram_hashtag(self, hashtag: str, limit: int = 50,
                                on_progress=None) -> list:
        raise NotImplementedError

    def fetch_facebook_hashtag(self, hashtag: str, limit: int = 50,
                               on_progress=None) -> list:
        raise NotImplementedError

    def fetch_x_hashtag(self, hashtag: str, limit: int = 50,
                       on_progress=None) -> list:
        raise NotImplementedError

    def fetch_youtube_keyword(self, keyword: str, limit: int = 50,
                              on_progress=None) -> list:
        raise NotImplementedError


def _parse_create_time(val) -> Optional[int]:
    """ส่ง unix int หรือ ISO string เข้ามา → return unix int."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        v = int(val)
        # ถ้าใหญ่เกิน 10^11 น่าจะเป็น ms → /1000
        return v // 1000 if v > 1e11 else v
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            pass
        try:
            s = val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            return int(dt.timestamp())
        except Exception:
            return None
    return None


def _enrich_temporal(item: dict) -> dict:
    """เพิ่ม posted_date + days_ago จาก create_time."""
    ct = _parse_create_time(item.get("create_time"))
    item["create_time"] = ct
    if ct:
        try:
            dt = datetime.fromtimestamp(ct)
            item["posted_date"] = dt.strftime("%Y-%m-%d")
            item["days_ago"] = (datetime.now() - dt).days
        except Exception:
            item["posted_date"] = ""
            item["days_ago"] = None
    else:
        item["posted_date"] = ""
        item["days_ago"] = None
    return item


def normalize_tiktok_post(raw: dict, source: str) -> dict:
    """Map raw API response → our internal schema.

    รองรับทั้ง Apify (nested authorMeta) และ Bright Data (flat top-level fields).
    """
    author = raw.get("authorMeta") or raw.get("author") or {}
    if isinstance(author, str):
        author = {"name": author, "uniqueId": author}

    hashtags_list = raw.get("hashtags") or []
    if hashtags_list and isinstance(hashtags_list[0], dict):
        hashtags_list = [h.get("name", "") for h in hashtags_list if h.get("name")]

    return {
        "video_id": str(raw.get("id") or raw.get("post_id") or ""),
        "author_unique_id": (
            author.get("name") or author.get("uniqueId")
            or raw.get("account_id") or raw.get("author_unique_id") or ""
        ),
        "author_nickname": (
            author.get("nickName") or author.get("nickname")
            or raw.get("profile_username") or raw.get("author_nickname") or ""
        ),
        "caption": raw.get("text") or raw.get("desc") or raw.get("description") or "",
        "create_time": raw.get("createTime") or raw.get("create_time"),
        "play_count": int(raw.get("playCount") or raw.get("play_count") or 0),
        "like_count": int(
            raw.get("diggCount") or raw.get("digg_count") or raw.get("like_count") or 0
        ),
        "share_count": int(
            raw.get("shareCount") or raw.get("share_count")
            or raw.get("num_share_count") or 0
        ),
        "save_count": int(
            raw.get("collectCount") or raw.get("collect_count") or raw.get("save_count") or 0
        ),
        "comment_count": int(raw.get("commentCount") or raw.get("comment_count") or 0),
        "follower_count": int(
            author.get("fans") or author.get("follower_count")
            or raw.get("profile_followers") or 0
        ),
        "following_count": int(author.get("following") or author.get("following_count") or 0),
        "heart_count": int(author.get("heart") or author.get("heart_count") or 0),
        "video_count": int(author.get("video") or author.get("videos_count") or 0),
        "channel_topic": (
            author.get("signature") or author.get("biography") or author.get("bio")
            or raw.get("profile_biography") or ""
        ),
        "is_verified": bool(author.get("verified") or raw.get("is_verified")),
        "is_private": bool(author.get("privateAccount") or raw.get("is_private")),
        "bio_link": author.get("bioLink") or author.get("bio_link") or "",
        "url": (
            raw.get("webVideoUrl") or raw.get("url") or raw.get("profile_url") or ""
        ),
        "hashtags": hashtags_list,
        "is_ad": bool(raw.get("isAd") or raw.get("is_ad")),
        "is_sponsored": bool(raw.get("isSponsored") or raw.get("is_sponsored")),
        "is_pinned": bool(raw.get("isPinned") or raw.get("is_pinned")),
        "location_created": (
            raw.get("locationCreated") or raw.get("location")
            or raw.get("region") or raw.get("commerce_info")
        ),
        "video_duration": int(
            (raw.get("videoMeta") or {}).get("duration") or raw.get("video_duration") or 0
        ),
        "profile_avatar": (author.get("avatar") or raw.get("profile_avatar") or ""),
        "_source": source,
        "_raw": raw,
    }


def normalize_ig_post(raw: dict, source: str) -> dict:
    """Map IG hashtag/post raw → internal schema."""
    short = raw.get("shortCode") or raw.get("shortcode") or ""
    type_ = (raw.get("type") or "").lower()
    is_reel = type_ == "video" or raw.get("productType") == "clips"
    url = raw.get("url") or (
        f"https://www.instagram.com/{'reel' if is_reel else 'p'}/{short}/" if short else ""
    )
    hashtags_list = raw.get("hashtags") or []
    if hashtags_list and isinstance(hashtags_list[0], dict):
        hashtags_list = [h.get("name", "") for h in hashtags_list if h.get("name")]
    item = {
        "video_id": str(raw.get("id") or short or ""),
        "media_code": short,
        "author_unique_id": raw.get("ownerUsername") or raw.get("owner_username") or "",
        "author_nickname": raw.get("ownerFullName") or raw.get("owner_full_name") or "",
        "caption": raw.get("caption") or "",
        "create_time": raw.get("timestamp") or raw.get("taken_at"),
        "play_count": int(raw.get("videoViewCount") or raw.get("video_view_count") or 0),
        "like_count": int(raw.get("likesCount") or raw.get("likes_count") or 0),
        "share_count": 0,
        "save_count": 0,
        "comment_count": int(raw.get("commentsCount") or raw.get("comments_count") or 0),
        "follower_count": 0,
        "following_count": 0,
        "channel_topic": "",
        "url": url,
        "hashtags": hashtags_list,
        "is_reel": is_reel,
        "profile_avatar": "",
        "platform": "instagram",
        "_source": source,
        "_raw": raw,
    }
    return _enrich_temporal(item)


def normalize_fb_post(raw: dict, source: str) -> dict:
    """Map FB raw → internal schema."""
    import re
    author = raw.get("author") or {}
    reactions_obj = raw.get("reactions") or {}
    if isinstance(reactions_obj, dict):
        like_count = int(
            (reactions_obj.get("like") or 0) + (reactions_obj.get("love") or 0)
        )
        reactions_total = sum(int(v or 0) for v in reactions_obj.values())
    else:
        like_count = int(raw.get("reactions_count") or 0)
        reactions_total = int(raw.get("reactions_count") or 0)

    # Extract username slug from URL (e.g., "kaitosushi.art" จาก "https://www.facebook.com/kaitosushi.art")
    auth_url = author.get("url") or ""
    slug = ""
    if auth_url:
        m = re.search(r"facebook\.com/(?!profile\.php)([^/?#]+)", auth_url)
        if m:
            slug = m.group(1).strip()
    # fallback to numeric ID ถ้าหา slug ไม่ได้ (เช่น URL เป็น profile.php?id=...)
    author_id = slug or str(author.get("id") or author.get("username") or "")

    item = {
        "video_id": str(raw.get("post_id") or raw.get("id") or ""),
        "author_unique_id": author_id,
        "author_id_numeric": str(author.get("id") or ""),
        "author_nickname": author.get("name") or "",
        "caption": raw.get("message") or raw.get("text") or "",
        "create_time": raw.get("timestamp") or raw.get("createdTime"),
        "play_count": 0,
        "like_count": like_count,
        "share_count": int(raw.get("reshare_count") or raw.get("shares") or 0),
        "save_count": 0,
        "comment_count": int(raw.get("comments_count") or 0),
        "reactions_breakdown": reactions_obj if isinstance(reactions_obj, dict) else {},
        "reactions_total": reactions_total,
        "follower_count": 0,
        "following_count": 0,
        "channel_topic": "",
        "url": raw.get("url") or "",
        "hashtags": [],
        "profile_avatar": author.get("profile_picture_url") or "",
        "author_url": author.get("url") or "",
        "platform": "facebook",
        "_source": source,
        "_raw": raw,
    }
    # extract hashtags from message
    import re
    if item["caption"]:
        item["hashtags"] = [m for m in re.findall(r"#([A-Za-z0-9_฀-๿]+)", item["caption"])]
    return _enrich_temporal(item)


def normalize_x_tweet(raw: dict, source: str) -> dict:
    """Map X/Twitter tweet → internal schema.
    map retweet_count → share_count (similar concept)
    map view_count → play_count (impressions)
    """
    import re
    author = raw.get("author") or raw.get("user") or {}
    if isinstance(author, str):
        author = {"userName": author}

    text = (
        raw.get("fullText") or raw.get("full_text")
        or raw.get("text") or raw.get("rawText") or ""
    )
    hashtags_list = []
    raw_hashtags = raw.get("hashtags") or raw.get("entities", {}).get("hashtags") or []
    for h in raw_hashtags:
        if isinstance(h, dict):
            hashtags_list.append(h.get("text") or h.get("tag") or h.get("name") or "")
        elif isinstance(h, str):
            hashtags_list.append(h)
    if not hashtags_list and text:
        hashtags_list = [m for m in re.findall(r"#([A-Za-z0-9_฀-๿]+)", text)]

    tweet_url = (
        raw.get("twitterUrl") or raw.get("url")
        or raw.get("permalink") or ""
    )
    item = {
        "video_id": str(
            raw.get("id") or raw.get("id_str") or raw.get("rest_id") or raw.get("tweet_id") or ""
        ),
        "author_unique_id": (
            author.get("userName") or author.get("screen_name")
            or author.get("username") or ""
        ),
        "author_nickname": author.get("name") or "",
        "caption": text,
        "create_time": (
            raw.get("createdAt") or raw.get("created_at")
            or raw.get("createdAtIso") or raw.get("date")
        ),
        "play_count": int(
            raw.get("viewCount") or raw.get("view_count")
            or raw.get("impressionCount") or 0
        ),
        "like_count": int(
            raw.get("likeCount") or raw.get("favoriteCount")
            or raw.get("favorite_count") or raw.get("likes") or 0
        ),
        "share_count": int(
            raw.get("retweetCount") or raw.get("retweet_count") or 0
        ),
        "save_count": int(raw.get("bookmarkCount") or raw.get("bookmark_count") or 0),
        "comment_count": int(
            raw.get("replyCount") or raw.get("reply_count")
            or raw.get("replies") or 0
        ),
        "quote_count": int(raw.get("quoteCount") or raw.get("quote_count") or 0),
        "follower_count": int(
            author.get("followers") or author.get("followersCount")
            or author.get("followers_count") or 0
        ),
        "following_count": int(
            author.get("following") or author.get("friendsCount")
            or author.get("friends_count") or 0
        ),
        "channel_topic": author.get("description") or author.get("bio") or "",
        "url": tweet_url,
        "hashtags": [h for h in hashtags_list if h],
        "is_verified": bool(author.get("verified") or author.get("isVerified")),
        "is_private": bool(author.get("isPrivate") or author.get("private")),
        "profile_avatar": (
            author.get("profilePicture") or author.get("profile_image_url")
            or author.get("avatarUrl") or ""
        ),
        "author_url": author.get("twitterUrl") or author.get("url") or "",
        "platform": "x",
        "_source": source,
        "_raw": raw,
    }
    return _enrich_temporal(item)


def normalize_youtube_video(raw: dict, source: str) -> dict:
    """Map YouTube video → internal schema. รองรับทั้ง Apify + BD."""
    title = raw.get("title") or ""
    desc = raw.get("description") or raw.get("text") or ""
    duration = raw.get("duration") or raw.get("length") or raw.get("video_length") or 0
    # handle_name (BD) มี @ prefix หรือไม่
    handle = (
        raw.get("handle_name") or raw.get("channelHandle")
        or raw.get("youtuber") or raw.get("channelName")
        or raw.get("channel") or raw.get("author") or ""
    )
    if isinstance(handle, str):
        handle = handle.lstrip("@")
    hashtags = raw.get("hashtags") or raw.get("tags") or []
    if hashtags is None:
        hashtags = []
    item = {
        "video_id": str(
            raw.get("video_id") or raw.get("id") or raw.get("videoId")
            or raw.get("shortcode") or ""
        ),
        "author_unique_id": handle,
        "author_nickname": (
            raw.get("channelName") or raw.get("channel") or handle or ""
        ),
        "caption": title + ("\n" + desc if desc and desc != title else ""),
        "create_time": (
            raw.get("date_posted") or raw.get("date") or raw.get("uploadDate")
            or raw.get("publishedAt") or raw.get("published_at")
        ),
        "play_count": int(
            raw.get("views") or raw.get("viewCount") or raw.get("view_count") or 0
        ),
        "like_count": int(raw.get("likes") or raw.get("likeCount") or 0),
        "share_count": 0,
        "save_count": 0,
        "comment_count": int(
            raw.get("num_comments") or raw.get("commentsCount")
            or raw.get("comment_count") or raw.get("commentCount") or 0
        ),
        "follower_count": int(
            raw.get("subscribers") or raw.get("numberOfSubscribers")
            or raw.get("subscriberCount") or 0
        ),
        "following_count": 0,
        "channel_topic": raw.get("channelDescription") or "",
        "url": raw.get("url") or raw.get("videoUrl") or "",
        "hashtags": hashtags if isinstance(hashtags, list) else [],
        "video_duration": int(duration) if isinstance(duration, (int, float)) else 0,
        "video_duration_text": str(duration) if not isinstance(duration, (int, float)) else "",
        "profile_avatar": (
            raw.get("avatar_img_channel") or raw.get("channelAvatarUrl")
            or raw.get("channelImage") or ""
        ),
        "author_url": raw.get("channel_url") or raw.get("channelUrl") or "",
        "thumbnail_url": (
            raw.get("preview_image") or raw.get("thumbnailUrl") or raw.get("thumbnail") or ""
        ),
        "youtube_category": raw.get("category") or "",
        "is_sponsored": bool(raw.get("is_sponsored")),
        "is_verified": bool(raw.get("verified")),
        "transcript": raw.get("transcript") or "",
        "platform": "youtube",
        "_source": source,
        "_raw": raw,
    }
    return _enrich_temporal(item)
