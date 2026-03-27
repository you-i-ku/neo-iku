"""記憶CRUD操作"""
import json
import asyncio
from datetime import datetime
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.memory.models import Conversation, Message, IkuLog, ToolAction, PersonaEpisode


async def create_conversation(session: AsyncSession, is_imported: bool = False, source: str = "chat",
                               trigger: str | None = None, persona_id: int | None = None) -> Conversation:
    conv = Conversation(is_imported=is_imported, source=source, trigger=trigger, persona_id=persona_id)
    session.add(conv)
    await session.flush()
    return conv


async def end_conversation(session: AsyncSession, conversation_id: int):
    conv = await session.get(Conversation, conversation_id)
    if conv:
        conv.ended_at = datetime.utcnow()
        await session.commit()


async def add_message(session: AsyncSession, conversation_id: int, role: str, content: str) -> Message:
    msg = Message(conversation_id=conversation_id, role=role, content=content)
    session.add(msg)
    await session.flush()
    # FTS5にも挿入
    await session.execute(text(
        "INSERT INTO messages_fts(rowid, content) VALUES (:id, :content)"
    ), {"id": msg.id, "content": content})
    # ベクトル埋め込み（fire-and-forget）
    try:
        from app.memory.vector_store import store_embedding
        asyncio.create_task(store_embedding("messages", msg.id, content))
    except Exception:
        pass
    return msg


async def get_conversation_messages(session: AsyncSession, conversation_id: int) -> list[Message]:
    result = await session.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
    )
    return list(result.scalars().all())


async def add_iku_log(session: AsyncSession, file_name: str, role: str, content: str, sequence: int) -> IkuLog:
    log = IkuLog(file_name=file_name, role=role, content=content, sequence=sequence)
    session.add(log)
    await session.flush()
    # FTS5にも挿入
    await session.execute(text(
        "INSERT INTO iku_logs_fts(rowid, content) VALUES (:id, :content)"
    ), {"id": log.id, "content": content})
    return log


async def add_persona_episode(session: AsyncSession, persona_id: int,
                               file_name: str, role: str, content: str, sequence: int) -> PersonaEpisode:
    """ペルソナエピソードを追加"""
    ep = PersonaEpisode(persona_id=persona_id, file_name=file_name, role=role, content=content, sequence=sequence)
    session.add(ep)
    await session.flush()
    # FTS5にも挿入
    await session.execute(text(
        "INSERT INTO persona_episodes_fts(rowid, content) VALUES (:id, :content)"
    ), {"id": ep.id, "content": content})
    # ベクトル埋め込み（fire-and-forget）
    try:
        from app.memory.vector_store import store_embedding
        asyncio.create_task(store_embedding("persona_episodes", ep.id, content))
    except Exception:
        pass
    return ep


async def count_persona_episodes(session: AsyncSession, persona_id: int) -> int:
    result = await session.execute(
        select(func.count(PersonaEpisode.id)).where(PersonaEpisode.persona_id == persona_id)
    )
    return result.scalar() or 0


async def count_iku_logs(session: AsyncSession) -> int:
    result = await session.execute(select(func.count(IkuLog.id)))
    return result.scalar() or 0


async def count_messages(session: AsyncSession) -> int:
    result = await session.execute(select(func.count(Message.id)))
    return result.scalar() or 0


async def count_conversations(session: AsyncSession) -> int:
    result = await session.execute(select(func.count(Conversation.id)))
    return result.scalar() or 0


async def record_tool_action(
    session: AsyncSession,
    conversation_id: int | None,
    tool_name: str,
    args: dict,
    result: str,
    status: str = "success",
    execution_ms: int | None = None,
    expected_result: str | None = None,
    intent: str | None = None,
    persona_id: int | None = None,
    mirror: str | None = None,
) -> ToolAction:
    """ツール実行履歴を記録"""
    args_json = json.dumps(args, ensure_ascii=False)
    result_summary = result[:500]

    action = ToolAction(
        conversation_id=conversation_id,
        persona_id=persona_id,
        tool_name=tool_name,
        arguments=args_json,
        result_summary=result_summary,
        expected_result=expected_result,
        intent=intent,
        status=status,
        execution_ms=execution_ms,
        mirror=mirror,
    )
    session.add(action)
    await session.flush()
    # FTS5にも挿入
    await session.execute(text(
        "INSERT INTO tool_actions_fts(rowid, tool_name, arguments, result_summary) VALUES (:id, :name, :args, :summary)"
    ), {"id": action.id, "name": tool_name, "args": args_json, "summary": result_summary})
    # アクションログベクトル化（fire-and-forget）
    try:
        from app.memory.vector_store import store_embedding
        text_for_embed = f"{tool_name}: {args_json[:200]} → {result_summary[:200]}"
        asyncio.create_task(store_embedding("tool_actions", action.id, text_for_embed))
    except Exception:
        pass
    return action
