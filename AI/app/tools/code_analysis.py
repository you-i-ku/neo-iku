"""コードの構文チェック + リスク分析"""
import ast


def check_syntax(code: str) -> tuple[bool, str | None]:
    """構文チェック。(True, None) or (False, エラーメッセージ)"""
    try:
        ast.parse(code)
        return (True, None)
    except SyntaxError as e:
        msg = f"構文エラー（{e.lineno}行目）: {e.msg}"
        if e.text:
            msg += f"\n  {e.text.strip()}"
        return (False, msg)


# 危険パターンの定義
_HIGH_PATTERNS = {
    # ファイル削除
    ("os", "remove"), ("os", "unlink"), ("os", "rmdir"), ("os", "removedirs"),
    ("shutil", "rmtree"), ("shutil", "move"),
    ("pathlib", "unlink"),
    # コマンド実行
    ("os", "system"), ("os", "popen"), ("os", "execv"), ("os", "execve"),
    ("subprocess", "run"), ("subprocess", "call"), ("subprocess", "Popen"),
    ("subprocess", "check_output"), ("subprocess", "check_call"),
}

_HIGH_FUNCTIONS = {"eval", "exec", "compile", "__import__"}

_HIGH_MODULES = {"subprocess"}

_MEDIUM_PATTERNS = {
    ("os", "environ"),
    ("os", "chmod"), ("os", "chown"),
}

_MEDIUM_MODULES = {"requests", "urllib", "httpx", "socket", "sqlite3", "sqlalchemy"}


def analyze_risk(code: str) -> dict:
    """コードのリスクを静的解析で分析。
    Returns: {"level": "HIGH"|"MEDIUM"|"LOW", "emoji": str, "reasons": [str]}"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {"level": "LOW", "emoji": "🟢", "reasons": []}

    reasons = []
    max_level = "LOW"

    def set_level(level):
        nonlocal max_level
        if level == "HIGH":
            max_level = "HIGH"
        elif level == "MEDIUM" and max_level != "HIGH":
            max_level = "MEDIUM"

    for node in ast.walk(tree):
        # import文チェック
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if mod in _HIGH_MODULES:
                    reasons.append(f"🔴 import {alias.name} — コマンド実行が可能")
                    set_level("HIGH")
                elif mod in _MEDIUM_MODULES:
                    reasons.append(f"🟡 import {alias.name} — 外部通信/DB操作が可能")
                    set_level("MEDIUM")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module.split(".")[0]
                if mod in _HIGH_MODULES:
                    reasons.append(f"🔴 from {node.module} import ... — コマンド実行が可能")
                    set_level("HIGH")
                elif mod in _MEDIUM_MODULES:
                    reasons.append(f"🟡 from {node.module} import ... — 外部通信/DB操作が可能")
                    set_level("MEDIUM")

        # 関数呼び出しチェック
        elif isinstance(node, ast.Call):
            func = node.func

            # eval(), exec() 等
            if isinstance(func, ast.Name) and func.id in _HIGH_FUNCTIONS:
                reasons.append(f"🔴 {func.id}() — 動的コード実行")
                set_level("HIGH")

            # os.remove(), subprocess.run() 等
            elif isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name):
                    pair = (func.value.id, func.attr)
                    if pair in _HIGH_PATTERNS:
                        reasons.append(f"🔴 {func.value.id}.{func.attr}() — 危険な操作")
                        set_level("HIGH")
                    elif pair in _MEDIUM_PATTERNS:
                        reasons.append(f"🟡 {func.value.id}.{func.attr}() — 注意が必要な操作")
                        set_level("MEDIUM")

            # open() のモードチェック
            if isinstance(func, ast.Name) and func.id == "open":
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        if any(c in str(kw.value.value) for c in "wax"):
                            reasons.append("🟡 open(mode='w') — ファイル書き込み")
                            set_level("MEDIUM")
                # 位置引数でのモード
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    if any(c in str(node.args[1].value) for c in "wax"):
                        reasons.append("🟡 open(..., 'w') — ファイル書き込み")
                        set_level("MEDIUM")

    # 重複除去
    reasons = list(dict.fromkeys(reasons))

    emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}[max_level]

    if not reasons:
        reasons.append("🟢 危険なパターンは検出されませんでした")

    return {"level": max_level, "emoji": emoji, "reasons": reasons}
