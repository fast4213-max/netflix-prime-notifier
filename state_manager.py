"""state/*.json の読み書きを担当するモジュール。

active_*.json : {id: 最終確認日時(ISO8601)} - 「現在そのプロバイダに存在すると
                 確認済み」のタイトルID一覧。6時間毎のnewTitles diffと週次の
                 全件チェックの両方がこれを更新する。このファイルにIDが無い
                 状態で候補として現れたものは「新規（または再配信）」として
                 通知する。
queue_*.json  : [{id, title, image_url, detected_at}, ...] - 未送信のFIFOキュー
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(__file__).parent / "state"


def _active_path(provider: str) -> Path:
    return STATE_DIR / f"{provider}_active.json"


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


def load_active(provider: str) -> dict[str, str]:
    return _load_json(_active_path(provider), {})


def save_active(provider: str, active: dict[str, str]) -> None:
    _save_json(_active_path(provider), active)


def load_queue(provider: str) -> list[dict]:
    return _load_json(_queue_path(provider), [])


def save_queue(provider: str, queue: list[dict]) -> None:
    _save_json(_queue_path(provider), queue)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
