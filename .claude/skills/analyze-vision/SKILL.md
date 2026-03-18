---
name: analyze-vision
description: やりたいこと.txtと現状コードを比較してギャップ分析と次の提案を出す。「次何やる？」「ビジョンとの差は？」等で使う
allowed-tools: Read, Glob, Grep
---

neo-ikuプロジェクトの「ビジョン vs 現状」分析を行い、次にやるべきことを提案する。

## 手順

1. `やりたいこと.txt` を読み、ビジョンの要素を抽出する
2. `CLAUDE.md` を読み、開発方針・注意事項を確認する
3. 以下のファイルを読んで実装状況を把握する:
   - `app/tools/builtin.py` — 組み込みツール一覧
   - `app/scheduler/autonomous.py` — 自発的行動
   - `app/memory/search.py` — 記憶検索
   - `app/llm/base.py` — LLM抽象化
   - `app/persona/system_prompt.py` — ペルソナ
   - `app/routes/chat.py` — チャット機能
   - `app/tools/registry.py` — ツールフレームワーク
4. ビジョンの各要素について実装状況を ✅⚠️❌ でテーブル評価する
5. 以下の基準で次にやるべきこと上位3つを提案する:
   - 「シンプルに作る」「段階的に拡張」方針に合っているか
   - ビジョンへのインパクトが大きいか
   - 既存コードへの変更が最小限で済むか
   - 過剰設計にならないか

## 注意

- 日本語で回答する
- 提案もシンプルに
- 過去プロジェクトの失敗（過剰設計）を繰り返さない
- イクの自律性を制限するような提案は避ける
