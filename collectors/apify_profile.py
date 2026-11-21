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


def _fb_extra_text(r):
    """Text the caption alone misses (Nov 2026 audit):
      * a SHARED post — the KOL's own text is often empty and the promo
        is in the shared post (~68 empty-caption FB posts a week)
      * the outbound link (e.g. finansiahero.com/promotion)
      * the page owner's own comments — 'ลิงก์สมัครในคอมเมนต์' promos
    Labelled so the AI / the team can see where the words came from."""
    parts = []
    sp = r.get("sharedPost") or {}
    if isinstance(sp, dict) and (sp.get("text") or "").strip():
        who = (sp.get("pageName") or {})
        who = who.get("name") if isinstance(who, dict) else str(who or "")
        parts.append(f"[แชร์จาก {who or 'โพสต์อื่น'}] {sp['text'].strip()[:1500]}")
    link = (r.get("link") or "").strip()
    if link and "facebook.com" not in link:
        parts.append(f"[ลิงก์] {link}")
    owner = ((r.get("user") or {}).get("name") or r.get("pageName") or "").strip().lower()
    for c in (r.get("topComments") or [])[:10]:
        if not isinstance(c, dict):
            continue
        name = (c.get("profileName") or (c.get("author") or {}).get("name")
                or (c.get("user") or {}).get("name") or "")
        if owner and name and name.strip().lower() == owner \
                and (c.get("text") or "").strip():
            parts.append(f"[คอมเมนต์เจ้าของเพจ] {c['text'].strip()[:600]}")
    return "\n".join(parts)


def _tiktok_fallback(prov, user, limit):
    raw = prov._call_actor_sync(
        "igview-owner~tiktok-profile-video-scraper",
        # $0.02/item + $0.02/start (vs apidojo $0.0003) — keep it small.
        {"usernames": [user], "maxVideos": limit + 1})
    out = []
    for r in raw:
        vid = str(r.get("video_id") or "")
        if not vid or str(r.get("is_top") or 0) not in ("0", "False", "false"):
            continue  # pinned clips sit on top out of date order
        cap = r.get("title") or ""
        out.append({
            "post_id": vid,
            "url": f"https://www.tiktok.com/@{user}/video/{vid}",
            "caption": cap,
            "hashtags": re.findall(r"#([^\s#]+)", cap),
            "posted_at": r.get("create_time_date") or "",
            "pinned": False,
            "subtitle_url": None,
        })
    if not out:
        raise RuntimeError("TikTok ไม่พบบัญชี/ไม่มีคลิป (ชื่อบัญชีอาจเปลี่ยน ถูกปิด หรือลิงก์ในชีตผิด)")
    return out[:limit]


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
        fresh = [r for r in raw if not r.get("isPinned")]
        if not any("/video/" in (r.get("postPage") or "") for r in fresh):
            # apidojo answers "noResults" (or stale items without a clip
            # link) for some live accounts — @easymoneyconcept 232K and
            # @sothida.c, Nov 2026. A second actor reads them fine.
            return _tiktok_fallback(prov, user, limit)
        out = []
        for r in fresh:
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
        out = []
        for r in raw[:payload["resultsLimit"]]:
            post = _shape(normalize_fb_post(r, "apify"))
            post["extra"] = _fb_extra_text(r)  # scanned with the caption
            out.append(post)
        return out

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
        def _keep(raw):
            # A renamed/deleted account comes back as one placeholder item
            # with no id — keeping it made the channel look "ok" for months
            # (เจต's @jadenipat7 → @jadejoodfire, Sep–Nov 2026). Drop it so
            # the scan gets 0 posts and marks the channel blocked.
            out = [_shape(normalize_x_tweet(r, "apify")) for r in raw[:n]]
            return [p for p in out
                    if p["post_id"] and "/status/" in (p["url"] or "")]

        try:
            got = _keep(prov._call_actor_sync(ACTORS["x_tweets"], payload))
        except Exception:
            got = []
        if got:
            return got
        # apidojo's search died after X's 21 Nov 2026 update ("Search is
        # temporarily unavailable") and returns 0 items with SUCCEEDED, so
        # every X channel looked blocked. Fall back to kaitoeasyapi (same
        # tweet shape, ~$0.25/1K) before calling the channel empty.
        return _keep(prov._call_actor_sync(ACTORS["x_tweets_cheap"], {
            "twitterContent": f"from:{user}", "maxItems": n,
            "queryType": "Latest",
        }))

    if platform == "threads":
        m = re.search(r"threads\.(?:com|net)/@([\w.]+)", url)
        if not m:
            raise RuntimeError("cannot parse threads username")
        user = m.group(1)
        # One dataset item per profile (billed once, ~$0.002) carrying its
        # recent posts; pinned posts come first, so sort by date ourselves.
        raw = prov._call_actor_sync(
            "sourabhbgp~threads-scraper",
            {"urls": [f"https://www.threads.com/@{user}"],
             "maxResults": max(limit, 10) + 10})
        prof = next((r for r in raw if r.get("type") == "profile"), None)
        if not prof:
            raise RuntimeError("Threads ไม่พบบัญชี (ชื่ออาจเปลี่ยน ถูกปิด หรือเป็นส่วนตัว)")
        posts = sorted(prof.get("posts") or [],
                       key=lambda p: p.get("postedAt") or "", reverse=True)
        out = []
        for p in posts:
            code = p.get("postCode")
            if not code:
                continue
            text = p.get("text") or ""
            out.append({
                "post_id": "th-" + code,
                "url": f"https://www.threads.com/@{user}/post/{code}",
                "caption": text,
                "hashtags": re.findall(r"#([^\s#]+)", text),
                "posted_at": p.get("postedAt") or "",
                "pinned": False,
            })
        return out

    raise RuntimeError(f"apify_profile: unsupported platform {platform}")
