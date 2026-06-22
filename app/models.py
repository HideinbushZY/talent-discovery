"""统一数据结构（pydantic）。对应 spec §5 / §9。"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Source = Literal["github", "x"]
EvidenceStrength = Literal["hard", "medium", "soft"]
Category = Literal["technical", "marketing", "other"]
Maturity = Literal["well_supported", "experimental"]


class Evidence(BaseModel):
    type: str                      # commit | repo | code | post | profile
    description: str
    url: Optional[str] = None
    metric: Optional[str] = None   # "120 commits" | "4.2k likes"


class Hireability(BaseModel):
    level: Literal["high", "medium", "low"] = "medium"
    reasons: List[str] = Field(default_factory=list)


class Subscores(BaseModel):
    relevance: float = 0.0
    depth_or_influence: float = 0.0
    recency: float = 0.0


class Candidate(BaseModel):
    id: str                        # "github:login" | "x:handle"
    source: Source
    name: Optional[str] = None
    handle: Optional[str] = None
    avatar_url: Optional[str] = None
    profile_url: Optional[str] = None
    bio: Optional[str] = None
    location: Optional[str] = None
    org: Optional[str] = None
    followers: int = 0

    problem_fit_score: float = 0.0
    weighted_score: float = 0.0          # 跨渠道排序用（problem_fit_score × 通道权重因子）
    subscores: Subscores = Field(default_factory=Subscores)
    evidence_strength: EvidenceStrength = "soft"
    why_relevant: str = ""
    evidence: List[Evidence] = Field(default_factory=list)
    hireability: Hireability = Field(default_factory=Hireability)
    contact_hint: Optional[str] = None

    # 内部排序辅助（不一定展示）
    raw_relevance: float = 0.0


class ChannelPlan(BaseModel):
    applicable: bool
    reason: str = ""
    weight: float = 0.0
    # github 专用
    seed_repos: List[str] = Field(default_factory=list)
    code_search_queries: List[str] = Field(default_factory=list)
    relevant_paths_hint: List[str] = Field(default_factory=list)
    # x 专用
    keywords: List[str] = Field(default_factory=list)
    phrases: List[str] = Field(default_factory=list)


class ProblemAnalysis(BaseModel):
    domain: str
    category: Category
    maturity: Maturity
    subproblems: List[str] = Field(default_factory=list)
    channels: Dict[str, ChannelPlan]   # {"github": ChannelPlan, "x": ChannelPlan}


class ChannelReport(BaseModel):
    """每个通道在 dashboard 顶部的诚实说明。"""
    channel: Source
    applicable: bool
    reason: str
    weight: float
    note: str = ""              # 跳过原因 / 采集结果说明
    collected: int = 0
    error: Optional[str] = None


class SearchResult(BaseModel):
    problem: str
    domain: str
    category: Category
    maturity: Maturity
    subproblems: List[str]
    channel_reports: List[ChannelReport]
    candidates: List[Candidate]
    notes: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)
