"""全文検索（メッセージ + イク過去ログ + 日記）"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from config import MEMORY_SEARCH_LIMIT


async def search_messages(session: AsyncSession, query: str, limit: int = MEMORY_SEARCH_LIMIT) -> list[dict]:
    """通常の会話メッセージをFTS5で検索"""
    return await _fts_search(session, "messages_fts", "messages", query, limit,
                             columns=["id", "role", "content"])


async def search_iku_logs(session: AsyncSession, query: str, limit: int = MEMORY_SEARCH_LIMIT) -> list[dict]:
    """イク過去ログをFTS5で検索"""
    return await _fts_search(session, "iku_logs_fts", "iku_logs", query, limit,
                             columns=["id", "role", "content"])


async def search_diary(session: AsyncSession, query: str, limit: int = MEMORY_SEARCH_LIMIT) -> list[dict]:
    """日記・内省メモをFTS5で検索"""
    return await _fts_search(session, "memory_summaries_fts", "memory_summaries", query, limit,
                             columns=["id", "content", "keywords", "created_at", "source"])


async def search_tool_actions(session: AsyncSession, query: str = "",
                               tool_name: str = "", limit: int = 10) -> list[dict]:
    """ツール実行履歴を検索（FTS5 + tool_nameフィルタ）"""
    # クエリもツール名フィルタもない場合は最新を返す
    if not query.strip() and not tool_name.strip():
        from sqlalchemy import text
        result = await session.execute(text(
            "SELECT id, tool_name, arguments, result_summary, status, execution_ms, created_at "
            "FROM tool_actions ORDER BY id DESC LIMIT :limit"
        ), {"limit": limit})
        return [dict(zip(["id", "tool_name", "arguments", "result_summary", "status", "execution_ms", "created_at"], row))
                for row in result.fetchall()]

    if query.strip():
        # FTS5検索
        results = await _fts_search(
            session, "tool_actions_fts", "tool_actions", query, limit,
            columns=["id", "tool_name", "arguments", "result_summary", "status", "execution_ms", "created_at"],
        )
    else:
        results = []

    # tool_nameフィルタ
    if tool_name.strip():
        if results:
            results = [r for r in results if r["tool_name"] == tool_name.strip()]
        else:
            # クエリなし + tool_nameフィルタのみ
            from sqlalchemy import text
            result = await session.execute(text(
                "SELECT id, tool_name, arguments, result_summary, status, execution_ms, created_at "
                "FROM tool_actions WHERE tool_name = :name ORDER BY id DESC LIMIT :limit"
            ), {"name": tool_name.strip(), "limit": limit})
            results = [dict(zip(["id", "tool_name", "arguments", "result_summary", "status", "execution_ms", "created_at"], row))
                       for row in result.fetchall()]

    return results


def _build_fts_query(query: str, use_trigram: bool) -> str:
    """検索クエリを構築する。trigramの場合は各単語をフレーズとして扱う"""
    words = query.strip().split()
    if not words:
        return ""
    if use_trigram:
        # trigram: 各単語をそのまま部分文字列検索（3文字以上が有効）
        # 短い単語はそのまま、長い単語もそのまま渡す（trigramが分解してくれる）
        escaped = ['"' + w.replace('"', '""') + '"' for w in words]
        return " OR ".join(escaped)
    else:
        # デフォルトtokenizer: OR検索 + prefix match
        parts = []
        for w in words:
            escaped = w.replace('"', '""')
            parts.append(f'"{escaped}"')
            parts.append(f'"{escaped}"*')  # prefix match
        return " OR ".join(parts)


async def _fts_search(session: AsyncSession, fts_table: str, source_table: str,
                       query: str, limit: int, columns: list[str]) -> list[dict]:
    """FTS5検索の共通処理"""
    if not query.strip():
        return []

    # trigram使用判定（テーブルのtokenizer設定を確認）
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

    try:
        result = await session.execute(text(f"""
            SELECT {col_list}, rank
            FROM {fts_table}
            JOIN {source_table} t ON {fts_table}.rowid = t.id
            WHERE {fts_table} MATCH :query
            ORDER BY rank
            LIMIT :limit
        """), {"query": fts_query, "limit": limit})

        return [dict(zip(columns, row[:-1])) for row in result.fetchall()]
    except Exception:
        # フォールバック: LIKE検索（各単語でOR）
        words = query.strip().split()
        like_conditions = " OR ".join(f"t.content LIKE :p{i}" for i in range(len(words)))
        params = {f"p{i}": f"%{w}%" for i, w in enumerate(words)}
        params["limit"] = limit

        result = await session.execute(text(f"""
            SELECT {col_list}
            FROM {source_table} t
            WHERE {like_conditions}
            ORDER BY t.id DESC
            LIMIT :limit
        """), params)

        return [dict(zip(columns, row)) for row in result.fetchall()]
