"""YouTube collector.

Two paths:
  * If YOUTUBE_API_KEY is set -> YouTube Data API v3. Works from datacenter
    IPs (Railway), reliable, free quota (~10k units/day; we use ~90/day).
  * Otherwise -> public RSS feed (free, no key) — fine from a residential
    IP but YouTube blocks the channel-page/feed scrape from datacenter IPs
    (404/500), which is why RSS-only failed on Railway.
"""
import json
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")
_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_MEDIA = "{http://search.yahoo.com/mrss/}"
_API = "https://www.googleapis.com/youtube/v3"

_CHANNEL_ID_RE = re.compile(r"/channel/(UC[\w-]{20,})")
_HTML_CID_RE = re.compile(r'"(?:channelId|externalId)"\s*:\s*"(UC[\w-]{20,})"')
_HANDLE_RE = re.compile(r"/@([^/?#]+)")
_USER_RE = re.compile(r"/user/([^/?#]+)")
_CUSTOM_RE = re.compile(r"/c/([^/?#]+)")


def _key():
    return os.environ.get("YOUTUBE_API_KEY", "").strip()


def _fetch(url):
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")


def _api(path, **params):
    params["key"] = _key()
    data = _fetch(f"{_API}/{path}?{urlencode(params)}")
    return json.loads(data)


# --- channel id resolution ---

def _channel_id_from_url(url):
    m = _CHANNEL_ID_RE.search(url)
    return m.group(1) if m else ""


def _resolve_via_api(url):
    cid = _channel_id_from_url(url)
    if cid:
        return cid
    m = _HANDLE_RE.search(url)
    if m:
        r = _api("channels", part="id", forHandle=m.group(1))
        items = r.get("items") or []
        if items:
            return items[0]["id"]
    m = _USER_RE.search(url)
    if m:
        r = _api("channels", part="id", forUsername=m.group(1))
        items = r.get("items") or []
        if items:
            return items[0]["id"]
    # /c/custom or anything else -> search (costs more quota, rare)
    m = _CUSTOM_RE.search(url) or _HANDLE_RE.search(url)
    q = m.group(1) if m else url
    r = _api("search", part="snippet", q=q, type="channel", maxResults=1)
    items = r.get("items") or []
    if items:
        return items[0]["snippet"]["channelId"]
    return ""


def _resolve_via_html(url):
    cid = _channel_id_from_url(url)
    if cid:
        return cid
    m = _HTML_CID_RE.search(_fetch(url))
    return m.group(1) if m else ""


def resolve_channel_id(profile_url):
    return _resolve_via_api(profile_url) if _key() else _resolve_via_html(profile_url)


# --- recent videos ---

def _collect_via_api(channel, cid, limit):
    # The uploads playlist id is the channel id with UC -> UU.
    uploads = "UU" + cid[2:]
    r = _api("playlistItems", part="snippet", playlistId=uploads,
             maxResults=max(1, min(limit, 50)))
    out = []
    for it in r.get("items", []):
        sn = it.get("snippet") or {}
        vid = (sn.get("resourceId") or {}).get("videoId") or ""
        if not vid:
            continue
        text = f"{sn.get('title','')}\n{sn.get('description','')}"
        out.append({
            "post_id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "caption": text,
            "hashtags": re.findall(r"#([A-Za-z0-9_฀-๿]+)", text),
            "posted_at": sn.get("publishedAt", ""),
            "pinned": False,
        })
    return out


def _collect_via_rss(cid):
    feed = _fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
    root = ET.fromstring(feed)
    out = []
    for entry in root.findall(f"{_ATOM}entry"):
        vid = entry.findtext(f"{_YT}videoId") or ""
        if not vid:
            continue
        title = entry.findtext(f"{_ATOM}title") or ""
        published = entry.findtext(f"{_ATOM}published") or ""
        group = entry.find(f"{_MEDIA}group")
        desc = group.findtext(f"{_MEDIA}description") if group is not None else ""
        text = f"{title}\n{desc or ''}"
        out.append({
            "post_id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "caption": text,
            "hashtags": re.findall(r"#([A-Za-z0-9_฀-๿]+)", text),
            "posted_at": published,
            "pinned": False,
        })
    return out


def collect(channel, limit=15):
    cid = channel.get("channel_id_cache")
    if not cid:
        cid = resolve_channel_id(channel["profile_url"])
        if not cid:
            raise RuntimeError("cannot resolve YouTube channel_id")
        channel["_resolved_channel_id"] = cid  # caller persists this
    if _key():
        return _collect_via_api(channel, cid, limit)
    return _collect_via_rss(cid)
