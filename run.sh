#!/usr/bin/env bash
# 一键启动：从问题出发的人才发现 (v1 demo)
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "首次运行：创建 venv 并安装依赖…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f ".env" ]; then
  echo "⚠ 缺少 .env，请先 cp .env.example .env 并填入密钥（见 README.md）"
  exit 1
fi

PORT="${PORT:-8848}"
echo "→ http://127.0.0.1:${PORT}"
exec ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" "$@"
