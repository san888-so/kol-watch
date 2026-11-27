"""Self-heal guard (San, 27 Nov 2026: "fix it on the spot when it breaks").

Started first thing at boot, so it already exists when the process later
runs out of threads (26 Nov). Every minute it asks health.check() and pings
our own /healthz. If the app stays unhealthy for UNHEALTHY_MIN minutes it
records why and exits with code 1; Railway's ON_FAILURE restart policy then
boots a fresh container (the scheduler resumes a round cut off < 3 h ago).
After the restart we LINE the KOL Watch OA what happened.

Limits so it can't restart-loop: at least GAP_MIN between self-restarts and
at most MAX_PER_DAY per Bangkok day. Past those it only logs — SO! Deadman
(outside, Railway) still LINEs after 15 min red.
"""
import json
import os
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import db

TH = timezone(timedelta(hours=7))
CHECK_SEC = 60
UNHEALTHY_MIN = 10
GAP_MIN = 60
MAX_PER_DAY = 3
GRACE_MIN = 5          # let the app finish booting before judging it


def _log():
    try:
        return json.loads(db.get_setting("selfheal_log", "[]") or "[]")
    except ValueError:
        return []


def _probe_web(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=15) as r:
            return r.status == 200, None
    except urllib.error.HTTPError as e:
        return e.code == 503, None if e.code == 503 else f"เว็บตอบ {e.code}"
    except Exception as e:
        return False, f"เว็บไม่ตอบ ({type(e).__name__})"


def _problems(port):
    import health
    probs = []
    try:
        ok, info = health.check()
        probs += info.get("problems") or []
    except Exception as e:
        probs.append(f"เช็กสุขภาพไม่ได้ ({type(e).__name__}: {str(e)[:80]})")
    web_ok, why = _probe_web(port)
    if not web_ok and why:
        probs.append(why)
    return probs


def _allowed(now):
    log = _log()
    today = now.astimezone(TH).strftime("%Y-%m-%d")
    times = [datetime.fromisoformat(x["at"]) for x in log]
    if times and now - max(times) < timedelta(minutes=GAP_MIN):
        return False, f"เพิ่งรีสตาร์ทไปเมื่อ {max(times).astimezone(TH):%H:%M}"
    n = sum(1 for t in times if t.astimezone(TH).strftime("%Y-%m-%d") == today)
    if n >= MAX_PER_DAY:
        return False, f"วันนี้รีสตาร์ทเองครบ {MAX_PER_DAY} ครั้งแล้ว"
    return True, ""


def _restart(now, probs):
    log = [x for x in _log()
           if now - datetime.fromisoformat(x["at"]) < timedelta(days=7)]
    log.append({"at": now.isoformat(), "why": probs})
    db.set_setting("selfheal_log", json.dumps(log, ensure_ascii=False))
    db.set_setting("selfheal_pending", json.dumps(
        {"at": now.isoformat(), "why": probs}, ensure_ascii=False))
    print(f"[selfheal] unhealthy {UNHEALTHY_MIN}+ min → exiting for a restart: {probs}",
          flush=True)
    os._exit(1)


def announce_restart():
    """Called at boot: if the last process exited on purpose, tell the OA."""
    try:
        p = db.get_setting("selfheal_pending", "") or ""
        if not p:
            return
        db.set_setting("selfheal_pending", "")
        d = json.loads(p)
        at = datetime.fromisoformat(d["at"]).astimezone(TH)
        import notify_line
        notify_line.send_texts([
            f"🛠️ KOL Watch รีสตาร์ทตัวเองอัตโนมัติ ({at:%d/%m %H:%M})\n"
            f"สาเหตุ: {' · '.join(d.get('why') or ['ไม่ทราบ'])}\n"
            f"ตอนนี้ระบบกลับมาแล้ว รอบสแกนที่ค้างจะทำต่อให้เอง"])
    except Exception:
        traceback.print_exc()


def guard_thread(stop, port):
    boot = datetime.now(timezone.utc)
    bad_since = None
    held = ""
    while not stop.wait(CHECK_SEC):
        try:
            now = datetime.now(timezone.utc)
            if now - boot < timedelta(minutes=GRACE_MIN):
                continue
            probs = _problems(port)
            if not probs:
                if bad_since:
                    print("[selfheal] healthy again", flush=True)
                bad_since, held = None, ""
                continue
            bad_since = bad_since or now
            if now - bad_since < timedelta(minutes=UNHEALTHY_MIN):
                continue
            ok, why = _allowed(now)
            if ok:
                _restart(now, probs)
            elif why != held:
                held = why
                print(f"[selfheal] unhealthy but not restarting ({why}): {probs}", flush=True)
        except Exception:
            traceback.print_exc()
