"""Facebook groups — read every new post, flag Exclusive KOLs (San, 24 Nov 2026).

Two kinds of group (list seeded from groups_seed.json, editable by API):
  general  stock-talk groups (Dime groups, กระแสหุ้น …). Every post is kept;
           a post or top comment WRITTEN BY an Exclusive KOL that names a
           competitor is a breach.
  kol      a group owned by an Exclusive KOL (ม้าเฉียว, Salary Investor …).
           Everything above, plus anyone (team / admins) posting a competitor
           together with sign-up wording in the KOL's own group.

Collector: thedoor~facebook-group-post-scraper ($0.0015 per post, no login,
public groups only). Every round asks for posts newer than the last good
round (+30 min overlap), so each post is paid for about once.

Hits go into the shared `hit` table as kind 'group' under the KOL's channel,
LINE once for posts from the last 3 days. Money guards: a daily cap
(`groups_daily_usd`) and the Apify reserve kept for post scans.
"""
import json
import os
import re
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta

import db
import matcher
import comments   # Apify helper, credit headroom, quota errors

ACTOR = "thedoor~facebook-group-post-scraper"
USD_PER_POST = 0.0015
GROUPS_PER_RUN = 10
RUN_CAP_USD = 15
SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "groups_seed.json")

SCHEMA = """
CREATE TABLE IF NOT EXISTS fb_group (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,       -- slug or numeric id in /groups/<key>/
    name TEXT, url TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'general',
    owner_kol TEXT DEFAULT '', members TEXT DEFAULT '', section TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1, note TEXT DEFAULT '',
    last_ok_at TEXT, last_error TEXT DEFAULT '', posts INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS group_post (
    post_id TEXT PRIMARY KEY,
    group_key TEXT NOT NULL,
    author TEXT, author_id TEXT, author_url TEXT,
    kol_name TEXT DEFAULT '',       -- Exclusive KOL who wrote it ('' = someone else)
    text TEXT, posted_at TEXT, post_url TEXT,
    comments_count INTEGER DEFAULT 0,
    top_comment TEXT, top_comment_author TEXT, top_comment_kol TEXT DEFAULT '',
    matched TEXT DEFAULT '',
    scraped_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gpost_group ON group_post(group_key, posted_at);
CREATE INDEX IF NOT EXISTS idx_gpost_kol ON group_post(kol_name);
CREATE TABLE IF NOT EXISTS group_spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, day TEXT NOT NULL, usd REAL, posts INTEGER, groups INTEGER
);
CREATE TABLE IF NOT EXISTS fb_author (
    fb_id TEXT PRIMARY KEY, kol_name TEXT NOT NULL
);
"""

# Competitors not in the post watchlist that show up in KOL groups
# (ม้าเฉียว's groups: Pi Securities team A6 invites, Nov 2026 audit).
DEFAULT_EXTRA = {
    "Pi Securities": r"pia6|pi a6|pi ทีม|ทีม ?a6|x pi\b|\+ ?pi\b|บล\.? ?พาย|pi securities|pi financial|pi cme|adminpia6",
}
INVITE = re.compile(
    r"สมัคร|เปิดบัญชี|เปิดพอร์ต|ลิงก์|ลิ้งค์|ลิงค์|link|โค้ด|referral|โปร|promotion|คลิก|"
    r"ลงทะเบียน|ทัก ?line|ทักไลน์|line\.me|lin\.ee|bit\.ly|แจกหุ้น|รับหุ้นฟรี", re.I)

STATE = {"running": False, "current": "", "last_error": "", "last_round": None}
_lock = threading.Lock()


def _s(key, default):
    v = db.get_setting(key, None)
    return default if v in (None, "") else v


def enabled():
    return _s("groups_enabled", "1") == "1"


def interval_min():
    try:
        return max(30, int(float(_s("groups_interval_min", "120"))))
    except ValueError:
        return 120


def daily_cap():
    try:
        return float(_s("groups_daily_usd", "20"))
    except ValueError:
        return 20.0


def _today():
    return datetime.now(comments.TH_TZ).strftime("%Y-%m-%d")


def spent_today():
    with db._conn() as c:
        r = c.execute("SELECT SUM(usd) FROM group_spend WHERE day=?", (_today(),)).fetchone()
    return float(r[0] or 0)


def init():
    with db._lock, db._conn() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM fb_group").fetchone()[0] == 0 and os.path.exists(SEED):
            seed = json.load(open(SEED, encoding="utf-8"))
            for g in seed.get("groups", []):
                c.execute("""INSERT OR IGNORE INTO fb_group(key,name,url,kind,owner_kol,members,
                    section,enabled,note) VALUES(?,?,?,?,?,?,?,?,?)""",
                          (g["key"], g.get("name"), g["url"], g.get("kind", "general"),
                           g.get("owner_kol", ""), g.get("members", ""), g.get("section", ""),
                           1 if g.get("enabled", True) else 0, g.get("note", "")))
            for fid, kol in (seed.get("fb_authors") or {}).items():
                c.execute("INSERT OR IGNORE INTO fb_author(fb_id,kol_name) VALUES(?,?)", (fid, kol))


# ───────────────────────────── who wrote it ─────────────────────────────

def _norm(s):
    return re.sub(r"[\s\W_]+", "", (s or "").lower())


def _is_excl(x):
    return "non" not in (x or "").lower()


def _kol_index():
    """Exclusive KOLs keyed by FB id, profile URL and normalised name."""
    ids, urls, names, chan = {}, {}, {}, {}
    for ch in db.list_channels():
        if not _is_excl(ch.get("kol_exclusivity")):
            continue
        k = ch["kol_name"]
        if ch["platform"] == "facebook" or k not in chan:
            chan[k] = ch["id"]
        if len(_norm(k)) >= 5:
            names[_norm(k)] = k
        if ch["platform"] != "facebook":
            continue
        u = (ch["profile_url"] or "").split("?")[0].rstrip("/").lower()
        m = re.search(r"[?&]id=(\d+)", ch["profile_url"] or "")
        if m:
            ids[m.group(1)] = k
        elif u:
            urls[u.replace("://m.", "://www.")] = k
    with db._conn() as c:
        for fid, k in c.execute("SELECT fb_id, kol_name FROM fb_author"):
            k = names.get(_norm(k), k)
            if k in chan:
                ids.setdefault(fid, k)
    return {"ids": ids, "urls": urls, "names": names, "chan": chan}


def _who(idx, user, name=None, url=None):
    user = user or {}
    fid = str(user.get("id") or "")
    if fid in idx["ids"]:
        return idx["ids"][fid]
    u = (url or user.get("profileUrl") or "").split("?")[0].rstrip("/").lower()
    m = re.search(r"[?&]id=(\d+)", url or user.get("profileUrl") or "")
    if m and m.group(1) in idx["ids"]:
        return idx["ids"][m.group(1)]
    if u and u.replace("://m.", "://www.") in idx["urls"]:
        return idx["urls"][u.replace("://m.", "://www.")]
    return idx["names"].get(_norm(name or user.get("name")), "")


# ───────────────────────────── keyword rules ─────────────────────────────

def _extra():
    try:
        d = json.loads(_s("groups_extra_regex", "")) if _s("groups_extra_regex", "") else DEFAULT_EXTRA
    except Exception:
        d = DEFAULT_EXTRA
    return {k: re.compile(v, re.I) for k, v in d.items()}


def _find(text, wl, extra):
    if not text:
        return []
    found = matcher.scan(text, [], watchlist=wl)
    have = {f["keyword"] for f in found}
    low = text.lower()
    for name, rx in extra.items():
        m = rx.search(low)
        if m and name not in have:
            a = max(0, m.start() - 45)
            found.append({"keyword": name, "variant": m.group(0),
                          "snippet": text[a:m.end() + 45].replace("\n", " ")})
    return found


# ───────────────────────────── fetch + save ─────────────────────────────

def _ts(v):
    try:
        v = float(v or 0)
        v = v / 1000 if v > 1e11 else v
        return datetime.fromtimestamp(v, timezone.utc).isoformat() if v else ""
    except Exception:
        return ""


def _group_key_of(p, by_url):
    u = (p.get("group_url") or p.get("inputUrl") or p.get("post_url") or "")
    m = re.search(r"/groups/([^/?#]+)", u)
    k = m.group(1) if m else ""
    return by_url.get(k, k)


def _save(items, groups, idx, alert_cutoff):
    by_key = {g["key"]: g for g in groups}
    by_url = {g["key"]: g["key"] for g in groups}
    wl = matcher.get_watchlist()
    extra = _extra()
    now = datetime.now(timezone.utc).isoformat()
    new = hits = 0
    with db._conn() as c:
        ids = [str(p.get("post_id") or "") for p in items]
        have = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            have |= {r[0] for r in c.execute(
                f"SELECT post_id FROM group_post WHERE post_id IN ({','.join('?' * len(chunk))})", chunk)}
    rows, found_rows = [], []
    for p in items:
        pid = str(p.get("post_id") or "")
        if not pid or pid in have:
            continue
        have.add(pid)
        gk = _group_key_of(p, by_url)
        g = by_key.get(gk, {"kind": "general", "owner_kol": "", "name": gk})
        sp = p.get("sharedPost")
        text = (p.get("text") or "") + (("\n" + (sp.get("text") or "")) if isinstance(sp, dict) else "")
        user = p.get("user") or {}
        kol = _who(idx, user, p.get("user_name"), p.get("user_url"))
        tc = p.get("topComment")
        tc = tc[0] if isinstance(tc, list) and tc else tc
        tc = tc if isinstance(tc, dict) else {}
        tcu = tc.get("user") or tc.get("author") or {}
        tc_kol = _who(idx, tcu, tcu.get("name")) if tc else ""
        posted = _ts(p.get("creation_timestamp"))
        matched = []
        checks = []
        if kol:                        # the KOL wrote the post
            checks.append(("post", kol, text, "KOL โพสต์เอง"))
        elif g["kind"] == "kol" and g.get("owner_kol") and g["owner_kol"] in idx["chan"]:
            checks.append(("owner", g["owner_kol"], text, f"ในกลุ่มของ KOL · {p.get('user_name') or 'สมาชิก'} โพสต์"))
        if tc_kol and tc.get("text"):
            checks.append(("comment", tc_kol, tc.get("text") or "", "KOL คอมเมนต์ในกลุ่ม"))
        for what, who, t, label in checks:
            f = _find(t, wl, extra)
            if what == "owner" and not INVITE.search(t or ""):
                f = []                 # members chatting about a brand ≠ breach
            for x in f:
                matched.append(x["keyword"])
                found_rows.append((who, what, label, x, p, g, posted))
        rows.append((pid, gk, p.get("user_name") or user.get("name"), str(user.get("id") or ""),
                     p.get("user_url") or user.get("profileUrl") or "", kol, text, posted,
                     p.get("post_url") or "", int(p.get("comments_count") or 0),
                     tc.get("text") or "", tcu.get("name") or "", tc_kol,
                     ",".join(dict.fromkeys(matched)), now))
    if rows:
        with db._lock, db._conn() as c:
            c.executemany("""INSERT OR IGNORE INTO group_post(post_id,group_key,author,author_id,
                author_url,kol_name,text,posted_at,post_url,comments_count,top_comment,
                top_comment_author,top_comment_kol,matched,scraped_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        new = len(rows)
    for who, what, label, x, p, g, posted in found_rows:
        cid = idx["chan"].get(who)
        if not cid:
            continue
        snippet = f"👥 [{label} · {g.get('name') or ''}] {x['snippet']}"[:500]
        pid = str(p.get("post_id")) + ("#tc" if what == "comment" else "")
        hid = db.add_hit(cid, who, "facebook", p.get("post_url") or "", "g" + pid,
                         posted, x["keyword"], snippet, kind="group")
        if not hid:
            continue
        hits += 1
        if (posted or "") < alert_cutoff:
            db.mark_notified(hid)      # backfill: dashboard only
    return new, hits


def flush_alerts():
    with db._conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM hit WHERE kind='group' AND notified=0 AND handle_status='new'")]
    by_post = {}
    for h in rows:
        by_post.setdefault((h["kol_name"], h["post_id"]), []).append(h)
    for hs in by_post.values():
        h = hs[0]
        words = ", ".join(dict.fromkeys(x["matched_keyword"] for x in hs))
        txt = (f"👥 KOL Exclusive พูดถึงคู่แข่งในกลุ่ม Facebook\nKOL: {h['kol_name']}\nคำ: {words}\n"
               f"{(h['snippet'] or '')[:350]}\nโพสต์: {h['post_url']}")
        try:
            import notify_line
            ok, _ = notify_line.send_texts([txt])
        except Exception:
            traceback.print_exc()
            ok = False
        if ok:
            for x in hs:
                db.mark_notified(x["id"])


def list_groups(enabled_only=False):
    with db._conn() as c:
        q = "SELECT * FROM fb_group" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY kind DESC, id"
        return [dict(r) for r in c.execute(q)]


def run_round(newer_than=None, older_than=None, keys=None, cap_usd=None, alerts=True):
    """Fetch every enabled group once. newer_than: '135 minutes' / 'YYYY-MM-DD'.
    Returns a summary dict."""
    groups = [g for g in list_groups(enabled_only=True) if not keys or g["key"] in keys]
    if not groups:
        return {"groups": 0}
    if newer_than is None:
        last = _s("groups_last_round_ok", "")
        if last:
            mins = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
            newer_than = f"{int(min(mins, 3 * 24 * 60)) + 30} minutes"
        else:
            newer_than = "1 days"
    idx = _kol_index()
    alert_cutoff = ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
                    if alerts else "9999")
    started = datetime.now(timezone.utc).isoformat()
    tot = {"groups": len(groups), "posts": 0, "new": 0, "hits": 0, "usd": 0.0,
           "errors": 0, "window": newer_than}
    for i in range(0, len(groups), GROUPS_PER_RUN):
        batch = groups[i:i + GROUPS_PER_RUN]
        STATE["current"] = f"กลุ่ม {i + 1}–{i + len(batch)} จาก {len(groups)}"
        payload = {"url": [g["url"] for g in batch], "maxPosts": 3000,
                   "postsNewerThan": newer_than, "sortingOrder": "newest_posts",
                   "includeTopComment": True}
        if older_than:
            payload["postsOlderThan"] = older_than
        try:
            items, usd = comments._apify(ACTOR, payload, cap_usd or RUN_CAP_USD)
        except comments.QuotaError:
            raise
        except Exception as e:
            tot["errors"] += 1
            STATE["last_error"] = str(e)[:300]
            with db._lock, db._conn() as c:
                c.executemany("UPDATE fb_group SET last_error=? WHERE key=?",
                              [(str(e)[:300], g["key"]) for g in batch])
            continue
        new, hits = _save(items, batch, idx, alert_cutoff)
        now = datetime.now(timezone.utc).isoformat()
        with db._lock, db._conn() as c:
            c.execute("INSERT INTO group_spend(at,day,usd,posts,groups) VALUES(?,?,?,?,?)",
                      (now, _today(), usd, len(items), len(batch)))
            for g in batch:
                n = c.execute("SELECT COUNT(*) FROM group_post WHERE group_key=?", (g["key"],)).fetchone()[0]
                c.execute("UPDATE fb_group SET last_ok_at=?, last_error='', posts=? WHERE key=?",
                          (now, n, g["key"]))
        tot["posts"] += len(items); tot["new"] += new; tot["hits"] += hits; tot["usd"] += usd
    if not keys and not older_than and tot["errors"] < len(range(0, len(groups), GROUPS_PER_RUN)):
        db.set_setting("groups_last_round_ok", started)
    tot["usd"] = round(tot["usd"], 3)
    STATE["last_round"] = {**tot, "at": started}
    db.set_setting("groups_last_round", json.dumps(STATE["last_round"]))
    return tot


def _due():
    last = _s("groups_last_round_ok", "")
    if not last:
        return True
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
    return age >= interval_min()


def worker_thread(stop):
    init()
    stop.wait(60)
    while not stop.is_set():
        try:
            if enabled() and _due() and not STATE["running"]:
                room = comments.apify_headroom()
                if spent_today() >= daily_cap():
                    STATE["current"] = "ถึงเพดานเงินวันนี้แล้ว"
                elif room is not None and room < comments.reserve_usd():
                    STATE["current"] = f"รอเครดิต Apify (เหลือ ${room:.0f})"
                else:
                    STATE["running"] = True
                    try:
                        r = run_round()
                        print(f"[groups] round {r}", flush=True)
                        STATE["current"] = ""
                    except comments.QuotaError as e:
                        STATE["current"] = "เครดิต Apify หมด รอเพิ่มลิมิต"
                        STATE["last_error"] = str(e)[:300]
                    finally:
                        STATE["running"] = False
            flush_alerts()
        except Exception as e:
            STATE["last_error"] = str(e)[:300]
            STATE["running"] = False
            traceback.print_exc()
        stop.wait(300)


def start_now():
    if STATE["running"]:
        return {"error": "already running"}
    def go():
        STATE["running"] = True
        try:
            print(f"[groups] manual round {run_round()}", flush=True)
        except Exception as e:
            STATE["last_error"] = str(e)[:300]
            traceback.print_exc()
        finally:
            STATE["running"] = False
            STATE["current"] = ""
    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


# ───────────────────────────── API ─────────────────────────────

def status():
    with db._conn() as c:
        tot = c.execute("""SELECT COUNT(*) n, SUM(kol_name!='') kol, SUM(matched!='') matched,
            MAX(posted_at) newest FROM group_post""").fetchone()
        h = c.execute("""SELECT COUNT(*) n, SUM(handle_status='new') open
            FROM hit WHERE kind='group'""").fetchone()
        usd_all = c.execute("SELECT SUM(usd) FROM group_spend").fetchone()[0] or 0
        week = c.execute("SELECT SUM(usd), SUM(posts) FROM group_spend WHERE at >= ?",
                         ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),)).fetchone()
        by_kol = [dict(r) for r in c.execute("""SELECT kol_name, COUNT(*) posts,
            SUM(matched!='') matched, MAX(posted_at) last FROM group_post
            WHERE kol_name!='' GROUP BY kol_name ORDER BY posts DESC LIMIT 50""")]
    try:
        last = json.loads(_s("groups_last_round", "") or "null")
    except Exception:
        last = None
    return {"enabled": enabled(), "running": STATE["running"], "now": STATE["current"],
            "last_error": STATE["last_error"], "interval_min": interval_min(),
            "daily_cap": daily_cap(), "spent_today": round(spent_today(), 3),
            "spent_total": round(usd_all, 2),
            "week_usd": round(week[0] or 0, 2), "week_posts": week[1] or 0,
            "last_round": last, "last_ok": _s("groups_last_round_ok", ""),
            "posts": tot["n"], "kol_posts": tot["kol"] or 0, "matched": tot["matched"] or 0,
            "newest": tot["newest"], "hits": h["n"] or 0, "open_hits": h["open"] or 0,
            "groups": list_groups(), "by_kol": by_kol}


def posts(q):
    who = (q.get("who") or ["kol"])[0]
    grp = (q.get("group") or [""])[0]
    limit = min(int((q.get("limit") or ["300"])[0]), 2000)
    where, args = [], []
    if who == "kol":
        where.append("(p.kol_name!='' OR p.top_comment_kol!='')")
    elif who == "matched":
        where.append("p.matched!=''")
    if grp:
        where.append("p.group_key=?"); args.append(grp)
    sql = ("SELECT p.*, g.name group_name, g.kind group_kind FROM group_post p "
           "LEFT JOIN fb_group g ON g.key=p.group_key"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY p.posted_at DESC LIMIT ?")
    with db._conn() as c:
        return {"posts": [dict(r) for r in c.execute(sql, args + [limit])]}


def hits(status=None):
    with db._conn() as c:
        q = "SELECT * FROM hit WHERE kind='group'" + (" AND handle_status=?" if status else "") \
            + " ORDER BY posted_at DESC LIMIT 1000"
        return {"hits": [dict(r) for r in c.execute(q, (status,) if status else ())]}


def configure(payload):
    keys = {"groups_enabled", "groups_interval_min", "groups_daily_usd", "groups_extra_regex"}
    for k, v in (payload or {}).items():
        if k in keys:
            db.set_setting(k, str(v))
    with db._lock, db._conn() as c:
        for g in payload.get("add_groups") or []:
            m = re.search(r"/groups/([^/?#]+)", g.get("url") or "")
            if not m:
                continue
            c.execute("""INSERT OR IGNORE INTO fb_group(key,name,url,kind,owner_kol,members,section)
                VALUES(?,?,?,?,?,?,?)""", (m.group(1), g.get("name") or m.group(1),
                f"https://www.facebook.com/groups/{m.group(1)}/", g.get("kind") or "general",
                g.get("owner_kol") or "", g.get("members") or "", g.get("section") or ""))
        for key, on in (payload.get("enable") or {}).items():
            c.execute("UPDATE fb_group SET enabled=? WHERE key=?", (1 if on else 0, key))
        for fid, kol in (payload.get("fb_authors") or {}).items():
            c.execute("INSERT OR REPLACE INTO fb_author(fb_id,kol_name) VALUES(?,?)", (str(fid), kol))
    return status()
