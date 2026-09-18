"""初回セットアップ用: 既存の新着候補を「既読」として一括登録する（通知は送らない）。

repository_dispatch (event_type=init-read) から呼ばれる想定。
本番運用(main.py)を開始する前に一度だけ実行し、既存タイトルが
一斉に「新着」扱いされて大量通知が飛ぶのを防ぐ。
"""

from __future__ import annotations

import json
from pathlib import Path

import state_manager
from justwatch_client import JustWatchError, fetch_new_titles

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    config = load_config()
    for provider_key, provider_cfg in config["providers"].items():
        try:
            candidates = fetch_new_titles(
                provider_short_name=provider_cfg["short_name"],
                count=config["init_fetch_count"],
                country=config["country"],
                language=config["language"],
                object_types=config["object_types"],
            )
        except JustWatchError as exc:
            print(f"[{provider_key}] JustWatch取得エラー: {exc}")
            continue

        seen = state_manager.load_seen(provider_key)
        now = state_manager.now_iso()
        for entry in candidates:
            seen.setdefault(entry.id, now)
        state_manager.save_seen(provider_key, seen)
        print(f"[{provider_key}] {len(candidates)}件を既読登録しました（通知なし）。")


if __name__ == "__main__":
    main()
