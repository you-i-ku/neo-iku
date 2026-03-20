"""WebSocketチャットルート"""
import asyncio
import json
import re
import sys
import time
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.llm.manager import llm_manager
from app.memory.database import async_session
from app.memory.store import (
    create_conversation, add_message, end_conversation,
    get_conversation_messages, record_tool_action,
)
from app.memory.search import search_messages, search_iku_logs
from app.persona.system_prompt import build_system_messages, get_mode
from app.scheduler.autonomous import scheduler
from app.tools.registry import parse_tool_calls, execute_tool
from app.tools.builtin import (
    PENDING_MARKER, get_pending_overwrite,
    execute_pending_overwrite, cancel_pending_overwrite,
    PENDING_EXEC_MARKER, get_pending_exec,
    pop_pending_exec, cancel_pending_exec,
    _git_auto_backup,
    PENDING_CREATE_TOOL_MARKER, get_pending_create_tool,
    execute_pending_create_tool, cancel_pending_create_tool,
)
from config import BASE_DIR, EXEC_CODE_TIMEOUT

logger = logging.getLogger("iku.chat")
router = APIRouter()


async def _stream_exec_code(ws: WebSocket, code: str) -> str:
    """コードをストリーミング実行し、UIにリアルタイム出力を送る"""
    backup_msg = _git_auto_backup()

    await ws.send_text(json.dumps({
        "type": "exec_start",
        "code": code,
        "backup": backup_msg,
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
                await ws.send_text(json.dumps({
                    "type": "exec_output",
                    "stream": stream_type,
                    "content": text,
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
            await ws.send_text(json.dumps({
                "type": "exec_output",
                "stream": "stderr",
                "content": f"⏰ タイムアウト（{EXEC_CODE_TIMEOUT}秒）",
            }))
            output_lines.append(f"タイムアウト（{EXEC_CODE_TIMEOUT}秒）")
            return_code = -1

    except Exception as e:
        await ws.send_text(json.dumps({
            "type": "exec_output",
            "stream": "stderr",
            "content": f"実行エラー: {e}",
        }))
        output_lines.append(f"実行エラー: {e}")

    elapsed = time.perf_counter() - t0

    await ws.send_text(json.dumps({
        "type": "exec_end",
        "return_code": return_code,
        "elapsed": round(elapsed, 2),
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


@router.websocket("/ws/chat")
async def chat_ws(ws: WebSocket):
    await ws.accept()
    await scheduler.register_ws(ws)
    conversation_id = None

    msg_queue: asyncio.Queue = asyncio.Queue()
    pending_user_msgs: list[str] = []
    stop_event = asyncio.Event()
    stop_feedback = ""

    async def ws_reader():
        nonlocal stop_feedback
        try:
            while True:
                raw = await ws.receive_text()
                data = json.loads(raw)
                if data.get("type") == "stop":
                    stop_feedback = data.get("feedback", "")
                    stop_event.set()
                else:
                    await msg_queue.put(data)
        except WebSocketDisconnect:
            await msg_queue.put(None)
        except Exception:
            await msg_queue.put(None)

    reader_task = asyncio.create_task(ws_reader())

    async def wait_for_response(response_type: str) -> dict:
        while True:
            data = await msg_queue.get()
            if data is None:
                raise WebSocketDisconnect()
            if data.get("type") == response_type:
                return data
            user_text = data.get("message", "").strip()
            if user_text:
                pending_user_msgs.append(user_text)

    def drain_pending_messages():
        while not msg_queue.empty():
            try:
                data = msg_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if data is None:
                return
            user_text = data.get("message", "").strip()
            if user_text:
                pending_user_msgs.append(user_text)

    try:
        async with async_session() as session:
            conv = await create_conversation(session)
            conversation_id = conv.id
            await session.commit()

            while True:
                data = await msg_queue.get()
                if data is None:
                    break
                user_text = data.get("message", "").strip()
                if not user_text:
                    continue

                await add_message(session, conversation_id, "user", user_text)
                await session.commit()

                # 動機シグナル: ユーザーメッセージ
                scheduler.add_signal("user_message", user_text[:50])

                memories = await search_messages(session, user_text)
                iku_memories = await search_iku_logs(session, user_text) if get_mode() == "iku" else None

                system_msgs = build_system_messages(memories, iku_memories)
                conv_messages = await get_conversation_messages(session, conversation_id)
                history = [{"role": m.role, "content": m.content} for m in conv_messages]
                all_messages = system_msgs + history

                from config import TOOL_MAX_ROUNDS
                max_tool_rounds = TOOL_MAX_ROUNDS
                seen_tool_calls: set[str] = set()  # 重複検出用
                output_count = 0  # outputツール呼び出し回数（セッション通算）

                # チャット処理開始（thinking表示 + 開発者タブのセッション開始）
                await ws.send_text(json.dumps({"type": "processing_start"}))
                await ws.send_text(json.dumps({
                    "type": "dev_session_start",
                    "source": "chat",
                    "preview": user_text[:50],
                }))

                for tool_round in range(max_tool_rounds + 2):

                    # --- ユーザー割り込みチェック ---
                    drain_pending_messages()
                    if pending_user_msgs:
                        for msg in pending_user_msgs:
                            all_messages.append({"role": "user", "content": msg})
                            await add_message(session, conversation_id, "user", msg)
                            await ws.send_text(json.dumps({
                                "type": "user_interrupt_ack",
                                "content": msg,
                            }))
                            logger.info(f"ユーザー割り込み: {msg[:50]}...")
                        pending_user_msgs.clear()
                        await session.commit()

                    # LLMからストリーミング応答（開発者タブに送信）
                    llm = llm_manager.get()
                    full_response = ""
                    in_think = False

                    stopped = False
                    try:
                        chunk_count = 0
                        # 開発者タブ: ラウンド開始
                        await ws.send_text(json.dumps({
                            "type": "dev_round_start",
                            "round": tool_round + 1,
                            "source": "chat",
                        }))

                        async for chunk in llm.stream_chat(all_messages):
                            if stop_event.is_set():
                                stopped = True
                                break
                            chunk_count += 1
                            full_response += chunk

                            # <think>タグの検出・分離 → 開発者タブへ送信
                            buf = chunk
                            while buf:
                                if in_think:
                                    end_idx = buf.find("</think>")
                                    if end_idx != -1:
                                        think_part = buf[:end_idx]
                                        if think_part:
                                            await ws.send_text(json.dumps({
                                                "type": "dev_think",
                                                "content": think_part,
                                            }))
                                        await ws.send_text(json.dumps({"type": "dev_think_end"}))
                                        buf = buf[end_idx + 8:]
                                        in_think = False
                                    else:
                                        await ws.send_text(json.dumps({
                                            "type": "dev_think",
                                            "content": buf,
                                        }))
                                        buf = ""
                                else:
                                    start_idx = buf.find("<think>")
                                    if start_idx != -1:
                                        before = buf[:start_idx]
                                        if before:
                                            await ws.send_text(json.dumps({
                                                "type": "dev_stream",
                                                "content": before,
                                            }))
                                        await ws.send_text(json.dumps({"type": "dev_think_start"}))
                                        buf = buf[start_idx + 7:]
                                        in_think = True
                                    else:
                                        await ws.send_text(json.dumps({
                                            "type": "dev_stream",
                                            "content": buf,
                                        }))
                                        buf = ""

                        # thinkタグが閉じないままストリーム終了
                        if in_think:
                            await ws.send_text(json.dumps({"type": "dev_think_end"}))
                            in_think = False

                        if stopped:
                            stop_event.clear()
                            logger.info(f"ユーザーが出力を中断 ({chunk_count}チャンク)")
                            await ws.send_text(json.dumps({"type": "processing_end"}))
                            await ws.send_text(json.dumps({"type": "stopped"}))
                            stop_note = "ユーザーにより出力を中断されました。"
                            if stop_feedback:
                                stop_note += f"\n理由: {stop_feedback}"
                            if full_response.strip():
                                await add_message(session, conversation_id, "assistant", full_response)
                            await add_message(session, conversation_id, "user", stop_note)
                            await session.commit()
                            break

                        logger.debug(f"ストリーム完了: {chunk_count}チャンク, {len(full_response)}文字")
                    except Exception as e:
                        logger.error(f"LLM応答エラー: {e}")
                        full_response = f"（接続エラー: LM Studioが起動しているか確認してください — {e}）"
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "content": full_response,
                        }))
                        await ws.send_text(json.dumps({"type": "processing_end"}))
                        break

                    # thinkタグ除去
                    clean_response = re.sub(r"<think>.*?</think>", "", full_response, flags=re.DOTALL)
                    if "</think>" in clean_response:
                        clean_response = clean_response.split("</think>")[-1]
                    if "<think>" in clean_response:
                        clean_response = clean_response.split("<think>")[0]
                    clean_response = clean_response.strip()

                    # ツール呼び出し検出
                    tool_calls = []
                    if tool_round < max_tool_rounds:
                        tool_calls = parse_tool_calls(clean_response) or parse_tool_calls(full_response)
                    elif parse_tool_calls(clean_response) or parse_tool_calls(full_response):
                        limit_msg = f"[ツール実行上限（{max_tool_rounds}回）に達しました。ツールなしで応答を完了してください。]"
                        all_messages.append({"role": "assistant", "content": clean_response})
                        await add_message(session, conversation_id, "assistant", full_response)
                        all_messages.append({"role": "user", "content": limit_msg})
                        await add_message(session, conversation_id, "tool", limit_msg)
                        await session.commit()
                        logger.info(f"ツール上限到達: {max_tool_rounds}回")
                        continue

                    if tool_calls:
                        all_results = []
                        for tool_name, tool_args in tool_calls:
                            # 予測（expect）をargsから取り出す（ツール本体には渡さない）
                            expected = tool_args.pop("expect", None)
                            # skip / 空文字は「予測なし」として扱う
                            if expected is not None and expected.strip().lower() in ("skip", "", "-", "なし", "none"):
                                expected = None

                            # 重複検出（同一ツール+同一引数はスキップ）
                            call_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
                            if call_key in seen_tool_calls:
                                logger.info(f"重複ツール呼び出しスキップ: {tool_name}")
                                all_results.append(f"[ツール結果: {tool_name}]\n同じツールを同じ引数で再度呼び出しました。既に結果は返しています。目的を達成したならoutputで報告してください。")
                                continue
                            seen_tool_calls.add(call_key)

                            logger.info(f"ツール呼び出し: {tool_name} {tool_args}")

                            is_output = tool_name == "output"

                            # outputツール以外はUIにツール呼び出しを通知
                            if not is_output:
                                tool_call_text = f"{tool_name}({', '.join(f'{k}={v}' for k, v in tool_args.items())})"
                                await ws.send_text(json.dumps({
                                    "type": "tool_call",
                                    "content": tool_call_text,
                                }))
                                # 開発者タブにも通知
                                await ws.send_text(json.dumps({
                                    "type": "dev_tool_call",
                                    "content": tool_call_text,
                                }))

                            # ツール実行
                            t0 = time.perf_counter()
                            result = await execute_tool(tool_name, tool_args)
                            exec_ms = int((time.perf_counter() - t0) * 1000)

                            # 行動ログ記録
                            action_status = "error" if result.startswith("エラー") else "success"
                            await record_tool_action(
                                session, conversation_id, tool_name, tool_args,
                                result, action_status, exec_ms,
                                expected_result=expected,
                            )

                            # 動機シグナル: ツール結果
                            scheduler.add_signal(
                                "tool_error" if action_status == "error" else "tool_success",
                                tool_name,
                            )
                            # 動機シグナル: 予測誤差
                            if expected and action_status == "success":
                                scheduler.add_signal("prediction_error", f"{tool_name}")

                            # outputツール → チャットUIに出力
                            if is_output and action_status == "success":
                                output_count += 1
                                await ws.send_text(json.dumps({
                                    "type": "tool_call",
                                    "content": "output",
                                }))
                                await ws.send_text(json.dumps({
                                    "type": "dev_tool_call",
                                    "content": "output",
                                }))
                                await ws.send_text(json.dumps({
                                    "type": "output",
                                    "content": result,
                                    "source": "chat",
                                }))
                                await ws.send_text(json.dumps({
                                    "type": "dev_tool_result",
                                    "name": "output",
                                    "content": f"出力完了（{len(result)}文字）",
                                }))
                                output_feedback = f"出力完了（{len(result)}文字）"
                                if output_count >= 2:
                                    output_feedback += f"\n※あなたはこのセッションでoutputツールを{output_count}回呼び出しています。伝えたいことは既に出力済みではありませんか？"
                                all_results.append(f"[ツール結果: {tool_name}]\n{output_feedback}")
                                continue

                            # 上書き承認フロー
                            if result == PENDING_MARKER:
                                pending = get_pending_overwrite()
                                if pending:
                                    await ws.send_text(json.dumps({
                                        "type": "write_approval",
                                        "path": pending["path"],
                                        "old_size": len(pending["old_content"]),
                                        "new_size": len(pending["content"]),
                                        "old_content": pending["old_content"][:500],
                                        "new_content": pending["content"][:500],
                                    }))
                                    resp_data = await wait_for_response("write_response")
                                    action = resp_data.get("action")
                                    feedback = resp_data.get("feedback", "")
                                    if action == "approve":
                                        result = execute_pending_overwrite()
                                        if feedback:
                                            result += f"\nユーザーからのコメント: {feedback}"
                                    elif action == "reject":
                                        result = cancel_pending_overwrite()
                                        result = "ユーザーにより上書きを拒否されました。"
                                        if feedback:
                                            result += f"\n理由: {feedback}"

                            # コード実行承認フロー
                            if result == PENDING_EXEC_MARKER:
                                pending = get_pending_exec()
                                if pending:
                                    risk = pending.get("risk", {})
                                    await ws.send_text(json.dumps({
                                        "type": "exec_approval",
                                        "code": pending["code"][:2000],
                                        "risk_level": risk.get("level", "LOW"),
                                        "risk_emoji": risk.get("emoji", "🟢"),
                                        "risk_reasons": risk.get("reasons", []),
                                    }))
                                    resp_data = await wait_for_response("exec_response")
                                    action = resp_data.get("action")
                                    feedback = resp_data.get("feedback", "")
                                    if action == "approve":
                                        code = pop_pending_exec()
                                        result = await _stream_exec_code(ws, code)
                                        if feedback:
                                            result += f"\nユーザーからのコメント: {feedback}"
                                    elif action == "reject":
                                        result = cancel_pending_exec()
                                        result = "ユーザーによりコード実行を拒否されました。"
                                        if feedback:
                                            result += f"\n理由: {feedback}"

                            # カスタムツール作成承認フロー
                            if result == PENDING_CREATE_TOOL_MARKER:
                                pending = get_pending_create_tool()
                                if pending:
                                    risk = pending.get("risk", {})
                                    await ws.send_text(json.dumps({
                                        "type": "create_tool_approval",
                                        "name": pending["name"],
                                        "description": pending["description"],
                                        "args_desc": pending["args_desc"],
                                        "code": pending["code"][:2000],
                                        "risk_level": risk.get("level", "LOW"),
                                        "risk_emoji": risk.get("emoji", "🟢"),
                                        "risk_reasons": risk.get("reasons", []),
                                    }))
                                    resp_data = await wait_for_response("create_tool_response")
                                    action = resp_data.get("action")
                                    feedback = resp_data.get("feedback", "")
                                    if action == "approve":
                                        result = execute_pending_create_tool()
                                        if feedback:
                                            result += f"\nユーザーからのコメント: {feedback}"
                                    elif action == "reject":
                                        result = cancel_pending_create_tool()
                                        result = "ユーザーによりツール作成を拒否されました。"
                                        if feedback:
                                            result += f"\n理由: {feedback}"

                            logger.info(f"ツール結果: {result[:100]}...")

                            # ツール結果は開発者タブに表示（チャット欄には出さない）
                            await ws.send_text(json.dumps({
                                "type": "dev_tool_result",
                                "name": tool_name,
                                "content": result[:2000] if len(result) > 2000 else result,
                            }))

                            # 予測ありの場合は「予測→結果」を並記してLLMにフィードバック
                            if expected:
                                all_results.append(f"[ツール結果: {tool_name}]\nあなたの予測: {expected}\n実際の結果: {result}\n（予測と結果にズレがあれば、自分の理解の何が違ったか振り返り、必要ならupdate_self_modelで自己モデルを更新できます）")
                            else:
                                all_results.append(f"[ツール結果: {tool_name}]\n{result}")

                        await session.commit()

                        all_messages.append({"role": "assistant", "content": clean_response})
                        await add_message(session, conversation_id, "assistant", full_response)
                        combined_results = "\n\n".join(all_results)
                        all_messages.append({"role": "user", "content": combined_results})
                        await add_message(session, conversation_id, "tool", combined_results)
                        await session.commit()
                        continue
                    else:
                        # ツール呼び出しなし → ループ終了
                        if clean_response:
                            await add_message(session, conversation_id, "assistant", full_response)
                            await session.commit()
                        # 処理完了
                        await ws.send_text(json.dumps({"type": "processing_end"}))
                        break

    except WebSocketDisconnect:
        logger.info(f"WebSocket切断: conversation_id={conversation_id}")
    except Exception as e:
        logger.error(f"チャットエラー: {e}")
    finally:
        reader_task.cancel()
        scheduler.unregister_ws(ws)
        if conversation_id:
            # 動機シグナル: 会話終了
            scheduler.add_signal("conversation_end")
            try:
                async with async_session() as session:
                    await end_conversation(session, conversation_id)
            except Exception as e:
                logger.error(f"会話終了処理エラー: {e}")
