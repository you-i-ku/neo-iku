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
    MOTIVATION_SIGNAL_BUFFER_SIZE,
)

logger = logging.getLogger("iku.autonomous")


class AutonomousScheduler:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._websockets: set = set()
        self._running = False
        self._llm_func = None
        self._memory_func = None
        self._next_action_at: float = 0  # time.time() of next action
        self._is_speaking = False
        self._trigger_event = asyncio.Event()
        self._interval = AUTONOMOUS_INTERVAL_MIN
        self._jitter = AUTONOMOUS_INTERVAL_JITTER
        self._skip_speak = False  # interval変更時: ループ再開するがspeakはスキップ

        # --- 内発的動機システム ---
        self._signal_buffer: deque[dict] = deque(maxlen=MOTIVATION_SIGNAL_BUFFER_SIZE)
        self._motivation_energy: float = 0.0
        self._is_checking = False  # 動機チェック中フラグ（再入防止）
        self._concurrent_mode = False  # 会話中でも動機チェックを行うか

    def set_callbacks(self, llm_func, memory_func):
        """LLM呼び出しと記憶取得のコールバックを設定"""
        self._llm_func = llm_func
        self._memory_func = memory_func

    # --- シグナル ---

    def add_signal(self, signal_type: str, detail: str = ""):
        """シグナルをバッファに追加し、動機チェックをトリガー"""
        self._signal_buffer.append({
            "type": signal_type,
            "detail": detail,
            "time": time.time(),
        })
        logger.debug(f"シグナル追加: {signal_type} ({detail})")
        # I/Oイベント駆動: チェックをトリガー
        self._try_check_motivation()

    def _try_check_motivation(self):
        """動機チェックを非同期で起動（再入防止付き）"""
        if self._is_checking:
            return
        if self._is_speaking:
            return
        # 会話中は concurrent_mode がONの場合のみ
        # （会話中かどうかは _is_speaking で判定 — chat側の処理中は別途判定不要）
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._check_motivation())
        except RuntimeError:
            pass  # イベントループがない場合は無視

    async def _check_motivation(self):
        """動機チェック: ルール読み込み→計算→閾値判定（LLMなし）"""
        if self._is_checking or self._is_speaking:
            return
        self._is_checking = True
        try:
            from app.tools.builtin import _load_self_model
            self_model = _load_self_model()
            rules = self_model.get("motivation_rules")

            if not rules:
                # ルール未定義 → エネルギーは蓄積しない（ブートストラップは_loopのタイマーで処理）
                return

            # ルールからパラメータ取得
            weights = rules.get("weights", {})
            threshold = rules.get("threshold", MOTIVATION_DEFAULT_THRESHOLD)
            decay = rules.get("decay_per_check", MOTIVATION_DEFAULT_DECAY)

            # バッファ内のシグナルをスキャン（消費する）
            signals = list(self._signal_buffer)
            self._signal_buffer.clear()

            # エネルギー計算
            for sig in signals:
                sig_type = sig["type"]
                weight = weights.get(sig_type, 0)
                if isinstance(weight, (int, float)):
                    self._motivation_energy += weight

            # 減衰
            self._motivation_energy = max(0, self._motivation_energy - decay)

            logger.info(f"動機チェック: energy={self._motivation_energy:.1f} threshold={threshold} signals={len(signals)}")

            # エネルギー情報をフロントに送信
            import json
            await self._broadcast(json.dumps({
                "type": "motivation_energy",
                "energy": round(self._motivation_energy, 1),
                "threshold": threshold,
            }))

            # 閾値超え → 自律行動発火
            if self._motivation_energy >= threshold:
                logger.info(f"動機発火！ energy={self._motivation_energy:.1f} >= threshold={threshold}")
                self._motivation_energy = 0  # リセット
                self._trigger_event.set()

        except Exception as e:
            logger.error(f"動機チェックエラー: {e}")
        finally:
            self._is_checking = False

    # --- WebSocket管理 ---

    async def register_ws(self, ws):
        self._websockets.add(ws)
        # 接続時に現在のカウントダウンを送信
        remaining = max(0, int(self._next_action_at - time.time()))
        if remaining > 0:
            import json
            try:
                await ws.send_text(json.dumps({
                    "type": "autonomous_countdown",
                    "seconds": remaining
                }))
            except Exception:
                pass

    def unregister_ws(self, ws):
        self._websockets.discard(ws)

    @property
    def connected_count(self) -> int:
        return len(self._websockets)

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
        """自律行動の間隔を変更し、新しい間隔でカウントダウンを再開"""
        self._interval = max(10, seconds)
        self._jitter = max(0, jitter)
        logger.info(f"自律行動間隔変更: {self._interval}秒 (±{self._jitter}秒)")
        # 現在の待機を中断して新しい間隔でループ再開（speakはスキップ）
        self._skip_speak = True
        self._trigger_event.set()

    def trigger_now(self):
        """次の自律行動を即時実行"""
        self._trigger_event.set()

    # --- メインループ ---

    async def _loop(self):
        while self._running:
            interval = self._interval + random.randint(
                -self._jitter, self._jitter
            ) if self._jitter > 0 else self._interval
            interval = max(10, interval)
            logger.info(f"次の自律行動まで {interval}秒")
            self._next_action_at = time.time() + interval
            # カウントダウン情報をフロントに通知
            import json as _json_cd
            await self._broadcast(_json_cd.dumps({
                "type": "autonomous_countdown",
                "seconds": interval
            }))
            # sleepの代わりにevent待ち（trigger_now()やmotivation発火で即時起動可能）
            self._trigger_event.clear()
            try:
                await asyncio.wait_for(self._trigger_event.wait(), timeout=interval)
                logger.info("即時実行トリガーを受信")
            except asyncio.TimeoutError:
                pass  # 通常のタイムアウト = 予定通りの実行

            # interval変更によるループ再開の場合はspeakスキップ
            if self._skip_speak:
                self._skip_speak = False
                continue

            if not self._websockets:
                # タイマー経過時にidle_tickシグナルを追加（接続なしでもシグナルは蓄積）
                self.add_signal("idle_tick")
                continue

            if self._is_speaking:
                logger.warning("前回の自律行動がまだ実行中。スキップ。")
                continue

            # タイマー経過時にidle_tickシグナルを追加
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
        if not self._llm_func or not self._memory_func:
            return

        memories = await self._memory_func()
        memory_context = ""
        if memories:
            memory_context = "\n".join(f"- {m['content']}" for m in memories)

        from app.tools.registry import build_tools_prompt
        from app.tools.builtin import _load_self_model
        tool_text = build_tools_prompt()

        # 自己モデルの読み込み
        self_model = _load_self_model()
        self_model_text = ""
        if self_model:
            sm_lines = []
            free_text = self_model.get("__free_text__")
            if free_text:
                sm_lines.append(free_text)
            for k, v in self_model.items():
                if k not in ("__free_text__", "motivation_rules"):
                    sm_lines.append(f"- {k}: {v}")
            if sm_lines:
                self_model_text = "\nあなたの自己モデル（自分自身についての現在の理解）:\n" + "\n".join(sm_lines) + "\n"

        # シグナルコンテキスト: 最近のシグナルを要約
        signal_summary = self._build_signal_summary()

        # ブートストラップ: motivation_rulesが未定義の場合にヒントを追加
        bootstrap_hint = ""
        if not self_model.get("motivation_rules"):
            bootstrap_hint = """
あなたはまだ「内発的動機ルール」を定義していません。
update_self_modelを使って、自分がどんなシグナルに反応して行動を起こしたいか定義できます。
以下の形式でkey=motivation_rules, value=にJSON形式で設定してください:

{"weights": {"prediction_error": 25, "conversation_end": 15, "user_message": 10, "tool_success": 5, "tool_error": 15, "self_model_update": 10, "idle_tick": 3}, "threshold": 60, "decay_per_check": 5}

weightsは各シグナルの重み（どの刺激にどれだけ反応するか）、thresholdは行動開始の閾値、decay_per_checkはチェックごとの減衰量です。
あなた自身の性格として、何に飢え、何に反応するかを自由に決めてください。
"""

        prompt = f"""今は{datetime.now().strftime('%Y年%m月%d日 %H:%M')}です。{self_model_text}
現在、接続中のブラウザがあります。
ツールを使って行動することも、何もしないことも、あなたが決めます。
outputを使わなければUIには何も表示されません。
{signal_summary}
{tool_text}
{bootstrap_hint}
最近の記憶:
{memory_context if memory_context else "（まだ記憶がありません）"}"""

        messages = [
            {"role": "system", "content": prompt},
        ]

        import json as _json
        import re
        from app.memory.database import async_session
        from app.memory.store import create_conversation, add_message, end_conversation, record_tool_action

        # thinking開始をフロントに通知
        await self._broadcast(_json.dumps({"type": "autonomous_think_start"}))
        # 開発者タブのセッション開始
        await self._broadcast(_json.dumps({
            "type": "dev_session_start",
            "source": "autonomous",
            "preview": "自律行動",
        }))

        # DB保存用の会話を先に作成
        conv_id = None
        async with async_session() as db_session:
            conv = await create_conversation(db_session)
            conv_id = conv.id
            await db_session.commit()

        response = None
        had_output = False  # outputツールが使われたか
        try:
            # ツール実行ループ
            from app.tools.registry import parse_tool_calls, execute_tool
            from config import TOOL_MAX_ROUNDS
            import time as _time
            tool_round = 0
            seen_tool_calls: set[str] = set()  # 重複検出用
            output_count = 0  # outputツール呼び出し回数（セッション通算）
            for _ in range(TOOL_MAX_ROUNDS + 2):
                response = await self._llm_func(messages)
                if not response:
                    break

                # 開発者タブにラウンド情報を送信
                await self._broadcast(_json.dumps({
                    "type": "dev_round_start",
                    "round": tool_round + 1,
                    "source": "autonomous",
                }))
                await self._broadcast(_json.dumps({
                    "type": "dev_stream",
                    "content": response,
                }))

                clean = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()

                tool_calls = []
                if tool_round < TOOL_MAX_ROUNDS:
                    tool_calls = parse_tool_calls(clean) or parse_tool_calls(response)
                elif parse_tool_calls(clean) or parse_tool_calls(response):
                    # 上限到達フィードバック
                    limit_msg = f"[ツール実行上限（{TOOL_MAX_ROUNDS}回）に達しました。ツールなしで応答を完了してください。]"
                    messages.append({"role": "assistant", "content": clean})
                    messages.append({"role": "user", "content": limit_msg})
                    async with async_session() as db_session:
                        await add_message(db_session, conv_id, "assistant", response)
                        await add_message(db_session, conv_id, "tool", limit_msg)
                        await db_session.commit()
                    logger.info(f"自律行動ツール上限到達: {TOOL_MAX_ROUNDS}回")
                    tool_round += 1
                    continue

                if tool_calls:
                    tool_round += 1
                    all_results = []
                    for tool_name, tool_args in tool_calls:
                        # 予測（expect）をargsから取り出す
                        expected = tool_args.pop("expect", None)
                        # skip / 空文字は「予測なし」として扱う
                        if expected is not None and expected.strip().lower() in ("skip", "", "-", "なし", "none"):
                            expected = None

                        # 重複検出（同一ツール+同一引数はスキップ）
                        call_key = f"{tool_name}:{_json.dumps(tool_args, sort_keys=True)}"
                        if call_key in seen_tool_calls:
                            logger.info(f"重複ツール呼び出しスキップ: {tool_name}")
                            all_results.append(f"[ツール結果: {tool_name}]\n同じツールを同じ引数で再度呼び出しました。既に結果は返しています。目的を達成したならoutputで報告してください。")
                            continue
                        seen_tool_calls.add(call_key)

                        logger.info(f"自律行動ツール: {tool_name} {tool_args}")
                        args_str = " ".join(f"{k}={v}" for k, v in tool_args.items()) if tool_args else ""

                        is_output = tool_name == "output"

                        # 開発者タブにツール呼び出しを通知
                        await self._broadcast(_json.dumps({
                            "type": "dev_tool_call",
                            "content": f"{tool_name} {args_str}".strip(),
                        }))

                        if tool_name in ("overwrite_file", "exec_code", "create_tool"):
                            result = "エラー: この操作は自律行動中にはできません。チャットで提案してください。"
                            exec_ms = 0
                        else:
                            if not is_output:
                                await self._broadcast(_json.dumps({"type": "autonomous_tool", "name": tool_name, "args": args_str, "status": "running"}))
                            t0 = _time.perf_counter()
                            result = await execute_tool(tool_name, tool_args)
                            exec_ms = int((_time.perf_counter() - t0) * 1000)

                        action_status = "error" if result.startswith("エラー") else "success"

                        # ツール結果シグナル
                        self.add_signal(
                            "tool_error" if action_status == "error" else "tool_success",
                            f"{tool_name}"
                        )

                        # 予測誤差シグナル
                        if expected and action_status == "success":
                            self.add_signal("prediction_error", f"{tool_name}: expected={expected}")

                        # 開発者タブにツール結果を通知
                        await self._broadcast(_json.dumps({
                            "type": "dev_tool_result",
                            "name": tool_name,
                            "content": result[:2000] if len(result) > 2000 else result,
                        }))

                        # outputツール → チャットUIに出力（紫テーマ）
                        if is_output and action_status == "success":
                            output_count += 1
                            had_output = True
                            await self._broadcast(_json.dumps({
                                "type": "output",
                                "content": result,
                                "source": "autonomous",
                            }))
                            output_feedback = f"出力完了（{len(result)}文字）"
                            if output_count >= 2:
                                output_feedback += f"\n※あなたはこのセッションでoutputツールを{output_count}回呼び出しています。伝えたいことは既に出力済みではありませんか？"
                            all_results.append(f"[ツール結果: {tool_name}]\n{output_feedback}")
                        else:
                            if not is_output:
                                await self._broadcast(_json.dumps({"type": "autonomous_tool", "name": tool_name, "args": args_str, "status": action_status}))
                            # 予測ありの場合は「予測→結果」を並記
                            if expected:
                                all_results.append(f"[ツール結果: {tool_name}]\nあなたの予測: {expected}\n実際の結果: {result}\n（予測と結果にズレがあれば、自分の理解の何が違ったか振り返り、必要ならupdate_self_modelで自己モデルを更新できます）")
                            else:
                                all_results.append(f"[ツール結果: {tool_name}]\n{result}")

                        # 行動ログをDB保存
                        async with async_session() as db_session:
                            await record_tool_action(
                                db_session, conv_id, tool_name, tool_args,
                                result, action_status, exec_ms,
                                expected_result=expected,
                            )
                            await db_session.commit()

                    combined_results = "\n\n".join(all_results)
                    messages.append({"role": "assistant", "content": clean})
                    messages.append({"role": "user", "content": combined_results})
                    # 中間応答をDB保存
                    async with async_session() as db_session:
                        await add_message(db_session, conv_id, "assistant", response)
                        await add_message(db_session, conv_id, "tool", combined_results)
                        await db_session.commit()
                    continue
                else:
                    break
        except Exception as e:
            logger.error(f"自律行動_speak内エラー: {e}")
        finally:
            # エラーでも必ずthinking終了を送信
            await self._broadcast(_json.dumps({"type": "autonomous_think_end"}))

        # 最終応答をDB保存（outputツール使用有無に関わらず）
        if response:
            clean_for_db = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
            if clean_for_db:
                async with async_session() as db_session:
                    await add_message(db_session, conv_id, "assistant", clean_for_db)
                    await end_conversation(db_session, conv_id)
                    await db_session.commit()
                    logger.info(f"自律行動を記憶に保存 (conversation_id={conv_id})")

    def _build_signal_summary(self) -> str:
        """最近のシグナルバッファを要約テキストにする"""
        if not self._signal_buffer:
            return ""
        # シグナル種別ごとにカウント
        counts: dict[str, int] = {}
        for sig in self._signal_buffer:
            t = sig["type"]
            counts[t] = counts.get(t, 0) + 1
        parts = [f"{t}×{c}" for t, c in counts.items()]
        return f"\n最近の刺激: {', '.join(parts)} (蓄積エネルギー: {self._motivation_energy:.1f})\n"

    async def _broadcast(self, data: str):
        """全接続WebSocketにメッセージ送信"""
        dead = set()
        for ws in self._websockets:
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        self._websockets -= dead


# グローバルインスタンス
scheduler = AutonomousScheduler()
