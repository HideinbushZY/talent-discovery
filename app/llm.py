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
                     retries: int = 2, providers: list | None = None) -> Dict[str, Any]:
    """强制 JSON 输出并解析为 dict。多供应商兜底：主供应商耗尽重试后降级到下一个。

    providers 可覆盖（如复核阶段传入更快的非思考模型）；默认用 config.LLM_PROVIDERS。
    """
    providers = providers if providers is not None else config.LLM_PROVIDERS
    if not providers:
        raise RuntimeError("未配置任何 LLM 供应商（KIMI_API_KEY）")
    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=120) as client:
        for pi, prov in enumerate(providers):
            try:
                return await _chat_one(client, prov, system, user, max_tokens, retries)
            except Exception as e:  # noqa: BLE001
                last_err = e
                if pi + 1 < len(providers):
                    obs.log(_LOG, logging.WARNING, "llm_provider_failover",
                            error=str(e)[:100], **{"from": prov["name"], "to": providers[pi + 1]["name"]})
    raise last_err if last_err else RuntimeError("LLM 调用失败")


async def _chat_one(client: httpx.AsyncClient, prov: Dict[str, Any],
                    system: str, user: str, max_tokens: int, retries: int) -> Dict[str, Any]:
    """单个供应商：强制 JSON 输出 + 失败/空内容重试并提高 token 预算（不传 temperature）。"""
    headers = {"Authorization": f"Bearer {prov['api_key']}", "Content-Type": "application/json"}
    budget = max_tokens
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        payload = {
            "model": prov["model"],
            "max_tokens": budget,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            r = await client.post(f"{prov['base_url']}/chat/completions", headers=headers, json=payload)
            if r.status_code != 200:
                raise RuntimeError(f"{prov['name']} API {r.status_code}: {r.text[:160]}")
            content = r.json()["choices"][0]["message"].get("content") or ""
            if not content.strip():
                raise RuntimeError("模型返回空内容（可能 thinking 占满 token）")
            return _parse_json(content)
        except Exception as e:  # noqa: BLE001
            last_err = e
            next_budget = min(int(budget * 1.6), 16000)
            if attempt < retries:
                obs.log(_LOG, logging.WARNING, "llm_retry", provider=prov["name"],
                        attempt=attempt + 1, error=str(e)[:100], next_budget=next_budget)
                budget = next_budget
                await asyncio.sleep(0.6 * (attempt + 1))
            else:
                budget = next_budget
    raise last_err if last_err else RuntimeError("LLM 调用失败")


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
5) **known_people（关键）**：如果你**确知**该难题领域有公认的开源作者/maintainer/技术负责人，在 GitHub 的 known_people 里**直接点名**：
   - name（中文或英文人名）、handle（其 GitHub 用户名，**只有你有把握是真实的才填**）、why（一句他为何是该领域关键人，引用其代表项目）。
   - 尽量给该领域**最有代表性的真实的人**（论文一作、知名 repo 作者、行业 lead）；**宁缺毋滥，绝不编造 handle**。不确定 handle 就别写这条，把其代表项目放进 seed_repos。
   - 系统会逐个用 GitHub API 核实你给的 handle，编的会被丢弃，但点名能让系统发现"光靠仓库 top 贡献者捞不到的关键人"。
6) **web_queries（关键）**：给 2-3 条**搜索引擎查询**（像在 Google/百度里找"这个领域有哪些开源项目/谁在做的"）。**优先用中文**（很多中国本土开源栈只在中文资料里出现）。系统会联网搜索这些 query、抽出相关 GitHub 仓库——这能补上你不知道的项目（尤其中国本土的）。例：「端侧 语音识别 唤醒词 开源 框架」「中文 大模型 Agent 工具调用 github」。
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
    "relevant_paths_hint": ["path/hint"],
    "known_people": [
      {"name": "人名", "handle": "github用户名", "why": "一句：他为何是该领域关键人，代表项目"}
    ]
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
            "known_people": [p for p in (raw.get("known_people", []) or []) if isinstance(p, dict)][:10],
            "web_queries": [str(q) for q in (raw.get("web_queries", []) or []) if q][:4],
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
        self_text = ((c.get("bio") or "") + " " + (c.get("_self_text") or "")).strip()
        lines.append(json.dumps(
            {"id": c["id"], "name": c.get("name") or c.get("handle"),
             "bio": (c.get("bio") or "")[:200], "source": c.get("source"),
             "evidence": ev[:600], "self_text": self_text[:400]},
            ensure_ascii=False,
        ))
    system = (
        "你在为'从问题出发的人才发现'做相关性复核。给定企业难题和一批来自"
        f"{channel} 的候选人（含其证据），对每人做三件事：\n"
        "1) relevance：与难题的真实契合度（0-1）；蹭词/明显无关/bot 给低分。\n"
        "2) why_relevant：一句中文，引用其具体证据。\n"
        "3) cn_lang：其**中文工作能力**（0-1）——只依据 self_text（候选人**自己写**的文字："
        "简介/提交信息/帖子原文）：全是流利中文≈1，夹杂中文/能读写≈0.5，看不出中文能力≈0。"
        "注意：只看 self_text，**不要**被 evidence 里系统生成的中文描述（如「X 的贡献者」）误导。\n"
        '只输出 JSON：{"reviews":[{"id":"原样id","relevance":数字,"why_relevant":"一句中文","cn_lang":数字}]}，每人一条。'
    )
    user = (f"难题：{problem}\n子问题：{', '.join(subproblems)}\n\n候选人（每行一个 JSON）：\n"
            + "\n".join(lines))
    try:
        # 复核走更快的非思考模型（config.LLM_REVIEW_PROVIDERS），失败回退到主供应商
        data = await _chat_json(system, user, max_tokens=6000,
                                providers=config.LLM_REVIEW_PROVIDERS)
    except Exception:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for r in data.get("reviews", []) or []:
        rid = r.get("id")
        if not rid:
            continue
        cn = r.get("cn_lang")
        out[rid] = {
            "relevance": max(0.0, min(1.0, _safe_float(r.get("relevance", 0)))),
            "why_relevant": r.get("why_relevant", ""),
            "cn_lang": None if cn is None else max(0.0, min(1.0, _safe_float(cn))),
        }
    return out


# ─────────────────────────────────────────────────────────────
# 结果导读（轻量摘要）——给招人方做决策用，严格只用已采集候选、不发挥
# ─────────────────────────────────────────────────────────────
async def summarize_results(problem: str, subproblems: List[str],
                            candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """生成 grounded 导读：landscape + 建议先接触谁 + 分组。

    只基于传入的候选（已筛选+核验）；提到的人必须用名单里的 handle。
    代码侧再校验一遍 handle 真实存在，过滤掉模型可能编出来的人。失败返回 None（优雅降级）。
    """
    cands = [c for c in candidates if c.get("handle")][:15]
    if not cands:
        return None
    by_handle = {c["handle"]: c for c in cands}
    items = []
    for c in cands:
        items.append({
            "handle": c["handle"],
            "name": c.get("name") or c["handle"],
            "source": c.get("source"),
            "why": (c.get("why_relevant") or "")[:160],
            "evidence": [(e.get("description") or "")[:80] for e in (c.get("evidence") or [])[:2]],
            "china_fit": (c.get("china_fit") or {}).get("level"),
            "hireability": (c.get("hireability") or {}).get("level"),
        })
    system = (
        "你在为'从问题出发的人才发现'写一段**给招聘方做决策用的导读**。下面给你企业难题和一份"
        "**已筛选+核验过的候选名单**（每人含 handle、为何相关、证据、china_fit、可挖性）。\n\n"
        "硬性要求：\n"
        "- 只能用给你的候选信息；提到的每个人都必须用名单里出现过的 handle；**绝不编造任何人、"
        "仓库或事实**，没依据就不写。\n"
        "- 读者是接下来要去**接触人**的招聘方，要可执行、具体、不说空话套话。\n\n"
        "产出三部分：\n"
        "1) overview：2-4 句中文，讲清这批人大致分成哪几类、整体证据强弱（硬证据=可核验的代码贡献 / "
        "软证据=观点影响力），像当面跟招人的人交代。\n"
        "2) recommended_first：从名单里挑 2-3 个**最该先接触**的人，每人一句理由（结合相关性/证据/"
        "可挖性/china_fit）。\n"
        "3) groups：把候选按子方向/技术线分 2-4 组，每组一个简短中文标签 + 该组 handle 列表。\n\n"
        '只输出 JSON：{"overview":"...","recommended_first":[{"handle":"...","reason":"..."}],'
        '"groups":[{"label":"...","handles":["..."]}]}'
    )
    user = (f"难题：{problem}\n子问题：{', '.join(subproblems)}\n\n"
            "候选名单（每行一个 JSON）：\n"
            + "\n".join(json.dumps(it, ensure_ascii=False) for it in items))
    try:
        # 走更快的非思考模型；失败/异常优雅降级为无摘要
        data = await _chat_json(system, user, max_tokens=2000, providers=config.LLM_REVIEW_PROVIDERS)
    except Exception:  # noqa: BLE001
        return None

    valid = set(by_handle)
    overview = (data.get("overview") or "").strip()[:600]
    rec: List[Dict[str, Any]] = []
    for r in (data.get("recommended_first") or []):
        h = (r.get("handle") or "").strip()
        if h in valid and h not in [x["handle"] for x in rec]:   # 防幻觉：handle 必须真实存在
            rec.append({"handle": h, "name": by_handle[h].get("name") or h,
                        "reason": (r.get("reason") or "")[:120]})
    groups: List[Dict[str, Any]] = []
    for g in (data.get("groups") or []):
        hs = [h for h in (g.get("handles") or []) if h in valid]
        label = (g.get("label") or "").strip()
        if label and hs:
            groups.append({"label": label[:24], "handles": hs})
    if not overview and not rec:
        return None
    return {"overview": overview, "recommended_first": rec[:3], "groups": groups[:4]}


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
