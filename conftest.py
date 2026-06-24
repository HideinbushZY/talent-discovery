"""根级 conftest：保证项目根在 sys.path 上 + 提供共享测试夹具。"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import pytest

from app.connectors.base import add_evidence, new_candidate


@pytest.fixture
def make_gh_cand():
    """构造一个 GitHub 候选 dict（含 _signals），用于评分/管线测试。"""
    def _make(login="dev", relevance_hits=2.0, depth=100, matched_paths=True,
              recency_ts=None, bio="", org=None, followers=0, contact=None):
        c = new_candidate("github", login)
        c["name"] = login
        c["bio"] = bio
        c["org"] = org
        c["followers"] = followers
        if contact:
            c["contact_hint"] = contact
        c["_signals"].update(relevance_hits=relevance_hits, depth=depth,
                             matched_paths=matched_paths, recency_ts=recency_ts)
        add_evidence(c, "repo", f"{login} 的贡献", url=f"https://github.com/{login}", metric="100 commits")
        return c
    return _make


@pytest.fixture
def make_x_cand():
    def _make(handle="voice", relevance_hits=5.0, depth=3000, recency_ts=None,
              bio="", followers=1000, contact="DM (X)"):
        c = new_candidate("x", handle)
        c["name"] = handle
        c["bio"] = bio
        c["followers"] = followers
        c["contact_hint"] = contact
        c["_signals"].update(relevance_hits=relevance_hits, depth=depth,
                             recency_ts=recency_ts, matched_paths=False)
        add_evidence(c, "post", "相关帖", url=f"https://x.com/{handle}/status/1", metric="❤1.0k")
        return c
    return _make
