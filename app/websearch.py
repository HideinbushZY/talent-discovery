"""联网发现：用搜索引擎找相关 GitHub 仓库，补 LLM 不知道的（尤其中国本土栈）。

为什么需要：Kimi 不认识 sherpa-onnx/FunASR/WeNet/MiniCPM 这类中国开源栈（逼它给还会编），
但搜索引擎的索引认识。本模块把"靠模型记忆"换成"现搜 + 后续 GitHub 核实"。

产出 owner/repo 列表，并入 stage-1 的 seed_repos → 现有连接器拉其贡献者/作者。
后端：有 TAVILY_API_KEY 用 Tavily（带正文、召回高）；否则用免 key 的 ddgs(DuckDuckGo)。
任何失败都返回空（优雅降级，不影响主流程）。
"""
from __future__ import annotations

import asyncio
import re
from typing import List

import httpx

from . import config
from . import observability as obs

_log = obs.get_logger("websearch")
# 两种来源：github.com/owner/repo 链接；以及 ddgs 标题常见的 "GitHub - owner/repo: …" 形式。
# 链接用域名边界 lookbehind，避免把 docs.github.com / help.github.com 等子域误当仓库 owner。
_GH_URL_RE = re.compile(r"(?<![\w.])github\.com/([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*)", re.I)
_GH_TITLE_RE = re.compile(r"GitHub\s*[-—:|]\s*([A-Za-z0-9][\w-]*)/([A-Za-z0-9][\w.-]*)", re.I)
# GitHub 自家页面/文档路径，不是人才仓库
_RESERVED_OWNER = {"topics", "search", "sponsors", "about", "features", "marketplace",
                   "collections", "explore", "trending", "settings", "orgs", "users",
                   "login", "join", "pricing", "site", "apps", "readme", "notifications",
                   "github", "en", "zh", "zh-hans", "code-search", "search-github",
                   "customer-stories", "enterprise", "security", "sponsors", "contact"}
_NON_REPO = {"blob", "tree", "wiki", "issues", "pulls", "releases", "actions", "graphs",
             "stargazers", "explore", "articles", "topics", "search"}


def _extract_repos(texts: List[str]) -> List[str]:
    found: List[str] = []
    for t in texts:
        t = t or ""
        for rx in (_GH_URL_RE, _GH_TITLE_RE):
            for m in rx.finditer(t):
                owner, repo = m.group(1), m.group(2)
                repo = repo.rstrip(".,)]\"'").removesuffix(".git")
                if not repo or owner.lower() in _RESERVED_OWNER or repo.lower() in _NON_REPO:
                    continue
                full = f"{owner}/{repo}"
                if full not in found:
                    found.append(full)
    return found


async def _tavily(client: httpx.AsyncClient, query: str, k: int) -> List[str]:
    r = await client.post(
        "https://api.tavily.com/search",
        json={"api_key": config.TAVILY_API_KEY, "query": query,
              "max_results": k, "include_raw_content": True},
        timeout=20,
    )
    if r.status_code != 200:
        return []
    texts: List[str] = []
    for it in r.json().get("results", []):
        texts += [it.get("url", ""), it.get("content", ""), it.get("raw_content") or ""]
    return texts


def _ddg_sync(query: str, k: int) -> List[str]:
    try:
        from ddgs import DDGS
        texts: List[str] = []
        for x in DDGS().text(query, max_results=k):
            texts += [x.get("href") or x.get("url") or "", x.get("title", ""), x.get("body") or ""]
        return texts
    except Exception as e:  # noqa: BLE001
        _log.warning("ddgs failed: %s", str(e)[:120])
        return []


async def discover(queries: List[str], per_query: int = 6, cap: int = 12) -> List[str]:
    """对若干查询联网搜索，抽取相关 GitHub owner/repo（去重、限量）。"""
    if not config.WEB_SEARCH or not queries:
        return []
    repos: List[str] = []
    async with httpx.AsyncClient() as client:
        for q in queries[:3]:
            try:
                if config.TAVILY_API_KEY:
                    texts = await _tavily(client, q, per_query)
                else:
                    texts = await asyncio.to_thread(_ddg_sync, q, per_query)
            except Exception as e:  # noqa: BLE001
                obs.log(_log, 30, "web_search_failed", query=q[:60], error=str(e)[:100])
                texts = []
            for full in _extract_repos(texts):
                if full not in repos:
                    repos.append(full)
    return repos[:cap]
