"""記憶API"""
from fastapi import APIRouter, Query
from sqlalchemy import select
from app.memory.database import async_session
from app.memory.models import Message, IkuLog
from app.memory.search import search_messages, search_iku_logs, search_persona_episodes
from app.persona.system_prompt import get_active_persona_id

router = APIRouter(prefix="/api/memories")


@router.get("")
async def list_memories(limit: int = Query(50, le=200), offset: int = Query(0, ge=0)):
    """最近のメッセージ一覧"""
    async with async_session() as session:
        result = await session.execute(
            select(Message).order_by(Message.created_at.desc()).limit(limit).offset(offset)
        )
        messages = result.scalars().all()
        return [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "created_at": str(m.created_at),
                "source": "chat",
            }
            for m in messages
        ]


@router.get("/search")
async def search(q: str = Query("", min_length=1)):
    """メッセージ + エピソードを横断検索"""
    pid = get_active_persona_id()
    async with async_session() as session:
        chat_results = await search_messages(session, q, limit=10, persona_id=pid)
        if pid is not None:
            episode_results = await search_persona_episodes(session, q, pid, limit=10)
        else:
            episode_results = await search_iku_logs(session, q, limit=10)
        return {
            "chat": chat_results,
            "iku_logs": episode_results,  # 後方互換のキー名
        }


@router.get("/recent")
async def recent_memories(limit: int = Query(10, le=50)):
    """最近のメッセージ"""
    async with async_session() as session:
        result = await session.execute(
            select(Message).order_by(Message.created_at.desc()).limit(limit)
        )
        messages = result.scalars().all()
        return [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "created_at": str(m.created_at),
                "source": "chat",
            }
            for m in messages
        ]
