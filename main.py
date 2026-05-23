"""KOL Competitor-Keyword Watch — local web UI on http://127.0.0.1:5070"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import traceback
import webbrowser
from http.cookies import SimpleCookie
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import db
import registry
import scheduler

# Railway injects PORT; locally use KOLWATCH_PORT (default 5070).
PORT = int(os.environ.get("PORT") or os.environ.get("KOLWATCH_PORT") or "5070")
HERE = os.path.dirname(os.path.abspath(__file__))

# Dashboard credentials — must be supplied via env (no defaults so no
# secret ever lives in the repo). See .env.example.
AUTH_USER = os.environ.get("KOLWATCH_USER", "")
AUTH_PASS = os.environ.get("KOLWATCH_PASS", "")
_SESSIONS = set()


def _new_session():
    tok = secrets.token_urlsafe(32)
    _SESSIONS.add(tok)
    return tok


def _authed(handler):
    cookie = SimpleCookie(handler.headers.get("Cookie", ""))
    m = cookie.get("kw_session")
    return bool(m and m.value in _SESSIONS)


LOGIN_HTML = """<!DOCTYPE html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>เข้าสู่ระบบ — KOL Watch</title><style>
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.box{background:#1e293b;padding:32px;border-radius:12px;width:300px}
h1{font-size:18px;margin:0 0 18px}label{font-size:12px;color:#94a3b8;display:block;margin:10px 0 4px}
input{width:100%;box-sizing:border-box;background:#0f172a;border:1px solid #334155;color:#e2e8f0;
border-radius:6px;padding:9px;font:inherit}button{width:100%;margin-top:18px;background:#3b82f6;
color:#fff;border:0;padding:10px;border-radius:6px;cursor:pointer;font-size:14px}
.err{color:#f87171;font-size:13px;margin-top:12px;__ERR__}</style></head><body>
<form class="box" method="POST" action="/login">
<h1>🛡️ KOL Competitor Watch</h1>
<label>Username</label><input name="username" autofocus autocomplete="username">
<label>Password</label><input name="password" type="password" autocomplete="current-password">
<button>เข้าสู่ระบบ</button><div class="err">ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง</div>
</form></body></html>"""


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
        "hits": db.list_hits(),
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
            "line_token_set": bool(db.get_setting("line_token", "")),
            "line_to": db.get_setting("line_to", ""),
            "line_mode": db.get_setting("line_mode", "broadcast"),
            "posts_per_check": db.get_setting("posts_per_check", "3"),
            "enable_bd_fallback": db.get_setting("enable_bd_fallback", "0"),
            "scan_platforms": db.get_setting("scan_platforms",
                "youtube,lemon8,tiktok,instagram,facebook,x"),
            "watchlist": db.get_setting("watchlist", "{}"),
        },
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


def api_hit_status(payload):
    hid = payload.get("id")
    status = payload.get("status")
    if not hid or status not in ("new", "contacted", "removed"):
        return {"error": "id + valid status required"}, 400
    db.set_hit_status(int(hid), status)
    return {"ok": True}, 200


def api_settings(payload):
    for k in ("run_times", "auto_enabled", "sheet_id", "line_to",
              "line_mode", "posts_per_check", "watchlist",
              "enable_bd_fallback", "scan_platforms"):
        if k in payload:
            db.set_setting(k, payload[k])
    if payload.get("line_token"):
        db.set_setting("line_token", payload["line_token"])
    return {"ok": True}, 200


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

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/login":
                self._send_html(LOGIN_HTML.replace("__ERR__", "display:none"))
                return
            if not _authed(self):
                self._redirect("/login")
                return
            if path == "/logout":
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                m = cookie.get("kw_session")
                if m:
                    _SESSIONS.discard(m.value)
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
            elif path == "/api/posts":
                self._json(200, {"posts": db.list_all_posts()})
            elif path == "/api/posts.csv":
                self._send_csv()
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"error": str(e)})

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
                body = self.rfile.read(n).decode("utf-8") if n else ""
                data = json.loads(body) if body else {}
                for ev in (data.get("events") or []):
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
            u = (form.get("username", [""])[0])
            p = (form.get("password", [""])[0])
            if hmac.compare_digest(u, AUTH_USER) and hmac.compare_digest(p, AUTH_PASS):
                tok = _new_session()
                self._redirect("/", extra_headers=[
                    ("Set-Cookie",
                     f"kw_session={tok}; HttpOnly; SameSite=Lax; Path=/")])
            else:
                self._send_html(LOGIN_HTML.replace("__ERR__", ""), code=401)
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
            elif path == "/api/hit_status":
                r, c = api_hit_status(payload)
            elif path == "/api/settings":
                r, c = api_settings(payload)
            else:
                r, c = {"error": "not found"}, 404
            self._json(c, r)
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"error": str(e)})


def main():
    if not AUTH_USER or not AUTH_PASS:
        import sys
        sys.exit("Set KOLWATCH_USER and KOLWATCH_PASS (see .env.example) "
                 "before starting.")
    db.init()
    stop = threading.Event()
    threading.Thread(target=scheduler.scheduler_thread, args=(stop,),
                     daemon=True).start()
    # Local default 127.0.0.1; on a hosted env (Railway sets PORT) bind
    # 0.0.0.0 so the platform's router can reach us. Override via env.
    default_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    host = os.environ.get("KOLWATCH_HOST", default_host)
    srv = HTTPServer((host, PORT), Handler)
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
