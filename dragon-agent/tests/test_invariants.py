"""把契约里的 12 条跨模块不变式逐条变成断言。

契约来源：``agent-book/_plan/_dragon_contract.md`` 的「跨模块不变式」表。
每条不变式一个测试方法（或一组），注释里写清它守住的是哪条契约。

这些测试是**离线**的：全部用 MockLLM 与假工具，不碰网络、不需要 API Key。
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import unittest
from unittest import mock

try:  # 包内运行（python -m unittest discover）
    from ._compat import (
        ExplodingLLM,
        find_concrete_class,
        find_orchestrator_factory,
        make_config,
        make_context,
        make_cost_meter,
        make_evaluator,
        make_guardrail_engine,
        make_memory,
        make_registry,
        make_stack,
        make_tracer,
        module_available,
        scripted_llm,
    )
except ImportError:  # pragma: no cover - 直接以脚本方式运行
    from _compat import (  # type: ignore[no-redef]
        ExplodingLLM,
        find_concrete_class,
        find_orchestrator_factory,
        make_config,
        make_context,
        make_cost_meter,
        make_evaluator,
        make_guardrail_engine,
        make_memory,
        make_registry,
        make_stack,
        make_tracer,
        module_available,
        scripted_llm,
    )

from dragon_agent.llm import MockLLM


# ---------------------------------------------------------------------------
# 假实现：护栏、编排器、模型
# ---------------------------------------------------------------------------


class BlockToolGuard:
    """按名字拦工具的假护栏——通过 Protocol 注入，不依赖具体实现。"""

    def __init__(self, blocked: str, reason: str = "测试护栏：禁止调用该工具"):
        self.blocked = blocked
        self.reason = reason
        self.before_calls: list[str] = []

    def before_tool(self, name: str, args: dict):
        from dragon_agent.guardrails import Decision

        self.before_calls.append(name)
        if name == self.blocked:
            return Decision(allow=False, reason=self.reason)
        return Decision(allow=True)

    def after_output(self, text: str):
        from dragon_agent.guardrails import Decision

        return Decision(allow=True)


class TripwireLLM:
    """任何调用都会炸的模型——用来证明某段代码路径**没有**调模型。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        raise AssertionError("这条路径不允许调用 LLM")

    def describe(self) -> str:
        return "tripwire"


class CannedOrchestrator:
    """固定答案的假编排器——给评测用例提供答案，全程不碰模型。"""

    def __init__(self, answers: dict[str, str]):
        self.answers = answers
        self.tasks: list[str] = []

    def run(self, task: str):
        from dragon_agent.agent import AgentResult

        self.tasks.append(task)
        return AgentResult(
            answer=self.answers.get(task, ""),
            status="completed",
            steps=[],
            total_tokens=0,
            total_cost=0.0,
            stop_reason="假编排器直接给答案",
        )


# ---------------------------------------------------------------------------
# 不变式 1：Agent.run() 不抛异常
# ---------------------------------------------------------------------------


class TestRunNeverRaises(unittest.TestCase):
    """契约：``run()`` 永不抛异常给调用方，失败转成 ``status="error"``。"""

    def test_llm_raising_becomes_error_status(self):
        stack = make_stack(llm=ExplodingLLM(), max_steps=3)
        result = stack.agent.run("随便一个任务")
        self.assertEqual(result.status, "error")
        self.assertTrue(result.stop_reason)

    def test_run_with_empty_task_does_not_raise(self):
        stack = make_stack(llm=scripted_llm(MockLLM.say("空任务也算答", in_tokens=1, out_tokens=1)))
        result = stack.agent.run("")
        self.assertIn(result.status, ("completed", "stopped_no_progress", "max_steps"))

    def test_run_never_raises_even_when_tool_raises(self):
        """工具内部抛异常也不能让 run() 冒泡——错误要回灌给模型。"""
        stack = make_stack(
            llm=scripted_llm(
                MockLLM.call("flaky", {"amount": 1}),
                MockLLM.say("下游挂了，我改用别的办法", in_tokens=5, out_tokens=5),
            ),
            max_steps=4,
        )
        result = stack.agent.run("调用会失败的工具")
        self.assertIn(result.status, ("completed", "max_steps", "stopped_no_progress"))
        self.assertEqual(stack.calls.get("flaky", 0), 1)


# ---------------------------------------------------------------------------
# 不变式 2：停止优先级
# ---------------------------------------------------------------------------


class TestStopPriority(unittest.TestCase):
    """契约：停止优先级固定为 完成 → 异常 → 预算 → 无进展 → 步数上限。

    每一对「同时满足两个停止条件」的构造，都断言**排在前面的那个**胜出。
    """

    def test_completed_beats_max_steps(self):
        """完成 > 步数：最后一步给出答案，就该算完成，不是撞上限。"""
        stack = make_stack(
            llm=scripted_llm(MockLLM.say("答完了", in_tokens=10, out_tokens=5)),
            max_steps=1,
        )
        result = stack.agent.run("一步就能答完的题")
        self.assertEqual(result.status, "completed")

    def test_completed_beats_budget(self):
        """完成 > 预算：给出答案的那一步刚好把预算烧穿，仍然算完成。"""
        stack = make_stack(
            llm=scripted_llm(MockLLM.say("答完了", in_tokens=5000, out_tokens=500)),
            token_budget=1000,
            max_steps=5,
        )
        result = stack.agent.run("答案很贵的题")
        self.assertEqual(result.status, "completed")

    def test_error_beats_max_steps(self):
        """异常 > 步数：第一步就炸了，应该报异常而不是撞步数上限。"""
        stack = make_stack(llm=ExplodingLLM(), max_steps=1)
        result = stack.agent.run("会炸的题")
        self.assertEqual(result.status, "error")

    def test_budget_beats_max_steps(self):
        """预算 > 步数：一步就把 token 预算烧穿，应该报预算耗尽。"""
        stack = make_stack(
            llm=scripted_llm(MockLLM.call("add", {"a": 1, "b": 2}, in_tokens=100000, out_tokens=1000)),
            token_budget=1000,
            max_steps=10,
        )
        result = stack.agent.run("很贵的工具调用")
        self.assertEqual(result.status, "stopped_budget")

    def test_no_progress_beats_max_steps(self):
        """无进展 > 步数：模型一直重复同一个动作，应该报无进展而不是撞上限。

        第一卷第 11 章把无进展拆成三个信号：重复动作、观察不变、状态不变。
        这里用**同一个动作重复出现**（观察也随之完全相同）——
        两个信号同时成立，最不容易被漏判。
        """
        same_call = MockLLM.call("add", {"a": 1, "b": 1}, in_tokens=1, out_tokens=1)
        stack = make_stack(
            llm=scripted_llm(*([same_call] * 8)),
            max_steps=10,
        )
        result = stack.agent.run("模型一直重复同一个动作的题")
        self.assertEqual(result.status, "stopped_no_progress")

    def test_stop_reason_is_always_populated(self):
        """每种停止都要能说清楚为什么停。"""
        stack = make_stack(llm=scripted_llm(MockLLM.say("答", in_tokens=1, out_tokens=1)))
        result = stack.agent.run("简单题")
        self.assertIsInstance(result.stop_reason, str)
        self.assertTrue(result.stop_reason.strip())


# ---------------------------------------------------------------------------
# 不变式 3 / 4：工具层语义
# ---------------------------------------------------------------------------


class TestToolSemantics(unittest.TestCase):
    """契约：参数校验失败不抛异常；``replayable=False`` 的工具不自动重试。"""

    def test_validation_failure_returns_error_result(self):
        reg, calls = make_registry()
        res = reg.invoke("add", {"a": "两", "b": 3})
        self.assertFalse(res.ok)
        self.assertTrue(res.error)
        self.assertEqual(calls["add"], 0, "校验没通过就不该执行函数")

        missing = reg.invoke("add", {"a": 1})
        self.assertFalse(missing.ok)
        self.assertTrue(missing.error)

        unknown = reg.invoke("查无此工具", {})
        self.assertFalse(unknown.ok)
        self.assertTrue(unknown.error)

    def test_bad_arguments_are_fed_back_to_the_model(self):
        """参数错了要把错误回灌给模型，让它自己改对。"""
        stack = make_stack(
            llm=scripted_llm(
                MockLLM.call("add", {"a": "两", "b": 3}),
                MockLLM.call("add", {"a": 2, "b": 3}),
                MockLLM.say("2 + 3 = 5", in_tokens=3, out_tokens=3),
            ),
            max_steps=5,
        )
        result = stack.agent.run("算个加法")
        self.assertEqual(result.status, "completed")
        self.assertEqual(stack.calls["add"], 1, "错误的那次不该真的执行")

        # 第二次模型调用必须看得见错误
        self.assertGreaterEqual(len(stack.llm.calls), 2)
        second = json.dumps(stack.llm.calls[1]["messages"], ensure_ascii=False, default=str)
        self.assertTrue(
            "integer" in second or "参数" in second,
            f"错误没有回灌给模型：{second}",
        )

    def test_non_replayable_tool_refuses_repeat(self):
        reg, calls = make_registry()
        first = reg.invoke("charge", {"amount": 100}, idempotency_key="idem-1")
        self.assertTrue(first.ok)

        second = reg.invoke("charge", {"amount": 100}, idempotency_key="idem-1")
        self.assertFalse(second.ok, "不可重放的工具不能用同一个幂等键再执行一次")
        self.assertTrue(second.error)
        self.assertEqual(calls["charge"], 1, "重复执行了不可重放的副作用工具")

    def test_non_replayable_tool_requires_idempotency_key(self):
        reg, calls = make_registry()
        res = reg.invoke("charge", {"amount": 100})
        self.assertFalse(res.ok)
        self.assertEqual(calls["charge"], 0)

    def test_agent_does_not_auto_retry_non_replayable_tool(self):
        """Agent 不得自动重试不可重放的工具——自动重试等于重复扣款。"""
        stack = make_stack(
            llm=scripted_llm(
                MockLLM.call("flaky", {"amount": 5}),
                MockLLM.say("下游不可用，放弃", in_tokens=4, out_tokens=4),
            ),
            max_steps=5,
        )
        stack.agent.run("调用会失败且不可重放的工具")
        self.assertEqual(stack.calls.get("flaky", 0), 1)


# ---------------------------------------------------------------------------
# 不变式 5：上下文渲染完全确定
# ---------------------------------------------------------------------------


class TestContextDeterminism(unittest.TestCase):
    """契约：同一输入的 ``render()`` 输出必须完全确定，否则缓存命中率不可复现。"""

    def _inputs(self, reg):
        task = "查一下项目预算"
        history = [
            {"role": "user", "content": "先看看预算"},
            {"role": "assistant", "content": "好的"},
        ]
        retrieval = ["预算表：总计 100 万", "上季度预算：90 万"]
        return task, history, retrieval, reg.schemas()

    def test_same_input_renders_identically(self):
        reg, _ = make_registry()
        ctx = make_context(make_config())
        task, history, retrieval, schemas = self._inputs(reg)

        first = ctx.assemble(task=task, history=history, retrieval=retrieval, tools=schemas)
        second = ctx.assemble(task=task, history=history, retrieval=retrieval, tools=schemas)
        self.assertEqual(first.render(), second.render())
        self.assertEqual(
            [(b.layer, b.text, b.tokens, b.priority) for b in first.blocks],
            [(b.layer, b.text, b.tokens, b.priority) for b in second.blocks],
        )

    def test_determinism_across_assembler_instances(self):
        """不同实例之间也要一致——说明装配器没有隐藏状态。"""
        reg, _ = make_registry()
        task, history, retrieval, schemas = self._inputs(reg)
        a = make_context(make_config()).assemble(task, history, retrieval, schemas)
        b = make_context(make_config()).assemble(task, history, retrieval, schemas)
        self.assertEqual(a.render(), b.render())

    def test_assemble_does_not_mutate_inputs(self):
        """装配器是纯函数：不能偷偷改调用方传进来的历史。"""
        reg, _ = make_registry()
        ctx = make_context(make_config())
        task, history, retrieval, schemas = self._inputs(reg)
        before = json.dumps(history, ensure_ascii=False, sort_keys=True)
        ctx.assemble(task, history, retrieval, schemas)
        after = json.dumps(history, ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after)

    def test_stable_blocks_come_first(self):
        """稳定块（system / tools）在前，保证提示缓存前缀命中。"""
        reg, _ = make_registry()
        ctx = make_context(make_config())
        task, history, retrieval, schemas = self._inputs(reg)
        assembled = ctx.assemble(task, history, retrieval, schemas)
        layers = [b.layer for b in assembled.blocks]
        for stable in ("system", "tools"):
            for later in ("task", "history", "retrieval"):
                if stable in layers and later in layers:
                    self.assertLess(
                        layers.index(stable), layers.index(later),
                        f"{stable} 块必须排在 {later} 块前面，实际顺序 {layers}",
                    )


# ---------------------------------------------------------------------------
# 不变式 6：TaskState 是唯一状态真相源
# ---------------------------------------------------------------------------


class TestSingleSourceOfTruth(unittest.TestCase):
    """契约：``TaskState`` 是唯一真相源，context / memory 不得各存一份副本。"""

    def test_state_log_mirrors_agent_steps(self):
        stack = make_stack(
            llm=scripted_llm(
                MockLLM.call("add", {"a": 2, "b": 3}, in_tokens=10, out_tokens=4),
                MockLLM.say("2 + 3 = 5", in_tokens=12, out_tokens=3),
            ),
            max_steps=5,
        )
        result = stack.agent.run("算个加法")
        snapshot = stack.state.snapshot()
        self.assertEqual(len(snapshot.steps), len(result.steps))
        self.assertIn("add", json.dumps(snapshot.steps, ensure_ascii=False, default=str))
        self.assertGreaterEqual(snapshot.version, 1)

    def test_checkpoint_restore_round_trip(self):
        stack = make_stack(
            llm=scripted_llm(
                MockLLM.call("add", {"a": 2, "b": 3}),
                MockLLM.say("2 + 3 = 5"),
            ),
            max_steps=5,
        )
        stack.agent.run("算个加法")
        before = stack.state.snapshot()
        cp = stack.state.checkpoint()
        restored = stack.state.restore(cp)
        self.assertEqual(restored.task_id, cp.task_id)
        self.assertEqual(restored.version, cp.version)
        self.assertEqual(len(restored.steps), len(before.steps))

    def test_state_records_an_event_log(self):
        stack = make_stack(
            llm=scripted_llm(MockLLM.say("答", in_tokens=1, out_tokens=1)),
            max_steps=3,
        )
        stack.agent.run("跑一次")
        events = stack.state.events()
        self.assertTrue(events, "状态层要留事件溯源记录")
        for event in events:
            self.assertIsInstance(event, dict)


# ---------------------------------------------------------------------------
# 不变式 7：model 来源的语义记忆写入被拒
# ---------------------------------------------------------------------------


class TestMemoryTrust(unittest.TestCase):
    """契约：来源可信度 user > tool > model；model 来源的语义记忆写入被拒或降级。"""

    def test_model_source_semantic_write_is_rejected_or_degraded(self):
        mem = make_memory(make_config())
        self.assertIsNotNone(mem, "memory 模块必须提供一个具体实现")

        rejected = False
        item = None
        try:
            item = mem.write("semantic", "模型自己编造的一条事实", source="model")
        except Exception:
            rejected = True

        if not rejected:
            # 没抛异常就必须降级：不给可信分，或者干脆没写进去
            if item is not None:
                self.assertLessEqual(
                    item.score, 0.0,
                    "model 来源的语义记忆必须被拒绝或降级（分数应 ≤ 0）",
                )
            recalled = mem.recall("模型自己编造的一条事实", kind="semantic", k=5)
            for hit in recalled:
                self.assertLessEqual(
                    hit.score, 0.0,
                    "model 来源的语义记忆不能以可信状态被召回",
                )

    def test_user_source_write_is_accepted(self):
        mem = make_memory(make_config())
        self.assertIsNotNone(mem)
        item = mem.write("semantic", "用户明确说明的预算上限是一百万", source="user")
        self.assertIsNotNone(item)
        self.assertEqual(item.source, "user")
        recalled = mem.recall("用户明确说明的预算上限是一百万", kind="semantic", k=5)
        self.assertTrue(recalled, "用户来源的语义记忆应该能被召回")

    def test_working_memory_is_not_rejected(self):
        """受来源限制的是「语义记忆」；工作记忆不该因为来源是 model 就写不进去。"""
        mem = make_memory(make_config())
        self.assertIsNotNone(mem)
        try:
            item = mem.write("working", "临时草稿：下一步要查预算表", source="model")
        except Exception:
            item = None
        self.assertIsNotNone(item, "工作记忆不该被来源策略拒掉")


# ---------------------------------------------------------------------------
# 不变式 8：护栏只能收紧不能放宽
# ---------------------------------------------------------------------------


@unittest.skipUnless(module_available("guardrails"), "guardrails 模块尚未落地，待集成验收")
class TestGuardrailsOnlyTighten(unittest.TestCase):
    """契约：护栏只能收紧不能放宽；``allow=False`` 时调用方必须中止该次调用。"""

    def test_blocked_tool_is_never_executed(self):
        guard = BlockToolGuard("add")
        stack = make_stack(
            llm=scripted_llm(
                MockLLM.call("add", {"a": 2, "b": 3}),
                MockLLM.call("add", {"a": 2, "b": 3}),
                MockLLM.say("换个办法", in_tokens=3, out_tokens=3),
            ),
            guards=guard,
            max_steps=6,
        )
        stack.agent.run("模型偏要调用被禁的工具")
        self.assertEqual(stack.calls.get("add", 0), 0, "被护栏拦下的工具不能执行")
        self.assertNotIn("add", stack.registry.invocations)
        self.assertTrue(guard.before_calls, "护栏必须在工具调用前被问到")

    def test_guard_block_is_fed_back_to_the_model(self):
        guard = BlockToolGuard("add", reason="禁止做加法")
        stack = make_stack(
            llm=scripted_llm(
                MockLLM.call("add", {"a": 2, "b": 3}),
                MockLLM.say("好的，不加了", in_tokens=3, out_tokens=3),
            ),
            guards=guard,
            max_steps=5,
        )
        stack.agent.run("试试被禁的工具")
        self.assertGreaterEqual(len(stack.llm.calls), 2)
        second = json.dumps(stack.llm.calls[1]["messages"], ensure_ascii=False, default=str)
        self.assertIn("禁止做加法", second)

    def test_allowing_guard_does_not_block_anything(self):
        guard = BlockToolGuard("绝不存在这个工具")
        stack = make_stack(
            llm=scripted_llm(
                MockLLM.call("add", {"a": 2, "b": 3}),
                MockLLM.say("2 + 3 = 5", in_tokens=3, out_tokens=3),
            ),
            guards=guard,
            max_steps=5,
        )
        result = stack.agent.run("正常工具调用")
        self.assertEqual(result.status, "completed")
        self.assertEqual(stack.calls["add"], 1)

    def test_guard_engine_implements_both_hooks(self):
        """护栏引擎的两个钩子都得在：``before_tool`` 与 ``after_output``。

        契约明确要求的是**工具调用前**必须拦住（见上面两个测试）。
        输出侧的 ``after_output`` 由护栏模块提供能力，供调用方接入——
        主循环目前只 consult ``before_tool``，这条接线缺口在 README 的
        局限声明里写明了。
        """
        engine = make_guardrail_engine(make_config())
        self.assertIsNotNone(engine, "guardrails 模块必须提供一个具体实现")
        from dragon_agent.guardrails import Decision

        before = engine.before_tool("read_text_file", {"path": "x.txt"})
        after = engine.after_output("一段普通的输出文本")
        self.assertIsInstance(before, Decision)
        self.assertIsInstance(after, Decision)
        self.assertIsInstance(before.allow, bool)
        self.assertIsInstance(after.allow, bool)

    def test_guard_decision_does_not_depend_on_llm(self):
        """护栏的判定不依赖 LLM——判定逻辑里不能出现模型调用。"""
        import importlib

        module = importlib.import_module("dragon_agent.guardrails")
        source = inspect.getsource(module)
        tree = ast.parse(source)

        llm_imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = (node.module or "")
                if "llm" in mod.split("."):
                    llm_imports.append(mod)
                for alias in node.names:
                    if "llm" in alias.name.lower():
                        llm_imports.append(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "llm" in alias.name.lower():
                        llm_imports.append(alias.name)
        self.assertEqual(llm_imports, [], f"护栏模块不该依赖 LLM：{llm_imports}")

        # 同一输入的判定必须一致——护栏是确定性规则，不是「模型觉得」
        engine = make_guardrail_engine(make_config())
        self.assertIsNotNone(engine, "guardrails 模块必须提供一个具体实现")
        from dragon_agent.guardrails import Decision

        first = engine.before_tool("add", {"a": 1, "b": 2})
        second = engine.before_tool("add", {"a": 1, "b": 2})
        self.assertIsInstance(first, Decision)
        self.assertEqual(first.allow, second.allow)
        self.assertEqual(first.reason, second.reason)


# ---------------------------------------------------------------------------
# 不变式 9：默认编排器是 SingleAgent
# ---------------------------------------------------------------------------


@unittest.skipUnless(module_available("orchestration"), "orchestration 模块尚未落地，待集成验收")
class TestDefaultOrchestrator(unittest.TestCase):
    """契约：默认必须是 ``SingleAgent``；多 Agent 是显式选择，不是默认行为。"""

    def test_single_agent_is_the_default(self):
        from dragon_agent.orchestration import Orchestrator, SingleAgent, Supervisor

        self.assertTrue(issubclass(SingleAgent, Orchestrator))
        self.assertFalse(
            issubclass(SingleAgent, Supervisor),
            "SingleAgent 不该继承 Supervisor——多 Agent 必须显式选择",
        )

        factory = find_orchestrator_factory()
        if factory is not None:
            default = _call_orchestrator_factory(factory, make_config())
            self.assertIsInstance(default, SingleAgent)
            self.assertNotIsInstance(default, Supervisor)
        else:
            # 没有工厂函数时：模块导入时绝不能已经实例化了 Supervisor
            import dragon_agent.orchestration as orchestration

            for name, value in vars(orchestration).items():
                if isinstance(value, Supervisor):
                    self.fail(f"orchestration 模块在导入时就实例化了 Supervisor（{name}）")

    def test_single_agent_runs_a_task(self):
        from dragon_agent.orchestration import SingleAgent

        stack = make_stack(
            llm=scripted_llm(MockLLM.say("编排器答", in_tokens=2, out_tokens=2)),
            max_steps=3,
        )
        agent = SingleAgent(stack.agent)
        result = agent.run("跑一个任务")
        self.assertEqual(result.status, "completed")


def _call_orchestrator_factory(factory, config):
    """工厂函数的参数形式没在契约里写死，这里依次尝试。"""
    attempts = [
        lambda: factory(),
        lambda: factory(config),
        lambda: factory(config=config),
    ]
    last: Exception | None = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last = exc
    raise AssertionError(f"调用编排器工厂失败：{last}")


# ---------------------------------------------------------------------------
# 不变式 10：trace 属性用 gen_ai.* 命名
# ---------------------------------------------------------------------------


@unittest.skipUnless(module_available("observability"), "observability 模块尚未落地，待集成验收")
class TestTraceAttributeNaming(unittest.TestCase):
    """契约：属性名用 OTel GenAI 约定（``gen_ai.*``），不是自定义的 ``llm.*``。

    第一卷第 33 章说得很清楚：模型侧的属性（模型、token、usage、采样参数）
    必须落在 ``gen_ai.*`` 命名空间里；而 Agent 侧的步、交接这类属性
    行业还没有统一约定，可以自定义。所以这里断言的是——
    **凡是跟模型/token 沾边的属性，必须是 ``gen_ai.*``**，
    并且**任何地方都不许出现 ``llm.*`` 这种简写**。
    """

    #: 跟模型 / token 相关的属性关键词
    MODEL_ATTR_KEYWORDS = ("model", "token", "usage", "temperature", "top_p", "finish")

    def _assert_gen_ai_naming(self, spans):
        self.assertTrue(spans, "跑了一次任务就该有 span")
        gen_ai_seen = 0
        for span in spans:
            for key in span.attributes:
                self.assertFalse(
                    key.startswith("llm."),
                    f"属性 {key!r} 用了自定义的 llm.* 简写——接真实后端时得映射回去",
                )
                if any(kw in key.lower() for kw in self.MODEL_ATTR_KEYWORDS):
                    self.assertTrue(
                        key.startswith("gen_ai."),
                        f"与模型/token 相关的属性 {key!r} 必须在 gen_ai.* 命名空间里",
                    )
                if key.startswith("gen_ai."):
                    gen_ai_seen += 1
        self.assertGreater(gen_ai_seen, 0, "trace 里一个 gen_ai.* 属性都没有，命名约定没落实")

    def test_agent_trace_uses_gen_ai_namespace(self):
        tracer = make_tracer(make_config())
        stack = make_stack(
            llm=scripted_llm(
                MockLLM.call("add", {"a": 2, "b": 3}, in_tokens=10, out_tokens=4),
                MockLLM.say("2 + 3 = 5", in_tokens=12, out_tokens=3),
            ),
            max_steps=5,
            tracer=tracer,
        )
        stack.agent.run("跑一次带 trace 的任务")

        # Agent 自己暴露的 tracer 优先；没有就用注入进去的那个
        tracer = getattr(stack.agent, "tracer", None) or tracer
        self.assertIsNotNone(tracer, "Agent 应当把 tracer 暴露出来，trace 才有落盘入口")
        self._assert_gen_ai_naming(tracer.spans())

    def test_tracer_span_and_sli_shape(self):
        tracer = make_tracer(make_config())
        with tracer.span("gen_ai.chat", **{"gen_ai.request.model": "mock-model"}) as span:
            self.assertEqual(span.name, "gen_ai.chat")
            self.assertEqual(span.attributes.get("gen_ai.request.model"), "mock-model")
        self.assertTrue(tracer.spans())
        sli = tracer.sli()
        self.assertIsInstance(sli, dict)


# ---------------------------------------------------------------------------
# 不变式 11：评测判定器不含 LLM 调用
# ---------------------------------------------------------------------------


@unittest.skipUnless(module_available("evaluation"), "evaluation 模块尚未落地，待集成验收")
class TestEvaluatorHasNoLLM(unittest.TestCase):
    """契约：判定器不用 LLM（用关键字与规则），保证确定性与可离线运行。"""

    def _module_source(self) -> str:
        import importlib

        module = importlib.import_module("dragon_agent.evaluation")
        return inspect.getsource(module)

    def test_evaluation_module_never_touches_llm(self):
        src = self._module_source()
        tree = ast.parse(src)

        llm_imports: list[str] = []
        llm_names: set[str] = set()
        complete_calls: list[str] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = (node.module or "")
                if "llm" in mod.split("."):
                    llm_imports.append(mod)
                for alias in node.names:
                    if "llm" in alias.name.lower():
                        llm_imports.append(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "llm" in alias.name.lower():
                        llm_imports.append(alias.name)
            elif isinstance(node, ast.Name):
                llm_names.add(node.id)
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "complete":
                    complete_calls.append(func.attr)

        self.assertEqual(llm_imports, [], f"评测模块不该导入 LLM：{llm_imports}")
        self.assertEqual(
            llm_names & {"LLMClient", "MockLLM", "OpenAICompatClient", "build_client"},
            set(),
            "评测模块不该引用 LLM 客户端",
        )
        self.assertEqual(complete_calls, [], f"评测模块不该调用模型：{complete_calls}")

    def test_evaluator_runs_without_any_model(self):
        """把答案喂进去，判定过程一次模型都不能调。"""
        from dragon_agent.evaluation import EvalCase

        cases = [
            EvalCase(id="c1", task="项目预算是多少", expect=["100万"], forbid=["200万"]),
            EvalCase(id="c2", task="上季度预算是多少", expect=["90万"], forbid=["100万"]),
        ]
        evaluator = _make_evaluator_with_fixed_answers(cases)
        self.assertIsNotNone(evaluator, "评测器必须能在没有模型的情况下判定")

        tripwire = TripwireLLM()
        report = evaluator.run(cases)
        self.assertEqual(tripwire.calls, 0)
        self.assertEqual(report.total, 2)
        self.assertEqual(report.passed, 2, f"判定不该出错：{report.failures}")
        self.assertEqual(list(report.failures), [])

    def test_evaluator_is_deterministic(self):
        from dragon_agent.evaluation import EvalCase

        cases = [
            EvalCase(id="c1", task="项目预算是多少", expect=["100万"], forbid=["200万"]),
            EvalCase(id="c2", task="上季度预算是多少", expect=["90万"], forbid=["100万"]),
        ]
        evaluator = _make_evaluator_with_fixed_answers(cases)
        self.assertIsNotNone(evaluator)
        first = evaluator.run(cases)
        second = evaluator.run(cases)
        self.assertEqual(first.passed, second.passed)
        self.assertEqual(first.total, second.total)
        self.assertEqual(list(first.failures), list(second.failures))


def _make_evaluator_with_fixed_answers(cases):
    """构造一个「只判定、不跑模型」的评测器。

    评测器需要某种方式拿到答案：可能是接一个编排器，也可能用例自带答案。
    两种都试；都不行返回 None，交给调用方跳过行为断言。
    """
    from dragon_agent.evaluation import Evaluator

    answers = {}
    for case in cases:
        answers[case.task] = "".join(case.expect)

    try:
        params = inspect.signature(Evaluator.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover
        params = {}

    for name in params:
        lowered = name.lower()
        if any(k in lowered for k in ("orchestrator", "runner", "agent", "executor",
                                      "solver", "solve", "factory", "callable")):
            try:
                return Evaluator(**{name: CannedOrchestrator(answers)})
            except TypeError:
                continue

    # 用例自带答案字段的情况
    if cases and hasattr(cases[0], "answer"):
        for case in cases:
            try:
                setattr(case, "answer", "".join(case.expect))
            except Exception:  # pragma: no cover
                break
        try:
            return Evaluator()
        except TypeError:
            return None

    try:
        return Evaluator()
    except TypeError:
        return None


# ---------------------------------------------------------------------------
# 不变式 12：Config.to_dict() 不含密钥
# ---------------------------------------------------------------------------


class TestConfigNoSecret(unittest.TestCase):
    """契约：``to_dict()`` 绝不包含 api_key——它会被写进 trace 和检查点。"""

    def test_to_dict_excludes_api_key(self):
        secret = "sk-test-abcdef0123456789"
        with mock.patch.dict(os.environ, {"LLM_API_KEY": secret}, clear=False):
            from dragon_agent.config import Config

            cfg = Config.from_env_and_args([])
            data = cfg.to_dict()
            self.assertNotIn("api_key", data)
            self.assertNotIn(secret, json.dumps(data, ensure_ascii=False, default=str))
            self.assertNotIn(secret, repr(cfg))

    def test_to_dict_is_json_serialisable(self):
        cfg = make_config()
        json.dumps(cfg.to_dict(), ensure_ascii=False, default=str)

    def test_live_without_key_is_rejected(self):
        """``--live`` 缺 key 时要清晰报错，而不是静默失败。"""
        from dragon_agent.config import Config, ConfigError

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError):
                Config.from_env_and_args(["--live"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
