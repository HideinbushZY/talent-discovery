"""评分与可挖性启发式（app/scoring.py）。"""
import time

from app import scoring


def test_log_norm_bounds():
    assert scoring._log_norm(0, 100) == 0.0
    assert scoring._log_norm(-5, 100) == 0.0
    assert scoring._log_norm(100, 100) == 1.0
    assert 0 < scoring._log_norm(10, 100) < 1


def test_recency_none_is_neutral():
    assert scoring._recency_score(None) == 0.6


def test_recency_recent_high_old_low():
    assert scoring._recency_score(time.time()) > 0.95
    assert scoring._recency_score(time.time() - 365 * 86400 * 3) < 0.1


def test_score_github_exact_formula(make_gh_cand):
    # depth 信号=400 → _log_norm=1.0；recency None → 0.6；rel=0.9
    # 100*(0.5*0.9 + 0.35*1.0 + 0.15*0.6) = 89.0
    c = make_gh_cand(depth=400, matched_paths=True, recency_ts=None)
    scoring.score_github(c, 0.9)
    assert c["problem_fit_score"] == 89.0
    assert c["evidence_strength"] == "hard"
    assert c["subscores"]["relevance"] == 0.9


def test_score_github_medium_strength(make_gh_cand):
    c = make_gh_cand(matched_paths=False, depth=2)
    scoring.score_github(c, 0.5)
    assert c["evidence_strength"] == "medium"


def test_score_github_heuristic_relevance_without_llm(make_gh_cand):
    c = make_gh_cand(relevance_hits=8, matched_paths=True, depth=50)
    scoring.score_github(c, None)
    assert c["subscores"]["relevance"] > 0
    assert 0 <= c["problem_fit_score"] <= 100


def test_score_x_is_soft(make_x_cand):
    c = make_x_cand()
    scoring.score_x(c, 0.8)
    assert c["evidence_strength"] == "soft"
    assert 0 <= c["problem_fit_score"] <= 100


def test_hireability_freelance_high(make_gh_cand):
    c = make_gh_cand(bio="open to freelance work, DMs open")
    assert scoring.hireability(c)["level"] == "high"


def test_hireability_bigorg_low(make_gh_cand):
    c = make_gh_cand(bio="staff engineer", org="Google", contact=None)
    assert scoring.hireability(c)["level"] == "low"


def test_hireability_contact_bumps_high(make_gh_cand):
    c = make_gh_cand(bio="backend engineer", org=None, contact="me@example.com")
    assert scoring.hireability(c)["level"] == "high"


def test_china_fit_high_chinese_bio_and_cn_location():
    c = {"bio": "分布式向量检索工程师", "name": "Qin Liu", "location": "Beijing, China", "org": ""}
    r = scoring.china_fit(c)
    assert r["level"] == "high"
    assert any("中文" in x for x in r["reasons"])
    assert any("中国" in x for x in r["reasons"])


def test_china_fit_medium_cn_org_only():
    c = {"bio": "backend engineer", "name": "Wei Wang", "location": "Remote", "org": "ByteDance"}
    r = scoring.china_fit(c)
    assert r["level"] == "medium"
    assert any("bytedance" in x.lower() for x in r["reasons"])


def test_china_fit_greater_china_is_modest():
    c = {"bio": "ML engineer", "name": "Alex", "location": "Singapore", "org": ""}
    r = scoring.china_fit(c)
    assert r["score"] == 0.2          # 大中华区单一弱信号
    assert r["level"] == "low"


def test_china_fit_low_when_no_signals():
    c = {"bio": "web developer", "name": "John Smith", "location": "Berlin, Germany", "org": "Acme"}
    r = scoring.china_fit(c)
    assert r["level"] == "low"
    assert r["score"] == 0.0
    assert r["reasons"] == []          # 不编造信号


def test_china_fit_uses_llm_judgment_when_given():
    # 英文 bio，但 Kimi 判定中文能力高（看了 commit/帖子）+ 北京 → high
    c = {"bio": "backend engineer", "name": "Wei", "location": "Beijing, China", "org": ""}
    r = scoring.china_fit(c, llm_cn_lang=0.9)
    assert r["level"] == "high"
    assert any("中文" in x and "AI" in x for x in r["reasons"])


def test_china_fit_llm_overrides_regex():
    # bio 里有中文字，但 Kimi 判定中文能力低 → 信 AI，不计中文分
    c = {"bio": "我 love coding", "name": "X", "location": "Remote", "org": ""}
    r = scoring.china_fit(c, llm_cn_lang=0.1)
    assert not any("中文" in x for x in r["reasons"])
    assert r["score"] == 0.0


def test_apply_weight_factor(make_gh_cand):
    c = make_gh_cand()
    c["problem_fit_score"] = 80.0
    scoring.apply_weight([c], 0.4)          # factor = 0.6 + 0.4*min(1, 0.8) = 0.92
    assert c["weighted_score"] == 73.6
    c2 = make_gh_cand()
    c2["problem_fit_score"] = 80.0
    scoring.apply_weight([c2], 0.6)         # factor = 1.0
    assert c2["weighted_score"] == 80.0
