"""Load provider API tokens from config file or env vars.

Config file: ~/.tiktok_scraper/providers.json
{
  "apify_token": "apify_api_xxxxx",
  "brightdata_token": "xxxxx",
  "brightdata_datasets": {
    "tiktok_hashtag": "gd_xxxxx",
    "instagram_hashtag": "gd_xxxxx",
    ...
  }
}

Env vars (override file):
  APIFY_TOKEN
  BRIGHTDATA_TOKEN
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_PATH = Path.home() / ".tiktok_scraper" / "providers.json"


def load_config() -> dict:
    """Merge file + env. Env wins."""
    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}

    # Env overrides
    if os.environ.get("APIFY_TOKEN"):
        cfg["apify_token"] = os.environ["APIFY_TOKEN"]
    if os.environ.get("BRIGHTDATA_TOKEN"):
        cfg["brightdata_token"] = os.environ["BRIGHTDATA_TOKEN"]

    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
