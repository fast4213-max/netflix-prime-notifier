"""全ジャンル版の新着を、ジャンル別のDiscordチャンネルへ振り分ける。

設計は docs/DESIGN_GENRE.md。アニメは既存の通知（main.process_provider）が
担当するので、ここでは「アニメではないもの」だけを扱う。

振り分けは`config.json`の`genre.channels`を上から順に評価し、最初に条件を満たした
ものに決める。最後のチャンネル（条件なし）が受け皿なので、アニメ以外は必ずどれか
1つのチャンネルに入る（漏れない）。
"""

from __future__ import annotations

from animephilia_client import CalendarEvent


def is_anime(event: CalendarEvent, genre_cfg: dict) -> bool:
    """ジャンル表記にアニメを含むか。アニメ版ページとの突き合わせの保険。"""
    keywords = genre_cfg.get("anime_keywords", ["アニメ"])
    return any(k in g for g in event.genres for k in keywords)


def _matches(event: CalendarEvent, rule: dict, groups: dict[str, list[str]]) -> bool:
    if "kind" in rule and event.kind != rule["kind"]:
        return False
    if "region" in rule and event.region != rule["region"]:
        return False
    group = rule.get("genre_group")
    if group is not None:
        keywords = groups.get(group, [])
        if not any(k in g for g in event.genres for k in keywords):
            return False
    return True


def classify(event: CalendarEvent, genre_cfg: dict) -> str:
    """イベントの行き先チャンネルのキーを返す（受け皿があるので必ず返る）。"""
    groups = genre_cfg.get("keyword_groups", {})
    channels = genre_cfg["channels"]
    for rule in channels:
        if _matches(event, rule, groups):
            return rule["key"]
    # 設定の末尾に条件なしの受け皿が無い場合の保険。取りこぼさず末尾へ入れる。
    return channels[-1]["key"]


def webhook_env_name(genre_cfg: dict, provider_key: str, channel_key: str) -> str:
    """`DISCORD_WEBHOOK_{会社}_{ジャンル}`形式の環境変数名を返す。"""
    prefix = genre_cfg["provider_env_prefix"][provider_key]
    return f"DISCORD_WEBHOOK_{prefix}_{channel_key.upper()}"


def channel_label(genre_cfg: dict, channel_key: str) -> str:
    for rule in genre_cfg["channels"]:
        if rule["key"] == channel_key:
            return rule.get("label", channel_key)
    return channel_key
