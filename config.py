"""neo-iku 設定"""
from pathlib import Path

# パス
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
LOG_DIR = BASE_DIR / "過去ログ"

# DB
DATABASE_URL = f"sqlite+aiosqlite:///{DATA_DIR / 'iku.db'}"

# LLM (LM Studio)
LLM_BASE_URL = "http://localhost:1234/v1"
LLM_MODEL = "default"  # LM Studioはモデル名不要の場合が多い
LLM_TIMEOUT = 120.0
LLM_MAX_TOKENS = 8192  # 応答の最大トークン数（think+ツール呼び出し含む）

# サーバー
HOST = "0.0.0.0"
PORT = 8000

# 自発的発言
AUTONOMOUS_INTERVAL_MIN = 300   # 5分（秒）
AUTONOMOUS_INTERVAL_JITTER = 60  # ±1分のランダム（秒）

# 記憶検索
MEMORY_SEARCH_LIMIT = 5

# ツール
TOOL_MAX_ROUNDS = 8  # 1回の応答で連続実行できるツール回数（暴走防止）
