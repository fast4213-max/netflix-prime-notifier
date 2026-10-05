# Netflix × Prime Video 重複配信通知 設計書（案）

NetflixとPrime Videoの両方で配信されている（された）アニメを突き合わせ、
該当があればDiscordの専用チャンネルへ通知する機能の設計案。
既存の新着通知（`docs/DESIGN.md`）には手を入れず、その上に追加する。

| 項目 | 案 |
|---|---|
| 突き合わせ対象 | 直近30日以内に配信開始を検知したタイトル同士 |
| 突き合わせキー | 正規化タイトル（NFKC・空白除去・末尾の括弧除去） |
| 実行契機 | 既存の1時間毎の `run-notify` の末尾（新規cronは不要） |
| 通知先 | 新規Webhook `DISCORD_WEBHOOK_CROSS`（専用チャンネル） |
| 通知タイミング | 2社目に載った時点で1回だけ |

---

## 1. 現状の制約（なぜ新しいstateが要るか）

- `state/*_active.json` は `{作品URL: 検知日時}` のみで**タイトルを持たない**。
  URLはNetflix/Amazonで体系が違い、突き合わせキーにならない。
- タイトルはキュー（送信後に消える）にしか残らない。
- 既存の `_provisional_titles` は「同一プロバイダ内の仮ID→URL付きID」用で、
  プロバイダ間の突き合わせには使えない。

→ タイトルを保持するカタログstateを追加する。

## 2. 追加するstate

| ファイル | 内容 | 保持 |
|---|---|---|
| `state/catalog_netflix.json` / `catalog_prime_video.json` | `{id: {title, key, release, seen_at, image_url, url}}` | 30日（`seen_at`基準）。照合期間と同じ |
| `state/cross_notified.json` | `{key: 通知日時}` 通知済みペア | 90日（`active`と同じ考え方） |
| `state/cross_queue.json` | 未送信FIFO（既存queue_runnerと同形式） | 送信まで |

- カタログへの登録は**通知の有無と無関係に**、`process_provider` が見た全候補
  （7日ウィンドウ内、既読スキップ分も含む）に対して行う。毎時実行なので、
  既読済みでも30日分が自然に溜まる。
- 通知を画像待ちで保留中のタイトルも登録してよい（照合には画像不要）。

## 3. 突き合わせロジック

```
実行末尾（両プロバイダのprocess_provider完了後）:
  for A, B in [(netflix, prime_video), (prime_video, netflix)]:
    for entry in catalog[A]:               # 今回新たに登録された分だけ見れば十分
      match = catalog[B].get(entry.key)    # B側の30日以内
      if match and entry.key not in cross_notified:
          queue_cross(entry, match); cross_notified[key] = now
```

- 双方向に評価するが `cross_notified` のキーは `key` 単独なので、
  NetflixとPrimeのどちらが後から載っても**通知は1回だけ**。
- 「30日」はカタログの保持期間そのもの。先に載った側が30日経つと消えるので、
  それより後に2社目が来ても通知されない。
- 正規化は `main._normalize_title` を共通モジュールへ移して流用。

### 3.1 タイトル揺れ（最大のリスク）

| 揺れの例 | 完全一致 | 対処 |
|---|---|---|
| 全角/半角・空白 | ○ | NFKCで吸収済み |
| 末尾「(字幕版)」「(吹替版)」 | ○ | 既存の括弧除去で吸収 |
| 「第2期」vs「2nd Season」vs「Season2」 | ✕ | **フェーズ2**: 季表記を落とした `base_key` で照合 |
| 副題の有無・表記ゆれ | ✕ | フェーズ2: 類似度（difflib/rapidfuzz）≥ 閾値 |

- **フェーズ1は完全一致のみ**（誤検知ゼロ優先）で運用し、取りこぼしがどれだけ
  出るかを実データで見てからフェーズ2へ。
- フェーズ2の曖昧一致は断定せず「同一作品の可能性」として別表記で通知する。

## 4. 通知フォーマット

```json
{"embeds": [{
  "title": "<作品タイトル>",
  "description": "Netflix: 2026-09-21 / Prime Video: 2026-10-03",
  "image": {"url": "<画像URL>"}
}]}
```

- 既存の新着通知は「リンクなし」方針だが、本チャンネルは「どちらで見るか選ぶ」
  用途なので、**両方の作品ページURLをdescriptionに載せる案**（要判断。Amazonの
  アフィリエイトタグは既存同様に除去済みのURLを使う）。
- 送信は既存の `queue_runner.drain_queue`（embed 10件まとめ・429待機・400切り分け）
  をそのまま使う。サニタイズも共通。

## 5. 実装変更

| ファイル | 変更 |
|---|---|
| `config.json` | `providers` とは別に `cross: {webhook_env: DISCORD_WEBHOOK_CROSS, window_days: 30}` を追加 |
| `titles.py`（新規） | `_normalize_title` を移設 |
| `state_manager.py` | catalog / cross_notified の load/save/prune を追加 |
| `main.py` | `process_provider` でカタログ登録、`main()` 末尾で `cross_match.run()` |
| `cross_match.py`（新規） | 突き合わせ＋キュー投入＋`drain_queue` |
| `init_read.py` | カタログも登録（通知なし）。**`cross_notified`には入れない**（初回に溜まった重複は一度は通知したいか要判断） |
| `dispatch.yml` | `DISCORD_WEBHOOK_CROSS` をenvに追加 |
| `DESIGN.md` | 本機能への参照を追記 |

エラーは既存の `broadcast_errors` に乗せる（クロスチャンネルにも届ける）。

## 6. 初期導入時の挙動

- カタログは導入した日から溜まり始めるので、**最初の30日間は照合範囲が
  段階的に広がる**（導入直後は直近7日分しか持っていない）。
- 過去分を一括で埋めたい場合は、`path=/month/YYYY/MM/` で過去1か月を取得して
  カタログだけ初期投入する案がある。ただし DESIGN.md 2.2節の通り、未確定で
  `url`/`image` が空のことがある。タイトルだけ使うなら実害は小さい。

## 7. 決定事項（ユーザー確認済み）

| 項目 | 決定 |
|---|---|
| 「同じ作品」 | 両方で配信されたら通知（配信日の近さは問わない） |
| 照合期間 | 当初30日。配信日が3か月以上離れる作品を取りこぼすため**365日**に変更（`config.json` の `cross.window_days`）。通知済みの記録はカタログより90日長く保持（先に消えると二重通知になるため） |
| チャンネル | 専用（`DISCORD_WEBHOOK_CROSS`） |
| リンク | Netflixを優先（Netflixに無ければPrime Video）。タイトルがリンクになる |
| 導入時 | `init-cross` で過去`window_days`日分を取り込み、該当を一度だけ通知 |
| 照合 | 完全一致（強化した正規化。下記の検証結果による） |

## 8. 実データでの検証（2026-10-05）

`/month/YYYY/MM/` で取得した2026年7〜10月分（Netflix 160タイトル / Prime 331タイトル）を突き合わせた。

| 方式 | 一致 |
|---|---|
| 既存の `_normalize_title`（NFKC・空白除去・末尾括弧除去） | 38件。取りこぼし3件 |
| 本機能の `match_key`（上記＋`〜`→`~`、`「」『』`除去、記号除去） | 41件。取りこぼし0件 |

取りこぼした3件はいずれも同一作品で表記ゆれだけだった:
`テムパル〜…〜`/`テムパル～…～`（波ダッシュと全角チルダ）、
`逃げ上手の若君 第二期`/`「逃げ上手の若君」第二期`、`BLEACH 千年血戦篇`/`『BLEACH』千年血戦篇`（Prime側だけ括弧で囲む）。

強化後に残った類似候補は別作品（ヤニねこ/ヤニねこ【オンエア版】、別の映画クレヨンしんちゃん、
ギャビーのドールハウスの別作品）で、誤一致は無かった。したがって**完全一致で運用でき、類似度照合は不要**。

注意: 41件のうち2件（猫と竜、落第賢者の学院無双）は両社の配信日が3か月以上離れており、
30日窓では拾えない。そのため `window_days` を365にした（取得できる過去データは2026年7月以降のみ）。

## 9. 実装

| ファイル | 内容 |
|---|---|
| `cross_match.py` | 照合キー・カタログ登録・突き合わせ・専用チャンネル送信 |
| `init_cross.py` | 導入用（`event_type: init-cross`）。月別ページから過去分を投入して一度通知 |
| `animephilia_client.py` | `fetch_month_events`、`pick_image_url` を追加 |
| `state_manager.py` | catalog / cross_notified の読み書き |
| `notifier.py` / `queue_runner.py` | embedにリンク・説明文を付けられるよう拡張（通常の新着は従来どおり） |
| `main.py` | 毎時の取得結果をカタログへ登録し、末尾で突き合わせ |
| `dispatch.yml` / `config.json` | `init-cross`、`DISCORD_WEBHOOK_CROSS`、`cross` 設定 |
