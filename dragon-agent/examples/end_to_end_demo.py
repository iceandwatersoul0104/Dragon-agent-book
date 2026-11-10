#!/usr/bin/env python3
"""端到端示例：从用户输入走到最终答案，把每一步都摊开给你看。

运行方式（工程根目录下）::

    python examples/end_to_end_demo.py

全程离线：用 MockLLM 按剧本返回响应，不需要 API Key，也不需要网络。
输出里能看到四样东西：

1. 每一步的**动作**（模型想调什么工具）与**观察**（工具回了什么）；
2. 每一步的 **token** 与**成本**；
3. 最终答案与**停止原因**；
4. 这一轮产生的 **trace**（span 列表）。

想换成真实模型，把 ``build_llm`` 里的 ``MockLLM`` 换成
``dragon_agent.llm.build_client(config)`` 并设好 ``LLM_API_KEY`` 即可——
控制流一个字都不用改，这就是「控制流与不确定性分离」的实际样子。
"""
from __future__ import annotations

import dataclasses
import pathlib
import sys

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
from dragon_agent.llm import LLMResponse, MockLLM, ToolCall, Usage
from dragon_agent.state import StateStore
from dragon_agent.tools import ToolRegistry, ToolSpec

TASK = "查一下项目预算，并告诉我总额"

# --------------------------------------------------------------------- 工具


def build_tools() -> ToolRegistry:
    """注册两个工具：一个纯读，一个带副作用。"""
    registry = ToolRegistry()

    @registry.tool(
        name="read_budget",
        description="读取项目的预算表，返回各科目的预算金额。",
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

    @registry.tool(
        name="notify",
        description="给相关同事发一条通知。有副作用，幂等可重放。",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        side_effect=True,
        replayable=True,
    )
    def notify(text: str) -> dict:
        return {"sent": True, "chars": len(text)}

    return registry


# --------------------------------------------------------------------- 模型


def build_llm() -> MockLLM:
    """按剧本返回响应的假模型。

    剧本 = 模型在这个任务上会做的两次决策：
    第一次决定去查预算表，第二次把观察整理成答案。
    """
    return MockLLM(script=[
        LLMResponse(
            text="先读预算表",
            tool_calls=[ToolCall(name="read_budget", arguments={"fiscal_year": "2026"})],
            usage=Usage(input_tokens=128, output_tokens=21),
            finish_reason="tool_calls",
        ),
        LLMResponse(
            text="项目 2026 年预算总额为 100 万元，其中研发 62 万、市场 25 万、运维 13 万。",
            usage=Usage(input_tokens=196, output_tokens=34),
            finish_reason="stop",
        ),
    ])


# --------------------------------------------------------------------- 主流程


def main() -> int:
    # Config 是 frozen dataclass，要改字段就重建一份（不原地改）。
    config = dataclasses.replace(Config.from_env_and_args([]), max_steps=6)

    llm = build_llm()
    tools = build_tools()
    context = ContextAssembler(config)
    state = StateStore(config)
    agent = Agent(config, llm, tools, context, state)

    print("=" * 72)
    print(f"任务：{TASK}")
    print(f"模型：{llm.describe()}")
    print(f"预算：步数 {config.max_steps} / token {config.token_budget} / "
          f"成本 {config.cost_budget} 美元")
    print("=" * 72)

    result = agent.run(TASK)

    print("\n--- 轨迹 " + "-" * 60)
    for step in result.steps:
        action = "（无动作）"
        if step.action is not None:
            action = f"{step.action.name}({step.action.arguments})"
        print(f"[第 {step.index} 步]")
        print(f"  思考   ：{step.thought or '（空）'}")
        print(f"  动作   ：{action}")
        print(f"  观察   ：{step.observation or '（空）'}")
        print(f"  用量   ：{step.tokens} tokens / {step.cost:.6f} 美元")
    print("-" * 72)

    print("\n最终答案：")
    print(result.answer)

    print("\n--- 成本账 " + "-" * 60)
    print(f"步数       ：{len(result.steps)}")
    print(f"token 合计：{result.total_tokens}")
    print(f"成本合计   ：{result.total_cost:.6f} 美元")
    print(f"最终状态   ：{result.status}")
    print(f"停止原因   ：{result.stop_reason}")
    print("-" * 72)

    tracer = getattr(agent, "tracer", None)
    print("\n--- trace " + "-" * 62)
    if tracer is not None:
        for span in tracer.spans():
            attrs = " ".join(f"{k}={v}" for k, v in sorted(span.attributes.items()))
            print(f"{span.name} [{span.status}] {attrs}")
    else:
        print("这一轮没有 trace：observability 模块尚未落地，Agent 拿到的是空 tracer。")
        print("模块到位后，这里会列出这一轮产生的每一个 span（模型、工具、token、成本）。")
    print("-" * 72)

    print("\n提示：把上面的 MockLLM 换成 build_client(config) 并设好 LLM_API_KEY，")
    print("      就能用同一套控制流去调真实模型。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
