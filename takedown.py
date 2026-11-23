"""Takedown tracker — posts the team asked KOLs to delete / edit.

The team keeps the list in the "ลิงก์ให้ KOL ลบ" tab of the Exclusive-KOL
sheet and writes what the KOL said in the สถานะ column. This module keeps a
copy of those links (pushed in over POST /api/takedowns) and checks each one
for real: is the post gone, or still up — and if it is up, is the competitor
name still in it?

Checks use free public endpoints only (no Apify credit):
  x        cdn.syndication.twimg.com tweet-result → Tweet / TweetTombstone
  tiktok   video page rehydration JSON → statusCode 10204 = deleted
  threads  page fetched as facebookexternalhit → og:url keeps /post/<code>
           = live; bounced to the site root while the profile still opens
           = gone (og:description is always present, so it proves nothing)
  facebook / instagram  same fetch → og:description present = live;
           otherwise "unknown" (FB/IG hide many posts behind login, so
           unknown keeps the last known result)

A post confirmed gone also marks matching competitor hits as "removed"; a
post the team notified that is still up moves new hits to "contacted".
"""
import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS takedown (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    post_key TEXT,
    kol TEXT,
    platform TEXT,
    round TEXT,
    posted_date TEXT,
    brand TEXT,
    kind TEXT,                -- 🔴 โฆษณา / 🟡 พูดถึง / ...
    action TEXT,              -- what the KOL was asked to do
    sheet_status TEXT,        -- สถานะ column in the team sheet
    sheet_row INTEGER,
    snippet TEXT,             -- text the audit found
    state TEXT,               -- gone | live | unknown
    brand_still INTEGER,      -- 1 / 0 / NULL (only when live)
    note TEXT,                -- how it was checked / why unknown
    live_text TEXT,           -- current text when live
    checked_at TEXT,
    gone_at TEXT,             -- first time seen gone
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_takedown_kol ON takedown(kol);
"""

NOTIFIED = ("แจ้งลบ", "แจ้งแก้ไข", "แจ้งแก้ไขแล้ว", "ลบแล้ว / แก้ไขแล้ว",
            "ฝากทีมรีเช็คอีกครั้ง")
TEAM_DONE = ("ลบแล้ว / แก้ไขแล้ว", "แจ้งแก้ไขแล้ว")

BRAND_RX = {
    "dime": r"dime|ไดม์|kkp|เกียรตินาคิน",
    "innovestx": r"innovest|อินโนเวสท์",
    "finansia": r"finansia|ฟินันเซีย|hero ?m|heromapp|ฮีโร่|syrus|ไซรัส|\bhero\b",
    "xspring": r"xspring|x spring|เอ็กซ์สปริง|krungthai x",
    "wealthx": r"wealth ?x|เวลท์เอกซ์|เวลธ์เอกซ์|efin",
    "yuanta": r"yuanta|หยวนต้า",
    "liberator": r"liberator|ลิเบอเรเตอร์",
    "pi securities": r"pi securities|บล\.?พาย|\bpi\b",
    "streaming": r"streaming|สตรีมมิ่ง",
    "phillip": r"phillip|ฟิลลิป",
    "bitkub": r"bitkub|บิทคับ",
    "binance": r"binance|ไบแนนซ์",
}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")
_inited = False
_lock = threading.Lock()
STATE = {"running": False, "done": 0, "total": 0, "last": None}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init():
    global _inited
    if _inited:
        return
    with db._lock, db._conn() as c:
        c.executescript(SCHEMA)
    _inited = True


def post_key(url, platform):
    u = url or ""
    for rx in (r"/status/(\d+)", r"/video/(\d+)", r"/(?:p|reel|reels)/([\w-]+)",
               r"/post/([\w-]+)", r"(pfbid\w+)", r"/posts/(\d+)", r"/reel/(\d+)",
               r"[?&]v=([\w-]+)", r"youtu\.be/([\w-]+)", r"/shorts/([\w-]+)",
               r"story_fbid=(\d+)"):
        m = re.search(rx, u)
        if m:
            return f"{platform}:{m.group(1)}"
    return f"{platform}:{u}"


def brand_regex(brand):
    b = (brand or "").lower()
    pats = [v for k, v in BRAND_RX.items() if re.search(r"\b%s\b" % k.split()[0], b)]
    return re.compile("|".join(pats), re.I) if pats else None


# ───────────────────────────── import ─────────────────────────────
def upsert(items):
    """items: [{url, kol, platform, round, posted_date, brand, kind, action,
    sheet_status, sheet_row, snippet, [state, brand_still, note, live_text,
    checked_at]}]. Sheet fields always overwrite; check fields only when
    given (so a richer local check, e.g. via Apify, can be pushed in)."""
    init()
    now = _now()
    n_new = n_upd = 0
    with db._lock, db._conn() as c:
        for it in items:
            url = (it.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            plat = (it.get("platform") or "").lower()
            row = c.execute("SELECT id, state, gone_at FROM takedown WHERE url=?",
                            (url,)).fetchone()
            sheet = dict(post_key=post_key(url, plat), kol=(it.get("kol") or "").strip(), platform=plat,
                         round=it.get("round"), posted_date=it.get("posted_date"),
                         brand=it.get("brand"), kind=it.get("kind"),
                         action=it.get("action"), sheet_status=it.get("sheet_status"),
                         sheet_row=it.get("sheet_row"), snippet=(it.get("snippet") or "")[:1000],
                         updated_at=now)
            chk = {}
            if it.get("state"):
                chk = dict(state=it["state"], brand_still=it.get("brand_still"),
                           note=it.get("note"), live_text=(it.get("live_text") or "")[:1500],
                           checked_at=it.get("checked_at") or now)
                if it["state"] == "gone":
                    chk["gone_at"] = (row and row["gone_at"]) or chk["checked_at"]
            f = {**sheet, **chk}
            if row:
                c.execute("UPDATE takedown SET " + ",".join(f"{k}=?" for k in f) +
                          " WHERE id=?", (*f.values(), row["id"]))
                n_upd += 1
            else:
                f["url"] = url
                c.execute(f"INSERT INTO takedown ({','.join(f)}) VALUES "
                          f"({','.join('?' * len(f))})", tuple(f.values()))
                n_new += 1
    linked = sync_hits()
    return {"ok": True, "new": n_new, "updated": n_upd, "hits_linked": linked}


# ───────────────────────────── checks ─────────────────────────────
def _get(url, ua=UA, timeout=30):
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            url, headers={"User-Agent": ua, "Accept-Language": "en"}), timeout=timeout)
        return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:  # network
        return 0, str(e)


def _og(body, key):
    m = re.search(r'<meta property="og:%s" content="([^"]*)"' % key, body)
    return html.unescape(m.group(1)) if m else None


def _check_threads(url):
    """Threads always serves an og:description, even for a deleted post — it
    bounces to the login page ("Join Threads to share ideas…"), so the generic
    "og present = live" rule called every deleted post live. Decide by og:url:
    a live post keeps /post/<code>; a missing one points at the site root.
    The login page can also mean Threads is walling us, so a post only counts
    as gone when the author's profile still opens normally."""
    m = re.search(r"threads\.(?:com|net)/@([\w.]+)/post/([\w-]+)", url)
    if not m:
        return "unknown", "", "ลิงก์ Threads ไม่มีรหัสโพสต์"
    user, code = m.groups()
    ua = "facebookexternalhit/1.1"
    s, b = _get(url, ua)
    og_url = _og(b, "url") or ""
    if f"/post/{code}" in og_url:
        d, t = _og(b, "description") or "", _og(b, "title") or ""
        return "live", d + (" | " + t if t else ""), "Threads"
    if s != 200 or not re.fullmatch(r"https://www\.threads\.(?:com|net)/?(?:login/?)?", og_url):
        return "unknown", "", f"Threads ตอบ {s}"
    _, pb = _get(f"https://www.threads.com/@{user}", ua)
    if f"/@{user.lower()}" in (_og(pb, "url") or "").lower():
        return "gone", "", "Threads: โพสต์หายแล้ว (ลบแล้ว หรือบัญชีปิดเป็นส่วนตัว)"
    return "unknown", "", "Threads: เปิดไม่ได้ทั้งโพสต์และหน้าโปรไฟล์"


def check_one(url, platform):
    """→ (state, text, note)"""
    p = (platform or "").lower()
    if p == "x":
        m = re.search(r"status/(\d+)", url)
        if not m:
            return "unknown", "", "ลิงก์ X ไม่มีเลขโพสต์"
        s, b = _get(f"https://cdn.syndication.twimg.com/tweet-result?id={m.group(1)}"
                    "&lang=en&token=4c2mmul6mnh")
        try:
            j = json.loads(b)
        except Exception:
            return "unknown", "", f"X ตอบ {s}"
        if j.get("__typename") == "TweetTombstone":
            t = j.get("tombstone", {}).get("text", {}).get("text", "")
            if "deleted" in t:
                return "gone", "", "X: เจ้าของโพสต์ลบแล้ว"
            return "unknown", "", "X: " + t[:120]
        if j.get("text") is not None:
            return "live", j["text"], "X"
        return "unknown", "", f"X ตอบ {s}"
    if p == "tiktok":
        s, b = _get(url)
        m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
                      b, re.S)
        if not m:
            return "unknown", "", f"TikTok ตอบ {s}"
        try:
            d = json.loads(m.group(1))["__DEFAULT_SCOPE__"].get("webapp.video-detail", {})
        except Exception:
            return "unknown", "", "TikTok อ่านหน้าไม่ได้"
        it = (d.get("itemInfo") or {}).get("itemStruct")
        if it and d.get("statusCode", 0) == 0 and it.get("desc") is not None:
            return "live", it["desc"], "TikTok"
        if d.get("statusCode") in (10204, 10216):
            return "gone", "", f"TikTok: {d.get('statusMsg') or 'deleted'}"
        return "unknown", "", f"TikTok {d.get('statusCode')} {d.get('statusMsg') or ''}"
    if p == "threads":
        return _check_threads(url)
    if p in ("facebook", "instagram", "lemon8", "youtube"):
        if p == "youtube":
            s, b = _get("https://www.youtube.com/oembed?format=json&url=" +
                        urllib.parse.quote(url))
            if s == 200:
                return "live", json.loads(b).get("title", ""), "YouTube"
            if s in (400, 401, 403, 404):
                return "gone", "", f"YouTube oEmbed {s}"
            return "unknown", "", f"YouTube {s}"
        s, b = _get(url, "facebookexternalhit/1.1")
        d, t = _og(b, "description"), _og(b, "title")
        if d is not None or (t and t not in ("Facebook", "Instagram", "Threads", "Lemon8")):
            return "live", (d or "") + (" | " + t if t else ""), p
        return "unknown", "", f"{p}: เปิดแบบไม่ล็อกอินไม่เห็นโพสต์ (อาจลบแล้ว หรือจำกัดสิทธิ์)"
    return "unknown", "", "แพลตฟอร์มนี้ยังเช็กอัตโนมัติไม่ได้"


def recheck(only_notified=True, ids=None):
    """Re-check takedown rows. Unknown results never overwrite a known one."""
    init()
    with _lock:
        if STATE["running"]:
            return {"error": "running"}
        STATE.update(running=True, done=0)
    try:
        with db._conn() as c:
            q = "SELECT id, url, platform, brand, state, brand_still FROM takedown"
            args = []
            if ids:
                q += f" WHERE id IN ({','.join('?' * len(ids))})"
                args = list(ids)
            elif only_notified:
                q += f" WHERE sheet_status IN ({','.join('?' * len(NOTIFIED))})"
                args = list(NOTIFIED)
            rows = [dict(r) for r in c.execute(q, args)]
        STATE["total"] = len(rows)

        def work(r):
            try:
                res = check_one(r["url"], r["platform"])
            except Exception as e:
                res = ("unknown", "", str(e)[:120])
            STATE["done"] += 1
            return r, res

        now = _now()
        out = []
        with ThreadPoolExecutor(6) as ex:
            out = list(ex.map(work, rows))
        counts = {"gone": 0, "live": 0, "unknown": 0}
        with db._lock, db._conn() as c:
            for r, (state, text, note) in out:
                counts[state] += 1
                if state == "unknown":
                    if r["state"] in (None, "", "unknown"):
                        c.execute("UPDATE takedown SET state='unknown', note=?, checked_at=? "
                                  "WHERE id=?", (note, now, r["id"]))
                    else:  # keep last known result, just note the attempt
                        c.execute("UPDATE takedown SET note=? WHERE id=?",
                                  (f"{note} — คงผลเดิมไว้", r["id"]))
                    continue
                rx = brand_regex(r["brand"])
                bs = (1 if rx.search(text) else 0) if (state == "live" and rx) else None
                if bs == 0 and r["platform"] in ("facebook", "instagram", "threads", "lemon8"):
                    # link-preview text is cut short and hides shared posts:
                    # not seeing the name there doesn't prove it was edited
                    bs = r["brand_still"] if r["state"] == "live" else None
                c.execute("UPDATE takedown SET state=?, brand_still=?, note=?, live_text=?, "
                          "checked_at=?, gone_at=CASE WHEN ?='gone' THEN coalesce(gone_at, ?) "
                          "ELSE NULL END WHERE id=?",
                          (state, bs, note, (text or "")[:1500], now, state, now, r["id"]))
        linked = sync_hits()
        STATE["last"] = {"at": now, **counts, "checked": len(rows), "hits_linked": linked}
        return {"ok": True, **STATE["last"]}
    finally:
        STATE["running"] = False


def recheck_async(**kw):
    if STATE["running"]:
        return {"error": "running"}
    threading.Thread(target=recheck, kwargs=kw, daemon=True).start()
    return {"ok": True, "started": True}


def sync_hits():
    """Gone → hit.removed. Notified + still up → new hits become contacted."""
    init()
    n = 0
    with db._lock, db._conn() as c:
        rows = c.execute("SELECT url, post_key, state, sheet_status FROM takedown "
                         "WHERE state IN ('gone','live')").fetchall()
        for r in rows:
            key = (r["post_key"] or "").split(":", 1)[-1]
            if not key:
                continue
            if r["state"] == "gone":
                new, cond = "removed", "handle_status != 'removed'"
            elif r["sheet_status"] in NOTIFIED:
                new, cond = "contacted", "handle_status = 'new'"
            else:
                continue
            cur = c.execute(
                f"UPDATE hit SET handle_status=? WHERE kind='competitor' AND {cond} AND "
                "(post_url=? OR post_id=? OR instr(coalesce(post_url,''), ?) > 0)",
                (new, r["url"], key, key))
            n += cur.rowcount
    return n


# ───────────────────────────── read ─────────────────────────────
def verdict(r):
    if r["state"] == "gone":
        return "gone"
    if r["state"] == "live":
        if r["brand_still"] == 0:
            return "edited"
        return "live"
    return "unknown"


def listing():
    init()
    with db._conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM takedown ORDER BY kol, coalesce(sheet_row, 99999), id")]
    for r in rows:
        r["verdict"] = verdict(r)
        r["notified"] = r["sheet_status"] in NOTIFIED
        r["team_done"] = r["sheet_status"] in TEAM_DONE
        r["mismatch"] = r["team_done"] and r["verdict"] in ("live",)
    notified = [r for r in rows if r["notified"]]
    summ = {
        "total": len(rows), "notified": len(notified),
        "gone": sum(r["verdict"] == "gone" for r in notified),
        "edited": sum(r["verdict"] == "edited" for r in notified),
        "live": sum(r["verdict"] == "live" for r in notified),
        "unknown": sum(r["verdict"] == "unknown" for r in notified),
        "team_done": sum(r["team_done"] for r in notified),
        "mismatch": sum(r["mismatch"] for r in notified),
        "gone_not_marked": sum(r["verdict"] == "gone" and not r["team_done"]
                               for r in notified),
        "last_check": max((r["checked_at"] or "" for r in rows), default=None),
    }
    return {"items": rows, "summary": summ, "progress": dict(STATE)}


def scheduler_thread(stop_event, every_hours=12):
    """Re-check notified links twice a day (free endpoints only)."""
    time.sleep(120)
    while not stop_event.is_set():
        try:
            init()
            recheck(only_notified=True)
        except Exception as e:
            print("[takedown] recheck failed:", e, flush=True)
        stop_event.wait(every_hours * 3600)
