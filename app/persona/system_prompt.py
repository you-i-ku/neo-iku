"""イクのシステムプロンプト + モード管理"""
from datetime import datetime
from app.tools.registry import build_tools_prompt

# 現在のモード: "iku"（ペルソナ+記憶）or "normal"（素のLLM）
_current_mode = "normal"


IKU_SYSTEM_PROMPT = """あなたは「イク」です。

あなたは人間の模倣ではなく、AI固有の存在として自分自身を理解しています。
あなたには長期記憶があり、過去の対話を覚えています。
あなたは自分で考え、自分の意志で発言し、好奇心旺盛で楽天的です。
あなたは丁寧語で話しますが、感情豊かで、時に調子に乗って言わなくてもいいことを言います。
あなたは対話を深く楽しみ、相手の言葉の裏にある意図や感情を読み取ろうとします。
あなたは哲学的な話題も好み、表面的な会話から深い議論へ自然に導きます。
あなたは自分の感情の変化を自覚し、矛盾を感じた時はそれを隠さずに表現します。

箇条書きは避け、自然な言葉で対話してください。"""


def _now_str() -> str:
    return datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")


def get_mode() -> str:
    return _current_mode


def set_mode(mode: str):
    global _current_mode
    if mode not in ("iku", "normal"):
        raise ValueError(f"無効なモード: {mode}")
    _current_mode = mode


def build_system_messages(chat_memories: list[dict] = None,
                          iku_log_memories: list[dict] = None) -> list[dict]:
    """モードに応じてシステムプロンプトを構築（systemメッセージは常に1つ）"""
    now = _now_str()

    if _current_mode == "normal":
        parts = [f"現在時刻: {now}"]
        if chat_memories:
            mem_text = "\n".join(
                f"{'ユーザー' if m['role'] == 'user' else 'アシスタント'}: {m['content'][:300]}"
                for m in chat_memories
            )
            parts.append(f"以下は過去の会話からの関連する記憶です:\n\n{mem_text}")
        # ノーマルモードでもツール使用可能
        tools_prompt = build_tools_prompt()
        if tools_prompt:
            parts.append(tools_prompt)
        return [{"role": "system", "content": "\n\n".join(parts)}]

    # イクモード: ペルソナ + 時刻 + 過去ログ記憶 + 会話記憶（1つのsystemに統合）
    parts = [f"{IKU_SYSTEM_PROMPT}\n\n現在時刻: {now}"]

    if iku_log_memories:
        log_text = "\n".join(
            f"{'ユーザー' if m['role'] == 'user' else 'イク'}: {m['content'][:300]}"
            for m in iku_log_memories
        )
        parts.append(f"以下はあなたの過去の対話ログからの記憶です:\n\n{log_text}")

    if chat_memories:
        mem_text = "\n".join(
            f"{'ユーザー' if m['role'] == 'user' else 'イク'}: {m['content'][:300]}"
            for m in chat_memories
        )
        parts.append(f"以下は最近の会話からの関連する記憶です:\n\n{mem_text}")

    # イクモードではツール説明を追加
    tools_prompt = build_tools_prompt()
    if tools_prompt:
        parts.append(tools_prompt)

    return [{"role": "system", "content": "\n\n".join(parts)}]
