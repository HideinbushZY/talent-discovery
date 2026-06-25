"""评分与画像（spec §7）。

两渠道归一到同一把 0-100 尺子，再按 applicable 通道权重做轻度加权。
- GitHub: 100 ×(0.50×相关性 + 0.35×贡献深度 + 0.15×活跃度)
- X:      100 ×(0.50×话题相关性 + 0.35×影响力互动 + 0.15×活跃度)
"""
from __future__ import annotations

import math
import re
import time
from typing import Any, Dict, List

# 可挖性启发式词典（spec §7.3）
_HIRE_PLUS = ["open to work", "open-to-work", "freelance", "consulting", "consultant",
              "dms open", "dm open", "open dms", "founder", "indie", "for hire",
              "available", "hire me", "co-founder", "cofounder", "求职", "接单", "外包", "自由职业"]
_HIRE_BIGORG = ["google", "meta", "facebook", "openai", "anthropic", "microsoft",
                "amazon", "apple", "nvidia", "deepmind", "bytedance", "tencent",
                "alibaba", "netflix", "stripe", "databricks"]

# 中国契合度（China-fit）——给中国公司用：衡量"能否直接为我所用"。
# 全部是**岗位相关的客观信号**（中文能力 / 地理时区 / 中国市场经验），非族裔推断。
_HAN_RE = re.compile(r"[一-鿿]")   # 中日韩统一表意文字（中文）
_CN_LOCATIONS = ["china", "中国", "中华人民共和国", "prc", "beijing", "北京", "shanghai", "上海",
                 "shenzhen", "深圳", "hangzhou", "杭州", "guangzhou", "广州", "chengdu", "成都",
                 "nanjing", "南京", "wuhan", "武汉", "xi'an", "西安", "suzhou", "苏州",
                 "tianjin", "天津", "chongqing", "重庆", "changsha", "长沙", "hefei", "合肥"]
_GREATER_CN = ["hong kong", "香港", "taiwan", "台湾", "taipei", "台北", "macau", "澳门", "singapore", "新加坡"]
_CN_ORGS = ["bytedance", "字节", "tiktok", "tencent", "腾讯", "alibaba", "阿里", "ant group", "蚂蚁",
            "baidu", "百度", "huawei", "华为", "meituan", "美团", "didi", "滴滴", "xiaomi", "小米",
            "jd.com", "京东", "netease", "网易", "kuaishou", "快手", "pinduoduo", "拼多多",
            "sensetime", "商汤", "megvii", "旷视", "moonshot", "月之暗面", "zhipu", "智谱",
            "deepseek", "深度求索", "minimax", "01.ai", "零一万物", "bilibili", "哔哩"]


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


def china_fit(cand: Dict[str, Any], llm_cn_lang: float | None = None) -> Dict[str, Any]:
    """中国契合度（启发式）：给中国公司用，衡量候选"能否直接为我所用"。

    只看**岗位相关的客观信号**——不做族裔推断：
      - 中文能力：优先用 Kimi 对候选自述文字的判定（llm_cn_lang 0-1），无则回退正则（有无中文字）
      - 地理/时区：位于中国 / 大中华区（结构化 location 字段）
      - 中国市场经验：现/曾任职中国公司（结构化 org 字段）
    输出 {level, score(0-1), reasons[]}，UI 标注"启发式信号、仅供参考"。
    """
    bio = cand.get("bio") or ""
    name = cand.get("name") or ""
    loc_raw = (cand.get("location") or "").strip()
    loc = loc_raw.lower()
    blob = (bio + " " + (cand.get("org") or "")).lower()
    reasons: List[str] = []
    score = 0.0

    # 中文能力：优先信 Kimi 的判定（看候选真实文字），无则回退"有无中文字"正则
    if llm_cn_lang is not None:
        if llm_cn_lang >= 0.7:
            score += 0.45
            reasons.append("能用中文工作（AI 判定）")
        elif llm_cn_lang >= 0.4:
            score += 0.25
            reasons.append("具备一定中文能力（AI 判定）")
        # < 0.4：AI 认为基本不具备 → 不计中文分（信 AI，不再用正则）
    elif _HAN_RE.search(bio):
        score += 0.45
        reasons.append("个人简介用中文")
    elif _HAN_RE.search(name):
        score += 0.25
        reasons.append("署名含中文")

    # 地理 / 时区
    if any(l in loc for l in _CN_LOCATIONS):
        score += 0.40
        reasons.append(f"位于中国（{loc_raw[:24]}）")
    elif any(l in loc for l in _GREATER_CN):
        score += 0.20
        reasons.append(f"位于大中华区（{loc_raw[:24]}）")

    # 中国市场 / 公司经验
    cn_org = next((o for o in _CN_ORGS if o in blob), None)
    if cn_org:
        score += 0.40
        reasons.append(f"中国市场/公司经验（{cn_org}）")

    score = min(1.0, round(score, 2))
    level = "high" if score >= 0.6 else "medium" if score >= 0.3 else "low"
    return {"level": level, "score": score, "reasons": reasons}


def rank_score(cand: Dict[str, Any], china_boost: float) -> float:
    """总榜排序分 = 跨渠道加权分 + 中国契合加成。

    相关性/加权分仍是主导，china_fit 只做"往上顶"——给中国公司用时把本地能上手的人提前，
    但不会把跟难题不沾边的人顶到前面（大的契合度差距盖不过去）。
    """
    base = cand.get("weighted_score", cand.get("problem_fit_score", 0.0))
    cf = (cand.get("china_fit") or {}).get("score", 0.0) or 0.0
    return round(base + china_boost * cf, 1)


def apply_weight(cands: List[Dict[str, Any]], weight: float) -> None:
    """按通道权重对该通道候选的最终分做轻度加权（仅用于跨渠道排序）。

    诚实标注：渠道内排名比跨渠道绝对分更可靠（spec §7.1 注）。
    采用温和加权 0.6 + 0.4×weight*2，避免某通道被完全压没。
    """
    factor = 0.6 + 0.4 * min(1.0, weight * 2)
    for c in cands:
        c["weighted_score"] = round(c["problem_fit_score"] * factor, 1)
