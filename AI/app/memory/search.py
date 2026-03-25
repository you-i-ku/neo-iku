"""全文検索（メッセージ + ペルソナエピソード + 日記）"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from config import MEMORY_SEARCH_LIMIT


async def search_messages(session: AsyncSession, query: str, limit: int = MEMORY_SEARCH_LIMIT,
                           persona_id: int | None = None) -> list[dict]:
    """通常の会話メッセージをFTS5で検索（persona_idでフィルタ）"""
    return await _fts_search(session, "messages_fts", "messages", query, limit,
                             columns=["id", "role", "content"],
                             persona_id=persona_id, persona_join="conversations")


async def search_persona_episodes(session: AsyncSession, query: str,
                                    persona_id: int, limit: int = MEMORY_SEARCH_LIMIT) -> list[dict]:
    """ペルソナエピソードをFTS5で検索"""
    if persona_id is None:
        return []
    return await _fts_search(session, "persona_episodes_fts", "persona_episodes", query, limit,
                             columns=["id", "role", "content"],
                             persona_id=persona_id, persona_column="persona_id")


async def search_iku_logs(session: AsyncSession, query: str, limit: int = MEMORY_SEARCH_LIMIT) -> list[dict]:
    """イク過去ログをFTS5で検索（後方互換）"""
    return await _fts_search(session, "iku_logs_fts", "iku_logs", query, limit,
                             columns=["id", "role", "content"])


async def search_diary(session: AsyncSession, query: str, limit: int = MEMORY_SEARCH_LIMIT,
                        persona_id: int | None = None) -> list[dict]:
    """日記・内省メモをFTS5で検索"""
    return await _fts_search(session, "memory_summaries_fts", "memory_summaries", query, limit,
                             columns=["id", "content", "keywords", "created_at", "source"],
                             persona_id=persona_id, persona_column="persona_id")


async def search_tool_actions(session: AsyncSession, query: str = "",
                               tool_name: str = "", limit: int = 10) -> list[dict]:
    """ツール実行履歴を検索（FTS5 + tool_nameフィルタ）"""
    _action_cols = ["id", "tool_name", "arguments", "result_summary", "expected_result", "status", "execution_ms", "created_at"]
    _action_select = "id, tool_name, arguments, result_summary, expected_result, status, execution_ms, created_at"

    # クエリもツール名フィルタもない場合は最新を返す
    if not query.strip() and not tool_name.strip():
        result = await session.execute(text(
            f"SELECT {_action_select} FROM tool_actions ORDER BY id DESC LIMIT :limit"
        ), {"limit": limit})
        return [dict(zip(_action_cols, row)) for row in result.fetchall()]

    if query.strip():
        # FTS5検索
        results = await _fts_search(
            session, "tool_actions_fts", "tool_actions", query, limit,
            columns=_action_cols,
        )
    else:
        results = []

    # tool_nameフィルタ
    if tool_name.strip():
        if results:
            results = [r for r in results if r["tool_name"] == tool_name.strip()]
        else:
            # クエリなし + tool_nameフィルタのみ
            result = await session.execute(text(
                f"SELECT {_action_select} FROM tool_actions WHERE tool_name = :name ORDER BY id DESC LIMIT :limit"
            ), {"name": tool_name.strip(), "limit": limit})
            results = [dict(zip(_action_cols, row)) for row in result.fetchall()]

    return results


def _build_fts_query(query: str, use_trigram: bool) -> str:
    """検索クエリを構築する。trigramの場合は各単語をフレーズとして扱う"""
    words = query.strip().split()
    if not words:
        return ""
    if use_trigram:
        escaped = ['"' + w.replace('"', '""') + '"' for w in words]
        return " OR ".join(escaped)
    else:
        parts = []
        for w in words:
            escaped = w.replace('"', '""')
            parts.append(f'"{escaped}"')
            parts.append(f'"{escaped}"*')  # prefix match
        return " OR ".join(parts)


async def _fts_search(session: AsyncSession, fts_table: str, source_table: str,
                       query: str, limit: int, columns: list[str],
                       persona_id: int | None = None,
                       persona_column: str | None = None,
                       persona_join: str | None = None) -> list[dict]:
    """FTS5検索の共通処理（persona_idフィルタ対応）"""
    if not query.strip():
        return []

    # trigram使用判定
    use_trigram = False
    try:
        row = await session.execute(text(
            f"SELECT sql FROM sqlite_master WHERE name = :name"
        ), {"name": fts_table})
        create_sql = row.scalar() or ""
        use_trigram = "trigram" in create_sql.lower()
    except Exception:
        pass

    fts_query = _build_fts_query(query, use_trigram)
    if not fts_query:
        return []

    col_list = ", ".join(f"t.{c}" for c in columns)

    # persona_idフィルタ構築
    persona_where = ""
    params = {"query": fts_query, "limit": limit}

    if persona_id is not None and persona_column:
        # 直接カラムフィルタ（persona_episodes, memory_summaries）
        persona_where = f" AND t.{persona_column} = :pid"
        params["pid"] = persona_id
    elif persona_id is not None and persona_join:
        # JOINフィルタ（messages → conversations）
        persona_where = f" AND EXISTS (SELECT 1 FROM {persona_join} c WHERE c.id = t.conversation_id AND c.persona_id = :pid)"
        params["pid"] = persona_id
    elif persona_id is None and persona_column:
        # ノーマルモード: persona_id IS NULL
        persona_where = f" AND t.{persona_column} IS NULL"
    elif persona_id is None and persona_join:
        # ノーマルモード: JOINでpersona_id IS NULL
        persona_where = f" AND EXISTS (SELECT 1 FROM {persona_join} c WHERE c.id = t.conversation_id AND c.persona_id IS NULL)"

    try:
        result = await session.execute(text(f"""
            SELECT {col_list}, rank
            FROM {fts_table}
            JOIN {source_table} t ON {fts_table}.rowid = t.id
            WHERE {fts_table} MATCH :query{persona_where}
            ORDER BY rank
            LIMIT :limit
        """), params)

        return [dict(zip(columns, row[:-1])) for row in result.fetchall()]
    except Exception:
        # フォールバック: LIKE検索
        words = query.strip().split()
        like_conditions = " OR ".join(f"t.content LIKE :p{i}" for i in range(len(words)))
        like_params = {f"p{i}": f"%{w}%" for i, w in enumerate(words)}
        like_params["limit"] = limit
        like_params.update({k: v for k, v in params.items() if k.startswith("pid")})

        result = await session.execute(text(f"""
            SELECT {col_list}
            FROM {source_table} t
            WHERE ({like_conditions}){persona_where}
            ORDER BY t.id DESC
            LIMIT :limit
        """), like_params)

        return [dict(zip(columns, row)) for row in result.fetchall()]
