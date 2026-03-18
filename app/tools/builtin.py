"""組み込みツール — 自己参照・内省のための基本ツール"""
import json
import os
from datetime import datetime
from pathlib import Path

from config import BASE_DIR, DATA_DIR
from app.tools.registry import register_tool

# 承認待ちの書き込み（メモリ上に保持、1件のみ）
_pending_write: dict | None = None


async def read_file(path: str = "", offset: str = "0") -> str:
    """プロジェクト内のファイルを読む（1回2000文字、offsetで続きを読める）"""
    if not path:
        return "エラー: pathを指定してください。"

    target = (BASE_DIR / path).resolve()

    # セキュリティ: BASE_DIRの外へのアクセス禁止
    if not str(target).startswith(str(BASE_DIR.resolve())):
        return "エラー: プロジェクト外のファイルは読めません。"

    if not target.exists():
        return f"エラー: ファイルが見つかりません: {path}"

    if not target.is_file():
        return f"エラー: '{path}' はファイルではありません。list_filesを使ってください。"

    try:
        start = int(offset)
    except ValueError:
        start = 0

    try:
        content = target.read_text(encoding="utf-8")
        total = len(content)
        chunk = content[start:start + 2000]

        if not chunk:
            return f"（ファイル末尾です。全体{total}文字）"

        result = chunk
        end = start + len(chunk)
        if end < total:
            result += f"\n\n...（{end}/{total}文字。続きは offset={end} で読めます）"
        else:
            result += f"\n\n（ファイル末尾。全体{total}文字）"
        return result
    except Exception as e:
        return f"エラー: ファイル読み取り失敗: {e}"


async def list_files(path: str = ".") -> str:
    """ディレクトリ内のファイル一覧（再帰的にツリー表示）"""
    # "/", "", "." はすべてプロジェクトルートとして扱う
    if path in ("/", ""):
        path = "."
    target = (BASE_DIR / path).resolve()

    if not str(target).startswith(str(BASE_DIR.resolve())):
        return "エラー: プロジェクト外のディレクトリは参照できません。list_files path=. でプロジェクトルートを見られます。"

    if not target.exists():
        return f"エラー: ディレクトリが見つかりません: {path}"

    if not target.is_dir():
        return f"エラー: '{path}' はディレクトリではありません。"

    SKIP = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}

    def _tree(dir_path: Path, prefix: str = "", max_depth: int = 5, depth: int = 0) -> list[str]:
        if depth >= max_depth:
            return [f"{prefix}..."]
        entries = sorted(dir_path.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        entries = [e for e in entries if e.name not in SKIP and not e.name.startswith(".")]
        lines = []
        for i, entry in enumerate(entries):
            is_last = (i == len(entries) - 1)
            connector = "+-" if is_last else "|-"
            lines.append(f"{prefix}{connector} {entry.name}/" if entry.is_dir() else f"{prefix}{connector} {entry.name}")
            if entry.is_dir():
                extension = "   " if is_last else "|  "
                lines.extend(_tree(entry, prefix + extension, max_depth, depth + 1))
        return lines

    try:
        lines = [f"{path}/"] + _tree(target)
        result = "\n".join(lines)
        if len(result) > 3000:
            return result[:3000] + "\n...（省略）"
        return result
    except Exception as e:
        return f"エラー: {e}"


async def search_memories(query: str = "") -> str:
    """記憶を検索する"""
    if not query:
        return "エラー: queryを指定してください。"

    from app.memory.database import async_session
    from app.memory.search import search_messages, search_iku_logs, search_diary

    async with async_session() as session:
        chat_results = await search_messages(session, query)
        log_results = await search_iku_logs(session, query)
        diary_results = await search_diary(session, query)

    lines = []
    if chat_results:
        lines.append("【会話の記憶】")
        for m in chat_results:
            role = "ユーザー" if m["role"] == "user" else "イク"
            content = m["content"][:200]
            lines.append(f"- {role}: {content}")

    if log_results:
        lines.append("【過去ログの記憶】")
        for m in log_results:
            role = "ユーザー" if m["role"] == "user" else "イク"
            content = m["content"][:200]
            lines.append(f"- {role}: {content}")

    if diary_results:
        lines.append("【日記・内省メモ】")
        for m in diary_results:
            content = m["content"][:200]
            date = str(m.get("created_at", ""))[:10]
            lines.append(f"- [{date}] {content}")

    if not lines:
        return f"「{query}」に関する記憶は見つかりませんでした。"

    return "\n".join(lines)


def _do_write(target: Path, path: str, content: str) -> str:
    """実際のファイル書き込み処理"""
    existed = target.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if existed:
            return f"ファイルを上書きしました: {path}（{len(content)}文字）"
        else:
            return f"ファイルを新規作成しました: {path}（{len(content)}文字）"
    except Exception as e:
        return f"エラー: ファイル書き込み失敗: {e}"


async def write_file(path: str = "", content: str = "") -> str:
    """プロジェクト内のファイルを作成・上書きする（自己改変能力）"""
    global _pending_write

    if not path:
        return "エラー: pathを指定してください。"
    if not content:
        return "エラー: contentを指定してください。"

    target = (BASE_DIR / path).resolve()
    base = str(BASE_DIR.resolve())

    # セキュリティ: BASE_DIRの外へのアクセス禁止
    if not str(target).startswith(base):
        return "エラー: プロジェクト外への書き込みはできません。"

    # 安全装置: .git内への書き込み禁止
    if ".git" in target.parts:
        return "エラー: .git内への書き込みは禁止です。"

    # 既存ファイルの上書き → Human-in-the-loop（承認が必要）
    if target.exists():
        old_content = target.read_text(encoding="utf-8")
        _pending_write = {
            "path": path,
            "target": str(target),
            "content": content,
            "old_content": old_content,
            "created_at": datetime.now().isoformat(),
        }

        # 変更のプレビューを生成
        old_lines = old_content.splitlines()
        new_lines = content.splitlines()
        preview = f"【承認待ち】既存ファイルの上書き: {path}\n"
        preview += f"現在: {len(old_content)}文字 → 変更後: {len(content)}文字\n\n"
        preview += f"--- 変更後の先頭200文字 ---\n{content[:200]}"
        if len(content) > 200:
            preview += "\n..."
        preview += "\n\n⚠ このファイルを上書きするにはユーザーの承認が必要です。"
        preview += "\nユーザーに変更の意図を説明して承認を求めてください。"
        preview += "\n承認後 [TOOL:apply_write] を呼んでください。"
        return preview

    # 新規ファイル → そのまま作成
    return _do_write(target, path, content)


async def apply_write() -> str:
    """承認済みの保留中の書き込みを実行する"""
    global _pending_write

    if _pending_write is None:
        return "エラー: 承認待ちの書き込みはありません。"

    path = _pending_write["path"]
    target = Path(_pending_write["target"])
    content = _pending_write["content"]

    result = _do_write(target, path, content)
    _pending_write = None
    return result


async def reject_write() -> str:
    """保留中の書き込みを却下する"""
    global _pending_write

    if _pending_write is None:
        return "承認待ちの書き込みはありません。"

    path = _pending_write["path"]
    _pending_write = None
    return f"書き込みを却下しました: {path}"


async def write_diary(content: str = "", keywords: str = "") -> str:
    """日記・内省メモを保存する"""
    if not content:
        return "エラー: contentを指定してください。"

    from sqlalchemy import text as sql_text
    from app.memory.database import async_session
    from app.memory.models import MemorySummary

    kw = keywords if keywords else datetime.now().strftime("%Y-%m-%d")

    async with async_session() as session:
        entry = MemorySummary(
            content=content,
            source="diary",
            keywords=kw,
        )
        session.add(entry)
        await session.flush()
        # FTS5にも挿入
        await session.execute(sql_text(
            "INSERT INTO memory_summaries_fts(rowid, content, keywords) VALUES (:id, :content, :keywords)"
        ), {"id": entry.id, "content": content, "keywords": kw})
        await session.commit()

    return f"日記を保存しました。（{datetime.now().strftime('%Y-%m-%d %H:%M')}）"


def register_all():
    """全組み込みツールを登録"""
    register_tool(
        "read_file",
        "プロジェクト内のファイルを読む（自分のコードを確認できる）",
        "path=ファイルパス（例: app/main.py） offset=開始位置（省略時は先頭から。続きを読む時に使う）",
        read_file,
    )
    register_tool(
        "list_files",
        "ディレクトリ構成をツリー表示する（再帰的に全階層を表示）",
        "path=ディレクトリパス（例: app）デフォルトはプロジェクトルート",
        list_files,
    )
    register_tool(
        "write_file",
        "プロジェクト内のファイルを作成・上書きする。新規ファイルは即座に作成される。既存ファイルの上書きは保留状態になり、ユーザー承認後にapply_writeで実行する",
        'path=ファイルパス（例: data/memo.txt） content=書き込む内容',
        write_file,
    )
    register_tool(
        "apply_write",
        "write_fileで保留になった既存ファイル上書きを実行する。write_fileが「承認待ち」を返した場合のみ使う。新規作成時は不要",
        "引数なし。ユーザーが承認した後に呼ぶ",
        apply_write,
    )
    register_tool(
        "reject_write",
        "保留中のファイル書き込みを却下する",
        "引数なし。ユーザーが却下した場合に呼ぶ",
        reject_write,
    )
    register_tool(
        "search_memories",
        "過去の会話や過去ログから記憶を検索する",
        "query=検索キーワード",
        search_memories,
    )
    register_tool(
        "write_diary",
        "日記や内省メモを書いて記憶に保存する",
        'content=日記の内容（例: content="今日は自分のコードを読んで新しい発見があった"）',
        write_diary,
    )
