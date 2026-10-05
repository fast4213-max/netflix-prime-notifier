"""重複配信通知の導入用: 過去1か月分をカタログに投入し、該当を一度だけ通知する。

repository_dispatch (event_type=init-cross) から呼ばれる想定。
月別ページから過去`window_days`日分の配信を両プロバイダ分カタログへ入れ、
すでに両方で配信されている作品をまとめて専用チャンネルへ通知する。
通知済みは記録されるので、やり直しても二重通知にならない。
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timedelta, timezone

import cross_match
from animephilia_client import fetch_month_events
from main import load_config

_JST = timezone(timedelta(hours=9))


def _months(start, end) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def main() -> None:
    config = load_config()
    window_days = config["cross"].get("window_days", 30)
    today = datetime.now(_JST).date()
    since = today - timedelta(days=window_days)

    failed: list[str] = []
    for provider_key in config["providers"]:
        events = []
        try:
            for year, month in _months(since, today):
                events.extend(fetch_month_events(provider_key, year, month))
        except Exception as exc:  # noqa: BLE001 片方の失敗で全体を止めない
            print(f"[{provider_key}] Animephilia取得エラー: {exc}")
            traceback.print_exc()
            failed.append(provider_key)
            continue
        events = [e for e in events if e.start_date[:10] >= since.isoformat()]
        added = cross_match.register_events(
            provider_key,
            events,
            window_days=window_days,
            seen_at=cross_match.seed_seen_at(events),
        )
        print(f"[{provider_key}] 過去{window_days}日の{len(events)}件中、{added}件をカタログに登録しました。")

    if failed:
        # 片方のカタログが空のまま通知すると、一致が見つからず何も通知されない
        # まま「導入済み」に見えてしまう。やり直しが必要だと分かるようにする。
        sys.exit(f"取得に失敗したプロバイダ: {', '.join(failed)}。init-crossをやり直してください。")

    errors = cross_match.process(config)
    if errors:
        sys.exit("\n".join(errors))


if __name__ == "__main__":
    main()
