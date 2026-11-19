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
import sys
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
            "options": {"temperature": 0, "num_ctx": 8192},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.loads(r.read().decode("utf-8"))
    return json.loads(out["message"]["content"])


def review(post, policy, known):
    cap = (post.get("caption") or "").strip()
    if len(cap) < 4 and not post.get("hashtags"):
        return {"level": "none", "competitors": [], "foreign_stock_trading": False,
                "mentions_webull": False, "reason": "ไม่มีแคปชันให้อ่าน", "evidence": ""}
    s, u = prompt.build(policy, known, post)
    v = llm(s, u, prompt.SCHEMA)
    v["competitors"] = [c for c in (v.get("competitors") or [])
                        if str(c).strip() and str(c).strip().lower() not in
                        ("none", "webull", "วีบลู", "-", "n/a")]
    lvl = v.get("level") or "none"
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
