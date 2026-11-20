"""LINE notifications via Messaging API.

Delivery mode is the `line_mode` setting:
  - "broadcast" (default) -> /message/broadcast — reaches EVERYONE who
    added the bot, no per-person setup. Ignores line_to.
  - "push"               -> /message/push to a single line_to (user/group).

push_hit() -> a per-hit alert; digest() -> end-of-run summary.
"""
import json
import os
import time
import threading
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import db

_PUSH = "https://api.line.me/v2/bot/message/push"
_BROADCAST = "https://api.line.me/v2/bot/message/broadcast"
_OAUTH = "https://api.line.me/v2/oauth/accessToken"

# Cache the minted access token in-process. Token TTL is 30 days; we
# refresh well before that so a long-running process never serves stale.
_TOKEN_TTL_SEC = 25 * 24 * 3600  # 25 days
_token_cache = {"value": "", "expires_at": 0}
_token_lock = threading.Lock()


def _mint_token(cid, secret):
    body = urlencode({
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": secret,
    }).encode("utf-8")
    req = Request(_OAUTH, data=body, method="POST",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("access_token") or ""


def _get_token():
    """Stateless: mint a fresh access token from channel_id+secret on every
    call (cached for 25 days). Falls back to the DB-stored line_token only
    if env vars aren't set, so the agency never has to babysit token expiry
    again — the May incident where the digest silently failed for a day
    happened because the DB token had quietly hit its 30-day expiry."""
    cid = os.environ.get("LINE_CHANNEL_ID", "").strip()
    secret = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
    if cid and secret:
        with _token_lock:
            now = time.time()
            if _token_cache["value"] and now < _token_cache["expires_at"]:
                return _token_cache["value"]
            try:
                tok = _mint_token(cid, secret)
            except Exception:
                tok = ""
            if tok:
                _token_cache["value"] = tok
                _token_cache["expires_at"] = now + _TOKEN_TTL_SEC
                # Mirror to DB so the dashboard "token set" indicator is
                # truthful and external scripts can still read it.
                try:
                    db.set_setting("line_token", tok)
                    db.set_setting("line_token_minted_at",
                                   datetime.now(timezone.utc).isoformat())
                except Exception:
                    pass
                return tok
    return (db.get_setting("line_token", "") or "").strip()


def _send(messages):
    token = _get_token()
    if not token:
        db.set_setting("line_last_error", "no token (channel_id/secret env missing?)")
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
            ok = 200 <= r.status < 300
            msg = f"HTTP {r.status}"
            db.set_setting("line_last_error",
                           "" if ok else f"{msg} at {datetime.now(timezone.utc).isoformat()}")
            return ok, msg
    except Exception as e:
        err = str(e)[:200]
        # 401 from LINE = token died early — drop the cache so the next
        # call re-mints from channel_id+secret right away.
        if "401" in err or "Unauthorized" in err:
            with _token_lock:
                _token_cache["value"] = ""
                _token_cache["expires_at"] = 0
        db.set_setting("line_last_error",
                       f"{err} at {datetime.now(timezone.utc).isoformat()}")
        return False, err


def verify_signature(body_bytes, signature):
    """X-Line-Signature check (HMAC-SHA256 with the channel secret)."""
    import base64, hashlib, hmac
    secret = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
    if not secret or not signature:
        return False
    mac = hmac.new(secret.encode(), body_bytes, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature)


_NAMES = {}


def display_name(user_id):
    """LINE display name of a friend of the OA (cached in memory)."""
    if not user_id:
        return "ไม่ทราบชื่อ"
    if user_id in _NAMES:
        return _NAMES[user_id]
    name = user_id[-6:]
    try:
        req = Request(f"https://api.line.me/v2/bot/profile/{user_id}",
                      headers={"Authorization": f"Bearer {_get_token()}"})
        with urlopen(req, timeout=10) as r:
            name = json.loads(r.read().decode("utf-8")).get("displayName") or name
            _NAMES[user_id] = name
    except Exception:
        pass
    return name


def reply_text(reply_token, text):
    """Reply to a webhook event (free — doesn't count toward the quota)."""
    if not reply_token:
        return False
    body = json.dumps({"replyToken": reply_token,
                       "messages": [{"type": "text", "text": text[:4900]}]}
                      ).encode("utf-8")
    req = Request("https://api.line.me/v2/bot/message/reply", data=body,
                  method="POST", headers={
                      "Content-Type": "application/json",
                      "Authorization": f"Bearer {_get_token()}"})
    try:
        with urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


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


def push_flex(alt_text, bubble):
    """One Flex bubble (used by the AI urgent alert)."""
    return _send([{"type": "flex", "altText": alt_text[:400],
                   "contents": bubble}])


def digest(lines):
    return _send([{"type": "text", "text": "\n".join(lines)[:4900]}])


def send_texts(texts):
    """Send several text bubbles, batching across LINE requests (5 msg/req cap)."""
    ok_all, last = True, "no messages"
    for i in range(0, len(texts), 5):
        batch = texts[i:i + 5]
        ok, msg = _send([{"type": "text", "text": t[:4900]} for t in batch])
        ok_all = ok_all and ok
        last = msg
    return ok_all, last
