"""YouTube collector via public RSS feed — free, no API key, no quota.

Feed: https://www.youtube.com/feeds/videos.xml?channel_id=UCxxxx
Needs a UCxxxx channel id; resolve it once from any channel URL form and cache.
"""
import re
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")
_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_MEDIA = "{http://search.yahoo.com/mrss/}"

_CHANNEL_ID_RE = re.compile(r"/channel/(UC[\w-]{20,})")
_HTML_CID_RE = re.compile(r'"(?:channelId|externalId)"\s*:\s*"(UC[\w-]{20,})"')


def _fetch(url):
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")


def resolve_channel_id(profile_url):
    """Return UCxxxx for any YouTube channel URL form, or '' if not found."""
    m = _CHANNEL_ID_RE.search(profile_url)
    if m:
        return m.group(1)
    html = _fetch(profile_url)
    m = _HTML_CID_RE.search(html)
    return m.group(1) if m else ""


def collect(channel):
    """Return list of {post_id, url, caption, hashtags, posted_at}."""
    cid = channel.get("channel_id_cache")
    if not cid:
        cid = resolve_channel_id(channel["profile_url"])
        if not cid:
            raise RuntimeError("cannot resolve YouTube channel_id")
        channel["_resolved_channel_id"] = cid  # caller persists this

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
        desc = ""
        if group is not None:
            desc = group.findtext(f"{_MEDIA}description") or ""
        text = f"{title}\n{desc}"
        hashtags = re.findall(r"#([A-Za-z0-9_฀-๿]+)", text)
        out.append({
            "post_id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "caption": text,
            "hashtags": hashtags,
            "posted_at": published,
            "pinned": False,
        })
    return out
