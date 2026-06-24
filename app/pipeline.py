"""四阶段管线编排（spec §4）。run_pipeline 是异步生成器，实时吐进度事件，最后吐结果。"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List

from . import config, llm, scoring
from . import observability as obs
from .connectors.github import GitHubConnector
from .connectors.x import XConnector
from .models import Candidate, ChannelReport, SearchResult

_SENTINEL = object()


def _event(**kw) -> Dict[str, Any]:
    return kw


async def run_pipeline(problem: str) -> AsyncIterator[Dict[str, Any]]:
    queue: asyncio.Queue = asyncio.Queue()

    async def progress(channel: str, message: str):
        await queue.put(_event(type="progress", channel=channel, message=message))

    async def orchestrate():
        trace = obs.Trace(obs.new_request_id(), problem)
        trace.started()
        notes: List[str] = []
        try:
            # ── 阶段 1：难题理解 ──────────────────────────────
            trace.start("analyze")
            await queue.put(_event(type="status", stage=1, message="阶段1：难题理解 + 逐通道路由（Kimi）…"))
            try:
                analysis = await llm.analyze_problem(problem)
            except Exception as e:  # noqa: BLE001
                analysis = llm.heuristic_analysis(problem)
                notes.append(f"⚠ Kimi 难题理解失败，已用启发式兜底（结果偏弱）：{str(e)[:120]}")
                trace.event("llm_analyze_fallback", error=str(e)[:120])
                obs.capture_exception(e)
            trace.end("analyze")

            await queue.put(_event(type="analysis", data=analysis))

            channels = analysis["channels"]
            gh_plan = channels["github"]
            x_plan = channels["x"]

            # ── 阶段 2：双渠道并行采集（只跑 applicable）────────
            trace.start("collect")
            await queue.put(_event(type="status", stage=2, message="阶段2：双渠道并行采集（仅适用通道）…"))

            tasks = {}
            if gh_plan["applicable"] and config.HAS_GITHUB:
                tasks["github"] = asyncio.create_task(GitHubConnector().collect(gh_plan, progress))
            if x_plan["applicable"] and config.HAS_X:
                tasks["x"] = asyncio.create_task(XConnector().collect(x_plan, progress))

            collected: Dict[str, Any] = {}
            for name, task in tasks.items():
                try:
                    collected[name] = await task
                except Exception as e:  # noqa: BLE001
                    collected[name] = ([], {"collected": 0, "error": str(e)[:160]})
            trace.end("collect")

            # trace：逐通道采集结果 / 跳过
            for ch in ("github", "x"):
                if ch in collected:
                    _r = collected[ch][1]
                    if _r.get("error"):
                        trace.event("channel_error", channel=ch, error=str(_r["error"])[:120])
                    else:
                        trace.event("channel_ok", channel=ch, n=_r.get("collected", 0), degraded=False)
                elif not channels[ch]["applicable"]:
                    trace.event("channel_skipped", channel=ch, degraded=False)

            # ── 阶段 3：评分与画像 ────────────────────────────
            trace.start("review_score")
            await queue.put(_event(type="status", stage=3, message="阶段3：相关性复核 + 评分 + 画像（Kimi）…"))
            subproblems = analysis.get("subproblems", [])
            all_cands: List[Dict[str, Any]] = []

            async def review_and_score(ch: str, cands: List[Dict[str, Any]]):
                if not cands:
                    return []
                reviews = await llm.review_candidates(problem, subproblems, ch, cands)
                if not reviews:
                    trace.event("review_empty", channel=ch)   # 复核全失败 → 走启发式
                for c in cands:
                    rv = reviews.get(c["id"], {})
                    rel = rv.get("relevance")
                    if ch == "github":
                        scoring.score_github(c, rel)
                    else:
                        scoring.score_x(c, rel)
                    why = rv.get("why_relevant")
                    if why:
                        c["why_relevant"] = why
                    elif not c.get("why_relevant"):
                        c["why_relevant"] = _fallback_why(c)
                    c["hireability"] = scoring.hireability(c)
                    c["china_fit"] = scoring.china_fit(c, llm_cn_lang=rv.get("cn_lang"))
                scoring.apply_weight(cands, channels[ch]["weight"])
                return cands

            # 两通道的 Claude 复核 + 评分并行跑
            scored = await asyncio.gather(
                *[review_and_score(ch, cands) for ch, (cands, _rep) in collected.items()]
            )
            for group in scored:
                all_cands.extend(group)
            trace.end("review_score")

            # ── 阶段 4：融合总榜 ──────────────────────────────
            all_cands.sort(key=lambda c: c.get("weighted_score", c["problem_fit_score"]), reverse=True)
            top = all_cands[:30]

            # 通道报告
            reports: List[ChannelReport] = []
            for ch in ("github", "x"):
                plan = channels[ch]
                rep = dict(collected.get(ch, ([], {}))[1]) if ch in collected else {}
                note = ""
                if not plan["applicable"]:
                    note = plan.get("reason", "该通道不适用，已跳过。")
                elif not config.HAS_GITHUB and ch == "github":
                    note = "未配置 GITHUB_TOKEN，跳过。"
                elif not config.HAS_X and ch == "x":
                    note = "未配置 X_API_BEARER_TOKEN，跳过。"
                elif rep.get("error"):
                    note = f"采集出错：{rep['error']}"
                elif rep.get("note"):
                    note = rep["note"]
                elif rep.get("collected", 0) == 0:
                    note = "该通道本次未找到匹配人才。"
                reports.append(ChannelReport(
                    channel=ch,
                    applicable=plan["applicable"],
                    reason=plan.get("reason", ""),
                    weight=plan.get("weight", 0.0),
                    note=note,
                    collected=rep.get("collected", 0),
                    error=rep.get("error"),
                ))

            if analysis["maturity"] == "experimental":
                notes.append("实验性：这类难题目前主要靠 X 的软证据，结果可能偏弱，仅供参考。")

            x_reads = collected.get("x", ([], {}))[1].get("reads_used", 0) if "x" in collected else 0
            gh_collected = collected.get("github", ([], {}))[1].get("collected", 0) if "github" in collected else 0
            x_collected = collected.get("x", ([], {}))[1].get("collected", 0) if "x" in collected else 0
            model = llm._resolved_model or config.KIMI_MODEL

            result = SearchResult(
                problem=problem,
                domain=analysis["domain"],
                category=analysis["category"],
                maturity=analysis["maturity"],
                subproblems=subproblems,
                channel_reports=reports,
                candidates=[Candidate(**c) for c in top],
                notes=notes,
                meta={
                    "elapsed_sec": trace.elapsed(),
                    "model": model,
                    "x_reads_used": x_reads,
                    "total_candidates": len(all_cands),
                    "plan": {"github": gh_plan, "x": x_plan},
                    **trace.meta(),     # request_id, stages_sec, degradations
                },
            )
            trace.done(category=analysis["category"], maturity=analysis["maturity"],
                       domain=analysis["domain"], candidates=len(top), total=len(all_cands),
                       github=gh_collected, x=x_collected, x_reads=x_reads, model=model)
            await queue.put(_event(type="done", result=result.model_dump()))
        except Exception as e:  # noqa: BLE001
            trace.error(e)          # 结构化日志 + Sentry（堆栈不回传客户端）
            await queue.put(_event(type="error", message=str(e)))
        finally:
            await queue.put(_SENTINEL)

    task = asyncio.create_task(orchestrate())
    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()


def _fallback_why(c: Dict[str, Any]) -> str:
    ev = c.get("evidence", [])
    if ev:
        return f"证据显示其与该难题相关：{ev[0]['description']}"
    return "与该难题主题相关（信号较弱）。"


async def run_to_result(problem: str) -> Dict[str, Any]:
    """非流式：跑完返回最终结果 dict（给 POST / 测试用）。"""
    final = None
    async for ev in run_pipeline(problem):
        if ev["type"] == "done":
            final = ev["result"]
        elif ev["type"] == "error":
            raise RuntimeError(ev["message"])
    return final
