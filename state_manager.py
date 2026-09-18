"""state/*.json の読み書きを担当するモジュール。

seen_*.json  : {id: 初回検知日時(ISO8601)} - 重複検知防止用
queue_*.json : [{id, title, image_url, detected_at}, ...] - 未送信のFIFOキュー
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path(__file__).parent / "state"


def _seen_path(provider: str) -> Path:
    return STATE_DIR / f"{provider}_seen.json"


def _queue_path(provider: str) -> Path:
    return STATE_DIR / f"{provider}_queue.json"


def _load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def load_seen(provider: str) -> dict[str, str]:
    return _load_json(_seen_path(provider), {})


def save_seen(provider: str, seen: dict[str, str]) -> None:
    _save_json(_seen_path(provider), seen)


def prune_seen(seen: dict[str, str], retention_days: int) -> dict[str, str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    kept = {}
    for entry_id, detected_at in seen.items():
        try:
            detected = datetime.fromisoformat(detected_at)
        except ValueError:
            continue
        if detected >= cutoff:
            kept[entry_id] = detected_at
    return kept


def load_queue(provider: str) -> list[dict]:
    return _load_json(_queue_path(provider), [])


def save_queue(provider: str, queue: list[dict]) -> None:
    _save_json(_queue_path(provider), queue)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
