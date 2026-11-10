"""工具层的契约测试：注册、校验、回灌、幂等与副作用、并行对齐。

这个文件守的是 ``tools.py`` docstring 里写死的几条：

- 参数校验失败**不抛异常**，返回 ``ToolResult(ok=False)``，由上层回灌；
- 工具内部异常**也不抛异常**，同样变成 ``ToolResult(ok=False)``；
- ``replayable=False`` 的工具，幂等键用过之后必须**拒绝重复执行**；
- 校验失败的调用**不能真的执行**工具函数。

全部离线：不联网、不需要 API Key、不写用户目录。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.llm import ToolCall
from dragon_agent.tool_runner import (
    FEEDBACK_ENVIRONMENT,
    FEEDBACK_FIELD,
    FEEDBACK_POLICY,
    classify_feedback,
    invoke_many,
    render_feedback,
)
from dragon_agent.tool_schema import schema_for
from dragon_agent.tools import ToolError, ToolRegistry, ToolSpec, default_registry


class _Counter:
    """记录被调用了几次、参数是什么。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"called": len(self.calls)}


class TestRegistration(unittest.TestCase):
    def test_schemas_expose_name_description_and_parameters(self):
        """模型看到的就是这三样：名字、描述、参数 schema。"""
        reg = default_registry()
        schemas = reg.schemas()
        self.assertEqual(len(schemas), 4)
        for schema in schemas:
            self.assertEqual(sorted(schema), ["description", "name", "parameters"])
            self.assertTrue(schema["description"])
            self.assertEqual(schema["parameters"]["type"], "object")

    def test_duplicate_registration_is_fatal(self):
        """重复注册是编程错误，该在注册时就炸出来。"""
        reg = ToolRegistry()

        @reg.tool(name="x", description="x", parameters={"type": "object", "properties": {}})
        def first():
            return 1

        with self.assertRaises(ToolError):
            reg.register(ToolSpec(name="x", description="x"), first)

    def test_unknown_tool_lookup_raises(self):
        """按名字取一个没注册的工具，是编程错误。"""
        with self.assertRaises(ToolError):
            ToolRegistry().get("nope")

    def test_invocations_record_execution_order(self):
        """执行记录能用来断言"到底执行了什么"。"""
        reg = default_registry()
        reg.invoke("add", {"a": 1, "b": 2})
        reg.invoke("add", {"a": 3, "b": 4})
        self.assertEqual(reg.invocations, ["add", "add"])


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.reg = default_registry()

    def test_missing_required_argument(self):
        """缺必填参数：不抛异常，返回失败结果。"""
        result = self.reg.invoke("add", {"a": 1})
        self.assertFalse(result.ok)
        self.assertIn("缺少必填参数", result.error or "")
        self.assertIn("b", result.error or "")

    def test_wrong_type(self):
        """类型不对：回灌给模型。"""
        result = self.reg.invoke("add", {"a": "x", "b": 2})
        self.assertFalse(result.ok)
        self.assertIn("integer", result.error or "")

    def test_boolean_is_not_an_integer(self):
        """Python 里 bool 是 int 的子类，但模型写 true 不等于写了 1。"""
        result = self.reg.invoke("add", {"a": True, "b": 2})
        self.assertFalse(result.ok)
        self.assertIn("boolean", result.error or "")

    def test_enum_violation(self):
        """枚举越界要拦下来。"""
        reg = ToolRegistry()

        @reg.tool(
            name="pick",
            description="选一个模式",
            parameters={
                "type": "object",
                "properties": {"mode": {"type": "string", "enum": ["fast", "slow"]}},
                "required": ["mode"],
            },
        )
        def pick(mode: str) -> str:
            return mode

        result = reg.invoke("pick", {"mode": "turbo"})
        self.assertFalse(result.ok)
        self.assertIn("只能取", result.error or "")
        self.assertIn("fast", result.error or "")

    def test_extra_arguments_are_ignored_by_validation_but_fail_on_call(self):
        """多余参数在校验层被忽略，执行时因为函数签名不匹配而失败。

        两种表现都不是抛异常——都是回灌给模型。
        """
        result = self.reg.invoke("add", {"a": 1, "b": 2, "extra": "?"})
        self.assertFalse(result.ok)
        self.assertIn("调用参数不匹配", result.error or "")

    def test_failed_validation_does_not_execute_the_tool(self):
        """校验失败的工具**不能真的执行**——这关系到副作用。"""
        counter = _Counter()
        reg = ToolRegistry()

        @reg.tool(
            name="danger",
            description="有副作用",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "required": ["x"],
            },
            side_effect=True,
        )
        def danger(x: int):
            return counter(x=x)

        reg.invoke("danger", {})
        self.assertEqual(counter.calls, [])


class TestFailureIsAlwaysAResult(unittest.TestCase):
    def test_unknown_tool_returns_failed_result(self):
        """未知工具不是异常，是一条失败结果。"""
        result = ToolRegistry().invoke("nope", {})
        self.assertFalse(result.ok)
        self.assertIn("未知工具", result.error or "")

    def test_internal_exception_returns_failed_result(self):
        """工具内部抛异常，也要变成失败结果。"""
        reg = ToolRegistry()

        @reg.tool(name="boom", description="会炸",
                  parameters={"type": "object", "properties": {}})
        def boom():
            raise RuntimeError("炸了")

        result = reg.invoke("boom", {})
        self.assertFalse(result.ok)
        self.assertIn("RuntimeError", result.error or "")
        self.assertIn("炸了", result.error or "")

    def test_result_render_is_a_string(self):
        """回灌文本必须始终是字符串。"""
        reg = default_registry()
        ok = reg.invoke("add", {"a": 1, "b": 2})
        bad = reg.invoke("add", {"a": 1})
        self.assertIsInstance(ok.render(), str)
        self.assertIsInstance(bad.render(), str)


class TestIdempotencyAndSideEffects(unittest.TestCase):
    def setUp(self):
        self.reg = default_registry()

    def test_replayable_tool_hits_the_idempotency_cache(self):
        """可重放的副作用工具：同一个幂等键不再执行，返回第一次的结果。"""
        first = self.reg.invoke("send_report", {"content": "x"}, idempotency_key="k")
        second = self.reg.invoke("send_report", {"content": "x"}, idempotency_key="k")
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertFalse(first.idempotent_hit)
        self.assertTrue(second.idempotent_hit)
        self.assertEqual(second.output, first.output)

    def test_non_replayable_tool_refuses_a_used_idempotency_key(self):
        """不可重放的副作用工具：幂等键用过之后必须拒绝执行。"""
        self.reg.invoke("submit_order", {"sku": "S", "qty": 1}, idempotency_key="k")
        before = len(self.reg.invocations)
        second = self.reg.invoke("submit_order", {"sku": "S", "qty": 1}, idempotency_key="k")
        self.assertFalse(second.ok)
        self.assertIn("不可重放", second.error or "")
        self.assertEqual(len(self.reg.invocations), before)  # 没有真的执行第二次

    def test_non_replayable_tool_requires_an_idempotency_key(self):
        """不可重放的副作用工具，没有幂等键就不许执行。"""
        result = self.reg.invoke("submit_order", {"sku": "S", "qty": 1})
        self.assertFalse(result.ok)
        self.assertIn("必须提供幂等键", result.error or "")

    def test_read_only_tool_needs_no_idempotency_key(self):
        """只读工具不需要幂等键。"""
        result = self.reg.invoke("add", {"a": 1, "b": 2})
        self.assertTrue(result.ok)

    def test_different_keys_execute_twice(self):
        """不同的幂等键是两次不同的操作。"""
        self.reg.invoke("send_report", {"content": "x"}, idempotency_key="k1")
        self.reg.invoke("send_report", {"content": "x"}, idempotency_key="k2")
        self.assertEqual(self.reg.invocations.count("send_report"), 2)


class TestFeedbackShapes(unittest.TestCase):
    def test_three_shapes_are_distinguished(self):
        """字段级 / 语义级 / 环境级要分得开。"""
        reg = default_registry()
        field = reg.invoke("add", {"a": 1})
        policy = reg.invoke("submit_order", {"sku": "S", "qty": 1})
        environment = reg.invoke("read_text_file", {"path": "不存在的.txt"})
        self.assertEqual(classify_feedback(field.error), FEEDBACK_FIELD)
        self.assertEqual(classify_feedback(policy.error), FEEDBACK_POLICY)
        self.assertEqual(classify_feedback(environment.error), FEEDBACK_ENVIRONMENT)

    def test_rendered_feedback_carries_the_shape(self):
        """回灌文本里要能看出是哪一种。"""
        reg = default_registry()
        text = render_feedback(reg.invoke("add", {"a": 1}))
        self.assertIn(FEEDBACK_FIELD, text)
        self.assertIn("修正参数", text)

    def test_success_renders_plain_output(self):
        """成功时不加任何前缀。"""
        reg = default_registry()
        self.assertEqual(render_feedback(reg.invoke("add", {"a": 1, "b": 2})), "3")


class TestBatchInvocation(unittest.TestCase):
    def setUp(self):
        self.reg = default_registry()

    def test_results_align_with_calls(self):
        """results[i] 永远对应 calls[i]。"""
        calls = [
            ("add", {"a": 1, "b": 2}),
            ("read_text_file", {"path": "不存在的.txt"}),
            ("add", {"a": 10, "b": 20}),
        ]
        results = invoke_many(self.reg, calls)
        self.assertEqual(len(results), len(calls))
        self.assertTrue(results[0].ok)
        self.assertFalse(results[1].ok)
        self.assertTrue(results[2].ok)
        self.assertEqual(results[0].output, 3)
        self.assertEqual(results[2].output, 30)

    def test_one_failure_does_not_affect_the_others(self):
        """失败隔离：一个失败不影响其他调用。"""
        calls = [("add", {"a": 1, "b": 2}), ("add", {"a": "x", "b": 2}), ("add", {"a": 2, "b": 3})]
        results = invoke_many(self.reg, calls)
        self.assertEqual([r.ok for r in results], [True, False, True])
        self.assertEqual(results[2].output, 5)

    def test_parallel_execution_keeps_the_same_order(self):
        """并行也必须对齐——顺序是对齐契约的一部分。"""
        calls = [("add", {"a": i, "b": 1}) for i in range(6)]
        results = invoke_many(self.reg, calls, parallel=True)
        self.assertEqual([r.output for r in results], [1, 2, 3, 4, 5, 6])

    def test_tool_call_objects_are_accepted(self):
        """也可以直接传 llm.ToolCall。"""
        results = invoke_many(self.reg, [ToolCall(name="add", arguments={"a": 1, "b": 2})])
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].output, 3)

    def test_batch_with_prefix_gives_each_call_its_own_key(self):
        """带前缀的批次里，每个调用有自己的幂等键。"""
        results = invoke_many(
            self.reg,
            [("submit_order", {"sku": "A", "qty": 1}), ("submit_order", {"sku": "B", "qty": 1})],
            idempotency_prefix="batch-1",
        )
        self.assertTrue(results[0].ok)
        self.assertTrue(results[1].ok)


class TestSchemaGeneration(unittest.TestCase):
    def test_schema_is_derived_from_annotations(self):
        """从类型注解生成的 schema：类型、必填、枚举、数组都要对上。"""
        def demo(a: int, b: str = "x", c: bool = False, d: list[int] | None = None):
            return a

        schema = schema_for(demo)
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["properties"]["a"]["type"], "integer")
        self.assertEqual(schema["properties"]["b"]["type"], "string")
        self.assertEqual(schema["properties"]["c"]["type"], "boolean")
        self.assertEqual(schema["properties"]["d"]["type"], "array")
        self.assertEqual(schema["properties"]["d"]["items"]["type"], "integer")
        self.assertEqual(schema["required"], ["a"])

    def test_generated_schema_passes_the_registry_validator(self):
        """生成的 schema 要能被 tools.py 的校验器直接用。"""

        def demo(a: int, b: str = "x") -> int:
            return a

        reg = ToolRegistry()

        @reg.tool(name="demo", description="演示", parameters=schema_for(demo))
        def tool_demo(a: int, b: str = "x") -> int:
            return a

        result = reg.invoke("demo", {"a": 1, "b": "y"})
        self.assertTrue(result.ok)
        bad = reg.invoke("demo", {})
        self.assertFalse(bad.ok)
        wrong_type = reg.invoke("demo", {"a": "x"})
        self.assertFalse(wrong_type.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
