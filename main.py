"""本番実行: Animephiliaの配信カレンダーから新着を取得し、Discordの各チャンネルへ通知する。

repository_dispatch (event_type=run-notify) から呼ばれる想定。
1時間毎の実行で、Animephiliaのカレンダーが返す直近1週間分のイベントとの
差分のみを見る（JustWatch経由の方式は新着を検知できていなかったため廃止し、
animephilia_client経由のこの方式に一本化した。過去のカタログ全体との
突き合わせは行わず、今後の配信のみを対象とする）。

エラーは発生の都度send せず、実行の最後に1通へまとめて各チャンネルへ送る。
さらに、`state_manager.ERROR_STREAK_THRESHOLD`回連続で同じ種別のエラーが
起きるまでは通知しない（単発のタイムアウトは次回実行で直ることが多く、
その都度通知すると「次回実行時に再試行します」という自己解決する通知だけが
鳴り続けるため）。通知した後も、同じエラーが続く間は
`state_manager.ERROR_COOLDOWN_HOURS`時間に1回までに間引く
（毎時実行なので、素直に送ると1日24通×2チャンネル飛んでしまうため）。
"""

from __future__ import annotations

import json
import re
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cross_match
import state_manager
from animephilia_client import AnimephiliaError, fetch_recent_events, resolve_image_url
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

# Animephiliaからの取得失敗を表すエラー文の枕詞（process_providerが付ける）。
# 上の「構造変化」「接続不調」のヒントはこの種のエラーにだけ当てはまる。
# Discord側の失敗（Webhook失効など）に「Animephiliaへの接続に失敗しました」と
# 出すと、見当違いの場所を調べさせてしまう。
_FETCH_ERROR_MARKER = "Animephiliaからの新着取得に失敗しました"

# 1回きりで取り返しがつかない（その件の通知が失われる）エラーの目印。
# 次の実行で同じエラーが再発することはないので、連続回数のしきい値を
# 待っていると永久に通知されない。クールダウンも掛けず、起きたら即通知する。
_IMMEDIATE_ERROR_MARKERS = ("通知を諦めました",)


def _is_immediate_error(error: str) -> bool:
    return any(marker in error for marker in _IMMEDIATE_ERROR_MARKERS)


def broadcast_errors(config: dict, errors: list[str]) -> None:
    """実行中に溜まったエラーを1通にまとめ、両方のチャンネルへ送る。

    Animephiliaのサイト構造が変わった等、片方だけの問題では済まない可能性が
    ある異常は、通知チャンネルを一方しか見ていない人が気づけないことが無いよう
    両方のチャンネルに送る。ただし連続回数が`ERROR_STREAK_THRESHOLD`に満たない
    エラーと、クールダウン中のエラーは送らない（Discordに拒否されて1件の通知を
    諦めた場合など、再発しない一度きりのエラーは例外として即時に送る）。
    """
    # 今回の実行結果を反映する。今回起きなかった種別は連続が途切れたものとして
    # ここで捨てられるので、エラーが0件でも必ず保存する（そうしないと
    # 「連続5回目」のまま記録が残り、次に1回だけ起きたときに即通知になる）。
    error_log = state_manager.record_error_run(state_manager.load_error_log(), errors)

    if not errors:
        state_manager.save_error_log(error_log)
        return

    now = state_manager.now_iso()
    fresh = [
        e
        for e in errors
        if _is_immediate_error(e) or state_manager.should_notify_error(error_log, e)
    ]

    if not fresh:
        streaks = ", ".join(
            f"{state_manager.error_streak(error_log, e)}回目" for e in errors
        )
        print(
            f"エラー{len(errors)}件は通知条件を満たさないため通知を省略しました"
            f"（連続{streaks} / 通知は{state_manager.ERROR_STREAK_THRESHOLD}回連続から、"
            f"通知済みなら{state_manager.ERROR_COOLDOWN_HOURS}時間のクールダウン）。"
        )
        state_manager.save_error_log(error_log)
        return

    persistent = [e for e in fresh if not _is_immediate_error(e)]
    fetch_errors = [e for e in persistent if _FETCH_ERROR_MARKER in e]

    hints: list[str] = []
    if fetch_errors:
        # 1件でもnonce取得失敗やレスポンス形式異常（＝実際にサイトの中身が
        # 変わった疑いが強いもの）が混ざっていれば構造変化を疑う文言にする。
        # 該当が無ければ、残るのはタイムアウト/5xx等の接続層の一時的な不調
        # なので、構造変化を疑わせる強い文言は避ける。
        if any(marker in e for e in fetch_errors for marker in _STRUCTURE_ERROR_MARKERS):
            hints.append(
                "サイトの構造が変わった可能性があります。GitHub Actionsの実行ログ"
                "（Notifyワークフロー）にエラー詳細を残してあるので確認してください。"
            )
        else:
            hints.append(
                "Animephiliaへの接続に失敗しました。サイトの構造が変わったのではなく、"
                "ネットワーク不調の可能性が高いです。何時間も続く場合はサイトの"
                "構造変化を疑ってください。"
            )
    if len(fetch_errors) < len(fresh):
        hints.append(
            "詳細はGitHub Actionsの実行ログ（Notifyワークフロー）を確認してください。"
        )

    def describe(e: str) -> str:
        if _is_immediate_error(e):
            return f"・{e}"
        return f"・{e}（{state_manager.error_streak(error_log, e)}回連続）"

    body = "\n".join(describe(e) for e in fresh)
    if persistent:
        # 見出しの回数は実際の連続回数（通知後もクールダウン明けに再通知されるため、
        # しきい値の3回とは限らない）。
        streak = max(state_manager.error_streak(error_log, e) for e in persistent)
        headline = f"⚠️ 新着チェックが{streak}回連続で失敗しました。"
        footer = "次回実行時に再試行します。"
    else:
        # 即時通知のエラーだけのときは「連続で失敗」でも「再試行」でもない。
        headline = "⚠️ 新着の通知中にエラーが発生しました。"
        footer = ""
    message = "\n".join(part for part in (headline, body, *hints, footer) if part)

    delivered = False
    for provider_key, provider_cfg in config["providers"].items():
        webhook_url = resolve_webhook_url(provider_key, provider_cfg)
        if webhook_url and try_send_error(webhook_url, message):
            delivered = True

    # 1通も届いていないのに記録すると、`should_notify_error`が
    # ERROR_COOLDOWN_HOURS時間ぶん「通知済み」と誤判定して沈黙する。
    # Webhook未設定やDiscord側の障害で送れなかっただけなので、
    # 記録せずに次回実行で送り直す。
    if not delivered:
        print(
            "エラー通知をどのチャンネルにも届けられませんでした。"
            "クールダウンには記録せず、次回実行で再送します。"
        )
        state_manager.save_error_log(error_log)
        return

    state_manager.mark_errors_notified(error_log, fresh, now)
    state_manager.save_error_log(error_log)


# 配信前のタイトルはAmazon等のURLがまだ無く、`{provider}:{日付}:{タイトル}`の
# 仮IDで既読登録される（animephilia_client参照）。配信が始まってURLが付くとIDが
# 変わり、同じタイトルが新着として二度通知されてしまう。また配信日が延期されると
# 仮IDの日付部分が変わって、やはり二重通知になる。そこで、仮IDで既読登録済みの
# タイトルと同じタイトルが現れたら通知せず既読にだけする。
# 何か月も後に同名の別作品が来たときまで握りつぶさないよう、期間を区切る。
_PROVISIONAL_MATCH_DAYS = 30

# 配信後は「(字幕版)」等の版表記が後ろに付くことがあるので、照合時は落とす。
_TRAILING_BRACKETS_RE = re.compile(r"(?:\([^()]*\))+$")


def _normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title)
    text = re.sub(r"\s+", "", text)
    stripped = _TRAILING_BRACKETS_RE.sub("", text)
    # 括弧だけのタイトルを空にしてしまわないようにする。
    return stripped or text


def _provisional_titles(provider_key: str, active: dict[str, str]) -> set[str]:
    """仮IDで最近既読登録されたタイトル（正規化済み）の集合を返す。"""
    threshold = datetime.now(timezone.utc) - timedelta(days=_PROVISIONAL_MATCH_DAYS)
    prefix = f"{provider_key}:"
    titles: set[str] = set()
    for key, seen_at in active.items():
        if not key.startswith(prefix):
            continue
        parts = key.split(":", 2)
        if len(parts) != 3:
            continue
        try:
            seen = datetime.fromisoformat(seen_at)
        except (TypeError, ValueError):
            seen = None
        if seen is not None:
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if seen < threshold:
                continue
        titles.add(_normalize_title(parts[2]))
    return titles


# Animephiliaのカレンダー記事は配信日だけ決まった段階で掲載し、日時が
# タイムゾーン無しの日付だけのことがある。記事は日本向けなので日本時間で解釈する。
_JST = timezone(timedelta(hours=9))

# 配信開始から画像が付くまで少し掛かることがあるので、配信日時を過ぎてもこの期間は
# 画像を待つ。カレンダーは配信後も1週間ほど載り続けるので、この範囲なら取りこぼさない。
_IMAGE_WAIT_AFTER_RELEASE = timedelta(days=2)


def _release_at(start_date: str) -> datetime | None:
    """カレンダーの配信日時（`2026-09-27`や`2026-09-27T23:30:00+09:00`）を返す。"""
    try:
        released = datetime.fromisoformat(start_date)
    except (TypeError, ValueError):
        return None
    if released.tzinfo is None:
        released = released.replace(tzinfo=_JST)
    return released


def _should_wait_for_image(entry) -> bool:
    """ポスター画像がまだ無く、付くのを待つべきタイトルかを返す。

    配信前のタイトルは画像が無く、予告編のYouTube動画だけが載っていることが多い。
    そのまま通知するとYouTubeのサムネイルになり、配信後に画像が付いても
    重複防止のため通知し直されない。そこで画像が付くまでは既読にせず毎回
    見直す。ただし配信日時から`_IMAGE_WAIT_AFTER_RELEASE`を過ぎても画像が
    無いままなら、通知を取りこぼさないようYouTubeのサムネイル（無ければ画像なし）
    で通知する。
    """
    if entry.image_url:
        return False
    released = _release_at(entry.start_date)
    if released is None:
        return False
    return datetime.now(timezone.utc) < released + _IMAGE_WAIT_AFTER_RELEASE


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

    # 重複配信の突き合わせ用に、見えたタイトルは通知の有無と無関係に全部記録する。
    cross_match.register_events(
        provider_key,
        candidates,
        window_days=config.get("cross", {}).get("window_days", 30),
    )

    now = state_manager.now_iso()
    provisional_titles = _provisional_titles(provider_key, active)
    queued_titles: set[str] = set()
    # 画像付きで載っているタイトル。画像待ちのタイトルと同じものが画像付きでも
    # 載っていれば、そちらで通知される（または通知済み）ので待つ必要が無い。
    titles_with_image = {_normalize_title(e.title) for e in candidates if e.image_url}
    new_count = 0
    waiting_count = 0
    for entry in candidates:
        if entry.id in active:
            continue
        normalized = _normalize_title(entry.title)
        if not entry.image_url and normalized in titles_with_image:
            active[entry.id] = now
            print(
                f"[{provider_key}] 画像付きの同じタイトルがあるためスキップ: {entry.title!r}"
            )
            continue
        if _should_wait_for_image(entry):
            # 既読にしないので、次回以降の実行で画像が付いていれば通知される。
            waiting_count += 1
            print(
                f"[{provider_key}] 画像が付くまで通知を保留: {entry.title!r}"
                f"（配信日時 {entry.start_date}）"
            )
            continue
        active[entry.id] = now
        if normalized in provisional_titles:
            print(
                f"[{provider_key}] 配信前に通知済みのタイトルのためスキップ: {entry.title!r}"
            )
            continue
        # 同じ実行内で仮IDとURL付きの両方が返ってきた場合も1回だけにする。
        if entry.url is None:
            if normalized in queued_titles:
                print(
                    f"[{provider_key}] 同じタイトルを今回通知するためスキップ: {entry.title!r}"
                )
                continue
            provisional_titles.add(normalized)
        queued_titles.add(normalized)
        queue.append(
            {
                "id": entry.id,
                "title": entry.title,
                "image_url": resolve_image_url(entry),
                "url": entry.url,
                "detected_at": now,
            }
        )
        new_count += 1

    print(
        f"[{provider_key}] 候補{len(candidates)}件中、新着{new_count}件を検知"
        f"（画像待ちで保留{waiting_count}件）。送信待ちキュー: {len(queue)}件"
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

    # 両プロバイダの取得が済んだ後で、Netflix×Prime Videoの重複配信を突き合わせる。
    try:
        errors.extend(cross_match.process(config))
    except Exception as exc:  # noqa: BLE001 重複通知の失敗で新着通知のエラー集約まで止めない
        print("[cross] 想定外のエラーが発生しました:")
        traceback.print_exc()
        errors.append(f"[cross] 想定外のエラーが発生しました: {exc}")

    broadcast_errors(config, errors)


if __name__ == "__main__":
    main()
