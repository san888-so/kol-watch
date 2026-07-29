"""Watch runner — scan all channels for new posts, match keywords, alert.

Fires at each time in the run_times setting (default 08:00,16:00). Each run:
per channel -> collect recent posts -> skip already-seen (incremental) ->
keyword scan -> on hit: record + immediate LINE push. End of run: digest.
"""
import json
import threading
import traceback
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
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
import dca_report

PROGRESS = {"running": False, "current": "", "done": 0, "total": 0,
            "last_run": None, "summary": ""}

# Set by the Stop button; run_once checks it between channels and bails out.
_STOP = threading.Event()


def request_stop():
    if PROGRESS["running"]:
        _STOP.set()
        return True
    return False


# Cheap health-check: probe every channel with limit=1, record pass/empty/error
# per channel without writing seen_post/hit. Used to find Sheet links that
# error or return no posts (dead URLs, private accounts, broken handles).
HEALTH = {"running": False, "done": 0, "total": 0, "results": []}


def run_health_check():
    if HEALTH["running"]:
        return
    HEALTH.update(running=True, done=0, total=0, results=[])
    channels = db.list_channels()
    HEALTH["total"] = len(channels)
    try:
        for ch in channels:
            try:
                posts = collectors.collect(ch, limit=1)
                n = len(posts)
                HEALTH["results"].append({
                    "kol": ch["kol_name"], "platform": ch["platform"],
                    "url": ch["profile_url"],
                    "status": "ok" if n > 0 else "empty",
                    "count": n, "error": ""})
            except Exception as e:
                HEALTH["results"].append({
                    "kol": ch["kol_name"], "platform": ch["platform"],
                    "url": ch["profile_url"],
                    "status": "error", "count": 0,
                    "error": str(e)[:200]})
            HEALTH["done"] += 1
    finally:
        HEALTH["running"] = False


# LINE text bubble cap = 5000 chars; leave headroom for the rejoin header.
_BUBBLE_CHAR_CAP = 4500


def _post_time_th(s):
    """Render a post's posted_at string as 'HH:MM น. DD/MM' in Bangkok time.
    Empty / date-only -> the date as-is."""
    if not s:
        return "—"
    if len(s) == 10 and s.count("-") == 2:  # YYYY-MM-DD only
        return s
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(TH_TZ).strftime("%H:%M น. %d/%m")
    except Exception:
        return s[:16]


def _build_brand_bubbles(hits):
    """Group brand-hits by KOL and pack into LINE bubbles (text bubbles cap
    ~5000 chars; we use 4500). Each bubble carries N KOLs (user-configured),
    auto-splitting earlier if a bubble would exceed the char cap."""
    try:
        kols_per_bubble = max(1, int(db.get_setting("brand_kols_per_bubble", "5")))
    except ValueError:
        kols_per_bubble = 5
    stamp = datetime.now(TH_TZ).strftime("%d/%m %H:%M")

    by_kol = OrderedDict()
    for h in hits:
        by_kol.setdefault(h["kol_name"], []).append(h)

    def head(n, total):
        return (f"📊 รายงานแบรนด์ลูกค้า — {stamp} (GMT+7)\n"
                f"เจอ {total} โพสต์ จาก {len(by_kol)} KOL"
                + (f"  (ส่วน {n})" if n > 1 else "")
                + "\n────────\n")

    bubbles, part = [], 1
    body, packed = "", 0
    total_hits = len(hits)
    idx = 0
    for kol, entries in by_kol.items():
        idx += 1
        block = f"\n{idx}. {kol}\n"
        for e in entries:
            block += (f"   • [{e['matched_keyword']}] {e['platform']} "
                      f"{_post_time_th(e.get('posted_at',''))}\n"
                      f"     {e['post_url']}\n")
        first = (packed == 0)
        prospective = (head(part, total_hits) if first else "") + body + block
        if (not first and (packed >= kols_per_bubble
                           or len(prospective) > _BUBBLE_CHAR_CAP)):
            bubbles.append(head(part, total_hits) + body)
            part += 1
            body, packed = block, 1
        else:
            body += block
            packed += 1
    if body:
        bubbles.append(head(part, total_hits) + body)
    return bubbles


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
    brand_wl = matcher.get_client_brands()
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
            # Competitor scan only applies to Exclusive KOLs (per contract).
            # Brand (client) scan applies to everyone.
            is_excl = "non" not in (ch.get("kol_exclusivity") or "").lower()
            for p in posts:
                pid = p.get("post_id") or ""
                if not pid or db.post_seen(ch["id"], pid):
                    continue
                new_posts += 1
                found = matcher.scan(p.get("caption", ""),
                                     p.get("hashtags", [])) if is_excl else []
                db.mark_seen(ch["id"], pid, p.get("url", ""),
                             p.get("posted_at", ""),
                             caption=p.get("caption", ""),
                             hashtags=p.get("hashtags", []),
                             matched=bool(found))
                for f in found:
                    # Skip if this post+keyword was already flagged (any
                    # status) — no duplicate alerts, incl. ones marked
                    # contacted/removed and Lemon8 re-detections.
                    if db.hit_exists_for(ch["id"], f["keyword"],
                                         p.get("url", ""), pid):
                        continue
                    hid = db.add_hit(
                        ch["id"], ch["kol_name"], ch["platform"],
                        p.get("url", ""), pid, p.get("posted_at", ""),
                        f["keyword"], f["snippet"], kind="competitor")
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

                # ---- Client-brand report (no immediate push; batched at end) ----
                if brand_wl:
                    bf = matcher.scan(p.get("caption", ""),
                                      p.get("hashtags", []),
                                      watchlist=brand_wl)
                    for f in bf:
                        if db.hit_exists_for(ch["id"], f["keyword"],
                                             p.get("url", ""), pid):
                            continue
                        db.add_hit(ch["id"], ch["kol_name"], ch["platform"],
                                   p.get("url", ""), pid,
                                   p.get("posted_at", ""),
                                   f["keyword"], f["snippet"],
                                   kind="brand")

            db.update_channel(ch["id"], status="ok", last_error="",
                              last_checked=datetime.now(timezone.utc).isoformat())
            PROGRESS["done"] += 1

        # ---- Brand report digest (batched, grouped by KOL) ----
        # Only LINE-alert posts dated within the recency window — Apify
        # occasionally returns the same old post with a different URL form,
        # which we register as a new hit because we can't dedupe it without
        # the canonical post_id. Silently archive those older "rediscoveries".
        brand_pending = db.unnotified_brand_hits()
        try:
            max_age_days = int(db.get_setting("brand_digest_max_age_days", "3"))
        except ValueError:
            max_age_days = 3
        cutoff_date = (datetime.now(TH_TZ) - timedelta(days=max_age_days)).date()
        fresh, stale = [], []
        for h in brand_pending:
            pa = (h.get("posted_at") or "")[:10]
            try:
                d = datetime.strptime(pa, "%Y-%m-%d").date()
            except ValueError:
                d = None
            (fresh if (d is None or d >= cutoff_date) else stale).append(h)
        for h in stale:           # archive without LINE
            db.mark_notified(h["id"])
        brand_sent = 0
        if fresh and not stopped:
            bubbles = _build_brand_bubbles(fresh)
            ok, _ = notify_line.send_texts(bubbles)
            if ok:
                for h in fresh:
                    db.mark_notified(h["id"])
                brand_sent = len(fresh)

        stamp = datetime.now(TH_TZ).strftime("%Y-%m-%d %H:%M")
        head = "🛑 หยุดโดยผู้ใช้" if stopped else "📋 KOL Watch"
        scanned = f"{PROGRESS['done']}/{len(channels)}" if stopped else str(len(channels))
        total_kols = len({c["kol_name"] for c in channels})
        brand_label = " / ".join(brand_wl.keys()) or "แบรนด์ลูกค้า"
        summary = (
            f"{head} — รอบ {stamp} ({triggered_by})\n"
            f"ช่องที่สแกน: {scanned}\n"
            f"จากทั้งหมด KOL: {total_kols}\n"
            f"โพสต์ใหม่: {new_posts}\n"
            f"⚠️ เจอคำคู่แข่ง: {hits}\n"
            f"📊 เจอโพส {brand_label}: {brand_sent}  (เฉพาะโพสต์ลงภายใน {max_age_days} วัน)\n"
            f"❌ เช็กไม่ได้: {blocked}"
        )
        PROGRESS["summary"] = summary
        if not stopped:  # don't LINE-spam a digest on a manual stop
            notify_line.digest([summary])
    finally:
        PROGRESS.update(running=False, current="",
                        last_run=datetime.now(timezone.utc).isoformat())
        _STOP.clear()
    return {"ok": True, "new_posts": new_posts, "hits": hits, "blocked": blocked}


def _load_fired(key):
    """Read a persisted {HH:MM -> date} fired-map from settings."""
    try:
        return json.loads(db.get_setting(key, "") or "{}")
    except Exception:
        return {}


def _save_fired(key, fired):
    try:
        db.set_setting(key, json.dumps(fired))
    except Exception:
        pass


def scheduler_thread(stop_event):
    """Fire run_once when Bangkok wall-clock crosses any configured time.

    Times in `run_times` (e.g. 08:00,16:00) are always interpreted in
    Asia/Bangkok — the comparison is timezone-aware, so it works whether
    the container's OS TZ is UTC, Bangkok, or anything else.

    Fired-state is persisted to the DB (on the Railway volume) so a restart
    inside a catch-up window doesn't re-fire — without this, redeploying
    between 10:00–11:00 would send the daily report twice.
    """
    last_fired = _load_fired("sched_watch_fired")     # "HH:MM" -> date (TH)
    report_fired = _load_fired("sched_report_fired")  # "HH:MM" -> date (TH)

    # Boot self-test: build the report once in THIS environment so any
    # env-specific breakage (missing tzdata, moved sheet columns, network)
    # screams in the deploy logs immediately instead of silently no-showing
    # at 10:00 tomorrow.
    try:
        msgs = dca_report.build_messages()
        print(f"[dca_report] boot self-test OK — builds {len(msgs)} bubble(s)",
              flush=True)
        db.set_setting("dca_selftest", json.dumps(
            {"at": datetime.now(TH_TZ).strftime("%Y-%m-%d %H:%M"),
             "ok": True, "bubbles": len(msgs)}))
    except Exception as e:
        print("[dca_report] boot self-test FAILED:", flush=True)
        traceback.print_exc()
        db.set_setting("dca_selftest", json.dumps(
            {"at": datetime.now(TH_TZ).strftime("%Y-%m-%d %H:%M"),
             "ok": False, "err": str(e)[:200]}))

    while not stop_event.is_set():
        try:
            now = datetime.now(TH_TZ)
            today = now.strftime("%Y-%m-%d")

            def _due(hhmm, fired, window=600):
                try:
                    target = datetime.strptime(
                        f"{today} {hhmm}", "%Y-%m-%d %H:%M"
                    ).replace(tzinfo=TH_TZ)
                except ValueError:
                    return False
                return (fired.get(hhmm) != today and now >= target
                        and (now - target).total_seconds() < window)

            if (db.get_setting("auto_enabled", "1") or "1") == "1":
                times = [t.strip() for t in
                         (db.get_setting("run_times", "08:00,16:00") or "").split(",")
                         if t.strip()]
                for hhmm in times:
                    if _due(hhmm, last_fired):
                        last_fired[hhmm] = today
                        _save_fired("sched_watch_fired", last_fired)
                        run_once(triggered_by=f"auto {hhmm} TH")

            # Daily DCA content-status report (independent of the keyword watch).
            # Uses a 1-hour catch-up window so a brief restart near the target
            # time doesn't skip the day — last_fired guards against duplicates.
            if (db.get_setting("dca_report_enabled", "1") or "1") == "1":
                report_times = [t.strip() for t in
                                (db.get_setting("dca_report_time", "10:00,16:00")
                                 or "10:00,16:00").split(",") if t.strip()]
                for rt in report_times:
                    if _due(rt, report_fired, window=3600):
                        # Persist BEFORE sending: if the send raises, we still
                        # don't retry-loop or double-fire on restart this day.
                        report_fired[rt] = today
                        _save_fired("sched_report_fired", report_fired)
                        print(f"[dca_report] firing for {rt} TH", flush=True)
                        ok, msg = False, ""
                        try:
                            ok, msg = dca_report.send_report()
                            print(f"[dca_report] {rt} result ok={ok} msg={msg}",
                                  flush=True)
                        except Exception as e:
                            msg = str(e)[:200]
                            print(f"[dca_report] {rt} EXCEPTION:", flush=True)
                            traceback.print_exc()
                        db.set_setting("dca_last_run", json.dumps(
                            {"at": now.strftime("%Y-%m-%d %H:%M"), "slot": rt,
                             "ok": ok, "msg": msg}))
        except Exception:
            traceback.print_exc()
        stop_event.wait(30)
