"""Animephilia(animephilia.net)の配信カレンダーから新着タイトルを取得するクライアント。

JustWatchの`newTitles`インデックスが新着を検知できていない疑いがあるため、
1時間毎の新着チェックはこちらに置き換える。Animephiliaの「新着・配信予定
カレンダー」ページ（アニメに限らずNetflix/Prime Videoの全ジャンルを扱う方の
ページ）が使っているWordPress管理者向けajaxの内部エンドポイント
(`get_svod_calendar_events`)を直接叩く。

これは非公開の内部APIであり、サイトの実装が変わればいつ壊れてもおかしくない
（そのときは作り直す前提）。ページ本文をパースするより、このエンドポイントを
叩く方がサイト側のJS実装と同じ土俵に立てるため崩れにくいと判断した。

引数無しでこのAPIを呼ぶと、当日を含む直近1週間分のイベントが返る
（実機確認済み）。カレンダー記事側は「配信日が確定してから掲載」する運用の
ため、掲載日が配信日より数日後になることがある。そのため呼び出し側では
「配信日が今日かどうか」ではなく、この1週間分のレスポンスに含まれる
IDが既知かどうかで新着判定を行うこと。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

_BASE_URL = "https://animephilia.net"
_AJAX_URL = f"{_BASE_URL}/wp-admin/admin-ajax.php"

# 一時的なネットワーク断や5xxで「サイト構造が変わった」旨のエラー通知が飛ぶのは
# 誤報なので、諦める前に数回リトライする。
#
# GitHub Actionsのランナーからは、通常1秒程度で返るページが稀に丸ごと
# タイムアウトする（実測: 同じコードで成功する実行と全滅する実行がある。
# データセンタIPに対するサイト側の遮断と思われる）。リトライで待ち時間が
# 積み上がるので、1回あたりのタイムアウトは短めにして最悪値を抑える。
_REQUEST_TIMEOUT_SECONDS = 15
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (2, 5)

# 既定のUser-Agent(`python-httpx/x.y`)のままだと、WAFやCDNがデータセンタIPからの
# 明らかなスクリプトアクセスとして落とすことがある。このajaxはそもそもブラウザの
# ページ内から呼ばれる想定のエンドポイントなので、ブラウザと同じヘッダを付ける。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

_ARRIVAL_CALENDAR_PATH = {
    "netflix": "/netflix-arrival-calendar/",
    "prime_video": "/amazon-prime-video-arrival-calendar/",
}

# ページのインラインスクリプトに `ajax_calendar = {"url":"...","nonce":"..."}`
# という形で埋め込まれているWordPressのnonce。ajax呼び出しの度に必要。
_NONCE_PATTERN = re.compile(r'ajax_calendar\s*=\s*\{"url":"[^"]*","nonce":"([0-9a-f]+)"\}')


class AnimephiliaError(Exception):
    """Animephiliaからのカレンダー取得に失敗したときに送出する。"""


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    title: str
    start_date: str
    url: str | None
    image_url: str | None


def _strip_tracking_params(url: str) -> str:
    """Amazonのアフィリエイトタグ(`?tag=...`)などのクエリ文字列を取り除く。

    当サイトのアフィリエイトIDを自分たちの通知にそのまま埋め込みたくないため。
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _request_with_retry(description: str, send):
    """`send()`を最大`_MAX_ATTEMPTS`回試し、全滅したらAnimephiliaErrorにする。"""
    last_exc: httpx.HTTPError | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = send()
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                wait = _RETRY_BACKOFF_SECONDS[attempt]
                print(f"{description}に失敗しました（{exc}）。{wait}秒後に再試行します。")
                time.sleep(wait)
    raise AnimephiliaError(f"{description}に失敗しました: {last_exc}") from last_exc


def _fetch_nonce(provider_short_name: str) -> str:
    page_path = _ARRIVAL_CALENDAR_PATH[provider_short_name]
    response = _request_with_retry(
        "Animephiliaのページ取得",
        lambda: httpx.get(
            _BASE_URL + page_path,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=_HEADERS,
        ),
    )

    match = _NONCE_PATTERN.search(response.text)
    if not match:
        raise AnimephiliaError(
            "Animephiliaのページからnonceを取得できませんでした"
            "（サイトの構造が変わった可能性があります）"
        )
    return match.group(1)


def fetch_recent_events(provider_short_name: str) -> list[CalendarEvent]:
    """直近1週間分（当日含む）の新着イベントを取得する。

    provider_short_name: "netflix" または "prime_video"
    """
    page_path = _ARRIVAL_CALENDAR_PATH[provider_short_name]
    nonce = _fetch_nonce(provider_short_name)

    response = _request_with_retry(
        "Animephiliaのカレンダー取得",
        lambda: httpx.post(
            _AJAX_URL,
            data={
                "action": "get_svod_calendar_events",
                "service": provider_short_name,
                "type": "new",
                "genre": "all",
                "path": page_path,
                "nonce": nonce,
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
            # ajaxはカレンダーページ内から呼ばれる前提なのでRefererも合わせる。
            headers={**_HEADERS, "Referer": _BASE_URL + page_path},
        ),
    )

    try:
        payload = response.json()
    except ValueError as exc:
        raise AnimephiliaError(
            f"Animephiliaのレスポンスがカレンダー形式ではありません: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise AnimephiliaError(f"Animephiliaのレスポンス形式が想定と異なります: {payload}")

    events: list[CalendarEvent] = []
    for date, items in payload.items():
        if not isinstance(items, list):
            raise AnimephiliaError(f"Animephiliaのレスポンス形式が想定と異なります: {payload}")
        for item in items:
            if not isinstance(item, dict):
                raise AnimephiliaError(
                    f"Animephiliaのレスポンス形式が想定と異なります: {item}"
                )
            title = item.get("title")
            if not title:
                continue
            raw_url = item.get("url") or None
            url = _strip_tracking_params(raw_url) if raw_url else None
            # urlが確認されていないタイトルはidの一意性をtitle+dateで代用する。
            event_id = url or f"{provider_short_name}:{date}:{title}"
            events.append(
                CalendarEvent(
                    id=event_id,
                    title=title,
                    start_date=item.get("start", date),
                    url=url,
                    image_url=item.get("image") or None,
                )
            )
    return events
