"""Discord Webhookへの通知送信を担当するモジュール。"""

from __future__ import annotations

import requests


class RateLimited(Exception):
    """Discordから429が返ってきたときに送出する。"""

    def __init__(self, retry_after: float | None):
        self.retry_after = retry_after
        super().__init__(f"Discord rate limited, retry_after={retry_after}")


def send_title_embed(webhook_url: str, title: str, image_url: str | None) -> None:
    """タイトル+画像のみのシンプルなembedを送信する。

    Raises:
        RateLimited: Discordから429が返ってきた場合。
        requests.RequestException: それ以外のHTTPエラーの場合。
    """
    embed: dict = {"title": title}
    if image_url:
        embed["image"] = {"url": image_url}
    _post(webhook_url, {"embeds": [embed]})


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
