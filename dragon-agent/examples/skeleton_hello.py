#!/usr/bin/env python3
"""hello-world：配置 → 日志 → 模型 → 工具 → 错误分类的最小闭环。

运行方式（工程根目录下）::

    python examples/skeleton_hello.py
    python examples/skeleton_hello.py --trace-id abc123    # 指定 trace_id
    python examples/skeleton_hello.py --live              # 需要 LLM_API_KEY

全程离线：用 ``MockLLM`` 按剧本返回响应，不需要 API Key，也不需要网络。
这个脚本故意只依赖 ``config`` / ``llm`` / ``tools`` / ``errors`` /
``logging_setup`` 五个模块——**编排层还没写**，这里用一个最小的
ReAct 回路（问模型 → 执行工具 → 回灌观察 → 再问）演示分层是怎么搭的。

你会看到四件事：

1. 配置不合法时**早失败**：进程直接退出，不进入运行期；
2. 每条日志都是一行 JSON，带同一个 ``trace_id``；
3. 工具抛的 ``DependencyError`` 被工具层接住，转成失败结果回灌给模型；
4. 模型层的 ``ModelError`` 按"可重试"策略退避重试，再失败才降级。
"""
from __future__ import annotations

import pathlib
import sys
import time

# 允许不安装、直接在工程根目录跑这个脚本。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.config import Config, ConfigError
from dragon_agent.errors import DependencyError, ModelError, classify, policy_for
from dragon_agent.llm import LLMResponse, MockLLM, ToolCall, Usage
from dragon_agent.logging_setup import (
    configure_logging,
    get_logger,
    log_event,
    new_trace_id,
    trace_scope,
)
from dragon_agent.tools import ToolRegistry

TASK = "查一下 2026 年的项目预算，并告诉我总额；如果查不到就说明缺哪一年"

log = get_logger("hello")


# --------------------------------------------------------------------- 工具层


def build_tools() -> ToolRegistry:
    """注册一个只读工具。查不到的那一年会抛依赖错误。"""
    registry = ToolRegistry()

    @registry.tool(
        name="read_budget",
        description="读取指定财年的预算表，返回各科目金额与总额。",
        parameters={
            "type": "object",
            "properties": {"fiscal_year": {"type": "string"}},
            "required": ["fiscal_year"],
        },
    )
    def read_budget(fiscal_year: str) -> dict:
        if fiscal_year != "2026":
            # 外部依赖不可用——该重试，不是模型的错。
            raise DependencyError(
                f"预算库 {fiscal_year} 不可用",
                details={"fiscal_year": fiscal_year, "source": "budget-store"},
            )
        return {
            "fiscal_year": fiscal_year,
            "items": {"研发": 620000, "市场": 250000, "运维": 130000},
            "total": 1_000_000,
        }

    return registry


# --------------------------------------------------------------------- 模型层


class ResilientLLM:
    """按错误策略包装模型客户端。

    这是"模型错误"这一类的处理策略落地的地方：``ModelError`` 可重试，
    所以退避重试；重试用尽才降级成一条 ``finish_reason="error"`` 的响应，
    而不是把异常抛给上层。
    """

    def __init__(self, inner, max_retries: int = 2, backoff_s: float = 0.05):
        self.inner = inner
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.attempts = 0

    def complete(self, messages, tools=None, **kwargs) -> LLMResponse:
        for attempt in range(self.max_retries + 1):
            self.attempts += 1
            try:
                return self.inner.complete(messages, tools=tools, **kwargs)
            except ModelError as exc:
                policy = policy_for(exc)
                if not policy["retryable"] or attempt == self.max_retries:
                    log_event(
                        log, "model.degraded", level=40,
                        kind=classify(exc), message=exc.message,
                        attempts=self.attempts, strategy=policy["strategy"],
                    )
                    return LLMResponse(
                        text=f"[llm] {exc.message}", finish_reason="error"
                    )
                log_event(
                    log, "model.retry", level=30,
                    kind=classify(exc), attempt=attempt + 1,
                    backoff_s=self.backoff_s,
                )
                time.sleep(self.backoff_s)
        raise AssertionError("不可达")

    def describe(self) -> str:
        return self.inner.describe()


def build_llm() -> MockLLM:
    """按剧本返回响应的假模型。

    剧本 = 模型在这个任务上会做的三次决策：
    先查 2026 年，查不到 1999 年之后如实说明，最后给出答案。
    """
    return MockLLM(script=[
        LLMResponse(
            text="先读 2026 年预算表",
            tool_calls=[ToolCall(name="read_budget", arguments={"fiscal_year": "2026"})],
            usage=Usage(input_tokens=128, output_tokens=21),
            finish_reason="tool_calls",
        ),
        LLMResponse(
            text="再确认一下 1999 年",
            tool_calls=[ToolCall(name="read_budget", arguments={"fiscal_year": "1999"})],
            usage=Usage(input_tokens=151, output_tokens=18),
            finish_reason="tool_calls",
        ),
        LLMResponse(
            text="2026 年预算总额 100 万元（研发 62 万、市场 25 万、运维 13 万）；1999 年预算库不可用，未能查到。",
            usage=Usage(input_tokens=196, output_tokens=34),
            finish_reason="stop",
        ),
    ])


# --------------------------------------------------------------------- 编排


def run_task(config: Config, llm, tools: ToolRegistry, task: str):
    """一个最小的 ReAct 回路。

    真正的编排层（第 6、10 章）会比这个完整得多：它还有状态、检查点、
    预算守卫和停止准则。但**形状**已经在这里了——
    编排层决定"下一步做什么"，模型层只管"一次调用"，工具层只管"一次执行"。
    """
    messages = [{"role": "user", "content": task}]
    transcript: list[tuple[str, bool, str]] = []

    for step in range(config.max_steps):
        resp = llm.complete(messages, tools=tools.schemas())
        messages.append({"role": "assistant", "content": resp.text})
        if not resp.has_tool_calls:
            return resp.text, transcript, step + 1

        for call in resp.tool_calls:
            result = tools.invoke(call.name, call.arguments)
            observation = result.render()
            transcript.append((call.name, result.ok, observation))
            log_event(
                log, "tool.finished",
                tool=call.name, ok=result.ok,
                duration_ms=result.duration_ms,
                observation=observation[:120],
            )
            # 失败也回灌——模型只有看到错误才有机会改对。
            messages.append(
                {"role": "user", "content": f"工具 {call.name} 返回：{observation}"}
            )
    return "（达到步数上限，没能给出最终答案）", transcript, config.max_steps


# --------------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 1) 配置：早失败。配置不合法就别进运行期。
    try:
        config = Config.from_env_and_args(argv)
    except ConfigError as exc:
        print(f"配置不合法，已退出：{exc}", file=sys.stderr)
        return 2

    # 2) 日志：一行一条 JSON。
    configure_logging(level="INFO")

    trace_id = new_trace_id()
    for i, a in enumerate(argv):
        if a == "--trace-id" and i + 1 < len(argv):
            trace_id = argv[i + 1]

    with trace_scope(trace_id) as tid:
        log_event(
            log, "run.start",
            task=TASK, model=config.model, live=config.live,
            max_steps=config.max_steps, token_budget=config.token_budget,
        )

        tools = build_tools()
        llm = ResilientLLM(build_llm())

        answer, transcript, steps = run_task(config, llm, tools, TASK)

        log_event(log, "run.finished", steps=steps, tool_calls=len(transcript))

    print("=" * 72)
    print(f"trace_id：{tid}")
    print(f"任务    ：{TASK}")
    print(f"模型    ：{llm.describe()}（尝试 {llm.attempts} 次）")
    print("=" * 72)

    print("\n--- 工具调用 " + "-" * 58)
    for name, ok, observation in transcript:
        flag = "成功" if ok else "失败"
        print(f"  [{flag}] {name} → {observation}")
    print("-" * 72)

    print("\n最终答案：")
    print(answer)
    print("-" * 72)
    print("\n提示：上面 stderr 的每一行都是一条 JSON 日志，")
    print("      用同一个 trace_id 就能把这次运行的日志串起来。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
