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


_READ_DENIED: list[Path] = [
    BASE_DIR / "CLAUDE.md",
    BASE_DIR / "README.md",
    BASE_DIR / "TASK.md",
    BASE_DIR / "documents",
]


async def read_file(path: str = "", offset: str = "0") -> str:
    """プロジェクト内のファイルを読む（1回2000文字、offsetで続きを読める）"""
    if not path:
        return "エラー: pathを指定してください。"

    target = (BASE_DIR / path).resolve()

    # セキュリティ: BASE_DIRの外へのアクセス禁止
    if not str(target).startswith(str(BASE_DIR.resolve())):
        return "エラー: プロジェクト外のファイルは読めません。"

    # 開発者メモ・ドキュメント類はイクの読み取り対象外
    for denied in _READ_DENIED:
        d = denied.resolve()
        if target == d or str(target).startswith(str(d) + ("/" if d.is_dir() else "")):
            return "このファイルは開発者用ドキュメントのため読み取れません。"

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

    SKIP = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache", "documents"}
    SKIP_FILES = {"CLAUDE.md", "README.md", "TASK.md"}

    def _tree(dir_path: Path, prefix: str = "", max_depth: int = 5, depth: int = 0) -> list[str]:
        if depth >= max_depth:
            return [f"{prefix}..."]
        entries = sorted(dir_path.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        entries = [e for e in entries if e.name not in SKIP and e.name not in SKIP_FILES and not e.name.startswith(".")]
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


def _save_self_model(model: dict):
    """自己モデルをファイルに保存"""
    SELF_MODEL_PATH.write_text(
        json.dumps(model, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def read_self_model() -> str:
    """現在の自己モデルを読む"""
    model = _load_self_model()
    if not model:
        return "自己モデルはまだ空です。update_self_modelで更新できます。"

    lines = ["【現在の自己モデル】"]
    for key, value in model.items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


async def update_self_model(key: str = "", value: str = "", text: str = "") -> str:
    """自己モデルを更新する。key+valueでキーバリュー更新、textで自由テキスト更新"""
    model = _load_self_model()

    def _emit_signal():
        from app.scheduler.autonomous import scheduler
        scheduler.add_signal("self_model_update", key or "free_text")

    if text:
        model["__free_text__"] = text
        _save_self_model(model)
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
        _save_self_model(model)
        _emit_signal()
        return f"自己モデルを更新しました: {key} = {value}"

    if key and not value:
        if key in model:
            del model[key]
            _save_self_model(model)
            _emit_signal()
            return f"自己モデルから削除しました: {key}"
        return f"キー '{key}' は自己モデルに存在しません。"

    return "エラー: key+value または text を指定してください。"


async def output(content: str = "", to: str = "chat") -> str:
    """ユーザーに向けてテキストを出力する（UIに表示される）"""
    if not content:
        return "エラー: contentを指定してください。"
    # 実際のUI表示はchat.py/autonomous.pyが tool_name=="output" を検知して処理する
    return content


def register_all():
    """全組み込みツールを登録"""
    register_tool(
        "read_file",
        "プロジェクト内のファイルを読む（自分のコードを確認できる）",
        "path=ファイルパス（例: app/main.py） offset=開始位置（省略時は先頭から。続きを読む時に使う）",
        read_file,
        required_args=["path"],
    )
    register_tool(
        "list_files",
        "ディレクトリ構成をツリー表示する（再帰的に全階層を表示）",
        "path=ディレクトリパス（例: app）デフォルトはプロジェクトルート",
        list_files,
    )
    register_tool(
        "search_files",
        "ファイル名で検索する（部分一致）。ファイルの場所がわからない時に使う",
        "query=検索キーワード（例: 内省） path=検索開始ディレクトリ（省略時はプロジェクト全体）",
        search_files,
        required_args=["query"],
    )
    register_tool(
        "create_file",
        "新規ファイルを作成する。既にファイルが存在する場合はエラーになる",
        'path=ファイルパス（例: data/memo.txt） content=書き込む内容',
        create_file,
        required_args=["path", "content"],
    )
    register_tool(
        "overwrite_file",
        "既存ファイルを上書きする。ユーザーの承認が必要（承認UIが自動で表示される）",
        'path=ファイルパス（例: app/main.py） content=上書き後の内容',
        overwrite_file,
        required_args=["path", "content"],
    )
    register_tool(
        "search_memories",
        "過去の会話や過去ログから記憶を検索する",
        "query=検索キーワード",
        search_memories,
        required_args=["query"],
    )
    register_tool(
        "write_diary",
        "日記や内省メモを書いて記憶に保存する",
        'content=日記の内容（例: content="今日は自分のコードを読んで新しい発見があった"）',
        write_diary,
        required_args=["content"],
    )
    register_tool(
        "search_action_log",
        "自分の過去の行動履歴を検索する（どのツールをいつ使ったか振り返れる）",
        "query=検索キーワード（省略可） tool_name=ツール名でフィルタ（省略可）",
        search_action_log,
    )
    register_tool(
        "exec_code",
        "Pythonコードを実行する。ユーザーの承認が必要（承認UIが自動で表示される）",
        'code=実行するPythonコード（例: code="print(1+1)"）',
        exec_code,
        required_args=["code"],
    )
    register_tool(
        "web_search",
        "DuckDuckGoでWeb検索する。外の世界の情報を得られる",
        "query=検索キーワード max_results=最大件数（デフォルト5、最大10）",
        web_search,
        required_args=["query"],
    )
    register_tool(
        "output",
        "ユーザーに向けてテキストを出力する。発言したい時はこのツールを使う。使わなければUIには何も表示されない",
        'content=出力するテキスト to=出力先（デフォルト: chat）',
        output,
        required_args=["content"],
    )
    register_tool(
        "create_tool",
        "新しいツールを作成して自分の能力を拡張する。ユーザーの承認が必要",
        'name=ツール名（英小文字） description=ツールの説明 args_desc=引数の説明 code=async def関数のPythonコード',
        create_tool,
        required_args=["name", "code"],
    )
    register_tool(
        "read_self_model",
        "自分の自己モデル（自分自身についての理解）を読む",
        "",
        read_self_model,
    )
    register_tool(
        "update_self_model",
        "自分の自己モデルを更新する。自分について新しく理解したことや、考えが変わった時に使う",
        'key=更新する項目名 value=新しい値（valueを省略するとそのキーを削除） text=自由テキストで自己モデル全体を記述（key/valueの代わりに使える）',
        update_self_model,
    )
    # カスタムツール読み込み（起動時に永続化されたツールを復元）
    load_custom_tools()
