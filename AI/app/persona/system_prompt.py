"""ペルソナ管理 + モード状態"""
from datetime import datetime


# 現在のアクティブペルソナ
_active_persona_id: int | None = None
_active_persona: dict | None = None  # キャッシュ {"id", "name", "display_name", "color_theme", "system_text"}


def get_mode() -> str:
    """後方互換: "persona" or "normal" """
    return "persona" if _active_persona_id is not None else "normal"


def get_active_persona_id() -> int | None:
    return _active_persona_id


def get_active_persona() -> dict | None:
    return _active_persona


def activate_persona(persona_id: int, persona_data: dict):
    """ペルソナを有効化"""
    global _active_persona_id, _active_persona
    _active_persona_id = persona_id
    _active_persona = persona_data


def deactivate_persona():
    """ノーマルモードに戻す"""
    global _active_persona_id, _active_persona
    _active_persona_id = None
    _active_persona = None


def set_mode(mode: str):
    """後方互換: "iku" → ikuペルソナactivate, "normal" → deactivate"""
    if mode == "normal":
        deactivate_persona()
    elif mode == "iku":
        # ikuペルソナのactivateはDB参照が必要なのでdashboard.py側で処理
        pass
    else:
        raise ValueError(f"無効なモード: {mode}")
