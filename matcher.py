"""Competitor-keyword matcher — Thai + English, evasion-aware.

Scans caption + hashtags for watchlist terms. Handles common evasion:
lowercase, NFKC, zero-width removal, whitespace collapse, leetspeak
(0->o 1->l 3->e 4->a 5->s 7->t @->a $->s), and a space-stripped pass
to catch "m o o m o o" / "moo moo".
"""
import json
import re
import unicodedata

import db

_ZW = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_LEET = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a",
                       "5": "s", "7": "t", "@": "a", "$": "s"})
_THAI = re.compile(r"[฀-๿]")

# Ordinary Thai words that contain a competitor variant as a substring
# (Thai has no spaces, so plain substring matching can't tell them apart).
# These are blanked out before matching. Extra words can be added without a
# deploy via the `watchlist_exclude` setting (JSON list of strings).
_DEFAULT_EXCLUDE = [
    "พาราไดม์", "พาราไดม",   # paradigm — contains ไดม์ (Dime)
    "มูมมาม",                # messy eating — contains มูม (Moomoo)
]


def _exclude_words():
    words = list(_DEFAULT_EXCLUDE)
    try:
        extra = json.loads(db.get_setting("watchlist_exclude", "[]") or "[]")
        words += [w for w in extra if isinstance(w, str) and w.strip()]
    except Exception:
        pass
    # longest first so "พาราไดม์" is removed before "พาราไดม"
    return sorted({_norm(w) for w in words if _norm(w)}, key=len, reverse=True)


def _norm(s):
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_ZW).lower().translate(_LEET)
    return re.sub(r"\s+", " ", s).strip()


def get_watchlist():
    try:
        return json.loads(db.get_setting("watchlist", "{}"))
    except Exception:
        return {}


def get_unbranded_watchlist():
    """Broad topic words (e.g. หุ้นนอก) that are only a competitor hit when
    the post does not mention our client's brand. Empty = feature off."""
    try:
        wl = json.loads(db.get_setting("watchlist_unbranded", "{}") or "{}")
        return wl if isinstance(wl, dict) else {}
    except Exception:
        return {}


# A broad topic word (หุ้นนอก ...) only counts when the post also looks like
# it is selling something: sign-up / referral / promo wording, or a short or
# app-store link. Plain articles about US stocks carry none of these, so they
# no longer alert (the AI reviewer still reads every Exclusive post).
# Override without a deploy via the `watchlist_promo_signals` setting
# (JSON list of words; replaces this default list).
_DEFAULT_PROMO_SIGNALS = [
    "สมัคร", "เปิดบัญชี", "เปิดพอร์ต", "โค้ด", "รหัสแนะนำ", "ลิงก์", "ลิ้งก์",
    "ลิงค์", "ลิ้งค์", "ในไบโอ", "ค่าคอม", "ฟรีค่าธรรมเนียม", "ไม่มีค่าธรรมเนียม",
    "โปรโมชั่น", "โปรโมชัน", "รับหุ้นฟรี", "แจกหุ้น", "สปอนเซอร์", "พาร์ทเนอร์",
    "โหลดแอป", "ดาวน์โหลดแอป", "แอปเทรด", "แอพเทรด",
    "sign up", "signup", "referral", "promo", "link in bio", "sponsored",
    "#ad", "open account", "download",
]
_PROMO_LINK = re.compile(
    r"(bit\.ly|s\.id/|linktr\.ee|onelink|app\.link|page\.link|tinyurl|cutt\.ly|"
    r"shorturl|t\.co/|lin\.ee|apps\.apple\.com|play\.google\.com|/ref=|"
    r"[?&](ref|referral|invite|inviter|code|promo)=)", re.I)


def _promo_signals():
    try:
        v = json.loads(db.get_setting("watchlist_promo_signals", "") or "null")
        if isinstance(v, list) and any(isinstance(w, str) and w.strip() for w in v):
            return [w for w in v if isinstance(w, str) and w.strip()]
    except Exception:
        pass
    return list(_DEFAULT_PROMO_SIGNALS)


def promo_signal(caption, hashtags):
    """Return the first sign-up/referral/promo signal found, or ''."""
    raw = "  ".join([caption or ""] + [("#" + h) for h in (hashtags or [])])
    m = _PROMO_LINK.search(raw)
    if m:
        return m.group(0)
    text = _norm(raw)
    for w in _promo_signals():
        if _norm(w) and _norm(w) in text:
            return w
    return ""


def get_client_brands():
    """Brands belonging to *our client* — surfaced in a separate report tab
    (not a competitor alert). Default Webull/วีบลู; editable in Settings."""
    try:
        return json.loads(db.get_setting("client_brands", "{}"))
    except Exception:
        return {}


def _variant_hits(text, text_nospace, variant):
    """Return True if a normalized variant appears in the text.

    Thai variants: plain substring (Thai has no word boundaries).
    Latin variants: word-ish boundary so 'dime' doesn't fire inside
    'dimension'; also test the space-stripped text for 'm o o m o o'.
    """
    v = _norm(variant)
    if not v:
        return False
    if _THAI.search(v):
        return v in text
    vns = v.replace(" ", "")
    pat = re.compile(r"(?<![a-z0-9])" + re.escape(vns) + r"(?![a-z0-9])")
    return bool(pat.search(text) or pat.search(text_nospace))


def scan(caption, hashtags, watchlist=None):
    """Return list of {keyword, variant, snippet} for every watchlist hit.

    `watchlist` defaults to the competitor watchlist. Pass a different dict
    to scan against another list (e.g. get_client_brands())."""
    if watchlist is None:
        watchlist = get_watchlist()
    parts = [caption or ""]
    parts += [("#" + h) for h in (hashtags or [])]
    raw = "  ".join(parts)
    text = _norm(raw)
    for w in _exclude_words():
        text = text.replace(w, " ")
    text_nospace = text.replace(" ", "")
    hits = []
    for keyword, variants in watchlist.items():
        for variant in ([keyword] + list(variants)):
            if _variant_hits(text, text_nospace, variant):
                hits.append({
                    "keyword": keyword,
                    "variant": variant,
                    "snippet": _snippet(raw, variant),
                })
                break  # one hit per keyword is enough
    return hits


def _snippet(raw, variant, pad=45):
    """Best-effort context window around the match in the original text."""
    nv = _norm(variant).replace(" ", "")
    norm_chars, idx_map = [], []
    for i, ch in enumerate(_norm(raw)):
        if ch != " ":
            norm_chars.append(ch)
            idx_map.append(i)
    hay = "".join(norm_chars)
    pos = hay.find(nv)
    if pos < 0:
        return raw[:120].strip()
    src = _norm(raw)
    a = max(0, idx_map[pos] - pad)
    b = min(len(src), idx_map[min(pos + len(nv) - 1, len(idx_map) - 1)] + pad)
    return ("…" if a > 0 else "") + src[a:b].strip() + ("…" if b < len(src) else "")
