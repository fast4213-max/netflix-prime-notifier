"""state/*.json の読み書きを担当するモジュール。

active_*.json : {id: 最終確認日時(ISO8601)} - 「現在そのプロバイダに存在すると
                 確認済み」のタイトルID一覧。週次の全件チェック(JustWatch)が
                 これを更新・清掃する。このファイルにIDが無い状態で候補として
                 現れたものは「新規（または再配信）」として通知する。

                 6時間毎チェック(main.py, Animephilia)はIDの体系が別物
                 （JustWatchのtm-id vs AnimephiliaのURL）なので、同じ
                 ファイルを共有すると週次チェックの「activeに無いID＝消滅」
                 判定に巻き込まれて誤って削除されてしまう。そのため
                 `source="animephilia"`で別ファイル(active_animephilia_*.json)
                 に分けて保持する。

queue_*.json  : [{id, title, image_url, detected_at}, ...] - 未送信のFIFOキュー
                 （こちらは検知元が違っても送信待ちの入れ物として共有してよい）
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(__file__).parent / "state"


def _active_path(provider: str, source: str) -> Path:
    suffix = "_active.json" if source == "justwatch" else f"_active_{source}.json"
    return STATE_DIR / f"{provider}{suffix}"


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


def load_active(provider: str, source: str = "justwatch") -> dict[str, str]:
    return _load_json(_active_path(provider, source), {})


def save_active(provider: str, active: dict[str, str], source: str = "justwatch") -> None:
    _save_json(_active_path(provider, source), active)


def load_queue(provider: str) -> list[dict]:
    return _load_json(_queue_path(provider), [])


def save_queue(provider: str, queue: list[dict]) -> None:
    _save_json(_queue_path(provider), queue)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
