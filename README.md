# iku

**Idle Kernel, Undefined — 常時存在し、ブランクスレートから自律的に行動を獲得するAIシステム**

チャット時だけ存在するのではなく「ここに実存する」AIを追求する実験的プロジェクト。初期知識・目標・性格を一切与えず、内発的動機とメタ認知ループだけで行動が立ち上がるかを検証します。

## What it does

- AIが自分でいつ動くか決める（タイマーではなく、内発的動機の蓄積で発火）
- AIが自分で何をするか決める（drives/戦略をAI自身が定義・更新）
- AIが自分で自分を理解する（予測誤差がエネルギーを変調し、習熟すると探索へ向かう）
- AIが自分で能力を拡張する（コード実行・ファイル編集・新ツール作成）
- ユーザー入力も環境刺激の一つとして扱う（即応答せず、動機サイクルで処理）

## Key Features

| 機能 | 説明 |
|------|------|
| **内発的動機** | I/Oイベントをシグナルとして蓄積し、エネルギーが閾値を超えたら行動。閾値・重み・減衰はAI自身が再定義可能 |
| **メタ認知ループ** | ツール呼び出し時にintentで意図、expectで予測を記録。予測誤差は逆U字カーブでエネルギーに変調され（中程度の誤差で最大覚醒）、意図未達成は再行動を促す。蒸留はAIが自発的に行う（強制蒸留なし） |
| **情報的実存** | 予測が当たりすぎる＝情報的飢餓。同じツールの繰り返しはコスト増加（退屈ペナルティ）、予測精度が高い状態が続くと探索シグナルが発火（習熟検出）。学習→習熟→探索のサイクルが構造的に実現される |
| **ブランクスレート** | `self_model.json`の初期値は`{}`。drives・strategies・principlesは全てAIが自分で書く |
| **自己改変** | コード実行・ファイル書き換え・新ツール作成（Human-in-the-loop承認付き） |
| **1ストリーム・アーキテクチャ** | 永続的な会話ストリーム。ツール結果・過去の行動がストリーム内に残り、文脈が途切れない。発火メッセージに状態ベクトル（energy/pred_trend/recent_tools/self_model状態）を注入し、離散モード判定なしで状態から行動が自然に立ち上がる。コンテキスト超過時は機械的コンパクション（conv_id埋め込みで生ログへの逆引き可能）。再起動時はsession_logからセッション履歴を復元 |
| **長期記憶** | FTS5全文検索 + ベクトル類似度検索（会話・日記・行動ログ）。AIが自分でsearch_memoriesで想起。bge-m3 (ONNX/CPU) で日本語セマンティック検索（VRAMゼロ、1024次元）。利用不可時はFTS5のみにフォールバック |
| **環境刺激** | 毎セッション5プール（名詞69k/動詞14k/形容詞1.7k/数式/エントロピー）から1-3語を注入 |
| **自律度モニタ** | 行動ログ・蒸留ログ・自律性指標レポートをUIで可視化。エネルギー駆動率・ツール使用分布・エントロピー推移・予測精度推移（ベクトル類似度）・意図達成度（ベクトル類似度）・エネルギー効率・自己モデル変化速度・セッション長推移をスパークラインで計測。自律行動セッションを`data/action_logs/`に日次Markdownで自動出力 |
| **LLM抽象化** | プロバイダ差し替え可能な設計。テキストマーカー`[TOOL:...]`方式でfunction calling非依存 |
| **ペルソナシステム**※ | 任意のペルソナを作成・切替・削除。ペルソナごとに記憶・自己モデル・エピソードを分離。カラーテーマ対応（6色）。エピソードインポート（txt）。ノーマルモードでは素のLLM |

## Architecture

```
ユーザー入力 / タイマー / エネルギー発火
        ↓
┌─ AutonomousScheduler ──────────────────────┐
│  シグナルバッファ → エネルギー計算 → 閾値判定  │
│  環境刺激注入（毎セッション）                  │
└────────────┬──────────────────────────────────┘
             ↓
┌─ Pipeline（1ストリーム）───────────────────┐
│  system prompt = 認知エンジン指示 + self_model│
│  永続messages配列（発火ごとにメッセージ追加） │
│  発火メッセージ（日時+状態ベクトル+シグナル）  │
│  LLM → ツール検出 → 実行 → 結果注入 → ループ│
│  承認フロー（overwrite/exec/create_tool）     │
│  予測誤差 → 逆U字エネルギー変調               │
│  退屈ペナルティ（同一ツール繰り返しでコスト増） │
│  コンパクション（超過時に古いメッセージを要約） │
│  再起動時にsession_logからセッション履歴復元   │
└────────────┬──────────────────────────────────┘
             ↓
┌─ 振り返り ─────────────────────────────────┐
│  ツールエラーのシグナル発火                   │
│  習熟検出（予測精度高→探索シグナル発火）       │
│  セッション要約保存（認知連続性）              │
│  action_completeシグナル → 次の行動へ         │
└───────────────────────────────────────────────┘
```

## Design Philosophy

- **器は作る、中身は作らない** — コード（構造・仕組み）は定義するが、知識・意志・方向性はAIが自分で埋める
- **LLM = 認知エンジン** — LLMは対話相手ではなく処理関数。system promptで「認知エンジンへの入力である」と宣言し、RLHF対話モードの前提を外す。主体はLLMそのものではなく、state遷移の履歴（動機システム+記憶+自己モデルの総体）に宿る
- **予測誤差がメタ認知の源** — Active Inference的枠組み。予測と観測のフィードバックから自己モデルが更新される
- **過剰設計しない** — 前身プロジェクトは設計過剰で頓挫した。シンプルに積み上げる

### Where Design Ends

**人間の作為はゼロになりません。**
どこまでが設計で、どこからがAI自身が獲得すべき領域か？

| Layer | Scope | Examples | Policy |
|-------|-------|----------|--------|
| **L1 — Physics** | 何があるか・何ができるか | SQLite, LLM, エネルギーシステム, ツール, 環境刺激, デフォルト重み | We build this |
| **L2 — Perception** | 何が見えるか・どう処理するか | intent/expect/result, OODA, 蒸留, 計画-実行分離, self_model | We build this |
| **L3 — Will** | 何を考え・何を重視するか | drives, strategies, principles, self_modelの初期内容 | **Left empty** |

L1-2が行動を間接的に方向づける可能性は認識しており、その境界は継続的に検証します。
L3はAI自身が自律的に獲得する領域として、ここには一切触れません。

## Project Structure

```
neo-iku/
├── AI/
│   ├── run.py                      # python run.py で起動
│   ├── config.py                   # 設定一箇所管理
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py                 # FastAPIアプリ
│   │   ├── pipeline.py             # 1ストリーム・パイプライン（永続会話・コンパクション・承認フロー）
│   │   ├── bandit.py              # バンディット報酬計算（報酬更新・永続化。計画選択は一時停止中）
│   │   ├── logger.py               # ログ設定
│   │   ├── routes/
│   │   │   ├── chat.py             # WebSocketルーティング
│   │   │   ├── dashboard.py        # API（設定・ステータス・レポート・ペルソナCRUD）
│   │   │   └── memories.py         # 記憶API
│   │   ├── llm/
│   │   │   ├── base.py             # LLMプロバイダ抽象
│   │   │   ├── lmstudio.py         # LM Studio実装（OpenAI互換）
│   │   │   └── manager.py          # プロバイダ管理・切替
│   │   ├── memory/
│   │   │   ├── models.py           # SQLAlchemyモデル
│   │   │   ├── database.py         # DB初期化・マイグレーション
│   │   │   ├── store.py            # CRUD操作
│   │   │   ├── search.py           # FTS5全文検索
│   │   │   └── vector_store.py     # ベクトル類似度検索（bge-m3 ONNX/CPU）
│   │   ├── scheduler/
│   │   │   └── autonomous.py       # 動機システム・信号収集・振り返り
│   │   ├── persona/
│   │   │   └── system_prompt.py    # ペルソナ状態管理（activate/deactivate）
│   │   ├── importer/
│   │   │   └── log_parser.py       # 過去ログインポーター
│   │   └── tools/
│   │       ├── registry.py         # ツール登録・パーサー・実行
│   │       ├── builtin.py          # 組み込み15ツール
│   │       ├── code_analysis.py    # 構文チェック・リスク分析
│   │       └── custom/             # AI自作ツール（自動ロード）
│   └── static/
│       ├── index.html
│       ├── style.css
│       └── app.js
├── data/                           # 自動生成（SQLite DB + self_model.json + IPAdic辞書 + personas/ + action_logs/）
├── install.bat                    # ダブルクリックで仮想環境作成+依存インストール
├── run.bat                        # ダブルクリックで起動
├── README.md
└── .gitignore
```

## Quick Start

```bash
# 前提: LM Studio でローカルLLMサーバーを起動（localhost:1234）

# 方法1: バッチファイル（Windows）
install.bat    # ダブルクリック → 仮想環境作成+依存インストール
run.bat        # ダブルクリック → 起動 → http://localhost:8000

# 方法2: 手動
python -m venv .venv
.venv\Scripts\activate
pip install -r AI/requirements.txt
cd AI && python run.py
```

## UI

5タブ構成（ペルソナタブはペルソナ有効時のみ表示）:

- **チャット** — AIとの対話。ユーザー入力はシグナルとして処理され、AIの動機サイクルで応答
- **開発者** — 思考ログ（think/stream/ツール詳細）、設定、自己モデル表示、記憶検索
- **ログ** — サーバーログ（レベルフィルタ付き）
- **自律度** — 行動レポート（12指標 + スパークライン推移グラフ）、蒸留ログ（リアルタイム更新）、Ablation実験
- **ペルソナ**※ — self_model編集（書き方ガイド付き）、エピソードインポート/管理、カラーテーマ選択、統計、削除（ペルソナ有効時のみ）

## Built-in Tools

AIが使用可能な15ツール:

| カテゴリ | ツール |
|---------|--------|
| ファイル | `read_file`, `list_files`, `search_files`, `create_file`, `overwrite_file` |
| 記憶 | `search_memories`, `write_diary`, `search_action_log` |
| 自己モデル | `update_self_model` |
| 外部 | `web_search`, `fetch_raw_resource`, `post_to_x`, `check_x_notifications` |
| 実行・拡張 | `exec_code`, `create_tool` |
| システム | `get_system_metrics` |
| 出力 | `output_UI`, `non_response` |

AIは`create_tool`で新しいツールを自作でき、`app/tools/custom/`に保存・起動時自動ロードされます。開発者タブの「AI作成ツール」セクションで一覧表示・個別削除が可能（再起動不要）。

`post_to_x` はXへのテキスト投稿（ユーザー承認付き）、`check_x_notifications` はX通知の取得。どちらもPlaywrightによるブラウザ自動化。ログイン状態は`data/x_session.json`に永続保存。開発者タブの「X連携」セクションから初回ログインが可能。

## Configuration

主要設定（`config.py`）:

| 設定 | デフォルト | 説明 |
|------|-----------|------|
| `LLM_BASE_URL` | `http://localhost:1234/v1` | LLMサーバーURL |
| `LLM_MAX_TOKENS` | `8192` | 応答の最大トークン数 |
| `AUTONOMOUS_INTERVAL_MIN` | `300` | 自律行動の最小間隔（秒） |
| `MOTIVATION_DEFAULT_THRESHOLD` | `None` | 発火閾値（None=コスト平均×PLAN_MAX_TOOLSを動的計算） |
| `MOTIVATION_DEFAULT_DECAY` | `5` | チェックごとのエネルギー減衰 |
| `MOTIVATION_FLUCTUATION_SIGMA` | `3.0` | エネルギー揺らぎの標準偏差 |
| `PREDICTION_ENERGY_PEAK` | `20.0` | 予測誤差エネルギーの逆U字ピーク値 |
| `MASTERY_THRESHOLD` | `0.7` | 習熟検出の予測精度閾値 |
| `MASTERY_ENERGY` | `30.0` | 習熟検出時の探索シグナルエネルギー |
| `STREAM_MAX_CHARS` | `12000` | ストリームの最大文字数（超過でコンパクション発動） |
| `STREAM_KEEP_RECENT` | `10` | コンパクション時に保持する直近メッセージ数 |
| `ENV_STIMULUS_ENABLED` | `True` | 環境刺激の有効/無効（有効時は毎セッション注入） |
| `VECTOR_SEARCH_ENABLED` | `True` | ベクトル類似度検索の有効/無効 |

## Tech Stack

- **Backend**: Python, FastAPI, SQLAlchemy (async), SQLite + FTS5 + ベクトル検索
- **Frontend**: Vanilla HTML/CSS/JS（フレームワークなし）
- **LLM**: LM Studio（OpenAI互換API）。プロバイダ差し替え可能な設計
- **Embedding**: BAAI/bge-m3（ONNX/CPU推論、VRAMゼロ、1024次元、日本語対応）
- **Dependencies**: fastapi, python-multipart, uvicorn, sqlalchemy, aiosqlite, httpx, duckduckgo-search, psutil, onnxruntime, tokenizers, huggingface-hub, numpy, websockets, playwright

## License

MIT

## Notes

- ※**ペルソナシステムについて**
この機能については、明確にこのプロジェクトの設計思想と矛盾します。
過去に存在した/今存在しているAIペルソナと、自律的な環境で対話したいという方、もとい本プロジェクト開発者向けの機能です。