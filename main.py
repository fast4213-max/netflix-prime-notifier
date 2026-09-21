"""本番実行: Animephiliaの配信カレンダーから新着を取得し、Discordの各チャンネルへ通知する。

repository_dispatch (event_type=run-notify) から呼ばれる想定。
6時間毎の実行で、Animephiliaのカレンダーが返す直近1週間分のイベントとの
差分のみを見る（JustWatchの`newTitles`インデックスが新着を検知できて
いなかったため、animephilia_client経由のこの方式に切り替えた）。
取りこぼしや「配信終了→再配信」の検知はweekly_catalog_check.py（週次の
JustWatch全件チェック）が担当する。
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


def process_provider(provider_key: str, provider_cfg: dict, config: dict) -> None:
    webhook_url = resolve_webhook_url(provider_key, provider_cfg)
    if not webhook_url:
        return

    active = state_manager.load_active(provider_key, source="animephilia")
    queue = state_manager.load_queue(provider_key)

    try:
        candidates = fetch_recent_events(provider_key)
    except AnimephiliaError as exc:
        print(f"[{provider_key}] Animephilia取得エラー: {exc}")
        try_send_error(
            webhook_url,
            f"⚠️ [{provider_key}] Animephiliaからの新着取得に失敗しました: {exc}\n"
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

    state_manager.save_active(provider_key, active, source="animephilia")
    drain_queue(provider_key, webhook_url, queue, config)


def main() -> None:
    config = load_config()
    for provider_key, provider_cfg in config["providers"].items():
        try:
            process_provider(provider_key, provider_cfg, config)
        except Exception:  # noqa: BLE001 1プロバイダの想定外エラーで全体を止めない
            print(f"[{provider_key}] 想定外のエラーが発生しました:")
            traceback.print_exc()


if __name__ == "__main__":
    main()
