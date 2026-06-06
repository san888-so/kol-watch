"""Daily KOL content-status report -> LINE OA broadcast.

Pulls live numbers from the Webull KOL tracking Google Sheet (public, no auth),
computes the status breakdown per campaign (matched by the "Phase" column),
formats a Thai report, and broadcasts it to the KOL Watch LINE OA.

Run:  python3 dca_report.py            # broadcast
      python3 dca_report.py --dry      # print only, no send
"""
import io
import sys
import json
import datetime
import urllib.parse
import urllib.request
import zoneinfo

import openpyxl

# Asia/Bangkok with a fixed +07:00 fallback if the container lacks tzdata
# (Railway's playwright base image was missing the IANA tz files — caused the
# scheduler to error out at 10:00/16:00 silently before this was wired).
try:
    TH_TZ = zoneinfo.ZoneInfo("Asia/Bangkok")
except Exception:
    TH_TZ = datetime.timezone(datetime.timedelta(hours=7))

SHEET_ID = "1O6ls0VtoCxZHlIEsZ6J9FqJSSUuQjaPcKV_FZ6QkGgM"
SHEET_TAB = "Content Status"

# Header is on row index 1 (0-based) in the Content Status tab.
HDR_ROW = 1
COL_CODE = 0         # "Code" — join key to the PIC sheet
COL_NAME = 2          # "a Name"
COL_PHASE = 7         # "Phase"
COL_NAMECONTENT = 8  # "Name + Content" — fallback join key
COL_REVIEW = 42       # "Review Status" — authoritative status, matches Count tab
COL_LASTUPDATE = 43  # "Last Update" (sheet column AR), e.g. "27/05/26 12:35"

# PIC ("ผู้ดูแล") lives in a separate sheet whose Content Status tab has PIC in
# column A, then Code in column B (everything shifted one column vs the main sheet).
PIC_SHEET_ID = "1L2LZxKhBPvM6NICbH9dLF0wGAjlMQKPZcfG6yx2ebFs"
PIC_COL_PIC = 0
PIC_COL_CODE = 1
PIC_COL_NAMECONTENT = 9
NO_PIC = "ไม่เจอชื่อ"

STALE_DAYS = 3       # warn when an in-progress item hasn't moved in >= this many days
MAX_BUBBLE = 4500    # LINE text message hard limit is ~5000; keep margin
MAX_BUBBLES = 10     # one bubble per campaign + warning — batched across LINE
                     # requests (API caps at 5 messages per request).
LINE_BATCH = 5       # LINE API max messages per push/broadcast call.

# Campaigns to report on. (title, Phase value in the sheet).
CAMPAIGNS = [
    ("DCA", "Q2 DCA"),
    ("Multi Portfolio (Direct)", "MP Direct Communicate"),
    ("Multi Portfolio (App Showcase)", "MP App Showcase"),
    ("Multi Portfolio (Competitions)", "MP Competitions"),
    ("Vega AI", "Vega AI"),
]

# When a campaign has more than this many in-progress items, summarize by
# status count instead of listing every KOL name (keeps the LINE message short).
LIST_LIMIT = 6

# Review Status -> Thai action phrase. Per the sheet's Count legend,
# "Draft Approved"=Scheduled and "Storyline Approved"=waiting 1st draft.
INPROG_LABEL = {
    "Wait for 1st Storyline": "รอ Storyline",
    "Wait for Revised Storyline": "รอแก้ Storyline",
    "Need to Revise Storyline": "รอแก้ Storyline",
    "Review Storyline": "รอลูกค้า Review Storyline",
    "Storyline Approved": "รอ 1st Draft",
    "Wait for 1st Draft": "รอ 1st Draft",
    "Wait for Revised Draft": "รอแก้ Draft",
    "Need to Revise Draft": "รอ KOL แก้ไข Draft",
    "Review Draft": "รอลูกค้า Review Draft",
    "Need to Review": "รอลูกค้า Review",
    "Wait for P'Book check ka": "รอ P'Book ตรวจ",
}
PUBLISHED = "Published"
SCHEDULED = {"Scheduled", "Draft Approved"}
REJECTED_PREFIX = "KOL Rejected"


def _cell(row, i):
    return str(row[i]).strip() if i < len(row) and row[i] is not None else ""


def _short(name, n=24):
    name = name.split(" - ")[0].strip()  # drop long descriptive suffix
    return name if len(name) <= n else name[: n - 1] + "…"


def _parse_dt(s):
    s = s.strip()
    for fmt in ("%d/%m/%y %H:%M", "%d/%m/%y", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def fetch_rows(sheet_id=SHEET_ID, attempts=3):
    """Download the workbook and return Content Status rows. Retries on
    transient network/HTTP errors so a single Google blip doesn't skip a run."""
    import time
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    last = None
    for i in range(attempts):
        try:
            raw = urllib.request.urlopen(url, timeout=60).read()
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            return list(wb[SHEET_TAB].iter_rows(values_only=True))[HDR_ROW + 1:]
        except Exception as e:
            last = e
            print(f"[dca_report] fetch_rows attempt {i+1}/{attempts} failed: {e}",
                  flush=True)
            if i < attempts - 1:
                time.sleep(10 * (i + 1))
    raise last


def fetch_pic_map(sheet_id=PIC_SHEET_ID):
    """Return {Code: PIC, Name+Content: PIC} from the PIC sheet. On any
    failure return {} so the report still sends (PIC shown as NO_PIC)."""
    try:
        rows = fetch_rows(sheet_id)  # same tab + header layout
    except Exception:
        return {}
    by_key = {}
    for r in rows:
        pic = _cell(r, PIC_COL_PIC)
        if not pic:
            continue
        code = _cell(r, PIC_COL_CODE)
        nc = _cell(r, PIC_COL_NAMECONTENT)
        if code:
            by_key[code] = pic
        if nc:
            by_key[nc] = pic
    return by_key


def _pic_for(row, pic_map):
    return (pic_map.get(_cell(row, COL_CODE))
            or pic_map.get(_cell(row, COL_NAMECONTENT))
            or NO_PIC)


def _campaign_lines(title, rows):
    published = scheduled = 0
    rejected, inprog = [], []
    for r in rows:
        name = _cell(r, COL_NAME)
        st = _cell(r, COL_REVIEW)
        if st.startswith(REJECTED_PREFIX):
            rejected.append(name)
        elif st == PUBLISHED:
            published += 1
        elif st in SCHEDULED:
            scheduled += 1
        else:
            inprog.append((name, st))

    total = len(rows)
    L = [f"━━━ {title} — {total} contents ━━━",
         f"✅ Active: {total - len(rejected)}   ❌ Reject: {len(rejected)}"]
    if rejected:
        L.append("   ↳ " + ", ".join(_short(n) for n in rejected))
    L.append(f"📤 โพสต์แล้ว: {published}   📅 Schedule: {scheduled}")
    L.append(f"⏳ กำลังทำ: {len(inprog)}")
    # Group in-progress items by status; one KOL name per line for readability.
    groups = {}
    for name, st in inprog:
        groups.setdefault(INPROG_LABEL.get(st, st), []).append(_short(name, 40))
    for label, names in sorted(groups.items(), key=lambda x: -len(x[1])):
        L.append(f"   ▸ {label} ({len(names)})")
        for name in names:
            L.append(f"      - {name}")
    return L


def _warning_lines(rows_by_phase, now, pic_map):
    """KOLs whose in-progress content hasn't moved in >= STALE_DAYS days,
    per campaign, longest-idle first. Skips Published/Rejected (finished/dead).
    Each line shows the content's PIC (ผู้ดูแล) from the PIC sheet."""
    out = [f"⚠️ WARNING — งานค้างในไปป์ไลน์ ไม่เคลื่อนไหว ≥{STALE_DAYS} วัน (อิง Last Update)"]
    found = False
    for title, phase in CAMPAIGNS:
        stale = []
        for r in rows_by_phase.get(phase, []):
            st = _cell(r, COL_REVIEW)
            # Skip finished (Published), dead (Rejected), and already-queued
            # (Scheduled/Draft Approved) — warn only on items still in progress.
            if st == PUBLISHED or st in SCHEDULED or st.startswith(REJECTED_PREFIX):
                continue
            dt = _parse_dt(_cell(r, COL_LASTUPDATE))
            if not dt:
                continue
            days = (now.date() - dt.date()).days
            if days >= STALE_DAYS:
                stale.append((days, _cell(r, COL_NAME), st, _pic_for(r, pic_map)))
        if stale:
            found = True
            stale.sort(key=lambda x: -x[0])
            out.append(f"▢ {title} ({len(stale)})")
            for days, name, st, pic in stale:
                out.append(f"   • {_short(name, 40)} — ค้าง {days} วัน ({INPROG_LABEL.get(st, st)})")
                out.append(f"      ผู้ดูแล: {pic}")
    if not found:
        out.append("   ✅ ไม่มี — ทุกชิ้นงานเคลื่อนไหวภายใน 3 วัน")
    return out


def _chunk(text, limit=MAX_BUBBLE):
    """Pack a long report into LINE bubbles: split on blank-line section
    boundaries first, then hard-split any oversized section by lines."""
    bubbles, cur = [], ""
    for block in text.split("\n\n"):
        add = ("\n\n" + block) if cur else block
        if cur and len(cur) + len(add) > limit:
            bubbles.append(cur)
            cur = block
        else:
            cur += add
    if cur:
        bubbles.append(cur)
    final = []
    for bub in bubbles:
        if len(bub) <= limit:
            final.append(bub)
            continue
        cur = ""
        for ln in bub.split("\n"):
            add = ("\n" + ln) if cur else ln
            if cur and len(cur) + len(add) > limit:
                final.append(cur)
                cur = ln
            else:
                cur += add
        if cur:
            final.append(cur)
    return final[:MAX_BUBBLES]


def build_messages(sheet_id=SHEET_ID):
    """Return the report as a list of LINE text bubbles — one bubble per
    campaign + one for the warning section. If we'd exceed MAX_BUBBLES,
    merge the smallest neighboring sections together."""
    rows = fetch_rows(sheet_id)
    now = datetime.datetime.now(TH_TZ)
    by_phase = {phase: [r for r in rows if _cell(r, COL_PHASE) == phase]
                for _, phase in CAMPAIGNS}

    intro = [f"📊 KOL CONTENT STATUS — DAILY REPORT  📅 {now:%d %b %Y} (GMT+7)"]
    sections = []  # each: list of lines for one bubble
    first = True
    for title, phase in CAMPAIGNS:
        if not by_phase.get(phase):
            continue
        s = (intro + [""] if first else []) + _campaign_lines(title, by_phase[phase])
        sections.append(s)
        first = False
    sections.append(_warning_lines(by_phase, now, fetch_pic_map()))

    # Merge smallest neighbor pairs until count <= MAX_BUBBLES.
    def size(sec): return sum(len(x) + 1 for x in sec)
    while len(sections) > MAX_BUBBLES:
        # find adjacent pair with smallest combined size
        i = min(range(len(sections) - 1),
                key=lambda j: size(sections[j]) + size(sections[j + 1]))
        sections[i:i + 2] = [sections[i] + [""] + sections[i + 1]]

    # Hard-split any oversized section by lines.
    bubbles = []
    for s in sections:
        text = "\n".join(s)
        if len(text) <= MAX_BUBBLE:
            bubbles.append(text)
        else:
            bubbles.extend(_chunk(text))
    return bubbles[:MAX_BUBBLES]


def build_report(sheet_id=SHEET_ID):
    """Single-string form (for --dry preview); bubbles joined with a marker."""
    return "\n\n— — — (bubble break) — — —\n\n".join(build_messages(sheet_id))


def mint_token(channel_id, channel_secret):
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": channel_id,
        "client_secret": channel_secret,
    }).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/oauth/accessToken", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())["access_token"]


def broadcast(bubbles, token):
    last = None
    for i in range(0, len(bubbles), LINE_BATCH):
        batch = bubbles[i:i + LINE_BATCH]
        payload = json.dumps({"messages":
            [{"type": "text", "text": t[:4900]} for t in batch]}).encode()
        req = urllib.request.Request(
            "https://api.line.me/v2/bot/message/broadcast", data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            last = r.status
    return last


def send_report():
    """In-app entry point: build the report and broadcast via the app's
    configured LINE token/mode (notify_line). Returns (ok, message)."""
    import db
    import notify_line
    sheet_id = (db.get_setting("dca_sheet_id", SHEET_ID) or SHEET_ID).strip()
    return notify_line.send_texts(build_messages(sheet_id))


def main():
    if "--dry" in sys.argv:
        print(build_report())
        return
    bubbles = build_messages()
    # Prefer KOL Watch's stored token; else mint from channel id+secret env.
    import os
    token = (os.environ.get("LINE_CHANNEL_TOKEN") or "").strip()
    if not token:
        cid = os.environ["LINE_CHANNEL_ID"]
        sec = os.environ["LINE_CHANNEL_SECRET"]
        token = mint_token(cid, sec)
    status = broadcast(bubbles, token)
    print(f"BROADCAST HTTP {status} ({len(bubbles)} bubble(s))")


if __name__ == "__main__":
    main()
