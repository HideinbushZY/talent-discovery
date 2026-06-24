"""FastAPI 入口：作业化搜索（POST 建作业 / GET 取结果 / SSE 续传）+ 健康检查 + 静态前端。

公网安全：设置环境变量 APP_PASSWORD 后，所有页面/接口走 HTTP Basic Auth
（用户名任意，密码 = APP_PASSWORD）。未设置则本地开放（仅供本机开发）。

Phase B：搜索是**后台作业**——客户端断开/代理超时不影响作业，结果落库可重取。
"""
from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from . import config, llm
from . import observability as obs
from . import store
from .jobs import manager as jobs

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

obs.setup_logging()
obs.init_sentry()
obs.get_logger("startup").info("talent-discovery 启动 | 配置=%s" % config.summary())

_basic = HTTPBasic(auto_error=False)
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


def require_auth(creds: Optional[HTTPBasicCredentials] = Depends(_basic)):
    """APP_PASSWORD 设置后强制 Basic Auth；未设置则放行（本地开发）。"""
    pw = config.APP_PASSWORD
    if not pw:
        return
    ok = creds is not None and secrets.compare_digest(creds.password, pw)
    if not ok:
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": 'Basic realm="talent-discovery"'})


app = FastAPI(title="从问题出发的人才发现", version="2.0",
              dependencies=[Depends(require_auth)])


class SearchBody(BaseModel):
    problem: str


class FeedbackBody(BaseModel):
    job_id: str
    candidate_id: str
    vote: str            # "up" | "down"
    comment: str = ""
    problem: str = ""


class SessionFeedbackBody(BaseModel):
    job_id: str
    useful: str = ""        # 很有用 | 一般 | 没用
    would_use: str = ""     # 会 | 可能 | 不会
    comment: str = ""
    problem: str = ""


@app.get("/api/health")
async def health():
    out = {"ok": True, "auth": bool(config.APP_PASSWORD),
           "config": config.summary(),
           "channels": {"github": config.HAS_GITHUB, "x": config.HAS_X, "llm": config.HAS_LLM}}
    try:
        out["model"] = await llm.resolve_model()
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["model_error"] = str(e)[:200]
    return out


@app.post("/api/search")
async def create_search(body: SearchBody):
    """建一个后台搜索作业，立即返回 job_id（不阻塞）。"""
    if not (body.problem or "").strip():
        return JSONResponse({"error": "problem 不能为空"}, status_code=400)
    job_id = jobs.create(body.problem.strip())
    return {"job_id": job_id}


@app.get("/api/search/{job_id}")
async def get_search(job_id: str):
    """轮询/恢复：返回作业状态 + 最终结果（内存没有则查库）。"""
    data = await jobs.status_view(job_id)
    if not data:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return data


def _sse(idx: int, ev: dict) -> str:
    return f"id: {idx}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"


@app.get("/api/search/{job_id}/stream")
async def stream_search(job_id: str, request: Request):
    """SSE：从 Last-Event-ID 续传作业事件 + 静默期心跳保活，直到 close。"""
    job = jobs.get(job_id)
    if job is None:
        # 内存里没有（可能进程重启过）→ 从库里一次性返回最终结果
        data = await jobs.status_view(job_id)

        async def once():
            if data and data.get("result"):
                yield _sse(0, {"type": "done", "result": data["result"]})
            elif data and data.get("status") == "error":
                yield _sse(0, {"type": "error", "message": data.get("error") or "失败"})
            else:
                yield _sse(0, {"type": "error", "message": "job not found"})
            yield _sse(1, {"type": "close"})
        return StreamingResponse(once(), media_type="text/event-stream", headers=_SSE_HEADERS)

    last = request.headers.get("Last-Event-ID")
    start = int(last) + 1 if (last and last.isdigit()) else 0

    async def gen():
        idx = start
        quiet = 0
        while True:
            advanced = False
            while idx < len(job.events):
                ev = job.events[idx]
                yield _sse(idx, ev)
                idx += 1
                advanced = True
                if ev.get("type") == "close":
                    return
            if advanced:
                quiet = 0
                continue
            await asyncio.sleep(1)
            quiet += 1
            if quiet % 8 == 0:        # 每 ~8 秒发一个心跳注释，防代理空闲超时
                yield ": ping\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.post("/api/feedback")
async def submit_feedback(body: FeedbackBody):
    """内测反馈：对某次搜索里的某个候选投 准/不准 + 可选备注（落库）。"""
    if body.vote not in ("up", "down"):
        return JSONResponse({"error": "vote 必须是 up 或 down"}, status_code=400)
    ok = await store.save_feedback(body.job_id, body.candidate_id,
                                   body.problem[:200], body.vote, body.comment[:500])
    obs.log(obs.get_logger("feedback"), 20, "feedback",
            candidate=body.candidate_id, vote=body.vote, has_comment=bool(body.comment))
    return {"ok": ok}


@app.post("/api/session-feedback")
async def submit_session_feedback(body: SessionFeedbackBody):
    """整体评价：这次搜索是否有用 / 会不会真用它（衡量"用法是否接受"）。"""
    ok = await store.save_session_feedback(body.job_id, body.problem[:200],
                                           body.useful[:20], body.would_use[:20], body.comment[:500])
    obs.log(obs.get_logger("feedback"), 20, "session_feedback",
            useful=body.useful, would_use=body.would_use)
    return {"ok": ok}


@app.get("/api/feedback")
async def get_feedback(limit: int = 500):
    """给项目方回看内测反馈（候选级 + 整体级，受 Basic Auth 保护）。"""
    return {"items": await store.list_feedback(limit),
            "sessions": await store.list_session_feedback(limit)}


@app.get("/api/feedback/summary")
async def feedback_summary():
    """两个内测核心指标的汇总：结果准不准（命中率）+ 用法是否接受。"""
    return await store.summary()


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")
