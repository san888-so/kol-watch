"""/healthz for outside watchdogs (27 Nov 2026).

On 26 Nov 22:10 the container ran out of pids: the web server answered 502
and the 22:00 scan round hung, so no round ran for 15 hours — and nobody was
told, because every alert is sent from inside this same process. This page
lets watchers that live elsewhere (SO! Deadman on Railway, So! Monitor on
sanmini) see it: 200 = healthy, 503 = something is wrong (reasons listed).
"""
import os
import threading
from datetime import datetime, timezone

import db

RUNNING_MAX_MIN = 90      # a round normally takes ~10 min
BOOT = datetime.now(timezone.utc)


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _zombies():
    n = 0
    try:
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                s = _read(f"/proc/{pid}/stat")
                if s and s.rsplit(")", 1)[-1].split()[0] == "Z":
                    n += 1
    except OSError:
        return None
    return n


def _scan_limit_min():
    """How long without a finished round counts as stuck: hourly mode 150
    min; otherwise the longest gap between run_times + 90 min."""
    if db.hourly_until():
        return 150
    times = []
    for t in (db.get_setting("run_times", "08:00,16:00") or "").split(","):
        try:
            h, m = t.strip().split(":")
            times.append(int(h) * 60 + int(m))
        except ValueError:
            pass
    times = sorted(set(times))
    if not times:
        return 26 * 60
    gaps = [(times[(i + 1) % len(times)] - t) % 1440 or 1440
            for i, t in enumerate(times)]
    return max(gaps) + 90


def check():
    problems = []
    now = datetime.now(timezone.utc)

    try:
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start()
        t.join(2)
    except RuntimeError:
        problems.append("เปิด thread ใหม่ไม่ได้ (process/thread เต็ม)")

    cur, mx = _read("/sys/fs/cgroup/pids.current"), _read("/sys/fs/cgroup/pids.max")
    pids = {"current": int(cur) if cur.isdigit() else None,
            "max": int(mx) if mx.isdigit() else None}
    if pids["current"] and pids["max"] and pids["current"] > 0.7 * pids["max"]:
        problems.append(f"process/thread ใช้ไป {pids['current']}/{pids['max']}")
    zombies = _zombies()
    if zombies and zombies > 50:
        problems.append(f"zombie process ค้าง {zombies} ตัว")

    scan = {}
    with db._conn() as c:
        last = c.execute("SELECT started_at, finished_at, status FROM scan_run "
                         "ORDER BY id DESC LIMIT 1").fetchone()
        done = c.execute("SELECT finished_at FROM scan_run WHERE status='done' "
                         "AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    if last and last["status"] == "running":
        mins = (now - datetime.fromisoformat(last["started_at"])).total_seconds() / 60
        scan["running_min"] = round(mins)
        if mins > RUNNING_MAX_MIN:
            problems.append(f"รอบสแกนค้างมา {mins / 60:.1f} ชม.")
    limit = _scan_limit_min()
    scan["limit_min"] = limit
    if done:
        # Count from boot if that is later: a fresh process gets one full
        # interval to run a round before it is called stale (else a restart
        # would look broken until the next round, and selfheal would loop).
        since = max(datetime.fromisoformat(done["finished_at"]), BOOT)
        age = (now - since).total_seconds() / 60
        scan["last_done"] = done["finished_at"]
        scan["age_min"] = round(age)
        if age > limit:
            problems.append(f"ไม่มีรอบสแกนเสร็จมา {age / 60:.1f} ชม.")

    return (not problems), {"ok": not problems, "problems": problems,
                            "scan": scan, "pids": pids, "zombies": zombies,
                            "at": now.isoformat()}
