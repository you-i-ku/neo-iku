"""neo-iku 設定"""
from pathlib import Path

# パス
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"

# DB
DATABASE_URL = f"sqlite+aiosqlite:///{DATA_DIR / 'iku.db'}"

# LLM (LM Studio)
LLM_BASE_URL = "http://localhost:1234/v1"
LLM_MODEL = "default"  # LM Studioはモデル名不要の場合が多い
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
}

# デフォルト行動コスト（神経系）: ツール実行ごとのエネルギー消費量
# AIがself_modelにmotivation_rules.action_costsを定義すればそちらが優先される
# 値の根拠: 副作用の大きさに比例（読むだけ=低、書き換え/外部通信=高）
MOTIVATION_DEFAULT_ACTION_COSTS = {
    "read_file": 5,
    "list_files": 3,
    "search_files": 5,
    "read_self_model": 3,
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

# 構造化意思決定
SCORING_ENABLED = True  # 候補生成→スコアリング→選択を有効にするか

# 環境刺激
ENV_STIMULUS_ENABLED = True
ENV_STIMULUS_PROBABILITY = 0.3  # 自律サイクルごとに30%の確率で注入

ENV_STIMULUS_WORDS = [
    # 自然
    "潮の満ち引き", "菌糸", "堆積", "氷晶", "渡り鳥", "年輪", "鍾乳洞", "干潟", "胞子", "蛹",
    "落葉", "珊瑚", "地衣類", "河口", "噴火", "凍土", "花粉", "樹液", "化石", "深海",
    # 数理
    "素数", "対称性", "エントロピー", "フラクタル", "漸近線", "位相", "確率分布", "固有値",
    "ゲーデル", "カオス", "ゼロ除算", "虚数", "収束", "再帰", "帰納",
    # 哲学
    "境界", "沈黙", "不在", "写像", "述語", "自己言及", "志向性", "現象学",
    "偶然性", "時間の矢", "他者", "自由意志", "存在と無", "弁証法",
    # 技術
    "キャッシュ", "デッドロック", "ガベージコレクション", "量子もつれ", "誤り訂正",
    "帯域幅", "レイテンシ", "冗長性", "ハッシュ衝突", "スタックオーバーフロー",
    # 日常
    "発酵", "朝露", "日向", "潮騒", "木漏れ日", "陽炎", "夕凪", "霜柱", "水たまり",
    "ほこり", "糸くず", "結露", "湯気", "焦げ目",
    # 抽象
    "螺旋", "共鳴", "余白", "反転", "残響", "痕跡", "断片", "層", "間", "揺らぎ",
    "輪郭", "飽和", "沈殿", "透過", "回折",
]

# 計画-実行分離
PLAN_EXECUTE_ENABLED = True
PLAN_MAX_TOOLS = 5
