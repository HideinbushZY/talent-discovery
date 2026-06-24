"""搜索结果持久化（SQLite，stdlib，零额外依赖）。

让"算完的结果不丢、可重取、有历史"。
注意：Railway 容器文件系统是临时的——本库在**单次部署内**有效（足以扛住
断线重连/代理超时）；要跨重启/多实例持久，把这层换成 Postgres（接口不变）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path

from . import observability as obs

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "searches.db"
_log = obs.get_logger("store")
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS searches(
                 id TEXT PRIMARY KEY, problem TEXT, status TEXT,
                 result_json TEXT, error TEXT, created REAL, updated REAL)"""
        )
        _conn.commit()
    return _conn


def _save_sync(job_id, problem, status, result, error, created):
    try:
        with _lock:
            c = _connect()
            c.execute(
                "INSERT OR REPLACE INTO searches VALUES (?,?,?,?,?,?,?)",
                (job_id, problem, status,
                 json.dumps(result, ensure_ascii=False) if result else None,
                 error, created, time.time()),
            )
            c.commit()
    except Exception as e:  # noqa: BLE001 —— 落库是尽力而为，失败绝不影响搜索本身
        _log.warning("store.save failed: %s", str(e)[:120])


def _get_sync(job_id):
    try:
        with _lock:
            c = _connect()
            row = c.execute(
                "SELECT problem,status,result_json,error FROM searches WHERE id=?", (job_id,)
            ).fetchone()
        if not row:
            return None
        problem, status, rj, error = row
        return {"problem": problem, "status": status,
                "result": json.loads(rj) if rj else None, "error": error}
    except Exception as e:  # noqa: BLE001
        _log.warning("store.get failed: %s", str(e)[:120])
        return None


async def save(job_id, problem, status, result, error, created):
    await asyncio.to_thread(_save_sync, job_id, problem, status, result, error, created)


async def get(job_id):
    return await asyncio.to_thread(_get_sync, job_id)
