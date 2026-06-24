"""配置脱敏（app/config.py summary）。"""
from app import config


def test_mask_known_value(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "abcdefghij")
    assert config.summary()["github_token"] == "abcdef…ghij (10 chars)"


def test_mask_empty(monkeypatch):
    monkeypatch.setattr(config, "X_BEARER_TOKEN", "")
    assert config.summary()["x_bearer"] == "(未设置)"
