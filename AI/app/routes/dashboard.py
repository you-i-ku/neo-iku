"""ダッシュボードAPI"""
import math
import logging
from datetime import datetime
from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import text
from app.llm.manager import llm_manager
from app.memory.database import async_session
from app.memory.store import count_messages, count_conversations, count_iku_logs
from app.persona.system_prompt import get_mode, set_mode

from app.scheduler.autonomous import scheduler
from app.pipeline import pipeline

logger = logging.getLogger("iku.dashboard")
router = APIRouter(prefix="/api")


@router.get("/status")
async def get_status():
    """イクの状態を返す"""
    llm = llm_manager.get()
    llm_available = await llm.is_available()

    async with async_session() as session:
        msg_count = await count_messages(session)
        conv_count = await count_conversations(session)
        log_count = await count_iku_logs(session)

    return {
        "llm_available": llm_available,
        "llm_provider": llm_manager.active_name,
        "message_count": msg_count,
        "conversation_count": conv_count,
        "iku_log_count": log_count,
        "connected_clients": pipeline.connected_count,
        "mode": get_mode(),
    }


class ModeRequest(BaseModel):
    mode: str


@router.post("/mode")
async def change_mode(req: ModeRequest):
    """モード切替。イクモードに切り替え時、過去ログを自動インポート。"""
    set_mode(req.mode)

    if req.mode == "iku":
        # 過去ログが未インポートなら自動実行
        async with async_session() as session:
            existing = await count_iku_logs(session)

        if existing == 0:
            from app.importer.log_parser import import_iku_logs
            result = await import_iku_logs(async_session)
            logger.info(f"過去ログ自動インポート: {result}")
            return {"mode": get_mode(), "import": result}

    return {"mode": get_mode()}


@router.get("/models")
async def get_models():
    """LM Studioのロード済みモデル一覧"""
    llm = llm_manager.get()
    models = []
    if hasattr(llm, "list_models"):
        models = await llm.list_models()
    current = llm.model if hasattr(llm, "model") else "unknown"
    return {"models": models, "current": current}


class ModelRequest(BaseModel):
    model: str


@router.post("/models/select")
async def select_model(req: ModelRequest):
    """使用モデルを切り替え"""
    llm = llm_manager.get()
    if hasattr(llm, "set_model"):
        llm.set_model(req.model)
    return {"model": req.model}


# --- 開発用ツール ---

class IntervalRequest(BaseModel):
    seconds: int

@router.post("/dev/autonomous-interval")
async def set_autonomous_interval(req: IntervalRequest):
    """自律行動の間隔を変更"""
    scheduler.set_interval(req.seconds)
    return {"interval": scheduler._interval}


@router.post("/dev/autonomous-trigger")
async def trigger_autonomous():
    """自律行動を即時実行"""
    scheduler.trigger_now()
    return {"triggered": True}



@router.post("/dev/reset-db")
async def reset_db():
    """iku_logs以外の全テーブルをクリア"""
    from sqlalchemy import text
    async with async_session() as session:
        for table in ["messages", "conversations", "memory_summaries", "tool_actions", "self_model_snapshots"]:
            await session.execute(text(f"DELETE FROM {table}"))
        for fts in ["messages_fts", "memory_summaries_fts", "tool_actions_fts"]:
            await session.execute(text(f"DELETE FROM {fts}"))
        await session.commit()
    return {"reset": True}


@router.post("/dev/clear-self-model")
async def clear_self_model():
    """self_model.jsonの内容をクリア"""
    from app.tools.builtin import SELF_MODEL_PATH
    SELF_MODEL_PATH.write_text("{}", encoding="utf-8")
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
    return {
        "autonomous_interval": scheduler._interval,
        "concurrent_mode": scheduler._concurrent_mode,
        "motivation_energy": round(scheduler._motivation_energy, 1),
        "energy_breakdown": {k: round(v, 1) for k, v in scheduler._energy_breakdown.items()},
        "ablation": {
            "energy": scheduler.ablation_energy,
            "self_model": scheduler.ablation_self_model,
            "prediction": scheduler.ablation_prediction,
            "distillation": scheduler.ablation_distillation,
        },
    }


class AblationRequest(BaseModel):
    flag: str  # "energy" | "self_model" | "prediction" | "distillation"
    enabled: bool

@router.post("/dev/ablation")
async def set_ablation(req: AblationRequest):
    """Ablationフラグの切替"""
    flag_map = {
        "energy": "ablation_energy",
        "self_model": "ablation_self_model",
        "prediction": "ablation_prediction",
        "distillation": "ablation_distillation",
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
            "distillation": scheduler.ablation_distillation,
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
):
    """自律度計測レポートを生成する"""
    async with async_session() as session:
        # 1. Autonomy Ratio
        autonomy = await _calc_autonomy_ratio(session, date_from, date_to)

        # 2. Tool Diversity (Shannon Entropy)
        diversity = await _calc_tool_diversity(session, date_from, date_to)

        # 3. Self-Evolution
        evolution = await _calc_self_evolution(session, date_from, date_to)

        # 4. Error Recovery
        recovery = await _calc_error_recovery(session, date_from, date_to)

        # 5. Metacognitive Accuracy
        metacognition = await _calc_metacognition(session, date_from, date_to)

        # 6. Memory Utilization
        memory = await _calc_memory_utilization(session, date_from, date_to)

        # 7. Principle Accumulation
        principles = await _calc_principle_accumulation(session, date_from, date_to)

        # 8. Intent Diversity
        intent_div = await _calc_intent_diversity(session, date_from, date_to)

        # 9-13. Time-series metrics
        tool_entropy_ts = await _calc_tool_entropy_timeseries(session, date_from, date_to)
        prediction_ts = await _calc_prediction_accuracy_timeseries(session, date_from, date_to)
        energy_eff = await _calc_energy_efficiency(session, date_from, date_to)
        sm_velocity = await _calc_self_model_velocity(session, date_from, date_to)
        session_length = await _calc_session_length_trend(session, date_from, date_to)

    # Summary
    total_actions = sum(diversity["distribution"].values()) if diversity["distribution"] else 0
    autonomy_ratio = autonomy["ratio"]
    normalized_entropy = (diversity["entropy"] / diversity["max_entropy"]) if diversity["max_entropy"] > 0 else 0
    recovery_rate = recovery["recovery_rate"]

    # normalize self-evolution: cap at 50 changes
    normalized_self_evolution = min(evolution["total_changes"] / 50, 1.0) if evolution["total_changes"] > 0 else 0
    # normalize memory utilization: cap at 100 operations
    mem_total = memory["memory_search"] + memory["memory_write"] + memory["action_search"]
    normalized_memory_util = min(mem_total / 100, 1.0) if mem_total > 0 else 0

    autonomy_score = round(
        0.3 * autonomy_ratio
        + 0.2 * normalized_entropy
        + 0.2 * recovery_rate
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
            "error_recovery": recovery,
            "metacognitive_accuracy": metacognition,
            "memory_utilization": memory,
            "principle_accumulation": principles,
            "intent_diversity": intent_div,
            "tool_entropy_ts": tool_entropy_ts,
            "prediction_accuracy_ts": prediction_ts,
            "energy_efficiency": energy_eff,
            "self_model_velocity": sm_velocity,
            "session_length_trend": session_length,
        },
    }


async def _calc_autonomy_ratio(session, date_from: str, date_to: str) -> dict:
    rows = (await session.execute(text(
        "SELECT source, COUNT(*) as cnt FROM conversations "
        "WHERE started_at BETWEEN :f AND :t AND is_imported = 0 "
        "GROUP BY source"
    ), {"f": date_from, "t": date_to})).fetchall()

    counts = {r[0] or "chat": r[1] for r in rows}
    autonomous = counts.get("autonomous", 0)
    chat = counts.get("chat", 0)
    total = autonomous + chat
    ratio = round(autonomous / total, 3) if total > 0 else 0.0

    # トリガー別内訳（タイマー vs エネルギー vs 手動）
    trigger_rows = (await session.execute(text(
        "SELECT trigger, COUNT(*) as cnt FROM conversations "
        "WHERE started_at BETWEEN :f AND :t AND is_imported = 0 AND source = 'autonomous' "
        "GROUP BY trigger"
    ), {"f": date_from, "t": date_to})).fetchall()

    trigger_counts = {(r[0] or "timer"): r[1] for r in trigger_rows}
    energy = trigger_counts.get("energy", 0)
    timer = trigger_counts.get("timer", 0)
    manual = trigger_counts.get("manual", 0)
    energy_ratio = round(energy / autonomous, 3) if autonomous > 0 else 0.0

    return {
        "autonomous": autonomous, "chat": chat, "ratio": ratio,
        "trigger": {"energy": energy, "timer": timer, "manual": manual, "energy_ratio": energy_ratio},
    }


async def _calc_tool_diversity(session, date_from: str, date_to: str) -> dict:
    rows = (await session.execute(text(
        "SELECT tool_name, COUNT(*) as cnt FROM tool_actions "
        "WHERE created_at BETWEEN :f AND :t "
        "GROUP BY tool_name"
    ), {"f": date_from, "t": date_to})).fetchall()

    distribution = {r[0]: r[1] for r in rows}
    total = sum(distribution.values())
    if total == 0:
        return {"entropy": 0.0, "max_entropy": 0.0, "distribution": {}}

    entropy = 0.0
    for count in distribution.values():
        p = count / total
        if p > 0:
            entropy -= p * math.log2(p)

    max_entropy = round(math.log2(len(distribution)), 3) if len(distribution) > 1 else 0.0
    return {"entropy": round(entropy, 3), "max_entropy": max_entropy, "distribution": distribution}


async def _calc_self_evolution(session, date_from: str, date_to: str) -> dict:
    rows = (await session.execute(text(
        "SELECT changed_key, COUNT(*) as cnt FROM self_model_snapshots "
        "WHERE created_at BETWEEN :f AND :t "
        "GROUP BY changed_key"
    ), {"f": date_from, "t": date_to})).fetchall()

    changes_by_key = {(r[0] or "unknown"): r[1] for r in rows}
    total = sum(changes_by_key.values())
    return {"total_changes": total, "unique_keys": len(changes_by_key), "changes_by_key": changes_by_key}


async def _calc_error_recovery(session, date_from: str, date_to: str) -> dict:
    rows = (await session.execute(text(
        "SELECT tool_name, status FROM tool_actions "
        "WHERE created_at BETWEEN :f AND :t "
        "ORDER BY created_at"
    ), {"f": date_from, "t": date_to})).fetchall()

    total_errors = 0
    recovered = 0

    for i, row in enumerate(rows):
        if row[1] == "error":
            total_errors += 1
            if i + 1 < len(rows):
                next_row = rows[i + 1]
                if next_row[1] != "error":
                    recovered += 1

    recovery_rate = round(recovered / total_errors, 3) if total_errors > 0 else 0.0
    return {"total_errors": total_errors, "recovered": recovered, "recovery_rate": recovery_rate}


async def _calc_metacognition(session, date_from: str, date_to: str) -> dict:
    row = (await session.execute(text(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success "
        "FROM tool_actions "
        "WHERE expected_result IS NOT NULL AND created_at BETWEEN :f AND :t"
    ), {"f": date_from, "t": date_to})).fetchone()

    total = row[0] or 0
    success = row[1] or 0
    return {"predictions_made": total, "success_rate": round(success / total, 3) if total > 0 else 0.0}


async def _calc_memory_utilization(session, date_from: str, date_to: str) -> dict:
    rows = (await session.execute(text(
        "SELECT tool_name, COUNT(*) as cnt FROM tool_actions "
        "WHERE tool_name IN ('search_memories', 'write_diary', 'search_action_log') "
        "AND created_at BETWEEN :f AND :t "
        "GROUP BY tool_name"
    ), {"f": date_from, "t": date_to})).fetchall()

    counts = {r[0]: r[1] for r in rows}
    return {
        "memory_search": counts.get("search_memories", 0),
        "memory_write": counts.get("write_diary", 0),
        "action_search": counts.get("search_action_log", 0),
    }


async def _calc_principle_accumulation(session, date_from: str, date_to: str) -> dict:
    row = (await session.execute(text(
        "SELECT COUNT(*) FROM self_model_snapshots "
        "WHERE changed_key = 'principles' AND created_at BETWEEN :f AND :t"
    ), {"f": date_from, "t": date_to})).fetchone()

    distillation_count = row[0] if row else 0

    # 現在のself_model.jsonから原則数を取得
    from app.tools.builtin import _load_self_model
    model = _load_self_model()
    principles = model.get("principles", [])
    current = len(principles) if isinstance(principles, list) else 0

    return {"distillation_count": distillation_count, "current_principles": current}


async def _calc_intent_diversity(session, date_from: str, date_to: str) -> dict:
    """intent使用率 + ユニーク意図数"""
    row = (await session.execute(text(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN intent IS NOT NULL AND intent != '' THEN 1 ELSE 0 END) as with_intent "
        "FROM tool_actions "
        "WHERE created_at BETWEEN :f AND :t"
    ), {"f": date_from, "t": date_to})).fetchone()

    total = row[0] or 0
    with_intent = row[1] or 0
    usage_rate = round(with_intent / total, 3) if total > 0 else 0.0

    # ユニーク意図数
    unique_row = (await session.execute(text(
        "SELECT COUNT(DISTINCT intent) FROM tool_actions "
        "WHERE intent IS NOT NULL AND intent != '' AND created_at BETWEEN :f AND :t"
    ), {"f": date_from, "t": date_to})).fetchone()
    unique_intents = unique_row[0] if unique_row else 0

    return {
        "total_actions": total,
        "with_intent": with_intent,
        "usage_rate": usage_rate,
        "unique_intents": unique_intents,
    }


async def _calc_tool_entropy_timeseries(session, date_from: str, date_to: str) -> dict:
    """日別ツールエントロピー（行動多様性の推移）"""
    rows = (await session.execute(text(
        "SELECT DATE(created_at) as day, tool_name, COUNT(*) as cnt "
        "FROM tool_actions WHERE created_at BETWEEN :f AND :t "
        "GROUP BY day, tool_name ORDER BY day"
    ), {"f": date_from, "t": date_to})).fetchall()

    # 日別に集計
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


async def _calc_prediction_accuracy_timeseries(session, date_from: str, date_to: str) -> dict:
    """日別予測精度推移"""
    rows = (await session.execute(text(
        "SELECT DATE(created_at) as day, "
        "COUNT(*) as total, "
        "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as hits "
        "FROM tool_actions "
        "WHERE expected_result IS NOT NULL AND expected_result != '' "
        "AND created_at BETWEEN :f AND :t "
        "GROUP BY day ORDER BY day"
    ), {"f": date_from, "t": date_to})).fetchall()

    days = []
    for day, total, hits in rows:
        rate = round(hits / total, 3) if total > 0 else 0.0
        days.append({"date": str(day), "total": total, "hits": hits, "rate": rate})

    return {"days": days}


async def _calc_energy_efficiency(session, date_from: str, date_to: str) -> dict:
    """エネルギー効率: セッションごとのユニークツールパターン率"""
    rows = (await session.execute(text(
        "SELECT conversation_id, tool_name "
        "FROM tool_actions "
        "WHERE created_at BETWEEN :f AND :t AND conversation_id IS NOT NULL "
        "ORDER BY conversation_id, created_at"
    ), {"f": date_from, "t": date_to})).fetchall()

    if not rows:
        return {"avg_efficiency": 0.0, "session_count": 0, "sessions": []}

    # セッション別にツール列を集める
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


async def _calc_self_model_velocity(session, date_from: str, date_to: str) -> dict:
    """自己モデル変化速度（日別スナップショット数）"""
    rows = (await session.execute(text(
        "SELECT DATE(created_at) as day, COUNT(*) as cnt "
        "FROM self_model_snapshots "
        "WHERE created_at BETWEEN :f AND :t "
        "GROUP BY day ORDER BY day"
    ), {"f": date_from, "t": date_to})).fetchall()

    days = [{"date": str(day), "count": cnt} for day, cnt in rows]
    total = sum(d["count"] for d in days)
    avg_per_day = round(total / len(days), 2) if days else 0.0

    return {"days": days, "total": total, "avg_per_day": avg_per_day}


async def _calc_session_length_trend(session, date_from: str, date_to: str) -> dict:
    """セッション長推移（自律セッションごとのツール実行数）"""
    rows = (await session.execute(text(
        "SELECT c.id, DATE(c.started_at) as day, COUNT(t.id) as action_count "
        "FROM conversations c "
        "LEFT JOIN tool_actions t ON t.conversation_id = c.id "
        "WHERE c.source = 'autonomous' AND c.is_imported = 0 "
        "AND c.started_at BETWEEN :f AND :t "
        "GROUP BY c.id "
        "ORDER BY c.started_at"
    ), {"f": date_from, "t": date_to})).fetchall()

    if not rows:
        return {"days": [], "avg_length": 0.0}

    # 日別平均
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
