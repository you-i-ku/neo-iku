"""ツールレジストリ — 登録・検出・実行"""
import re
import logging

logger = logging.getLogger("iku.tools")

# ツール登録辞書: name -> {description, args_desc, func}
_tools: dict = {}


def register_tool(name: str, description: str, args_desc: str, func):
    """ツールを登録する"""
    _tools[name] = {
        "description": description,
        "args_desc": args_desc,
        "func": func,
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
    lines.append("複数行の内容を書き込む場合はブロック形式を使ってください:")
    lines.append("[TOOL:write_file path=ファイルパス]")
    lines.append("ここに内容を書く（複数行OK）")
    lines.append("[/TOOL]")
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
            args[part.group(1)] = part.group(2).replace('\\"', '"')

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
        # else: パースできない → 空のargsを返す

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


async def execute_tool(name: str, args: dict) -> str:
    """ツールを実行し結果文字列を返す。エラーも文字列として返す"""
    tool = _tools.get(name)
    if not tool:
        return f"エラー: ツール '{name}' は存在しません。"

    try:
        result = await tool["func"](**args)
        return str(result)
    except Exception as e:
        logger.error(f"ツール実行エラー ({name}): {e}")
        return f"エラー: {e}"
