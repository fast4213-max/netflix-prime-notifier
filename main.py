"""本番実行: JustWatchの新着を取得し、Discordの各チャンネルへ通知する。

repository_dispatch (event_type=run-notify) から呼ばれる想定。
"""

from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path

import requests

import state_manager
from justwatch_client import JustWatchError, fetch_new_titles
from notifier import RateLimited, send_error_message, send_title_embed

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def process_provider(provider_key: str, provider_cfg: dict, config: dict) -> None:
    webhook_url = os.environ.get(provider_cfg["webhook_env"])
    if not webhook_url:
        print(
            f"[{provider_key}] 環境変数 {provider_cfg['webhook_env']} が設定されていません。"
            "このプロバイダの処理をスキップします。"
        )
        return

    seen = state_manager.load_seen(provider_key)
    queue = state_manager.load_queue(provider_key)

    try:
        candidates = fetch_new_titles(
            provider_short_name=provider_cfg["short_name"],
            count=config["new_titles_fetch_count"],
            country=config["country"],
            language=config["language"],
            object_types=config["object_types"],
        )
    except JustWatchError as exc:
        print(f"[{provider_key}] JustWatch取得エラー: {exc}")
        _try_send_error(
            webhook_url,
            f"⚠️ [{provider_key}] JustWatchからの新着取得に失敗しました: {exc}\n"
            "次回実行時に再試行します。",
        )
        # 取得失敗時もキューの続きだけは送っておく（新規追加は無し）
        _drain_queue(provider_key, webhook_url, queue, config)
        state_manager.save_seen(provider_key, seen)
        return

    allowed_types = provider_cfg["allowed_monetization_types"]
    matched = [
        c for c in candidates if c.has_offer(provider_cfg["short_name"], allowed_types)
    ]

    now = state_manager.now_iso()
    new_count = 0
    for entry in matched:
        if entry.id in seen:
            continue
        seen[entry.id] = now
        queue.append(
            {
                "id": entry.id,
                "title": entry.title,
                "image_url": entry.poster_url,
                "detected_at": now,
            }
        )
        new_count += 1

    print(
        f"[{provider_key}] 候補{len(candidates)}件中、条件に合う新着{new_count}件を検知。"
        f"送信待ちキュー: {len(queue)}件"
    )

    _drain_queue(provider_key, webhook_url, queue, config)

    seen = state_manager.prune_seen(seen, config["seen_id_retention_days"])
    state_manager.save_seen(provider_key, seen)


def _drain_queue(provider_key: str, webhook_url: str, queue: list[dict], config: dict) -> None:
    limit = config["notify_limit_per_run"]
    interval = config["send_interval_seconds"]
    sent_count = 0

    while queue and sent_count < limit:
        item = queue[0]
        try:
            send_title_embed(webhook_url, item["title"], item.get("image_url"))
        except RateLimited:
            print(
                f"[{provider_key}] Discordのレート制限を受けたため送信を打ち切ります。"
                f"残り{len(queue)}件は破棄せず次回実行に持ち越します。"
            )
            break
        except requests.RequestException as exc:
            print(f"[{provider_key}] Discord送信エラー: {exc}")
            _try_send_error(
                webhook_url,
                f"⚠️ [{provider_key}] Discordへの通知送信に失敗しました: {exc}\n"
                "次回実行時に再試行します。",
            )
            break
        else:
            queue.pop(0)
            sent_count += 1
            if queue and sent_count < limit:
                time.sleep(interval)

    print(f"[{provider_key}] 今回の送信: {sent_count}件。キュー残り: {len(queue)}件")
    state_manager.save_queue(provider_key, queue)


def _try_send_error(webhook_url: str, message: str) -> None:
    try:
        send_error_message(webhook_url, message)
    except Exception as exc:  # noqa: BLE001 通知自体の失敗はログのみに留める
        print(f"エラー通知の送信にも失敗しました: {exc}")


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
