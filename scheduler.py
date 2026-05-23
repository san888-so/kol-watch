"""Watch runner — scan all channels for new posts, match keywords, alert.

Fires at each time in the run_times setting (default 08:00,16:00). Each run:
per channel -> collect recent posts -> skip already-seen (incremental) ->
keyword scan -> on hit: record + immediate LINE push. End of run: digest.
"""
import threading
import traceback
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
    TH_TZ = ZoneInfo("Asia/Bangkok")
except Exception:  # pragma: no cover  — fall back to a fixed +07:00 offset
    from datetime import timezone as _tz, timedelta as _td
    TH_TZ = _tz(_td(hours=7))

import db
import matcher
import notify_line
import collectors

PROGRESS = {"running": False, "current": "", "done": 0, "total": 0,
            "last_run": None, "summary": ""}

# Set by the Stop button; run_once checks it between channels and bails out.
_STOP = threading.Event()


def request_stop():
    if PROGRESS["running"]:
        _STOP.set()
        return True
    return False


def run_once(triggered_by="manual"):
    if PROGRESS["running"]:
        return {"error": "already running"}
    PROGRESS.update(running=True, done=0, current="", summary="")
    _STOP.clear()
    stopped = False
    # Only scan platforms the operator enabled (cost control — e.g. drop
    # Facebook, which is ~80% of the Apify bill, when budget is tight).
    enabled = {p.strip() for p in
               (db.get_setting("scan_platforms",
                               "youtube,lemon8,tiktok,instagram,facebook,x")
                or "").split(",") if p.strip()}
    channels = [c for c in db.list_channels() if c["platform"] in enabled]
    PROGRESS["total"] = len(channels)
    try:
        limit = int(db.get_setting("posts_per_check", "3") or "3")
    except ValueError:
        limit = 3

    new_posts = hits = blocked = 0
    try:
        for ch in channels:
            if _STOP.is_set():
                stopped = True
                break
            PROGRESS["current"] = f"{ch['kol_name']} / {ch['platform']}"
            try:
                posts = collectors.collect(ch, limit=limit)
            except Exception as e:
                blocked += 1
                db.update_channel(ch["id"], status="blocked",
                                  last_error=str(e)[:400],
                                  last_checked=datetime.now(timezone.utc).isoformat())
                PROGRESS["done"] += 1
                continue

            cid = ch.get("_resolved_channel_id")
            if cid and not ch.get("channel_id_cache"):
                db.update_channel(ch["id"], channel_id_cache=cid)

            # Scan every fetched post; skip ones already seen. We deliberately
            # do NOT break at the first seen post: a pinned/old post can sit
            # on top of the feed (esp. FB/IG/X, and apidojo TikTok which
            # doesn't flag pinned), and breaking there would miss the newer
            # posts below it. posts_per_check is small so scanning all is cheap.
            for p in posts:
                pid = p.get("post_id") or ""
                if not pid or db.post_seen(ch["id"], pid):
                    continue
                new_posts += 1
                found = matcher.scan(p.get("caption", ""), p.get("hashtags", []))
                db.mark_seen(ch["id"], pid, p.get("url", ""),
                             p.get("posted_at", ""),
                             caption=p.get("caption", ""),
                             hashtags=p.get("hashtags", []),
                             matched=bool(found))
                for f in found:
                    hid = db.add_hit(
                        ch["id"], ch["kol_name"], ch["platform"],
                        p.get("url", ""), pid, p.get("posted_at", ""),
                        f["keyword"], f["snippet"])
                    if hid:
                        hits += 1
                        row = {"kol_name": ch["kol_name"],
                               "platform": ch["platform"],
                               "matched_keyword": f["keyword"],
                               "post_url": p.get("url", ""),
                               "snippet": f["snippet"]}
                        ok, _ = notify_line.push_hit(row)
                        if ok:
                            db.mark_notified(hid)

            db.update_channel(ch["id"], status="ok", last_error="",
                              last_checked=datetime.now(timezone.utc).isoformat())
            PROGRESS["done"] += 1

        stamp = datetime.now(TH_TZ).strftime("%Y-%m-%d %H:%M")
        unresolved = len(db.list_unresolved())
        head = "🛑 หยุดโดยผู้ใช้" if stopped else "📋 KOL Watch"
        scanned = f"{PROGRESS['done']}/{len(channels)}" if stopped else str(len(channels))
        summary = (
            f"{head} — รอบ {stamp} ({triggered_by})\n"
            f"ช่องที่สแกน: {scanned}\n"
            f"โพสต์ใหม่: {new_posts}\n"
            f"⚠️ เจอคำคู่แข่ง: {hits}\n"
            f"❌ เช็กไม่ได้ (private/บล็อก): {blocked}\n"
            f"🔗 ลิงก์ใน Sheet ที่อ่านไม่ออก: {unresolved}"
        )
        PROGRESS["summary"] = summary
        if not stopped:  # don't LINE-spam a digest on a manual stop
            notify_line.digest([summary])
    finally:
        PROGRESS.update(running=False, current="",
                        last_run=datetime.now(timezone.utc).isoformat())
        _STOP.clear()
    return {"ok": True, "new_posts": new_posts, "hits": hits, "blocked": blocked}


def scheduler_thread(stop_event):
    """Fire run_once when Bangkok wall-clock crosses any configured time.

    Times in `run_times` (e.g. 08:00,16:00) are always interpreted in
    Asia/Bangkok — the comparison is timezone-aware, so it works whether
    the container's OS TZ is UTC, Bangkok, or anything else.
    """
    last_fired = {}  # "HH:MM" -> date string (in Bangkok)
    while not stop_event.is_set():
        try:
            if (db.get_setting("auto_enabled", "1") or "1") == "1":
                times = [t.strip() for t in
                         (db.get_setting("run_times", "08:00,16:00") or "").split(",")
                         if t.strip()]
                now = datetime.now(TH_TZ)
                today = now.strftime("%Y-%m-%d")
                for hhmm in times:
                    try:
                        target = datetime.strptime(
                            f"{today} {hhmm}", "%Y-%m-%d %H:%M"
                        ).replace(tzinfo=TH_TZ)
                    except ValueError:
                        continue
                    if (last_fired.get(hhmm) != today and now >= target
                            and (now - target).total_seconds() < 600):
                        last_fired[hhmm] = today
                        run_once(triggered_by=f"auto {hhmm} TH")
        except Exception:
            traceback.print_exc()
        stop_event.wait(30)
