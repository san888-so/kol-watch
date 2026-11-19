# -*- coding: utf-8 -*-
"""kol-ai-guard — reads every post KOL Watch scans and decides whether an
Exclusive KOL is talking about a competitor (incl. brands not on the
keyword list, e.g. "Finansia HeroM") or teaching foreign-stock trading
outside Webull.

Runs forever on sanmini (launchd com.so.kol-ai-guard):
  poll KOL Watch /api/ai/pending  ->  local LLM (Ollama)  ->  POST /api/ai/review
KOL Watch stores the verdict and sends the red LINE alert for "urgent".
Polling means nothing on sanmini is exposed to the internet; new posts are
picked up within a minute, even while a scan is still running.
Stdlib only.
"""
import json
import os
import re
import sys
import unicodedata
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import prompt  # noqa: E402


def _load_env():
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
BASE = os.environ.get("KOLWATCH_URL", "https://kol-watch-production.up.railway.app").rstrip("/")
KEY = os.environ.get("KOLWATCH_KEY", "")
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("AI_MODEL", "gemma4:12b")
BATCH = int(os.environ.get("AI_BATCH", "4"))
IDLE_SEC = int(os.environ.get("AI_IDLE_SEC", "60"))
# Same num_ctx as sogov-deep (gemma4:12b, 24576): a different value makes
# Ollama reload the model every time the two jobs alternate.
NUM_CTX = int(os.environ.get("AI_NUM_CTX", "24576"))


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def kw(method, path, body=None, timeout=60):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Authorization": "Bearer " + KEY, "Content-Type": "application/json",
        "User-Agent": "kol-ai-guard/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def llm(system, user, schema):
    body = {"model": MODEL, "stream": False, "format": schema, "think": False,
            "keep_alive": "20m",
            "options": {"temperature": 0, "num_ctx": NUM_CTX},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.loads(r.read().decode("utf-8"))
    return json.loads(out["message"]["content"])


_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)


def _norm(t):
    t = unicodedata.normalize("NFKC", t or "").translate(_ZW).lower()
    return re.sub(r"[\s\-_.·•@#/]+", "", t)


def _grounded(name, hay):
    """A competitor counts only if its name is really in the post (text,
    hashtags or links) — the model sometimes lists brands it was only
    given as examples. Multi-word names pass if any word of 4+ chars that
    isn't generic is present (e.g. 'Finansia' of 'Finansia HERO')."""
    n = _norm(name)
    if len(n) >= 3 and n in hay:
        return True
    generic = {"securities", "securitie", "trade", "trading", "invest", "app",
               "global", "wealth", "bank", "thailand", "หลักทรัพย์", "แอป"}
    for w in re.split(r"[\s/()+,]+", name or ""):
        wn = _norm(w)
        if len(wn) >= 4 and wn not in generic and wn in hay:
            return True
    return False


def _quote(evidence, caption, names):
    """Keep the evidence only if it is really quoted from the post;
    otherwise cut a window around the first competitor name."""
    raw = caption or ""
    ev = (evidence or "").strip().strip('"“”')
    parts = [x.strip() for x in re.split(r"\.\.\.|…", ev) if len(x.strip()) >= 8]
    if parts and all(_norm(x) in _norm(raw) for x in parts):
        return ev
    low = raw.lower()
    for nm in names:
        for w in [nm] + re.split(r"\s+", nm):
            i = low.find(w.lower()) if len(w) >= 3 else -1
            if i >= 0:
                a, b = max(0, i - 50), min(len(raw), i + len(w) + 60)
                return ("…" if a else "") + raw[a:b].replace("\n", " ").strip() + ("…" if b < len(raw) else "")
    return ""


_URL = re.compile(r"https?://[^\s<>\"')]+")
_SHORT = ("t.co", "bit.ly", "lin.ee", "tinyurl.com", "cutt.ly", "s.id",
          "rebrand.ly", "shorturl.at", "is.gd", "ow.ly", "goo.gl", "tiny.cc")


def _expand_links(caption):
    """Follow short links (t.co, bit.ly, lin.ee ...) to their real
    destination — broker referral links often hide behind them.
    Returns [(short, final_url)]; HEAD only, nothing is downloaded."""
    out = []
    for u in _URL.findall(caption or "")[:4]:
        host = re.sub(r"^https?://", "", u).split("/")[0].lower()
        if host.startswith("www."):
            host = host[4:]
        if host not in _SHORT:
            continue
        try:
            req = urllib.request.Request(u, method="HEAD",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                fin = r.geturl()
            if fin and fin != u:
                out.append((u, fin))
        except Exception:
            pass
    return out


def _where(name, post, links):
    """Short Thai phrase: where in the post the name was found."""
    n = _norm(name)
    for short, fin in links:
        if n and n in _norm(fin):
            return "ลิงก์ย่อพาไป " + re.sub(r"^https?://(www\.)?", "", fin).split("/")[0]
    for u in _URL.findall(post.get("caption") or ""):
        if n and n in _norm(u):
            return "ลิงก์ " + re.sub(r"^https?://(www\.)?", "", u).split("/")[0]
    tags = " ".join(post.get("hashtags") or []) + " " + " ".join(
        re.findall(r"#\S+", post.get("caption") or ""))
    if n and n in _norm(tags):
        return "แฮชแท็ก"
    return "ข้อความ"


def review(post, policy, known):
    cap = (post.get("caption") or "").strip()
    if len(cap) < 4 and not post.get("hashtags"):
        return {"level": "none", "competitors": [], "foreign_stock_trading": False,
                "mentions_webull": False, "reason": "ไม่มีแคปชันให้อ่าน", "evidence": ""}
    links = _expand_links(cap)
    p2 = dict(post)
    if links:
        p2["caption"] = cap + "\n[ลิงก์ย่อในโพสต์พาไปที่: " + \
            ", ".join(f for _, f in links) + "]"
    s, u = prompt.build(policy, known, p2)
    v = llm(s, u, prompt.SCHEMA)
    v["competitors"] = [c for c in (v.get("competitors") or [])
                        if str(c).strip() and str(c).strip().lower() not in
                        ("none", "webull", "วีบลู", "-", "n/a")]
    hay = _norm((post.get("caption") or "") + " "
                + " ".join(post.get("hashtags") or []) + " "
                + (post.get("post_url") or "") + " "
                + " ".join(f for _, f in links))
    named = v["competitors"]
    v["competitors"] = [c for c in named if _grounded(c, hay)]
    dropped = [c for c in named if c not in v["competitors"]]
    v["evidence"] = _quote(v.get("evidence"), post.get("caption"), v["competitors"])
    lvl = v.get("level") or "none"
    # Nothing named in the post -> can't be "urgent" (at most "watch").
    if lvl == "urgent" and not v["competitors"]:
        lvl = "watch"
        v["reason"] = (v.get("reason") or "") + " · (ไม่พบชื่อคู่แข่งในโพสต์จริง" \
            + (": " + ", ".join(dropped) if dropped else "") + ")"
    if lvl == "watch" and not v["competitors"] and not v.get("foreign_stock_trading"):
        lvl = "none"
    # Foreign-stock content that is about Webull itself is what we paid for.
    if lvl != "none" and not v["competitors"] and v.get("mentions_webull"):
        lvl = "none"
    # Urgent with nothing named and no foreign-trading angle -> not actionable.
    if lvl == "urgent" and not v["competitors"] and not v.get("foreign_stock_trading"):
        lvl = "watch"
    if lvl == "urgent":
        cs, cu = prompt.build_confirm(post, v)
        c = llm(cs, cu, prompt.CONFIRM_SCHEMA)
        if not c.get("confirm"):
            lvl = "watch"
            v["reason"] = (v.get("reason") or "") + " · (รอบสองไม่ยืนยัน: " + (c.get("reason") or "") + ")"
    v["level"] = lvl
    why = (v.get("why_short") or "").strip()
    if lvl != "none" and v["competitors"]:
        locs = []
        for c in v["competitors"][:3]:
            w = _where(c, post, links)
            if w not in locs:
                locs.append(w)
        why = (why + " · " if why else "") + "พบใน" + ", ".join(locs)
    v["why"] = why[:200]
    return v


def main():
    if not KEY:
        sys.exit("KOLWATCH_KEY missing (.env)")
    log(f"kol-ai-guard up · {BASE} · model {MODEL}")
    while True:
        try:
            d = kw("GET", f"/api/ai/pending?limit={BATCH}")
        except Exception as e:
            log("pending fetch failed:", str(e)[:200])
            time.sleep(IDLE_SEC)
            continue
        posts = d.get("posts") or []
        if not d.get("enabled") or not posts:
            time.sleep(IDLE_SEC)
            continue
        policy, known = d.get("policy") or "", d.get("known_competitors") or []
        for p in posts:
            t = time.time()
            try:
                v = review(p, policy, known)
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                log("LLM unreachable, retry later:", str(e)[:200])
                time.sleep(IDLE_SEC)
                break
            except Exception as e:
                # Bad JSON etc. — record it so one odd post can't block the queue.
                log("review failed", p["seen_post_id"], str(e)[:200])
                v = {"level": "skip", "competitors": [], "reason": "AI อ่านไม่สำเร็จ: " + str(e)[:120],
                     "evidence": ""}
            v["seen_post_id"] = p["seen_post_id"]
            try:
                r = kw("POST", "/api/ai/review", {"model": MODEL, "results": [v]})
            except Exception as e:
                log("save failed", p["seen_post_id"], str(e)[:200])
                time.sleep(10)
                continue
            log(f"#{p['seen_post_id']} {v['level']:6} {time.time()-t:5.1f}s "
                f"{p.get('kol','')[:28]} {v.get('competitors')} "
                f"{'→ LINE' if r.get('alerted') else ''} · left {d.get('pending_total')}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
        sys.exit(1)
