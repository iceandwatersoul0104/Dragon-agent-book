"""端到端链路测试：从用户输入到最终答案，走完整条回路。

断言的是**最终状态**与**成本账**——不是"跑完不崩就算过"：
- 最终状态：``AgentResult.status``、``stop_reason``、答案内容、步骤条数；
- 成本账：token 账与钱账都要对得上，而且各步之和与总计自洽。

全程离线：MockLLM + 假工具，不碰网络、不需要 API Key。
"""
from __future__ import annotations

import json
import unittest

try:  # 包内运行（python -m unittest discover）
    from ._compat import (
        make_config,
        make_cost_meter,
        make_stack,
        make_tracer,
        module_available,
        scripted_llm,
    )
except ImportError:  # pragma: no cover - 直接以脚本方式运行
    from _compat import (  # type: ignore[no-redef]
        make_config,
        make_cost_meter,
        make_stack,
        make_tracer,
        module_available,
        scripted_llm,
    )

from dragon_agent.llm import MockLLM, Usage

# 剧本里三段用量：工具调用一步、最终回答一步。
USAGE_CALL = Usage(input_tokens=120, output_tokens=18)
USAGE_ANSWER = Usage(input_tokens=180, output_tokens=12)
EXPECTED_TOTAL_TOKENS = USAGE_CALL.total + USAGE_ANSWER.total  # 330


def _happy_path_stack(**config_overrides):
    """标准剧本：先调用 add 工具，再给出最终答案。"""
    return make_stack(
        llm=scripted_llm(
            MockLLM.call("add", {"a": 2, "b": 3}, in_tokens=USAGE_CALL.input_tokens,
                         out_tokens=USAGE_CALL.output_tokens),
            MockLLM.say("2 + 3 = 5", in_tokens=USAGE_ANSWER.input_tokens,
                       out_tokens=USAGE_ANSWER.output_tokens),
        ),
        max_steps=5,
        **config_overrides,
    )


class TestEndToEnd(unittest.TestCase):
    """走完整条链路：输入 → 规划 → 工具 → 观察 → 答案 → 停止。"""

    # ------------------------------------------------------------------ 状态

    def test_happy_path_reaches_a_final_answer(self):
        stack = _happy_path_stack()
        result = stack.agent.run("帮我算一下 2 + 3")

        self.assertEqual(result.status, "completed")
        self.assertIn("5", result.answer)
        self.assertTrue(result.stop_reason.strip(), "必须说清楚为什么停")
        self.assertGreaterEqual(len(result.steps), 1)

        # 工具确实被调用过一次，而且只调了一次
        self.assertEqual(stack.registry.invocations, ["add"])
        self.assertEqual(stack.calls["add"], 1)

        # 状态层同步落账
        snapshot = stack.state.snapshot()
        self.assertEqual(len(snapshot.steps), len(result.steps))
        self.assertGreaterEqual(snapshot.version, 1)

    def test_observation_is_fed_back_to_the_model(self):
        """工具结果必须回到下一轮请求里，否则模型就是瞎猜。"""
        stack = _happy_path_stack()
        stack.agent.run("帮我算一下 2 + 3")

        self.assertGreaterEqual(len(stack.llm.calls), 2)
        second = json.dumps(stack.llm.calls[1]["messages"], ensure_ascii=False, default=str)
        self.assertIn("5", second, "第二次模型请求里应当看得见工具的观察结果")

    def test_final_answer_does_not_leak_the_secret_plan(self):
        """最终答案里应该只有答案本身，不该把整段上下文倒出来。"""
        stack = _happy_path_stack()
        result = stack.agent.run("帮我算一下 2 + 3")
        self.assertNotIn("add", result.answer.lower())


# ---------------------------------------------------------------------------
# 成本账
# ---------------------------------------------------------------------------


class TestCostLedger(unittest.TestCase):
    """token 账与钱账：数字必须来自实际运行，不许估算。"""

    def test_token_ledger_matches_the_scripted_usage(self):
        stack = _happy_path_stack()
        result = stack.agent.run("帮我算一下 2 + 3")

        # 两次模型调用（工具调用 + 最终回答）的用量之和
        self.assertEqual(result.total_tokens, EXPECTED_TOTAL_TOKENS)
        # 各步之和不得超过总计（最终回答可能不单独记成一步）
        step_tokens = sum(step.tokens for step in result.steps)
        self.assertGreater(step_tokens, 0)
        self.assertLessEqual(step_tokens, result.total_tokens)

    def test_mock_pricing_costs_nothing(self):
        """mock 模式单价为 0，钱账必须是 0——不是「大概很小」。"""
        stack = _happy_path_stack()
        result = stack.agent.run("帮我算一下 2 + 3")
        self.assertEqual(result.total_cost, 0.0)

    def test_cost_ledger_follows_the_configured_prices(self):
        """单价表来自配置：钱账必须按配置的单价算出来。

        计价口径见 ``dragon_agent/pricing.py``：
        ``cost = (input_tokens * 输入单价 + output_tokens * 输出单价) / 1e6``。
        """
        stack = _happy_path_stack(price_in_per_mtok=1.0, price_out_per_mtok=2.0)
        result = stack.agent.run("帮我算一下 2 + 3")

        cfg = stack.config
        input_tokens = USAGE_CALL.input_tokens + USAGE_ANSWER.input_tokens
        output_tokens = USAGE_CALL.output_tokens + USAGE_ANSWER.output_tokens
        expected = (input_tokens * cfg.price_in_per_mtok
                    + output_tokens * cfg.price_out_per_mtok) / 1_000_000
        self.assertAlmostEqual(result.total_cost, expected, places=9)
        self.assertGreater(result.total_cost, 0.0)

        # 再用 CostMeter 交叉核对一遍：同口径、同单价，必须算出同一个数
        oracle = _expected_cost(stack.config, [USAGE_CALL, USAGE_ANSWER], stack.agent)
        if oracle is not None:
            self.assertAlmostEqual(result.total_cost, oracle, places=9)

    def test_step_costs_are_consistent_with_the_total(self):
        """各步的钱加起来不能超过总账。"""
        stack = _happy_path_stack(price_in_per_mtok=1.0, price_out_per_mtok=2.0)
        result = stack.agent.run("帮我算一下 2 + 3")
        step_cost = sum(step.cost for step in result.steps)
        self.assertGreaterEqual(result.total_cost, step_cost - 1e-12)
        self.assertGreaterEqual(result.total_cost, 0.0)


def _expected_cost(config, usages, agent=None):
    """用 ``CostMeter`` 当裁判，算出这次运行**应该**花多少钱。

    优先用 Agent 自己的 meter（保证同源）；拿不到就用同一份配置造一个。
    裁判造不出来就返回 None——交叉核对是加分项，不是必需项。
    """
    meter = None
    if agent is not None:
        for attr in ("cost_meter", "meter"):
            candidate = getattr(agent, attr, None)
            if candidate is not None and hasattr(candidate, "charge"):
                meter = candidate
                break
    if meter is None:
        try:
            meter = make_cost_meter(config)
        except Exception:
            meter = None
    if meter is None:
        return None
    try:
        return sum(meter.charge(u, config.model) for u in usages)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 错误回灌与护栏：链路上的失败也要能走完
# ---------------------------------------------------------------------------


class TestErrorRecoveryEndToEnd(unittest.TestCase):
    """参数错了、回灌了、改对了——这也算一次完整的端到端。"""

    def test_invalid_arguments_are_recovered(self):
        stack = make_stack(
            llm=scripted_llm(
                MockLLM.call("add", {"a": "两", "b": 3}),   # 类型错
                MockLLM.call("add", {"a": 2, "b": 3}),      # 改对了
                MockLLM.say("2 + 3 = 5"),
            ),
            max_steps=6,
        )
        result = stack.agent.run("帮我算一下 2 + 3")

        self.assertEqual(result.status, "completed")
        self.assertIn("5", result.answer)
        self.assertEqual(stack.calls["add"], 1, "参数错的那一次不该真的执行函数")
        self.assertEqual(stack.registry.invocations, ["add"])

        # 第二轮请求要带上门禁错误，第三轮要带上门禁结果
        self.assertGreaterEqual(len(stack.llm.calls), 3)
        second = json.dumps(stack.llm.calls[1]["messages"], ensure_ascii=False, default=str)
        self.assertTrue("integer" in second or "参数" in second,
                        f"参数错误没有回灌：{second}")
        third = json.dumps(stack.llm.calls[2]["messages"], ensure_ascii=False, default=str)
        self.assertIn("5", third)

    def test_budget_guard_stops_the_chain(self):
        """预算烧穿要停，并且说清楚是预算停的。"""
        stack = make_stack(
            llm=scripted_llm(
                MockLLM.call("add", {"a": 1, "b": 2}, in_tokens=100_000, out_tokens=1_000),
                MockLLM.say("不会走到这里"),
            ),
            token_budget=1_000,
            max_steps=10,
        )
        result = stack.agent.run("跑一个很贵的任务")

        self.assertEqual(result.status, "stopped_budget")
        self.assertTrue(result.stop_reason.strip())
        self.assertTrue(
            any(k in result.stop_reason.lower() for k in ("预算", "budget", "token", "cost")),
            f"停止原因要能看出是预算停的：{result.stop_reason!r}",
        )
        self.assertGreaterEqual(result.total_tokens, 1_000)

    def test_max_steps_stops_the_chain(self):
        """步数用完也要停，并且说清楚是步数停的。"""
        stack = make_stack(
            llm=scripted_llm(*[MockLLM.call("add", {"a": i, "b": i}) for i in range(6)]),
            max_steps=3,
        )
        result = stack.agent.run("一直调工具的活儿")

        self.assertEqual(result.status, "max_steps")
        self.assertTrue(result.stop_reason.strip())
        self.assertTrue(
            any(k in result.stop_reason.lower() for k in ("步数", "step", "轮次")),
            f"停止原因要能看出是步数停的：{result.stop_reason!r}",
        )
        self.assertLessEqual(len(result.steps), 3)


# ---------------------------------------------------------------------------
# 公共 API 与 trace
# ---------------------------------------------------------------------------


class TestPublicAPIEndToEnd(unittest.TestCase):
    """走一遍对外承诺的公共 API：读者拿到 README 就能照着跑。"""

    def test_public_api_runs_the_whole_chain(self):
        from dragon_agent import (
            Agent,
            Config,
            ContextAssembler,
            MockLLM as PublicMockLLM,
            StateStore,
            ToolRegistry,
            ToolSpec,
        )

        config = Config.from_env_and_args([])
        llm = PublicMockLLM(script=[
            MockLLM.call("add", {"a": 2, "b": 3}, in_tokens=120, out_tokens=18),
            MockLLM.say("2 + 3 = 5", in_tokens=180, out_tokens=12),
        ])
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="add",
                description="两个整数相加，返回它们的和。",
                parameters={
                    "type": "object",
                    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                    "required": ["a", "b"],
                },
            ),
            lambda a, b: a + b,
        )
        context = ContextAssembler(config)
        state = StateStore(config)
        agent = Agent(config, llm, registry, context, state)

        result = agent.run("帮我算一下 2 + 3")
        self.assertEqual(result.status, "completed")
        self.assertIn("5", result.answer)
        self.assertEqual(result.total_tokens, EXPECTED_TOTAL_TOKENS)

    @unittest.skipUnless(module_available("observability"), "observability 模块尚未落地，待集成验收")
    def test_trace_records_the_run(self):
        """跑完要留下 trace：能看见模型、工具与用量。"""
        tracer = make_tracer(make_config())
        stack = _happy_path_stack(tracer=tracer)
        stack.agent.run("帮我算一下 2 + 3")

        # Agent 自己暴露的 tracer 优先；没有就用注入进去的那个
        tracer = getattr(stack.agent, "tracer", None) or tracer
        self.assertIsNotNone(tracer, "Agent 必须把 tracer 暴露出来")
        spans = tracer.spans()
        self.assertTrue(spans, "跑了一次任务就该有 span")

        gen_ai_seen = 0
        for span in spans:
            for key in span.attributes:
                self.assertFalse(key.startswith("llm."), f"属性 {key!r} 用了 llm.* 简写")
                if any(kw in key.lower() for kw in ("model", "token", "usage")):
                    self.assertTrue(
                        key.startswith("gen_ai."),
                        f"与模型/token 相关的属性 {key!r} 必须在 gen_ai.* 命名空间里",
                    )
                if key.startswith("gen_ai."):
                    gen_ai_seen += 1
        self.assertGreater(gen_ai_seen, 0, "trace 里一个 gen_ai.* 属性都没有")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
