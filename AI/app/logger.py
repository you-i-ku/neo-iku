"""WebSocketへログをブロードキャストするハンドラ"""
import asyncio
import json
import logging


class WSLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self._websockets: set = set()

    def register(self, ws):
        self._websockets.add(ws)

    def unregister(self, ws):
        self._websockets.discard(ws)

    def emit(self, record):
        if not self._websockets:
            return
        msg = self.format(record)
        data = json.dumps({"type": "log", "level": record.levelname, "msg": msg})
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
