"""動作確認用: 各チャンネルに1件だけ試験通知を送る（state/queueには触れない）。

repository_dispatch (event_type=test-notify) から呼ばれる想定。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from animephilia_client import AnimephiliaError, fetch_recent_events
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

        # 取得に失敗しても送信自体は試す（Discordへの疎通確認が主目的のため）。
        # ただし「画像なしの素のテスト通知」が届いたときにDiscord側の問題なのか
        # 取得側の問題なのか区別がつかないと困るので、理由をタイトルに出す。
        title = "テスト通知（Animephiliaから取得できず）"
        image_url = None
        try:
            candidates = fetch_recent_events(provider_key)
            if candidates:
                title = f"[テスト通知] {candidates[0].title}"
                image_url = candidates[0].image_url
            else:
                title = "テスト通知（取得は成功／直近1週間の配信は0件）"
        except AnimephiliaError as exc:
            print(f"[{provider_key}] Animephilia取得に失敗したため固定テキストで送信します: {exc}")

        send_title_embed(webhook_url, title, image_url)
        print(f"[{provider_key}] 試験通知を送信しました: {title}")


if __name__ == "__main__":
    main()
