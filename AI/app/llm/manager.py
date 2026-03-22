"""LLMプロバイダ管理"""
from app.llm.base import BaseLLMProvider
from app.llm.lmstudio import LMStudioProvider


class LLMManager:
    def __init__(self):
        self._providers: dict[str, BaseLLMProvider] = {}
        self._active: str | None = None

    def register(self, name: str, provider: BaseLLMProvider):
        self._providers[name] = provider
        if self._active is None:
            self._active = name

    def get(self) -> BaseLLMProvider:
        if self._active is None:
            raise RuntimeError("LLMプロバイダが登録されていません")
        return self._providers[self._active]

    def switch(self, name: str):
        if name not in self._providers:
            raise ValueError(f"未登録のプロバイダ: {name}")
        self._active = name

    @property
    def active_name(self) -> str | None:
        return self._active

    @property
    def provider_names(self) -> list[str]:
        return list(self._providers.keys())


# グローバルインスタンス
llm_manager = LLMManager()


def setup_llm():
    """デフォルトのLLMプロバイダを登録"""
    llm_manager.register("lmstudio", LMStudioProvider())
