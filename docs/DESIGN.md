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

   **重要な制約と対策（実機確認済み）**: `newTitles`は1リクエストの`first`に
   150以上を指定すると`page too large`（`TOO_BIG`）エラーで拒否される
   （100までは成功）。一方で`offset`によるページングは正常に機能し、
   `offset=0`と`offset=100`で取得したデータに重複が無いことも実機確認済み。
   そのため`justwatch_client.fetch_new_titles()`は、指定件数が100を超える場合
   `first=100`ずつ`offset`をずらしながら複数回リクエストして結果を連結する
   実装にした。これにより`new_titles_fetch_count`/`init_fetch_count`は
   実質的に100件の壁を超えて設定できる（デフォルトは300件/500件、
   1プロバイダあたり3〜5リクエストで取得。実行は1時間毎なのでリクエスト数が
   増えても負荷上は問題ない）。
   なお、ページングで取得件数を伸ばしても「1回の実行間隔の間に設定件数を超える
   新着が追加されると、はみ出した分は検知されない」という性質そのものは残る
   （`notify_limit_per_run`超過分がキューに残るのとは異なり、こちらは
   本当に「見えなくなる」）。300件は現実的な追加ペース（1日あたり数件〜十数件
   程度）に対して十分な安全マージンがある想定だが、必要であれば
   `new_titles_fetch_count`をさらに増やす、または実行間隔を短くすることで
   対応できる。
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

---

## 13. v2: 週次全件チェック方式への設計変更

運用開始後、以下2点を理由に設計を見直した:

1. `newTitles`（新着順インデックス、件数ベースの取得）だけでは、JustWatch側の
   インデックス反映漏れや`new_titles_fetch_count`超過による**取りこぼしのリスク**が残る
2. 「配信終了→カタログから消滅→再配信」されたタイトルを**再度新規通知したい**という要件。
   従来の`seen_ids`（一度検知したらほぼ恒久的に既読）方式では、90日以内の再配信を
   拾えなかった

### 13.1 状態設計の変更: `seen` → `active`

`state/{provider}_seen.json`（一度検知したら基本ずっと既読、90日でprune）を廃止し、
`state/{provider}_active.json`（**現在そのプロバイダに実在すると確認済みのID**、
`{id: 最終確認日時}`）に置き換えた。

- 6時間毎チェック（`main.py`, newTitlesとの差分）・週次チェック
  （`weekly_catalog_check.py`, 全件との差分）の**どちらも同じ`active`を見る**
- `active`に無いIDが見つかったら「新規（または再配信）」として通知しキューに積み、
  `active`に登録する
- **消滅の検知は週次チェックだけが行う**: 全件取得した結果と`active`を比較し、
  「前回はあったが今回は無いID」を`active`から除外する
- 消滅日時を別途保存して厳密に日数計算する方式（例:「消滅から7日以上で再通知」）は
  **不採用**。実装をシンプルにするため、「週次チェックの実行間隔（＝概ね1週間）」を
  そのまま量子化された再通知の粒度として受け入れる。同じ週内で消えて復活した
  タイトルは、週次チェックからは「ずっとあった」ようにしか見えないため再通知されない
- `active`は週次チェックのたびに実際のカタログと同期されるため、`seen_ids`のような
  日数ベースのprune処理は不要になった（サイズはカタログの実サイズに自然に収束する）

### 13.2 週次全件取得: JustWatch `popularTitles`の1999件の壁と年代分割

JustWatchの`popularTitles`（`newTitles`とは別の、全件カタログ相当のクエリ）は、
`offset + first`が2000以上になると**エラーにはならず無条件で空リストを返す**
（実機確認済み）。そのため1クエリだけでは最大1999件までしか取得できない。

Netflix JP・Prime Video JPともに、フィルタ無しで叩くと1999件の壁に到達すること
（＝実カタログはそれ以上ある）を実機確認した。これを回避するため、
`min_release_year`/`max_release_year`で**公開年ごとに区切って**複数回に分けて
取得し、結果をID重複排除しつつ連結する方式にした（`justwatch_client.fetch_full_catalog`）。

実機確認した年代ごとの区切り方と件数（2026年時点、Netflix/Prime Video JP）:

- `〜1979`, `1980〜1999`, `2000年〜実行時点の翌年まで`を1年ごと、の29区間
- どの区間も1999件の壁に到達しないことを確認済み（最大区間でも1,500件未満）
- Netflix合計 約8,700件、Prime Video合計 約14,000件
- 年区間は`datetime.now().year`から**実行時点で動的に計算**するため、年が変わっても
  設定変更は不要。区間の上限を「実行時点の翌年」まで含めているのは、JustWatchには
  公開前の作品が翌年の年号で既に登録されていることがあるため
- 万一どこかの区間が1999件の壁に近づいた場合（`_CATALOG_PAGE_CAP_WARN_THRESHOLD`
  =1900件以上）は、実行ログに警告を出力する。その場合は該当区間をさらに
  月単位などに分割する対応が必要になる

`popularTitles`のレスポンス形式（`offers`の`monetizationType`/`package.shortName`）は
`newTitles`と同一のため、Prime Videoの「追加課金なしで見られるものだけ」フィルタ
（3章）は全件取得側でもそのまま流用できる。

### 13.3 実行頻度の変更: 1時間毎 → 6時間毎 + 週次

- `run-notify`（`main.py`）: 1時間毎 → **6時間毎**に変更。JustWatch側の負荷軽減と、
  検知遅延の許容（最大6時間程度なら実用上問題ない）とのバランスを取った
- `weekly-catalog-check`（`weekly_catalog_check.py`）: 新規追加。週1回
  （例: 毎週日曜04:00 JST）、cron-job.orgに`event_type: "weekly-catalog-check"`の
  ジョブを追加登録する（README.md参照）
- `init_read.py`も、newTitlesベースの部分既読化から**全件取得ベースの既読化**に
  変更した。週次チェックが正しく機能するには`active_{provider}.json`が
  「現在のカタログ全体」を正しく反映している必要があるため

### 13.4 Discord通知のバッチ化と429リトライ方式の変更

- 6時間毎に間隔を伸ばしたことで1回あたりの蓄積件数が増える見込みのため、
  `notify_limit_per_run`を30→150に引き上げた
- 1件＝1メッセージだった送信方式を、**最大`discord_embeds_per_message`
  （デフォルト10）件を1メッセージのembedとしてまとめて送る**方式に変更
  （`notifier.send_title_embeds` / `queue_runner.drain_queue`）。
  Discordの複数embed画像グルーピング表示（同一メッセージ内で画像が並んで
  表示される挙動）は非公式・undocumentedな挙動のため、列数を確実に
  コントロールすることはできない。実際の見た目は`test-notify`等で都度確認する
- 429時の挙動を変更: 従来は「即座に送信ループを打ち切り、次回実行に持ち越す」
  方式だったが、実行間隔が6時間に伸びたことで持ち越しの影響が大きくなるため、
  **`retry_after`だけ`sleep`して同一実行内でリトライ**する方式に変更した。
  連続で`rate_limit_max_retries`（デフォルト3）回失敗した場合のみ、
  本当に輻輳が続いていると判断して打ち切り、残りを次回実行に持ち越す
  （破棄はしない）

### 13.5 ファイル構成の変更点

```
netflix-prime-notifier/
├── main.py                    # 6時間毎: newTitles差分チェック
├── weekly_catalog_check.py    # 新規: 週次の全件チェック
├── init_read.py               # 変更: 全件取得ベースの初回既読化に変更
├── queue_runner.py            # 新規: キュー送信処理を共通化（main.py/weekly_catalog_check.pyで共有）
├── webhook_config.py          # 新規: Webhook URL解決処理を共通化
├── justwatch_client.py        # 追加: fetch_full_catalog（年代分割による全件取得）
├── notifier.py                 # 追加: send_title_embeds（複数embedをまとめて送信）
├── state_manager.py            # 変更: seen_*.json → active_*.json（prune処理は廃止）
└── state/
    ├── netflix_active.json     # 変更: 旧netflix_seen.jsonを置き換え
    ├── prime_video_active.json
    ├── netflix_queue.json
    └── prime_video_queue.json
```

---

## 14. v3: 6時間毎チェックをJustWatchからAnimephiliaに切り替え

運用開始後、`newTitles`インデックスが実際には新着を検知できていない疑いが出た
（本来出ているはずの新着が拾えていない）。JustWatch非公式APIの反映漏れは元々
既知のリスクだったため（4章参照）、6時間毎チェックの情報源を差し替えることにした。

### 14.1 代替データソースの調査

[Animephilia](https://animephilia.net/)（アニメ配信情報サイト、検索結果で上位に
出るためユーザーが実際に使っている＝信頼できると判断）のNetflix/Prime Video
「新着・配信予定カレンダー」記事を調査した。

記事ページ自体（例:
[`/netflix-arrival-calendar/`](https://animephilia.net/netflix-arrival-calendar/)、
[`/amazon-prime-video-arrival-calendar/`](https://animephilia.net/amazon-prime-video-arrival-calendar/)）
は静的HTMLの時点では中身が空で、JS（WordPressプラグイン`my-simplecalendar`）が
ページ読込後に非公開のWordPress管理者向けajaxエンドポイント
(`/wp-admin/admin-ajax.php`, `action=get_svod_calendar_events`)を叩いて
イベントデータをJSON取得し、カレンダーとして描画している。これを直接
POSTすることで、JS実行なしにデータを取得できることを実機確認した。

```
POST https://animephilia.net/wp-admin/admin-ajax.php
  action=get_svod_calendar_events
  service=netflix | prime_video
  type=new
  genre=all
  path=<記事のURLパス>              # 例: /netflix-arrival-calendar/
  nonce=<ページ本文の`ajax_calendar`変数から取得>

→ { "2026-09-15": [{title, start, url, region, kind, genres, image, tags}, ...], ... }
```

確認できた重要な性質（実機確認済み）:

- `path`に記事のトップパスを指定した場合、**当日を含む直近1週間分**が返る
  （`path`に`/month/YYYY/MM/`を付けると任意の月のカレンダー範囲が返るが、
  6時間毎チェックでは使わない。後述）
- `genre=all`を指定すると、**アニメに限らず映画・国内外ドラマ等すべての
  ジャンル**が対象になる（サイト自体はアニメ特化だが、このカレンダーは
  ジャンルを問わず配信サービス全体の新着を扱っている）。当初は
  アニメ専用の季節カレンダー記事（`<script type="application/json"
  id="2026-fall-anime-netflix">`のような、シーズンごとの配信予定を
  埋め込んだJSONブロック）しか見つけられなかったため通知対象がアニメに
  限定される懸念があったが、この`genre=all`のカレンダーを使うことで
  従来通り映画・シリーズ全体を対象にできる
- Prime Video側はサイト側が「対象：プライム会員特典（見放題）作品のみ掲載」
  と明記しており、レンタル/購入のみのタイトルは元から含まれない
  （3章のJustWatch側フィルタと同じ意図に自然と合致）
- `type=new`（デフォルトのカレンダー記事の設定）で返る範囲には、
  **配信日が未来のタイトルは含まれない**。カレンダー記事自体は将来の
  配信予定も扱っているが、それは`/month/YYYY/MM/`で未来月を指定した
  場合にのみ現れ、その場合は`url`/`image`が空（未確定）のことがある。
  デフォルトの「直近1週間」レスポンスでは実機確認した範囲で全件`url`が
  埋まっていた（＝配信確定済みのみ）。
  **6時間毎チェックでは`/month/`は使わず常にデフォルトの直近1週間分だけを
  見るため、未確定の未来作品を通知してしまうことは無い**。アニメ専用の
  季節カレンダーJSON（将来の配信予定の事前告知データ）は一切使用しない
  （未来の作品は「実際に配信されてから」上記のカレンダーAPI経由で
  自然に検知・通知される設計であり、事前告知データを個別に扱う必要はない）
- 記事の掲載タイミングは配信日そのものではなく「情報が確認でき次第」
  更新される運用のため、掲載が配信日から数日遅れることがある
  （例: 配信日が2日前でも、掲載＝レスポンスに現れるのは今日、ということが
  実際にあり得る）。そのため**「配信日が今日かどうか」ではなく、
  「直近1週間分のレスポンスに含まれるIDが既知(`active_animephilia_*.json`)
  かどうか」で新着判定する**（12.1節のactive方式と同じ考え方を流用）

### 14.2 非公式・非公開APIであることのリスクと対策

このajaxエンドポイントはサイトの管理画面向け内部実装であり、公式に
提供されているAPIではない。サイトの実装変更で無告知に壊れる前提で運用する:

- 取得処理は`animephilia_client.py`に隔離し、失敗時は`AnimephiliaError`を
  送出してエラーチャンネルに通知する（4章のJustWatch方針と同じ）
- 壊れた場合は都度作り直す（構造の互換性維持や後方互換シムは追わない）

### 14.3 状態管理: JustWatchとAnimephiliaでIDの体系が違う問題

Animephiliaが返すIDは自前のタイトルURL（`netflix.com/title/...`、
`amazon.co.jp/gp/video/detail/...`）であり、JustWatchのコンテンツID
（`tm1234567`のような形式）とは無関係。もし同じ`active_{provider}.json`を
共有すると、週次のJustWatch全件チェック（12章）が「全件取得したIDに
含まれない」ものとして扱い、Animephilia経由で検知したIDを毎週
誤って`active`から削除してしまう（＝週が明けるたびに再度「新着」として
誤通知され続ける）。

これを避けるため、`state_manager.py`に`source`引数を追加し、状態ファイルを
検知経路ごとに分離した:

- `active_{provider}.json`（source="justwatch"、従来通り） …
  週次の全件チェックが使う
- `active_animephilia_{provider}.json`（source="animephilia"、新規） …
  6時間毎チェックが使う。こちらは「消滅」の検知は行わない
  （一度出た配信は残り続け、消えた場合の再通知は週次チェック側に任せる）
- `queue_{provider}.json`は検知経路によらず共有のまま
  （送信待ちの入れ物であり、IDの体系不一致は問題にならないため）

`init_read.py`もAnimephilia側の状態を初回に通知なしで既読化するように
拡張した（直近1週間分をそのまま「新規」として初回に一斉通知しないため）。

### 14.4 週次全件チェックは引き続きJustWatchを使用

Animephiliaには「現在配信中の全タイトル一覧」に相当するデータが無い
（あくまで日々の新着カレンダーのみ）。「取りこぼしの補完」「配信終了→
再配信の検知」という週次チェックの役割はAnimephiliaでは代替できないため、
`weekly_catalog_check.py`は変更せずJustWatchの`fetch_full_catalog`を
使い続ける。

### 14.5 ファイル構成の変更点（v3時点、後にv4で一部差し戻し）

```
netflix-prime-notifier/
├── main.py                          # 変更: animephilia_client経由に切り替え
├── test_notify.py                   # 変更: 同上
├── init_read.py                     # 変更: Animephilia側の初回既読化を追加
├── animephilia_client.py            # 新規: Animephiliaカレンダーajax取得
├── state_manager.py                 # 変更: source引数を追加し状態ファイルを分離
├── weekly_catalog_check.py          # 変更なし（JustWatch全件チェックのまま）
├── justwatch_client.py              # 変更なし（週次チェック・init_readで使用継続）
└── state/
    ├── netflix_active.json           # JustWatch(tm-id)、週次チェック用
    ├── prime_video_active.json
    ├── netflix_active_animephilia.json   # 新規: Animephilia(URL)、6時間毎チェック用
    ├── prime_video_active_animephilia.json
    ├── netflix_queue.json
    └── prime_video_queue.json
```

---

## 15. v4: JustWatchを完全に廃止し、Animephiliaへ一本化

v3では「6時間毎チェックはAnimephilia、週次の全件チェック（取りこぼし補完・
再配信検知）はJustWatchのまま残す」というハイブリッド構成にしたが、
ユーザーの意向により**JustWatch経由の処理を全廃**し、Animephilia単体に
一本化した。合わせて「過去のカタログ全体を把握する」という要件自体を
取り下げ、**今後配信されるものだけを検知できればよい**という前提に変更した。

### 15.1 削除したもの

- `justwatch_client.py`（JustWatchの`newTitles`/`popularTitles`クエリ実装）
- `weekly_catalog_check.py`（週次の全件チェック。JustWatchの`fetch_full_catalog`
  に依存していたため、代替手段を用意せず機能ごと削除。Animephiliaには
  全件カタログ相当のデータが無く代替できないが、「今後の配信だけ検知できれば
  よい」という前提変更により、この機能自体が不要になった）
- `scripts/debug_justwatch.py` / `.github/workflows/debug.yml`
  （JustWatch専用のデバッグ手段だったため）
- `state/netflix_active.json` / `state/prime_video_active.json`
  （JustWatchのtm-idベースの状態。読み書きするコードが無くなったため削除）
- `.github/workflows/dispatch.yml`の`weekly-catalog-check`イベント種別
- `config.json`の`country` / `language` / `object_types` /
  `providers.*.short_name` / `providers.*.allowed_monetization_types`
  （すべてJustWatch向けのフィルタ設定。Animephilia側は元々カレンダー記事
  自体が「Prime会員特典（見放題）」のみに絞って掲載しているため、
  こちら側でのmonetization種別フィルタは不要）

### 15.2 状態管理: source引数を廃止し単一の`active_{provider}.json`に統合

14.3節で導入した`state_manager.py`の`source`引数（JustWatchとAnimephiliaの
ID体系が違うため状態ファイルを分離する仕組み）は、JustWatch側が丸ごと
無くなったことで存在意義が無くなったため撤去した。ファイル名も
`active_animephilia_{provider}.json`から元の`active_{provider}.json`に戻し、
`load_active(provider)` / `save_active(provider, active)`という単純な
シグネチャに戻した。

### 15.3 「配信終了→再配信」の再検知は行わない

週次チェックが担っていた「一度カタログから消えて再配信されたタイトルの
再通知」機能は、代替を用意せず削除した。Animephiliaの直近1週間カレンダーは
「消滅」を扱わない（一度通知したIDが二度と消えない）仕様のため、再配信は
「その配信日でカレンダーに新しいイベントとして載る」場合にのみ拾われる
（掲載側の運用に依存する）。

### 15.4 直近1週間のローリングウィンドウで「遅延掲載」をどこまで拾えるか

Animephiliaの配信日別カレンダーAPIは、**リクエストした瞬間の「今日」を
基準にした直近7日間のローリングウィンドウ**を返す（固定の暦週グリッドでは
ない。実機確認済み: 2026-09-21に叩くと2026-09-15〜21が返り、翌日に叩けば
2026-09-16〜22が返る）。1時間毎チェックは常にこのデフォルトの直近1週間を
取得するため、以下のように動く:

- ある作品の配信日が9/19で、Animephilia側の掲載（`url`が確定して
  レスポンスに現れるタイミング）が9/22〜9/25の間であれば、9/19はまだ
  ウィンドウ内（`[実行日-6, 実行日]`）に収まっているため通知される
- 配信日から7日を超えて掲載が遅れた場合（例: 9/26以降に掲載）は
  ウィンドウの外に出るため取りこぼす
- 実行間隔（15.6節の通り最終的に1時間毎）が短いほど、ウィンドウ内であれば
  掲載後の検知は早くなる

### 15.5 ファイル構成（最終形）

```
netflix-prime-notifier/
├── .github/workflows/
│   └── dispatch.yml              # repository_dispatch/workflow_dispatch
│                                    (run-notify / init-read / test-notify)
├── config.json                   # 通知件数上限などのみ（プロバイダ固有フィルタ設定は無し）
├── main.py                       # 1時間毎: Animephiliaカレンダーとの差分チェック
├── animephilia_client.py         # Animephiliaカレンダーajax取得
├── notifier.py                   # Discord Webhook送信共通処理
├── queue_runner.py               # 送信待ちキューの処理（main.pyから使用）
├── webhook_config.py             # Webhook URL解決処理
├── state_manager.py              # state(JSON)の読み書き
├── init_read.py                  # 初回セットアップ用：直近1週間分の既読化のみ、通知なし
├── test_notify.py                # テスト用：各チャンネルに1件だけ試験通知
├── state/
│   ├── netflix_active.json       # Animephiliaで既通知のID
│   ├── prime_video_active.json
│   ├── netflix_queue.json        # 未送信の新着FIFOキュー
│   └── prime_video_queue.json
├── requirements.txt
├── README.md
└── docs/
    └── DESIGN.md                 # 本ドキュメント
```

### 15.6 実行間隔を6時間毎→1時間毎に変更

JustWatch時代は取得コスト（1リクエストあたりのAPI負荷、レート制限）の
観点から実行間隔を6時間毎にしていたが、Animephilia移行後はリクエスト数が
大幅に少なく（1プロバイダあたりnonce取得+ajax取得の2リクエストのみ）、
Discordのレート制限（`config.json`の`rate_limit_max_retries`等で吸収できる
範囲）にも実行間隔短縮の影響はほぼ無いため、検知の遅延を減らす目的で
cron-job.org側の`run-notify`実行間隔を1時間毎に変更した。1時間毎にすることで、
14.1節で述べた「掲載が配信日から遅れる」ケースでも、直近1週間の
ローリングウィンドウ内であれば掲載後遅くとも1時間以内に検知できる。

### 15.7 データ取得失敗時の通知先を両チャンネルに変更

Animephiliaのajaxエンドポイントが壊れた場合、従来は失敗したプロバイダの
チャンネルにしかエラー通知していなかった。片方のチャンネルしか見ていない
運用者が気づけない事態を避けるため、`main.py`に`broadcast_error()`を追加し、
取得失敗（`AnimephiliaError`、および想定外の例外）時はNetflix/Prime Video
**両方**のチャンネルにエラーメッセージを送るようにした。また修正を
やりやすくするため、従来はエラーメッセージの文字列のみをログ出力していた
箇所を、`traceback.print_exc()`でGitHub Actionsの実行ログにフルスタック
トレースを残すよう変更した。
