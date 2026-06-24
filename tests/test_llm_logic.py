"""LLM 层的纯逻辑：JSON 解析、权重归一化、启发式兜底、调用重试（app/llm.py）。"""
import pytest

from app import config as appconfig
from app import llm


def test_safe_float():
    f = llm._safe_float
    assert f("oops") == 0.0
    assert f(None) == 0.0
    assert f("") == 0.0
    assert f("0.5") == 0.5
    assert f(3) == 3.0


def test_parse_json_plain():
    assert llm._parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_fenced():
    assert llm._parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_embedded_fallback():
    assert llm._parse_json('结果如下 {"a": 1} 完毕') == {"a": 1}


def test_normalize_both_applicable_keep_weights():
    raw = {"category": "technical", "domain": "x", "subproblems": ["a", "b"],
           "github": {"applicable": True, "weight": 0.6, "reason": "r"},
           "x": {"applicable": True, "weight": 0.4, "reason": "r"}}
    out = llm._normalize_analysis(raw)
    assert out["category"] == "technical"
    assert out["maturity"] == "well_supported"
    assert out["channels"]["github"]["weight"] == 0.6
    assert out["channels"]["x"]["weight"] == 0.4


def test_normalize_github_skipped_x_becomes_full():
    raw = {"category": "marketing",
           "github": {"applicable": False, "weight": 0.0, "reason": "no"},
           "x": {"applicable": True, "weight": 0.5, "reason": "y"}}
    out = llm._normalize_analysis(raw)
    assert out["channels"]["github"]["applicable"] is False
    assert out["channels"]["github"]["weight"] == 0.0
    assert out["channels"]["x"]["weight"] == 1.0


def test_normalize_zero_weights_even_split():
    raw = {"category": "other",
           "github": {"applicable": True, "weight": 0},
           "x": {"applicable": True, "weight": 0}}
    out = llm._normalize_analysis(raw)
    assert out["maturity"] == "experimental"
    assert out["channels"]["github"]["weight"] == 0.5
    assert out["channels"]["x"]["weight"] == 0.5


def test_normalize_bad_category_and_weight():
    raw = {"category": "banana",
           "github": {"applicable": True, "weight": "oops"},
           "x": {"applicable": True, "weight": 1}}
    out = llm._normalize_analysis(raw)
    assert out["category"] == "other"
    assert out["channels"]["x"]["weight"] == 1.0


def test_heuristic_technical():
    out = llm.heuristic_analysis("我们的 RAG 向量检索太慢")
    assert out["category"] == "technical"
    assert out["channels"]["github"]["applicable"] is True


def test_heuristic_marketing_skips_github():
    out = llm.heuristic_analysis("品牌在年轻用户里没有辨识度")
    assert out["category"] == "marketing"
    assert out["channels"]["github"]["applicable"] is False


def test_heuristic_other_is_experimental():
    out = llm.heuristic_analysis("xyzzy frobnicate quux")
    assert out["category"] == "other"
    assert out["maturity"] == "experimental"


# ── _chat_json 重试韧性（mock httpx，不触网）──────────────────────
class _FakeResp:
    def __init__(self, status, content):
        self.status_code = status
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}

    @property
    def text(self):
        return "err-body"


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        status, content = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return _FakeResp(status, content)


_ONE_PROVIDER = [{"name": "p", "api_key": "k", "base_url": "http://x", "model": "m"}]


async def test_chat_json_retries_empty_then_succeeds(monkeypatch):
    monkeypatch.setattr(appconfig, "LLM_PROVIDERS", _ONE_PROVIDER)
    fake = _FakeClient([(200, ""), (200, '{"ok": true}')])   # 先空内容，再成功
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: fake)
    out = await llm._chat_json("sys", "user", max_tokens=100)
    assert out == {"ok": True}
    assert fake.calls == 2                                    # 重试了一次


async def test_chat_json_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(appconfig, "LLM_PROVIDERS", _ONE_PROVIDER)
    fake = _FakeClient([(500, "boom")])                       # 一直 500
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: fake)
    with pytest.raises(Exception):
        await llm._chat_json("sys", "user", max_tokens=100, retries=1)
    assert fake.calls == 2                                    # 1 次 + 1 次重试


class _ByModelClient:
    """按 payload 的 model 返回不同结果，用于验证多供应商降级。"""
    def __init__(self):
        self.models = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        m = json["model"]
        self.models.append(m)
        if m == "primary-model":
            return _FakeResp(500, "boom")
        return _FakeResp(200, '{"ok": true, "via": "fallback"}')


async def test_chat_json_failover_to_second_provider(monkeypatch):
    monkeypatch.setattr(appconfig, "LLM_PROVIDERS", [
        {"name": "primary", "api_key": "k1", "base_url": "http://x", "model": "primary-model"},
        {"name": "fallback", "api_key": "k2", "base_url": "http://y", "model": "fallback-model"},
    ])
    fake = _ByModelClient()
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: fake)
    out = await llm._chat_json("sys", "user", max_tokens=50, retries=0)
    assert out["ok"] is True                                  # 主失败 → 降级到第二供应商成功
    assert fake.models == ["primary-model", "fallback-model"]
