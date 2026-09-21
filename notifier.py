"""Discord Webhookへの通知送信を担当するモジュール。"""

from __future__ import annotations

import requests

# Discordの制限値。超えると400 Bad Requestが返る。
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_TITLE_LENGTH = 256
MAX_CONTENT_LENGTH = 2000


class RateLimited(Exception):
    """Discordから429が返ってきたときに送出する。"""

    def __init__(self, retry_after: float | None):
        self.retry_after = retry_after
        super().__init__(f"Discord rate limited, retry_after={retry_after}")


class InvalidRequest(Exception):
    """Discordから400が返ってきたときに送出する。

    ペイロードそのものが受け付けられていないので、同じ内容を何度送り直しても
    成功しない。呼び出し側はリトライせず、問題のある件を切り分けること。
    """

    def __init__(self, body: str):
        self.body = body
        super().__init__(f"Discord rejected the payload (400): {body}")


class WebhookUnusable(Exception):
    """Webhook URL自体が使えない（401/403/404）ときに送出する。

    URLの失効・削除・権限誤りなので、リトライしても回復しない。
    """

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Discord webhook unusable ({status_code}): {body}")


def _sanitize_title(title: str | None) -> str:
    """embedのtitleをDiscordが受け付ける形に整える。

    Discordはtitleが空文字でも256文字超でも400を返す。1件でも400になると
    キューの先頭が詰まって以降の通知が一切流れなくなるため、送る前に丸める。
    """
    text = (title or "").strip()
    if not text:
        return "(タイトル不明)"
    if len(text) > MAX_EMBED_TITLE_LENGTH:
        return text[: MAX_EMBED_TITLE_LENGTH - 1] + "…"
    return text


def _sanitize_image_url(image_url: str | None) -> str | None:
    """http(s)以外のURLはDiscordに拒否されるので落とす。"""
    if not image_url:
        return None
    if not image_url.startswith(("http://", "https://")):
        return None
    return image_url


def send_title_embed(webhook_url: str, title: str, image_url: str | None) -> None:
    """タイトル+画像のみのシンプルなembedを送信する（1件用）。

    Raises:
        RateLimited: Discordから429が返ってきた場合。
        InvalidRequest: Discordから400が返ってきた場合。
        WebhookUnusable: Webhook URLが失効している場合（401/403/404）。
        requests.RequestException: それ以外のHTTPエラーの場合。
    """
    send_title_embeds(webhook_url, [(title, image_url)])


def send_title_embeds(webhook_url: str, items: list[tuple[str, str | None]]) -> None:
    """複数件のタイトル+画像を1メッセージにまとめて送信する。

    1回の呼び出しで1メッセージだけ送る。Discordの1メッセージあたりのembed上限は
    `MAX_EMBEDS_PER_MESSAGE`個のため、呼び出し側でそれ以下に区切ってから呼ぶこと。
    上限超過は黙って切り捨てず例外にする（切り捨てると呼び出し側が「送信済み」と
    みなしてキューから消してしまい、通知が消失するため）。

    Raises:
        ValueError: itemsが`MAX_EMBEDS_PER_MESSAGE`件を超えている場合。
        RateLimited: Discordから429が返ってきた場合（このメッセージ分は未送信）。
        InvalidRequest: Discordから400が返ってきた場合。
        WebhookUnusable: Webhook URLが失効している場合（401/403/404）。
        requests.RequestException: それ以外のHTTPエラーの場合。
    """
    if not items:
        return
    if len(items) > MAX_EMBEDS_PER_MESSAGE:
        raise ValueError(
            f"1メッセージに送れるembedは{MAX_EMBEDS_PER_MESSAGE}件までです"
            f"（{len(items)}件渡されました）"
        )
    embeds = []
    for title, image_url in items:
        embed: dict = {"title": _sanitize_title(title)}
        sanitized_image = _sanitize_image_url(image_url)
        if sanitized_image:
            embed["image"] = {"url": sanitized_image}
        embeds.append(embed)
    _post(webhook_url, {"embeds": embeds})


def send_error_message(webhook_url: str, content: str) -> None:
    """エラー内容をテキストメッセージとして送信する。

    429の場合はここでも RateLimited が送出されうるので、
    呼び出し側は429時にこの関数自体の呼び出しを避けること
    （送信中に429を受けた直後にエラー通知を試みても大抵は無意味なため）。
    """
    if len(content) > MAX_CONTENT_LENGTH:
        content = content[: MAX_CONTENT_LENGTH - 1] + "…"
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
    if response.status_code == 400:
        raise InvalidRequest(response.text[:500])
    if response.status_code in (401, 403, 404):
        raise WebhookUnusable(response.status_code, response.text[:500])
    # raise_for_status()はレスポンス本文を含めないため、原因調査用に足しておく。
    if response.status_code >= 400:
        raise requests.HTTPError(
            f"Discord returned {response.status_code}: {response.text[:500]}",
            response=response,
        )
