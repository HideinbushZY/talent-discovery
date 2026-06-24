# 从问题出发的人才发现 (Talent Discovery, v1 Demo)

[![CI](https://github.com/HideinbushZY/talent-discovery/actions/workflows/ci.yml/badge.svg)](https://github.com/HideinbushZY/talent-discovery/actions/workflows/ci.yml)

输入一个**企业的难题**，系统把它翻译成可检索的信号，在 **GitHub**（动手实现/构建过这类问题的人）和 **X / Twitter**（在这类问题上最前沿、最有话语权的人）两个渠道**诚实地**并行找人，产出一个**融合总榜 dashboard**——没有对应人才的通道会如实跳过并说明。

全程**实时**调用：GitHub REST/Code Search + X API v2 + Kimi（Moonshot）。

---

## 快速开始

```bash
cd talent-discovery
cp .env.example .env        # 填入你的密钥（见下）
./run.sh                    # 首次会自动建 venv + 装依赖
# 打开 http://127.0.0.1:8848
```

手动方式：

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn app.main:app --reload --port 8848
```

---

## 公网部署（Railway）

部署后会得到一个 `https://<your-app>.up.railway.app` 地址。
访问受 **HTTP Basic Auth** 保护（用户名任意，密码 = Railway 变量 `APP_PASSWORD`）。

部署要点：
- 启动命令在 `Procfile`（`uvicorn app.main:app --host 0.0.0.0 --port $PORT`）；`.python-version` 锁 3.11。
- 密钥与口令**只存在 Railway service 变量里**，不进 git / 不进镜像（`.railwayignore` 排除 `.env`、`.venv`）。
- 公网防滥用：`APP_PASSWORD` 口令门 + 进程级 `X_SESSION_READ_CAP` + 线上 `X_READ_BUDGET=150`（约 $0.75/次）。

重新部署 / 改配置：
```bash
railway up -y                      # 重新部署当前目录
railway variables --set "APP_PASSWORD=新口令"   # 改口令（会触发重部署）
railway redeploy -y                # 仅用新变量重启（不重建）
railway down                       # 下线（删除部署）
```
> 注意：用 `--skip-deploys` 设变量后，必须 `railway redeploy` 才会把变量注入运行容器。

---

## 密钥（`.env`，本地开发用）

| 变量 | 用途 | 获取 |
|---|---|---|
| `GITHUB_TOKEN` | 读公开数据 + 代码搜索（5000/hr） | GitHub → Settings → Developer settings → Personal access tokens（fine-grained：Public Repositories read-only 即可） |
| `X_API_BEARER_TOKEN` | X v2 推文搜索（按量付费 $0.005/读） | console.x.com → 创建 App → Keys and tokens → Bearer Token（**保持控制台显示的 URL 编码原样**，含 `%2B`/`%3D`） |
| `KIMI_API_KEY` | Kimi / Moonshot LLM 认证（sk- 开头） | platform.moonshot.cn → API Keys |
| `KIMI_BASE_URL` | OpenAI 兼容接口地址 | `https://api.moonshot.cn/v1`（国际站 `.ai`） |
| `KIMI_MODEL` | 阶段1 难题理解模型（重质量） | 当前 `kimi-k2.6` |
| `LLM_REVIEW_MODEL` | 阶段3 复核/打分模型（非思考、更快） | 当前 `moonshot-v1-128k`（比 k2.6 快 ~2.6×） |
| `APP_PASSWORD` | 公网访问口令（用户名任意/密码=此值） | 自定义；公网部署必填 |
| `X_READ_BUDGET` | 每次搜索 X 读取上限（先小后大） | 默认 `300` |
| `X_SESSION_READ_CAP` | 进程级 X 读取总上限（防失控，约 $15 触顶即停） | 默认 `3000` |
| `TOP_N_PER_CHANNEL` | 每通道进入评分的候选数 | 默认 `40` |
| `LLM_FALLBACK_MODEL` | 主模型失败后的兜底模型（同 key） | 默认 `moonshot-v1-128k`（非思考、更稳） |
| `LOG_FORMAT` / `LOG_LEVEL` | 日志格式 / 级别 | `json`（默认）/ `INFO` |
| `SENTRY_DSN` | 异常上报（需另装 `sentry-sdk`） | 选填 |

> 完整变量（含第三方供应商兜底 `LLM_FALLBACK_API_KEY` / `_BASE_URL` / `_PROVIDER_MODEL`、`PORT` 等）见 `.env.example`。
>
> 注意：`kimi-k2.6` 默认开启 thinking，与强制具名 tool_choice 不兼容，故 LLM 层用 JSON 输出模式约束结构（见 `app/llm.py`）。
> 健康检查：`curl -u u:口令 localhost:8848/api/health` 会返回脱敏配置 + 探测到的模型，便于排错。

---

## 它怎么工作（四阶段管线）

```
难题(自然语言)
  → [1] 难题理解：分型 + 成熟度 + 逐通道适用性 + 检索计划   (Kimi / JSON 结构化输出)
  → [2] 双渠道并行采集（只跑 applicable 的通道）
        ├ GitHub: 核心 repo 贡献排名 + 相关模块提交 + 全网代码搜索 → 去重到"人"
        └ X:      话题相关近期帖 → 聚合高影响力/高互动作者（硬性读取预算）
  → [3] 评分与画像：0-100 问题契合度 + 证据强度(hard/soft) + 可挖性 + 一句 why  (Kimi 复核 + 打分函数)
  → [4] 融合总榜 Dashboard（统一排名 + 来源徽章 + 逐通道诚实说明 + 实验性提示）
```

**诚实路由**：不预设哪个通道该有人。纯品牌/创意营销、纯管理类难题，GitHub 通常 `applicable=false`，dashboard 会显示"这儿没有能解决它的人才，已跳过"。技术/营销=完整支持；经营/业务/管理=实验性（挂提示条）。

**评分**（两渠道归一到同一把尺子，再按通道权重轻度加权）：
- GitHub: `100 ×(0.50×相关性 + 0.35×贡献深度 + 0.15×活跃度)`，代码贡献=hard 证据
- X: `100 ×(0.50×话题相关性 + 0.35×影响力互动 + 0.15×活跃度)`，观点/影响力=soft 证据
- 诚实标注：渠道内排名比跨渠道绝对分更可靠（权重为配置项）。

---

## API

- `GET /` — 单页前端（零构建）
- `POST /api/search {"problem": "..."}` → `{"job_id": "..."}` — 建**后台作业**，立即返回（不阻塞）
- `GET /api/search/{job_id}` — 轮询/恢复：作业状态 + 最终结果（内存没有则查库）
- `GET /api/search/{job_id}/stream` — **SSE** 续传进度（支持 `Last-Event-ID` 断点续传 + 心跳保活）
- `GET /api/health` — 配置 + 模型自检

> **Phase B 架构**：搜索是后台作业——管线 detached 跑完、结果落库（SQLite，见 `app/store.py`），
> 客户端断开/代理空闲超时**都不丢结果**：SSE 自动重连续传、轮询兜底、刷新页面也能用 job_id 取回。
> 这解决了"130–290 秒长请求被代理掐断"的问题。多实例/水平扩展再把内存注册表+SQLite 换成 Redis/Postgres。

> 设置 `APP_PASSWORD` 后，以上所有接口都需 HTTP Basic Auth（用户名任意 / 密码=口令）；浏览器 EventSource 在页面登录后会自动带上凭证。

候选人统一 schema 见 `app/models.py`（`Candidate`）。

---

## 成本（一次搜索）

| 项 | 估算 |
|---|---|
| GitHub | $0 |
| X（≤300 读） | 约 $0.5–1.5（先用小预算 `X_READ_BUDGET=60` 跑通） |
| Kimi（Moonshot，分型 + 两批复核画像） | 约 $0.02–0.2（按 Moonshot 计费） |

---

## 代码结构

```
app/
  main.py          FastAPI：作业化搜索（POST建作业/GET取结果/SSE续传）+ health + 静态
  jobs.py          作业管理：把长流水线从 HTTP 请求解耦（后台跑、累积事件、落库）
  store.py         结果持久化（SQLite，stdlib；多实例换 Postgres）
  pipeline.py      四阶段编排（异步生成器，流式进度）
  llm.py           Kimi/Moonshot（OpenAI 兼容，JSON 模式）：分型路由 + 相关性复核 + 画像
  config.py        .env 读取 + 成本/范围配置
  models.py        统一 pydantic schema
  scoring.py       打分函数 + 可挖性启发式
  cache.py         TTL 缓存 + 限速 + 限并发
  observability.py 结构化日志 + 每次搜索 trace（耗时/降级）+ 可选 Sentry
  connectors/
    base.py        通道连接器接口（可插拔，便于加 LinkedIn/Substack…）
    github.py      GitHubConnector（混合策略）
    x.py           XConnector（读取预算 + 聚合作者）
web/
  index.html       Tailwind CDN + 原生 JS 的融合总榜 dashboard
tests/             pytest 单元 + 集成测试（不触网、不花钱）
evals/             阶段1 路由质量评测（golden set + runner）
```

## 测试与评测

```bash
# 安装开发依赖
./.venv/bin/pip install -r requirements-dev.txt

# 单元 + 集成测试（mock 掉 GitHub/X/Kimi，确定性、秒级、零成本）
./.venv/bin/python -m pytest

# 阶段1 路由质量评测（会调用真实 Kimi，需 KIMI_API_KEY）
./.venv/bin/python -m evals.run_eval
```

- `tests/`：评分公式、权重归一化、JSON 解析、缓存/限速、连接器解析，以及**整条管线的集成测试**（双通道排序、GitHub 诚实跳过、experimental 提示、LLM 失败兜底）。
- `evals/golden_set.json`：一组难题 + 期望的 category / maturity / 逐通道适用性，作为改 prompt 或换模型后的**回归门槛**。往里加例子即可扩大覆盖。

**LLM 分工 + 韧性**：阶段1 难题理解用 `kimi-k2.6`（重质量）；**阶段3 复核/打分用非思考的 `moonshot-v1-128k`**（重速度，复核耗时 ~71s→27s）。单次调用失败/空内容会**重试并加大 token 预算**；耗尽重试后**自动降级**到下一个供应商（复核失败回退到 kimi-k2.6），还可配 `LLM_FALLBACK_API_KEY` 接入完全不同的供应商。降级打 `llm_provider_failover` 日志。

**可观测性**：每次搜索在日志里留**一行结构化 JSON**（`search.done`）——含 `request_id`、各阶段耗时、降级事件、X 读取数、模型。`request_id` 也回传到结果 `meta` 并显示在页面，便于和日志对账。降级（LLM 兜底、通道出错、复核失败、限速）走 `WARNING`。配 `SENTRY_DSN` 可把异常上报 Sentry。

**CI（GitHub Actions）**：
- `.github/workflows/ci.yml` —— 每次 push / PR 自动跑 `pytest`（不触网、不需密钥）。
- `.github/workflows/eval.yml` —— **手动触发**的路由评测（调用真实 Kimi）。需在仓库 `Settings → Secrets and variables → Actions` 添加 `KIMI_API_KEY`。

## v1 不做（后续迭代）

新通道（LinkedIn / Substack / HF / arXiv）、跨渠道身份打通（GitHub↔X 同一人合并）、outreach 草稿、候选保存与追踪、权重可视化调参。架构已为新通道预留可插拔 connector 接口。
