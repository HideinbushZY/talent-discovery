"""LLM 层：Kimi / Moonshot（OpenAI 兼容接口）。

- 阶段1：难题理解（分型 + 成熟度 + 逐通道适用性 + 拆解）。
- 阶段3：相关性复核 + 画像（why_relevant），批量一次调用。
kimi-k2.6 默认开启 thinking，与强制具名 tool_choice 不兼容，因此统一用
JSON 输出模式（response_format=json_object）+ 提示里给出 JSON 模板来约束结构。
若 LLM 不可用，提供启发式兜底，保证 demo 仍能跑（会在 notes 标注）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from . import config
from . import observability as obs

_LOG = obs.get_logger("llm")
_resolved_model: Optional[str] = None
_resolve_lock: Optional[asyncio.Lock] = None


async def _chat_json(system: str, user: str, max_tokens: int = 2500,
                     retries: int = 2) -> Dict[str, Any]:
    """调用 Kimi chat/completions，强制 JSON 输出并解析为 dict。

    kimi-k2.6（thinking 模型）只接受 temperature=1，故不传该参数用默认值。
    韧性：失败/空内容/解析错时重试，并**提高 token 预算**（空内容多半是 thinking 把
    预算吃光），带退避。retries=0 可关闭重试（测试用）。
    """
    if not config.KIMI_API_KEY:
        raise RuntimeError("未配置 KIMI_API_KEY")
    headers = {"Authorization": f"Bearer {config.KIMI_API_KEY}",
               "Content-Type": "application/json"}
    budget = max_tokens
    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=200) as client:
        for attempt in range(retries + 1):
            payload = {
                "model": config.KIMI_MODEL,
                "max_tokens": budget,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            try:
                r = await client.post(f"{config.KIMI_BASE_URL}/chat/completions",
                                      headers=headers, json=payload)
                if r.status_code != 200:
                    raise RuntimeError(f"Kimi API {r.status_code}: {r.text[:160]}")
                content = r.json()["choices"][0]["message"].get("content") or ""
                if not content.strip():
                    raise RuntimeError("模型返回空内容（可能 thinking 占满 token）")
                return _parse_json(content)
            except Exception as e:  # noqa: BLE001
                last_err = e
                next_budget = min(int(budget * 1.6), 16000)   # 下次给更大预算
                if attempt < retries:
                    obs.log(_LOG, logging.WARNING, "kimi_retry", attempt=attempt + 1,
                            error=str(e)[:100], next_budget=next_budget)
                    budget = next_budget
                    await asyncio.sleep(0.6 * (attempt + 1))
                else:
                    budget = next_budget
    raise last_err if last_err else RuntimeError("Kimi 调用失败")


def _parse_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    # 去掉可能的 ```json ... ``` 包裹
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text
        text = text.lstrip("json").lstrip("JSON").strip().strip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 容错：截取第一个 { 到最后一个 }
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            return json.loads(text[i:j + 1])
        raise


async def resolve_model() -> str:
    """验证 Kimi 连通并返回模型名（缓存）。供健康检查/进度展示用。"""
    global _resolved_model, _resolve_lock
    if _resolved_model:
        return _resolved_model
    if _resolve_lock is None:
        _resolve_lock = asyncio.Lock()
    async with _resolve_lock:
        if _resolved_model:
            return _resolved_model
        if not config.KIMI_API_KEY:
            raise RuntimeError("未配置 KIMI_API_KEY")
        headers = {"Authorization": f"Bearer {config.KIMI_API_KEY}"}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{config.KIMI_BASE_URL}/models", headers=headers)
            if r.status_code != 200:
                raise RuntimeError(f"Kimi 鉴权/连通失败 {r.status_code}: {r.text[:120]}")
            ids = [m.get("id") for m in r.json().get("data", [])]
        if config.KIMI_MODEL not in ids:
            raise RuntimeError(f"模型 {config.KIMI_MODEL} 不在可用列表：{ids[:8]}")
        _resolved_model = config.KIMI_MODEL
        return _resolved_model


# ─────────────────────────────────────────────────────────────
# 阶段 1：难题理解
# ─────────────────────────────────────────────────────────────
_ANALYZE_SYSTEM = """你是"从问题出发的人才发现"系统的难题理解层。给定一个企业难题，你要：
1) 识别 domain 与 category：technical / marketing / other（经营、业务、管理等都归 other）。
2) 逐通道适用性：分别判断 GitHub、X 是否真有能解决此难题的人才。
   - GitHub 找"动手实现/构建过这类问题的人"。对纯品牌/创意营销、纯管理/经营类难题，GitHub 通常没有对应人才信号 → applicable=false、weight=0，并在 reason 里诚实说明。
   - X 找"在这类问题上最前沿、最有话语权的人"。绝大多数难题 X 都 applicable。
3) 权重 weight 只在 applicable 的通道间分配并归一化（两个都适用就分配如 0.6/0.4；只剩一个就 1.0；不适用的为 0）。
4) 为 applicable 的通道给出可检索信号：GitHub 给真实存在的 seed_repos（owner/repo）、code_search_queries、relevant_paths_hint；X 给 keywords（英文为主）、phrases。
诚实优先：难题该塌回单通道就塌回，不要硬凑。

只输出一个 JSON 对象，结构严格如下（不要任何多余文字）：
{
  "domain": "中文领域名，简洁",
  "category": "technical | marketing | other",
  "subproblems": ["子问题1", "子问题2", "子问题3"],
  "github": {
    "applicable": true,
    "reason": "中文，说明 GitHub 上是否有能解决此难题的人才",
    "weight": 0.6,
    "seed_repos": ["owner/repo", "owner/repo"],
    "code_search_queries": ["query1", "query2"],
    "relevant_paths_hint": ["path/hint"]
  },
  "x": {
    "applicable": true,
    "reason": "中文，说明 X 上是否有此难题的前沿/有话语权的人",
    "weight": 0.4,
    "keywords": ["kw1", "kw2", "kw3"],
    "phrases": ["exact phrase"]
  }
}"""


async def analyze_problem(problem: str) -> Dict[str, Any]:
    # 预算要给 thinking 留足空间（kimi-k2.6 的思考 token 也计入 max_tokens）
    data = await _chat_json(_ANALYZE_SYSTEM, f"企业难题：{problem}", max_tokens=6000)
    return _normalize_analysis(data)


def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalize_analysis(d: Dict[str, Any]) -> Dict[str, Any]:
    """补全字段 + 归一化权重，转成 ProblemAnalysis 兼容的 dict。"""
    cat = d.get("category", "other")
    if cat not in ("technical", "marketing", "other"):
        cat = "other"
    maturity = "well_supported" if cat in ("technical", "marketing") else "experimental"

    def chan(raw: Dict[str, Any], kind: str) -> Dict[str, Any]:
        raw = raw or {}
        applicable = bool(raw.get("applicable", False))
        out = {
            "applicable": applicable,
            "reason": raw.get("reason", ""),
            "weight": _safe_float(raw.get("weight", 0)),
            "seed_repos": list(raw.get("seed_repos", []) or []),
            "code_search_queries": list(raw.get("code_search_queries", []) or []),
            "relevant_paths_hint": list(raw.get("relevant_paths_hint", []) or []),
            "keywords": list(raw.get("keywords", []) or []),
            "phrases": list(raw.get("phrases", []) or []),
        }
        if not applicable:
            out["weight"] = 0.0
        return out

    gh = chan(d.get("github", {}), "github")
    x = chan(d.get("x", {}), "x")

    total = (gh["weight"] if gh["applicable"] else 0) + (x["weight"] if x["applicable"] else 0)
    if total <= 0:
        act = [c for c in (gh, x) if c["applicable"]]
        for c in act:
            c["weight"] = 1.0 / len(act) if act else 0.0
    else:
        if gh["applicable"]:
            gh["weight"] = round(gh["weight"] / total, 3)
        if x["applicable"]:
            x["weight"] = round(x["weight"] / total, 3)

    return {
        "domain": d.get("domain", "未识别"),
        "category": cat,
        "maturity": maturity,
        "subproblems": list(d.get("subproblems", []) or [])[:4],
        "channels": {"github": gh, "x": x},
    }


# ─────────────────────────────────────────────────────────────
# 阶段 3：相关性复核 + 画像（批量）
# ─────────────────────────────────────────────────────────────
# kimi-k2.6 的 thinking token 计入 max_tokens，候选太多会把预算吃光导致空输出。
# 故把复核切成小批并行跑：每批小、各自留足 token、互不拖累（某批失败只丢该批）。
_REVIEW_BATCH = 10
_REVIEW_CONCURRENCY = 3


async def review_candidates(
    problem: str,
    subproblems: List[str],
    channel: str,
    candidates: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """返回 {id: {relevance, why_relevant}}。分小批并行复核，失败批跳过（走启发式兜底）。"""
    if not candidates:
        return {}
    batches = [candidates[i:i + _REVIEW_BATCH] for i in range(0, len(candidates), _REVIEW_BATCH)]
    sem = asyncio.Semaphore(_REVIEW_CONCURRENCY)

    async def run(batch):
        async with sem:
            return await _review_batch(problem, subproblems, channel, batch)

    out: Dict[str, Dict[str, Any]] = {}
    for r in await asyncio.gather(*(run(b) for b in batches)):
        out.update(r)
    return out


async def _review_batch(problem, subproblems, channel, candidates) -> Dict[str, Dict[str, Any]]:
    lines = []
    for c in candidates:
        ev = "; ".join(
            f"{e.get('type','')}:{e.get('description','')}({e.get('metric','')})"
            for e in c.get("evidence", [])[:4]
        )
        lines.append(json.dumps(
            {"id": c["id"], "name": c.get("name") or c.get("handle"),
             "bio": (c.get("bio") or "")[:200], "source": c.get("source"),
             "evidence": ev[:600]},
            ensure_ascii=False,
        ))
    system = (
        "你在为'从问题出发的人才发现'做相关性复核。给定企业难题和一批来自"
        f"{channel} 的候选人（含其证据），判断每人与难题的真实契合度（0-1），"
        "并写一句中文 why（引用其具体证据）。蹭词、明显无关、bot 给低分。\n"
        '只输出 JSON 对象，结构：{"reviews":[{"id":"原样id","relevance":0.0到1.0的数字,'
        '"why_relevant":"一句中文"}]}，对每个候选人都给一条。'
    )
    user = (f"难题：{problem}\n子问题：{', '.join(subproblems)}\n\n候选人（每行一个 JSON）：\n"
            + "\n".join(lines))
    try:
        data = await _chat_json(system, user, max_tokens=6000)
    except Exception:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for r in data.get("reviews", []) or []:
        rid = r.get("id")
        if not rid:
            continue
        out[rid] = {
            "relevance": max(0.0, min(1.0, _safe_float(r.get("relevance", 0)))),
            "why_relevant": r.get("why_relevant", ""),
        }
    return out


# ─────────────────────────────────────────────────────────────
# 启发式兜底（LLM 不可用时）
# ─────────────────────────────────────────────────────────────
_TECH_HINTS = ["rag", "向量", "vector", "检索", "延迟", "latency", "模型", "训练", "推理",
               "数据库", "api", "编译", "kernel", "gpu", "分布式", "索引", "embedding",
               "工程", "算法", "代码", "sdk", "爬虫", "前端", "后端", "系统"]
_MKT_HINTS = ["品牌", "营销", "增长", "用户", "辨识度", "记忆点", "内容", "投放",
              "social", "brand", "marketing", "growth", "广告", "传播", "种草"]


def heuristic_analysis(problem: str) -> Dict[str, Any]:
    p = problem.lower()
    tech = sum(h in p for h in _TECH_HINTS)
    mkt = sum(h in p for h in _MKT_HINTS)
    if tech >= mkt and tech > 0:
        cat = "technical"
    elif mkt > 0:
        cat = "marketing"
    else:
        cat = "other"

    github_ok = cat == "technical"
    raw = {
        "domain": problem[:40],
        "category": cat,
        "subproblems": [problem[:60]],
        "github": {
            "applicable": github_ok,
            "reason": "技术类难题，GitHub 上可能有动手实现者" if github_ok
            else "非技术/工具类难题，GitHub 上通常无对应人才（启发式判断）",
            "weight": 0.6 if github_ok else 0,
            "seed_repos": [],
            "code_search_queries": [w for w in problem.split() if len(w) > 3][:3],
            "relevant_paths_hint": [],
        },
        "x": {
            "applicable": True,
            "reason": "X 上通常有该话题的讨论者",
            "weight": 0.4 if github_ok else 1.0,
            "keywords": [w for w in problem.replace("，", " ").replace("。", " ").split() if len(w) > 2][:5],
            "phrases": [],
        },
    }
    return _normalize_analysis(raw)
