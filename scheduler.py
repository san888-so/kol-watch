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
import alert_card
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
                posts = collectors.collect(ch, limit=1, windowed=False)
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
        d = d.astimezone(TH_TZ)
        if d.year != datetime.now(TH_TZ).year:   # say the year if not this one
            return d.strftime("%d/%m/") + str(d.year + 543)[-2:]
        return d.strftime("%H:%M น. %d/%m")
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


_PLAT_TH = {"facebook": "FB", "instagram": "IG", "tiktok": "TikTok",
            "youtube": "YouTube", "x": "X", "lemon8": "Lemon8",
            "threads": "Threads"}
_DIGEST_MAX_POSTS = 15


def _competitor_post_lines(posts):
    """Digest block: one numbered entry per post (not per keyword), so the
    team can follow up each post. Capped to keep the LINE text under 5000."""
    out = ["────────", "โพสต์ที่เจอคำคู่แข่ง (แยกทีละโพสต์):"]
    for i, c in enumerate(posts[:_DIGEST_MAX_POSTS], 1):
        when = _post_time_th(c.get("posted_at", ""))
        out.append(f"{i}. {c['kol']} · {_PLAT_TH.get(c['platform'], c['platform'])}"
                   + (f" · {when}" if when and when != "—" else "")
                   + (" · โพสต์เก่า (เพิ่งเจอ)" if c.get("old") else ""))
        out.append(f"   คำที่เจอ: {', '.join(c['keywords'])}")
        if c.get("url"):
            out.append(f"   {c['url']}")
    rest = len(posts) - _DIGEST_MAX_POSTS
    if rest > 0:
        out.append(f"…และอีก {rest} โพสต์ ดูในหน้าเว็บ แท็บเจอคำคู่แข่ง")
    out.append("────────")
    return "\n".join(out)


def _push_keyword_card(ch, p, matches, unbranded_wl, hit_ids=()):
    """Send one alert card for a post's keyword matches (same layout as the
    AI card). Broad topic words alone (หุ้นนอก ...) get the orange
    'เฝ้าระวัง' card; a competitor name gets the red 'ด่วน' card."""
    named = [f for f in matches if f["keyword"] not in (unbranded_wl or {})]
    broad = not named
    words = []
    for f in matches:
        w = f.get("variant") or f["keyword"]
        if w not in words:
            words.append(w)
    if broad:
        sig = next((f.get("promo_signal") for f in matches
                    if f.get("promo_signal")), "")
        why = ("เจอคำกว้างในรายการเฝ้าระวัง + คำชวนสมัคร/ลิงก์"
               + (f" ({sig})" if sig else "")
               + " และโพสต์ไม่มีชื่อ Webull · ยังไม่รู้ว่าชวนใช้เจ้าไหน AI จะอ่านซ้ำ")
    else:
        why = "ชื่อคู่แข่งในรายการตรงกับแคปชัน/แฮชแท็กของโพสต์"
    post = {"kol": ch["kol_name"], "platform": ch["platform"],
            "post_url": p.get("url", ""), "posted_at": p.get("posted_at", ""),
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "exclusivity": ch.get("kol_exclusivity", "")}
    alt, bubble = alert_card.build(
        post, source="keyword",
        competitors=[f["keyword"] for f in named], words=words, why=why,
        evidence=matches[0].get("snippet", ""), broad=broad,
        hit_ids=hit_ids)
    ok, _ = notify_line.push_flex(alt, bubble)
    if not ok:  # Flex refused for any reason -> fall back to the old text
        f = matches[0]
        ok, _ = notify_line.push_hit({
            "kol_name": ch["kol_name"], "platform": ch["platform"],
            "matched_keyword": ", ".join(m["keyword"] for m in matches),
            "post_url": p.get("url", ""), "snippet": f.get("snippet", "")})
    return ok


def _is_old_post(posted_at):
    """True when the post is older than `competitor_alert_max_age_days`
    (default 30). A post with no date counts as new (alert, to be safe)."""
    if not posted_at:
        return False
    try:
        days = int(db.get_setting("competitor_alert_max_age_days", "30") or "30")
    except ValueError:
        days = 30
    try:
        d = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    return datetime.now(timezone.utc) - d > timedelta(days=days)


# Errors a retry cannot fix (FB profile shown to friends only, quota out).
_NO_RETRY = ("not_available", "only shared it", "hard limit",
             "platform-feature-disabled", "monthly usage")


def _collect_with_retry(ch, limit):
    """Exclusive KOLs must be fully covered: a failed or empty fetch is
    tried again (San, 21 Nov 2026 — "ถ้าใครโหลดได้ไม่ครบ ให้โหลดซ้ำ").
    Scrapers fail transiently (X search outage, TikTok/Lemon8 empty pages
    that load fine an hour later), so wait a bit between tries."""
    is_excl = "non" not in (ch.get("kol_exclusivity") or "").lower()
    try:
        retries = int(db.get_setting("exclusive_retries", "2") or "2")
    except ValueError:
        retries = 2
    delays = [10, 45, 90][:retries] if is_excl else []
    for attempt in range(len(delays) + 1):
        try:
            return collectors.collect(ch, limit=limit)
        except Exception as e:
            msg = str(e).lower()
            if (attempt >= len(delays) or _STOP.is_set()
                    or any(k in msg for k in _NO_RETRY)):
                raise
            print(f"[retry] {ch['kol_name']} / {ch['platform']} try "
                  f"{attempt + 2}: {str(e)[:120]}", flush=True)
            _STOP.wait(delays[attempt])


def run_once(triggered_by="manual", only_before=None, exclusive_only=False):
    if PROGRESS["running"]:
        return {"error": "already running"}
    PROGRESS.update(running=True, done=0, current="", summary="")
    _STOP.clear()
    stopped = False
    # Only scan platforms the operator enabled (cost control — e.g. drop
    # Facebook, which is ~80% of the Apify bill, when budget is tight).
    enabled = {p.strip() for p in
               (db.get_setting("scan_platforms",
                               "youtube,lemon8,tiktok,instagram,facebook,x,threads")
                or "").split(",") if p.strip()}
    channels = [c for c in db.list_channels() if c["platform"] in enabled]
    if exclusive_only:  # "เฝ้าเข้ม" hourly round: Exclusive KOLs only
        channels = [c for c in channels
                    if "non" not in (c.get("kol_exclusivity") or "").lower()]
    if only_before:  # resuming a scan a restart cut off: skip channels done
        channels = [c for c in channels
                    if not c.get("last_checked") or c["last_checked"] < only_before]
    PROGRESS["total"] = len(channels)
    # Marker so a restart (deploy, crash) mid-scan can resume on boot.
    run_started = only_before or datetime.now(timezone.utc).isoformat()
    db.set_setting("scan_running_since", run_started)
    db.set_setting("scan_running_excl", "1" if exclusive_only else "")
    run_id = None
    try:
        run_id = db.start_run(triggered_by,
                              "resume" if only_before else
                              ("exclusive" if exclusive_only else "all"),
                              len(channels))
    except Exception:
        traceback.print_exc()
    try:
        limit = int(db.get_setting("posts_per_check", "3") or "3")
    except ValueError:
        limit = 3

    new_posts = hits = blocked = fetched = brand_new = 0
    blocked_list = []  # for the scan log: which channels failed this round
    run_status = "error"
    comp_posts = []   # one entry per post with a new competitor hit (digest list)
    brand_wl = matcher.get_client_brands()
    unbranded_wl = matcher.get_unbranded_watchlist()
    try:
        for ch in channels:
            if _STOP.is_set():
                stopped = True
                break
            PROGRESS["current"] = f"{ch['kol_name']} / {ch['platform']}"
            try:
                posts = _collect_with_retry(ch, limit)
            except Exception as e:
                blocked += 1
                blocked_list.append({"kol": ch["kol_name"],
                                     "platform": ch["platform"],
                                     "url": ch["profile_url"],
                                     "error": str(e)[:300]})
                db.update_channel(ch["id"], status="blocked",
                                  last_error=str(e)[:400],
                                  last_checked=datetime.now(timezone.utc).isoformat())
                PROGRESS["done"] += 1
                continue

            fetched += len(posts or [])
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
                base = (p.get("caption") or "").strip()
                extra = (p.get("extra") or "").strip()
                text = (base + "\n" + extra).strip() if extra else base
                # Dedupe key: the caption, or (empty caption, e.g. a plain
                # FB share) the start of the extra text.
                key = base if len(base) >= 20 else text[:300]
                if not pid or db.post_seen(ch["id"], pid, key):
                    continue
                p = dict(p, caption=text)
                new_posts += 1
                found = matcher.scan(p.get("caption", ""),
                                     p.get("hashtags", [])) if is_excl else []
                # Client-brand matches, computed once: used for the brand
                # report below and to decide whether broad topic words count.
                bf = matcher.scan(p.get("caption", ""), p.get("hashtags", []),
                                  watchlist=brand_wl) if brand_wl else []
                # Broad topic words (หุ้นนอก ...) are a hit only on posts
                # that do NOT mention the client — a Webull-sponsored post
                # about US stocks is exactly what we paid for. Skipped when a
                # named competitor already matched (one alert per post is enough).
                # They also need a sign-up/referral/promo signal: a plain
                # article about US stocks is not an alert (the AI reviewer
                # still reads it, so a disguised ad is not lost).
                if is_excl and unbranded_wl and not bf and not found:
                    broad = matcher.scan(p.get("caption", ""),
                                         p.get("hashtags", []),
                                         watchlist=unbranded_wl)
                    sig = matcher.promo_signal(p.get("caption", ""),
                                               p.get("hashtags", [])) if broad else ""
                    if sig:
                        for f in broad:
                            f["promo_signal"] = sig
                        found += broad
                db.mark_seen(ch["id"], pid, p.get("url", ""),
                             p.get("posted_at", ""),
                             caption=p.get("caption", ""),
                             hashtags=p.get("hashtags", []),
                             matched=bool(found),
                             subtitle_url=p.get("subtitle_url", ""))
                new_hits = []   # (hit id, match) — one LINE card per post
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
                        new_hits.append((hid, f))
                if new_hits:
                    comp_posts.append({
                        "kol": ch["kol_name"], "platform": ch["platform"],
                        "url": p.get("url", ""),
                        "posted_at": p.get("posted_at", ""),
                        "keywords": list(dict.fromkeys(
                            f["keyword"] for _, f in new_hits)),
                    })
                    # An old post found only now (a channel just added, or a
                    # scraper reaching further back) is not "urgent": it is
                    # kept on the web page and in the round summary, but no
                    # red card (San, 22 Nov 2026 — 2025 videos alerted at 1am).
                    if _is_old_post(p.get("posted_at", "")):
                        comp_posts[-1]["old"] = True
                    elif _push_keyword_card(ch, p, [f for _, f in new_hits],
                                            unbranded_wl,
                                            [h for h, _ in new_hits]):
                        for hid, _ in new_hits:
                            db.mark_notified(hid)

                # ---- Client-brand report (no immediate push; batched at end) ----
                if brand_wl:
                    for f in bf:
                        if db.hit_exists_for(ch["id"], f["keyword"],
                                             p.get("url", ""), pid):
                            continue
                        if db.add_hit(ch["id"], ch["kol_name"], ch["platform"],
                                      p.get("url", ""), pid,
                                      p.get("posted_at", ""),
                                      f["keyword"], f["snippet"],
                                      kind="brand"):
                            brand_new += 1

            _now_iso = datetime.now(timezone.utc).isoformat()
            db.update_channel(ch["id"], status="ok", last_error="",
                              last_checked=_now_iso, last_ok=_now_iso)
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
            f"⚠️ เจอคำคู่แข่ง: {len(comp_posts)} โพสต์\n"
            f"📊 เจอโพส {brand_label}: {brand_sent}  (เฉพาะโพสต์ลงภายใน {max_age_days} วัน)\n"
            f"❌ เช็กไม่ได้: {blocked}"
        )
        if comp_posts:
            summary += "\n" + _competitor_post_lines(comp_posts)
        budget = _apify_budget_line()
        if budget:
            summary += "\n" + budget
        try:
            import ai_review
            ai_line = ai_review.digest_line()
            if ai_line:
                summary += "\n" + ai_line
        except Exception:
            traceback.print_exc()
        PROGRESS["summary"] = summary
        # Hourly Exclusive rounds: no digest unless something was found —
        # alerts already went out one card per post; 24 digests/day is spam.
        if exclusive_only and not comp_posts:
            stopped = True  # (reuse: suppresses the digest below)
        if not stopped:  # don't LINE-spam a digest on a manual stop
            notify_line.digest([summary])
        db.set_setting("scan_running_since", "")  # finished (or stopped)
        run_status = ("stopped" if (_STOP.is_set()
                                    or PROGRESS["done"] < len(channels))
                      else "done")
    finally:
        try:
            db.finish_run(run_id, done=PROGRESS["done"], fetched=fetched,
                          new_posts=new_posts, hit_posts=len(comp_posts),
                          hits=hits, brand_hits=brand_new, blocked=blocked,
                          blocked_json=blocked_list, status=run_status)
        except Exception:
            traceback.print_exc()
        PROGRESS.update(running=False, current="",
                        last_run=datetime.now(timezone.utc).isoformat())
        _STOP.clear()
    return {"ok": True, "new_posts": new_posts, "hits": hits,
            "hit_posts": len(comp_posts), "blocked": blocked}


def _apify_budget_line():
    """Warn in the digest when this month's Apify spend nears the cap —
    at the cap every TikTok/IG/FB/X channel stops being scanned."""
    try:
        import os
        from urllib.request import urlopen
        tok = (os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN")
               or "").strip()
        if not tok:
            return ""
        with urlopen("https://api.apify.com/v2/users/me/limits?token=" + tok,
                     timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))["data"]
        used = float(d["current"]["monthlyUsageUsd"])
        cap = float(d["limits"]["maxMonthlyUsageUsd"])
        end = d["monthlyUsageCycle"]["endAt"][:10]
        start = datetime.fromisoformat(
            d["monthlyUsageCycle"]["startAt"].replace("Z", "+00:00"))
        days = max((datetime.now(timezone.utc) - start).total_seconds() / 86400, 0.5)
        per_day = used / days
        left = (cap - used) / per_day if per_day > 0 else 999
        if used >= cap * 0.98:
            return (f"⛔ Apify ใช้ครบเพดาน ${used:.0f}/${cap:.0f} แล้ว — "
                    f"TikTok/IG/FB/X หยุดสแกนจนถึง {end} (ต้องเพิ่มเพดาน)")
        if used >= cap * 0.6 or left < 10:
            return (f"💸 Apify ใช้ไป ${used:.0f}/${cap:.0f} (~${per_day:.0f}/วัน) — "
                    f"อีก ~{left:.0f} วันเต็มเพดาน (รอบบิลจบ {end})")
    except Exception:
        traceback.print_exc()
    return ""


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

    # Resume a scan that a restart (deploy / crash) cut off — only the
    # channels it hadn't reached yet. Ignored if it's stale (> 3 h).
    try:
        since = db.get_setting("scan_running_since", "") or ""
        if since:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(since)).total_seconds()
            if age < 3 * 3600:
                print(f"[scan] resuming scan cut off by restart (since {since})",
                      flush=True)
                run_once(triggered_by="ต่อรอบที่ค้างหลังรีสตาร์ท",
                         only_before=since,
                         exclusive_only=(db.get_setting("scan_running_excl", "")
                                         or "") == "1")
            else:
                db.set_setting("scan_running_since", "")
    except Exception:
        traceback.print_exc()

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

                # "เฝ้าเข้ม" mode: Exclusive KOLs also every hour in between
                # the full scans, until excl_hourly_until (then auto-off).
                until = db.get_setting("excl_hourly_until", "") or ""
                if until and now.isoformat() < until:
                    excl_fired = _load_fired("sched_excl_fired")
                    for h in range(24):
                        hhmm = f"{h:02d}:00"
                        if hhmm in times:
                            continue  # full scan already covers this hour
                        if _due(hhmm, excl_fired):
                            excl_fired[hhmm] = today
                            _save_fired("sched_excl_fired", excl_fired)
                            run_once(triggered_by=f"เฝ้าเข้ม Exclusive {hhmm}",
                                     exclusive_only=True)

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
