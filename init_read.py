"""初回セットアップ用: 現在配信中の全タイトルを「既存」として一括登録する（通知は送らない）。

repository_dispatch (event_type=init-read) から呼ばれる想定。
本番運用(main.py / weekly_catalog_check.py)を開始する前に一度だけ実行し、
既存タイトルが一斉に「新規」扱いされて大量通知が飛ぶのを防ぐ。

週次の全件チェックが正しく機能するには`active_{provider}.json`が「現在の
カタログ全体」を正しく反映している必要があるため、newTitles(直近の新着のみ)
ではなく全件取得(fetch_full_catalog)を使う。
"""

from __future__ import annotations

import json
from pathlib import Path

import state_manager
from justwatch_client import JustWatchError, fetch_full_catalog

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    config = load_config()
    for provider_key, provider_cfg in config["providers"].items():
        try:
            candidates = fetch_full_catalog(
                provider_short_name=provider_cfg["short_name"],
                country=config["country"],
                language=config["language"],
                object_types=config["object_types"],
            )
        except JustWatchError as exc:
            print(f"[{provider_key}] JustWatch取得エラー: {exc}")
            continue

        allowed_types = provider_cfg["allowed_monetization_types"]
        matched = [
            c for c in candidates if c.has_offer(provider_cfg["short_name"], allowed_types)
        ]

        active = state_manager.load_active(provider_key)
        now = state_manager.now_iso()
        for entry in matched:
            active.setdefault(entry.id, now)
        state_manager.save_active(provider_key, active)
        print(f"[{provider_key}] {len(matched)}件を既存登録しました（通知なし）。")


if __name__ == "__main__":
    main()
