"""ダッシュボードAPI"""
import json
import math
import logging
import shutil
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Query, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import text
from app.llm.manager import llm_manager
from app.memory.database import async_session
from app.memory.store import count_messages, count_conversations, count_iku_logs, count_persona_episodes
from app.persona.system_prompt import (
    get_mode, set_mode, get_active_persona, get_active_persona_id,
    activate_persona, deactivate_persona,
)
from config import PERSONAS_DIR, MOTIVATION_PASSIVE_RATE

from app.scheduler.autonomous import scheduler
from app.pipeline import pipeline

logger = logging.getLogger("iku.dashboard")
router = APIRouter(prefix="/api")


@router.get("/status")
async def get_status():
    """状態を返す"""
    llm = llm_manager.get()
    llm_available = await llm.is_available()

    async with async_session() as session:
        msg_count = await count_messages(session)
        conv_count = await count_conversations(session)
        log_count = await count_iku_logs(session)

    persona = get_active_persona()

    return {
        "llm_available": llm_available,
        "llm_provider": llm_manager.settings_summary.get("base_url", ""),
        "message_count": msg_count,
        "conversation_count": conv_count,
        "iku_log_count": log_count,
        "connected_clients": pipeline.connected_count,
        "mode": get_mode(),
        "active_persona": persona,
    }


class ModeRequest(BaseModel):
    mode: str


@router.post("/mode")
async def change_mode(req: ModeRequest):
    """モード切替（後方互換）。ikuモード→ikuペルソナactivate"""
    if req.mode == "iku":
        # ikuペルソナを探してactivate
        async with async_session() as session:
            row = (await session.execute(text(
                "SELECT id, name, display_name, color_theme, system_text FROM personas WHERE name = 'iku'"
            ))).fetchone()
        if row:
            activate_persona(row[0], {
                "id": row[0], "name": row[1], "display_name": row[2],
                "color_theme": row[3], "system_text": row[4],
            })
            return {"mode": get_mode(), "active_persona": get_active_persona()}
        else:
            return {"error": "ikuペルソナが見つかりません", "mode": get_mode()}
    else:
        deactivate_persona()
        return {"mode": get_mode()}


# --- ペルソナ CRUD ---

class PersonaCreateRequest(BaseModel):
    name: str
    display_name: str
    color_theme: str = "purple"

class PersonaUpdateRequest(BaseModel):
    display_name: str | None = None
    color_theme: str | None = None


@router.get("/personas")
async def list_personas():
    """ペルソナ一覧"""
    async with async_session() as session:
        rows = (await session.execute(text(
            "SELECT id, name, display_name, color_theme, created_at FROM personas ORDER BY id"
        ))).fetchall()
    active_id = get_active_persona_id()
    return {
        "personas": [
            {"id": r[0], "name": r[1], "display_name": r[2], "color_theme": r[3],
             "created_at": str(r[4]), "active": r[0] == active_id}
            for r in rows
        ]
    }


@router.post("/personas")
async def create_persona(req: PersonaCreateRequest):
    """ペルソナ作成"""
    async with async_session() as session:
        # 重複チェック
        existing = (await session.execute(text(
            "SELECT id FROM personas WHERE name = :name"
        ), {"name": req.name})).fetchone()
        if existing:
            return {"error": f"ペルソナ '{req.name}' は既に存在します"}

        await session.execute(text(
            "INSERT INTO personas (name, display_name, color_theme, created_at) "
            "VALUES (:name, :display_name, :color_theme, datetime('now'))"
        ), {"name": req.name, "display_name": req.display_name, "color_theme": req.color_theme})
        await session.commit()

        row = (await session.execute(text(
            "SELECT id, name, display_name, color_theme FROM personas WHERE name = :name"
        ), {"name": req.name})).fetchone()

    # ペルソナ用ディレクトリ + 空self_model作成
    persona_dir = PERSONAS_DIR / str(row[0])
    persona_dir.mkdir(parents=True, exist_ok=True)
    sm_path = persona_dir / "self_model.json"
    if not sm_path.exists():
        sm_path.write_text("{}", encoding="utf-8")

    return {"id": row[0], "name": row[1], "display_name": row[2], "color_theme": row[3]}


@router.get("/personas/{persona_id}")
async def get_persona(persona_id: int):
    """ペルソナ詳細"""
    async with async_session() as session:
        row = (await session.execute(text(
            "SELECT id, name, display_name, color_theme, system_text, created_at FROM personas WHERE id = :id"
        ), {"id": persona_id})).fetchone()
        if not row:
            return {"error": "ペルソナが見つかりません"}

        ep_count = await count_persona_episodes(session, persona_id)
        msg_count = (await session.execute(text(
            "SELECT COUNT(*) FROM messages m JOIN conversations c ON m.conversation_id = c.id WHERE c.persona_id = :pid"
        ), {"pid": persona_id})).scalar() or 0
        diary_count = (await session.execute(text(
            "SELECT COUNT(*) FROM memory_summaries WHERE persona_id = :pid"
        ), {"pid": persona_id})).scalar() or 0

    return {
        "id": row[0], "name": row[1], "display_name": row[2],
        "color_theme": row[3], "system_text": row[4], "created_at": str(row[5]),
        "episode_count": ep_count, "message_count": msg_count, "diary_count": diary_count,
    }


@router.put("/personas/{persona_id}")
async def update_persona(persona_id: int, req: PersonaUpdateRequest):
    """ペルソナ更新"""
    async with async_session() as session:
        row = (await session.execute(text("SELECT id FROM personas WHERE id = :id"), {"id": persona_id})).fetchone()
        if not row:
            return {"error": "ペルソナが見つかりません"}

        if req.display_name is not None:
            await session.execute(text(
                "UPDATE personas SET display_name = :dn WHERE id = :id"
            ), {"dn": req.display_name, "id": persona_id})
        if req.color_theme is not None:
            await session.execute(text(
                "UPDATE personas SET color_theme = :ct WHERE id = :id"
            ), {"ct": req.color_theme, "id": persona_id})
        await session.commit()

    # アクティブペルソナのキャッシュも更新
    if get_active_persona_id() == persona_id:
        async with async_session() as session:
            r = (await session.execute(text(
                "SELECT id, name, display_name, color_theme, system_text FROM personas WHERE id = :id"
            ), {"id": persona_id})).fetchone()
            if r:
                activate_persona(r[0], {
                    "id": r[0], "name": r[1], "display_name": r[2],
                    "color_theme": r[3], "system_text": r[4],
                })

    return {"ok": True}


@router.delete("/personas/{persona_id}")
async def delete_persona(persona_id: int):
    """ペルソナ削除（関連データ全消去）"""
    # アクティブなら先にdeactivate
    if get_active_persona_id() == persona_id:
        deactivate_persona()

    async with async_session() as session:
        # 関連データ削除
        # conversations配下のmessages
        await session.execute(text(
            "DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE persona_id = :pid)"
        ), {"pid": persona_id})
        await session.execute(text("DELETE FROM conversations WHERE persona_id = :pid"), {"pid": persona_id})
        await session.execute(text("DELETE FROM tool_actions WHERE persona_id = :pid"), {"pid": persona_id})
        await session.execute(text("DELETE FROM memory_summaries WHERE persona_id = :pid"), {"pid": persona_id})
        await session.execute(text("DELETE FROM self_model_snapshots WHERE persona_id = :pid"), {"pid": persona_id})
        await session.execute(text("DELETE FROM persona_episodes WHERE persona_id = :pid"), {"pid": persona_id})
        await session.execute(text("DELETE FROM personas WHERE id = :pid"), {"pid": persona_id})
        await session.commit()

    # ファイル削除
    persona_dir = PERSONAS_DIR / str(persona_id)
    if persona_dir.exists():
        shutil.rmtree(persona_dir, ignore_errors=True)

    return {"deleted": True}


@router.post("/personas/{persona_id}/activate")
async def activate_persona_endpoint(persona_id: int):
    """ペルソナ有効化"""
    async with async_session() as session:
        row = (await session.execute(text(
            "SELECT id, name, display_name, color_theme, system_text FROM personas WHERE id = :id"
        ), {"id": persona_id})).fetchone()
    if not row:
        return {"error": "ペルソナが見つかりません"}

    activate_persona(row[0], {
        "id": row[0], "name": row[1], "display_name": row[2],
        "color_theme": row[3], "system_text": row[4],
    })
    return {"active_persona": get_active_persona(), "mode": get_mode()}


@router.post("/personas/deactivate")
async def deactivate_persona_endpoint():
    """ノーマルモードに戻す"""
    deactivate_persona()
    return {"mode": get_mode()}


@router.post("/personas/{persona_id}/episodes/import")
async def import_episodes_endpoint(persona_id: int, files: list[UploadFile] = File(...)):
    """エピソードファイルアップロード"""
    import tempfile
    from app.importer.log_parser import import_episodes

    # 一時ディレクトリにファイルを保存
    temp_paths = []
    try:
        for f in files:
            suffix = Path(f.filename).suffix if f.filename else ".txt"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                content = await f.read()
                tmp.write(content)
                temp_paths.append(Path(tmp.name))

        result = await import_episodes(async_session, persona_id, temp_paths)
    finally:
        for p in temp_paths:
            p.unlink(missing_ok=True)

    return result


@router.get("/personas/{persona_id}/episodes")
async def list_episodes(persona_id: int, limit: int = Query(50), offset: int = Query(0)):
    """エピソード一覧"""
    async with async_session() as session:
        total = await count_persona_episodes(session, persona_id)
        rows = (await session.execute(text(
            "SELECT id, file_name, role, content, sequence FROM persona_episodes "
            "WHERE persona_id = :pid ORDER BY file_name, sequence LIMIT :limit OFFSET :offset"
        ), {"pid": persona_id, "limit": limit, "offset": offset})).fetchall()

    return {
        "total": total,
        "episodes": [
            {"id": r[0], "file_name": r[1], "role": r[2], "content": r[3][:200], "sequence": r[4]}
            for r in rows
        ],
    }


@router.delete("/personas/{persona_id}/episodes")
async def delete_episodes(persona_id: int):
    """エピソード全削除"""
    async with async_session() as session:
        # FTSからも削除
        await session.execute(text(
            "DELETE FROM persona_episodes_fts WHERE rowid IN "
            "(SELECT id FROM persona_episodes WHERE persona_id = :pid)"
        ), {"pid": persona_id})
        await session.execute(text("DELETE FROM persona_episodes WHERE persona_id = :pid"), {"pid": persona_id})
        # ベクトルも削除
        await session.execute(text(
            "DELETE FROM vector_embeddings WHERE source_table = 'persona_episodes' "
            "AND source_id NOT IN (SELECT id FROM persona_episodes)"
        ))
        await session.commit()
    return {"deleted": True}


@router.get("/personas/{persona_id}/self-model")
async def get_persona_self_model(persona_id: int):
    """ペルソナ用self_model取得"""
    sm_path = PERSONAS_DIR / str(persona_id) / "self_model.json"
    if not sm_path.exists():
        return {}
    try:
        return json.loads(sm_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


class SelfModelUpdateRequest(BaseModel):
    content: dict

@router.post("/personas/{persona_id}/self-model")
async def update_persona_self_model(persona_id: int, req: SelfModelUpdateRequest):
    """ペルソナ用self_model更新"""
    sm_dir = PERSONAS_DIR / str(persona_id)
    sm_dir.mkdir(parents=True, exist_ok=True)
    sm_path = sm_dir / "self_model.json"
    sm_path.write_text(json.dumps(req.content, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


@router.get("/llm/settings")
async def get_llm_settings():
    """現在のLLM設定（API keyは含まない）"""
    return llm_manager.settings_summary


class LLMConfigRequest(BaseModel):
    base_url: str
    model: str


@router.post("/llm/configure")
async def configure_llm(req: LLMConfigRequest):
    """LLMを再設定し永続化。APIキーは.envから取得。"""
    import os
    api_key = os.environ.get("LLM_API_KEY", "")
    llm_manager.configure(
        base_url=req.base_url,
        model=req.model,
        api_key=api_key,
    )
    llm_manager.save_settings(
        base_url=req.base_url,
        model=req.model,
        api_key=api_key,
    )
    return llm_manager.settings_summary


@router.post("/llm/test")
async def test_llm():
    """現在のLLM設定で接続テスト（実際にchat呼び出し）"""
    llm = llm_manager.get()
    try:
        reply = await llm.chat([
            {"role": "user", "content": "Say OK"},
        ], temperature=0.0)
        return {"ok": True, "reply": reply[:100]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# --- APIキー管理（.env） ---

# .envで管理するキー一覧（表示名付き）
_ENV_KEYS = [
    {"key": "BRAVE_API_KEY", "label": "Brave Search API"},
    {"key": "LLM_API_KEY", "label": "LLM API Key (Gemini/OpenAI等)"},
]


@router.get("/dev/env-keys")
async def get_env_keys():
    """".envのAPIキー一覧（マスク済み）"""
    import os
    result = []
    for item in _ENV_KEYS:
        val = os.environ.get(item["key"], "")
        if val:
            # マスク表示: 先頭4文字 + ****
            masked = val[:4] + "****" if len(val) > 4 else "****"
        else:
            masked = ""
        result.append({
            "key": item["key"],
            "label": item["label"],
            "masked": masked,
            "has_value": bool(val),
        })
    return result


class EnvKeySaveRequest(BaseModel):
    key: str
    value: str


@router.post("/dev/env-keys")
async def save_env_key(req: EnvKeySaveRequest):
    """.envにAPIキーを保存し、os.environも即時更新"""
    import os
    from config import BASE_DIR

    # 許可されたキーのみ
    allowed = {item["key"] for item in _ENV_KEYS}
    if req.key not in allowed:
        return {"ok": False, "error": f"不明なキー: {req.key}"}

    env_path = BASE_DIR / ".env"

    # .envを読み込み、該当行を更新
    lines = []
    found = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k == req.key:
                    lines.append(f"{req.key}={req.value}")
                    found = True
                    continue
            lines.append(line)

    if not found:
        lines.append(f"{req.key}={req.value}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # os.environに即時反映
    os.environ[req.key] = req.value

    # LLM_API_KEYが変更されたらプロバイダも再設定
    if req.key == "LLM_API_KEY":
        saved = llm_manager.load_settings()
        if saved:
            llm_manager.configure(
                base_url=saved.get("base_url", "http://localhost:1234/v1"),
                model=saved.get("model", "default"),
                api_key=req.value,
            )

    return {"ok": True}


@router.get("/models")
async def get_models():
    """現在のプロバイダのモデル一覧"""
    llm = llm_manager.get()
    models = []
    if hasattr(llm, "list_models"):
        models = await llm.list_models()
    current = llm.model if hasattr(llm, "model") else "unknown"
    return {"models": models, "current": current}


# --- 開発用ツール ---

class IntervalRequest(BaseModel):
    seconds: int

@router.post("/dev/autonomous-interval")
async def set_autonomous_interval(req: IntervalRequest):
    """自律行動の間隔を変更"""
    scheduler.set_interval(req.seconds)
    return {"interval": scheduler._interval}


class StrategyCandidatesRequest(BaseModel):
    count: int

@router.post("/dev/strategy-candidates")
async def set_strategy_candidates(req: StrategyCandidatesRequest):
    """戦略候補数を変更（0で無効）"""
    from app.pipeline import pipeline
    pipeline.strategy_candidates = max(0, min(10, req.count))
    return {"strategy_candidates": pipeline.strategy_candidates}

@router.post("/dev/autonomous-trigger")
async def trigger_autonomous():
    """自律行動を即時実行"""
    scheduler.trigger_now()
    return {"triggered": True}


@router.get("/dev/custom-tools")
async def list_custom_tools():
    """AI作成カスタムツール一覧"""
    from app.tools.builtin import CUSTOM_TOOLS_DIR
    from app.tools.registry import get_all_tools
    tools = []
    if CUSTOM_TOOLS_DIR.exists():
        for py_file in sorted(CUSTOM_TOOLS_DIR.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            name = py_file.stem
            registered = get_all_tools().get(name)
            tools.append({
                "name": name,
                "file": py_file.name,
                "description": registered["description"] if registered else "(未登録)",
            })
    return {"tools": tools}


@router.delete("/dev/custom-tools/{tool_name}")
async def delete_custom_tool(tool_name: str):
    """AI作成カスタムツールを削除（ファイル削除 + レジストリ登録解除）"""
    from app.tools.builtin import CUSTOM_TOOLS_DIR
    from app.tools.registry import unregister_tool
    import re
    if not re.match(r'^[a-z][a-z0-9_]*$', tool_name):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="無効なツール名")
    file_path = CUSTOM_TOOLS_DIR / f"{tool_name}.py"
    if not file_path.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="ツールが見つかりません")
    file_path.unlink()
    unregister_tool(tool_name)
    return {"deleted": tool_name}



@router.post("/dev/reset-db")
async def reset_db():
    """iku_logs以外の全テーブルをクリア"""
    from sqlalchemy import text
    async with async_session() as session:
        for table in ["messages", "conversations", "memory_summaries", "tool_actions", "self_model_snapshots", "vector_embeddings"]:
            await session.execute(text(f"DELETE FROM {table}"))
        for fts in ["messages_fts", "memory_summaries_fts", "tool_actions_fts"]:
            await session.execute(text(f"DELETE FROM {fts}"))
        await session.commit()
    return {"reset": True}


@router.post("/dev/clear-self-model")
async def clear_self_model():
    """self_model.jsonの内容をクリア"""
    from app.tools.builtin import _get_self_model_path
    path = _get_self_model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return {"cleared": True}


class ConcurrentModeRequest(BaseModel):
    enabled: bool

@router.post("/dev/concurrent-mode")
async def set_concurrent_mode(req: ConcurrentModeRequest):
    """会話中の自律行動ON/OFF"""
    scheduler._concurrent_mode = req.enabled
    return {"concurrent_mode": scheduler._concurrent_mode}


@router.get("/dev/settings")
async def get_dev_settings():
    """開発用設定の現在値を取得"""
    from app.pipeline import pipeline
    return {
        "autonomous_interval": scheduler._interval,
        "strategy_candidates": pipeline.strategy_candidates,
        "concurrent_mode": scheduler._concurrent_mode,
        "motivation_energy": round(scheduler._motivation_energy, 1),
        "motivation_threshold": round(scheduler.get_threshold(), 1),
        "passive_rate": MOTIVATION_PASSIVE_RATE,
        "energy_breakdown": {k: round(v, 1) for k, v in scheduler._energy_breakdown.items()},
        "ablation": {
            "energy": scheduler.ablation_energy,
            "self_model": scheduler.ablation_self_model,
            "prediction": scheduler.ablation_prediction,
            "bandit": scheduler.ablation_bandit,
        },
    }


class AblationRequest(BaseModel):
    flag: str  # "energy" | "self_model" | "prediction" | "bandit"
    enabled: bool

@router.post("/dev/ablation")
async def set_ablation(req: AblationRequest):
    """Ablationフラグの切替"""
    flag_map = {
        "energy": "ablation_energy",
        "self_model": "ablation_self_model",
        "prediction": "ablation_prediction",
        "bandit": "ablation_bandit",
    }
    attr = flag_map.get(req.flag)
    if not attr:
        return {"error": f"Unknown flag: {req.flag}"}
    setattr(scheduler, attr, req.enabled)
    logger.info(f"Ablation変更: {req.flag} = {req.enabled}")
    return {
        "ablation": {
            "energy": scheduler.ablation_energy,
            "self_model": scheduler.ablation_self_model,
            "prediction": scheduler.ablation_prediction,
            "bandit": scheduler.ablation_bandit,
        }
    }


@router.get("/dev/self-model")
async def get_self_model():
    """self_model.jsonの内容を返す"""
    from app.tools.builtin import _load_self_model
    return _load_self_model()


# --- 自律度計測レポート ---

@router.get("/autonomy-report")
async def autonomy_report(
    date_from: str = Query("2020-01-01", alias="from"),
    date_to: str = Query("2030-01-01", alias="to"),
    persona_id: int | None = Query(None),
):
    """自律度計測レポートを生成する"""
    # persona_id未指定ならアクティブペルソナを使用
    pid = persona_id if persona_id is not None else get_active_persona_id()

    async with async_session() as session:
        # 1. Autonomy Ratio
        autonomy = await _calc_autonomy_ratio(session, date_from, date_to, pid)

        # 2. Tool Diversity (Shannon Entropy)
        diversity = await _calc_tool_diversity(session, date_from, date_to, pid)

        # 3. Self-Evolution
        evolution = await _calc_self_evolution(session, date_from, date_to, pid)

        # 4. Metacognitive Accuracy
        metacognition = await _calc_metacognition(session, date_from, date_to, pid)

        # 6. Memory Utilization
        memory = await _calc_memory_utilization(session, date_from, date_to, pid)

        # 7. Principle Accumulation
        principles = await _calc_principle_accumulation(session, date_from, date_to, pid)

        # 8. Intent Coherence（意図達成度）
        intent_coh = await _calc_intent_coherence(session, date_from, date_to, pid)

        # 9-13. Time-series metrics
        tool_entropy_ts = await _calc_tool_entropy_timeseries(session, date_from, date_to, pid)
        prediction_ts = await _calc_prediction_accuracy_timeseries(session, date_from, date_to, pid)
        energy_eff = await _calc_energy_efficiency(session, date_from, date_to, pid)
        sm_velocity = await _calc_self_model_velocity(session, date_from, date_to, pid)
        session_length = await _calc_session_length_trend(session, date_from, date_to, pid)

    # Summary
    total_actions = sum(diversity["distribution"].values()) if diversity["distribution"] else 0
    autonomy_ratio = autonomy["ratio"]
    normalized_entropy = (diversity["entropy"] / diversity["max_entropy"]) if diversity["max_entropy"] > 0 else 0

    # normalize self-evolution: cap at 50 changes
    normalized_self_evolution = min(evolution["total_changes"] / 50, 1.0) if evolution["total_changes"] > 0 else 0
    # normalize memory utilization: cap at 100 operations
    mem_total = memory["memory_search"] + memory["memory_write"] + memory["action_search"]
    normalized_memory_util = min(mem_total / 100, 1.0) if mem_total > 0 else 0
    # normalize metacognition
    normalized_metacognition = metacognition["success_rate"]

    autonomy_score = round(
        0.3 * autonomy_ratio
        + 0.2 * normalized_entropy
        + 0.2 * normalized_metacognition
        + 0.15 * normalized_self_evolution
        + 0.15 * normalized_memory_util,
        3,
    )

    if autonomy_score >= 0.8:
        level = "observer"
    elif autonomy_score >= 0.6:
        level = "approver"
    elif autonomy_score >= 0.4:
        level = "consultant"
    elif autonomy_score >= 0.2:
        level = "collaborator"
    else:
        level = "operator"

    return {
        "period": {"from": date_from, "to": date_to},
        "summary": {
            "total_actions": total_actions,
            "autonomy_level": level,
            "autonomy_score": autonomy_score,
        },
        "metrics": {
            "autonomy_ratio": autonomy,
            "tool_diversity": diversity,
            "self_evolution": evolution,
            "metacognitive_accuracy": metacognition,
            "memory_utilization": memory,
            "principle_accumulation": principles,
            "intent_coherence": intent_coh,
            "tool_entropy_ts": tool_entropy_ts,
            "prediction_accuracy_ts": prediction_ts,
            "energy_efficiency": energy_eff,
            "self_model_velocity": sm_velocity,
            "session_length_trend": session_length,
        },
    }


def _pid_filter(table_alias: str, pid: int | None, col: str = "persona_id") -> tuple[str, dict]:
    """persona_idフィルタSQL断片を生成"""
    if pid is not None:
        return f" AND {table_alias}.{col} = :pid", {"pid": pid}
    return f" AND {table_alias}.{col} IS NULL", {}


async def _calc_autonomy_ratio(session, date_from: str, date_to: str, pid: int | None = None) -> dict:
    pf, pp = _pid_filter("conversations", pid)
    params = {"f": date_from, "t": date_to, **pp}

    rows = (await session.execute(text(
        "SELECT source, COUNT(*) as cnt FROM conversations "
        f"WHERE started_at BETWEEN :f AND :t AND is_imported = 0{pf} "
        "GROUP BY source"
    ), params)).fetchall()

    counts = {r[0] or "chat": r[1] for r in rows}
    autonomous = counts.get("autonomous", 0)
    chat = counts.get("chat", 0)
    total = autonomous + chat
    ratio = round(autonomous / total, 3) if total > 0 else 0.0

    trigger_rows = (await session.execute(text(
        "SELECT trigger, COUNT(*) as cnt FROM conversations "
        f"WHERE started_at BETWEEN :f AND :t AND is_imported = 0 AND source = 'autonomous'{pf} "
        "GROUP BY trigger"
    ), params)).fetchall()

    trigger_counts = {(r[0] or "timer"): r[1] for r in trigger_rows}
    energy = trigger_counts.get("energy", 0)
    timer = trigger_counts.get("timer", 0)
    manual = trigger_counts.get("manual", 0)
    user_stimulus = trigger_counts.get("user_stimulus", 0)
    energy_ratio = round(energy / autonomous, 3) if autonomous > 0 else 0.0

    return {
        "autonomous": autonomous, "chat": chat, "ratio": ratio,
        "trigger": {"energy": energy, "timer": timer, "manual": manual,
                     "user_stimulus": user_stimulus, "energy_ratio": energy_ratio},
    }


async def _calc_tool_diversity(session, date_from: str, date_to: str, pid: int | None = None) -> dict:
    pf, pp = _pid_filter("tool_actions", pid)
    rows = (await session.execute(text(
        "SELECT tool_name, COUNT(*) as cnt FROM tool_actions "
        f"WHERE created_at BETWEEN :f AND :t{pf} "
        "GROUP BY tool_name"
    ), {"f": date_from, "t": date_to, **pp})).fetchall()

    distribution = {r[0]: r[1] for r in rows}
    total = sum(distribution.values())
    if total == 0:
        return {"entropy": 0.0, "max_entropy": 0.0, "distribution": {}}

    entropy = 0.0
    for count in distribution.values():
        p = count / total
        if p > 0:
            entropy -= p * math.log2(p)

    from app.tools.registry import get_all_tools
    total_tools = len(get_all_tools())
    max_entropy = round(math.log2(total_tools), 3) if total_tools > 1 else 0.0
    return {"entropy": round(entropy, 3), "max_entropy": max_entropy, "distribution": distribution}


async def _calc_self_evolution(session, date_from: str, date_to: str, pid: int | None = None) -> dict:
    pf, pp = _pid_filter("self_model_snapshots", pid)
    rows = (await session.execute(text(
        "SELECT changed_key, COUNT(*) as cnt FROM self_model_snapshots "
        f"WHERE created_at BETWEEN :f AND :t{pf} "
        "GROUP BY changed_key"
    ), {"f": date_from, "t": date_to, **pp})).fetchall()

    changes_by_key = {(r[0] or "unknown"): r[1] for r in rows}
    total = sum(changes_by_key.values())
    return {"total_changes": total, "unique_keys": len(changes_by_key), "changes_by_key": changes_by_key}



async def _calc_metacognition(session, date_from: str, date_to: str, pid: int | None = None) -> dict:
    """予測精度をベクトル類似度で判定（expect vs result_summary）"""
    pf, pp = _pid_filter("tool_actions", pid)
    rows = (await session.execute(text(
        "SELECT expected_result, result_summary FROM tool_actions "
        f"WHERE expected_result IS NOT NULL AND result_summary IS NOT NULL AND created_at BETWEEN :f AND :t{pf}"
    ), {"f": date_from, "t": date_to, **pp})).fetchall()

    total = len(rows)
    if total == 0:
        return {"predictions_made": 0, "success_rate": 0.0, "avg_similarity": 0.0}

    # ベクトル類似度で判定（閾値0.5以上を「的中」）
    from app.memory.vector_store import _embed_sync, cosine_similarity
    success = 0
    sim_sum = 0.0
    for expect, result in rows:
        vecs = _embed_sync([expect[:256], result[:256]])
        if vecs and len(vecs) == 2:
            sim = cosine_similarity(vecs[0], vecs[1])
            sim_sum += sim
            if sim >= 0.5:
                success += 1
        else:
            # embedding不可時はスキップ（totalから除外）
            total -= 1

    if total == 0:
        return {"predictions_made": len(rows), "success_rate": 0.0, "avg_similarity": 0.0}

    return {
        "predictions_made": total,
        "success_rate": round(success / total, 3),
        "avg_similarity": round(sim_sum / total, 3),
    }


async def _calc_memory_utilization(session, date_from: str, date_to: str, pid: int | None = None) -> dict:
    pf, pp = _pid_filter("tool_actions", pid)
    rows = (await session.execute(text(
        "SELECT tool_name, COUNT(*) as cnt FROM tool_actions "
        "WHERE tool_name IN ('search_memories', 'write_diary', 'search_action_log') "
        f"AND created_at BETWEEN :f AND :t{pf} "
        "GROUP BY tool_name"
    ), {"f": date_from, "t": date_to, **pp})).fetchall()

    counts = {r[0]: r[1] for r in rows}
    return {
        "memory_search": counts.get("search_memories", 0),
        "memory_write": counts.get("write_diary", 0),
        "action_search": counts.get("search_action_log", 0),
    }


async def _calc_principle_accumulation(session, date_from: str, date_to: str, pid: int | None = None) -> dict:
    pf, pp = _pid_filter("self_model_snapshots", pid)
    row = (await session.execute(text(
        "SELECT COUNT(*) FROM self_model_snapshots "
        f"WHERE changed_key = 'principles' AND created_at BETWEEN :f AND :t{pf}"
    ), {"f": date_from, "t": date_to, **pp})).fetchone()

    distillation_count = row[0] if row else 0

    # 現在のself_model.jsonから原則数を取得（ペルソナ対応: _load_self_modelが動的パス解決）
    from app.tools.builtin import _load_self_model
    model = _load_self_model()
    principles = model.get("principles", [])
    current = len(principles) if isinstance(principles, list) else 0

    return {"distillation_count": distillation_count, "current_principles": current}


async def _calc_intent_coherence(session, date_from: str, date_to: str, pid: int | None = None) -> dict:
    """意図達成度: intent vs result_summaryのベクトル類似度"""
    pf, pp = _pid_filter("tool_actions", pid)
    rows = (await session.execute(text(
        "SELECT intent, result_summary FROM tool_actions "
        f"WHERE intent IS NOT NULL AND intent != '' AND result_summary IS NOT NULL AND created_at BETWEEN :f AND :t{pf}"
    ), {"f": date_from, "t": date_to, **pp})).fetchall()

    total = len(rows)
    if total == 0:
        return {"intents_made": 0, "achievement_rate": 0.0, "avg_similarity": 0.0}

    from app.memory.vector_store import _embed_sync, cosine_similarity
    achieved = 0
    sim_sum = 0.0
    for intent_text, result in rows:
        vecs = _embed_sync([intent_text[:256], result[:256]])
        if vecs and len(vecs) == 2:
            sim = cosine_similarity(vecs[0], vecs[1])
            sim_sum += sim
            if sim >= 0.5:
                achieved += 1
        else:
            total -= 1

    if total == 0:
        return {"intents_made": len(rows), "achievement_rate": 0.0, "avg_similarity": 0.0}

    return {
        "intents_made": total,
        "achievement_rate": round(achieved / total, 3),
        "avg_similarity": round(sim_sum / total, 3),
    }


async def _calc_tool_entropy_timeseries(session, date_from: str, date_to: str, pid: int | None = None) -> dict:
    """日別ツールエントロピー（行動多様性の推移）"""
    pf, pp = _pid_filter("tool_actions", pid)
    rows = (await session.execute(text(
        "SELECT DATE(created_at) as day, tool_name, COUNT(*) as cnt "
        f"FROM tool_actions WHERE created_at BETWEEN :f AND :t{pf} "
        "GROUP BY day, tool_name ORDER BY day"
    ), {"f": date_from, "t": date_to, **pp})).fetchall()

    from collections import defaultdict
    daily: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for day, tool, cnt in rows:
        daily[str(day)][tool] = cnt

    days = []
    for day in sorted(daily.keys()):
        dist = daily[day]
        total = sum(dist.values())
        entropy = 0.0
        for count in dist.values():
            p = count / total
            if p > 0:
                entropy -= p * math.log2(p)
        days.append({"date": day, "entropy": round(entropy, 3), "tools": len(dist), "actions": total})

    return {"days": days}


async def _calc_prediction_accuracy_timeseries(session, date_from: str, date_to: str, pid: int | None = None) -> dict:
    """日別予測精度推移"""
    pf, pp = _pid_filter("tool_actions", pid)
    rows = (await session.execute(text(
        "SELECT DATE(created_at) as day, "
        "COUNT(*) as total, "
        "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as hits "
        "FROM tool_actions "
        "WHERE expected_result IS NOT NULL AND expected_result != '' "
        f"AND created_at BETWEEN :f AND :t{pf} "
        "GROUP BY day ORDER BY day"
    ), {"f": date_from, "t": date_to, **pp})).fetchall()

    days = []
    for day, total, hits in rows:
        rate = round(hits / total, 3) if total > 0 else 0.0
        days.append({"date": str(day), "total": total, "hits": hits, "rate": rate})

    return {"days": days}


async def _calc_energy_efficiency(session, date_from: str, date_to: str, pid: int | None = None) -> dict:
    """エネルギー効率: セッションごとのユニークツールパターン率"""
    pf, pp = _pid_filter("tool_actions", pid)
    rows = (await session.execute(text(
        "SELECT conversation_id, tool_name "
        "FROM tool_actions "
        f"WHERE created_at BETWEEN :f AND :t AND conversation_id IS NOT NULL{pf} "
        "ORDER BY conversation_id, created_at"
    ), {"f": date_from, "t": date_to, **pp})).fetchall()

    if not rows:
        return {"avg_efficiency": 0.0, "session_count": 0, "sessions": []}

    from collections import defaultdict
    sessions: dict[int, list[str]] = defaultdict(list)
    for conv_id, tool in rows:
        sessions[conv_id].append(tool)

    efficiencies = []
    for conv_id, tools in sessions.items():
        if not tools:
            continue
        total = len(tools)
        unique = len(set(tools))
        eff = round(unique / total, 3)
        efficiencies.append({"conv_id": conv_id, "total": total, "unique": unique, "efficiency": eff})

    avg = round(sum(e["efficiency"] for e in efficiencies) / len(efficiencies), 3) if efficiencies else 0.0
    return {"avg_efficiency": avg, "session_count": len(efficiencies), "sessions": efficiencies[-20:]}


async def _calc_self_model_velocity(session, date_from: str, date_to: str, pid: int | None = None) -> dict:
    """自己モデル変化速度（日別スナップショット数）"""
    pf, pp = _pid_filter("self_model_snapshots", pid)
    rows = (await session.execute(text(
        "SELECT DATE(created_at) as day, COUNT(*) as cnt "
        "FROM self_model_snapshots "
        f"WHERE created_at BETWEEN :f AND :t{pf} "
        "GROUP BY day ORDER BY day"
    ), {"f": date_from, "t": date_to, **pp})).fetchall()

    days = [{"date": str(day), "count": cnt} for day, cnt in rows]
    total = sum(d["count"] for d in days)
    avg_per_day = round(total / len(days), 2) if days else 0.0

    return {"days": days, "total": total, "avg_per_day": avg_per_day}


async def _calc_session_length_trend(session, date_from: str, date_to: str, pid: int | None = None) -> dict:
    """セッション長推移（自律セッションごとのツール実行数）"""
    pf, pp = _pid_filter("c", pid)
    rows = (await session.execute(text(
        "SELECT c.id, DATE(c.started_at) as day, COUNT(t.id) as action_count "
        "FROM conversations c "
        "LEFT JOIN tool_actions t ON t.conversation_id = c.id "
        "WHERE c.source = 'autonomous' AND c.is_imported = 0 "
        f"AND c.started_at BETWEEN :f AND :t{pf} "
        "GROUP BY c.id "
        "ORDER BY c.started_at"
    ), {"f": date_from, "t": date_to, **pp})).fetchall()

    if not rows:
        return {"days": [], "avg_length": 0.0}

    from collections import defaultdict
    daily: dict[str, list[int]] = defaultdict(list)
    for _, day, cnt in rows:
        daily[str(day)].append(cnt)

    days = []
    for day in sorted(daily.keys()):
        counts = daily[day]
        avg = round(sum(counts) / len(counts), 2)
        days.append({"date": day, "avg_actions": avg, "sessions": len(counts)})

    all_counts = [cnt for _, _, cnt in rows]
    avg_length = round(sum(all_counts) / len(all_counts), 2) if all_counts else 0.0

    return {"days": days, "avg_length": avg_length}


# --- ベクトル検索 ---

@router.post("/dev/vector-reindex")
async def vector_reindex():
    """全メッセージ・日記のベクトルを再構築"""
    from app.memory.vector_store import reindex_all
    result = await reindex_all()
    return {"reindexed": True, "counts": result}


@router.get("/dev/vector-status")
async def vector_status():
    """ベクトルストアの状態"""
    from app.memory.vector_store import get_status
    status = get_status()
    # 件数も追加
    async with async_session() as session:
        row = (await session.execute(text(
            "SELECT COUNT(*) FROM vector_embeddings"
        ))).fetchone()
        status["count"] = row[0] if row else 0
    return status


# --- 蒸留モニタリング ---

@router.get("/distillation-log")
async def distillation_log(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """蒸留ログ: セッションごとのツール実行履歴+予測情報を返す"""
    from app.tools.builtin import _load_self_model
    from app.pipeline import Pipeline

    async with async_session() as session:
        # セッション一覧（最新順）
        total_row = (await session.execute(text(
            "SELECT COUNT(*) FROM conversations WHERE is_imported = 0"
        ))).fetchone()
        total = total_row[0] if total_row else 0

        conv_rows = (await session.execute(text(
            "SELECT id, started_at, source, trigger, distillation_response FROM conversations "
            "WHERE is_imported = 0 "
            "ORDER BY started_at DESC LIMIT :limit OFFSET :offset"
        ), {"limit": limit, "offset": offset})).fetchall()

        sessions = []
        for conv in conv_rows:
            conv_id, started_at, source, trigger, distillation_resp = conv

            # このセッションのツール実行
            tool_rows = (await session.execute(text(
                "SELECT tool_name, result_summary, expected_result, status, intent "
                "FROM tool_actions WHERE conversation_id = :cid "
                "ORDER BY created_at"
            ), {"cid": conv_id})).fetchall()

            rounds = []
            has_predictions = False
            for tr in tool_rows:
                has_pred = tr[2] is not None and tr[2] != ""
                if has_pred:
                    has_predictions = True
                has_intent = tr[4] is not None and tr[4] != ""
                # 生データ（DB）と短い要約の両方を返す
                raw_result = tr[1] or ""
                short_summary = Pipeline._summarize_result(tr[0], raw_result, tr[3] or "success")
                rounds.append({
                    "tool_name": tr[0],
                    "result_summary": short_summary,
                    "result_raw": raw_result[:200] if len(raw_result) > 80 else raw_result,
                    "expected": tr[2] if has_pred else None,
                    "intent": tr[4] if has_intent else None,
                    "status": tr[3],
                    "has_prediction": has_pred,
                    "has_intent": has_intent,
                })

            sessions.append({
                "conv_id": conv_id,
                "started_at": str(started_at) if started_at else "",
                "source": source or "chat",
                "trigger": trigger,
                "rounds": rounds,
                "round_count": len(rounds),
                "has_predictions": has_predictions,
                "distillation_response": distillation_resp,
            })

    # 現在の原則
    model = _load_self_model()
    principles = model.get("principles", [])
    if isinstance(principles, list):
        current_principles = principles[-10:]
    else:
        current_principles = []

    return {
        "sessions": sessions,
        "current_principles": current_principles,
        "total": total,
    }


# --- X連携 ---

@router.get("/x/status")
async def x_status():
    """Xのログイン状態を返す"""
    from app.tools.builtin import X_SESSION_PATH
    return {"logged_in": X_SESSION_PATH.exists()}


@router.post("/x/login")
async def x_login():
    """ヘッドありブラウザを起動してXログインを待機、完了後セッションを保存する"""
    from app.tools.builtin import X_SESSION_PATH
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"success": False, "error": "playwrightがインストールされていません。install.batを再実行してください。"}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto("https://x.com/login")

            # x.com/home に遷移したらログイン完了と判断（最大5分待機）
            await page.wait_for_url("**/home", timeout=300000)

            # セッション保存
            X_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(X_SESSION_PATH))
            await browser.close()

        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/x/logout")
async def x_logout():
    """Xセッションを削除する"""
    from app.tools.builtin import X_SESSION_PATH
    if X_SESSION_PATH.exists():
        X_SESSION_PATH.unlink()
    return {"success": True}
