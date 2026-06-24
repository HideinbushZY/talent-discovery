"""可观测性 trace（app/observability.py）。"""
from app import observability as obs


def test_request_id_is_8_hex():
    rid = obs.new_request_id()
    assert isinstance(rid, str) and len(rid) == 8
    int(rid, 16)   # 应为合法十六进制


def test_trace_stages_and_elapsed():
    t = obs.Trace("rid1", "problem")
    t.start("s1")
    t.end("s1")
    assert "s1" in t.stages
    assert isinstance(t.elapsed(), float)


def test_degradations_filter_excludes_normal_events():
    t = obs.Trace("rid", "p")
    t.event("channel_ok", channel="github", degraded=False)
    t.event("channel_skipped", channel="x", degraded=False)
    t.event("review_empty", channel="x")               # degraded 默认 True
    t.event("llm_analyze_fallback", error="boom")
    m = t.meta()
    assert m["request_id"] == "rid"
    assert m["degradations"] == ["review_empty", "llm_analyze_fallback"]
    assert "channel_ok" not in m["degradations"]


def test_capture_exception_without_sentry_is_noop():
    obs.capture_exception(ValueError("x"))             # 没装 sentry-sdk 也不应抛


def test_init_sentry_without_dsn_returns_false(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert obs.init_sentry() is False
