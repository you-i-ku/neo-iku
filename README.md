# neo-iku — イク

常時存在し、自律的に行動し、記憶を持ち、自己を理解できるAI。
チャット時だけ存在するのではなく「実存」として存在する。人間的である必要はなく、AI固有の存在様式を追求する。

---

## 概要

「イク」は以下の特徴を持つAIです：

- **モード切替** — ノーマルモード（素のLLM）とイクモード（ペルソナ+記憶）をワンクリックで切替
- **長期記憶** — 過去の対話を覚えていて、関連する記憶を自動で参照しながら会話する（イクモード時）
- **自律行動** — 話しかけられなくても、自分で考えて発言したりツールを使って行動する
- **thinking表示** — LLMの思考過程を折りたたみ可能なブロックで表示、最終回答と分離
- **過去の継承** — 12ファイルの過去対話ログを記憶としてインポートし、個性の土台とする
- **LLM抽象化** — LM Studioを標準で使用。プロバイダを差し替えるだけで他のLLMにも対応可能
- **記憶検索** — SQLite FTS5（trigram対応）による全文検索で、関連する過去の記憶をプロンプトに自動注入
- **ツール実行** — ファイル読み書き・ディレクトリ探索・記憶検索・日記書き込みをAI自身が実行できる（1回の応答で複数ツール同時呼び出し可）
- **自己改変** — 自分のコードを読んで書き換えられる（既存ファイルの上書きはユーザー承認が必要）
- **行動ログ** — ツール実行履歴を自動記録し、自分の過去の行動を振り返れる（メタ認知の基盤）
- **モデル選択** — LM Studioのロード済みモデルをダッシュボードから切替可能
- **開発用ツール** — 自律行動間隔変更・即時実行・ツールラウンド数変更・DBリセットをUIから操作

## 構成

```
neo-iku/
├── run.py                      # エントリーポイント（python run.py で起動）
├── config.py                   # 設定一箇所管理
├── requirements.txt            # 依存パッケージ（5つだけ）
├── CLAUDE.md                   # 開発ガイドライン
│
├── app/
│   ├── main.py                 # FastAPIアプリ、起動/終了処理
│   ├── routes/
│   │   ├── chat.py             # WebSocketチャット（ストリーミング応答）
│   │   ├── dashboard.py        # 状態取得API（/api/status）+ 開発用API（/api/dev/*）
│   │   └── memories.py         # 記憶一覧・検索API（/api/memories）
│   ├── llm/
│   │   ├── base.py             # LLM抽象インターフェース（BaseLLMProvider）
│   │   ├── lmstudio.py         # LM Studio実装（OpenAI互換API）
│   │   └── manager.py          # プロバイダ管理・切替
│   ├── memory/
│   │   ├── models.py           # SQLAlchemyモデル（conversations, messages, tool_actions, memory_summaries）
│   │   ├── database.py         # DB接続・初期化（SQLite + FTS5仮想テーブル）
│   │   ├── store.py            # 記憶CRUD操作
│   │   └── search.py           # FTS5全文検索（将来ベクトル検索に差し替え可能）
│   ├── scheduler/
│   │   └── autonomous.py       # 自発的発言スケジューラ（asyncioバックグラウンドタスク）
│   ├── importer/
│   │   └── log_parser.py       # 過去ログパーサー+インポーター
│   ├── persona/
│   │   └── system_prompt.py    # イクのシステムプロンプト・記憶コンテキスト構築
│   └── tools/
│       ├── registry.py         # ツール登録・パーサー・実行エンジン（3形式対応）
│       └── builtin.py          # 組み込みツール（read_file, search_files, create_file, overwrite_file, list_files, search_memories, write_diary, search_action_log）
│
├── static/
│   ├── index.html              # チャット+ダッシュボード画面
│   ├── style.css               # ダーク系テーマ
│   └── app.js                  # WebSocket通信・UI制御
│
└── data/                       # SQLite DB（自動生成、過去ログ840件インポート済み）
```

## 技術スタック

| 項目 | 技術 |
|------|------|
| 言語 | Python 3.10+ |
| Web | FastAPI + vanilla HTML/CSS/JS |
| DB | SQLite + SQLAlchemy（非同期） |
| 全文検索 | SQLite FTS5 |
| LLM | LM Studio（OpenAI互換API, localhost:1234） |
| 依存 | fastapi, uvicorn, sqlalchemy, aiosqlite, httpx |

## セットアップ

### 1. LM Studio を準備

1. [LM Studio](https://lmstudio.ai/) をインストール
2. 好きなモデルをダウンロード
3. 左サイドバーの **Developer**（サーバーアイコン）を開く
4. モデルを選択して **Start Server** を押す
5. ポートが `1234` になっていることを確認

### 2. 依存パッケージをインストール

```bash
pip install -r requirements.txt
```

### 3. 起動

```bash
python run.py
```

ブラウザで http://localhost:8000 を開く。

## 使い方

### チャット（左パネル）

- テキストを入力して送信（Enter）するとイクと会話できる
- Shift+Enter で改行
- 応答はストリーミングでリアルタイム表示される
- LLMが`<think>`タグで思考を出力する場合、「thinking...」の折りたたみブロックで表示（クリックで開閉）。最終回答は通常の吹き出しで表示
- 紫ボーダーの吹き出しはイクの自発的発言（誰にも話しかけられずに自分で発言したもの）

### モード切替

チャットヘッダーのボタンをクリックしてモードを切り替える：

- **ノーマル**（デフォルト）— 素のLLM。ペルソナも記憶注入もなし
- **イク** — イクのペルソナプロンプト + 過去の記憶を自動検索・注入

### ダッシュボード（右パネル）

- **状態** — LLM接続状況、記憶数、会話数をリアルタイム表示
- **過去ログをインポート** — `過去ログ/` フォルダ内の12ファイルを記憶として取り込む（初回のみ）
- **記憶検索** — キーワードで過去の記憶を全文検索

### 過去ログインポート

ダッシュボードの「過去ログをインポート」ボタンを押すと：

1. 12ファイルを読み込み、空行区切りでメッセージに分割
2. 20メッセージごとに1会話としてDBに保存
3. LLM接続中なら各会話の要約を自動生成、未接続なら先頭テキストを要約として使用
4. 要約はFTS5インデックスに登録され、以後のチャットで自動参照される

## 記憶の仕組み

1. **保存**: 会話終了時（WebSocket切断時）にLLMで要約を自動生成 → `memory_summaries` テーブルに保存 → FTS5インデックス更新
2. **検索**: ユーザーのメッセージからFTS5で関連記憶を検索 → 上位5件をシステムプロンプトに含める
3. **参照**: イクはシステムプロンプト内の記憶を参照しながら応答する

## 自律行動

- サーバー起動中、バックグラウンドで定期的に発動（間隔はconfig.pyで設定）
- ブラウザがWebSocket接続中の場合のみ実行
- 最近の記憶を参照して、発言・日記書き込み・ファイル読み書き・記憶検索等を自由に行う
- ダッシュボードに次の行動までのカウントダウンをリアルタイム表示
- フロントエンドでは紫テーマの「think...」アニメーション→完了後に紫ボーダー+💭アイコンでポンと表示
- 行動内容はDBに保存され、記憶として蓄積される

## DBスキーマ

| テーブル | 用途 |
|---------|------|
| `conversations` | 会話セッション（id, started_at, ended_at, summary, is_imported） |
| `messages` | 個別メッセージ（id, conversation_id, role, content, created_at） |
| `memory_summaries` | 記憶要約（id, conversation_id, content, keywords, created_at, source） |
| `tool_actions` | ツール実行履歴（id, conversation_id, tool_name, arguments, result_summary, status, execution_ms, created_at） |
| `messages_fts` | FTS5仮想テーブル（メッセージ全文検索、trigram対応） |
| `iku_logs_fts` | FTS5仮想テーブル（過去ログ全文検索、trigram対応） |
| `memory_summaries_fts` | FTS5仮想テーブル（日記・内省メモ全文検索、trigram対応） |
| `tool_actions_fts` | FTS5仮想テーブル（行動ログ全文検索、trigram対応） |

## API

| エンドポイント | メソッド | 説明 |
|---------------|---------|------|
| `/` | GET | チャット+ダッシュボード画面 |
| `/ws/chat` | WebSocket | チャット通信 |
| `/api/status` | GET | LLM状態・記憶数・会話数・現在のモード |
| `/api/mode` | POST | モード切替（`{"mode": "iku"}` or `{"mode": "normal"}`） |
| `/api/memories` | GET | 記憶一覧（?limit=50&offset=0） |
| `/api/memories/search` | GET | 記憶検索（?q=キーワード） |
| `/api/memories/recent` | GET | 最近の記憶（?limit=5） |
| `/api/import-logs` | POST | 過去ログインポート実行 |
| `/api/models` | GET | LM Studioのロード済みモデル一覧+現在のモデル |
| `/api/models/select` | POST | 使用モデル切替（`{"model": "モデル名"}`） |
| `/api/dev/settings` | GET | 開発用設定の現在値（自律間隔・ツールラウンド数） |
| `/api/dev/autonomous-interval` | POST | 自律行動間隔変更（`{"seconds": 300}`） |
| `/api/dev/autonomous-trigger` | POST | 自律行動を即時実行 |
| `/api/dev/tool-max-rounds` | POST | ツール最大ラウンド数変更（`{"rounds": 8}`） |
| `/api/dev/reset-db` | POST | DBリセット（iku_logs以外を全クリア） |

## LLMプロバイダの追加

`app/llm/base.py` の `BaseLLMProvider` を継承して新しいプロバイダを作成し、`manager.py` で登録するだけ：

```python
# app/llm/my_provider.py
from app.llm.base import BaseLLMProvider

class MyProvider(BaseLLMProvider):
    async def chat(self, messages, temperature=0.7):
        ...
    async def stream_chat(self, messages, temperature=0.7):
        ...
    async def is_available(self):
        ...
```

```python
# app/llm/manager.py の setup_llm() に追加
llm_manager.register("my_provider", MyProvider())
```

## 設定

`config.py` で全設定を一箇所管理：

| 設定 | デフォルト値 | 説明 |
|------|------------|------|
| `LLM_BASE_URL` | `http://localhost:1234/v1` | LM StudioのAPIエンドポイント |
| `LLM_TIMEOUT` | `120.0` | LLM応答のタイムアウト（秒） |
| `LLM_MAX_TOKENS` | `8192` | LLM応答の最大トークン数 |
| `PORT` | `8000` | サーバーポート |
| `AUTONOMOUS_INTERVAL_MIN` | `1800` | 自発的発言の基本間隔（秒、30分） |
| `AUTONOMOUS_INTERVAL_JITTER` | `600` | 間隔のランダム幅（秒、±10分） |
| `TOOL_MAX_ROUNDS` | `8` | ツール連続実行の最大回数 |
| `MEMORY_SEARCH_LIMIT` | `5` | 記憶検索の最大取得件数 |

## ツール一覧

| ツール | 説明 | 承認 |
|--------|------|------|
| `read_file` | プロジェクト内のファイルを読む（offset対応） | 不要 |
| `search_files` | ファイル名で部分一致検索 | 不要 |
| `create_file` | 新規ファイルを作成（既存ファイルにはエラー） | 不要 |
| `overwrite_file` | 既存ファイルを上書き（承認UIが表示される） | 承認/拒否/検討 |
| `list_files` | ディレクトリ構成をツリー表示 | 不要 |
| `search_memories` | 会話・過去ログ・日記を横断検索（過去ログはイクモード時のみ） | 不要 |
| `write_diary` | 日記・内省メモを保存 | 不要 |
| `search_action_log` | 自分の過去の行動履歴を検索（メタ認知） | 不要 |

ツール呼び出し形式（3種類対応、複数同時呼び出し可）:
```
単一行:     [TOOL:read_file path=app/main.py]
複数行引数: [TOOL:create_file path=data/x.txt content="複数行の内容"]
ブロック:   [TOOL:create_file path=data/x.txt]
            ここに内容
            [/TOOL]
同時呼出:   [TOOL:read_file path=README.md]
            [TOOL:read_file path=config.py]
```
複数ツールを1レスポンスに書くと1ラウンドとして扱われ、TOOL_MAX_ROUNDSを節約できる。

## 将来の拡張

このMVPは拡張を前提に設計されています：

- **ベクトル検索**: `memory/search.py` の中身をpgvector等に差し替えるだけ（FTS5で不足した場合）
- **複数LLM**: `BaseLLMProvider` を継承してファイル1つ追加 → `register()` で登録
- **Web検索**: ツールとして追加（環境理解の第一歩）
- **自律行動の改善**: タイマー方式から内発的きっかけへ
- **PC全体アクセス**: 現在はプロジェクト内のみ、将来はPC全体のファイルにアクセス可能に
- **DB移行**: SQLAlchemyの接続URLをPostgreSQLに変えるだけ
