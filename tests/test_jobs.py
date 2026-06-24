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


async def test_feedback_save_list_and_upsert(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "fb.db")
    monkeypatch.setattr(store, "_conn", None)
    assert await store.save_feedback("job1", "github:a", "难题", "up", "很准") is True
    assert await store.save_feedback("job1", "github:b", "难题", "down", "") is True
    # 同一 (job, candidate) 改票 → 覆盖，不新增
    assert await store.save_feedback("job1", "github:a", "难题", "down", "改主意了") is True
    items = await store.list_feedback()
    assert len(items) == 2
    a = next(i for i in items if i["candidate_id"] == "github:a")
    assert a["vote"] == "down" and a["comment"] == "改主意了"


async def test_session_feedback_and_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "ses.db")
    monkeypatch.setattr(store, "_conn", None)
    await store.save_feedback("j1", "github:a", "难题", "up", "")
    await store.save_feedback("j1", "github:b", "难题", "down", "")
    await store.save_session_feedback("j1", "难题", "很有用", "会", "好用")
    await store.save_session_feedback("j1", "难题", "一般", "可能", "")   # 覆盖同一 job
    sess = await store.list_session_feedback()
    assert len(sess) == 1 and sess[0]["useful"] == "一般"
    s = await store.summary()
    assert s["结果准不准"]["命中率"] == 0.5      # 1 up / 2 voted
    assert s["用法是否接受"]["sessions"] == 1
    assert s["backend"] == "sqlite"


async def test_status_view_falls_back_to_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "s.db")
    monkeypatch.setattr(store, "_conn", None)
    # 直接写库，模拟"内存已淘汰、仅库里有"
    await store.save("gone123", "老难题", "done", {"problem": "老难题", "candidates": []}, None, 0.0)
    view = await jobs.manager.status_view("gone123")
    assert view and view["status"] == "done"
    assert view["result"]["problem"] == "老难题"
    assert await jobs.manager.status_view("nonexistent") is None
