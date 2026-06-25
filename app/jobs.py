"""作业管理：把长流水线从 HTTP 请求里解耦。

搜索 = 一个后台作业：detached 跑完整管线、累积事件、最后落库。
客户端断开/代理超时**不影响**作业；客户端可重连续传（Last-Event-ID）或轮询取结果。
单进程内存注册表 + SQLite 持久化；要多实例/水平扩展再换 Redis/Celery（接口已隔离）。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from . import observability as obs
from . import store

_log = obs.get_logger("jobs")
_MAX_JOBS = 200   # 内存里最多保留多少个作业（旧的淘汰，已落库的仍可从 DB 取）


class Job:
    def __init__(self, job_id: str, problem: str, china_first: bool = False):
        self.id = job_id
        self.problem = problem
        self.china_first = china_first
        self.status = "running"            # running | done | error
        self.events: List[Dict[str, Any]] = []
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.created = time.time()


class JobManager:
    def __init__(self):
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()

    def create(self, problem: str, china_first: bool = False) -> str:
        from .pipeline import run_pipeline   # 延迟导入避免循环依赖
        job_id = secrets.token_hex(6)
        job = Job(job_id, problem, china_first)
        self._jobs[job_id] = job
        self._evict()
        asyncio.create_task(self._run(job, run_pipeline))
        obs.log(_log, logging.INFO, "job.create", job_id=job_id, problem=problem[:80], china_first=china_first)
        return job_id

    async def _run(self, job: Job, run_pipeline) -> None:
        try:
            async for ev in run_pipeline(job.problem, job.china_first):
                job.events.append(ev)
                if ev.get("type") == "done":
                    job.result = ev["result"]
                    job.status = "done"
                elif ev.get("type") == "error":
                    job.error = ev.get("message")
                    job.status = "error"
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = str(e)
            job.events.append({"type": "error", "message": str(e)})
        finally:
            if job.status == "running":     # 生成器结束却没给 done/error
                job.status = "done" if job.result else "error"
            job.events.append({"type": "close"})   # 终止哨兵，streamer 据此收尾
            await store.save(job.id, job.problem, job.status, job.result, job.error, job.created)
            obs.log(_log, logging.INFO, "job.finish", job_id=job.id,
                    status=job.status, events=len(job.events))

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    async def status_view(self, job_id: str) -> Optional[Dict[str, Any]]:
        """给轮询/恢复用：内存优先，没有则查库（进程重启后仍可取到已完成结果）。"""
        j = self._jobs.get(job_id)
        if j:
            return {"status": j.status, "result": j.result, "error": j.error,
                    "events_count": len(j.events)}
        return await store.get(job_id)

    def _evict(self):
        while len(self._jobs) > _MAX_JOBS:
            self._jobs.popitem(last=False)


manager = JobManager()
