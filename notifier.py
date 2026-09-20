"""Discord Webhookへの通知送信を担当するモジュール。"""

from __future__ import annotations

import requests


class RateLimited(Exception):
    """Discordから429が返ってきたときに送出する。"""

    def __init__(self, retry_after: float | None):
        self.retry_after = retry_after
        super().__init__(f"Discord rate limited, retry_after={retry_after}")


_MAX_EMBEDS_PER_MESSAGE = 10


def send_title_embed(webhook_url: str, title: str, image_url: str | None) -> None:
    """タイトル+画像のみのシンプルなembedを送信する（1件用）。

    Raises:
        RateLimited: Discordから429が返ってきた場合。
        requests.RequestException: それ以外のHTTPエラーの場合。
    """
    send_title_embeds(webhook_url, [(title, image_url)])


def send_title_embeds(webhook_url: str, items: list[tuple[str, str | None]]) -> None:
    """複数件のタイトル+画像を1メッセージにまとめて送信する。

    1回の呼び出しで1メッセージだけ送る。Discordの1メッセージあたりの
    embed上限は10個のため、呼び出し側で`_MAX_EMBEDS_PER_MESSAGE`件以下に
    区切ってから呼ぶこと（キュー処理側でchunk化して呼び出す設計）。

    Raises:
        RateLimited: Discordから429が返ってきた場合（このメッセージ分は未送信）。
        requests.RequestException: それ以外のHTTPエラーの場合。
    """
    if not items:
        return
    embeds = []
    for title, image_url in items[:_MAX_EMBEDS_PER_MESSAGE]:
        embed: dict = {"title": title}
        if image_url:
            embed["image"] = {"url": image_url}
        embeds.append(embed)
    _post(webhook_url, {"embeds": embeds})


def send_error_message(webhook_url: str, content: str) -> None:
    """エラー内容をテキストメッセージとして送信する。

    429の場合はここでも RateLimited が送出されうるので、
    呼び出し側は429時にこの関数自体の呼び出しを避けること
    （送信中に429を受けた直後にエラー通知を試みても大抵は無意味なため）。
    """
    _post(webhook_url, {"content": content})


def _post(webhook_url: str, payload: dict) -> None:
    response = requests.post(webhook_url, json=payload, timeout=15)
    if response.status_code == 429:
        retry_after = None
        try:
            retry_after = response.json().get("retry_after")
        except ValueError:
            pass
        raise RateLimited(retry_after)
    response.raise_for_status()
