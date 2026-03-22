"""WebSocketへログをブロードキャストするハンドラ"""
import asyncio
import json
import logging
from collections import deque

BUFFER_SIZE = 500


class WSLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self._websockets: set = set()
        self._buffer: deque[str] = deque(maxlen=BUFFER_SIZE)

    def register(self, ws):
        self._websockets.add(ws)
        # 接続時に既存のバッファを送信
        if self._buffer:
            history = list(self._buffer)
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self._send_history(ws, history))
                )
            except RuntimeError:
                pass

    async def _send_history(self, ws, history: list[str]):
        try:
            for data in history:
                await ws.send_text(data)
        except Exception:
            pass

    def unregister(self, ws):
        self._websockets.discard(ws)

    def emit(self, record):
        msg = self.format(record)
        data = json.dumps({"type": "log", "level": record.levelname, "msg": msg})
        self._buffer.append(data)

        if not self._websockets:
            return
        dead = set()
        for ws in list(self._websockets):
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(
                    lambda w=ws, d=data: asyncio.ensure_future(w.send_text(d))
                )
            except RuntimeError:
                dead.add(ws)
            except Exception:
                dead.add(ws)
        self._websockets -= dead


ws_log_handler = WSLogHandler()
ws_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))


def setup_ws_logging():
    logging.getLogger().addHandler(ws_log_handler)
