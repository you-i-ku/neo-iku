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
- 依存: fastapi, uvicorn, sqlalchemy, aiosqlite, httpx

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
│   └── tools/              # registry.py(登録・パース・実行), builtin.py(組み込みツール)
├── static/                 # index.html, style.css, app.js
└── data/                   # SQLite DB（自動生成、過去ログ840件インポート済み）
```

## ツールフレームワーク

- AIはテキストマーカー `[TOOL:ツール名 引数=値]` でツールを呼び出す（function calling非依存、小さいモデルでも動く）
- 3形式対応: 単一行 `[TOOL:name args]`、複数行クォート `[TOOL:name content="..."]`、ブロック `[TOOL:name]\n内容\n[/TOOL]`
- ツールはモード問わず有効（イクモードはペルソナのレイヤー、ツールはAI自体の能力）
- 組み込みツール: read_file, search_files, create_file, overwrite_file, list_files, search_memories, write_diary, exec_code, search_action_log
- `app/tools/registry.py` の `register_tool()` で新ツールを追加可能
- ツール実行ループ: 最大`TOOL_MAX_ROUNDS`回（デフォルト8）まで連続呼び出し可能（config.pyで管理、UIから動的変更可）
- 1レスポンス内の複数ツール呼び出しは1ラウンドとしてカウント（`parse_tool_calls()`で全マッチを検出）
- ツール上限到達時: LLMがまだツールを呼ぼうとしていたらフィードバックメッセージを返し、ツールなしで応答を完了させる
- create_file: 新規ファイル作成（即実行）。overwrite_file: 既存ファイル上書き（UI承認フロー: 承認/拒否/検討）
- exec_code: Pythonコード実行（UI承認フロー: 承認/拒否。実行前にgit自動バックアップ。ストリーミングターミナルポップアップで結果表示）
- `register_tool()` の `required_args` で必須引数を指定可能。欠けた呼び出しは自動スキップ（LLMが会話中にツール名を言及した際の誤検出防止）
- 引数パーサーはクォート内の `\n`→改行、`\t`→タブのエスケープシーケンス変換に対応

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
