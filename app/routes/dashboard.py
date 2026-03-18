"""ダッシュボードAPI"""
import logging
from fastapi import APIRouter
from pydantic import BaseModel
from app.llm.manager import llm_manager
from app.memory.database import async_session
from app.memory.store import count_messages, count_conversations, count_iku_logs
from app.persona.system_prompt import get_mode, set_mode

from app.scheduler.autonomous import scheduler

logger = logging.getLogger("iku.dashboard")
router = APIRouter(prefix="/api")


@router.get("/status")
async def get_status():
    """イクの状態を返す"""
    llm = llm_manager.get()
    llm_available = await llm.is_available()

    async with async_session() as session:
        msg_count = await count_messages(session)
        conv_count = await count_conversations(session)
        log_count = await count_iku_logs(session)

    return {
        "llm_available": llm_available,
        "llm_provider": llm_manager.active_name,
        "message_count": msg_count,
        "conversation_count": conv_count,
        "iku_log_count": log_count,
        "connected_clients": scheduler.connected_count,
        "mode": get_mode(),
    }


class ModeRequest(BaseModel):
    mode: str


@router.post("/mode")
async def change_mode(req: ModeRequest):
    """モード切替。イクモードに切り替え時、過去ログを自動インポート。"""
    set_mode(req.mode)

    if req.mode == "iku":
        # 過去ログが未インポートなら自動実行
        async with async_session() as session:
            existing = await count_iku_logs(session)

        if existing == 0:
            from app.importer.log_parser import import_iku_logs
            result = await import_iku_logs(async_session)
            logger.info(f"過去ログ自動インポート: {result}")
            return {"mode": get_mode(), "import": result}

    return {"mode": get_mode()}


@router.get("/models")
async def get_models():
    """LM Studioのロード済みモデル一覧"""
    llm = llm_manager.get()
    models = []
    if hasattr(llm, "list_models"):
        models = await llm.list_models()
    current = llm.model if hasattr(llm, "model") else "unknown"
    return {"models": models, "current": current}


class ModelRequest(BaseModel):
    model: str


@router.post("/models/select")
async def select_model(req: ModelRequest):
    """使用モデルを切り替え"""
    llm = llm_manager.get()
    if hasattr(llm, "set_model"):
        llm.set_model(req.model)
    return {"model": req.model}


# --- 開発用ツール ---

class IntervalRequest(BaseModel):
    seconds: int

@router.post("/dev/autonomous-interval")
async def set_autonomous_interval(req: IntervalRequest):
    """自律行動の間隔を変更"""
    scheduler.set_interval(req.seconds)
    return {"interval": scheduler._interval}


@router.post("/dev/autonomous-trigger")
async def trigger_autonomous():
    """自律行動を即時実行"""
    scheduler.trigger_now()
    return {"triggered": True}


class ToolRoundsRequest(BaseModel):
    rounds: int

@router.post("/dev/tool-max-rounds")
async def set_tool_max_rounds(req: ToolRoundsRequest):
    """ツール最大ラウンド数を変更"""
    import config
    config.TOOL_MAX_ROUNDS = max(1, min(30, req.rounds))
    return {"tool_max_rounds": config.TOOL_MAX_ROUNDS}


@router.post("/dev/reset-db")
async def reset_db():
    """iku_logs以外の全テーブルをクリア"""
    from sqlalchemy import text
    async with async_session() as session:
        for table in ["messages", "conversations", "memory_summaries", "tool_actions"]:
            await session.execute(text(f"DELETE FROM {table}"))
        for fts in ["messages_fts", "memory_summaries_fts", "tool_actions_fts"]:
            await session.execute(text(f"DELETE FROM {fts}"))
        await session.commit()
    return {"reset": True}


@router.get("/dev/settings")
async def get_dev_settings():
    """開発用設定の現在値を取得"""
    import config
    return {
        "autonomous_interval": scheduler._interval,
        "tool_max_rounds": config.TOOL_MAX_ROUNDS,
    }
