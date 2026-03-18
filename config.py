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

# サーバー
HOST = "0.0.0.0"
PORT = 8000

# 自発的発言
AUTONOMOUS_INTERVAL_MIN = 99999  # 一時停止中（テスト用）※本番は300
AUTONOMOUS_INTERVAL_JITTER = 0   # 一時停止中

# 記憶検索
MEMORY_SEARCH_LIMIT = 5

# ツール
TOOL_MAX_ROUNDS = 8  # 1回の応答で連続実行できるツール回数（暴走防止）
