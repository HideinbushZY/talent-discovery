"""作业管理 + 持久化（app/jobs.py, app/store.py）。"""
import asyncio

import pytest

from app import jobs, pipeline, store


async def _wait_done(job_id, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        j = jobs.manager.get(job_id)
        if j and j.status != "running":
            return j
        await asyncio.sleep(0.02)
    return jobs.manager.get(job_id)


async def test_job_runs_detached_and_persists(monkeypatch, tmp_path):
    # 隔离 DB + 复位连接缓存
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(store, "_conn", None)

    async def fake_pipeline(problem):
        yield {"type": "status", "stage": 1, "message": "分型中"}
        yield {"type": "analysis", "data": {"domain": "X"}}
        yield {"type": "done", "result": {"problem": problem, "candidates": [], "meta": {"request_id": "r1"}}}

    monkeypatch.setattr(pipeline, "run_pipeline", fake_pipeline)

    job_id = jobs.manager.create("测试难题")
    job = await _wait_done(job_id)

    assert job.status == "done"
    assert job.result["problem"] == "测试难题"
    assert any(e["type"] == "done" for e in job.events)
    assert job.events[-1]["type"] == "close"          # 终止哨兵

    # 已落库，可重取（即使内存淘汰）
    rec = await store.get(job_id)
    assert rec and rec["status"] == "done"
    assert rec["result"]["problem"] == "测试难题"


async def test_job_error_is_captured(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "e.db")
    monkeypatch.setattr(store, "_conn", None)

    async def boom_pipeline(problem):
        yield {"type": "status", "stage": 1, "message": "开始"}
        raise RuntimeError("pipeline 炸了")

    monkeypatch.setattr(pipeline, "run_pipeline", boom_pipeline)
    job_id = jobs.manager.create("难题")
    job = await _wait_done(job_id)
    assert job.status == "error"
    assert "炸了" in (job.error or "")
    assert job.events[-1]["type"] == "close"


async def test_status_view_falls_back_to_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "s.db")
    monkeypatch.setattr(store, "_conn", None)
    # 直接写库，模拟"内存已淘汰、仅库里有"
    await store.save("gone123", "老难题", "done", {"problem": "老难题", "candidates": []}, None, 0.0)
    view = await jobs.manager.status_view("gone123")
    assert view and view["status"] == "done"
    assert view["result"]["problem"] == "老难题"
    assert await jobs.manager.status_view("nonexistent") is None
