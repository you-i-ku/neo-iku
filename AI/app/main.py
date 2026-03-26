"""FastAPIアプリケーション"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from fastapi import WebSocket, WebSocketDisconnect
from config import STATIC_DIR, DATA_DIR
from app.memory.database import init_db
from app.llm.manager import setup_llm, llm_manager
from app.routes import chat, dashboard, memories
from app.scheduler.autonomous import scheduler
from app.pipeline import pipeline
from app.tools.builtin import register_all as register_tools
from app.logger import ws_log_handler, setup_ws_logging

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(message)s")
setup_ws_logging()
logger = logging.getLogger("iku")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 起動処理
    DATA_DIR.mkdir(exist_ok=True)
    await init_db()
    setup_llm()
    register_tools()

    # パイプライン＆スケジューラ開始
    pipeline.start()
    scheduler.start()

    logger.info("イク、起動しました。")
    yield

    # 終了処理
    scheduler.stop()
    pipeline.stop()
    logger.info("イク、停止しました。")


app = FastAPI(title="neo-iku", lifespan=lifespan)

# ルート登録
app.include_router(chat.router)
app.include_router(dashboard.router)
app.include_router(memories.router)

# 静的ファイル
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(
        str(STATIC_DIR / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.websocket("/ws/logs")
async def log_ws(ws: WebSocket):
    await ws.accept()
    ws_log_handler.register(ws)
    try:
        while True:
            await ws.receive_text()  # 切断検知用
    except WebSocketDisconnect:
        pass
    finally:
        ws_log_handler.unregister(ws)
