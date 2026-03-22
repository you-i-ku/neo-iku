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
from app.persona.system_prompt import get_mode, IKU_SYSTEM_PROMPT
from app.tools.registry import parse_tool_calls, execute_tool, build_tools_prompt
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
from config import BASE_DIR, EXEC_CODE_TIMEOUT, CONTEXT_KEEP_ROUNDS, CHAT_HISTORY_MESSAGES

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


@dataclass
class PipelineResult:
    """パイプラインの実行結果"""
    conv_id: int = 0
    step_history: list = field(default_factory=list)
    last_full_result: str = ""
    had_output: bool = False
    last_response: str = ""


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
        import config as _config

        logger.info(f"パイプライン処理開始: source={req.source} goal={req.goal[:60]!r}")

        # DB会話作成
        conv_id = req.conv_id
        if conv_id is None:
            async with async_session() as session:
                conv = await create_conversation(session)
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
        tool_round = 0
        seen_tool_calls: set[str] = set()
        output_count = 0
        had_output = False
        response = ""

        try:
            # 共通コンテキスト構築
            tool_text = build_tools_prompt()
            logger.debug(f"tool_text 構築完了: {len(tool_text)} chars")
            system_base = self._build_system_base()
            logger.debug(f"system_base 構築完了: {len(system_base)} chars")
            memory_context = req.memory_context
            # 記憶検索はAIが自分でsearch_memoriesツールを使って行う（自動注入しない）

            # chat の場合、ユーザーメッセージをDB保存
            if req.source == "chat":
                async with async_session() as session:
                    await add_message(session, conv_id, "user", req.goal)
                    await session.commit()
                self._emit_signal("user_message", req.goal[:50])
                logger.debug("ユーザーメッセージDB保存完了")

            # --- マルチターンmessages構築 ---
            messages = [
                {"role": "system", "content": system_base or ""},
            ]

            # Phase 4: 会話継続 — 既存conv_idが渡された場合、過去のやり取りをロード
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

            for _ in range(_config.TOOL_MAX_ROUNDS + 2):
                # ユーザー割り込みチェック
                while not self._interrupt_queue.empty():
                    try:
                        msg = self._interrupt_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    step_history.append({
                        "tool": "(ユーザー割り込み)",
                        "args_summary": msg[:80],
                        "result_summary": "ユーザーメッセージ",
                    })
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

                # ストリーミングLLM
                logger.info(f"LLM呼び出し: round={tool_round} messages={len(trimmed)}")
                response = await self._call_llm_streaming(trimmed, req.source, tool_round)
                logger.info(f"LLM応答: round={tool_round} response_len={len(response)}")
                if not response:
                    logger.warning(f"LLM応答が空: round={tool_round} — LM Studioが応答しているか確認してください")
                    break

                # 中断チェック
                if self._stop_event.is_set():
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
                    break

                clean = self._strip_think(response)

                # assistantメッセージをmessagesに追加（thinkタグ除去）
                messages.append({"role": "assistant", "content": clean})

                # ツール上限チェック
                tool_calls = []
                if tool_round < _config.TOOL_MAX_ROUNDS:
                    tool_calls = parse_tool_calls(clean) or parse_tool_calls(response)
                elif parse_tool_calls(clean) or parse_tool_calls(response):
                    limit_msg = f"[ツール実行上限（{_config.TOOL_MAX_ROUNDS}回）に達しました。ツールなしで応答を完了してください。]"
                    async with async_session() as session:
                        await add_message(session, conv_id, "assistant", response)
                        await add_message(session, conv_id, "tool", limit_msg)
                        await session.commit()
                    messages.append({"role": "user", "content": limit_msg})
                    last_full_result = limit_msg
                    step_history.append({"tool": "(上限到達)", "args_summary": "", "result_summary": f"上限{_config.TOOL_MAX_ROUNDS}回到達"})
                    tool_round += 1
                    continue

                if not tool_calls:
                    # ツールなし → 最終応答（AIが自律的にループを終了した）
                    if clean:
                        async with async_session() as session:
                            await add_message(session, conv_id, "assistant", response)
                            await session.commit()
                    break

                # --- ツール実行 ---
                tool_round += 1
                all_results = []

                for tool_name, tool_args in tool_calls:
                    expected = tool_args.pop("expect", None)
                    if expected is not None and expected.strip().lower() in ("skip", "", "-", "なし", "none"):
                        expected = None

                    # 重複検出
                    call_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
                    if call_key in seen_tool_calls:
                        logger.info(f"重複ツール呼び出しスキップ: {tool_name}")
                        all_results.append(f"[ツール結果: {tool_name}]\n同じツールを同じ引数で再度呼び出しました。既に結果は返しています。目的を達成したならツールを呼ばずに応答を完了してください。")
                        step_history.append({"tool": tool_name, "args_summary": "", "result_summary": "重複スキップ"})
                        continue
                    seen_tool_calls.add(call_key)

                    logger.info(f"ツール: {tool_name} {tool_args}")
                    args_str = " ".join(f"{k}={v}" for k, v in tool_args.items()) if tool_args else ""
                    is_output = tool_name == "output"

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
                    if expected and action_status == "success":
                        self._emit_signal("prediction_error", f"{tool_name}: expected={expected}")

                    # dev tab結果
                    await self._broadcast(json.dumps({
                        "type": "dev_tool_result",
                        "name": tool_name,
                        "content": result[:2000] if len(result) > 2000 else result,
                    }))

                    # output処理
                    if is_output and action_status == "success":
                        output_count += 1
                        had_output = True
                        await self._broadcast(json.dumps({"type": "tool_call", "content": "output"}))
                        await self._broadcast(json.dumps({
                            "type": "output", "content": result, "source": req.source,
                        }))
                        output_feedback = f"出力完了（{len(result)}文字）。追加の行動がなければツールを呼ばずに応答を完了してください。"
                        if output_count >= 2:
                            output_feedback += f"\n※あなたはこのセッションでoutputツールを{output_count}回呼び出しています。伝えたいことは既に出力済みではありませんか？"
                        all_results.append(f"[ツール結果: {tool_name}]\n{output_feedback}")
                    else:
                        if not is_output:
                            await self._broadcast(json.dumps({
                                "type": "autonomous_tool", "name": tool_name,
                                "args": args_str, "status": action_status,
                            }))
                        if expected:
                            all_results.append(f"[ツール結果: {tool_name}]\nあなたの予測: {expected}\n実際の結果: {result}\n（予測と結果にズレがあれば、自分の理解の何が違ったか振り返り、必要ならupdate_self_modelで自己モデルを更新できます）")
                        else:
                            all_results.append(f"[ツール結果: {tool_name}]\n{result}")

                    # DB記録
                    async with async_session() as session:
                        await record_tool_action(
                            session, conv_id, tool_name, tool_args,
                            result, action_status, exec_ms,
                            expected_result=expected,
                        )
                        await session.commit()

                    # step_history
                    step_history.append({
                        "tool": tool_name,
                        "args_summary": args_str[:80],
                        "result_summary": self._summarize_result(tool_name, result, action_status),
                    })

                combined_results = "\n\n".join(all_results)
                last_full_result = combined_results

                # ツール結果をuserロールでmessagesに追加
                messages.append({"role": "user", "content": combined_results})

                # DB保存（中間）
                async with async_session() as session:
                    await add_message(session, conv_id, "assistant", response)
                    await add_message(session, conv_id, "tool", combined_results)
                    await session.commit()

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

        return PipelineResult(
            conv_id=conv_id,
            step_history=step_history,
            last_full_result=last_full_result,
            had_output=had_output,
            last_response=response,
        )

    # --- ストリーミングLLM ---

    async def _call_llm_streaming(self, messages: list[dict], source: str, round_num: int) -> str:
        llm = llm_manager.get()
        full_response = ""
        in_think = False

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
                            await self._broadcast(json.dumps({"type": "dev_think_start"}))
                            buf = buf[start_idx + 7:]
                            in_think = True
                        else:
                            await self._broadcast(json.dumps({"type": "dev_stream", "content": buf}))
                            buf = ""

            if in_think:
                await self._broadcast(json.dumps({"type": "dev_think_end"}))

        except Exception as e:
            import traceback
            logger.error(f"LLMストリーミングエラー: {e}\n{traceback.format_exc()}")
            await self._broadcast(json.dumps({
                "type": "error",
                "content": f"（LLMエラー: {e}）",
            }))
            return ""

        logger.info(f"stream_chat 完了: total_chars={len(full_response)}")
        return full_response

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

        return result

    async def _wait_approval(self, timeout: float = 300) -> dict:
        loop = asyncio.get_running_loop()
        self._pending_approval = loop.create_future()
        try:
            return await asyncio.wait_for(self._pending_approval, timeout=timeout)
        except asyncio.TimeoutError:
            return {"action": "reject", "feedback": "承認タイムアウト（5分）"}
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
        self_model = _load_self_model()
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
                sm_text = "\nあなたの自己モデル（自分自身についての現在の理解）:\n" + "\n".join(sm_lines)
            principles = self_model.get("principles")
            if isinstance(principles, list) and principles:
                recent = principles[-5:]
                p_lines = [f"- {p['text']}" if isinstance(p, dict) and 'text' in p else f"- {p}" for p in recent]
                sm_text += "\nあなたが経験から蒸留した原則:\n" + "\n".join(p_lines)

        if get_mode() == "iku":
            return f"{IKU_SYSTEM_PROMPT}\n{sm_text}"
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

        return f"""今は{now}です。

行動目標: {action_goal}
{f'{chr(10)}{ctx_text}' if ctx_text else ''}

{tool_text}"""

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
    def _summarize_result(tool_name: str, result: str, status: str) -> str:
        if status == "error":
            return f"エラー: {result[:60]}"
        match tool_name:
            case "output":
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
