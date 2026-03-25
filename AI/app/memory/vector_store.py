"""ベクトル検索ストア — bge-m3 (ONNX/CPU) / LM Studio フォールバック"""
import json
import math
import logging
import asyncio
import numpy as np
from sqlalchemy import text
from config import VECTOR_SEARCH_ENABLED, VECTOR_SEARCH_LIMIT
from app.memory.database import async_session

logger = logging.getLogger("iku.vector")

# embedding利用可能かどうか（起動後の最初の試行で判定）
_embed_available: bool | None = None

# ONNX bge-m3 モデル（遅延初期化、シングルトン）
_onnx_session = None
_onnx_tokenizer = None
_onnx_tried = False


def _load_bge_m3():
    """bge-m3 ONNXモデルを遅延初期化で取得（HuggingFaceから自動ダウンロード）"""
    global _onnx_session, _onnx_tokenizer, _onnx_tried
    if _onnx_tried:
        return _onnx_session is not None
    _onnx_tried = True
    try:
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer
        import onnxruntime as ort

        model_path = hf_hub_download("BAAI/bge-m3", "onnx/model.onnx")
        tok_path = hf_hub_download("BAAI/bge-m3", "onnx/tokenizer.json")

        _onnx_tokenizer = Tokenizer.from_file(tok_path)
        _onnx_tokenizer.enable_padding(pad_id=1, pad_token="<pad>", length=128)
        _onnx_tokenizer.enable_truncation(max_length=512)

        _onnx_session = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"]
        )
        logger.info("bge-m3 (ONNX/CPU) をロード完了 — VRAMゼロ、1024次元")
        return True
    except ImportError as e:
        logger.info(f"ONNX推論の依存不足: {e} — LM Studioフォールバックを使用")
        return False
    except Exception as e:
        logger.warning(f"bge-m3ロード失敗: {e} — LM Studioフォールバックを使用")
        return False


def _embed_sync(texts: list[str]) -> list[list[float]] | None:
    """bge-m3 ONNX（CPU同期）でembedding取得"""
    if not _load_bge_m3():
        return None
    try:
        encoded = _onnx_tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)

        outputs = _onnx_session.run(
            None, {"input_ids": input_ids, "attention_mask": attention_mask}
        )

        # mean pooling + L2 normalize
        embeddings = outputs[0]  # (batch, seq, 1024)
        mask = attention_mask[:, :, np.newaxis].astype(np.float32)
        pooled = (embeddings * mask).sum(axis=1) / mask.sum(axis=1)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        pooled = pooled / norms

        return [vec.tolist() for vec in pooled]
    except Exception as e:
        logger.warning(f"bge-m3推論エラー: {e}")
        return None


async def _embed_via_lmstudio(texts: list[str]) -> list[list[float]] | None:
    """LM Studio /v1/embeddings 経由でembedding取得（フォールバック）"""
    try:
        from app.llm.manager import llm_manager
        llm = llm_manager.get()
        return await llm.embed(texts)
    except Exception as e:
        logger.debug(f"LM Studio embedding失敗: {e}")
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure Python cosine similarity"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def embed_text(text_content: str) -> list[float] | None:
    """テキスト1件の埋め込みベクトルを取得（bge-m3 ONNX優先 → LM Studio → None）"""
    global _embed_available
    if _embed_available is False:
        return None

    truncated = text_content[:2000]

    # 1. bge-m3 ONNX（CPU、VRAMゼロ）
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _embed_sync, [truncated])
    if result and len(result) > 0:
        _embed_available = True
        return result[0]

    # 2. LM Studio フォールバック
    lm_result = await _embed_via_lmstudio([truncated])
    if lm_result and len(lm_result) > 0:
        _embed_available = True
        return lm_result[0]

    # 3. どちらも使えない
    _embed_available = False
    logger.info("Embedding利用不可（bge-m3・LM Studio両方失敗）— FTS5のみで検索")
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
                          limit: int = VECTOR_SEARCH_LIMIT,
                          persona_id: int | None = None) -> list[dict]:
    """ベクトル類似度検索（persona_idフィルタ対応）"""
    if not VECTOR_SEARCH_ENABLED:
        return []

    query_vec = await embed_text(query)
    if query_vec is None:
        return []

    # 指定テーブルの全embeddingをロード（小〜中規模向けbrute force）
    placeholders = ", ".join(f"'{t}'" for t in source_tables)
    async with async_session() as session:
        if persona_id is not None:
            # persona_idフィルタ: テーブルごとに異なるJOIN戦略
            rows = []
            for tbl in source_tables:
                if tbl == "messages":
                    tbl_rows = (await session.execute(text(
                        "SELECT v.source_table, v.source_id, v.embedding FROM vector_embeddings v "
                        "JOIN messages m ON v.source_id = m.id "
                        "JOIN conversations c ON m.conversation_id = c.id "
                        "WHERE v.source_table = 'messages' AND c.persona_id = :pid"
                    ), {"pid": persona_id})).fetchall()
                elif tbl in ("memory_summaries", "persona_episodes", "tool_actions"):
                    # 直接persona_idカラムを持つテーブル
                    src = tbl
                    tbl_rows = (await session.execute(text(
                        f"SELECT v.source_table, v.source_id, v.embedding FROM vector_embeddings v "
                        f"JOIN {src} t ON v.source_id = t.id "
                        f"WHERE v.source_table = :tbl AND t.persona_id = :pid"
                    ), {"tbl": tbl, "pid": persona_id})).fetchall()
                else:
                    tbl_rows = (await session.execute(text(
                        "SELECT source_table, source_id, embedding FROM vector_embeddings "
                        "WHERE source_table = :tbl"
                    ), {"tbl": tbl})).fetchall()
                rows.extend(tbl_rows)
        else:
            # ノーマルモード: persona_id IS NULL のデータのみ
            rows = []
            for tbl in source_tables:
                if tbl == "messages":
                    tbl_rows = (await session.execute(text(
                        "SELECT v.source_table, v.source_id, v.embedding FROM vector_embeddings v "
                        "JOIN messages m ON v.source_id = m.id "
                        "JOIN conversations c ON m.conversation_id = c.id "
                        "WHERE v.source_table = 'messages' AND c.persona_id IS NULL"
                    ))).fetchall()
                elif tbl in ("memory_summaries", "tool_actions"):
                    tbl_rows = (await session.execute(text(
                        f"SELECT v.source_table, v.source_id, v.embedding FROM vector_embeddings v "
                        f"JOIN {tbl} t ON v.source_id = t.id "
                        f"WHERE v.source_table = :tbl AND t.persona_id IS NULL"
                    ), {"tbl": tbl})).fetchall()
                else:
                    tbl_rows = (await session.execute(text(
                        "SELECT source_table, source_id, embedding FROM vector_embeddings "
                        "WHERE source_table = :tbl"
                    ), {"tbl": tbl})).fetchall()
                rows.extend(tbl_rows)

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
                elif table == "persona_episodes":
                    row = (await session.execute(text(
                        "SELECT role, content FROM persona_episodes WHERE id = :id"
                    ), {"id": sid})).fetchone()
                    if row:
                        enriched.append({
                            **r, "role": row[0], "content": row[1],
                        })
                elif table == "iku_logs":
                    row = (await session.execute(text(
                        "SELECT role, content FROM iku_logs WHERE id = :id"
                    ), {"id": sid})).fetchone()
                    if row:
                        enriched.append({
                            **r, "role": row[0], "content": row[1],
                        })
                elif table == "tool_actions":
                    row = (await session.execute(text(
                        "SELECT tool_name, result_summary FROM tool_actions WHERE id = :id"
                    ), {"id": sid})).fetchone()
                    if row:
                        enriched.append({
                            **r, "content": f"{row[0]}: {row[1]}",
                        })
            except Exception:
                continue
    return enriched


async def reindex_all() -> dict:
    """全メッセージ・日記・エピソード・アクションログのベクトルを再構築"""
    global _embed_available
    _embed_available = None  # リセットして再判定

    counts = {"messages": 0, "memory_summaries": 0, "persona_episodes": 0, "tool_actions": 0, "skipped": 0}

    async with async_session() as session:
        await session.execute(text("DELETE FROM vector_embeddings"))
        await session.commit()

    # messages
    async with async_session() as session:
        rows = (await session.execute(text("SELECT id, content FROM messages"))).fetchall()
    for row in rows:
        await store_embedding("messages", row[0], row[1])
        if _embed_available is False:
            counts["skipped"] = len(rows)
            return counts
        counts["messages"] += 1

    # memory_summaries
    async with async_session() as session:
        rows = (await session.execute(text("SELECT id, content FROM memory_summaries"))).fetchall()
    for row in rows:
        await store_embedding("memory_summaries", row[0], row[1])
        counts["memory_summaries"] += 1

    # persona_episodes
    async with async_session() as session:
        rows = (await session.execute(text("SELECT id, content FROM persona_episodes"))).fetchall()
    for row in rows:
        await store_embedding("persona_episodes", row[0], row[1])
        counts["persona_episodes"] += 1

    # tool_actions
    async with async_session() as session:
        rows = (await session.execute(text(
            "SELECT id, tool_name, arguments, result_summary FROM tool_actions"
        ))).fetchall()
    for row in rows:
        text_for_embed = f"{row[1]}: {row[2][:200]} → {row[3][:200]}"
        await store_embedding("tool_actions", row[0], text_for_embed)
        counts["tool_actions"] += 1

    return counts


def get_status() -> dict:
    """ベクトルストアの状態"""
    return {
        "enabled": VECTOR_SEARCH_ENABLED,
        "embed_available": _embed_available,
        "backend": "bge-m3-onnx" if _onnx_session else ("lmstudio" if _embed_available else "none"),
    }
