"""集中读取环境变量与运行配置。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根（app/ 的上一级）
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# ── 访问控制 ──────────────────────────────────────────────────
# 设置后，所有页面/接口需 HTTP Basic Auth（用户名任意，密码=此值）。
# 公网部署必须设置；留空=本地开放（仅供本机开发）。
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()

# ── 密钥 ──────────────────────────────────────────────────────
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
X_BEARER_TOKEN = os.getenv("X_API_BEARER_TOKEN", "").strip()

# LLM = Kimi / Moonshot（OpenAI 兼容接口）
KIMI_API_KEY = (os.getenv("KIMI_API_KEY", "") or os.getenv("MOONSHOT_API_KEY", "")).strip()
KIMI_BASE_URL = os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1").strip().rstrip("/")
KIMI_MODEL = os.getenv("KIMI_MODEL", "kimi-k2.6").strip() or "kimi-k2.6"

# ── 成本 / 范围控制 ───────────────────────────────────────────
X_READ_BUDGET = _int("X_READ_BUDGET", 300)        # 每次搜索 X 帖子读取上限
X_SESSION_READ_CAP = _int("X_SESSION_READ_CAP", 3000)  # 进程级 X 读取总上限（防失控，~$15）
TOP_N_PER_CHANNEL = _int("TOP_N_PER_CHANNEL", 40)  # 每通道进入评分的候选数
PORT = _int("PORT", 8848)

# ── 派生开关 ──────────────────────────────────────────────────
HAS_GITHUB = bool(GITHUB_TOKEN)
HAS_X = bool(X_BEARER_TOKEN)
HAS_LLM = bool(KIMI_API_KEY)


def summary() -> dict:
    """启动时打印的脱敏配置概览。"""
    def mask(v: str) -> str:
        if not v:
            return "(未设置)"
        return f"{v[:6]}…{v[-4:]} ({len(v)} chars)"

    return {
        "github_token": mask(GITHUB_TOKEN),
        "x_bearer": mask(X_BEARER_TOKEN),
        "kimi_key": mask(KIMI_API_KEY),
        "llm_base_url": KIMI_BASE_URL,
        "llm_model": KIMI_MODEL,
        "x_read_budget": X_READ_BUDGET,
        "top_n_per_channel": TOP_N_PER_CHANNEL,
    }
