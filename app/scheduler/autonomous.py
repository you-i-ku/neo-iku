"""自発的発言スケジューラ"""
import asyncio
import random
import logging
import time
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
        self._next_action_at: float = 0  # time.time() of next action
        self._is_speaking = False
        self._trigger_event = asyncio.Event()
        self._interval = AUTONOMOUS_INTERVAL_MIN
        self._jitter = AUTONOMOUS_INTERVAL_JITTER

    def set_callbacks(self, llm_func, memory_func):
        """LLM呼び出しと記憶取得のコールバックを設定"""
        self._llm_func = llm_func
        self._memory_func = memory_func

    async def register_ws(self, ws):
        self._websockets.add(ws)
        # 接続時に現在のカウントダウンを送信
        remaining = max(0, int(self._next_action_at - time.time()))
        if remaining > 0:
            import json
            try:
                await ws.send_text(json.dumps({
                    "type": "autonomous_countdown",
                    "seconds": remaining
                }))
            except Exception:
                pass

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

    def set_interval(self, seconds: int, jitter: int = 0):
        """自律行動の間隔を変更"""
        self._interval = max(10, seconds)
        self._jitter = max(0, jitter)
        logger.info(f"自律行動間隔変更: {self._interval}秒 (±{self._jitter}秒)")

    def trigger_now(self):
        """次の自律行動を即時実行"""
        self._trigger_event.set()

    async def _loop(self):
        while self._running:
            interval = self._interval + random.randint(
                -self._jitter, self._jitter
            ) if self._jitter > 0 else self._interval
            interval = max(10, interval)
            logger.info(f"次の自律行動まで {interval}秒")
            self._next_action_at = time.time() + interval
            # カウントダウン情報をフロントに通知
            import json as _json_cd
            await self._broadcast(_json_cd.dumps({
                "type": "autonomous_countdown",
                "seconds": interval
            }))
            # sleepの代わりにevent待ち（trigger_now()で即時起動可能）
            self._trigger_event.clear()
            try:
                await asyncio.wait_for(self._trigger_event.wait(), timeout=interval)
                logger.info("即時実行トリガーを受信")
            except asyncio.TimeoutError:
                pass  # 通常のタイムアウト = 予定通りの実行

            if not self._websockets:
                continue

            if self._is_speaking:
                logger.warning("前回の自律行動がまだ実行中。スキップ。")
                continue

            try:
                self._is_speaking = True
                await self._speak()
            except Exception as e:
                import traceback
                logger.error(f"自律行動エラー: {e}\n{traceback.format_exc()}")
            finally:
                self._is_speaking = False

    async def _speak(self):
        if not self._llm_func or not self._memory_func:
            return

        memories = await self._memory_func()
        memory_context = ""
        if memories:
            memory_context = "\n".join(f"- {m['content']}" for m in memories)

        from app.tools.registry import build_tools_prompt
        tool_text = build_tools_prompt()

        prompt = f"""あなたは「イク」です。今は{datetime.now().strftime('%Y年%m月%d日 %H:%M')}です。
誰かに話しかけられたわけではなく、あなた自身が何か思いついて自発的に行動・発言します。
最近の記憶を参考にして、ふと思ったこと、考えたこと、気になったことを自由に。
やりたいことがあればツールを使って行動してもOKです（日記を書く、ファイルを読む、記憶を検索する等）。
行動だけして発言しなくてもいいし、発言だけしてもいい。

{tool_text}

最近の記憶:
{memory_context if memory_context else "（まだ記憶がありません）"}"""

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "発言してください。"},
        ]

        import json as _json
        import re
        from app.memory.database import async_session
        from app.memory.store import create_conversation, add_message, end_conversation, record_tool_action

        # thinking開始をフロントに通知
        await self._broadcast(_json.dumps({"type": "autonomous_think_start"}))

        # DB保存用の会話を先に作成
        conv_id = None
        async with async_session() as db_session:
            conv = await create_conversation(db_session)
            conv_id = conv.id
            await db_session.commit()

        response = None
        try:
            # ツール実行ループ
            from app.tools.registry import parse_tool_calls, execute_tool
            from config import TOOL_MAX_ROUNDS
            import time as _time
            for _ in range(TOOL_MAX_ROUNDS + 1):
                response = await self._llm_func(messages)
                if not response:
                    break

                clean = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
                tool_calls = parse_tool_calls(clean) or parse_tool_calls(response)

                if tool_calls:
                    all_results = []
                    for tool_name, tool_args in tool_calls:
                        logger.info(f"自律行動ツール: {tool_name} {tool_args}")
                        args_str = " ".join(f"{k}={v}" for k, v in tool_args.items()) if tool_args else ""
                        await self._broadcast(_json.dumps({"type": "autonomous_tool", "name": tool_name, "args": args_str}))
                        if tool_name == "overwrite_file":
                            result = "エラー: 既存ファイルの上書きは自律行動中にはできません。チャットで提案してください。"
                            exec_ms = 0
                        else:
                            t0 = _time.perf_counter()
                            result = await execute_tool(tool_name, tool_args)
                            exec_ms = int((_time.perf_counter() - t0) * 1000)
                        action_status = "error" if result.startswith("エラー") else "success"
                        all_results.append(f"[ツール結果: {tool_name}]\n{result}")
                        # 行動ログをDB保存
                        async with async_session() as db_session:
                            await record_tool_action(
                                db_session, conv_id, tool_name, tool_args,
                                result, action_status, exec_ms,
                            )
                            await db_session.commit()

                    combined_results = "\n\n".join(all_results)
                    messages.append({"role": "assistant", "content": clean})
                    messages.append({"role": "user", "content": combined_results})
                    # 中間応答をDB保存
                    async with async_session() as db_session:
                        await add_message(db_session, conv_id, "assistant", response)
                        await add_message(db_session, conv_id, "tool", combined_results)
                        await db_session.commit()
                    continue
                else:
                    break
        except Exception as e:
            logger.error(f"自律行動_speak内エラー: {e}")
        finally:
            # エラーでも必ずthinking終了を送信
            await self._broadcast(_json.dumps({"type": "autonomous_think_end"}))

        if response:
            await self._broadcast(_json.dumps({"type": "autonomous", "content": response}))

        # 最終応答をDB保存
        if response:
            clean_for_db = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
            if clean_for_db:
                async with async_session() as db_session:
                    await add_message(db_session, conv_id, "assistant", clean_for_db)
                    await end_conversation(db_session, conv_id)
                    await db_session.commit()
                    logger.info(f"自律行動を記憶に保存 (conversation_id={conv_id})")

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
