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
    PENDING_POST_X_MARKER, get_pending_post_x,
    execute_pending_post_x, cancel_pending_post_x,
    X_SESSION_EXPIRED_MARKER,
)
from app.memory.store import get_conversation_messages
from config import BASE_DIR, EXEC_CODE_TIMEOUT, CONTEXT_KEEP_ROUNDS, CHAT_HISTORY_MESSAGES, TOOL_MAX_CALLS_PER_RESPONSE, TOOL_SAME_NAME_LIMIT, PLAN_EXECUTE_ENABLED, PLAN_MAX_TOOLS, STRATEGY_CANDIDATES
from app.bandit import bandit_select_tools, compute_reward, update_reward

logger = logging.getLogger("iku.pipeline")


@dataclass
class PipelineRequest:
    """パイプラインへのリクエスト"""
    source: str  # "autonomous"（全て自律行動として処理）
    goal: str  # 行動目標（drivesから。空なら省略）
    conv_id: int | None = None
    memory_context: str = ""
    signal_summary: str = ""
    bootstrap_hint: str = ""
    selected_action: dict | None = None
    trigger: str | None = None  # "timer" / "energy" / "manual" / "user_stimulus"
    user_input: str = ""  # ユーザー入力（環境刺激として扱う。空なら省略）
    mirror_values: list = field(default_factory=list)  # 鏡の値（ツール順序変更・戦略選択用）


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
    strategy_text: str = ""  # 選択された戦略テキスト
    strategy_candidates: list = field(default_factory=list)  # 戦略候補一覧


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

        # 戦略候補数（ランタイム変更可能）
        self.strategy_candidates: int = STRATEGY_CANDIDATES

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
                step_status = s.get("status", "success")
                short_summary = self._summarize_result(tool_name, result_summary, step_status)
                rounds.append({
                    "tool_name": tool_name,
                    "result_summary": short_summary,
                    "result_raw": result_summary[:200] if len(result_summary) > 80 else result_summary,
                    "expected": expected if has_pred else None,
                    "intent": intent if has_intent else None,
                    "status": step_status,
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
        """全リクエスト共通パイプライン: 計画→実行（フォールバック: 1ショット）"""
        logger.info(f"パイプライン処理開始: source={req.source} trigger={req.trigger} goal={req.goal[:60]!r}")

        # === 1. セットアップ（共通） ===
        conv_id = req.conv_id
        if conv_id is None:
            async with async_session() as session:
                conv = await create_conversation(session, source=req.source, trigger=req.trigger, persona_id=get_active_persona_id())
                conv_id = conv.id
                await session.commit()

        preview = req.goal[:50] if req.goal else "自律行動"
        await self._broadcast(json.dumps({
            "type": "dev_session_start",
            "source": req.source,
            "preview": preview,
        }))
        await self._broadcast(json.dumps({"type": "autonomous_think_start"}))

        # ユーザー入力をDB記録（環境刺激としての記録）
        if req.user_input:
            async with async_session() as session:
                await add_message(session, conv_id, "user", req.user_input)
                await session.commit()

        step_history: list[dict] = []
        last_full_result = ""
        seen_tool_calls: set[str] = set()
        had_output = False
        plan_text = ""
        plan_response = ""
        response = ""

        try:
            system_base = self._build_system_base()
            action_goal = req.goal

            # セッション履歴の生成（認知連続性）
            from app.scheduler.autonomous import scheduler as _sched
            _sm_for_history = _load_self_model() if _sched.ablation_self_model else {}
            session_history = self._render_session_history(_sm_for_history)

            # 鏡の値からツール順序seed + 表示文字列生成
            mirror_seed = None
            mirror_display = ""
            if req.mirror_values:
                mirror_seed = int(sum(abs(v) * 10000 for v in req.mirror_values)) % (2**31)
                mirror_display = ", ".join(f"{v:.4f}" for v in req.mirror_values)

            # === 1.5 戦略候補生成 ===
            selected_strategy = ""
            strategy_candidates = []
            if PLAN_EXECUTE_ENABLED and self.strategy_candidates > 0:
                strategy_prompt = self._build_strategy_prompt(
                    action_goal, req.signal_summary,
                    user_input=req.user_input,
                    mirror_display=mirror_display,
                    session_history=session_history,
                )
                strat_messages = [
                    {"role": "system", "content": system_base or ""},
                    {"role": "user", "content": strategy_prompt},
                ]

                logger.info("戦略候補生成: LLM呼び出し")
                strat_response, _, _ = await self._call_llm_streaming(strat_messages, req.source, 0)

                if strat_response:
                    clean_strat = self._strip_think(strat_response)
                    strategy_candidates = self._parse_strategy_candidates(clean_strat)

                if strategy_candidates:
                    selected_strategy = await self._select_strategy_by_mirror(
                        strategy_candidates, req.mirror_values
                    )
                    logger.info(f"戦略選択: {selected_strategy} (候補{len(strategy_candidates)}件)")
                    await self._broadcast(json.dumps({
                        "type": "dev_strategy",
                        "candidates": strategy_candidates,
                        "selected": selected_strategy,
                    }))
                else:
                    logger.warning("戦略候補パース失敗 → 戦略なしで計画続行")

            # === 2. 計画フェーズ ===
            use_plan_execute = PLAN_EXECUTE_ENABLED
            planned_tools = None

            if use_plan_execute:
                from app.scheduler.autonomous import scheduler
                if scheduler.ablation_bandit:
                    # --- バンディット計画 ---
                    bandit_rewards = self._load_bandit_rewards()
                    all_tool_names = list(get_all_tools().keys())

                    planned_tools = bandit_select_tools(
                        all_tool_names=all_tool_names,
                        bandit_rewards=bandit_rewards,
                        available_energy=scheduler._motivation_energy,
                        action_costs_fn=lambda name: scheduler._get_action_cost_with_boredom(name),
                        max_tools=PLAN_MAX_TOOLS,
                    )

                    if not planned_tools:
                        logger.info(f"バンディット計画: ツール選択なし（energy={scheduler._motivation_energy:.1f}） → 1ショットにフォールバック")
                        use_plan_execute = False
                    else:
                        plan_text = " → ".join(planned_tools)
                        plan_response = f"[bandit] {plan_text}"
                        logger.info(f"バンディット計画: [{plan_text}]")
                        async with async_session() as session:
                            await add_message(session, conv_id, "assistant", plan_response)
                            await add_message(session, conv_id, "tool", f"[計画] {plan_text}")
                            await session.commit()
                        await self._broadcast(json.dumps({
                            "type": "dev_bandit_plan",
                            "tools": planned_tools,
                            "energy": scheduler._motivation_energy,
                        }))
                else:
                    # --- LLM計画 ---
                    planning_tool_text = build_planning_prompt(mirror_seed=mirror_seed)
                    planning_prompt = self._build_planning_prompt(
                        action_goal, planning_tool_text, req.signal_summary,
                        user_input=req.user_input,
                        selected_strategy=selected_strategy,
                        mirror_display=mirror_display,
                        session_history=session_history,
                    )
                    plan_messages = [
                        {"role": "system", "content": system_base or ""},
                        {"role": "user", "content": planning_prompt},
                    ]
                    logger.info("計画フェーズ: LLM呼び出し")
                    plan_response, _, _ = await self._call_llm_streaming(plan_messages, req.source, 0)

                    if plan_response:
                        clean_plan = self._strip_think(plan_response)
                        planned_tools = parse_plan(clean_plan)

                    if not planned_tools:
                        logger.warning("計画パース失敗 → 1ショットにフォールバック")
                        use_plan_execute = False
                    else:
                        planned_tools = planned_tools[:PLAN_MAX_TOOLS]
                        plan_text = " → ".join(planned_tools)
                        logger.info(f"計画確定: [{plan_text}]")
                        async with async_session() as session:
                            await add_message(session, conv_id, "assistant", plan_response)
                            await add_message(session, conv_id, "tool", f"[計画] {plan_text}")
                            await session.commit()

            # === 3. 実行フェーズ ===
            if use_plan_execute:
                # --- 計画-実行パス: 毎ラウンド自由選択 ---
                execution_results: list[dict] = []
                exec_tool_text = build_tools_prompt(mirror_seed=mirror_seed)
                max_rounds = len(planned_tools)

                for round_idx in range(max_rounds):
                    if self._stop_event.is_set():
                        logger.info("計画実行中断: ユーザーによる停止")
                        break

                    exec_prompt = self._build_execution_prompt(
                        action_goal=action_goal,
                        tool_text=exec_tool_text,
                        plan_text=plan_text,
                        previous_results=execution_results,
                        signal_summary=req.signal_summary,
                        round_idx=round_idx, total_rounds=max_rounds,
                        user_input=req.user_input,
                        mirror_display=mirror_display,
                        selected_strategy=selected_strategy,
                        session_history=session_history,
                    )
                    exec_messages = [
                        {"role": "system", "content": system_base or ""},
                        {"role": "user", "content": exec_prompt},
                    ]

                    logger.info(f"実行フェーズ {round_idx + 1}/{max_rounds}")
                    exec_response, repeat_detected, _ = await self._call_llm_streaming(
                        exec_messages, req.source, round_idx + 1
                    )

                    if not exec_response:
                        logger.warning(f"実行フェーズ {round_idx + 1}: LLM応答なし")
                        await self._broadcast(json.dumps({
                            "type": "dev_tool_result", "name": "(empty)",
                            "content": "（LLM応答なし — このラウンドはスキップ）",
                        }))
                        continue

                    if repeat_detected:
                        exec_response = self._trim_repeated(exec_response)

                    clean_exec = self._strip_think(exec_response)
                    tool_calls = parse_tool_calls(clean_exec) or parse_tool_calls(exec_response)

                    round_results = []
                    round_tool_name = None
                    hit_non_response = False
                    for tc_name, tc_args in (tool_calls or []):
                        if tc_name == "non_response":
                            hit_non_response = True
                        if round_tool_name is None:
                            round_tool_name = tc_name

                        result_text, status, tool_had_output = await self._execute_single_tool(
                            tc_name, tc_args, conv_id, req.source, seen_tool_calls, step_history,
                            mirror_values=req.mirror_values or None,
                        )
                        if tool_had_output:
                            had_output = True
                        if result_text:
                            round_results.append(result_text)

                    # stream参照
                    for sh in step_history:
                        if sh.get("stream") is None:
                            sh["stream"] = exec_response
                            break

                    # DB記録（ラウンドごと）
                    combined = "\n\n".join(round_results) if round_results else ""
                    async with async_session() as session:
                        await add_message(session, conv_id, "assistant", exec_response)
                        if combined:
                            await add_message(session, conv_id, "tool", combined)
                        await session.commit()

                    last_full_result = combined
                    actual_tool = round_tool_name or "(none)"
                    summary = self._summarize_result(actual_tool, combined, "success" if combined else "error") if combined else "実行なし"
                    execution_results.append({
                        "tool": actual_tool,
                        "result": combined[:500],
                        "summary": summary,
                    })

                    # non_responseで即終了
                    if hit_non_response:
                        logger.info("non_response検出: 実行ループ終了")
                        break

                # ツール未実行検出
                if not step_history:
                    self._emit_signal("tool_fail", "計画-実行: ツール未実行")

            else:
                # --- フォールバック: 1ショットLLM呼び出し ---
                tool_text = build_tools_prompt(mirror_seed=mirror_seed)
                messages = [{"role": "system", "content": system_base or ""}]

                initial_prompt = self._build_initial_prompt(
                    action_goal=action_goal,
                    tool_text=tool_text,
                    memory_context=req.memory_context,
                    signal_summary=req.signal_summary,
                    bootstrap_hint=req.bootstrap_hint,
                    user_input=req.user_input,
                    mirror_display=mirror_display,
                    session_history=session_history,
                )
                messages.append({"role": "user", "content": initial_prompt})

                trimmed = self._trim_messages(messages)

                logger.info(f"フォールバック1ショットLLM呼び出し: messages={len(trimmed)}")
                response, repeat_detected, stream_had_tool_markers = await self._call_llm_streaming(trimmed, req.source, 0)

                if not response:
                    logger.warning("LLM応答が空")
                elif self._stop_event.is_set():
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
                    if repeat_detected:
                        response = self._trim_repeated(response)

                    clean = self._strip_think(response)
                    tool_calls = parse_tool_calls(clean) or parse_tool_calls(response)

                    all_results = []
                    for tool_name, tool_args in (tool_calls or []):
                        result_text, status, tool_had_output = await self._execute_single_tool(
                            tool_name, tool_args, conv_id, req.source, seen_tool_calls, step_history,
                            mirror_values=req.mirror_values or None,
                        )
                        if tool_had_output:
                            had_output = True
                        if result_text:
                            all_results.append(result_text)

                    # stream参照
                    for sh in step_history:
                        if sh.get("stream") is None:
                            sh["stream"] = response

                    # ツール未実行検出
                    executed_tools = [s["tool"] for s in step_history if s.get("tool")]
                    if not executed_tools:
                        fail_msg = ("ツールマーカーを検出しましたが、正しい形式で実行されませんでした。"
                                    if stream_had_tool_markers else "ツールが実行されませんでした。")
                        all_results.append(fail_msg)
                        step_history.append({"tool": "(tool_fail)", "args_summary": "", "result_summary": fail_msg})
                        self._emit_signal("tool_fail", fail_msg)

                    # DB保存
                    combined_results = "\n\n".join(all_results)
                    last_full_result = combined_results
                    async with async_session() as session:
                        await add_message(session, conv_id, "assistant", response)
                        if combined_results:
                            await add_message(session, conv_id, "tool", combined_results)
                        await session.commit()

            # action_completeシグナル
            self._emit_signal("action_complete", action_goal[:50] if action_goal else req.source)

        except Exception as e:
            import traceback
            logger.error(f"パイプライン処理エラー: {e}\n{traceback.format_exc()}")
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
            last_response=response,
            plan_text=plan_text,
            plan_stream=plan_response,
            strategy_text=selected_strategy,
            strategy_candidates=strategy_candidates,
        )

    async def _execute_single_tool(
        self, tool_name: str, tool_args: dict, conv_id: int,
        req_source: str, seen_tool_calls: set, step_history: list,
        mirror_values: list | None = None,
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

        # 重複検出（failしたツールは除外: リトライを許可する）
        call_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
        if call_key in seen_tool_calls:
            logger.info(f"重複ツール呼び出し: {tool_name}")
            msg = f"[system] {tool_name} は既に全く同じ引数で実行済みです。結果は前回と同一になります。"
            step_history.append({"tool": tool_name, "args_summary": "", "result_summary": "重複（同一引数で実行済み）"})
            await self._broadcast(json.dumps({
                "type": "autonomous_tool", "name": tool_name,
                "args": "", "status": "skipped",
            }))
            return msg, "skipped", False

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

        # エネルギーチェック（output_UIは常に許可）
        if _sched.ablation_energy and tool_name != "output_UI":
            _energy = _sched._motivation_energy
            _sm = _load_self_model()
            _rules = _sm.get("motivation_rules")
            _ai_costs = _rules.get("action_costs", {}) if isinstance(_rules, dict) else {}
            from config import MOTIVATION_DEFAULT_ACTION_COSTS, MOTIVATION_DEFAULT_ACTION_COST_FALLBACK
            _cost = _ai_costs.get(tool_name,
                    MOTIVATION_DEFAULT_ACTION_COSTS.get(tool_name, MOTIVATION_DEFAULT_ACTION_COST_FALLBACK))
            if isinstance(_cost, (int, float)) and _cost > 0 and _energy < _cost:
                fail_msg = (
                    f"[system] tool実行不可: "
                    f"motivation_energy={_energy:.1f} < action_costs.{tool_name}={_cost}"
                )
                logger.info(f"エネルギー不足: {tool_name} energy={_energy:.1f} cost={_cost}")
                step_history.append({
                    "tool": tool_name, "args_summary": args_str,
                    "result_summary": fail_msg,
                    "intent": intent, "expected": expected,
                    "status": "fail",
                })
                self._emit_signal("tool_fail", f"energy_insufficient:{tool_name}")
                await self._broadcast(json.dumps({
                    "type": "dev_tool_result", "name": tool_name, "content": fail_msg,
                }))
                await self._broadcast(json.dumps({
                    "type": "autonomous_tool", "name": tool_name,
                    "args": args_str, "status": "error",
                }))
                # DB記録（エネルギー不足）
                mirror_json = None
                if mirror_values:
                    mirror_json = json.dumps(mirror_values)
                async with async_session() as session:
                    await record_tool_action(
                        session, conv_id, tool_name, tool_args,
                        fail_msg, "fail", None,
                        expected_result=expected,
                        intent=intent,
                        persona_id=get_active_persona_id(),
                        mirror=mirror_json,
                    )
                    await session.commit()
                return fail_msg, "fail", False

        # エネルギーチェック通過 → 重複セットに追加（failは含めない）
        seen_tool_calls.add(call_key)

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

        # 予測誤差 → エネルギー変調（逆U字カーブ）
        pred_accuracy = None
        if expected and action_status == "success":
            try:
                from app.memory.vector_store import _embed_sync, cosine_similarity
                embs = _embed_sync([expected, result[:500]])
                if embs and len(embs) == 2:
                    sim = cosine_similarity(embs[0], embs[1])
                    pred_accuracy = sim
                    pred_energy = self._compute_prediction_energy(sim)
                    self._emit_signal("prediction_made", f"{tool_name}: sim={sim:.2f}", weight_override=pred_energy)
                    logger.info(f"予測誤差エネルギー: {tool_name} sim={sim:.2f} → energy={pred_energy:.1f}")
                else:
                    self._emit_signal("prediction_made", f"{tool_name}: {expected[:50]}")
            except Exception as e:
                logger.debug(f"予測誤差embedding失敗: {e}")
                self._emit_signal("prediction_made", f"{tool_name}: {expected[:50]}")
        elif expected:
            self._emit_signal("prediction_made", f"{tool_name}: {expected[:50]}")

        # 意図達成度 → エネルギー変調
        if intent and action_status == "success":
            try:
                from app.memory.vector_store import _embed_sync, cosine_similarity
                embs = _embed_sync([intent, result[:500]])
                if embs and len(embs) == 2:
                    sim = cosine_similarity(embs[0], embs[1])
                    intent_energy = self._compute_intent_energy(sim)
                    self._emit_signal("intent_result", f"{tool_name}: sim={sim:.2f}", weight_override=intent_energy)
                    logger.info(f"意図達成度エネルギー: {tool_name} sim={sim:.2f} → energy={intent_energy:.1f}")
            except Exception as e:
                logger.debug(f"意図達成度embedding失敗: {e}")

        # エネルギー消費（退屈乗数統合済み）
        from app.scheduler.autonomous import scheduler
        scheduler.consume_energy(tool_name)

        # ツール使用記録（退屈乗数・習熟検出用）
        scheduler.record_tool_usage(tool_name, pred_accuracy)

        # バンディット報酬更新
        reward_val = compute_reward(pred_accuracy)
        bandit_rw = self._load_bandit_rewards()
        update_reward(bandit_rw, tool_name, reward_val)
        self._save_bandit_rewards(bandit_rw)

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
        mirror_json = None
        if mirror_values:
            mirror_json = json.dumps(mirror_values)
        async with async_session() as session:
            await record_tool_action(
                session, conv_id, tool_name, tool_args,
                result, action_status, exec_ms,
                expected_result=expected,
                intent=intent,
                persona_id=get_active_persona_id(),
                mirror=mirror_json,
            )
            await session.commit()

        # step_history
        step_history.append({
            "tool": tool_name,
            "args_summary": args_str[:80],
            "result_summary": self._summarize_result(tool_name, result, action_status),
            "expected": expected,
            "intent": intent,
            "status": action_status,
        })

        return result_text, action_status, had_output

    def _build_strategy_prompt(self, action_goal: str, signal_summary: str,
                               user_input: str = "", mirror_display: str = "",
                               session_history: str = "") -> str:
        """戦略候補生成用プロンプト（ツール名を含めない — ツール選択はバンディットの役割）"""
        now = datetime.now().strftime('%Y年%m月%d日 %H:%M')
        goal_line = f"\n行動目標: {action_goal}" if action_goal else ""
        signal_line = f"\n{signal_summary}" if signal_summary else ""
        user_line = f"\n\n【ユーザー入力】\n{user_input}" if user_input else ""
        mirror_line = f"\n[{mirror_display}]" if mirror_display else ""
        session_line = f"\n\n{session_history}" if session_history else ""

        return f"""【状況】
日時: {now}{goal_line}{signal_line}{mirror_line}{session_line}{user_line}

【能力カテゴリ】
ファイル読み書き、記憶の検索と記録、自己モデルの更新、Web検索と情報取得、コード実行と拡張、システム状態の確認、発言と沈黙

【出力指示】
上記の能力を使って取りうるアプローチを{self.strategy_candidates}つ、それぞれ異なる方向性で列挙する。具体的なツール名は使わず、方針・意図・目的を記述する。各アプローチは1行で簡潔に。

形式:
A. アプローチの説明
B. アプローチの説明
C. アプローチの説明"""

    def _parse_strategy_candidates(self, text: str) -> list[str]:
        """戦略候補テキストをパースしてリストで返す"""
        candidates = []
        for line in text.strip().split("\n"):
            line = line.strip()
            # "A. ...", "B. ...", "1. ...", "- ..." 等のパターン
            m = re.match(r'^(?:[A-Z][\.\):]|[0-9]+[\.\):]|\-)\s*(.+)', line)
            if m:
                candidate = m.group(1).strip()
                if candidate:
                    candidates.append(candidate)
        return candidates

    async def _select_strategy_by_mirror(self, candidates: list[str], mirror_values: list) -> str:
        """mirror cosine類似度で戦略候補を選択。mirrorなしならランダム"""
        import random as _rand
        if not mirror_values or len(mirror_values) < 5:
            return _rand.choice(candidates)

        try:
            from app.memory.vector_store import _embed_sync
            loop = asyncio.get_event_loop()
            embeddings = await loop.run_in_executor(None, _embed_sync, candidates)
            if not embeddings or not embeddings[0]:
                return _rand.choice(candidates)

            # mirrorと同じ5次元を抽出してcosine similarity
            dim = len(embeddings[0])
            indices = [int(i * dim / 5) for i in range(5)]

            best_idx = 0
            best_sim = -2.0
            for i, emb in enumerate(embeddings):
                emb_5 = [emb[idx] for idx in indices]
                # cosine similarity
                dot = sum(a * b for a, b in zip(mirror_values, emb_5))
                norm_m = sum(a * a for a in mirror_values) ** 0.5
                norm_e = sum(a * a for a in emb_5) ** 0.5
                sim = dot / (norm_m * norm_e) if norm_m > 0 and norm_e > 0 else 0
                if sim > best_sim:
                    best_sim = sim
                    best_idx = i

            logger.info(f"戦略選択（mirror cosine）: idx={best_idx}, sim={best_sim:.4f}")
            return candidates[best_idx]
        except Exception as e:
            logger.warning(f"mirror戦略選択エラー → ランダムフォールバック: {e}")
            return _rand.choice(candidates)

    def _build_planning_prompt(self, action_goal: str, tool_text: str, signal_summary: str,
                               user_input: str = "",
                               selected_strategy: str = "", mirror_display: str = "",
                               session_history: str = "") -> str:
        """計画フェーズ用プロンプト"""
        now = datetime.now().strftime('%Y年%m月%d日 %H:%M')
        goal_line = f"\n行動目標: {action_goal}" if action_goal else ""
        strategy_line = f"\nアプローチ: {selected_strategy}" if selected_strategy else ""
        signal_line = f"\n{signal_summary}" if signal_summary else ""
        user_line = f"\n\n【ユーザー入力】\n{user_input}" if user_input else ""
        mirror_line = f"\n[{mirror_display}]" if mirror_display else ""
        session_line = f"\n\n{session_history}" if session_history else ""

        return f"""【状況】
日時: {now}{goal_line}{strategy_line}{signal_line}{mirror_line}{session_line}{user_line}

【利用可能ツール】
{tool_text}

【出力指示】
上記ツールから実行順序を計画する。各ステップで前の結果を参照可能。最大{PLAN_MAX_TOOLS}個。

形式:
1. ツール名
2. ツール名

ツール呼び出し（[TOOL:...]）は不要。計画のみ出力。"""

    def _build_execution_prompt(
        self, action_goal: str, tool_text: str, plan_text: str,
        previous_results: list[dict], signal_summary: str,
        round_idx: int, total_rounds: int,
        user_input: str = "", mirror_display: str = "",
        selected_strategy: str = "",
        session_history: str = "",
    ) -> str:
        """実行フェーズ用プロンプト（自由選択）"""
        now = datetime.now().strftime('%Y年%m月%d日 %H:%M')
        goal_line = f"\n行動目標: {action_goal}" if action_goal else ""
        strategy_line = f"\nアプローチ: {selected_strategy}" if selected_strategy else ""
        user_line = f"\n\n【ユーザー入力】\n{user_input}" if user_input else ""
        mirror_line = f"\n[{mirror_display}]" if mirror_display else ""
        session_line = f"\n\n{session_history}" if session_history else ""

        # 計画
        plan_line = f"\n計画: {plan_text}" if plan_text else ""

        # これまでの結果
        prev_text = ""
        if previous_results:
            prev_lines = []
            for i, pr in enumerate(previous_results):
                prev_lines.append(f"- R{i + 1} {pr['tool']}: {pr['summary']}")
                if pr.get("result"):
                    prev_lines.append(f"  結果: {pr['result'][:300]}")
            prev_text = "\n\n【これまでの結果】\n" + "\n".join(prev_lines)

        return f"""【状況】
日時: {now}{goal_line}{strategy_line}{plan_line}{mirror_line}{session_line}
ラウンド {round_idx + 1}/{total_rounds}{user_line}{prev_text}

【利用可能ツール】
{tool_text}

【出力指示】
ツールを1つ選んで呼び出す。expect=に結果の予測を記述すること。
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

        elif result == PENDING_POST_X_MARKER:
            pending = get_pending_post_x()
            if pending:
                await self._broadcast(json.dumps({
                    "type": "post_x_approval",
                    "text": pending["text"],
                    "char_count": len(pending["text"]),
                }))
                resp = await self._wait_approval()
                if resp["action"] == "approve":
                    result = await execute_pending_post_x()
                    if result == X_SESSION_EXPIRED_MARKER:
                        await self._broadcast(json.dumps({"type": "x_session_expired"}))
                        result = "エラー: Xのセッションが切れています。開発者タブの「Xにログイン」から再ログインしてください。"
                    elif resp.get("feedback"):
                        result += f"\nユーザーからのコメント: {resp['feedback']}"
                else:
                    cancel_pending_post_x()
                    result = "ユーザーにより投稿を拒否されました。"
                    if resp.get("feedback"):
                        result += f"\n理由: {resp['feedback']}"
                    self._emit_signal("approval_denied", "post_to_x")

        # check_x_notificationsのセッション切れ検出
        elif result == X_SESSION_EXPIRED_MARKER:
            await self._broadcast(json.dumps({"type": "x_session_expired"}))
            result = "エラー: Xのセッションが切れています。開発者タブの「Xにログイン」から再ログインしてください。"

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

    def _emit_signal(self, signal_type: str, detail: str = "", weight_override: float | None = None):
        try:
            from app.scheduler.autonomous import scheduler
            scheduler.add_signal(signal_type, detail, weight_override=weight_override)
        except Exception:
            pass

    # --- バンディット報酬永続化 ---

    def _load_bandit_rewards(self) -> dict:
        sm = _load_self_model()
        r = sm.get("bandit_rewards", {})
        return r if isinstance(r, dict) else {}

    def _save_bandit_rewards(self, rewards: dict):
        sm = _load_self_model()
        sm["bandit_rewards"] = rewards
        from app.tools.builtin import _save_self_model
        _save_self_model(sm, changed_key="bandit_rewards")

    # --- 情報的実存: エネルギー変調 ---

    @staticmethod
    def _compute_prediction_energy(similarity: float) -> float:
        """逆U字カーブ: 中程度の予測誤差で最大エネルギー"""
        from config import PREDICTION_ENERGY_PEAK
        return PREDICTION_ENERGY_PEAK * 4 * similarity * (1 - similarity)

    @staticmethod
    def _compute_intent_energy(similarity: float) -> float:
        """意図達成度: 未達成で高エネルギー（再行動促進）、達成で適度"""
        INTENT_BASE = 5.0
        INTENT_UNFULFILLED_BONUS = 15.0
        return INTENT_BASE + INTENT_UNFULFILLED_BONUS * (1 - similarity)

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
                if k not in ("__free_text__", "motivation_rules", "session_log", "session_archive"):
                    sm_lines.append(f"- {k}: {v}")
            if sm_lines:
                sm_text = "\n[self_model]\n" + "\n".join(sm_lines)
            principles = self_model.get("principles")
            if isinstance(principles, list) and principles:
                recent = principles[-5:]
                p_lines = [f"- {p['text']}" if isinstance(p, dict) and 'text' in p else f"- {p}" for p in recent]
                sm_text += "\n[characteristics]\n" + "\n".join(p_lines)

        return sm_text

    def _render_session_history(self, self_model: dict) -> str:
        """session_archive + session_logからセッション履歴テキストを生成"""
        parts = []

        # アーカイブ（圧縮済み1行形式）
        archive = self_model.get("session_archive", "")
        if isinstance(archive, str) and archive.strip():
            parts.append(archive.strip())

        # 直近のセッションログ（詳細形式）
        log = self_model.get("session_log", [])
        if isinstance(log, list):
            for s in log:
                tools_parts = []
                for st in s.get("steps", []):
                    tool_str = st.get("tool", "?")
                    summary = st.get("summary", "")
                    extras = []
                    if summary:
                        extras.append(summary)
                    if st.get("intent"):
                        extras.append(f"intent={st['intent']}")
                    if st.get("expect"):
                        extras.append(f"expect={st['expect']}")
                    if extras:
                        tool_str += f"({', '.join(extras)})"
                    tools_parts.append(tool_str)
                tools_chain = " → ".join(tools_parts) if tools_parts else "(no action)"
                sm_part = ""
                if s.get("self_model_changed"):
                    sm_part = f" [sm:{','.join(s['self_model_changed'])}]"
                time_str = s.get("time", "?")
                # time_strから日付部分を省略（HH:MMだけ）
                if " " in time_str:
                    time_str = time_str.split(" ", 1)[1]
                line = f"#{s.get('session', '?')} {time_str} {s.get('trigger', '?')} → {tools_chain}{sm_part}"
                parts.append(line)

        if not parts:
            return ""
        return "【直近のセッション履歴】\n" + "\n".join(parts)

    def _build_initial_prompt(
        self, action_goal: str, tool_text: str,
        memory_context: str = "", signal_summary: str = "",
        bootstrap_hint: str = "", user_input: str = "",
        mirror_display: str = "",
        session_history: str = "",
    ) -> str:
        """初回ラウンド用のプロンプト構築（フォールバック1ショット用）"""
        now = datetime.now().strftime('%Y年%m月%d日 %H:%M')

        ctx_parts = []
        if session_history:
            ctx_parts.append(session_history)
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
        if mirror_display:
            parts.append(f"[{mirror_display}]")

        if user_input:
            parts.append(f"\n【ユーザー入力】\n{user_input}")

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
        if status == "fail":
            return f"失敗: {result[:60]}"
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
            case "update_self_model":
                return "自己モデル更新完了"
            case "search_action_log":
                # 各行が "- [timestamp]" で始まる
                return f"{result.count(chr(10) + '- [')}件の行動ログ"
            case _:
                return f"{result[:80]}..." if len(result) > 80 else result


# グローバルインスタンス
pipeline = Pipeline()
