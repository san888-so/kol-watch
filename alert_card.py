"""One LINE Flex card for every competitor alert.

Both alert sources use it, so the team sees one format:
  * keyword  — the scan matched a word from the competitor watchlist
               (or a broad topic word such as หุ้นนอก) in the caption
  * ai       — the kol-ai-guard agent on sanmini read the post and said urgent

The card always shows: the KOL and their contract (Exclusive), platform and
post time, the competitor, the exact word that was found, a short "found
because" line, the quoted text, and a button to open the post.
"""
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    TH_TZ = ZoneInfo("Asia/Bangkok")
except Exception:  # pragma: no cover
    TH_TZ = timezone(timedelta(hours=7))

RED = "#D92D20"
ORANGE = "#DC6803"
GREY = "#667085"
INK = "#111111"


def _txt(text, **kw):
    d = {"type": "text", "text": text or "-", "wrap": True}
    d.update(kw)
    return d


def _row(label, value, **kw):
    style = {"size": "sm", "color": INK}
    style.update(kw)
    return {"type": "box", "layout": "baseline", "spacing": "sm",
            "margin": "md",
            "contents": [_txt(label, size="sm", color=GREY, flex=2),
                         _txt(value, flex=6, **style)]}


def when_th(s):
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


def _contract(post):
    ex = (post.get("exclusivity") or "").strip()
    if not ex:
        return ""
    return "EXCLUSIVE" if "non" not in ex.lower() and "exclusive" in ex.lower() \
        else ex.upper()


def build(post, *, source, competitors=(), words=(), why="", evidence="",
          foreign=False, broad=False, model="", hit_ids=()):
    """Return (altText, bubble).

    post: kol, platform, post_url, posted_at, scanned_at, exclusivity
    source: "keyword" or "ai"
    words: exact words found in the post, e.g. ["finansiahero"]
    broad: only a broad topic word matched (no competitor name) -> orange
    """
    colour = ORANGE if broad else RED
    stamp = datetime.now(TH_TZ).strftime("%d/%m %H:%M")
    title = ("🟠 เฝ้าระวัง · เจอคำกว้าง" if broad
             else "🚨 ด่วน · เจอโพสต์คู่แข่ง")
    by = "AI ตรวจพบ" if source == "ai" else "เจอจากคำค้น"
    posted = ("โพสต์ " + when_th(post.get("posted_at"))) if post.get("posted_at") \
        else ("สแกนเจอ " + when_th(post.get("scanned_at")))

    meta = [_txt(f"{post.get('platform', '')} · {posted}", size="sm",
                 color=GREY, flex=5, gravity="center")]
    contract = _contract(post)
    if contract:
        meta.insert(0, {"type": "box", "layout": "vertical", "flex": 0,
                        "backgroundColor": INK, "cornerRadius": "4px",
                        "paddingStart": "6px", "paddingEnd": "6px",
                        "paddingTop": "2px", "paddingBottom": "2px",
                        "contents": [_txt(contract, size="xxs",
                                          color="#FFFFFF", weight="bold",
                                          wrap=False)]})
    rows = [
        _txt(post.get("kol") or "-", weight="bold", size="xl", color=INK),
        {"type": "box", "layout": "horizontal", "spacing": "sm",
         "margin": "sm", "contents": meta},
        {"type": "separator", "margin": "md"},
    ]
    comps = [c for c in competitors if c]
    if comps:
        rows.append(_row("คู่แข่ง", ", ".join(comps), size="md",
                         weight="bold", color=colour))
    elif not broad:
        rows.append(_row("คู่แข่ง", "สอนเทรดหุ้นต่างประเทศ (ไม่ใช่ Webull)",
                         size="md", weight="bold", color=colour))
    ws = [w for w in words if w]
    if ws:
        rows.append(_row("คำที่เจอ", ", ".join(f"“{w}”" for w in ws),
                         weight="bold", color=colour if broad else INK))
    if foreign:
        rows.append(_row("หุ้นนอก", "พูดเรื่องซื้อ/เทรดหุ้นต่างประเทศ"))
    if why:
        rows.append(_row("เจอเพราะ", why, weight="bold"))
    ev = (evidence or "").strip()
    if ev:
        rows.append({"type": "box", "layout": "vertical", "margin": "md",
                     "backgroundColor": "#FFFAEB" if broad else "#FEF3F2",
                     "cornerRadius": "8px", "paddingAll": "10px",
                     "contents": [_txt(f"“{ev[:220]}”", size="sm",
                                       color="#93370D" if broad else "#912018")]})

    footer = []
    url = post.get("post_url") or ""
    if url.startswith("http"):
        footer.append({"type": "button", "style": "primary", "color": colour,
                       "height": "sm",
                       "action": {"type": "uri", "label": "เปิดโพสต์",
                                  "uri": url[:1000]}})
    ids = ",".join(str(i) for i in hit_ids if i)
    if ids:
        # Team verdict buttons -> LINE postback -> /webhook (main.py)
        footer.append({"type": "box", "layout": "horizontal", "spacing": "sm",
                       "contents": [
            {"type": "button", "style": "secondary", "height": "sm",
             "action": {"type": "postback", "label": "🚨 คู่แข่ง",
                        "data": f"kw=v&v=competitor&h={ids}",
                        "displayText": "🚨 ยืนยัน: โพสต์คู่แข่ง"}},
            {"type": "button", "style": "secondary", "height": "sm",
             "action": {"type": "postback", "label": "✓ ผ่าน",
                        "data": f"kw=v&v=pass&h={ids}",
                        "displayText": "✓ ผ่าน: ไม่ใช่คู่แข่ง"}}]})
    note = (f"AI อ่านโพสต์ ({model or 'sanmini'})" if source == "ai"
            else "คำค้นตรงกับรายชื่อในหน้าเว็บ")
    footer.append(_txt(f"{note} · เปิดดูโพสต์ แล้วกดตัดสิน", size="xxs",
                       color="#98A2B3", align="center", margin="sm"))

    bubble = {
        "type": "bubble", "size": "mega",
        "header": {"type": "box", "layout": "vertical",
                   "backgroundColor": colour, "paddingAll": "16px",
                   "contents": [
                       _txt(title, weight="bold", size="lg", color="#FFFFFF"),
                       _txt(f"{by} {stamp} น.", size="xs",
                            color="#FFFFFF")]},
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "contents": rows},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm",
                   "contents": footer},
    }
    what = ", ".join(comps) or ", ".join(ws) or "หุ้นต่างประเทศ"
    head = "🟠 เฝ้าระวัง" if broad else "🚨 ด่วน"
    alt = f"{head}: {post.get('kol', '')} พูดถึง {what}"[:390]
    return alt, bubble
