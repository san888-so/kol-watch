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


def _norm(s):
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_ZW).lower().translate(_LEET)
    return re.sub(r"\s+", " ", s).strip()


def get_watchlist():
    try:
        return json.loads(db.get_setting("watchlist", "{}"))
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


def scan(caption, hashtags):
    """Return list of {keyword, variant, snippet} for every watchlist hit."""
    parts = [caption or ""]
    parts += [("#" + h) for h in (hashtags or [])]
    raw = "  ".join(parts)
    text = _norm(raw)
    text_nospace = text.replace(" ", "")
    hits = []
    for keyword, variants in get_watchlist().items():
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
