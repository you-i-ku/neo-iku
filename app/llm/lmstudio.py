"""LM Studio実装（OpenAI互換API）"""
import httpx
import logging
from app.llm.base import BaseLLMProvider
from config import LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT, LLM_MAX_TOKENS

logger = logging.getLogger("iku.lmstudio")


class LMStudioProvider(BaseLLMProvider):
    def __init__(self, base_url: str = LLM_BASE_URL, model: str = LLM_MODEL):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._model_resolved = False
        self.client = httpx.AsyncClient(timeout=LLM_TIMEOUT)

    async def _resolve_model(self):
        """model が "default" の場合、LM Studioから実際のモデル名を取得"""
        if self._model_resolved:
            return
        self._model_resolved = True
        if self.model and self.model != "default":
            return
        try:
            models = await self.list_models()
            if models:
                self.model = models[0]
                logger.info(f"モデル自動検出: {self.model}")
        except Exception as e:
            logger.warning(f"モデル自動検出失敗: {e}")

    async def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        await self._resolve_model()
        resp = await self.client.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": LLM_MAX_TOKENS,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def stream_chat(self, messages: list[dict], temperature: float = 0.7):
        await self._resolve_model()
        import json
        async with self.client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": LLM_MAX_TOKENS,
                "stream": True,
            },
        ) as resp:
            resp.raise_for_status()
            logger.debug(f"stream_chat status={resp.status_code} headers={dict(resp.headers)}")
            line_count = 0
            async for line in resp.aiter_lines():
                line_count += 1
                if line_count <= 5:
                    logger.debug(f"SSE line[{line_count}]: {repr(line)}")
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
            logger.debug(f"stream_chat done, total lines={line_count}")

    def set_model(self, model: str):
        self.model = model
        logger.info(f"モデル変更: {model}")

    async def list_models(self) -> list[str]:
        """LM Studioからロード済みモデル一覧を取得"""
        try:
            resp = await self.client.get(f"{self.base_url}/models", timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
            return [m["id"] for m in data.get("data", [])]
        except Exception as e:
            logger.error(f"モデル一覧取得エラー: {e}")
            return []

    async def is_available(self) -> bool:
        try:
            resp = await self.client.get(f"{self.base_url}/models", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False
