"""GitHubConnector（spec §6.1）：核心 repo 贡献排名 + 全网代码搜索，去重到"人"。"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List

import httpx

from .. import config
from ..cache import RateLimiter, TTLCache, gather_limited
from .base import Connector, ProgressCb, add_evidence, new_candidate

API = "https://api.github.com"
# owner/repo 合法格式，防止把不可信字符串拼进 API 路径（路径注入）
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_user_cache = TTLCache(ttl=1800)
_repo_cache = TTLCache(ttl=1800)


def _parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class GitHubConnector(Connector):
    source = "github"

    def __init__(self):
        self.core_rl = RateLimiter(rate=80, period=60)     # 远低于 5000/hr
        self.search_rl = RateLimiter(rate=8, period=60)    # 代码搜索 ~10/min，留余量
        self.headers = {
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "talent-discovery-demo",
        }

    async def _get(self, client: httpx.AsyncClient, path: str, params=None, search=False):
        rl = self.search_rl if search else self.core_rl
        await rl.acquire()
        url = path if path.startswith("http") else f"{API}{path}"
        r = await client.get(url, params=params, headers=self.headers, timeout=30)
        # 主限速(403)、二级限速、滥用检测、429 都按"限速"处理（上层 catch → 返回空，优雅降级）
        if r.status_code == 429 or (r.status_code == 403 and (
                "rate limit" in r.text.lower() or "secondary" in r.text.lower())):
            raise RuntimeError("github rate limited")
        r.raise_for_status()
        return r.json()

    # ── 数据获取 ──────────────────────────────────────────────
    async def _contributors(self, client, full: str, limit=20):
        try:
            data = await self._get(client, f"/repos/{full}/contributors", {"per_page": limit, "anon": "false"})
            return [c for c in data if c.get("type") == "User"][:limit]
        except Exception:
            return []

    async def _commits_on_path(self, client, full: str, path: str, limit=20):
        try:
            return await self._get(client, f"/repos/{full}/commits",
                                   {"path": path, "per_page": limit})
        except Exception:
            return []

    async def _code_search(self, client, query: str, limit=15):
        try:
            data = await self._get(client, "/search/code",
                                   {"q": query, "per_page": limit}, search=True)
            return data.get("items", [])
        except Exception:
            return []

    async def _enrich_user(self, client, login: str) -> Dict[str, Any]:
        cached = _user_cache.get(login)
        if cached is not None:
            return cached
        try:
            u = await self._get(client, f"/users/{login}")
        except Exception:
            u = {}
        _user_cache.set(login, u)
        return u

    # ── 主流程 ────────────────────────────────────────────────
    async def collect(self, plan: Dict[str, Any], progress: ProgressCb):
        cands: Dict[str, Dict[str, Any]] = {}
        report = {"collected": 0, "error": None, "note": ""}

        def cand_for(login: str) -> Dict[str, Any]:
            key = login.lower()
            if key not in cands:
                cands[key] = new_candidate("github", login)
            return cands[key]

        seed_repos = [r for r in plan.get("seed_repos", []) if _REPO_RE.match(r or "")][:6]
        path_hints = plan.get("relevant_paths_hint", [])[:3]
        queries = plan.get("code_search_queries", [])[:3]

        async with httpx.AsyncClient() as client:
            # A. 核心 repo 贡献排名
            await progress("github", f"拉取 {len(seed_repos)} 个核心仓库的贡献者…")

            async def do_repo(full):
                out = []
                contribs = await self._contributors(client, full)
                for c in contribs:
                    out.append(("contrib", full, c))
                # 相关模块/路径提交者
                for hint in path_hints:
                    commits = await self._commits_on_path(client, full, hint, limit=15)
                    for cm in commits:
                        out.append(("pathcommit", f"{full}|{hint}", cm))
                return out

            repo_results = await gather_limited(seed_repos, do_repo, concurrency=4)
            for res in repo_results:
                if not res:
                    continue
                for kind, ctx, obj in res:
                    if kind == "contrib":
                        login = obj.get("login")
                        if not login:
                            continue
                        c = cand_for(login)
                        c["avatar_url"] = obj.get("avatar_url")
                        c["profile_url"] = obj.get("html_url")
                        n = obj.get("contributions", 0)
                        c["_signals"]["depth"] += n
                        c["_signals"]["relevance_hits"] += 1.0
                        add_evidence(c, "repo", f"{ctx} 的贡献者",
                                     url=f"https://github.com/{ctx}/graphs/contributors",
                                     metric=f"{n} contributions")
                    else:  # pathcommit
                        full, hint = ctx.split("|", 1)
                        author = obj.get("author") or {}
                        login = author.get("login")
                        if not login:
                            continue
                        c = cand_for(login)
                        c["avatar_url"] = author.get("avatar_url")
                        c["profile_url"] = author.get("html_url")
                        c["_signals"]["matched_paths"] = True
                        c["_signals"]["relevance_hits"] += 2.0
                        c["_signals"]["depth"] += 3
                        ts = _parse_ts(((obj.get("commit") or {}).get("author") or {}).get("date"))
                        if ts and (c["_signals"]["recency_ts"] or 0) < ts:
                            c["_signals"]["recency_ts"] = ts
                        _lines = ((obj.get("commit") or {}).get("message") or "").splitlines()
                        msg = (_lines[0] if _lines else "")[:60]
                        add_evidence(c, "commit", f"在 {full} 的 `{hint}` 模块提交：{msg}",
                                     url=obj.get("html_url"), metric="相关模块提交")

            # B. 全网相关代码搜索
            if queries:
                await progress("github", f"代码搜索 {len(queries)} 条 query（限速较严，稍候）…")
            secondary_repos: Dict[str, int] = {}
            for q in queries:
                items = await self._code_search(client, q)
                for it in items:
                    repo = it.get("repository") or {}
                    owner = repo.get("owner") or {}
                    ologin = owner.get("login")
                    otype = owner.get("type")
                    full = repo.get("full_name", "")
                    if otype == "User" and ologin:
                        c = cand_for(ologin)
                        c["avatar_url"] = owner.get("avatar_url")
                        c["profile_url"] = owner.get("html_url")
                        c["_signals"]["relevance_hits"] += 1.5
                        c["_signals"]["matched_paths"] = True
                        add_evidence(c, "code", f"其仓库 {full} 命中难题相关代码（{q}）",
                                     url=it.get("html_url"))
                    elif full:
                        secondary_repos[full] = secondary_repos.get(full, 0) + 1

            # 代码搜索命中的 org 仓库 → 取其 top 贡献者补人
            sec = sorted(secondary_repos.items(), key=lambda kv: -kv[1])[:3]
            if sec:
                async def do_sec(item):
                    full, _ = item
                    return full, await self._contributors(client, full, limit=8)
                for full, contribs in await gather_limited(sec, do_sec, concurrency=3):
                    for obj in contribs or []:
                        login = obj.get("login")
                        if not login:
                            continue
                        c = cand_for(login)
                        c["avatar_url"] = obj.get("avatar_url")
                        c["profile_url"] = obj.get("html_url")
                        c["_signals"]["relevance_hits"] += 1.0
                        c["_signals"]["depth"] += obj.get("contributions", 0)
                        add_evidence(c, "code", f"活跃于代码搜索命中的仓库 {full}",
                                     url=f"https://github.com/{full}")

            # C. 取 Top N 充实 profile（bio/followers/location/org）
            ranked = sorted(cands.values(),
                            key=lambda c: (c["_signals"]["matched_paths"],
                                           c["_signals"]["relevance_hits"],
                                           c["_signals"]["depth"]),
                            reverse=True)[: config.TOP_N_PER_CHANNEL]
            await progress("github", f"充实 {len(ranked)} 位候选的资料…")

            async def enrich(c):
                u = await self._enrich_user(client, c["handle"])
                if u:
                    c["name"] = u.get("name") or c["handle"]
                    c["bio"] = u.get("bio")
                    c["location"] = u.get("location")
                    c["org"] = u.get("company")
                    c["followers"] = u.get("followers", 0)
                    c["avatar_url"] = c["avatar_url"] or u.get("avatar_url")
                    c["profile_url"] = c["profile_url"] or u.get("html_url")
                    email = u.get("email")
                    blog = u.get("blog")
                    if email:
                        c["contact_hint"] = email
                    elif blog:
                        c["contact_hint"] = blog
                    add_evidence(c, "profile", f"GitHub 主页：{c['name']}", url=u.get("html_url"),
                                 metric=f"{u.get('followers',0)} followers · {u.get('public_repos',0)} repos")
                return c

            ranked = [c for c in await gather_limited(ranked, enrich, concurrency=8) if c]

        report["collected"] = len(ranked)
        return ranked, report
