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

try:
    from zoneinfo import ZoneInfo
    TH_TZ = ZoneInfo("Asia/Bangkok")
except Exception:  # pragma: no cover
    TH_TZ = timezone(timedelta(hours=7))

LEVELS = ("urgent", "watch", "none", "skip")
RED = "#D92D20"


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
        new = db.save_ai_review(
            sid, level, comps, r.get("foreign_stock_trading"),
            r.get("mentions_webull"), str(r.get("reason") or "")[:500],
            str(r.get("evidence") or "")[:300], model)
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
            ok, _ = notify_line.push_flex(*alert_flex(post, comps, r, model))
            if ok:
                db.mark_ai_alerted(sid)
                if hid:
                    db.mark_notified(hid)
                out["alerted"] += 1
    return out


def _txt(text, **kw):
    d = {"type": "text", "text": text or "-", "wrap": True}
    d.update(kw)
    return d


def alert_flex(post, comps, r, model):
    """Red 'urgent' Flex bubble. Returns (altText, bubble)."""
    foreign = bool(r.get("foreign_stock_trading"))
    what = ", ".join(comps) if comps else "สอนเทรดหุ้นต่างประเทศ (ไม่ใช่ Webull)"
    stamp = datetime.now(TH_TZ).strftime("%d/%m %H:%M")
    posted = _posted_th(post.get("posted_at"))
    rows = [
        _txt(post["kol"], weight="bold", size="xl", color="#111111"),
        _txt(f"{post['platform']} · โพสต์ {posted}", size="sm", color="#667085"),
        {"type": "separator", "margin": "md"},
        {"type": "box", "layout": "baseline", "margin": "md", "spacing": "sm",
         "contents": [_txt("คู่แข่ง", size="sm", color="#667085", flex=2),
                      _txt(what, size="md", weight="bold", color=RED, flex=6)]},
    ]
    if foreign:
        rows.append({"type": "box", "layout": "baseline", "spacing": "sm",
                     "contents": [_txt("หุ้นนอก", size="sm", color="#667085", flex=2),
                                  _txt("พูดเรื่องซื้อ/เทรดหุ้นต่างประเทศ",
                                       size="sm", color="#111111", flex=6)]})
    rows.append(_txt(r.get("reason") or "", size="sm", color="#344054",
                     margin="md"))
    ev = (r.get("evidence") or "").strip()
    if ev:
        rows.append({"type": "box", "layout": "vertical", "margin": "md",
                     "backgroundColor": "#FEF3F2", "cornerRadius": "8px",
                     "paddingAll": "10px",
                     "contents": [_txt(f"“{ev[:200]}”", size="sm",
                                       color="#912018")]})
    footer = []
    url = post.get("post_url") or ""
    if url.startswith("http"):
        footer.append({"type": "button", "style": "primary", "color": RED,
                       "height": "sm",
                       "action": {"type": "uri", "label": "เปิดโพสต์",
                                  "uri": url[:1000]}})
    footer.append(_txt(f"AI อ่านจากแคปชัน ({model or 'sanmini'}) · "
                       "เช็กโพสต์ก่อนแจ้ง KOL", size="xxs",
                       color="#98A2B3", align="center", margin="sm"))
    bubble = {
        "type": "bubble", "size": "mega",
        "header": {"type": "box", "layout": "vertical",
                   "backgroundColor": RED, "paddingAll": "16px",
                   "contents": [
                       _txt("🚨 ด่วน · เจอโพสต์คู่แข่ง", weight="bold",
                            size="lg", color="#FFFFFF"),
                       _txt(f"AI ตรวจพบ {stamp} น.", size="xs",
                            color="#FEE4E2")]},
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "contents": rows},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm",
                   "contents": footer},
    }
    alt = f"🚨 ด่วน: {post['kol']} พูดถึง {what}"[:390]
    return alt, bubble


def _posted_th(s):
    if not s:
        return "—"
    if len(s) == 10:
        return s
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(TH_TZ).strftime("%d/%m %H:%M")
    except Exception:
        return s[:16]


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
    line = (f"🤖 AI อ่านโพสต์ (24 ชม.): 🔴 ด่วน {st.get('urgent', 0)} · "
            f"🟠 เฝ้าระวัง {st.get('watch', 0)} · รออ่าน {s['pending']}")
    q = s["agent_quiet_min"]
    if q is None or q > 20:
        line += ("\n⚠️ AI agent บน sanmini เงียบ "
                 + ("(ยังไม่เคยต่อ)" if q is None else f"{q} นาที")
                 + " — โพสต์ยังไม่ถูกอ่าน")
    return line
