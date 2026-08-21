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
# Fallback column indices (used only if header-based resolution fails).
# The sheet has shifted columns before — someone inserted a column and the
# report silently mis-read Review Status/Last Update — so we resolve by
# header text at load time (see _resolve_cols) instead of hard-coding.
COL_CODE = 0         # "Code" — join key to the PIC sheet
COL_NAME = 2          # "a Name"
COL_PHASE = 7         # "Phase"
COL_NAMECONTENT = 8  # "Name + Content" — fallback join key
COL_REVIEW = 43       # "Review Status"
COL_LASTUPDATE = 44  # "Last Update"

# PIC ("ผู้ดูแล") lives in our own working file, tab "PIC Of KOLs":
#   col B = KOL name, col C = PIC, col D = backup PIC.
# (It used to be read off that file's Content Status tab, col A. The team
# restructured that tab — col A went blank — so every warning line printed
# "ไม่เจอชื่อ". Keyed by KOL name now, which matches the main sheet's Name.)
PIC_SHEET_ID = "1L2LZxKhBPvM6NICbH9dLF0wGAjlMQKPZcfG6yx2ebFs"
PIC_TAB = "PIC Of KOLs"
PIC_HDR_ROW = 1      # header on row index 1; data starts at 2
PIC_COL_KOL = 1      # "KOLs List"
PIC_COL_PIC = 2      # "PIC"
PIC_COL_BACKUP = 3   # "สำรอง"
NO_PIC = "ไม่เจอชื่อ"

# Links appended at the end of the report so the team can jump straight into
# the files. Override without a deploy via the `dca_links` setting
# (JSON list of [label, url] pairs; "[]" hides the section).
COUNT_TAB_GID = "794926805"      # "Count (Content Status)" tab of the master sheet
WORK_STATUS_GID = "0"            # "Content Status" tab of our working file
DEFAULT_LINKS = [
    ("ไฟล์งานของเรา (Working File)",
     f"https://docs.google.com/spreadsheets/d/{PIC_SHEET_ID}/edit#gid={WORK_STATUS_GID}"),
    ("สรุปยอด Count (Content Status)",
     f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={COUNT_TAB_GID}"),
]

STALE_DAYS = 3       # warn when an in-progress item hasn't moved in >= this many days
MAX_BUBBLE = 4500    # LINE text message hard limit is ~5000; keep margin
MAX_BUBBLES = 10     # one bubble per campaign + warning — batched across LINE
                     # requests (API caps at 5 messages per request).
LINE_BATCH = 5       # LINE API max messages per push/broadcast call.

# Campaigns to report on: (title, Phase value in the sheet).
# Campaigns rotate as work wraps up, so the live list comes from the
# `dca_campaigns` setting (see get_campaigns) — editable via /api/settings
# with no code deploy. This is only the fallback when that setting is empty.
DEFAULT_CAMPAIGNS = [
    ("Q3 Community Campaign (Leader)", "CC Leader"),
    ("Q3 Community Campaign (Participant)", "CC Participant"),
    ("Market Hotspot (Aug)", "MH Aug"),
    ("Market Hotspot (Sep)", "MH Sep"),
]


def get_campaigns():
    """Read [(title, phase), ...] from the `dca_campaigns` setting.

    Accepts JSON as a list of {"title","phase"} objects, [title, phase]
    pairs, or bare "Phase" strings (title defaults to the phase name, so a
    brand-new campaign can be switched on by adding just its Phase value).
    """
    try:
        import db
        raw = (db.get_setting("dca_campaigns", "") or "").strip()
        if not raw:
            return DEFAULT_CAMPAIGNS
        out = []
        for item in json.loads(raw):
            if isinstance(item, dict):
                phase = str(item.get("phase") or "").strip()
                title = str(item.get("title") or "").strip() or phase
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                title, phase = str(item[0]).strip(), str(item[1]).strip()
            elif isinstance(item, str):
                phase = title = item.strip()
            else:
                continue
            if phase:
                out.append((title or phase, phase))
        return out or DEFAULT_CAMPAIGNS
    except Exception as e:
        print(f"[dca_report] bad dca_campaigns setting ({e}); using defaults",
              flush=True)
        return DEFAULT_CAMPAIGNS

def get_links():
    """Read [(label, url), ...] from the `dca_links` setting (same shapes as
    dca_campaigns: {"label","url"} objects or [label, url] pairs). Falls back
    to DEFAULT_LINKS; an explicit empty list hides the section."""
    try:
        import db
        raw = (db.get_setting("dca_links", "") or "").strip()
        if not raw:
            return DEFAULT_LINKS
        out = []
        for item in json.loads(raw):
            if isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                label = str(item.get("label") or "").strip() or url
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                label, url = str(item[0]).strip(), str(item[1]).strip()
            elif isinstance(item, str):
                url = label = item.strip()
            else:
                continue
            if url:
                out.append((label or url, url))
        return out
    except Exception as e:
        print(f"[dca_report] bad dca_links setting ({e}); using defaults",
              flush=True)
        return DEFAULT_LINKS


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


# Map column labels (normalized) -> which module-level COL_* they update.
# Used by _resolve_cols() to rebind indices from the header row.
_COL_LABELS = {
    "code": "COL_CODE",
    "a name": "COL_NAME",
    "phase": "COL_PHASE",
    "name + content": "COL_NAMECONTENT",
    "review status": "COL_REVIEW",
    "last update": "COL_LASTUPDATE",
}


def _resolve_cols(header, sheet_label=""):
    """Re-bind module-level COL_* from header text. Robust to inserted /
    reordered columns — the sheet has already shifted once and the report
    silently mis-read for a week."""
    hits = {}
    for i, h in enumerate(header):
        key = (str(h) if h is not None else "").strip().lower()
        target = _COL_LABELS.get(key)
        if target and target not in hits:
            hits[target] = i
    for target, idx in hits.items():
        g = globals()
        old = g.get(target)
        if old != idx:
            print(f"[dca_report] {sheet_label}{target}: {old} -> {idx}",
                  flush=True)
            g[target] = idx


def fetch_rows(sheet_id=SHEET_ID, attempts=3):
    """Download the workbook and return Content Status rows. Retries on
    transient network/HTTP errors so a single Google blip doesn't skip a run.
    Resolves column indices from the header so a Sheet column-insert doesn't
    silently break the report."""
    import time
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    last = None
    for i in range(attempts):
        try:
            raw = urllib.request.urlopen(url, timeout=60).read()
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            all_rows = list(wb[SHEET_TAB].iter_rows(values_only=True))
            if len(all_rows) > HDR_ROW and sheet_id == SHEET_ID:
                _resolve_cols(all_rows[HDR_ROW])
            return all_rows[HDR_ROW + 1:]
        except Exception as e:
            last = e
            print(f"[dca_report] fetch_rows attempt {i+1}/{attempts} failed: {e}",
                  flush=True)
            if i < attempts - 1:
                time.sleep(10 * (i + 1))
    raise last


def fetch_pic_map(sheet_id=PIC_SHEET_ID):
    """Return {KOL name: PIC} from our working file's "PIC Of KOLs" tab.
    On any failure return {} so the report still sends (PIC shown as NO_PIC)."""
    import time
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    rows = None
    for i in range(3):
        try:
            raw = urllib.request.urlopen(url, timeout=60).read()
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            if PIC_TAB not in wb.sheetnames:
                print(f"[dca_report] PIC tab {PIC_TAB!r} missing — "
                      f"showing {NO_PIC}", flush=True)
                return {}
            rows = list(wb[PIC_TAB].iter_rows(values_only=True))[PIC_HDR_ROW + 1:]
            break
        except Exception as e:
            print(f"[dca_report] fetch_pic_map attempt {i+1}/3 failed: {e}",
                  flush=True)
            if i < 2:
                time.sleep(10 * (i + 1))
    if rows is None:
        return {}
    by_name = {}
    for r in rows:
        kol = _cell(r, PIC_COL_KOL)
        pic = _cell(r, PIC_COL_PIC) or _cell(r, PIC_COL_BACKUP)
        if kol and pic:
            by_name.setdefault(kol, pic)
    if not by_name:
        print("[dca_report] PIC map is empty — tab layout changed?", flush=True)
    return by_name


def _pic_for(row, pic_map):
    return (pic_map.get(_cell(row, COL_NAME))
            or pic_map.get(_cell(row, COL_CODE))
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


def _warning_lines(rows_by_phase, now, pic_map, campaigns):
    """KOLs whose in-progress content hasn't moved in >= STALE_DAYS days,
    per campaign, longest-idle first. Skips Published/Rejected (finished/dead).
    Each line shows the content's PIC (ผู้ดูแล) from the PIC sheet."""
    out = [f"⚠️ WARNING — งานค้างในไปป์ไลน์ ไม่เคลื่อนไหว ≥{STALE_DAYS} วัน (อิง Last Update)"]
    found = False
    for title, phase in campaigns:
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
    campaigns = get_campaigns()
    # Skip template rows (Phase filled but KOL name still blank) — they're
    # placeholders the team adds for future capacity and would render as
    # empty bullet lines.
    by_phase = {phase: [r for r in rows
                        if _cell(r, COL_PHASE) == phase and _cell(r, COL_NAME)]
                for _, phase in campaigns}

    intro = [f"📊 KOL CONTENT STATUS — DAILY REPORT  📅 {now:%d %b %Y} (GMT+7)"]
    sections = []  # each: list of lines for one bubble
    first = True
    for title, phase in campaigns:
        if not by_phase.get(phase):
            print(f"[dca_report] campaign '{title}' (Phase={phase!r}) matched "
                  f"0 rows — typo or renamed in the sheet?", flush=True)
            continue
        s = (intro + [""] if first else []) + _campaign_lines(title, by_phase[phase])
        sections.append(s)
        first = False
    sections.append(_warning_lines(by_phase, now, fetch_pic_map(), campaigns))
    links = get_links()
    if links:
        sections.append(["🔗 ลิงก์ไฟล์งาน"]
                        + [f"▸ {label}\n{url}" for label, url in links])

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
    if len(bubbles) > MAX_BUBBLES:
        # Don't silently drop campaigns — surface it so we notice and raise
        # the cap rather than the report quietly going incomplete.
        print(f"[dca_report] WARNING: {len(bubbles)} bubbles exceeds cap "
              f"{MAX_BUBBLES}; later sections will be truncated", flush=True)
    return bubbles[:MAX_BUBBLES]


def build_report(sheet_id=SHEET_ID):
    """Single-string form (for --dry preview); bubbles joined with a marker."""
    return "\n\n— — — (bubble break) — — —\n\n".join(build_messages(sheet_id))


def list_phases(sheet_id=SHEET_ID):
    """Every Phase in the sheet with its status counts, most-unfinished first.

    Use this to spot a newly-started campaign and switch it on by adding its
    Phase to the `dca_campaigns` setting — no code change needed."""
    rows = fetch_rows(sheet_id)
    active = {p for _, p in get_campaigns()}
    agg = {}
    for r in rows:
        phase, name = _cell(r, COL_PHASE), _cell(r, COL_NAME)
        if not phase or not name:      # skip blank template rows
            continue
        a = agg.setdefault(phase, {"phase": phase, "total": 0, "published": 0,
                                   "scheduled": 0, "rejected": 0, "in_progress": 0})
        a["total"] += 1
        st = _cell(r, COL_REVIEW)
        if st.startswith(REJECTED_PREFIX):
            a["rejected"] += 1
        elif st == PUBLISHED:
            a["published"] += 1
        elif st in SCHEDULED:
            a["scheduled"] += 1
        else:
            a["in_progress"] += 1
    for a in agg.values():
        a["reporting"] = a["phase"] in active
        # "Unfinished" = anything not yet posted (in-progress or queued).
        a["unfinished"] = a["in_progress"] + a["scheduled"]
    return sorted(agg.values(), key=lambda a: (-a["unfinished"], -a["total"]))


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
