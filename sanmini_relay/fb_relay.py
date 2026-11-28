#!/usr/bin/env python3
"""fb_relay — read Facebook pages that need a login and push their posts to
KOL Watch (/api/relay_posts). Runs on sanmini a few times a day.

Why: ม้าเฉียว's page facebook.com/machaodohoon is age-restricted, so every
logged-out scraper (Apify, Bright Data) gets "not_available" (28 Nov 2026).
A spare FB account that is logged in sees it fine.

    python3 fb_relay.py login [--copy-to sanmini]
        On a Mac with a screen. Opens Chrome on facebook.com/login: log in
        with the SPARE account (never anyone's real account). When the
        login is detected the cookies are saved to
        ~/.kolwatch/fb_relay_cookies.json and, with --copy-to, copied there.
    python3 fb_relay.py check     logged in? (headless)
    python3 fb_relay.py run [--scroll N] [--dry] [--jitter MIN]
        Headless: open every page in the config, collect posts, push them.

Config ~/.kolwatch/fb_relay.json:
    {"api": "https://kol-watch-production.up.railway.app",
     "key": "<a KOLWATCH_API_KEYS key>",
     "pages": ["https://www.facebook.com/machaodohoon"]}

Only FB's own responses to the page we open are read (the GraphQL the page
loads while scrolling + the JSON embedded in the HTML). Nothing is replayed
or called directly, and it scrolls like a person a few times a day.
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
SCRAPEKIT = os.environ.get("SCRAPEKIT_DIR",
                           os.path.join(HOME, "scrapekit"))
if not os.path.isdir(SCRAPEKIT):
    SCRAPEKIT = os.path.join(HOME, "Desktop", "Claude Code", "scrapekit")
sys.path.insert(0, SCRAPEKIT)

from scrapekit.browser import ScrapeKit  # noqa: E402
from scrapekit.adapters.facebook import Facebook  # noqa: E402

CONF = os.path.join(HOME, ".kolwatch", "fb_relay.json")
COOKIES = os.path.join(HOME, ".kolwatch", "fb_relay_cookies.json")
PROFILE = os.path.join(HOME, ".scrapekit", "fb-relay-profile")
PORT = 9241            # own port: never attach to another job's Chrome
TH = timezone(timedelta(hours=7))
_COOKIE_FIELDS = ("name", "value", "domain", "path", "secure", "httpOnly",
                  "sameSite", "expires")


def log(*a):
    print(datetime.now(TH).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def load_conf():
    with open(CONF, encoding="utf-8") as f:
        c = json.load(f)
    if not (c.get("api") and c.get("key") and c.get("pages")):
        raise SystemExit(f"{CONF}: api, key and pages are required")
    return c


# ---------------------------------------------------------------- cookies
def _browser_call(page, method, params=None):
    """CDP call on the BROWSER target, not the tab: cookies live in the
    browser, and the tab we opened can be closed or swapped while someone
    logs in (28 Nov 2026: "Session with given id not found")."""
    b = page._b
    return b._runner.run(b._conn.send(method, params or {}))


def get_cookies(page):
    res = _browser_call(page, "Storage.getCookies")
    return [c for c in res.get("cookies", [])
            if (c.get("domain") or "").lstrip(".").endswith("facebook.com")]


def save_cookies(page):
    cs = [{k: c[k] for k in _COOKIE_FIELDS if k in c} for c in get_cookies(page)]
    if not any(c["name"] == "c_user" for c in cs):
        return False
    os.makedirs(os.path.dirname(COOKIES), exist_ok=True)
    tmp = COOKIES + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cs, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, COOKIES)
    return True


def load_cookies(page):
    if not os.path.exists(COOKIES):
        return False
    with open(COOKIES) as f:
        cs = json.load(f)
    _browser_call(page, "Storage.setCookies", {"cookies": cs})
    return True


def logged_in(page):
    return any(c["name"] == "c_user" for c in get_cookies(page))


# ---------------------------------------------------------------- reading
_INLINE_JS = r"""
Array.from(document.querySelectorAll('script[type="application/json"]'))
  .map(s => s.textContent)
  .filter(t => t && (t.includes('creation_time') || t.includes('"post_id"')))
"""


def read_page(page, url, scroll):
    """Open `url`, scroll, return (posts, page_problem)."""
    chunks, lock = [], threading.Lock()

    def on_resp(r):
        if "/graphql" not in r.url:
            return
        try:
            body = r.body()
        except Exception:
            return
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                with lock:
                    chunks.append({"url": r.url, "json": obj})

    page._response_cbs = [on_resp]
    page.goto(url, wait_until="load", timeout=40)
    time.sleep(4 + random.random() * 2)
    cur = page.evaluate("location.href") or ""
    body = (page.evaluate("document.body ? document.body.innerText.slice(0, 3000) : ''")
            or "")
    if "/login" in cur or "/checkpoint" in cur:
        return [], "บัญชี FB สำรองหลุดล็อกอิน หรือติดหน้ายืนยันตัวตน (checkpoint)"
    if ("This content isn't available" in body or "เนื้อหานี้ไม่พร้อมใช้งาน" in body):
        return [], "เพจเปิดไม่ได้แม้ล็อกอินแล้ว (เพจถูกลบ ซ่อน หรือบล็อกบัญชีสำรอง)"
    for _ in range(scroll):
        page.evaluate("window.scrollBy(0, Math.round(window.innerHeight * "
                      f"{1.6 + random.random():.2f}))")
        time.sleep(2.5 + random.random() * 2.5)
    time.sleep(2)
    for t in page.evaluate(_INLINE_JS) or []:
        try:
            chunks.append({"url": "inline", "json": json.loads(t)})
        except Exception:
            pass
    page._response_cbs = []
    return Facebook._dig_posts(chunks), ""


def shape(raw, page_url):
    """scrapekit post → KOL Watch post. Keeps only the page's own posts."""
    handle = page_url.rstrip("/").split("/")[-1].lower()
    names = {}
    for p in raw:
        if p.get("author"):
            names[p["author"]] = names.get(p["author"], 0) + 1
    owner = max(names, key=names.get) if names else None
    out = []
    for p in raw:
        au = (p.get("author_url") or "").lower()
        if owner and p.get("author") not in (owner, None) and handle not in au:
            continue   # someone else's post on the timeline / a shared post
        ts = p.get("created_time")
        posted = (datetime.fromtimestamp(ts, TH).isoformat(timespec="seconds")
                  if ts else "")
        text = p.get("text") or ""
        out.append({
            "post_id": str(p["post_id"]),
            "url": p.get("permalink") or f"https://www.facebook.com/{p['post_id']}",
            "posted_at": posted,
            "caption": text,
            "hashtags": re.findall(r"#[^\s#]+", text),
        })
    return out


def push(conf, page_url, posts, ok=True, error=""):
    body = json.dumps({"profile_url": page_url, "ok": ok, "error": error,
                       "source": "sanmini fb_relay", "posts": posts},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        conf["api"].rstrip("/") + "/api/relay_posts", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + conf["key"]})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- commands
def cmd_login(args):
    b = ScrapeKit.launch(port=PORT, profile_dir=PROFILE, headless=False)
    try:
        page = b.new_page()
        page.goto("https://www.facebook.com/login", wait_until="load")
        log("ล็อกอินบัญชี FB สำรองในหน้าต่าง Chrome ที่เปิดขึ้นมา (รอได้ 15 นาที)")
        for _ in range(300):
            try:
                if logged_in(page):
                    break
            except Exception as e:   # Chrome busy / closing: try again
                log("รอ...", str(e)[:80])
            time.sleep(3)
        else:
            raise SystemExit("ไม่เห็นการล็อกอินภายใน 15 นาที")
        time.sleep(5)   # let FB finish setting its cookies
        if not save_cookies(page):
            raise SystemExit("ล็อกอินแล้วแต่บันทึก cookie ไม่ได้ ลองใหม่อีกครั้ง")
        log("ล็อกอินแล้ว บันทึก cookie ที่", COOKIES)
    finally:
        b.close()
    if args.copy_to:
        subprocess.run(["ssh", args.copy_to, "mkdir -p ~/.kolwatch"], check=True)
        subprocess.run(["scp", "-q", COOKIES,
                        f"{args.copy_to}:.kolwatch/fb_relay_cookies.json"],
                       check=True)
        subprocess.run(["ssh", args.copy_to,
                        "chmod 600 ~/.kolwatch/fb_relay_cookies.json"], check=True)
        log("คัดลอกไป", args.copy_to, "แล้ว")


def _headless():
    b = ScrapeKit.launch(port=PORT, profile_dir=PROFILE, headless=True)
    page = b.new_page()
    load_cookies(page)
    return b, page


def cmd_check(args):
    b, page = _headless()
    try:
        page.goto("https://www.facebook.com/", wait_until="load", timeout=40)
        time.sleep(3)
        ok = logged_in(page) and "/login" not in (page.evaluate("location.href") or "")
        print("logged in" if ok else "NOT logged in")
        return 0 if ok else 2
    finally:
        b.close()


def cmd_run(args):
    conf = load_conf()
    if args.jitter:
        time.sleep(random.random() * args.jitter * 60)
    b, page = _headless()
    rc = 0
    try:
        page.goto("https://www.facebook.com/", wait_until="load", timeout=40)
        time.sleep(3 + random.random() * 3)
        if not (logged_in(page) and "/login" not in (page.evaluate("location.href") or "")):
            err = (("บัญชี FB สำรองหลุดล็อกอิน" if os.path.exists(COOKIES)
                    else "ยังไม่ได้ล็อกอินบัญชี FB สำรอง")
                   + " — ให้รัน fb_relay.py login (ดู sanmini_relay/README.md)")
            log(err)
            if not args.dry:
                for u in conf["pages"]:
                    push(conf, u, [], ok=False, error=err)
            return 2
        for u in conf["pages"]:
            try:
                raw, problem = read_page(page, u, args.scroll)
                posts = [] if problem else shape(raw, u)
                if not problem and not posts:
                    problem = "เปิดเพจได้แต่อ่านโพสต์ไม่ออก (หน้าตา FB อาจเปลี่ยน)"
                newest = max((p["posted_at"] for p in posts), default="")
                log(u, f"{len(posts)} โพสต์ ล่าสุด {newest}", problem or "")
                if args.dry:
                    for p in posts[:5]:
                        log("  ", p["posted_at"], p["post_id"], p["caption"][:60].replace("\n", " "))
                else:
                    log("  push:", push(conf, u, posts, ok=not problem, error=problem))
                if problem:
                    rc = 1
            except Exception as e:
                rc = 1
                log(u, "ERROR", e)
                if not args.dry:
                    try:
                        push(conf, u, [], ok=False, error=f"fb_relay error: {e}"[:300])
                    except Exception as e2:
                        log("  push failed too:", e2)
            time.sleep(5 + random.random() * 10)
        save_cookies(page)   # FB rotates some cookies; keep the fresh ones
    finally:
        b.close()
    return rc


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login")
    lg.add_argument("--copy-to", default="")
    sub.add_parser("check")
    rn = sub.add_parser("run")
    rn.add_argument("--scroll", type=int, default=8)
    rn.add_argument("--dry", action="store_true")
    rn.add_argument("--jitter", type=float, default=0,
                    help="wait a random 0..N minutes first")
    args = ap.parse_args()
    return {"login": cmd_login, "check": cmd_check, "run": cmd_run}[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
