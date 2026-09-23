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
  対象とし、1時間毎に直近1週間分のカレンダーとの差分だけを見る
- 外部サービス cron-job.org から起動する構成（GitHubの`schedule:`は使わない）。
  ジョブは`run-notify`（1時間ごと）のみ

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

以下のいずれかで起動します。

- **GitHubのWeb UIから**: Actions → Notify → Run workflow → プルダウンで
  `init-read` を選んで実行（`workflow_dispatch`対応済みなので、これが一番手軽です）
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
テスト通知が届くことを確認してください。届いたタイトルで状態を判別できます。

| 届くタイトル | 意味 |
|---|---|
| `[テスト通知] <作品名>`（画像あり） | 取得・送信ともに正常 |
| `テスト通知（Animephiliaから取得できず）` | Discordは正常、Animephiliaからの取得が失敗 |
| `テスト通知（取得は成功／直近1週間の配信は0件）` | 両方正常、単に掲載が0件 |

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
| `notify_limit_per_run` | 1回の実行あたりのDiscord送信上限（件数） | 150 |
| `send_interval_seconds` | Discordメッセージ送信の間隔（秒） | 0.5 |
| `discord_embeds_per_message` | 1メッセージにまとめるembed数の上限（Discordの上限は10） | 10 |
| `rate_limit_max_retries` | 429を受けたときに`retry_after`分待って同一実行内でリトライする最大回数。超えたら残りは次回実行に持ち越す | 3 |
| `max_rejected_per_run` | Discordが400で拒否した件を切り離して除外する上限。超えたら個別データの問題ではないと判断し、残りは捨てずにキューへ残して打ち切る | 5 |

## エラー通知の仕組み

- エラーは発生の都度ではなく、**実行の最後に1通へまとめて**両方のチャンネルへ
  送ります（片方のチャンネルしか見ていない人が見落とさないため）
- 同じ種別のエラーが**3回連続**するまでは通知しません。Animephiliaへの接続は
  タイムアウトで単発に失敗することがあり、その多くは次の実行（1時間後）で
  勝手に直るためです。1時間毎の実行なので、3回連続＝約3時間ずっと失敗している
  状態になって初めて通知が飛びます
- 通知後も、同じ種別のエラーは**6時間に1回まで**に間引かれます（`state/errors.json`で管理）。
  1時間毎に実行されるため、間引かないと1日24通×2チャンネル飛んでしまいます
- 1回でも成功すると連続回数はリセットされ、次は改めて3回連続から数え直します
- ただし、Discordに拒否されて1件の通知を諦めたときは、1回目から即座に通知します
  （その件は再送されないため、待っていると失われたことに気づけないためです）
- 429（レート制限）はエラー通知しません。想定内の輻輳であり、また429直後に
  同じWebhookへエラーを送っても失敗する可能性が高いためです（実行ログには残ります）

## state管理の仕組み（`active_{provider}.json`）

- 「Animephiliaのカレンダーで既に通知済み（または初回既読化済み）」なIDを保持する
- 1時間毎チェックは、Animephiliaのカレンダーが返す**直近1週間分**のうち、
  ここに無いIDを新着として通知する
- 「消滅」の検知は行わない（過去カタログ全体を追わない設計のため）。一度通知した
  IDは残り続ける
- Animephilia側の掲載は配信日そのものではなく確認でき次第更新される運用のため、
  配信日から数日遅れて掲載されることがある。カレンダー取得は常に
  「実行時点から見て直近1週間」を返すローリングウィンドウなので、配信日から
  概ね1週間以内に掲載されればこの仕組みで検知できる。それ以上遅れて掲載
  された場合は取りこぼす
- 90日より古い記録は自動的に整理されます。カレンダーが返すのは直近1週間分だけ
  なので、それより十分古いIDが再び現れることはなく、残しても毎時コミットされる
  stateが肥大化するだけのためです（検知範囲7日に対して約13倍の安全マージン）
- `queue_{provider}.json`: 未送信の送信待ちFIFOキュー
- `errors.json`: エラーの連続失敗回数と最終通知日時（正常時は`{}`）

## トラブルシューティング

Animephiliaの配信カレンダーが使っている`/wp-admin/admin-ajax.php`
(`action=get_svod_calendar_events`)は非公式・非公開のエンドポイントのため、
サイトの実装変更で無告知に壊れる可能性があります。壊れた場合は
`animephilia_client.py`のnonce取得（`ajax_calendar`変数のパース）や
リクエストパラメータ（`service`/`type`/`genre`/`path`）が現在のページ実装と
一致しているか、ブラウザの開発者ツールで実際のリクエストを見て確認してください。

### 「Animephiliaからの新着取得に失敗しました」が届く

GitHub Actionsのランナーから animephilia.net への接続が稀にタイムアウトします
（同じコードで成功する実行と全滅する実行が観測されています。データセンタIPに
対するサイト側の遮断と思われます）。最大3回リトライするので一時的なものなら
自動的に復帰し、次の毎時実行でも再取得されます。

継続的に届く場合は遮断が恒常化している可能性があるため、取得経路の変更
（プロキシ経由など）の検討が必要です。なお同一エラーは3回連続してから、以降6時間に1回までに
間引かれるので、届いても1日最大4回です。

### 実行が数分かかる／止まって見える

取得がタイムアウトしてリトライを待っている状態です（最悪で1プロバイダあたり
約50秒 × 2）。ワークフローは`PYTHONUNBUFFERED=1`でログを逐次出力するので、
Actionsの実行ログでリトライの進行状況を確認できます。15分でジョブは
打ち切られます。
