# netflix-prime-notifier

NetflixとPrime Videoの新着タイトルをDiscordの別チャンネルに通知するBotです。
設計の詳細は [docs/DESIGN.md](docs/DESIGN.md) を参照してください。

- Netflix: タイトル+画像を通知
- Prime Video: [Animephilia](https://animephilia.net/)のカレンダー自体が
  「プライム会員特典（見放題）」のみを掲載しているため、レンタル/購入のみの
  タイトルは自然と対象外になる
- データソースはAnimephiliaの配信カレンダー記事が使っている非公開ajax
  エンドポイントを直接叩く方式（`animephilia_client.py`）。過去にJustWatchの
  非公式APIを使っていたが、新着を検知できていなかったため廃止しこちらに一本化した
  （非公式・非公開のAPIのため、サイトの実装が変われば壊れる前提。壊れたら
  作り直す運用とする）
- 過去のカタログ全体との突き合わせは行わない。**今後配信されるものだけ**を
  対象とし、6時間毎に直近1週間分のカレンダーとの差分だけを見る
- 外部サービス cron-job.org から起動する構成（GitHubの`schedule:`は使わない）。
  ジョブは`run-notify`（6時間ごと）のみ

## セットアップ手順

### 1. リポジトリのActions権限を確認

state更新をGitHub Actions自身がcommit & pushするため、書き込み権限が必要です。

1. リポジトリの **Settings**
2. **Actions → General**
3. 一番下の **Workflow permissions**
4. **Read and write permissions** を選択して保存

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

いきなり本番実行すると、直近1週間分の配信が全部「新着」扱いされて一斉通知が飛びます。
そのため最初に1回だけ、通知なしで既読化するジョブを手動実行してください。

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

実行後、`state/*_active.json` に直近1週間分のIDが登録されていればOKです。

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
| 実行間隔 | 6時間ごと |

初回既読化・テスト通知用のジョブは、普段はOFFにしておいてください。

## チューニング可能な設定値（`config.json`）

| 項目 | 説明 | 初期値 |
|---|---|---|
| `notify_limit_per_run` | 1回の実行あたりのDiscord送信上限（件数） | 150 |
| `send_interval_seconds` | Discordメッセージ送信の間隔（秒） | 0.5 |
| `discord_embeds_per_message` | 1メッセージにまとめるembed数の上限（Discordの上限は10） | 10 |
| `rate_limit_max_retries` | 429を受けたときに`retry_after`分待って同一実行内でリトライする最大回数。超えたら残りは次回実行に持ち越す | 3 |

## state管理の仕組み（`active_{provider}.json`）

- 「Animephiliaのカレンダーで既に通知済み（または初回既読化済み）」なIDを保持する
- 6時間毎チェックは、Animephiliaのカレンダーが返す**直近1週間分**のうち、
  ここに無いIDを新着として通知する
- 「消滅」の検知は行わない（過去カタログ全体を追わない設計のため）。一度通知した
  IDは残り続ける
- Animephilia側の掲載は配信日そのものではなく確認でき次第更新される運用のため、
  配信日から数日遅れて掲載されることがある。カレンダー取得は常に
  「実行時点から見て直近1週間」を返すローリングウィンドウなので、配信日から
  概ね1週間以内に掲載されればこの仕組みで検知できる。それ以上遅れて掲載
  された場合は取りこぼす
- `queue_{provider}.json`: 未送信の送信待ちFIFOキュー

## トラブルシューティング

Animephiliaの配信カレンダーが使っている`/wp-admin/admin-ajax.php`
(`action=get_svod_calendar_events`)は非公式・非公開のエンドポイントのため、
サイトの実装変更で無告知に壊れる可能性があります。壊れた場合は
`animephilia_client.py`のnonce取得（`ajax_calendar`変数のパース）や
リクエストパラメータ（`service`/`type`/`genre`/`path`）が現在のページ実装と
一致しているか、ブラウザの開発者ツールで実際のリクエストを見て確認してください。
