"""工程骨架的第一个测试：配置、异常分类、结构化日志。

约定跟 ``tests/__init__.py`` 一致：
- **全部离线**：不联网、不需要 API Key、不写用户目录。
- 断言写**期望值**，不写"跑完不崩就算过"。

这个文件守的是第二卷第 2 章立下的骨架约定：

- 配置不合法要在**启动期**早失败（``ConfigError``），不进入运行期；
- ``Config.to_dict()`` 绝不含密钥；
- 异常按四类分派，**同名类不许造分身**；
- 日志字段固定、``trace_id`` 贯穿、敏感字段脱敏。
"""
from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
import json
import logging
import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent import errors
from dragon_agent.config import Config, ConfigError
from dragon_agent.errors import (
    DependencyError,
    DragonError,
    ModelError,
    OrchestrationError,
)
from dragon_agent.logging_setup import (
    JsonFormatter,
    configure_logging,
    current_trace_id,
    get_logger,
    log_event,
    new_trace_id,
    redact,
    redact_text,
    trace_scope,
)
from dragon_agent.tools import ToolError, ToolRegistry


# ---------------------------------------------------------------------------
# 配置：早失败 + 不泄露密钥
# ---------------------------------------------------------------------------


class TestConfig(unittest.TestCase):
    def test_invalid_budget_fails_immediately(self):
        """步数 / token 预算非法时，构造阶段就抛错——不等到跑起来才发现。"""
        with self.assertRaises(ConfigError):
            Config(max_steps=0)
        with self.assertRaises(ConfigError):
            Config(token_budget=0)
        with self.assertRaises(ConfigError):
            Config(cost_budget=-0.01)

    def test_live_without_key_fails_immediately(self):
        """--live 缺 key 属于启动期错误：早失败。"""
        with self.assertRaises(ConfigError):
            Config(live=True, api_key=None)

    def test_to_dict_never_contains_secret(self):
        """配置会被写进 trace 和检查点，to_dict() 里不能有密钥。"""
        cfg = Config(api_key="sk-super-secret-value")
        dumped = cfg.to_dict()
        self.assertNotIn("api_key", dumped)
        self.assertNotIn("sk-super-secret-value", json.dumps(dumped, ensure_ascii=False))
        # 但 repr 里要能看出"有 key"，只是不能露出值
        self.assertIn("***", repr(cfg))

    def test_config_is_frozen(self):
        """配置运行中不可变；要改就重建一份。"""
        cfg = Config()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.max_steps = 99  # type: ignore[misc]
        rebuilt = dataclasses.replace(cfg, max_steps=99)
        self.assertEqual(rebuilt.max_steps, 99)
        self.assertEqual(cfg.max_steps, 12)

    def test_from_env_and_args_reads_env_and_args(self):
        """环境变量与命令行都能改配置，命令行优先。"""
        old = os.environ.get("LLM_MODEL")
        try:
            os.environ["LLM_MODEL"] = "env-model"
            cfg = Config.from_env_and_args([])
            self.assertEqual(cfg.model, "env-model")
            cfg2 = Config.from_env_and_args(["--model", "arg-model"])
            self.assertEqual(cfg2.model, "arg-model")
        finally:
            if old is None:
                os.environ.pop("LLM_MODEL", None)
            else:
                os.environ["LLM_MODEL"] = old


# ---------------------------------------------------------------------------
# 异常分类
# ---------------------------------------------------------------------------


class TestErrorTaxonomy(unittest.TestCase):
    def test_each_category_declares_kind_and_retryable(self):
        """分类与可重试性是**类属性**：按类判断和按字段判断结果一致。"""
        self.assertEqual(ModelError.kind, "model")
        self.assertEqual(OrchestrationError.kind, "orchestration")
        self.assertEqual(DependencyError.kind, "dependency")
        self.assertTrue(ModelError.retryable)
        self.assertTrue(DependencyError.retryable)
        self.assertFalse(OrchestrationError.retryable)

    def test_all_framework_errors_share_one_root(self):
        """上层只 catch DragonError 就能兜住全部框架异常。"""
        for exc in (ModelError("m"), OrchestrationError("o"), DependencyError("d")):
            self.assertIsInstance(exc, DragonError)
            self.assertIsInstance(exc, Exception)

    def test_instance_can_override_retryable(self):
        """同一类错误在不同场景下重试性不同——实例可以覆盖。"""
        exc = DependencyError("d", retryable=False)
        self.assertFalse(exc.retryable)
        self.assertIs(DependencyError.retryable, True)  # 类属性没被改

    def test_to_dict_shape(self):
        """异常的字典表示要能直接落盘。"""
        d = DependencyError("预算库不可用", details={"source": "budget-store"}).to_dict()
        self.assertEqual(d["type"], "DependencyError")
        self.assertEqual(d["kind"], "dependency")
        self.assertEqual(d["message"], "预算库不可用")
        self.assertTrue(d["retryable"])
        self.assertEqual(d["details"], {"source": "budget-store"})

    def test_tool_error_is_not_duplicated(self):
        """errors.ToolError 与 tools.ToolError 必须是**同一个类**。

        造两个同名类会让 except 只抓住一半——这是骨架期就该定死的事。
        """
        self.assertIs(errors.ToolError, ToolError)

    def test_classify_maps_every_category(self):
        """classify 把任意异常映射到分类，未知异常落到 unknown。"""
        self.assertEqual(errors.classify(ModelError("m")), "model")
        self.assertEqual(errors.classify(DependencyError("d")), "dependency")
        self.assertEqual(errors.classify(OrchestrationError("o")), "orchestration")
        self.assertEqual(errors.classify(ToolError("t")), "tool")
        self.assertEqual(errors.classify(ConfigError("c")), "config")
        self.assertEqual(errors.classify(ValueError("v")), "unknown")

    def test_policy_matches_retryability(self):
        """策略表与类的 retryable 必须一致——不能让两处各写一份。"""
        for kind in ("model", "dependency"):
            self.assertTrue(errors.POLICY[kind]["retryable"], kind)
        for kind in ("tool", "orchestration", "config"):
            self.assertFalse(errors.POLICY[kind]["retryable"], kind)
        self.assertTrue(errors.policy_for(ModelError("m"))["retryable"])
        self.assertFalse(errors.policy_for(OrchestrationError("o"))["retryable"])

    def test_tool_runtime_failure_is_a_result_not_an_exception(self):
        """工具运行期失败**不抛异常**：参数错返回 ToolResult(ok=False)。"""
        registry = ToolRegistry()

        @registry.tool(
            name="boom",
            description="总是失败的工具",
            parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
        )
        def boom(x: int) -> str:
            raise DependencyError("依赖不可用", details={"x": x})

        result = registry.invoke("boom", {"x": 1})
        self.assertFalse(result.ok)
        self.assertIn("DependencyError", result.error or "")

    def test_tool_validation_failure_does_not_raise(self):
        """参数校验失败也不抛异常。"""
        registry = ToolRegistry()

        @registry.tool(
            name="need_x",
            description="需要 x",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "required": ["x"],
            },
        )
        def need_x(x: int) -> int:
            return x

        result = registry.invoke("need_x", {})
        self.assertFalse(result.ok)
        self.assertIn("x", result.error or "")


# ---------------------------------------------------------------------------
# 结构化日志
# ---------------------------------------------------------------------------


class TestLogging(unittest.TestCase):
    def test_redact_masks_sensitive_keys(self):
        """敏感键整值替换，不留长度、不留前缀。"""
        out = redact({"api_key": "sk-abcdefgh12345", "Authorization": "Bearer xyz",
                      "nested": {"password": "p", "keep": "v"},
                      "list": [{"token": "t"}]})
        self.assertEqual(out["api_key"], "***REDACTED***")
        self.assertEqual(out["Authorization"], "***REDACTED***")
        self.assertEqual(out["nested"]["password"], "***REDACTED***")
        self.assertEqual(out["nested"]["keep"], "v")
        self.assertEqual(out["list"][0]["token"], "***REDACTED***")
        self.assertNotIn("abcdefgh", json.dumps(out, ensure_ascii=False))

    def test_redact_text_masks_key_shapes(self):
        """正文里的密钥形态也要遮——键名对不上时靠正则兜底。"""
        masked = redact_text("用 sk-abcdefgh12345 调了一下，Bearer tok-xyz98765 也不行")
        self.assertNotIn("abcdefgh12345", masked)
        self.assertNotIn("tok-xyz98765", masked)
        self.assertIn("***REDACTED***", masked)

    def test_trace_id_flows_in_scope_and_restores(self):
        """trace_id 在作用域内可见，退出后恢复原值。"""
        self.assertIsNone(current_trace_id())
        with trace_scope("trace-outer") as tid:
            self.assertEqual(tid, "trace-outer")
            self.assertEqual(current_trace_id(), "trace-outer")
            with trace_scope("trace-inner"):
                self.assertEqual(current_trace_id(), "trace-inner")
            self.assertEqual(current_trace_id(), "trace-outer")
        self.assertIsNone(current_trace_id())

    def test_new_trace_id_is_unique(self):
        ids = {new_trace_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)

    def test_json_formatter_emits_fixed_fields(self):
        """一行一条 JSON，固定字段齐全，扩展字段带上。"""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="dragon_agent.test", level=logging.INFO, pathname=__file__,
            lineno=1, msg="工具调用完成", args=(), exc_info=None,
        )
        record.trace_id = "trace-1"
        record.tool = "read_budget"
        record.api_key = "sk-leak-me-12345"
        payload = json.loads(formatter.format(record))

        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["logger"], "dragon_agent.test")
        self.assertEqual(payload["msg"], "工具调用完成")
        self.assertEqual(payload["trace_id"], "trace-1")
        self.assertEqual(payload["tool"], "read_budget")
        self.assertEqual(payload["api_key"], "***REDACTED***")
        self.assertIn("ts", payload)

    def test_json_formatter_picks_up_current_trace_id(self):
        """没显式传 trace_id 时，用作用域里的当前值。"""
        formatter = JsonFormatter()
        with trace_scope("trace-scope"):
            record = logging.LogRecord(
                name="dragon_agent.test", level=logging.INFO, pathname=__file__,
                lineno=1, msg="hello", args=(), exc_info=None,
            )
            payload = json.loads(formatter.format(record))
        self.assertEqual(payload["trace_id"], "trace-scope")

    def test_configure_logging_is_idempotent(self):
        """重复配置不能挂出两个 handler——否则日志会打印两遍。"""
        logger = configure_logging(level="INFO", stream=io.StringIO())
        configure_logging(level="INFO", stream=io.StringIO())
        self.assertEqual(len(logger.handlers), 1)
        logger.handlers.clear()

    def test_log_event_renames_reserved_fields(self):
        """扩展字段撞上 LogRecord 保留名时改名，而不是抛 KeyError。"""
        stream = io.StringIO()
        logger = configure_logging(level="INFO", stream=stream)
        try:
            log_event(logger, "tool.finished", name="read_budget", args="x")
            payload = json.loads(stream.getvalue().strip())
            self.assertEqual(payload["field_name"], "read_budget")
            self.assertEqual(payload["field_args"], "x")
        finally:
            logger.handlers.clear()


# ---------------------------------------------------------------------------
# hello-world 入口
# ---------------------------------------------------------------------------


def _load_example():
    """按文件路径加载示例脚本——它不在包里，不能直接 import。"""
    path = ROOT / "examples" / "skeleton_hello.py"
    spec = importlib.util.spec_from_file_location("skeleton_hello", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestSkeletonHello(unittest.TestCase):
    def test_example_runs_offline_and_exits_zero(self):
        """hello-world 必须离线跑通，退出码 0，trace_id 出现在输出里。"""
        module = _load_example()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.main(["--trace-id", "trace-test-001"])
        self.assertEqual(code, 0)
        self.assertIn("trace-test-001", out.getvalue())
        self.assertIn("最终答案", out.getvalue())

    def test_example_fails_fast_without_key(self):
        """--live 缺 key 时早失败，退出码 2，不是 1 也不是 0。"""
        module = _load_example()
        old = os.environ.pop("LLM_API_KEY", None)
        old2 = os.environ.pop("OPENAI_API_KEY", None)
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = module.main(["--live"])
            self.assertEqual(code, 2)
            self.assertIn("配置不合法", err.getvalue())
        finally:
            if old is not None:
                os.environ["LLM_API_KEY"] = old
            if old2 is not None:
                os.environ["OPENAI_API_KEY"] = old2

    def test_example_logs_are_json_lines_with_one_trace_id(self):
        """日志是一行一条 JSON，且整轮只有一个 trace_id。"""
        module = _load_example()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            module.main(["--trace-id", "trace-test-002"])
        lines = [l for l in err.getvalue().splitlines() if l.strip()]
        self.assertGreaterEqual(len(lines), 3)
        trace_ids = set()
        for line in lines:
            payload = json.loads(line)  # 解析失败会直接让测试挂掉
            self.assertIn("ts", payload)
            self.assertIn("level", payload)
            self.assertIn("logger", payload)
            self.assertIn("msg", payload)
            trace_ids.add(payload["trace_id"])
        self.assertEqual(trace_ids, {"trace-test-002"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
