"""XConnector（spec §6.2）：话题相关近期帖搜索，聚合到高影响力/高互动作者。

成本控制（spec §6.3）：每次搜索硬性读取预算 X_READ_BUDGET（每条帖 ≈ 1 read ≈ $0.005），
超预算即停并在 report 里说明。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

import httpx

from .. import config
from ..cache import RateLimiter
from .base import Connector, ProgressCb, add_evidence, new_candidate

SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"

# 进程级 X 读取累计：防止多次/并发搜索（刷新、双标签页、爬虫）把付费读取叠加到失控。
# 单进程 async demo，模块全局即可（无需锁）。
_session_reads = 0


def _parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _build_query(keywords: List[str], phrases: List[str]) -> str:
    terms: List[str] = []
    for k in keywords[:6]:
        k = k.strip()
        if not k:
            continue
        terms.append(f'"{k}"' if " " in k else k)
    for p in phrases[:2]:
        p = p.strip()
        if p:
            terms.append(f'"{p}"')
    if not terms:
        return ""
    return f"({' OR '.join(terms)}) -is:retweet"


def _fmt_metric(n: int) -> str:
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


class XConnector(Connector):
    source = "x"

    def __init__(self):
        self.rl = RateLimiter(rate=4, period=60)  # pay-per-use 仍有分钟级限速，保守
        self.headers = {
            "Authorization": f"Bearer {config.X_BEARER_TOKEN}",
            "User-Agent": "talent-discovery-demo",
        }

    async def collect(self, plan: Dict[str, Any], progress: ProgressCb):
        global _session_reads
        report = {"collected": 0, "error": None, "note": "", "reads_used": 0}
        query = _build_query(plan.get("keywords", []), plan.get("phrases", []))
        if not query:
            report["error"] = "未生成有效检索词"
            return [], report

        budget = max(10, config.X_READ_BUDGET)
        reads_used = 0
        authors: Dict[str, Dict[str, Any]] = {}   # author_id -> aggregate
        users: Dict[str, Dict[str, Any]] = {}      # author_id -> user obj
        next_token = None

        await progress("x", f"搜索近期帖子（预算 ≤{budget} reads）：{query[:80]}")

        async with httpx.AsyncClient() as client:
            while reads_used < budget:
                remaining = budget - reads_used
                if remaining < 10:      # 不足 X API 最小页(10)，停止以免超预算
                    break
                if _session_reads >= config.X_SESSION_READ_CAP:
                    report["note"] = (report["note"] or "") + \
                        f"已达进程级 X 读取总上限({config.X_SESSION_READ_CAP})，停止。"
                    break
                page = min(100, remaining)
                params = {
                    "query": query,
                    "max_results": page,
                    "tweet.fields": "public_metrics,created_at,author_id,lang",
                    "expansions": "author_id",
                    "user.fields": "public_metrics,description,location,profile_image_url,name,username,url,verified",
                }
                if next_token:
                    params["next_token"] = next_token
                await self.rl.acquire()
                try:
                    r = await client.get(SEARCH_URL, params=params, headers=self.headers, timeout=30)
                except Exception as e:  # noqa: BLE001
                    report["error"] = f"请求失败：{e}"
                    break
                if r.status_code == 429:
                    report["note"] = "命中 X 分钟级限速，已用部分结果返回。"
                    break
                if r.status_code == 402:
                    report["error"] = ("X 账户按量付费余额已用尽（credits depleted）——"
                                       "请到 console.x.com 充值后重试；本次已跳过 X 通道。")
                    break
                if r.status_code == 403:
                    report["error"] = "X 访问被拒（403）：可能是 App 权限/计费未就绪。"
                    break
                if r.status_code == 401:
                    report["error"] = "X 鉴权失败（401）：Bearer Token 无效或已失效。"
                    break
                if r.status_code != 200:
                    report["error"] = f"X API 返回 {r.status_code}"
                    break

                body = r.json()
                tweets = body.get("data", []) or []
                inc_users = {u["id"]: u for u in (body.get("includes", {}).get("users", []) or [])}
                users.update(inc_users)
                reads_used += len(tweets)
                _session_reads += len(tweets)

                for t in tweets:
                    aid = t.get("author_id")
                    if not aid:
                        continue
                    pm = t.get("public_metrics", {}) or {}
                    eng = (pm.get("like_count", 0) + pm.get("retweet_count", 0)
                           + pm.get("reply_count", 0) + pm.get("quote_count", 0))
                    a = authors.setdefault(aid, {"count": 0, "engagement": 0, "latest": None, "top": None})
                    a["count"] += 1
                    a["engagement"] += eng
                    ts = _parse_ts(t.get("created_at"))
                    if ts and (a["latest"] or 0) < ts:
                        a["latest"] = ts
                    if not a["top"] or eng > a["top"]["eng"]:
                        a["top"] = {"id": t.get("id"), "eng": eng, "text": (t.get("text") or "")[:120], "pm": pm}

                next_token = body.get("meta", {}).get("next_token")
                if not next_token or not tweets:
                    break

        report["reads_used"] = reads_used

        # 聚合为候选
        cands: List[Dict[str, Any]] = []
        for aid, agg in authors.items():
            u = users.get(aid)
            if not u:
                continue
            handle = u.get("username", aid)
            c = new_candidate("x", handle)
            upm = u.get("public_metrics", {}) or {}
            c["name"] = u.get("name")
            c["bio"] = u.get("description")
            c["location"] = u.get("location")
            c["followers"] = upm.get("followers_count", 0)
            c["avatar_url"] = u.get("profile_image_url")
            c["profile_url"] = f"https://x.com/{handle}"
            c["contact_hint"] = u.get("url") or "DM (X)"
            c["_signals"]["relevance_hits"] = float(agg["count"])
            c["_signals"]["depth"] = float(agg["engagement"])
            c["_signals"]["recency_ts"] = agg["latest"]

            top = agg.get("top")
            if top and top.get("id"):
                pm = top["pm"]
                c["_self_text"] = (top.get("text") or "")[:600]   # 帖子原文，供 LLM 判定中文能力
                c["_signals"]["matched_paths"] = False
                add_evidence(
                    c, "post",
                    f"相关高互动帖：{top['text']}",
                    url=f"https://x.com/{handle}/status/{top['id']}",
                    metric=f"❤{_fmt_metric(pm.get('like_count',0))} ↺{_fmt_metric(pm.get('retweet_count',0))}",
                )
            add_evidence(c, "profile", f"X 主页 @{handle}", url=c["profile_url"],
                         metric=f"{_fmt_metric(c['followers'])} followers · 命中 {agg['count']} 帖")
            cands.append(c)

        # 取话题相关 + 影响力高的 Top N
        cands.sort(key=lambda c: (c["_signals"]["relevance_hits"],
                                  c["_signals"]["depth"],
                                  c["followers"]), reverse=True)
        cands = cands[: config.TOP_N_PER_CHANNEL]
        report["collected"] = len(cands)
        if not cands and not report["error"]:
            report["note"] = report["note"] or f"近期帖中未聚合到有效作者（读取 {reads_used} 条）。"
        return cands, report
