"""NetflixとPrime Videoの両方で配信されているタイトルを突き合わせて通知する。

設計は docs/DESIGN_CROSS.md。

- 各プロバイダで観測したタイトルを`state/catalog_{provider}.json`に`window_days`日分
  溜める（通知の有無とは無関係に、毎時の取得結果をすべて登録する）
- 両方のカタログに同じ照合キーがあり、まだ通知していなければ専用チャンネルへ通知する
  （通知は1作品につき1回だけ。`state/cross_notified.json`で管理）
- 通知のリンクはNetflixを優先する（両方にあるときはNetflixで見てもらう）
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone

import state_manager
from animephilia_client import CalendarEvent, pick_image_url
from queue_runner import drain_queue
from webhook_config import resolve_webhook_url

CROSS_KEY = "cross"
PROVIDER_LABELS = {"netflix": "Netflix", "prime_video": "Prime Video"}

# 配信後は「(字幕版)」等の版表記が後ろに付くことがあるので、照合時は落とす。
_TRAILING_BRACKETS_RE = re.compile(r"(?:\([^()]*\))+$")
# 実データ（Netflix約160件×Prime約330件）で確認した表記ゆれ:
#   「テムパル〜…〜」と「テムパル～…～」（波ダッシュと全角チルダ）
#   「逃げ上手の若君」と「「逃げ上手の若君」」（Prime側だけ括弧で囲む）
_QUOTES_RE = re.compile(r"[「」『』【】\"“”]")
_SEPARATORS_RE = re.compile(r"[\s・:!?、,.。\-~]")


def match_key(title: str) -> str:
    """2社のタイトルを同一視するための照合キー（完全一致用）。"""
    text = unicodedata.normalize("NFKC", title).replace("〜", "~")
    text = _QUOTES_RE.sub("", text)
    stripped = _TRAILING_BRACKETS_RE.sub("", text)
    text = stripped or text
    return _SEPARATORS_RE.sub("", text).lower()


def _retention_days(window_days: int) -> int:
    # 初期投入は配信日の0時（JST）を登録日時にするので、境界日のエントリが
    # 時刻の端数で落ちないよう1日の余裕を持たせる。
    return window_days + 1


def _notified_retention_days(window_days: int) -> int:
    # 通知済みの記録はカタログより長く持つ。先に消えると、カタログに残っている
    # 作品が再び「未通知」に見えて二重通知になる。
    return _retention_days(window_days) + 90


def _release_iso(start_date: str) -> str:
    return (start_date or "")[:10]


def register_events(
    provider_key: str,
    events: list[CalendarEvent],
    *,
    window_days: int,
    seen_at: dict[str, str] | None = None,
) -> int:
    """取得したイベントをカタログへ登録して保存する。新規登録件数を返す。

    `seen_at`は初期投入用（照合キー→登録日時を配信日にする）。通常は現在時刻。
    すでにあるキーは初回観測日時を保ち、URL/画像が後から付いたら埋める。
    """
    catalog = state_manager.load_catalog(provider_key)
    now = state_manager.now_iso()
    added = 0
    for event in events:
        key = match_key(event.title)
        if not key:
            continue
        entry = catalog.get(key)
        if entry is None:
            catalog[key] = {
                "title": event.title,
                "release": _release_iso(event.start_date),
                "url": event.url,
                "image_url": event.image_url,
                "video_url": event.video_url,
                "seen_at": (seen_at or {}).get(key, now),
            }
            added += 1
            continue
        for field, value in (
            ("url", event.url),
            ("image_url", event.image_url),
            ("video_url", event.video_url),
        ):
            if not entry.get(field) and value:
                entry[field] = value
    state_manager.save_catalog(provider_key, state_manager.prune_catalog(catalog, _retention_days(window_days)))
    return added


def seed_seen_at(events: list[CalendarEvent]) -> dict[str, str]:
    """初期投入用: 配信日（日本時間の0時）を登録日時として扱う。"""
    jst = timezone(timedelta(hours=9))
    result: dict[str, str] = {}
    for event in events:
        try:
            released = datetime.fromisoformat(event.start_date[:10]).replace(tzinfo=jst)
        except ValueError:
            continue
        result[match_key(event.title)] = released.astimezone(timezone.utc).isoformat()
    return result


def _describe(netflix: dict, prime: dict) -> str:
    return (
        f"Netflix: {netflix.get('release') or '不明'} / "
        f"Prime Video: {prime.get('release') or '不明'}"
    )


def find_new_matches(window_days: int) -> list[dict]:
    """両方のカタログにあって未通知の作品を、キュー用の要素にして返す。

    通知済みの記録はここで更新・保存する（キューに積んだ時点で既読扱い。
    未送信分はキューに残り、次回実行で送られる）。
    """
    netflix = state_manager.prune_catalog(
        state_manager.load_catalog("netflix"), _retention_days(window_days)
    )
    prime = state_manager.prune_catalog(
        state_manager.load_catalog("prime_video"), _retention_days(window_days)
    )
    notified = state_manager.prune_by_age(
        state_manager.load_cross_notified(), _notified_retention_days(window_days)
    )
    now = state_manager.now_iso()

    items: list[dict] = []
    for key in sorted(set(netflix) & set(prime)):
        if key in notified:
            continue
        n, p = netflix[key], prime[key]
        # リンクはNetflix優先。Netflix側のURLがまだ無ければPrime Videoで代用する。
        url = n.get("url") or p.get("url")
        image = pick_image_url(n.get("image_url") or p.get("image_url"), n.get("video_url") or p.get("video_url"))
        items.append(
            {
                "id": f"{CROSS_KEY}:{key}",
                "title": n.get("title") or p.get("title"),
                "image_url": image,
                "url": url,
                "description": _describe(n, p),
                "detected_at": now,
            }
        )
        notified[key] = now
    state_manager.save_cross_notified(notified)
    return items


def process(config: dict) -> list[str]:
    """突き合わせてキューに積み、専用チャンネルへ送る。エラー文のリストを返す。"""
    cross_cfg = config.get(CROSS_KEY)
    if not cross_cfg:
        return []
    webhook_url = resolve_webhook_url(CROSS_KEY, cross_cfg)
    if not webhook_url:
        # Webhook未設定ならカタログだけ溜めて、通知済みにはしない
        # （設定した後にまとめて通知される）。
        return []

    queue = state_manager.load_queue(CROSS_KEY)
    # 先にキューへ積んでから通知済みを保存する。drain_queueはどの経路でも
    # キューを保存するので、積んだ分が通知済みだけ記録されて消えることはない。
    queue.extend(find_new_matches(cross_cfg.get("window_days", 30)))
    state_manager.save_queue(CROSS_KEY, queue)
    print(f"[{CROSS_KEY}] 重複配信の送信待ち: {len(queue)}件")
    return drain_queue(CROSS_KEY, webhook_url, queue, config)
