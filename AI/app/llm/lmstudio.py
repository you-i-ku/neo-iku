"""LM Studio実装（OpenAI互換API）"""
import httpx
import logging
from app.llm.base import BaseLLMProvider
from config import (
    LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT, LLM_MAX_TOKENS,
    LLM_FREQUENCY_PENALTY, LLM_PRESENCE_PENALTY,
    LLM_REPEAT_DETECTION_WINDOW, LLM_REPEAT_DETECTION_THRESHOLD,
)

logger = logging.getLogger("iku.lmstudio")


class LMStudioProvider(BaseLLMProvider):
    def __init__(self, base_url: str = LLM_BASE_URL, model: str = LLM_MODEL):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._model_resolved = False
        self.client = httpx.AsyncClient(timeout=LLM_TIMEOUT)
        self.last_repeat_detected = False  # 直近のstream_chatでループ検出したか

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
                "frequency_penalty": LLM_FREQUENCY_PENALTY,
                "presence_penalty": LLM_PRESENCE_PENALTY,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def _detect_repeat(text: str) -> bool:
        """直近テキストにループパターンがあるか検出（短文〜長文両対応）"""
        if len(text) < LLM_REPEAT_DETECTION_WINDOW:
            return False
        # 検査範囲: 末尾から十分な量を取る
        scan_len = min(len(text), LLM_REPEAT_DETECTION_WINDOW * LLM_REPEAT_DETECTION_THRESHOLD * 3)
        window = text[-scan_len:]

        # ルール1: 末尾連続一致（同一パターンが連続するケース）
        max_plen = min(len(window) // LLM_REPEAT_DETECTION_THRESHOLD, 500)
        for plen in range(5, max_plen + 1):
            pattern = window[-plen:]
            count = 0
            pos = len(window) - plen
            while pos >= 0:
                if window[pos:pos + plen] == pattern:
                    count += 1
                    pos -= plen
                else:
                    break
            if count >= LLM_REPEAT_DETECTION_THRESHOLD:
                return True

        # ルール2: サンプリング出現回数（交互パターン・変種混在ケース）
        # 末尾付近から50文字サンプルを複数取り、テキスト全体で何回出現するか
        sample_len = 50
        check_start = max(0, len(window) - LLM_REPEAT_DETECTION_WINDOW)
        for offset in range(check_start, len(window) - sample_len, 25):
            sample = window[offset:offset + sample_len]
            count = 0
            pos = 0
            while True:
                idx = window.find(sample, pos)
                if idx == -1:
                    break
                count += 1
                if count >= LLM_REPEAT_DETECTION_THRESHOLD:
                    return True
                pos = idx + 1

        return False

    def _find_repeat_start(self, text: str) -> int:
        """ループが始まった位置を返す（見つからなければ-1）"""
        scan_len = min(len(text), LLM_REPEAT_DETECTION_WINDOW * LLM_REPEAT_DETECTION_THRESHOLD * 3)
        window = text[-scan_len:]

        # ルール1: 末尾連続一致
        max_plen = min(len(window) // LLM_REPEAT_DETECTION_THRESHOLD, 500)
        for plen in range(5, max_plen + 1):
            pattern = window[-plen:]
            count = 0
            pos = len(window) - plen
            while pos >= 0:
                if window[pos:pos + plen] == pattern:
                    count += 1
                    pos -= plen
                else:
                    break
            if count >= LLM_REPEAT_DETECTION_THRESHOLD:
                first_repeat_pos = len(window) - plen * count
                return len(text) - scan_len + first_repeat_pos + plen

        # ルール2: サンプリング出現回数
        sample_len = 50
        check_start = max(0, len(window) - LLM_REPEAT_DETECTION_WINDOW)
        for offset in range(check_start, len(window) - sample_len, 25):
            sample = window[offset:offset + sample_len]
            positions = []
            pos = 0
            while True:
                idx = window.find(sample, pos)
                if idx == -1:
                    break
                positions.append(idx)
                pos = idx + 1
            if len(positions) >= LLM_REPEAT_DETECTION_THRESHOLD:
                # 2番目の出現位置でカット（1回分残す）
                return len(text) - scan_len + positions[1]

        return -1

    async def stream_chat(self, messages: list[dict], temperature: float = 0.7):
        await self._resolve_model()
        self.last_repeat_detected = False
        import json
        async with self.client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": LLM_MAX_TOKENS,
                "frequency_penalty": LLM_FREQUENCY_PENALTY,
                "presence_penalty": LLM_PRESENCE_PENALTY,
                "stream": True,
            },
        ) as resp:
            resp.raise_for_status()
            logger.debug(f"stream_chat status={resp.status_code} headers={dict(resp.headers)}")
            line_count = 0
            accumulated = ""
            repeat_check_interval = 20  # 20チャンクごとにチェック
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
                        accumulated += content
                        yield content
                        # ループ検出
                        if line_count % repeat_check_interval == 0 and self._detect_repeat(accumulated):
                            logger.warning(f"ループ検出: {line_count}行目で繰り返しパターンを検出。ストリーミングを中断します。")
                            self.last_repeat_detected = True
                            break
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
