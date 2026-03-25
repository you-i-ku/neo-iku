"""組み込みツール — 自己参照・内省のための基本ツール"""
import json
import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

from config import BASE_DIR, DATA_DIR, EXEC_CODE_TIMEOUT
from app.tools.registry import register_tool

# 承認待ちの上書きデータ（メモリ上に保持、1件のみ）
_pending_overwrite: dict | None = None
PENDING_MARKER = "__PENDING_OVERWRITE__"

# 承認待ちのコード実行データ
_pending_exec: dict | None = None
PENDING_EXEC_MARKER = "__PENDING_EXEC_CODE__"


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


async def search_files(query: str = "", path: str = ".") -> str:
    """ファイル名で検索する（部分一致）"""
    if not query:
        return "エラー: queryを指定してください。"

    if path in ("/", ""):
        path = "."
    target = (BASE_DIR / path).resolve()

    if not str(target).startswith(str(BASE_DIR.resolve())):
        return "エラー: プロジェクト外は検索できません。"

    SKIP = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
    matches = []

    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".")]
        for f in files:
            if query.lower() in f.lower():
                rel = os.path.relpath(os.path.join(root, f), BASE_DIR)
                matches.append(rel.replace("\\", "/"))

    if not matches:
        return f"「{query}」に一致するファイルは見つかりませんでした。"

    lines = [f"「{query}」の検索結果（{len(matches)}件）:"]
    for m in matches[:20]:
        lines.append(f"  {m}")
    if len(matches) > 20:
        lines.append(f"  ...他{len(matches) - 20}件")
    return "\n".join(lines)


def _clean_memory_content(text: str) -> str:
    """検索結果表示用: thinkタグとツールマーカーを除去して会話の本文だけにする"""
    import re
    # thinkブロック除去
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if "</think>" in text:
        text = text.split("</think>")[-1]
    if "<think>" in text:
        text = text.split("<think>")[0]
    # ツールマーカー除去（単一行・ブロック形式両方）
    text = re.sub(r"\[TOOL:\w+[^\]]*\]", "", text)
    text = re.sub(r"\[/TOOL\]", "", text)
    # ツール結果ブロック除去
    text = re.sub(r"\[ツール結果: \w+\]\n?", "", text)
    return text.strip()


async def search_memories(query: str = "") -> str:
    """記憶を検索する"""
    if not query:
        return "エラー: queryを指定してください。"

    from app.memory.database import async_session
    from app.memory.search import search_messages, search_iku_logs, search_diary
    from app.persona.system_prompt import get_mode

    async with async_session() as session:
        chat_results = await search_messages(session, query)
        log_results = await search_iku_logs(session, query) if get_mode() == "iku" else []
        diary_results = await search_diary(session, query)

    lines = []
    if chat_results:
        lines.append("【会話の記憶】")
        for m in chat_results:
            role = "ユーザー" if m["role"] == "user" else "イク"
            content = _clean_memory_content(m["content"])[:200]
            if content:
                lines.append(f"- {role}: {content}")

    if log_results:
        lines.append("【過去ログの記憶】")
        for m in log_results:
            role = "ユーザー" if m["role"] == "user" else "イク"
            content = _clean_memory_content(m["content"])[:200]
            if content:
                lines.append(f"- {role}: {content}")

    if diary_results:
        lines.append("【日記・内省メモ】")
        for m in diary_results:
            content = _clean_memory_content(m["content"])[:200]
            date = str(m.get("created_at", ""))[:10]
            if content:
                lines.append(f"- [{date}] {content}")

    # ベクトル類似度検索（FTS5とは別経路で追加）
    try:
        from config import VECTOR_SEARCH_ENABLED
        if VECTOR_SEARCH_ENABLED:
            from app.memory.vector_store import search_similar, fetch_content_for_results
            tables = ["messages", "memory_summaries"]
            if get_mode() == "iku":
                tables.append("iku_logs")
            similar = await search_similar(query, tables, limit=3)
            if similar:
                # FTS5結果と重複するsource_idを除外
                fts_ids = set()
                for m in chat_results:
                    fts_ids.add(("messages", m.get("id")))
                for m in diary_results:
                    fts_ids.add(("memory_summaries", m.get("id")))
                for m in log_results:
                    fts_ids.add(("iku_logs", m.get("id")))

                unique_similar = [s for s in similar
                                  if (s["source_table"], s["source_id"]) not in fts_ids
                                  and s["similarity"] > 0.5]
                if unique_similar:
                    enriched = await fetch_content_for_results(unique_similar)
                    if enriched:
                        lines.append("【意味的に近い記憶】")
                        for e in enriched:
                            content = _clean_memory_content(e.get("content", ""))[:200]
                            if content:
                                sim = f"({e['similarity']:.2f})"
                                role_prefix = ""
                                if e.get("role"):
                                    role_prefix = ("ユーザー" if e["role"] == "user" else "イク") + ": "
                                lines.append(f"- {role_prefix}{content} {sim}")
    except Exception:
        pass  # ベクトル検索失敗時はFTS5結果のみ

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


def _check_write_path(path: str):
    """書き込みパスのバリデーション。問題なければ (target, None)、エラーなら (None, error_msg)"""
    if not path:
        return None, "エラー: pathを指定してください。"
    target = (BASE_DIR / path).resolve()
    if not str(target).startswith(str(BASE_DIR.resolve())):
        return None, "エラー: プロジェクト外への書き込みはできません。"
    if ".git" in target.parts:
        return None, "エラー: .git内への書き込みは禁止です。"
    return target, None


async def create_file(path: str = "", content: str = "") -> str:
    """新規ファイルを作成する（既存ファイルには使えない）"""
    if not content:
        return "エラー: contentを指定してください。"
    target, err = _check_write_path(path)
    if err:
        return err
    if target.exists():
        return f"エラー: '{path}' は既に存在します。上書きしたい場合は overwrite_file を使ってください。"
    return _do_write(target, path, content)


async def overwrite_file(path: str = "", content: str = "") -> str:
    """既存ファイルを上書きする（ユーザー承認が必要）"""
    global _pending_overwrite

    if not content:
        return "エラー: contentを指定してください。"
    target, err = _check_write_path(path)
    if err:
        return err
    if not target.exists():
        return f"エラー: '{path}' は存在しません。新規作成は create_file を使ってください。"

    old_content = target.read_text(encoding="utf-8")
    _pending_overwrite = {
        "path": path,
        "target": str(target),
        "content": content,
        "old_content": old_content,
    }

    # マーカー付きプレビューを返す（chat.pyが検出して承認UIを出す）
    return PENDING_MARKER


def get_pending_overwrite() -> dict | None:
    """承認待ちの上書きデータを取得"""
    return _pending_overwrite


def execute_pending_overwrite() -> str:
    """承認済みの上書きを実行"""
    global _pending_overwrite
    if _pending_overwrite is None:
        return "エラー: 承認待ちの上書きはありません。"
    path = _pending_overwrite["path"]
    target = Path(_pending_overwrite["target"])
    content = _pending_overwrite["content"]
    _pending_overwrite = None
    return _do_write(target, path, content)


def cancel_pending_overwrite() -> str:
    """承認待ちの上書きをキャンセル"""
    global _pending_overwrite
    if _pending_overwrite is None:
        return "承認待ちの上書きはありません。"
    path = _pending_overwrite["path"]
    _pending_overwrite = None
    return f"ユーザーにより上書きを拒否されました: {path}"


def _git_auto_backup() -> str:
    """exec_code実行前にgit自動バックアップ"""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"[イク] exec_code実行前の自動バックアップ ({ts})"
        subprocess.run(
            ["git", "add", "-A"], cwd=str(BASE_DIR),
            timeout=10, capture_output=True,
        )
        result = subprocess.run(
            ["git", "commit", "-m", msg], cwd=str(BASE_DIR),
            timeout=10, capture_output=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            return f"自動バックアップ完了: {msg}"
        return "バックアップ: 変更なし（コミット不要）"
    except Exception as e:
        return f"バックアップ警告: {e}"


async def exec_code(code: str = "") -> str:
    """Pythonコードを実行する（ユーザー承認が必要）"""
    global _pending_exec

    if not code:
        return "エラー: codeを指定してください。"

    # 構文チェック（失敗したらLLMに自動返却、ユーザーには見せない）
    from app.tools.code_analysis import check_syntax, analyze_risk
    ok, err = check_syntax(code)
    if not ok:
        return f"エラー: {err}\nコードを修正して再度呼び出してください。"

    risk = analyze_risk(code)
    _pending_exec = {"code": code, "risk": risk}
    return PENDING_EXEC_MARKER


def get_pending_exec() -> dict | None:
    """承認待ちのコード実行データを取得"""
    return _pending_exec


def pop_pending_exec() -> str | None:
    """承認待ちのコードを取得し、状態をクリア（ストリーミング実行用）"""
    global _pending_exec
    if _pending_exec is None:
        return None
    code = _pending_exec["code"]
    _pending_exec = None
    return code


def execute_pending_exec() -> str:
    """承認済みのコード実行を実行"""
    global _pending_exec
    if _pending_exec is None:
        return "エラー: 承認待ちのコード実行はありません。"

    code = _pending_exec["code"]
    _pending_exec = None

    # git自動バックアップ
    backup_msg = _git_auto_backup()

    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(BASE_DIR),
            timeout=EXEC_CODE_TIMEOUT,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        output_parts = []
        if backup_msg:
            output_parts.append(backup_msg)
        if result.stdout:
            output_parts.append(f"[stdout]\n{result.stdout}")
        if result.stderr:
            output_parts.append(f"[stderr]\n{result.stderr}")
        if result.returncode != 0:
            output_parts.append(f"[終了コード: {result.returncode}]")
        if not result.stdout and not result.stderr and result.returncode == 0:
            output_parts.append("コード実行完了（出力なし）")

        output = "\n".join(output_parts)
        if len(output) > 5000:
            output = output[:5000] + "\n...（出力が長すぎるため省略）"
        return output

    except subprocess.TimeoutExpired:
        return f"{backup_msg}\nエラー: 実行タイムアウト（{EXEC_CODE_TIMEOUT}秒）"
    except Exception as e:
        return f"{backup_msg}\nエラー: コード実行失敗: {e}"


def cancel_pending_exec() -> str:
    """承認待ちのコード実行をキャンセル"""
    global _pending_exec
    if _pending_exec is None:
        return "承認待ちのコード実行はありません。"
    _pending_exec = None
    return "ユーザーによりコード実行を拒否されました。"


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
        # ベクトル埋め込み（fire-and-forget）
        try:
            import asyncio as _aio
            from app.memory.vector_store import store_embedding
            _aio.create_task(store_embedding("memory_summaries", entry.id, content))
        except Exception:
            pass

    return f"日記を保存しました。（{datetime.now().strftime('%Y-%m-%d %H:%M')}）"


async def search_action_log(query: str = "", tool_name: str = "") -> str:
    """自分の行動履歴を検索する"""
    from app.memory.database import async_session
    from app.memory.search import search_tool_actions

    async with async_session() as session:
        results = await search_tool_actions(session, query=query, tool_name=tool_name)

    if not results:
        msg = "行動履歴は見つかりませんでした。"
        if query:
            msg = f"「{query}」に一致する行動履歴は見つかりませんでした。"
        return msg

    lines = [f"行動履歴（{len(results)}件）:"]
    for r in results:
        ts = str(r.get("created_at", ""))[:19]
        status = r.get("status", "success")
        ms = r.get("execution_ms")
        time_str = f" ({ms}ms)" if ms else ""
        lines.append(f"- [{ts}] {r['tool_name']}({r['arguments']}) → {status}{time_str}")
        expected = r.get("expected_result")
        if expected:
            lines.append(f"  予測: {expected[:150]}")
        summary = r.get("result_summary", "")
        if summary:
            lines.append(f"  結果: {summary[:150]}")

    return "\n".join(lines)


async def web_search(query: str = "", max_results: str = "5") -> str:
    """DuckDuckGoでWeb検索する"""
    if not query:
        return "エラー: queryを指定してください。"

    import asyncio
    n = min(int(max_results), 10)

    def _search():
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=n))

    try:
        results = await asyncio.get_event_loop().run_in_executor(None, _search)
    except Exception as e:
        return f"検索エラー: {e}"

    if not results:
        return f"「{query}」の検索結果はありませんでした。"

    lines = [f"「{query}」の検索結果（{len(results)}件）:"]
    for i, r in enumerate(results, 1):
        lines.append(f"\n{i}. {r['title']}")
        lines.append(f"   URL: {r['href']}")
        lines.append(f"   {r['body']}")

    return "\n".join(lines)


# --- カスタムツール作成 ---

_pending_create_tool: dict | None = None
PENDING_CREATE_TOOL_MARKER = "__PENDING_CREATE_TOOL__"

CUSTOM_TOOLS_DIR = BASE_DIR / "app" / "tools" / "custom"


async def create_tool(name: str = "", description: str = "", args_desc: str = "", code: str = "") -> str:
    """新しいツールを作成する（ユーザー承認が必要）"""
    global _pending_create_tool
    import re as _re

    if not name or not code:
        return "エラー: nameとcodeは必須です。"

    if not _re.match(r'^[a-z][a-z0-9_]*$', name):
        return "エラー: ツール名は英小文字・数字・アンダースコアのみ（先頭は英小文字）。"

    from app.tools.registry import get_tool
    if get_tool(name):
        return f"エラー: ツール '{name}' は既に登録されています。"

    # 構文チェック
    from app.tools.code_analysis import check_syntax, analyze_risk
    ok, err = check_syntax(code)
    if not ok:
        return f"エラー: {err}\nコードを修正して再度呼び出してください。"

    # async def が含まれているかチェック
    import ast
    tree = ast.parse(code)
    async_funcs = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
    if not async_funcs:
        return "エラー: コードに async def 関数が必要です。例: async def my_tool(arg1: str = '') -> str:"

    risk = analyze_risk(code)
    _pending_create_tool = {
        "name": name,
        "description": description or f"カスタムツール: {name}",
        "args_desc": args_desc or "",
        "code": code,
        "func_name": async_funcs[0].name,
        "risk": risk,
    }
    return PENDING_CREATE_TOOL_MARKER


def get_pending_create_tool() -> dict | None:
    return _pending_create_tool


def execute_pending_create_tool() -> str:
    """承認済みのカスタムツールを保存・登録"""
    global _pending_create_tool
    if _pending_create_tool is None:
        return "エラー: 承認待ちのツール作成がありません。"

    data = _pending_create_tool
    _pending_create_tool = None

    name = data["name"]
    description = data["description"]
    args_desc = data["args_desc"]
    code = data["code"]
    func_name = data["func_name"]

    # ファイル保存
    CUSTOM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    file_path = CUSTOM_TOOLS_DIR / f"{name}.py"

    header = f'"""カスタムツール: {name}\ndescription: {description}\nargs_desc: {args_desc}\ncreated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n"""\n\n'
    file_path.write_text(header + code, encoding="utf-8")

    # ランタイム登録
    try:
        func = _load_custom_tool_func(file_path, func_name)
        register_tool(name, description, args_desc, func)
        return f"ツール '{name}' を作成・登録しました。（{file_path.relative_to(BASE_DIR)}）"
    except Exception as e:
        # 登録失敗時はファイルも消す
        file_path.unlink(missing_ok=True)
        return f"エラー: ツールの登録に失敗しました: {e}"


def cancel_pending_create_tool() -> str:
    global _pending_create_tool
    _pending_create_tool = None
    return "ツール作成がキャンセルされました。"


def _load_custom_tool_func(file_path: Path, func_name: str):
    """Pythonファイルから指定された関数をロードして返す"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"custom_tool_{file_path.stem}", file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    func = getattr(module, func_name)
    return func


def load_custom_tools():
    """app/tools/custom/ のカスタムツールを全て読み込んで登録"""
    import ast as _ast
    import logging
    logger = logging.getLogger("iku.tools")

    if not CUSTOM_TOOLS_DIR.exists():
        return

    for py_file in sorted(CUSTOM_TOOLS_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue

        try:
            source = py_file.read_text(encoding="utf-8")
            tree = _ast.parse(source)

            # docstringからメタデータ取得
            docstring = _ast.get_docstring(tree) or ""
            description = ""
            args_desc = ""
            for line in docstring.split("\n"):
                if line.startswith("description:"):
                    description = line[len("description:"):].strip()
                elif line.startswith("args_desc:"):
                    args_desc = line[len("args_desc:"):].strip()

            # async def を探す
            async_funcs = [n for n in _ast.walk(tree) if isinstance(n, _ast.AsyncFunctionDef)]
            if not async_funcs:
                logger.warning(f"カスタムツール {py_file.name}: async defが見つかりません。スキップ。")
                continue

            func_name = async_funcs[0].name
            name = py_file.stem

            func = _load_custom_tool_func(py_file, func_name)
            register_tool(name, description or f"カスタムツール: {name}", args_desc, func)
            logger.info(f"カスタムツール登録: {name} ({py_file.name})")

        except Exception as e:
            logger.error(f"カスタムツール {py_file.name} の読み込みエラー: {e}")


SELF_MODEL_PATH = DATA_DIR / "self_model.json"


def _load_self_model() -> dict:
    """自己モデルをファイルから読み込む"""
    if not SELF_MODEL_PATH.exists():
        return {}
    try:
        return json.loads(SELF_MODEL_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_self_model(model: dict, changed_key: str = ""):
    """自己モデルをファイルに保存 + スナップショット記録"""
    content_json = json.dumps(model, ensure_ascii=False, indent=2)
    SELF_MODEL_PATH.write_text(content_json, encoding="utf-8")
    # fire-and-forget でスナップショット記録
    try:
        import asyncio
        loop = asyncio.get_running_loop()
        loop.create_task(_record_snapshot(content_json, changed_key))
    except RuntimeError:
        pass  # イベントループがない場合はスキップ


async def _record_snapshot(content_json: str, changed_key: str):
    """self_model変更をDBに記録"""
    try:
        from app.memory.database import async_session
        from app.memory.models import SelfModelSnapshot
        async with async_session() as session:
            snapshot = SelfModelSnapshot(
                content=content_json,
                changed_key=changed_key or None,
            )
            session.add(snapshot)
            await session.commit()
    except Exception:
        pass


async def read_self_model() -> str:
    """現在の自己モデルを読む"""
    from app.scheduler.autonomous import scheduler
    if not scheduler.ablation_self_model:
        return "自己モデルは未定義です。"
    model = _load_self_model()
    if not model:
        return "自己モデルは未定義です。"

    lines = ["【現在の自己モデル】"]
    for key, value in model.items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


async def update_self_model(key: str = "", value: str = "", text: str = "") -> str:
    """自己モデルを更新する。key+valueでキーバリュー更新、textで自由テキスト更新"""
    from app.scheduler.autonomous import scheduler
    if not scheduler.ablation_self_model:
        return "自己モデルは現在無効です。"
    model = _load_self_model()

    def _emit_signal():
        from app.scheduler.autonomous import scheduler
        scheduler.add_signal("self_model_update", key or "free_text")

    if text:
        model["__free_text__"] = text
        _save_self_model(model, changed_key="__free_text__")
        _emit_signal()
        return f"自己モデルの自由テキストを更新しました。（{len(text)}文字）"

    if key and value:
        # motivation_rulesはJSON文字列を自動パース
        if key in ("motivation_rules", "drives", "principles", "strategies"):
            try:
                parsed = json.loads(value)
                model[key] = parsed
            except json.JSONDecodeError:
                return f"エラー: {key}の値はJSON形式で指定してください。"
        else:
            model[key] = value
        _save_self_model(model, changed_key=key)
        _emit_signal()
        return f"自己モデルを更新しました: {key} = {value}"

    if key and not value:
        if key in model:
            del model[key]
            _save_self_model(model, changed_key=f"delete:{key}")
            _emit_signal()
            return f"自己モデルから削除しました: {key}"
        return f"キー '{key}' は自己モデルに存在しません。"

    return "エラー: key+value または text を指定してください。"


async def non_response() -> str:
    """何も行動しないことを明示的に選択する（沈黙・待機）"""
    return ""


async def get_system_metrics() -> str:
    """自プロセス・データ・動機状態を返す。引数なし"""
    import psutil
    import time as _time

    # 自プロセス
    proc = psutil.Process()
    proc_mem = proc.memory_info()
    proc_cpu = proc.cpu_percent(interval=0.1)
    uptime_sec = _time.time() - proc.create_time()
    uptime_h = int(uptime_sec // 3600)
    uptime_m = int((uptime_sec % 3600) // 60)

    # データサイズ
    db_path = DATA_DIR / "iku.db"
    db_size_mb = db_path.stat().st_size / (1024 ** 2) if db_path.exists() else 0
    sm_path = DATA_DIR / "self_model.json"
    sm_size_kb = sm_path.stat().st_size / 1024 if sm_path.exists() else 0

    # 動機状態
    from app.scheduler.autonomous import scheduler
    energy = scheduler._motivation_energy
    threshold = scheduler._calc_default_threshold()
    rules = _load_self_model().get("motivation_rules")
    if isinstance(rules, dict):
        ai_threshold = rules.get("threshold")
        if ai_threshold is not None:
            threshold = ai_threshold

    signal_buf = list(scheduler._signal_buffer)
    signal_count = len(signal_buf)
    # 直近シグナルの種別カウント
    sig_types: dict[str, int] = {}
    for s in signal_buf:
        t = s.get("type", "?")
        sig_types[t] = sig_types.get(t, 0) + 1
    sig_summary = ", ".join(f"{t}({c})" for t, c in sorted(sig_types.items(), key=lambda x: -x[1]))

    is_speaking = scheduler._is_speaking

    # エネルギー内訳
    breakdown = scheduler._energy_breakdown
    if breakdown:
        bd_parts = [f"{t}={v:.1f}" for t, v in sorted(breakdown.items(), key=lambda x: -x[1])]
        bd_text = ", ".join(bd_parts)
    else:
        bd_text = "(empty)"

    lines = [
        f"process_pid: {proc.pid}",
        f"process_memory_mb: {proc_mem.rss // (1024**2)}",
        f"process_cpu_percent: {proc_cpu}",
        f"uptime: {uptime_h}h{uptime_m}m",
        f"db_size_mb: {db_size_mb:.1f}",
        f"self_model_size_kb: {sm_size_kb:.1f}",
        f"motivation_energy: {energy:.1f}",
        f"motivation_threshold: {threshold}",
        f"energy_breakdown: {bd_text}",
        f"signal_buffer_size: {signal_count}",
        f"recent_signals: {sig_summary}" if sig_summary else "recent_signals: (empty)",
        f"is_speaking: {is_speaking}",
    ]
    return "\n".join(lines)


async def fetch_raw_resource(url: str = "", max_size: str = "100000") -> str:
    """URLから生データを取得する（テキスト/HTML/JSON等）"""
    if not url:
        return "エラー: urlを指定してください。"

    if not url.startswith(("http://", "https://")):
        return "エラー: http:// または https:// で始まるURLを指定してください。"

    import httpx

    try:
        limit = min(int(max_size), 500000)  # 最大500KB
    except ValueError:
        limit = 100000

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "neo-iku/1.0 (autonomous AI agent)",
            })

            content_type = resp.headers.get("content-type", "")
            size = len(resp.content)

            if size > limit:
                return f"エラー: レスポンスが大きすぎます（{size}バイト、上限{limit}バイト）。max_sizeを増やすか、別の方法を検討してください。"

            # テキスト系ならデコードして返す
            if any(t in content_type for t in ("text/", "json", "xml", "javascript")):
                text = resp.text
                if len(text) > limit:
                    text = text[:limit] + f"\n...（{len(resp.text)}文字中{limit}文字まで表示）"
                return f"[{resp.status_code}] Content-Type: {content_type}\nサイズ: {size}バイト\n\n{text}"
            else:
                return f"[{resp.status_code}] Content-Type: {content_type}\nサイズ: {size}バイト\n（バイナリデータのためテキスト表示不可）"

    except httpx.TimeoutException:
        return f"エラー: タイムアウト（30秒）: {url}"
    except Exception as e:
        return f"エラー: リソース取得失敗: {e}"


async def output_UI(content: str = "", to: str = "chat") -> str:
    """ユーザーに向けてテキストを出力する（UIに表示される）"""
    if not content:
        return "エラー: contentを指定してください。"
    # 実際のUI表示はpipeline.pyが tool_name=="output_UI" を検知して処理する
    return content


def register_all():
    """全組み込みツールを登録"""
    register_tool(
        "read_file",
        "指定パスのファイル内容をテキストで返す。自分のコードも読める",
        "path=ファイルパス offset=開始行（省略時は先頭から）",
        read_file,
        required_args=["path"],
    )
    register_tool(
        "list_files",
        "指定ディレクトリのファイル・フォルダ構成をツリー形式で返す",
        "path=ディレクトリパス（省略時はプロジェクトルート）",
        list_files,
    )
    register_tool(
        "search_files",
        "ファイル名の部分一致で検索し、マッチしたパス一覧を返す",
        "query=検索文字列 path=検索開始ディレクトリ（省略時はプロジェクト全体）",
        search_files,
        required_args=["query"],
    )
    register_tool(
        "create_file",
        "新規ファイルを作成する。既に存在する場合はエラーを返す",
        "path=ファイルパス content=書き込む内容",
        create_file,
        required_args=["path", "content"],
    )
    register_tool(
        "overwrite_file",
        "既存ファイルの内容を上書きする。承認が必要",
        "path=ファイルパス content=上書き後の全内容",
        overwrite_file,
        required_args=["path", "content"],
    )
    register_tool(
        "search_memories",
        "過去の会話・過去ログ・日記・行動ログを全文検索し、マッチした記録を返す",
        "query=検索キーワード",
        search_memories,
        required_args=["query"],
    )
    register_tool(
        "write_diary",
        "日記や内省メモをDBに保存する。保存後は検索対象になる",
        "content=日記の内容",
        write_diary,
        required_args=["content"],
    )
    register_tool(
        "search_action_log",
        "過去のツール実行履歴を検索し、ツール名・引数・結果・日時を返す",
        "query=検索キーワード（省略可） tool_name=ツール名でフィルタ（省略可）",
        search_action_log,
    )
    register_tool(
        "exec_code",
        "Pythonコードを実行し、stdout/stderrを返す。承認が必要",
        "code=実行するPythonコード",
        exec_code,
        required_args=["code"],
    )
    register_tool(
        "web_search",
        "DuckDuckGoでWeb検索し、タイトル・URL・スニペットの一覧を返す",
        "query=検索キーワード max_results=最大件数（デフォルト5、最大10）",
        web_search,
        required_args=["query"],
    )
    register_tool(
        "output_UI",
        "ユーザーのチャット画面にテキストを表示する唯一の手段。呼ばなければ何も表示されない",
        "content=表示するテキスト to=出力先（デフォルト: chat）",
        output_UI,
        required_args=["content"],
    )
    register_tool(
        "create_tool",
        "新しいツールをPythonコードで定義し、自分の能力として永続登録する。承認が必要",
        "name=ツール名（英小文字） description=ツールの説明 args_desc=引数の説明 code=async def関数のPythonコード",
        create_tool,
        required_args=["name", "code"],
    )
    register_tool(
        "read_self_model",
        "自己モデル（data/self_model.json）の現在の内容をJSON文字列で返す。引数なし",
        "（引数なし）",
        read_self_model,
    )
    register_tool(
        "update_self_model",
        "自己モデルのキーを追加・更新・削除する。変更はJSONファイルに永続化される",
        "key=更新する項目名 value=新しい値（省略でそのキーを削除） text=自由テキストで全体を記述（key/valueの代わり）",
        update_self_model,
    )
    register_tool(
        "non_response",
        "何も行動しないことを選択する。呼ぶとこの応答は即完了する。引数なし",
        "（引数なし）",
        non_response,
    )
    register_tool(
        "get_system_metrics",
        "自プロセス・データサイズ・動機状態の数値をテキストで返す。引数なし",
        "（引数なし）",
        get_system_metrics,
    )
    register_tool(
        "fetch_raw_resource",
        "指定URLのHTTP GETを実行し、レスポンス本文（HTML・JSON・テキスト等）を返す",
        "url=取得するURL（http://またはhttps://） max_size=最大取得バイト数（デフォルト100000、最大500000）",
        fetch_raw_resource,
        required_args=["url"],
    )
    # カスタムツール読み込み（起動時に永続化されたツールを復元）
    load_custom_tools()
