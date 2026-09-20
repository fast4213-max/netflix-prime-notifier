"""プロバイダ設定からDiscord Webhook URLを解決する共通処理。"""

from __future__ import annotations

import os


def resolve_webhook_url(provider_key: str, provider_cfg: dict) -> str | None:
    """環境変数からWebhook URLを取得する。未設定ならNoneを返し、その旨を表示する。"""
    webhook_url = os.environ.get(provider_cfg["webhook_env"])
    if not webhook_url:
        print(
            f"[{provider_key}] 環境変数 {provider_cfg['webhook_env']} が設定されていません。"
            "このプロバイダの処理をスキップします。"
        )
        return None
    return webhook_url
