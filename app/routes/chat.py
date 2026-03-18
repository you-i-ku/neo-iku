"""WebSocketチャットルート"""
import json
import re
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.llm.manager import llm_manager
from app.memory.database import async_session
from app.memory.store import (
    create_conversation, add_message, end_conversation,
    get_conversation_messages,
)
from app.memory.search import search_messages, search_iku_logs
from app.persona.system_prompt import build_system_messages, get_mode
from app.scheduler.autonomous import scheduler
from app.tools.registry import parse_tool_call, execute_tool

logger = logging.getLogger("iku.chat")
router = APIRouter()


@router.websocket("/ws/chat")
async def chat_ws(ws: WebSocket):
    await ws.accept()
    scheduler.register_ws(ws)
    conversation_id = None

    try:
        async with async_session() as session:
            conv = await create_conversation(session)
            conversation_id = conv.id
            await session.commit()

            while True:
                raw = await ws.receive_text()
                data = json.loads(raw)
                user_text = data.get("message", "").strip()
                if not user_text:
                    continue

                # ユーザーメッセージ保存
                await add_message(session, conversation_id, "user", user_text)
                await session.commit()

                # 常に記憶検索（モード問わず）
                memories = await search_messages(session, user_text)
                iku_memories = await search_iku_logs(session, user_text) if get_mode() == "iku" else None

                system_msgs = build_system_messages(memories, iku_memories)
                conv_messages = await get_conversation_messages(session, conversation_id)
                history = [{"role": m.role, "content": m.content} for m in conv_messages]
                all_messages = system_msgs + history

                # ツール実行ループ（最大5回）
                from config import TOOL_MAX_ROUNDS
                max_tool_rounds = TOOL_MAX_ROUNDS

                for tool_round in range(max_tool_rounds + 1):
                    # LLMからストリーミング応答（think/回答を分離）
                    llm = llm_manager.get()
                    full_response = ""
                    in_think = False
                    think_buffer = ""
                    response_buffer = ""

                    try:
                        chunk_count = 0
                        async for chunk in llm.stream_chat(all_messages):
                            chunk_count += 1
                            if chunk_count == 1:
                                logger.debug(f"最初のチャンク: {repr(chunk[:50])}")
                            full_response += chunk

                            # <think>タグの検出・分離
                            buf = (think_buffer if in_think else response_buffer) + chunk
                            while buf:
                                if in_think:
                                    end_idx = buf.find("</think>")
                                    if end_idx != -1:
                                        think_part = buf[:end_idx]
                                        if think_part:
                                            await ws.send_text(json.dumps({
                                                "type": "think",
                                                "content": think_part,
                                            }))
                                        await ws.send_text(json.dumps({"type": "think_end"}))
                                        buf = buf[end_idx + 8:]
                                        in_think = False
                                    else:
                                        await ws.send_text(json.dumps({
                                            "type": "think",
                                            "content": buf,
                                        }))
                                        buf = ""
                                else:
                                    start_idx = buf.find("<think>")
                                    if start_idx != -1:
                                        before = buf[:start_idx]
                                        if before:
                                            await ws.send_text(json.dumps({
                                                "type": "stream",
                                                "content": before,
                                            }))
                                        await ws.send_text(json.dumps({"type": "think_start"}))
                                        buf = buf[start_idx + 7:]
                                        in_think = True
                                    else:
                                        if "<" in buf and buf.rstrip().endswith("<"):
                                            response_buffer = buf
                                            buf = ""
                                        else:
                                            await ws.send_text(json.dumps({
                                                "type": "stream",
                                                "content": buf,
                                            }))
                                            buf = ""

                            think_buffer = ""
                            response_buffer = ""

                        # thinkタグが閉じないままストリーム終了した場合
                        if in_think:
                            await ws.send_text(json.dumps({"type": "think_end"}))
                            in_think = False

                        logger.debug(f"ストリーム完了: {chunk_count}チャンク, {len(full_response)}文字")
                        await ws.send_text(json.dumps({"type": "stream_end"}))
                    except Exception as e:
                        logger.error(f"LLM応答エラー: {e}")
                        full_response = f"（接続エラー: LM Studioが起動しているか確認してください — {e}）"
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "content": full_response,
                        }))
                        break

                    # thinkタグ除去（頑強版: 閉じタグのみ・開きタグのみにも対応）
                    clean_response = re.sub(r"<think>.*?</think>", "", full_response, flags=re.DOTALL)
                    # </think> だけが残ってる場合（<think>が欠落）→ </think>より前を全部消す
                    if "</think>" in clean_response:
                        clean_response = clean_response.split("</think>")[-1]
                    # <think> だけが残ってる場合（</think>が欠落）→ <think>以降を全部消す
                    if "<think>" in clean_response:
                        clean_response = clean_response.split("<think>")[0]
                    clean_response = clean_response.strip()

                    # ツール呼び出し検出（clean→full_responseの順で探す。小さいモデルはthink内に書くことがある）
                    tool_call = None
                    if tool_round < max_tool_rounds:
                        tool_call = parse_tool_call(clean_response) or parse_tool_call(full_response)

                    if tool_call:
                        tool_name, tool_args = tool_call
                        logger.info(f"ツール呼び出し: {tool_name} {tool_args}")

                        # UIにツール実行を通知
                        await ws.send_text(json.dumps({
                            "type": "tool_call",
                            "content": f"{tool_name}({', '.join(f'{k}={v}' for k, v in tool_args.items())})",
                        }))

                        # ツール実行
                        result = await execute_tool(tool_name, tool_args)
                        logger.info(f"ツール結果: {result[:100]}...")

                        await ws.send_text(json.dumps({
                            "type": "tool_result",
                            "content": result[:2000] if len(result) > 2000 else result,
                        }))

                        # アシスタント応答をhistoryに追加
                        all_messages.append({"role": "assistant", "content": clean_response})
                        # ツール結果をuserメッセージとしてhistoryに追加
                        all_messages.append({"role": "user", "content": f"[ツール結果: {tool_name}]\n{result}"})
                        # ループ継続 → 次のLLM呼び出し
                        continue
                    else:
                        # ツール呼び出しなし → ループ終了
                        if clean_response:
                            # think含む全文を記録（過程もセットで保存）
                            await add_message(session, conversation_id, "assistant", full_response)
                            await session.commit()
                        break

    except WebSocketDisconnect:
        logger.info(f"WebSocket切断: conversation_id={conversation_id}")
    except Exception as e:
        logger.error(f"チャットエラー: {e}")
    finally:
        scheduler.unregister_ws(ws)
        if conversation_id:
            try:
                async with async_session() as session:
                    await end_conversation(session, conversation_id)
            except Exception as e:
                logger.error(f"会話終了処理エラー: {e}")
