# Netflix / Prime Video 新着通知Bot 設計書

Animephiliaの配信カレンダーを情報源に、NetflixとPrime Videoの新着タイトルを
Discordの別チャンネルへ通知するBotの設計書。

**本書は現行実装（稼働中）の仕様を記述する。** 過去の設計からの変更経緯は
付録Aにまとめてある。

| 項目 | 値 |
|---|---|
| 対象 | Netflix / Prime Video（日本） |
| 情報源 | animephilia.net の配信カレンダー（非公開ajax） |
| 検知範囲 | 当日を含む直近7日分（サイト側のAPI仕様） |
| 実行間隔 | 1時間毎（cron-job.org → repository_dispatch） |
| 対象ジャンル | 全ジャンル（映画・シリーズ・ドラマ等。アニメ限定ではない） |
| 通知先 | Discord Webhook × 2（Netflix用 / Prime Video用） |

---

## 1. 全体アーキテクチャ

```
cron-job.org (1時間毎)
      │  POST /repos/{owner}/{repo}/dispatches
      │  body: {"event_type": "run-notify"}
      ▼
GitHub Actions (dispatch.yml)
      │  concurrency: notify-state で直列化
      ▼
main.py
  ├─ 1. Animephiliaのカレンダーから直近7日分のイベントを取得（プロバイダ毎）
  ├─ 2. active（通知済みID一覧）と突き合わせ、未知のIDのみ抽出
  │       → 即座にactiveへ登録し、queueへ追加
  ├─ 3. queueの先頭から上限件数分をDiscordへembed送信
  │       送信できた分だけqueueから削除、残りは次回実行へ持ち越し（破棄しない）
  ├─ 4. 実行中のエラーを収集し、最後に1通へ集約して両チャンネルへ通知
  └─ 5. state/*.json を更新 → git commit & push（GITHUB_TOKEN, contents:write）
```

- Netflixチャンネル: Netflixの新着通知 + エラー通知
- Prime Videoチャンネル: Prime Videoの新着通知 + エラー通知
- 専用のエラー監視チャンネルは作らず、各本チャンネルにエラーを流す

---

## 2. データ取得（animephilia_client.py）

### 2.1 エンドポイント

カレンダー記事ページは静的HTMLの時点では空で、JS（WordPressプラグイン
`my-simplecalendar`）が管理画面向けajaxを叩いて描画している。これを直接
POSTすることで、JS実行なしにデータを取得する。

```
POST https://animephilia.net/wp-admin/admin-ajax.php
  action=get_svod_calendar_events
  service=netflix | prime_video
  type=new
  genre=all
  path=<記事のURLパス>     # /netflix-arrival-calendar/ | /amazon-prime-video-arrival-calendar/
  nonce=<ページ本文の`ajax_calendar`変数から正規表現で抽出>

→ {"2026-09-15": [{title, start, url, region, kind, genres, image, tags?}, ...], ...}
```

nonceはajax呼び出しの度に必要なため、毎回カレンダーページを取得して抽出する
（1プロバイダあたり nonce取得 + ajax の2リクエスト）。

### 2.2 レスポンスの性質（実機確認済み）

- **当日を含む直近7日分のローリングウィンドウ**を返す。固定の暦週グリッドではない
  （2026-09-21に叩くと 09-15〜09-21、翌日は 09-16〜09-22）
- `genre=all` により、アニメに限らず映画・国内外ドラマ等すべてのジャンルが対象
- **配信日が未来のタイトルは含まれない**（`type=new`のため）。`path`に
  `/month/YYYY/MM/`を付ければ任意月が取れるが、未確定で`url`/`image`が空の
  ことがあるため**使用しない**
- Prime Video側はサイトが「プライム会員特典（見放題）作品のみ掲載」と明記して
  おり、レンタル/購入のみのタイトルは元から含まれない（こちら側でのフィルタ不要）
- 記事の掲載は配信日そのものではなく「情報が確認でき次第」の運用のため、
  **掲載が配信日から数日遅れることがある**

### 2.3 リトライ

| 設定 | 値 |
|---|---|
| 1リクエストのタイムアウト | 15秒（通常1〜2秒で返る） |
| 最大試行回数 | 3回 |
| バックオフ | 2秒 → 5秒 |

GitHub Actionsのランナーからは、通常1秒程度で返るページが稀に丸ごと
タイムアウトする（データセンタIPに対するサイト側の遮断と思われる。同じコードで
成功する実行と全滅する実行が実際に観測されている）。一時的な通信断で
「サイト構造が変わった」旨の誤報が飛ぶのを防ぐためリトライする。

既定のUser-Agent（`python-httpx/x.y`）はWAF/CDNに弾かれうるため、ブラウザ相当の
`User-Agent` / `Accept-Language` を付与し、ajaxには`Referer`も合わせる。

### 2.4 非公式APIであることのリスク

サイトの管理画面向け内部実装であり、公式APIではない。実装変更で無告知に壊れる
前提で運用する:

- 取得処理は`animephilia_client.py`に隔離し、失敗時は`AnimephiliaError`を送出
- 壊れた場合は都度作り直す（後方互換シムは追わない）

---

## 3. 新着判定（main.py）

### 3.1 ID体系

| 条件 | ID |
|---|---|
| `url`あり | 作品URL（クエリ文字列を除去） |
| `url`なし | `{provider}:{日付}:{タイトル}` |

URLのクエリ除去は、Amazonのアフィリエイトタグ（`?tag=animephilia-svod-cal-22`）を
自分たちの通知に埋め込まないため。

### 3.2 判定ロジック

`state/{provider}_active.json` に**無いIDが現れたら新着**として扱い、
即座にactiveへ登録してqueueへ積む。

**「配信日が今日かどうか」では判定しない。** 2.2節の通り掲載が配信日から遅れる
ため、配信日基準では取りこぼす。直近7日分のレスポンスに含まれるIDが既知かどうかで
判定することで、掲載が数日遅れても初めて観測した時点で通知できる。

検知時点で既読化とキュー追加を同時に行うため、キューに滞留している間に
カレンダーから同じ作品が消えても問題なく、二重にキューへ積まれることもない。

### 3.3 拾えるケース・取りこぼすケース

- 配信日9/19の作品が9/22〜9/25に掲載 → 9/19はまだウィンドウ内なので**通知される**
- 配信日から7日を超えて掲載（9/26以降） → ウィンドウ外のため**取りこぼす**
- 実行間隔が1時間なので、ウィンドウ内であれば掲載後遅くとも1時間以内に検知

---

## 4. Discord通知（notifier.py / queue_runner.py）

### 4.1 フォーマット

タイトルと画像のみのシンプルなembed（リンクなし、フッターなし）:

```json
{"embeds": [{"title": "<作品タイトル>", "image": {"url": "<画像URL>"}}]}
```

エラーは通常のテキストメッセージで送信:

```json
{"content": "⚠️ 新着チェックでエラーが発生しました。\n・[netflix] ...\n..."}
```

### 4.2 送信前のサニタイズ

Discordの制限を超えると400が返り、キューの先頭が詰まって以降の通知が
一切流れなくなるため、送信前に丸める:

| 対象 | 処理 |
|---|---|
| embed title | 256文字で切り詰め（末尾を`…`に） |
| 空のtitle | `(タイトル不明)` に置換 |
| 画像URL | http(s)以外は除去 |
| エラー本文 | 2000文字で切り詰め |

### 4.3 キュー運用

| 設定 | 値 |
|---|---|
| 1メッセージのembed数 | 10（Discord上限。設定値が超過していても自動で切り詰め） |
| 1実行の送信上限 | 150件 |
| 送信間隔 | 0.5秒 |
| キュー順序 | FIFO（検知順） |

上限を超えた分は**破棄せず** `state/{provider}_queue.json` に残し、次回実行以降へ
持ち越す。上限を超え続ける限り複数回の実行にまたがって消化されるだけで、
データが失われることはない。

`notifier.send_title_embeds()` は上限超過を黙って切り捨てず**例外にする**。
切り捨てると呼び出し側が「送信済み」とみなしてキューから削除し、通知が消失するため。

### 4.4 Discord応答別の挙動

| 応答 | 挙動 | エラー通知 |
|---|---|---|
| 429 | `retry_after`待って同一実行内でリトライ。3回連続で打ち切り持ち越し | しない（※） |
| 400 | 1件ずつに分割して原因の1件を特定し、その1件だけ除外して続行 | する |
| 400が5件超 | 個別の内容の問題ではないと判断し、残りは捨てずに保持して打ち切り | する |
| 401/403/404 | Webhook失効。再試行せず中断、キューは保持 | する |
| 5xx / 通信断 | キュー保持、次回実行で再試行 | する |

※ 429は想定内の輻輳であり本当の異常ではない。また429直後に同じWebhookへ
エラーを送っても同じ理由で失敗する可能性が高く無意味なため、実行ログにのみ記録する。

**400を1件ずつ切り分ける理由:** 400は同じ内容を何度送り直しても成功しない。
従来は同じチャンクを毎回再送し続けるため、1件の不正なデータでキューの先頭が
永久に詰まり、以降の新着が一切通知されなくなっていた。

**5件で打ち切る理由:** 立て続けに400が返るのは個別データの問題ではなく送信の
仕組み側が壊れている可能性が高いため、それ以上キューを捨てない。

### 4.5 キューの保存保証

`drain_queue()` はどの経路で抜けても`finally`でキューを保存する。保存し損ねると、
active側には「通知済み」と記録されたまま未送信の件が消えてしまうため。

---

## 5. エラーハンドリング（main.py）

### 5.1 集約と間引き

エラーは発生の都度送らず、**実行の最後に1通へ集約**して**両方のチャンネル**へ送る。

- 両チャンネルへ送るのは、片方しか見ていない人が気づけない事態を避けるため
- 同一内容のエラーは**6時間に1回まで**に間引く（`state/errors.json`で管理）。
  毎時実行なので、素直に送ると1日24通×2チャンネル飛んでしまう

**通知文言の出し分け**（実機で発生・修正済み）: `process_provider`の例外は
すべて`f"[{provider}] Animephiliaからの新着取得に失敗しました: {内訳}"`という
同じ枕詞で始まるため、枕詞では「本当にサイトの構造が変わったのか」「単なる
接続エラーか」を判別できない。実際に2026-09-21にGitHub Actionsランナーから
Animephiliaへの接続が`httpx.ConnectTimeout`で3回リトライとも失敗する事象が
発生し、この時「サイトの構造が変わった可能性があります」と通知したが、実際は
一時的なネットワーク不調で次の実行では正常に戻っていた。誤診断を避けるため、
`main.broadcast_errors`で内訳側の文言を見て判定するようにした:

- `_STRUCTURE_ERROR_MARKERS`（`"構造が変わった"` / `"レスポンス形式が想定と
  異なります"` / `"レスポンスがカレンダー形式ではありません"`）のいずれかを
  含む場合のみ「サイトの構造が変わった可能性があります」と通知する
- 含まない場合（`_request_with_retry`がリトライを使い切って諦めた
  タイムアウト/5xx等）は「接続に失敗しました。一時的なネットワーク不調の
  可能性が高く、通常は次回実行で自動復旧します」という文言にする

### 5.2 例外の扱い

| 例外 | 扱い |
|---|---|
| `AnimephiliaError` | エラー収集。キューの続きだけは送信（新規追加なし） |
| 想定外の例外 | 1プロバイダで止めず、次のプロバイダへ進む |

いずれも`traceback.print_exc()`でGitHub Actionsの実行ログにフルスタックトレースを
残す。ワークフローは正常終了させる（exit 0、失敗扱いにしない）。

---

## 6. state管理（state_manager.py）

| ファイル | 内容 | 保持期間 |
|---|---|---|
| `{provider}_active.json` | `{ID: 検知日時}` 通知済みID一覧 | **90日** |
| `{provider}_queue.json` | `[{id, title, image_url, detected_at}, ...]` 未送信FIFO | 送信まで |
| `errors.json` | `{エラー署名: 最終通知日時}` クールダウン記録 | 上書き |

### 6.1 activeの保持期間が90日である理由

検知範囲は7日（2.2節）なので、それより十分古いエントリはもう候補として現れない。
残しても再通知の抑止には効かず、毎時コミットされるstateが際限なく膨らむだけ。

7日ちょうどではなく90日にしているのは**安全マージン**。記憶を消した直後に
そのIDがまだAPIに載っていると再び「新着」と誤判定して二重通知になるため、
検知範囲の約13倍の余裕を取っている。

定常状態のサイズ見積もり（現在の掲載ペース: Netflix 34件/週、Prime 68件/週）:

| ファイル | 件数 | サイズ |
|---|---|---|
| netflix_active.json | 約440件 | 約35KB |
| prime_video_active.json | 約880件 | 約80KB |

### 6.2 書き込みの安全性

- 一時ファイルへ書いてから`replace()`で置換する（途中終了でJSONが壊れない）
- 読み込み失敗時は初期値で実行を継続する（最悪、再通知が出るだけで済む）

---

## 7. GitHub Actions（.github/workflows/dispatch.yml）

`schedule`は使わず、外部cronからの`repository_dispatch`と手動の
`workflow_dispatch`のみ。

| 設定 | 値 | 理由 |
|---|---|---|
| `permissions` | `contents: write` | state更新のcommit & push |
| `concurrency` | `group: notify-state`, `cancel-in-progress: false` | 下記参照 |
| `timeout-minutes` | 15 | 暴走時の打ち切り |
| `PYTHONUNBUFFERED` | `1` | 下記参照 |

### 7.1 concurrencyで直列化する理由

state/*.jsonは「通知済みかどうか」の唯一の記録なので、実行が重なると両方が
同じstateを読んで**同じ新着を二重通知**し、後からpushした側が弾かれて記録を失う。

`cancel-in-progress`を`false`にしているのは、途中で止めるとキューの送信途中で
stateが未保存のまま失われるため。

### 7.2 PYTHONUNBUFFEREDを設定する理由

これが無いとPythonのprintがブロックバッファリングされ、実行中はログが一切
流れない。取得のリトライ待ちで数分かかったとき「固まった」ようにしか見えず、
実際に切り分けに支障が出た。

### 7.3 event_typeによる分岐

| event_type | スクリプト | 動作 |
|---|---|---|
| `run-notify` | `main.py` | 本番。新着検知＋通知 |
| `init-read` | `init_read.py` | 初回既読化。直近7日分を通知せずstate登録 |
| `test-notify` | `test_notify.py` | 疎通確認。stateに触れず各チャンネルへ1件送信 |

### 7.4 stateのcommit & push

差分がある場合のみcommitし、pushが弾かれたらrebaseして**最大5回リトライ**する。
pushが弾かれたままだと「通知済み」の記録が消えて次回に二重通知が出るため。
5回失敗したらワークフローを失敗させる（`::error::`）。

---

## 8. 外部cron設定（cron-job.org / ユーザー側作業）

| 設定項目 | 値 |
|---|---|
| URL | `https://api.github.com/repos/fast4213-max/netflix-prime-notifier/dispatches` |
| Method | POST |
| Headers | `Authorization: Bearer {PAT}` / `Accept: application/vnd.github+json` / `Content-Type: application/json` |
| Body | `{"event_type": "run-notify"}` |
| 実行間隔 | 1時間毎 |

初回用・テスト用に、Bodyだけ変えたジョブを用意し普段はOFFにしておく
（`init-read` / `test-notify`）。GitHubのWeb UIからは
Actions → Notify → Run workflow のプルダウンでも手動実行できる。

---

## 9. 認証情報

| 項目 | 保存先 | 用途 |
|---|---|---|
| GitHub PAT (classic, `repo`スコープ) | cron-job.org側のみ（**GitHub Secretsには入れない**） | repository_dispatch起動用 |
| `DISCORD_WEBHOOK_NETFLIX` | GitHub Secrets | Netflixチャンネルへの通知 |
| `DISCORD_WEBHOOK_PRIME` | GitHub Secrets | Prime Videoチャンネルへの通知 |

Secrets未設定時は該当プロバイダの処理をスキップし、その旨をログに出力する。

リポジトリ設定で **Settings → Actions → General → Workflow permissions →
Read and write permissions** が必須（state更新のpushに必要）。

---

## 10. ファイル構成

```
netflix-prime-notifier/
├── .github/workflows/
│   └── dispatch.yml              # repository_dispatch / workflow_dispatch
├── config.json                   # 通知件数上限などのチューニング値
├── main.py                       # 本番: 新着検知＋通知
├── animephilia_client.py         # Animephiliaカレンダー取得
├── notifier.py                   # Discord Webhook送信
├── queue_runner.py               # 送信待ちキューの処理
├── webhook_config.py             # Webhook URL解決
├── state_manager.py              # state(JSON)の読み書き
├── init_read.py                  # 初回セットアップ: 既読化のみ、通知なし
├── test_notify.py                # 疎通確認: 各チャンネルへ1件送信
├── state/
│   ├── netflix_active.json       # 通知済みID
│   ├── prime_video_active.json
│   ├── netflix_queue.json        # 未送信FIFOキュー
│   ├── prime_video_queue.json
│   └── errors.json               # エラー通知のクールダウン記録
├── requirements.txt              # requests, httpx
├── README.md
└── docs/
    └── DESIGN.md                 # 本ドキュメント
```

---

## 11. 設定値一覧

### config.json

| キー | 既定値 | 意味 |
|---|---|---|
| `notify_limit_per_run` | 150 | 1実行あたりの送信件数上限 |
| `send_interval_seconds` | 0.5 | メッセージ間のsleep秒数 |
| `discord_embeds_per_message` | 10 | 1メッセージにまとめるembed数（上限10で自動切り詰め） |
| `rate_limit_max_retries` | 3 | 429の連続リトライ上限 |
| `max_rejected_per_run` | 5 | 400で除外する件数の上限（超えたら打ち切り） |
| `providers.*.webhook_env` | - | Webhook URLを読む環境変数名 |

### コード内定数

| 定数 | 値 | 場所 |
|---|---|---|
| `ACTIVE_RETENTION_DAYS` | 90 | state_manager.py |
| `ERROR_COOLDOWN_HOURS` | 6 | state_manager.py |
| `_REQUEST_TIMEOUT_SECONDS` | 15 | animephilia_client.py |
| `_MAX_ATTEMPTS` | 3 | animephilia_client.py |
| `_RETRY_BACKOFF_SECONDS` | (2, 5) | animephilia_client.py |
| `MAX_EMBEDS_PER_MESSAGE` | 10 | notifier.py |
| `MAX_EMBED_TITLE_LENGTH` | 256 | notifier.py |
| `MAX_CONTENT_LENGTH` | 2000 | notifier.py |

---

## 12. 導入・運用手順

1. リポジトリ設定でActions権限を Read and write に
2. GitHub Secretsに2つのWebhook URLを登録
3. `init-read` を1回実行 → 直近7日分を既読化（通知は飛ばさない）
4. `test-notify` で両チャンネルに1件ずつ試験通知 → 見た目・画像表示を確認
5. cron-job.orgに `run-notify` ジョブ（1時間毎）を登録し本稼働開始

`test-notify`は取得に失敗しても送信自体は試みる（Discordへの疎通確認が主目的の
ため）。その場合タイトルに理由が出るので、Discord側の問題か取得側の問題かを
区別できる:

| 届くタイトル | 意味 |
|---|---|
| `[テスト通知] <作品名>`（画像あり） | 取得・送信ともに正常 |
| `テスト通知（Animephiliaから取得できず）` | Discordは正常、取得が失敗 |
| `テスト通知（取得は成功／直近1週間の配信は0件）` | 両方正常、単に掲載が0件 |

---

## 13. 既知の制約・リスク

| 項目 | 内容 |
|---|---|
| 検知範囲 | 直近7日のみ。配信日から7日超で掲載された作品は取りこぼす |
| 非公式API | サイト実装の変更で無告知に壊れる。壊れたら作り直す前提 |
| IP遮断 | GitHub ActionsのIPからのアクセスが稀にタイムアウトする。リトライで吸収するが、継続的に遮断された場合は取得経路の変更が必要 |
| 再配信の検知 | 「配信終了→再配信」は、その配信日でカレンダーに新イベントとして載る場合のみ拾える（掲載側の運用に依存） |
| 未来の配信予定 | 扱わない。実際に配信されてからカレンダー経由で自然に検知される |
| embedの見た目 | Discordの複数embed画像グルーピング表示は非公式・undocumentedな挙動のため、列数を確実にコントロールできない |

---

## 付録A. 設計変更の経緯

### A.1 v1: JustWatch + newTitles（廃止）

JustWatchの非公開GraphQL API（`apis.justwatch.com/graphql`）の`newTitles`クエリを
情報源とし、`seen_ids`との差分で新着を検知していた。Prime Videoは
`monetizationType`が`flatrate`/`free`/`ads`のものだけを通すフィルタを掛けていた
（プロバイダ短縮名 Netflix=`nfx` / Prime Video=`amp` は実機確認済み）。

### A.2 v2: 週次全件チェックの追加（廃止）

`newTitles`だけでは取りこぼしのリスクが残ること、「配信終了→再配信」を
再通知したい要件から、`popularTitles`による週次の全件チェックを追加。
状態を`seen`（恒久的な既読）から`active`（実在すると確認済みのID）に変更し、
週次チェックで消滅を検知する方式にした。

このとき判明した知見: `popularTitles`は`offset + first`が2000以上になると
**エラーにならず空リストを返す**ため、1クエリでは1999件までしか取れない。
`min_release_year`/`max_release_year`で公開年ごとに29区間へ分割して取得していた
（Netflix 約8,700件、Prime Video 約14,000件）。

合わせて実行間隔を1時間毎→6時間毎に変更し、`notify_limit_per_run`を30→150へ、
送信を1件1メッセージから最大10件のembedまとめ送りへ、429時の挙動を
「即打ち切り」から「`retry_after`待って同一実行内でリトライ」へ変更した。

### A.3 v3: 新着チェックをAnimephiliaへ切り替え

運用開始後、`newTitles`が実際には新着を検知できていない疑いが出た（本来出ている
はずの新着が拾えない）。JustWatch非公式APIの反映漏れは既知のリスクだったため、
情報源をAnimephiliaのカレンダーへ差し替えた。

このとき、JustWatch（`tm1234567`形式）とAnimephilia（作品URL）でID体系が違うため、
状態ファイルを`source`引数で分離していた（週次チェックがAnimephilia由来のIDを
毎週誤って削除してしまうのを避けるため）。

### A.4 v4: JustWatchを完全廃止しAnimephiliaへ一本化

「過去のカタログ全体を把握する」という要件自体を取り下げ、**今後配信されるものだけを
検知できればよい**という前提に変更。`justwatch_client.py` /
`weekly_catalog_check.py` / デバッグ用スクリプトを削除し、v3で導入した`source`引数も
撤去して`active_{provider}.json`に統合した。

実行間隔は1時間毎に戻した。Animephilia移行後はリクエスト数が大幅に少ないため
（1プロバイダあたり2リクエスト）、間隔短縮のコストがほぼ無く、検知遅延を
減らせるため。

### A.5 v5: 通知が止まる/重複する不具合の修正

デバッグにより以下の不具合を確認し修正した。詳細は各章に反映済み。

| 不具合 | 影響 | 対処 |
|---|---|---|
| 400をキュー先頭で無限に再送 | **以降の新着が永久に通知されない** | 1件ずつ切り分けて除外（4.4節） |
| 256文字超のタイトル・不正な画像URLを無検査で送信 | 上記の400を自ら作り込む | 送信前にサニタイズ（4.2節） |
| embed上限超過を黙って切り捨て | 通知が消失 | 上限で切り詰め＋例外化（4.3節） |
| キュー要素のキー欠損でKeyError | キュー未保存のままactiveだけ更新され通知が永久に失われる | 欠損許容＋`finally`で保存（4.5節） |
| エラーを都度送信 | 1原因で各チャンネルに4通×毎時 | 1通へ集約＋6時間クールダウン（5.1節） |
| 取得の一時的失敗で即エラー通知 | 誤報 | 最大3回リトライ（2.3節） |
| activeを無限に保持 | stateが際限なく肥大化 | 90日で整理（6.1節） |
| ワークフローの並行実行 | **二重通知＋state消失** | concurrencyで直列化＋push リトライ（7.1/7.4節） |
| 401/403/404を5xxと同一視 | 無駄な再試行 | 区別して即中断（4.4節） |
| stdoutのバッファリング | 実行中ログが見えず「固まった」ように見える | `PYTHONUNBUFFERED=1`（7.2節） |
