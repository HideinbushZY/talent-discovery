"""联网发现的仓库抽取/降级逻辑（不打网络）。"""
import asyncio

from app import config, websearch


def test_extract_repos_from_mixed_text():
    texts = [
        "看这个 https://github.com/k2-fsa/sherpa-onnx 很强",
        "GitHub - FunAudioLLM/CosyVoice: Multi-lingual",
        "https://github.com/openbmb/MiniCPM-V/blob/main/README.md",   # 取 owner/repo，忽略后缀
        "https://github.com/wenet-e2e/wenet.git",                     # 去 .git
        "无关 https://example.com/foo/bar",
    ]
    repos = websearch._extract_repos(texts)
    assert "k2-fsa/sherpa-onnx" in repos
    assert "FunAudioLLM/CosyVoice" in repos
    assert "openbmb/MiniCPM-V" in repos
    assert "wenet-e2e/wenet" in repos
    assert all("example.com" not in r for r in repos)


def test_extract_skips_reserved_and_non_repo_paths():
    texts = [
        "https://github.com/search?q=asr",          # 保留路径，非仓库
        "https://github.com/topics/speech",          # 保留 owner
        "https://github.com/sponsors/someone",       # 保留 owner
        "https://github.com/owner/repo/issues/3",    # owner/repo 合法，issues 是第三段忽略
    ]
    repos = websearch._extract_repos(texts)
    assert "owner/repo" in repos
    assert not any(r.startswith(("search/", "topics/", "sponsors/")) for r in repos)


def test_extract_rejects_github_docs_and_own_pages():
    texts = [
        "https://docs.github.com/en/articles/something",   # 子域文档，非仓库
        "https://github.com/github/explore",               # GitHub 自家 explore 页
        "https://help.github.com/en/desktop",              # 子域帮助
        "https://github.com/code-search/understanding-syntax",
        "https://github.com/k2-fsa/sherpa-onnx",           # 真仓库，应保留
    ]
    repos = websearch._extract_repos(texts)
    assert repos == ["k2-fsa/sherpa-onnx"]


def test_extract_dedups():
    texts = ["github.com/a/b", "github.com/a/b", "github.com/a/b"]
    assert websearch._extract_repos(texts) == ["a/b"]


def test_discover_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH", False)
    assert asyncio.run(websearch.discover(["anything"])) == []


def test_discover_skips_when_no_queries():
    assert asyncio.run(websearch.discover([])) == []
