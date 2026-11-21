"""KOL Competitor-Keyword Watch — local web UI on http://127.0.0.1:5070"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import traceback
import webbrowser
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import db
import registry
import scheduler
import ai_review
import takedown
import notify_line
import dash_api
import hr_auth

# Railway injects PORT; locally use KOLWATCH_PORT (default 5070).
PORT = int(os.environ.get("PORT") or os.environ.get("KOLWATCH_PORT") or "5070")
HERE = os.path.dirname(os.path.abspath(__file__))

# Dashboard credentials — must be supplied via env (no defaults so no
# secret ever lives in the repo). See .env.example.
AUTH_USER = os.environ.get("KOLWATCH_USER", "")
AUTH_PASS = os.environ.get("KOLWATCH_PASS", "")
# Extra team accounts: KOLWATCH_USERS="petch:pw1,waan:pw2" (full access).
# API keys for scripts / Claude: KOLWATCH_API_KEYS="petch:key1,waan:key2"
# sent as header "Authorization: Bearer <key>" or "X-API-Key: <key>".


def _pairs(env):
    out = {}
    for item in (os.environ.get(env, "") or "").split(","):
        if ":" in item:
            k, v = item.split(":", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


USERS = _pairs("KOLWATCH_USERS")
if AUTH_USER and AUTH_PASS:
    USERS.setdefault(AUTH_USER, AUTH_PASS)
API_KEYS = {v: k for k, v in _pairs("KOLWATCH_API_KEYS").items()}


def _check_login(u, p):
    ok = False
    for name, pw in USERS.items():
        if hmac.compare_digest(u, name) and hmac.compare_digest(p, pw):
            ok = True
    return ok


def _api_key_user(handler):
    key = handler.headers.get("X-API-Key", "") or ""
    auth = handler.headers.get("Authorization", "") or ""
    if auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    if not key:
        return None
    for k, user in API_KEYS.items():
        if hmac.compare_digest(key, k):
            return user
    return None


_FAILS = {}   # ip -> [count, first_ts] — slows password guessing


def _cookies(handler):
    return SimpleCookie(handler.headers.get("Cookie", ""))


def current_user(handler):
    """Signed-cookie user ({u, n, via}) or None. HR users must still be on
    the allow-list (setting hr_users) — removing a name locks them out now."""
    m = _cookies(handler).get("kw_auth")
    obj = hr_auth.unsign(m.value) if m else None
    if not obj:
        return None
    if obj.get("via") == "account":           # shared env account (legacy)
        return obj if obj.get("u") in USERS else None
    return obj if hr_auth.is_allowed(obj.get("u")) else None


def _authed(handler):
    if current_user(handler):
        return True
    return _api_key_user(handler) is not None


LOGIN_HTML = """<!DOCTYPE html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>เข้าสู่ระบบ — KOL Watch</title>
<link href="https://fonts.googleapis.com/css2?family=Anuphan:wght@400;600;700&display=swap" rel="stylesheet">
<style>
body{font-family:'Anuphan',system-ui,sans-serif;background:#eef2f9;color:#0f1b2d;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;padding:16px;box-sizing:border-box}
.box{background:#fff;border:1px solid #e4eaf3;padding:30px;border-radius:16px;width:340px;max-width:100%;
box-shadow:0 10px 30px -12px rgba(15,27,45,.25);box-sizing:border-box}
.brand{display:flex;gap:10px;align-items:center;margin-bottom:18px}
.mk{width:38px;height:38px;border-radius:9px;background:#1e5eff;display:grid;place-items:center}
.brand b{display:block;font-size:16px}.brand small{color:#6b7a90;font-size:12px}
.line{display:flex;gap:10px;align-items:center;justify-content:center;width:100%;box-sizing:border-box;background:#06c755;
color:#fff;border-radius:9px;padding:11px;font-weight:700;text-decoration:none;font-size:14px}
.line:hover{background:#05b04b}
.or{display:flex;align-items:center;gap:10px;color:#9aa8bd;font-size:12px;margin:18px 0 4px}
.or:before,.or:after{content:"";flex:1;height:1px;background:#e4eaf3}
label{font-size:12px;font-weight:600;color:#6b7a90;display:block;margin:12px 0 5px}
input{width:100%;box-sizing:border-box;background:#f6f8fc;border:1px solid #e4eaf3;color:#0f1b2d;
border-radius:9px;padding:10px 12px;font:inherit}input:focus{outline:2px solid #1e5eff;outline-offset:0}
button{width:100%;margin-top:18px;background:#1e5eff;color:#fff;border:0;padding:11px;border-radius:9px;
cursor:pointer;font:inherit;font-weight:700}button:hover{background:#1749cc}
.err{background:#fef3f2;border:1px solid #fecdca;color:#b42318;font-size:13px;margin-bottom:14px;padding:9px 12px;border-radius:9px;__ERR__}
.hint{color:#9aa8bd;font-size:11.5px;margin-top:14px;text-align:center}</style></head><body>
<div class="box">
<div class="brand"><div class="mk"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/></svg></div>
<div><b>KOL Watch</b><small>Sociology × Webull</small></div></div>
<div class="err">__MSG__</div>
<a class="line" href="/sso/start"><svg width="20" height="20" viewBox="0 0 24 24" fill="#fff"><path d="M12 3C6.5 3 2 6.6 2 11c0 3.9 3.5 7.2 8.3 7.9.3.1.8.2.9.5.1.3.1.7 0 1l-.1.9c0 .3-.2 1 .9.5s5.8-3.4 7.9-5.9C21.3 14.3 22 12.7 22 11c0-4.4-4.5-8-10-8z"/></svg>เข้าด้วย LINE (SO! HR)</a>
<div class="or">หรือใช้รหัส SO! HR</div>
<form method="POST" action="/login">
<label>ชื่อผู้ใช้ SO! HR</label><input name="username" autocomplete="username" autocapitalize="none" placeholder="เช่น petch">
<label>รหัสผ่าน SO! HR</label><input name="password" type="password" autocomplete="current-password">
<button>เข้าสู่ระบบ</button></form>
<div class="hint">ใช้บัญชีเดียวกับ hr.sociology.asia</div>
</div></body></html>"""


def login_page(msg=""):
    import html as _h
    return (LOGIN_HTML.replace("__ERR__", "" if msg else "display:none")
            .replace("__MSG__", _h.escape(msg)))


def _client_ip(handler):
    return (handler.headers.get("X-Forwarded-For") or handler.client_address[0]
            or "?").split(",")[0].strip()


def _public_base(handler):
    env = os.environ.get("KOLWATCH_PUBLIC_URL", "")
    if env:
        return env.rstrip("/")
    proto = (handler.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
    host = (handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host")
            or f"127.0.0.1:{PORT}").split(",")[0].strip()
    return f"{proto}://{host}"


def _cookie_attrs(handler, max_age):
    secure = "; Secure" if _public_base(handler).startswith("https") else ""
    return f"; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}{secure}"


def _html():
    with open(os.path.join(HERE, "static", "index.html"), encoding="utf-8") as f:
        return f.read()


def api_state():
    chans = db.list_channels()
    from collections import Counter
    by_plat = Counter(c["platform"] for c in chans)
    blocked = [c for c in chans if c["status"] == "blocked"]
    return {
        "counts": {
            "kols": len({c["kol_id"] for c in chans}),
            "channels": len(chans),
            "by_platform": dict(by_plat),
            "blocked": len(blocked),
        },
        "hits": db.list_hits(kind="competitor"),
        "brand_hits": db.list_hits(kind="brand"),
        "unresolved": db.list_unresolved(),
        "line_sources": db.list_line_sources(),
        "blocked": [{"kol": c["kol_name"], "platform": c["platform"],
                     "url": c["profile_url"], "error": c.get("last_error")}
                    for c in blocked],
        "progress": scheduler.PROGRESS,
        "settings": {
            "run_times": db.get_setting("run_times", "08:00,16:00"),
            "auto_enabled": db.get_setting("auto_enabled", "1"),
            "sheet_id": db.get_setting("sheet_id", ""),
            "sheet_gid": db.get_setting("sheet_gid", "401523688"),
            "line_token_set": bool(db.get_setting("line_token", "")),
            "line_last_error": db.get_setting("line_last_error", ""),
            "line_token_minted_at": db.get_setting("line_token_minted_at", ""),
            "line_to": db.get_setting("line_to", ""),
            "line_mode": db.get_setting("line_mode", "broadcast"),
            "posts_per_check": db.get_setting("posts_per_check", "3"),
            "enable_bd_fallback": db.get_setting("enable_bd_fallback", "0"),
            "scan_platforms": db.get_setting("scan_platforms",
                "youtube,lemon8,tiktok,instagram,facebook,x,threads"),
            "client_brands": db.get_setting("client_brands", "{}"),
            "brand_kols_per_bubble": db.get_setting("brand_kols_per_bubble", "5"),
            "brand_digest_max_age_days": db.get_setting("brand_digest_max_age_days", "3"),
            "watchlist": db.get_setting("watchlist", "{}"),
            "watchlist_unbranded": db.get_setting("watchlist_unbranded", "{}"),
            "skip_channels": db.get_setting("skip_channels", "{}"),
            "dca_report_enabled": db.get_setting("dca_report_enabled", "1"),
            "dca_report_time": db.get_setting("dca_report_time", "10:00,16:00"),
            "dca_last_run": db.get_setting("dca_last_run", ""),
            "dca_selftest": db.get_setting("dca_selftest", ""),
            "dca_campaigns": db.get_setting("dca_campaigns", ""),
            "dca_links": db.get_setting("dca_links", ""),
            "ai_review_enabled": db.get_setting("ai_review_enabled", "1"),
            "ai_alerts_enabled": db.get_setting("ai_alerts_enabled", "1"),
            "ai_alert_max_age_days": db.get_setting("ai_alert_max_age_days", "3"),
            "ai_review_policy": db.get_setting("ai_review_policy", ""),
        },
        "ai": ai_review.status(),
    }


def api_sync(payload):
    sid = (payload.get("sheet_id") or db.get_setting("sheet_id", "") or "").strip()
    if not sid:
        return {"error": "sheet_id required"}, 400
    db.set_setting("sheet_id", sid)
    try:
        res = registry.sync_from_sheet(sid)
    except Exception as e:
        return {"error": f"sync failed: {e}"}, 502
    return {"ok": True, **res}, 200


def api_run():
    if scheduler.PROGRESS["running"]:
        return {"error": "run already in progress"}, 409
    threading.Thread(target=scheduler.run_once, args=("manual",),
                     daemon=True).start()
    return {"ok": True}, 200


def api_stop():
    return ({"ok": True} if scheduler.request_stop()
            else {"error": "no run in progress"}), 200


def api_brand_resend():
    """Re-run the brand digest for unsent brand hits (notified=0) within
    the recency window. Used when the prior scan's LINE send failed."""
    pending = db.unnotified_brand_hits()
    if not pending:
        return {"ok": True, "pending": 0, "sent": 0}, 200
    try:
        max_age_days = int(db.get_setting("brand_digest_max_age_days", "3"))
    except ValueError:
        max_age_days = 3
    from datetime import datetime as _dt, timedelta as _td
    cutoff = (_dt.now(scheduler.TH_TZ) - _td(days=max_age_days)).date()
    fresh, stale = [], []
    for h in pending:
        pa = (h.get("posted_at") or "")[:10]
        try:
            dd = _dt.strptime(pa, "%Y-%m-%d").date()
        except ValueError:
            dd = None
        (fresh if (dd is None or dd >= cutoff) else stale).append(h)
    for h in stale:
        db.mark_notified(h["id"])
    if not fresh:
        return {"ok": True, "pending": len(pending), "fresh": 0,
                "stale_archived": len(stale), "sent": 0}, 200
    bubbles = scheduler._build_brand_bubbles(fresh)
    import notify_line
    ok, msg = notify_line.send_texts(bubbles)
    if ok:
        for h in fresh:
            db.mark_notified(h["id"])
    return {"ok": ok, "pending": len(pending), "fresh": len(fresh),
            "stale_archived": len(stale),
            "sent": len(fresh) if ok else 0,
            "bubbles": len(bubbles), "line_status": msg}, 200


def api_dedupe_hits():
    """Collapse duplicate hit rows (same channel+keyword+post_id) that were
    created before the dedup fix when Apify returned a slightly different URL
    each scan. Keeps the oldest row per group."""
    import sqlite3
    with db._lock, db._conn() as c:
        before = c.execute("SELECT COUNT(*) FROM hit").fetchone()[0]
        c.execute("""
            DELETE FROM hit
            WHERE id NOT IN (
              SELECT MIN(id) FROM hit
              WHERE post_id IS NOT NULL AND post_id != ''
              GROUP BY channel_id, matched_keyword, post_id
            ) AND post_id IS NOT NULL AND post_id != ''
        """)
        after = c.execute("SELECT COUNT(*) FROM hit").fetchone()[0]
    return {"ok": True, "before": before, "after": after,
            "removed": before - after}, 200


def api_health_check():
    if scheduler.HEALTH["running"]:
        return {"error": "health-check already running"}, 409
    threading.Thread(target=scheduler.run_health_check, daemon=True).start()
    return {"ok": True}, 200


def api_health_status():
    return scheduler.HEALTH, 200


def api_import_posts(payload):
    """Load old posts fetched outside the hourly scan (the year backfill,
    San 22 Nov 2026: "สแกนพวก Exclusive ให้เคลียร์ เอาลง DB ให้หมด").

    payload: {"posts": [{"channel_id", "post_id", "url", "posted_at",
              "caption", "hashtags": [...]}]}  (≤ 3000 per call)
    Each post goes through the same dedupe and keyword match as a scan,
    but: scanned_at = posted_at, so the backlog does not flood the AI
    reviewer queue or today's counts; hits are saved notified=1, so the
    team's LINE gets no cards for them — they show on the web page."""
    import matcher
    posts = payload.get("posts") or []
    if not isinstance(posts, list) or len(posts) > 3000:
        return {"error": "posts: list of at most 3000"}, 400
    chans = {c["id"]: c for c in db.list_channels(active_only=False)}
    brand_wl = matcher.get_client_brands()
    unbranded_wl = matcher.get_unbranded_watchlist()
    out = {"received": len(posts), "added": 0, "dupe": 0, "no_channel": 0,
           "competitor_hits": 0, "brand_hits": 0}
    for p in posts:
        ch = chans.get(p.get("channel_id"))
        pid = str(p.get("post_id") or "").strip()
        if not ch or not pid:
            out["no_channel"] += 1
            continue
        cap = (p.get("caption") or "").strip()
        tags = [t for t in (p.get("hashtags") or []) if t]
        key = cap if len(cap) >= 20 else ""
        if db.post_seen(ch["id"], pid, key):
            out["dupe"] += 1
            continue
        is_excl = "non" not in (ch.get("kol_exclusivity") or "").lower()
        found = matcher.scan(cap, tags) if is_excl else []
        bf = matcher.scan(cap, tags, watchlist=brand_wl) if brand_wl else []
        if is_excl and unbranded_wl and not bf and not found:
            broad = matcher.scan(cap, tags, watchlist=unbranded_wl)
            sig = matcher.promo_signal(cap, tags) if broad else ""
            if sig:
                for f in broad:
                    f["promo_signal"] = sig
                found += broad
        posted = p.get("posted_at") or ""
        db.import_seen(ch["id"], pid, p.get("url") or "", posted, cap, tags,
                       matched=bool(found), scanned_at=posted or "2026-01-01")
        out["added"] += 1
        for kind, fs in (("competitor", found), ("brand", bf)):
            for f in fs:
                if db.hit_exists_for(ch["id"], f["keyword"],
                                     p.get("url") or "", pid):
                    continue
                hid = db.add_hit(ch["id"], ch["kol_name"], ch["platform"],
                                 p.get("url") or "", pid, posted,
                                 f["keyword"], f["snippet"], kind=kind)
                if hid:
                    db.mark_backfill_hit(hid, posted)
                    out[kind + "_hits"] += 1
    return out, 200


def api_brand_backfill():
    """Re-scan every already-collected post against the client_brands list
    and record brand hits. Marks them notified=1 so we don't blast LINE
    with historical posts — they just populate the report tab."""
    import matcher
    brand = matcher.get_client_brands()
    if not brand:
        return {"error": "client_brands ว่าง — ตั้งค่าก่อน"}, 400
    added = scanned = 0
    for r in db.all_seen_with_meta():
        scanned += 1
        tags = (r.get("hashtags") or "").split()
        for f in matcher.scan(r["caption"], tags, watchlist=brand):
            if db.hit_exists_for(r["channel_id"], f["keyword"],
                                 r["post_url"], r["post_id"]):
                continue
            hid = db.add_hit(r["channel_id"], r["kol_name"], r["platform"],
                             r["post_url"], r["post_id"], r["posted_at"],
                             f["keyword"], f["snippet"], kind="brand")
            if hid:
                db.mark_notified(hid)
                added += 1
    return {"ok": True, "scanned": scanned, "added": added}, 200


_VERDICT_TH = {"competitor": "🚨 คู่แข่ง", "pass": "✓ ผ่าน"}


def _line_postback(ev):
    """คู่แข่ง / ผ่าน button on an alert card (alert_card.py)."""
    q = parse_qs((ev.get("postback") or {}).get("data") or "")
    if (q.get("kw") or [""])[0] != "v":
        return
    verdict = (q.get("v") or [""])[0]
    if verdict not in _VERDICT_TH:
        return
    ids = [x for x in (q.get("h") or [""])[0].split(",") if x.strip()]
    uid = (ev.get("source") or {}).get("userId") or ""
    who = notify_line.display_name(uid)
    res = db.set_verdict(ids, verdict, who)
    if not res:
        notify_line.reply_text(ev.get("replyToken"),
                               "ไม่เจอรายการนี้ในระบบแล้ว")
        return
    msg = (f"บันทึกแล้ว: {res['kol']} ({res['platform']}) = "
           f"{_VERDICT_TH[verdict]} · โดย {who}")
    p = res.get("prev")
    if p and (p.get("verdict") != verdict or p.get("by") != who):
        when = ""
        try:
            when = scheduler.datetime.fromisoformat(p["at"]).astimezone(
                scheduler.TH_TZ).strftime("%d/%m %H:%M")
        except Exception:
            pass
        msg += (f"\n(ก่อนหน้านี้ {p.get('by') or '-'} กด "
                f"{_VERDICT_TH.get(p.get('verdict'), p.get('verdict'))} {when})")
    if verdict == "competitor":
        msg += "\nรายการอยู่ในหน้าเว็บ แท็บเจอคำคู่แข่ง"
    notify_line.reply_text(ev.get("replyToken"), msg)


def api_hit_status(payload):
    hid = payload.get("id")
    status = payload.get("status")
    if not hid or status not in ("new", "contacted", "removed", "ignored"):
        return {"error": "id + valid status required"}, 400
    db.set_hit_status(int(hid), status)
    return {"ok": True}, 200


def api_dca_report_test():
    """Send the content-status report right now (manual trigger / smoke test)."""
    import dca_report
    try:
        bubbles = dca_report.build_messages()
        ok, msg = dca_report.send_report()
        return {"ok": ok, "msg": msg, "bubbles": len(bubbles)}, 200
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": str(e)[:300]}, 200


def api_settings(payload):
    for k in ("run_times", "auto_enabled", "sheet_id", "sheet_gid", "line_to",
              "line_mode", "posts_per_check", "watchlist",
              "enable_bd_fallback", "scan_platforms",
              "client_brands", "brand_kols_per_bubble", "watchlist_exclude",
              "watchlist_unbranded", "watchlist_promo_signals", "skip_channels",
              "brand_digest_max_age_days",
              "dca_report_enabled", "dca_report_time", "dca_sheet_id",
              "dca_campaigns", "dca_links",
              "ai_review_enabled", "ai_alerts_enabled",
              "ai_alert_max_age_days", "ai_review_policy",
              "extra_channels", "exclusive_override", "hr_users"):
        if k in payload:
            db.set_setting(k, payload[k])
    if payload.get("line_token"):
        db.set_setting("line_token", payload["line_token"])
    return {"ok": True}, 200


API_DOCS = {
    "auth": "Header 'Authorization: Bearer <API key>' (or X-API-Key), or log in on the web",
    "GET": {
        "/api/state": "overview: KOLs, channels, hits, settings, last runs",
        "/api/overview": "home page numbers: tiles, 14-day series, items needing attention",
        "/api/runs": "scan log — every scan round (plus older rounds rebuilt from post timestamps)",
        "/api/run_detail?id=": "posts / hits / failed channels of one scan round",
        "/api/keywords": "watched competitor words, broad words, client brands, hit counts",
        "/api/channels": "every monitored channel grouped by KOL (Exclusive first)",
        "/api/post_counts": "posts per channel per month (?year=2026)",
        "/api/blocked": "channels that could not be checked, with reason + fix",
        "/api/posts_q?q=&platform=&excl=excl|non&kol=&flag=hit|brand|urgent|watch&from=&to=&sort=&page=&size=": "search the post database",
        "/api/post?id=": "one post with its hits and AI verdict",
        "/api/takedowns": "links the team asked KOLs to delete/edit + real check (gone / live / edited)",
        "/api/posts": "every scanned post (JSON)",
        "/api/posts.csv": "every scanned post (CSV, opens in Excel/Sheets)",
        "/api/export": "every table in the database (JSON, LINE token removed)",
        "/api/export.db": "full SQLite database file",
        "/api/dca_phases": "campaign phases in the Webull content-status sheet",
        "/api/health_check": "last channel health check",
        "/api/ai/reviews?days=7&levels=urgent,watch": "posts the AI flagged (competitor / foreign-stock trading)",
        "/api/ai/pending?limit=5": "(agent) next posts for the AI to read",
    },
    "POST": {
        "/api/settings": "change settings (watchlist, watchlist_unbranded, run_times, dca_campaigns, ...)",
        "/api/sync": "re-sync KOL list from the Google Sheet",
        "/api/run": "run a full scan now (costs Apify credit)",
        "/api/stop": "stop a running scan",
        "/api/hit_status": "mark a hit handled {id, status}",
        "/api/takedowns": "upsert takedown links {items:[{url, kol, platform, brand, action, sheet_status, ...}]}",
        "/api/takedowns/recheck": "re-check notified takedown links now (free, no Apify) {ids?}",
        "/api/dca_report_test": "send the daily report to LINE NOW (broadcasts!)",
        "/api/ai/review": "(agent) save AI verdicts {model, results:[{seen_post_id, level, competitors, ...}]}",
    },
}


_q1 = lambda q, k, d="": (q.get(k) or [d])[0]
DASH_GET = {
    "/api/overview": lambda q: dash_api.overview(),
    "/api/progress": lambda q: {"progress": scheduler.PROGRESS,
                                "next": dash_api.next_run(),
                                "last": (db.list_scan_runs(1) or [None])[0],
                                "last_check": dash_api.last_check()},
    "/api/runs": lambda q: dash_api.runs(int(_q1(q, "limit", "2000"))),
    "/api/run_detail": lambda q: dash_api.run_detail(_q1(q, "id")),
    "/api/keywords": lambda q: dash_api.keywords(),
    "/api/channels": lambda q: dash_api.channels(),
    "/api/post_counts": lambda q: dash_api.post_counts(_q1(q, "year", "2026") or 2026),
    "/api/blocked": lambda q: dash_api.blocked(),
    "/api/tables": lambda q: dash_api.tables(),
    "/api/posts_q": lambda q: dash_api.posts_query(q),
    "/api/post": lambda q: dash_api.post_detail(int(_q1(q, "id", "0") or 0)),
    "/api/takedowns": lambda q: takedown.listing(),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _send_html(self, body, code=200, extra_headers=None):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra_headers or {}):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _redirect(self, location, extra_headers=None):
        self.send_response(302)
        self.send_header("Location", location)
        for k, v in (extra_headers or {}):
            self.send_header(k, v)
        self.end_headers()

    def _login_ok(self, username, name, via):
        tok = hr_auth.session_cookie(username, name, via)
        self._redirect("/", extra_headers=[
            ("Set-Cookie", "kw_auth=" + tok + _cookie_attrs(self, hr_auth.SESSION_TTL)),
            ("Set-Cookie", "kw_sso=" + _cookie_attrs(self, 0))])

    def _sso_callback(self):
        q = parse_qs(urlparse(self.path).query)
        m = _cookies(self).get("kw_sso")
        st = hr_auth.unsign(m.value) if m else None
        state = (q.get("state") or [""])[0]
        if not st or not hmac.compare_digest(state, st.get("s", "x")):
            self._redirect("/login?e=failed")
            return
        u = hr_auth.exchange((q.get("code") or [""])[0],
                             _public_base(self) + "/sso/callback")
        if not u:
            self._redirect("/login?e=failed")
            return
        if not hr_auth.is_allowed(u["username"]):
            print(f"[auth] LINE login denied (not on list): {u['username']}", flush=True)
            self._redirect("/login?e=denied")
            return
        hr_auth.remember(u, "line")
        print(f"[auth] LINE login: {u['username']}", flush=True)
        self._login_ok(u["username"].lower(), hr_auth.display_name(u), "line")

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/login":
                q = parse_qs(urlparse(self.path).query)
                msg = {"failed": "เข้าด้วย LINE ไม่สำเร็จ ลองอีกครั้งหรือใช้รหัสผ่าน SO! HR",
                       "denied": "บัญชีนี้ยังไม่ได้รับสิทธิ์ใช้ KOL Watch (แจ้ง San)"
                       }.get((q.get("e") or [""])[0], "")
                self._send_html(login_page(msg))
                return
            if path == "/sso/start":
                state = secrets.token_urlsafe(16)
                self._redirect(
                    hr_auth.authorize_url(_public_base(self) + "/sso/callback", state),
                    extra_headers=[("Set-Cookie", "kw_sso=" + hr_auth.sign(
                        {"s": state, "exp": int(time.time()) + 600})
                        + _cookie_attrs(self, 600))])
                return
            if path == "/sso/callback":
                self._sso_callback()
                return
            if path == "/logout":
                self._redirect("/login", extra_headers=[
                    ("Set-Cookie", "kw_auth=" + _cookie_attrs(self, 0))])
                return
            if not _authed(self):
                self._redirect("/login")
                return
            if path == "/":
                body = _html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                self._json(200, api_state())
            elif path == "/api/me":
                u = current_user(self)
                self._json(200, {"user": u and {"username": u["u"], "name": u.get("n"),
                                                "via": u.get("via")},
                                 "api_key": _api_key_user(self)})
            elif path == "/api/users":
                self._json(200, {"users": hr_auth.list_users(),
                                 "allowed": hr_auth.allowed_users()})
            elif path in DASH_GET:
                q = parse_qs(urlparse(self.path).query)
                self._json(200, DASH_GET[path](q))
            elif path == "/api/posts":
                self._json(200, {"posts": db.list_all_posts()})
            elif path == "/api/health_check":
                self._json(200, scheduler.HEALTH)
            elif path == "/api/dca_phases":
                import dca_report
                self._json(200, {"phases": dca_report.list_phases(),
                                 "reporting": dca_report.get_campaigns()})
            elif path == "/api/posts.csv":
                self._send_csv()
            elif path == "/api/export":
                self._json(200, db.export_all())
            elif path == "/api/export.db":
                self._send_db()
            elif path == "/api/docs":
                self._json(200, API_DOCS)
            elif path == "/api/ai/pending":
                q = parse_qs(urlparse(self.path).query)
                self._json(200, ai_review.pending(
                    (q.get("limit") or ["5"])[0], _api_key_user(self) or "web"))
            elif path == "/api/ai/reviews":
                q = parse_qs(urlparse(self.path).query)
                lv = tuple(x for x in (q.get("levels") or ["urgent,watch"])[0]
                           .split(",") if x in ai_review.LEVELS) or ("urgent", "watch")
                self._json(200, {"status": ai_review.status(),
                                 "reviews": db.list_ai_reviews(
                                     lv, int((q.get("days") or ["14"])[0]))})
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"error": str(e)})

    def _send_db(self):
        import sqlite3, tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        src = sqlite3.connect(db.DB_PATH, timeout=30)
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        src.close()
        dst.execute("DELETE FROM settings WHERE key IN ('line_token')")
        dst.commit(); dst.close()
        with open(tmp, "rb") as f:
            body = f.read()
        os.unlink(tmp)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-sqlite3")
        self.send_header("Content-Disposition",
                         'attachment; filename="kol_watch.db"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_csv(self):
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["kol_name", "platform", "posted_at", "post_url",
                    "caption", "hashtags", "matched", "scanned_at"])
        for p in db.list_all_posts():
            w.writerow([p["kol_name"], p["platform"], p.get("posted_at", ""),
                        p.get("post_url", ""),
                        (p.get("caption") or "").replace("\n", " "),
                        p.get("hashtags", ""), p.get("matched", 0),
                        p.get("scanned_at", "")])
        body = ("﻿" + buf.getvalue()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         'attachment; filename="kol_posts.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        # LINE webhook — open endpoint (LINE servers can't log in). Captures
        # source ids (user/group/room) so the operator can pick one in UI.
        if path == "/webhook":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(n) if n else b""
                body = raw.decode("utf-8") if raw else ""
                data = json.loads(body) if body else {}
                signed = notify_line.verify_signature(
                    raw, self.headers.get("X-Line-Signature", ""))
                for ev in (data.get("events") or []):
                    if ev.get("type") == "postback" and signed:
                        _line_postback(ev)
                        continue
                    src = ev.get("source") or {}
                    st = src.get("type") or ""
                    sid = src.get(f"{st}Id") if st else None
                    if not sid:
                        continue
                    msg = ev.get("message") or {}
                    db.record_line_source(
                        sid, st,
                        last_user=src.get("userId") or "",
                        last_text=(msg.get("text") or "")[:120])
            except Exception:
                traceback.print_exc()
            # LINE just wants a 200 — body content is ignored.
            self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
            return
        if path == "/login":
            n = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(n).decode("utf-8")) if n else {}
            u = (form.get("username", [""])[0]).strip()
            p = (form.get("password", [""])[0])
            ip = _client_ip(self)
            rec = _FAILS.get(ip)
            if rec and rec[0] >= 10 and time.time() - rec[1] < 300:
                self._send_html(login_page("ลองผิดหลายครั้งเกินไป รอ 5 นาทีแล้วลองใหม่"), code=429)
                return
            if u in USERS and _check_login(u, p):     # shared env account (legacy)
                _FAILS.pop(ip, None)
                self._login_ok(u, u, "account")
                return
            user, err = hr_auth.verify_password(u.lower(), p) if u and p else (
                None, "กรอกชื่อผู้ใช้และรหัสผ่าน SO! HR")
            if user and not hr_auth.is_allowed(user["username"]):
                print(f"[auth] password login denied (not on list): {user['username']}", flush=True)
                self._send_html(login_page("บัญชีนี้ยังไม่ได้รับสิทธิ์ใช้ KOL Watch (แจ้ง San)"), code=403)
                return
            if user:
                _FAILS.pop(ip, None)
                hr_auth.remember(user, "password")
                print(f"[auth] password login: {user['username']}", flush=True)
                self._login_ok(user["username"].lower(), hr_auth.display_name(user), "password")
                return
            if not rec or time.time() - rec[1] >= 300:
                rec = [0, time.time()]
            rec[0] += 1
            _FAILS[ip] = rec
            self._send_html(login_page(err), code=401)
            return
        if not _authed(self):
            self._json(401, {"error": "unauthorized"})
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            payload = {}
        try:
            if path == "/api/sync":
                r, c = api_sync(payload)
            elif path == "/api/run":
                r, c = api_run()
            elif path == "/api/stop":
                r, c = api_stop()
            elif path == "/api/import_posts":
                r, c = api_import_posts(payload)
            elif path == "/api/brand_backfill":
                r, c = api_brand_backfill()
            elif path == "/api/health_check":
                r, c = api_health_check()
            elif path == "/api/dedupe_hits":
                r, c = api_dedupe_hits()
            elif path == "/api/brand_resend":
                r, c = api_brand_resend()
            elif path == "/api/hit_status":
                r, c = api_hit_status(payload)
            elif path == "/api/settings":
                r, c = api_settings(payload)
            elif path == "/api/dca_report_test":
                r, c = api_dca_report_test()
            elif path == "/api/ai/review":
                r, c = ai_review.save(payload), 200
            elif path == "/api/takedowns":
                r, c = takedown.upsert(payload.get("items") or []), 200
            elif path == "/api/takedowns/recheck":
                r, c = takedown.recheck_async(ids=payload.get("ids")), 200
            else:
                r, c = {"error": "not found"}, 404
            self._json(c, r)
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"error": str(e)})


def main():
    if not USERS and not os.environ.get("HR_SSO_KEY"):
        import sys
        sys.exit("Set HR_SSO_KEY (SO! HR login) or KOLWATCH_USER/KOLWATCH_PASS "
                 "before starting.")
    db.init()
    hr_auth.init()
    stop = threading.Event()
    threading.Thread(target=scheduler.scheduler_thread, args=(stop,),
                     daemon=True).start()
    takedown.init()
    threading.Thread(target=takedown.scheduler_thread, args=(stop,),
                     daemon=True).start()
    # Local default 127.0.0.1; on a hosted env (Railway sets PORT) bind
    # 0.0.0.0 so the platform's router can reach us. Override via env.
    default_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    host = os.environ.get("KOLWATCH_HOST", default_host)
    srv = ThreadingHTTPServer((host, PORT), Handler)
    srv.daemon_threads = True
    url = f"http://{host}:{PORT}"
    print(f"KOL Watch running at {url}")
    if host == "127.0.0.1":
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
