"""Comments under KOL posts — pull, store, keyword-check (San, 24 Nov 2026).

Exclusive KOLs only (setting `comments_scope`), every post since
`comments_from` (default 2026-01-01). Each post is fetched twice at most:
pass 1 once the post is 2 days old (the backfill takes every older post at
once), pass 2 at 7 days old if pass 1 ran before the post was 6 days old.

Collectors (no LLM):
  facebook  apify~facebook-comments-scraper   $0.0017 / comment
  instagram apify~instagram-comment-scraper   $0.0021 / comment
  tiktok    clockworks~tiktok-comments-scraper $0.00075 / comment
  threads   futurizerush~threads-replies-scraper ~$0.0025 / reply
  youtube   YouTube Data API commentThreads (free, 1 unit / 100 comments)
  x         off — Twitter search has been down since 21 Nov 2026

Every comment is run through the competitor watchlist. A comment written by
the KOL themselves (author = the channel owner) that names a competitor is a
breach → hit kind 'comment', status 'new', LINE text when the comment is
recent. Followers naming a competitor → hit kind 'comment', status
'follower' (dashboard only, no LINE).

Money guards: a daily cap (`comments_daily_usd`, default 150) checked before
every batch, a per-run cap passed to Apify (maxTotalChargeUsd), and any
"monthly usage limit" answer from Apify pauses the whole job and says so.
"""
import json
import os
import re
import threading
import time
import traceback
import urllib.parse
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

import db
import matcher

try:
    from zoneinfo import ZoneInfo
    TH_TZ = ZoneInfo("Asia/Bangkok")
except Exception:  # pragma: no cover
    TH_TZ = timezone(timedelta(hours=7))

API = "https://api.apify.com/v2"
ACTORS = {
    "facebook": "apify~facebook-comments-scraper",
    "instagram": "apify~instagram-comment-scraper",
    "tiktok": "clockworks~tiktok-comments-scraper",
    "threads": "futurizerush~threads-replies-scraper",
}
# posts per Apify run and how many runs of one platform at once
BATCH = {"facebook": 40, "instagram": 40, "tiktok": 25, "threads": 20,
         "youtube": 50}
PARALLEL = {"facebook": 8, "instagram": 2, "tiktok": 4, "threads": 2,
            "youtube": 1}
# rough $ per post, only to show "cost left" on the dashboard
EST_PER_POST = {"facebook": 0.017, "instagram": 0.0085, "tiktok": 0.006,
                "threads": 0.004, "youtube": 0.0, "x": 0.0004}
RUN_CAP_USD = {"facebook": 12, "instagram": 4, "tiktok": 3, "threads": 2}
DEFAULT_PLATFORMS = "youtube,instagram,tiktok,threads,facebook"

SCHEMA = """
CREATE TABLE IF NOT EXISTS comment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seen_post_id INTEGER NOT NULL,
    platform TEXT,
    comment_id TEXT NOT NULL,
    parent_id TEXT,
    author TEXT,
    author_id TEXT,
    is_kol INTEGER DEFAULT 0,
    pinned INTEGER DEFAULT 0,
    text TEXT,
    posted_at TEXT,
    likes INTEGER,
    scraped_at TEXT NOT NULL,
    matched TEXT,
    UNIQUE(seen_post_id, comment_id)
);
CREATE INDEX IF NOT EXISTS idx_comment_post ON comment(seen_post_id);
CREATE INDEX IF NOT EXISTS idx_comment_kol ON comment(is_kol);
CREATE INDEX IF NOT EXISTS idx_comment_matched ON comment(matched);
CREATE TABLE IF NOT EXISTS comment_fetch (
    seen_post_id INTEGER PRIMARY KEY,
    platform TEXT,
    passes INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 0,
    last_fetched TEXT,
    n_comments INTEGER DEFAULT 0,
    status TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_cfetch_status ON comment_fetch(status);
CREATE TABLE IF NOT EXISTS comment_spend (
    day TEXT NOT NULL,
    platform TEXT NOT NULL,
    usd REAL DEFAULT 0,
    runs INTEGER DEFAULT 0,
    posts INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    PRIMARY KEY(day, platform)
);
"""

STATE = {"running": False, "paused_reason": "", "current": {},
         "last_error": "", "started_at": None}
_lock = threading.Lock()


class QuotaError(RuntimeError):
    pass


# ───────────────────────────── settings / db ─────────────────────────────

def init():
    with db._lock, db._conn() as c:
        c.executescript(SCHEMA)
        # a deploy/restart mid-batch leaves rows 'running' — make them due again
        c.execute("UPDATE comment_fetch SET status='retry' WHERE status='running'")


def _s(key, default):
    v = db.get_setting(key, None)
    return default if v in (None, "") else v


def enabled():
    return _s("comments_enabled", "0") == "1"


def platforms():
    return [p.strip() for p in _s("comments_platforms", DEFAULT_PLATFORMS).split(",")
            if p.strip()]


def daily_cap():
    try:
        return float(_s("comments_daily_usd", "150"))
    except ValueError:
        return 150.0


def _today():
    return datetime.now(TH_TZ).strftime("%Y-%m-%d")


def spent_today():
    with db._conn() as c:
        r = c.execute("SELECT COALESCE(SUM(usd),0) s FROM comment_spend WHERE day=?",
                      (_today(),)).fetchone()
        return float(r["s"] or 0)


def _add_spend(platform, usd, posts, comments):
    with db._lock, db._conn() as c:
        c.execute("""INSERT INTO comment_spend(day,platform,usd,runs,posts,comments)
            VALUES(?,?,?,?,?,?) ON CONFLICT(day,platform) DO UPDATE SET
            usd=usd+excluded.usd, runs=runs+1, posts=posts+excluded.posts,
            comments=comments+excluded.comments""",
                  (_today(), platform, float(usd or 0), 1, posts, comments))


def _scope_sql():
    scope = _s("comments_scope", "exclusive")
    if scope == "all":
        return ""
    return " AND lower(COALESCE(k.exclusivity,'')) NOT LIKE '%non%'"


def due_posts(platform, limit, now=None, count=False):
    """Posts whose comments should be fetched now (pass 1 or pass 2)."""
    now = now or datetime.now(timezone.utc)
    frm = _s("comments_from", "2026-01-01")[:10]
    p1_before = (now - timedelta(days=2)).isoformat()
    p2_before = (now - timedelta(days=7)).isoformat()
    with db._conn() as c:
        cols = "COUNT(*) n" if count else """sp.id seen_post_id, sp.post_id, sp.post_url, sp.posted_at,
                   ch.id channel_id, ch.platform, ch.profile_url,
                   ch.channel_id_cache, k.name kol_name,
                   cf.passes, cf.last_fetched"""
        tail = "" if count else "ORDER BY sp.posted_at DESC LIMIT ?"
        args = [platform, frm, p1_before, p2_before] + ([] if count else [limit])
        rows = c.execute(f"""
            SELECT {cols}
            FROM seen_post sp
            JOIN channel ch ON ch.id = sp.channel_id
            JOIN kol k ON k.id = ch.kol_id
            LEFT JOIN comment_fetch cf ON cf.seen_post_id = sp.id
            WHERE k.active=1 AND ch.active=1 AND ch.platform=?
              {_scope_sql()}
              AND substr(sp.posted_at,1,10) >= ?
              AND sp.post_url LIKE 'http%'
              AND (
                (cf.seen_post_id IS NULL AND sp.posted_at <= ?)
                OR (cf.status IN ('retry','error') AND cf.attempts < 3)
                OR (cf.status='ok' AND cf.passes=1 AND sp.posted_at <= ?
                    AND julianday(cf.last_fetched) - julianday(sp.posted_at) < 6)
              )
            {tail}""", args).fetchall()
        if count:
            return rows[0]["n"]
        return [dict(r) for r in rows]


def due_count(platform):
    return due_posts(platform, 0, count=True)


def _claim(posts):
    with db._lock, db._conn() as c:
        for p in posts:
            c.execute("""INSERT INTO comment_fetch(seen_post_id,platform,status,attempts)
                VALUES(?,?,'running',1) ON CONFLICT(seen_post_id) DO UPDATE SET
                status='running', attempts=attempts+1""",
                      (p["seen_post_id"], p["platform"]))


def _finish(post, n, status="ok", error=""):
    now = datetime.now(timezone.utc).isoformat()
    with db._lock, db._conn() as c:
        if status == "ok":
            c.execute("""UPDATE comment_fetch SET status='ok', passes=passes+1,
                attempts=0, last_fetched=?, n_comments=?, error='' WHERE seen_post_id=?""",
                      (now, n, post["seen_post_id"]))
        else:
            # a quota stop is not the post's fault: don't use up its 3 tries
            back = 1 if status == "retry" else 0
            c.execute("""UPDATE comment_fetch SET status=?, error=?,
                attempts=MAX(attempts-?,0) WHERE seen_post_id=?""",
                      (status, error[:300], back, post["seen_post_id"]))


# ───────────────────────────── handles / KOL detection ─────────────────────

def _handle(platform, url):
    url = url or ""
    pats = {
        "tiktok": r"tiktok\.com/@([^/?#]+)",
        "instagram": r"instagram\.com/([^/?#]+)",
        "threads": r"threads\.(?:net|com)/@([^/?#]+)",
        "facebook": r"facebook\.com/(?:profile\.php\?id=(\d+)|([^/?#]+))",
    }
    m = re.search(pats.get(platform, r"$^"), url, re.I)
    if not m:
        return ""
    return next((g for g in m.groups() if g), "").lower()


def _threads_url(post):
    h = _handle("threads", post["profile_url"])
    code = (post["post_url"] or "").rstrip("/").split("/")[-1]
    return f"https://www.threads.com/@{h}/post/{code}" if h and code else ""


# ───────────────────────────── Apify ─────────────────────────────

def _token():
    return os.environ.get("APIFY_TOKEN", "").strip()


def _quota_text(t):
    t = (t or "").lower()
    return any(q in t for q in ("monthly usage", "hard limit", "usage limit",
                                "platform-feature-disabled", "not enough credit",
                                "insufficient credit"))


def _apify(actor, payload, cap_usd, timeout=3600):
    h = {"Authorization": "Bearer " + _token()}
    r = requests.post(f"{API}/acts/{actor}/runs", params={"maxTotalChargeUsd": cap_usd},
                      json=payload, headers=h, timeout=60)
    if r.status_code >= 400:
        if _quota_text(r.text) or r.status_code == 402:
            raise QuotaError(r.text[:300])
        raise RuntimeError(f"Apify {r.status_code}: {r.text[:200]}")
    run = r.json()["data"]
    t0 = time.time()
    while run["status"] in ("READY", "RUNNING"):
        if time.time() - t0 > timeout:
            requests.post(f"{API}/actor-runs/{run['id']}/abort", headers=h, timeout=30)
            raise RuntimeError("Apify run timed out")
        time.sleep(10)
        run = requests.get(f"{API}/actor-runs/{run['id']}", headers=h, timeout=30).json()["data"]
    items, off = [], 0
    while True:
        rr = requests.get(f"{API}/datasets/{run['defaultDatasetId']}/items",
                          params={"clean": 1, "offset": off, "limit": 5000},
                          headers=h, timeout=120)
        page = rr.json() if rr.ok else []
        items += page
        if len(page) < 5000:
            break
        off += 5000
    usd = float(run.get("usageTotalUsd") or 0)
    if run["status"] != "SUCCEEDED" and not items:
        msg = run.get("statusMessage") or run["status"]
        if _quota_text(msg):
            raise QuotaError(msg)
        raise RuntimeError(f"Apify run {run['status']}: {msg}"[:300])
    return items, usd


# ───────────────────────────── per-platform shaping ───────────────────────

def _iso_ts(v):
    try:
        return datetime.fromtimestamp(int(v), timezone.utc).isoformat()
    except Exception:
        return ""


def _fetch_apify(platform, posts):
    """Run one batch, return ({seen_post_id: [comment dicts]}, usd, errors)."""
    # One link can belong to two posts (a collab post under two KOLs, or the
    # same post saved twice) — Apify rejects duplicate links, so send each
    # once and give its comments to every post that has it.
    by_url, urls = {}, []
    for p in posts:
        u = _threads_url(p) if platform == "threads" else p["post_url"]
        if not u:
            continue
        key = _norm_url(platform, u)
        if key not in by_url:
            by_url[key] = []
            urls.append(u)
        by_url[key].append(p)
    if not urls:
        return {}, 0.0, {}
    since = None
    # pass 2: only comments newer than the first fetch (FB charges per comment)
    if all(p.get("passes") for p in posts):
        since = min(p["last_fetched"] for p in posts)[:10]
    if platform == "facebook":
        payload = {"startUrls": [{"url": u} for u in urls],
                   "includeNestedComments": True, "viewOption": "RANKED_UNFILTERED"}
        if since:
            payload["onlyCommentsNewerThan"] = since
    elif platform == "instagram":
        payload = {"directUrls": urls, "resultsLimit": 1000,
                   "includeNestedComments": True}
    elif platform == "tiktok":
        payload = {"postURLs": urls, "commentsPerPost": 1000,
                   "maxRepliesPerComment": 50}
    elif platform == "threads":
        payload = {"post_urls": [{"url": u} for u in urls], "max_replies": 300,
                   "include_nested_replies": True}
    else:
        raise RuntimeError(platform)
    items, usd = _apify(ACTORS[platform], payload, RUN_CAP_USD[platform])
    out, errors = {p["seen_post_id"]: [] for p in posts}, {}
    for it in items:
        src = (it.get("inputUrl") or it.get("input") or it.get("submittedVideoUrl")
               or it.get("source_post_url") or it.get("facebookUrl")
               or it.get("postUrl") or it.get("videoWebUrl") or it.get("url") or "")
        for p in by_url.get(_norm_url(platform, src)) or []:
            if it.get("error"):
                errors[p["seen_post_id"]] = str(it.get("errorDescription") or it.get("error"))
                continue
            c = _shape(platform, it, p)   # per post: "is it the KOL" differs per owner
            if c:
                out[p["seen_post_id"]].append(c)
                for rep in (it.get("replies") or []) if platform == "instagram" else []:
                    rc = _shape(platform, rep, p, parent=c["comment_id"])
                    if rc:
                        out[p["seen_post_id"]].append(rc)
    return out, usd, errors


def _norm_url(platform, u):
    if platform == "facebook":   # permalink.php?story_fbid=… — keep the query
        return (u or "").strip().rstrip("/").lower().replace("://m.", "://www.")
    u = (u or "").split("?")[0].rstrip("/").lower()
    u = u.replace("://m.", "://www.").replace("threads.net", "threads.com")
    if platform == "tiktok":
        m = re.search(r"/video/(\d+)", u)
        return "tt:" + m.group(1) if m else u
    if platform == "instagram":
        m = re.search(r"/(?:p|reel|reels|tv)/([^/]+)", u)
        return "ig:" + m.group(1) if m else u
    if platform == "threads":
        return "th:" + u.split("/")[-1]
    return u


def _shape(platform, it, post, parent=None):
    owner = _handle(platform, post["profile_url"])
    if platform == "facebook":
        cid = str(it.get("commentId") or it.get("id") or "")
        author = it.get("profileName") or (it.get("author") or {}).get("name") or ""
        aurl = it.get("profileUrl") or (it.get("author") or {}).get("url") or ""
        aid = str(it.get("profileId") or (it.get("author") or {}).get("id") or "")
        is_kol = bool((aid and aid == str(it.get("facebookId") or ""))
                      or (owner and _handle("facebook", aurl) == owner))
        return _row(cid, it.get("replyToCommentId") or parent, author, aid or aurl,
                    is_kol, 0, it.get("text"), it.get("date"), it.get("likesCount"))
    if platform == "instagram":
        u = it.get("ownerUsername") or (it.get("owner") or {}).get("username") or ""
        return _row(it.get("id"), parent, u, u, owner and u.lower() == owner, 0,
                    it.get("text"), it.get("timestamp"), it.get("likesCount"))
    if platform == "tiktok":
        u = it.get("uniqueId") or ""
        return _row(it.get("cid"), it.get("repliesToId") or parent, u, it.get("uid") or u,
                    owner and u.lower() == owner, it.get("pinnedByAuthor"),
                    it.get("text"), it.get("createTimeISO") or _iso_ts(it.get("createTime")),
                    it.get("diggCount"))
    if platform == "threads":
        if it.get("item_type") and it.get("item_type") != "reply":
            return None
        u = it.get("author_username") or ""
        return _row(it.get("reply_id") or it.get("post_code"), parent, u,
                    it.get("author_user_id") or u, owner and u.lower() == owner, 0,
                    it.get("text_content"), it.get("created_at"), it.get("like_count"))
    return None


def _row(cid, parent, author, author_id, is_kol, pinned, text, posted_at, likes):
    cid = str(cid or "").strip()
    if not cid:
        return None
    try:
        likes = int(likes or 0)
    except (TypeError, ValueError):
        likes = 0
    return {"comment_id": cid, "parent_id": str(parent or ""), "author": author or "",
            "author_id": str(author_id or ""), "is_kol": 1 if is_kol else 0,
            "pinned": 1 if pinned else 0, "text": (text or "")[:4000],
            "posted_at": posted_at or "", "likes": likes}


# ───────────────────────────── YouTube (free) ─────────────────────────────

def _yt_key():
    return os.environ.get("YOUTUBE_API_KEY", "").strip()


def _yt(path, **params):
    params["key"] = _yt_key()
    u = f"https://www.googleapis.com/youtube/v3/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(u, timeout=30) as r:
        return json.load(r)


def _fetch_youtube(posts):
    out, errors = {p["seen_post_id"]: [] for p in posts}, {}
    ids = {p["post_id"]: p for p in posts if p.get("post_id")}
    counts = {}
    vid_list = list(ids)
    for i in range(0, len(vid_list), 50):
        try:
            d = _yt("videos", part="statistics", id=",".join(vid_list[i:i + 50]))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            if "quotaExceeded" in body or "dailyLimitExceeded" in body:
                raise QuotaError("YouTube API quota: " + body[:200])
            raise
        for it in d.get("items", []):
            counts[it["id"]] = int(it.get("statistics", {}).get("commentCount", 0) or 0)
    for vid, p in ids.items():
        if not counts.get(vid):
            continue   # 0 comments, comments off, or video gone
        owner = p.get("channel_id_cache") or ""
        tok = ""
        try:
            for _ in range(50):   # up to 5,000 threads per video
                params = dict(part="snippet,replies", videoId=vid, maxResults=100,
                              textFormat="plainText")
                if tok:
                    params["pageToken"] = tok
                d = _yt("commentThreads", **params)
                for th in d.get("items", []):
                    top = th["snippet"]["topLevelComment"]
                    out[p["seen_post_id"]].append(_yt_row(top, None, owner))
                    for rep in (th.get("replies") or {}).get("comments", []):
                        out[p["seen_post_id"]].append(_yt_row(rep, top["id"], owner))
                tok = d.get("nextPageToken")
                if not tok:
                    break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            if "quotaExceeded" in body or "dailyLimitExceeded" in body:
                raise QuotaError("YouTube API quota: " + body[:200])
            if "commentsDisabled" in body or e.code == 404:
                continue   # comments turned off / video gone = nothing to read
            errors[p["seen_post_id"]] = f"YouTube {e.code}: {body[:150]}"
        except Exception as e:
            errors[p["seen_post_id"]] = str(e)[:200]
    return out, 0.0, errors


def _yt_row(c, parent, owner):
    s = c["snippet"]
    aid = (s.get("authorChannelId") or {}).get("value", "")
    return _row(c["id"], parent, s.get("authorDisplayName"), aid,
                bool(owner and aid == owner), 0, s.get("textOriginal") or s.get("textDisplay"),
                s.get("publishedAt"), s.get("likeCount"))


# ───────────────────────────── store + match ─────────────────────────────

def _save(post, comments, wl, unbranded_wl, alert_cutoff):
    """Insert comments, keyword-check them, record hits. Returns (#new, #hits)."""
    now = datetime.now(timezone.utc).isoformat()
    new_rows, hits = [], 0
    with db._conn() as c:
        have = {r[0] for r in c.execute(
            "SELECT comment_id FROM comment WHERE seen_post_id=?", (post["seen_post_id"],))}
    for cm in comments:
        if not cm or cm["comment_id"] in have:
            continue
        have.add(cm["comment_id"])
        found = matcher.scan(cm["text"], [], watchlist=wl) if cm["text"] else []
        # Broad words (หุ้นนอก …) count only on a KOL comment that sells
        # something and does NOT name our client — same rule as posts.
        if cm["is_kol"] and not found and unbranded_wl and cm["text"] \
                and not matcher.scan(cm["text"], [], watchlist=matcher.get_client_brands()):
            broad = matcher.scan(cm["text"], [], watchlist=unbranded_wl)
            if broad and matcher.promo_signal(cm["text"], []):
                found = broad
        cm["matched"] = ",".join(dict.fromkeys(f["keyword"] for f in found))
        cm["_found"] = found
        new_rows.append(cm)
    if not new_rows:
        return 0, 0
    with db._lock, db._conn() as c:
        c.executemany("""INSERT OR IGNORE INTO comment(seen_post_id,platform,comment_id,
            parent_id,author,author_id,is_kol,pinned,text,posted_at,likes,scraped_at,matched)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(post["seen_post_id"], post["platform"], r["comment_id"], r["parent_id"],
              r["author"], r["author_id"], r["is_kol"], r["pinned"], r["text"],
              r["posted_at"], r["likes"], now, r["matched"]) for r in new_rows])
    for r in new_rows:
        for f in r["_found"]:
            who = ("KOL ตอบเอง" if r["is_kol"] else f"ผู้ติดตาม {r['author']}")
            pin = " · ปักหมุด" if r["pinned"] else ""
            snippet = f"💬 [{who}{pin}] {f['snippet']}"[:500]
            hid = db.add_hit(post["channel_id"], post["kol_name"], post["platform"],
                             post["post_url"], f"{post['post_id']}#c{r['comment_id']}",
                             r["posted_at"] or post["posted_at"], f["keyword"], snippet,
                             kind="comment")
            if not hid:
                continue
            hits += 1
            recent = (r["posted_at"] or "") >= alert_cutoff
            with db._lock, db._conn() as c:
                c.execute("UPDATE hit SET handle_status=?, notified=? WHERE id=?",
                          ("new" if r["is_kol"] else "follower",
                           0 if (r["is_kol"] and recent) else 1, hid))
            if r["is_kol"] and recent:
                _line_alert(post, r, f, hid)
    return len(new_rows), hits


def _line_alert(post, r, f, hid):
    try:
        import notify_line
        ok, _ = notify_line.send_texts([
            f"💬 KOL ตอบคอมเมนต์พูดถึงคู่แข่ง\n"
            f"KOL: {post['kol_name']} ({post['platform']})\n"
            f"คำ: {f['keyword']}\n"
            f"“{(r['text'] or '')[:300]}”\n"
            f"โพสต์: {post['post_url']}"])
        if ok:
            db.mark_notified(hid)
    except Exception:
        traceback.print_exc()


# ───────────────────────────── batch + loop ─────────────────────────────

def run_batch(platform, posts, claim=True):
    if claim:
        _claim(posts)
    wl = matcher.get_watchlist()
    unbranded = matcher.get_unbranded_watchlist()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    try:
        if platform == "youtube":
            res, usd, errors = _fetch_youtube(posts)
        else:
            res, usd, errors = _fetch_apify(platform, posts)
    except QuotaError:
        for p in posts:
            _finish(p, 0, "retry", "quota")
        raise
    except Exception as e:
        for p in posts:
            _finish(p, 0, "error", str(e))
        raise
    total_new = total_hits = 0
    for p in posts:
        comments = res.get(p["seen_post_id"], [])
        err = errors.get(p["seen_post_id"], "")
        n, h = _save(p, comments, wl, unbranded, cutoff)
        total_new += n
        total_hits += h
        # "no items / private" = no public comments: done, not an error
        if err and "private" not in err.lower() and "no_items" not in err.lower() \
                and "empty" not in err.lower() and "not found" not in err.lower() \
                and "isn't available" not in err.lower():
            _finish(p, len(comments), "error", err)
        else:
            _finish(p, len(comments))
    _add_spend(platform, usd, len(posts), total_new)
    return {"posts": len(posts), "comments": total_new, "hits": total_hits, "usd": usd}


_HEADROOM = {"at": 0, "usd": None}


def apify_headroom():
    """$ left before the Apify account's monthly usage limit (cached 2 min).
    None when it can't be read (then we don't block on it)."""
    if time.time() - _HEADROOM["at"] < 120:
        return _HEADROOM["usd"]
    try:
        d = requests.get(f"{API}/users/me/limits",
                         headers={"Authorization": "Bearer " + _token()},
                         timeout=20).json()["data"]
        _HEADROOM["usd"] = (float(d["limits"]["maxMonthlyUsageUsd"])
                            - float(d["current"]["monthlyUsageUsd"]))
    except Exception:
        _HEADROOM["usd"] = None
    _HEADROOM["at"] = time.time()
    return _HEADROOM["usd"]


def reserve_usd():
    """Apify credit kept back for the hourly post scans — comments never
    eat into it. Raise the Apify monthly limit and comments carry on by
    themselves at the next 15-minute look."""
    try:
        return float(_s("comments_apify_reserve_usd", "300"))
    except ValueError:
        return 300.0


def _work_platform(platform, stop):
    while not stop.is_set() and enabled() and not STATE["paused_reason"]:
        if platform != "youtube" and spent_today() >= daily_cap():
            STATE["current"][platform] = "ถึงเพดานเงินวันนี้แล้ว"
            return
        if platform != "youtube":
            room = apify_headroom()
            if room is not None and room < reserve_usd():
                STATE["current"][platform] = (
                    f"รอเครดิต Apify (เหลือ ${room:.0f} · กันไว้ให้สแกนโพสต์ ${reserve_usd():.0f})")
                return
        with _lock:   # claim under a lock so two workers never take the same posts
            posts = due_posts(platform, BATCH[platform])
            if posts:
                _claim(posts)
        if not posts:
            STATE["current"][platform] = "ครบแล้ว"
            return
        STATE["current"][platform] = f"กำลังดึง {len(posts)} โพสต์"
        try:
            r = run_batch(platform, posts, claim=False)
            print(f"[comments] {platform}: {r}", flush=True)
        except QuotaError as e:
            print(f"[comments] quota {platform}: {e}", flush=True)
            if platform == "youtube":   # free quota resets daily; others carry on
                STATE["current"][platform] = "โควตา YouTube วันนี้หมด ต่อพรุ่งนี้"
                return
            STATE["paused_reason"] = f"{platform}: {e}"[:300]
            db.set_setting("comments_enabled", "0")
            db.set_setting("comments_paused_reason", STATE["paused_reason"])
            _notify_pause(STATE["paused_reason"])
            return
        except Exception as e:
            STATE["last_error"] = f"{platform}: {e}"[:300]
            traceback.print_exc()
            time.sleep(30)


def _notify_pause(reason):
    try:
        import notify_line
        notify_line.send_texts([f"⏸ ดึงคอมเมนต์หยุดชั่วคราว: เครดิต Apify ไม่พอ\n{reason[:200]}"])
    except Exception:
        traceback.print_exc()


_WORKERS = {}          # platform -> number of live worker threads
_STOP = threading.Event()


def _worker(platform, stop):
    try:
        _work_platform(platform, stop)
    except Exception:
        traceback.print_exc()
    finally:
        with _lock:
            _WORKERS[platform] -= 1
            STATE["running"] = any(_WORKERS.values())


def ensure_workers(stop=None):
    """Top every switched-on platform up to its PARALLEL worker count. Safe to
    call any time: a platform switched on mid-round starts right away, one
    whose queue was empty picks up newly due posts."""
    stop = stop or _STOP
    started = 0
    with _lock:
        for p in platforms():
            if p not in BATCH:
                continue
            for _ in range(PARALLEL[p] - _WORKERS.get(p, 0)):
                _WORKERS[p] = _WORKERS.get(p, 0) + 1
                threading.Thread(target=_worker, args=(p, stop), daemon=True).start()
                started += 1
        STATE["running"] = any(_WORKERS.values())
    if started:
        STATE["started_at"] = datetime.now(timezone.utc).isoformat()
    return started


def worker_thread(stop):
    global _STOP
    _STOP = stop
    init()
    time.sleep(20)
    while not stop.is_set():
        try:
            if enabled():
                STATE["paused_reason"] = ""
                ensure_workers(stop)
        except Exception:
            traceback.print_exc()
        stop.wait(900)   # new posts become due over time — look again in 15 min


def start_now(stop=None):
    """Start (or top up) the workers right away (Start button / config)."""
    STATE["paused_reason"] = ""
    db.set_setting("comments_paused_reason", "")
    return ensure_workers(stop)


# ───────────────────────────── API ─────────────────────────────

def status():
    with db._conn() as c:
        per = {r["platform"]: dict(r) for r in c.execute("""
            SELECT platform, COUNT(*) posts, SUM(status='ok') done,
                   SUM(status IN ('error')) errors, SUM(n_comments) comments
            FROM comment_fetch GROUP BY platform""")}
        spend = {r["platform"]: dict(r) for r in c.execute("""
            SELECT platform, SUM(usd) usd, SUM(comments) comments
            FROM comment_spend GROUP BY platform""")}
        tot = c.execute("""SELECT COUNT(*) n, SUM(is_kol) kol,
            SUM(matched IS NOT NULL AND matched!='') matched FROM comment""").fetchone()
        hits = c.execute("""SELECT SUM(handle_status!='follower') kol_hits,
            SUM(handle_status='follower') follower_hits FROM hit WHERE kind='comment'""").fetchone()
    plats = []
    for p in ["youtube", "instagram", "tiktok", "threads", "facebook", "x"]:
        on = p in platforms() and p in BATCH
        left = due_count(p) if on else 0
        a, s = per.get(p, {}), spend.get(p, {})
        plats.append({"platform": p, "on": on, "done": a.get("done") or 0,
                      "errors": a.get("errors") or 0, "left": left,
                      "comments": s.get("comments") or 0,
                      "usd": round(s.get("usd") or 0, 2),
                      "est_left_usd": round(left * EST_PER_POST.get(p, 0), 0),
                      "now": STATE["current"].get(p, "")})
    return {"enabled": enabled(), "running": STATE["running"],
            "paused_reason": STATE["paused_reason"] or _s("comments_paused_reason", ""),
            "last_error": STATE["last_error"], "scope": _s("comments_scope", "exclusive"),
            "from": _s("comments_from", "2026-01-01"), "daily_cap": daily_cap(),
            "spent_today": round(spent_today(), 2), "platforms": plats,
            "apify_headroom": apify_headroom(), "reserve": reserve_usd(),
            "total_comments": tot["n"] or 0, "kol_comments": tot["kol"] or 0,
            "matched_comments": tot["matched"] or 0,
            "kol_hits": hits["kol_hits"] or 0, "follower_hits": hits["follower_hits"] or 0}


def listing(q):
    """Comment search: ?who=kol|all&hit=1&kol=&platform=&text=&page="""
    g = lambda k, d="": (q.get(k) or [d])[0]
    where, args = ["1=1"], []
    if g("who", "kol") == "kol":
        where.append("cm.is_kol=1")
    if g("hit") == "1":
        where.append("cm.matched IS NOT NULL AND cm.matched!=''")
    if g("kol"):
        where.append("k.name=?"); args.append(g("kol"))
    if g("platform"):
        where.append("cm.platform=?"); args.append(g("platform"))
    if g("text"):
        where.append("cm.text LIKE ?"); args.append("%" + g("text") + "%")
    if g("post"):
        where.append("cm.seen_post_id=?"); args.append(int(g("post")))
    page = max(1, int(g("page", "1") or 1))
    size = min(200, int(g("size", "50") or 50))
    with db._conn() as c:
        sql = f"""FROM comment cm JOIN seen_post sp ON sp.id=cm.seen_post_id
            JOIN channel ch ON ch.id=sp.channel_id JOIN kol k ON k.id=ch.kol_id
            WHERE {' AND '.join(where)}"""
        n = c.execute("SELECT COUNT(*) " + sql, args).fetchone()[0]
        rows = c.execute(f"""SELECT cm.*, k.name kol, sp.post_url, sp.posted_at post_at
            {sql} ORDER BY cm.posted_at DESC LIMIT ? OFFSET ?""",
                         args + [size, (page - 1) * size]).fetchall()
    return {"total": n, "page": page, "rows": [dict(r) for r in rows]}


def hits(status=None):
    with db._conn() as c:
        q = "SELECT * FROM hit WHERE kind='comment'"
        a = []
        if status:
            q += " AND handle_status=?"; a.append(status)
        q += " ORDER BY posted_at DESC LIMIT 1000"
        return [dict(r) for r in c.execute(q, a)]


def configure(payload):
    for k in ("comments_enabled", "comments_platforms", "comments_daily_usd",
              "comments_scope", "comments_from", "comments_apify_reserve_usd"):
        if k in payload:
            db.set_setting(k, str(payload[k]))
    if enabled():
        start_now(_STOP)
    return status()
