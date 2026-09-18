"""動作確認用: 各チャンネルに1件だけ試験通知を送る（state/queueには触れない）。

repository_dispatch (event_type=test-notify) から呼ばれる想定。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from justwatch_client import JustWatchError, fetch_new_titles
from notifier import send_title_embed

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    config = load_config()
    for provider_key, provider_cfg in config["providers"].items():
        webhook_url = os.environ.get(provider_cfg["webhook_env"])
        if not webhook_url:
            print(f"[{provider_key}] 環境変数 {provider_cfg['webhook_env']} が未設定のためスキップ")
            continue

        title = "テスト通知"
        image_url = None
        try:
            candidates = fetch_new_titles(
                provider_short_name=provider_cfg["short_name"],
                count=1,
                country=config["country"],
                language=config["language"],
                object_types=config["object_types"],
            )
            if candidates:
                title = f"[テスト通知] {candidates[0].title}"
                image_url = candidates[0].poster_url
        except JustWatchError as exc:
            print(f"[{provider_key}] JustWatch取得に失敗したため固定テキストで送信します: {exc}")

        send_title_embed(webhook_url, title, image_url)
        print(f"[{provider_key}] 試験通知を送信しました: {title}")


if __name__ == "__main__":
    main()
