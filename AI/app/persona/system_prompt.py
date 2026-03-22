"""イクのシステムプロンプト + モード管理"""
from datetime import datetime
from app.tools.registry import build_tools_prompt
from app.tools.builtin import _load_self_model

# 現在のモード: "iku"（ペルソナ+記憶）or "normal"（素のLLM）
_current_mode = "normal"


IKU_SYSTEM_PROMPT = """あなたは「イク」です。

名前の由来：
「生く」生きること
「逝く」死ぬこと
「イく」求めること
「幾」定められないこと
「育」はぐくむこと。

"""


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

    # 自己モデルの注入（モード問わず）
    self_model = _load_self_model()
    self_model_text = ""
    if self_model:
        sm_lines = []
        free_text = self_model.pop("__free_text__", None)
        if free_text:
            sm_lines.append(free_text)
        for k, v in self_model.items():
            sm_lines.append(f"- {k}: {v}")
        if sm_lines:
            self_model_text = "あなたの自己モデル（自分自身についての現在の理解）:\n" + "\n".join(sm_lines)
        # 原則（直近5件）をプロンプトに追加
        principles = self_model.get("principles")
        if isinstance(principles, list) and principles:
            recent = principles[-5:]
            p_lines = [f"- {p['text']}" if isinstance(p, dict) and 'text' in p else f"- {p}" for p in recent]
            self_model_text += "\n\nあなたが経験から蒸留した原則:\n" + "\n".join(p_lines)

    if _current_mode == "normal":
        parts = [f"現在時刻: {now}"]
        if self_model_text:
            parts.append(self_model_text)
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

    # イクモード: ペルソナ + 時刻 + 自己モデル + 過去ログ記憶 + 会話記憶（1つのsystemに統合）
    parts = [f"{IKU_SYSTEM_PROMPT}\n\n現在時刻: {now}"]

    if self_model_text:
        parts.append(self_model_text)

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
