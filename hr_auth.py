"""Login with SO! HR — KOL Watch keeps no passwords of its own for people.

  password  /login (POST)  → hr.sociology.asia/api/sso/verify  → signed cookie
  LINE      /sso/start     → hr.sociology.asia/sso/authorize   → /sso/callback
                              → /api/sso/exchange (server-to-server)

Who may enter = setting `hr_users` (comma list of SO! HR usernames, editable on
the Settings page). Removing a name locks that person out on their next click.

env: HR_SSO_KEY (= SSO_SERVICE_KEY of so-hr) · HR_SSO_BASE (default
https://hr.sociology.asia) · KOLWATCH_PUBLIC_URL · KOLWATCH_SECRET (optional;
otherwise a random secret is kept next to the DB so sessions survive deploys).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone

import requests

import db

HR_BASE = os.environ.get("HR_SSO_BASE", "https://hr.sociology.asia").rstrip("/")
APP = "kol-watch"
SESSION_TTL = 14 * 86400
DEFAULT_USERS = "san,petch,waan,pafhan,ploysis"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_user (
    username TEXT PRIMARY KEY, emp_id TEXT, name_th TEXT, name_en TEXT,
    position TEXT, role TEXT, via TEXT,
    first_login TEXT, last_login TEXT, logins INTEGER DEFAULT 0
);
"""


def init():
    with db._lock, db._conn() as c:
        c.executescript(_SCHEMA)


# ------------------------------------------------------------ signing
_SECRET = None


def _secret():
    global _SECRET
    if _SECRET:
        return _SECRET
    k = os.environ.get("KOLWATCH_SECRET", "")
    if not k:
        path = os.path.join(db.DATA_DIR, ".session_secret")
        os.makedirs(db.DATA_DIR, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        k = open(path).read().strip()
    _SECRET = k.encode()
    return _SECRET


def sign(obj):
    raw = base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":"),
                                              ensure_ascii=False).encode()).decode().rstrip("=")
    mac = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:40]
    return f"{raw}.{mac}"


def unsign(tok):
    try:
        raw, mac = (tok or "").rsplit(".", 1)
    except ValueError:
        return None
    good = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:40]
    if not hmac.compare_digest(mac, good):
        return None
    try:
        obj = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        return None
    if obj.get("exp", 0) < time.time():
        return None
    return obj


def session_cookie(username, name, via):
    return sign({"u": username, "n": name, "via": via,
                 "exp": int(time.time() + SESSION_TTL)})


# ------------------------------------------------------------ who may enter
def allowed_users():
    raw = db.get_setting("hr_users", "") or os.environ.get("KOLWATCH_HR_USERS", "") \
        or DEFAULT_USERS
    return [x.strip().lower() for x in raw.replace("\n", ",").split(",") if x.strip()]


def is_allowed(username):
    return (username or "").lower() in allowed_users()


def remember(u, via):
    now = datetime.now(timezone.utc).isoformat()
    with db._lock, db._conn() as c:
        c.execute("""INSERT INTO app_user(username, emp_id, name_th, name_en, position,
                       role, via, first_login, last_login, logins)
                     VALUES(?,?,?,?,?,?,?,?,?,1)
                     ON CONFLICT(username) DO UPDATE SET emp_id=excluded.emp_id,
                       name_th=excluded.name_th, name_en=excluded.name_en,
                       position=excluded.position, role=excluded.role, via=excluded.via,
                       last_login=excluded.last_login, logins=app_user.logins+1""",
                  (u["username"].lower(), u.get("emp_id"), u.get("name_th"),
                   u.get("name_en"), u.get("position_th"), u.get("role"), via, now, now))


def list_users():
    allowed = allowed_users()
    with db._conn() as c:
        rows = {r["username"]: dict(r) for r in c.execute("SELECT * FROM app_user")}
    out = []
    for name in allowed:
        r = rows.get(name, {"username": name})
        r["allowed"] = True
        out.append(r)
    for name, r in rows.items():
        if name not in allowed:
            out.append(dict(r, allowed=False))
    return out


def display_name(u):
    return u.get("name_en") or u.get("username", "").capitalize()


# ------------------------------------------------------------ SO! HR calls
def _key():
    return os.environ.get("HR_SSO_KEY", "")


def verify_password(username, password):
    """→ (user dict | None, Thai error message)"""
    if not _key():
        return None, "ยังไม่ได้ตั้งค่าการเชื่อม SO! HR บนเซิร์ฟเวอร์"
    try:
        r = requests.post(HR_BASE + "/api/sso/verify", timeout=12,
                          headers={"X-SSO-Key": _key()},
                          json={"username": username, "password": password, "app": APP})
        body = r.json()
    except (requests.RequestException, ValueError):
        return None, "ต่อ SO! HR ไม่ได้ ลองใหม่อีกครั้ง"
    if r.status_code == 200 and body.get("ok"):
        return body["user"], None
    if r.status_code == 429:
        return None, f"ใส่รหัสผิดหลายครั้ง รอ {body.get('retry_after', 300)} วินาทีแล้วลองใหม่"
    if r.status_code == 403:
        return None, "กุญแจเชื่อม SO! HR ไม่ถูกต้อง (แจ้งแอดมิน)"
    return None, "ชื่อผู้ใช้หรือรหัสผ่าน SO! HR ไม่ถูกต้อง"


def authorize_url(redirect_uri, state):
    from urllib.parse import urlencode
    return HR_BASE + "/sso/authorize?" + urlencode(
        {"app": APP, "redirect_uri": redirect_uri, "state": state})


def exchange(code, redirect_uri):
    if not _key():
        return None
    try:
        r = requests.post(HR_BASE + "/api/sso/exchange", timeout=12,
                          headers={"X-SSO-Key": _key()},
                          json={"code": code, "app": APP, "redirect_uri": redirect_uri})
        body = r.json() if r.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        body = {}
    return body.get("user") if body.get("ok") else None
