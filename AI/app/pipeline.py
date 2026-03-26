"""統一パイプライン — チャットも自律行動も同じループを通る"""
import asyncio
import json
import re
import sys
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.llm.manager import llm_manager
from app.memory.database import async_session
from app.memory.store import (
    create_conversation, add_message, end_conversation,
    record_tool_action,
)
from app.persona.system_prompt import get_active_persona_id
from app.tools.registry import parse_tool_calls, execute_tool, build_tools_prompt, build_planning_prompt, parse_plan, get_all_tools, get_tool
from app.tools.builtin import (
    _load_self_model,
    PENDING_MARKER, get_pending_overwrite,
    execute_pending_overwrite, cancel_pending_overwrite,
    PENDING_EXEC_MARKER, get_pending_exec,
    pop_pending_exec, cancel_pending_exec,
    _git_auto_backup,
    PENDING_CREATE_TOOL_MARKER, get_pending_create_tool,
    execute_pending_create_tool, cancel_pending_create_tool,
)
from app.memory.store import get_conversation_messages
from config import BASE_DIR, EXEC_CODE_TIMEOUT, CONTEXT_KEEP_ROUNDS, CHAT_HISTORY_MESSAGES, TOOL_MAX_CALLS_PER_RESPONSE, TOOL_SAME_NAME_LIMIT, PLAN_EXECUTE_ENABLED, PLAN_MAX_TOOLS

logger = logging.getLogger("iku.pipeline")


@dataclass
class PipelineRequest:
    """パイプラインへのリクエスト"""
    source: str  # "chat" | "autonomous"
    goal: str  # ユーザーメッセージ or 行動目標
    conv_id: int | None = None
    memory_context: str = ""
    signal_summary: str = ""
    bootstrap_hint: str = ""
    selected_action: dict | None = None
    trigger: str | None = None  # "timer" / "energy" / "manual" / None(chat)


@dataclass
class PipelineResult:
    """パイプラインの実行結果"""
    conv_id: int = 0
    step_history: list = field(default_factory=list)
    last_full_result: str = ""
    had_output: bool = False
    last_response: str = ""
    plan_text: str = ""  # 計画フェーズのツールリスト（plan-execute時）
    plan_stream: str = ""  # 計画フェーズのLLM生テキスト


class Pipeline:
    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._websockets: set = set()
        self._processing = False
        self._task: asyncio.Task | None = None

        # 承認待ち
        self._pending_approval: asyncio.Future | None = None

        # 中断
        self._stop_event = asyncio.Event()
        self._stop_feedback = ""

        # ユーザー割り込みキュー（処理中のチャットメッセージ）
        self._interrupt_queue: asyncio.Queue = asyncio.Queue()

    # --- WebSocket管理 ---

    async def register_ws(self, ws):
        self._websockets.add(ws)
        # 接続時に現在のカウントダウン状態を送信
        from app.scheduler.autonomous import scheduler
        remaining = max(0, int(scheduler._next_action_at - __import__('time').time()))
        if remaining > 0:
            await ws.send_text(json.dumps({
                "type": "autonomous_countdown",
                "seconds": remaining,
            }))

    def unregister_ws(self, ws):
        self._websockets.discard(ws)

    @property
    def connected_count(self) -> int:
        return len(self._websockets)

    async def _broadcast(self, data: str):
        dead = set()
        for ws in self._websockets:
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        self._websockets -= dead

    async def _broadcast_distillation_session(
        self, conv_id: int, source: str, trigger: str | None, step_history: list[dict]
    ):
        """蒸留ログ: セッション完了時にWSで通知"""
        try:
            rounds = []
            has_predictions = False
            for s in step_history:
                tool_name = s.get("tool", "")
                result_summary = s.get("result_summary", "")
                expected = s.get("expected")
                intent = s.get("intent")
                has_pred = expected is not None and expected != ""
                has_intent = intent is not None and intent != ""
                if has_pred:
                    has_predictions = True
                short_summary = self._summarize_result(tool_name, result_summary, "success")
                rounds.append({
                    "tool_name": tool_name,
                    "result_summary": short_summary,
                    "result_raw": result_summary[:200] if len(result_summary) > 80 else result_summary,
                    "expected": expected if has_pred else None,
                    "intent": intent if has_intent else None,
                    "status": "success",
                    "has_prediction": has_pred,
                    "has_intent": has_intent,
                })

            await self._broadcast(json.dumps({
                "type": "distillation_session",
                "session": {
                    "conv_id": conv_id,
                    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source": source or "chat",
                    "trigger": trigger,
                    "rounds": rounds,
                    "round_count": len(rounds),
                    "has_predictions": has_predictions,
                    "distillation_response": None,
                },
            }))
        except Exception as e:
            logger.error(f"蒸留セッションWS通知エラー: {e}")

    # --- 制御 ---

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            logger.info("パイプライン開始")

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def submit(self, request: PipelineRequest) -> PipelineResult:
        """リクエストをキューに入れ、完了を待つ"""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[PipelineResult] = loop.create_future()
        await self._queue.put((request, future))
        return await future

    def request_stop(self, feedback: str = ""):
        self._stop_feedback = feedback
        self._stop_event.set()

    def resolve_approval(self, action: str, feedback: str = ""):
        if self._pending_approval and not self._pending_approval.done():
            self._pending_approval.set_result({"action": action, "feedback": feedback})

    def add_interrupt(self, message: str):
        self._interrupt_queue.put_nowait(message)
        self._emit_signal("user_message", message[:50])

    @property
    def is_processing(self) -> bool:
        return self._processing

    # --- メインループ ---

    async def _run(self):
        while True:
            item = await self._queue.get()
            if item is None:
                break
            request, future = item
            self._processing = True
            try:
                if request.source == "autonomous" and PLAN_EXECUTE_ENABLED:
                    result = await self._process_plan_execute(request)
                else:
                    result = await self._process(request)
                if not future.done():
                    future.set_result(result)
            except Exception as e:
                import traceback
                logger.error(f"パイプラインエラー: {e}\n{traceback.format_exc()}")
                if not future.done():
                    future.set_result(PipelineResult())
            finally:
                self._processing = False
                self._stop_event.clear()

    # --- 統一パイプライン ---

    async def _process(self, req: PipelineRequest) -> PipelineResult:
        import config as _config

        logger.info(f"パイプライン処理開始: source={req.source} goal={req.goal[:60]!r}")

        # DB会話作成
        conv_id = req.conv_id
        if conv_id is None:
            async with async_session() as session:
                conv = await create_conversation(session, source=req.source, trigger=req.trigger, persona_id=get_active_persona_id())
                conv_id = conv.id
                await session.commit()

        # セッション開始通知
        preview = req.goal[:50] if req.source == "chat" else "自律行動"
        await self._broadcast(json.dumps({
            "type": "dev_session_start",
            "source": req.source,
            "preview": preview,
        }))
        if req.source == "chat":
            await self._broadcast(json.dumps({"type": "processing_start"}))
        else:
            await self._broadcast(json.dumps({"type": "autonomous_think_start"}))

        action_goal = req.goal
        step_history: list[dict] = []
        last_full_result = ""
        seen_tool_calls: set[str] = set()
        had_output = False
        response = ""

        try:
            # 共通コンテキスト構築
            tool_text = build_tools_prompt()
            logger.debug(f"tool_text 構築完了: {len(tool_text)} chars")
            system_base = self._build_system_base()
            logger.debug(f"system_base 構築完了: {len(system_base)} chars")
            memory_context = req.memory_context

            # chat の場合、ユーザーメッセージをDB保存
            if req.source == "chat":
                async with async_session() as session:
                    await add_message(session, conv_id, "user", req.goal)
                    await session.commit()
                self._emit_signal("user_message", req.goal[:50])
                logger.debug("ユーザーメッセージDB保存完了")

            # --- messages構築 ---
            messages = [
                {"role": "system", "content": system_base or ""},
            ]

            # 会話継続 — 既存conv_idが渡された場合、過去のやり取りをロード
            if req.conv_id is not None:
                try:
                    async with async_session() as session:
                        prev_msgs = await get_conversation_messages(session, req.conv_id)
                    for msg in prev_msgs[-CHAT_HISTORY_MESSAGES:]:
                        if msg.role in ("user", "assistant"):
                            clean_content = self._strip_think(msg.content) if msg.role == "assistant" else msg.content
                            messages.append({"role": msg.role, "content": clean_content})
                    logger.debug(f"会話履歴ロード: {len(prev_msgs)}件中{min(len(prev_msgs), CHAT_HISTORY_MESSAGES)}件")
                except Exception as e:
                    logger.warning(f"会話履歴ロード失敗: {e}")

            # 初回プロンプト（ツール一覧・目標・コンテキスト）
            initial_prompt = self._build_initial_prompt(
                action_goal=action_goal,
                tool_text=tool_text,
                memory_context=memory_context,
                signal_summary=req.signal_summary,
                bootstrap_hint=req.bootstrap_hint,
            )
            messages.append({"role": "user", "content": initial_prompt})

            # ユーザー割り込みチェック（LLM呼び出し前に1回）
            while not self._interrupt_queue.empty():
                try:
                    msg = self._interrupt_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                messages.append({"role": "user", "content": f"ユーザーからの追加メッセージ: {msg}"})
                async with async_session() as session:
                    await add_message(session, conv_id, "user", msg)
                    await session.commit()
                await self._broadcast(json.dumps({
                    "type": "user_interrupt_ack",
                    "content": msg,
                }))

            # コンテキストウィンドウ管理
            trimmed = self._trim_messages(messages)

            # --- 1回のLLM呼び出し ---
            logger.info(f"LLM呼び出し: messages={len(trimmed)}")
            response, repeat_detected, stream_had_tool_markers = await self._call_llm_streaming(trimmed, req.source, 0)
            logger.info(f"LLM応答: response_len={len(response)} repeat={repeat_detected} markers={stream_had_tool_markers}")

            if not response:
                logger.warning("LLM応答が空 — LM Studioが応答しているか確認してください")
            elif self._stop_event.is_set():
                # 中断チェック
                self._stop_event.clear()
                stop_note = "ユーザーにより出力を中断されました。"
                if self._stop_feedback:
                    stop_note += f"\n理由: {self._stop_feedback}"
                if response.strip():
                    async with async_session() as session:
                        await add_message(session, conv_id, "assistant", response)
                        await add_message(session, conv_id, "user", stop_note)
                        await session.commit()
                await self._broadcast(json.dumps({"type": "stopped"}))
            else:
                # ループ検出時は繰り返し部分を切り落とす
                if repeat_detected:
                    response = self._trim_repeated(response)

                clean = self._strip_think(response)
                tool_calls = parse_tool_calls(clean) or parse_tool_calls(response)

                # --- ツール実行（1応答内の全ツールを実行） ---
                all_results = []
                for tool_name, tool_args in (tool_calls or []):
                    # non_response → 沈黙を選択した記録
                    if tool_name == "non_response":
                        logger.info("non_response: 沈黙を選択")
                        all_results.append("[non_response: 沈黙を選択]")
                        step_history.append({"tool": "non_response", "args_summary": "", "result_summary": "沈黙を選択"})
                        continue

                    expected = tool_args.pop("expect", None)
                    if expected is not None and expected.strip().lower() in ("skip", "", "-", "なし", "none"):
                        expected = None
                    intent = tool_args.pop("intent", None)
                    if intent is not None and intent.strip().lower() in ("", "-", "なし", "none"):
                        intent = None
                    # Ablation: 予測無効時はexpect/intentを強制クリア
                    from app.scheduler.autonomous import scheduler as _sched
                    if not _sched.ablation_prediction:
                        expected = None
                        intent = None

                    # 重複検出
                    call_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
                    if call_key in seen_tool_calls:
                        logger.info(f"重複ツール呼び出しスキップ: {tool_name}")
                        step_history.append({"tool": tool_name, "args_summary": "", "result_summary": "重複スキップ"})
                        continue
                    seen_tool_calls.add(call_key)

                    logger.info(f"ツール: {tool_name} {tool_args}")
                    args_str = " ".join(f"{k}={v}" for k, v in tool_args.items()) if tool_args else ""
                    is_output = tool_name == "output_UI"

                    # dev tab通知
                    await self._broadcast(json.dumps({
                        "type": "dev_tool_call",
                        "content": f"{tool_name} {args_str}".strip(),
                    }))
                    if not is_output:
                        tool_call_text = f"{tool_name}({', '.join(f'{k}={v}' for k, v in tool_args.items())})"
                        await self._broadcast(json.dumps({"type": "tool_call", "content": tool_call_text}))
                        await self._broadcast(json.dumps({
                            "type": "autonomous_tool", "name": tool_name,
                            "args": args_str, "status": "running",
                        }))

                    # ツール実行
                    t0 = time.perf_counter()
                    result = await execute_tool(tool_name, tool_args)
                    exec_ms = int((time.perf_counter() - t0) * 1000)

                    # 承認フロー（chat/autonomous共通 — 接続クライアントに承認UIを表示）
                    result = await self._resolve_pending(tool_name, result)

                    action_status = "error" if result.startswith("エラー") else "success"

                    # シグナル
                    self._emit_signal(
                        "tool_error" if action_status == "error" else "tool_success",
                        tool_name,
                    )
                    if expected:
                        self._emit_signal("prediction_made", f"{tool_name}: {expected[:50]}")

                    # エネルギー消費（ツール実行コスト）
                    from app.scheduler.autonomous import scheduler
                    scheduler.consume_energy(tool_name)

                    # dev tab結果
                    await self._broadcast(json.dumps({
                        "type": "dev_tool_result",
                        "name": tool_name,
                        "content": result[:2000] if len(result) > 2000 else result,
                    }))

                    # output_UI処理
                    if is_output and action_status == "success":
                        had_output = True
                        await self._broadcast(json.dumps({"type": "tool_call", "content": "output_UI"}))
                        await self._broadcast(json.dumps({
                            "type": "output", "content": result, "source": req.source,
                        }))
                    elif not is_output:
                        await self._broadcast(json.dumps({
                            "type": "autonomous_tool", "name": tool_name,
                            "args": args_str, "status": action_status,
                        }))

                    parts = []
                    if intent:
                        parts.append(f"あなたの意図: {intent}")
                    if expected:
                        parts.append(f"あなたの予測: {expected}")
                    parts.append(f"実際の結果: {result}" if (intent or expected) else result)
                    all_results.append(f"[ツール結果: {tool_name}]\n" + "\n".join(parts))

                    # DB記録
                    async with async_session() as session:
                        await record_tool_action(
                            session, conv_id, tool_name, tool_args,
                            result, action_status, exec_ms,
                            expected_result=expected,
                            intent=intent,
                            persona_id=get_active_persona_id(),
                        )
                        await session.commit()

                    # step_history
                    step_history.append({
                        "tool": tool_name,
                        "args_summary": args_str[:80],
                        "result_summary": self._summarize_result(tool_name, result, action_status),
                        "expected": expected,
                        "intent": intent,
                        "stream": response,
                    })

                # ツール未実行検出
                executed_tools = [s["tool"] for s in step_history if s.get("tool")]
                if not executed_tools:
                    if stream_had_tool_markers:
                        # ストリーミングで[TOOLを検出したがパース/実行に至らなかった
                        fail_msg = "ツールマーカーを検出しましたが、正しい形式で実行されませんでした。"
                    else:
                        fail_msg = "ツールが実行されませんでした。"
                    all_results.append(fail_msg)
                    step_history.append({"tool": "(tool_fail)", "args_summary": "", "result_summary": fail_msg})
                    self._emit_signal("tool_fail", fail_msg)
                    logger.info(f"tool_fail: {fail_msg}")

                # DB保存
                combined_results = "\n\n".join(all_results)
                last_full_result = combined_results
                async with async_session() as session:
                    await add_message(session, conv_id, "assistant", response)
                    if combined_results:
                        await add_message(session, conv_id, "tool", combined_results)
                    await session.commit()

            # action_completeシグナル発火
            self._emit_signal("action_complete", action_goal[:50] if action_goal else req.source)

        except Exception as e:
            import traceback
            logger.error(f"パイプライン処理エラー: {e}\n{traceback.format_exc()}")
        finally:
            if req.source == "chat":
                await self._broadcast(json.dumps({"type": "processing_end"}))
            else:
                await self._broadcast(json.dumps({"type": "autonomous_think_end"}))

        # 会話終了
        try:
            async with async_session() as session:
                await end_conversation(session, conv_id)
                await session.commit()
        except Exception:
            pass

        # 蒸留ログ: セッション完了をWS通知
        await self._broadcast_distillation_session(
            conv_id, req.source, req.trigger, step_history
        )

        return PipelineResult(
            conv_id=conv_id,
            step_history=step_history,
            last_full_result=last_full_result,
            had_output=had_output,
            last_response=response,
        )

    # --- 計画-実行分離パイプライン ---

    async def _process_plan_execute(self, req: PipelineRequest) -> PipelineResult:
        """自律行動用: 計画フェーズ → 実行フェーズ（ツールごとに1回のLLM呼び出し）"""
        logger.info(f"計画-実行パイプライン開始: goal={req.goal[:60]!r}")

        # DB会話作成
        conv_id = req.conv_id
        if conv_id is None:
            async with async_session() as session:
                conv = await create_conversation(session, source=req.source, trigger=req.trigger, persona_id=get_active_persona_id())
                conv_id = conv.id
                await session.commit()

        await self._broadcast(json.dumps({
            "type": "dev_session_start",
            "source": req.source,
            "preview": "自律行動（計画-実行）",
        }))
        await self._broadcast(json.dumps({"type": "autonomous_think_start"}))

        step_history: list[dict] = []
        last_full_result = ""
        had_output = False
        seen_tool_calls: set[str] = set()
        plan_text = ""
        plan_response = ""

        try:
            system_base = self._build_system_base()
            signal_summary = req.signal_summary
            action_goal = req.goal
            planning_tool_text = build_planning_prompt()

            # === Phase 1: 計画 ===
            planning_prompt = self._build_planning_prompt(action_goal, planning_tool_text, signal_summary)
            plan_messages = [
                {"role": "system", "content": system_base or ""},
                {"role": "user", "content": planning_prompt},
            ]

            logger.info("計画フェーズ: LLM呼び出し")
            plan_response, _, _ = await self._call_llm_streaming(plan_messages, req.source, 0)

            if not plan_response:
                logger.warning("計画フェーズ: LLM応答なし → フォールバック")
                return await self._process(req)

            clean_plan = self._strip_think(plan_response)
            planned_tools = parse_plan(clean_plan)

            if not planned_tools:
                logger.warning(f"計画パース失敗 → フォールバック: {clean_plan[:200]}")
                return await self._process(req)

            planned_tools = planned_tools[:PLAN_MAX_TOOLS]
            plan_text = " → ".join(planned_tools)
            logger.info(f"計画確定: [{plan_text}]")

            # 計画をDB記録
            async with async_session() as session:
                await add_message(session, conv_id, "assistant", plan_response)
                await add_message(session, conv_id, "tool", f"[計画] {plan_text}")
                await session.commit()

            # === Phase 2: 実行 ===
            execution_results: list[dict] = []  # {"tool": name, "result": text, "summary": text}

            for round_idx, tool_name in enumerate(planned_tools):
                if self._stop_event.is_set():
                    logger.info("計画実行中断: ユーザーによる停止")
                    break

                tool_info = get_tool(tool_name)
                if not tool_info:
                    logger.warning(f"計画内の未登録ツール: {tool_name} → スキップ")
                    continue

                # 実行プロンプト構築
                exec_prompt = self._build_execution_prompt(
                    tool_name=tool_name,
                    tool_info=tool_info,
                    action_goal=action_goal,
                    previous_results=execution_results[-3:],
                    signal_summary=signal_summary,
                    round_idx=round_idx,
                    total_rounds=len(planned_tools),
                )
                exec_messages = [
                    {"role": "system", "content": system_base or ""},
                    {"role": "user", "content": exec_prompt},
                ]

                logger.info(f"実行フェーズ {round_idx + 1}/{len(planned_tools)}: {tool_name}")
                exec_response, repeat_detected, _ = await self._call_llm_streaming(
                    exec_messages, req.source, round_idx + 1
                )

                if not exec_response:
                    logger.warning(f"実行フェーズ {round_idx + 1}: LLM応答なし")
                    continue

                if repeat_detected:
                    exec_response = self._trim_repeated(exec_response)

                clean_exec = self._strip_think(exec_response)
                tool_calls = parse_tool_calls(clean_exec) or parse_tool_calls(exec_response)

                # ツール実行（計画内ツール + output_UI/non_response のみ許可）
                round_results = []
                for tc_name, tc_args in (tool_calls or []):
                    allowed = (tc_name == tool_name or tc_name in ("output_UI", "non_response"))
                    if not allowed:
                        logger.info(f"計画外ツールスキップ: {tc_name}（計画={tool_name}）")
                        continue

                    result, status, tool_had_output = await self._execute_single_tool(
                        tc_name, tc_args, conv_id, req.source, seen_tool_calls, step_history
                    )
                    if tool_had_output:
                        had_output = True
                    round_results.append(result)

                # step_historyにstream追加（このラウンドで追加されたエントリに）
                for sh in step_history:
                    if sh.get("stream") is None and sh.get("tool") == tool_name:
                        sh["stream"] = exec_response
                        break

                # DB記録
                combined = "\n\n".join(round_results) if round_results else ""
                async with async_session() as session:
                    await add_message(session, conv_id, "assistant", exec_response)
                    if combined:
                        await add_message(session, conv_id, "tool", combined)
                    await session.commit()

                last_full_result = combined

                # 実行結果を蓄積（次のラウンドの前提として使う）
                summary = self._summarize_result(tool_name, combined, "success" if combined else "error") if combined else "実行なし"
                execution_results.append({
                    "tool": tool_name,
                    "result": combined[:500],
                    "summary": summary,
                })

            # ツール未実行検出
            if not step_history:
                self._emit_signal("tool_fail", "計画-実行: ツール未実行")

            # action_completeシグナル
            self._emit_signal("action_complete", action_goal[:50] if action_goal else "plan_execute")

        except Exception as e:
            import traceback
            logger.error(f"計画-実行エラー: {e}\n{traceback.format_exc()}")
        finally:
            await self._broadcast(json.dumps({"type": "autonomous_think_end"}))

        # 会話終了
        try:
            async with async_session() as session:
                await end_conversation(session, conv_id)
                await session.commit()
        except Exception:
            pass

        # 蒸留ログ: セッション完了をWS通知
        await self._broadcast_distillation_session(
            conv_id, req.source, req.trigger, step_history
        )

        return PipelineResult(
            conv_id=conv_id,
            step_history=step_history,
            last_full_result=last_full_result,
            had_output=had_output,
            last_response="",
            plan_text=plan_text,
            plan_stream=plan_response,
        )

    async def _execute_single_tool(
        self, tool_name: str, tool_args: dict, conv_id: int,
        req_source: str, seen_tool_calls: set, step_history: list
    ) -> tuple[str, str, bool]:
        """単一ツール実行。(result_text, status, had_output) を返す"""
        had_output = False

        # non_response
        if tool_name == "non_response":
            logger.info("non_response: 沈黙を選択")
            step_history.append({"tool": "non_response", "args_summary": "", "result_summary": "沈黙を選択"})
            return "[non_response: 沈黙を選択]", "success", False

        expected = tool_args.pop("expect", None)
        if expected is not None and expected.strip().lower() in ("skip", "", "-", "なし", "none"):
            expected = None
        intent = tool_args.pop("intent", None)
        if intent is not None and intent.strip().lower() in ("", "-", "なし", "none"):
            intent = None
        # Ablation: 予測無効時はexpect/intentを強制クリア
        from app.scheduler.autonomous import scheduler as _sched
        if not _sched.ablation_prediction:
            expected = None
            intent = None

        # 重複検出
        call_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
        if call_key in seen_tool_calls:
            logger.info(f"重複ツール呼び出しスキップ: {tool_name}")
            step_history.append({"tool": tool_name, "args_summary": "", "result_summary": "重複スキップ"})
            return "", "skipped", False
        seen_tool_calls.add(call_key)

        args_str = " ".join(f"{k}={v}" for k, v in tool_args.items()) if tool_args else ""
        is_output = tool_name == "output_UI"

        # dev tab通知
        await self._broadcast(json.dumps({
            "type": "dev_tool_call",
            "content": f"{tool_name} {args_str}".strip(),
        }))
        if not is_output:
            tool_call_text = f"{tool_name}({', '.join(f'{k}={v}' for k, v in tool_args.items())})"
            await self._broadcast(json.dumps({"type": "tool_call", "content": tool_call_text}))
            await self._broadcast(json.dumps({
                "type": "autonomous_tool", "name": tool_name,
                "args": args_str, "status": "running",
            }))

        # ツール実行
        t0 = time.perf_counter()
        result = await execute_tool(tool_name, tool_args)
        exec_ms = int((time.perf_counter() - t0) * 1000)

        # 承認フロー
        result = await self._resolve_pending(tool_name, result)

        action_status = "error" if result.startswith("エラー") else "success"

        # シグナル
        self._emit_signal(
            "tool_error" if action_status == "error" else "tool_success",
            tool_name,
        )
        if expected:
            self._emit_signal("prediction_made", f"{tool_name}: {expected[:50]}")

        # エネルギー消費
        from app.scheduler.autonomous import scheduler
        scheduler.consume_energy(tool_name)

        # dev tab結果
        await self._broadcast(json.dumps({
            "type": "dev_tool_result",
            "name": tool_name,
            "content": result[:2000] if len(result) > 2000 else result,
        }))

        # output_UI処理
        if is_output and action_status == "success":
            had_output = True
            await self._broadcast(json.dumps({"type": "tool_call", "content": "output_UI"}))
            await self._broadcast(json.dumps({
                "type": "output", "content": result, "source": req_source,
            }))
        elif not is_output:
            await self._broadcast(json.dumps({
                "type": "autonomous_tool", "name": tool_name,
                "args": args_str, "status": action_status,
            }))

        parts = []
        if intent:
            parts.append(f"あなたの意図: {intent}")
        if expected:
            parts.append(f"あなたの予測: {expected}")
        parts.append(f"実際の結果: {result}" if (intent or expected) else result)
        result_text = f"[ツール結果: {tool_name}]\n" + "\n".join(parts)

        # DB記録
        async with async_session() as session:
            await record_tool_action(
                session, conv_id, tool_name, tool_args,
                result, action_status, exec_ms,
                expected_result=expected,
                intent=intent,
                persona_id=get_active_persona_id(),
            )
            await session.commit()

        # step_history
        step_history.append({
            "tool": tool_name,
            "args_summary": args_str[:80],
            "result_summary": self._summarize_result(tool_name, result, action_status),
            "expected": expected,
            "intent": intent,
        })

        return result_text, action_status, had_output

    def _build_planning_prompt(self, action_goal: str, tool_text: str, signal_summary: str) -> str:
        """計画フェーズ用プロンプト"""
        now = datetime.now().strftime('%Y年%m月%d日 %H:%M')
        goal_line = f"\n行動目標: {action_goal}" if action_goal else ""
        signal_line = f"\n{signal_summary}" if signal_summary else ""

        return f"""【状況】
日時: {now}{goal_line}{signal_line}

【利用可能ツール】
{tool_text}

【出力指示】
上記ツールから実行順序を計画する。各ステップで前の結果を参照可能。最大{PLAN_MAX_TOOLS}個。

形式:
1. ツール名
2. ツール名

ツール呼び出し（[TOOL:...]）は不要。計画のみ出力。"""

    def _build_execution_prompt(
        self, tool_name: str, tool_info: dict, action_goal: str,
        previous_results: list[dict], signal_summary: str,
        round_idx: int, total_rounds: int,
    ) -> str:
        """実行フェーズ用プロンプト（1ツール分）"""
        now = datetime.now().strftime('%Y年%m月%d日 %H:%M')
        goal_line = f"\n行動目標: {action_goal}" if action_goal else ""

        # 前回までの結果
        prev_text = ""
        if previous_results:
            prev_lines = []
            for pr in previous_results:
                prev_lines.append(f"- {pr['tool']}: {pr['summary']}")
                if pr.get("result"):
                    prev_lines.append(f"  結果: {pr['result'][:300]}")
            prev_text = "\n前のステップの結果:\n" + "\n".join(prev_lines)

        # ツール説明
        desc = tool_info["description"]
        args_desc = tool_info.get("args_desc", "")
        tool_desc = f"{tool_name}: {desc}"
        if args_desc:
            tool_desc += f"\n  引数: {args_desc}"

        return f"""【状況】
日時: {now}{goal_line}
{prev_text}

【タスク】
ステップ {round_idx + 1}/{total_rounds}: {tool_name}

ツール情報:
  {tool_desc}

【書式】
  [TOOL:{tool_name} 引数A=値A 引数B=値B]
  [TOOL:{tool_name} 引数A=値A intent=この操作の目的 expect=予測される結果]
ブロック書式:
  [TOOL:{tool_name}]
  複数行の内容
  [/TOOL]

【出力指示】
上記ツールを1つだけ呼び出す。output_UIで発言も可。expect=に結果の予測を記述すること。
ツールの結果はシステムが返す。結果を自分で生成しない。[TOOL:...]マーカーだけ出力する。"""

    # --- ストリーミングLLM ---

    async def _call_llm_streaming(self, messages: list[dict], source: str, round_num: int) -> tuple[str, bool]:
        llm = llm_manager.get()
        full_response = ""
        in_think = False
        stream_text = ""  # think外テキスト（ツール検出用）
        tool_overflow = False

        logger.info(f"_call_llm_streaming 開始: round={round_num} model={getattr(llm, 'model', '?')} base_url={getattr(llm, 'base_url', '?')}")

        await self._broadcast(json.dumps({
            "type": "dev_round_start",
            "round": round_num + 1,
            "source": source,
        }))

        try:
            logger.info(f"stream_chat 呼び出し中...")
            async for chunk in llm.stream_chat(messages):
                if self._stop_event.is_set():
                    break
                full_response += chunk

                # think/stream分離 → dev tabに送信
                buf = chunk
                while buf:
                    if in_think:
                        end_idx = buf.find("</think>")
                        if end_idx != -1:
                            if buf[:end_idx]:
                                await self._broadcast(json.dumps({"type": "dev_think", "content": buf[:end_idx]}))
                            await self._broadcast(json.dumps({"type": "dev_think_end"}))
                            buf = buf[end_idx + 8:]
                            in_think = False
                        else:
                            await self._broadcast(json.dumps({"type": "dev_think", "content": buf}))
                            buf = ""
                    else:
                        start_idx = buf.find("<think>")
                        if start_idx != -1:
                            if buf[:start_idx]:
                                await self._broadcast(json.dumps({"type": "dev_stream", "content": buf[:start_idx]}))
                                stream_text += buf[:start_idx]
                            await self._broadcast(json.dumps({"type": "dev_think_start"}))
                            buf = buf[start_idx + 7:]
                            in_think = True
                        else:
                            await self._broadcast(json.dumps({"type": "dev_stream", "content": buf}))
                            stream_text += buf
                            buf = ""

                # ストリーミング中ツール呼び出し検出（think外テキストのみ）
                # [TOOL 付近（前後30文字）に登録済みツール名があればカウント
                if not in_think and not tool_overflow:
                    tool_names = list(get_all_tools().keys())
                    hits: list[str] = []
                    for m in re.finditer(r'\[TOOL', stream_text):
                        vicinity = stream_text[m.start():m.start() + 30]
                        for tn in tool_names:
                            if tn in vicinity:
                                hits.append(tn)
                    if hits:
                        if len(hits) > TOOL_MAX_CALLS_PER_RESPONSE:
                            logger.warning(f"ストリーミング中ツール総数超過: {len(hits)}個検出、中断")
                            tool_overflow = True
                        else:
                            counts: dict[str, int] = {}
                            for n in hits:
                                counts[n] = counts.get(n, 0) + 1
                            for n, c in counts.items():
                                if c > TOOL_SAME_NAME_LIMIT:
                                    logger.warning(f"ストリーミング中同一ツール超過: {n}={c}回、中断")
                                    tool_overflow = True
                                    break

                if tool_overflow:
                    break

            if in_think:
                await self._broadcast(json.dumps({"type": "dev_think_end"}))

        except Exception as e:
            import traceback
            logger.error(f"LLMストリーミングエラー: {e}\n{traceback.format_exc()}")
            await self._broadcast(json.dumps({
                "type": "error",
                "content": f"（LLMエラー: {e}）",
            }))
            return ("", False, False)

        repeat_detected = getattr(llm, "last_repeat_detected", False)
        # ストリーミング中に[TOOLマーカーを検出したか
        stream_had_tool_markers = bool(re.search(r'\[TOOL', stream_text))
        logger.info(f"stream_chat 完了: total_chars={len(full_response)} repeat={repeat_detected} tool_markers={stream_had_tool_markers}")
        return (full_response, repeat_detected, stream_had_tool_markers)

    # --- 承認フロー ---

    async def _resolve_pending(self, tool_name: str, result: str) -> str:
        if result == PENDING_MARKER:
            pending = get_pending_overwrite()
            if pending:
                await self._broadcast(json.dumps({
                    "type": "write_approval",
                    "path": pending["path"],
                    "old_size": len(pending["old_content"]),
                    "new_size": len(pending["content"]),
                    "old_content": pending["old_content"][:500],
                    "new_content": pending["content"][:500],
                }))
                resp = await self._wait_approval()
                if resp["action"] == "approve":
                    result = execute_pending_overwrite()
                    if resp.get("feedback"):
                        result += f"\nユーザーからのコメント: {resp['feedback']}"
                else:
                    cancel_pending_overwrite()
                    result = "ユーザーにより上書きを拒否されました。"
                    if resp.get("feedback"):
                        result += f"\n理由: {resp['feedback']}"
                    self._emit_signal("approval_denied", f"overwrite_file: {pending['path']}")

        elif result == PENDING_EXEC_MARKER:
            pending = get_pending_exec()
            if pending:
                risk = pending.get("risk", {})
                await self._broadcast(json.dumps({
                    "type": "exec_approval",
                    "code": pending["code"][:2000],
                    "risk_level": risk.get("level", "LOW"),
                    "risk_emoji": risk.get("emoji", "🟢"),
                    "risk_reasons": risk.get("reasons", []),
                }))
                resp = await self._wait_approval()
                if resp["action"] == "approve":
                    code = pop_pending_exec()
                    result = await self._stream_exec_code(code)
                    if resp.get("feedback"):
                        result += f"\nユーザーからのコメント: {resp['feedback']}"
                else:
                    cancel_pending_exec()
                    result = "ユーザーによりコード実行を拒否されました。"
                    if resp.get("feedback"):
                        result += f"\n理由: {resp['feedback']}"
                    self._emit_signal("approval_denied", "exec_code")

        elif result == PENDING_CREATE_TOOL_MARKER:
            pending = get_pending_create_tool()
            if pending:
                risk = pending.get("risk", {})
                await self._broadcast(json.dumps({
                    "type": "create_tool_approval",
                    "name": pending["name"],
                    "description": pending["description"],
                    "args_desc": pending["args_desc"],
                    "code": pending["code"][:2000],
                    "risk_level": risk.get("level", "LOW"),
                    "risk_emoji": risk.get("emoji", "🟢"),
                    "risk_reasons": risk.get("reasons", []),
                }))
                resp = await self._wait_approval()
                if resp["action"] == "approve":
                    result = execute_pending_create_tool()
                    if resp.get("feedback"):
                        result += f"\nユーザーからのコメント: {resp['feedback']}"
                else:
                    cancel_pending_create_tool()
                    result = "ユーザーによりツール作成を拒否されました。"
                    if resp.get("feedback"):
                        result += f"\n理由: {resp['feedback']}"
                    self._emit_signal("approval_denied", f"create_tool: {pending['name']}")

        return result

    async def _wait_approval(self) -> dict:
        from config import APPROVAL_TIMEOUT
        loop = asyncio.get_running_loop()
        self._pending_approval = loop.create_future()
        try:
            return await asyncio.wait_for(self._pending_approval, timeout=APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            minutes = int(APPROVAL_TIMEOUT // 60)
            # UI通知: タイムアウトしたことをユーザーに知らせる
            await self._broadcast(json.dumps({
                "type": "approval_timeout",
                "message": f"承認要求が{minutes}分間応答なしのためタイムアウトしました",
            }))
            return {"action": "reject", "feedback": f"承認要求がタイムアウトしました（{minutes}分）。ユーザーが不在だった可能性があります。"}
        finally:
            self._pending_approval = None

    # --- コード実行ストリーミング ---

    async def _stream_exec_code(self, code: str) -> str:
        backup_msg = _git_auto_backup()
        await self._broadcast(json.dumps({
            "type": "exec_start", "code": code, "backup": backup_msg,
        }))

        output_lines = []
        return_code = -1
        t0 = time.perf_counter()

        try:
            import os as _os
            env = {**_os.environ, "PYTHONIOENCODING": "utf-8"}
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(BASE_DIR),
                env=env,
            )

            async def read_stream(stream, stream_type):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip("\n")
                    output_lines.append(f"[{stream_type}] {text}")
                    await self._broadcast(json.dumps({
                        "type": "exec_output", "stream": stream_type, "content": text,
                    }))

            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        read_stream(proc.stdout, "stdout"),
                        read_stream(proc.stderr, "stderr"),
                    ),
                    timeout=EXEC_CODE_TIMEOUT,
                )
                return_code = await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await self._broadcast(json.dumps({
                    "type": "exec_output", "stream": "stderr",
                    "content": f"⏰ タイムアウト（{EXEC_CODE_TIMEOUT}秒）",
                }))
                output_lines.append(f"タイムアウト（{EXEC_CODE_TIMEOUT}秒）")

        except Exception as e:
            await self._broadcast(json.dumps({
                "type": "exec_output", "stream": "stderr",
                "content": f"実行エラー: {e}",
            }))
            output_lines.append(f"実行エラー: {e}")

        elapsed = time.perf_counter() - t0
        await self._broadcast(json.dumps({
            "type": "exec_end", "return_code": return_code, "elapsed": round(elapsed, 2),
        }))

        result_parts = [backup_msg]
        if output_lines:
            result_parts.append("\n".join(output_lines))
        if return_code == 0:
            result_parts.append(f"正常終了 ({round(elapsed, 2)}秒)")
        elif return_code == -1:
            result_parts.append("異常終了")
        else:
            result_parts.append(f"終了コード: {return_code} ({round(elapsed, 2)}秒)")
        if not output_lines and return_code == 0:
            result_parts.append("コード実行完了（出力なし）")

        result = "\n".join(result_parts)
        if len(result) > 5000:
            result = result[:5000] + "\n...（出力が長すぎるため省略）"
        return result

    # --- シグナル送信 ---

    def _emit_signal(self, signal_type: str, detail: str = ""):
        try:
            from app.scheduler.autonomous import scheduler
            scheduler.add_signal(signal_type, detail)
        except Exception:
            pass

    # --- プロンプト構築（非LLMコア） ---

    def _build_system_base(self) -> str:
        """ペルソナ + 自己モデルのテキスト構築"""
        from app.scheduler.autonomous import scheduler
        self_model = _load_self_model() if scheduler.ablation_self_model else {}
        sm_text = ""
        if self_model:
            sm_lines = []
            free_text = self_model.get("__free_text__")
            if free_text:
                sm_lines.append(free_text)
            for k, v in self_model.items():
                if k not in ("__free_text__", "motivation_rules"):
                    sm_lines.append(f"- {k}: {v}")
            if sm_lines:
                sm_text = "\n[self_model]\n" + "\n".join(sm_lines)
            principles = self_model.get("principles")
            if isinstance(principles, list) and principles:
                recent = principles[-5:]
                p_lines = [f"- {p['text']}" if isinstance(p, dict) and 'text' in p else f"- {p}" for p in recent]
                sm_text += "\n[characteristics]\n" + "\n".join(p_lines)

        return sm_text

    def _build_initial_prompt(
        self, action_goal: str, tool_text: str,
        memory_context: str = "", signal_summary: str = "",
        bootstrap_hint: str = "",
    ) -> str:
        """初回ラウンド用のプロンプト構築（マルチターンの起点）"""
        now = datetime.now().strftime('%Y年%m月%d日 %H:%M')

        ctx_parts = []
        if memory_context:
            ctx_parts.append(f"最近の記憶:\n{memory_context[:500]}")
        if signal_summary:
            ctx_parts.append(signal_summary)
        if bootstrap_hint:
            ctx_parts.append(bootstrap_hint)
        ctx_text = "\n".join(ctx_parts)

        parts = [f"【状況】\n日時: {now}"]
        if action_goal:
            parts.append(f"行動目標: {action_goal}")

        if ctx_text:
            parts.append(f"\n【コンテキスト】\n{ctx_text}")

        parts.append(f"\n【ツール】\n{tool_text}")

        return "\n".join(parts)

    def _trim_messages(self, messages: list[dict]) -> list[dict]:
        """コンテキストウィンドウ管理: system + 初回promptは常に保持、中間を圧縮"""
        # messages[0] = system, messages[1] = 初回prompt（ツール一覧含む）
        # 以降はassistant/userのペアが続く
        if len(messages) <= 2 + CONTEXT_KEEP_ROUNDS * 2:
            return messages  # 圧縮不要

        head = messages[:2]  # system + 初回prompt
        tail_count = CONTEXT_KEEP_ROUNDS * 2  # assistant + user のペア数
        tail = messages[-tail_count:]
        middle_count = len(messages) - 2 - tail_count
        skipped_rounds = middle_count // 2

        if skipped_rounds > 0:
            summary = {"role": "user", "content": f"[以前のやり取り: {skipped_rounds}ステップ省略]"}
            return head + [summary] + tail
        return messages

    # --- ユーティリティ ---

    @staticmethod
    def _strip_think(text: str) -> str:
        clean = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        if "</think>" in clean:
            clean = clean.split("</think>")[-1].strip()
        if "<think>" in clean:
            clean = clean.split("<think>")[0].strip()
        return clean

    @staticmethod
    def _trim_repeated(text: str) -> str:
        """ループ検出された応答から繰り返し部分を切り落とす（1回分だけ残す）"""
        llm = llm_manager.get()
        if hasattr(llm, "_find_repeat_start"):
            cut_pos = llm._find_repeat_start(text)
            if cut_pos > 0:
                return text[:cut_pos]
        return text

    @staticmethod
    def _summarize_result(tool_name: str, result: str, status: str) -> str:
        if status == "error":
            return f"エラー: {result[:60]}"
        match tool_name:
            case "output_UI":
                return f"出力完了（{len(result)}文字）"
            case "read_file":
                return f"取得成功（{result.count(chr(10)) + 1}行）"
            case "search_memories":
                return f"{result.count('---')}件ヒット"
            case "write_diary":
                return "日記保存完了"
            case "web_search":
                return f"{result.count('URL:')}件の結果"
            case "search_files":
                return f"{result.count(chr(10)) + 1 if result.strip() else 0}件マッチ"
            case "list_files":
                return f"{result.count(chr(10)) + 1 if result.strip() else 0}項目"
            case "create_file":
                return "ファイル作成完了"
            case "overwrite_file":
                return "ファイル上書き完了"
            case "read_self_model":
                return f"自己モデル取得（{len(result)}文字）"
            case "update_self_model":
                return "自己モデル更新完了"
            case "search_action_log":
                return f"{result.count('---')}件の行動ログ"
            case _:
                return f"{result[:80]}..." if len(result) > 80 else result


# グローバルインスタンス
pipeline = Pipeline()
