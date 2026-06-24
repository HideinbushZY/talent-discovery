"""四阶段管线集成测试：mock 掉 GitHub/X/Kimi，不触网、不花钱，验证编排正确。"""
from app import config, pipeline
from app.connectors.base import add_evidence, new_candidate


def _gh_cands(n=3):
    out = []
    for i in range(n):
        c = new_candidate("github", f"dev{i}")
        c["name"] = f"Dev {i}"
        c["bio"] = "vector db engineer"
        c["_signals"].update(relevance_hits=3.0, depth=200 - i * 10, matched_paths=True, recency_ts=None)
        add_evidence(c, "repo", "milvus 贡献者", url=f"https://github.com/dev{i}", metric="100 commits")
        out.append(c)
    return out


def _x_cands(n=2):
    out = []
    for i in range(n):
        c = new_candidate("x", f"voice{i}")
        c["name"] = f"Voice {i}"
        c["bio"] = "RAG enthusiast"
        c["followers"] = 5000
        c["_signals"].update(relevance_hits=4.0, depth=2000, matched_paths=False, recency_ts=None)
        add_evidence(c, "post", "相关帖", url=f"https://x.com/voice{i}/status/1", metric="❤1.0k")
        out.append(c)
    return out


class _FakeConn:
    def __init__(self, cands):
        self._cands = cands

    async def collect(self, plan, progress):
        return list(self._cands), {"collected": len(self._cands), "error": None, "note": "", "reads_used": 50}


def _patch(monkeypatch, analysis, gh_cands, x_cands):
    async def fake_analyze(problem):
        return analysis

    async def fake_review(problem, subs, channel, cands):
        return {c["id"]: {"relevance": 0.9, "why_relevant": f"why {c['id']}"} for c in cands}

    monkeypatch.setattr(pipeline.llm, "analyze_problem", fake_analyze)
    monkeypatch.setattr(pipeline.llm, "review_candidates", fake_review)
    monkeypatch.setattr(pipeline, "GitHubConnector", lambda: _FakeConn(gh_cands))
    monkeypatch.setattr(pipeline, "XConnector", lambda: _FakeConn(x_cands))
    monkeypatch.setattr(config, "HAS_GITHUB", True)
    monkeypatch.setattr(config, "HAS_X", True)


_DUAL = {
    "domain": "向量检索", "category": "technical", "maturity": "well_supported",
    "subproblems": ["a", "b"],
    "channels": {
        "github": {"applicable": True, "reason": "有", "weight": 0.6,
                   "seed_repos": ["milvus-io/milvus"], "code_search_queries": ["q"], "relevant_paths_hint": []},
        "x": {"applicable": True, "reason": "有", "weight": 0.4, "keywords": ["vector"], "phrases": []},
    },
}


async def test_dual_channel_ranked_and_scored(monkeypatch):
    _patch(monkeypatch, _DUAL, _gh_cands(3), _x_cands(2))
    res = await pipeline.run_to_result("RAG 太慢")
    assert res["category"] == "technical"
    assert res["maturity"] == "well_supported"
    assert len(res["candidates"]) == 5
    ws = [c["weighted_score"] for c in res["candidates"]]
    assert ws == sorted(ws, reverse=True)               # 按加权分降序
    assert all(c["weighted_score"] > 0 for c in res["candidates"])
    gh = [c for c in res["candidates"] if c["source"] == "github"]
    assert gh and all(c["evidence_strength"] == "hard" for c in gh)
    assert any(c["why_relevant"].startswith("why ") for c in res["candidates"])   # 来自复核
    reps = {r["channel"]: r for r in res["channel_reports"]}
    assert reps["github"]["applicable"] and reps["x"]["applicable"]
    assert reps["github"]["collected"] == 3


async def test_github_honestly_skipped(monkeypatch):
    analysis = {
        "domain": "品牌", "category": "marketing", "maturity": "well_supported", "subproblems": ["a"],
        "channels": {
            "github": {"applicable": False, "reason": "纯品牌无对应人才", "weight": 0.0,
                       "seed_repos": [], "code_search_queries": [], "relevant_paths_hint": []},
            "x": {"applicable": True, "reason": "有", "weight": 1.0, "keywords": ["brand"], "phrases": []},
        },
    }
    _patch(monkeypatch, analysis, _gh_cands(3), _x_cands(2))
    res = await pipeline.run_to_result("品牌辨识度")
    assert res["candidates"], "应有 X 候选"
    assert all(c["source"] == "x" for c in res["candidates"])      # 全部来自 X
    reps = {r["channel"]: r for r in res["channel_reports"]}
    assert reps["github"]["applicable"] is False
    assert "无对应人才" in (reps["github"]["note"] + reps["github"]["reason"])


async def test_experimental_banner_note(monkeypatch):
    analysis = {
        "domain": "远程协作", "category": "other", "maturity": "experimental", "subproblems": ["a"],
        "channels": {
            "github": {"applicable": False, "reason": "跳过", "weight": 0.0,
                       "seed_repos": [], "code_search_queries": [], "relevant_paths_hint": []},
            "x": {"applicable": True, "reason": "有", "weight": 1.0, "keywords": ["remote"], "phrases": []},
        },
    }
    _patch(monkeypatch, analysis, [], _x_cands(2))
    res = await pipeline.run_to_result("远程协作效率低")
    assert res["maturity"] == "experimental"
    assert any("实验性" in n for n in res["notes"])


async def test_llm_failure_falls_back_to_heuristic(monkeypatch):
    async def boom(problem):
        raise RuntimeError("kimi down")

    async def empty_review(p, s, ch, cands):
        return {}

    monkeypatch.setattr(pipeline.llm, "analyze_problem", boom)
    monkeypatch.setattr(pipeline.llm, "review_candidates", empty_review)
    monkeypatch.setattr(pipeline, "GitHubConnector", lambda: _FakeConn(_gh_cands(2)))
    monkeypatch.setattr(pipeline, "XConnector", lambda: _FakeConn(_x_cands(1)))
    monkeypatch.setattr(config, "HAS_GITHUB", True)
    monkeypatch.setattr(config, "HAS_X", True)
    res = await pipeline.run_to_result("我们的 RAG 向量检索太慢")
    assert any("启发式兜底" in n for n in res["notes"])      # 已降级但没崩
    assert res["candidates"]
