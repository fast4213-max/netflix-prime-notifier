"""初回セットアップ用: 直近の配信分を「既存」として一括登録する（通知は送らない）。

repository_dispatch (event_type=init-read) から呼ばれる想定。
本番運用(main.py)を開始する前に一度だけ実行し、Animephiliaのカレンダーが
返す直近1週間分がいきなり全部「新着」扱いされて一斉通知が飛ぶのを防ぐ。

過去のカタログ全体を既読化する必要は無い（今後の配信だけを検知できれば良い
ため）。直近1週間分だけを対象にすれば十分。
"""

from __future__ import annotations

import state_manager
from animephilia_client import AnimephiliaError, fetch_recent_events


def main() -> None:
    for provider_key in ("netflix", "prime_video"):
        try:
            recent = fetch_recent_events(provider_key)
        except AnimephiliaError as exc:
            print(f"[{provider_key}] Animephilia取得エラー: {exc}")
            continue

        active = state_manager.load_active(provider_key)
        now = state_manager.now_iso()
        for entry in recent:
            active.setdefault(entry.id, now)
        state_manager.save_active(provider_key, active)
        print(f"[{provider_key}] 直近{len(recent)}件を既存登録しました（通知なし）。")


if __name__ == "__main__":
    main()
