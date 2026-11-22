# ジャンル別通知 設計書

アニメ以外の新着（映画・ドラマ・バラエティなど）を、ジャンル別のDiscordチャンネルへ
通知する機能。アニメの通知（`docs/DESIGN.md`）と重複配信通知（`docs/DESIGN_CROSS.md`）には
手を入れず、その上に追加する。

| 項目 | 値 |
|---|---|
| 情報源 | Animephiliaの**全ジャンル版**新着カレンダー（`/netflix-arrival-calendar/`、`/amazon-prime-video-arrival-calendar/`） |
| 対象 | アニメ以外（アニメは既存の通知が担当） |
| 通知先 | 7ジャンル × 2社 = 最大14チャンネル（Webhook未設定のチャンネルはスキップ） |
| 実行契機 | 既存の`run-notify`（毎時）の中。各プロバイダのアニメ処理の直後に実行 |

## 1. 振り分け

APIは各作品に`kind`（`movie`/`tv`）、`region`（`domestic`/`overseas`）、`genres`を返す。
`config.json`の`genre.channels`を**上から順に評価し、最初に合ったもの**に決める。
最後の`other`は条件なしの受け皿なので、アニメ以外は必ずどれか1チャンネルに入る。

| 順 | チャンネル（キー） | 条件 |
|---|---|---|
| 1 | ドキュメンタリー (`documentary`) | `genres`に「ドキュメンタリー」 |
| 2 | バラエティ (`variety`) | `genres`に「バラエティ」「リアリティ」「お笑い」「トーク」のいずれか |
| 3 | 邦画 (`movie_jp`) | `kind=movie` かつ `region=domestic` |
| 4 | 洋画 (`movie_foreign`) | `kind=movie` かつ `region=overseas` |
| 5 | 国内ドラマ (`drama_jp`) | `kind=tv` かつ `domestic` かつドラマ系ジャンル語あり |
| 6 | 海外ドラマ (`drama_foreign`) | `kind=tv` かつ `overseas` かつドラマ系ジャンル語あり |
| 7 | その他 (`other`) | 上のどれでもない（キッズのみ、スポーツ中継、`kind`/`region`欠落など） |

- ジャンル語はサービスで表記が違う（Netflix「バラエティ番組」「リアリティ番組」、
  Prime「トークショー・バラエティ」「リアリティショー」）ため、**部分一致**にしている。
- ドラマ系ジャンル語（`keyword_groups.drama`）は設定で調整できる。コードは触らない。
- 既知の限界: ジャンルが「国内」だけのバラエティ番組などはドラマ系の語が無いので「その他」に入る。

## 2. アニメの除外（二重通知の防止）

全ジャンル版にはアニメも混ざる。一方で**アニメ版にだけ載るアニメもある**ため、全ジャンル版は
アニメ版の上位集合ではない。以下のいずれかに当たるものを「アニメ」として除外する。

1. 同じ実行でアニメ版カレンダーに載っているID
2. アニメの既読（`{provider}_active.json`）にあるID
3. `genres`に「アニメ」を含む

実データでは「全ジャンル版でアニメ表記なのにアニメ版に無い」作品は0件だった
（テストで確認: `tests/test_genre_router.py`）。アニメ版の取得に失敗した回は、除外できないので
その回のジャンル別通知を見送る（エラー自体はアニメ側が通知する）。

## 3. state

| ファイル | 内容 |
|---|---|
| `state/{provider}_genre_active.json` | `{ID: 検知日時}`。**会社ごとに1つ**（ジャンル判定が後で変わっても再通知しない） |
| `state/{provider}_genre_queue.json` | 未送信FIFO。各要素が`channel`を持つ。会社ごとに1ファイル |

- 送信はチャンネル毎に`queue_runner.drain_queue`で行う（上限`notify_limit_per_run`はチャンネル毎）。
  キューの保存は`persist`で差し替え、最後にまとめて1ファイルへ書く。
- Webhook未設定のチャンネルの作品は**既読にもせず送らない**。Webhookを後から設定すれば、
  その時点の直近1週間分が通知される。

## 4. 通知の中身

アニメと同じ（タイトル+画像、タイトルは作品ページへのリンク）。画像待ち、同名の重複排除、
仮IDの扱いは`main.process_provider`と同じ処理を使う。

## 5. 漏れ防止

1. 受け皿`other`があり、未知の`kind`/`region`も落とさない。
2. 実行毎に「候補数 = 既読 + アニメ + 保留 + スキップ + 新着」を数え、合わなければエラー通知する。
3. キューは送信できた分だけ消す（失敗・429・Webhook失効は残して次回再送）。
4. 取りこぼしが起きるのは、Actionsが7日を超えて止まった場合だけ（既存と同じ）。

## 6. 設定値

`config.json`の`genre`:

| キー | 内容 |
|---|---|
| `anime_keywords` | アニメ判定語 |
| `provider_env_prefix` | Webhook環境変数名の会社部分（netflix→`NETFLIX`、prime_video→`PRIME`） |
| `keyword_groups` | `documentary`/`variety`/`drama`のジャンル語 |
| `channels` | 評価順のチャンネル定義（`key`、`label`、`kind`、`region`、`genre_group`） |

Webhook環境変数名は `DISCORD_WEBHOOK_{会社}_{キーの大文字}`（例: `DISCORD_WEBHOOK_NETFLIX_MOVIE_JP`）。
