#!/usr/bin/env python3
"""状态与检查点实跑：回放、对比、崩溃恢复的副作用去重。

运行方式（工程根目录下）::

    python examples/state_replay_demo.py

全程离线：不联网、不需要 API Key、不写用户目录。

看四件事：

1. **录下来**——一次运行的模型响应与每一步；
2. **回放**——用录下来的响应重跑，逐步对比；
3. **对比**——把工具的行为改掉，回放立刻报出差异（这才是回放的用处）；
4. **恢复**——崩溃后重启，副作用动作不会被执行第二次。
"""
from __future__ import annotations

import dataclasses
import pathlib
import sys

# 允许不安装、直接在工程根目录跑这个脚本。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.agent import Agent
from dragon_agent.config import Config
from dragon_agent.context import ContextAssembler
from dragon_agent.llm import MockLLM
from dragon_agent.state import (
    ExecutedActions,
    RecordingRegistry,
    StateStore,
    compare_steps,
    record_run,
    replay,
    restore_executed_actions,
)
from dragon_agent.tools import ToolRegistry, ToolSpec

TASK = "读一下预算表，并告诉我总额"


def make_tools(answers: dict | None = None) -> ToolRegistry:
    """一组确定性工具。``answers`` 可以改掉某个工具的返回值，用来制造差异。"""
    answers = answers or {}
    registry = ToolRegistry()
    calls = {"read_budget": 0, "charge": 0}

    def read_budget(year: str) -> dict:
        calls["read_budget"] += 1
        return answers.get("read_budget", {"year": year, "total": 1_000_000})

    def charge(amount: int) -> dict:
        calls["charge"] += 1
        return {"charged": amount}

    registry.register(
        ToolSpec(
            name="read_budget",
            description="读取指定财年的预算表。",
            parameters={
                "type": "object",
                "properties": {"year": {"type": "string"}},
                "required": ["year"],
            },
        ),
        read_budget,
    )
    registry.register(
        ToolSpec(
            name="charge",
            description="扣款。副作用且不可重放。",
            parameters={
                "type": "object",
                "properties": {"amount": {"type": "integer"}},
                "required": ["amount"],
            },
            side_effect=True,
            replayable=False,
        ),
        charge,
    )
    registry.calls = calls
    return registry


def build_agent(llm, registry=None, executed=None):
    """按签名装配 Agent。录制与回放走的是同一套装配代码。"""
    config = dataclasses.replace(Config.from_env_and_args([]), max_steps=6)
    registry = registry if registry is not None else make_tools()
    if executed is not None:
        registry = RecordingRegistry(registry, executed)
    context = ContextAssembler(config)
    state = StateStore(config, task_id="demo-task")
    return Agent(config, llm, registry, context, state)


def script() -> list:
    return [
        MockLLM.call("read_budget", {"year": "2026"}, in_tokens=120, out_tokens=18),
        MockLLM.say("2026 年预算总额 100 万元。", in_tokens=180, out_tokens=22),
    ]


def main() -> int:
    # ------------------------------------------------------------ 1
    print("=" * 74)
    print("1) 录下来：一次运行的模型响应与每一步")
    print("=" * 74)
    record = record_run(build_agent, TASK, MockLLM(script=script()))
    print(f"  任务：{record.task}")
    print(f"  录到 {len(record.responses)} 条模型响应，{len(record.steps)} 个步骤")
    for step in record.steps:
        action = step["action"] or "（无动作）"
        print(f"    第 {step['index']} 步：{action} → {step['observation'][:40]}")
    print(f"  最终状态：{record.final['status']}，停止原因：{record.final['stop_reason']}")

    # ------------------------------------------------------------ 2
    print()
    print("=" * 74)
    print("2) 回放：用录下来的响应重跑一遍")
    print("=" * 74)
    result = replay(build_agent, record, lambda responses: MockLLM(script=list(responses)))
    print(f"  回放状态：{result.status}")
    print(f"  与原运行一致：{result.identical}")
    print(f"  差异：{result.differences or '（没有差异）'}")

    # ------------------------------------------------------------ 3
    print()
    print("=" * 74)
    print("3) 对比：把工具的行为改掉，回放立刻报出差异")
    print("=" * 74)
    changed = make_tools(answers={"read_budget": {"year": "2026", "total": 999_999}})
    result2 = replay(
        lambda llm: build_agent(llm, registry=changed),
        record,
        lambda responses: MockLLM(script=list(responses)),
    )
    print(f"  与原运行一致：{result2.identical}")
    for diff in result2.differences:
        print(f"    · {diff}")

    # ------------------------------------------------------------ 4
    print()
    print("=" * 74)
    print("4) 恢复：崩溃重启后，副作用动作不会被执行第二次")
    print("=" * 74)
    executed = ExecutedActions()
    registry = RecordingRegistry(make_tools(), executed)
    first = registry.invoke("charge", {"amount": 100}, idempotency_key="order-42")
    print(f"  第一次执行：ok={first.ok}  output={first.output}")
    print(f"  登记里有 {len(executed)} 条已执行动作")

    # 模拟崩溃：进程重启，工具层的进程内缓存没了，但登记还在
    restarted = make_tools()
    restored = restore_executed_actions(restarted, executed)
    second = restarted.invoke("charge", {"amount": 100}, idempotency_key="order-42")
    print(f"  重启后装回登记：{restored} 条")
    print(f"  重启后再执行：ok={second.ok}  error={second.error}")
    print(f"  扣款函数实际被调用：{restarted.calls['charge']} 次（第一次那次是在重启前的进程里）")

    ok = result.identical and not result2.identical and second.ok is False
    print()
    print(f"自检结果：{'全部通过' if ok else '有项目未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
