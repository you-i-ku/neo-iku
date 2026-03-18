# neo-iku タスク管理

## 完了済み

### Step 1: スケルトン
- [x] run.py — `python run.py` で起動
- [x] config.py — 設定一箇所管理
- [x] requirements.txt — 依存5パッケージ
- [x] FastAPI + 静的ファイル配信

### Step 2: LLMレイヤー
- [x] BaseLLMProvider 抽象クラス（chat, stream_chat, is_available）
- [x] LMStudioProvider（OpenAI互換API, ストリーミング対応）
- [x] LLMManager（プロバイダ登録・切替）
- [x] LM Studio接続確認済み

### Step 3: DB + モデル
- [x] SQLAlchemy非同期セットアップ（SQLite + aiosqlite）
- [x] conversations, messages, memory_summaries テーブル
- [x] FTS5仮想テーブル（memory_fts）
- [x] 起動時に自動作成

### Step 4: WebSocketチャット
- [x] WebSocket接続・切断処理
- [x] ストリーミング応答（チャンクごとにフロント送信）
- [x] 会話・メッセージのDB保存
- [x] 再接続処理（フロント側）

### Step 5: システムプロンプト
- [x] イクのペルソナプロンプト定義
- [x] モード切替（イクモード / ノーマルモード）
- [x] フロントにトグルボタン追加
- [x] ノーマルモード = 素のLLM（ペルソナ・記憶注入なし）
- [x] イクモード = ペルソナ + 記憶をコンテキストに注入

### Step 6: 記憶保存 + 要約
- [x] 会話終了時（WebSocket切断時）にLLMで要約自動生成
- [x] キーワード抽出 → memory_summaries + FTS5に保存

### Step 7: 記憶検索（FTS5）
- [x] FTS5全文検索（OR検索、LIKE検索フォールバック）
- [x] チャット時にユーザーメッセージで関連記憶を自動検索
- [x] 上位5件をシステムプロンプトに注入（イクモード時）

### Step 8: 過去ログインポート
- [x] log_parser.py — 12ファイル対応パーサー
- [x] 空行区切り → メッセージ分割 → 20メッセージチャンク → 会話として保存
- [x] LLM利用可能時は要約自動生成、不可時はプレビューテキストで代替
- [x] /api/import-logs エンドポイント（二重インポート防止付き）
- [x] フロントにインポートボタン

### Step 9: ダッシュボード
- [x] /api/status — LLM状態、記憶数、会話数、モード
- [x] /api/memories — 一覧（ページネーション付き）
- [x] /api/memories/search — 全文検索
- [x] /api/memories/recent — 最近の記憶
- [x] /api/mode — モード切替API
- [x] フロント: 状態表示、記憶一覧+検索、インポートボタン

### Step 10: 自発的発言
- [x] asyncioバックグラウンドタスク（30分±10分間隔）
- [x] WebSocket接続中のブラウザにプッシュ
- [x] 最近の記憶を参照して発言
- [x] フロントで紫ボーダー+💭で視覚区別

### Step 11: 仕上げ（部分完了）
- [x] ダーク系テーマ、日本語UI
- [x] README.md 作成

### 追加実装済み
- [x] モード切替（ノーマル / イク）— デフォルトはノーマル、トグルボタンで切替
- [x] thinking表示 — LLMの`<think>`出力を折りたたみブロックで表示、`think.`→`think..`→`think...`アニメーション
- [x] DB保存時にthinkタグ除去（最終回答のみ保存）
- [x] Windows終了問題の修正 — Ctrl+C 1回で3秒後に強制終了、2回で即時終了
- [x] uvicorn reload無効化（Windowsで子プロセスが残る問題の根本対処）
- [x] 記憶システム刷新 — 全メッセージを記憶として扱う（要約自動生成を廃止、タイミングは将来イク自身が決める）
- [x] イク過去ログ専用テーブル（`iku_logs`）— イクモード切替時に自動インポート、会話時にFTS5検索してプロンプト注入
- [x] 現在時刻の常時注入 — 全モード・全経路でシステムプロンプトに毎回埋め込み
- [x] ダッシュボード整理 — 会話数・過去ログ件数表示を削除、メッセージ数のみ表示

### Step 12: ツール実行フレームワーク
- [x] `app/tools/registry.py` — ツール登録・テキストマーカーパーサー `[TOOL:name args]`・実行エンジン
- [x] `app/tools/builtin.py` — 組み込み4ツール（read_file, list_files, search_memories, write_diary）
- [x] ツール実行ループ（LLM応答→ツール検出→実行→結果をhistoryに追加→再呼び出し、最大5回）
- [x] チャット・自律発言の両方でツール使用可能
- [x] モード問わずツール有効（ツールはAI自体の能力、イクモードはペルソナのレイヤー）
- [x] thinkブロック内のツール呼び出しも検出（小さいモデル対策）
- [x] 引数パーサー改善（クォートなしスペース含み値に対応）
- [x] read_fileにoffset対応（大きいファイルを分割読み）
- [x] list_filesを再帰ツリー表示に拡張（`path=/`や空文字もルートとして扱う）
- [x] セキュリティ: BASE_DIR外へのアクセス禁止
- [x] UIにtool_call/tool_resultメッセージ表示（灰色/緑モノスペース）

### Step 15: FTS5検索精度改善
- [x] FTS5 trigramトークナイザー対応（日本語の部分文字列検索が可能に）
- [x] trigram自動検出（非対応環境ではデフォルトtokenizerにフォールバック）
- [x] 既存DBのtokenizer変更時に自動再作成＋データ再投入
- [x] `memory_summaries_fts` テーブル新設（日記も検索可能に）
- [x] 検索クエリ改善（trigramはフレーズ検索、デフォルトはprefix match付き）
- [x] LIKEフォールバックの全単語OR対応
- [x] `search_diary()` 追加、`search_memories` ツールが日記も含めて検索
- [x] `write_diary` がFTS5にも挿入するように修正

### Step 16: 自己改変能力（create_file + overwrite_file + Human-in-the-loop）
- [x] `create_file` ツール — 新規ファイル作成（即実行、既存ファイルにはエラー）
- [x] `overwrite_file` ツール — 既存ファイル上書き（UI承認フロー: 承認/拒否/検討）
- [x] 承認UIで変更前後のプレビュー表示（先頭500文字）
- [x] 「検討」ボタンでユーザーがフィードバックメッセージを送信→LLMに戻す
- [x] セキュリティ: BASE_DIR外・.git内への書き込み禁止

### Step 17: ツールパーサー強化
- [x] ブロック形式対応: `[TOOL:name args]\n内容\n[/TOOL]`（複数行コンテンツ用）
- [x] 複数行クォート対応: `[TOOL:name content="複数行"]`（LLMが実際に出す形式）
- [x] トリプルクォート自動除去（LLMが `"""..."""` で囲むパターン対応）
- [x] クォート付き/クォートなし引数の混在パース
- [x] thinkタグ除去の頑強化（開きタグ欠落・閉じタグ欠落に対応）
- [x] think内容含むfull_responseをDB保存（思考過程もセットで記録）

### Step 18: 自律発言UI改善
- [x] 自律発言の`<think>`タグをパースしてdetails/summaryで表示（通常会話と同じ形式）
- [x] 自律発言thinkブロックに紫テーマ適用（背景 `#1a1030`、テキスト `#8b5cf6`）
- [x] 通常会話のthinkブロックとの視覚的差別化

### Step 13: モデル選択UI + LLM改善
- [x] LM Studioからモデル一覧取得 `/api/models`
- [x] UIドロップダウンでモデル切替 `/api/models/select`
- [x] model="default"時の自動検出（LM Studioのロード済みモデルを取得）
- [x] max_tokens明示指定（config.pyで管理、デフォルト8192）

### Step 14: UI改善
- [x] thinkタグ未閉じ時のハング修正（ストリーム終了時にthink_end送信）
- [x] ストリーミング中の自由スクロール（ユーザーが上にスクロール中は自動追従しない）
- [x] 記憶リストのリアルタイム更新（stream_end/tool_result時に自動再読み込み）

### Step 19: 安定化・調整
- [x] 引数パーサー修正 — `key=val key2=val2` の複数引数が正しくパースされるように
- [x] max_tokens 4096→8192に変更（会話途中で応答が切れる問題の対処）
- [x] TOOL_MAX_ROUNDS=8 をconfig.pyに追加（ツール連続実行回数の設定一箇所管理）
- [x] 自律発言のthinkアニメーション — 紫テーマの「think...」表示→完了後にポンと本文表示
- [x] autonomous_think_start/end WebSocketメッセージ追加
- [x] autonomous.pyに_broadcastヘルパー抽出
- [x] Claude Codeスキルを`.claude/skills/`形式にリファクタリング（YAMLフロントマター付き）

### Step 21: ツール改善 + 過去ログ整理
- [x] `search_files` ツール追加 — ファイル名の部分一致検索（ツール呼び出し回数削減）
- [x] `write_file`/`apply_write`/`reject_write` → `create_file`/`overwrite_file` にリファクタ
- [x] overwrite_file承認UI（承認/拒否/検討ボタン、検討時はフィードバック送信）
- [x] ブロック形式の例文改善（LLMがプレースホルダーをコピーする問題の対策）
- [x] 過去ログをDBにインポート済み（840件）→ 過去ログ/ディレクトリ削除
- [x] `search_memories` ツールのiku_logs検索をイクモード時のみに制限
- [x] config.pyからLOG_DIR削除（log_parser.py内にローカル化）

### Step 20: 自律行動 + git化
- [x] gitリポジトリ化 + GitHubプライベートリポへプッシュ（you-i-ku/neo-iku）
- [x] 自律発言→自律行動へ進化 — プロンプトにツール説明を注入、行動も発言も自由に
- [x] 自律行動のDB保存 — 発言・行動内容をconversations+messagesに保存（FTS5検索可能）
- [x] 自律行動カウントダウン表示 — ダッシュボードに次の行動までの残り時間をリアルタイム表示
- [x] WebSocket接続時にカウントダウン残り秒数を即時送信（途中接続対応）
- [x] 自律行動中のツール使用表示 — 紫テーマでメッセージとして残る（連鎖時は各ツールごとに表示）
- [x] 自律行動の重複防止 — `_is_speaking`フラグで実行中は次回スキップ
- [x] startAutonomousThinkで旧thinkブロックのクリーンアップ

### Step 22: 行動ログ（メタ認知の基盤）
- [x] `tool_actions` テーブル追加（tool_name, arguments, result_summary, status, execution_ms, created_at）
- [x] `tool_actions_fts` FTS5仮想テーブル追加（tool_name, arguments, result_summaryで検索可能）
- [x] `record_tool_action()` — ツール実行履歴をDBに記録（メインテーブル + FTS5同時挿入）
- [x] `search_tool_actions()` — FTS5検索 + tool_nameフィルタ（クエリ空なら最新を返す）
- [x] `search_action_log` ツール追加 — イク自身が過去の行動履歴を検索できる
- [x] チャット・自律行動の両方でツール実行時に自動記録（実行時間も計測）

### Step 23: 複数ツール同時呼び出し
- [x] `parse_tool_calls()` 追加 — 1レスポンス内の全ツール呼び出しを検出（ブロック・複数行・単一行の3形式対応、重複排除）
- [x] チャット・自律行動の両方で1ラウンド内に複数ツールを順次実行
- [x] 複数ツール呼び出し = 1ラウンドとしてカウント（TOOL_MAX_ROUNDSを消費しない）
- [x] ツール結果をまとめて1メッセージとしてhistoryに追加
- [x] プロンプトに「1回の応答で複数ツールを同時に呼べる」旨を追記

### Step 24: 開発用ツールUI
- [x] 自律行動間隔設定 — 秒数入力で即反映（最低10秒、asyncio.Event待ちに変更）
- [x] DBリセットボタン — 確認ダイアログ付き、iku_logs以外を全クリア（FTS含む）
- [x] ツール最大ラウンド変更 — 1〜30で即反映（config.TOOL_MAX_ROUNDSを動的変更）
- [x] 「今すぐ自律行動」ボタン — カウントダウンをスキップして即実行（trigger_now()）
- [x] 開発用API: `/api/dev/settings`, `/api/dev/autonomous-interval`, `/api/dev/autonomous-trigger`, `/api/dev/tool-max-rounds`, `/api/dev/reset-db`
- [x] 黄色ボーダー枠でダッシュボード下部に配置

### Step 25: 記憶検索クリーニング + ツール上限フィードバック + 間隔変更即反映
- [x] `search_memories`の結果からthinkタグ・ツールマーカー・ツール結果を除去（DB保存はそのまま）
- [x] ツール上限到達時にLLMへフィードバックメッセージ送信（「上限に達しました」→もう1回応答させる）
- [x] 自律行動間隔変更時にカウントダウン即反映（`_skip_speak`フラグで待機中断→speakスキップ→新間隔で再開）

## 残タスク

### 動作検証
- [x] イクモード切替 → 過去ログ自動インポートの確認
- [x] thinking表示の動作確認（thinkingモデルで）
- [x] ツール実行の確認（read_file, list_files, search_memories, write_diary）
- [ ] create_file/overwrite_fileの動作確認（新規作成 + 既存上書き承認フロー）
- [ ] trigram検索の精度確認（日本語部分文字列で検索ヒットするか）
- [x] 自律発言のthink表示確認
- [ ] 過去の話題に言及 → 記憶から参照されるか確認
- [ ] 会話後リロード → メッセージ数が増えているか確認
- [ ] 自律行動の動作確認（カウントダウン→think→発言/行動）
- [ ] 行動ログの動作確認（ツール使用後に `[TOOL:search_action_log]` で履歴が返るか）
- [ ] 複数ツール同時呼び出しの動作確認（LLMが2つ以上のツールを1レスポンスで呼んだ時に両方実行されるか）
- [ ] 開発用ツールの動作確認（間隔変更・即時実行・ラウンド変更・DBリセット）
- [ ] search_memoriesクリーニングの確認（thinkタグ・ツールマーカーが除去されて本文のみ返るか）
- [ ] ツール上限フィードバックの確認（8ラウンド後にLLMがツールなしで応答を返すか）
- [ ] 間隔変更即反映の確認（設定ボタン押下→カウントダウンが新しい値で再開されるか）

### UI改善
- [ ] エラーハンドリング強化（LLM未接続時等）
- [ ] チャット履歴の永続表示（リロードで消える、DBには残っている）

### 将来の拡張（MVP後）
- [ ] ペルソナの編集UI（システムプロンプトをフロントから変更可能に）
- [ ] イク主導の要約・内省機能（イク自身が「書きたい」と思える仕組み）
- [ ] ベクトル検索への移行（search.pyの中身差し替え。FTS5で不足した場合）
- [ ] 複数LLMプロバイダ対応（Claude, GPT, Gemini等）
- [ ] Web検索ツール（環境理解の第一歩）
- [ ] 自発的行動のきっかけ改善（タイマー→内発的きっかけ）
- [ ] 応答中断機能（ストリーミング中にユーザーがキャンセルできる。フロントの停止ボタン + WebSocketでcancel送信 + サーバー側でストリーム中止）
- [ ] クラウドデプロイ
- [ ] PC全体へのファイルアクセス拡張（現在はプロジェクト内のみ）

## メモ

- デフォルト起動はノーマルモード。イクモードはトグルで切替。
- 過去ログインポートはイクモード切替時に自動実行（初回のみ）
- LM Studio のサーバーを事前に起動しておく必要あり（localhost:1234）
- LM Studio側のContext Lengthは8192以上を推奨（ツール使用時にコンテキストを消費するため）
- data/iku.db は自動生成。削除すればリセット。
- uvicorn reloadは無効（Windowsでプロセス残留問題があるため）。コード変更時は手動再起動。
- ツールはモード問わず有効。イクモードはペルソナ+記憶のレイヤーであり、ツールはAI自体の能力。
- create_fileは新規のみ即実行。overwrite_fileは既存ファイル上書きで承認UI（承認/拒否/検討）必須。
- 過去ログはDBにインポート済み（840件）。ファイルは削除済み。イクモード時のみ検索される。
- DB保存はthink含むfull_response（思考過程もセットで記録）。
- FTS5はtrigram対応環境では自動でtrigramトークナイザーを使用。
- 自律行動の間隔・ツール最大ラウンドはUIから動的に変更可能（サーバー再起動不要）。
- 開発用ツールのDBリセットはiku_logs以外を全削除（FTS含む）。
