"""可观测性模块的契约测试：span 树、命名约定、脱敏、采样、SLI、成本账。

这个文件守的是 ``observability.py`` docstring 里写死的几条：

- 属性名用 OTel GenAI 约定（``gen_ai.*``），**不用** ``llm.*`` 简写；
- span 有父子关系，嵌套时子 span 挂在父 span 下；
- 脱敏在**采集端**：敏感键整值替换、长文本截断、工具参数只记摘要；
- 采样按**任务粒度**，SLI 从**全量计数器**算，不从采样 span 算；
- 成本账能按维度拆分，且同一个账本不会记两遍钱。

全部离线：不联网、不需要 API Key、不写用户目录。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.config import Config
from dragon_agent.llm import LLMResponse, MockLLM, ToolCall, Usage
from dragon_agent.observability import (
    ERROR_TYPES,
    GEN_AI_PREFIX,
    AlwaysSampler,
    CostMeter,
    ErrorOrSlowSampler,
    JsonlExporter,
    RatioSampler,
    Span,
    Tracer,
    classify_error,
    normalize_attribute_key,
    summarize_arguments,
)
from dragon_agent.tools import ToolRegistry, ToolSpec


class _Config:
    """最小配置替身：只要单价那几个字段。"""

    def __init__(self, price_in: float = 0.0, price_out: float = 0.0) -> None:
        self.price_in_per_mtok = price_in
        self.price_out_per_mtok = price_out
        self.model = "mock-model"


# --------------------------------------------------------------------------- span 树


class TestSpanTree(unittest.TestCase):
    def test_span_is_a_context_manager(self):
        """``tracer.span()`` 返回的对象本身就是上下文管理器。"""
        tracer = Tracer(_Config())
        with tracer.span("gen_ai.chat", **{"gen_ai.request.model": "m"}) as span:
            self.assertEqual(span.name, "gen_ai.chat")
            self.assertEqual(span.attributes["gen_ai.request.model"], "m")
        self.assertTrue(tracer.spans())

    def test_nested_spans_share_trace_and_link_parent(self):
        """嵌套的 span 共用 trace_id，子 span 的 parent 指向父 span。"""
        tracer = Tracer(_Config())
        with tracer.span("invoke_agent") as outer:
            with tracer.span("gen_ai.chat") as inner:
                pass
        spans = {span.name: span for span in tracer.spans()}
        self.assertEqual(spans["gen_ai.chat"].trace_id, spans["invoke_agent"].trace_id)
        self.assertEqual(spans["gen_ai.chat"].parent, spans["invoke_agent"].span_id)
        self.assertIsNone(spans["invoke_agent"].parent)

    def test_separate_roots_have_separate_traces(self):
        tracer = Tracer(_Config())
        with tracer.span("a"):
            pass
        with tracer.span("b"):
            pass
        spans = tracer.spans()
        self.assertNotEqual(spans[0].trace_id, spans[1].trace_id)

    def test_span_records_duration_and_status(self):
        tracer = Tracer(_Config())
        with tracer.span("x") as span:
            pass
        self.assertGreater(span.end, span.start)
        self.assertGreaterEqual(span.duration_ms, 0.0)
        self.assertEqual(span.status, "ok")

    def test_exception_marks_span_error_and_propagates(self):
        """异常要被记进 span，但**照常往外抛**——观测层不吞异常。"""
        tracer = Tracer(_Config())
        with self.assertRaises(ValueError):
            with tracer.span("x") as span:
                raise ValueError("炸了")
        self.assertEqual(span.status, "error")
        self.assertEqual(span.error, "invalid_arguments")
        self.assertEqual(span.attributes["error.type"], "invalid_arguments")


# --------------------------------------------------------------------------- 命名约定


class TestGenAiNaming(unittest.TestCase):
    def test_model_attrs_are_normalized_into_gen_ai_namespace(self):
        """写错的键名自动归位，导出层永远拿得到标准属性。"""
        self.assertEqual(normalize_attribute_key("llm.model"),
                         "gen_ai.llm.model")
        self.assertEqual(normalize_attribute_key("input_tokens"),
                         "gen_ai.input_tokens")
        self.assertEqual(normalize_attribute_key("gen_ai.request.model"),
                         "gen_ai.request.model")
        self.assertEqual(normalize_attribute_key("tool.name"), "tool.name")

    def test_no_llm_shortcut_in_recorded_attributes(self):
        tracer = Tracer(_Config())
        with tracer.span("x", **{"llm.model": "m", "tool.name": "add"}):
            pass
        for span in tracer.spans():
            for key in span.attributes:
                self.assertFalse(key.startswith("llm."))
        self.assertIn("gen_ai.llm.model", tracer.spans()[0].attributes)
        self.assertIn("tool.name", tracer.spans()[0].attributes)

    def test_renamed_attributes_are_reported(self):
        """改名要能被看见——否则没人会去对齐命名约定。"""
        tracer = Tracer(_Config())
        with tracer.span("x", **{"llm.model": "m"}):
            pass
        renamed = tracer.sli()["renamed_attributes"]
        self.assertEqual(renamed.get("llm.model"), "gen_ai.llm.model")

    def test_every_model_related_key_is_gen_ai(self):
        """凡是跟模型/token 沾边的属性，都必须落在 gen_ai.* 下。"""
        tracer = Tracer(_Config())
        with tracer.span("x", **{"gen_ai.request.model": "m",
                                 "gen_ai.usage.input_tokens": 3,
                                 "gen_ai.response.finish_reasons": "stop"}):
            pass
        keywords = ("model", "token", "usage", "temperature", "top_p", "finish")
        for span in tracer.spans():
            for key in span.attributes:
                if any(word in key.lower() for word in keywords):
                    self.assertTrue(key.startswith(GEN_AI_PREFIX))

    def test_error_types_are_a_fixed_enum(self):
        """错误类型必须是固定枚举，不能是自由文本。"""
        self.assertIn("tool_failure", ERROR_TYPES)
        self.assertIn("timeout", ERROR_TYPES)
        self.assertEqual(classify_error(TimeoutError("x")), "timeout")
        self.assertEqual(classify_error(ValueError("x")), "invalid_arguments")


# --------------------------------------------------------------------------- 脱敏


class TestRedaction(unittest.TestCase):
    def test_sensitive_keys_are_masked(self):
        tracer = Tracer(_Config())
        with tracer.span("x", **{"tool.api_key": "sk-abcdefghijklmnop"}):
            pass
        value = tracer.spans()[0].attributes["tool.api_key"]
        self.assertNotIn("sk-abcdefghijklmnop", str(value))

    def test_secret_shaped_text_is_masked(self):
        tracer = Tracer(_Config())
        with tracer.span("x", **{"tool.note": "用 sk-abcdefghijklmnop 就行"}):
            pass
        value = tracer.spans()[0].attributes["tool.note"]
        self.assertNotIn("sk-abcdefghijklmnop", str(value))

    def test_long_values_are_truncated(self):
        tracer = Tracer(_Config())
        with tracer.span("x", **{"tool.note": "长" * 1000}):
            pass
        value = tracer.spans()[0].attributes["tool.note"]
        self.assertLess(len(value), 400)
        self.assertIn("截断", value)

    def test_tool_arguments_are_summarized_not_stored(self):
        """工具参数只记摘要——参数里常有订单号、手机号。"""
        summary = summarize_arguments({"phone": "13800000000", "amount": 100})
        self.assertNotIn("13800000000", summary)
        self.assertIn("amount", summary)
        self.assertIn("phone", summary)

    def test_prompt_text_is_not_recorded_by_default(self):
        """默认路径不记 prompt 全文——那是钱和合规风险。"""
        tracer = Tracer(_Config())
        with tracer.span("gen_ai.chat", **{"gen_ai.request.model": "m"}):
            pass
        for span in tracer.spans():
            for key in span.attributes:
                self.assertNotIn("prompt", key)
                self.assertNotIn("completion", key)


# --------------------------------------------------------------------------- 采样


class TestSampling(unittest.TestCase):
    def test_ratio_sampler_is_deterministic(self):
        """同一个 trace_id 反复判，结论必须一致——否则没法复现。"""
        sampler = RatioSampler(0.5)
        trace_id = "abc123"
        first = sampler.should_sample(trace_id)
        for _ in range(5):
            self.assertEqual(sampler.should_sample(trace_id), first)

    def test_ratio_sampler_endpoints(self):
        self.assertTrue(RatioSampler(1.0).should_sample("x"))
        self.assertFalse(RatioSampler(0.0).should_sample("x"))

    def test_error_or_slow_sampler_keeps_slow_tasks(self):
        """慢任务必留——长尾问题恰恰藏在低频路径里。"""
        sampler = ErrorOrSlowSampler(ratio=0.0, slow_ms=1000.0)
        self.assertFalse(sampler.should_sample("x"))
        self.assertTrue(sampler.keep_because_slow(1500.0))
        self.assertFalse(sampler.keep_because_slow(10.0))

    def test_metrics_are_full_volume_even_when_sampled_out(self):
        """采样丢的是 trace，**不是指标**——成功率不能有偏。"""
        tracer = Tracer(_Config(), sampler=RatioSampler(0.0))
        tracer.record_task(type("R", (), {"status": "completed", "steps": [1],
                                          "total_tokens": 10, "total_cost": 0.1,
                                          "stop_reason": "ok"})())
        tracer.record_tool_call("add", True)
        tracer.record_tool_call("add", False)
        sli = tracer.sli()
        self.assertEqual(sli["task_count"], 1)
        self.assertEqual(sli["tool_calls"], 2)
        self.assertEqual(sli["tool_error_rate"], 0.5)
        self.assertEqual(sli["task_success_rate"], 1.0)
        self.assertEqual(sli["span_count"], 0)


# --------------------------------------------------------------------------- SLI


class TestSLI(unittest.TestCase):
    def _task(self, status: str = "completed", steps: int = 2,
              cost: float = 0.1):
        return type("R", (), {
            "status": status,
            "steps": [1] * steps,
            "total_tokens": 10,
            "total_cost": cost,
            "stop_reason": "ok",
        })()

    def test_success_rate_uses_task_level_judgement(self):
        """成功判定发生在任务层，不拿"没抛异常"顶替。"""
        tracer = Tracer(_Config())
        tracer.record_task(self._task("completed"))
        tracer.record_task(self._task("max_steps"))
        tracer.record_task(self._task("stopped_no_progress"))
        sli = tracer.sli()
        self.assertEqual(sli["task_count"], 3)
        self.assertEqual(sli["success_count"], 1)
        self.assertAlmostEqual(sli["task_success_rate"], 1 / 3, places=6)

    def test_no_progress_rate(self):
        tracer = Tracer(_Config())
        tracer.record_task(self._task("stopped_no_progress"))
        tracer.record_task(self._task("completed"))
        sli = tracer.sli()
        self.assertEqual(sli["no_progress_count"], 1)
        self.assertAlmostEqual(sli["no_progress_rate"], 0.5)

    def test_tool_error_rate_buckets_by_tool_name(self):
        """整体错误率会掩盖某个具体工具的退化。"""
        tracer = Tracer(_Config())
        tracer.record_tool_call("add", True)
        tracer.record_tool_call("read", False)
        tracer.record_tool_call("read", False)
        tracer.record_tool_call("add", True)
        sli = tracer.sli()
        self.assertAlmostEqual(sli["tool_error_rate"], 0.5)
        self.assertEqual(sli["tool_error_rate_by_tool"], {"read": 2})

    def test_cost_per_success_task(self):
        """每次成功成本 = 总成本 / 成功任务数。

        成本以**成本账**为准（它才是真正记过钱的那本账），不是任务结果
        里回传的总数——两本账必须只有一个权威。
        """
        tracer = Tracer(_Config(price_in=1.0, price_out=0.0))
        tracer.meter.charge(Usage(input_tokens=1_000_000), "m", task_id="t1")
        tracer.record_task(self._task("completed", cost=0.5))
        tracer.meter.charge(Usage(input_tokens=1_000_000), "m", task_id="t2")
        tracer.record_task(self._task("max_steps", cost=0.5))
        sli = tracer.sli()
        self.assertAlmostEqual(sli["total_cost_usd"], 2.0)
        self.assertAlmostEqual(sli["cost_per_success_usd"], 2.0)

    def test_steps_distribution_has_percentiles(self):
        tracer = Tracer(_Config())
        for count in range(1, 11):
            tracer.record_task(self._task("completed", steps=count))
        percentiles = tracer.sli()["steps_per_task"]
        self.assertEqual(percentiles["count"], 10)
        self.assertEqual(percentiles["p50"], 5)
        self.assertGreaterEqual(percentiles["p90"], percentiles["p50"])
        self.assertEqual(percentiles["max"], 10)

    def test_empty_sli_is_all_zero_not_crash(self):
        sli = Tracer(_Config()).sli()
        self.assertEqual(sli["task_count"], 0)
        self.assertEqual(sli["task_success_rate"], 0.0)
        self.assertEqual(sli["cost_per_success_usd"], 0.0)

    def test_reset_clears_counters_and_spans(self):
        tracer = Tracer(_Config())
        with tracer.span("x"):
            pass
        tracer.record_task(self._task())
        tracer.reset()
        self.assertEqual(tracer.spans(), [])
        self.assertEqual(tracer.sli()["task_count"], 0)


# --------------------------------------------------------------------------- 成本账


class TestCostMeter(unittest.TestCase):
    def test_charge_uses_config_prices(self):
        """单价来自配置，不写死在代码里。"""
        meter = CostMeter(_Config(price_in=1.0, price_out=2.0))
        usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
        cost = meter.charge(usage, "mock-model")
        self.assertAlmostEqual(cost, 3.0)
        self.assertAlmostEqual(meter.total(), 3.0)

    def test_breakdown_by_model_and_task(self):
        """成本账能按维度拆分。"""
        meter = CostMeter(_Config(price_in=1.0, price_out=0.0))
        meter.charge(Usage(input_tokens=1_000_000), "model-a", task_id="t1")
        meter.charge(Usage(input_tokens=2_000_000), "model-b", task_id="t1")
        meter.charge(Usage(input_tokens=1_000_000), "model-a", task_id="t2")
        by_model = meter.breakdown("model")
        self.assertAlmostEqual(by_model["model-a"]["cost_usd"], 2.0)
        self.assertAlmostEqual(by_model["model-b"]["cost_usd"], 2.0)
        by_task = meter.breakdown("task_id")
        self.assertAlmostEqual(by_task["t1"]["cost_usd"], 3.0)
        self.assertEqual(by_task["t1"]["calls"], 2)

    def test_cached_input_is_not_double_billed(self):
        """缓存命中的输入是 input_tokens 的子集，不能重复计费。"""
        meter = CostMeter(_Config(price_in=1.0, price_out=0.0))
        usage = Usage(input_tokens=1_000_000, output_tokens=0)
        cost = meter.charge(usage, "m", cached_input_tokens=1_000_000)
        self.assertAlmostEqual(cost, 1.0)

    def test_charge_never_raises(self):
        """计价失败不该让任务崩掉——最坏是这次记 0 美元。"""
        meter = CostMeter(_Config())
        self.assertEqual(meter.charge(object(), "m"), 0.0)

    def test_tokens_are_accumulated(self):
        meter = CostMeter(_Config())
        meter.charge(Usage(input_tokens=10, output_tokens=5), "m")
        meter.charge(Usage(input_tokens=7, output_tokens=3), "m")
        tokens = meter.tokens()
        self.assertEqual(tokens["input_tokens"], 17)
        self.assertEqual(tokens["output_tokens"], 8)
        self.assertEqual(meter.calls(), 2)


# --------------------------------------------------------------------------- 导出


class TestExporter(unittest.TestCase):
    def test_jsonl_exporter_writes_one_line_per_span(self):
        tracer = Tracer(_Config())
        with tracer.span("a"):
            pass
        with tracer.span("b"):
            pass
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "trace.jsonl"
            tracer.export(JsonlExporter(path))
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
            self.assertEqual(len(lines), 2)
            payload = json.loads(lines[0])
            self.assertIn("traceId", payload)
            self.assertIn("spanId", payload)
            self.assertIn("attributes", payload)

    def test_otel_payload_uses_camel_case_ids(self):
        tracer = Tracer(_Config())
        with tracer.span("a", **{"gen_ai.request.model": "m"}) as span:
            pass
        payload = span.to_otel()
        self.assertEqual(payload["name"], "a")
        self.assertTrue(payload["traceId"])
        self.assertEqual(payload["attributes"]["gen_ai.request.model"], "m")


# --------------------------------------------------------------------------- 自动插桩


class TestAutoInstrumentation(unittest.TestCase):
    def _agent(self, tracer: Tracer, script):
        from dragon_agent.agent import Agent
        from dragon_agent.context import ContextAssembler
        from dragon_agent.state import StateStore

        config = Config.from_env_and_args([])
        registry = ToolRegistry()
        registry.register(
            ToolSpec(name="add", description="相加",
                     parameters={"type": "object",
                                 "properties": {"a": {"type": "integer"},
                                                "b": {"type": "integer"}},
                                 "required": ["a", "b"]}),
            lambda a, b: a + b,
        )
        return Agent(config, MockLLM(script=script), registry,
                     ContextAssembler(config), StateStore(config), tracer=tracer)

    def test_agent_run_produces_a_span_tree(self):
        """带 tracer 的 Agent 跑一次，任务 / 模型 / 工具三层 span 都在。"""
        tracer = Tracer(_Config())
        agent = self._agent(tracer, [
            LLMResponse(text="算一下",
                         tool_calls=[ToolCall(name="add", arguments={"a": 2, "b": 3})],
                         usage=Usage(input_tokens=10, output_tokens=4),
                         finish_reason="tool_calls"),
            LLMResponse(text="5", usage=Usage(input_tokens=12, output_tokens=3),
                        finish_reason="stop"),
        ])
        agent.run("跑一次带 trace 的任务")
        names = [span.name for span in tracer.spans()]
        self.assertIn("invoke_agent", names)
        self.assertIn("gen_ai.chat", names)
        self.assertIn("execute_tool", names)

    def test_child_spans_hang_under_the_task_span(self):
        """模型与工具的 span 是任务 span 的子节点。"""
        tracer = Tracer(_Config())
        agent = self._agent(tracer, [
            LLMResponse(text="算一下",
                         tool_calls=[ToolCall(name="add", arguments={"a": 2, "b": 3})],
                         usage=Usage(input_tokens=10, output_tokens=4),
                         finish_reason="tool_calls"),
            LLMResponse(text="5", usage=Usage(input_tokens=12, output_tokens=3),
                        finish_reason="stop"),
        ])
        agent.run("跑一次带 trace 的任务")
        spans = {span.name: span for span in tracer.spans()}
        task = spans["invoke_agent"]
        self.assertEqual(spans["gen_ai.chat"].parent, task.span_id)
        self.assertEqual(spans["execute_tool"].parent, task.span_id)
        self.assertEqual(spans["gen_ai.chat"].trace_id, task.trace_id)

    def test_usage_is_recorded_on_model_spans(self):
        tracer = Tracer(_Config())
        agent = self._agent(tracer, [
            LLMResponse(text="答", usage=Usage(input_tokens=12, output_tokens=3),
                        finish_reason="stop"),
        ])
        agent.run("跑一次")
        chat = [s for s in tracer.spans() if s.name == "gen_ai.chat"][0]
        self.assertEqual(chat.attributes["gen_ai.usage.input_tokens"], 12)
        self.assertEqual(chat.attributes["gen_ai.usage.output_tokens"], 3)
        self.assertEqual(chat.attributes["gen_ai.response.finish_reasons"], "stop")

    def test_tool_failure_is_recorded_as_error(self):
        """工具失败要能在 trace 里看见，还得分桶。

        **参数不合法**与**工具自身失败**是两个桶——整体错误率会把它们
        混在一起，看不出是模型传错了还是下游挂了。
        """
        tracer = Tracer(_Config())
        agent = self._agent(tracer, [
            LLMResponse(text="算一下",
                         tool_calls=[ToolCall(name="add", arguments={"a": 2})],
                         usage=Usage(input_tokens=10, output_tokens=4),
                         finish_reason="tool_calls"),
            LLMResponse(text="补上 b", usage=Usage(input_tokens=12, output_tokens=3),
                        finish_reason="stop"),
        ])
        agent.run("参数不对")
        tool_span = [s for s in tracer.spans() if s.name == "execute_tool"][0]
        self.assertEqual(tool_span.attributes["tool.result.status"], "error")
        self.assertEqual(tool_span.attributes["error.type"], "invalid_arguments")
        self.assertEqual(tracer.sli()["tool_errors"], 1)

    def test_tool_runtime_failure_is_a_different_bucket(self):
        """工具自己抛错 → tool_failure，不是 invalid_arguments。"""
        tracer = Tracer(_Config())
        from dragon_agent.agent import Agent
        from dragon_agent.context import ContextAssembler
        from dragon_agent.state import StateStore

        config = Config.from_env_and_args([])
        registry = ToolRegistry()
        registry.register(
            ToolSpec(name="boom", description="总是炸",
                     parameters={"type": "object", "properties": {}}),
            lambda: (_ for _ in ()).throw(RuntimeError("下游挂了")),
        )
        agent = Agent(config,
                      MockLLM(script=[
                          LLMResponse(text="调一下",
                                      tool_calls=[ToolCall(name="boom", arguments={})],
                                      usage=Usage(input_tokens=5, output_tokens=2),
                                      finish_reason="tool_calls"),
                          LLMResponse(text="算了", usage=Usage(input_tokens=6, output_tokens=2),
                                      finish_reason="stop"),
                      ]),
                      registry, ContextAssembler(config), StateStore(config),
                      tracer=tracer)
        agent.run("工具炸了")
        tool_span = [s for s in tracer.spans() if s.name == "execute_tool"][0]
        self.assertEqual(tool_span.attributes["error.type"], "tool_failure")

    def test_task_span_records_the_outcome(self):
        tracer = Tracer(_Config())
        agent = self._agent(tracer, [
            LLMResponse(text="答", usage=Usage(input_tokens=1, output_tokens=1),
                        finish_reason="stop"),
        ])
        agent.run("跑一次")
        task = [s for s in tracer.spans() if s.name == "invoke_agent"][0]
        self.assertEqual(task.attributes["agent.status"], "completed")
        self.assertEqual(tracer.sli()["task_success_rate"], 1.0)

    def test_agent_without_tracer_is_unaffected(self):
        """不带 tracer 的 Agent 走原路径，埋点层不改变任何行为。"""
        from dragon_agent.agent import Agent
        from dragon_agent.context import ContextAssembler
        from dragon_agent.state import StateStore

        config = Config.from_env_and_args([])
        agent = Agent(config, MockLLM(script=[LLMResponse(text="答")]),
                      ToolRegistry(), ContextAssembler(config), StateStore(config))
        result = agent.run("没有 tracer")
        self.assertEqual(result.status, "completed")
        self.assertIsNone(getattr(agent, "tracer", None))


if __name__ == "__main__":
    unittest.main()
