"""AI post review — the bridge to the kol-ai-guard agent on sanmini.

The agent polls GET /api/ai/pending (scanned posts from Exclusive KOLs the
AI hasn't read yet), reads each caption with a local LLM, and POSTs its
verdict to /api/ai/review. Verdicts are stored in ai_review; an "urgent"
verdict also becomes a competitor hit + a red LINE Flex alert, unless the
keyword matcher already alerted the same post.

Polling (not push) means nothing on sanmini has to be reachable from the
internet, and posts are picked up while a scan is still running.
"""
import json
from datetime import datetime, timezone, timedelta

import db
import matcher
import notify_line
import alert_card

try:
    from zoneinfo import ZoneInfo
    TH_TZ = ZoneInfo("Asia/Bangkok")
except Exception:  # pragma: no cover
    TH_TZ = timezone(timedelta(hours=7))

LEVELS = ("urgent", "watch", "none", "skip")


def _now():
    return datetime.now(timezone.utc)


def _since():
    """Only posts scanned after the feature went live are queued. On first
    use, start one day back so today's posts get read too."""
    s = db.get_setting("ai_review_since", "") or ""
    if not s:
        s = (_now() - timedelta(days=1)).isoformat()
        db.set_setting("ai_review_since", s)
    return s


def pending(limit, agent_name):
    db.set_setting("ai_agent_seen", _now().isoformat())
    db.set_setting("ai_agent_name", agent_name or "")
    enabled = (db.get_setting("ai_review_enabled", "1") or "1") == "1"
    since = _since()
    posts = db.ai_pending(limit=max(1, min(int(limit or 5), 50)),
                          since=since) if enabled else []
    return {
        "enabled": enabled,
        "pending_total": db.ai_pending_count(since) if enabled else 0,
        "policy": db.get_setting("ai_review_policy", "") or "",
        "known_competitors": list(matcher.get_watchlist().keys()),
        "client_brands": list(matcher.get_client_brands().keys()),
        "posts": posts,
    }


def _fresh(posted_at):
    try:
        days = int(db.get_setting("ai_alert_max_age_days", "3") or "3")
    except ValueError:
        days = 3
    pa = (posted_at or "")[:10]
    try:
        d = datetime.strptime(pa, "%Y-%m-%d").date()
    except ValueError:
        return True  # unknown date -> alert rather than miss it
    return d >= (datetime.now(TH_TZ) - timedelta(days=days)).date()


def _keyword_alert_exists(post):
    with db._conn() as c:
        r = c.execute("""SELECT 1 FROM hit WHERE kind='competitor'
                         AND channel_id=? AND (post_id=? OR post_url=?)
                         LIMIT 1""",
                      (post["channel_id"], post["post_id"],
                       post["post_url"])).fetchone()
    return bool(r)


def save(payload):
    model = str(payload.get("model") or "")[:60]
    out = {"saved": 0, "alerted": 0, "skipped_dup": 0}
    for r in payload.get("results") or []:
        try:
            sid = int(r.get("seen_post_id"))
        except (TypeError, ValueError):
            continue
        level = (r.get("level") or "none").strip().lower()
        if level not in LEVELS:
            level = "none"
        comps = [str(x).strip() for x in (r.get("competitors") or [])
                 if str(x).strip() and str(x).strip().lower() != "none"]
        post = db.ai_post(sid)
        if not post:
            continue
        # Posts before the competitor-watch start (2025 and older) are not
        # a breach: keep the AI's reading but never as urgent/watch.
        if level in ("urgent", "watch") and db.before_watch(post.get("posted_at")):
            level = "skip"
        new = db.save_ai_review(
            sid, level, comps, r.get("foreign_stock_trading"),
            r.get("mentions_webull"), str(r.get("reason") or "")[:500],
            str(r.get("evidence") or "")[:300], model,
            str(r.get("why") or r.get("why_short") or "")[:200])
        if not new:
            out["skipped_dup"] += 1
            continue
        out["saved"] += 1
        if (level == "urgent"
                and (db.get_setting("ai_alerts_enabled", "1") or "1") == "1"
                and not _keyword_alert_exists(post)
                and _fresh(post.get("posted_at"))):
            label = ("AI: " + ", ".join(comps)) if comps else "AI: หุ้นต่างประเทศ"
            hid = db.add_hit(post["channel_id"], post["kol"], post["platform"],
                             post["post_url"], post["post_id"],
                             post.get("posted_at", ""), label[:120],
                             str(r.get("evidence") or r.get("reason") or "")[:300],
                             kind="competitor")
            ok, _ = notify_line.push_flex(*alert_flex(post, comps, r, model,
                                                      hit_ids=[hid]))
            if ok:
                db.mark_ai_alerted(sid)
                if hid:
                    db.mark_notified(hid)
                out["alerted"] += 1
    return out


def alert_flex(post, comps, r, model, hit_ids=()):
    """Red 'urgent' card (shared layout with keyword alerts)."""
    why = (r.get("why") or r.get("why_short") or "").strip() \
        or (r.get("reason") or "")[:120]
    return alert_card.build(
        post, source="ai", competitors=comps, why=why,
        evidence=r.get("evidence") or "",
        foreign=bool(r.get("foreign_stock_trading")), model=model,
        hit_ids=hit_ids)


def status():
    seen = db.get_setting("ai_agent_seen", "") or ""
    mins = None
    if seen:
        try:
            mins = int((_now() - datetime.fromisoformat(seen)).total_seconds() // 60)
        except Exception:
            pass
    day = (_now() - timedelta(days=1)).isoformat()
    return {
        "enabled": (db.get_setting("ai_review_enabled", "1") or "1") == "1",
        "agent": db.get_setting("ai_agent_name", "") or "",
        "agent_seen": seen,
        "agent_quiet_min": mins,
        "pending": db.ai_pending_count(_since()),
        "last24h": db.ai_stats_since(day),
    }


def digest_line():
    """One line for the end-of-scan LINE digest."""
    s = status()
    if not s["enabled"]:
        return ""
    st = s["last24h"]
    # AI reads every new post (not just keyword hits): 🔴 = likely promotes a
    # competitor / another broker, 🟠 = unclear, worth a human look.
    line = (f"🤖 AI อ่านทุกโพสต์ใหม่ (24 ชม.): 🔴 น่าจะโปรคู่แข่ง {st.get('urgent', 0)} · "
            f"🟠 ควรดูอีกที {st.get('watch', 0)} · ⏳ ยังไม่ได้อ่าน {s['pending']}")
    q = s["agent_quiet_min"]
    if q is None or q > 20:
        line += ("\n⚠️ AI agent บน sanmini เงียบ "
                 + ("(ยังไม่เคยต่อ)" if q is None else f"{q} นาที")
                 + " — โพสต์ยังไม่ถูกอ่าน")
    return line
