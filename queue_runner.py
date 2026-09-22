"""送信待ちキューをDiscordへ送信する共通処理。

main.py（1時間毎の新着チェック）から使う。
エラーは自分では通知せず、文字列のリストとして呼び出し側へ返す
（同一原因のエラーが各チャンネルへ何通も飛ぶのを防ぐため、
通知の集約はmain.py側で行う）。
"""

from __future__ import annotations

import time

import requests

import state_manager
from notifier import (
    MAX_EMBEDS_PER_MESSAGE,
    InvalidRequest,
    RateLimited,
    WebhookUnusable,
    send_error_message,
    send_title_embeds,
)


def _chunk_items(chunk: list[dict]) -> list[tuple[str, str | None]]:
    """キューの要素からembed用の(title, image_url)を組み立てる。

    古い形式のstateなどでキーが欠けていても例外で落とさない
    （落とすとキューが保存されないまま実行が終わり、active側には
    「通知済み」として記録済みの件が永久に失われるため）。
    """
    items: list[tuple[str, str | None]] = []
    for item in chunk:
        if not isinstance(item, dict):
            continue
        items.append((item.get("title") or "(タイトル不明)", item.get("image_url")))
    return items


def drain_queue(
    provider_key: str, webhook_url: str, queue: list[dict], config: dict
) -> list[str]:
    """キューの先頭から上限件数分だけ、複数件をまとめてDiscordへ送信する。

    - 送信は`discord_embeds_per_message`件ずつ1メッセージにまとめる
      （Discordの上限`MAX_EMBEDS_PER_MESSAGE`件を超える設定値は切り詰める）
    - 429を受けたら`retry_after`だけ待って同一実行内でリトライする。
      連続で`rate_limit_max_retries`回失敗したら、輻輳が続いていると判断して
      打ち切り、残りは次回実行に持ち越す（破棄しない）
    - 400を受けたら1件ずつに分割して原因の1件を特定し、その1件だけ捨てる
      （何度送っても成功しない内容なので、そのままだとキューの先頭が
      永久に詰まって以降の新着が一切通知されなくなる）

    Returns:
        発生したエラーの説明文のリスト（正常時は空）。
    """
    limit = config["notify_limit_per_run"]
    interval = config["send_interval_seconds"]
    embeds_per_message = config.get("discord_embeds_per_message", MAX_EMBEDS_PER_MESSAGE)
    embeds_per_message = max(1, min(embeds_per_message, MAX_EMBEDS_PER_MESSAGE))
    max_retries = config.get("rate_limit_max_retries", 3)
    max_rejected = config.get("max_rejected_per_run", 5)
    sent_count = 0
    rejected_count = 0
    errors: list[str] = []

    # どの経路で抜けてもキューを必ず保存する。保存し損ねると、active側には
    # 「通知済み」と記録されたまま未送信の件が消えてしまう。
    try:
        while queue and sent_count < limit:
            chunk_size = min(embeds_per_message, limit - sent_count, len(queue))
            chunk = queue[:chunk_size]

            retries = 0
            while True:
                try:
                    send_title_embeds(webhook_url, _chunk_items(chunk))
                except RateLimited as exc:
                    retries += 1
                    if retries > max_retries:
                        print(
                            f"[{provider_key}] Discordのレート制限が{max_retries}回連続で解消しないため"
                            f"送信を打ち切ります。残り{len(queue)}件は次回実行に持ち越します。"
                        )
                        return errors
                    wait = exc.retry_after if exc.retry_after else 1.0
                    print(
                        f"[{provider_key}] Discordのレート制限を受けました。"
                        f"{wait}秒待って同一実行内でリトライします（{retries}/{max_retries}回目）。"
                    )
                    time.sleep(wait)
                    continue
                except InvalidRequest as exc:
                    if chunk_size > 1:
                        # 何件目が悪いか分からないので1件ずつに切り替えて特定する。
                        print(
                            f"[{provider_key}] Discordにまとめ送信を拒否されました（400）。"
                            "1件ずつ送り直して原因の1件を特定します。"
                        )
                        chunk_size = 1
                        chunk = queue[:1]
                        retries = 0
                        continue
                    rejected_count += 1
                    if rejected_count > max_rejected:
                        # 1件だけ変な内容だったのではなく、送信の仕組み側が
                        # 壊れている可能性が高い。これ以上捨てずに残す。
                        print(
                            f"[{provider_key}] 400が{max_rejected}件を超えました。"
                            f"個別の内容の問題ではないと判断し、残り{len(queue)}件は"
                            "捨てずにキューへ残して打ち切ります。"
                        )
                        errors.append(
                            f"[{provider_key}] Discordが立て続けに送信を拒否しました（400）。"
                            f"残り{len(queue)}件はキューに残してあります: {exc}"
                        )
                        return errors
                    bad = queue[0]
                    print(
                        f"[{provider_key}] Discordに拒否された1件をキューから除外します: "
                        f"{bad.get('title')!r} ({exc})"
                    )
                    errors.append(
                        f"[{provider_key}] Discordが受け付けない内容だったため、"
                        f"次の1件の通知を諦めました: {bad.get('title')!r}"
                    )
                    del queue[0]
                    break
                except WebhookUnusable as exc:
                    print(f"[{provider_key}] Webhook URLが使用できません: {exc}")
                    errors.append(
                        f"[{provider_key}] Discord Webhook URLが使用できません"
                        f"（HTTP {exc.status_code}）。URLが失効・削除された可能性があります。"
                        f"残り{len(queue)}件は送信せずキューに残しました。"
                    )
                    return errors
                except requests.RequestException as exc:
                    print(f"[{provider_key}] Discord送信エラー: {exc}")
                    errors.append(
                        f"[{provider_key}] Discordへの通知送信に失敗しました: {exc}\n"
                        "次回実行時に再試行します。"
                    )
                    return errors
                else:
                    del queue[:chunk_size]
                    sent_count += chunk_size
                    break

            if queue and sent_count < limit:
                time.sleep(interval)

        print(f"[{provider_key}] 今回の送信: {sent_count}件。キュー残り: {len(queue)}件")
        return errors
    finally:
        state_manager.save_queue(provider_key, queue)


def try_send_error(webhook_url: str, message: str) -> bool:
    """エラー通知を送る。送信できたらTrue、失敗したらログだけ出してFalseを返す。

    呼び出し側は戻り値を見て「本当に届いたか」を判断すること。1通も届いて
    いないのに送信済みとして扱うと、クールダウンで次の数時間も黙ってしまい、
    障害に誰も気づけなくなる。
    """
    try:
        send_error_message(webhook_url, message)
        return True
    except Exception as exc:  # noqa: BLE001 通知自体の失敗はログのみに留める
        print(f"エラー通知の送信にも失敗しました: {exc}")
        return False
