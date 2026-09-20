"""送信待ちキューをDiscordへ送信する共通処理。

main.py（6時間毎の新着チェック）とweekly_catalog_check.py（週次の全件
チェック）の両方から使う。
"""

from __future__ import annotations

import time

import requests

import state_manager
from notifier import RateLimited, send_error_message, send_title_embeds


def drain_queue(provider_key: str, webhook_url: str, queue: list[dict], config: dict) -> None:
    """キューの先頭から上限件数分だけ、複数件をまとめてDiscordへ送信する。

    - 送信は`discord_embeds_per_message`件ずつ1メッセージにまとめる
    - 429を受けたら`retry_after`だけ待って同一実行内でリトライする。
      連続で`rate_limit_max_retries`回失敗したら、輻輳が続いていると判断して
      打ち切り、残りは次回実行に持ち越す（破棄しない）
    """
    limit = config["notify_limit_per_run"]
    interval = config["send_interval_seconds"]
    embeds_per_message = config.get("discord_embeds_per_message", 10)
    max_retries = config.get("rate_limit_max_retries", 3)
    sent_count = 0

    while queue and sent_count < limit:
        chunk_size = min(embeds_per_message, limit - sent_count, len(queue))
        chunk = queue[:chunk_size]
        items = [(item["title"], item.get("image_url")) for item in chunk]

        retries = 0
        while True:
            try:
                send_title_embeds(webhook_url, items)
            except RateLimited as exc:
                retries += 1
                if retries > max_retries:
                    print(
                        f"[{provider_key}] Discordのレート制限が{max_retries}回連続で解消しないため"
                        f"送信を打ち切ります。残り{len(queue)}件は次回実行に持ち越します。"
                    )
                    state_manager.save_queue(provider_key, queue)
                    return
                wait = exc.retry_after if exc.retry_after else 1.0
                print(
                    f"[{provider_key}] Discordのレート制限を受けました。"
                    f"{wait}秒待って同一実行内でリトライします（{retries}/{max_retries}回目）。"
                )
                time.sleep(wait)
                continue
            except requests.RequestException as exc:
                print(f"[{provider_key}] Discord送信エラー: {exc}")
                try_send_error(
                    webhook_url,
                    f"⚠️ [{provider_key}] Discordへの通知送信に失敗しました: {exc}\n"
                    "次回実行時に再試行します。",
                )
                state_manager.save_queue(provider_key, queue)
                return
            else:
                del queue[:chunk_size]
                sent_count += chunk_size
                break

        if queue and sent_count < limit:
            time.sleep(interval)

    print(f"[{provider_key}] 今回の送信: {sent_count}件。キュー残り: {len(queue)}件")
    state_manager.save_queue(provider_key, queue)


def try_send_error(webhook_url: str, message: str) -> None:
    try:
        send_error_message(webhook_url, message)
    except Exception as exc:  # noqa: BLE001 通知自体の失敗はログのみに留める
        print(f"エラー通知の送信にも失敗しました: {exc}")
