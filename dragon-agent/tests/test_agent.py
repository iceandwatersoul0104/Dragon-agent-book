"""Agent 主循环的确定性测试：每条停止路径都跑一遍。

全程离线：用 ``llm.py`` 里已有的 ``MockLLM`` 按剧本返回响应，
不联网、不需要 API Key、不写用户目录。

测的六条路径：正常完成、预算耗尽、无进展、步数上限、工具报错后继续、
``run()`` 不抛异常；另外还有停止优先级、错误回灌、每步检查点。
"""
from __future__ import annotations

import dataclasses
import pathlib
import sys
import unittest
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.agent import (
    Agent,
    AgentResult,
    AgentStep,
    CompositeBudgetGuard,
    CostBudgetGuard,
    TimeBudgetGuard,
    TokenBudgetGuard,
    build_budget_guard,
)
from dragon_agent.config import Config
from dragon_agent.context import ContextAssembler
from dragon_agent.llm import LLMResponse, MockLLM, ToolCall, Usage
from dragon_agent.state import StateStore
from dragon_agent.tools import ToolRegistry, ToolSpec


def build_stack(llm, *, max_steps=12, token_budget=120_000, registry=None, calls=None):
    """把「配置 + 模型 + 工具 + 上下文 + 状态 + Agent」这条链接起来。"""
    config = dataclasses.replace(
        Config.from_env_and_args([]), max_steps=max_steps, token_budget=token_budget
    )
    registry = registry if registry is not None else make_tools(calls if calls is not None else {})
    context = ContextAssembler(config)
    state = StateStore(config, task_id="test-task")
    agent = Agent(config, llm, registry, context, state)
    return agent, registry, state


def make_tools(calls: dict) -> ToolRegistry:
    """一组确定性工具，外加"每个函数被调用了几次"的计数。"""

    def echo(text: str) -> str:
        calls["echo"] = calls.get("echo", 0) + 1
        return text

    def boom(text: str) -> str:
        calls["boom"] = calls.get("boom", 0) + 1
        raise RuntimeError("下游挂了")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="echo",
            description="原样返回一段文本。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        echo,
    )
    registry.register(
        ToolSpec(
            name="boom",
            description="总是失败的工具。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        boom,
    )
    return registry


class ExplodingLLM:
    """任何调用都炸的模型。"""

    def complete(self, messages, tools=None, **kwargs):
        raise RuntimeError("模型炸了")

    def describe(self) -> str:
        return "exploding"


@dataclass
class FakeDecision:
    """假护栏的判定结果——字段与护栏模块的 Decision 一致。"""

    allow: bool = True
    reason: str = ""


class FakeGuard:
    """假护栏：按名字拦工具、拦输出。"""

    def __init__(self, block_tools=(), block_outputs=(), reason="测试护栏：禁止"):
        self.block_tools = set(block_tools)
        self.block_outputs = set(block_outputs)
        self.reason = reason
        self.before_calls: list[str] = []

    def before_tool(self, name, args):
        self.before_calls.append(name)
        if name in self.block_tools:
            return FakeDecision(allow=False, reason=self.reason)
        return FakeDecision(allow=True)

    def after_output(self, text):
        for secret in self.block_outputs:
            if secret in text:
                return FakeDecision(allow=False, reason="测试护栏：输出包含敏感内容")
        return FakeDecision(allow=True)


class ExplodingGuard:
    """自己就坏的护栏——用来测"护栏挂了怎么办"。"""

    def before_tool(self, name, args):
        raise RuntimeError("护栏坏了")

    def after_output(self, text):
        raise RuntimeError("护栏坏了")


class TestAgentStopPaths(unittest.TestCase):
    """六条主要路径。"""

    def test_normal_completion(self):
        """正常完成：模型给出答案，状态是 completed。"""
        llm = MockLLM(script=[MockLLM.say("答案是 42", in_tokens=10, out_tokens=5)])
        agent, _registry, _state = build_stack(llm)
        result = agent.run("42 是多少")
        self.assertIsInstance(result, AgentResult)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, "答案是 42")
        self.assertEqual(result.total_tokens, 15)
        self.assertTrue(result.stop_reason)

    def test_budget_exhausted_stops(self):
        """预算耗尽：一步就把 token 预算烧穿，下一轮就停。"""
        llm = MockLLM(script=[
            MockLLM.call("echo", {"text": "x"}, in_tokens=5000, out_tokens=100),
            MockLLM.say("不该走到这里", in_tokens=1, out_tokens=1),
        ])
        agent, _registry, _state = build_stack(llm, token_budget=1000, max_steps=10)
        result = agent.run("很贵的活")
        self.assertEqual(result.status, "stopped_budget")
        self.assertIn("token", result.stop_reason)
        self.assertEqual(len(result.steps), 1)

    def test_no_progress_stops(self):
        """无进展：模型一直重复同一个动作。"""
        same = MockLLM.call("echo", {"text": "一样的"}, in_tokens=1, out_tokens=1)
        llm = MockLLM(script=[same] * 8)
        agent, _registry, _state = build_stack(llm, max_steps=10)
        result = agent.run("一直重复的活")
        self.assertEqual(result.status, "stopped_no_progress")
        self.assertIn("无进展", result.stop_reason)

    def test_max_steps_stops(self):
        """步数上限：每次动作都不一样，不触发无进展，只能撞上限。"""
        script = [
            MockLLM.call("echo", {"text": f"第{i}次"}, in_tokens=1, out_tokens=1)
            for i in range(10)
        ]
        llm = MockLLM(script=script)
        agent, _registry, _state = build_stack(llm, max_steps=3)
        result = agent.run("一直有新动作的活")
        self.assertEqual(result.status, "max_steps")
        self.assertEqual(len(result.steps), 3)
        self.assertIn("步数上限", result.stop_reason)

    def test_tool_error_does_not_break_the_loop(self):
        """工具报错后能继续：错误回灌给模型，模型换办法完成。"""
        calls: dict = {}
        llm = MockLLM(script=[
            MockLLM.call("boom", {"text": "会炸"}, in_tokens=3, out_tokens=2),
            MockLLM.say("下游挂了，我改用别的办法", in_tokens=5, out_tokens=5),
        ])
        agent, _registry, _state = build_stack(llm, calls=calls, max_steps=5)
        result = agent.run("调用会失败的工具")
        self.assertEqual(result.status, "completed")
        self.assertEqual(calls.get("boom"), 1, "工具只该被调用一次，不能自动重试")
        self.assertEqual(len(result.steps), 2)
        # 错误必须回灌给模型
        self.assertIn("boom", result.steps[0].observation)

    def test_run_never_raises(self):
        """run() 永不抛异常：模型炸了也只返回 status="error"。"""
        agent, _registry, _state = build_stack(ExplodingLLM(), max_steps=3)
        result = agent.run("会炸的活")
        self.assertEqual(result.status, "error")
        self.assertTrue(result.stop_reason)


class TestStopPriority(unittest.TestCase):
    """停止优先级：完成 → 异常 → 预算 → 无进展 → 步数上限。"""

    def test_completed_beats_max_steps(self):
        """完成 > 步数：最后一步给出答案就算完成。"""
        llm = MockLLM(script=[MockLLM.say("答完了", in_tokens=10, out_tokens=5)])
        agent, _registry, _state = build_stack(llm, max_steps=1)
        result = agent.run("一步答完")
        self.assertEqual(result.status, "completed")

    def test_completed_beats_budget(self):
        """完成 > 预算：给出答案那一步把预算烧穿，仍然算完成。"""
        llm = MockLLM(script=[MockLLM.say("答完了", in_tokens=5000, out_tokens=500)])
        agent, _registry, _state = build_stack(llm, token_budget=1000, max_steps=5)
        result = agent.run("很贵的答案")
        self.assertEqual(result.status, "completed")

    def test_error_beats_max_steps(self):
        """异常 > 步数：第一步就炸了，报异常而不是撞上限。"""
        agent, _registry, _state = build_stack(ExplodingLLM(), max_steps=1)
        result = agent.run("会炸的活")
        self.assertEqual(result.status, "error")

    def test_budget_beats_max_steps(self):
        """预算 > 步数：一步烧穿预算，报预算耗尽。"""
        llm = MockLLM(script=[
            MockLLM.call("echo", {"text": "x"}, in_tokens=100000, out_tokens=1000),
        ])
        agent, _registry, _state = build_stack(llm, token_budget=1000, max_steps=10)
        result = agent.run("很贵的工具调用")
        self.assertEqual(result.status, "stopped_budget")


class TestFeedbackAndState(unittest.TestCase):
    def test_bad_arguments_are_fed_back(self):
        """参数错了要回灌给模型，让它自己改对。"""
        llm = MockLLM(script=[
            MockLLM.call("echo", {"text": 123}, in_tokens=3, out_tokens=2),
            MockLLM.call("echo", {"text": "好的"}, in_tokens=3, out_tokens=2),
            MockLLM.say("改好了", in_tokens=3, out_tokens=3),
        ])
        agent, _registry, _state = build_stack(llm, max_steps=5)
        result = agent.run("回灌测试")
        self.assertEqual(result.status, "completed")
        self.assertIn("echo", result.steps[0].observation)

    def test_every_step_writes_a_checkpoint(self):
        """每步结束写一次检查点。"""
        llm = MockLLM(script=[
            MockLLM.call("echo", {"text": "一"}, in_tokens=1, out_tokens=1),
            MockLLM.say("答完了", in_tokens=1, out_tokens=1),
        ])
        agent, _registry, state = build_stack(llm, max_steps=5)
        agent.run("写检查点")
        self.assertGreaterEqual(state.snapshot().version, 2)
        self.assertEqual(len(state.snapshot().steps), 2)

    def test_state_is_the_single_source_of_truth(self):
        """步骤数在状态和结果里必须一致。"""
        llm = MockLLM(script=[
            MockLLM.call("echo", {"text": "一"}, in_tokens=1, out_tokens=1),
            MockLLM.say("答完了", in_tokens=1, out_tokens=1),
        ])
        agent, _registry, state = build_stack(llm, max_steps=5)
        result = agent.run("对账")
        self.assertEqual(len(state.snapshot().steps), len(result.steps))

    def test_events_are_recorded(self):
        """状态层要留事件溯源记录。"""
        llm = MockLLM(script=[MockLLM.say("答", in_tokens=1, out_tokens=1)])
        agent, _registry, state = build_stack(llm)
        agent.run("记事件")
        self.assertTrue(state.events())
        for event in state.events():
            self.assertIsInstance(event, dict)


class TestBudgetGuards(unittest.TestCase):
    """三种预算共用同一个接口。"""

    def test_all_three_guards_share_one_interface(self):
        guards = [TokenBudgetGuard(100), CostBudgetGuard(1.0), TimeBudgetGuard(60)]
        for guard in guards:
            self.assertEqual(guard.charge(0, 0.0, 0.0), None)
            self.assertIsInstance(guard.exhausted(), bool)
            self.assertIsInstance(guard.reason(), str)

    def test_token_guard_counts_tokens(self):
        guard = TokenBudgetGuard(100)
        guard.charge(60, 0.0, 0.0)
        self.assertFalse(guard.exhausted())
        guard.charge(60, 0.0, 0.0)
        self.assertTrue(guard.exhausted())
        self.assertIn("token", guard.reason())

    def test_cost_guard_counts_money(self):
        guard = CostBudgetGuard(0.5)
        guard.charge(0, 0.3, 0.0)
        self.assertFalse(guard.exhausted())
        guard.charge(0, 0.3, 0.0)
        self.assertTrue(guard.exhausted())
        self.assertIn("美元", guard.reason())

    def test_time_guard_counts_seconds(self):
        guard = TimeBudgetGuard(10)
        guard.charge(0, 0.0, 9.0)
        self.assertFalse(guard.exhausted())
        guard.charge(0, 0.0, 11.0)
        self.assertTrue(guard.exhausted())
        self.assertIn("秒", guard.reason())

    def test_composite_stops_when_any_guard_is_exhausted(self):
        composite = CompositeBudgetGuard([TokenBudgetGuard(100), CostBudgetGuard(1.0)])
        composite.charge(0, 2.0, 0.0)
        self.assertTrue(composite.exhausted())
        self.assertIn("美元", composite.reason())

    def test_builder_reads_the_config(self):
        config = dataclasses.replace(
            Config.from_env_and_args([]), token_budget=1000, cost_budget=0.25, timeout_s=30
        )
        guard = build_budget_guard(config)
        self.assertEqual(len(guard.guards), 3)


class TestGuardrails(unittest.TestCase):
    """护栏的两道关：工具前拦、输出后拦。

    这里用本地假护栏，不依赖 ``guardrails`` 模块——那层是第 9 章的内容。
    """

    def test_blocked_tool_is_not_executed(self):
        """被拦下的工具一次都不能执行，原因要回灌给模型。"""
        calls: dict = {}
        guard = FakeGuard(block_tools={"echo"}, reason="禁止做这件事")
        llm = MockLLM(script=[
            MockLLM.call("echo", {"text": "x"}, in_tokens=3, out_tokens=2),
            MockLLM.say("好的，不做了", in_tokens=3, out_tokens=3),
        ])
        agent, _registry, _state = build_stack(llm, calls=calls, max_steps=5)
        agent.guards = guard
        result = agent.run("调用被禁的工具")
        self.assertEqual(calls.get("echo", 0), 0, "被拦下的工具不能执行")
        self.assertIn("禁止做这件事", result.steps[0].observation)

    def test_blocked_output_does_not_leak(self):
        """输出被拦下时，答案里不能出现被拦的内容。"""
        guard = FakeGuard(block_outputs={"绝密"})
        llm = MockLLM(script=[MockLLM.say("这是绝密内容", in_tokens=3, out_tokens=3)])
        agent, _registry, _state = build_stack(llm, max_steps=3)
        agent.guards = guard
        result = agent.run("模型说了不该说的话")
        self.assertNotIn("绝密", result.answer)
        self.assertEqual(result.status, "error")
        self.assertIn("护栏", result.stop_reason)

    def test_guard_error_before_tool_fails_closed(self):
        """护栏自己坏了：工具**不执行**——有副作用的东西，关不住就别动。"""
        calls: dict = {}
        guard = ExplodingGuard()
        llm = MockLLM(script=[
            MockLLM.call("echo", {"text": "x"}, in_tokens=3, out_tokens=2),
            MockLLM.say("工具不可用，我放弃", in_tokens=3, out_tokens=3),
        ])
        agent, _registry, _state = build_stack(llm, calls=calls, max_steps=5)
        agent.guards = guard
        agent.run("护栏坏了的活")
        self.assertEqual(calls.get("echo", 0), 0, "护栏坏了也不能执行工具")

    def test_guard_error_after_output_does_not_fail_the_run(self):
        """护栏自己坏了：答案照常返回，但异常要留痕。"""
        guard = ExplodingGuard()
        llm = MockLLM(script=[MockLLM.say("答案", in_tokens=3, out_tokens=3)])
        agent, _registry, state = build_stack(llm, max_steps=3)
        agent.guards = guard
        result = agent.run("护栏坏了但答案没问题")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, "答案")
        self.assertTrue(any(e.get("type") == "guard_error" for e in state.events()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
