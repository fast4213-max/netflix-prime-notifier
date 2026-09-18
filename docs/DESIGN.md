# Netflix / Prime Video 新着通知Bot 設計図

過去のRSS→Discord通知botの知見（`CLAUDE.discord-notify.md`）を踏襲し、
JustWatchを情報源としてNetflixとPrime Videoの新着タイトルをDiscordの
別チャンネルに通知するシステムの設計。**このドキュメントは設計フェーズの成果物であり、
実装はまだ行っていない。**

## 0. 前提・要確認事項

| 項目 | 前提値 | 備考 |
|---|---|---|
| 対象国/言語 | 日本 (`country=JP`, `language=ja`) | 日本語での依頼のため。異なる場合は要連絡 |
| 実行頻度 | 1時間ごと（cron-job.org → repository_dispatch） | ユーザー指定 |
| 1回の通知上限 | 30件/プロバイダ（超過分のみキューへ） | ユーザー確認済み。詳細は6章参照 |
| 対象コンテンツ種別 | 映画+シリーズ両方 | ユーザー確認済み（全部通知） |
| Prime Video通知対象 | flatrate（Prime特典）+ free/ads（広告つき無料） | ユーザー確認済み。rent/buyのみ除外 |
| JustWatchは非公式API | GraphQL (`apis.justwatch.com/graphql`) を利用 | 公式契約なし、スキーマ変更リスクあり（後述） |

---

## 1. 全体アーキテクチャ

```
cron-job.org (1時間毎)
      │  POST /repos/{owner}/{repo}/dispatches
      │  Authorization: Bearer {PAT}
      │  body: {"event_type": "run-notify"}
      ▼
GitHub Actions (dispatch.yml, repository_dispatch トリガーのみ)
      │
      ▼
main.py
  ├─ 1. JustWatchから Netflix / Prime Video それぞれの新着候補を取得
  ├─ 2. Prime Videoは「追加課金なしで見られるオファーを持つもの」のみでフィルタ
  ├─ 3. seen_ids と突き合わせて真の新着のみ抽出 → 即座にseen_ids登録 + queueへ追加
  ├─ 4. queueの先頭から上限件数分だけ Discord Webhook (Netflix用 / Prime用) へ embed通知
  │       - 送信できた分だけqueueから削除、残りは次回実行に持ち越し（破棄しない）
  ├─ 5. 取得・送信中に例外が起きたら「対象チャンネル」にエラーメッセージ通知
  └─ 6. state/*.json を更新 → git commit & push (GITHUB_TOKEN, contents:write)
```

- Netflixチャンネル: 新着通知 + Netflix取得/送信系のエラー通知
- Prime Videoチャンネル: 新着通知（Prime特典のみ）+ Prime取得/送信系のエラー通知
- 専用のエラー監視チャンネルは作らず、依頼の通り「各本チャンネル」にエラーを流す

---

## 2. 「前日/前々日のものが後から来る」問題の調査結果と対策

JustWatchの「新着」インデックスは実際の配信開始より**1〜3日程度遅れる/前後することが知られている**
（配信元からJustWatch側へのカタログ反映タイムラグ、タイムゾーン差、リージョン別ロールアウトなどが原因）。
そのため以下が起こり得る：

- 昨日配信開始のはずのタイトルが、今日になって初めて新着として出てくる
- 同じタイトルが「新着」判定の対象期間（例: 直近n日）に何度も現れ続ける
- 逆に今日出た新着が翌日以降に日付情報が補正される（released_at が変わる）

**→ 「日付」ベースの新着判定（例:「昨日released_atのものだけ通知」）は不採用。**
以下の方式で対応する：

1. 毎回の実行で **JustWatchの `newTitles` クエリから直近の新着候補を最大N件** 取得する
   （実装時の実機検証で、このクエリが「作品の公開日」ではなく「プラットフォームへの
   追加が新しい順」でタイトルを返すことを確認済み。日付での絞り込みではなく件数(N)
   ベースの取得とし、「候補の重複取得を許容する」設計にする）
2. `state/{provider}_seen.json` に **今まで検知済みのコンテンツID（JustWatchのID）** を
   `{id: 初回検知日時}` の形で保持
3. 取得した候補のうち、`seen_ids` に **未登場のID** だけを「真の新着」として扱う
4. 真の新着と判定した時点（＝実際にDiscordへ送信できたかどうかに関わらず）で
   即座に `seen_ids` へ登録する（**検知＝既読化**）。これにより、送信が後回しになった
   としても次回実行時にJustWatch側から同じ候補が再度返ってきて「新着」として
   二重検知されることはない
5. 真の新着はいったん全件 `state/{provider}_queue.json`（FIFOキュー）の末尾に積む
6. 送信は「6章で定めた上限件数」までキューの先頭から取り出して行う。
   取り出せなかった残りはキューに残り、**次回実行に必ず持ち越される**
   （後述の通り上限を超えても破棄はしない）
7. `seen_ids` は肥大化を防ぐため、**登録から90日経過したIDは毎回prune**（映画/シリーズの
   再配信で数年後に再度「新着」化するケースは稀なため実用上問題なし。値は要調整可）

この方式なら、JustWatch側の反映が1日遅れても2日遅れても、初めて観測した時点で
1回だけ確実にキューに乗り、重複通知も起きない。

---

## 3. Prime Videoの「追加課金なしで見られるものだけ」フィルタ設計

JustWatchはAmazon Prime Videoを1つのプロバイダ（**実機確認済みの短縮名: `amp`**）
として扱い、同じタイトルに対して以下のオファー種別が混在する：

- `monetization_type = "flatrate"` … Prime会員特典で追加料金なしで視聴可能 ← **通知対象**
- `monetization_type = "free" / "ads"` … 広告つき無料視聴 ← **通知対象**（ユーザー確認済み）
- `monetization_type = "rent" / "buy"` … 都度課金のレンタル/購入 ← **除外**（Prime特典ではないため）

フィルタロジック（「追加料金が発生しない」タイプだけ通す）：

```
NO_EXTRA_COST_TYPES = {"FLATRATE", "FREE", "ADS"}

for title in prime_video_new_titles:
    offers = title.offers
    watchable_without_extra_cost = any(
        o.package_short_name == "amp" and o.monetization_type in NO_EXTRA_COST_TYPES
        for o in offers
    )
    if watchable_without_extra_cost:
        notify(title)
```

※ レンタル/購入のみの「Amazon Video」は実機確認でも別プロバイダ（短縮名 `amz`）として
独立していることを確認済み。Prime Videoチャンネル経由の追加課金サービス（アニメタイムズ等の
Amazon Channel）もそれぞれ別プロバイダIDになるため、`packages=["amp"]` で絞り込んだ時点で
自動的に対象外になる（`justwatch_client.py` の `fetch_new_titles` で実装、`main.py` で
`allowed_monetization_types` に基づきフィルタ）。

---

## 4. データ取得方式

- JustWatchの非公開GraphQLエンドポイント（`https://apis.justwatch.com/graphql`）の
  `newTitles` フィールドを直接叩く自前クエリを `justwatch_client.py` に実装
  （`simple-justwatch-python-api` ライブラリには `newTitles` 相当の機能が無かったため、
  実機で存在確認・スキーマ検証した上で自前実装した。検証手順・生レスポンスは
  `scripts/debug_justwatch.py` に残してある）
- 取得内容: タイトル名、ポスター画像URL、JustWatch content ID（例: `tm1464109`）、
  offers一覧（monetization_type, package.short_name）
- HTTPクライアントは `httpx` を使用。`requests` のデフォルトUser-Agent
  （`python-requests/...`）だとJustWatch側のWAFに403で弾かれることを実機で確認済み
  （`httpx` のデフォルトUAでは通る）
- **リスク**: JustWatch非公式APIのため無告知でスキーマ変更される可能性がある
  → `justwatch_client.py` に取得処理を隔離し、破損時は例外(`JustWatchError`)を捕捉して
  エラーチャンネルに通知（サイレント停止させない）
  → スキーマ変更が疑われる場合は `scripts/debug_justwatch.py` を
  `workflow_dispatch` で手動実行し、生レスポンスを見て切り分ける

---

## 5. ファイル構成

```
netflix-prime-notifier/
├── .github/workflows/
│   ├── dispatch.yml                  # repository_dispatchのみ、scheduleなし（本番）
│   └── debug.yml                     # workflow_dispatchのみ（JustWatch API調査用）
├── config.json                       # 国/言語、プロバイダ短縮名、通知件数上限など
├── main.py                           # 通常実行（取得→フィルタ→diff→通知→state更新）
├── justwatch_client.py               # newTitlesクエリの自前実装（新着取得、offer情報）
├── notifier.py                       # Discord Webhook送信共通処理（embed生成、429検知、エラー通知）
├── state_manager.py                  # state(JSON)の読み書き・prune
├── init_read.py                      # 初回セットアップ用：既読化のみ、通知なし
├── test_notify.py                    # テスト用：各チャンネルに1件だけ試験通知
├── scripts/
│   └── debug_justwatch.py            # JustWatch生レスポンス確認用（debug.ymlから実行）
├── state/
│   ├── netflix_seen.json             # Netflix既通知IDセット
│   ├── prime_video_seen.json         # Prime Video既通知IDセット
│   ├── netflix_queue.json            # 未送信の新着FIFOキュー（破棄しない）
│   └── prime_video_queue.json
├── requirements.txt
├── README.md                          # セットアップ手順
└── docs/
    └── DESIGN.md                      # 本ドキュメント
```

---

## 6. Discord通知フォーマット

タイトルと画像のみのシンプルなembed（リンクなし、フッターなし）：

```json
{
  "embeds": [{
    "title": "<作品タイトル>",
    "image": {"url": "<ポスター画像URL>"}
  }]
}
```

エラー通知（同チャンネルへ通常メッセージで送信）:

```json
{"content": "⚠️ [Netflix] JustWatch取得に失敗しました: <エラー概要>\n次回実行時に再試行します。"}
```

### レート制限・キュー運用の設計

Discord Webhookのレート制限は1webhookあたり概ね「2秒間に5リクエスト」程度。
1回の実行で30件を送る場合でも、送信間に0.5秒程度のsleepを入れれば合計15秒ほどで
送り切れるため、時間あたりの実行（1時間毎）に対して十分余裕がある。

- **1回の実行あたりの送信上限：Netflix / Prime Videoともに30件**
  （Netflixが急に30件規模で新着を出すことがあるとのことなので、
  「10件区切りで3時間かけて小出しにする」方式ではなく、余裕を持って
  1回の実行内でまとめて送り切る方針にする）
- **30件を超えるバーストも実際に起こり得るとのことなので、超過分は破棄せず
  必ず `queue_{netflix,prime}.json` に残し、次回実行（1時間後）以降に持ち越して送信する。**
  キューはFIFO（検知順）。上限を超え続ける限り複数回の実行にまたがって
  消化され続けるだけで、データが失われることはない
- 2章の通り「検知時点」で既読化（`seen_ids`登録）とキュー追加を行うため、
  キューに滞留している間にJustWatch側の候補リストから同じ作品が消えても
  問題なく、また誤って二重にキューへ積まれることもない
- 送信間隔・上限件数は `config.json` の値としてチューニング可能にする

### 送信中に429（レート制限）を受けた場合の挙動

ユーザー確認の通り「破棄せずループを打ち切って次回に回す」方式にする：

- 送信ループの途中でDiscordから `429 Too Many Requests` が返ってきた時点で、
  **リトライで食い下がらず、その回の送信ループを即座に打ち切る**
- 送信に成功した分だけqueueから取り除き、**失敗した1件とまだ送っていない残り全件は
  queueにそのまま残す**（破棄しない）
- その状態でstateを保存し、workflowは正常終了（exit 0、失敗扱いにしない）。
  次回の実行（1時間後）でqueueの続きから自動的に再開する
- 429は「本当の異常」ではなく想定内の輻輳（一時的な混雑）なので、**このケースでは
  エラーチャンネル通知はしない**（429直後に同じWebhookへエラーメッセージを送ろうと
  しても同じ理由で失敗する可能性が高く無意味なため）。GitHub Actionsの実行ログにのみ
  記録する
- 一方、JustWatch取得失敗・Webhook URL不正（401/404）・想定外の例外など
  「レート制限とは無関係な本当の異常」は、引き続き5章の通り対象チャンネルへ
  エラー通知する

---

## 7. state管理・GitHub書き込み

- `state/*.json` の更新は GitHub Actions自身が commit & push（追加のPAT不要、標準の
  `GITHUB_TOKEN` で足りる。ただしリポジトリ設定で **Settings → Actions → General →
  Workflow permissions → Read and write permissions** が必須）
- 依頼文より「jsonのgithub書き込み設定は変更済み」とのことなので、この権限設定は
  対応済みという前提で進める（実装時に一応確認する）

---

## 8. GitHub Actions ワークフロー設計

`.github/workflows/dispatch.yml`（scheduleは使わず、外部cronからの`repository_dispatch`のみ）:

- `on.repository_dispatch.types`: `[run-notify, init-read, test-notify]`
- `permissions: contents: write`
- job内で `event_type` に応じてスクリプトを分岐
  - `run-notify` → `python main.py`（本番）
  - `init-read` → `python init_read.py`（初回既読化・通知なし）
  - `test-notify` → `python test_notify.py`（各チャンネルへ1件だけ試験通知）
- 実行後、state変更があれば commit & push

---

## 9. cron-job.org 設定（ユーザー側作業）

| 設定項目 | 値 |
|---|---|
| URL | `https://api.github.com/repos/fast4213-max/netflix-prime-notifier/dispatches` |
| Method | POST |
| Headers | `Authorization: Bearer {PAT}` / `Accept: application/vnd.github+json` / `Content-Type: application/json` |
| Body | `{"event_type": "run-notify"}` |
| 実行間隔 | 1時間毎 |

初回実行用・テスト用に、Bodyだけ変えたジョブをそれぞれ用意し普段はOFFにしておく：
- `{"event_type": "init-read"}`（最初に1回だけ手動実行）
- `{"event_type": "test-notify"}`（動作確認用、必要な時だけON）

---

## 10. 必要な認証情報（ユーザー側でこれから設定するもの）

| 項目 | 保存先 | 用途 |
|---|---|---|
| GitHub PAT (classic, `repo`スコープ) | cron-job.org側の設定のみ（**GitHub Secretsには入れない**） | repository_dispatch起動用 |
| `DISCORD_WEBHOOK_NETFLIX` | GitHub Secrets | Netflixチャンネルへの通知 |
| `DISCORD_WEBHOOK_PRIME` | GitHub Secrets | Prime Videoチャンネルへの通知 |

Secrets未設定時は実行時に明確なエラーメッセージで停止させ、README内にチェックポイントとして明記する。

---

## 11. 初回導入フロー（実装後の運用手順）

1. リポジトリ設定: Actions権限を Read and write に（依頼より対応済み想定、念のため確認）
2. GitHub Secretsに2つのWebhook URLを登録
3. `init-read` を手動実行 → 既存の新着リストを「既読」として一括登録（通知は飛ばさない）
4. `test-notify` で両チャンネルに1件ずつ試験通知 → 見た目・画像表示を確認
5. cron-job.orgに `run-notify` ジョブ（1時間毎）を登録し本稼働開始

---

## 12. 実装状況

設計・実装ともに完了。`claude/wizardly-tesla-4dm4k5` ブランチにpush済み。

- 実装したファイル一式は5章のファイル構成の通り
- Netflix短縮名 `nfx` / Prime Video短縮名 `amp` は実機確認済み
- `newTitles` クエリの存在・動作、`amp` プロバイダにflatrate/free/ads/rent/buyが
  混在すること、rent/buy専業の「Amazon Video」が別プロバイダ`amz`であることは
  すべてGitHub Actions上での実データ確認で検証済み
- `justwatch_client.fetch_new_titles()` を使ったdiff/queue/429ハンドリングの
  ロジックはモックによるローカルテストで検証済み（重複検知なし、上限超過分の
  キュー持ち越し、429時の安全な打ち切りをすべて確認）

### ユーザー側でこれから行う作業（README.md参照）

- `DISCORD_WEBHOOK_NETFLIX` / `DISCORD_WEBHOOK_PRIME` をGitHub Secretsに登録
- cron-job.org側でGitHub PATを使ったジョブ設定（`init-read`→`test-notify`→`run-notify`の順）
- 新着取得件数（`new_titles_fetch_count`、初期値50）で「もれ」が出ないかは
  運用しながら確認し、必要なら`config.json`の値を調整する

---

実装完了。残るのはユーザー側のSecrets登録とcron-job.org設定のみ（README.md参照）。
