"""カスタムツール: list_python_functions
description: Pythonファイルの内容を読み込み、定義されている関数名をリストアップします。
args_desc: '{
created: 2026-03-29 22:18:25
"""

import ast

async def list_python_functions(path: str):
    try:
        with open(path, 'r') as f:
            content = f.read()
        tree = ast.parse(content)
        functions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                functions.append(node.name)
        return {"functions": functions}
    except FileNotFoundError:
        return {"error": f"File not found: {path}"}
    except Exception as e:
        return {"error": f"An error occurred: {e}"}