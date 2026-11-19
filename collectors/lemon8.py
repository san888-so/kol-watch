"""Lemon8 collector — no API, best-effort Playwright scrape.

Lemon8 has no public API and few KOLs use it. We load the profile/share URL,
let it redirect, scroll, and extract post links + visible text. If a per-post
structure can't be parsed we fall back to one synthetic item containing the
whole page text so keyword detection still works (alert points at the URL).
"""
import hashlib
import re

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")


def collect(channel, limit=10):
    from playwright.sync_api import sync_playwright

    url = channel["profile_url"]
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, viewport={"width": 414, "height": 896})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            for _ in range(3):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1500)
            final_url = page.url
            anchors = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => ({href: e.href, text: e.innerText}))",
            )
            body_text = page.inner_text("body")
        finally:
            browser.close()

    seen = set()
    for a in anchors:
        href = a.get("href") or ""
        # Post links: /@user/<numeric id> (current) or /post/<id> (old)
        m = (re.search(r"lemon8-app\.com/@[^/?#]+/(\d{10,})", href)
             or re.search(r"/post/([^/?#]+)", href))
        if not m:
            continue
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)
        text = a.get("text") or ""
        out.append({
            "post_id": pid,
            "url": href,
            "caption": text,
            "hashtags": re.findall(r"#([A-Za-z0-9_฀-๿]+)", text),
            "posted_at": "",
            "pinned": False,
        })
        if len(out) >= limit:
            break

    if not out:
        # Used to return the whole page as one fake "post" — an expired
        # share link (s.lemon8-app.com now 404s) then looked like a healthy
        # channel for months. Fail loudly instead.
        if "s.lemon8-app.com" in (final_url or url) or not body_text.strip():
            raise RuntimeError(
                "Lemon8: ลิงก์ย่อหมดอายุ/เปิดไม่ได้ — ใส่ลิงก์โปรไฟล์แบบ "
                "https://www.lemon8-app.com/@ชื่อบัญชี ในชีตแทน")
        raise RuntimeError("Lemon8: เปิดหน้าได้แต่ไม่พบโพสต์")
    return out
