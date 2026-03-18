"""ツールレジストリ — 登録・検出・実行"""
import re
import logging

logger = logging.getLogger("iku.tools")

# ツール登録辞書: name -> {description, args_desc, func}
_tools: dict = {}


def register_tool(name: str, description: str, args_desc: str, func, required_args: list[str] | None = None):
    """ツールを登録する。required_argsがあると、それらが欠けた呼び出しは無視される"""
    _tools[name] = {
        "description": description,
        "args_desc": args_desc,
        "func": func,
        "required_args": required_args or [],
    }
    logger.debug(f"ツール登録: {name}")


def get_tool(name: str) -> dict | None:
    return _tools.get(name)


def get_all_tools() -> dict:
    return dict(_tools)


def build_tools_prompt() -> str:
    """システムプロンプトに追加するツール説明文を生成"""
    if not _tools:
        return ""

    lines = ["あなたは以下のツールを使えます。使いたい時は応答の中に [TOOL:ツール名 引数名=値] と書いてください。",
             "ツールの結果が返ってくるので、それを見て回答を続けてください。", ""]

    for name, info in _tools.items():
        lines.append(f"- {name}: {info['description']}")
        if info["args_desc"]:
            lines.append(f"  引数: {info['args_desc']}")

    lines.append("")
    lines.append("重要: [TOOL:...]は必ず応答テキスト内に書いてください（thinkの外に）。")
    lines.append('例: [TOOL:read_file path=app/main.py]')
    lines.append('例: [TOOL:search_memories query=過去の会話]')
    lines.append('例: [TOOL:write_diary content=今日は自分のコードを読んで面白い発見があった]')
    lines.append("")
    lines.append("1回の応答で複数のツールを同時に呼び出すこともできます:")
    lines.append('例: [TOOL:read_file path=README.md]')
    lines.append('[TOOL:read_file path=config.py]')
    lines.append("")
    lines.append("複数行の内容を書き込む場合はブロック形式を使えます:")
    lines.append("  [TOOL:create_file path=ファイルパス]")
    lines.append("  （ここに書き込みたい内容をそのまま全文書く。何行でもOK）")
    lines.append("  [/TOOL]")
    lines.append("※[TOOL]〜[/TOOL]の間がそのままファイルに書き込まれます。省略せず、実際の内容を全て書いてください。")
    return "\n".join(lines)


# テキストマーカーパターン
# ブロック形式: [TOOL:name key=value]\n内容\n[/TOOL]
_BLOCK_PATTERN = re.compile(r"\[TOOL:(\w+)(.*?)\]\s*\n(.*?)\[/TOOL\]", re.DOTALL)
# 複数行対応: [TOOL:name key="複数行の値"] — content="..."が改行を含むケース
_MULTILINE_PATTERN = re.compile(r'\[TOOL:(\w+)\s+(.*?")\s*\]', re.DOTALL)
# 単一行: [TOOL:name key=value]
_TOOL_PATTERN = re.compile(r"\[TOOL:(\w+)(.*?)\]")


def _parse_args(args_str: str) -> dict:
    """引数文字列をパースして辞書を返す。クォート付きとクォートなしの混在に対応。"""
    args = {}
    if not args_str:
        return args

    # クォート付きの値をパース: key="value"（複数行対応）
    quoted = list(re.finditer(r'(\w+)="((?:[^"\\]|\\.)*)"', args_str, re.DOTALL))
    if quoted:
        for part in quoted:
            val = part.group(2).replace('\\"', '"')
            val = val.replace('\\n', '\n').replace('\\t', '\t')
            args[part.group(1)] = val

        # クォートされてない引数も拾う（path=xxx content="yyy" の path 部分）
        remaining = args_str
        for part in quoted:
            remaining = remaining.replace(part.group(0), "")
        for part in re.finditer(r'(\w+)=([^\s"]+)', remaining):
            if part.group(1) not in args:
                args[part.group(1)] = part.group(2)
    else:
        # クォートなし: まず複数引数パターン (key=value key=value) を試す
        multi = list(re.finditer(r'(\w+)=([^\s]+)', args_str))
        if len(multi) >= 2:
            # 複数引数: それぞれ個別に取る
            for part in multi:
                args[part.group(1)] = part.group(2)
        elif multi:
            # 1つだけ: key= の後ろ全部を値として取る（スペース含むかもしれない）
            single = re.match(r'(\w+)=(.*)', args_str, re.DOTALL)
            if single:
                args[single.group(1)] = single.group(2).strip()
        else:
            # パターンにマッチしないが引数文字列は存在する → パース失敗マーカー
            if args_str.strip():
                args["__parse_failed__"] = args_str.strip()

    return args


def _clean_content(content: str) -> str:
    """LLMが付けがちなトリプルクォートや余計な囲みを除去"""
    content = content.strip()
    for q in ['"""', "'''"]:
        if content.startswith(q) and content.endswith(q):
            content = content[3:-3].strip()
    return content


def parse_tool_call(text: str) -> tuple[str, dict] | None:
    """テキストから最初のツール呼び出しを検出し (name, args_dict) を返す。
    3つの形式に対応:
    1. ブロック形式: [TOOL:name args]\\n内容\\n[/TOOL]
    2. 複数行クォート: [TOOL:name content="複数行"]
    3. 単一行: [TOOL:name key=value]"""

    # 1. ブロック形式を先にチェック（[/TOOL]で閉じる形式）
    block_match = _BLOCK_PATTERN.search(text)
    if block_match:
        name = block_match.group(1)
        args_str = block_match.group(2).strip()
        block_content = block_match.group(3)

        args = _parse_args(args_str)
        if "content" not in args:
            args["content"] = _clean_content(block_content)

        return (name, args)

    # 2. 複数行クォート形式（content="改行を含む値"]）
    multi_match = _MULTILINE_PATTERN.search(text)
    if multi_match:
        name = multi_match.group(1)
        args_str = multi_match.group(2).strip()
        args = _parse_args(args_str)

        # contentの値からトリプルクォート除去
        if "content" in args:
            args["content"] = _clean_content(args["content"])

        return (name, args)

    # 3. 単一行形式
    match = _TOOL_PATTERN.search(text)
    if not match:
        return None

    name = match.group(1)
    args_str = match.group(2).strip()
    args = _parse_args(args_str)

    return (name, args)


def parse_tool_calls(text: str) -> list[tuple[str, dict]]:
    """テキストから全てのツール呼び出しを検出し [(name, args_dict), ...] を返す。
    重複検出を避けるためマッチ済み範囲を除外する。"""
    results = []
    matched_spans = []

    def _overlaps(start: int, end: int) -> bool:
        return any(s <= start < e or s < end <= e for s, e in matched_spans)

    # 1. ブロック形式を先にチェック
    for m in _BLOCK_PATTERN.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        name = m.group(1)
        args_str = m.group(2).strip()
        block_content = m.group(3)
        args = _parse_args(args_str)
        if "content" not in args:
            args["content"] = _clean_content(block_content)
        results.append((m.start(), name, args))
        matched_spans.append((m.start(), m.end()))

    # 2. 複数行クォート形式
    for m in _MULTILINE_PATTERN.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        name = m.group(1)
        args_str = m.group(2).strip()
        args = _parse_args(args_str)
        if "content" in args:
            args["content"] = _clean_content(args["content"])
        results.append((m.start(), name, args))
        matched_spans.append((m.start(), m.end()))

    # 3. 単一行形式
    for m in _TOOL_PATTERN.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        name = m.group(1)
        args_str = m.group(2).strip()
        args = _parse_args(args_str)
        results.append((m.start(), name, args))
        matched_spans.append((m.start(), m.end()))

    # 出現順にソート
    results.sort(key=lambda x: x[0])

    # 必須引数チェック: 欠けている場合はエラー情報を付与
    filtered = []
    for _, name, args in results:
        tool = _tools.get(name)

        # パース失敗マーカーがある場合 → エラーとして返す
        if "__parse_failed__" in args:
            raw = args["__parse_failed__"]
            args["__error__"] = f"引数のパースに失敗しました。元の引数: {raw}"
            logger.debug(f"ツール呼び出しエラー（パース失敗）: {name} raw={raw}")
            filtered.append((name, args))
            continue

        if tool and tool.get("required_args"):
            missing = [r for r in tool["required_args"] if not args.get(r)]
            if missing:
                # argsが完全に空 → 会話文中の言及と見なしてスキップ
                if not args:
                    logger.debug(f"ツール呼び出しスキップ（引数なし、会話中の言及）: {name}")
                    continue
                # 一部の引数はあるが必須が欠けている → パース失敗、エラーとして返す
                args["__error__"] = f"必須引数が不足しています: {', '.join(missing)}"
                logger.debug(f"ツール呼び出しエラー（必須引数不足）: {name} missing={missing}")
        filtered.append((name, args))

    return filtered


async def execute_tool(name: str, args: dict) -> str:
    """ツールを実行し結果文字列を返す。エラーも文字列として返す"""
    # 引数パースエラーがある場合はそのまま返す
    if "__error__" in args:
        error_msg = args["__error__"]
        return f"エラー: {error_msg}（引数の書き方を確認してください。例: [TOOL:{name} key=value]）"

    tool = _tools.get(name)
    if not tool:
        return f"エラー: ツール '{name}' は存在しません。"

    try:
        result = await tool["func"](**args)
        return str(result)
    except Exception as e:
        logger.error(f"ツール実行エラー ({name}): {e}")
        return f"エラー: {e}"
