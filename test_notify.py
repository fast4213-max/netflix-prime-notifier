"""動作確認用: 各チャンネルに1件だけ試験通知を送る（state/queueには触れない）。

repository_dispatch (event_type=test-notify) から呼ばれる想定。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

from animephilia_client import fetch_recent_events
from notifier import send_title_embed

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    config = load_config()
    failed: list[str] = []
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
        except Exception as exc:  # noqa: BLE001 取得が何で落ちても疎通確認は続ける
            print(f"[{provider_key}] Animephilia取得に失敗したため固定テキストで送信します: {exc}")
            traceback.print_exc()

        # 1チャンネル目が失敗してもそこで止めない。疎通確認が目的なので、
        # 残りのチャンネルも必ず試してから、まとめて失敗を報告する。
        try:
            send_title_embed(webhook_url, title, image_url)
        except Exception as exc:  # noqa: BLE001 全チャンネルを試し切るため握る
            print(f"[{provider_key}] 試験通知の送信に失敗しました: {exc}")
            traceback.print_exc()
            failed.append(provider_key)
            continue
        print(f"[{provider_key}] 試験通知を送信しました: {title}")

    if failed:
        # 疎通確認が目的なので、失敗はワークフローを赤くして気づけるようにする。
        sys.exit(f"試験通知に失敗したチャンネル: {', '.join(failed)}")


if __name__ == "__main__":
    main()
