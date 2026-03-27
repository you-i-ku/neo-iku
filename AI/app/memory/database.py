"""DB接続管理"""
import logging
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
from config import DATABASE_URL
from app.memory.models import Base

logger = logging.getLogger("iku.database")

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


async def _migrate_column(conn, table: str, column: str, col_type: str = "TEXT", default: str = ""):
    """カラムが無ければ追加する（ALTER TABLE マイグレーション）"""
    try:
        await conn.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
    except Exception:
        default_clause = f" DEFAULT {default}" if default else ""
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}{default_clause}"))


async def init_db():
    """テーブル作成 + FTS5仮想テーブル"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # --- 既存DBマイグレーション ---
        await _migrate_column(conn, "tool_actions", "expected_result")
        await _migrate_column(conn, "tool_actions", "intent")
        await _migrate_column(conn, "tool_actions", "persona_id", "INTEGER")
        await _migrate_column(conn, "conversations", "source", default="'chat'")
        await _migrate_column(conn, "conversations", "trigger")
        await _migrate_column(conn, "conversations", "distillation_response")
        await _migrate_column(conn, "conversations", "persona_id", "INTEGER")
        await _migrate_column(conn, "conversations", "distillation_principle")
        await _migrate_column(conn, "tool_actions", "mirror")
        await _migrate_column(conn, "memory_summaries", "persona_id", "INTEGER")
        await _migrate_column(conn, "self_model_snapshots", "persona_id", "INTEGER")

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
            "persona_episodes_fts": f"USING fts5(content, content_rowid='id'{tokenize_opt})",
            "memory_summaries_fts": f"USING fts5(content, keywords, content_rowid='id'{tokenize_opt})",
            "tool_actions_fts": f"USING fts5(tool_name, arguments, result_summary{tokenize_opt})",
        }
        for table_name, definition in fts_tables.items():
            if await _needs_rebuild(conn, table_name, use_trigram):
                await conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
            await conn.execute(text(f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} {definition}"))

        # tokenizer変更でFTSテーブルを再作成した場合、データを再投入
        await _repopulate_fts(conn, "messages_fts", "messages",
                              "INSERT INTO messages_fts(rowid, content) SELECT id, content FROM messages")
        await _repopulate_fts(conn, "iku_logs_fts", "iku_logs",
                              "INSERT INTO iku_logs_fts(rowid, content) SELECT id, content FROM iku_logs")
        await _repopulate_fts(conn, "persona_episodes_fts", "persona_episodes",
                              "INSERT INTO persona_episodes_fts(rowid, content) SELECT id, content FROM persona_episodes")
        await _repopulate_fts(conn, "memory_summaries_fts", "memory_summaries",
                              "INSERT INTO memory_summaries_fts(rowid, content, keywords) SELECT id, content, keywords FROM memory_summaries")
        await _repopulate_fts(conn, "tool_actions_fts", "tool_actions",
                              "INSERT INTO tool_actions_fts(rowid, tool_name, arguments, result_summary) SELECT id, tool_name, arguments, result_summary FROM tool_actions")

        # --- イクデータ移行: iku_logs → persona_episodes ---
        await _migrate_iku_to_persona(conn)


async def _repopulate_fts(conn, fts_table: str, source_table: str, insert_sql: str):
    """FTSテーブルが空でソーステーブルにデータがあれば再投入"""
    try:
        fts_count = (await conn.execute(text(f"SELECT COUNT(*) FROM {fts_table}"))).scalar()
        src_count = (await conn.execute(text(f"SELECT COUNT(*) FROM {source_table}"))).scalar()
        if src_count > 0 and fts_count == 0:
            await conn.execute(text(insert_sql))
    except Exception:
        pass  # テーブルが存在しない場合等はスキップ


async def _migrate_iku_to_persona(conn):
    """personasテーブルが空 かつ iku_logsにデータあり → ikuペルソナ作成+エピソード移行"""
    try:
        persona_count = (await conn.execute(text("SELECT COUNT(*) FROM personas"))).scalar()
        if persona_count > 0:
            return  # 既にペルソナ存在 → 移行済み

        iku_count = (await conn.execute(text("SELECT COUNT(*) FROM iku_logs"))).scalar()
        if iku_count == 0:
            return  # iku_logsにデータなし → 移行不要

        # ikuペルソナ作成
        await conn.execute(text(
            "INSERT INTO personas (name, display_name, color_theme, created_at) "
            "VALUES ('iku', 'イク', 'purple', datetime('now'))"
        ))
        persona_row = (await conn.execute(text("SELECT id FROM personas WHERE name = 'iku'"))).fetchone()
        persona_id = persona_row[0]

        # iku_logs → persona_episodes にコピー
        await conn.execute(text(
            "INSERT INTO persona_episodes (persona_id, file_name, role, content, sequence) "
            "SELECT :pid, file_name, role, content, sequence FROM iku_logs"
        ), {"pid": persona_id})

        # persona_episodes_fts にデータ投入
        await conn.execute(text(
            "INSERT INTO persona_episodes_fts(rowid, content) "
            "SELECT id, content FROM persona_episodes"
        ))

        logger.info(f"イクデータ移行完了: {iku_count}件 → persona_episodes (persona_id={persona_id})")
    except Exception as e:
        logger.warning(f"イクデータ移行中にエラー: {e}")


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
