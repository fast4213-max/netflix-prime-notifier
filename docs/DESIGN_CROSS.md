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

## 7. 確認したい点

1. 「同じ作品」の意味: ①両社で配信されている作品を知らせる（本案）か、
   ②両社で**配信日が近い**（例: 30日以内の差）ものだけか。
2. 「一ヶ月」の基準: 配信日基準か、Botが検知した日基準か（本案は検知日基準で、
   掲載遅れの影響を受けにくい）。
3. 専用チャンネルを新規にするか、既存2チャンネルに載せるか（本案は専用）。
4. 通知にリンクを載せるか。
5. 初回導入時に、既に両社にある分を一度通知するか（`init-read` で黙らせるか）。
6. フェーズ1（完全一致のみ）で始めてよいか。
