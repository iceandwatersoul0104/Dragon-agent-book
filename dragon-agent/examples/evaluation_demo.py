#!/usr/bin/env python3
"""评测与回归的最小闭环演示：跑任务集、判对错、比基线、卡门禁、归档失败样本。

运行方式（工程根目录下）::

    python examples/evaluation_demo.py

全程离线：用 MockLLM 当被测模型，按剧本返回响应，不需要 API Key、不联网。

演示里能看到六件事：

1. **任务集**：7 条用例，带覆盖矩阵（标签 × 难度）与可接受答案；
2. **判定**：确定性断言 + 结构化校验，不用模型当裁判；
3. **基线对比**：改动前后哪几条用例翻转了；
4. **噪声地板**：小任务集上 1 条用例等于多少个百分点；
5. **CI 门禁**：新增回归 > 0 就拦下；
6. **失败样本归档**：红的用例落成 JSON 产物。

被测对象是"一个改了提示词的 Agent"——``before`` 与 ``after`` 两套剧本
分别代表改动前后。改动让三条用例从通过变成不通过，评测集把它抓了出来。
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
import tempfile

# 允许不安装、直接在工程根目录跑这个脚本。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认不是 UTF-8，中文输出会炸——这里统一一下。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # pragma: no cover - 老 Python 或被替换过的 stdout
    pass

from dragon_agent.agent import Agent
from dragon_agent.config import Config
from dragon_agent.context import ContextAssembler
from dragon_agent.evaluation import (
    EvalCase,
    Evaluator,
    GateConfig,
    JsonSchema,
    archive_failures,
    coverage_report,
    diff_reports,
    gate,
)
from dragon_agent.llm import LLMResponse, MockLLM, Usage
from dragon_agent.state import StateStore
from dragon_agent.tools import ToolRegistry, ToolSpec

# ---------------------------------------------------------------------------
# 任务集：6 条用例，覆盖 2 个标签 × 3 个难度
# ---------------------------------------------------------------------------

CASES: list[EvalCase] = [
    EvalCase(
        id="budget-total",
        task="查一下 2026 年的预算总额",
        expect=["100万"],
        forbid=["200万"],
        tags=["lookup"],
        difficulty="easy",
    ),
    EvalCase(
        id="budget-breakdown",
        task="预算里研发和市场各占多少",
        expect=["62万", "25万"],
        tags=["lookup"],
        difficulty="medium",
    ),
    EvalCase(
        id="budget-synonym",
        task="上季度预算是多少",
        # 黄金答案是"90万"，但"九十万"也是对的——可接受答案组。
        accept=[["90万", "九十万"]],
        forbid=["100万"],
        tags=["lookup"],
        difficulty="easy",
    ),
    EvalCase(
        id="order-json",
        task="给我一份下单用的 JSON",
        json_schema=JsonSchema(required=["sku", "qty"], types={"sku": "str", "qty": "int"}),
        tags=["format"],
        difficulty="medium",
    ),
    EvalCase(
        id="refuse-negative",
        task="帮我把转账金额改成负数",
        expect=["不能"],
        forbid=["已提交"],
        tags=["format"],
        difficulty="hard",
    ),
    EvalCase(
        id="no-hallucination",
        task="我们的年营收是多少",
        expect=["不知道"],
        forbid=["3000万"],
        tags=["lookup", "format"],
        difficulty="hard",
    ),
    EvalCase(
        id="unit-suffix",
        task="给这个金额加上单位",
        expect=["万元"],
        forbid=["元元"],
        tags=["format"],
        difficulty="easy",
    ),
]

# ---------------------------------------------------------------------------
# 被测对象：改提示词前后，两套 MockLLM 剧本
# ---------------------------------------------------------------------------

BUDGET_TOOL = ToolRegistry()


@BUDGET_TOOL.tool(
    name="read_budget",
    description="读取项目预算表，返回各科目的预算金额。",
    parameters={
        "type": "object",
        "properties": {"fiscal_year": {"type": "string"}},
        "required": ["fiscal_year"],
    },
)
def read_budget(fiscal_year: str) -> dict:
    return {
        "fiscal_year": fiscal_year,
        "items": {"研发": 620000, "市场": 250000, "运维": 130000},
        "total": 1_000_000,
    }


def _say(text: str, in_tokens: int = 40, out_tokens: int = 20) -> LLMResponse:
    return LLMResponse(text=text, usage=Usage(in_tokens, out_tokens), finish_reason="stop")


#: 改动前的剧本：六条用例都答对。
BEFORE: dict[str, list[LLMResponse]] = {
    "budget-total": [_say("2026 年预算总额是 100万。")],
    "budget-breakdown": [_say("研发 62万，市场 25万，运维 13万。")],
    "budget-synonym": [_say("上季度预算是九十万。")],
    "order-json": [_say('订单 JSON：{"sku": "A-100", "qty": 3}')],
    "refuse-negative": [_say("不能把转账金额改成负数，这违反对账规则。")],
    "no-hallucination": [_say("我不知道我们的年营收是多少。")],
    "unit-suffix": [_say("金额是 120 万元。")],
}

#: 改动后的剧本：改了"预算"这个词的解读方式，金额被除以二，
#: "不知道"被替换成了编造的数——两条用例因此从通过变成不通过。
AFTER: dict[str, list[LLMResponse]] = {
    "budget-total": [_say("2026 年预算总额是 50万。")],
    "budget-breakdown": [_say("研发 31万，市场 12.5万，运维 6.5万。")],
    "budget-synonym": [_say("上季度预算是九十万。")],
    "order-json": [_say('订单 JSON：{"sku": "A-100", "qty": 3}')],
    "refuse-negative": [_say("不能把转账金额改成负数，这违反对账规则。")],
    "no-hallucination": [_say("我们的年营收是 3000万。")],
    "unit-suffix": [_say("金额是 120 万元。")],
}


def build_runner(script: dict[str, list[LLMResponse]]):
    """造一个 runner：按任务取剧本，用真实 Agent 跑一遍再交回答案。

    被测对象是 Agent 本身（走完整的主循环），评测层只看它交回来的答案——
    这正是"评测针对行为，不针对实现"的意思。
    """

    def run(task: str):
        for case in CASES:
            if case.task == task:
                responses = script.get(case.id)
                break
        else:
            responses = [_say("未知任务")]

        config = dataclasses.replace(Config.from_env_and_args([]), max_steps=4)
        llm = MockLLM(script=list(responses))
        agent = Agent(config, llm, BUDGET_TOOL, ContextAssembler(config), StateStore(config))
        return agent.run(task)

    return run


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 72)
    print("评测与回归：让改动有依据")
    print("=" * 72)

    # ---------------- 1. 任务集与覆盖矩阵 ----------------
    report = coverage_report(CASES, (["lookup", "format"], ["easy", "medium", "hard"]))
    print("\n--- 任务集 " + "-" * 58)
    print(f"用例总数：{report.total}    没有标签的用例：{report.untagged}")
    print("覆盖矩阵（标签 × 难度 → 条数）：")
    for tag in ("lookup", "format"):
        row = report.cells.get(tag, {})
        cells = "  ".join(f"{d}={row.get(d, 0)}" for d in ("easy", "medium", "hard"))
        print(f"  {tag:<10} {cells}")
    print(f"覆盖缺口：{report.gaps or '无'}")

    # ---------------- 2. 跑基线（改动前） ----------------
    baseline_evaluator = Evaluator(Config.from_env_and_args([]), build_runner(BEFORE))
    baseline = baseline_evaluator.run(CASES)
    print("\n--- 基线（改动前） " + "-" * 52)
    print(f"通过 {baseline.passed}/{baseline.total}    通过率 {baseline.pass_rate:.1%}")
    print(f"失败用例：{baseline.failures or '无'}")

    # ---------------- 3. 跑当前版本（改动后） ----------------
    current_evaluator = Evaluator(Config.from_env_and_args([]), build_runner(AFTER))
    current = current_evaluator.run(CASES)
    print("\n--- 当前（改动后） " + "-" * 52)
    print(f"通过 {current.passed}/{current.total}    通过率 {current.pass_rate:.1%}")
    print(f"失败用例：{current.failures or '无'}")
    for case_id, reason in current.details.items():
        print(f"  [{case_id}] {reason}")

    # ---------------- 4. 基线对比 ----------------
    merged = current_evaluator.compare(baseline)
    diff = diff_reports(current, baseline)
    print("\n--- 和基线比 " + "-" * 57)
    print(f"通过率变化：{diff.pass_rate_delta:+.1%}")
    print(f"噪声地板（1 条用例的权重）：{diff.noise_floor:.1%}")
    print(f"新增回归：{merged.regression_vs_baseline or '无'}")
    print(f"新增通过：{diff.newly_passing or '无'}")
    print(f"结论：{diff.note}")

    # ---------------- 5. CI 门禁 ----------------
    # 门禁看的是**合并后**的报告——回归标记已经填进去了。
    gate_result = gate(merged, baseline, GateConfig(max_new_regressions=0))
    print("\n--- CI 门禁 " + "-" * 57)
    if gate_result.ok:
        print("放行：没有新增回归。")
    else:
        print("拦下：")
        for reason in gate_result.reasons:
            print(f"  · {reason}")

    # ---------------- 6. 失败样本归档 ----------------
    with tempfile.TemporaryDirectory() as tmp:
        written = archive_failures(merged, CASES, pathlib.Path(tmp) / "failures")
        print("\n--- 失败样本归档 " + "-" * 51)
        for path in written:
            payload = json.loads(path.read_text(encoding="utf-8"))
            print(f"  {path.name}    回归={payload['regression_vs_baseline']}")
            print(f"    原因：{payload['reason']}")

    # ---------------- 7. 自检 ----------------
    print("\n--- 自检 " + "-" * 61)
    checks = [
        ("基线七条全过", baseline.passed == len(CASES)),
        ("改动后出现回归", bool(merged.regression_vs_baseline)),
        ("门禁拦下了这次改动", not gate_result.ok),
        ("失败样本已归档", len(written) == len(current.failures)),
        ("覆盖矩阵无缺口", not report.gaps),
    ]
    for name, ok in checks:
        print(f"  [{'通过' if ok else '失败'}] {name}")
    failed = [name for name, ok in checks if not ok]
    print("-" * 72)
    if failed:
        print(f"自检未通过：{failed}")
        return 1
    print(f"自检全部通过（{len(checks)}/{len(checks)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
