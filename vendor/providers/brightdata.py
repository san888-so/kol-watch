"""Bright Data provider — call Datasets API.

Reference: https://docs.brightdata.com/api-reference/web-scraper-api/overview

Workflow:
  1. POST /datasets/v3/trigger → snapshot_id
  2. GET /datasets/v3/progress/{snapshot_id} → poll until 'ready'
  3. GET /datasets/v3/snapshot/{snapshot_id}?format=json → data

ห้ามแตะ scrapers ที่ทีมเก่าใช้: ใช้ public dataset_id ที่ Bright Data จัดให้
ผ่าน API token ใหม่ของเรา → snapshot ใหม่ทุกครั้ง ไม่กระทบของเก่า
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import requests

from providers.base import (
    ProviderBase,
    normalize_tiktok_post,
    normalize_x_tweet,
    normalize_youtube_video,
)


# Dataset IDs ของ Bright Data (public scrapers)
# Ref: https://brightdata.com/cp/scrapers/browse/social-media-scrapers/
DATASETS = {
    # TikTok
    "tiktok_posts_by_keyword": "gd_lu702nij2f790tmv9h",     # TikTok posts by search keyword
    "tiktok_posts_by_url": "gd_lu702nij2f790tmv9h",
    "tiktok_profile_by_url": "gd_l1villgoiiidt09ci",
    "tiktok_comments_by_url": "gd_lkf2st302ap89utw5k",

    # Instagram
    "instagram_posts_by_url": "gd_lk5ns7kz21pck8jpis",
    "instagram_posts_by_hashtag": "gd_lk5ns7kz21pck8jpis",
    "instagram_profiles": "gd_l1vikfch901nx3by4",
    "instagram_comments": "gd_ltppn085pokosxh13",
    "instagram_reels": "gd_lyclm20il4r5helnj",

    # Facebook
    "facebook_posts_by_url": "gd_lyclm1571iy3mv57zw",
    "facebook_posts_by_profile": "gd_lkaxegm826bjpoo9m5",
    "facebook_pages": "gd_lkj4lboofsd0rt4xtd",
    "facebook_comments": "gd_lkay758p1eanlolqw8",

    # X
    "x_posts_by_url": "gd_lwxkxvnf1cynvib9co",
    "x_profiles": "gd_lwxmeb2u1cniijd7t4",

    # YouTube
    "youtube_videos": "gd_lk56epmy2i5g7lzu0k",
    "youtube_profiles": "gd_lk538t2k2p1k3oos71",
    "youtube_comments": "gd_lk9q0ew71spt1mxywf",
}

API_BASE = "https://api.brightdata.com/datasets/v3"


class BrightDataProvider(ProviderBase):
    name = "brightdata"

    def __init__(self, token: str, poll_interval: int = 5, max_wait_sec: int = 600):
        self.token = token
        self.poll_interval = poll_interval
        self.max_wait = max_wait_sec
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def is_configured(self) -> bool:
        return bool(self.token)

    def _trigger(self, dataset_id: str, payload: list, discover_by: Optional[str] = None) -> str:
        params = {
            "dataset_id": dataset_id,
            "include_errors": "true",
            "notify": "false",
        }
        if discover_by:
            params["type"] = "discover_new"
            params["discover_by"] = discover_by

        r = self.session.post(f"{API_BASE}/trigger", json=payload, params=params, timeout=60)
        if r.status_code not in (200, 202):
            raise RuntimeError(f"BD trigger HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        snapshot_id = data.get("snapshot_id")
        if not snapshot_id:
            raise RuntimeError(f"BD trigger missing snapshot_id: {data}")
        return snapshot_id

    def _wait_snapshot(self, snapshot_id: str, on_progress: Optional[Callable] = None) -> None:
        elapsed = 0
        while elapsed < self.max_wait:
            r = self.session.get(f"{API_BASE}/progress/{snapshot_id}", timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"BD progress HTTP {r.status_code}: {r.text[:200]}")
            d = r.json()
            status = d.get("status") or ""
            if on_progress:
                on_progress(status, d.get("rows", 0), d.get("rows", 0))
            if status == "ready":
                return
            if status in ("failed", "canceled"):
                raise RuntimeError(f"BD snapshot {snapshot_id} status={status}: {d}")
            time.sleep(self.poll_interval)
            elapsed += self.poll_interval
        raise RuntimeError(f"BD snapshot {snapshot_id} timed out after {self.max_wait}s")

    def _fetch_snapshot(self, snapshot_id: str) -> list:
        r = self.session.get(
            f"{API_BASE}/snapshot/{snapshot_id}",
            params={"format": "json"},
            timeout=120,
        )
        if r.status_code != 200:
            raise RuntimeError(f"BD snapshot fetch HTTP {r.status_code}: {r.text[:200]}")
        # Response: array of dicts (ndjson or json array)
        text = r.text.strip()
        if text.startswith("["):
            return r.json()
        # NDJSON fallback
        import json as _json
        out = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_json.loads(line))
            except Exception:
                pass
        return out

    def _run_discover(self, dataset_id: str, payload: list,
                      discover_by: str = "keyword",
                      on_progress: Optional[Callable] = None) -> list:
        snapshot_id = self._trigger(dataset_id, payload, discover_by=discover_by)
        if on_progress:
            on_progress("triggered", 0, 0)
        self._wait_snapshot(snapshot_id, on_progress=on_progress)
        return self._fetch_snapshot(snapshot_id)

    # --- TikTok ---

    def fetch_tiktok_hashtag(self, hashtag: str, limit: int = 50,
                             on_progress=None) -> list:
        tag = hashtag.lstrip("#").strip()
        # BD's TikTok discover by keyword expects: [{"search_keyword": "...", "num_of_posts": N}]
        payload = [{"search_keyword": tag, "num_of_posts": limit}]
        raw = self._run_discover(
            DATASETS["tiktok_posts_by_keyword"],
            payload,
            discover_by="keyword",
            on_progress=on_progress,
        )
        return [normalize_tiktok_post(r, "brightdata") for r in raw[:limit]]

    # --- Instagram ---

    def fetch_instagram_hashtag(self, hashtag: str, limit: int = 50,
                                on_progress=None) -> list:
        tag = hashtag.lstrip("#").strip()
        payload = [{"hashtag": tag, "num_of_posts": limit}]
        raw = self._run_discover(
            DATASETS["instagram_posts_by_hashtag"],
            payload,
            discover_by="hashtag",
            on_progress=on_progress,
        )
        return raw[:limit]

    # --- Facebook ---

    def fetch_facebook_hashtag(self, hashtag: str, limit: int = 50,
                               on_progress=None) -> list:
        # FB ของ BD ส่วนใหญ่ require URL ไม่ใช่ hashtag — ต้องดูเอกสารเฉพาะ
        # TODO: ระบุ dataset เหมาะสมเมื่อ test แล้ว
        raise NotImplementedError("Bright Data FB hashtag flow ยังไม่ได้ test")

    # --- X ---

    def fetch_x_hashtag(self, hashtag: str, limit: int = 50,
                       on_progress=None) -> list:
        tag = hashtag.lstrip("#").strip()
        payload = [{"keyword": f"#{tag}", "num_of_posts": limit}]
        try:
            raw = self._run_discover(
                DATASETS["x_posts_by_url"],
                payload,
                discover_by="keyword",
                on_progress=on_progress,
            )
        except Exception as e:
            raise RuntimeError(f"BD X scrape failed: {e}")
        return [normalize_x_tweet(r, "brightdata") for r in raw[:limit]]

    # --- YouTube ---

    def fetch_youtube_keyword(self, keyword: str, limit: int = 50,
                              on_progress=None) -> list:
        payload = [{"keyword": keyword, "num_of_posts": limit}]
        try:
            raw = self._run_discover(
                DATASETS["youtube_videos"],
                payload,
                discover_by="keyword",
                on_progress=on_progress,
            )
        except Exception as e:
            raise RuntimeError(f"BD YouTube scrape failed: {e}")
        return [normalize_youtube_video(r, "brightdata") for r in raw[:limit]]
