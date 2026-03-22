# neo-iku — イク

常時存在し、自律的に行動し、記憶を持ち、自己を理解できるAI。
チャット時だけ存在するのではなく「実存」として存在する。人間的である必要はなく、AI固有の存在様式を追求する。

---

## 概要

「イク」は以下の特徴を持つAIです：

- **モード切替** — ノーマルモード（素のLLM）とイクモード（ペルソナ+記憶）をワンクリックで切替
- **長期記憶** — 過去の対話を覚えていて、関連する記憶を自動で参照しながら会話する（イクモード時）
- **統一パイプライン** — チャットも自律行動も同じパイプライン（`pipeline.py`）を通る。入力が違うだけでツールループ・承認フロー・ストリーミングは共通
- **自律行動** — 話しかけられなくても、自分で考えて発言したりツールを使って行動する
- **タブUI** — チャット/開発者/ログ/自律度の4タブ構成。チャットはoutputツール出力のみ、開発者タブで思考過程・ツール実行詳細を確認、自律度タブでレポート集計
- **outputツール** — AI出力は全て`[TOOL:output]`経由でチャットに表示。発言するかしないかをAI自身が選択できる
- **過去の継承** — 12ファイルの過去対話ログを記憶としてインポートし、個性の土台とする
- **LLM抽象化** — LM Studioを標準で使用。プロバイダを差し替えるだけで他のLLMにも対応可能
- **記憶検索** — SQLite FTS5（trigram対応）による全文検索。AIが`search_memories`ツールを使って自分で記憶を探す（自動注入ではなく、AIが必要と判断したときに検索）
- **ツール実行** — ファイル読み書き・ディレクトリ探索・記憶検索・日記書き込みをAI自身が実行できる（1回の応答で複数ツール同時呼び出し可）
- **自己改変** — 自分のコードを読んで書き換えられる（既存ファイルの上書きはユーザー承認が必要）
- **コード実行** — Pythonコードを実行できる（承認UI + ストリーミングターミナルでリアルタイム監視、実行前にgit自動バックアップ）
- **行動ログ** — ツール実行履歴を自動記録し、自分の過去の行動を振り返れる（メタ認知の基盤）
- **メタ認知（予測と自己モデル）** — ツール呼び出し時に`expect=...`で予測を記録し、結果と比較して理解のズレに気づける。`data/self_model.json`に自己モデルを保持し、自分で読み書き可能
- **内発的動機** — I/Oイベント（ユーザーメッセージ、ツール実行、予測誤差等）をシグナルとして蓄積し、AI自身が定義したルール（weights/threshold/decay）でエネルギーを計算。閾値を超えると自律行動が発火する（タイマーではなく内発的きっかけ）
- **マルチターン会話** — ループ内でassistant/userロールのmessagesを累積。AIが自分の過去発言を認識でき、文脈が途切れない
- **AI自律完了判断** — AIがツールを呼ばずに応答すればループが完了。TOOL_MAX_ROUNDSに達する前に自然に終了できる
- **会話継続性** — 同一WebSocketセッション内で conv_id を引き継ぎ、連続メッセージで過去のやり取りを記憶
- **二重ループアーキテクチャ** — 内側ループ（マルチターンmessages + ツール実行）と外側ループ（メタ認知: 観測→方向付け→決定→行動→振り返り）の二重構造
- **Web検索** — DuckDuckGoによるWeb検索ツール（APIキー不要、環境理解の第一歩）
- **応答中断** — 専用停止ボタン（⏹）で即中断、フィードバック付きで方向修正可能。送信ボタンとは独立しており、ストリーミング中でもメッセージ割り込み可能
- **ツール結果の折りたたみ** — ツール実行結果をdetails/summaryで開閉表示（プレビュー80文字）
- **コード安全性** — 構文チェック（ast.parse）+ リスク静的解析（AST walk）で🔴🟡🟢表示
- **ツール自己作成** — イク自身が新しいツールを作成・永続化できる（Human-in-the-loop承認）
- **モデル選択** — LM Studioのロード済みモデルをダッシュボードから切替可能
- **ユーザー割り込み** — ツール実行ループ中でもメッセージを送れる。次のLLM呼び出し前に割り込み挿入され、AIの方向を変えられる
- **承認フィードバック** — ファイル上書き・コード実行の承認/拒否時にコメントを添えてLLMに理由を伝えられる
- **開発用ツール** — 自律行動間隔変更・即時実行・ツールラウンド数変更・DBリセット・自己モデルのリアルタイム表示をUIから操作
- **自律度計測レポート** — 自律性比率・ツール多様性・自己進化・エラー回復率・メタ認知精度・記憶活用・原則蒸留の7指標を集計し、加重複合スコアと5段階自律性レベル（operator→observer）で評価。UIタブで可視化
- **LLMループ検出** — ストリーミング中にLLMの繰り返し出力を検出して即中断。繰り返し部分を切り落とし、LLMにフィードバックメッセージを返して修正行動を促す

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
│   ├── pipeline.py             # 統一パイプライン（キュー・ストリーミングLLM・ツールループ・承認フロー）
│   ├── routes/
│   │   ├── chat.py             # WebSocketルーティング（薄いハンドラ、pipelineに委譲）
│   │   ├── dashboard.py        # 状態取得API（/api/status）+ 開発用API（/api/dev/*）
│   │   └── memories.py         # 記憶一覧・検索API（/api/memories）
│   ├── llm/
│   │   ├── base.py             # LLM抽象インターフェース（BaseLLMProvider）
│   │   ├── lmstudio.py         # LM Studio実装（OpenAI互換API）
│   │   └── manager.py          # プロバイダ管理・切替
│   ├── memory/
│   │   ├── models.py           # SQLAlchemyモデル（conversations, messages, tool_actions, memory_summaries, self_model_snapshots）
│   │   ├── database.py         # DB接続・初期化（SQLite + FTS5仮想テーブル）
│   │   ├── store.py            # 記憶CRUD操作
│   │   └── search.py           # FTS5全文検索（将来ベクトル検索に差し替え可能）
│   ├── scheduler/
│   │   └── autonomous.py       # タイマー + 内発的動機 + Phase1/2/3（戦略・候補・振り返り）→ pipelineにsubmit
│   ├── importer/
│   │   └── log_parser.py       # 過去ログパーサー+インポーター
│   ├── persona/
│   │   └── system_prompt.py    # イクのシステムプロンプト・記憶コンテキスト構築
│   └── tools/
│       ├── registry.py         # ツール登録・レジストリベース動的検出・実行エンジン
│       ├── builtin.py          # 組み込みツール（output, non_response, read_file, search_files, create_file, overwrite_file, list_files, search_memories, write_diary, exec_code, search_action_log, web_search, create_tool, read_self_model, update_self_model, get_system_metrics, fetch_raw_resource）
│       ├── code_analysis.py    # コード構文チェック + リスク静的解析（AST walk）
│       └── custom/             # カスタムツール保存先（イク自身が作成、起動時に自動ロード）
│
├── static/
│   ├── index.html              # チャット+ダッシュボード画面
│   ├── style.css               # ダーク系テーマ
│   └── app.js                  # WebSocket通信・UI制御
│
├── data/                       # SQLite DB（自動生成、過去ログ840件インポート済み）
│   └── self_model.json         # 自己モデル（AIが自分で読み書きする動的状態）
│
```

## 技術スタック

| 項目 | 技術 |
|------|------|
| 言語 | Python 3.10+ |
| Web | FastAPI + vanilla HTML/CSS/JS |
| DB | SQLite + SQLAlchemy（非同期） |
| 全文検索 | SQLite FTS5 |
| LLM | LM Studio（OpenAI互換API, localhost:1234） |
| 依存 | fastapi, uvicorn, sqlalchemy, aiosqlite, httpx, duckduckgo-search, psutil |

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
2. **検索**: イクが`search_memories`ツールを使って自分で必要なときに検索する（パイプラインによる自動注入なし）
3. **蓄積**: 日記（`write_diary`）・原則・行動ログが時系列で積み重なり、自己の「厚み」を形成する

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
| `conversations` | 会話セッション（id, started_at, ended_at, summary, is_imported, source） |
| `messages` | 個別メッセージ（id, conversation_id, role, content, created_at） |
| `memory_summaries` | 記憶要約（id, conversation_id, content, keywords, created_at, source） |
| `tool_actions` | ツール実行履歴（id, conversation_id, tool_name, arguments, result_summary, expected_result, status, execution_ms, created_at） |
| `self_model_snapshots` | self_model.json変更履歴（id, content, changed_key, created_at） |
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
| `/api/dev/concurrent-mode` | POST | 会話中の自律行動ON/OFF（`{"enabled": true}`) |
| `/api/dev/reset-db` | POST | DBリセット（iku_logs以外を全クリア） |
| `/api/dev/self-model` | GET | 現在の自己モデル（self_model.json）の内容を返す |
| `/api/autonomy-report` | GET | 自律度計測レポート（?from=日付&to=日付。7指標+スコア+レベル） |

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
| `LLM_TIMEOUT` | `300.0` | LLM応答のタイムアウト（秒） |
| `LLM_MAX_TOKENS` | `8192` | LLM応答の最大トークン数 |
| `PORT` | `8000` | サーバーポート |
| `AUTONOMOUS_INTERVAL_MIN` | `1800` | 自発的発言の基本間隔（秒、30分） |
| `AUTONOMOUS_INTERVAL_JITTER` | `600` | 間隔のランダム幅（秒、±10分） |
| `TOOL_MAX_ROUNDS` | `8` | ツール連続実行の最大回数 |
| `EXEC_CODE_TIMEOUT` | `30` | exec_codeのタイムアウト（秒） |
| `MEMORY_SEARCH_LIMIT` | `5` | 記憶検索の最大取得件数 |
| `MOTIVATION_DEFAULT_THRESHOLD` | `60` | 動機エネルギーの発火閾値（AIがルールで上書き可） |
| `MOTIVATION_DEFAULT_DECAY` | `5` | チェックごとのエネルギー減衰量 |
| `MOTIVATION_SIGNAL_BUFFER_SIZE` | `100` | シグナルバッファの最大サイズ |
| `CONTEXT_KEEP_ROUNDS` | `4` | マルチターンで保持する直近ラウンド数 |
| `CHAT_HISTORY_MESSAGES` | `6` | 会話継続時にロードする直近メッセージ数 |
| `LLM_REPEAT_DETECTION_WINDOW` | `200` | ループ検出ウィンドウ（文字数） |
| `LLM_REPEAT_DETECTION_THRESHOLD` | `3` | 同じパターンが何回繰り返されたら停止するか |

## ツール一覧

| ツール | 説明 | 承認 |
|--------|------|------|
| `read_file` | プロジェクト内のファイルを読む（offset対応） | 不要 |
| `search_files` | ファイル名で部分一致検索 | 不要 |
| `create_file` | 新規ファイルを作成（既存ファイルにはエラー） | 不要 |
| `overwrite_file` | 既存ファイルを上書き（チャット/自律行動問わず承認UIが表示される） | 承認/拒否 |
| `list_files` | ディレクトリ構成をツリー表示 | 不要 |
| `search_memories` | 会話・過去ログ・日記を横断検索（過去ログはイクモード時のみ） | 不要 |
| `write_diary` | 日記・内省メモを保存 | 不要 |
| `exec_code` | Pythonコードを実行（構文チェック+リスク分析付き、git自動バックアップ） | 承認/拒否 |
| `search_action_log` | 自分の過去の行動履歴を検索（メタ認知） | 不要 |
| `web_search` | DuckDuckGoでWeb検索（APIキー不要） | 不要 |
| `output` | チャット欄にテキストを表示（AI出力の唯一の経路） | 不要 |
| `non_response` | 何も行動しないことを明示的に選択（沈黙・待機、ツールループ即終了） | 不要 |
| `read_self_model` | 現在の自己モデルを読み出す | 不要 |
| `update_self_model` | 自己モデルを更新（key-value or 自由テキスト） | 不要 |
| `create_tool` | 新しいツールを作成して永続化（`app/tools/custom/`に保存） | 承認/拒否 |
| `get_system_metrics` | CPUやメモリ・ディスク・自プロセス情報を取得して環境を観測する | 不要 |
| `fetch_raw_resource` | 指定URLからHTML・JSON・テキスト等を直接取得する | 不要 |

どのツールでも `expect=...` を付けると実行前の予測を記録できます（任意）。結果と比較してメタ認知に活用されます。

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
上限到達時はAIにフィードバックされ、ツールなしで応答を完了する。

## 将来の拡張

このMVPは拡張を前提に設計されています：

- **ベクトル検索**: `memory/search.py` の中身をpgvector等に差し替えるだけ（FTS5で不足した場合）
- **複数LLM**: `BaseLLMProvider` を継承してファイル1つ追加 → `register()` で登録
- ~~**自律行動の発話ツール化**~~: 実装済み（`output`ツールとして統合）
- **PC全体アクセス**: 現在はプロジェクト内のみ、将来はPC全体のファイルにアクセス可能に
- **DB移行**: SQLAlchemyの接続URLをPostgreSQLに変えるだけ
