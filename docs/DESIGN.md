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
| 1回の通知上限 | 10件/プロバイダ（超過分はキューへ） | RSS botの知見を踏襲。変更可 |
| 対象コンテンツ種別 | 映画+シリーズ両方 | 特に指定なければ両方通知 |
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
  ├─ 1. JustWatchから Netflix / Prime Video それぞれの新着一覧を取得
  ├─ 2. Prime Videoは「Prime特典(flatrate)のみ」でフィルタ
  ├─ 3. state/*.json の既通知IDセットと突き合わせて真の新着のみ抽出
  ├─ 4. 新着を Discord Webhook (Netflix用 / Prime用) へ embed通知
  │       - 上限超過分は queue_*.json に退避、次回以降に繰り越し
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

1. 毎回の実行で **直近Nデー分（例: 過去4日）の新着候補** をJustWatchから取得する
   （日付ではなく「候補の重複取得を許容する」設計）
2. `state/seen_ids.json` に **今まで通知済みのコンテンツID（JustWatchのID）** を
   `{id: 初回検知日時}` の形で保持
3. 取得した候補のうち、`seen_ids` に **未登場のID** だけを「真の新着」として通知
4. 通知後、そのIDを `seen_ids` に追加
5. `seen_ids` は肥大化を防ぐため、**登録から90日経過したIDは毎回prune**（映画/シリーズの
   再配信で数年後に再度「新着」化するケースは稀なため実用上問題なし。値は要調整可）

この方式なら、JustWatch側の反映が1日遅れても2日遅れても、初めて観測した時点で
1回だけ確実に通知され、重複通知も起きない。

---

## 3. Prime Videoの「Prime特典のみ」フィルタ設計

JustWatchはAmazon Prime Videoを1つのプロバイダ（技術名 `prv` 想定、実装時に実APIレスポンスで要確定）
として扱い、同じタイトルに対して以下のオファー種別が混在する：

- `monetization_type = "flatrate"` … Prime会員特典で追加料金なしで視聴可能 ← **これだけ通知したい**
- `monetization_type = "rent" / "buy"` … 都度課金のレンタル/購入（Prime特典ではない）
- `monetization_type = "free" / "ads"` … 広告つき無料視聴など（Prime Video内だが特典とは別扱い、要方針確認）

フィルタロジック：

```
for title in prime_video_new_titles:
    offers = title.offers
    has_flatrate = any(
        o.package_short_name == "prv" and o.monetization_type == "flatrate"
        for o in offers
    )
    if has_flatrate:
        notify(title)
```

※ Prime Videoチャンネル経由の追加課金サービス（U-NEXTアドオン等）はJustWatch上では
別プロバイダIDとして扱われるため、`providers=["prv"]` で絞り込んだ時点で自動的に対象外。

---

## 4. データ取得方式

- ライブラリ: `simple-justwatch-python-api`（PyPI, GraphQL経由でJustWatchを叩く非公式ラッパー）
  を利用し、生GraphQLクエリの自前メンテコストを下げる
- 取得内容: タイトル名、ポスター画像URL、JustWatch content ID、offers一覧（monetization_type,
  package_short_name）
- **リスク**: JustWatch非公式APIのため無告知でスキーマ変更される可能性がある
  → `justwatch_client.py` に取得処理を隔離し、破損時は例外を捕捉してエラーチャンネルに
  「JustWatch API取得失敗」を通知（サイレント停止させない）
  → ライブラリのバージョンは `requirements.txt` でピン留めし、計画的にのみ更新

---

## 5. ファイル構成

```
netflix-prime-notifier/
├── .github/workflows/dispatch.yml   # repository_dispatchのみ、scheduleなし
├── config.json                       # 国/言語、プロバイダID、通知件数上限など
├── main.py                           # 通常実行（取得→フィルタ→diff→通知→state更新）
├── justwatch_client.py               # JustWatch取得ラッパー（新着取得、offerフィルタ）
├── notifier.py                       # Discord Webhook送信共通処理（embed生成、レート制御、エラー通知）
├── state_manager.py                  # state(JSON)の読み書き・prune
├── init_read.py                      # 初回セットアップ用：既読化のみ、通知なし
├── test_notify.py                    # テスト用：各チャンネルに1件だけ試験通知
├── state/
│   ├── netflix_seen.json             # Netflix既通知IDセット
│   ├── prime_seen.json               # Prime Video既通知IDセット
│   ├── netflix_queue.json            # 上限超過分の繰越キュー
│   └── prime_queue.json
├── requirements.txt
├── README.md                          # セットアップ手順（下記5章参照）
└── docs/
    └── DESIGN.md                      # 本ドキュメント
```

---

## 6. Discord通知フォーマット

Embedで以下を送信（タイトル+画像。依頼通り最小構成）：

```json
{
  "embeds": [{
    "title": "<作品タイトル>",
    "url": "<JustWatchの作品ページURL>",
    "image": {"url": "<ポスター画像URL>"},
    "footer": {"text": "Netflix 新着"}   // or "Prime Video 新着（会員特典）"
  }]
}
```

エラー通知（同チャンネルへ通常メッセージ or 目立つ色のembedで送信）:

```json
{"content": "⚠️ [Netflix] JustWatch取得に失敗しました: <エラー概要>\n次回実行時に再試行します。"}
```

レート制限対策：1メッセージ送信毎に軽いsleep（例: 0.5〜1秒）、1回の実行での送信上限
（例: 10件）を超えた分はqueueに保存し次回実行分に繰り越す（RSS botの知見を踏襲）。

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

## 12. 未確定・実装時に確定させる項目

- JustWatchレスポンスにおけるAmazon Prime Videoの実際の `package_short_name`
  （`prv`と想定しているが実APIレスポンスで確認要）
- 「新着」取得の遡り日数N（デフォルト4日を想定、実データで調整）
- 映画/シリーズ両方を通知するか、どちらかに絞るか
- 1回あたりの通知上限件数（デフォルト10件/プロバイダ）
- `free`/`ads`（広告付き無料）をPrime Video通知に含めるか否か

---

以上が設計。この内容で問題なければ、次のステップとして実装（各ファイルの作成、
JustWatchレスポンスの実データ確認、Discord embed実装）に進む。
