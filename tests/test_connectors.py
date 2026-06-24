"""连接器纯逻辑：候选骨架/证据去重、X 查询构建、时间解析。"""
from app.connectors import github as ghmod
from app.connectors import x as xmod
from app.connectors.base import add_evidence, new_candidate


def test_new_candidate_shape():
    c = new_candidate("github", "Alice")
    assert c["id"] == "github:Alice"
    assert c["source"] == "github"
    assert c["handle"] == "Alice"
    assert c["evidence"] == []
    assert "relevance_hits" in c["_signals"]


def test_add_evidence_dedup_by_url():
    c = new_candidate("github", "a")
    add_evidence(c, "repo", "desc1", url="http://x")
    add_evidence(c, "repo", "desc2", url="http://x")   # 同 url → 跳过
    assert len(c["evidence"]) == 1


def test_add_evidence_dedup_by_desc_when_no_url():
    c = new_candidate("github", "a")
    add_evidence(c, "repo", "same")
    add_evidence(c, "repo", "same")
    assert len(c["evidence"]) == 1


def test_build_query_basic():
    q = xmod._build_query(["vector search", "pgvector"], ["RAG infra"])
    assert q.startswith("(") and "-is:retweet" in q
    assert '"vector search"' in q     # 多词加引号
    assert "pgvector" in q
    assert '"RAG infra"' in q


def test_build_query_empty():
    assert xmod._build_query([], []) == ""


def test_fmt_metric():
    assert xmod._fmt_metric(1500) == "1.5k"
    assert xmod._fmt_metric(500) == "500"


def test_x_parse_ts():
    assert xmod._parse_ts(None) is None
    assert xmod._parse_ts("not-a-date") is None
    ts = xmod._parse_ts("2026-01-01T00:00:00Z")
    assert isinstance(ts, float) and ts > 0


def test_github_parse_ts():
    assert ghmod._parse_ts(None) is None
    assert ghmod._parse_ts("2026-01-01T00:00:00Z") > 0


def test_repo_format_validation():
    assert ghmod._REPO_RE.match("milvus-io/milvus")
    assert ghmod._REPO_RE.match("facebookresearch/faiss")
    assert not ghmod._REPO_RE.match("../../etc/passwd")    # 路径注入
    assert not ghmod._REPO_RE.match("owner/repo/extra")
    assert not ghmod._REPO_RE.match("noslash")
    assert not ghmod._REPO_RE.match("")
