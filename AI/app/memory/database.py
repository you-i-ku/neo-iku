"""DB接続管理"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
from config import DATABASE_URL
from app.memory.models import Base

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _needs_rebuild(conn, fts_table: str, use_trigram: bool) -> bool:
    """既存FTSテーブルのtokenizerが期待と異なるかチェック"""
    try:
        row = await conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name = :name"
        ), {"name": fts_table})
        sql = row.scalar()
        if sql is None:
            return False  # テーブルが無い → CREATE IF NOT EXISTSで作られる
        has_trigram = "trigram" in sql.lower()
        return has_trigram != use_trigram
    except Exception:
        return False


async def init_db():
    """テーブル作成 + FTS5仮想テーブル"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # tool_actionsにexpected_resultカラム追加（既存DBマイグレーション）
        try:
            await conn.execute(text("SELECT expected_result FROM tool_actions LIMIT 1"))
        except Exception:
            await conn.execute(text("ALTER TABLE tool_actions ADD COLUMN expected_result TEXT"))

        # conversationsにsourceカラム追加（既存DBマイグレーション）
        try:
            await conn.execute(text("SELECT source FROM conversations LIMIT 1"))
        except Exception:
            await conn.execute(text("ALTER TABLE conversations ADD COLUMN source TEXT DEFAULT 'chat'"))

        # conversationsにtriggerカラム追加（既存DBマイグレーション: timer/energy/manual区別）
        try:
            await conn.execute(text("SELECT trigger FROM conversations LIMIT 1"))
        except Exception:
            await conn.execute(text("ALTER TABLE conversations ADD COLUMN trigger TEXT"))

        # trigram tokenizer対応チェック（日本語検索に必須）
        use_trigram = False
        try:
            await conn.execute(text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS _trigram_test USING fts5(x, tokenize='trigram')"
            ))
            await conn.execute(text("DROP TABLE IF EXISTS _trigram_test"))
            use_trigram = True
        except Exception:
            pass

        tokenize_opt = ", tokenize='trigram'" if use_trigram else ""

        # 既存テーブルのtokenizerが変わった場合は再作成
        fts_tables = {
            "messages_fts": f"USING fts5(content, content_rowid='id'{tokenize_opt})",
            "iku_logs_fts": f"USING fts5(content, content_rowid='id'{tokenize_opt})",
            "memory_summaries_fts": f"USING fts5(content, keywords, content_rowid='id'{tokenize_opt})",
            "tool_actions_fts": f"USING fts5(tool_name, arguments, result_summary{tokenize_opt})",
        }
        for table_name, definition in fts_tables.items():
            if await _needs_rebuild(conn, table_name, use_trigram):
                await conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
            await conn.execute(text(f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} {definition}"))

        # tokenizer変更でFTSテーブルを再作成した場合、データを再投入
        # messages_ftsが空かチェック
        row = await conn.execute(text("SELECT COUNT(*) FROM messages_fts"))
        msg_fts_count = row.scalar()
        row = await conn.execute(text("SELECT COUNT(*) FROM messages"))
        msg_count = row.scalar()

        if msg_count > 0 and msg_fts_count == 0:
            await conn.execute(text(
                "INSERT INTO messages_fts(rowid, content) SELECT id, content FROM messages"
            ))

        row = await conn.execute(text("SELECT COUNT(*) FROM iku_logs_fts"))
        log_fts_count = row.scalar()
        row = await conn.execute(text("SELECT COUNT(*) FROM iku_logs"))
        log_count = row.scalar()

        if log_count > 0 and log_fts_count == 0:
            await conn.execute(text(
                "INSERT INTO iku_logs_fts(rowid, content) SELECT id, content FROM iku_logs"
            ))

        row = await conn.execute(text("SELECT COUNT(*) FROM memory_summaries_fts"))
        diary_fts_count = row.scalar()
        row = await conn.execute(text("SELECT COUNT(*) FROM memory_summaries"))
        diary_count = row.scalar()

        if diary_count > 0 and diary_fts_count == 0:
            await conn.execute(text(
                "INSERT INTO memory_summaries_fts(rowid, content, keywords) SELECT id, content, keywords FROM memory_summaries"
            ))

        row = await conn.execute(text("SELECT COUNT(*) FROM tool_actions_fts"))
        action_fts_count = row.scalar()
        row = await conn.execute(text("SELECT COUNT(*) FROM tool_actions"))
        action_count = row.scalar()

        if action_count > 0 and action_fts_count == 0:
            await conn.execute(text(
                "INSERT INTO tool_actions_fts(rowid, tool_name, arguments, result_summary) SELECT id, tool_name, arguments, result_summary FROM tool_actions"
            ))


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
