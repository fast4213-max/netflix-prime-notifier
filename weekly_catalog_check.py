"""週次実行: プロバイダの全タイトルを取得し、取りこぼしと再配信を検知する。

repository_dispatch (event_type=weekly-catalog-check) から呼ばれる想定。
6時間毎のnewTitles差分（main.py）だけでは以下を取りこぼす:

- JustWatchの「新着」インデックス自体に載らなかった/見逃した新着
- 一度配信終了して`active_*.json`から外れ、その後再配信されたタイトル
  （「同じ週の中で消えて復活」した場合は検知できない。週次チェックの
  実行間隔＝約1週間の粒度でしか消滅・復活を判定しないため）

このスクリプトは`active_{provider}.json`を正とし、今回取得した全件と
突き合わせて「今回新たに存在が確認できたID」を新規（または再配信）として
キューに積み、「前回はあったが今回は無いID」を`active`から外す。
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import state_manager
from justwatch_client import JustWatchError, fetch_full_catalog
from queue_runner import drain_queue, try_send_error
from webhook_config import resolve_webhook_url

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def process_provider(provider_key: str, provider_cfg: dict, config: dict) -> None:
    webhook_url = resolve_webhook_url(provider_key, provider_cfg)
    if not webhook_url:
        return

    active = state_manager.load_active(provider_key)
    queue = state_manager.load_queue(provider_key)

    try:
        candidates = fetch_full_catalog(
            provider_short_name=provider_cfg["short_name"],
            country=config["country"],
            language=config["language"],
            object_types=config["object_types"],
        )
    except JustWatchError as exc:
        print(f"[{provider_key}] JustWatch全件取得エラー: {exc}")
        try_send_error(
            webhook_url,
            f"⚠️ [{provider_key}] JustWatchからの週次全件取得に失敗しました: {exc}\n"
            "次回実行時に再試行します。",
        )
        drain_queue(provider_key, webhook_url, queue, config)
        return

    allowed_types = provider_cfg["allowed_monetization_types"]
    matched = [
        c for c in candidates if c.has_offer(provider_cfg["short_name"], allowed_types)
    ]
    current_ids = {c.id for c in matched}

    now = state_manager.now_iso()
    new_count = 0
    for entry in matched:
        if entry.id in active:
            active[entry.id] = now  # 生存確認のタイムスタンプ更新
            continue
        # activeに無い = 前回の週次チェック時点では存在しなかった
        # （真の新規、またはnewTitlesが取りこぼした新規、または一度消えて再配信）
        active[entry.id] = now
        queue.append(
            {
                "id": entry.id,
                "title": entry.title,
                "image_url": entry.poster_url,
                "detected_at": now,
            }
        )
        new_count += 1

    removed_ids = [entry_id for entry_id in active if entry_id not in current_ids]
    for entry_id in removed_ids:
        del active[entry_id]

    print(
        f"[{provider_key}] 全件{len(candidates)}件中、条件に合う{len(matched)}件を確認。"
        f"新規/再配信{new_count}件を検知、消滅{len(removed_ids)}件を除外。"
        f"送信待ちキュー: {len(queue)}件"
    )

    state_manager.save_active(provider_key, active)
    drain_queue(provider_key, webhook_url, queue, config)


def main() -> None:
    config = load_config()
    for provider_key, provider_cfg in config["providers"].items():
        try:
            process_provider(provider_key, provider_cfg, config)
        except Exception:  # noqa: BLE001 1プロバイダの想定外エラーで全体を止めない
            print(f"[{provider_key}] 想定外のエラーが発生しました:")
            traceback.print_exc()


if __name__ == "__main__":
    main()
