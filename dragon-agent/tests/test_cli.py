"""第 14 章命令行入口的契约测试：退出码、护栏组件、值班手册、评测判定。

这个文件守的是 ``dragon_agent/cli.py`` docstring 里写死的几条：

- **缺 API Key 且带 ``--live`` 时，以非 0 退出码退出并给清晰提示**——
  静默失败比报错贵得多；
- **mock 模式下所有子命令都能跑通，退出码 0**；
- **退出码有语义**：0 正常 / 2 用法错 / 3 配置错 / 4 任务失败 /
  5 熔断或预算耗尽 / 6 评测回归；
- **限流、熔断、预算告警都不抛异常**，只返回"拿不到"；
- **判定器不含 LLM 调用**，且每档预算告警**只报一次**；
- **``Config.to_dict()`` 不含密钥**。

全部离线：不联网、不需要 API Key、不写用户目录。
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.cli import (
    EXIT_BREAKER,
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REGRESSION,
    EXIT_USAGE,
    ONCALL_ACTIONS,
    BoundedQueue,
    BudgetWatcher,
    CircuitBreaker,
    CostThresholds,
    FallbackLLMClient,
    TokenBucket,
    ToolRateLimiter,
    build_config,
    build_parser,
    build_script_llm,
    build_thresholds,
    default_eval_set_path,
    judge,
    load_eval_rows,
    main,
    status_exit_code,
)
from dragon_agent.config import Config
from dragon_agent.llm import LLMResponse, Usage


class _FakeClock:
    """可注入的时钟：``sleep`` 会推进时间，因此测试不用真的等。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


def _no_key_env() -> mock._patch:
    """清掉所有可能带 key 的环境变量。"""
    return mock.patch.dict(os.environ, {}, clear=True)


def _with_key_env(value: str) -> mock._patch:
    return mock.patch.dict(os.environ, {"LLM_API_KEY": value}, clear=True)


def run_cli(argv: list[str]) -> int:
    """跑一次 CLI，**吞掉它的输出**。

    CLI 的输出是给人看的，不是给测试断言的——测试要断言的是退出码。
    """
    with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
        return main(argv)


class TestExitCodes(unittest.TestCase):
    """退出码是值班脚本与 CI 的接口，必须稳定。"""

    def test_live_without_key_exits_config_code(self):
        with _no_key_env():
            code = run_cli(["run", "查预算", "--live"])
        self.assertEqual(code, EXIT_CONFIG)
        self.assertNotEqual(code, EXIT_OK)

    def test_live_without_key_prints_hint(self):
        buffer = io.StringIO()
        # 这里直接调 main：提示走的是 stderr，得由测试自己接住。
        with _no_key_env(), mock.patch("sys.stderr", buffer):
            main(["run", "查预算", "--live"])
        text = buffer.getvalue()
        self.assertIn("LLM_API_KEY", text)
        self.assertIn("OPENAI_API_KEY", text)

    def test_live_with_key_does_not_exit_config(self):
        # 有 key 时不该在启动期就挂掉——失败要发生在运行期，而不是配置期。
        with _with_key_env("sk-test-not-a-real-key"):
            code = run_cli(["tools", "--live"])
        self.assertEqual(code, EXIT_OK)

    def test_mock_run_exits_zero(self):
        with _no_key_env():
            self.assertEqual(run_cli(["run", "查一下项目预算"]), EXIT_OK)

    def test_tools_exits_zero(self):
        with _no_key_env():
            self.assertEqual(run_cli(["tools"]), EXIT_OK)

    def test_demo_exits_zero(self):
        with _no_key_env():
            self.assertEqual(run_cli(["demo"]), EXIT_OK)

    def test_runbook_exits_zero(self):
        with _no_key_env():
            self.assertEqual(run_cli(["runbook"]), EXIT_OK)

    def test_no_command_prints_help_and_exits_usage(self):
        with _no_key_env():
            self.assertEqual(run_cli([]), EXIT_USAGE)

    def test_eval_exits_zero_on_bundled_set(self):
        with _no_key_env():
            self.assertEqual(run_cli(["eval"]), EXIT_OK)

    def test_status_exit_code_mapping(self):
        class _Result:
            def __init__(self, status: str) -> None:
                self.status = status

        self.assertEqual(status_exit_code(_Result("completed")), EXIT_OK)
        self.assertEqual(status_exit_code(_Result("stopped_budget")), EXIT_BREAKER)
        self.assertEqual(status_exit_code(_Result("max_steps")), EXIT_FAILED)
        self.assertEqual(status_exit_code(_Result("stopped_no_progress")), EXIT_FAILED)
        self.assertEqual(status_exit_code(_Result("error")), EXIT_FAILED)


class TestEval(unittest.TestCase):
    """评测判定：确定性、可离线、能识别回归。"""

    def test_judge_detects_missing_expect(self):
        ok, reasons = judge("预算是一百万", ["研发"], [])
        self.assertFalse(ok)
        self.assertTrue(any("研发" in r for r in reasons))

    def test_judge_detects_forbidden(self):
        ok, reasons = judge("预算未知", [], ["未知"])
        self.assertFalse(ok)
        self.assertTrue(any("未知" in r for r in reasons))

    def test_judge_passes_clean_answer(self):
        ok, reasons = judge("预算 100 万，研发 62 万", ["100 万", "研发"], ["未知"])
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_bundled_eval_set_all_pass(self):
        path = default_eval_set_path()
        self.assertTrue(path.exists(), f"默认评测集缺失：{path}")
        rows = load_eval_rows(path)
        self.assertGreaterEqual(len(rows), 3)
        for row in rows:
            self.assertTrue(row.id)
            self.assertTrue(row.task)
            self.assertTrue(row.expect, f"{row.id} 没有判据")

    def test_eval_reports_regression_with_nonzero_code(self):
        rows = [
            {"id": "bad-1", "task": "查预算", "expect": ["绝对不会出现的词"],
             "answer": "预算总额 100 万元"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "cases.jsonl"
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                            encoding="utf-8")
            with _no_key_env():
                code = run_cli(["eval", "--eval-set", str(path)])
        self.assertEqual(code, EXIT_REGRESSION)

    def test_eval_detects_regression_against_baseline(self):
        # 这条用例现在会挂（判据要的词不在答案里），但基线里它是过的——
        # 这就是"回归"的定义。
        rows = [
            {"id": "c1", "task": "查预算", "expect": ["研发"],
             "answer": "预算总额 100 万元"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "cases.jsonl"
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                            encoding="utf-8")
            baseline = pathlib.Path(tmp) / "baseline.json"
            baseline.write_text(json.dumps({"case_results": {"c1": True}}),
                                encoding="utf-8")
            with _no_key_env():
                code = run_cli(["eval", "--eval-set", str(path), "--baseline", str(baseline)])
        self.assertEqual(code, EXIT_REGRESSION)

    def test_eval_baseline_accepts_simplified_format(self):
        rows = [
            {"id": "c1", "task": "查预算", "expect": ["研发"],
             "answer": "预算总额 100 万元"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "cases.jsonl"
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                            encoding="utf-8")
            baseline = pathlib.Path(tmp) / "baseline.json"
            baseline.write_text(json.dumps({"passed_ids": ["c1"]}), encoding="utf-8")
            with _no_key_env():
                code = run_cli(["eval", "--eval-set", str(path), "--baseline", str(baseline)])
        self.assertEqual(code, EXIT_REGRESSION)

    def test_no_regression_when_baseline_agrees(self):
        rows = [
            {"id": "c1", "task": "查预算", "expect": ["预算"],
             "answer": "预算总额 100 万元"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "cases.jsonl"
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                            encoding="utf-8")
            baseline = pathlib.Path(tmp) / "baseline.json"
            baseline.write_text(json.dumps({"case_results": {"c1": True}}),
                                encoding="utf-8")
            with _no_key_env():
                code = run_cli(["eval", "--eval-set", str(path), "--baseline", str(baseline)])
        self.assertEqual(code, EXIT_OK)

    def test_script_llm_replays_script(self):
        llm = build_script_llm([{"text": "先查", "tool": "add", "args": {"a": 1, "b": 2}},
                                {"text": "结果是 3"}])
        first = llm.complete([{"role": "user", "content": "hi"}])
        self.assertTrue(first.has_tool_calls)
        self.assertEqual(first.tool_calls[0].name, "add")
        second = llm.complete([{"role": "user", "content": "hi"}])
        self.assertFalse(second.has_tool_calls)
        self.assertEqual(second.text, "结果是 3")

    def test_load_eval_rows_skips_comments_and_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "cases.jsonl"
            path.write_text("# 注释\n\n" + json.dumps(
                {"id": "a", "task": "t", "expect": ["x"]}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            rows = load_eval_rows(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, "a")


class TestTokenBucket(unittest.TestCase):
    """模型侧限流：令牌桶。"""

    def test_burst_is_allowed_then_limited(self):
        clock = _FakeClock()
        bucket = TokenBucket(rate_per_s=1.0, burst=3, clock=clock, sleep=clock.sleep)
        self.assertTrue(bucket.acquire(1, timeout_s=0.0))
        self.assertTrue(bucket.acquire(1, timeout_s=0.0))
        self.assertTrue(bucket.acquire(1, timeout_s=0.0))
        self.assertFalse(bucket.acquire(1, timeout_s=0.0))
        self.assertEqual(bucket.rejected, 1)

    def test_tokens_refill_over_time(self):
        clock = _FakeClock()
        bucket = TokenBucket(rate_per_s=2.0, burst=1, clock=clock, sleep=clock.sleep)
        self.assertTrue(bucket.acquire(1, timeout_s=0.0))
        self.assertFalse(bucket.acquire(1, timeout_s=0.0))
        clock.now += 1.0  # 过了 1 秒，按 2/秒 补回 2 个令牌
        self.assertTrue(bucket.acquire(1, timeout_s=0.0))

    def test_acquire_never_raises(self):
        clock = _FakeClock()
        bucket = TokenBucket(rate_per_s=0.001, burst=1, clock=clock, sleep=clock.sleep)
        bucket.acquire(1, timeout_s=0.0)
        self.assertIsInstance(bucket.acquire(1, timeout_s=0.01), bool)


class TestCircuitBreaker(unittest.TestCase):
    """自动熔断。"""

    def test_opens_after_threshold_failures(self):
        breaker = CircuitBreaker(threshold=3, cooldown_s=30.0, clock=_FakeClock())
        self.assertTrue(breaker.allow())
        breaker.record_failure()
        breaker.record_failure()
        self.assertTrue(breaker.allow())
        breaker.record_failure()
        self.assertEqual(breaker.state, "open")
        self.assertFalse(breaker.allow())

    def test_half_open_after_cooldown(self):
        clock = _FakeClock()
        breaker = CircuitBreaker(threshold=1, cooldown_s=10.0, clock=clock)
        breaker.record_failure()
        self.assertFalse(breaker.allow())
        clock.now += 11.0
        self.assertTrue(breaker.allow())
        self.assertEqual(breaker.state, "half_open")

    def test_success_closes_circuit(self):
        breaker = CircuitBreaker(threshold=2, cooldown_s=0.0, clock=_FakeClock())
        breaker.record_failure()
        breaker.record_success()
        self.assertEqual(breaker.state, "closed")
        self.assertEqual(breaker.failures, 0)

    def test_half_open_failure_reopens(self):
        clock = _FakeClock()
        breaker = CircuitBreaker(threshold=1, cooldown_s=1.0, clock=clock)
        breaker.record_failure()
        clock.now += 2.0
        self.assertTrue(breaker.allow())
        breaker.record_failure()
        self.assertEqual(breaker.state, "open")


class TestBudgetWatcher(unittest.TestCase):
    """预算告警。"""

    def test_warns_at_warn_ratio(self):
        watcher = BudgetWatcher(cost_budget=1.0, thresholds=CostThresholds())
        fired = watcher.observe(cost=0.85)
        self.assertEqual(len(fired), 1)
        self.assertIn("warn", fired[0])

    def test_each_level_fires_only_once(self):
        watcher = BudgetWatcher(cost_budget=1.0, thresholds=CostThresholds())
        self.assertEqual(len(watcher.observe(cost=0.85)), 1)
        self.assertEqual(len(watcher.observe(cost=0.85)), 0)
        self.assertEqual(len(watcher.observe(cost=0.95)), 1)

    def test_under_threshold_is_silent(self):
        watcher = BudgetWatcher(cost_budget=1.0, thresholds=CostThresholds())
        self.assertEqual(watcher.observe(cost=0.1), [])

    def test_token_budget_is_watched_too(self):
        watcher = BudgetWatcher(token_budget=1000, thresholds=CostThresholds())
        fired = watcher.observe(cost=0.0, tokens=900)
        self.assertEqual(len(fired), 1)
        self.assertIn("token", fired[0])


class TestToolRateLimiter(unittest.TestCase):
    """工具侧限流。"""

    def test_blocks_over_concurrency(self):
        limiter = ToolRateLimiter(max_concurrency=2)
        self.assertTrue(limiter.acquire("a"))
        self.assertTrue(limiter.acquire("b"))
        self.assertFalse(limiter.acquire("c"))
        self.assertEqual(limiter.blocked, 1)

    def test_release_frees_slot(self):
        limiter = ToolRateLimiter(max_concurrency=1)
        limiter.acquire("a")
        self.assertFalse(limiter.acquire("b"))
        limiter.release("a")
        self.assertTrue(limiter.acquire("b"))

    def test_per_tool_rate_limit(self):
        clock = _FakeClock()
        limiter = ToolRateLimiter(rates={"slow": (0.001, 1)}, max_concurrency=10,
                                  clock=clock, sleep=clock.sleep)
        self.assertTrue(limiter.acquire("slow"))
        self.assertFalse(limiter.acquire("slow", timeout_s=0.0))
        self.assertTrue(limiter.acquire("other"))


class TestBoundedQueue(unittest.TestCase):
    """队列与背压。"""

    def test_drops_when_full_and_no_wait(self):
        queue = BoundedQueue(capacity=2, wait_s=0.01)
        self.assertTrue(queue.put("a"))
        self.assertTrue(queue.put("b"))
        self.assertFalse(queue.put("c"))
        self.assertEqual(queue.dropped, 1)
        self.assertEqual(len(queue), 2)

    def test_get_returns_items_in_order(self):
        queue = BoundedQueue(capacity=4, wait_s=0.01)
        queue.put("a")
        queue.put("b")
        self.assertEqual(queue.get(timeout=0.1), "a")
        self.assertEqual(queue.get(timeout=0.1), "b")

    def test_get_returns_none_on_timeout(self):
        queue = BoundedQueue(capacity=1, wait_s=0.01)
        self.assertIsNone(queue.get(timeout=0.01))


class TestFallbackClient(unittest.TestCase):
    """降级模型与熔断。"""

    class _RateLimited:
        def complete(self, messages, tools=None, **kwargs):
            return LLMResponse(text="[llm] HTTP 429", finish_reason="error")

        def describe(self):
            return "primary"

    class _Working:
        def complete(self, messages, tools=None, **kwargs):
            return LLMResponse(text="备用模型的回答", usage=Usage(10, 5), finish_reason="stop")

        def describe(self):
            return "fallback"

    def test_switches_to_fallback_on_rate_limit(self):
        client = FallbackLLMClient(primary=self._RateLimited(),
                                   fallback=self._Working(),
                                   breaker=CircuitBreaker(threshold=3))
        response = client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(response.text, "备用模型的回答")
        self.assertTrue(client.degraded)
        self.assertEqual(client.switches, 1)

    def test_degradation_sticks(self):
        client = FallbackLLMClient(primary=self._RateLimited(),
                                   fallback=self._Working(),
                                   breaker=CircuitBreaker(threshold=3))
        client.complete([{"role": "user", "content": "hi"}])
        client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(client.switches, 1)
        self.assertIn("已降级", client.describe())

    def test_breaker_rejects_when_open(self):
        breaker = CircuitBreaker(threshold=1, cooldown_s=300.0, clock=_FakeClock())
        client = FallbackLLMClient(primary=self._RateLimited(),
                                   fallback=self._Working(), breaker=breaker)
        client.complete([{"role": "user", "content": "hi"}])   # 触发一次失败
        breaker.record_failure()                                 # 打开熔断
        response = client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(response.finish_reason, "error")
        self.assertIn("熔断", response.text)

    def test_non_retryable_failure_does_not_degrade(self):
        class _BadRequest:
            def complete(self, messages, tools=None, **kwargs):
                return LLMResponse(text="[llm] HTTP 400", finish_reason="error")

            def describe(self):
                return "primary"

        client = FallbackLLMClient(primary=_BadRequest(), fallback=self._Working(),
                                   breaker=CircuitBreaker(threshold=3))
        client.complete([{"role": "user", "content": "hi"}])
        self.assertFalse(client.degraded)


class TestThresholdsAndRunbook(unittest.TestCase):
    """阈值与值班手册。"""

    def test_threshold_validation(self):
        with self.assertRaises(ValueError):
            CostThresholds(warn_ratio=0.0)
        with self.assertRaises(ValueError):
            CostThresholds(warn_ratio=0.95, degrade_ratio=0.9)
        with self.assertRaises(ValueError):
            CostThresholds(breaker_failures=0)

    def test_degrade_ratio_not_below_warn_ratio(self):
        with self.assertRaises(ValueError):
            CostThresholds(warn_ratio=0.9, degrade_ratio=0.5)

    def test_oncall_actions_cover_required_signals(self):
        text = " ".join(
            f"{a.signal} {a.threshold} {a.action} {a.escalate}" for a in ONCALL_ACTIONS
        )
        for keyword in ("限流", "工具", "成功率", "成本", "容器"):
            self.assertIn(keyword, text)
        for action in ONCALL_ACTIONS:
            self.assertTrue(action.signal)
            self.assertTrue(action.threshold)
            self.assertTrue(action.action)
            self.assertTrue(action.escalate, f"{action.signal} 没有升级路径")

    def test_runbook_json_output_is_machine_readable(self):
        buffer = io.StringIO()
        # 直接调 main：JSON 要由测试自己从 stdout 接住。
        with _no_key_env(), mock.patch("sys.stdout", buffer):
            code = main(["runbook", "--json"])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, EXIT_OK)
        self.assertIn("thresholds", payload)
        self.assertIn("actions", payload)
        self.assertEqual(len(payload["actions"]), len(ONCALL_ACTIONS))


class TestConfigHandling(unittest.TestCase):
    """配置与密钥：进得去、不出得来。"""

    def test_to_dict_never_contains_api_key(self):
        with _with_key_env("sk-super-secret-value"):
            config = build_config(_Args())
        payload = config.to_dict()
        self.assertNotIn("api_key", payload)
        self.assertNotIn("sk-super-secret-value", json.dumps(payload, ensure_ascii=False))

    def test_budget_and_cost_are_two_separate_ledgers(self):
        args = _Args()
        args.budget = 20_000
        args.cost = 0.05
        config = build_config(args)
        self.assertEqual(config.token_budget, 20_000)
        self.assertAlmostEqual(config.cost_budget, 0.05)

    def test_price_overrides_are_applied(self):
        args = _Args()
        args.price_in = 3.0
        args.price_out = 15.0
        config = build_config(args)
        self.assertAlmostEqual(config.price_in_per_mtok, 3.0)
        self.assertAlmostEqual(config.price_out_per_mtok, 15.0)

    def test_parser_accepts_global_flags_on_both_sides(self):
        parser = build_parser()
        args = parser.parse_args(["--live", "run", "任务", "--budget", "100"])
        self.assertTrue(args.live)
        self.assertEqual(args.budget, 100)
        self.assertEqual(args.task, "任务")


class TestThresholdPrecedence(unittest.TestCase):
    """阈值来源：命令行 > 环境变量 > 默认值。

    环境变量夹在中间，是为了让配置中心能下发全局档位，
    又让值班的人能在某一次运行里临时覆盖它。
    """

    def test_env_var_is_used_when_flag_absent(self):
        env = {"DRAGON_WARN_RATIO": "0.5", "DRAGON_DEGRADE_RATIO": "0.7"}
        with mock.patch.dict(os.environ, env, clear=True):
            thresholds = build_thresholds(_Args())
        self.assertAlmostEqual(thresholds.warn_ratio, 0.5)
        self.assertAlmostEqual(thresholds.degrade_ratio, 0.7)

    def test_flag_beats_env_var(self):
        args = _Args()
        args.warn_ratio = 0.3
        with mock.patch.dict(os.environ, {"DRAGON_WARN_RATIO": "0.95"}, clear=True):
            thresholds = build_thresholds(args)
        self.assertAlmostEqual(thresholds.warn_ratio, 0.3)

    def test_broken_env_var_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"DRAGON_WARN_RATIO": "不是数字"}, clear=True):
            thresholds = build_thresholds(_Args())
        self.assertAlmostEqual(thresholds.warn_ratio, CostThresholds().warn_ratio)

    def test_defaults_when_nothing_is_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            thresholds = build_thresholds(_Args())
        self.assertAlmostEqual(thresholds.warn_ratio, 0.8)
        self.assertAlmostEqual(thresholds.degrade_ratio, 0.9)
        self.assertEqual(thresholds.breaker_failures, 3)


class _Args:
    """``argparse.Namespace`` 的最小替身。"""

    live = False
    budget = None
    cost = None
    max_steps = None
    model = None
    fallback_model = None
    trace = None
    workspace = None
    price_in = None
    price_out = None
    warn_ratio = None
    degrade_ratio = None
    breaker_threshold = None
    model_rps = None
    tool_concurrency = None
    queue_capacity = None


if __name__ == "__main__":
    unittest.main()
