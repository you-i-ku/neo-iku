"""バンディット計画選択 — LLM計画フェーズの代替"""
import random
from config import (
    BANDIT_DEFAULT_REWARD, BANDIT_NOISE_SIGMA, BANDIT_COLD_NOISE_SIGMA,
    PREDICTION_ENERGY_PEAK,
)


def compute_reward(pred_accuracy: float | None) -> float:
    """予測精度から報酬を計算（逆U字カーブ）。pred_accuracy=Noneならデフォルト中間値"""
    if pred_accuracy is None:
        return BANDIT_DEFAULT_REWARD
    x = max(0.0, min(1.0, pred_accuracy))
    return PREDICTION_ENERGY_PEAK * 4 * x * (1 - x)


def bandit_select_tools(
    all_tool_names: list[str],
    bandit_rewards: dict,
    available_energy: float,
    action_costs_fn,
    max_tools: int,
) -> list[str]:
    """バンディットでツール選択。エネルギー予算内で可能な限り選ぶ"""
    # スコア計算
    scored = []
    for name in all_tool_names:
        if name == "non_response":
            continue
        entry = bandit_rewards.get(name)
        if entry and isinstance(entry, dict) and "mean" in entry:
            mean = entry["mean"]
            sigma = BANDIT_NOISE_SIGMA
        else:
            mean = BANDIT_DEFAULT_REWARD
            sigma = BANDIT_COLD_NOISE_SIGMA
        score = mean + random.gauss(0, sigma)
        scored.append((name, score))

    # スコア降順
    scored.sort(key=lambda x: x[1], reverse=True)

    # エネルギー予算内で選択
    selected = []
    energy_remaining = available_energy
    for name, _score in scored:
        if len(selected) >= max_tools:
            break
        cost = action_costs_fn(name)
        if cost <= energy_remaining:
            selected.append(name)
            energy_remaining -= cost

    return selected


def update_reward(bandit_rewards: dict, tool_name: str, reward: float) -> dict:
    """増分平均で報酬を更新"""
    entry = bandit_rewards.get(tool_name)
    if entry and isinstance(entry, dict) and "mean" in entry and "count" in entry:
        old_mean = entry["mean"]
        old_count = entry["count"]
        new_count = old_count + 1
        new_mean = old_mean + (reward - old_mean) / new_count
        bandit_rewards[tool_name] = {"mean": round(new_mean, 2), "count": new_count}
    else:
        bandit_rewards[tool_name] = {"mean": round(reward, 2), "count": 1}
    return bandit_rewards
