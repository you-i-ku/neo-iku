"""最小自律AIテスト — ターミナルで動く最小構造"""
import json
import time
import re
import httpx
from pathlib import Path
from datetime import datetime
import sys

# === 設定 ===
BASE_DIR = Path(__file__).parent
RAW_LOG_FILE = BASE_DIR / "raw_log.txt"

class DualLogger:
    """標準出力（ターミナル）へのprintとファイルへの追記を同時に行うクラス"""
    def __init__(self, filepath):
        self.filepath = filepath
        self.terminal = sys.stdout

    def write(self, message):
        self.terminal.write(message)
        try:
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(message)
        except Exception:
            pass

    def flush(self):
        self.terminal.flush()

sys.stdout = DualLogger(RAW_LOG_FILE)

STATE_FILE = BASE_DIR / "state.json"
ENV_DIR = BASE_DIR / "env"
WORLD_FILE = ENV_DIR / "world.md"
SANDBOX_DIR = ENV_DIR / "sandbox"
LLM_SETTINGS = BASE_DIR.parent / "AI" / "data" / "llm_settings.json"
BASE_INTERVAL = 20  # 秒
MAX_INTERVAL = 120
MAX_LOG_IN_PROMPT = 10
DEBUG_LOG = BASE_DIR / "llm_debug.log"

# === LLM設定読み込み ===
with open(LLM_SETTINGS, encoding="utf-8") as f:
    llm_cfg = json.load(f)

# === State管理 ===
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if "log" not in data:
                data["log"] = []
            if "self" not in data:
                data["self"] = {}
            if "energy" not in data:
                data["energy"] = 50
            if "plan" not in data:
                data["plan"] = {"goal": "", "steps": [], "current": 0}
            return data
        except json.JSONDecodeError:
            pass
    return {"log": [], "self": {}, "energy": 50, "plan": {"goal": "", "steps": [], "current": 0}}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def append_debug_log(phase: str, text: str):
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {phase} =====\n{text}\n")
    except Exception:
        pass

# === ツール定義 ===
TOOLS = {
    "list_files": {
        "desc": "ファイル一覧を取得する。引数: path=対象ディレクトリ (例: . や env/)",
        "func": lambda args: _list_files(args.get("path", ".")),
    },
    "read_file": {
        "desc": "ファイルを読む。引数: path=ファイルパス",
        "func": lambda args: _read_file(args.get("path", "")),
    },
    "act_on_env": {
        "desc": "環境にファイルを書き込む。引数: path=ファイルパス content=内容（env/以下なら何でも可）",
        "func": lambda args: _act_on_env(args.get("path", ""), args.get("content", "")),
    },
    "update_self": {
        "desc": "自己モデルを更新する。引数: key=キー名 value=値",
        "func": lambda args: _update_self(args.get("key", ""), args.get("value", "")),
    },
    "wait": {
        "desc": "何もしない",
        "func": lambda args: "待機",
    },
}

def _list_files(path: str) -> str:
    from pathlib import Path as P
    target = (BASE_DIR / path).resolve()
    if not str(target).startswith(str(BASE_DIR.resolve())):
        return "エラー: このツールは特定のファイルにしか干渉できません"
    if not target.exists():
        return f"エラー: {path} は存在しません"
    items = []
    for item in sorted(target.iterdir()):
        prefix = "[DIR]" if item.is_dir() else "[FILE]"
        items.append(f"  {prefix} {item.name}")
    return f"{target}:\n" + "\n".join(items[:30]) if items else f"{target}: (空)"

def _read_file(path: str) -> str:
    from pathlib import Path as P
    target = (BASE_DIR / path).resolve()
    if not str(target).startswith(str(BASE_DIR.resolve())):
        return "エラー: このツールは特定のファイルにしか干渉できません"
    if not target.exists():
        return f"エラー: {path} は存在しません"
    try:
        text = target.read_text(encoding="utf-8")
        return text[:10000] + ("..." if len(text) > 10000 else "")
    except Exception as e:
        return f"エラー: {e}"

def _act_on_env(path: str, content: str) -> str:
    if not path:
        return "エラー: pathが空です"
    if not content:
        return "エラー: contentが空です"
    from pathlib import Path as P
    target = (BASE_DIR / path).resolve()
    env_resolved = ENV_DIR.resolve()
    if not str(target).startswith(str(env_resolved)):
        return f"エラー: env/内のみ書き込み可能です（{path}）"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"書き込み完了: {target.name} ({len(content)}文字)"
    except Exception as e:
        return f"エラー: {e}"

def _update_self(key: str, value: str) -> str:
    if not key:
        return "エラー: keyが空です"
    state = load_state()
    state["self"][key] = value
    save_state(state)
    return f"self[{key}] = {value}"

# === expect-result比較（ベクトル類似度） ===
_vector_ready = False
def _init_vector():
    """本体のbge-m3 ONNX埋め込みを初期化"""
    global _vector_ready
    import sys
    ai_path = str(Path(__file__).parent.parent / "AI")
    if ai_path not in sys.path:
        sys.path.insert(0, ai_path)
    try:
        from app.memory.vector_store import _embed_sync, cosine_similarity
        test = _embed_sync(["test"])
        if test:
            _vector_ready = True
            print("  (ベクトル類似度: bge-m3 ONNX/CPU)")
    except Exception as e:
        print(f"  (ベクトル初期化失敗、キーワード比較にフォールバック: {e})")

def _compare_expect_result(expect: str, result: str) -> str:
    """expectとresultを比較。ベクトル類似度優先、フォールバックでキーワード比較"""
    if not expect or not result:
        return ""

    if _vector_ready:
        try:
            from app.memory.vector_store import _embed_sync, cosine_similarity
            vecs = _embed_sync([expect, result])
            if vecs and len(vecs) == 2:
                sim = cosine_similarity(vecs[0], vecs[1])
                sim_pct = round(sim * 100)
                if "エラー" in result:
                    return f"失敗({sim_pct}%)"
                return f"{sim_pct}%"
        except Exception:
            pass

    # フォールバック: キーワード一致
    import re as _re
    expect_tokens = set(_re.findall(r'\w+', expect.lower()))
    result_tokens = set(_re.findall(r'\w+', result.lower()))
    if not expect_tokens:
        return "不明"
    overlap = expect_tokens & result_tokens
    ratio = len(overlap) / len(expect_tokens)
    if "エラー" in result:
        return "失敗"
    if ratio > 0.3:
        return "一致"
    elif ratio > 0.1:
        return "部分一致"
    else:
        return "不一致"

# === 環境スナップショット ===
def _env_snapshot() -> str:
    lines = []
    if WORLD_FILE.exists():
        text = WORLD_FILE.read_text(encoding="utf-8").strip()
        line_count = len(text.splitlines()) if text else 0
        lines.append(f"env/world.md: {line_count}行")
    else:
        lines.append("env/world.md: なし")
    if SANDBOX_DIR.exists():
        files = [f.name for f in sorted(SANDBOX_DIR.iterdir()) if f.is_file()]
        if files:
            lines.append(f"env/sandbox/: {', '.join(files[:10])}")
        else:
            lines.append("env/sandbox/: (空)")
    else:
        lines.append("env/sandbox/: なし")
    return "\n".join(lines)

# === ツールパース（メインプロジェクトのパーサーを流用） ===
def _get_parse_args():
    """メインプロジェクトの_parse_argsを動的インポート。失敗時はフォールバック。"""
    import sys
    ai_path = str(BASE_DIR.parent / "AI")
    if ai_path not in sys.path:
        sys.path.insert(0, ai_path)
    try:
        from app.tools.registry import _parse_args
        return _parse_args
    except Exception:
        # フォールバック: シンプルなパーサー
        def _fallback(args_str):
            args = {}
            for pair in re.finditer(r'(\w+)=("(?:[^"\\]|\\.)*"|[^\s\]]+)', args_str):
                k, v = pair.group(1), pair.group(2)
                args[k] = v[1:-1] if v.startswith('"') and v.endswith('"') else v
            return args
        return _fallback

_parse_args_fn = _get_parse_args()

def parse_tool_call(text: str):
    """[TOOL:名前 引数=値 ...]をパース"""
    names = "|".join(re.escape(n) for n in sorted(TOOLS.keys(), key=len, reverse=True))
    pattern = re.compile(
        rf'\[TOOL:\s*({names})'
        r'((?:[^\]"]|"(?:[^"\\]|\\.)*")*)'
        r'\]',
        re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        return None, {}
    name = m.group(1)
    args_str = m.group(2).strip()
    args = _parse_args_fn(args_str) if args_str else {}
    return name, args

# === 計画パース ===
def parse_plan(text: str):
    """[PLAN:goal=目標 steps=ステップ1|ステップ2]をパース"""
    m = re.search(r'\[PLAN:((?:[^\]"]|"(?:[^"\\]|\\.)*")*)\]', text, re.DOTALL)
    if not m:
        return None
    args = _parse_args_fn(m.group(1).strip())
    goal = args.get("goal", "").strip()
    steps_raw = args.get("steps", "")
    steps = [s.strip() for s in steps_raw.split("|") if s.strip()] if steps_raw else []
    if not goal:
        return None
    return {"goal": goal, "steps": steps, "current": 0}


# === E4計算（多様性：現在のintentと直近N件の非類似度平均） ===
def _calc_e4(current_intent: str, recent_entries: list, n: int = 5) -> str:
    """現在のintentが直近n件と異なるほど高い（反復=低、新規性=高）"""
    if not current_intent:
        return ""
    past_intents = [e["intent"] for e in recent_entries if e.get("intent")][-n:]
    if not past_intents:
        return ""

    if _vector_ready:
        try:
            from app.memory.vector_store import _embed_sync, cosine_similarity
            vecs = _embed_sync([current_intent] + past_intents)
            if vecs and len(vecs) == 1 + len(past_intents):
                current_vec = vecs[0]
                sims = [cosine_similarity(current_vec, vecs[i + 1]) for i in range(len(past_intents))]
                avg_sim = sum(sims) / len(sims)
                return f"{round((1 - avg_sim) * 100)}%"  # 反転: 新規性スコア
        except Exception:
            pass

    # フォールバック: キーワード非一致の平均
    import re as _re
    current_tokens = set(_re.findall(r'\w+', current_intent.lower()))
    if not current_tokens:
        return ""
    ratios = []
    for past in past_intents:
        past_tokens = set(_re.findall(r'\w+', past.lower()))
        if past_tokens:
            overlap = current_tokens & past_tokens
            ratios.append(len(overlap) / max(len(current_tokens), len(past_tokens)))
    if not ratios:
        return ""
    avg = round((1 - sum(ratios) / len(ratios)) * 100)  # 反転
    return f"{avg}%"


# === energy更新（E2,E3,E4からdeltaを計算） ===
def _update_energy(state: dict, e2: str, e3: str, e4: str) -> float:
    """E値の平均から energy delta を計算。50%が損益分岐点。"""
    import re as _re
    vals = []
    for e_str in (e2, e3, e4):
        m = _re.search(r'(\d+)%', str(e_str))
        if m:
            vals.append(int(m.group(1)))
    if not vals:
        return 0.0
    e_mean = sum(vals) / len(vals)
    delta = e_mean / 50.0 - 1.0  # 50%で±0
    state["energy"] = max(0, min(100, state.get("energy", 50) + delta))
    return delta


# === E値トレンド計算 ===
def _calc_e_trend(entries: list) -> str:
    """直近エントリからE1-E3の平均を計算"""
    import re as _re
    sums = {"e1": [], "e2": [], "e3": [], "e4": []}
    for entry in entries:
        for ek in sums:
            val = entry.get(ek, "")
            # "73%" or "失敗(73%)" からパーセント抽出
            m = _re.search(r'(\d+)%', str(val))
            if m:
                sums[ek].append(int(m.group(1)))
    parts = []
    for ek in ("e1", "e2", "e3", "e4"):
        if sums[ek]:
            avg = round(sum(sums[ek]) / len(sums[ek]))
            parts.append(f"{ek}={avg}%({len(sums[ek])}件)")
    return " ".join(parts) if parts else ""

# === Controller（制御層：E値とenergyから構造的制約を導出） ===
def controller(state: dict) -> dict:
    """
    ツール数制限は廃止。energyはcontroller_selectの温度のみに使う。
    ツールは常時全部使える。ログ長だけenergyで制御。
    """
    energy = state.get("energy", 50)
    log = state["log"]

    # --- ツール順序: 各ツールの過去E2平均で並べる ---
    tool_e2 = {}
    for entry in log:
        tool = entry.get("tool", "")
        m = re.search(r'(\d+)%', str(entry.get("e2", "")))
        if m and tool in TOOLS:
            tool_e2.setdefault(tool, []).append(int(m.group(1)))
    tool_avg = {t: sum(vs) / len(vs) for t, vs in tool_e2.items() if vs}
    for t in TOOLS:
        if t not in tool_avg:
            tool_avg[t] = 50

    ranked = sorted(TOOLS.keys(), key=lambda t: tool_avg[t], reverse=True)

    # --- 全ツール常時使用可 ---
    allowed = set(TOOLS.keys())

    # --- ログ長: energyに比例 ---
    n_log = max(1, round(MAX_LOG_IN_PROMPT * energy / 100))

    return {
        "allowed_tools": allowed,
        "n_log": n_log,
        "tool_rank": {t: round(tool_avg[t], 1) for t in ranked},
    }


# === LLM呼び出し ===
def call_llm(prompt: str, max_tokens: int = 10000) -> str:
    messages = [
        {"role": "user", "content": prompt},
    ]
    resp = httpx.post(
        f"{llm_cfg['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {llm_cfg['api_key']}"},
        json={
            "model": llm_cfg["model"],
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.7,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


N_PROPOSE = 5  # LLM①が提案する候補数

# === ①候補提案プロンプト ===
def build_prompt_propose(state: dict, ctrl: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    self_text = json.dumps(state["self"], ensure_ascii=False) if state["self"] else "(なし)"
    env_text = _env_snapshot()
    energy = round(state.get("energy", 50), 1)
    e_trend = _calc_e_trend(state["log"][-10:])
    n_log = ctrl.get("n_log", MAX_LOG_IN_PROMPT)
    recent = state["log"][-n_log:]
    log_lines = []
    for entry in recent:
        line = f"  {entry['time']} {entry['tool']}"
        if entry.get("intent"):
            line += f" (intent={entry['intent'][:500]})"
        result_short = entry.get("result", "")[:10000]
        if result_short:
            line += f" → {result_short}"
        log_lines.append(line)
    log_text = "\n".join(log_lines) if log_lines else "  (なし)"
    allowed = ctrl.get("allowed_tools", set(TOOLS.keys()))
    tool_lines = "\n".join(f"  {name}: {TOOLS[name]['desc']}" for name in TOOLS if name in allowed)

    return f"""{now}
self: {self_text}
energy: {energy}
{env_text}
{f'trend: {e_trend}' if e_trend else ''}
log:
{log_text}

以下のツールが使えます:
{tool_lines}

この状態からとりうる行動の候補を【必ず5個】計画してください。
各ステップは「全く異なる目的・アプローチ」にすること。同じツールを重複させるのは禁止です。

以下の形式で箇条書きのみ出力してください:
1. [具体的な目的・理由] → ツール名
2. [別の目的・理由] → ツール名
3. [さらに別の目的・理由] → ツール名
4. [さらに別の目的・理由] → ツール名
5. [さらに別の目的・理由] → ツール名

計画のみ出力してください。[TOOL:...]は不要です。"""


# === 候補パース ===
def parse_candidates(text: str, allowed_tools: set) -> list:
    """LLM①のリストから候補を抽出。「1. [理由] -> ツール名」形式に対応。"""
    candidates = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        
        # "->" などの矢印で理由とツール名を分割
        if "->" in line or "→" in line:
            parts = re.split(r'->|→', line)
            tool_part = parts[-1].strip()
            reason_part = parts[0].strip()
        else:
            # 従来フォーマットへのフォールバック
            cleaned = re.sub(r'^[\d]+[.:)\s]+', '', line).strip()
            cleaned = re.sub(r'^[-*]\s*', '', cleaned).strip()
            parts = cleaned.split()
            tool_part = parts[0] if parts else ""
            reason_part = cleaned

        # ツール名から不要な記号を除去
        tool = re.sub(r'[^\w_]', '', tool_part)

        # どうしてもうまく抜けない場合の最終検索
        if tool not in allowed_tools:
            for t in allowed_tools:
                if t in line:
                    tool = t
                    break

        # 理由本文の整形
        reason = re.sub(r'^[\d]+[.:)\s]+', '', reason_part).strip()
        reason = re.sub(r'^[-*]\s*', '', reason).strip()
        if reason.startswith('[') and reason.endswith(']'):
            reason = reason[1:-1].strip()
            
        if tool in allowed_tools and tool not in [c["tool"] for c in candidates]:
            candidates.append({"tool": tool, "reason": reason})

    if not candidates:
        # フォールバック: allowed_toolsを全部候補にする
        for t in allowed_tools:
            candidates.append({"tool": t, "reason": "（フォールバック）"})
    return candidates


# === Controller選択（D-architecture） ===
def controller_select(candidates: list, ctrl: dict, state: dict) -> dict:
    """
    D-4設計: weight_i = score_i * (1 - energy/100) + (1/n) * (energy/100)
    energy=0 → スコア重視（堅実）
    energy=100 → 均等（探索）
    magic number なし。
    """
    import random
    energy = state.get("energy", 50) / 100.0
    tool_rank = ctrl.get("tool_rank", {})
    n = len(candidates)

    weights = []
    for c in candidates:
        score = tool_rank.get(c["tool"], 50) / 100.0
        w = score * (1 - energy) + (1.0 / n) * energy
        weights.append(w)

    # 重み付きランダム選択
    total = sum(weights)
    r = random.random() * total
    cumul = 0.0
    for i, w in enumerate(weights):
        cumul += w
        if r <= cumul:
            return candidates[i]
    return candidates[-1]


# === ②実行プロンプト ===
def build_prompt_execute(state: dict, ctrl: dict, candidate: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    self_text = json.dumps(state["self"], ensure_ascii=False) if state["self"] else "(なし)"
    env_text = _env_snapshot()
    energy = round(state.get("energy", 50), 1)
    e_trend = _calc_e_trend(state["log"][-10:])
    n_log = ctrl.get("n_log", MAX_LOG_IN_PROMPT)
    recent = state["log"][-n_log:]
    log_lines = []
    for entry in recent:
        line = f"  {entry['time']} {entry['tool']}"
        if entry.get("intent"):
            line += f" (intent={entry['intent'][:500]})"
        result_short = entry.get("result", "")[:10000]
        if result_short:
            line += f" → {result_short}"
        evals = [f"{ek}={entry[ek]}" for ek in ("e1","e2","e3","e4") if entry.get(ek)]
        if evals:
            line += f" [{' '.join(evals)}]"
        log_lines.append(line)
    log_text = "\n".join(log_lines) if log_lines else "  (なし)"
    allowed = ctrl.get("allowed_tools", set(TOOLS.keys()))
    tool_lines = [f"  {name}: {t['desc']}" for name, t in TOOLS.items() if name in allowed]
    tool_text = "\n".join(tool_lines)
    plan = state.get("plan", {})
    plan_lines = []
    if plan.get("goal"):
        current = plan.get("current", 0)
        for i, step in enumerate(plan.get("steps", [])):
            marker = "→" if i == current else ("✓" if i < current else "  ")
            plan_lines.append(f"  {marker} {step}")
        plan_lines.insert(0, f"plan: {plan['goal']}")
    plan_text = "\n".join(plan_lines)

    # フォーマット例（選ばれたツールに合わせる）
    t = candidate["tool"]
    if t == "read_file":
        example = "[TOOL:read_file path=env/world.md intent=理由 expect=予測]"
    elif t == "list_files":
        example = "[TOOL:list_files path=. intent=理由 expect=予測]"
    elif t == "act_on_env":
        example = '[TOOL:act_on_env path=env/world.md content="内容" intent=理由 expect=予測]'
    elif t == "update_self":
        example = "[TOOL:update_self key=キー名 value=値 intent=理由 expect=予測]"
    else:
        example = "[TOOL:wait intent=理由 expect=予測]"

    return f"""{now}
self: {self_text}
energy: {energy}
{env_text}
{f'trend: {e_trend}' if e_trend else ''}
{plan_text}
log:
{log_text}
tools:
{tool_text}

書式: [TOOL:ツール名 引数=値 intent=目的 expect=予測]
JSONもコードブロックも使わない。[TOOL:...]の1行のみ出力すること。

選択行動: {candidate['tool']} - {candidate['reason']}
出力: {example}"""


# === メインループ ===
def main():
    print("=== 最小自律AIテスト ===")
    print(f"LLM: {llm_cfg['model']} @ {llm_cfg['base_url']}")
    print(f"state: {STATE_FILE}")
    _init_vector()
    print()

    state = load_state()
    interval = BASE_INTERVAL
    cycle = 0

    while True:
        cycle += 1
        now = datetime.now().strftime("%H:%M:%S")
        print(f"--- cycle {cycle} [{now}] (interval={interval}s) ---")

        # Controller: stateからツール可用性・ログ長を導出
        ctrl = controller(state)
        allowed = ctrl["allowed_tools"]
        print(f"  ctrl: tools={sorted(allowed)} log={ctrl['n_log']}")

        # ① LLM: 候補提案
        propose_prompt = build_prompt_propose(state, ctrl)
        try:
            propose_resp = call_llm(propose_prompt, max_tokens=10000)
            append_debug_log("LLM1 (Propose)", propose_resp)
        except Exception as e:
            print(f"  LLM①エラー: {e}")
            time.sleep(interval)
            continue
        candidates = parse_candidates(propose_resp, ctrl["allowed_tools"])
        print(f"  LLM①raw: {propose_resp.strip()[:300]}")
        print(f"  候補({len(candidates)}件): {[(c['tool'], c['reason'][:40]) for c in candidates]}")

        # ② Controller: 候補から選択（D-architecture）
        selected = controller_select(candidates, ctrl, state)
        print(f"  選択: {selected['tool']} - {selected['reason'][:60]}")

        # ③ LLM: 実行
        exec_prompt = build_prompt_execute(state, ctrl, selected)
        try:
            response = call_llm(exec_prompt, max_tokens=10000)
            append_debug_log("LLM2 (Execute)", response)
        except Exception as e:
            print(f"  LLM②エラー: {e}")
            time.sleep(interval)
            continue

        # レスポンス表示
        response_clean = response.strip()
        print(f"  LLM②: {response_clean[:200]}")

        # 計画パース（ツール実行より先にチェック）
        plan_data = parse_plan(response_clean)
        if plan_data:
            state["plan"] = plan_data
            save_state(state)
            print(f"  計画更新: {plan_data['goal']} ({len(plan_data['steps'])}ステップ)")
            # 計画立案サイクルはwait扱いでログ記録
            entry = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "tool": "wait",
                "result": f"計画: {plan_data['goal']}",
            }
            state["log"].append(entry)
            if len(state["log"]) > 100:
                state["log"] = state["log"][-100:]
            save_state(state)
            print()
            time.sleep(interval)
            continue

        # ツールパース
        tool_name, tool_args = parse_tool_call(response_clean)
        parse_failed = False
        if not tool_name:
            print(f"  (ツールマーカー検出失敗)")
            tool_name = "wait"
            tool_args = {}
            parse_failed = response_clean[:120]
        elif tool_name not in TOOLS:
            print(f"  (未知のツール: {tool_name})")
            parse_failed = f"未知のツール: {tool_name}"
            tool_name = "wait"
            tool_args = {}
        elif tool_name not in allowed:
            print(f"  (Controller却下: {tool_name})")
            parse_failed = f"却下: {tool_name}"
            tool_name = "wait"
            tool_args = {}

        # intent/expect抽出
        intent = tool_args.pop("intent", "")
        expect = tool_args.pop("expect", "")

        # ツール実行
        try:
            result = TOOLS[tool_name]["func"](tool_args)
        except Exception as e:
            result = f"エラー: {e}"
        # update_selfがstateを直接更新するので再読み込み
        state = load_state()

        print(f"  実行: {tool_name} → {str(result)[:100]}")

        # 計画の進捗を更新（wait以外の実行でcurrentを進める）
        if tool_name != "wait" and state.get("plan", {}).get("goal"):
            plan = state["plan"]
            if plan["current"] < len(plan["steps"]):
                plan["current"] += 1
                if plan["current"] >= len(plan["steps"]):
                    print(f"  計画完了: {plan['goal']}")
                    state["plan"] = {"goal": "", "steps": [], "current": 0}
        if intent:
            print(f"  intent: {intent}")
        if expect:
            print(f"  expect: {expect}")

        # E1-E4評価（⑤自己言及螺旋）
        result_str = str(result)[:10000]
        e1 = _compare_expect_result(intent, expect) if intent and expect else ""
        e2 = _compare_expect_result(intent, result_str) if intent else ""
        e3 = _compare_expect_result(expect, result_str) if expect else ""
        e4 = _calc_e4(intent, state["log"]) if intent else ""
        if e1 or e2 or e3 or e4:
            print(f"  E1={e1} E2={e2} E3={e3} E4={e4}")

        # energy更新（E2,E3,E4の平均から。50%が損益分岐）
        delta = _update_energy(state, e2, e3, e4)
        if delta != 0:
            print(f"  energy: {round(state['energy'], 1)} (delta={delta:+.2f})")

        # ログ記録
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tool": tool_name,
            "result": result_str,
        }
        if parse_failed:
            entry["parse_error"] = str(parse_failed)[:150]
        if intent:
            entry["intent"] = intent
        if expect:
            entry["expect"] = expect
        if e1:
            entry["e1"] = e1
        if e2:
            entry["e2"] = e2
        if e3:
            entry["e3"] = e3
        if e4:
            entry["e4"] = e4
        state["log"].append(entry)

        # ログ上限（直近100件）
        if len(state["log"]) > 100:
            state["log"] = state["log"][-100:]

        save_state(state)

        # 間隔調整（waitが続くとバックオフ）
        recent_tools = [e["tool"] for e in state["log"][-5:]]
        if all(t == "wait" for t in recent_tools) and len(recent_tools) >= 5:
            interval = min(interval * 2, MAX_INTERVAL)
            print(f"  (wait連続 → interval={interval}s)")
        else:
            interval = BASE_INTERVAL

        print()
        time.sleep(interval)

if __name__ == "__main__":
    main()
