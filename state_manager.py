"""state/*.json の読み書きを担当するモジュール。

active_*.json : {id: 検知日時(ISO8601)} - Animephiliaのカレンダーで既に通知済み
                 （または初回既読化済み）のタイトルID一覧。ここに無いIDが
                 新着候補として現れたら「新規」として通知する。
queue_*.json  : [{id, title, image_url, detected_at}, ...] - 未送信のFIFOキュー
errors.json   : {エラー署名: 最終通知日時(ISO8601)} - 同じエラーを毎時間
                 通知し続けないためのクールダウン記録
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path(__file__).parent / "state"

# Animephiliaのカレンダーが返すのは直近1週間分だけなので、それより十分に古い
# エントリはもう候補として現れない。残しておいても再通知の抑止には効かず、
# 毎時間コミットされるstateが際限なく膨らむだけなので捨てる。
ACTIVE_RETENTION_DAYS = 90

# 同じ原因のエラーを何時間おきに再通知するか（毎時実行で毎回飛ぶのを防ぐ）。
ERROR_COOLDOWN_HOURS = 6


def _active_path(provider: str) -> Path:
    return STATE_DIR / f"{provider}_active.json"


def _queue_path(provider: str) -> Path:
    return STATE_DIR / f"{provider}_queue.json"


def _errors_path() -> Path:
    return STATE_DIR / "errors.json"


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as exc:
        # stateが壊れていても実行自体は続ける（最悪、再通知が出るだけで済む）。
        print(f"stateファイルの読み込みに失敗したため初期値で続行します: {path} ({exc})")
        return default


def _save_json(path: Path, data) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # 書き込み中にジョブが落ちてもJSONが壊れないよう、一時ファイル経由で置き換える。
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp_path.replace(path)


def load_active(provider: str) -> dict[str, str]:
    data = _load_json(_active_path(provider), {})
    return data if isinstance(data, dict) else {}


def save_active(provider: str, active: dict[str, str]) -> None:
    _save_json(_active_path(provider), active)


def prune_active(active: dict[str, str]) -> dict[str, str]:
    """`ACTIVE_RETENTION_DAYS`より古い記録を落とした新しい辞書を返す。"""
    threshold = datetime.now(timezone.utc) - timedelta(days=ACTIVE_RETENTION_DAYS)
    pruned: dict[str, str] = {}
    for entry_id, detected_at in active.items():
        try:
            seen = datetime.fromisoformat(detected_at)
        except (TypeError, ValueError):
            # 日時が読めないものは判断できないので安全側に倒して残す。
            pruned[entry_id] = detected_at
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if seen >= threshold:
            pruned[entry_id] = detected_at
    return pruned


def load_queue(provider: str) -> list[dict]:
    data = _load_json(_queue_path(provider), [])
    return data if isinstance(data, list) else []


def save_queue(provider: str, queue: list[dict]) -> None:
    _save_json(_queue_path(provider), queue)


def load_error_log() -> dict[str, str]:
    data = _load_json(_errors_path(), {})
    return data if isinstance(data, dict) else {}


def save_error_log(error_log: dict[str, str]) -> None:
    _save_json(_errors_path(), error_log)


def should_notify_error(error_log: dict[str, str], signature: str) -> bool:
    """同じエラーを直近`ERROR_COOLDOWN_HOURS`時間以内に通知済みならFalseを返す。"""
    last = error_log.get(signature)
    if not last:
        return True
    try:
        last_at = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_at >= timedelta(hours=ERROR_COOLDOWN_HOURS)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
