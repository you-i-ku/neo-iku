"""WebSocketチャットルート — パイプラインへのルーティングのみ"""
import asyncio
import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.pipeline import pipeline
from app.scheduler.autonomous import scheduler

logger = logging.getLogger("iku.chat")
router = APIRouter()


@router.websocket("/ws/chat")
async def chat_ws(ws: WebSocket):
    await ws.accept()
    await pipeline.register_ws(ws)
    scheduler.add_signal("user_connect")

    msg_queue: asyncio.Queue = asyncio.Queue()

    async def ws_reader():
        """WebSocketメッセージを読み取り、種別に応じて振り分ける"""
        try:
            while True:
                raw = await ws.receive_text()
                data = json.loads(raw)
                msg_type = data.get("type", "")

                if msg_type == "stop":
                    pipeline.request_stop(data.get("feedback", ""))

                elif msg_type in ("write_response", "exec_response", "create_tool_response"):
                    pipeline.resolve_approval(
                        data.get("action", "reject"),
                        data.get("feedback", ""),
                    )

                else:
                    await msg_queue.put(data)

        except WebSocketDisconnect:
            await msg_queue.put(None)
        except Exception:
            await msg_queue.put(None)

    reader_task = asyncio.create_task(ws_reader())

    try:
        while True:
            data = await msg_queue.get()
            if data is None:
                break
            user_text = data.get("message", "").strip()
            if not user_text:
                continue

            # パイプライン処理中ならユーザー割り込みとして追加
            if pipeline.is_processing:
                pipeline.add_interrupt(user_text)
                continue

            # ユーザー入力をシグナルとして蓄積（即応答ではなく、AIの動機サイクルで処理）
            scheduler.add_pending_message(user_text)
            await ws.send_text(json.dumps({
                "type": "message_ack",
            }))

    except WebSocketDisconnect:
        logger.info("WebSocket切断")
    except Exception as e:
        logger.error(f"チャットエラー: {e}")
    finally:
        reader_task.cancel()
        pipeline.unregister_ws(ws)
        scheduler.add_signal("conversation_end")
