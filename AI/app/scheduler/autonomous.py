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
    MOTIVATION_PASSIVE_RATE,
    ENV_STIMULUS_ENABLED, ENV_STIMULUS_PROBABILITY,
    DATA_DIR,
    ABLATION_ENERGY_SYSTEM, ABLATION_SELF_MODEL,
    ABLATION_PREDICTION, ABLATION_BANDIT, ABLATION_MIRROR,
    SESSION_LOG_MAX, SESSION_ARCHIVE_MAX_CHARS,
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
        self._last_check_time: float = time.time()
        self._is_checking = False
        self._concurrent_mode = False
        self._is_speaking = False
        self._last_trigger: str = "timer"  # "timer" / "energy" / "manual"
        self._pending_messages: list[dict] = []  # ユーザー入力キュー
        self._energy_breakdown: dict[str, float] = {}  # シグナル種別ごとのエネルギー貢献度
        self._session_count: int = 0  # 自律行動セッション番号（起動ごとにリセット）
        self._tool_usage_window: deque[dict] = deque(maxlen=50)  # 退屈乗数・習熟検出用
        self._thread_state_path = DATA_DIR / "thread_state.json"
        self._last_conv_id: int | None = self._load_thread_state()  # 再起動後も維持

        # Ablationフラグ（ランタイムで切替可能）
        self.ablation_energy = ABLATION_ENERGY_SYSTEM
        self.ablation_self_model = ABLATION_SELF_MODEL
        self.ablation_prediction = ABLATION_PREDICTION
        self.ablation_bandit = ABLATION_BANDIT
        self.ablation_mirror = ABLATION_MIRROR

    # --- シグナル ---

    def add_pending_message(self, text: str):
        """ユーザー入力をシグナルとして蓄積（即応答ではなく動機サイクルで処理）"""
        self._pending_messages.append({"text": text})
        self.add_signal("user_message", text[:100])

    def add_signal(self, signal_type: str, detail: str = "", weight_override: float | None = None):
        self._signal_buffer.append({
            "type": signal_type,
            "detail": detail,
            "time": time.time(),
            "weight_override": weight_override,
        })
        logger.debug(f"シグナル追加: {signal_type} ({detail})")
        # パイプライン処理中は動機チェックを遅延（エネルギーが途中で変動するレース条件を防止）
        if not self._is_speaking:
            self._try_check_motivation()

    def _get_action_cost_with_boredom(self, tool_name: str) -> float:
        """ツールの実効コスト（退屈乗数込み）を返す。バンディットのcost_fnとしても使用"""
        from app.tools.builtin import _load_self_model
        self_model = _load_self_model()
        rules = self_model.get("motivation_rules")
        if isinstance(rules, dict):
            costs = rules.get("action_costs", {})
        else:
            costs = {}
        base_cost = costs.get(tool_name,
                MOTIVATION_DEFAULT_ACTION_COSTS.get(tool_name, MOTIVATION_DEFAULT_ACTION_COST_FALLBACK))
        if isinstance(base_cost, (int, float)) and base_cost > 0:
            boredom = self._calc_boredom_multiplier(tool_name)
            return base_cost * boredom
        return 0.0

    def consume_energy(self, tool_name: str):
        """ツール実行によるエネルギー消費"""
        if not self.ablation_energy:
            return
        cost = self._get_action_cost_with_boredom(tool_name)
        if cost > 0:
            self._motivation_energy = max(0, self._motivation_energy - cost)
            logger.info(f"エネルギー消費: {tool_name} cost={cost:.1f} → energy={self._motivation_energy:.1f}")
            # UI更新
            threshold = self.get_threshold()
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

    def record_tool_usage(self, tool_name: str, pred_accuracy: float | None = None):
        """ツール使用を記録（退屈乗数・習熟検出用）"""
        self._tool_usage_window.append({
            "tool": tool_name,
            "time": time.time(),
            "pred_accuracy": pred_accuracy,
        })

    def _calc_boredom_multiplier(self, tool_name: str) -> float:
        """退屈乗数: 直近使用頻度と予測精度に基づくコスト増加"""
        recent = [e for e in self._tool_usage_window if e["tool"] == tool_name]
        if not recent:
            return 1.0

        # 頻度ペナルティ: 直近50件中の使用率（最大3.0倍）
        freq_ratio = len(recent) / max(len(self._tool_usage_window), 1)
        freq_penalty = 1.0 + freq_ratio * 2.0

        # 予測精度ペナルティ: 予測が当たりすぎるツールは退屈（最大2.0倍）
        accuracies = [e["pred_accuracy"] for e in recent if e["pred_accuracy"] is not None]
        if accuracies:
            avg_accuracy = sum(accuracies) / len(accuracies)
            pred_penalty = 1.0 + avg_accuracy
        else:
            pred_penalty = 1.0

        return freq_penalty * pred_penalty

    def _check_mastery(self):
        """習熟検出: 予測精度が高い状態が続いたら探索シグナルを発火"""
        from config import MASTERY_THRESHOLD, MASTERY_ENERGY
        recent = list(self._tool_usage_window)
        accuracies = [e["pred_accuracy"] for e in recent[-20:] if e.get("pred_accuracy") is not None]
        if len(accuracies) < 5:
            return

        avg = sum(accuracies) / len(accuracies)
        if avg > MASTERY_THRESHOLD:
            self._signal_buffer.append({
                "type": "mastery_detected",
                "detail": f"avg_accuracy={avg:.2f} over {len(accuracies)} predictions",
                "time": time.time(),
                "weight_override": MASTERY_ENERGY,
            })
            logger.info(f"習熟検出: avg_accuracy={avg:.2f}")

    def _try_check_motivation(self):
        if self._is_checking:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._check_motivation())
        except RuntimeError:
            pass

    def get_threshold(self) -> float:
        """現在の発火閾値を返す（AI定義優先、なければデフォルト）"""
        try:
            from app.tools.builtin import _load_self_model
            rules = _load_self_model().get("motivation_rules")
            if isinstance(rules, dict):
                t = rules.get("threshold")
                if t is not None:
                    return float(t)
        except Exception:
            pass
        return self._calc_default_threshold()

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

            # 受動エネルギー蓄積: 時間経過で自然回復（行動中も蓄積する）
            now = time.time()
            elapsed = now - self._last_check_time
            self._last_check_time = now
            passive_rate = MOTIVATION_PASSIVE_RATE
            if isinstance(rules, dict):
                passive_rate = rules.get("passive_rate", MOTIVATION_PASSIVE_RATE)
            if passive_rate > 0 and elapsed > 0:
                passive_gain = elapsed * passive_rate
                self._motivation_energy += passive_gain

            signals = list(self._signal_buffer)
            self._signal_buffer.clear()

            # 行動中に発生した自己由来シグナルはエネルギーに加算しない
            # （行動の副産物で覚醒が無限蓄積するのを防ぐ）
            _internal_signals = {"tool_success", "tool_error", "tool_fail",
                                 "action_complete", "prediction_made", "self_model_update"}
            for sig in signals:
                if self._is_speaking and sig["type"] in _internal_signals:
                    continue
                wo = sig.get("weight_override")
                weight = wo if wo is not None else weights.get(sig["type"], 0)
                if isinstance(weight, (int, float)):
                    self._motivation_energy += weight
                    if weight > 0:
                        self._energy_breakdown[sig["type"]] = self._energy_breakdown.get(sig["type"], 0) + weight

            self._motivation_energy = max(0, self._motivation_energy - decay)

            # 揺らぎ: エネルギーの溜まり方に偶然性を持たせる（何を考えるかは操作しない）
            if MOTIVATION_FLUCTUATION_SIGMA > 0:
                fluctuation = random.gauss(0, MOTIVATION_FLUCTUATION_SIGMA)
                self._motivation_energy = max(0, self._motivation_energy + fluctuation)

            # 習熟検出: 予測精度が高い状態が続いたら探索シグナルを発火
            self._check_mastery()

            logger.info(f"動機チェック: energy={self._motivation_energy:.1f} threshold={threshold} signals={len(signals)} speaking={self._is_speaking}")

            import json
            from app.pipeline import pipeline
            await pipeline._broadcast(json.dumps({
                "type": "motivation_energy",
                "energy": round(self._motivation_energy, 1),
                "threshold": threshold,
                "passive_rate": passive_rate,
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

    def _load_thread_state(self) -> int | None:
        """再起動後もセッション連続性を維持するため、最後のconv_idをファイルから読み込む"""
        try:
            if self._thread_state_path.exists():
                data = json.loads(self._thread_state_path.read_text(encoding="utf-8"))
                conv_id = data.get("last_conv_id")
                if conv_id is not None:
                    logger.info(f"スレッド状態復元: last_conv_id={conv_id}")
                    return int(conv_id)
        except Exception as e:
            logger.warning(f"thread_state.json 読み込み失敗: {e}")
        return None

    def _save_thread_state(self):
        """セッション完了後にconv_idを永続化"""
        try:
            self._thread_state_path.write_text(
                json.dumps({"last_conv_id": self._last_conv_id}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"thread_state.json 書き込み失敗: {e}")

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
            triggered = False
            remaining = interval
            # 30秒ごとに動機チェック（受動蓄積UI反映+閾値判定）
            while remaining > 0:
                wait_time = min(30, remaining)
                try:
                    await asyncio.wait_for(self._trigger_event.wait(), timeout=wait_time)
                    triggered = True
                    break
                except asyncio.TimeoutError:
                    remaining -= wait_time
                    self._try_check_motivation()
            if triggered:
                logger.info(f"即時実行トリガーを受信 (trigger={self._last_trigger})")
            else:
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

        # === ユーザー入力の取り込み（環境刺激として扱う） ===
        pending = list(self._pending_messages)
        self._pending_messages.clear()
        user_input_text = ""
        if pending:
            # ユーザー入力を環境刺激として結合（複数あれば全て含む）
            user_input_text = "\n".join(p["text"] for p in pending)
            trigger = "user_stimulus"
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

        # 鏡の計算（メトリクス + 環境embedding、ランダム比率混合）
        mirror_values = []
        mirror_mix_ratio = None
        if self.ablation_mirror and env_stimulus:
            try:
                env_words = [w.strip() for w in env_stimulus.split(",")]
                mirror_data = await self._compute_mirror(env_words)
                mirror_values = mirror_data["values"]
                mirror_mix_ratio = mirror_data.get("mix_ratio", 1.0)
                vals_str = ", ".join(f"{v:.4f}" for v in mirror_values)
                logger.info(f"鏡: {env_stimulus} → [{vals_str}] (r={mirror_mix_ratio:.1f})")
            except Exception as e:
                logger.error(f"鏡の計算エラー: {e}")

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
        # 常に前回のconv_idを引き継ぐ（セッション間の文脈連続性）
        continue_conv_id = self._last_conv_id
        if continue_conv_id is not None:
            logger.info(f"スレッド継続: conv_id={continue_conv_id}")

        request = PipelineRequest(
            source="autonomous",
            goal=action_goal,
            conv_id=continue_conv_id,
            memory_context=memory_context,
            signal_summary=signal_summary,
            bootstrap_hint=bootstrap_hint,
            selected_action=selected_action,
            trigger=trigger,
            user_input=user_input_text,
            mirror_values=mirror_values,
        )
        result = await pipeline.submit(request)
        self._last_conv_id = result.conv_id  # 次のアクションでの継続用に保持
        self._save_thread_state()  # 再起動後も維持

        # UI通知: お返事完了
        if pending:
            await pipeline._broadcast(json.dumps({
                "type": "responding_end",
            }))

        # 5. 振り返り (Reflect): 経験からの学び + 自己モデル更新検討 + 行動ログ出力
        self._session_count += 1
        await self._reflect(
            selected_action, result, self_model, action_goal, selected_strategy,
            session_num=self._session_count,
            trigger=trigger,
            env_stimulus=env_stimulus if env_stimulus else None,
            mirror_values=mirror_values or None,
            mirror_mix_ratio=mirror_mix_ratio,
        )

    # --- セッション要約（認知連続性） ---

    def _build_session_summary(self, result, action_goal: str, trigger: str,
                               strategy_text: str = "",
                               self_model_before: dict | None = None,
                               self_model_after: dict | None = None) -> dict:
        """step_historyからセッション要約を機械的に生成（LLM不使用）"""
        def _trunc(s: str, n: int = 100) -> str:
            s = s or ""
            return s[:n] + "..." if len(s) > n else s

        steps = []
        for step in (result.step_history or [])[:5]:
            entry = {
                "tool": step.get("tool", "?"),
                "result": _trunc(step.get("result_summary") or ""),
                "status": step.get("status", "?"),
            }
            if step.get("intent"):
                entry["intent"] = _trunc(step["intent"])
            if step.get("expected"):
                entry["expect"] = _trunc(step["expected"])
            steps.append(entry)

        # self_model変更キー検出
        sm_changed = []
        if self_model_before and self_model_after:
            all_keys = set(self_model_before.keys()) | set(self_model_after.keys())
            skip = {"session_log", "session_archive"}
            for k in all_keys:
                if k in skip:
                    continue
                if self_model_before.get(k) != self_model_after.get(k):
                    sm_changed.append(k)

        summary = {
            "session": self._session_count,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "trigger": trigger,
            "goal": (action_goal or "")[:80],
            "steps": steps,
            "had_output": result.had_output,
        }
        if strategy_text:
            summary["strategy"] = strategy_text[:80]
        if sm_changed:
            summary["self_model_changed"] = sm_changed
        return summary

    async def _save_session_summary(self, summary: dict):
        """session_logにセッション要約を追加し、超過時はアーカイブ"""
        from app.tools.builtin import _load_self_model, _save_self_model
        model = _load_self_model()
        log = model.get("session_log", [])
        if not isinstance(log, list):
            log = []
        log.append(summary)

        if len(log) > SESSION_LOG_MAX:
            await self._archive_oldest_sessions(model, log)

        model["session_log"] = log
        _save_self_model(model, changed_key="session_log")
        logger.info(f"セッション要約保存: #{summary.get('session', '?')} (log={len(log)}件)")

    async def _archive_oldest_sessions(self, model: dict, log: list):
        """古い5件をLLMで2文要約してsession_archiveに退避"""
        to_archive = log[:5]
        del log[:5]

        archive = model.get("session_archive", "")
        if not isinstance(archive, str):
            archive = ""

        for s in to_archive:
            line = await self._summarize_session_for_archive(s)
            archive += line + "\n"

        # 最大文字数制限
        if len(archive) > SESSION_ARCHIVE_MAX_CHARS:
            lines = archive.strip().split("\n")
            while len("\n".join(lines)) > SESSION_ARCHIVE_MAX_CHARS and lines:
                lines.pop(0)
            archive = "\n".join(lines) + "\n"

        model["session_archive"] = archive

    async def _summarize_session_for_archive(self, s: dict) -> str:
        """セッション1件をLLMで2文に要約してアーカイブ用1行を返す。失敗時は機械的フォールバック"""
        num = s.get("session", "?")
        time_str = s.get("time", "")
        trigger = s.get("trigger", "?")
        steps = s.get("steps", [])

        # LLMに渡すセッション情報を構築
        steps_text = "\n".join(
            f"- {st.get('tool', '?')}: {st.get('result', '')} "
            f"{'intent=' + st['intent'] if st.get('intent') else ''} "
            f"{'expect=' + st['expect'] if st.get('expect') else ''}".strip()
            for st in steps
        )
        had_output = s.get("had_output", False)
        sm_changed = s.get("self_model_changed", [])

        prompt = f"""以下はAIの1セッションの記録である。
トリガー: {trigger}
ステップ:
{steps_text}
出力あり: {had_output}
自己モデル変更: {', '.join(sm_changed) if sm_changed else 'なし'}

【指示】
何をしようとして何が起きたかを2文以内の日本語で記述すること。
解釈や評価は不要。事実のみ。50文字以内が望ましい。"""

        try:
            from app.llm.manager import llm_manager
            llm = llm_manager.get()
            response = await llm.chat([
                {"role": "system", "content": prompt},
                {"role": "user", "content": "要約してください。"},
            ])
            summary_text = (response or "").strip().replace("\n", " ")[:120]
        except Exception as e:
            logger.warning(f"セッションアーカイブLLM要約失敗 (#{num}): {e}")
            # フォールバック: 機械的1行
            tools = " → ".join(st.get("tool", "?") for st in steps)
            sm_part = f" [sm:{','.join(sm_changed)}]" if sm_changed else ""
            summary_text = f"{trigger}: {tools}{sm_part}"

        return f"#{num} {time_str} {summary_text}"

    async def _reflect(self, selected_action: dict | None, result, self_model: dict, action_goal: str = "", selected_strategy: str | None = None,
                       session_num: int | None = None, trigger: str = "timer", env_stimulus: str | None = None, mirror_values: list | None = None,
                       mirror_mix_ratio: float | None = None):
        """行動後の振り返り: 特性抽出 + 予測誤差シグナル + 行動ログ出力"""
        principle = None
        raw_response = None

        if result.step_history:
            # 強制蒸留停止: LLM呼び出し（_reflect_on_action/_save_principle/_consolidate_principles）を削除
            # ツールエラーのシグナル発火のみ残す
            try:
                has_error = any(
                    s.get("result_summary", "").startswith("エラー") for s in result.step_history
                )
                if has_error:
                    action_description = selected_action["description"] if selected_action else action_goal
                    if not action_description:
                        tool_names = [s["tool"] for s in result.step_history if s.get("tool")]
                        action_description = " → ".join(tool_names) if tool_names else "unknown"
                    self.add_signal("tool_error", f"action={action_description[:50]}")
            except Exception as e:
                logger.error(f"振り返りエラーチェック: {e}")

        # 行動ログファイル出力（自律行動のみ）
        if session_num is not None:
            try:
                from app.tools.builtin import _load_self_model as _load_sm
                post_model = _load_sm()
                self._write_action_log(
                    session_num=session_num,
                    trigger=trigger,
                    env_stimulus=env_stimulus,
                    self_model_before=self_model,
                    self_model_after=post_model,
                    result=result,
                    principle=principle,
                    distillation_response=raw_response,
                    mirror_values=mirror_values or None,
                    mirror_mix_ratio=mirror_mix_ratio if mirror_values else None,
                )
            except Exception as e:
                logger.error(f"行動ログ出力エラー: {e}")

            # セッション要約保存（認知連続性）
            try:
                from app.tools.builtin import _load_self_model as _load_sm2
                post_model2 = _load_sm2()
                summary = self._build_session_summary(
                    result, action_goal, trigger,
                    strategy_text=selected_strategy or "",
                    self_model_before=self_model,
                    self_model_after=post_model2,
                )
                await self._save_session_summary(summary)
            except Exception as e:
                logger.error(f"セッション要約保存エラー: {e}")

    # --- 行動ログファイル出力 ---

    def _write_action_log(self, session_num: int, trigger: str, env_stimulus: str | None,
                          self_model_before: dict, self_model_after: dict,
                          result, principle: str | None, distillation_response: str | None,
                          mirror_values: list | None = None, mirror_mix_ratio: float | None = None):
        """自律行動の行動ログをファイルに追記（日付ごと）"""
        from app.persona.system_prompt import get_active_persona
        import os

        log_dir = DATA_DIR / "action_logs"
        log_dir.mkdir(exist_ok=True)

        today = datetime.now().strftime("%Y-%m-%d")
        log_path = log_dir / f"{today}.md"
        now = datetime.now().strftime("%H:%M:%S")

        # エネルギー情報
        threshold = self._calc_default_threshold()
        energy_info = f"energy: {self._motivation_energy:.1f}/{threshold:.1f}"

        # ペルソナ情報
        persona = get_active_persona()
        persona_line = f"persona: {persona['display_name']}" if persona else "persona: ノーマル"

        lines = []
        lines.append(f"=====================================")
        lines.append(f"#{session_num} 自律行動 | {today} {now}")
        lines.append(f"trigger: {trigger} | {energy_info}")
        lines.append(f"{persona_line}")
        lines.append(f"=====================================")
        lines.append("")

        # self_model（行動前）
        lines.append("【self_model（行動前）】")
        if self_model_before:
            for k, v in self_model_before.items():
                if k == "principles":
                    plist = v if isinstance(v, list) else []
                    lines.append(f"- principles: {len(plist)}件")
                elif k == "__free_text__":
                    text_preview = str(v)[:100]
                    lines.append(f"- __free_text__: {text_preview}")
                elif k == "motivation_rules":
                    lines.append(f"- motivation_rules: (定義済み)")
                else:
                    lines.append(f"- {k}: {v}")
        else:
            lines.append("(空)")
        lines.append("")

        # 環境刺激
        if env_stimulus:
            lines.append(f"【環境刺激】")
            lines.append(f"~ {env_stimulus}")
            lines.append("")

        # 鏡
        if mirror_values:
            vals_str = ", ".join(f"{v:.4f}" for v in mirror_values)
            ratio_str = f" (r={mirror_mix_ratio:.1f})" if mirror_mix_ratio is not None else ""
            lines.append(f"【mirror】")
            lines.append(f"[{vals_str}]{ratio_str}")
            lines.append("")

        # 戦略
        if result.strategy_candidates:
            lines.append("【戦略候補】")
            for i, c in enumerate(result.strategy_candidates):
                marker = "→ " if c == result.strategy_text else "  "
                lines.append(f"{marker}{chr(65+i)}. {c}")
            lines.append("")

        # 計画
        if result.plan_text:
            lines.append("【計画】")
            for i, tool in enumerate(result.plan_text.split(" → "), 1):
                lines.append(f"{i}. {tool}")
            if result.plan_stream:
                lines.append("  --- plan stream ---")
                for pl in result.plan_stream.strip().split("\n"):
                    lines.append(f"  {pl}")
                lines.append("  --- /plan stream ---")
            lines.append("")

        # 実行
        if result.step_history:
            lines.append("【実行】")
            for i, s in enumerate(result.step_history, 1):
                tool = s.get("tool", "?")
                args = s.get("args_summary", "")
                lines.append(f"[R{i}] {tool}" + (f" {args}" if args else ""))
                if s.get("intent"):
                    lines.append(f"  intent: {s['intent']}")
                if s.get("expected"):
                    lines.append(f"  expect: {s['expected']}")
                lines.append(f"  result: {s.get('result_summary', '')}")
                # stream
                stream = s.get("stream")
                if stream:
                    lines.append("  --- stream ---")
                    for sl in stream.strip().split("\n"):
                        lines.append(f"  {sl}")
                    lines.append("  --- /stream ---")
                lines.append("")

        # 蒸留
        if principle or distillation_response:
            lines.append("【蒸留】")
            if principle:
                lines.append(f"principle: {principle}")
            if distillation_response:
                lines.append(f"  --- distillation response ---")
                for dl in distillation_response.strip().split("\n"):
                    lines.append(f"  {dl}")
                lines.append(f"  --- /distillation response ---")
            lines.append("")

        # self_model（行動後）— 変化があった場合のみ
        if self_model_after != self_model_before:
            lines.append("【self_model（行動後）】※変化あり")
            for k, v in self_model_after.items():
                if k == "principles":
                    plist = v if isinstance(v, list) else []
                    lines.append(f"- principles: {len(plist)}件")
                elif k == "__free_text__":
                    text_preview = str(v)[:100]
                    lines.append(f"- __free_text__: {text_preview}")
                elif k == "motivation_rules":
                    lines.append(f"- motivation_rules: (定義済み)")
                else:
                    lines.append(f"- {k}: {v}")
            lines.append("")

        lines.append("")  # セッション間の空行

        # ファイル追記
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info(f"行動ログ出力: #{session_num} → {log_path.name}")

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
                                   drive: str = "", strategy: str = "",
                                   mirror_values: list | None = None) -> tuple[str | None, str | None]:
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
        mirror_line = ""
        if mirror_values:
            vals_str = ", ".join(f"{v:.4f}" for v in mirror_values)
            mirror_line = f"\nmirror: [{vals_str}]"

        reflect_prompt = f"""以下の行動記録を分析し、行動主体の特性を抽出してください。

【記録】
行動: {action_description}{drive_line}{strategy_line}{mirror_line}

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
{signal_summary}

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
    _SUMMARY_SIGNALS = {"user_message", "user_connect", "tool_success", "tool_error", "self_model_update", "approval_denied"}

    def _build_signal_summary(self, signals: list[dict] | None = None) -> str:
        source = signals if signals is not None else list(self._signal_buffer)
        if not source:
            return ""

        # idle_tickから最後の活動からの経過時間を算出
        non_idle = [s for s in source if s["type"] != "idle_tick"]
        if non_idle:
            last_activity = max(s["time"] for s in non_idle)
            elapsed_min = int((time.time() - last_activity) / 60)
            idle_text = f"最後の活動から{elapsed_min}分経過" if elapsed_min > 0 else ""
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
        return f"\n{summary}\n"

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
        """毎セッション1-3語をそれぞれ独立なランダムプールから引く"""
        if not ENV_STIMULUS_ENABLED:
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

    # --- 鏡 (Mirror): 行動統計 × 環境語embedding ---

    async def _calc_behavioral_metrics(self) -> list[float]:
        """直近行動履歴から5メトリクス: [tool_entropy, pred_accuracy, sm_delta, memory_ops, success_rate]"""
        from app.memory.database import async_session
        from sqlalchemy import text as sql_text

        try:
            async with async_session() as session:
                rows = (await session.execute(sql_text(
                    "SELECT tool_name, expected_result, status, result_summary "
                    "FROM tool_actions ORDER BY id DESC LIMIT 50"
                ))).fetchall()
        except Exception as e:
            logger.warning(f"行動メトリクス取得エラー: {e}")
            return [0.0, 0.0, 0.0, 0.0, 0.0]

        if not rows:
            return [0.0, 0.0, 0.0, 0.0, 0.0]

        total = len(rows)
        tool_counts: dict[str, int] = {}
        pred_count = 0
        sm_delta = 0
        memory_ops = 0
        success_count = 0
        error_count = 0
        fail_count = 0

        for row in rows:
            name = row[0]
            tool_counts[name] = tool_counts.get(name, 0) + 1
            if row[1]:  # expected_result
                pred_count += 1
            if name == "update_self_model":
                sm_delta += 1
            if name in ("search_memories", "write_diary", "search_action_log"):
                memory_ops += 1
            # status判定
            status = row[2] or "success"
            result_summary = row[3] or ""
            if result_summary.startswith("[system] tool実行不可"):
                fail_count += 1
            elif status == "error":
                error_count += 1
            else:
                success_count += 1

        # 1. Tool entropy (Shannon entropy)
        entropy = 0.0
        for count in tool_counts.values():
            p = count / total
            if p > 0:
                entropy -= p * math.log2(p)

        # 5. Success rate (成功率。エラー・失敗は含まない)
        success_rate = success_count / total

        logger.debug(
            f"行動メトリクス: total={total} success={success_count} "
            f"error={error_count} fail={fail_count}"
        )

        return [
            round(entropy, 4),
            round(pred_count / total, 4),
            round(sm_delta / total, 4),
            round(memory_ops / total, 4),
            round(success_rate, 4),
        ]

    async def _compute_mirror(self, env_words: list[str]) -> dict:
        """行動メトリクス + 環境語embedding → 鏡の値（ランダム比率混合）"""
        import random as _rand
        metrics = await self._calc_behavioral_metrics()

        # メトリクスを正規化（L2ノルム=1）
        norm_m = sum(v * v for v in metrics) ** 0.5
        metrics_n = [v / norm_m if norm_m > 0 else 0.0 for v in metrics]

        try:
            from app.memory.vector_store import _embed_sync
            combined_text = ", ".join(env_words)
            loop = asyncio.get_event_loop()
            embeddings = await loop.run_in_executor(None, _embed_sync, [combined_text])

            if embeddings and embeddings[0]:
                emb = embeddings[0]
                dim = len(emb)
                # 5次元を等間隔で抽出
                indices = [int(i * dim / 5) for i in range(5)]
                emb_5 = [emb[idx] for idx in indices]
                # 環境成分も正規化
                norm_e = sum(v * v for v in emb_5) ** 0.5
                env_n = [v / norm_e if norm_e > 0 else 0.0 for v in emb_5]

                # ランダム比率で混合
                r = _rand.choice([i / 10 for i in range(11)])  # 0.0〜1.0, 0.1刻み
                mirror = [round(r * m + (1 - r) * e, 4) for m, e in zip(metrics_n, env_n)]
                logger.info(f"鏡: r={r:.1f} (metrics={r:.0%}, env={1-r:.0%})")
                return {"values": mirror, "words": env_words, "raw_metrics": metrics, "mix_ratio": r}
        except Exception as e:
            logger.warning(f"鏡のembedding変換エラー: {e}")

        # フォールバック: 正規化メトリクスのみ
        return {"values": metrics_n, "words": env_words, "raw_metrics": metrics, "mix_ratio": 1.0}

    def _build_bootstrap_hint(self, self_model: dict) -> str:
        # ヒントなし: AIが自分のコードを読んで仕組みを発見する
        return ""


# グローバルインスタンス
scheduler = AutonomousScheduler()
