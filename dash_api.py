"""Read-only views for the dashboard pages (overview, scan log, keywords,
channels, blocked, database browser). Kept out of main.py so the HTTP
handler stays a thin router."""
import json
import bisect
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

import db
import matcher
import scheduler

TH = scheduler.TH_TZ
EXCL_SQL = "lower(coalesce(k.exclusivity,'')) NOT LIKE '%non%'"
PLATFORMS = ["facebook", "instagram", "tiktok", "youtube", "x", "threads", "lemon8"]


def _is_excl(exclusivity):
    return "non" not in (exclusivity or "").lower()


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _parse(s):
    try:
        d = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _json(s, default):
    try:
        v = json.loads(s or "")
        return v if v is not None else default
    except Exception:
        return default


# ------------------------------------------------------------ blocked reasons
def classify_error(err):
    e = (err or "").lower()
    if "not_available" in e or "only shared it with" in e or "login" in e:
        return ("private", "ต้องล็อกอินถึงจะเห็น",
                "เป็นโปรไฟล์ส่วนตัวหรือจำกัดผู้ชม ระบบดึงแบบไม่ล็อกอินเลยไม่เห็นโพสต์ "
                "ต้องดูด้วยบัญชีจริงหรือขอให้ KOL เปิดเป็นสาธารณะ")
    if "not_found" in e or "ไม่พบบัญชี" in e or "does not exist" in e:
        return ("notfound", "ไม่พบบัญชี",
                "ลิงก์ในชีตอาจผิด บัญชีถูกปิด หรือเปลี่ยนชื่อ ให้ตรวจลิงก์แล้วแก้ในชีต")
    if "0 โพสต์" in e or "no_items" in e or "noresults" in e:
        return ("empty", "ดึงได้ 0 โพสต์",
                "บัญชีอาจเปลี่ยนชื่อ เป็นส่วนตัว หรือยังไม่เคยโพสต์ ให้เปิดลิงก์ดูว่ายังใช้ได้ไหม")
    if any(w in e for w in ("monthly usage", "usage limit", "402", "credit", "quota")):
        return ("budget", "เครดิตดึงข้อมูลหมด",
                "เพดานค่าใช้จ่าย Apify เต็ม ระบบจะดึงได้อีกเมื่อขยายเพดานหรือขึ้นรอบบิลใหม่")
    if "timeout" in e or "timed out" in e or "timed-out" in e:
        return ("timeout", "หมดเวลา",
                "แพลตฟอร์มตอบช้า ปกติรอบถัดไปจะกลับมาเอง")
    return ("other", "ข้อผิดพลาดอื่น", "ดูข้อความ error เต็มด้านล่าง")


# ------------------------------------------------------------ next run
def last_check():
    """When a channel was last checked — works before the scan log existed."""
    with db._conn() as c:
        return c.execute("SELECT max(last_checked) FROM channel "
                         "WHERE active=1").fetchone()[0]


def next_run():
    now = datetime.now(TH)
    if (db.get_setting("auto_enabled", "1") or "1") != "1":
        return {"at": None, "label": "ปิดสแกนอัตโนมัติอยู่"}
    times = sorted(t.strip() for t in
                   (db.get_setting("run_times", "08:00,16:00") or "").split(",")
                   if t.strip())
    if db.hourly_until():
        times = list(db.HOURLY_TIMES)
    cands = []
    for day in (0, 1):
        d = (now + timedelta(days=day)).strftime("%Y-%m-%d")
        for hhmm in times:
            try:
                t = datetime.strptime(f"{d} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=TH)
            except ValueError:
                continue
            if t > now:
                cands.append((t, "ทุกช่อง"))
    until = db.get_setting("excl_hourly_until", "") or ""
    hourly = bool(until and now.isoformat() < until)
    if hourly:
        t = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        if t.strftime("%H:%M") not in times:
            cands.append((t, "เฉพาะ Exclusive"))
    if not cands:
        return {"at": None, "label": "ไม่มีรอบที่ตั้งไว้"}
    t, kind = min(cands)
    return {"at": _iso(t), "kind": kind, "times": times,
            "hourly_until": until if hourly else "", "all_hourly_until": db.hourly_until()}


# ------------------------------------------------------------ scan log
_HIST_CACHE = {"at": 0, "key": None, "rows": []}


def _history_runs(before_iso):
    """Rounds from before the scan log existed, rebuilt from the time each
    post was saved (gaps > 25 min split rounds). Only rounds that saved at
    least one new post show up — an approximation, flagged as such."""
    if (_HIST_CACHE["key"] == before_iso
            and time.time() - _HIST_CACHE["at"] < 600):
        return _HIST_CACHE["rows"]
    with db._conn() as c:
        ts = [r[0] for r in c.execute(
            "SELECT scanned_at FROM seen_post WHERE scanned_at < ? "
            "ORDER BY scanned_at", (before_iso or "9999",))]
        hit_rows = c.execute(
            "SELECT detected_at, kind FROM hit WHERE detected_at < ? "
            "ORDER BY detected_at", (before_iso or "9999",)).fetchall()
    comp_t = [r[0] for r in hit_rows if r[1] == "competitor"]
    brand_t = [r[0] for r in hit_rows if r[1] == "brand"]
    clusters = []
    start = prev = None
    n = 0
    for s in ts:
        d = _parse(s)
        if d is None:
            continue
        if prev is None or (d - prev).total_seconds() > 1500:
            if start is not None:
                clusters.append((start, prev, n))
            start, n = d, 0
        prev = d
        n += 1
    if start is not None:
        clusters.append((start, prev, n))
    rows = []
    for a, b, n in clusters:
        lo, hi = _iso(a - timedelta(minutes=2)), _iso(b + timedelta(minutes=2))
        h = bisect.bisect_right(comp_t, hi) - bisect.bisect_left(comp_t, lo)
        bh = bisect.bisect_right(brand_t, hi) - bisect.bisect_left(brand_t, lo)
        rows.append({"id": f"h{int(a.timestamp())}-{int(b.timestamp())}",
                     "started_at": _iso(a), "finished_at": _iso(b),
                     "trigger": "ย้อนหลัง (ประมาณจากเวลาบันทึกโพสต์)",
                     "scope": "history", "total": None, "done": None,
                     "fetched": None, "new_posts": n, "hit_posts": None,
                     "hits": h, "brand_hits": bh, "blocked": None,
                     "status": "history"})
    rows.reverse()
    _HIST_CACHE.update(at=time.time(), key=before_iso, rows=rows)
    return rows


def runs(limit=2000):
    real = db.list_scan_runs(limit)
    for r in real:
        r["blocked_list"] = _json(r.pop("blocked_json", None), [])
    first = real[-1]["started_at"] if real else None
    hist = _history_runs(first)
    out = real + hist
    return {"runs": out[:limit], "real": len(real), "history": len(hist),
            "progress": scheduler.PROGRESS, "next": next_run()}


def run_detail(run_id):
    run = None
    if str(run_id).startswith("h"):
        try:
            a, b = str(run_id)[1:].split("-")
            start = datetime.fromtimestamp(int(a), timezone.utc)
            end = datetime.fromtimestamp(int(b), timezone.utc)
        except Exception:
            return {"error": "bad id"}
        run = {"id": run_id, "started_at": _iso(start), "finished_at": _iso(end),
               "trigger": "ย้อนหลัง (ประมาณจากเวลาบันทึกโพสต์)", "status": "history",
               "blocked_list": []}
        lo, hi = _iso(start - timedelta(seconds=1)), _iso(end + timedelta(seconds=1))
    else:
        run = db.get_scan_run(int(run_id))
        if not run:
            return {"error": "not found"}
        run["blocked_list"] = _json(run.pop("blocked_json", None), [])
        lo = run["started_at"]
        hi = run.get("finished_at") or _iso(datetime.now(timezone.utc))
    with db._conn() as c:
        posts = [dict(r) for r in c.execute(f"""
            SELECT sp.id, k.name AS kol, k.exclusivity, ch.platform,
                   sp.posted_at, sp.scanned_at, sp.post_url,
                   substr(sp.caption,1,260) AS caption, sp.matched,
                   a.level AS ai_level,
                   (SELECT group_concat(DISTINCT h.matched_keyword) FROM hit h
                     WHERE h.channel_id=sp.channel_id AND h.post_id=sp.post_id
                       AND h.kind='competitor') AS keywords,
                   (SELECT group_concat(DISTINCT h.matched_keyword) FROM hit h
                     WHERE h.channel_id=sp.channel_id AND h.post_id=sp.post_id
                       AND h.kind='brand') AS brands
            FROM seen_post sp JOIN channel ch ON ch.id=sp.channel_id
            JOIN kol k ON k.id=ch.kol_id
            LEFT JOIN ai_review a ON a.seen_post_id=sp.id
            WHERE sp.scanned_at >= ? AND sp.scanned_at <= ?
            ORDER BY sp.scanned_at DESC LIMIT 3000""", (lo, hi))]
        hits = [dict(r) for r in c.execute("""
            SELECT id, kol_name, platform, post_url, posted_at, matched_keyword,
                   snippet, detected_at, handle_status, kind
            FROM hit WHERE detected_at >= ? AND detected_at <= ?
              AND kind NOT IN ('comment', 'group')
            ORDER BY detected_at DESC LIMIT 1000""", (lo, hi))]
    for p in posts:
        p["excl"] = _is_excl(p.pop("exclusivity"))
    by_plat = Counter(p["platform"] for p in posts)
    by_kol = Counter(p["kol"] for p in posts)
    return {"run": run, "posts": posts, "hits": hits,
            "by_platform": dict(by_plat),
            "by_kol": by_kol.most_common(30)}


# ------------------------------------------------------------ overview
def overview():
    now = datetime.now(timezone.utc)
    chans = db.list_channels()
    kols = {}
    for c in chans:
        kols[c["kol_id"]] = _is_excl(c.get("kol_exclusivity"))
    by_plat = Counter(c["platform"] for c in chans)
    blocked = [c for c in chans if c["status"] == "blocked"]
    d1, d7, d14 = (_iso(now - timedelta(days=n)) for n in (1, 7, 15))
    with db._conn() as c:
        new24 = c.execute("SELECT count(*) FROM seen_post WHERE scanned_at>=?",
                          (d1,)).fetchone()[0]
        excl24 = c.execute(f"""SELECT count(*) FROM seen_post sp
            JOIN channel ch ON ch.id=sp.channel_id JOIN kol k ON k.id=ch.kol_id
            WHERE sp.scanned_at>=? AND {EXCL_SQL}""", (d1,)).fetchone()[0]
        open_hits = c.execute("""SELECT count(*) FROM hit WHERE kind='competitor'
            AND handle_status='new'""").fetchone()[0]
        hits7 = c.execute("""SELECT count(DISTINCT channel_id||'|'||post_id) FROM hit
            WHERE kind='competitor' AND detected_at>=?""", (d7,)).fetchone()[0]
        ai7 = dict(c.execute("""SELECT level, count(*) FROM ai_review
            WHERE reviewed_at>=? GROUP BY level""", (d7,)).fetchall())
        attention = [dict(r) for r in c.execute("""
            SELECT 'hit' AS src, h.id, h.kol_name AS kol, h.platform,
                   h.post_url, h.posted_at, h.detected_at AS at,
                   h.matched_keyword AS what, substr(h.snippet,1,160) AS text,
                   h.handle_status
            FROM hit h WHERE h.kind='competitor' AND h.handle_status='new'
            ORDER BY h.detected_at DESC LIMIT 8""")]
        attention += [dict(r) for r in c.execute("""
            SELECT 'ai' AS src, a.seen_post_id AS id, k.name AS kol, ch.platform,
                   s.post_url, s.posted_at, a.reviewed_at AS at,
                   a.competitors AS what, substr(coalesce(a.why,a.reason),1,160) AS text,
                   '' AS handle_status
            FROM ai_review a JOIN seen_post s ON s.id=a.seen_post_id
            JOIN channel ch ON ch.id=s.channel_id JOIN kol k ON k.id=ch.kol_id
            WHERE a.level='urgent' AND a.reviewed_at>=?
            ORDER BY a.reviewed_at DESC LIMIT 8""", (d7,))]
        scans = [r[0] for r in c.execute(
            "SELECT scanned_at FROM seen_post WHERE scanned_at>=?", (d14,))]
        hit_ts = [r[0] for r in c.execute(
            "SELECT detected_at FROM hit WHERE kind='competitor' AND detected_at>=?",
            (d14,))]
        plat14 = dict(c.execute("""SELECT ch.platform, count(*) FROM seen_post sp
            JOIN channel ch ON ch.id=sp.channel_id WHERE sp.scanned_at>=?
            GROUP BY ch.platform""", (d14,)).fetchall())
    for a in attention:
        if a["src"] == "ai":
            a["what"] = ", ".join(_json(a["what"], [])) or "AI ติดธง"
    attention.sort(key=lambda a: a.get("at") or "", reverse=True)

    def th_day(s):
        d = _parse(s)
        return d.astimezone(TH).strftime("%Y-%m-%d") if d else None
    days = [(datetime.now(TH) - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(13, -1, -1)]
    pc, hc = Counter(map(th_day, scans)), Counter(map(th_day, hit_ts))
    series = [{"day": d, "posts": pc.get(d, 0), "hits": hc.get(d, 0)} for d in days]

    real = db.list_scan_runs(8)
    for r in real:
        r["blocked_list"] = _json(r.pop("blocked_json", None), [])
    try:
        import ai_review
        ai = ai_review.status()
    except Exception:
        ai = None
    return {
        "kols": len(kols), "kols_excl": sum(1 for v in kols.values() if v),
        "kols_non": sum(1 for v in kols.values() if not v),
        "channels": len(chans),
        "channels_excl": sum(1 for c in chans if _is_excl(c.get("kol_exclusivity"))),
        "by_platform": dict(by_plat), "blocked": len(blocked),
        "new24": new24, "excl24": excl24, "open_hits": open_hits,
        "hits7": hits7, "ai7": ai7, "attention": attention[:10],
        "series": series, "platform14": plat14,
        "recent_runs": real, "progress": scheduler.PROGRESS,
        "next": next_run(), "ai": ai, "last_check": last_check(),
        "line_error": db.get_setting("line_last_error", "") or "",
        "keywords": len(matcher.get_watchlist()),
    }


# ------------------------------------------------------------ keywords
def keywords():
    wl = matcher.get_watchlist()
    unb = matcher.get_unbranded_watchlist()
    brands = matcher.get_client_brands()
    with db._conn() as c:
        stats = {}
        for r in c.execute("""SELECT matched_keyword, kind, count(*) n,
                   count(DISTINCT kol_name) kols, max(detected_at) last
                   FROM hit GROUP BY matched_keyword, kind"""):
            stats[(r["matched_keyword"], r["kind"])] = {
                "hits": r["n"], "kols": r["kols"], "last": r["last"]}

    def rows(d, kind):
        out = []
        for k, vs in d.items():
            vs = [v for v in vs if v and v != k]
            out.append({"name": k, "variants": vs, "n": 1 + len(vs),
                        **stats.get((k, kind), {"hits": 0, "kols": 0, "last": None})})
        return out
    ai_hits = [{"name": k[0], **v} for k, v in stats.items()
               if k[1] == "competitor" and k[0].startswith("AI:")]
    try:
        import ai_review
        policy = db.get_setting("ai_review_policy", "") or ""
        default_policy = getattr(ai_review, "DEFAULT_POLICY", "") or ""
    except Exception:
        policy, default_policy = "", ""
    comp = rows(wl, "competitor")
    return {
        "competitors": comp,
        "unbranded": rows(unb, "competitor"),
        "client_brands": rows(brands, "brand"),
        "exclude": matcher._exclude_words(),
        "promo_signals": matcher._promo_signals(),
        "ai_hits": sorted(ai_hits, key=lambda x: -x["hits"]),
        "ai_policy": policy, "ai_default_policy": default_policy,
        "totals": {"brands": len(comp),
                   "words": sum(r["n"] for r in comp),
                   "unbranded_words": sum(r["n"] for r in rows(unb, "competitor")),
                   "client_words": sum(r["n"] for r in rows(brands, "brand"))},
    }


# ------------------------------------------------------------ channels
def channels():
    chans = db.list_channels()
    extra = _json(db.get_setting("extra_channels", "{}"), {})
    extra_urls = set()
    for plats in (extra or {}).values():
        for urls in (plats or {}).values():
            for u in (urls or []):
                extra_urls.add(u.rstrip("/").lower())
    enabled = {p.strip() for p in (db.get_setting(
        "scan_platforms", ",".join(PLATFORMS)) or "").split(",") if p.strip()}
    skip = _json(db.get_setting("skip_channels", "{}"), {})
    y0 = "2026-01-01"
    with db._conn() as c:
        stats = {r["channel_id"]: dict(r) for r in c.execute("""
            SELECT channel_id, count(*) n,
                   sum(CASE WHEN posted_at >= ? THEN 1 ELSE 0 END) n_year,
                   max(posted_at) last_post
            FROM seen_post GROUP BY channel_id""", (y0,))}
        hitn = dict(c.execute("""SELECT channel_id, count(DISTINCT post_id)
            FROM hit WHERE kind='competitor' GROUP BY channel_id""").fetchall())
        inactive = c.execute("SELECT count(*) FROM channel WHERE active=0").fetchone()[0]
    kols = {}
    for ch in chans:
        k = kols.setdefault(ch["kol_id"], {
            "name": ch["kol_name"], "tier": ch.get("kol_tier") or "",
            "excl": _is_excl(ch.get("kol_exclusivity")),
            "exclusivity": ch.get("kol_exclusivity") or "",
            "skip": skip.get(ch["kol_name"], []), "channels": []})
        st = stats.get(ch["id"], {})
        k["channels"].append({
            "id": ch["id"], "platform": ch["platform"], "url": ch["profile_url"],
            "status": ch["status"], "error": ch.get("last_error") or "",
            "last_checked": ch.get("last_checked"), "last_ok": ch.get("last_ok"),
            "posts": st.get("n", 0), "posts_year": st.get("n_year", 0) or 0,
            "last_post": st.get("last_post"), "hits": hitn.get(ch["id"], 0),
            "source": "นอกชีต" if ch["profile_url"].rstrip("/").lower() in extra_urls
                      else "ชีต",
            "scanned": ch["platform"] in enabled})
    out = sorted(kols.values(), key=lambda k: (not k["excl"], k["name"].lower()))
    return {"kols": out, "platforms": PLATFORMS, "enabled": sorted(enabled),
            "inactive": inactive}


# ------------------------------------------------------------ post counts
# How far back each platform can be read without logging in — shown next
# to the count so a low number is not mistaken for a quiet KOL.
_LIMIT_NOTE = {
    "lemon8": "Lemon8 ไม่ล็อกอินเห็นแค่ ~20 โพสต์ล่าสุด ย้อนหลังไม่ได้",
}


def post_counts(year=2026):
    """Posts per channel per month for one year (San, 22 Nov 2026 —
    "สรุปยอดโพสต์ต่อช่อง"). Counts what is in seen_post: the hourly scan
    plus the backfill of everything since 1 Jan 2026."""
    y = str(int(year))
    chans = db.list_channels()
    with db._conn() as c:
        months = {}
        for r in c.execute("""
                SELECT channel_id, substr(posted_at,6,2) m, count(*) n
                FROM seen_post WHERE substr(posted_at,1,4)=?
                GROUP BY channel_id, m""", (y,)):
            months.setdefault(r[0], {})[r[1]] = r[2]
        span = {r[0]: (r[1], r[2]) for r in c.execute("""
                SELECT channel_id, min(posted_at), max(posted_at) FROM seen_post
                WHERE substr(posted_at,1,4)=? GROUP BY channel_id""", (y,))}
        nodate = dict(c.execute("""SELECT channel_id, count(*) FROM seen_post
                WHERE coalesce(posted_at,'')='' GROUP BY channel_id""").fetchall())
        hits = dict(c.execute("""SELECT channel_id, count(DISTINCT post_id)
                FROM hit WHERE kind='competitor' AND substr(posted_at,1,4)=?
                GROUP BY channel_id""", (y,)).fetchall())
    rows = []
    for ch in chans:
        m = months.get(ch["id"], {})
        mm = [m.get(f"{i:02d}", 0) for i in range(1, 13)]
        first, last = span.get(ch["id"], ("", ""))
        note = _LIMIT_NOTE.get(ch["platform"], "")
        if ch["status"] == "blocked":
            note = (ch.get("last_error") or "เช็กไม่ได้")[:140]
        rows.append({
            "id": ch["id"], "kol": ch["kol_name"],
            "excl": _is_excl(ch.get("kol_exclusivity")),
            "tier": ch.get("kol_tier") or "", "platform": ch["platform"],
            "url": ch["profile_url"], "status": ch["status"],
            "total": sum(mm), "months": mm, "first": first, "last": last,
            "no_date": nodate.get(ch["id"], 0), "hits": hits.get(ch["id"], 0),
            "note": note})
    rows.sort(key=lambda r: (not r["excl"], r["kol"].lower(), r["platform"]))
    return {"year": int(y), "rows": rows}


# ------------------------------------------------------------ blocked
def blocked():
    chans = db.list_channels()
    with db._conn() as c:
        stats = {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT channel_id, count(*), max(posted_at) FROM seen_post GROUP BY channel_id")}
        recent = [dict(r) for r in c.execute(
            "SELECT id, started_at, blocked, done, scope FROM scan_run "
            "ORDER BY started_at DESC LIMIT 30")]
    rows = []
    for ch in chans:
        if ch["status"] != "blocked":
            continue
        cat, label, fix = classify_error(ch.get("last_error"))
        n, last = stats.get(ch["id"], (0, None))
        rows.append({"id": ch["id"], "kol": ch["kol_name"],
                     "excl": _is_excl(ch.get("kol_exclusivity")),
                     "platform": ch["platform"], "url": ch["profile_url"],
                     "error": ch.get("last_error") or "", "cat": cat,
                     "reason": label, "fix": fix,
                     "last_checked": ch.get("last_checked"),
                     "last_ok": ch.get("last_ok"), "posts": n, "last_post": last})
    rows.sort(key=lambda r: (not r["excl"], r["cat"], r["kol"]))
    skip = _json(db.get_setting("skip_channels", "{}"), {})
    return {"blocked": rows, "unresolved": db.list_unresolved(),
            "skip": [{"kol": k, "platforms": v} for k, v in (skip or {}).items()],
            "recent_runs": recent,
            "by_reason": Counter(r["reason"] for r in rows)}


# ------------------------------------------------------------ database browser
def tables():
    with db._conn() as c:
        names = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out = []
        for n in names:
            out.append({"name": n, "rows": c.execute(
                f'SELECT count(*) FROM "{n}"').fetchone()[0]})
        span = c.execute("SELECT min(posted_at), max(posted_at), min(scanned_at) "
                         "FROM seen_post WHERE posted_at != ''").fetchone()
        kols = [dict(r) for r in c.execute(
            "SELECT DISTINCT k.name, k.exclusivity FROM kol k "
            "WHERE k.active=1 ORDER BY k.name")]
    for k in kols:
        k["excl"] = _is_excl(k.pop("exclusivity"))
    return {"tables": out, "first_post": span[0], "last_post": span[1],
            "first_scan": span[2], "kols": kols}


def posts_query(q):
    g = lambda k, d="": (q.get(k) or [d])[0]
    where, args = ["1=1"], []
    if g("q"):
        where.append("(sp.caption LIKE ? OR k.name LIKE ? OR sp.post_url LIKE ?)")
        args += [f"%{g('q')}%"] * 3
    if g("platform"):
        where.append("ch.platform=?")
        args.append(g("platform"))
    if g("excl") == "excl":
        where.append(EXCL_SQL)
    elif g("excl") == "non":
        where.append("NOT " + EXCL_SQL)
    if g("kol"):
        where.append("k.name=?")
        args.append(g("kol"))
    if g("flag") == "hit":
        where.append("EXISTS(SELECT 1 FROM hit h WHERE h.channel_id=sp.channel_id "
                     "AND h.post_id=sp.post_id AND h.kind='competitor')")
    elif g("flag") == "brand":
        where.append("EXISTS(SELECT 1 FROM hit h WHERE h.channel_id=sp.channel_id "
                     "AND h.post_id=sp.post_id AND h.kind='brand')")
    elif g("flag") in ("urgent", "watch"):
        where.append("a.level=?")
        args.append(g("flag"))
    if g("from"):
        where.append("sp.posted_at >= ?")
        args.append(g("from"))
    if g("to"):
        where.append("sp.posted_at <= ?")
        args.append(g("to") + "T99")
    order = {"posted": "(sp.posted_at='') ASC, sp.posted_at DESC",
             "posted_asc": "(sp.posted_at='') ASC, sp.posted_at ASC",
             "scanned": "sp.scanned_at DESC",
             "kol": "k.name ASC, sp.posted_at DESC"}.get(g("sort", "posted"))
    try:
        size = max(10, min(200, int(g("size", "50"))))
        page = max(1, int(g("page", "1")))
    except ValueError:
        size, page = 50, 1
    w = " AND ".join(where)
    base = f"""FROM seen_post sp JOIN channel ch ON ch.id=sp.channel_id
               JOIN kol k ON k.id=ch.kol_id
               LEFT JOIN ai_review a ON a.seen_post_id=sp.id WHERE {w}"""
    with db._conn() as c:
        total = c.execute(f"SELECT count(*) {base}", args).fetchone()[0]
        rows = [dict(r) for r in c.execute(f"""
            SELECT sp.id, k.name AS kol, k.exclusivity, ch.platform,
                   sp.posted_at, sp.scanned_at, sp.post_url,
                   substr(sp.caption,1,240) AS caption, a.level AS ai_level,
                   (SELECT group_concat(DISTINCT h.matched_keyword) FROM hit h
                     WHERE h.channel_id=sp.channel_id AND h.post_id=sp.post_id
                       AND h.kind='competitor') AS keywords,
                   (SELECT group_concat(DISTINCT h.matched_keyword) FROM hit h
                     WHERE h.channel_id=sp.channel_id AND h.post_id=sp.post_id
                       AND h.kind='brand') AS brands
            {base} ORDER BY {order} LIMIT ? OFFSET ?""",
            args + [size, (page - 1) * size])]
    for r in rows:
        r["excl"] = _is_excl(r.pop("exclusivity"))
    return {"total": total, "page": page, "size": size, "rows": rows}


def post_detail(pid):
    with db._conn() as c:
        p = c.execute("""
            SELECT sp.*, k.name AS kol, k.exclusivity, k.tier, ch.platform,
                   ch.profile_url
            FROM seen_post sp JOIN channel ch ON ch.id=sp.channel_id
            JOIN kol k ON k.id=ch.kol_id WHERE sp.id=?""", (pid,)).fetchone()
        if not p:
            return {"error": "not found"}
        p = dict(p)
        hits = [dict(r) for r in c.execute(
            "SELECT * FROM hit WHERE channel_id=? AND post_id=?",
            (p["channel_id"], p["post_id"]))]
        ai = c.execute("SELECT * FROM ai_review WHERE seen_post_id=?",
                       (pid,)).fetchone()
    p["excl"] = _is_excl(p.get("exclusivity"))
    ai = dict(ai) if ai else None
    if ai:
        ai["competitors"] = _json(ai.get("competitors"), [])
    return {"post": p, "hits": hits, "ai": ai}
