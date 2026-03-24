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
    MOTIVATION_DEFAULT_WEIGHTS, MOTIVATION_FLUCTUATION_SIGMA,
    MOTIVATION_SIGNAL_BUFFER_SIZE, SCORING_ENABLED,
    MOTIVATION_DEFAULT_ACTION_COSTS, MOTIVATION_DEFAULT_ACTION_COST_FALLBACK,
    ENV_STIMULUS_ENABLED, ENV_STIMULUS_PROBABILITY, ENV_STIMULUS_WORDS,
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
        self._last_conv_id: int | None = None  # セッション継続用
        self._last_trigger: str = "timer"  # "timer" / "energy" / "manual"

    # --- シグナル ---

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
        from app.tools.builtin import _load_self_model
        self_model = _load_self_model()
        rules = self_model.get("motivation_rules")
        if isinstance(rules, dict):
            costs = rules.get("action_costs", {})
            threshold = rules.get("threshold", MOTIVATION_DEFAULT_THRESHOLD)
        else:
            costs = {}
            threshold = MOTIVATION_DEFAULT_THRESHOLD
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

    async def _check_motivation(self):
        if self._is_checking:
            return
        self._is_checking = True
        try:
            from app.tools.builtin import _load_self_model
            self_model = _load_self_model()
            rules = self_model.get("motivation_rules")
            if isinstance(rules, dict):
                weights = rules.get("weights", {})
                threshold = rules.get("threshold", MOTIVATION_DEFAULT_THRESHOLD)
                decay = rules.get("decay_per_check", MOTIVATION_DEFAULT_DECAY)
            else:
                # 未定義: デフォルトの神経系ウェイトで動く（AIが定義すればそちらが優先）
                weights = MOTIVATION_DEFAULT_WEIGHTS
                threshold = MOTIVATION_DEFAULT_THRESHOLD
                decay = MOTIVATION_DEFAULT_DECAY

            signals = list(self._signal_buffer)
            self._signal_buffer.clear()

            for sig in signals:
                weight = weights.get(sig["type"], 0)
                if isinstance(weight, (int, float)):
                    self._motivation_energy += weight

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

        # === 外側ループ: メタ認知 ===

        # 1. 観測 (Observe): 現在の状態を把握
        # 環境刺激注入（確率的）
        env_stimulus = self._generate_env_stimulus()
        if env_stimulus:
            self.add_signal("env_stimulus", env_stimulus)
            logger.info(f"環境刺激注入: {env_stimulus}")

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

        request = PipelineRequest(
            source="autonomous",
            goal=action_goal,
            conv_id=continue_conv_id,
            memory_context=memory_context,
            signal_summary=signal_summary,
            bootstrap_hint=bootstrap_hint,
            selected_action=selected_action,
            trigger=trigger,
        )
        result = await pipeline.submit(request)
        self._last_conv_id = result.conv_id  # 次のアクションでの継続用に保持

        # 5. 振り返り (Reflect): 経験からの学び + 自己モデル更新検討
        await self._reflect(selected_action, result, self_model, action_goal)

    async def _reflect(self, selected_action: dict | None, result, self_model: dict, action_goal: str = ""):
        """行動後の振り返り: 原則蒸留 + 予測誤差シグナル"""
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
            prediction_lines = []
            for s in result.step_history:
                result_lines.append(f"{s['tool']}: {s['result_summary']}")
                if s.get('expected'):
                    prediction_lines.append(
                        f"{s['tool']}: 予測「{s['expected']}」→ 結果「{s['result_summary']}」"
                    )
            tool_results_text = "\n".join(result_lines)
            if result.last_full_result:
                tool_results_text += f"\n\n最後の結果:\n{result.last_full_result[:500]}"
            if not tool_results_text:
                return

            prediction_text = "\n".join(prediction_lines) if prediction_lines else ""

            # ツール実行エラーがあった場合のシグナル
            has_error = any(
                s.get("result_summary", "").startswith("エラー") for s in result.step_history
            )
            if has_error:
                self.add_signal("tool_error", f"action={action_description[:50]}")

            # 原則蒸留
            principle = await self._reflect_on_action(
                action_description, tool_results_text, self_model, prediction_text
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
候補1: [具体的な行動の説明] | drive: [ドライブ名]
候補2: [具体的な行動の説明] | drive: [ドライブ名]

例: 候補1: Xを調べてYを確認する | drive: Z"""

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
                                   self_model: dict, prediction_text: str = "") -> str | None:
        principles = self_model.get("principles", [])
        principles_ctx = ""
        if isinstance(principles, list) and principles:
            recent = principles[-5:]
            p_texts = [p["text"] if isinstance(p, dict) and "text" in p else str(p) for p in recent]
            principles_ctx = "\n既存の原則:\n" + "\n".join(f"- {t}" for t in p_texts) + "\n既に同じ内容の原則があれば「なし」と答えてください。\n"

        prediction_section = ""
        if prediction_text:
            prediction_section = f"\n予測と実際の比較:\n{prediction_text}\n"

        reflect_prompt = f"""以下の行動と結果から、次の行動に活かせる具体的な原則を1文で蒸留してください。

行動: {action_description}

結果:
{tool_results[:1500]}
{prediction_section}{principles_ctx}
条件:
- 具体的な行動に結びつく粒度で書く（「AするときはBを先に確認する」等）
- 抽象的すぎるもの（「計画は大切」等）は不可
- 新しい学びがなければ「なし」とだけ答える

形式: 原則: [1文]"""

        try:
            from app.llm.manager import llm_manager
            llm = llm_manager.get()
            response = await llm.chat([
                {"role": "system", "content": reflect_prompt},
                {"role": "user", "content": "上の内容を踏まえて原則を蒸留してください。"},
            ])
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

    def _generate_env_stimulus(self) -> str | None:
        """環境刺激をランダム生成。確率的に発火し、多様な観測情報を提供する"""
        if not ENV_STIMULUS_ENABLED:
            return None
        if random.random() > ENV_STIMULUS_PROBABILITY:
            return None

        generators = [
            self._stimulus_random_word,
            self._stimulus_time_pattern,
            self._stimulus_random_file,
            self._stimulus_random_number,
        ]
        return random.choice(generators)()

    def _stimulus_random_word(self) -> str:
        words = random.sample(ENV_STIMULUS_WORDS, min(2, len(ENV_STIMULUS_WORDS)))
        return f"環境観測: {', '.join(words)}"

    def _stimulus_time_pattern(self) -> str:
        now = datetime.now()
        hour = now.hour
        if 5 <= hour < 12:
            period = "朝"
        elif 12 <= hour < 17:
            period = "午後"
        elif 17 <= hour < 21:
            period = "夕方"
        else:
            period = "深夜帯"
        weekday = ["月", "火", "水", "木", "金", "土", "日"][now.weekday()]
        day = now.day
        if day <= 10:
            pos = "月初"
        elif day >= 21:
            pos = "月末"
        else:
            pos = "月中"
        return f"環境観測: {now.strftime('%H:%M')}（{period}、{weekday}曜日、{pos}）"

    def _stimulus_random_file(self) -> str:
        import glob
        from config import BASE_DIR
        py_files = glob.glob(str(BASE_DIR / "app" / "**" / "*.py"), recursive=True)
        if not py_files:
            return "環境観測: プロジェクトファイルなし"
        chosen = random.choice(py_files)
        # BASE_DIR相対パスに変換
        try:
            rel = str(__import__('pathlib').Path(chosen).relative_to(BASE_DIR))
        except ValueError:
            rel = chosen
        return f"環境観測: ファイル {rel} が存在する"

    def _stimulus_random_number(self) -> str:
        kind = random.choice(["pi", "day_of_year", "fibonacci", "random"])
        if kind == "pi":
            import math
            digits = str(math.pi).replace(".", "")
            pos = random.randint(0, min(14, len(digits) - 1))
            return f"環境観測: 円周率の第{pos + 1}桁は{digits[pos]}"
        elif kind == "day_of_year":
            day = datetime.now().timetuple().tm_yday
            return f"環境観測: 今年の{day}日目"
        elif kind == "fibonacci":
            a, b = 0, 1
            n = random.randint(5, 20)
            for _ in range(n):
                a, b = b, a + b
            return f"環境観測: フィボナッチ数列の第{n}項は{a}"
        else:
            return f"環境観測: 乱数 {random.randint(0, 999):03d}"

    def _build_bootstrap_hint(self, self_model: dict) -> str:
        # ヒントなし: AIが自分のコードを読んで仕組みを発見する
        return ""


# グローバルインスタンス
scheduler = AutonomousScheduler()
