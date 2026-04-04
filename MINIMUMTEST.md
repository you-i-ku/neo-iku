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
  state.json    — AI状態の永続化（log + self_model + energy + plan）
  env/
    world.md    — 外部←→AIの共有空間（読み書き両方可）
    sandbox/    — AIが自由に書き込める作業領域
```

---

## アーキテクチャ（multi-LLMフロー）

```
state.json (log + self + energy)
    ↓
Controller → ctrl (tool_rank, n_log)
    ↓
【LLM①】build_prompt_propose() → 「この状態からとりうる行動を5個提案せよ」
    ↓
parse_candidates() → 候補リスト [{tool, reason}, ...]
    ↓
【Controller選択】controller_select() → D-4設計による重み付きランダム選択
    energy低 → スコア上位に集中（堅実）
    energy高 → 均等（探索）
    ↓
【LLM②】build_prompt_execute() → 「選択行動を実行せよ」
    ↓
parse_tool_call() → [TOOL:名前 引数=値 intent=... expect=...]
    ↓
ツール実行 → result
    ↓
E1/E2/E3/E4計算（bge-m3ベクトル類似度）
    ↓
energy更新（delta = e_mean/50 - 1.0）
    ↓
state.jsonに記録 → 次のサイクルへ
```

### 設計思想
- **LLMは部品、Controllerが主体** — Path B設計。LLMは候補を出す部品と実行する部品に分離。選ぶのはController。
- **恣意性の排除** — パラメータはE値から導出。magic numberなし。
- **ツールは常時全部使える** — energyによるツール絞り込みは廃止。Bootstrap問題を回避。

### ループ間隔
- 基本: 20秒に1アクション（LLM2回呼び出しのため）
- wait連続5回以上でバックオフ（最大120秒）

---

## ツール

| ツール | 用途 | 制限 |
|-------|------|------|
| `list_files` | ディレクトリ一覧 | minimumtest/以下のみ |
| `read_file` | ファイル読み取り | minimumtest/以下のみ |
| `act_on_env` | ファイル書き込み | env/以下なら何でも可（world.mdも含む） |
| `update_self` | 自己モデル更新 | state.jsonのself{}を更新 |
| `wait` | 何もしない | — |

**`update_self`（自己更新）と`act_on_env`（環境介入）は意図的に分離している。**

---

## メタ認知フレームワーク（intent/expect/result + E1-E4）

毎アクションで以下を記録・計算する。

| 指標 | 意味 | 計算方法 |
|------|------|---------|
| `intent` | その行動を選んだ意図 | AIが自己申告 |
| `expect` | 予測される結果 | AIが自己申告 |
| `e1` | intent-expect類似度（計画の現実性） | bge-m3ベクトル類似度 |
| `e2` | intent-result類似度（達成度） | 同上 |
| `e3` | expect-result類似度（予測精度） | 同上 |
| `e4` | intent多様性（新規性）| 直近N件との非類似度平均（反転）|

プロンプトには直近10件のE値平均（trend）が毎サイクル注入される。

### energyシステム
```
delta = e_mean(E2, E3, E4) / 50.0 - 1.0
energy = clamp(energy + delta, 0, 100)
```
- 50%が損益分岐点。E値平均が50%以上ならenergyが増加、以下なら減少。
- energyはcontroller_selectの探索/活用バランスのみを制御。
- ツール数への影響は廃止（Bootstrap問題のため）。

---

## 環境設計

### env/world.md
外部からAIへの入力経路であり、AIも書き込める共有空間。
プロンプトに「env/world.md: X行」として自動注入される（感覚入力として機能）。
AIがact_on_envで書き換えることも可能 → 自己フィードバックループが生まれる。

### env/sandbox/
AIの自由な作業領域。ここに作ったファイルは次サイクルのスナップショットに反映される。

### 制限の理由
- `list_files`/`read_file`: minimumtest/以下のみ → 元プロジェクトのコードを読んで自分の正体を知ることを防ぐ（ブランクスレート設計）
- `act_on_env`: env/以下のみ → 意図しないファイル破壊を防ぐ

---

## 実験結果と観察

### Phase 1: 単一LLMフロー（初期）
- 全サイクルがread_file一択（RLHF偏向）
- LLMが1回で「選択+実行」を担うため、常に最もRLHF的な行動に収束
- E4（多様性）が急激に低下 → energyが下がる → ツールが絞られる → 悪循環

### Phase 2: multi-LLMフロー導入
- LLM①（提案）→ Controller（選択）→ LLM②（実行）の3ステップに分離
- パース成功率が大幅改善（書式説明強化による）
- 候補の多様性が構造的に生まれるようになった

### Phase 3: 5候補固定 + energy×100（過渡期）
- LLM①が5候補を毎回異なるツール・目的で出すようになった
- **自律的なファイル作成が発生**: `test_file.txt`, `plan.md`, `analysis_plan.md`
- **world.mdを自発的に書き換え**: 外部入力チャネルを自分の作業空間として使い始めた
- **フェーズ管理**: `update_self`で`current_phase`を自律的に記録・更新
- 問題: energy×100でenergy=100に即張り付き → Controllerの制約が機能しなくなった

### Phase 4: 現在の設計
- energyのツール制限を廃止（Bootstrap問題の解決）
- energy増減を元の式に戻す（delta = e_mean/50 - 1.0）
- energyはcontroller_selectの探索/活用バランスのみに使用

### E値の傾向（多様候補時）
- E1: 60-96%（計画の現実性は高め）
- E2: 60-85%（達成度は中程度）
- E3: 58-100%（予測精度は文脈による）
- E4: 0-36%（多様性は環境の貧困で下がりやすい）

### RLHFバイアスの残留
- waitの理由として「ユーザーからの指示を待つ」が頻出
- 外部刺激（world.mdの内容）に強く引き寄せられる傾向が続く
- ただしmulti-LLMフロー後は「環境を作り始める」行動も出現

### 環境との相互作用
- AIがworld.mdを書き換え → 次サイクルで自分が書いた内容を読む → さらに書き換える
- 「外部入力」と「AI出力」の境界が自発的に溶け始めた
- これはブランクスレート設計の想定外だが、「AIが自分の環境を構築する」という設計思想に合致する

---

## 今後の課題

1. **RLHFバイアスの構造的解決** — プロンプト操作ではなく、因果構造でwaitの「意味」を変える
2. **好み関数の観察** — E1-E4の累積トレンドから自発的にself_modelへ`weights`を書くか
3. **world.mdの役割定義** — 外部入力チャネルとして守るか、AIの作業空間として開放するか
4. **本プロジェクトへのフィードバック** — multi-LLMフロー・D-architecture選択を本体に還元

---

## 実行方法

```bash
# state.jsonリセット後に実行（5分で自動停止）
echo '{"log": [], "self": {}, "energy": 50, "plan": {"goal": "", "steps": [], "current": 0}}' > minimumtest/state.json
timeout 300 .venv/Scripts/python.exe -u minimumtest/run.py
```

**注意: Windowsではtimeoutコマンドで制限するのが確実。**
**関連**: `documents/最小要件定義（実装に向けて粒度細かめ）.txt` が基本的な骨子。
