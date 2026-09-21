"""本番実行: Animephiliaの配信カレンダーから新着を取得し、Discordの各チャンネルへ通知する。

repository_dispatch (event_type=run-notify) から呼ばれる想定。
6時間毎の実行で、Animephiliaのカレンダーが返す直近1週間分のイベントとの
差分のみを見る（JustWatch経由の方式は新着を検知できていなかったため廃止し、
animephilia_client経由のこの方式に一本化した。過去のカタログ全体との
突き合わせは行わず、今後の配信のみを対象とする）。
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import state_manager
from animephilia_client import AnimephiliaError, fetch_recent_events
from queue_runner import drain_queue, try_send_error
from webhook_config import resolve_webhook_url

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def broadcast_error(config: dict, message: str) -> None:
    """Animephiliaのサイト構造が変わった等、片方だけの問題では済まない可能性が
    ある異常は、通知チャンネルを一方しか見ていない人が気づけないことが無いよう
    両方のチャンネルに送る。
    """
    for other_key, other_cfg in config["providers"].items():
        webhook_url = resolve_webhook_url(other_key, other_cfg)
        if webhook_url:
            try_send_error(webhook_url, message)


def process_provider(provider_key: str, provider_cfg: dict, config: dict) -> None:
    webhook_url = resolve_webhook_url(provider_key, provider_cfg)
    if not webhook_url:
        return

    active = state_manager.load_active(provider_key)
    queue = state_manager.load_queue(provider_key)

    try:
        candidates = fetch_recent_events(provider_key)
    except AnimephiliaError as exc:
        print(f"[{provider_key}] Animephilia取得エラー: {exc}")
        traceback.print_exc()
        broadcast_error(
            config,
            f"⚠️ [{provider_key}] Animephiliaからの新着取得に失敗しました: {exc}\n"
            "サイトの構造が変わった可能性があります。GitHub Actionsの実行ログ"
            "（Notifyワークフロー）にエラー詳細を残してあるので確認してください。"
            "次回実行時に再試行します。",
        )
        # 取得失敗時もキューの続きだけは送っておく（新規追加は無し）
        drain_queue(provider_key, webhook_url, queue, config)
        return

    now = state_manager.now_iso()
    new_count = 0
    for entry in candidates:
        if entry.id in active:
            continue
        active[entry.id] = now
        queue.append(
            {
                "id": entry.id,
                "title": entry.title,
                "image_url": entry.image_url,
                "detected_at": now,
            }
        )
        new_count += 1

    print(
        f"[{provider_key}] 候補{len(candidates)}件中、新着{new_count}件を検知。"
        f"送信待ちキュー: {len(queue)}件"
    )

    state_manager.save_active(provider_key, active)
    drain_queue(provider_key, webhook_url, queue, config)


def main() -> None:
    config = load_config()
    for provider_key, provider_cfg in config["providers"].items():
        try:
            process_provider(provider_key, provider_cfg, config)
        except Exception as exc:  # noqa: BLE001 1プロバイダの想定外エラーで全体を止めない
            print(f"[{provider_key}] 想定外のエラーが発生しました:")
            traceback.print_exc()
            broadcast_error(
                config,
                f"⚠️ [{provider_key}] 想定外のエラーが発生しました: {exc}\n"
                "サイトの構造が変わった可能性があります。GitHub Actionsの実行ログ"
                "（Notifyワークフロー）にエラー詳細を残してあるので確認してください。",
            )


if __name__ == "__main__":
    main()
