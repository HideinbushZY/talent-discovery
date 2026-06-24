"""持久化：本地/测试用 SQLite，生产设 DATABASE_URL 则用 Postgres（跨部署不丢）。

存三类东西：
  searches          —— 搜索作业结果（断线/超时可重取）
  feedback          —— 每候选 👍/👎 + 备注（衡量"结果准不准"）
  session_feedback  —— 每次搜索整体评价（衡量"用法是否接受"）
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from . import observability as obs

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "searches.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_PG = bool(DATABASE_URL)
_log = obs.get_logger("store")
_lock = threading.Lock()
_conn = None          # SQLite 连接缓存（PG 每次新建短连接）
_pg_ready = False

_TS = "DOUBLE PRECISION" if _PG else "REAL"
_SCHEMA = [
    f"CREATE TABLE IF NOT EXISTS searches(id TEXT PRIMARY KEY, problem TEXT, status TEXT, "
    f"result_json TEXT, error TEXT, created {_TS}, updated {_TS})",
    f"CREATE TABLE IF NOT EXISTS feedback(job_id TEXT, candidate_id TEXT, problem TEXT, vote TEXT, "
    f"comment TEXT, created {_TS}, updated {_TS}, PRIMARY KEY (job_id, candidate_id))",
    f"CREATE TABLE IF NOT EXISTS session_feedback(job_id TEXT PRIMARY KEY, problem TEXT, useful TEXT, "
    f"would_use TEXT, comment TEXT, updated {_TS})",
]


def _sqlite():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        for s in _SCHEMA:
            _conn.execute(s)
        _conn.commit()
    return _conn


def _pg():
    import psycopg2
    conn = psycopg2.connect(DATABASE_URL)
    global _pg_ready
    if not _pg_ready:
        cur = conn.cursor()
        for s in _SCHEMA:
            cur.execute(s)
        conn.commit()
        _pg_ready = True
    return conn


def _run(sql_pg: str, sql_sqlite: str, params=(), write=False, fetch=False):
    """统一执行：按后端选占位符/SQL。fetch 返回行列表。"""
    with _lock:
        if _PG:
            conn = _pg()
            try:
                cur = conn.cursor()
                cur.execute(sql_pg, params)
                rows = cur.fetchall() if fetch else None
                if write:
                    conn.commit()
                return rows
            finally:
                conn.close()
        else:
            c = _sqlite()
            cur = c.execute(sql_sqlite, params)
            rows = cur.fetchall() if fetch else None
            if write:
                c.commit()
            return rows


# ── 搜索结果 ──────────────────────────────────────────────────
def _save_sync(job_id, problem, status, result, error, created):
    rj = json.dumps(result, ensure_ascii=False) if result else None
    now = time.time()
    try:
        _run(
            "INSERT INTO searches VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET "
            "problem=EXCLUDED.problem,status=EXCLUDED.status,result_json=EXCLUDED.result_json,"
            "error=EXCLUDED.error,updated=EXCLUDED.updated",
            "INSERT OR REPLACE INTO searches VALUES (?,?,?,?,?,?,?)",
            (job_id, problem, status, rj, error, created, now), write=True)
    except Exception as e:  # noqa: BLE001
        _log.warning("store.save failed: %s", str(e)[:120])


def _get_sync(job_id):
    try:
        rows = _run("SELECT problem,status,result_json,error FROM searches WHERE id=%s",
                    "SELECT problem,status,result_json,error FROM searches WHERE id=?",
                    (job_id,), fetch=True)
        if not rows:
            return None
        problem, status, rj, error = rows[0]
        return {"problem": problem, "status": status,
                "result": json.loads(rj) if rj else None, "error": error}
    except Exception as e:  # noqa: BLE001
        _log.warning("store.get failed: %s", str(e)[:120])
        return None


async def save(job_id, problem, status, result, error, created):
    await asyncio.to_thread(_save_sync, job_id, problem, status, result, error, created)


async def get(job_id):
    return await asyncio.to_thread(_get_sync, job_id)


# ── 候选反馈（结果准不准）────────────────────────────────────
def _save_feedback_sync(job_id, candidate_id, problem, vote, comment):
    now = time.time()
    try:
        _run(
            "INSERT INTO feedback VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (job_id,candidate_id) "
            "DO UPDATE SET problem=EXCLUDED.problem,vote=EXCLUDED.vote,comment=EXCLUDED.comment,updated=EXCLUDED.updated",
            "INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,?,?,?)",
            (job_id, candidate_id, problem, vote, comment, now, now), write=True)
        return True
    except Exception as e:  # noqa: BLE001
        _log.warning("store.save_feedback failed: %s", str(e)[:120])
        return False


def _list_feedback_sync(limit):
    try:
        rows = _run("SELECT job_id,candidate_id,problem,vote,comment,updated FROM feedback ORDER BY updated DESC LIMIT %s",
                    "SELECT job_id,candidate_id,problem,vote,comment,updated FROM feedback ORDER BY updated DESC LIMIT ?",
                    (limit,), fetch=True) or []
        cols = ["job_id", "candidate_id", "problem", "vote", "comment", "updated"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:  # noqa: BLE001
        _log.warning("store.list_feedback failed: %s", str(e)[:120])
        return []


async def save_feedback(job_id, candidate_id, problem, vote, comment):
    return await asyncio.to_thread(_save_feedback_sync, job_id, candidate_id, problem, vote, comment)


async def list_feedback(limit=500):
    return await asyncio.to_thread(_list_feedback_sync, limit)


# ── 整体评价（用法是否接受）─────────────────────────────────
def _save_session_sync(job_id, problem, useful, would_use, comment):
    now = time.time()
    try:
        _run(
            "INSERT INTO session_feedback VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (job_id) "
            "DO UPDATE SET problem=EXCLUDED.problem,useful=EXCLUDED.useful,would_use=EXCLUDED.would_use,"
            "comment=EXCLUDED.comment,updated=EXCLUDED.updated",
            "INSERT OR REPLACE INTO session_feedback VALUES (?,?,?,?,?,?)",
            (job_id, problem, useful, would_use, comment, now), write=True)
        return True
    except Exception as e:  # noqa: BLE001
        _log.warning("store.save_session failed: %s", str(e)[:120])
        return False


def _list_session_sync(limit):
    try:
        rows = _run("SELECT job_id,problem,useful,would_use,comment,updated FROM session_feedback ORDER BY updated DESC LIMIT %s",
                    "SELECT job_id,problem,useful,would_use,comment,updated FROM session_feedback ORDER BY updated DESC LIMIT ?",
                    (limit,), fetch=True) or []
        cols = ["job_id", "problem", "useful", "would_use", "comment", "updated"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:  # noqa: BLE001
        _log.warning("store.list_session failed: %s", str(e)[:120])
        return []


async def save_session_feedback(job_id, problem, useful, would_use, comment):
    return await asyncio.to_thread(_save_session_sync, job_id, problem, useful, would_use, comment)


async def list_session_feedback(limit=500):
    return await asyncio.to_thread(_list_session_sync, limit)


# ── 汇总：两个内测核心指标 ───────────────────────────────────
async def summary():
    fb = await list_feedback(5000)
    sess = await list_session_feedback(5000)
    up = sum(1 for f in fb if f["vote"] == "up")
    down = sum(1 for f in fb if f["vote"] == "down")
    voted = up + down

    def dist(items, key):
        d = {}
        for it in items:
            v = it.get(key)
            if v:
                d[v] = d.get(v, 0) + 1
        return d

    return {
        "backend": "postgres" if _PG else "sqlite",
        "结果准不准": {"votes": voted, "准": up, "不准": down,
                  "命中率": round(up / voted, 3) if voted else None},
        "用法是否接受": {"sessions": len(sess),
                   "有用度": dist(sess, "useful"), "会不会用": dist(sess, "would_use")},
    }
