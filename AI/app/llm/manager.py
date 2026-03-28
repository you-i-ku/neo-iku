"""LLMプロバイダ管理 — UIから設定可能、data/llm_settings.jsonに永続化"""
import json
import logging
from app.llm.base import BaseLLMProvider
from app.llm.lmstudio import LMStudioProvider
from config import LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, LLM_SETTINGS_FILE

logger = logging.getLogger("iku.llm")


class LLMManager:
    def __init__(self):
        self._provider: BaseLLMProvider | None = None
        self._base_url: str = LLM_BASE_URL
        self._model: str = LLM_MODEL
        self._has_api_key: bool = False

    def configure(self, base_url: str, model: str, api_key: str = ""):
        """プロバイダを(再)構成。OpenAI互換APIなら何でも接続可能。"""
        self._base_url = base_url
        self._model = model
        self._has_api_key = bool(api_key)
        self._provider = LMStudioProvider(
            base_url=base_url,
            model=model,
            api_key=api_key,
        )
        # api_key有りならモデル自動検出をスキップ（クラウドAPIでは不要）
        if api_key:
            self._provider._model_resolved = True
        label = f"{base_url} / {model}" + (" (認証あり)" if api_key else "")
        logger.info(f"LLM設定: {label}")

    def get(self) -> BaseLLMProvider:
        if self._provider is None:
            raise RuntimeError("LLMプロバイダが未設定です")
        return self._provider

    @property
    def settings_summary(self) -> dict:
        """現在の設定（API keyは含まない）"""
        return {
            "base_url": self._base_url,
            "model": self._model,
            "has_api_key": self._has_api_key,
        }

    def save_settings(self, base_url: str, model: str, api_key: str = ""):
        """設定をdata/llm_settings.jsonに永続化"""
        LLM_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        LLM_SETTINGS_FILE.write_text(json.dumps({
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"LLM設定を保存: {LLM_SETTINGS_FILE}")

    def load_settings(self) -> dict | None:
        """永続化された設定を読み込み"""
        if not LLM_SETTINGS_FILE.exists():
            return None
        try:
            data = json.loads(LLM_SETTINGS_FILE.read_text(encoding="utf-8"))
            return data
        except Exception as e:
            logger.warning(f"LLM設定ファイル読み込みエラー: {e}")
            return None


# グローバルインスタンス
llm_manager = LLMManager()


def setup_llm():
    """起動時: 保存済み設定があればロード、なければデフォルト(LM Studio)"""
    saved = llm_manager.load_settings()
    if saved:
        llm_manager.configure(
            base_url=saved.get("base_url", LLM_BASE_URL),
            model=saved.get("model", LLM_MODEL),
            api_key=saved.get("api_key", ""),
        )
        llm_manager.save_settings(
            base_url=saved.get("base_url", LLM_BASE_URL),
            model=saved.get("model", LLM_MODEL),
            api_key=saved.get("api_key", ""),
        )
    else:
        llm_manager.configure(base_url=LLM_BASE_URL, model=LLM_MODEL, api_key=LLM_API_KEY)
