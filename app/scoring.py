"""评分与画像（spec §7）。

两渠道归一到同一把 0-100 尺子，再按 applicable 通道权重做轻度加权。
- GitHub: 100 ×(0.50×相关性 + 0.35×贡献深度 + 0.15×活跃度)
- X:      100 ×(0.50×话题相关性 + 0.35×影响力互动 + 0.15×活跃度)
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List

# 可挖性启发式词典（spec §7.3）
_HIRE_PLUS = ["open to work", "open-to-work", "freelance", "consulting", "consultant",
              "dms open", "dm open", "open dms", "founder", "indie", "for hire",
              "available", "hire me", "co-founder", "cofounder", "求职", "接单", "外包", "自由职业"]
_HIRE_BIGORG = ["google", "meta", "facebook", "openai", "anthropic", "microsoft",
                "amazon", "apple", "nvidia", "deepmind", "bytedance", "tencent",
                "alibaba", "netflix", "stripe", "databricks"]


def _log_norm(x: float, cap: float) -> float:
    """log 归一到 0-1，x>=cap 视为 1。"""
    if x <= 0:
        return 0.0
    return min(1.0, math.log1p(x) / math.log1p(cap))


def _recency_score(ts: float | None) -> float:
    """最近活动时间衰减到 0-1。半衰期约 180 天；未知给中性 0.6。"""
    if not ts:
        return 0.6
    days = (time.time() - ts) / 86400.0
    if days < 0:
        days = 0
    return max(0.05, math.exp(-days / 260.0))


def score_github(cand: Dict[str, Any], llm_relevance: float | None) -> None:
    sig = cand["_signals"]
    # 相关性：以 Claude 复核为主；无复核时用命中信号回退
    if llm_relevance is not None:
        relevance = llm_relevance
    else:
        base = _log_norm(sig["relevance_hits"], 8)
        relevance = min(1.0, base + (0.2 if sig["matched_paths"] else 0.0))
    depth = _log_norm(sig["depth"], 400)            # 相关 commit/贡献计数
    recency = _recency_score(sig["recency_ts"])

    score = 100 * (0.50 * relevance + 0.35 * depth + 0.15 * recency)
    cand["raw_relevance"] = relevance
    cand["subscores"] = {"relevance": round(relevance, 3),
                         "depth_or_influence": round(depth, 3),
                         "recency": round(recency, 3)}
    cand["problem_fit_score"] = round(score, 1)
    # 证据强度：代码贡献等可核验产出为 hard
    cand["evidence_strength"] = "hard" if (sig["matched_paths"] or sig["depth"] >= 5) else "medium"


def score_x(cand: Dict[str, Any], llm_relevance: float | None) -> None:
    sig = cand["_signals"]
    if llm_relevance is not None:
        relevance = llm_relevance
    else:
        relevance = _log_norm(sig["relevance_hits"], 6)
    # 影响力互动：相关帖互动量 + 粉丝（log 归一后取较高权重组合）
    influence = 0.6 * _log_norm(sig["depth"], 5000) + 0.4 * _log_norm(cand.get("followers", 0), 200000)
    recency = _recency_score(sig["recency_ts"])

    score = 100 * (0.50 * relevance + 0.35 * influence + 0.15 * recency)
    cand["raw_relevance"] = relevance
    cand["subscores"] = {"relevance": round(relevance, 3),
                         "depth_or_influence": round(influence, 3),
                         "recency": round(recency, 3)}
    cand["problem_fit_score"] = round(score, 1)
    # 观点/影响力类证据为 soft
    cand["evidence_strength"] = "soft"


def hireability(cand: Dict[str, Any]) -> Dict[str, Any]:
    bio = (cand.get("bio") or "").lower()
    org = (cand.get("org") or "").lower()
    reasons: List[str] = []
    level = "medium"

    plus = [w for w in _HIRE_PLUS if w in bio]
    if plus:
        reasons.append(f"bio 含可挖性信号：{', '.join(plus[:3])}")
        level = "high"

    big = next((b for b in _HIRE_BIGORG if b in org or b in bio), None)
    if big:
        reasons.append(f"疑似头部大厂/核心团队（{big}），可挖性偏低")
        level = "low" if level != "high" else "medium"

    if cand.get("contact_hint"):
        reasons.append("有公开联系方式")
        if level == "medium":
            level = "high"

    # 大量粉丝且无可挖信号 → 知名度高、可挖性中性偏低
    if cand.get("source") == "x" and cand.get("followers", 0) > 100000 and not plus:
        reasons.append("影响力很大、可挖性可能偏低")
        if level == "medium":
            level = "low"

    if not reasons:
        reasons.append("无明显信号，按中性处理")
    return {"level": level, "reasons": reasons}


def apply_weight(cands: List[Dict[str, Any]], weight: float) -> None:
    """按通道权重对该通道候选的最终分做轻度加权（仅用于跨渠道排序）。

    诚实标注：渠道内排名比跨渠道绝对分更可靠（spec §7.1 注）。
    采用温和加权 0.6 + 0.4×weight*2，避免某通道被完全压没。
    """
    factor = 0.6 + 0.4 * min(1.0, weight * 2)
    for c in cands:
        c["weighted_score"] = round(c["problem_fit_score"] * factor, 1)
