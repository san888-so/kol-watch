"""LINE push notifications via Messaging API.

Needs settings: line_token (channel access token), line_to (user/group id).
push() -> a per-hit alert (KOL, platform, keyword, post link, snippet).
digest() -> end-of-run summary.
"""
import json
from urllib.request import Request, urlopen

import db

_PUSH = "https://api.line.me/v2/bot/message/push"


def _send(messages):
    token = (db.get_setting("line_token", "") or "").strip()
    to = (db.get_setting("line_to", "") or "").strip()
    if not token or not to:
        return False, "line_token / line_to not configured"
    body = json.dumps({"to": to, "messages": messages}).encode("utf-8")
    req = Request(_PUSH, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        with urlopen(req, timeout=20) as r:
            return (200 <= r.status < 300), f"HTTP {r.status}"
    except Exception as e:
        return False, str(e)[:200]


def push_hit(hit):
    text = (
        "🚨 พบโพสต์ถึงคู่แข่ง\n"
        f"KOL: {hit['kol_name']}\n"
        f"แพลตฟอร์ม: {hit['platform']}\n"
        f"คำที่เจอ: {hit['matched_keyword']}\n"
        f"ลิงก์: {hit['post_url']}\n"
        f"ข้อความ: {hit.get('snippet','')}"
    )
    return _send([{"type": "text", "text": text[:4900]}])


def digest(lines):
    return _send([{"type": "text", "text": "\n".join(lines)[:4900]}])
