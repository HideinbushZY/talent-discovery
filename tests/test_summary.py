"""结果导读的防幻觉护栏：摘要里的 handle 必须真实存在于候选名单，否则丢弃。"""
import asyncio

from app import llm


def test_summarize_filters_hallucinated_handles(monkeypatch):
    async def fake_chat(system, user, max_tokens=2000, retries=2, providers=None):
        return {
            "overview": "测试概览",
            "recommended_first": [
                {"handle": "real1", "reason": "r1"},
                {"handle": "ghost", "reason": "模型编造的人"},      # 幻觉 → 应过滤
                {"handle": "real1", "reason": "重复"},              # 重复 → 应去重
            ],
            "groups": [
                {"label": "组A", "handles": ["real1", "ghost", "real2"]},  # ghost 应被剔除
                {"label": "全幻觉组", "handles": ["ghost"]},               # 过滤后为空 → 丢弃
            ],
        }
    monkeypatch.setattr(llm, "_chat_json", fake_chat)
    cands = [{"handle": "real1", "name": "R1"}, {"handle": "real2", "name": "R2"}]
    out = asyncio.run(llm.summarize_results("难题", [], cands))

    assert out["overview"] == "测试概览"
    assert [r["handle"] for r in out["recommended_first"]] == ["real1"]   # 幻觉+重复都没了
    assert len(out["groups"]) == 1                                        # 全幻觉组被丢弃
    assert set(out["groups"][0]["handles"]) == {"real1", "real2"}


def test_summarize_empty_candidates_returns_none():
    assert asyncio.run(llm.summarize_results("难题", [], [])) is None
