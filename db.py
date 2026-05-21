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
    "run_times": "08:00,16:00",
    "auto_enabled": "1",
    "sheet_id": "",
    "watchlist": json.dumps({
        "Moomoo": ["moomoo", "moo moo", "m00moo", "mo0moo", "มูมู่", "มู่มู่", "มูม", "หมูหมู"],
        "Dime": ["dime", "ไดม์", "ไดมส์", "d1me"],
        "Liberator": ["liberator", "ลิเบอเรเตอร์", "ลิเบอเรเตอร", "ลิเบอเรเตอ", "l1berator"],
    }, ensure_ascii=False),
    "line_token": "",
    "line_to": "",
    "line_mode": "broadcast",
    "posts_per_check": "10",
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

def upsert_kol(name, tier="", exclusivity=""):
    with _lock, _conn() as c:
        r = c.execute("SELECT id FROM kol WHERE name=?", (name,)).fetchone()
        if r:
            c.execute("UPDATE kol SET tier=?, exclusivity=? WHERE id=?",
                      (tier, exclusivity, r["id"]))
            return r["id"]
        cur = c.execute(
            "INSERT INTO kol(name,tier,exclusivity) VALUES(?,?,?)",
            (name, tier, exclusivity))
        return cur.lastrowid


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


def list_channels(active_only=True):
    with _conn() as c:
        rows = c.execute("""
            SELECT ch.*, k.name AS kol_name, k.tier AS kol_tier
            FROM channel ch JOIN kol k ON k.id = ch.kol_id
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

def post_seen(channel_id, post_id):
    with _conn() as c:
        r = c.execute(
            "SELECT 1 FROM seen_post WHERE channel_id=? AND post_id=?",
            (channel_id, post_id)).fetchone()
        return r is not None


def mark_seen(channel_id, post_id, post_url, posted_at,
              caption="", hashtags=None, matched=False):
    now = datetime.now(timezone.utc).isoformat()
    tags = " ".join(hashtags or [])
    with _lock, _conn() as c:
        c.execute("""INSERT OR IGNORE INTO seen_post
            (channel_id,post_id,post_url,posted_at,caption,hashtags,scanned_at,matched)
            VALUES(?,?,?,?,?,?,?,?)""",
            (channel_id, post_id, post_url, posted_at, caption, tags,
             now, 1 if matched else 0))


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
            posted_at, matched_keyword, snippet):
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _conn() as c:
        try:
            cur = c.execute("""INSERT INTO hit
                (channel_id,kol_name,platform,post_url,post_id,posted_at,
                 matched_keyword,snippet,detected_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (channel_id, kol_name, platform, post_url, post_id,
                 posted_at, matched_keyword, snippet, now))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # already recorded


def list_hits(status=None):
    with _conn() as c:
        q = "SELECT * FROM hit"
        args = ()
        if status:
            q += " WHERE handle_status=?"
            args = (status,)
        q += " ORDER BY detected_at DESC LIMIT 1000"
        return [dict(r) for r in c.execute(q, args).fetchall()]


def unnotified_hits():
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM hit WHERE notified=0 ORDER BY detected_at ASC").fetchall()]


def mark_notified(hit_id):
    with _lock, _conn() as c:
        c.execute("UPDATE hit SET notified=1 WHERE id=?", (hit_id,))


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
