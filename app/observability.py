"""可观测性：结构化日志 + 每次搜索的耗时/降级 trace + 可选 Sentry。

设计目标：每次搜索在日志里留**一行结构化记录**（JSON），grep 一下就能看到
每次搜索走了哪些阶段、各花多久、在哪降级、读了多少 X、用的什么模型。

环境变量：
  LOG_LEVEL   日志级别（默认 INFO）
  LOG_FORMAT  json（默认，便于日志系统解析）| plain（本地易读）
  SENTRY_DSN  设置后且装了 sentry-sdk，则把异常上报 Sentry
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time
from typing import Any, Dict

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            base.update(fields)
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False, default=str)


def setup_logging() -> None:
    """配置 `td` 日志命名空间（幂等）。应用启动时调用一次。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    if os.getenv("LOG_FORMAT", "json").lower() == "plain":
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    else:
        handler.setFormatter(_JsonFormatter())
    root = logging.getLogger("td")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"td.{name}")


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """带结构化字段的日志。"""
    logger.log(level, msg, extra={"fields": fields})


# ── Sentry（可选）────────────────────────────────────────────
def init_sentry() -> bool:
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=dsn, traces_sample_rate=0.0)
        get_logger("obs").info("Sentry 已启用")
        return True
    except ImportError:
        get_logger("obs").warning("SENTRY_DSN 已设置但未安装 sentry-sdk（pip install sentry-sdk 后生效）")
        return False


def capture_exception(exc: BaseException) -> None:
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001
        pass


# ── 每次搜索的 trace ─────────────────────────────────────────
def new_request_id() -> str:
    return secrets.token_hex(4)


class Trace:
    """记录一次搜索的阶段耗时 + 降级/事件，结束时一行结构化日志带出。"""

    def __init__(self, request_id: str, problem: str):
        self.id = request_id
        self.problem = problem
        self.t0 = time.time()
        self.stages: Dict[str, float] = {}
        self.events: list[Dict[str, Any]] = []
        self._marks: Dict[str, float] = {}
        self._log = get_logger("search")

    def started(self) -> None:
        log(self._log, logging.INFO, "search.start", rid=self.id, problem=self.problem[:80])

    def start(self, name: str) -> None:
        self._marks[name] = time.time()

    def end(self, name: str) -> None:
        if name in self._marks:
            self.stages[name] = round(time.time() - self._marks.pop(name), 2)

    def event(self, name: str, **detail: Any) -> None:
        """记录一个事件（多为降级：失败/兜底/跳过/限速）。"""
        self.events.append({"event": name, **detail})
        log(self._log, logging.WARNING if detail.get("degraded", True) else logging.INFO,
            f"event:{name}", rid=self.id, **detail)

    def elapsed(self) -> float:
        return round(time.time() - self.t0, 1)

    def _degradations(self) -> list[str]:
        # 只算真正的降级事件（channel_ok / channel_skipped 等 degraded=False 的不算）
        return [e["event"] for e in self.events if e.get("degraded", True)]

    def meta(self) -> Dict[str, Any]:
        """放进结果 meta，便于前端/用户与日志对账。"""
        return {"request_id": self.id, "stages_sec": dict(self.stages),
                "degradations": self._degradations()}

    def done(self, **summary: Any) -> None:
        log(self._log, logging.INFO, "search.done",
            rid=self.id, elapsed_sec=self.elapsed(),
            stages_sec=self.stages, degradations=self._degradations(),
            **summary)

    def error(self, exc: BaseException) -> None:
        self._log.error("search.error", exc_info=exc,
                        extra={"fields": {"rid": self.id, "elapsed_sec": self.elapsed()}})
        capture_exception(exc)
