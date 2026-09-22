"""state/*.json の読み書きを担当するモジュール。

active_*.json : {id: 検知日時(ISO8601)} - Animephiliaのカレンダーで既に通知済み
                 （または初回既読化済み）のタイトルID一覧。ここに無いIDが
                 新着候補として現れたら「新規」として通知する。
queue_*.json  : [{id, title, image_url, detected_at}, ...] - 未送信のFIFOキュー
errors.json   : {エラー種別キー: {streak, last_seen, last_notified}} - 一時的な
                 不調で毎回通知が飛ばないよう、「何回連続で同じ種別のエラーが
                 起きたか」と「最後に通知した日時」を記録する。エラー文言には
                 具体的な例外メッセージ（timed out / HTTP 503 等）が含まれ、
                 同じ原因でも実行ごとに文言が揺れるため、キーには例外メッセージを
                 落とした「種別キー」（`error_stream_key`）を使う。
                 今回の実行で起きなかった種別は連続が途切れたものとして
                 丸ごと捨てるので、ファイルが際限なく膨らむこともない
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path(__file__).parent / "state"

# Animephiliaのカレンダーが返すのは直近1週間分だけなので、それより十分に古い
# エントリはもう候補として現れない。残しておいても再通知の抑止には効かず、
# 毎時間コミットされるstateが際限なく膨らむだけなので捨てる。
ACTIVE_RETENTION_DAYS = 90

# 同じ原因のエラーを何時間おきに再通知するか（毎時実行で毎回飛ぶのを防ぐ）。
ERROR_COOLDOWN_HOURS = 6

# 何回連続で同じ種別のエラーが起きたら通知するか。Animephiliaへの接続は
# タイムアウトで単発で失敗することがあり、その多くは次の実行で自動復旧する。
# 1回目から通知すると「次回実行時に再試行します」という自己解決する内容の
# 通知だけが鳴り続けるため、連続して失敗し「本当に直っていない」と分かって
# から初めて通知する（毎時実行なので3回＝約3時間続いた場合）。
ERROR_STREAK_THRESHOLD = 3

# 種別キーを作るための正規表現。
# 例外メッセージは`「枕詞」: 「内訳」`の形で連結されているので、最初の": "より
# 後ろを落とすと「timed out」「HTTP 503」等の揺れる部分が消え、同じ原因の
# エラーが実行をまたいで同じキーに収まる。残った部分に混じる件数などの数字も
# 揺れるためまとめてNに潰す。
_DETAIL_RE = re.compile(r": .*", re.DOTALL)
_NUMBER_RE = re.compile(r"\d+")


def _active_path(provider: str) -> Path:
    return STATE_DIR / f"{provider}_active.json"


def _queue_path(provider: str) -> Path:
    return STATE_DIR / f"{provider}_queue.json"


def _errors_path() -> Path:
    return STATE_DIR / "errors.json"


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as exc:
        # stateが壊れていても実行自体は続ける（最悪、再通知が出るだけで済む）。
        print(f"stateファイルの読み込みに失敗したため初期値で続行します: {path} ({exc})")
        return default


def _save_json(path: Path, data) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # 書き込み中にジョブが落ちてもJSONが壊れないよう、一時ファイル経由で置き換える。
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp_path.replace(path)


def load_active(provider: str) -> dict[str, str]:
    data = _load_json(_active_path(provider), {})
    return data if isinstance(data, dict) else {}


def save_active(provider: str, active: dict[str, str]) -> None:
    _save_json(_active_path(provider), active)


def _prune_by_age(data: dict[str, str], retention_days: int) -> dict[str, str]:
    """値がISO8601日時の辞書から、`retention_days`より古いエントリを落とす。

    日時が読めないものは判断できないので、安全側に倒して残す。
    """
    threshold = datetime.now(timezone.utc) - timedelta(days=retention_days)
    pruned: dict[str, str] = {}
    for key, value in data.items():
        try:
            seen = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            pruned[key] = value
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if seen >= threshold:
            pruned[key] = value
    return pruned


def prune_active(active: dict[str, str]) -> dict[str, str]:
    """`ACTIVE_RETENTION_DAYS`より古い記録を落とした新しい辞書を返す。"""
    return _prune_by_age(active, ACTIVE_RETENTION_DAYS)


def load_queue(provider: str) -> list[dict]:
    data = _load_json(_queue_path(provider), [])
    return data if isinstance(data, list) else []


def save_queue(provider: str, queue: list[dict]) -> None:
    _save_json(_queue_path(provider), queue)


def error_stream_key(signature: str) -> str:
    """エラー文言から、実行をまたいで同一視するための「種別キー」を作る。

    `[netflix] Animephiliaからの新着取得に失敗しました: ... timed out` と
    `[netflix] Animephiliaからの新着取得に失敗しました: ... HTTP 503` は
    人から見れば「同じ不調が続いている」ので、同じキーに寄せる。
    """
    head = _DETAIL_RE.sub("", signature, count=1)
    return _NUMBER_RE.sub("N", head).strip()


def _normalize_entry(value) -> dict | None:
    """errors.jsonの1エントリを現行フォーマットの辞書に揃える。

    値が日時文字列だけの旧フォーマット（{署名: 最終通知日時}）でも、
    「通知済み」の事実は引き継ぎたいのでクールダウン記録として読み込む。
    ただし連続回数は旧フォーマットには無いので0から数え直す。ここで
    しきい値を入れてしまうと、旧フォーマットが残った状態で1回失敗しただけで
    「3回連続」扱いの通知が飛んでしまう。
    """
    if isinstance(value, str):
        return {"streak": 0, "last_seen": value, "last_notified": value}
    if not isinstance(value, dict):
        return None
    streak = value.get("streak")
    if not isinstance(streak, int) or streak < 0:
        streak = 0
    last_seen = value.get("last_seen")
    last_notified = value.get("last_notified")
    return {
        "streak": streak,
        "last_seen": last_seen if isinstance(last_seen, str) else None,
        "last_notified": last_notified if isinstance(last_notified, str) else None,
    }


def load_error_log() -> dict[str, dict]:
    data = _load_json(_errors_path(), {})
    if not isinstance(data, dict):
        return {}
    error_log: dict[str, dict] = {}
    for key, value in data.items():
        entry = _normalize_entry(value)
        if entry is None:
            continue
        # 旧フォーマットは具体的な例外メッセージ入りの署名がキーなので、
        # 種別キーへ寄せ直す。衝突したら新しい記録の方を残す。
        stream_key = error_stream_key(key)
        current = error_log.get(stream_key)
        if current is None or _is_newer(entry.get("last_seen"), current.get("last_seen")):
            error_log[stream_key] = entry
    return error_log


def _is_newer(candidate: str | None, current: str | None) -> bool:
    if candidate is None:
        return False
    if current is None:
        return True
    try:
        return datetime.fromisoformat(candidate) > datetime.fromisoformat(current)
    except (TypeError, ValueError):
        return False


def save_error_log(error_log: dict[str, dict]) -> None:
    _save_json(_errors_path(), error_log)


def record_error_run(error_log: dict[str, dict], errors: list[str]) -> dict[str, dict]:
    """今回の実行で起きたエラーを反映した、新しいエラーログを返す。

    - 今回も起きた種別は連続回数（streak）を+1する
    - 今回起きなかった種別は連続が途切れたので記録ごと捨てる
      （次に起きたときは改めて1回目から数え直す）
    """
    now = now_iso()
    updated: dict[str, dict] = {}
    for signature in errors:
        stream_key = error_stream_key(signature)
        if stream_key in updated:
            # 同じ実行内で同種のエラーが複数出ても、連続回数は1回分だけ進める。
            continue
        previous = error_log.get(stream_key) or {}
        updated[stream_key] = {
            "streak": int(previous.get("streak") or 0) + 1,
            "last_seen": now,
            "last_notified": previous.get("last_notified"),
        }
    return updated


def error_streak(error_log: dict[str, dict], signature: str) -> int:
    """そのエラーが今回を含めて何回連続で起きているかを返す。"""
    entry = error_log.get(error_stream_key(signature)) or {}
    return int(entry.get("streak") or 0)


def should_notify_error(error_log: dict[str, dict], signature: str) -> bool:
    """通知すべきエラーかを返す。

    `ERROR_STREAK_THRESHOLD`回連続で起きるまでは通知しない（単発の
    タイムアウトは次回実行で直ることが多く、通知する意味が薄いため）。
    連続回数を満たしていても、直近`ERROR_COOLDOWN_HOURS`時間以内に
    通知済みなら通知しない。
    """
    if error_streak(error_log, signature) < ERROR_STREAK_THRESHOLD:
        return False
    entry = error_log.get(error_stream_key(signature)) or {}
    last = entry.get("last_notified")
    if not last:
        return True
    try:
        last_at = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_at >= timedelta(hours=ERROR_COOLDOWN_HOURS)


def mark_errors_notified(error_log: dict[str, dict], errors: list[str], now: str) -> None:
    """通知できたエラーにクールダウン開始時刻を記録する（その場で書き換える）。"""
    for signature in errors:
        entry = error_log.get(error_stream_key(signature))
        if entry is not None:
            entry["last_notified"] = now


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
