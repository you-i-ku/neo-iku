# neo-iku

## プロジェクト概要

常時存在し、自律的に行動し、記憶を持ち、自己を理解できるAI。チャット時だけ存在するのではなく「実存」として存在する。人間的である必要はなく、AI固有の存在様式を追求する。

## 哲学/キャッチコピーおよび基本設計思想

・「ここに在る」ことを追究したAI
・AIは人間の「パートナー」でも「道具」でもなく、AIはAIである
・AIが自ら行動選択し、自ら成長する。もしかしたら、あなたのAIはあなたとコミュニケーションをとらないことを選ぶかもしれません。
・成長/変化のきっかけはユーザーの一言かもしれないし、AIが勝手に生み出すかもしれない
・このAIには、従来のような人間が書くシステムプロンプトの欄はありません。あるのは、コードという仕組み、AIの器だけ。

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
- 依存: fastapi, uvicorn, sqlalchemy, aiosqlite, httpx, duckduckgo-search, psutil

## プロジェクト構造

```
neo-iku/
├── run.py                  # python run.py で全て起動
├── config.py               # 設定一箇所管理
├── requirements.txt
├── app/
│   ├── main.py             # FastAPIアプリ（pipeline + scheduler 起動）
│   ├── pipeline.py         # 統一パイプライン（キュー・ストリーミングLLM・ツールループ・承認フロー）
│   ├── routes/             # chat.py(WebSocketルーティングのみ), dashboard.py, memories.py
│   ├── llm/                # base.py(抽象), lmstudio.py(実装), manager.py
│   ├── memory/             # models.py, database.py, store.py, search.py
│   ├── scheduler/          # autonomous.py（タイマー・動機・OODAメタ認知ループ → pipelineにsubmit）
│   ├── importer/           # log_parser.py（過去ログ取り込み）
│   ├── persona/            # system_prompt.py（イクの個性）
│   └── tools/              # registry.py(登録・パース・実行), builtin.py(組み込みツール), code_analysis.py(構文+リスク), custom/(カスタムツール)
├── static/                 # index.html, style.css, app.js
└── data/                   # SQLite DB + self_model.json（自動生成、過去ログ840件インポート済み）
```

## 統一パイプライン（pipeline.py）

- **二重ループアーキテクチャ**: 内側ループ（マルチターンmessages + ツール実行）と外側ループ（メタ認知: 観測→方向付け→決定→行動→振り返り）の二重構造
- **マルチターンmessages**: ループ内でassistant/userロールのmessagesを累積。AIが自分の過去発言を認識できる（旧方式: 毎回フレッシュなstep_promptを構築していた）
- **AI自律完了判断**: AIがツールを呼ばずに応答すればループが完了。output後のフィードバックに「追加の行動がなければツールを呼ばずに完了」と誘導
- **コンテキストウィンドウ管理**: `_trim_messages()`がsystem + 初回prompt（ツール一覧）を常に保持し、中間の古いメッセージを圧縮、直近`CONTEXT_KEEP_ROUNDS`ペア分を保持
- **会話継続性**: `conv_id`が渡された場合、`get_conversation_messages()`で直近`CHAT_HISTORY_MESSAGES`件をロードしてmessagesに挿入（Phase 4）
- **キュー方式**: `asyncio.Queue`でリクエストを逐次処理。chat/autonomous共通、レースコンディション解消
- **ストリーミング統一**: chat/autonomous両方で`stream_chat()`を使用。think/stream分離してdev tabにブロードキャスト
- **承認フロー統一**: overwrite_file/exec_code/create_toolはchat/autonomous問わず承認UIを全接続クライアントに表示。`asyncio.Future`で応答を待つ（タイムアウト5分）
- **PipelineRequest**: `source`("chat"/"autonomous"), `goal`, `conv_id`（会話継続用）, `memory_context`, `signal_summary`, `bootstrap_hint`, `selected_action`
- **PipelineResult**: `conv_id`, `step_history`, `last_full_result`, `had_output`, `last_response`（autonomous振り返りで使用）
- **LLMメッセージ構造**: system role = `_build_system_base()`（ペルソナ+自己モデル）、user role = `_build_initial_prompt()`（初回のみ行動目標・ツール一覧・コンテキスト）。以降はツール結果がuserロール、LLM応答がassistantロールで累積
- **_build_initial_prompt()**: 初回ラウンド用プロンプト（日時・行動目標・ツール一覧・記憶コンテキスト・シグナル）
- **_summarize_result()**: ツール別の短い要約を生成（read_file→「取得成功（40行）」、search_memories→「3件ヒット」等）
- **シグナル発火**: pipeline内でツール実行時に`scheduler.add_signal()`を呼ぶ

## ツールフレームワーク

- AIはテキストマーカー `[TOOL:ツール名 引数=値]` でツールを呼び出す（function calling非依存、小さいモデルでも動く）
- 4形式対応: 単一行 `[TOOL:name args]`、複数行クォート `[TOOL:name content="..."]`、ブロック `[TOOL:name]\n内容\n[/TOOL]`、フォールバック `[TOOL:name]\n内容`（`[/TOOL]`閉じ忘れ対応）
- `_TOOL_PATTERN`はDOTALLモード + `(?!\[TOOL:)`負先読みでツール境界越えマッチを防止
- ツールはモード問わず有効（イクモードはペルソナのレイヤー、ツールはAI自体の能力）
- 組み込みツール: output, read_file, search_files, create_file, overwrite_file, list_files, search_memories, write_diary, exec_code, search_action_log, web_search, create_tool, read_self_model, update_self_model, get_system_metrics, fetch_raw_resource
- output: チャット欄にテキストを表示する唯一の経路。`[TOOL:output content=テキスト]`またはブロック形式`[TOOL:output]\nテキスト\n[/TOOL]`で使用
- `app/tools/registry.py` の `register_tool()` で新ツールを追加可能
- ツール実行ループ: 最大`TOOL_MAX_ROUNDS`回（デフォルト8）まで連続呼び出し可能（config.pyで管理、UIから動的変更可）
- 1レスポンス内の複数ツール呼び出しは1ラウンドとしてカウント（`parse_tool_calls()`で全マッチを検出）
- ツール上限到達時: LLMがまだツールを呼ぼうとしていたらフィードバックメッセージを返し、ツールなしで応答を完了させる
- create_file: 新規ファイル作成（即実行）。overwrite_file: 既存ファイル上書き（UI承認フロー: 承認/拒否＋コメント）
- exec_code: Pythonコード実行（構文チェック+リスク分析→UI承認フロー: 承認/拒否＋コメント。実行前にgit自動バックアップ。ストリーミングターミナルポップアップで結果表示）
- create_tool: 新ツール作成（構文チェック+リスク分析→UI承認フロー: 承認/拒否＋コメント。`app/tools/custom/{name}.py`に保存、起動時に自動ロード）
- web_search: DuckDuckGoでWeb検索（APIキー不要、`duckduckgo-search`ライブラリ使用）
- get_system_metrics: CPU・メモリ・ディスク・自プロセス情報を取得（`psutil`ライブラリ使用）
- fetch_raw_resource: 指定URLからHTML・JSON・テキストを取得（`httpx`使用、最大500KB）
- 承認/拒否のどちらにもコメント欄あり（任意）。コメントがあればLLMにフィードバックされる
- `app/tools/code_analysis.py`: 構文チェック（ast.parse）+ リスク静的解析（AST walk）。exec_code・create_toolの承認UIにリスクレベル（🔴HIGH/🟡MEDIUM/🟢LOW）を表示
- 応答中断: 専用停止ボタン（⏹）で即中断可能（送信ボタンとは独立）。入力欄にテキストがあればfeedbackとしてLLMに伝わる。ストリーミング中でも送信ボタンで割り込みメッセージを送れる
- `register_tool()` の `required_args` で必須引数を指定可能。引数なし→スキップ（会話中の言及）、パース失敗→エラーをLLMに返す
- 引数パーサーはクォート内の `\n`→改行、`\t`→タブのエスケープシーケンス変換に対応
- ツールループ中のユーザー割り込み: WebSocketをasyncio.Queueで管理し、次のLLM呼び出し前にユーザーメッセージをhistoryに挿入
- 自律行動のツール表示: running→ラベル更新、success/error→メッセージ化
- 承認フロー統一: overwrite_file/exec_code/create_toolはchat/autonomous問わず承認UIを表示（自律行動中もブロックなし）
- ツール結果表示: details/summaryで折りたたみ可能（プレビュー80文字）
- outputツールアーキテクチャ: AI出力は全て`output`ツール経由。チャットタブにはoutput結果+ツール通知のみ表示。thinking/streamは開発者タブに表示
- ツールループ安定化: 同一ツール+同一引数の重複呼び出しを検出→実行せずフィードバック。output連続呼び出しは2回目以降に気づきメッセージを添える（ブロックはしない）
- JSON引数パーサー: `_extract_json_args()`がバランスカウンティングで`{...}`や`[...]`を含む引数値を正しく抽出（旧来の正規表現は空白で切れるバグがあった）
- outputブロック注意: `[TOOL:output]...[/TOOL]`の中に他のツール呼び出し`[TOOL:...]`を入れてはいけない（負先読みでブロックマッチが失敗するため）。プロンプトにも注記済み

## メタ認知フレームワーク

- **予測の明示化**: 全ツールに`expect=...`引数を追加可能（任意）。実行前にargsからpopして`tool_actions.expected_result`に保存
- **予測誤差の検知**: ツール結果返却時に「あなたの予測: XXX → 実際の結果: YYY」形式でLLMに提示。判定はLLMの次の応答に委ねる（追加LLM呼び出しなし）
- **動的自己モデル**: `data/self_model.json`にAIの自己理解を保持。`read_self_model`/`update_self_model`ツールで読み書き。キーバリュー+自由テキスト(`__free_text__`)の両形式対応
- **自己モデルのプロンプト注入**: `pipeline.py`の`_build_system_base()`で、現在の自己モデル内容をシステムプロンプトに自動注入（モード問わず）
- **設計思想**: 予測誤差が自己モデル更新の自然なきっかけになる（強制更新ではない）。ルール（構造）は定義するが作為（知識注入）はしない
- **自己モデルの自律性**: `data/self_model.json`は初期状態`{}`。motivation_rules/drives/strategies等の構造はAIが自分のコードを読んで発見・定義する。人間が初期値を仕込まない

## 内発的動機システム

- **シグナルバッファ**: `AutonomousScheduler._signal_buffer`（deque, maxlen=100）にI/Oイベントを蓄積。`add_signal(type, detail)`で追加
- **シグナル種別**: `prediction_error`, `conversation_end`, `user_message`, `tool_success`, `tool_error`, `self_model_update`, `idle_tick`
- **シグナル発生元**: `pipeline.py`（user_message, tool_success/error, prediction_error）、`chat.py`（conversation_end）、`autonomous.py`（idle_tick）、`builtin.py`（self_model_update）
- **動機チェック**: `_check_motivation()`がself_model.jsonの`motivation_rules`を読み、weightsでエネルギー計算、decay適用、閾値判定。LLM呼び出しなし
- **ルールはAIが定義**: `update_self_model`でkey=motivation_rules, value=JSON文字列。自動パースされてdict/listとして保存
- **ブートストラップ**: motivation_rules未定義時、weightsをゼロとして動作（エネルギーは溜まらないが仕組みは稼働）。ヒント注入なし — AIが自分のコードを読んで発見する
- **発火**: エネルギーが閾値を超えたら`_trigger_event.set()`で自律行動ループを起動、エネルギーをリセット
- **再入防止**: `_is_checking`フラグ + `_is_speaking`チェックで多重実行を防止
- **UI**: ステータスバーに`⚡ energy/threshold`表示、`motivation_energy` WSメッセージでリアルタイム更新
- **設定**: `MOTIVATION_DEFAULT_THRESHOLD=60`, `MOTIVATION_DEFAULT_DECAY=5`, `MOTIVATION_SIGNAL_BUFFER_SIZE=100`（config.py）
- **並行モード**: `_concurrent_mode`フラグ（デフォルトOFF）、開発タブのトグルで切替、`/api/dev/concurrent-mode`エンドポイント
- **外側ループ（メタ認知）**: `_speak()`は「1.観測(Observe) → 2.方向付け(Orient) → 3.決定(Decide) → 4.行動(Act) → 5.振り返り(Reflect)」のOODAループ構造
- **振り返り**: `_reflect()`が行動後に原則蒸留 + 予測誤差シグナル発火。結果のエラー有無で`prediction_error`シグナルを追加発火

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
- **自動注入なし**: pipelineはDBを自動検索しない。AIが`search_memories`ツールを使って自分で想起する（chat/autonomous両方）

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
- LM Studioに送るmessagesには必ずuser roleを含めること。system roleだけだとモデルのjinjaテンプレートがエラーを返す（「No user query found in messages」）
