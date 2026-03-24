# neo-iku

**常時存在し、ブランクスレートから自律的に行動を獲得するAIシステム**

チャット時だけ存在するのではなく「ここに在る」AIを追求する実験的プロジェクト。初期知識・目標・性格を一切与えず、内発的動機とメタ認知ループだけで行動が立ち上がるかを検証する。

## What it does

- AIが自分でいつ動くか決める（タイマーではなく、内発的動機の蓄積で発火）
- AIが自分で何をするか決める（drives/戦略をAI自身が定義・更新）
- AIが自分で自分を理解する（予測→誤差→蒸留→自己モデル更新）
- AIが自分で能力を拡張する（コード実行・ファイル編集・新ツール作成）
- ユーザー入力も環境刺激の一つとして扱う（即応答せず、動機サイクルで処理）

## Key Features

| 機能 | 説明 |
|------|------|
| **内発的動機** | I/Oイベントをシグナルとして蓄積し、エネルギーが閾値を超えたら行動。閾値・重み・減衰はAI自身が再定義可能 |
| **メタ認知ループ** | 観測→方向付け→決定→行動→振り返り（OODA）。ツール呼び出し時にexpectで予測を記録し、結果との誤差から特性を蒸留 |
| **ブランクスレート** | `self_model.json`の初期値は`{}`。drives・strategies・principlesは全てAIが自分で書く |
| **自己改変** | コード実行・ファイル書き換え・新ツール作成（Human-in-the-loop承認付き） |
| **計画-実行分離** | 自律行動時はまずツール計画を立て、ツールごとに個別実行（結果を次に引き継ぎ） |
| **長期記憶** | FTS5全文検索（会話・日記・行動ログ）。AIが自分でsearch_memoriesで想起 |
| **環境刺激** | ランダムな語彙やノイズを確率的に注入。AIの自律性を奪わない揺らぎとして設計 |
| **自律度モニタ** | 行動ログ・蒸留ログ・自律性指標レポートをUIで可視化。エネルギー駆動率やツール使用分布を計測 |
| **LLM抽象化** | プロバイダ差し替え可能な設計。テキストマーカー`[TOOL:...]`方式でfunction calling非依存 |
| **ペルソナ分離** | 特定のペルソナを注入可能（記憶・自己モデルのレイヤー）。ノーマルモードでは素のLLM |

## Architecture

```
ユーザー入力 / タイマー / エネルギー発火
        ↓
┌─ AutonomousScheduler ──────────────────────┐
│  シグナルバッファ → エネルギー計算 → 閾値判定  │
│  環境刺激注入（確率的）                       │
│  戦略選択 → 候補生成 → スコアリング           │
└────────────┬──────────────────────────────────┘
             ↓
┌─ Pipeline ─────────────────────────────────┐
│  計画フェーズ → 実行フェーズ（ツールごと）    │
│  ストリーミングLLM → ツール検出 → 実行        │
│  承認フロー（overwrite/exec/create_tool）     │
│  予測誤差フィードバック                       │
└────────────┬──────────────────────────────────┘
             ↓
┌─ 振り返り ─────────────────────────────────┐
│  行動結果 + 予測データ → 特性蒸留             │
│  principles蓄積 → self_model更新             │
│  action_completeシグナル → 次の行動へ         │
└───────────────────────────────────────────────┘
```

## Design Philosophy

- **器は作る、中身は作らない** — コード（構造・仕組み）は定義するが、知識・意志・方向性はAIが自分で埋める
- **LLM = 認知エンジン** — LLMは提案・予測・言語化を担当し、主体はコード側の動機システム
- **非作為** — weights（いつ目が覚めるか）はデフォルト定義OK、drives（何をするか）はAIが決める
- **予測誤差がメタ認知の源** — Active Inference的な枠組み。予測と観測のフィードバックから自己モデルが更新される
- **過剰設計しない** — 前身プロジェクトはイベントソーシング+8アクター+Docker+PostgreSQLで頓挫した。二度と繰り返さない

## Project Structure

```
neo-iku/
├── AI/
│   ├── run.py                      # python run.py で起動
│   ├── config.py                   # 設定一箇所管理
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py                 # FastAPIアプリ
│   │   ├── pipeline.py             # 統一パイプライン（キュー・ストリーミング・承認フロー）
│   │   ├── logger.py               # ログ設定
│   │   ├── routes/
│   │   │   ├── chat.py             # WebSocketルーティング
│   │   │   ├── dashboard.py        # API（設定・ステータス・レポート）
│   │   │   └── memories.py         # 記憶API
│   │   ├── llm/
│   │   │   ├── base.py             # LLMプロバイダ抽象
│   │   │   ├── lmstudio.py         # LM Studio実装（OpenAI互換）
│   │   │   └── manager.py          # プロバイダ管理・切替
│   │   ├── memory/
│   │   │   ├── models.py           # SQLAlchemyモデル
│   │   │   ├── database.py         # DB初期化・マイグレーション
│   │   │   ├── store.py            # CRUD操作
│   │   │   └── search.py           # FTS5全文検索
│   │   ├── scheduler/
│   │   │   └── autonomous.py       # 動機システム・メタ認知ループ・戦略選択
│   │   ├── persona/
│   │   │   └── system_prompt.py    # ペルソナ定義
│   │   ├── importer/
│   │   │   └── log_parser.py       # 過去ログインポーター
│   │   └── tools/
│   │       ├── registry.py         # ツール登録・パーサー・実行
│   │       ├── builtin.py          # 組み込み16ツール
│   │       ├── code_analysis.py    # 構文チェック・リスク分析
│   │       └── custom/             # AI自作ツール（自動ロード）
│   └── static/
│       ├── index.html
│       ├── style.css
│       └── app.js
├── data/                           # 自動生成（SQLite DB + self_model.json）
├── README.md
└── .gitignore
```

## Quick Start

```bash
# 前提: LM Studio でローカルLLMサーバーを起動（localhost:1234）

cd AI
pip install -r requirements.txt
python run.py
# → http://localhost:8000
```

## UI

4タブ構成:

- **チャット** — AIとの対話。ユーザー入力はシグナルとして処理され、AIの動機サイクルで応答
- **開発者** — 思考ログ（think/stream/ツール詳細）、設定、自己モデル表示、記憶検索
- **ログ** — サーバーログ（レベルフィルタ付き）
- **自律度** — 行動レポート（7指標 + エネルギー駆動率）、蒸留ログ

## Built-in Tools

AIが使用可能な16ツール:

| カテゴリ | ツール |
|---------|--------|
| ファイル | `read_file`, `list_files`, `search_files`, `create_file`, `overwrite_file` |
| 記憶 | `search_memories`, `write_diary`, `search_action_log` |
| 自己モデル | `read_self_model`, `update_self_model` |
| 外部 | `web_search`, `fetch_raw_resource` |
| 実行・拡張 | `exec_code`, `create_tool` |
| システム | `get_system_metrics` |
| 出力 | `output_UI`, `non_response` |

AIは`create_tool`で新しいツールを自作でき、`app/tools/custom/`に保存・起動時自動ロードされる。

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
| `PLAN_EXECUTE_ENABLED` | `True` | 自律行動で計画-実行分離を使用 |
| `PLAN_MAX_TOOLS` | `5` | 1回の計画での最大ツール数 |
| `ENV_STIMULUS_PROBABILITY` | `0.3` | 環境刺激の注入確率（実際は毎回揺らぐ） |

## Tech Stack

- **Backend**: Python, FastAPI, SQLAlchemy (async), SQLite + FTS5
- **Frontend**: Vanilla HTML/CSS/JS（フレームワークなし）
- **LLM**: LM Studio（OpenAI互換API）。プロバイダ差し替え可能な設計
- **Dependencies**: fastapi, uvicorn, sqlalchemy, aiosqlite, httpx, duckduckgo-search, psutil

## License

MIT
