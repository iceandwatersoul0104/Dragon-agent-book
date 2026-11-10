"""回放与恢复的单元测试：录、重放、对比、副作用去重。

全部离线：用 ``llm.py`` 里的 ``MockLLM`` 驱动，不联网、不需要 API Key。
"""
from __future__ import annotations

import dataclasses
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.agent import Agent
from dragon_agent.config import Config
from dragon_agent.context import ContextAssembler
from dragon_agent.llm import MockLLM
from dragon_agent.state import (
    ExecutedActions,
    RecordingLLM,
    RecordingRegistry,
    RunRecord,
    StateStore,
    compare_steps,
    record_run,
    replay,
    restore_executed_actions,
)
from dragon_agent.tools import ToolRegistry, ToolSpec

TASK = "读一下预算表"


def make_tools(answers: dict | None = None) -> ToolRegistry:
    """确定性工具。``answers`` 可以改掉返回值，用来制造差异。"""
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
    config = dataclasses.replace(Config.from_env_and_args([]), max_steps=6)
    registry = registry if registry is not None else make_tools()
    if executed is not None:
        registry = RecordingRegistry(registry, executed)
    context = ContextAssembler(config)
    state = StateStore(config, task_id="test-task")
    return Agent(config, llm, registry, context, state)


def script():
    return [
        MockLLM.call("read_budget", {"year": "2026"}, in_tokens=120, out_tokens=18),
        MockLLM.say("总额 100 万。", in_tokens=180, out_tokens=22),
    ]


class TestRecording(unittest.TestCase):
    def test_recording_llm_records_every_response(self):
        """录下模型的每次响应。"""
        recorder = RecordingLLM(MockLLM(script=script()))
        recorder.complete([{"role": "user", "content": "x"}])
        recorder.complete([{"role": "user", "content": "x"}])
        self.assertEqual(len(recorder.responses), 2)

    def test_record_run_captures_responses_and_steps(self):
        """一次运行录下：任务 + 模型响应 + 每一步 + 最终结果。"""
        record = record_run(build_agent, TASK, MockLLM(script=script()))
        self.assertEqual(record.task, TASK)
        self.assertEqual(len(record.responses), 2)
        self.assertEqual(len(record.steps), 2)
        self.assertEqual(record.final["status"], "completed")
        self.assertEqual(record.steps[0]["action"], "read_budget")

    def test_run_record_round_trip(self):
        """记录落盘再读回来，内容不变。"""
        record = record_run(build_agent, TASK, MockLLM(script=script()))
        restored = RunRecord.from_dict(record.to_dict())
        self.assertEqual(restored.task, record.task)
        self.assertEqual(len(restored.responses), len(record.responses))
        self.assertEqual(restored.steps, record.steps)
        self.assertEqual(restored.responses[0].tool_calls[0].name, "read_budget")


class TestReplay(unittest.TestCase):
    def test_replay_is_identical(self):
        """同样的响应 + 同样的工具 = 同样的结果。"""
        record = record_run(build_agent, TASK, MockLLM(script=script()))
        result = replay(build_agent, record,
                        lambda responses: MockLLM(script=list(responses)))
        self.assertTrue(result.identical, result.differences)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.differences, [])

    def test_replay_detects_a_changed_tool(self):
        """工具行为变了，回放要报出差异——这才是回放的用处。"""
        record = record_run(build_agent, TASK, MockLLM(script=script()))
        changed = make_tools(answers={"read_budget": {"year": "2026", "total": 999_999}})
        result = replay(
            lambda llm: build_agent(llm, registry=changed),
            record,
            lambda responses: MockLLM(script=list(responses)),
        )
        self.assertFalse(result.identical)
        self.assertTrue(any("observation" in d for d in result.differences))
        self.assertTrue(any("第 1 步" in d for d in result.differences))

    def test_compare_steps_reports_step_count_mismatch(self):
        """步数不同要报出来。"""
        differences = compare_steps(
            [{"action": "a"}, {"action": "b"}],
            [{"action": "a"}],
        )
        self.assertTrue(any("步数不同" in d for d in differences))

    def test_compare_steps_reports_field_mismatch(self):
        """字段不同要指明是哪一步的哪个字段。"""
        differences = compare_steps(
            [{"action": "a", "observation": "x"}],
            [{"action": "a", "observation": "y"}],
        )
        self.assertTrue(any("observation" in d for d in differences))


class TestExecutedActions(unittest.TestCase):
    def test_record_and_lookup(self):
        executed = ExecutedActions()
        executed.record("k1", {"tool": "charge", "output": {"charged": 100}})
        self.assertTrue(executed.has("k1"))
        self.assertFalse(executed.has("k2"))
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed.get("k1")["tool"], "charge")

    def test_round_trip(self):
        executed = ExecutedActions()
        executed.record("k1", {"tool": "charge", "output": 1})
        restored = ExecutedActions.from_dict(executed.to_dict())
        self.assertTrue(restored.has("k1"))
        self.assertEqual(restored.get("k1")["output"], 1)

    def test_recording_registry_records_only_successful_calls(self):
        """失败的调用没造成影响，不登记。"""
        executed = ExecutedActions()
        registry = RecordingRegistry(make_tools(), executed)
        registry.invoke("charge", {"amount": 1}, idempotency_key="k1")
        registry.invoke("charge", {"amount": 1})  # 不可重放且没给键 → 被拒绝
        self.assertEqual(len(executed), 1)

    def test_restored_actions_block_repeated_side_effects(self):
        """崩溃重启后装回登记，不可逆的工具不会被执行第二次。"""
        executed = ExecutedActions()
        first_registry = RecordingRegistry(make_tools(), executed)
        first = first_registry.invoke("charge", {"amount": 100}, idempotency_key="order-42")
        self.assertTrue(first.ok)

        # 模拟进程重启：工具层的进程内缓存没了，登记还在
        restarted = make_tools()
        restored = restore_executed_actions(restarted, executed)
        self.assertEqual(restored, 1)
        second = restarted.invoke("charge", {"amount": 100}, idempotency_key="order-42")
        self.assertFalse(second.ok)
        self.assertIn("不可重放", second.error or "")
        self.assertEqual(restarted.calls["charge"], 0, "扣款函数不该被再执行一次")


class TestStateStillWorks(unittest.TestCase):
    """扩展 state.py 之后，原有行为不能变。"""

    def test_checkpoint_and_restore_still_work(self):
        store = StateStore(task_id="t1")
        store.record_step({"index": 1, "action": "add"})
        cp = store.checkpoint()
        restored = store.restore(cp)
        self.assertEqual(restored.task_id, cp.task_id)
        self.assertEqual(restored.version, cp.version)
        self.assertEqual(len(restored.steps), 1)

    def test_events_are_still_recorded(self):
        store = StateStore(task_id="t1")
        store.record({"type": "x"})
        self.assertTrue(store.events())


if __name__ == "__main__":
    unittest.main(verbosity=2)
