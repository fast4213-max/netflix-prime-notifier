# netflix-prime-notifier

JustWatchの非公式APIを使い、NetflixとPrime Videoの新着タイトルをDiscordの
別チャンネルに通知するBotです。設計の詳細は [docs/DESIGN.md](docs/DESIGN.md) を参照してください。

- Netflix: タイトル+画像を通知
- Prime Video: 「追加課金なしで視聴できるもの」（Prime特典のflatrate、広告つき無料視聴）のみ通知。
  レンタル/購入のみのタイトルは通知しない
- 外部サービス cron-job.org から1時間ごとに起動する構成（GitHubの`schedule:`は使わない）

## セットアップ手順

### 1. リポジトリのActions権限を確認

state更新をGitHub Actions自身がcommit & pushするため、書き込み権限が必要です。

1. リポジトリの **Settings**
2. **Actions → General**
3. 一番下の **Workflow permissions**
4. **Read and write permissions** を選択して保存

（依頼内容から対応済みとのことですが、初回実行前に念のため確認してください。）

### 2. Discord Webhook URLをGitHub Secretsに登録

Discordの各チャンネルで「連携サービスを編集」→「Webhookを作成」でURLを発行し、
リポジトリの **Settings → Secrets and variables → Actions** に以下の名前で登録します。

| Secret名 | 用途 |
|---|---|
| `DISCORD_WEBHOOK_NETFLIX` | Netflixチャンネルへの通知用Webhook URL |
| `DISCORD_WEBHOOK_PRIME` | Prime Videoチャンネルへの通知用Webhook URL |

未設定の場合、該当プロバイダの処理はスキップされ、ログにその旨が出力されます
（Discordへのエラー通知はWebhook自体が無いため送れません）。

### 3. GitHub PAT（cron-job.org用）を発行

cron-job.orgがGitHub APIの `repository_dispatch` を叩くための個人アクセストークンです。
**GitHub Secretsには登録しません**（cron-job.org側の設定にのみ使います）。

1. GitHubの **Settings → Developer settings → Personal access tokens (classic)**
2. スコープ `repo` を付与して発行

### 4. 初回既読化を実行（重要）

いきなり本番実行すると、既存の新着候補が全部「新着」扱いされて大量通知が飛びます。
そのため最初に1回だけ、通知なしで既読化するジョブを手動実行してください。

GitHubの **Actions** タブから `Notify` ワークフローを選択し、
`repository_dispatch` は手動実行できないため、以下のいずれかで起動します。

- cron-job.orgで一時的に `event_type: "init-read"` のジョブを作って1回だけ実行する
- または `curl` で直接叩く:

```bash
curl -X POST \
  -H "Authorization: Bearer <PAT>" \
  -H "Accept: application/vnd.github+json" \
  -H "Content-Type: application/json" \
  https://api.github.com/repos/<owner>/<repo>/dispatches \
  -d '{"event_type": "init-read"}'
```

実行後、`state/*_seen.json` に大量のIDが登録されていればOKです。

### 5. 試験通知で見た目を確認

同様に `event_type: "test-notify"` を1回実行し、両チャンネルに1件ずつ
テスト通知が届く（タイトル+画像）ことを確認してください。

### 6. cron-job.orgに本番ジョブを登録

| 設定項目 | 値 |
|---|---|
| URL | `https://api.github.com/repos/<owner>/<repo>/dispatches` |
| Method | POST |
| Headers | `Authorization: Bearer <PAT>` / `Accept: application/vnd.github+json` / `Content-Type: application/json` |
| Body | `{"event_type": "run-notify"}` |
| 実行間隔 | 1時間ごと |

初回既読化・テスト通知用のジョブは、普段はOFFにしておいてください。

## チューニング可能な設定値（`config.json`）

| 項目 | 説明 | 初期値 |
|---|---|---|
| `new_titles_fetch_count` | 1回の実行でJustWatchから取得する新着候補数。100件/リクエストが上限（150以上は`page too large`で拒否）だが、`offset`によるページングに対応しているため`justwatch_client.py`内で自動的に複数リクエストに分割して合算する | 300 |
| `init_fetch_count` | 初回既読化(init-read)で取得する件数（同じくページング対応） | 500 |
| `notify_limit_per_run` | 1回の実行あたりのDiscord送信上限 | 30 |
| `send_interval_seconds` | Discord送信の間隔（秒） | 0.5 |
| `seen_id_retention_days` | 既読IDを保持する日数 | 90 |
| `providers.prime_video.allowed_monetization_types` | Prime Videoで通知対象とする課金種別 | `FLATRATE`, `FREE`, `ADS` |

## トラブルシューティング

JustWatchは非公式APIのため、無告知でレスポンス形式が変わる可能性があります。
`scripts/debug_justwatch.py` をGitHub Actionsの `JustWatch API Debug` ワークフロー
（`workflow_dispatch`で手動実行）から実行すると、Netflix/Prime Videoの
プロバイダ短縮名や `newTitles` クエリの生レスポンスを直接確認できます。
