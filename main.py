"""本番実行: Animephiliaの配信カレンダーから新着を取得し、Discordの各チャンネルへ通知する。

repository_dispatch (event_type=run-notify) から呼ばれる想定。
1時間毎の実行で、Animephiliaのカレンダーが返す直近1週間分のイベントとの
差分のみを見る（JustWatch経由の方式は新着を検知できていなかったため廃止し、
animephilia_client経由のこの方式に一本化した。過去のカタログ全体との
突き合わせは行わず、今後の配信のみを対象とする）。

エラーは発生の都度send せず、実行の最後に1通へまとめて各チャンネルへ送る。
同じエラーが続く間は`state_manager.ERROR_COOLDOWN_HOURS`時間に1回までに
間引く（毎時実行なので、素直に送ると1日24通×2チャンネル飛んでしまうため）。
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import state_manager
from animephilia_client import AnimephiliaError, fetch_recent_events
from queue_runner import drain_queue, try_send_error
from webhook_config import resolve_webhook_url

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)



# animephilia_client.pyが実際にサイトの中身（nonce/レスポンス形式）を見て
# 「構造が変わった」と判断できたときだけ使う文言。process_provider側の
# エラー文はどれも「Animephiliaからの新着取得に失敗しました: {内訳}」という
# 同じ枕詞で始まるため、枕詞では判別できない。内訳側にこれらの文言が
# 含まれるかどうかで、タイムアウト等の接続層の一時的な不調と区別する。
_STRUCTURE_ERROR_MARKERS = (
    "構造が変わった",
    "レスポンス形式が想定と異なります",
    "レスポンスがカレンダー形式ではありません",
)


def broadcast_errors(config: dict, errors: list[str]) -> None:
    """実行中に溜まったエラーを1通にまとめ、両方のチャンネルへ送る。

    Animephiliaのサイト構造が変わった等、片方だけの問題では済まない可能性が
    ある異常は、通知チャンネルを一方しか見ていない人が気づけないことが無いよう
    両方のチャンネルに送る。ただし同一原因のエラーはクールダウン中なら送らない。
    """
    if not errors:
        return

    error_log = state_manager.load_error_log()
    now = state_manager.now_iso()
    fresh = [e for e in errors if state_manager.should_notify_error(error_log, e)]

    if not fresh:
        print(f"エラー{len(errors)}件はクールダウン中のため通知を省略しました。")
        return

    # 1件でもnonce取得失敗やレスポンス形式異常（＝実際にサイトの中身が
    # 変わった疑いが強いもの）が混ざっていれば構造変化を疑う文言にする。
    # 該当が無ければ、残るのはタイムアウト/5xx等の接続層の一時的な不調
    # なので、構造変化を疑わせる強い文言は避ける。
    if not any(marker in e for e in fresh for marker in _STRUCTURE_ERROR_MARKERS):
        hint = (
            "Animephiliaへの接続に失敗しました。サイトの構造が変わったのではなく、"
            "一時的なネットワーク不調の可能性が高いです。通常は次回実行で自動復旧します。"
            "何時間も続く場合はサイトの構造変化を疑ってください。"
        )
    else:
        hint = (
            "サイトの構造が変わった可能性があります。GitHub Actionsの実行ログ"
            "（Notifyワークフロー）にエラー詳細を残してあるので確認してください。"
        )

    body = "\n".join(f"・{e}" for e in fresh)
    message = f"⚠️ 新着チェックでエラーが発生しました。\n{body}\n{hint}\n次回実行時に再試行します。"

    for provider_key, provider_cfg in config["providers"].items():
        webhook_url = resolve_webhook_url(provider_key, provider_cfg)
        if webhook_url:
            try_send_error(webhook_url, message)

    for error in fresh:
        error_log[error] = now
    state_manager.save_error_log(error_log)


def process_provider(provider_key: str, provider_cfg: dict, config: dict) -> list[str]:
    """1プロバイダ分の新着チェックと送信を行い、エラーの説明文リストを返す。"""
    webhook_url = resolve_webhook_url(provider_key, provider_cfg)
    if not webhook_url:
        return []

    active = state_manager.load_active(provider_key)
    queue = state_manager.load_queue(provider_key)

    try:
        candidates = fetch_recent_events(provider_key)
    except AnimephiliaError as exc:
        print(f"[{provider_key}] Animephilia取得エラー: {exc}")
        traceback.print_exc()
        # 取得失敗時もキューの続きだけは送っておく（新規追加は無し）
        errors = drain_queue(provider_key, webhook_url, queue, config)
        return [f"[{provider_key}] Animephiliaからの新着取得に失敗しました: {exc}"] + errors

    now = state_manager.now_iso()
    new_count = 0
    for entry in candidates:
        if entry.id in active:
            continue
        active[entry.id] = now
        queue.append(
            {
                "id": entry.id,
                "title": entry.title,
                "image_url": entry.image_url,
                "detected_at": now,
            }
        )
        new_count += 1

    print(
        f"[{provider_key}] 候補{len(candidates)}件中、新着{new_count}件を検知。"
        f"送信待ちキュー: {len(queue)}件"
    )

    # 保存前に古い記録を落とす。カレンダーが返すのは直近1週間分なので、
    # 保持期間を超えたIDが再び新着扱いされることはない。
    pruned = state_manager.prune_active(active)
    if len(pruned) != len(active):
        print(
            f"[{provider_key}] {len(active) - len(pruned)}件の古い既読記録を整理しました"
            f"（残り{len(pruned)}件）。"
        )
    state_manager.save_active(provider_key, pruned)
    return drain_queue(provider_key, webhook_url, queue, config)


def main() -> None:
    config = load_config()
    errors: list[str] = []
    for provider_key, provider_cfg in config["providers"].items():
        try:
            errors.extend(process_provider(provider_key, provider_cfg, config))
        except Exception as exc:  # noqa: BLE001 1プロバイダの想定外エラーで全体を止めない
            print(f"[{provider_key}] 想定外のエラーが発生しました:")
            traceback.print_exc()
            errors.append(f"[{provider_key}] 想定外のエラーが発生しました: {exc}")

    broadcast_errors(config, errors)


if __name__ == "__main__":
    main()
