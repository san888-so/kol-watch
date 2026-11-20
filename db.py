"""SQLite store for KOL competitor-keyword watch."""
import os
import json
import sqlite3
import threading
from datetime import datetime, timezone

DATA_DIR = os.environ.get(
    "KOLWATCH_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "data"),
)
DB_PATH = os.path.join(DATA_DIR, "watch.db")
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS kol (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tier TEXT,
    exclusivity TEXT,
    UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS channel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kol_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    profile_url TEXT NOT NULL,
    channel_id_cache TEXT,
    last_seen_post_id TEXT,
    last_checked TEXT,
    status TEXT DEFAULT 'ok',
    last_error TEXT,
    FOREIGN KEY(kol_id) REFERENCES kol(id),
    UNIQUE(kol_id, platform, profile_url)
);

CREATE TABLE IF NOT EXISTS seen_post (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    post_id TEXT NOT NULL,
    post_url TEXT,
    posted_at TEXT,
    caption TEXT,
    hashtags TEXT,
    scanned_at TEXT NOT NULL,
    matched INTEGER DEFAULT 0,
    FOREIGN KEY(channel_id) REFERENCES channel(id),
    UNIQUE(channel_id, post_id)
);

CREATE TABLE IF NOT EXISTS hit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    kol_name TEXT,
    platform TEXT,
    post_url TEXT,
    post_id TEXT,
    posted_at TEXT,
    matched_keyword TEXT,
    snippet TEXT,
    detected_at TEXT NOT NULL,
    handle_status TEXT DEFAULT 'new',
    notified INTEGER DEFAULT 0,
    FOREIGN KEY(channel_id) REFERENCES channel(id),
    UNIQUE(channel_id, post_id, matched_keyword)
);

CREATE TABLE IF NOT EXISTS unresolved (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kol_name TEXT,
    platform TEXT,
    raw_value TEXT,
    reason TEXT,
    seen_at TEXT NOT NULL
);

-- AI reading of each scanned post (written by the sanmini kol-ai-guard
-- agent through /api/ai/review). One row per seen_post.
CREATE TABLE IF NOT EXISTS ai_review (
    seen_post_id INTEGER PRIMARY KEY,
    level TEXT NOT NULL,              -- urgent | watch | none | skip
    competitors TEXT,                 -- JSON list
    foreign_trading INTEGER DEFAULT 0,
    mentions_webull INTEGER DEFAULT 0,
    reason TEXT,
    evidence TEXT,
    model TEXT,
    reviewed_at TEXT NOT NULL,
    alerted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS line_source (
    source_id TEXT PRIMARY KEY,
    source_type TEXT,
    last_seen TEXT NOT NULL,
    last_user TEXT,
    last_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_channel_kol ON channel(kol_id);
CREATE INDEX IF NOT EXISTS idx_seen_channel ON seen_post(channel_id, post_id);
CREATE INDEX IF NOT EXISTS idx_hit_status ON hit(handle_status, detected_at);
"""

DEFAULT_SETTINGS = {
    "run_times": "08:00,17:00",
    "auto_enabled": "1",
    "sheet_id": "",
    # gid of the "All" tab — Exclusive + Non-Exclusive KOLs in one sheet
    "sheet_gid": "401523688",
    "watchlist": json.dumps({
        "Moomoo": ["moomoo", "moo moo", "m00moo", "mo0moo", "มูมู่", "มู่มู่", "มูม", "หมูหมู"],
        "Dime": ["dime", "ไดม์", "ไดมส์", "d1me"],
        "Liberator": ["liberator", "ลิเบอเรเตอร์", "ลิเบอเรเตอร", "ลิเบอเรเตอ", "l1berator"],
    }, ensure_ascii=False),
    # Broad topic words (e.g. หุ้นนอก) that only count as a competitor hit
    # when the post does NOT mention our client's brand — a Webull-sponsored
    # post about US stocks must not raise an alert. Same JSON shape as
    # watchlist. Empty = feature off.
    "watchlist_unbranded": "{}",
    "line_token": "",
    "line_to": "",
    "line_mode": "broadcast",
    "posts_per_check": "3",
    "enable_bd_fallback": "0",
    "scan_platforms": "youtube,lemon8,tiktok,instagram,facebook,x",
    # Brand-of-our-client report (separate from competitor alert): we scan
    # every collected post for these terms and LINE-digest the matches at
    # the end of each run, grouped by KOL.
    "client_brands": json.dumps({
        "Webull": ["webull", "วีบลู", "วีบูล", "วีบุล", "webullth", "webull th"],
    }, ensure_ascii=False),
    "brand_kols_per_bubble": "5",
    # Brand digest only LINE-alerts posts dated within this many days.
    # Older "rediscoveries" (Apify returning an old post with a slightly
    # different URL form -> new post_id) are silently archived without
    # spamming the team.
    "brand_digest_max_age_days": "3",
    # Daily DCA content-status report (separate from the keyword watch).
    "dca_report_enabled": "1",
    "dca_report_time": "10:00,16:00",
    "dca_sheet_id": "1O6ls0VtoCxZHlIEsZ6J9FqJSSUuQjaPcKV_FZ6QkGgM",
}


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init():
    with _lock, _conn() as c:
        c.executescript(SCHEMA)
        # Migration: add caption/hashtags to seen_post if upgrading an old db
        cols = {r["name"] for r in c.execute("PRAGMA table_info(seen_post)")}
        if "caption" not in cols:
            c.execute("ALTER TABLE seen_post ADD COLUMN caption TEXT")
        if "hashtags" not in cols:
            c.execute("ALTER TABLE seen_post ADD COLUMN hashtags TEXT")
        if "subtitle_url" not in cols:  # TikTok auto-subtitles (speech as text)
            c.execute("ALTER TABLE seen_post ADD COLUMN subtitle_url TEXT")
        # Migration: tag every hit with a kind so we can split the
        # competitor watch from the client-brand report.
        hit_cols = {r["name"] for r in c.execute("PRAGMA table_info(hit)")}
        if "kind" not in hit_cols:
            c.execute("ALTER TABLE hit ADD COLUMN kind TEXT NOT NULL DEFAULT 'competitor'")
        # Migration: KOLs whose contract was cancelled (or who were removed
        # from the Sheet) stay in the db for history but are no longer
        # scanned. See registry.sync_from_sheet.
        kol_cols = {r["name"] for r in c.execute("PRAGMA table_info(kol)")}
        if "active" not in kol_cols:
            c.execute("ALTER TABLE kol ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        ch_cols = {r["name"] for r in c.execute("PRAGMA table_info(channel)")}
        if "active" not in ch_cols:
            c.execute("ALTER TABLE channel ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if "last_ok" not in ch_cols:   # last SUCCESSFUL fetch (FB/IG window)
            c.execute("ALTER TABLE channel ADD COLUMN last_ok TEXT")
        ai_cols = {r["name"] for r in c.execute("PRAGMA table_info(ai_review)")}
        if "why" not in ai_cols:
            c.execute("ALTER TABLE ai_review ADD COLUMN why TEXT")
        # Team verdict from the LINE card buttons (คู่แข่ง / ผ่าน).
        for col in ("verdict", "verdict_by", "verdict_at"):
            if col not in hit_cols:
                c.execute(f"ALTER TABLE hit ADD COLUMN {col} TEXT")
        for col in ("human_verdict", "human_by", "human_at"):
            if col not in ai_cols:
                c.execute(f"ALTER TABLE ai_review ADD COLUMN {col} TEXT")
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))


def get_setting(key, default=None):
    with _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key, value):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))


# --- KOL / channel ---

def upsert_kol(name, tier="", exclusivity="", active=1):
    with _lock, _conn() as c:
        r = c.execute("SELECT id FROM kol WHERE name=?", (name,)).fetchone()
        if r:
            c.execute("UPDATE kol SET tier=?, exclusivity=?, active=? WHERE id=?",
                      (tier, exclusivity, int(active), r["id"]))
            return r["id"]
        cur = c.execute(
            "INSERT INTO kol(name,tier,exclusivity,active) VALUES(?,?,?,?)",
            (name, tier, exclusivity, int(active)))
        return cur.lastrowid


def count_kols():
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM kol").fetchone()["n"]


def deactivate_kols_not_in(names):
    """Mark every KOL whose name is not in `names` inactive (no longer in
    the Sheet). Rows are kept so old posts/hits stay readable. Returns the
    names that were switched off by this call."""
    keep = set(names)
    with _lock, _conn() as c:
        rows = c.execute("SELECT id, name FROM kol WHERE active=1").fetchall()
        gone = [r for r in rows if r["name"] not in keep]
        for r in gone:
            c.execute("UPDATE kol SET active=0 WHERE id=?", (r["id"],))
        return [r["name"] for r in gone]


def upsert_channel(kol_id, platform, profile_url):
    with _lock, _conn() as c:
        r = c.execute(
            "SELECT id FROM channel WHERE kol_id=? AND platform=? AND profile_url=?",
            (kol_id, platform, profile_url)).fetchone()
        if r:
            return r["id"]
        cur = c.execute(
            "INSERT INTO channel(kol_id,platform,profile_url) VALUES(?,?,?)",
            (kol_id, platform, profile_url))
        return cur.lastrowid


def set_active_channels(ids):
    """Channels in `ids` (the links in the Sheet now) on, all others off.
    Returns how many were switched off."""
    ids = set(ids)
    with _lock, _conn() as c:
        rows = c.execute("SELECT id, active FROM channel").fetchall()
        off = 0
        for r in rows:
            want = 1 if r["id"] in ids else 0
            if r["active"] != want:
                c.execute("UPDATE channel SET active=? WHERE id=?", (want, r["id"]))
                off += (want == 0)
        return off


def list_channels(active_only=True):
    """Channels to work on. By default only those of active KOLs — a KOL
    marked cancelled in the Sheet (or removed from it) is skipped by the
    scan, the health check and the dashboard counts."""
    where = "WHERE k.active=1 AND ch.active=1" if active_only else ""
    with _conn() as c:
        rows = c.execute(f"""
            SELECT ch.*, k.name AS kol_name, k.tier AS kol_tier,
                   k.exclusivity AS kol_exclusivity, k.active AS kol_active
            FROM channel ch JOIN kol k ON k.id = ch.kol_id
            {where}
            ORDER BY k.name, ch.platform
        """).fetchall()
        return [dict(r) for r in rows]


def update_channel(channel_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [channel_id]
    with _lock, _conn() as c:
        c.execute(f"UPDATE channel SET {cols} WHERE id=?", vals)


def clear_unresolved():
    """Reset only the unresolved-links report (rebuilt every sync).

    Note: we never wipe kol/channel on re-sync — that would orphan the
    seen_post/hit FK rows and, worse, drop each channel's incremental
    cursor (last_seen_post_id), forcing a full re-fetch. Sync merges.
    """
    with _lock, _conn() as c:
        c.execute("DELETE FROM unresolved")


# --- seen posts (incremental dedup) ---

def post_seen(channel_id, post_id, caption=None):
    """True if this post was already scanned on this channel.

    Matches the post_id first. If a caption is given (20+ chars) it also
    matches a seen row on the same channel with the identical caption —
    Facebook hands out a fresh link/id for the same post every day, and
    without this every old FB post was re-counted (and re-alerted) daily."""
    with _conn() as c:
        r = c.execute(
            "SELECT 1 FROM seen_post WHERE channel_id=? AND post_id=?",
            (channel_id, post_id)).fetchone()
        if r is not None:
            return True
        cap = (caption or "").strip()
        if len(cap) >= 20:
            r = c.execute(
                "SELECT 1 FROM seen_post WHERE channel_id=? AND caption=? LIMIT 1",
                (channel_id, caption)).fetchone()
            return r is not None
        return False


def mark_seen(channel_id, post_id, post_url, posted_at,
              caption="", hashtags=None, matched=False, subtitle_url=""):
    now = datetime.now(timezone.utc).isoformat()
    tags = " ".join(hashtags or [])
    with _lock, _conn() as c:
        c.execute("""INSERT OR IGNORE INTO seen_post
            (channel_id,post_id,post_url,posted_at,caption,hashtags,scanned_at,
             matched,subtitle_url)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (channel_id, post_id, post_url, posted_at, caption, tags,
             now, 1 if matched else 0, subtitle_url or None))


def list_all_posts():
    """Every collected post with KOL/platform/date — newest posted first."""
    with _conn() as c:
        rows = c.execute("""
            SELECT k.name AS kol_name, ch.platform AS platform,
                   sp.posted_at, sp.post_url, sp.caption, sp.hashtags,
                   sp.matched, sp.scanned_at
            FROM seen_post sp
            JOIN channel ch ON ch.id = sp.channel_id
            JOIN kol k ON k.id = ch.kol_id
            ORDER BY (sp.posted_at='') ASC, sp.posted_at DESC, sp.scanned_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


# --- hits ---

def add_hit(channel_id, kol_name, platform, post_url, post_id,
            posted_at, matched_keyword, snippet, kind="competitor"):
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _conn() as c:
        try:
            cur = c.execute("""INSERT INTO hit
                (channel_id,kol_name,platform,post_url,post_id,posted_at,
                 matched_keyword,snippet,detected_at,kind)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (channel_id, kol_name, platform, post_url, post_id,
                 posted_at, matched_keyword, snippet, now, kind))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # already recorded


def list_hits(status=None, kind="competitor"):
    with _conn() as c:
        q = "SELECT * FROM hit WHERE kind=?"
        args = [kind]
        if status:
            q += " AND handle_status=?"
            args.append(status)
        q += " ORDER BY detected_at DESC LIMIT 1000"
        return [dict(r) for r in c.execute(q, args).fetchall()]


def all_seen_with_meta():
    """seen_post rows joined with KOL/platform — used by brand backfill."""
    with _conn() as c:
        rows = c.execute("""
            SELECT sp.channel_id, sp.post_id, sp.post_url, sp.posted_at,
                   sp.caption, sp.hashtags,
                   k.name AS kol_name, ch.platform
            FROM seen_post sp
            JOIN channel ch ON ch.id = sp.channel_id
            JOIN kol k ON k.id = ch.kol_id
            WHERE sp.caption IS NOT NULL AND sp.caption != ''
        """).fetchall()
        return [dict(r) for r in rows]


def unnotified_brand_hits():
    """Brand hits this run not yet sent in a digest."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM hit WHERE kind='brand' AND notified=0 "
            "ORDER BY kol_name, detected_at ASC").fetchall()]


def hit_exists_for(channel_id, matched_keyword, post_url, post_id=None):
    """True if we've already recorded a hit for this same post+keyword.

    Match on post_id OR post_url so we dedupe against either kind of
    instability: Lemon8 fallback gives a synthetic post_id that mutates
    while the URL stays the same (the profile URL), and Apify Facebook
    sometimes returns a different URL form across runs (permalink vs
    /posts/, tracking params) while the post_id we derived from pfbid
    stays the same."""
    if not post_url and not post_id:
        return False
    with _conn() as c:
        q = "SELECT 1 FROM hit WHERE channel_id=? AND matched_keyword=? AND ("
        args = [channel_id, matched_keyword]
        clauses = []
        if post_url:
            clauses.append("post_url=?"); args.append(post_url)
        if post_id:
            clauses.append("post_id=?"); args.append(post_id)
        q += " OR ".join(clauses) + ") LIMIT 1"
        return c.execute(q, args).fetchone() is not None


def unnotified_hits():
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM hit WHERE notified=0 ORDER BY detected_at ASC").fetchall()]


def mark_notified(hit_id):
    with _lock, _conn() as c:
        c.execute("UPDATE hit SET notified=1 WHERE id=?", (hit_id,))


def set_verdict(hit_ids, verdict, by):
    """Team verdict from a LINE card button. verdict: 'competitor' | 'pass'.
    Returns {kol, platform, prev: {verdict, by, at} | None} or None if the
    hits don't exist. 'pass' also files the hit as ignored; the AI review of
    the same post gets the human verdict too (training signal for later)."""
    ids = [int(x) for x in hit_ids if str(x).isdigit()]
    if not ids:
        return None
    now = datetime.now(timezone.utc).isoformat()
    marks = ",".join("?" * len(ids))
    with _lock, _conn() as c:
        rows = c.execute(f"SELECT * FROM hit WHERE id IN ({marks})",
                         ids).fetchall()
        if not rows:
            return None
        first = dict(rows[0])
        prev = None
        if first.get("verdict"):
            prev = {"verdict": first["verdict"], "by": first.get("verdict_by"),
                    "at": first.get("verdict_at")}
        status_sql = ("handle_status='ignored'" if verdict == "pass" else
                      "handle_status=CASE WHEN handle_status='ignored' "
                      "THEN 'new' ELSE handle_status END")
        c.execute(f"""UPDATE hit SET verdict=?, verdict_by=?, verdict_at=?,
                      {status_sql} WHERE id IN ({marks})""",
                  [verdict, by, now] + ids)
        c.execute("""UPDATE ai_review SET human_verdict=?, human_by=?, human_at=?
                     WHERE seen_post_id IN (SELECT id FROM seen_post
                       WHERE channel_id=? AND post_id=?)""",
                  (verdict, by, now, first["channel_id"], first["post_id"]))
        return {"kol": first["kol_name"], "platform": first["platform"],
                "keyword": first["matched_keyword"], "prev": prev}


def set_hit_status(hit_id, status):
    with _lock, _conn() as c:
        c.execute("UPDATE hit SET handle_status=? WHERE id=?", (status, hit_id))


# --- unresolved sheet entries ---

def add_unresolved(kol_name, platform, raw_value, reason):
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _conn() as c:
        c.execute("""INSERT INTO unresolved
            (kol_name,platform,raw_value,reason,seen_at) VALUES(?,?,?,?,?)""",
            (kol_name, platform, raw_value, reason, now))


def list_unresolved():
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM unresolved ORDER BY kol_name").fetchall()]


# --- LINE source capture (from /webhook) ---

def record_line_source(source_id, source_type, last_user="", last_text=""):
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _conn() as c:
        c.execute("""INSERT INTO line_source(source_id,source_type,last_seen,last_user,last_text)
            VALUES(?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
              source_type=excluded.source_type,
              last_seen=excluded.last_seen,
              last_user=excluded.last_user,
              last_text=excluded.last_text""",
            (source_id, source_type, now, last_user, last_text))


def list_line_sources(limit=20):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM line_source ORDER BY last_seen DESC LIMIT ?",
            (limit,)).fetchall()]


def export_all():
    """Every table as lists of dicts (secrets stripped) — for team data pulls."""
    c = _conn()
    c.row_factory = sqlite3.Row
    out = {}
    try:
        names = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for n in names:
            rows = [dict(r) for r in c.execute(f'SELECT * FROM "{n}"')]
            if n == "settings":
                rows = [r for r in rows if r.get("key") != "line_token"]
            out[n] = rows
    finally:
        c.close()
    return out


# ---------------------------------------------------------------- AI review
def ai_pending(limit=5, since=""):
    """Scanned posts from Exclusive KOLs that the AI agent hasn't read yet,
    newest first (fresh posts get alerted before the backlog drains)."""
    with _conn() as c:
        rows = c.execute("""
            SELECT s.id AS seen_post_id, s.post_url, s.posted_at, s.caption,
                   s.hashtags, s.scanned_at, s.subtitle_url, ch.platform,
                   k.name AS kol,
                   k.exclusivity
            FROM seen_post s
            JOIN channel ch ON ch.id = s.channel_id
            JOIN kol k ON k.id = ch.kol_id
            LEFT JOIN ai_review a ON a.seen_post_id = s.id
            WHERE a.seen_post_id IS NULL AND s.scanned_at >= ?
              AND lower(coalesce(k.exclusivity,'')) NOT LIKE '%non%'
            ORDER BY s.id DESC LIMIT ?""", (since, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        raw = d.get("hashtags") or ""
        try:
            d["hashtags"] = json.loads(raw) if raw.startswith("[") else raw.split()
        except Exception:
            d["hashtags"] = raw.split()
        out.append(d)
    return out


def ai_pending_count(since=""):
    with _conn() as c:
        return c.execute("""
            SELECT count(*) FROM seen_post s
            JOIN channel ch ON ch.id = s.channel_id
            JOIN kol k ON k.id = ch.kol_id
            LEFT JOIN ai_review a ON a.seen_post_id = s.id
            WHERE a.seen_post_id IS NULL AND s.scanned_at >= ?
              AND lower(coalesce(k.exclusivity,'')) NOT LIKE '%non%'""",
            (since,)).fetchone()[0]


def ai_post(seen_post_id):
    with _conn() as c:
        r = c.execute("""
            SELECT s.id AS seen_post_id, s.channel_id, s.post_id, s.post_url,
                   s.posted_at, s.scanned_at, s.caption, ch.platform,
                   k.name AS kol, k.exclusivity
            FROM seen_post s JOIN channel ch ON ch.id = s.channel_id
            JOIN kol k ON k.id = ch.kol_id WHERE s.id=?""",
            (seen_post_id,)).fetchone()
        return dict(r) if r else None


def save_ai_review(seen_post_id, level, competitors, foreign_trading,
                   mentions_webull, reason, evidence, model, why=""):
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _conn() as c:
        cur = c.execute("""INSERT OR IGNORE INTO ai_review
            (seen_post_id,level,competitors,foreign_trading,mentions_webull,
             reason,evidence,model,reviewed_at,why)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (seen_post_id, level, json.dumps(competitors, ensure_ascii=False),
             int(bool(foreign_trading)), int(bool(mentions_webull)),
             reason, evidence, model, now, why))
        return cur.rowcount == 1   # False = already reviewed (no re-alert)


def mark_ai_alerted(seen_post_id):
    with _lock, _conn() as c:
        c.execute("UPDATE ai_review SET alerted=1 WHERE seen_post_id=?",
                  (seen_post_id,))


def list_ai_reviews(levels=("urgent", "watch"), days=14, limit=300):
    q = ",".join("?" * len(levels))
    with _conn() as c:
        rows = c.execute(f"""
            SELECT a.*, s.post_url, s.posted_at, ch.platform, k.name AS kol,
                   substr(s.caption,1,400) AS caption
            FROM ai_review a JOIN seen_post s ON s.id = a.seen_post_id
            JOIN channel ch ON ch.id = s.channel_id
            JOIN kol k ON k.id = ch.kol_id
            WHERE a.level IN ({q})
              AND a.reviewed_at >= datetime('now', ?)
            ORDER BY a.reviewed_at DESC LIMIT ?""",
            (*levels, f"-{int(days)} days", limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["competitors"] = json.loads(d.get("competitors") or "[]")
        except Exception:
            d["competitors"] = []
        out.append(d)
    return out


def ai_stats_since(since_iso):
    with _conn() as c:
        rows = c.execute("""SELECT level, count(*) n FROM ai_review
                            WHERE reviewed_at >= ? GROUP BY level""",
                         (since_iso,)).fetchall()
    return {r["level"]: r["n"] for r in rows}
