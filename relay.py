"""Posts pushed in by an outside fetcher for channels no scraper can read
logged out — e.g. ม้าเฉียว's FB page machaodohoon is age-restricted, so every
Apify / Bright Data actor gets "not_available" (28 Nov 2026). A logged-in
browser on sanmini (relay/fb_relay.py) reads the page a few times a day and
POSTs what it saw to /api/relay_posts.

The hourly scan reads those posts through collectors.collect() like any
other collector, so keyword matching, LINE cards and the AI reviewer all
work as usual. Which channels come from here: setting `relay_channels`, a
JSON list of profile URLs. When the fetcher stops sending (machine off,
account logged out) the channel shows under "เช็กไม่ได้" with the reason.
"""
import json
from datetime import datetime, timedelta, timezone

import db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_post (
    profile_url TEXT NOT NULL,
    post_id TEXT NOT NULL,
    url TEXT,
    posted_at TEXT,
    caption TEXT,
    hashtags TEXT,
    received_at TEXT NOT NULL,
    PRIMARY KEY (profile_url, post_id)
);
"""
_ready = False

# Posts the scan is handed on every round: a few days back, so a round that
# fails or is stopped mid-way still gets them next time (seen posts are
# dropped by the scan's own dedupe, so handing them again costs nothing).
_HAND_DAYS = 3
_KEEP_DAYS = 14


def _init():
    global _ready
    if not _ready:
        with db._lock, db._conn() as c:
            c.executescript(_SCHEMA)
        _ready = True


def norm(url):
    u = (url or "").strip().lower().split("?")[0].rstrip("/")
    for pre in ("https://", "http://"):
        if u.startswith(pre):
            u = u[len(pre):]
    for pre in ("www.", "m.", "web."):
        if u.startswith(pre):
            u = u[len(pre):]
    return u


def relay_urls():
    try:
        v = json.loads(db.get_setting("relay_channels", "[]") or "[]")
    except Exception:
        return set()
    return {norm(u) for u in v if isinstance(u, str) and u.strip()} \
        if isinstance(v, list) else set()


def is_relay(channel):
    return norm(channel.get("profile_url")) in relay_urls()


def _statuses():
    try:
        v = json.loads(db.get_setting("relay_status", "{}") or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def push(payload):
    """payload: {"profile_url", "ok": bool, "error": str, "source": str,
    "posts": [{"post_id", "url", "posted_at", "caption", "hashtags"}]}"""
    _init()
    key = norm(payload.get("profile_url"))
    if not key:
        return {"error": "profile_url required"}, 400
    if key not in relay_urls():
        return {"error": f"{key} is not in setting relay_channels"}, 400
    posts = payload.get("posts") or []
    if not isinstance(posts, list) or len(posts) > 500:
        return {"error": "posts: list of at most 500"}, 400
    now = datetime.now(timezone.utc)
    added = 0
    with db._lock, db._conn() as c:
        for p in posts:
            pid = str((p or {}).get("post_id") or "").strip()
            if not pid:
                continue
            tags = [t for t in (p.get("hashtags") or []) if isinstance(t, str)]
            cur = c.execute(
                "INSERT OR IGNORE INTO relay_post(profile_url, post_id, url,"
                " posted_at, caption, hashtags, received_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (key, pid, p.get("url") or "", p.get("posted_at") or "",
                 p.get("caption") or "", json.dumps(tags, ensure_ascii=False),
                 now.isoformat()))
            added += cur.rowcount
        c.execute("DELETE FROM relay_post WHERE received_at < ?",
                  ((now - timedelta(days=_KEEP_DAYS)).isoformat(),))
    st = _statuses()
    st[key] = {"at": now.isoformat(), "ok": bool(payload.get("ok", True)),
               "error": str(payload.get("error") or "")[:300],
               "sent": len(posts), "new": added,
               "source": str(payload.get("source") or "")[:60]}
    db.set_setting("relay_status", json.dumps(st, ensure_ascii=False))
    return {"ok": True, "received": len(posts), "new": added}, 200


def collect(channel):
    """Posts the outside fetcher sent lately. Raises (→ "เช็กไม่ได้") when it
    has not reported in time or its last report was a failure. Every
    message starts with "relay:" so the scan does not retry it."""
    _init()
    key = norm(channel.get("profile_url"))
    st = _statuses().get(key)
    if not st:
        raise RuntimeError("relay: ยังไม่มีข้อมูลจากตัวดึงบน sanmini "
                           "(รอบัญชี FB สำรองล็อกอิน)")
    try:
        max_h = float(db.get_setting("relay_max_age_h", "10") or "10")
    except ValueError:
        max_h = 10.0
    try:
        at = datetime.fromisoformat(st["at"])
        age_h = (datetime.now(timezone.utc) - at).total_seconds() / 3600
    except Exception:
        age_h = 1e9
    if age_h > max_h:
        raise RuntimeError(f"relay: ตัวดึงบน sanmini ไม่ได้ส่งข้อมูลมา "
                           f"{age_h:.0f} ชม. (เครื่องดับหรืองานหยุด)")
    if not st.get("ok"):
        raise RuntimeError("relay: " + (st.get("error") or "ตัวดึงบน sanmini ล้มเหลว"))
    since = (datetime.now(timezone.utc) - timedelta(days=_HAND_DAYS)).isoformat()
    with db._conn() as c:
        rows = c.execute(
            "SELECT post_id, url, posted_at, caption, hashtags FROM relay_post"
            " WHERE profile_url=? AND received_at>=? ORDER BY posted_at DESC",
            (key, since)).fetchall()
    out = []
    for r in rows:
        try:
            tags = json.loads(r["hashtags"] or "[]")
        except Exception:
            tags = []
        out.append({"post_id": r["post_id"], "url": r["url"] or "",
                    "posted_at": r["posted_at"] or "",
                    "caption": r["caption"] or "", "hashtags": tags})
    return out


def status():
    """For /api/relay_status: per relay channel, last report + stored posts."""
    _init()
    st = _statuses()
    with db._conn() as c:
        counts = {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT profile_url, COUNT(*), MAX(posted_at) FROM relay_post"
            " GROUP BY profile_url")}
    return {"channels": [
        {"url": u, "last_report": st.get(u), "stored": counts.get(u, (0, None))[0],
         "newest_post": counts.get(u, (0, None))[1]}
        for u in sorted(relay_urls())]}
