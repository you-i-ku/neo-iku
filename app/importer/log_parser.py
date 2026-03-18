"""過去ログパーサー+インポーター"""
import re
import logging
from pathlib import Path
from config import LOG_DIR

logger = logging.getLogger("iku.importer")


def parse_log_file(filepath: Path) -> list[dict]:
    """1ファイルをパースしてメッセージ列を返す。
    空行2つ以上で区切り、交互にuser/assistantと判定。
    """
    text = filepath.read_text(encoding="utf-8")

    # 空行2つ以上で分割
    blocks = re.split(r"\n\s*\n\s*\n", text)
    blocks = [b.strip() for b in blocks if b.strip()]

    messages = []
    for i, block in enumerate(blocks):
        # XMLタグブロックはスキップ（instructions等はペルソナに統合済み）
        if block.startswith("<") and ">" in block.split("\n")[0]:
            continue
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": block})

    return messages


def get_log_files() -> list[Path]:
    """過去ログファイルを番号順で返す"""
    if not LOG_DIR.exists():
        return []
    files = list(LOG_DIR.glob("*.txt"))
    def sort_key(f):
        match = re.match(r"(\d+)", f.name)
        return int(match.group(1)) if match else 999
    return sorted(files, key=sort_key)


async def import_iku_logs(session_factory):
    """過去ログをiku_logsテーブルにインポート"""
    from app.memory.store import add_iku_log, count_iku_logs

    # 既にインポート済みかチェック
    async with session_factory() as session:
        existing = await count_iku_logs(session)
        if existing > 0:
            logger.info(f"過去ログはインポート済み（{existing}件）")
            return {"status": "already_imported", "count": existing}

    files = get_log_files()
    total = 0

    for filepath in files:
        logger.info(f"インポート中: {filepath.name}")
        messages = parse_log_file(filepath)
        if not messages:
            continue

        # ファイルごとに独立セッション
        async with session_factory() as session:
            for seq, msg in enumerate(messages):
                await add_iku_log(session, filepath.name, msg["role"], msg["content"], seq)
                total += 1
            await session.commit()

    logger.info(f"過去ログインポート完了: {total}件")
    return {"status": "ok", "count": total}
