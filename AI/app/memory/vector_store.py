"""ベクトル検索ストア — LM Studio embeddingによる意味検索"""
import json
import math
import logging
import asyncio
from sqlalchemy import text
from config import VECTOR_SEARCH_ENABLED, VECTOR_SEARCH_LIMIT
from app.memory.database import async_session

logger = logging.getLogger("iku.vector")

# embedding利用可能かどうか（起動後の最初の試行で判定）
_embed_available: bool | None = None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure Python cosine similarity"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def embed_text(text_content: str) -> list[float] | None:
    """テキスト1件の埋め込みベクトルを取得"""
    global _embed_available
    if _embed_available is False:
        return None
    try:
        from app.llm.manager import llm_manager
        llm = llm_manager.get()
        result = await llm.embed([text_content[:2000]])  # 長すぎるテキストは切り詰め
        if result and len(result) > 0:
            _embed_available = True
            return result[0]
        _embed_available = False
        return None
    except Exception as e:
        logger.debug(f"Embedding取得失敗: {e}")
        _embed_available = False
        return None


async def store_embedding(source_table: str, source_id: int, content: str):
    """Fire-and-forget: コンテンツをembedしてDBに保存"""
    if not VECTOR_SEARCH_ENABLED:
        return
    try:
        vec = await embed_text(content)
        if vec is None:
            return
        async with async_session() as session:
            # 既存があれば上書き
            await session.execute(text(
                "DELETE FROM vector_embeddings WHERE source_table = :table AND source_id = :id"
            ), {"table": source_table, "id": source_id})
            await session.execute(text(
                "INSERT INTO vector_embeddings (source_table, source_id, embedding, created_at) "
                "VALUES (:table, :id, :emb, datetime('now'))"
            ), {"table": source_table, "id": source_id, "emb": json.dumps(vec)})
            await session.commit()
    except Exception as e:
        logger.warning(f"Embedding保存失敗: {e}")


async def search_similar(query: str, source_tables: list[str],
                          limit: int = VECTOR_SEARCH_LIMIT) -> list[dict]:
    """ベクトル類似度検索"""
    if not VECTOR_SEARCH_ENABLED:
        return []

    query_vec = await embed_text(query)
    if query_vec is None:
        return []

    # 指定テーブルの全embeddingをロード（小〜中規模向けbrute force）
    placeholders = ", ".join(f"'{t}'" for t in source_tables)
    async with async_session() as session:
        rows = (await session.execute(text(
            f"SELECT source_table, source_id, embedding FROM vector_embeddings "
            f"WHERE source_table IN ({placeholders})"
        ))).fetchall()

    if not rows:
        return []

    scored = []
    for row in rows:
        try:
            vec = json.loads(row[2])
            sim = cosine_similarity(query_vec, vec)
            scored.append({
                "source_table": row[0],
                "source_id": row[1],
                "similarity": sim,
            })
        except (json.JSONDecodeError, TypeError):
            continue

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:limit]


async def fetch_content_for_results(results: list[dict]) -> list[dict]:
    """ベクトル検索結果の実コンテンツを取得"""
    if not results:
        return []

    enriched = []
    async with async_session() as session:
        for r in results:
            table = r["source_table"]
            sid = r["source_id"]
            try:
                if table == "messages":
                    row = (await session.execute(text(
                        "SELECT role, content FROM messages WHERE id = :id"
                    ), {"id": sid})).fetchone()
                    if row:
                        enriched.append({
                            **r, "role": row[0], "content": row[1],
                        })
                elif table == "memory_summaries":
                    row = (await session.execute(text(
                        "SELECT content, created_at FROM memory_summaries WHERE id = :id"
                    ), {"id": sid})).fetchone()
                    if row:
                        enriched.append({
                            **r, "content": row[0], "created_at": str(row[1]),
                        })
                elif table == "iku_logs":
                    row = (await session.execute(text(
                        "SELECT role, content FROM iku_logs WHERE id = :id"
                    ), {"id": sid})).fetchone()
                    if row:
                        enriched.append({
                            **r, "role": row[0], "content": row[1],
                        })
            except Exception:
                continue
    return enriched


async def reindex_all() -> dict:
    """全メッセージ・日記のベクトルを再構築"""
    global _embed_available
    _embed_available = None  # リセットして再判定

    counts = {"messages": 0, "memory_summaries": 0, "skipped": 0}

    async with async_session() as session:
        await session.execute(text("DELETE FROM vector_embeddings"))
        await session.commit()

    async with async_session() as session:
        rows = (await session.execute(text(
            "SELECT id, content FROM messages"
        ))).fetchall()

    for row in rows:
        await store_embedding("messages", row[0], row[1])
        if _embed_available is False:
            counts["skipped"] = len(rows)
            return counts
        counts["messages"] += 1

    async with async_session() as session:
        rows = (await session.execute(text(
            "SELECT id, content FROM memory_summaries"
        ))).fetchall()

    for row in rows:
        await store_embedding("memory_summaries", row[0], row[1])
        counts["memory_summaries"] += 1

    return counts


def get_status() -> dict:
    """ベクトルストアの状態"""
    return {
        "enabled": VECTOR_SEARCH_ENABLED,
        "embed_available": _embed_available,
    }
