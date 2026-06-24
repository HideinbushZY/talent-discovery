"""阶段1 路由质量评测（需 KIMI_API_KEY，会调用真实 LLM）。

对 golden_set.json 跑 analyze_problem，比对可确定属性：
category / maturity / 逐通道适用性。这些应当稳定，可作为改 prompt/换模型后的回归门槛。

用法（项目根目录）：
    ./.venv/bin/python -m evals.run_eval
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from app import config, llm

GOLDEN = Path(__file__).parent / "golden_set.json"
FIELDS = ["category", "maturity", "github_applicable", "x_applicable"]


async def main() -> int:
    if not config.HAS_LLM:
        print("✗ 需要设置 KIMI_API_KEY 才能跑评测。")
        return 1

    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))
    totals = {f: [0, 0] for f in FIELDS}   # [correct, total]
    rows = []

    for c in cases:
        try:
            a = await llm.analyze_problem(c["problem"])
        except Exception as e:  # noqa: BLE001
            rows.append((c["problem"][:26], "ERROR " + str(e)[:50]))
            continue
        got = {
            "category": a["category"],
            "maturity": a["maturity"],
            "github_applicable": a["channels"]["github"]["applicable"],
            "x_applicable": a["channels"]["x"]["applicable"],
        }
        marks = []
        for f in FIELDS:
            if f in c:
                ok = got[f] == c[f]
                totals[f][0] += int(ok)
                totals[f][1] += 1
                marks.append(f"{f.split('_')[0]}={'✓' if ok else '✗(' + str(got[f]) + ')'}")
        rows.append((c["problem"][:26], "  ".join(marks)))

    print("=== 阶段1 路由评测 ===")
    for p, m in rows:
        print(f"  {p:28} {m}")
    print("--- 准确率 ---")
    oc = ot = 0
    for f in FIELDS:
        cc, tt = totals[f]
        oc += cc
        ot += tt
        if tt:
            print(f"  {f:18} {cc}/{tt} = {cc / tt * 100:.0f}%")
    if ot:
        print(f"  {'overall':18} {oc}/{ot} = {oc / ot * 100:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
