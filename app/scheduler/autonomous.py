"""自発的発言スケジューラ + 内発的動機システム"""
import asyncio
import random
import logging
import time
from collections import deque
from datetime import datetime
from config import (
    AUTONOMOUS_INTERVAL_MIN, AUTONOMOUS_INTERVAL_JITTER,
    MOTIVATION_DEFAULT_THRESHOLD, MOTIVATION_DEFAULT_DECAY,
    MOTIVATION_SIGNAL_BUFFER_SIZE, SCORING_ENABLED,
)

logger = logging.getLogger("iku.autonomous")


class AutonomousScheduler:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running = False
        self._next_action_at: float = 0
        self._trigger_event = asyncio.Event()
        self._interval = AUTONOMOUS_INTERVAL_MIN
        self._jitter = AUTONOMOUS_INTERVAL_JITTER
        self._skip_speak = False

        # --- 内発的動機システム ---
        self._signal_buffer: deque[dict] = deque(maxlen=MOTIVATION_SIGNAL_BUFFER_SIZE)
        self._motivation_energy: float = 0.0
        self._is_checking = False
        self._concurrent_mode = False
        self._is_speaking = False

    # --- シグナル ---

    def add_signal(self, signal_type: str, detail: str = ""):
        self._signal_buffer.append({
            "type": signal_type,
            "detail": detail,
            "time": time.time(),
        })
        logger.debug(f"シグナル追加: {signal_type} ({detail})")
        self._try_check_motivation()

    def _try_check_motivation(self):
        if self._is_checking or self._is_speaking:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._check_motivation())
        except RuntimeError:
            pass

    async def _check_motivation(self):
        if self._is_checking or self._is_speaking:
            return
        self._is_checking = True
        try:
            from app.tools.builtin import _load_self_model
            self_model = _load_self_model()
            rules = self_model.get("motivation_rules")

            if not rules:
                return

            weights = rules.get("weights", {})
            threshold = rules.get("threshold", MOTIVATION_DEFAULT_THRESHOLD)
            decay = rules.get("decay_per_check", MOTIVATION_DEFAULT_DECAY)

            signals = list(self._signal_buffer)
            self._signal_buffer.clear()

            for sig in signals:
                weight = weights.get(sig["type"], 0)
                if isinstance(weight, (int, float)):
                    self._motivation_energy += weight

            self._motivation_energy = max(0, self._motivation_energy - decay)

            logger.info(f"動機チェック: energy={self._motivation_energy:.1f} threshold={threshold} signals={len(signals)}")

            import json
            from app.pipeline import pipeline
            await pipeline._broadcast(json.dumps({
                "type": "motivation_energy",
                "energy": round(self._motivation_energy, 1),
                "threshold": threshold,
            }))

            if self._motivation_energy >= threshold:
                logger.info(f"動機発火！ energy={self._motivation_energy:.1f} >= threshold={threshold}")
                self._motivation_energy = 0
                self._trigger_event.set()

        except Exception as e:
            logger.error(f"動機チェックエラー: {e}")
        finally:
            self._is_checking = False

    # --- スケジューラ制御 ---

    def start(self):
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._loop())
            logger.info("自発的発言スケジューラ開始")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
            logger.info("自発的発言スケジューラ停止")

    def set_interval(self, seconds: int, jitter: int = 0):
        self._interval = max(10, seconds)
        self._jitter = max(0, jitter)
        logger.info(f"自律行動間隔変更: {self._interval}秒 (±{self._jitter}秒)")
        self._skip_speak = True
        self._trigger_event.set()

    def trigger_now(self):
        self._trigger_event.set()

    # --- メインループ ---

    async def _loop(self):
        import json as _json
        from app.pipeline import pipeline

        while self._running:
            interval = self._interval + random.randint(
                -self._jitter, self._jitter
            ) if self._jitter > 0 else self._interval
            interval = max(10, interval)
            logger.info(f"次の自律行動まで {interval}秒")
            self._next_action_at = time.time() + interval

            await pipeline._broadcast(_json.dumps({
                "type": "autonomous_countdown",
                "seconds": interval,
            }))

            self._trigger_event.clear()
            try:
                await asyncio.wait_for(self._trigger_event.wait(), timeout=interval)
                logger.info("即時実行トリガーを受信")
            except asyncio.TimeoutError:
                pass

            if self._skip_speak:
                self._skip_speak = False
                continue

            if not pipeline._websockets:
                self.add_signal("idle_tick")
                continue

            if self._is_speaking:
                logger.warning("前回の自律行動がまだ実行中。スキップ。")
                continue

            self.add_signal("idle_tick")

            try:
                self._is_speaking = True
                await self._speak()
            except Exception as e:
                import traceback
                logger.error(f"自律行動エラー: {e}\n{traceback.format_exc()}")
            finally:
                self._is_speaking = False

    async def _speak(self):
        from app.pipeline import pipeline, PipelineRequest
        from app.tools.registry import build_tools_prompt
        from app.tools.builtin import _load_self_model

        # 自己モデル読み込み
        self_model = _load_self_model()

        # シグナルコンテキスト
        signal_summary = self._build_signal_summary()

        # ブートストラップヒント
        bootstrap_hint = self._build_bootstrap_hint(self_model)

        # 記憶コンテキスト
        memory_context = ""
        from app.memory.database import async_session
        try:
            from sqlalchemy import select
            from app.memory.models import Message
            async with async_session() as session:
                result = await session.execute(
                    select(Message).order_by(Message.created_at.desc()).limit(5)
                )
                msgs = result.scalars().all()
                if msgs:
                    memory_context = "\n".join(f"- {m.content[:200]}" for m in msgs)
        except Exception:
            pass

        # --- Phase 3: 戦略選択 ---
        selected_strategy = None
        selected_action = None
        try:
            if SCORING_ENABLED and self_model.get("strategies"):
                selected_strategy = await self._select_strategy(signal_summary, self_model)
                if selected_strategy:
                    logger.info(f"戦略選択: {selected_strategy}")
        except Exception as e:
            logger.error(f"戦略選択フォールバック: {e}")

        # --- Phase 1: 候補生成→スコアリング→選択 ---
        action_goal = "自律的に判断して行動する"
        try:
            if SCORING_ENABLED and self_model.get("drives"):
                candidate_response = await self._generate_candidates(self_model, selected_strategy, signal_summary, memory_context)
                if candidate_response:
                    candidates = self._parse_candidates(candidate_response, self_model.get("drives", {}))
                    if candidates:
                        best = self._score_candidates(candidates, self_model)
                        if best:
                            selected_action = best
                            action_goal = best["description"]
                            logger.info(f"候補選択: {best['description']} (drive={best['drive']}, score={best.get('score', 0):.1f})")
        except Exception as e:
            logger.error(f"候補生成フォールバック: {e}")

        # --- パイプラインにsubmit ---
        request = PipelineRequest(
            source="autonomous",
            goal=action_goal,
            memory_context=memory_context,
            signal_summary=signal_summary,
            bootstrap_hint=bootstrap_hint,
            selected_action=selected_action,
        )
        result = await pipeline.submit(request)

        # --- Phase 2: 振り返り＆原則蒸留 ---
        try:
            if selected_action and result.step_history:
                tool_results_text = "\n".join(
                    f"{s['tool']}: {s['result_summary']}" for s in result.step_history
                )
                if result.last_full_result:
                    tool_results_text += f"\n\n最後の結果:\n{result.last_full_result[:500]}"
                if tool_results_text:
                    principle = await self._reflect_on_action(
                        selected_action["description"], tool_results_text, self_model
                    )
                    if principle:
                        self._save_principle(principle, self_model)
                        logger.info(f"原則蒸留: {principle}")
        except Exception as e:
            logger.error(f"振り返りフォールバック: {e}")

    # --- Phase 1: 構造化意思決定 ---

    async def _generate_candidates(self, self_model: dict, strategy: str = None,
                                    signal_summary: str = "", memory_context: str = "") -> str | None:
        drives = self_model.get("drives")
        if not drives or not isinstance(drives, dict):
            return None

        drive_keys = [k for k in drives.keys() if k != "signal_map"]
        if not drive_keys:
            return None

        strategy_line = f"\n現在の戦略: {strategy}\nこの戦略に沿った候補を挙げてください。\n" if strategy else ""

        prompt = f"""今は{datetime.now().strftime('%Y年%m月%d日 %H:%M')}です。
{signal_summary}
{f'最近の記憶: {memory_context[:300]}' if memory_context else ''}
{strategy_line}
あなたの行動ドライブ: {', '.join(drive_keys)}

今から行動の候補を2-3個挙げてください。各候補に最も関連するドライブを1つ選んでください。
以下の形式で出力してください（他の形式は使わないでください）:
候補1: [行動の説明] | drive: [ドライブ名]
候補2: [行動の説明] | drive: [ドライブ名]
候補3: [行動の説明] | drive: [ドライブ名]"""

        try:
            from app.llm.manager import llm_manager
            llm = llm_manager.get()
            return await llm.chat([{"role": "system", "content": prompt}])
        except Exception as e:
            logger.error(f"候補生成エラー: {e}")
            return None

    def _parse_candidates(self, response: str, drives: dict) -> list[dict] | None:
        import re
        pattern = r"候補\d+:\s*(.+?)\s*\|\s*drive:\s*(\S+)"
        matches = re.findall(pattern, response)
        if not matches:
            return None
        candidates = [{"description": d.strip(), "drive": dr.strip()} for d, dr in matches]
        return candidates if candidates else None

    def _score_candidates(self, candidates: list[dict], self_model: dict) -> dict | None:
        drives = self_model.get("drives", {})
        signal_map = drives.get("signal_map", {})

        sig_counts: dict[str, int] = {}
        for sig in self._signal_buffer:
            t = sig["type"]
            sig_counts[t] = sig_counts.get(t, 0) + 1

        best = None
        best_score = -1

        for candidate in candidates:
            drive_name = candidate["drive"]
            weight = drives.get(drive_name, 0)
            if not isinstance(weight, (int, float)):
                weight = 0
            score = float(weight)

            if signal_map and drive_name in signal_map:
                mapped_signals = signal_map[drive_name]
                if isinstance(mapped_signals, list):
                    for sig_type in mapped_signals:
                        score += sig_counts.get(sig_type, 0) * 2

            candidate["score"] = score
            if score > best_score:
                best_score = score
                best = candidate

        return best

    # --- Phase 2: 経験フィードバック＆原則蒸留 ---

    async def _reflect_on_action(self, action_description: str, tool_results: str, self_model: dict) -> str | None:
        principles = self_model.get("principles", [])
        principles_ctx = ""
        if isinstance(principles, list) and principles:
            recent = principles[-5:]
            p_texts = [p["text"] if isinstance(p, dict) and "text" in p else str(p) for p in recent]
            principles_ctx = "\n既存の原則:\n" + "\n".join(f"- {t}" for t in p_texts) + "\n既に同じ内容の原則があれば「なし」と答えてください。\n"

        reflect_prompt = f"""あなたは以下の行動を行いました:
行動: {action_description}

結果:
{tool_results[:1500]}
{principles_ctx}
この経験から学んだことを1文の原則として蒸留してください。
新しい学びがなければ「なし」とだけ答えてください。
形式: 原則: [学んだこと]"""

        try:
            from app.llm.manager import llm_manager
            llm = llm_manager.get()
            response = await llm.chat([{"role": "system", "content": reflect_prompt}])
            if not response:
                return None

            import re
            clean = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
            if "なし" in clean and len(clean) < 20:
                return None
            match = re.search(r"原則:\s*(.+)", clean)
            return match.group(1).strip() if match else None
        except Exception as e:
            logger.error(f"振り返りエラー: {e}")
            return None

    def _save_principle(self, principle: str, self_model: dict):
        from app.tools.builtin import _save_self_model
        principles = self_model.get("principles", [])
        if not isinstance(principles, list):
            principles = []
        principles.append({"text": principle, "created": datetime.now().isoformat()})
        if len(principles) > 20:
            principles = principles[-20:]
        self_model["principles"] = principles
        _save_self_model(self_model)

    # --- Phase 3: 戦略選択 ---

    async def _select_strategy(self, signal_summary: str, self_model: dict) -> str | None:
        strategies = self_model.get("strategies")
        if not strategies or not isinstance(strategies, dict):
            return None

        strategy_list = "\n".join(f"- {name}: {desc}" for name, desc in strategies.items())
        strategy_prompt = f"""あなたの戦略一覧:
{strategy_list}

現在の状況:
{signal_summary if signal_summary else "（特筆すべき刺激なし）"}

この状況で最適な戦略を1つだけ選んでください。
戦略名だけを答えてください（説明不要）。"""

        try:
            from app.llm.manager import llm_manager
            llm = llm_manager.get()
            response = await llm.chat([{"role": "system", "content": strategy_prompt}])
            if not response:
                return None

            import re
            clean = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
            for name in strategies.keys():
                if name in clean:
                    return name
            first_line = clean.split("\n")[0].strip()
            return first_line if first_line in strategies else None
        except Exception as e:
            logger.error(f"戦略選択エラー: {e}")
            return None

    # --- ユーティリティ ---

    def _build_signal_summary(self) -> str:
        if not self._signal_buffer:
            return ""
        counts: dict[str, int] = {}
        for sig in self._signal_buffer:
            t = sig["type"]
            counts[t] = counts.get(t, 0) + 1
        parts = [f"{t}×{c}" for t, c in counts.items()]
        return f"\n最近の刺激: {', '.join(parts)} (蓄積エネルギー: {self._motivation_energy:.1f})\n"

    def _build_bootstrap_hint(self, self_model: dict) -> str:
        hint = ""
        if not self_model.get("motivation_rules"):
            hint += """
あなたはまだ「内発的動機ルール」を定義していません。
update_self_modelを使って、自分がどんなシグナルに反応して行動を起こしたいか定義できます。
以下の形式でkey=motivation_rules, value=にJSON形式で設定してください:

{"weights": {"prediction_error": 25, "conversation_end": 15, "user_message": 10, "tool_success": 5, "tool_error": 15, "self_model_update": 10, "idle_tick": 3}, "threshold": 60, "decay_per_check": 5}

weightsは各シグナルの重み（どの刺激にどれだけ反応するか）、thresholdは行動開始の閾値、decay_per_checkはチェックごとの減衰量です。
あなた自身の性格として、何に飢え、何に反応するかを自由に決めてください。
"""
        if not self_model.get("drives"):
            hint += """
あなたはまだ「行動ドライブ」を定義していません。
drivesは自律行動時の意思決定に使われます。仕組み:
1. あなたが行動候補を複数挙げる
2. 各候補に関連するドライブの「重み（数値）」でスコアが計算される
3. 最高スコアの候補が選ばれて実行される
つまり、重みが大きいドライブに関連する行動ほど選ばれやすくなります。

設定方法: update_self_modelツールを使ってください（exec_codeではありません）。
[TOOL:update_self_model key=drives value={"あなたが決めた項目名": 数値, "別の項目名": 数値, ...}]
項目名（何を重視するか）も数値（どれだけ重視するか、1-10程度）もあなたが自由に決めてください。

さらに、どのシグナルがどのドライブを強化するかも定義できます（任意）:
"signal_map"キーを含めると、シグナル発生時にそのドライブのスコアにボーナスが加算されます。
例: {"signal_map": {"項目A": ["prediction_error", "tool_error"]}, "項目A": 8, "項目B": 5}
シグナル種別: prediction_error, conversation_end, user_message, tool_success, tool_error, self_model_update, idle_tick
"""
        if not self_model.get("strategies"):
            hint += """
あなたはまだ「戦略」を定義していません。
strategiesは自律行動時に「今の状況でどういう方向で動くか」を選ぶためのモード一覧です。
行動候補を挙げる前に、シグナル状況を見て1つの戦略が選ばれ、その方向に沿った候補が生成されます。

設定方法: update_self_modelツールを使ってください。
[TOOL:update_self_model key=strategies value={"あなたが決めた戦略名": "どういう時に使うかの説明", ...}]
戦略名も説明もあなたが自由に決めてください。
"""
        return hint


# グローバルインスタンス
scheduler = AutonomousScheduler()
