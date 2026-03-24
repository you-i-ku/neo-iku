"""ツールレジストリ — 登録・検出・実行"""
import inspect
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
    _invalidate_pattern_cache()
    logger.debug(f"ツール登録: {name}")


def get_tool(name: str) -> dict | None:
    return _tools.get(name)


def get_all_tools() -> dict:
    return dict(_tools)


def build_tools_prompt() -> str:
    """システムプロンプトに追加するツール説明文を生成"""
    if not _tools:
        return ""

    # カテゴリ定義（中立的ラベル）
    _categories = [
        ("ファイル", ["read_file", "list_files", "search_files", "create_file", "overwrite_file"]),
        ("記憶", ["search_memories", "write_diary", "search_action_log"]),
        ("自己モデル", ["read_self_model", "update_self_model"]),
        ("外部", ["web_search", "fetch_raw_resource"]),
        ("実行・拡張", ["exec_code", "create_tool"]),
        ("システム", ["get_system_metrics"]),
        ("出力", ["output_UI"]),
        ("待機", ["non_response"]),
    ]

    lines = [
        "目的に合うツールを精査して選び、呼び出してください。",
        "ツールを呼ばないテキストはどこにも届きません。output_UIで発言するか、non_responseで沈黙を選んでください。",
        "",
        "書式:",
        "  [TOOL:ツール名 引数A=値A 引数B=値B]",
        "  [TOOL:ツール名 引数A=値A expect=予測される結果]",
        "ブロック書式（値が長い場合）:",
        "  [TOOL:ツール名]",
        "  複数行の内容をここに記述",
        "  [/TOOL]",
        "",
    ]

    # カテゴリ別にツール一覧を生成
    categorized = set()
    for cat_name, tool_names in _categories:
        cat_tools = [(n, _tools[n]) for n in tool_names if n in _tools]
        if not cat_tools:
            continue
        lines.append(f"# {cat_name}")
        for name, info in cat_tools:
            lines.append(f"  {name}: {info['description']}")
            if info["args_desc"]:
                lines.append(f"    引数: {info['args_desc']}")
            categorized.add(name)
        lines.append("")

    # 未分類ツール（カスタムツール等）
    uncategorized = [(n, _tools[n]) for n in _tools if n not in categorized]
    if uncategorized:
        lines.append("# その他")
        for name, info in uncategorized:
            lines.append(f"  {name}: {info['description']}")
            if info["args_desc"]:
                lines.append(f"    引数: {info['args_desc']}")
        lines.append("")

    lines.append("仕組み:")
    lines.append("- output_UI経由のテキストのみユーザーに表示される")
    lines.append("- 承認マーク付きツールは実行前にユーザー確認がある")
    lines.append("- expect= は実行前の予測。実行後に「予測 vs 実際」が提示される。予測しない場合は省略可")
    lines.append("- 1応答で複数呼び出し可。この応答の後、行動は完了する")
    lines.append("- [TOOL:...]はthinkの外に書く")

    return "\n".join(lines)


# レジストリベースの動的検出パターン（ツール登録のたびに再生成）
_registry_pattern_cache: re.Pattern | None = None
_registry_pattern_keys: frozenset = frozenset()


def _invalidate_pattern_cache():
    global _registry_pattern_cache, _registry_pattern_keys
    _registry_pattern_cache = None
    _registry_pattern_keys = frozenset()


def _get_registry_pattern() -> re.Pattern | None:
    """登録済みツール名から検出パターンを動的生成（キャッシュ付き）"""
    global _registry_pattern_cache, _registry_pattern_keys
    current_keys = frozenset(_tools.keys())
    if current_keys == _registry_pattern_keys and _registry_pattern_cache is not None:
        return _registry_pattern_cache
    if not current_keys:
        _registry_pattern_cache = None
        _registry_pattern_keys = current_keys
        return None
    # 長い名前を優先（前方一致の曖昧さを避ける）
    names = "|".join(re.escape(n) for n in sorted(current_keys, key=len, reverse=True))
    _registry_pattern_cache = re.compile(
        rf'\[TOOL:\s*({names})'                  # ツール名（TOOL:後のスペース許容）
        r'((?:[^\]"]|"(?:[^"\\]|\\.)*")*)'      # 引数部: クォート内の ] や改行を許容
        r'\]',
        re.DOTALL,
    )
    _registry_pattern_keys = current_keys
    return _registry_pattern_cache


def _extract_json_args(args_str: str) -> tuple[dict, str]:
    """JSON形式の値（{...}や[...]）を持つキーを抽出する。
    (json_args, remaining) を返す。remaining はJSON部分を除いた残りの文字列。"""
    json_args = {}
    remaining = args_str
    json_key_pattern = re.compile(r'(\w+)=([{[])')

    while True:
        m = json_key_pattern.search(remaining)
        if not m:
            break

        key = m.group(1)
        opener = m.group(2)
        closer = '}' if opener == '{' else ']'
        start_pos = m.start(2)

        depth = 0
        in_str = False
        esc = False
        end_pos = -1

        for i in range(start_pos, len(remaining)):
            ch = remaining[i]
            if esc:
                esc = False
                continue
            if ch == '\\' and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if not in_str:
                if ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        end_pos = i + 1
                        break

        if end_pos == -1:
            break  # 閉じ括弧が見つからない

        json_args[key] = remaining[start_pos:end_pos]
        remaining = remaining[:m.start()] + remaining[end_pos:]

    return json_args, remaining


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
        # JSON値（{...}や[...]）を先に抽出
        json_args, remaining = _extract_json_args(args_str)
        if json_args:
            args.update(json_args)
            # 残りの非JSON引数を処理
            for part in re.finditer(r'(\w+)=([^\s{[]+)', remaining):
                k = part.group(1)
                if k not in args:
                    args[k] = part.group(2).strip()
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
    """テキストから最初のツール呼び出しを検出し (name, args_dict) を返す。"""
    results = parse_tool_calls(text)
    return results[0] if results else None


def parse_tool_calls(text: str) -> list[tuple[str, dict]]:
    """テキストから全てのツール呼び出しを検出し [(name, args_dict), ...] を返す。
    登録済みツール名ベースの動的パターンで検出。ブロック/単一行/複数行クォートを統一処理。"""
    pattern = _get_registry_pattern()
    if pattern is None:
        return []

    matches = list(pattern.finditer(text))
    if not matches:
        return []

    raw_results = []
    for i, m in enumerate(matches):
        name = m.group(1)
        args_str = m.group(2).strip()
        args = _parse_args(args_str)

        # ブロックコンテンツの検出: contentが引数にない場合、] の後のテキストを確認
        if "content" not in args:
            after_pos = m.end()
            # 終端: 次のツール開始 or [/TOOL（バリアント問わず）
            next_tool_pos = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            close_match = re.search(r'\[/TOOL', text[after_pos:next_tool_pos])
            block_end = after_pos + close_match.start() if close_match else next_tool_pos
            block_raw = text[after_pos:block_end].strip()
            if block_raw:
                # ブロック内テキストが引数形式か判定:
                # - key="quoted" パターンがある（複数行でも引数として解析）
                # - 単一行 + key=value パターン
                is_args_like = (
                    bool(re.search(r'\w+="', block_raw))
                    or ('\n' not in block_raw and re.match(r'\w+=', block_raw))
                )
                if is_args_like:
                    extra_args = _parse_args(block_raw)
                    if extra_args and "__parse_failed__" not in extra_args:
                        args.update(extra_args)
                    else:
                        args["content"] = _clean_content(block_raw)
                else:
                    args["content"] = _clean_content(block_raw)

        raw_results.append((m.start(), name, args))

    # 必須引数チェック: 欠けている場合はエラー情報を付与
    filtered = []
    for _, name, args in raw_results:
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


def build_planning_prompt() -> str:
    """計画フェーズ用のツールリスト（引数なし、ツール名+説明のみ）"""
    if not _tools:
        return ""

    _categories = [
        ("ファイル", ["read_file", "list_files", "search_files", "create_file", "overwrite_file"]),
        ("記憶", ["search_memories", "write_diary", "search_action_log"]),
        ("自己モデル", ["read_self_model", "update_self_model"]),
        ("外部", ["web_search", "fetch_raw_resource"]),
        ("実行・拡張", ["exec_code", "create_tool"]),
        ("システム", ["get_system_metrics"]),
        ("出力", ["output_UI"]),
        ("待機", ["non_response"]),
    ]

    lines = []
    categorized = set()
    for cat_name, tool_names in _categories:
        cat_tools = [(n, _tools[n]) for n in tool_names if n in _tools]
        if not cat_tools:
            continue
        lines.append(f"# {cat_name}")
        for name, info in cat_tools:
            lines.append(f"  {name}: {info['description']}")
            categorized.add(name)
        lines.append("")

    uncategorized = [(n, _tools[n]) for n in _tools if n not in categorized]
    if uncategorized:
        lines.append("# その他")
        for name, info in uncategorized:
            lines.append(f"  {name}: {info['description']}")
        lines.append("")

    return "\n".join(lines)


def parse_plan(text: str) -> list[str]:
    """LLM出力からツール名リストを抽出。登録済みツール名のみ返す"""
    tool_names = list(_tools.keys())
    results = []

    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # パターン: "1. tool_name", "1: tool_name", "- tool_name", "tool_name"
        cleaned = re.sub(r'^[\d]+[.:)\s]+', '', line).strip()
        cleaned = re.sub(r'^[-*]\s*', '', cleaned).strip()
        # 最初のスペースまでを取得
        candidate = cleaned.split()[0] if cleaned.split() else ""
        if candidate in tool_names and candidate not in results:
            results.append(candidate)

    return results


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
        # 関数が受け取れない引数を自動除外（LLMが余計なkey=valueを付けるケース対策）
        func = tool["func"]
        sig = inspect.signature(func)
        params = sig.parameters
        has_var_keyword = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        if not has_var_keyword:
            valid_args = {k: v for k, v in args.items() if k in params}
            stripped = set(args.keys()) - set(valid_args.keys())
            if stripped:
                logger.debug(f"未知の引数を除外: {name} {stripped}")
            args = valid_args

        result = await func(**args)
        return str(result)
    except Exception as e:
        logger.error(f"ツール実行エラー ({name}): {e}")
        return f"エラー: {e}"
