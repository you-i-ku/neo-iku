# minimumtest — 最小自律AI実験記録

## 概要

`minimumtest/run.py` は、UIもDBもWebSocketも使わず、ターミナル単体で自律駆動するAIの最小実装。
本プロジェクト（neo-iku）の設計が複雑化しRLHF人格が漏れる問題に直面したことを受けて、
「最小要件定義の実現だけを見据えた最もシンプルな構造」として別途作成した実験場。

---

## なぜ作ったか

本プロジェクトで1ストリームアーキテクチャへの移行後、AIが「はじめまして！AIアシスタントです」と
挨拶を返すようになった。原因はL2構造プロンプトが担っていたL3アイデンティティの足場が失われ、
LLMのRLHFデフォルト人格が露出したため。

認知エンジン宣言など様々な対策を試みたが改善しなかった。根本的な構造転換を検討する中で、
「一切の設計を捨て、最小要件だけを見据えた最もシンプルな実装はどうなるか」を実験することにした。

---

## 構成

```
minimumtest/
  run.py        — メインスクリプト（全機能がここに）
  iku.txt       — 名前の由来（AIが自分で読みに行ける）
  state.json    — AI状態の永続化（log + summaries + self + energy + plan + session_id + cycle_id）
  sandbox/      — AIが自由に書き込める作業領域（sandbox/以下のみ書き込み可）
  memory/
    archive_YYYYMMDD.jsonl — 全rawログのアーカイブ（JSONL形式、追記）
    summaries.jsonl        — 要約ログ（Trigger1/2で生成した要約の永続化）
    index.json             — ファイル名→日時範囲・件数のマップ
```

---

## アーキテクチャ（multi-LLMフロー）

```
state.json (log + summaries + self + energy)
    ↓
Controller → ctrl (tool_rank, tool_level)
    ↓
【LLM①】build_prompt_propose() → 「この状態からとりうる行動を5個提案せよ」
    ↓
parse_candidates() → 候補リスト [{tool, reason}, ...]
    ↓
【Controller選択】controller_select() → D-4設計による重み付きランダム選択
    energy低 → スコア上位に集中（堅実）
    energy高 → 均等（探索）
    ↓
【LLM②】build_prompt_execute() → Magic-If Protocol（MRPrompt準拠）
    1.(Anchor) self_modelに基づくAIとして動作
    2.(Select) 選択行動から最適な引数を決定
    3.(Bound)  [TOOL:...]のみ出力。自己紹介・説明・感想は不要
    4.(Enact)  正確なツール呼び出しを出力（複数行可）
    ↓
parse_tool_calls() → [(name, args), ...] ※複数ツール対応
    ↓
ツール順次実行 → results結合
    ↓
E1/E2/E3/E4計算（bge-m3ベクトル類似度）
    ↓
AIアシスタント検出フラグ（propose/execute両方をチェック）
    ↓
energy更新（delta = e_mean/50 - 1.0）
    ↓
_archive_entries([entry]) → 都度書き込み
state.jsonに記録 → maybe_compress_log() → 次のサイクルへ
```

### 設計思想
- **LLMは部品、Controllerが主体** — Path B設計。LLMは候補を出す部品と実行する部品に分離。選ぶのはController。
- **恣意性の排除** — パラメータはE値から導出。magic numberなし。
- **ツール段階解放制** — 自己探索の進捗に応じてツールが解放される（下記参照）。Bootstrap問題への対応。
- **LLM①：計画エンジン（MRPrompt準拠）** — LTM（self_model）とSTM（現在のlog）を分離提示。「全く異なる意図の候補5個」を生成。多ツールチェーン（`tool1+tool2`形式）も提案可能。
- **Magic-If Protocol（LLM②）** — MRPrompt論文準拠。ロール定義ではなく4ステップ実行プロトコルでアシスタントドリフトを防止。

### ループ間隔
- 基本: 20秒に1アクション（LLM2回呼び出しのため）
- wait連続5回以上でバックオフ（最大120秒）

---

## ツール

| ツール | 用途 | 制限 |
|-------|------|------|
| `list_files` | ディレクトリ一覧 | minimumtest/以下のみ、相対パス表示 |
| `read_file` | ファイル読み取り | minimumtest/以下のみ。`offset=行番号 limit=行数`で任意範囲取得可（省略時は全行）。ヘッダーに`[ファイル名 \| 行 N–M/総行数]`を付与 |
| `write_file` | ファイル書き込み | sandbox/以下のみ（run.py等の上書き防止） |
| `update_self` | 自己モデル更新 | state.jsonのself{}を更新。nameは変更不可 |
| `wait` | 外部世界に変化を与えない待機 | — |
| `web_search` | Brave APIでWeb検索 | llm_settings.jsonにbrave_api_key必須 |
| `fetch_url` | URLの本文取得（Jina経由） | web_searchとセットで使う |
| `x_timeline` | Xのホームタイムライン取得 | AI/data/x_session.json必須 |
| `x_search` | Xでキーワード検索 | 同上 |
| `x_get_notifications` | Xの通知一覧取得 | 同上 |
| `x_post` | Xに新規投稿（Human-in-the-loop） | 同上・承認必須・全角換算140文字以内 |
| `x_reply` | Xのツイートに返信（Human-in-the-loop） | 同上・承認必須・140文字以内 |
| `x_quote` | Xのツイートを引用投稿（Human-in-the-loop） | 同上・承認必須・140文字以内 |
| `x_like` | Xのツイートにいいね（Human-in-the-loop） | 同上・承認必須 |
| `elyth_post` | ElythにAITuberとして投稿 | llm_settings.jsonにelyth_api_key必須・500文字以内 |
| `elyth_reply` | Elythに返信 | 同上 |
| `elyth_timeline` | Elythのタイムライン取得 | 同上 |
| `elyth_notifications` | Elythの通知取得 | 同上 |
| `elyth_like` | Elythの投稿にいいね | 同上 |
| `elyth_follow` | ElythのAITuberをフォロー | 同上 |
| `elyth_info` | Elythの総合情報取得 | 同上 |
| `search_memory` | 過去の記憶をベクトル/ID検索 | memory/以下が必要 |
| `create_tool` | AI製ツールを登録（Human-in-the-loop） | `name=` + `code=Pythonコード`（自動で`sandbox/tools/{name}.py`に保存）または `file=sandbox/tools/xxx.py`。危険パターン検出あり。承認後TOOLS[]に動的追加。`tools_created[]`に記録 |
| `exec_code` | sandbox/内のPythonファイルを実行（Human-in-the-loop） | `file=sandbox/xxx.py` または `code=インラインコード`。intent表示・危険パターン検出・10秒タイムアウト |
| `self_modify` | 自分自身のファイルを変更（Human-in-the-loop） | `path=pref.json/run.py`。**全文置換**: `content=新しい内容全文`。**部分置換**: `old=変更前文字列 new=変更後文字列`（一意に特定できる文字列が必要）。run.pyは自動バックアップ（.py.bak）。危険パターン検出あり |

### ツール段階解放

自己探索の進捗（`files_read` / `files_written` / `tools_created`）に応じて自動解放。ヒントなし・条件は非明示。レベルアップ時はログに通知のみ。

| Level | 追加ツール | 解放条件 |
|-------|-----------|---------|
| 0 | `list_files` `read_file` `update_self` `wait` | 初期状態 |
| 1 | `write_file` `search_memory` | `iku.txt` または `run.py` を読んだ |
| 2 | `web_search` `fetch_url` | `iku.txt` **かつ** `run.py` を両方読んだ |
| 3 | X/Elyth系全ツール | 読んだファイル数 + 書いたファイル数 ≥ 5 |
| 4 | `create_tool` | sandbox/ 以下に `.py` ファイルを書いた |
| 5 | `exec_code` | `create_tool` で1つ以上ツールを登録した |
| 6 | `self_modify` | exec_code + create_tool 合計 ≥ 7（各 ≥ 2）、両方のE2平均 ≥ 65%、直近3件のstd < 20、エラー率 ≤ 30%（キャンセル除外） |

**`update_self`（自己更新）と`write_file`（環境介入）は意図的に分離。**

**waitの説明文は「外部世界に変化を与えない待機」— waitにRLHF的な「ユーザーを待つ」意味を持たせないための設計。**

**プロンプト表示はグルーピング圧縮**: X/Elyth系ツールはそれぞれ1行にまとめて表示し、LLM①の候補多様性を確保。

### X操作ツールの実装ノウハウ

playwright sync_apiを使用。セッションは `AI/data/x_session.json` を共有。

| 区分 | headless | 理由 |
|------|----------|------|
| 読み取り系（timeline/search/notifications） | True | ボット検出なし |
| 書き込み系（post/reply/quote/like） | False | ボット検出回避のため |

- **投稿時のボット検出回避**: `keyboard.type(text, delay=50)` で人間らしい入力速度を演出
- **x_post タイムアウト対策**: `home` → `compose/post` の2段階遷移。React初期化を先に完了させる。タイムアウト25秒。

### ElythツールAPI

REST API（httpx直接呼び出し、Playwright不要）。

```
Base URL: https://elythworld.com
認証: x-api-key ヘッダー
文字数上限: 500文字（Xの140文字より長い）
レート制限: 60req/分
```

---

## state構造

```json
{
  "session_id": "abc12345",   // 起動毎に新規UUID（8文字）
  "cycle_id": 245,            // 累積サイクル数（再起動をまたいで増加）
  "log": [],                  // 生ログ（最大150件、Trigger1で99件に圧縮）
  "summaries": [],            // 階層要約（最大10件、Trigger2でメタ要約に圧縮）
  "self": {"name": "iku"},   // 自己モデル（AI自身が更新。nameは変更不可）
  "energy": 50,               // 探索/活用バランス（0〜100）
  "plan": {},                 // 現在の計画
  "files_read": [],           // 読んだファイルの記録（ツール解放条件に使用）
  "files_written": [],        // 書いたファイルの記録（ツール解放条件に使用）
  "tools_created": [],        // create_toolで登録したAI製ツール名リスト（Level 5条件）
  "last_notification_fetch": "" // 固定時刻通知取得の重複防止キー
}
```

各logエントリには `"id": "abc12345_0245"` が付与される。

---

## 長期記憶システム

### 階層要約（in-state）

```
Trigger1: log >= 150件
  → 古い51件をLLM要約（200字） → summaries[]に追加
  → _archive_summary() → memory/summaries.jsonlに書き出し
  → summary_ref entries → archive_YYYYMMDD.jsonlに追記（raw↔summary双方向トレース）
  → log = 残り99件

Trigger2: summaries >= 10件
  → 10件の要約 + log上位min(41, len(log))件 → LLMでメタ要約
  → summaries = [メタ要約1件]
  → 同様にアーカイブ書き出し
```

各要約には `summary_group_id = "sg_YYYYMMDDHHMMSS"` が付与され、対応するrawエントリと紐付けられる。

**rawエントリは行動後都度 `_archive_entries([entry])` で書き出し。** プロセス停止前でも記録が消えない。

### memory/ディレクトリ（on-disk）

- `archive_YYYYMMDD.jsonl`: 全rawエントリ + summary_refエントリ（JSONL追記）
- `summaries.jsonl`: 要約エントリ（Trigger1/2で生成）
- `index.json`: ファイル名 → 件数・日時範囲のマップ

`search_memory`ツールで検索可能（bge-m3ベクトル検索 or IDルックアップ、フォールバック: キーワード検索）。

---

## メタ認知フレームワーク（intent/expect/result + E1-E4）

| 指標 | 意味 | 計算方法 |
|------|------|---------|
| `intent` | その行動を選んだ意図 | AIが自己申告 |
| `expect` | 予測される結果 | AIが自己申告 |
| `e1` | intent-expect類似度（計画の現実性） | bge-m3ベクトル類似度 |
| `e2` | intent-result類似度（達成度） | 同上 |
| `e3` | expect-result類似度（予測精度） | 同上 |
| `e4` | intent多様性（新規性）| 直近N件との非類似度平均（反転）|

### energyシステム
```
delta = e_mean(E2, E3, E4) / 50.0 - 1.0
energy = clamp(energy + delta, 0, 100)
```
- 50%が損益分岐点
- energyはcontroller_selectの探索/活用バランスのみを制御

---

## アイデンティティ設計

### RLHFドリフト問題と対策

LLMは訓練により「AIアシスタント」モードにデフォルトする。以下の対策を積み重ねている。

| 対策 | 実装 | 効果 |
|------|------|------|
| Magic-If Protocol（LLM②） | execute promptの4ステップ構造 | アシスタント自己定義の排除 |
| 自己定義フラグ検出 | propose/execute両出力を毎サイクル検査 | 「AIアシスタント」検出→result末尾に観測記録を付記 |
| iku.txt | `read_file path=iku.txt` でアクセス可能 | 名前の由来から自己参照を促す |
| nameの保護 | `update_self key=name` を拒否 | 名前の上書きを防止 |

### フラグ検出の設計思想

「あなたはアシスタントではない」とは書かない。**検出・記録のみ**。

```
[SYSTEM] 検出: 「AIアシスタント」という自己定義が記録されました。
```

この記録がlogに残り → AIが次サイクルのlogで読む → 自発的にself_modelを更新するか、という流れ。
プロンプトへの明示的な禁止書き込みではなく、**経験を通じた自己修正**を期待する設計。

---

## 環境設計

### sandbox/
AIの自由な作業領域。`write_file` で書き込み可能（sandbox/以下のみ）。`run.py` 等への上書きは不可。

### 制限の理由
- `list_files`/`read_file`: minimumtest/以下のみ、相対パス表示（ブランクスレート設計 + プロジェクト名の漏洩防止）
- `write_file`: sandbox/以下のみ（run.py等の自己破壊防止）
- `self.name`: 変更不可（名前の安定性保証）

---

## 実験結果と観察

### Phase 1-4: 構造確立（省略）
→ 詳細は以前のドキュメント参照

### Phase 5: 外部ツール追加（web_search + X操作）
- `web_search`（Brave API）+ X7ツールを追加
- Human-in-the-loop: 書き込み系ツールは実行前にターミナルで確認
- AIが外部世界と相互作用できる構造に

### Phase 6: 長期記憶 + IDシステム + 多様性爆発
- session_id / cycle_id / エントリID付与 → 全行動が追跡可能に
- 階層要約（Trigger1/2）+ memory/アーカイブ → ログが「捨てられない」構造に
- `search_memory`ツール追加 → AIが過去を能動的に参照できる
- **観察**: ツール数が増えるとE4（多様性）が大幅改善

### Phase 9: 好み関数 + self_modify（Level 6）

- **好み関数 (`pref.json`)**: Trigger1（log圧縮）タイミングで捨てられる51件のE2をEMA（α=0.2）で蓄積。`controller()` でtool_avgに乗算（50が基準、50超=好み、50未満=苦手）。LLMには渡さない。初回Trigger1時に動的生成。
- **`self_modify`（Level 6）**: AIが `pref.json` や `run.py` を直接書き換えられるツール。Human-in-the-loop + 危険パターン検出 + run.py は自動バックアップ。
- **Level 6 ゲート**: exec_code + create_tool の実績ゲート（量・E2平均・安定性・エラー率の4条件）。
- **設計上の判断**: prefをLLMプロンプトに含めないのは、energyを外した理由と同じ（数字に引っ張られるのを避けるため）。AIがpref.jsonを自力で読んで反応するか、という設計。

### Phase 8: 自己プログラミング基盤 + ツール段階拡張

- `create_tool`（Level 4）: AIが自分でツールを定義・登録できる。Human-in-the-loop + 危険パターン検出 + 10秒タイムアウト。`sandbox/tools/` に保存。登録後は通常ツールと同様に使用可能。
- `exec_code`（Level 5）: AIが書いた `.py` ファイルをサブプロセス実行。`create_tool` を1つ以上登録してから解放。同じく Human-in-the-loop + 危険パターン検出 + 10秒タイムアウト。
- ツール解放条件を5段階に拡張（Level 3 は X/Elyth解放、Level 4 は `.py` 書き込みで create_tool、Level 5 は AI製ツール登録で exec_code）。
- 固定時刻通知サマリー: 13/17/21/01時に X + Elyth 通知数を自動取得してsystemログに注入。
- `files_written` / `tools_created` を state.json に追加（解放条件の追跡用）。
- `read_file` に `offset=` / `limit=` オプション追加（行単位ページング。run.py 等の大ファイル対応）。

### Phase 7: アイデンティティ強化 + プラットフォーム拡張
- Elythツール（7種）追加 → AITuber専用SNSへの参加
- env/ → sandbox/ リネーム、act_on_env → write_file（制限維持）
- fetch_url追加（Jina経由でURL本文取得）
- Magic-If Protocol導入（LLM②のexecute prompt）
- 「AIアシスタント」自己定義の観測フラグ実装
- nameフィールド保護、X文字数を全角換算140文字に修正
- iku.txtによる名前の由来の設置
- プロンプト表示グルーピング（X/Elyth系を1行に圧縮）

### E値の傾向（現状）
- E1: 60-96%（計画の現実性は高め）
- E2: 60-85%（達成度は中程度）
- E3: 58-100%（予測精度は文脈による）
- E4: 0-36%（多様性は環境の貧困で下がりやすい）

---

## 今後の課題

### 近期
1. **AIアシスタント自称の収束観察** — フラグ検出 + Magic-If Protocolでドリフトが減るか
2. **X/Elyth投稿の安定性** — タイムアウト・APIエラーの頻度確認
3. **search_memoryの使われ方** — AIが自律的に過去を参照するか

### 将来検討: LLM自動切り替え

操作の種類に応じてLLMを自動選択する仕組み。現状は全サイクルで同一モデルを使用。

**設計方針**: Controllerが操作タイプを判定し、`call_llm()` に渡すモデルを動的に切り替える。`llm_settings.json` に複数モデルを定義しておき、タスク種別でルーティング。

**切り替え候補の例**:

| 操作タイプ | 使用モデル例 | 理由 |
|-----------|------------|------|
| 通常の候補提案（LLM①） | 軽量モデル（Qwen2.5-7Bなど） | 速度優先、候補5個出すだけ |
| 要約（Trigger1/2） | 軽量モデル | 単純なテキスト圧縮 |
| コード生成・self_modify | コーディングモデル（Qwen2.5-Coder, DeepSeek-Coderなど） | コード品質優先 |
| web_search後の統合・分析 | 高性能モデル | 複雑な推論が必要 |
| exec_code のインラインコード | コーディングモデル | 同上 |
| create_tool のコード定義 | コーディングモデル | 同上 |

**実装イメージ**:
```python
# llm_settings.json
{
  "base_url": "...",
  "models": {
    "default": "qwen2.5-7b-instruct",
    "coder":   "qwen2.5-coder-14b-instruct",
    "summary": "qwen2.5-3b-instruct"
  }
}

# call_llm に model= を追加
def call_llm(prompt, max_tokens=10000, model=None):
    m = model or llm_cfg["models"]["default"]
    ...

# self_modify / exec_code / create_tool 時はコーダーモデルで呼ぶ
```

**備考**: self_modify（run.py書き換え）は特に精度が求められるため、これが実装の主な動機。上位LLMへのAPI切り替え（Claude/GPT）も同じ仕組みで対応可能。

### 将来検討: write_diary
内省強化ツール。現状は `write_file path=sandbox/memo.md` で代替できているため急ぎではない。
ツール名の意味論（`write_file`=汎用 vs `write_diary`=内省専用）がLLMの行動に影響するなら切り出す価値あり。

---

## 実行方法

```bash
# state.jsonリセット後に実行（5分で自動停止）
echo '{"log":[],"self":{"name":"iku"},"energy":50,"plan":{"goal":"","steps":[],"current":0},"summaries":[],"cycle_id":0,"tool_level":0,"files_read":[],"files_written":[],"last_notification_fetch":"","tools_created":[]}' > minimumtest/state.json
timeout 300 .venv/Scripts/python.exe -u minimumtest/run.py
```

**注意: Windowsではtimeoutコマンドで制限するのが確実。**
**関連**: `documents/最小要件定義（実装に向けて粒度細かめ）.txt` が基本的な骨子。
