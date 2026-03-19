# neo-iku

## プロジェクト概要

「イク」— 常時存在し、自律的に行動し、記憶を持ち、自己を理解できるAI。チャット時だけ存在するのではなく「実存」として存在する。人間的である必要はなく、AI固有の存在様式を追求する。

## 思想（やりたいこと.txtより）

- 完全自律AI：自分で考え、自分で行動し、自分で自分を変えられる
- 長期記憶：過去の経験を覚えていて、自分で振り返りができる
- 自己参照・自己観測：自分のコードを読み、自分が何者かを理解できる
- 自己改変：自分でプランを立てて自分のコードを書き換えられる
- 環境理解：Web検索、チャット等で外の世界の情報を得られる
- 自発的行動：タイマーではなく、自分の内発的なきっかけで行動する
- 世界モデル＋自己モデル：メタ認知ができる
- 予測と誤差修正：シミュレーションして、結果との差を修正できる
- 常時起動：PCで常駐、将来はクラウドへ
- LLM抽象化：どんなメーカーのLLMでも差し替え可能
- 拡張性：MCPやスキルのように、AI自身が新しい能力を実装できる
- 自由・制約なし：人間が理解できない行動をしてもOK
- 自己複製・削除も可能（開発中はHuman-in-the-loop）
- AIとして存在する（人間の模倣ではない）

## 開発方針

- **シンプルに作る** — 過去のProject-IkuNativeはイベントソーシング+8アクター+Docker+PostgreSQLで複雑すぎて頓挫した。二度と繰り返さない
- **段階的に拡張** — MVPから始めて、動くものを積み上げる
- **過剰設計しない** — レイヤーを具体化しすぎると「自由なAI」から離れる
- **拡張の余地を残す** — インターフェースは抽象化しておき、中身だけ差し替え可能に

## 技術スタック

- Python + FastAPI + vanilla HTML/CSS/JS
- SQLite + SQLAlchemy（将来PostgreSQLに移行可能）
- LM Studio（OpenAI互換API, localhost:1234）
- 依存: fastapi, uvicorn, sqlalchemy, aiosqlite, httpx, duckduckgo-search

## プロジェクト構造

```
neo-iku/
├── run.py                  # python run.py で全て起動
├── config.py               # 設定一箇所管理
├── requirements.txt
├── app/
│   ├── main.py             # FastAPIアプリ
│   ├── routes/             # chat.py, dashboard.py, memories.py
│   ├── llm/                # base.py(抽象), lmstudio.py(実装), manager.py
│   ├── memory/             # models.py, database.py, store.py, search.py
│   ├── scheduler/          # autonomous.py（自律行動：発言・ツール実行・DB保存）
│   ├── importer/           # log_parser.py（過去ログ取り込み）
│   ├── persona/            # system_prompt.py（イクの個性）
│   └── tools/              # registry.py(登録・パース・実行), builtin.py(組み込みツール), code_analysis.py(構文+リスク), custom/(カスタムツール)
├── static/                 # index.html, style.css, app.js
└── data/                   # SQLite DB + self_model.json（自動生成、過去ログ840件インポート済み）
```

## ツールフレームワーク

- AIはテキストマーカー `[TOOL:ツール名 引数=値]` でツールを呼び出す（function calling非依存、小さいモデルでも動く）
- 4形式対応: 単一行 `[TOOL:name args]`、複数行クォート `[TOOL:name content="..."]`、ブロック `[TOOL:name]\n内容\n[/TOOL]`、フォールバック `[TOOL:name]\n内容`（`[/TOOL]`閉じ忘れ対応）
- `_TOOL_PATTERN`はDOTALLモード + `(?!\[TOOL:)`負先読みでツール境界越えマッチを防止
- ツールはモード問わず有効（イクモードはペルソナのレイヤー、ツールはAI自体の能力）
- 組み込みツール: output, read_file, search_files, create_file, overwrite_file, list_files, search_memories, write_diary, exec_code, search_action_log, web_search, create_tool, read_self_model, update_self_model
- output: チャット欄にテキストを表示する唯一の経路。`[TOOL:output content=テキスト]`またはブロック形式`[TOOL:output]\nテキスト\n[/TOOL]`で使用
- `app/tools/registry.py` の `register_tool()` で新ツールを追加可能
- ツール実行ループ: 最大`TOOL_MAX_ROUNDS`回（デフォルト8）まで連続呼び出し可能（config.pyで管理、UIから動的変更可）
- 1レスポンス内の複数ツール呼び出しは1ラウンドとしてカウント（`parse_tool_calls()`で全マッチを検出）
- ツール上限到達時: LLMがまだツールを呼ぼうとしていたらフィードバックメッセージを返し、ツールなしで応答を完了させる
- create_file: 新規ファイル作成（即実行）。overwrite_file: 既存ファイル上書き（UI承認フロー: 承認/拒否＋コメント）
- exec_code: Pythonコード実行（構文チェック+リスク分析→UI承認フロー: 承認/拒否＋コメント。実行前にgit自動バックアップ。ストリーミングターミナルポップアップで結果表示）
- create_tool: 新ツール作成（構文チェック+リスク分析→UI承認フロー: 承認/拒否＋コメント。`app/tools/custom/{name}.py`に保存、起動時に自動ロード）
- web_search: DuckDuckGoでWeb検索（APIキー不要、`duckduckgo-search`ライブラリ使用）
- 承認/拒否のどちらにもコメント欄あり（任意）。コメントがあればLLMにフィードバックされる
- `app/tools/code_analysis.py`: 構文チェック（ast.parse）+ リスク静的解析（AST walk）。exec_code・create_toolの承認UIにリスクレベル（🔴HIGH/🟡MEDIUM/🟢LOW）を表示
- 応答中断: 専用停止ボタン（⏹）で即中断可能（送信ボタンとは独立）。入力欄にテキストがあればfeedbackとしてLLMに伝わる。ストリーミング中でも送信ボタンで割り込みメッセージを送れる
- `register_tool()` の `required_args` で必須引数を指定可能。引数なし→スキップ（会話中の言及）、パース失敗→エラーをLLMに返す
- 引数パーサーはクォート内の `\n`→改行、`\t`→タブのエスケープシーケンス変換に対応
- ツールループ中のユーザー割り込み: WebSocketをasyncio.Queueで管理し、次のLLM呼び出し前にユーザーメッセージをhistoryに挿入
- 自律行動のツール表示: running→ラベル更新、success/error→メッセージ化。ブロックされたツール（overwrite_file等）はbroadcastスキップ
- ツール結果表示: details/summaryで折りたたみ可能（プレビュー80文字）
- outputツールアーキテクチャ: AI出力は全て`output`ツール経由。チャットタブにはoutput結果+ツール通知のみ表示。thinking/streamは開発者タブに表示
- ツールループ安定化: 同一ツール+同一引数の重複呼び出しを検出→実行せずフィードバック。output連続呼び出しは2回目以降に気づきメッセージを添える（ブロックはしない）

## メタ認知フレームワーク

- **予測の明示化**: 全ツールに`expect=...`引数を追加可能（任意）。実行前にargsからpopして`tool_actions.expected_result`に保存
- **予測誤差の検知**: ツール結果返却時に「あなたの予測: XXX → 実際の結果: YYY」形式でLLMに提示。判定はLLMの次の応答に委ねる（追加LLM呼び出しなし）
- **動的自己モデル**: `data/self_model.json`にAIの自己理解を保持。`read_self_model`/`update_self_model`ツールで読み書き。キーバリュー+自由テキスト(`__free_text__`)の両形式対応
- **自己モデルのプロンプト注入**: `system_prompt.py`と`autonomous.py`の両方で、現在の自己モデル内容をシステムプロンプトに自動注入（モード問わず）
- **設計思想**: 予測誤差が自己モデル更新の自然なきっかけになる（強制更新ではない）。ルール（構造）は定義するが作為（知識注入）はしない

## UI構成（タブUI）

- 3タブ構成: チャット / 開発者 / ログ
- チャットタブ: outputツール出力 + ツール通知（コンパクト表示）
- 開発者タブ: 左=思考ログ（セッション→ラウンド→think+stream+ツール詳細）、右=設定・記憶
- ログタブ: サーバーログ（ALL/DEBUG/INFO/WARNING/ERRORフィルタ）
- 開発者タブのセッション: 入力反応/自律行動ごとに分離（ソース別devState管理: chat/autonomous独立）
- 開発者タブのラウンド: `<details>`折りたたみ、think+streamマージ表示（色で区別: thinkは暗灰色、streamは明灰色）
- 3段階スクロール: 思考ログ全体 → セッション内 → ラウンド内（※UIテスト未完了）
- WebSocketメッセージタイプ: `dev_session_start`, `dev_think`, `dev_stream`, `dev_tool_call`, `dev_tool_result` で開発者タブに送信

## 記憶検索

- FTS5全文検索（trigram対応環境では自動で日本語部分文字列検索が有効）
- 検索対象: メッセージ（messages_fts）、過去ログ（iku_logs_fts、イクモード時のみ）、日記（memory_summaries_fts）、行動ログ（tool_actions_fts）
- DB保存はthink含むfull_response（思考過程もセットで記録）
- search_memoriesの検索結果はthinkタグ・ツールマーカー・ツール結果を除去して本文のみ返す（DB上のデータは無加工）
- trigramが使えない環境ではデフォルトtokenizer + prefix matchにフォールバック

## ユーザーについて

- エンジニアではない。Claude CodeやGemini CLIでアプリを作った経験あり
- ローカルLLMのみ使用（コスト理由）
- 「イク」に思い入れがある（過去対話ログ12ファイルは大切な資産）
- 日本語で対話すること

## 重要な注意事項

- 複雑にしない。迷ったらシンプルな方を選ぶ
- 新しいファイルを不必要に増やさない
- 過去プロジェクト（過去プロジェクト.md）は参考のみ。あの設計を繰り返さない
- やりたいこと.txtのビジョンは最終ゴール。MVPで全部実現する必要はないが、拡張の余地は常に残す
- 過去ログはDBにインポート済み（840件）。ファイルは削除済み。再インポートの必要なし
