"""LINE notifications via Messaging API.

Delivery mode is the `line_mode` setting:
  - "broadcast" (default) -> /message/broadcast — reaches EVERYONE who
    added the bot, no per-person setup. Ignores line_to.
  - "push"               -> /message/push to a single line_to (user/group).

push_hit() -> a per-hit alert; digest() -> end-of-run summary.
"""
import json
from urllib.request import Request, urlopen

import db

_PUSH = "https://api.line.me/v2/bot/message/push"
_BROADCAST = "https://api.line.me/v2/bot/message/broadcast"


def _send(messages):
    token = (db.get_setting("line_token", "") or "").strip()
    if not token:
        return False, "line_token not configured"
    mode = (db.get_setting("line_mode", "broadcast") or "broadcast").strip()

    if mode == "push":
        to = (db.get_setting("line_to", "") or "").strip()
        if not to:
            return False, "line_to not configured (push mode)"
        url = _PUSH
        payload = {"to": to, "messages": messages}
    else:  # broadcast — everyone who added the bot
        url = _BROADCAST
        payload = {"messages": messages}

    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, method="POST", headers={
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
