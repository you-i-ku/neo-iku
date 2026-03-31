"""neo-iku 設定"""
import os
from pathlib import Path

# パス
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

# .env読み込み（python-dotenvがあれば使う、なければos.environから直接）
_env_path = BASE_DIR / ".env"
try:
    from dotenv import load_dotenv
    load_dotenv(_env_path)
except ImportError:
    # python-dotenvなし: .envを手動パース
    if _env_path.exists():
        for line in _env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and k not in os.environ:
                    os.environ[k] = v
PERSONAS_DIR = DATA_DIR / "personas"
STATIC_DIR = BASE_DIR / "static"

# DB
DATABASE_URL = f"sqlite+aiosqlite:///{DATA_DIR / 'iku.db'}"

# LLM（デフォルト: LM Studio。UIから変更可、data/llm_settings.jsonに永続化）
# .envのLLM_BASE_URL/LLM_API_KEY/LLM_MODELがあればそちらを優先
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:1234/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "default")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")  # .envまたは環境変数から取得
LLM_TIMEOUT = 300.0  # 5分（ツール結果含むとコンテキストが大きくなるため）
LLM_MAX_TOKENS = 8192  # 応答の最大トークン数（think+ツール呼び出し含む）
LLM_FREQUENCY_PENALTY = 0.5  # 繰り返しペナルティ（0.0〜2.0、高いほど同じトークンを避ける）
LLM_PRESENCE_PENALTY = 0.3   # 存在ペナルティ（0.0〜2.0、高いほど新しい話題を促す）
LLM_REPEAT_DETECTION_WINDOW = 200  # ループ検出ウィンドウ（文字数）
LLM_REPEAT_DETECTION_THRESHOLD = 3  # 同じパターンが何回繰り返されたら停止するか

# サーバー
HOST = "0.0.0.0"
PORT = 8000

# 自発的発言
AUTONOMOUS_INTERVAL_MIN = 300  # 
AUTONOMOUS_INTERVAL_JITTER = 0   # 一時停止中

# 記憶検索
MEMORY_SEARCH_LIMIT = 5

# ツール
TOOL_MAX_CALLS_PER_RESPONSE = 6  # 1レスポンス内のツール呼び出し総数上限（ストリーミング中断用）
TOOL_SAME_NAME_LIMIT = 3  # 同一ツールの連続呼び出し上限（ストリーミング中断用）
EXEC_CODE_TIMEOUT = 30  # exec_codeのタイムアウト（秒）
APPROVAL_TIMEOUT = 1800  # 承認待ちタイムアウト（秒）デフォルト30分

# 内発的動機システム
MOTIVATION_DEFAULT_THRESHOLD = None  # None = 全action_costsの合計を動的計算。AIがmotivation_rules.thresholdで上書き可
MOTIVATION_DEFAULT_DECAY = 5  # チェックごとの減衰量
MOTIVATION_PASSIVE_RATE = 1.0  # 受動エネルギー蓄積（/秒）。時間経過で自然回復。AIがmotivation_rules.passive_rateで上書き可
MOTIVATION_FLUCTUATION_SIGMA = 3.0  # エネルギー揺らぎの標準偏差（0で無効）
MOTIVATION_SIGNAL_BUFFER_SIZE = 100  # シグナルバッファの最大サイズ

# デフォルト動機ウェイト（神経系）: シグナル種別ごとの覚醒エネルギー
# AIがself_modelにmotivation_rules.weightsを定義すればそちらが優先される
# 値の根拠: 高頻度シグナルは低め、低頻度シグナルは高め（情報量に比例）
MOTIVATION_DEFAULT_WEIGHTS = {
    "idle_tick": 3,           # 高頻度（毎タイマーサイクル）
    "tool_success": 8,        # 行動が成功した
    "tool_error": 10,         # 行動が失敗した（予想外 = 高情報量）
    "tool_fail": 10,          # ツール未実行（予想外 = 高情報量）
    "action_complete": 12,    # 一連の行動が完了した
    "user_message": 5,        # 外部からの入力（他の外部刺激と同じ重み。AIが重要と判断すれば自分で上書き）
    "conversation_end": 5,    # ユーザーが離れた
    "self_model_update": 8,   # 自己が変化した
    "prediction_made": 5,     # 予測を行った
    "env_stimulus": 5,         # 環境刺激（ランダム注入）
    "intent_result": 5,        # 意図達成度シグナル（動的weight_overrideで上書きされることが多い）
    "mastery_detected": 30,    # 習熟検出（高エネルギー → 探索行動を促す）
}

# デフォルト行動コスト（神経系）: ツール実行ごとのエネルギー消費量
# AIがself_modelにmotivation_rules.action_costsを定義すればそちらが優先される
# 値の根拠: 副作用の大きさに比例（読むだけ=低、書き換え/外部通信=高）
MOTIVATION_DEFAULT_ACTION_COSTS = {
    "read_file": 5,
    "list_files": 3,
    "search_files": 5,
    "search_memories": 8,
    "search_action_log": 5,
    "get_system_metrics": 3,
    "non_response": 0,        # 「動かない」はコストゼロ（エネルギーを保存する選択）
    "output_UI": 10,
    "write_diary": 15,
    "update_self_model": 20,
    "web_search": 15,
    "fetch_raw_resource": 15,
    "create_file": 25,
    "overwrite_file": 40,
    "exec_code": 40,
    "create_tool": 40,
}
MOTIVATION_DEFAULT_ACTION_COST_FALLBACK = 10  # 未登録ツール（カスタムツール等）のデフォルトコスト

# マルチターン
CONTEXT_KEEP_ROUNDS = 4  # マルチターンで保持する直近ラウンド数

# 会話継続
CHAT_HISTORY_MESSAGES = 6  # 会話継続時にロードする直近メッセージ数

# 1ストリーム・アーキテクチャ
STREAM_MAX_CHARS = 12000     # ストリームの最大文字数（超過でコンパクション発動）
STREAM_KEEP_RECENT = 10      # コンパクション時に保持する直近メッセージ数

# 構造化意思決定
SCORING_ENABLED = True  # 候補生成→スコアリング→選択を有効にするか

# 環境刺激
ENV_STIMULUS_ENABLED = True
ENV_STIMULUS_PROBABILITY = 0.3  # 自律サイクルごとに30%の確率で注入

# LLM設定ファイル（API key等をgit外に保存）
LLM_SETTINGS_FILE = DATA_DIR / "llm_settings.json"

# 計画-実行分離
PLAN_EXECUTE_ENABLED = True
PLAN_MAX_TOOLS = 5
STRATEGY_CANDIDATES = 3  # 戦略候補生成数（0で無効）

# === Ablationフラグ（実験用） ===
# 各サブシステムのON/OFFを切り替えて比較実験を行う
# 全てTrueが通常動作。Falseにすると該当機能が無効化される
# UIの自律度タブからランタイムで切替可能
# ベクトル検索
VECTOR_SEARCH_ENABLED = True
VECTOR_SEARCH_LIMIT = 5

# Web検索
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")  # Brave Search API（無料2000/月）

# 情報的実存: 予測誤差エネルギー（逆U字カーブ）
PREDICTION_ENERGY_PEAK = 20.0   # 予測誤差エネルギーの最大値（similarity=0.5でピーク）

# 情報的実存: 習熟検出
MASTERY_THRESHOLD = 0.7         # 習熟検出の予測精度閾値（直近の平均accuracy）
MASTERY_ENERGY = 30.0           # 習熟検出時のエネルギー（高い → 探索行動を促す）

# バンディット計画選択
BANDIT_DEFAULT_REWARD = 10.0     # expect=なし時のデフォルト報酬（逆U字ピーク20の半分）
BANDIT_NOISE_SIGMA = 3.0         # 通常時の探索ノイズ標準偏差
BANDIT_COLD_NOISE_SIGMA = 5.0    # コールドスタート時のノイズ（やや大きめ）

SESSION_LOG_MAX = 10              # session_logの最大保持件数
SESSION_ARCHIVE_MAX_CHARS = 1000  # session_archiveの最大文字数

ABLATION_ENERGY_SYSTEM = True    # False: エネルギー蓄積/閾値/消費を無効化（固定インターバルで発火）
ABLATION_SELF_MODEL = True       # False: self_model読み書きを無効化（常に空を返す）
ABLATION_PREDICTION = True       # False: expect=引数を無視（予測-検証ループ無効）
ABLATION_BANDIT = False          # True: バンディットアルゴリズムで計画フェーズを代替（デフォルトは旧LLM計画）
ABLATION_MIRROR = False          # True: 鏡の値をプロンプトに注入（ツール順序シャッフル・戦略選択に使用）
