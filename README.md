# netflix-prime-notifier

JustWatchの非公式APIを使い、NetflixとPrime Videoの新着タイトルをDiscordの
別チャンネルに通知するBotです。設計の詳細は [docs/DESIGN.md](docs/DESIGN.md) を参照してください。

- Netflix: タイトル+画像を通知
- Prime Video: 「追加課金なしで視聴できるもの」（Prime特典のflatrate、広告つき無料視聴）のみ通知。
  レンタル/購入のみのタイトルは通知しない
- 外部サービス cron-job.org から起動する構成（GitHubの`schedule:`は使わない）。2種類のジョブがある:
  - **6時間ごと**（`run-notify`）: JustWatchの「新着」インデックスとの差分だけを見る軽量チェック
  - **週次**（`weekly-catalog-check`）: プロバイダの全タイトルを取得し、6時間毎チェックの取りこぼしや
    「配信終了→再配信」を検知する全件チェック

## 既存運用からのアップグレード手順（重要）

このリポジトリは既に`run-notify`（旧: 1時間毎）で稼働中で、`state/*_seen.json`には
「新着として検知済みのID」のみが入っています（フルカタログ全件ではありません）。
今回`state/*_active.json`にリネームして引き継ぎましたが、**このままでは
週次チェック（`weekly-catalog-check`）を有効化した瞬間に、フルカタログに
存在するがまだ`active`に無い数千件のタイトルが一斉に「新規」扱いされて
大量誤通知が発生します。**

デプロイ後、`weekly-catalog-check`のcronジョブを有効化する前に、**必ずもう一度
`init-read`を手動実行**してください（新しい`init_read.py`はフルカタログ取得を
使うため、既存の`active`エントリはそのまま残しつつ、不足分のみを通知なしで
補完します）。実行後は`state/*_active.json`の件数が数千件規模になっているはずです
（Netflix約8,700件、Prime Video約14,000件が目安）。

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

`init-read`はプロバイダの全タイトルを取得するため数分かかります。
実行後、`state/*_active.json` に大量のIDが登録されていればOKです。

### 5. 試験通知で見た目を確認

同様に `event_type: "test-notify"` を1回実行し、両チャンネルに1件ずつ
テスト通知が届く（タイトル+画像）ことを確認してください。

### 6. cron-job.orgに本番ジョブを2つ登録

**6時間ごとの新着チェック（`run-notify`）**

| 設定項目 | 値 |
|---|---|
| URL | `https://api.github.com/repos/<owner>/<repo>/dispatches` |
| Method | POST |
| Headers | `Authorization: Bearer <PAT>` / `Accept: application/vnd.github+json` / `Content-Type: application/json` |
| Body | `{"event_type": "run-notify"}` |
| 実行間隔 | 6時間ごと |

**週次の全件チェック（`weekly-catalog-check`）**

| 設定項目 | 値 |
|---|---|
| URL | `https://api.github.com/repos/<owner>/<repo>/dispatches` |
| Method | POST |
| Headers | `Authorization: Bearer <PAT>` / `Accept: application/vnd.github+json` / `Content-Type: application/json` |
| Body | `{"event_type": "weekly-catalog-check"}` |
| 実行間隔 | 週1回（例: 毎週日曜 04:00 JST） |

初回既読化・テスト通知用のジョブは、普段はOFFにしておいてください。

## チューニング可能な設定値（`config.json`）

| 項目 | 説明 | 初期値 |
|---|---|---|
| `new_titles_fetch_count` | 6時間毎の実行でJustWatchから取得する新着候補数。100件/リクエストが上限（150以上は`page too large`で拒否）だが、`offset`によるページングに対応しているため`justwatch_client.py`内で自動的に複数リクエストに分割して合算する | 300 |
| `notify_limit_per_run` | 1回の実行あたりのDiscord送信上限（件数） | 150 |
| `send_interval_seconds` | Discordメッセージ送信の間隔（秒） | 0.5 |
| `discord_embeds_per_message` | 1メッセージにまとめるembed数の上限（Discordの上限は10） | 10 |
| `rate_limit_max_retries` | 429を受けたときに`retry_after`分待って同一実行内でリトライする最大回数。超えたら残りは次回実行に持ち越す | 3 |
| `providers.prime_video.allowed_monetization_types` | Prime Videoで通知対象とする課金種別 | `FLATRATE`, `FREE`, `ADS` |

## state管理の仕組み（`active_{provider}.json`）

- 「現在そのプロバイダに存在すると確認済みのタイトルID」を保持する
- 6時間毎チェック（新着インデックスとの差分）・週次チェック（全件との差分）の
  どちらも、ここに無いIDが見つかったら「新規（または再配信）」として通知しキューに積む
- 週次チェックだけが「前回はあったが今回は無いID」を検知して`active`から除外する。
  そのため「配信終了→再配信」の再通知は**週次チェックの実行間隔（約1週間）の粒度**でしか
  検知できない（同じ週内で消えて復活した場合は検知されない）

## トラブルシューティング

JustWatchは非公式APIのため、無告知でレスポンス形式が変わる可能性があります。
`scripts/debug_justwatch.py` をGitHub Actionsの `JustWatch API Debug` ワークフロー
（`workflow_dispatch`で手動実行）から実行すると、Netflix/Prime Videoの
プロバイダ短縮名や `newTitles` クエリの生レスポンスを直接確認できます。
