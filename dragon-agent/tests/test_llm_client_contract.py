"""模型客户端的契约测试：mock 与真实必须一模一样。

这个文件守的是 ``llm.py`` docstring 里写死的那条约束：

    任何业务模块都**不允许**用 ``isinstance`` 判断客户端类型——那会把 mock
    与真实的行为差异泄漏到控制流里，测试就测不出线上问题。

除了这条，还守四组性质：

- **同一种响应**：mock 与真实客户端返回同一个类、同一组字段；
- **脚本化可回放**：同一份剧本跑两遍，结果逐字相同；
- **失败可分类**：限流、5xx、超时、响应不对各归各的类；
- **账算得清**：缓存命中与未命中分开计价。

全部离线：不联网、不需要 API Key、不写用户目录。
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.config import ConfigError
from dragon_agent.llm import (
    LLMClient,
    LLMResponse,
    MockLLM,
    OpenAICompatClient,
    ToolCall,
    Usage,
)
from dragon_agent.llm_policy import (
    FAILURE_BAD_RESPONSE,
    FAILURE_NONE,
    FAILURE_RATE_LIMITED,
    FAILURE_SERVER_ERROR,
    FAILURE_TIMEOUT,
    FAILURE_TRANSPORT,
    RetryPolicy,
    ResilientLLMClient,
    classify_failure,
    request_fingerprint,
)
from dragon_agent.pricing import PriceTable, TokenCounts


# ---------------------------------------------------------------------------
# 契约一：mock 与真实返回同一种响应
# ---------------------------------------------------------------------------


class TestResponseShape(unittest.TestCase):
    def setUp(self):
        self.raw = {
            "choices": [{
                "message": {
                    "content": "预算总额 100 万元。",
                    "tool_calls": [{
                        "function": {"name": "read_budget", "arguments": '{"fiscal_year": "2026"}'}
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 196, "completion_tokens": 34},
        }
        self.real = OpenAICompatClient(model="demo-model", api_key="dummy")._parse(self.raw)
        self.mock = MockLLM(script=[LLMResponse(text="预算总额 100 万元。")]).complete(
            [{"role": "user", "content": "查预算"}]
        )

    def test_same_class(self):
        """类型必须完全相同，不是"字段差不多"。"""
        self.assertIs(type(self.real), type(self.mock))
        self.assertIs(type(self.real), LLMResponse)

    def test_same_field_set(self):
        """字段集合必须相同——多一个少一个都是契约漂移。"""
        self.assertEqual(set(self.real.to_dict()), set(self.mock.to_dict()))
        self.assertEqual(
            set(self.real.to_dict()),
            {"text", "tool_calls", "usage", "finish_reason"},
        )

    def test_both_satisfy_the_protocol(self):
        """两种客户端都要满足同一个 LLMClient 协议。"""
        self.assertIsInstance(MockLLM(), LLMClient)
        self.assertIsInstance(OpenAICompatClient(model="m", api_key="k"), LLMClient)

    def test_real_parse_extracts_usage_and_tool_calls(self):
        """真实响应的解析：用量、工具调用、结束原因都要对上。"""
        self.assertEqual(self.real.usage.input_tokens, 196)
        self.assertEqual(self.real.usage.output_tokens, 34)
        self.assertEqual(self.real.usage.total, 230)
        self.assertEqual(self.real.finish_reason, "tool_calls")
        self.assertEqual(len(self.real.tool_calls), 1)
        self.assertEqual(self.real.tool_calls[0].name, "read_budget")
        self.assertEqual(self.real.tool_calls[0].arguments, {"fiscal_year": "2026"})

    def test_real_parse_without_choices_is_an_error_response(self):
        """响应里没有 choices 时降级成错误响应，不抛异常。"""
        resp = OpenAICompatClient(model="m", api_key="k")._parse({"choices": []})
        self.assertEqual(resp.finish_reason, "error")
        self.assertIn("choices", resp.text)


class TestNoTypeBranching(unittest.TestCase):
    """业务模块不得用 isinstance 判断客户端类型。"""

    @staticmethod
    def _dotted_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = TestNoTypeBranching._dotted_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return None

    def test_no_isinstance_on_client_types(self):
        """扫全包源码：不允许出现 isinstance(..., MockLLM/OpenAICompatClient)。"""
        package = ROOT / "dragon_agent"
        offenders: list[str] = []
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Name) and func.id == "isinstance"):
                    continue
                for arg in node.args[1:]:
                    name = self._dotted_name(arg)
                    if name and name.split(".")[-1] in {"MockLLM", "OpenAICompatClient"}:
                        offenders.append(f"{path.name}:{node.lineno} {name}")
        self.assertEqual(offenders, [], "业务代码按客户端类型分支，mock 就测不出线上问题")


# ---------------------------------------------------------------------------
# 契约二：脚本化与确定性回放
# ---------------------------------------------------------------------------


class TestMockScript(unittest.TestCase):
    def test_script_is_consumed_in_order(self):
        """剧本按顺序消费。"""
        llm = MockLLM(script=[
            LLMResponse(text="一", usage=Usage(1, 1)),
            LLMResponse(text="二", usage=Usage(2, 2)),
        ])
        self.assertEqual(llm.complete([]).text, "一")
        self.assertEqual(llm.complete([]).text, "二")

    def test_last_response_repeats_when_script_is_exhausted(self):
        """剧本用完后循环最后一条——写测试时不必关心调用了几次。"""
        llm = MockLLM(script=[LLMResponse(text="一"), LLMResponse(text="二")])
        llm.complete([])
        self.assertEqual(llm.complete([]).text, "二")
        self.assertEqual(llm.complete([]).text, "二")

    def test_empty_script_returns_empty_stop_response(self):
        """空剧本返回一条空的 stop 响应，不是异常。"""
        resp = MockLLM().complete([])
        self.assertEqual(resp.text, "")
        self.assertEqual(resp.finish_reason, "stop")
        self.assertEqual(resp.usage.total, 0)

    def test_replay_is_deterministic(self):
        """同一份剧本跑两遍，结果逐字相同。"""
        def run() -> list[dict]:
            llm = MockLLM(script=[
                LLMResponse(text="一", tool_calls=[ToolCall(name="t", arguments={"a": 1})],
                            usage=Usage(3, 4)),
                LLMResponse(text="二", usage=Usage(5, 6)),
            ])
            return [llm.complete([]).to_dict() for _ in range(2)]
        self.assertEqual(run(), run())

    def test_calls_are_recorded(self):
        """调用记录能用来断言"到底发了几次、带了什么"。"""
        llm = MockLLM()
        llm.complete([{"role": "user", "content": "x"}], tools=[{"name": "t"}])
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0]["tools"], [{"name": "t"}])


class TestValueObjects(unittest.TestCase):
    def test_usage_total_and_add(self):
        """用量的合计是属性，且相加不会各算各的。"""
        self.assertEqual(Usage(3, 4).total, 7)
        self.assertEqual((Usage(3, 4) + Usage(10, 20)).total, 37)

    def test_tool_call_roundtrip(self):
        """工具调用的字典往返要无损。"""
        call = ToolCall(name="read_budget", arguments={"fiscal_year": "2026"})
        self.assertEqual(ToolCall.from_dict(call.to_dict()), call)

    def test_tool_call_from_dict_tolerates_garbage(self):
        """残缺的字典不能抛异常——模型返回的东西不可信。"""
        call = ToolCall.from_dict({})
        self.assertEqual(call.name, "")
        self.assertEqual(call.arguments, {})

    def test_response_to_dict_shape(self):
        """响应字典的字段名要固定。"""
        d = LLMResponse(text="x", tool_calls=[ToolCall(name="t")]).to_dict()
        self.assertEqual(sorted(d), ["finish_reason", "text", "tool_calls", "usage"])
        self.assertEqual(d["usage"]["total"], 0)


# ---------------------------------------------------------------------------
# 契约三：真实 API 的错误处理
# ---------------------------------------------------------------------------


class TestRealClientErrors(unittest.TestCase):
    def test_missing_key_fails_at_construction(self):
        """缺 key 构造即抛——不要等到发请求才失败。"""
        with self.assertRaises(ConfigError):
            OpenAICompatClient(model="m", api_key="")

    def test_transport_failure_becomes_an_error_response(self):
        """网络层异常降级成错误响应，不抛给调用方。

        这里用打桩的 urlopen 制造失败，不真的建连接——测试必须完全离线。
        """
        import urllib.error
        from unittest import mock

        client = OpenAICompatClient(model="m", api_key="k")
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            resp = client.complete([{"role": "user", "content": "x"}])
        self.assertEqual(resp.finish_reason, "error")
        self.assertTrue(resp.text.startswith("[llm]"))


class TestFailureClassification(unittest.TestCase):
    def test_each_failure_maps_to_its_own_kind(self):
        """六种结局各有各的类别。"""
        cases = [
            (LLMResponse(text="正常", finish_reason="stop"), FAILURE_NONE),
            (LLMResponse(text="[llm] HTTP 429", finish_reason="error"), FAILURE_RATE_LIMITED),
            (LLMResponse(text="[llm] HTTP 503", finish_reason="error"), FAILURE_SERVER_ERROR),
            (LLMResponse(text="[llm] HTTP 408", finish_reason="error"), FAILURE_TIMEOUT),
            (LLMResponse(text="[llm] TimeoutError: timed out", finish_reason="error"), FAILURE_TIMEOUT),
            (LLMResponse(text="[llm] URLError: refused", finish_reason="error"), FAILURE_TRANSPORT),
            (LLMResponse(text="[llm] 响应里没有 choices", finish_reason="error"), FAILURE_BAD_RESPONSE),
            (LLMResponse(text="[llm] HTTP 400", finish_reason="error"), FAILURE_BAD_RESPONSE),
        ]
        for resp, expected in cases:
            self.assertEqual(classify_failure(resp), expected, resp.text)


class TestRetryPolicy(unittest.TestCase):
    def test_backoff_is_exponential(self):
        """退避是指数的：0.2 → 0.4 → 0.8。"""
        policy = RetryPolicy(base_delay_s=0.2, max_delay_s=5.0, jitter_ratio=0.0)
        self.assertEqual(policy.delay_for(1), 0.2)
        self.assertEqual(policy.delay_for(2), 0.4)
        self.assertEqual(policy.delay_for(3), 0.8)

    def test_backoff_is_capped(self):
        """退避有上限，不会越等越久。"""
        policy = RetryPolicy(base_delay_s=0.2, max_delay_s=0.5, jitter_ratio=0.0)
        self.assertEqual(policy.delay_for(5), 0.5)

    def test_jitter_stays_in_range(self):
        """抖动不会把延迟变成负数。"""
        policy = RetryPolicy(base_delay_s=0.2, jitter_ratio=0.5)
        self.assertGreaterEqual(policy.delay_for(1, jitter=-1.0), 0.0)
        self.assertAlmostEqual(policy.delay_for(1, jitter=0.0), 0.2)

    def test_invalid_policy_is_rejected(self):
        """非法策略在构造时就报错。"""
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=0)
        with self.assertRaises(ValueError):
            RetryPolicy(base_delay_s=-1.0)


class _ScriptedClient:
    """按剧本吐失败的假客户端。"""

    def __init__(self, failures: list[str]):
        self.failures = list(failures)
        self.calls = 0

    def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.failures:
            return LLMResponse(text=self.failures.pop(0), finish_reason="error")
        return LLMResponse(text="成功了", usage=Usage(10, 2))

    def describe(self) -> str:
        return "scripted-client"


class TestResilientClient(unittest.TestCase):
    def setUp(self):
        self.waited: list[float] = []
        self.policy = RetryPolicy(max_attempts=3, base_delay_s=0.2,
                                  max_delay_s=5.0, jitter_ratio=0.0)

    def _wrap(self, inner):
        return ResilientLLMClient(inner, policy=self.policy, sleep=self.waited.append)

    def test_retryable_failure_is_retried_until_success(self):
        """限流会重试，直到成功为止。"""
        client = self._wrap(_ScriptedClient(["[llm] HTTP 429", "[llm] HTTP 429"]))
        resp = client.complete([])
        self.assertEqual(resp.text, "成功了")
        self.assertEqual(client.attempts, 3)
        self.assertEqual(self.waited, [0.2, 0.4])

    def test_non_retryable_failure_is_not_retried(self):
        """响应不对重试也没用——一次都不该重试。"""
        client = self._wrap(_ScriptedClient(["[llm] 响应里没有 choices"]))
        resp = client.complete([])
        self.assertEqual(client.attempts, 1)
        self.assertEqual(self.waited, [])
        self.assertEqual(resp.finish_reason, "error")

    def test_exhausted_retries_return_the_last_error_response(self):
        """重试用尽后原样返回最后一条错误响应——调用方看不出重试发生过。"""
        client = self._wrap(_ScriptedClient(["[llm] HTTP 503"] * 3))
        resp = client.complete([])
        self.assertEqual(client.attempts, 3)
        self.assertEqual(resp.text, "[llm] HTTP 503")
        self.assertEqual(resp.finish_reason, "error")

    def test_sleep_is_injectable(self):
        """注入 sleep，测试不用真的等。"""
        client = self._wrap(_ScriptedClient(["[llm] HTTP 429"] * 3))
        client.complete([])
        self.assertEqual(self.waited, [0.2, 0.4])

    def test_history_records_each_attempt(self):
        """每次尝试都留痕，给 trace 用。"""
        client = self._wrap(_ScriptedClient(["[llm] HTTP 429", "[llm] HTTP 503"]))
        client.complete([])
        self.assertEqual(
            [h["failure"] for h in client.history],
            [FAILURE_RATE_LIMITED, FAILURE_SERVER_ERROR, FAILURE_NONE],
        )

    def test_fingerprint_is_deterministic_and_discriminating(self):
        """请求指纹：同样的输入同样指纹，不同输入不同指纹。"""
        a = request_fingerprint([{"role": "user", "content": "x"}])
        b = request_fingerprint([{"role": "user", "content": "x"}])
        c = request_fingerprint([{"role": "user", "content": "y"}])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_dedupe_sends_the_request_once(self):
        """开启去重后，同一指纹只发一次。"""
        inner = _ScriptedClient([])
        client = ResilientLLMClient(inner, dedupe=True, sleep=self.waited.append)
        client.complete([{"role": "user", "content": "x"}])
        client.complete([{"role": "user", "content": "x"}])
        self.assertEqual(inner.calls, 1)


# ---------------------------------------------------------------------------
# 契约四：用量与成本核算
# ---------------------------------------------------------------------------


class TestTokenCounts(unittest.TestCase):
    def test_cached_is_a_subset_of_input(self):
        """缓存命中数是输入 token 的子集。"""
        counts = TokenCounts(input_tokens=100, output_tokens=10, cached_input_tokens=80)
        self.assertEqual(counts.uncached_input_tokens, 20)
        self.assertEqual(counts.total, 110)

    def test_cached_cannot_exceed_input(self):
        """缓存数超过输入数是数据错误，当场拒绝。"""
        with self.assertRaises(ValueError):
            TokenCounts(input_tokens=10, cached_input_tokens=11)

    def test_negative_counts_are_rejected(self):
        """负数 token 没有意义。"""
        with self.assertRaises(ValueError):
            TokenCounts(input_tokens=-1)

    def test_from_usage_keeps_the_same_numbers(self):
        """从 llm.Usage 转过来，数字要一致。"""
        counts = TokenCounts.from_usage(Usage(196, 34))
        self.assertEqual(counts.input_tokens, 196)
        self.assertEqual(counts.output_tokens, 34)
        self.assertEqual(counts.cached_input_tokens, 0)


class TestPricing(unittest.TestCase):
    def setUp(self):
        self.prices = PriceTable(input_per_mtok=3.0, output_per_mtok=15.0,
                                cached_input_per_mtok=0.3)

    def test_cache_hit_is_cheaper(self):
        """同样的 token，命中缓存要更便宜。"""
        with_cache = self.prices.cost(TokenCounts(100_000, 2_000, 80_000))
        without = self.prices.cost(TokenCounts(100_000, 2_000, 0))
        self.assertLess(with_cache, without)
        self.assertAlmostEqual(with_cache, 0.114)
        self.assertAlmostEqual(without, 0.33)

    def test_breakdown_sums_to_the_total(self):
        """明细三项加起来要等于总额。"""
        counts = TokenCounts(100_000, 2_000, 80_000)
        parts = self.prices.breakdown(counts)
        total = (parts["cost_uncached_input_usd"] + parts["cost_cached_input_usd"]
                 + parts["cost_output_usd"])
        self.assertAlmostEqual(total, parts["cost_total_usd"])
        self.assertAlmostEqual(parts["cost_total_usd"], self.prices.cost(counts))

    def test_cache_price_defaults_to_input_price(self):
        """不区分缓存价时，缓存价等于输入价。"""
        prices = PriceTable(input_per_mtok=2.0, output_per_mtok=4.0)
        self.assertEqual(prices.effective_cached_price, 2.0)
        counts = TokenCounts(1_000, 0, 1_000)
        self.assertAlmostEqual(prices.cost(counts), 0.002)

    def test_zero_price_is_free(self):
        """默认单价是 0——mock 模式不该产生任何成本。"""
        self.assertEqual(PriceTable().cost(TokenCounts(1_000_000, 1_000_000)), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
