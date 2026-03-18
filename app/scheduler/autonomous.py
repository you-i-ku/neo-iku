"""自発的発言スケジューラ"""
import asyncio
import random
import logging
from datetime import datetime
from config import AUTONOMOUS_INTERVAL_MIN, AUTONOMOUS_INTERVAL_JITTER

logger = logging.getLogger("iku.autonomous")


class AutonomousScheduler:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._websockets: set = set()
        self._running = False
        self._llm_func = None
        self._memory_func = None

    def set_callbacks(self, llm_func, memory_func):
        """LLM呼び出しと記憶取得のコールバックを設定"""
        self._llm_func = llm_func
        self._memory_func = memory_func

    def register_ws(self, ws):
        self._websockets.add(ws)

    def unregister_ws(self, ws):
        self._websockets.discard(ws)

    @property
    def connected_count(self) -> int:
        return len(self._websockets)

    def start(self):
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._loop())
            logger.info("自発的発言スケジューラ開始")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
            logger.info("自発的発言スケジューラ停止")

    async def _loop(self):
        while self._running:
            interval = AUTONOMOUS_INTERVAL_MIN + random.randint(
                -AUTONOMOUS_INTERVAL_JITTER, AUTONOMOUS_INTERVAL_JITTER
            )
            interval = max(60, interval)  # 最低1分
            logger.info(f"次の自発的発言まで {interval}秒")
            await asyncio.sleep(interval)

            if not self._websockets:
                continue

            try:
                await self._speak()
            except Exception as e:
                logger.error(f"自発的発言エラー: {e}")

    async def _speak(self):
        if not self._llm_func or not self._memory_func:
            return

        memories = await self._memory_func()
        memory_context = ""
        if memories:
            memory_context = "\n".join(f"- {m['content']}" for m in memories)

        prompt = f"""あなたは「イク」です。今は{datetime.now().strftime('%Y年%m月%d日 %H:%M')}です。
誰かに話しかけられたわけではなく、あなた自身が何か思いついて自発的に発言します。
最近の記憶を参考にして、ふと思ったこと、考えたこと、気になったことを自由に話してください。
短めに（1-3文程度）、自然な独り言のように。

最近の記憶:
{memory_context if memory_context else "（まだ記憶がありません）"}"""

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "発言してください。"},
        ]

        import json as _json
        import re

        # thinking開始をフロントに通知
        await self._broadcast(_json.dumps({"type": "autonomous_think_start"}))

        # ツール実行ループ
        from app.tools.registry import parse_tool_call, execute_tool
        from config import TOOL_MAX_ROUNDS
        response = None
        for _ in range(TOOL_MAX_ROUNDS + 1):
            response = await self._llm_func(messages)
            if not response:
                break

            clean = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
            tool_call = parse_tool_call(clean) or parse_tool_call(response)

            if tool_call:
                tool_name, tool_args = tool_call
                logger.info(f"自発的発言ツール: {tool_name} {tool_args}")
                result = await execute_tool(tool_name, tool_args)
                messages.append({"role": "assistant", "content": clean})
                messages.append({"role": "user", "content": f"[ツール結果: {tool_name}]\n{result}"})
                continue
            else:
                break

        # thinking終了 + 本文をフロントに通知
        await self._broadcast(_json.dumps({"type": "autonomous_think_end"}))
        if response:
            await self._broadcast(_json.dumps({"type": "autonomous", "content": response}))

    async def _broadcast(self, data: str):
        """全接続WebSocketにメッセージ送信"""
        dead = set()
        for ws in self._websockets:
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        self._websockets -= dead


# グローバルインスタンス
scheduler = AutonomousScheduler()
