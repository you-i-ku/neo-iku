"""LLM抽象インターフェース"""
from abc import ABC, abstractmethod


class BaseLLMProvider(ABC):
    """LLMプロバイダの抽象クラス。将来別のLLMを追加する場合はこれを継承。"""

    @abstractmethod
    async def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        """メッセージ列を送ってテキスト応答を得る"""
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """プロバイダが利用可能か確認"""
        ...

    @abstractmethod
    async def stream_chat(self, messages: list[dict], temperature: float = 0.7):
        """ストリーミング応答。yieldでチャンクを返す。"""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """テキストの埋め込みベクトルを取得。未対応ならNone"""
        return None
