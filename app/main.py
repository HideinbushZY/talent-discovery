"""FastAPI 入口：SSE 流式搜索 + 非流式 POST + 健康检查 + 静态前端。

公网安全：设置环境变量 APP_PASSWORD 后，所有页面/接口走 HTTP Basic Auth
（用户名任意，密码 = APP_PASSWORD）。未设置则本地开放（仅供本机开发）。
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from . import config, llm
from .pipeline import run_pipeline, run_to_result

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

_basic = HTTPBasic(auto_error=False)


def require_auth(creds: Optional[HTTPBasicCredentials] = Depends(_basic)):
    """APP_PASSWORD 设置后强制 Basic Auth；未设置则放行（本地开发）。"""
    pw = config.APP_PASSWORD
    if not pw:
        return
    ok = creds is not None and secrets.compare_digest(creds.password, pw)
    if not ok:
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": 'Basic realm="talent-discovery"'})


# 全局依赖：每个请求都先过鉴权
app = FastAPI(title="从问题出发的人才发现", version="1.0",
              dependencies=[Depends(require_auth)])


class SearchBody(BaseModel):
    problem: str


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


@app.get("/api/search/stream")
async def search_stream(problem: str):
    """SSE：实时回传进度与最终结果。前端用 EventSource 消费。"""
    async def gen():
        try:
            async for ev in run_pipeline(problem):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type':'error','message':str(e)}, ensure_ascii=False)}\n\n"
        yield "data: {\"type\":\"close\"}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/search")
async def search(body: SearchBody):
    """非流式：跑完返回完整结果（API/测试用）。"""
    try:
        result = await run_to_result(body.problem)
        return JSONResponse(result)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")
