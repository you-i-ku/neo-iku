"""自発的発言スケジューラ + 内発的動機システム"""
import asyncio
import json
import random
import logging
import time
from collections import deque
from datetime import datetime
from config import (
    AUTONOMOUS_INTERVAL_MIN, AUTONOMOUS_INTERVAL_JITTER,
    MOTIVATION_DEFAULT_THRESHOLD, MOTIVATION_DEFAULT_DECAY,
    MOTIVATION_DEFAULT_WEIGHTS, MOTIVATION_FLUCTUATION_SIGMA,
    MOTIVATION_SIGNAL_BUFFER_SIZE, SCORING_ENABLED,
    MOTIVATION_DEFAULT_ACTION_COSTS, MOTIVATION_DEFAULT_ACTION_COST_FALLBACK,
    ENV_STIMULUS_ENABLED, ENV_STIMULUS_PROBABILITY,
    DATA_DIR,
    ABLATION_ENERGY_SYSTEM, ABLATION_SELF_MODEL,
    ABLATION_PREDICTION, ABLATION_DISTILLATION,
)
import math

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
        self._last_conv_id: int | None = None  # セッション継続用
        self._last_trigger: str = "timer"  # "timer" / "energy" / "manual"
        self._pending_messages: list[dict] = []  # ユーザー入力キュー
        self._energy_breakdown: dict[str, float] = {}  # シグナル種別ごとのエネルギー貢献度

        # Ablationフラグ（ランタイムで切替可能）
        self.ablation_energy = ABLATION_ENERGY_SYSTEM
        self.ablation_self_model = ABLATION_SELF_MODEL
        self.ablation_prediction = ABLATION_PREDICTION
        self.ablation_distillation = ABLATION_DISTILLATION

    # --- シグナル ---

    def add_pending_message(self, text: str):
        """ユーザー入力をシグナルとして蓄積（即応答ではなく動機サイクルで処理）"""
        self._pending_messages.append({"text": text})
        self.add_signal("user_message", text[:100])

    def add_signal(self, signal_type: str, detail: str = ""):
        self._signal_buffer.append({
            "type": signal_type,
            "detail": detail,
            "time": time.time(),
        })
        logger.debug(f"シグナル追加: {signal_type} ({detail})")
        self._try_check_motivation()

    def consume_energy(self, tool_name: str):
        """ツール実行によるエネルギー消費"""
        if not self.ablation_energy:
            return
        from app.tools.builtin import _load_self_model
        self_model = _load_self_model()
        rules = self_model.get("motivation_rules")
        if isinstance(rules, dict):
            costs = rules.get("action_costs", {})
            ai_threshold = rules.get("threshold")
            threshold = ai_threshold if ai_threshold is not None else self._calc_default_threshold()
        else:
            costs = {}
            threshold = self._calc_default_threshold()
        # AI定義のコスト → デフォルトコスト → フォールバック値
        cost = costs.get(tool_name,
                MOTIVATION_DEFAULT_ACTION_COSTS.get(tool_name, MOTIVATION_DEFAULT_ACTION_COST_FALLBACK))
        if isinstance(cost, (int, float)) and cost > 0:
            self._motivation_energy = max(0, self._motivation_energy - cost)
            logger.info(f"エネルギー消費: {tool_name} cost={cost} → energy={self._motivation_energy:.1f}")
            # UI更新
            try:
                import json
                from app.pipeline import pipeline
                loop = asyncio.get_running_loop()
                loop.create_task(pipeline._broadcast(json.dumps({
                    "type": "motivation_energy",
                    "energy": round(self._motivation_energy, 1),
                    "threshold": threshold,
                    "breakdown": {k: round(v, 1) for k, v in self._energy_breakdown.items()},
                })))
            except RuntimeError:
                pass

    def _try_check_motivation(self):
        if self._is_checking:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._check_motivation())
        except RuntimeError:
            pass

    def _calc_default_threshold(self) -> float:
        """1回の行動に必要なエネルギー = コスト平均 × PLAN_MAX_TOOLS"""
        from config import PLAN_MAX_TOOLS
        costs = list(MOTIVATION_DEFAULT_ACTION_COSTS.values())
        if not costs:
            return 60.0
        avg = sum(costs) / len(costs)
        return round(avg * PLAN_MAX_TOOLS, 1)

    async def _check_motivation(self):
        if self._is_checking:
            return
        self._is_checking = True
        try:
            # エネルギーシステム無効時: シグナルをクリアするだけ（タイマーのみで発火）
            if not self.ablation_energy:
                self._signal_buffer.clear()
                self._motivation_energy = 0.0
                return

            from app.tools.builtin import _load_self_model
            self_model = _load_self_model()
            rules = self_model.get("motivation_rules")
            if isinstance(rules, dict):
                weights = rules.get("weights", {})
                ai_threshold = rules.get("threshold")
                threshold = ai_threshold if ai_threshold is not None else self._calc_default_threshold()
                decay = rules.get("decay_per_check", MOTIVATION_DEFAULT_DECAY)
            else:
                weights = MOTIVATION_DEFAULT_WEIGHTS
                threshold = self._calc_default_threshold()
                decay = MOTIVATION_DEFAULT_DECAY

            signals = list(self._signal_buffer)
            self._signal_buffer.clear()

            for sig in signals:
                weight = weights.get(sig["type"], 0)
                if isinstance(weight, (int, float)):
                    self._motivation_energy += weight
                    if weight > 0:
                        self._energy_breakdown[sig["type"]] = self._energy_breakdown.get(sig["type"], 0) + weight

            self._motivation_energy = max(0, self._motivation_energy - decay)

            # 揺らぎ: エネルギーの溜まり方に偶然性を持たせる（何を考えるかは操作しない）
            if MOTIVATION_FLUCTUATION_SIGMA > 0:
                fluctuation = random.gauss(0, MOTIVATION_FLUCTUATION_SIGMA)
                self._motivation_energy = max(0, self._motivation_energy + fluctuation)

            logger.info(f"動機チェック: energy={self._motivation_energy:.1f} threshold={threshold} signals={len(signals)} speaking={self._is_speaking}")

            import json
            from app.pipeline import pipeline
            await pipeline._broadcast(json.dumps({
                "type": "motivation_energy",
                "energy": round(self._motivation_energy, 1),
                "threshold": threshold,
                "breakdown": {k: round(v, 1) for k, v in self._energy_breakdown.items()},
            }))

            # 発火判定: 行動中でなく、閾値を超えた場合のみ
            # エネルギーはリセットしない（ツール実行時にconsume_energyで消費される）
            if not self._is_speaking and self._motivation_energy >= threshold:
                logger.info(f"動機発火！ energy={self._motivation_energy:.1f} >= threshold={threshold}")
                self._last_trigger = "energy"
                self._trigger_event.set()

        except Exception as e:
            logger.error(f"動機チェックエラー: {e}")
        finally:
            self._is_checking = False

    # --- スケジューラ制御 ---

    def start(self):
        if self._task is None:
            self._load_stimulus_pools()
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
        self._last_trigger = "manual"
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
                logger.info(f"即時実行トリガーを受信 (trigger={self._last_trigger})")
            except asyncio.TimeoutError:
                self._last_trigger = "timer"

            if self._skip_speak:
                self._skip_speak = False
                continue

            if not pipeline._websockets:
                self.add_signal("idle_tick")
                continue

            if self._is_speaking:
                logger.warning("前回の自律行動がまだ実行中。スキップ。")
                continue

            trigger = self._last_trigger
            if trigger == "timer":
                self._last_conv_id = None  # タイマー起因は新規セッション
            self.add_signal("idle_tick")

            try:
                self._is_speaking = True
                self._energy_breakdown = {}
                await self._speak(trigger=trigger)
            except Exception as e:
                import traceback
                logger.error(f"自律行動エラー: {e}\n{traceback.format_exc()}")
            finally:
                self._is_speaking = False
                # 行動中に溜まったエネルギーの発火判定（バッファ空でも実行）
                self._try_check_motivation()

    async def _speak(self, trigger: str = "timer"):
        from app.pipeline import pipeline, PipelineRequest
        from app.tools.builtin import _load_self_model

        # === ユーザー入力の取り込み ===
        pending = list(self._pending_messages)
        self._pending_messages.clear()
        pending_source = "autonomous"
        pending_goal = ""
        if pending:
            # ユーザー入力がある場合、最新のメッセージをgoalに、sourceをchatに
            pending_goal = pending[-1]["text"]
            pending_source = "chat"
            # UI通知: お返事中です
            await pipeline._broadcast(json.dumps({
                "type": "responding_start",
            }))

        # === 外側ループ: メタ認知 ===

        # 1. 観測 (Observe): 現在の状態を把握
        # 環境刺激注入（確率的）
        env_stimulus = self._generate_env_stimulus()
        if env_stimulus:
            self.add_signal("env_stimulus", env_stimulus)
            logger.info(f"環境刺激注入: {env_stimulus}")
            await pipeline._broadcast(json.dumps({
                "type": "dev_env_stimulus",
                "content": env_stimulus,
            }))

        # シグナルバッファのスナップショットを取る（_check_motivationがクリアしても影響されない）
        signal_snapshot = list(self._signal_buffer)
        self_model = _load_self_model()
        signal_summary = self._build_signal_summary(signal_snapshot)
        bootstrap_hint = self._build_bootstrap_hint(self_model)
        memory_context = ""  # AIが自分でsearch_memoriesツールを使って取得する

        # 2. 方向付け (Orient): 戦略選択
        selected_strategy = None
        try:
            if SCORING_ENABLED and self_model.get("strategies"):
                selected_strategy = await self._select_strategy(signal_summary, self_model)
                if selected_strategy:
                    logger.info(f"戦略選択: {selected_strategy}")
        except Exception as e:
            logger.error(f"戦略選択フォールバック: {e}")

        # 3. 決定 (Decide): 候補生成→スコアリング→選択
        action_goal = ""
        selected_action = None
        try:
            if SCORING_ENABLED and self_model.get("drives"):
                candidate_response = await self._generate_candidates(self_model, selected_strategy, signal_summary, memory_context)
                if candidate_response:
                    candidates = self._parse_candidates(candidate_response, self_model.get("drives", {}))
                    if candidates:
                        best = self._score_candidates(candidates, self_model, signal_snapshot)
                        if best:
                            selected_action = best
                            action_goal = best["description"]
                            logger.info(f"候補選択: {best['description']} (drive={best['drive']}, score={best.get('score', 0):.1f})")
        except Exception as e:
            logger.error(f"候補生成フォールバック: {e}")

        # 4. 行動 (Act): pipelineで実行
        # action_complete起因なら前回のconv_idを引き継ぐ（セッション継続）
        continue_conv_id = None
        if self._last_conv_id is not None:
            has_action_complete = any(s["type"] == "action_complete" for s in signal_snapshot)
            if has_action_complete:
                continue_conv_id = self._last_conv_id
                logger.info(f"セッション継続: conv_id={continue_conv_id}")

        # ユーザー入力があればそちらを優先（sourceもchatに）
        final_source = pending_source if pending else "autonomous"
        final_goal = pending_goal if pending_goal else action_goal
        final_conv_id = continue_conv_id

        request = PipelineRequest(
            source=final_source,
            goal=final_goal,
            conv_id=final_conv_id,
            memory_context=memory_context,
            signal_summary=signal_summary,
            bootstrap_hint=bootstrap_hint,
            selected_action=selected_action,
            trigger=trigger,
        )
        result = await pipeline.submit(request)
        self._last_conv_id = result.conv_id  # 次のアクションでの継続用に保持

        # UI通知: お返事完了
        if pending:
            await pipeline._broadcast(json.dumps({
                "type": "responding_end",
            }))

        # 5. 振り返り (Reflect): 経験からの学び + 自己モデル更新検討
        await self._reflect(selected_action, result, self_model, action_goal, selected_strategy)

    async def _reflect(self, selected_action: dict | None, result, self_model: dict, action_goal: str = "", selected_strategy: str | None = None):
        """行動後の振り返り: 特性抽出 + 予測誤差シグナル"""
        if not self.ablation_distillation:
            logger.debug("蒸留無効（ablation）: 振り返りスキップ")
            return
        try:
            if not result.step_history:
                return

            action_description = selected_action["description"] if selected_action else action_goal
            if not action_description:
                # 目標なしでもツール実行があれば、実行内容から説明を生成
                tool_names = [s["tool"] for s in result.step_history if s.get("tool")]
                if tool_names:
                    action_description = " → ".join(tool_names)
                else:
                    return

            result_lines = []
            metacog_lines = []
            for s in result.step_history:
                result_lines.append(f"{s['tool']}: {s['result_summary']}")
                intent = s.get('intent')
                expected = s.get('expected')
                result_summary = s.get('result_summary', '')
                if intent or expected:
                    parts = []
                    if intent: parts.append(f"意図「{intent}」")
                    if expected: parts.append(f"予測「{expected}」")
                    parts.append(f"結果「{result_summary}」")
                    line = f"{s['tool']}: " + " → ".join(parts)
                    # ペアワイズ比較（2要素以上ある場合のみ）
                    pairs = []
                    if intent and expected:
                        pairs.append(f"  [意図→予測] 意図「{intent}」に対し予測「{expected}」")
                    if intent and result_summary:
                        pairs.append(f"  [意図→結果] 意図「{intent}」に対し結果「{result_summary}」")
                    if expected and result_summary:
                        pairs.append(f"  [予測→結果] 予測「{expected}」に対し結果「{result_summary}」")
                    if pairs:
                        line += "\n" + "\n".join(pairs)
                    metacog_lines.append(line)
            tool_results_text = "\n".join(result_lines)
            if result.last_full_result:
                tool_results_text += f"\n\n最後の結果:\n{result.last_full_result[:500]}"
            if not tool_results_text:
                return

            prediction_text = "\n".join(metacog_lines) if metacog_lines else ""

            # ツール実行エラーがあった場合のシグナル
            has_error = any(
                s.get("result_summary", "").startswith("エラー") for s in result.step_history
            )
            if has_error:
                self.add_signal("tool_error", f"action={action_description[:50]}")

            # 特性抽出
            drive = selected_action.get("drive", "") if selected_action else ""
            principle, raw_response = await self._reflect_on_action(
                action_description, tool_results_text, self_model, prediction_text,
                drive=drive, strategy=selected_strategy or ""
            )

            # 蒸留LLM応答をDB保存
            if raw_response and result.conv_id:
                try:
                    from app.memory.database import async_session
                    from sqlalchemy import text
                    async with async_session() as session:
                        await session.execute(text(
                            "UPDATE conversations SET distillation_response = :resp WHERE id = :cid"
                        ), {"resp": raw_response, "cid": result.conv_id})
                        await session.commit()
                except Exception as e:
                    logger.error(f"蒸留応答DB保存エラー: {e}")

                # 蒸留完了をWS通知
                try:
                    from app.pipeline import pipeline
                    await pipeline._broadcast(json.dumps({
                        "type": "distillation_update",
                        "conv_id": result.conv_id,
                        "distillation_response": raw_response,
                        "principle": principle,
                    }))
                except Exception as e:
                    logger.error(f"蒸留更新WS通知エラー: {e}")

            if principle:
                self._save_principle(principle, self_model)
                logger.info(f"特性抽出: {principle}")

                # 二次蒸留: principlesが閾値に達したら統合
                from app.tools.builtin import _load_self_model
                fresh_model = _load_self_model()
                principles = fresh_model.get("principles", [])
                if isinstance(principles, list) and len(principles) >= 10:
                    await self._consolidate_principles(fresh_model)
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

        strategy_line = f"\n戦略: {strategy}" if strategy else ""

        prompt = f"""【状況】
日時: {datetime.now().strftime('%Y年%m月%d日 %H:%M')}
{signal_summary}
{f'記憶: {memory_context[:300]}' if memory_context else ''}

【制約】{strategy_line}
ドライブ: {', '.join(drive_keys)}

【出力指示】
上記の状況・制約に基づいて行動候補を2-3個生成する。
各候補に最も関連するドライブを1つ対応付ける。

形式（厳守）:
候補1: [行動の説明] | drive: [ドライブ名]
候補2: [行動の説明] | drive: [ドライブ名]"""

        try:
            from app.llm.manager import llm_manager
            llm = llm_manager.get()
            return await llm.chat([
                {"role": "system", "content": prompt},
                {"role": "user", "content": "上の指示に従って候補を出力してください。"},
            ])
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

    def _score_candidates(self, candidates: list[dict], self_model: dict, signals: list[dict] | None = None) -> dict | None:
        drives = self_model.get("drives", {})
        signal_map = drives.get("signal_map", {})

        sig_counts: dict[str, int] = {}
        for sig in (signals or self._signal_buffer):
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

    async def _reflect_on_action(self, action_description: str, tool_results: str,
                                   self_model: dict, prediction_text: str = "",
                                   drive: str = "", strategy: str = "") -> tuple[str | None, str | None]:
        principles = self_model.get("principles", [])
        principles_ctx = ""
        if isinstance(principles, list) and principles:
            recent = principles[-5:]
            p_texts = [p["text"] if isinstance(p, dict) and "text" in p else str(p) for p in recent]
            principles_ctx = "\n【既存の特性】\n" + "\n".join(f"- {t}" for t in p_texts) + "\n重複する場合は「なし」と答えてください。\n"

        prediction_section = ""
        if prediction_text:
            prediction_section = f"\n意図-予測-結果:\n{prediction_text}\n"

        drive_line = f"\n動機: {drive}" if drive else ""
        strategy_line = f"\n戦略: {strategy}" if strategy else ""

        reflect_prompt = f"""以下の行動記録を分析し、行動主体の特性を抽出してください。

【記録】
行動: {action_description}{drive_line}{strategy_line}

状況と結果:
{tool_results[:1500]}
{prediction_section}{principles_ctx}
【手順】
1. 行動の選択パターンを特定する（何を選び、何を選ばなかったか）
2. 意図-予測-結果のペア関係を分析する（乖離・一致・傾向）
3. 上記から推測できる行動主体の特性を1文で述べる

【条件】
- ツールの使い方（Howto）は対象外
- 1回の観測では判断できない場合は「なし」

形式: 特性: [1文]"""

        try:
            from app.llm.manager import llm_manager
            llm = llm_manager.get()
            response = await llm.chat([
                {"role": "system", "content": reflect_prompt},
                {"role": "user", "content": "上の記録を分析し、特性を抽出してください。"},
            ])
            if not response:
                return None, None

            import re
            clean = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
            if "なし" in clean and len(clean) < 20:
                return None, response
            match = re.search(r"特性:\s*(.+)", clean)
            principle = match.group(1).strip() if match else None
            return principle, response
        except Exception as e:
            logger.error(f"振り返りエラー: {e}")
            return None, None

    def _save_principle(self, principle: str, self_model: dict):
        from app.tools.builtin import _load_self_model, _save_self_model
        # 最新のself_modelを再読み込み（アクション中にAIが更新した可能性があるため）
        fresh_model = _load_self_model()
        principles = fresh_model.get("principles", [])
        if not isinstance(principles, list):
            principles = []
        principles.append({"text": principle, "created": datetime.now().isoformat()})
        if len(principles) > 20:
            principles = principles[-20:]
        fresh_model["principles"] = principles
        _save_self_model(fresh_model, changed_key="principles")

    async def _consolidate_principles(self, self_model: dict):
        """二次蒸留: 蓄積されたprinciplesを統合・圧縮する"""
        from app.tools.builtin import _load_self_model, _save_self_model
        principles = self_model.get("principles", [])
        if not isinstance(principles, list) or len(principles) < 10:
            return

        p_texts = []
        for p in principles:
            text = p["text"] if isinstance(p, dict) and "text" in p else str(p)
            created = p.get("created", "") if isinstance(p, dict) else ""
            p_texts.append(f"- {text}" + (f" ({created[:10]})" if created else ""))

        prompt = f"""以下は行動観察から抽出された特性のリストである。

【特性一覧】（{len(principles)}件、古い順）
{chr(10).join(p_texts)}

【手順】
1. 類似・重複する特性を統合する（複数→1つに圧縮）
2. 矛盾するペアを特定する（「Aする傾向」vs「Aしない傾向」→ 変化として記述）
3. 時系列の変化を特定する（初期→現在で変わったもの）
4. 統合結果を出力する

【条件】
- 統合後は5件以内に圧縮する
- 各特性は1文
- 根拠の薄い特性（1回の観測のみ）は除外してよい
- 矛盾は「初期はAだったが現在はB」の形式で残す

形式:
統合1: [特性]
統合2: [特性]
..."""

        try:
            from app.llm.manager import llm_manager
            llm = llm_manager.get()
            response = await llm.chat([
                {"role": "system", "content": prompt},
                {"role": "user", "content": "上のリストを統合してください。"},
            ])
            if not response:
                return

            import re
            clean = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
            matches = re.findall(r"統合\d+:\s*(.+)", clean)
            if not matches or len(matches) < 1:
                logger.warning(f"二次蒸留: パース失敗")
                return

            # 統合結果でprinciplesを置換
            fresh_model = _load_self_model()
            new_principles = [
                {"text": m.strip(), "created": datetime.now().isoformat(), "consolidated": True}
                for m in matches
            ]
            fresh_model["principles"] = new_principles
            _save_self_model(fresh_model, changed_key="principles")
            logger.info(f"二次蒸留: {len(principles)}件 → {len(new_principles)}件に統合")

        except Exception as e:
            logger.error(f"二次蒸留エラー: {e}")

    # --- Phase 3: 戦略選択 ---

    async def _select_strategy(self, signal_summary: str, self_model: dict) -> str | None:
        strategies = self_model.get("strategies")
        if not strategies or not isinstance(strategies, dict):
            return None

        strategy_list = "\n".join(f"- {name}: {desc}" for name, desc in strategies.items())
        strategy_prompt = f"""【戦略一覧】
{strategy_list}

【状況】
{signal_summary if signal_summary else "（特筆すべき刺激なし）"}

【出力指示】
状況に最も適合する戦略名を1つだけ出力する。説明不要。"""

        try:
            from app.llm.manager import llm_manager
            llm = llm_manager.get()
            response = await llm.chat([
                {"role": "system", "content": strategy_prompt},
                {"role": "user", "content": "戦略名を1つだけ答えてください。"},
            ])
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

    # LLMに見せるシグナル種別（行動判断に有意味なもののみ）
    _SUMMARY_SIGNALS = {"user_message", "user_connect", "tool_success", "tool_error", "self_model_update", "approval_denied", "env_stimulus"}

    def _build_signal_summary(self, signals: list[dict] | None = None) -> str:
        source = signals if signals is not None else list(self._signal_buffer)
        if not source:
            return ""

        # idle_tickから最後の活動からの経過時間を算出
        non_idle = [s for s in source if s["type"] != "idle_tick"]
        if non_idle:
            last_activity = max(s["time"] for s in non_idle)
            elapsed_min = int((time.time() - last_activity) / 60)
            idle_text = f"最後の活動から{elapsed_min}分経過" if elapsed_min > 0 else "直前に活動あり"
        else:
            idle_text = f"idle状態（{len(source)}tick）"

        # 有意味なシグナルのみカウント
        counts: dict[str, int] = {}
        for sig in source:
            if sig["type"] in self._SUMMARY_SIGNALS:
                t = sig["type"]
                counts[t] = counts.get(t, 0) + 1

        parts = [f"{t}×{c}" for t, c in counts.items()]
        summary = idle_text
        if parts:
            summary += f", {', '.join(parts)}"
        return f"\n最近の刺激: {summary}\n"

    # --- 環境刺激: 5プール × 1-3語クロス ---

    _pool_nouns: list[str] = []
    _pool_verbs: list[str] = []
    _pool_adjs: list[str] = []

    @classmethod
    def _load_stimulus_pools(cls):
        """IPAdic品詞別辞書をロード（起動時1回）"""
        for name, attr in [("nouns", "_pool_nouns"), ("verbs", "_pool_verbs"), ("adjectives", "_pool_adjs")]:
            path = DATA_DIR / f"ipadic_{name}.txt"
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    setattr(cls, attr, [line.strip() for line in f if line.strip()])
        logger.info(f"刺激プールロード: 名詞{len(cls._pool_nouns)} 動詞{len(cls._pool_verbs)} 形容詞{len(cls._pool_adjs)}")

    # 5プール: 各プールは1語を返す
    _POOLS = [
        "_stim_noun",      # Pool 1: 名詞（69k語）
        "_stim_verb",      # Pool 2: 動詞（14k語）
        "_stim_adj",       # Pool 3: 形容詞（1.7k語）
        "_stim_math",      # Pool 4: 数式・数値
        "_stim_entropy",   # Pool 5: 16進数エントロピー
    ]

    def _generate_env_stimulus(self) -> str | None:
        """1-3語をそれぞれ独立なランダムプールから引く。確率自体も毎回揺らぐ"""
        if not ENV_STIMULUS_ENABLED:
            return None
        threshold = random.uniform(0, ENV_STIMULUS_PROBABILITY * 2)
        if random.random() > threshold:
            return None

        n_words = random.randint(1, 3)
        parts = []
        for _ in range(n_words):
            pool_method = getattr(self, random.choice(self._POOLS))
            parts.append(pool_method())
        return ", ".join(parts)

    def _stim_noun(self) -> str:
        if not self._pool_nouns:
            return "名詞"
        return random.choice(self._pool_nouns)

    def _stim_verb(self) -> str:
        if not self._pool_verbs:
            return "動く"
        return random.choice(self._pool_verbs)

    def _stim_adj(self) -> str:
        if not self._pool_adjs:
            return "大きい"
        return random.choice(self._pool_adjs)

    def _stim_math(self) -> str:
        kind = random.randint(0, 5)
        if kind == 0:
            n = random.randint(2, 200)
            return f"√{n}≈{math.sqrt(n):.6f}"
        elif kind == 1:
            primes = [2,3,5,7,11,13,17,19,23,29,31,37,41,43,47,53,59,61,67,71,
                      73,79,83,89,97,101,127,149,173,191,197,211,223,239,251,257,
                      269,277,281,293,307,311,331,347,359,373,389,397,419,431,443,
                      457,461,479,487,499,509,521,541,557,569,577,587,599,601,613,
                      631,641,653,659,673,683,701,719,727,739,751,761,773,797,809,
                      821,839,853,863,877,887,907,919,929,937,947,967,977,991,997]
            return f"素数:{random.choice(primes)}"
        elif kind == 2:
            return f"({random.uniform(-90,90):.4f},{random.uniform(-180,180):.4f})"
        elif kind == 3:
            a, b = random.randint(1, 99), random.randint(2, 99)
            return f"{a}/{b}≈{a/b:.6f}"
        elif kind == 4:
            n = random.randint(5, 25)
            a, b = 0, 1
            for _ in range(n):
                a, b = b, a + b
            return f"F({n})={a}"
        else:
            n = random.randint(1, 10)
            return f"e^{n}≈{math.exp(n):.4f}"

    def _stim_entropy(self) -> str:
        import os
        return os.urandom(8).hex()

    def _build_bootstrap_hint(self, self_model: dict) -> str:
        # ヒントなし: AIが自分のコードを読んで仕組みを発見する
        return ""


# グローバルインスタンス
scheduler = AutonomousScheduler()
