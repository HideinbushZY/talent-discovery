"""通道连接器的统一接口（spec §4 可插拔设计）。

每个 connector 实现 collect()，输出统一的候选 dict 列表 + 一份通道报告。
候选 dict 字段对齐 models.Candidate（采集阶段先填原始信号，评分阶段再补分数/why）。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Tuple

# 进度回调：progress(stage_label, detail)
ProgressCb = Callable[[str, str], Awaitable[None]]

CollectResult = Tuple[List[Dict[str, Any]], Dict[str, Any]]


class Connector:
    source: str = "base"

    async def collect(self, plan: Dict[str, Any], progress: ProgressCb) -> CollectResult:
        """返回 (candidates, report)。report 含 collected/error/note。"""
        raise NotImplementedError


def new_candidate(source: str, key: str) -> Dict[str, Any]:
    """创建一个空候选骨架。"""
    return {
        "id": f"{source}:{key}",
        "source": source,
        "name": None,
        "handle": key,
        "avatar_url": None,
        "profile_url": None,
        "bio": None,
        "location": None,
        "org": None,
        "followers": 0,
        "evidence": [],
        # 采集阶段填入的原始信号（评分用，不直接展示）
        "_signals": {
            "relevance_hits": 0.0,   # 匹配强度累积（path/code 命中更高）
            "depth": 0.0,            # 相关贡献/影响计数
            "recency_ts": None,      # 最近相关活动的 epoch 秒
            "matched_paths": False,  # 是否命中相关模块/路径
        },
    }


def add_evidence(cand: Dict[str, Any], etype: str, description: str,
                 url: str | None = None, metric: str | None = None):
    # 去重（同 url 或同描述只留一条）
    for e in cand["evidence"]:
        if url and e.get("url") == url:
            return
        if not url and e.get("description") == description:
            return
    cand["evidence"].append({"type": etype, "description": description, "url": url, "metric": metric})
