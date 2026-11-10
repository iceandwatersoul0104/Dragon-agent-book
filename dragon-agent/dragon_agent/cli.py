"""命令行入口：跑任务、交互、列工具、跑评测、看 trace、查值班手册。

第二卷第 14 章的落地文件。它站在依赖图的**最上层**：可以读
``config`` / ``llm`` / ``tools`` / ``agent`` / ``orchestration`` /
``observability``，下层不知道它存在。

它同时是本章"上线三件套"的落点：

1. **成本阈值**（:class:`CostThresholds`）——告警、降级、熔断三档线；
2. **值班动作**（:data:`ONCALL_ACTIONS`）——信号 → 阈值 → 处置动作 → 升级路径；
3. **运行期护栏**——模型侧限流、工具侧限流、队列背压、自动熔断、降级模型。

四条纪律：

- **缺 API Key 早失败**：``--live`` 而没有 key，打印提示并以退出码 3 结束，
  不进运行期。静默失败比报错贵得多——报错还能改，静默失败只会烧钱。
- **mock 模式跑通全回路**：不设 key 也能跑完 ``run`` / ``chat`` / ``tools`` /
  ``eval`` / ``demo`` / ``runbook``，退出码 0。
- **退出码有语义**：0 正常、2 用法错、3 配置错、4 任务失败、5 熔断或预算耗尽、
  6 评测回归。CI 与值班脚本靠退出码判断，不用去解析输出。
- **预算记两笔账**：token 预算与美元预算是两件事——``--budget`` 管前者，
  ``--cost`` 管后者，告警线各按各的上限算比例。

子命令::

    run "<任务>"    跑一次完整回路
    chat            交互式（多轮）
    tools           列出可用工具
    eval            跑评测集
    demo            端到端演示（上线护栏全展示）
    trace <run_id>  看某次运行的记录
    runbook         打印值班手册：阈值、处置动作、升级路径
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .agent import Agent, AgentResult
from .config import Config, ConfigError
from .context import ContextAssembler
from .llm import LLMResponse, MockLLM, ToolCall, Usage, build_client
from .state import StateStore
from .tools import ToolRegistry, default_registry

__all__ = [
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_CONFIG",
    "EXIT_FAILED",
    "EXIT_BREAKER",
    "EXIT_REGRESSION",
    "CostThresholds",
    "OnCallAction",
    "ONCALL_ACTIONS",
    "TokenBucket",
    "CircuitBreaker",
    "BudgetWatcher",
    "ToolRateLimiter",
    "BoundedQueue",
    "FallbackLLMClient",
    "build_parser",
    "main",
]


# --------------------------------------------------------------------------- 退出码

#: 一切正常。
EXIT_OK = 0
#: 用法错（参数缺失、子命令不存在）。与 argparse 的退出码一致。
EXIT_USAGE = 2
#: 配置错——最常见的是 ``--live`` 但没有 API Key。**启动期早失败。**
EXIT_CONFIG = 3
#: 任务跑完了但没成功（error / max_steps / stopped_no_progress）。
EXIT_FAILED = 4
#: 熔断打开或预算耗尽——**主动停下**，不是失败。
EXIT_BREAKER = 5
#: 评测出现回归（有用例挂了，或相对基线退步）。
EXIT_REGRESSION = 6


# --------------------------------------------------------------------------- 成本阈值


@dataclass(frozen=True)
class CostThresholds:
    """上线三档线 + 限流参数。

    这三档线是**成本护栏的全部配置**，放在一个不可变的 dataclass 里，
    好处是：它可以整体替换、可以打印、可以被测试断言，而且不会在运行期
    被某个模块偷偷改掉。

    - ``warn_ratio``：成本花掉预算的这个比例时**告警**；
    - ``degrade_ratio`：花到这个比例时**降级模型**（换便宜的），
      同时是自动熔断的触发线；
    - ``breaker_failures``：连续这么多次失败后**熔断**打开。
    """

    warn_ratio: float = 0.8
    degrade_ratio: float = 0.9
    breaker_failures: int = 3
    breaker_cooldown_s: float = 30.0

    #: 模型侧限流：每秒允许的模型请求数，以及允许的突发量。
    model_rps: float = 5.0
    model_burst: int = 10

    #: 工具侧限流：全局同时在飞的工具调用上限。
    tool_concurrency: int = 4

    #: 队列容量与最长等待时间——满了就是背压。
    queue_capacity: int = 32
    queue_wait_s: float = 5.0

    def __post_init__(self) -> None:
        for name in ("warn_ratio", "degrade_ratio"):
            value = float(getattr(self, name))
            if not 0 < value <= 1:
                raise ValueError(f"{name} 必须在 (0, 1] 之间，收到 {value}")
        if self.degrade_ratio < self.warn_ratio:
            raise ValueError(
                f"degrade_ratio（{self.degrade_ratio}）不能小于 "
                f"warn_ratio（{self.warn_ratio}）"
            )
        if self.breaker_failures < 1:
            raise ValueError(f"breaker_failures 必须 ≥1，收到 {self.breaker_failures}")
        if self.model_rps <= 0:
            raise ValueError(f"model_rps 必须 >0，收到 {self.model_rps}")
        if self.tool_concurrency < 1:
            raise ValueError(f"tool_concurrency 必须 ≥1，收到 {self.tool_concurrency}")
        if self.queue_capacity < 1:
            raise ValueError(f"queue_capacity 必须 ≥1，收到 {self.queue_capacity}")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------- 值班动作


@dataclass(frozen=True)
class OnCallAction:
    """一条值班条目：看到什么信号 → 做什么动作 → 什么时候升级。"""

    signal: str
    threshold: str
    action: str
    escalate: str

    def to_dict(self) -> dict[str, str]:
        return {
            "signal": self.signal,
            "threshold": self.threshold,
            "action": self.action,
            "escalate": self.escalate,
        }


#: 值班手册的机器可读版本。完整手册见本章 README（``vol2/p14-production/README.md``）。
#: 这里只放**可执行的那部分**：信号、阈值、动作、升级路径。
ONCALL_ACTIONS: tuple[OnCallAction, ...] = (
    OnCallAction(
        signal="模型侧限流（HTTP 429）",
        threshold="429 比例 > 5%（5 分钟窗口）",
        action="CLI 自动降级到 --fallback-model；核对上游配额与并发设置",
        escalate="10 分钟未恢复 → 通知模型服务负责人",
    ),
    OnCallAction(
        signal="模型侧服务端错误（5xx）",
        threshold="5xx 比例 > 2%（5 分钟窗口）",
        action="熔断器打开后自动拒绝新请求；检查上游状态页与最近一次模型变更",
        escalate="5 分钟未恢复 → 升级到平台值班",
    ),
    OnCallAction(
        signal="工具错误率上升",
        threshold="单个工具错误率 > 10%（10 分钟窗口）",
        action="查该工具的下游依赖与最近变更；必要时在护栏里禁用它",
        escalate="影响主链路 → 升级到工具 owner",
    ),
    OnCallAction(
        signal="任务成功率下滑",
        threshold="5 分钟成功率 < 95%",
        action="冻结发布；回滚最近的模型或提示词变更；用回放定位差异",
        escalate="30 分钟未恢复 → 升级到工程负责人",
    ),
    OnCallAction(
        signal="成本超预算",
        threshold="单任务成本 > 预算的 80%",
        action="预算告警；收紧 --budget / --cost；考虑降级模型或缩短 max-steps",
        escalate="超过 100% → 熔断该任务并复盘调用链",
    ),
    OnCallAction(
        signal="无进展任务变多",
        threshold="无进展任务占比 > 5%",
        action="看 trace 里连续重复的动作；收紧 max-steps 或检查工具返回值",
        escalate="持续 1 小时 → 升级到工程负责人",
    ),
    OnCallAction(
        signal="上下文超限",
        threshold="出现 context_length_exceeded",
        action="检查上下文装配预算；确认检索层没有塞进超长片段",
        escalate="反复出现 → 升级到工程负责人",
    ),
    OnCallAction(
        signal="容器异常重启",
        threshold="5 分钟内重启 > 3 次",
        action="看容器日志与 OOM 事件；回滚到上一个镜像 tag",
        escalate="15 分钟未恢复 → 升级到平台值班",
    ),
)


# --------------------------------------------------------------------------- 限流：令牌桶


class TokenBucket:
    """模型侧限流：令牌桶。

    令牌桶比固定窗口滑窗好用的地方在于**允许突发**：平时按 ``rate_per_s``
    匀速发牌，攒下的令牌可以在瞬时打满 ``burst`` 个请求——Agent 的真实
    流量就是这种形状（模型突然要连调三次工具）。

    ``clock`` 与 ``sleep`` 可以注入，测试因此完全确定，不用真的等。
    """

    def __init__(
        self,
        rate_per_s: float,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_s <= 0:
            raise ValueError(f"rate_per_s 必须 >0，收到 {rate_per_s}")
        self.rate = float(rate_per_s)
        self.capacity = float(max(1, int(burst)))
        self.tokens = self.capacity
        self.granted = 0
        self.rejected = 0
        self._clock = clock
        self._sleep = sleep
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        delta = max(0.0, now - self._last)
        self._last = now
        self.tokens = min(self.capacity, self.tokens + delta * self.rate)

    def acquire(self, tokens: int = 1, timeout_s: float | None = None) -> bool:
        """取令牌。取到返回 True，超时没取到返回 False（**不抛异常**）。

        限流器不是护栏——它只是把请求压慢。压慢失败不该让任务崩掉，
        调用方拿到 False 之后自己决定是排队还是放弃。
        """
        need = float(max(1, int(tokens)))
        deadline = None if timeout_s is None else self._clock() + float(timeout_s)
        # 上限保护：万一注入的 sleep 不推进时钟，也不至于死循环。
        for _ in range(10_000):
            self._refill()
            if self.tokens >= need:
                self.tokens -= need
                self.granted += 1
                return True
            wait = (need - self.tokens) / self.rate
            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    self.rejected += 1
                    return False
                wait = min(wait, remaining)
            self._sleep(max(0.0, wait))
        self.rejected += 1
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rate_per_s": self.rate,
            "capacity": self.capacity,
            "available": round(self.tokens, 3),
            "granted": self.granted,
            "rejected": self.rejected,
        }


# --------------------------------------------------------------------------- 熔断


class CircuitBreaker:
    """自动熔断：连续失败到顶就停止打上游。

    三种状态：

    - ``closed``：正常放行；
    - ``open``：全部拒绝，直到冷却时间过去；
    - ``half_open``：放**一个**探测请求过去——成功就闭合，失败就重新打开。

    为什么要有 ``half_open``：一直 ``open`` 的话，上游恢复了也没人知道，
    只能等人手动介入。多放一个探测请求，等于让熔断器自己完成"试探—恢复"。
    """

    def __init__(
        self,
        threshold: int = 3,
        cooldown_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if threshold < 1:
            raise ValueError(f"threshold 必须 ≥1，收到 {threshold}")
        self.threshold = int(threshold)
        self.cooldown_s = float(cooldown_s)
        self.state = "closed"
        self.failures = 0
        self.trips = 0
        self.rejected = 0
        self._clock = clock
        self._opened_at = 0.0

    def allow(self) -> bool:
        """现在放不放行。"""
        if self.state == "closed":
            return True
        if self.state == "open":
            if self._clock() - self._opened_at >= self.cooldown_s:
                self.state = "half_open"
                return True
            self.rejected += 1
            return False
        # half_open：放一个探测请求。
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.state = "closed"

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.threshold:
            self.state = "open"
            self._opened_at = self._clock()
            self.trips += 1
            self.failures = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "threshold": self.threshold,
            "failures": self.failures,
            "trips": self.trips,
            "rejected": self.rejected,
        }


# --------------------------------------------------------------------------- 预算告警


class BudgetWatcher:
    """预算告警：按比例给档位，**每档只报一次**。

    每档只报一次是刻意的：告警每一步都刷屏，等于没有告警——值班的人
    会把它静音，然后真正的那次也看不见了。
    """

    def __init__(
        self,
        cost_budget: float = 0.0,
        token_budget: int = 0,
        thresholds: CostThresholds | None = None,
    ) -> None:
        self.cost_budget = float(cost_budget or 0.0)
        self.token_budget = int(token_budget or 0)
        self.thresholds = thresholds or CostThresholds()
        self.alerts: list[str] = []
        self._fired: set[str] = set()

    def _level(self, ratio: float) -> str:
        if ratio >= self.thresholds.degrade_ratio:
            return "critical"
        if ratio >= self.thresholds.warn_ratio:
            return "warn"
        return ""

    def observe(self, cost: float, tokens: int = 0) -> list[str]:
        """看一眼当前消耗，返回本次触发的告警文本（没触发就是空列表）。"""
        fired: list[str] = []
        if self.cost_budget > 0:
            ratio = float(cost) / self.cost_budget
            level = self._level(ratio)
            if level and f"cost:{level}" not in self._fired:
                self._fired.add(f"cost:{level}")
                text = (f"[预算告警] 成本已达预算的 {ratio:.0%}"
                        f"（{float(cost):.6f} / {self.cost_budget:.6f} 美元，档位 {level}）")
                self.alerts.append(text)
                fired.append(text)
        if self.token_budget > 0:
            ratio = float(tokens) / self.token_budget
            level = self._level(ratio)
            if level and f"token:{level}" not in self._fired:
                self._fired.add(f"token:{level}")
                text = (f"[预算告警] token 已用预算的 {ratio:.0%}"
                        f"（{int(tokens)} / {self.token_budget}，档位 {level}）")
                self.alerts.append(text)
                fired.append(text)
        return fired

    def to_dict(self) -> dict[str, Any]:
        return {
            "cost_budget": self.cost_budget,
            "token_budget": self.token_budget,
            "alerts": list(self.alerts),
        }


# --------------------------------------------------------------------------- 工具侧限流


class ToolRateLimiter:
    """工具侧限流：每个工具一把令牌桶，外加一把全局并发闸。

    为什么工具要单独限流：模型侧的限流保护的是**上游供应商**，工具侧的
    限流保护的是**自己的下游**（数据库、第三方 API、内部服务）。两边
    的故障互不相干——模型侧好好的，工具连不上，照样能把任务打穿。
    """

    def __init__(
        self,
        rates: dict[str, tuple[float, int]] | None = None,
        max_concurrency: int = 4,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency 必须 ≥1，收到 {max_concurrency}")
        self.max_concurrency = int(max_concurrency)
        self.buckets: dict[str, TokenBucket] = {
            name: TokenBucket(rate, burst, clock, sleep)
            for name, (rate, burst) in (rates or {}).items()
        }
        self.blocked = 0
        self.in_flight = 0
        self._lock = threading.Lock()

    def acquire(self, name: str, timeout_s: float = 0.0) -> bool:
        """占一个位置。拿不到返回 False。"""
        bucket = self.buckets.get(name)
        if bucket is not None and not bucket.acquire(1, timeout_s=timeout_s):
            self.blocked += 1
            return False
        with self._lock:
            if self.in_flight >= self.max_concurrency:
                self.blocked += 1
                return False
            self.in_flight += 1
        return True

    def release(self, name: str = "") -> None:
        with self._lock:
            if self.in_flight > 0:
                self.in_flight -= 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_concurrency": self.max_concurrency,
            "in_flight": self.in_flight,
            "blocked": self.blocked,
            "per_tool": {name: bucket.to_dict() for name, bucket in self.buckets.items()},
        }


# --------------------------------------------------------------------------- 队列与背压


class BoundedQueue:
    """有界队列：满了就是背压。

    背压有两种处理：**等**（把压力顶回上游）或**丢**（保主链路）。
    这里两种都支持——``put`` 默认等 ``wait_s`` 秒，等不到就丢并计数。

    丢弃数必须被观测到：被丢掉的请求如果没人知道，就等于凭空消失的任务。
    """

    def __init__(self, capacity: int = 32, wait_s: float = 5.0) -> None:
        if capacity < 1:
            raise ValueError(f"capacity 必须 ≥1，收到 {capacity}")
        self.capacity = int(capacity)
        self.wait_s = float(wait_s)
        self.dropped = 0
        self.waited = 0
        self._items: deque[Any] = deque()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

    def put(self, item: Any) -> bool:
        """入队。成功 True；等不到空间而丢弃返回 False。"""
        with self._cv:
            if len(self._items) >= self.capacity:
                self.waited += 1
                if not self._cv.wait(self.wait_s):
                    self.dropped += 1
                    return False
            self._items.append(item)
            self._cv.notify_all()
            return True

    def get(self, timeout: float = 1.0) -> Any:
        """出队。超时返回 None。"""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cv:
            while not self._items:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)
            return self._items.popleft()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "depth": len(self),
            "dropped": self.dropped,
            "waited": self.waited,
        }


# --------------------------------------------------------------------------- 降级模型


class FallbackLLMClient:
    """降级模型 + 熔断：主模型出可重试故障时切到备用模型。

    两条规则：

    1. **切换是粘的**——一旦降级，本次运行剩下的调用全走备用模型。
       半路切回会让同一次任务的前后答案来自不同模型，比一直用备用模型
       更难解释。
    2. **只有可重试故障才降级**——参数错、上下文超长这类故障换模型也
       解决不了，降级只会把一个明确的错误变成一个模糊的错误。
    """

    def __init__(self, primary: Any, fallback: Any = None,
                 breaker: CircuitBreaker | None = None) -> None:
        self.primary = primary
        self.fallback = fallback
        self.breaker = breaker or CircuitBreaker()
        self.degraded = False
        self.switches = 0

    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None = None,
                 **kwargs: Any) -> LLMResponse:
        if not self.breaker.allow():
            return LLMResponse(text="[llm] 熔断打开，已拒绝本次调用", finish_reason="error")

        client = self.fallback if self.degraded else self.primary
        response = client.complete(messages, tools=tools, **kwargs)
        failure = self._classify(response)
        if failure == "none":
            self.breaker.record_success()
            return response

        self.breaker.record_failure()
        if not self.degraded and self.fallback is not None and self._retryable(failure):
            self.degraded = True
            self.switches += 1
            fallback_response = self.fallback.complete(messages, tools=tools, **kwargs)
            if self._classify(fallback_response) == "none":
                self.breaker.record_success()
            return fallback_response
        return response

    @staticmethod
    def _retryable(failure: str) -> bool:
        """只有**可重试**的故障才降级。

        参数错、上下文超长这类故障换模型也解决不了——降级只会把一个明确的
        错误变成一个模糊的错误，还顺手把备份配额烧掉。
        """
        try:
            from .llm_policy import RETRYABLE_FAILURES

            return failure in RETRYABLE_FAILURES
        except Exception:  # pragma: no cover - llm_policy 缺失时的兜底
            return failure in ("rate_limited", "server_error", "timeout", "transport")

    @staticmethod
    def _classify(response: Any) -> str:
        try:
            from .llm_policy import classify_failure

            return classify_failure(response)
        except Exception:  # pragma: no cover - llm_policy 缺失时的兜底
            return "none" if getattr(response, "finish_reason", "") != "error" else "unknown"

    def describe(self) -> str:
        primary = self.primary.describe() if hasattr(self.primary, "describe") else "primary"
        if self.fallback is None:
            return str(primary)
        fallback = self.fallback.describe() if hasattr(self.fallback, "describe") else "fallback"
        return f"{primary} → {fallback}" + ("（已降级）" if self.degraded else "")


# --------------------------------------------------------------------------- mock 剧本


def mock_script(task: str) -> list[LLMResponse]:
    """mock 模式下的固定剧本：先调一次工具，再给出答案。

    剧本是**确定的**：同样的任务永远得到同样的轨迹。这样 mock 模式就不只是
    "跑完不崩"，还能用来验证控制流没被改坏——回归测试要的就是这个。
    """
    return [
        LLMResponse(
            text="先做一次工具调用，确认执行链路是通的。",
            tool_calls=[ToolCall(name="add", arguments={"a": 1, "b": 2})],
            usage=Usage(input_tokens=180, output_tokens=24),
            finish_reason="tool_calls",
        ),
        LLMResponse(
            text=(f"[mock] 任务「{task}」已处理：工具 add 返回 3。"
                  "这是 mock 回放；加 --live 并设置 LLM_API_KEY 后由真实模型作答。"),
            usage=Usage(input_tokens=260, output_tokens=48),
            finish_reason="stop",
        ),
    ]


# --------------------------------------------------------------------------- 评测用工具


def build_eval_registry() -> ToolRegistry:
    """评测用的工具集：形状与 ``default_registry`` 一致，数据是确定性的假数据。

    工具返回**固定**的值——评测要的是"答案里有没有那些关键事实"，
    工具本身必须每次都一样，否则测的是随机数。
    """
    registry = ToolRegistry()

    @registry.tool(
        name="read_budget",
        description="读取项目的预算表，返回各科目的预算金额。",
        parameters={
            "type": "object",
            "properties": {"fiscal_year": {"type": "string"}},
            "required": ["fiscal_year"],
        },
    )
    def read_budget(fiscal_year: str) -> dict[str, Any]:
        return {
            "fiscal_year": fiscal_year,
            "items": {"研发": 620000, "市场": 250000, "运维": 130000},
            "total": 1000000,
        }

    @registry.tool(
        name="echo",
        description="原样返回一段文本。",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    def echo(text: str) -> str:
        return text

    return registry


def build_script_llm(script: list[dict[str, Any]]) -> MockLLM:
    """把评测用例里的剧本转成假模型客户端。

    剧本条目有两种形态：带 ``tool`` 的是工具调用，不带的是最终回答。
    """
    responses: list[LLMResponse] = []
    for item in script or []:
        if item.get("tool"):
            responses.append(LLMResponse(
                text=str(item.get("text") or ""),
                tool_calls=[ToolCall(name=str(item["tool"]),
                                     arguments=dict(item.get("args") or {}))],
                usage=Usage(int(item.get("in_tokens") or 120),
                           int(item.get("out_tokens") or 20)),
                finish_reason="tool_calls",
            ))
        else:
            responses.append(LLMResponse(
                text=str(item.get("text") or ""),
                usage=Usage(int(item.get("in_tokens") or 200),
                           int(item.get("out_tokens") or 40)),
                finish_reason="stop",
            ))
    return MockLLM(script=responses)


# --------------------------------------------------------------------------- 装配


#: 阈值的环境变量名。配置文件中心（ConfigMap / 配置中心）把它下发成环境变量，
#: 命令行开关再覆盖它。优先级：**命令行 > 环境变量 > 内置默认值**。
THRESHOLD_ENV_VARS: dict[str, str] = {
    "warn_ratio": "DRAGON_WARN_RATIO",
    "degrade_ratio": "DRAGON_DEGRADE_RATIO",
    "breaker_failures": "DRAGON_BREAKER_THRESHOLD",
    "model_rps": "DRAGON_MODEL_RPS",
    "tool_concurrency": "DRAGON_TOOL_CONCURRENCY",
    "queue_capacity": "DRAGON_QUEUE_CAPACITY",
}


def _env_number(name: str, default: float) -> float:
    """从环境变量读一个数。读不到或读坏了就用默认值。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def build_thresholds(args: argparse.Namespace) -> CostThresholds:
    """从命令行参数与环境变量拼阈值。

    顺序是刻意的：**命令行 > 环境变量 > 内置默认值**。环境变量放在中间，
    是为了让它既能承载"配置中心下发的全局档位"，又能让值班的人临时
    用命令行覆盖某一次运行。
    """
    defaults = CostThresholds()

    def pick(flag: str, field: str) -> float:
        from_flag = getattr(args, flag, None)
        if from_flag is not None:
            return float(from_flag)
        env_name = THRESHOLD_ENV_VARS[field]
        return _env_number(env_name, float(getattr(defaults, field)))

    return CostThresholds(
        warn_ratio=pick("warn_ratio", "warn_ratio"),
        degrade_ratio=pick("degrade_ratio", "degrade_ratio"),
        breaker_failures=int(pick("breaker_threshold", "breaker_failures")),
        model_rps=pick("model_rps", "model_rps"),
        tool_concurrency=int(pick("tool_concurrency", "tool_concurrency")),
        queue_capacity=int(pick("queue_capacity", "queue_capacity")),
    )


def build_config(args: argparse.Namespace) -> Config:
    """按参数造配置。**唯一入口仍然是** ``Config.from_env_and_args``。

    这里只做两件事：把命令行参数翻译成它的参数列表，以及把单价覆盖上去
    （``Config`` 是 frozen 的，改字段只能整体重建）。
    """
    flags: list[str] = []
    if getattr(args, "live", False):
        flags.append("--live")
    for name, flag in (("budget", "--budget"), ("cost", "--cost"),
                       ("max_steps", "--max-steps"), ("model", "--model"),
                       ("trace", "--trace"), ("workspace", "--workspace")):
        value = getattr(args, name, None)
        if value is not None:
            flags.append(f"{flag}={value}")

    config = Config.from_env_and_args(flags)

    overrides: dict[str, Any] = {}
    price_in = getattr(args, "price_in", None)
    price_out = getattr(args, "price_out", None)
    if price_in is not None:
        overrides["price_in_per_mtok"] = float(price_in)
    if price_out is not None:
        overrides["price_out_per_mtok"] = float(price_out)
    if overrides:
        config = dataclasses.replace(config, **overrides)
    return config


def build_llm(config: Config, task: str, thresholds: CostThresholds,
              fallback_model: str | None = None) -> Any:
    """按配置选模型客户端。

    mock 模式走固定剧本；live 模式走真实 API，并套上**重试**与**降级**两层：
    先在主模型上按 ``llm_policy`` 的退避策略重试，重试用尽仍失败才降级。
    """
    if not config.live:
        return MockLLM(script=mock_script(task), name=config.model)

    from .llm_policy import ResilientLLMClient, RetryPolicy

    primary = ResilientLLMClient(build_client(config), policy=RetryPolicy(max_attempts=3))
    if not fallback_model:
        return primary

    fallback_config = dataclasses.replace(config, model=fallback_model)
    fallback = ResilientLLMClient(build_client(fallback_config),
                                   policy=RetryPolicy(max_attempts=2))
    return FallbackLLMClient(
        primary=primary,
        fallback=fallback,
        breaker=CircuitBreaker(threshold=thresholds.breaker_failures,
                               cooldown_s=thresholds.breaker_cooldown_s),
    )


def build_stack(config: Config, task: str, thresholds: CostThresholds,
                fallback_model: str | None = None,
                registry: ToolRegistry | None = None,
                llm: Any = None,
                tracer: Any = None,
                meter: Any = None) -> tuple[Agent, ToolRegistry, StateStore]:
    """装配「配置 + 模型 + 工具 + 上下文 + 状态 + Agent」这条链。"""
    registry = registry if registry is not None else default_registry()
    if llm is None:
        llm = build_llm(config, task, thresholds, fallback_model)
    if tracer is not None and meter is None:
        meter = tracer.meter
    agent = Agent(
        config=config,
        llm=llm,
        tools=registry,
        context=ContextAssembler(config=config),
        state=StateStore(config=config),
        tracer=tracer,
        meter=meter,
    )
    if tracer is not None:
        tracer.attach(agent)
    return agent, registry, agent.state


def build_tracer(config: Config, trace_dir: Path) -> Any:
    """按配置造一个 tracer，span 落到 ``trace_dir/spans.jsonl``。"""
    from .observability import JsonlExporter, Tracer

    trace_dir.mkdir(parents=True, exist_ok=True)
    return Tracer(config=config, exporter=JsonlExporter(trace_dir / "spans.jsonl"))


def trace_dir_of(config: Config, explicit: str | None = None) -> Path:
    """trace 目录。``--trace`` 给的是目录，不是文件。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    if config.trace_path:
        return Path(config.trace_path)
    return Path.cwd() / "traces"


def write_run_record(trace_dir: Path, run_id: str, record: dict[str, Any]) -> Path:
    """把一次运行的记录落盘，供 ``trace <run_id>`` 回看。"""
    runs = trace_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"{run_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def status_exit_code(result: AgentResult) -> int:
    """任务结果 → 退出码。"""
    status = result.status
    if status == "completed":
        return EXIT_OK
    if status == "stopped_budget":
        return EXIT_BREAKER
    return EXIT_FAILED


# --------------------------------------------------------------------------- 输出helpers


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _rule(title: str = "", width: int = 72) -> None:
    if title:
        _out(f"--- {title} " + "-" * max(0, width - len(title) - 5))
    else:
        _out("-" * width)


def _print_result(result: AgentResult, verbose: bool = True) -> None:
    """打印一次任务的结果。"""
    if verbose:
        _out("\n轨迹：")
        for step in result.steps:
            action = "（无动作）"
            if step.action is not None:
                action = f"{step.action.name}({step.action.arguments})"
            _out(f"  [第 {step.index} 步] {action}")
            if step.thought:
                _out(f"      思考：{step.thought}")
            if step.observation:
                _out(f"      观察：{step.observation}")
    _out("\n最终答案：")
    _out(result.answer or "（空）")
    _rule()
    _out(f"步数       ：{len(result.steps)}")
    _out(f"token 合计：{result.total_tokens}")
    _out(f"成本合计   ：{result.total_cost:.6f} 美元")
    _out(f"最终状态   ：{result.status}")
    _out(f"停止原因   ：{result.stop_reason}")
    _rule()


# --------------------------------------------------------------------------- 子命令：run


def cmd_run(args: argparse.Namespace) -> int:
    """跑一次完整回路。"""
    task = args.task
    thresholds = build_thresholds(args)
    config = build_config(args)
    watcher = BudgetWatcher(cost_budget=config.cost_budget,
                             token_budget=config.token_budget,
                             thresholds=thresholds)

    _out("=" * 72)
    _out(f"任务：{task}")
    _out(f"模型：{config.model}{'（live）' if config.live else '（mock）'}")
    _out(f"预算：步数 {config.max_steps} / token {config.token_budget} / "
         f"成本 {config.cost_budget:.6f} 美元")
    _out(f"阈值：告警 {thresholds.warn_ratio:.0%} / 降级 {thresholds.degrade_ratio:.0%} / "
         f"熔断 {thresholds.breaker_failures} 次")
    _out("=" * 72)

    tracer = None
    trace_dir = None
    if getattr(args, "trace", None):
        trace_dir = trace_dir_of(config, args.trace)
        tracer = build_tracer(config, trace_dir)

    agent, _registry, state = build_stack(
        config, task, thresholds,
        fallback_model=getattr(args, "fallback_model", None),
        tracer=tracer,
    )
    result = agent.run(task)

    for alert in watcher.observe(result.total_cost, result.total_tokens):
        _err(alert)

    _print_result(result)

    if tracer is not None and trace_dir is not None:
        tracer.export()
        run_id = state.snapshot().task_id
        record = {
            "run_id": run_id,
            "task": task,
            "status": result.status,
            "stop_reason": result.stop_reason,
            "answer": result.answer,
            "total_tokens": result.total_tokens,
            "total_cost_usd": result.total_cost,
            "steps": len(result.steps),
            "sli": tracer.sli(),
            "config": config.to_dict(),
        }
        path = write_run_record(trace_dir, run_id, record)
        _out(f"trace 已写入：{path}")

    return status_exit_code(result)


# --------------------------------------------------------------------------- 子命令：chat


def cmd_chat(args: argparse.Namespace) -> int:
    """交互式对话。

    本工程的 ``Agent.run()`` 是**单任务接口**：一次跑一个任务，没有会话状态。
    所以这里的"多轮"是把历史拼进任务文本——这是 CLI 层的补偿，不是引擎能力。

    接入有会话状态的编排层（第 10 章）之后，应当换成那个接口，而不是
    继续在 CLI 里拼字符串。
    """
    thresholds = build_thresholds(args)
    config = build_config(args)
    fallback_model = getattr(args, "fallback_model", None)

    _out("=" * 72)
    _out("dragon-agent 交互模式。输入任务，Ctrl-D 或 /exit 退出。")
    _out("可用指令：/help /cost /budget /exit")
    _out("=" * 72)

    history: list[str] = []
    total_cost = 0.0
    total_tokens = 0
    turns = 0

    while True:
        try:
            line = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            _out("")
            break
        if not line:
            continue
        if line in ("/exit", "/quit", "exit", "quit", "退出"):
            break
        if line == "/help":
            _out("可用指令：/help /cost /budget /exit")
            continue
        if line == "/cost":
            _out(f"累计成本 {total_cost:.6f} 美元 / 累计 token {total_tokens} / 轮次 {turns}")
            continue
        if line == "/budget":
            _out(f"单任务预算：token {config.token_budget} / 成本 {config.cost_budget:.6f} 美元 / "
                 f"步数 {config.max_steps}")
            continue

        task = line
        if history:
            recent = "\n".join(history[-6:])
            task = f"对话上下文：\n{recent}\n\n本轮任务：{line}"

        agent, _registry, _state = build_stack(
            config, task, thresholds, fallback_model=fallback_model,
        )
        result = agent.run(task)
        turns += 1
        total_cost += result.total_cost
        total_tokens += result.total_tokens

        _out(f"助手> {result.answer or '（空）'}")
        _out(f"      状态 {result.status} / 步数 {len(result.steps)} / "
             f"token {result.total_tokens} / 成本 {result.total_cost:.6f} 美元")
        history.append(f"用户：{line}")
        history.append(f"助手：{result.answer}")

    _out(f"\n共 {turns} 轮，累计 token {total_tokens}，累计成本 {total_cost:.6f} 美元。")
    return EXIT_OK


# --------------------------------------------------------------------------- 子命令：tools


def cmd_tools(args: argparse.Namespace) -> int:
    """列出可用工具。"""
    registry = default_registry()
    if getattr(args, "json", False):
        payload = []
        for spec in registry.schemas():
            payload.append(spec)
        _out(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    _out("=" * 72)
    _out(f"可用工具（{len(registry.names)} 个）：")
    _out("=" * 72)
    for name in registry.names:
        spec, _fn = registry.get(name)
        flags = []
        if spec.side_effect:
            flags.append("有副作用")
        if not spec.replayable:
            flags.append("不可重放")
        suffix = f"  [{'、'.join(flags)}]" if flags else ""
        _out(f"\n{spec.name}{suffix}")
        _out(f"  说明：{spec.description}")
        props = (spec.parameters or {}).get("properties") or {}
        required = set((spec.parameters or {}).get("required") or [])
        for param, schema in props.items():
            mark = "*" if param in required else ""
            _out(f"  参数：{param}{mark}（{schema.get('type', 'any')}）")
    _out("")
    _out("带 * 的是必填参数。")
    return EXIT_OK


# --------------------------------------------------------------------------- 子命令：eval


@dataclass
class EvalRow:
    """评测用例的一行。

    ``script`` 是 mock 模式下的回放剧本；``answer`` 是它的简写形式——
    只给答案不给剧本时，剧本就是"直接说这句"。
    """

    id: str
    task: str
    expect: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    script: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""


def load_eval_rows(path: Path) -> list[EvalRow]:
    """读 JSONL 评测集。空行与 ``#`` 开头的行跳过。"""
    rows: list[EvalRow] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} 第 {number} 行不是合法 JSON：{exc}") from exc
        answer = str(data.get("answer") or "")
        script = list(data.get("script") or [])
        if not script and answer:
            script = [{"text": answer}]
        rows.append(EvalRow(
            id=str(data.get("id") or f"case-{number}"),
            task=str(data.get("task") or ""),
            expect=[str(x) for x in (data.get("expect") or [])],
            forbid=[str(x) for x in (data.get("forbid") or [])],
            script=script,
            answer=answer,
        ))
    return rows


def judge(answer: str, expect: list[str], forbid: list[str]) -> tuple[bool, list[str]]:
    """判定一条答案。**判定器不含 LLM 调用**——只做关键字匹配。

    这条纪律很重要：判定一旦用模型，评测结果就有随机性，回归基线也就
    没意义了。规则判定是确定的，同一份答案永远同一个结论。
    """
    missing = [key for key in expect if key and key not in answer]
    leaked = [key for key in forbid if key and key in answer]
    reasons: list[str] = []
    if missing:
        reasons.append("缺少关键事实：" + "、".join(missing))
    if leaked:
        reasons.append("出现禁止内容：" + "、".join(leaked))
    return (not reasons), reasons


def run_eval_rows(rows: list[EvalRow], config: Config) -> dict[str, Any]:
    """逐条跑评测集，返回可落盘的报告。"""
    registry = build_eval_registry()
    results: list[dict[str, Any]] = []
    for row in rows:
        if config.live:
            llm: Any = build_client(config)
        else:
            llm = build_script_llm(row.script)
        agent, _registry, _state = build_stack(
            config, row.task, CostThresholds(), registry=registry, llm=llm,
        )
        result = agent.run(row.task)
        ok, reasons = judge(result.answer, row.expect, row.forbid)
        results.append({
            "id": row.id,
            "task": row.task,
            "status": result.status,
            "passed": ok,
            "reasons": reasons,
            "answer": result.answer,
            "tokens": result.total_tokens,
            "cost_usd": result.total_cost,
        })
    passed = sum(1 for item in results if item["passed"])
    return {
        "total": len(results),
        "passed": passed,
        "failed": [item["id"] for item in results if not item["passed"]],
        "results": results,
    }


def cmd_eval(args: argparse.Namespace) -> int:
    """跑评测集。

    优先用第 13 章的 ``dragon_agent.evaluation``；它还没落地时用本文件里的
    轻量实现（读 JSONL、逐条跑、关键字判定）。两条路径的判定口径一致：
    **判定器不含 LLM 调用**。
    """
    thresholds = build_thresholds(args)
    config = build_config(args)
    path = Path(getattr(args, "eval_set", None) or default_eval_set_path())
    if not path.exists():
        _err(f"错误：找不到评测集 {path}")
        return EXIT_USAGE

    try:
        rows = load_eval_rows(path)
    except (OSError, ValueError) as exc:
        _err(f"错误：评测集读取失败：{exc}")
        return EXIT_USAGE

    report, engine = run_eval_with_best_engine(rows, config, thresholds)
    report["engine"] = engine
    report["eval_set"] = str(path)

    regressions: list[str] = []
    baseline_path = getattr(args, "baseline", None)
    if baseline_path:
        try:
            baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _err(f"错误：基线文件读取失败：{exc}")
            return EXIT_USAGE
        # 基线支持两种形态：第 13 章 ``save_baseline`` 写的
        # ``{"case_results": {"id": true}}``，以及简化的 ``{"passed_ids": [...]}``。
        case_results = baseline.get("case_results") or {}
        previously = {str(k) for k, v in case_results.items() if v}
        previously |= {str(x) for x in (baseline.get("passed_ids") or [])}
        now_failed = set(report["failed"])
        regressions = sorted(previously & now_failed)
        report["baseline"] = str(baseline_path)
        report["regression_vs_baseline"] = regressions

    if getattr(args, "json", False):
        _out(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _out("=" * 72)
        _out(f"评测集：{path}")
        _out(f"判定器：{engine}（不含 LLM 调用）")
        if report.get("engine_fallback_reason"):
            _out(f"退回原因：{report['engine_fallback_reason']}")
        _out("=" * 72)
        for item in report["results"]:
            mark = "PASS" if item["passed"] else "FAIL"
            _out(f"[{mark}] {item['id']}  状态 {item['status']}  "
                 f"token {item['tokens']}  成本 {item['cost_usd']:.6f} 美元")
            if item["reasons"]:
                for reason in item["reasons"]:
                    _out(f"        {reason}")
        _rule()
        _out(f"用例 {report['total']} 条，通过 {report['passed']} 条，"
             f"失败 {report['total'] - report['passed']} 条")
        if regressions:
            _out(f"相对基线的回归：{len(regressions)} 条 —— {'、'.join(regressions)}")
        _rule()

    if report["failed"]:
        return EXIT_REGRESSION
    if regressions:
        return EXIT_REGRESSION
    return EXIT_OK


def default_eval_set_path() -> Path:
    """默认评测集的位置。"""
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "examples" / "eval_cases.jsonl",
        Path.cwd() / "examples" / "eval_cases.jsonl",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def run_eval_with_best_engine(rows: list[EvalRow], config: Config,
                              thresholds: CostThresholds) -> tuple[dict[str, Any], str]:
    """优先用 ``dragon_agent.evaluation``，不可用时退回本文件的轻量实现。

    第 13 章的 ``Evaluator`` 更完整（有覆盖矩阵、基线对比、门禁），能用就用。
    但它是**别的编委的文件**——接口对不上时不能改它，只能退回自实现，
    并把退回原因记进报告里，让看报告的人知道这次测的是哪条路径。
    """
    module = _load_evaluation_module()
    if module is None:
        return run_eval_rows(rows, config), "cli 内置轻量评测（自实现）"
    try:
        payload = _run_eval_with_evaluation(module, rows, config, thresholds)
    except Exception as exc:
        payload = run_eval_rows(rows, config)
        payload["engine_fallback_reason"] = f"{type(exc).__name__}: {exc}"
        return payload, "cli 内置轻量评测（自实现）"
    return payload, "dragon_agent.evaluation"


def _load_evaluation_module() -> Any:
    """拿第 13 章的评测模块。没落盘就返回 None。"""
    try:
        from . import evaluation

        return evaluation
    except Exception:
        return None


def _run_eval_with_evaluation(module: Any, rows: list[EvalRow], config: Config,
                              thresholds: CostThresholds) -> dict[str, Any]:
    """用第 13 章的 ``Evaluator`` 跑评测集。

    runner 是这里的关键：评测器只管"拿到答案 + 判定"，
    **答案怎么来**由 runner 决定——mock 模式回放剧本，live 模式调真实模型。
    判定器默认是确定性断言，**不含 LLM 调用**。
    """
    EvalCase = module.EvalCase
    Evaluator = module.Evaluator

    registry = build_eval_registry()
    by_task: dict[str, EvalRow] = {row.task: row for row in rows}
    per_case: dict[str, dict[str, Any]] = {}

    def runner(task: str) -> Any:
        row = by_task.get(task)
        if config.live:
            llm: Any = build_client(config)
        else:
            llm = build_script_llm(row.script if row is not None else [])
        agent, _registry, _state = build_stack(
            config, task, thresholds, registry=registry, llm=llm,
        )
        result = agent.run(task)
        if row is not None:
            per_case[row.id] = {
                "status": result.status,
                "answer": result.answer,
                "tokens": result.total_tokens,
                "cost_usd": result.total_cost,
            }
        return result

    cases = [EvalCase(id=row.id, task=row.task, expect=list(row.expect),
                      forbid=list(row.forbid))
             for row in rows]
    evaluator = Evaluator(config=config, runner=runner)
    report = evaluator.run(cases)

    results: list[dict[str, Any]] = []
    for row in rows:
        passed = bool(report.case_results.get(row.id, False))
        extra = per_case.get(row.id, {})
        reason = str(report.details.get(row.id, "") or "")
        results.append({
            "id": row.id,
            "task": row.task,
            "status": extra.get("status", ""),
            "passed": passed,
            "reasons": [reason] if (not passed and reason) else [],
            "answer": extra.get("answer", ""),
            "tokens": int(extra.get("tokens", 0) or 0),
            "cost_usd": float(extra.get("cost_usd", 0.0) or 0.0),
        })
    return {
        "total": int(getattr(report, "total", len(cases)) or 0),
        "passed": int(getattr(report, "passed", 0) or 0),
        "failed": [str(x) for x in (getattr(report, "failures", []) or [])],
        "results": results,
        "nondeterministic": bool(getattr(report, "nondeterministic", False)),
        "total_tokens": int(getattr(report, "total_tokens", 0) or 0),
        "total_cost": float(getattr(report, "total_cost", 0.0) or 0.0),
    }


# --------------------------------------------------------------------------- 子命令：trace


def cmd_trace(args: argparse.Namespace) -> int:
    """回看某次运行的记录。"""
    config = build_config(args)
    trace_dir = trace_dir_of(config, getattr(args, "trace", None))
    path = trace_dir / "runs" / f"{args.run_id}.json"
    if not path.exists():
        _err(f"错误：找不到 run_id={args.run_id} 的运行记录（{path}）")
        return EXIT_FAILED
    record = json.loads(path.read_text(encoding="utf-8"))
    if getattr(args, "json", False):
        _out(json.dumps(record, ensure_ascii=False, indent=2))
        return EXIT_OK
    _out("=" * 72)
    _out(f"run_id   ：{record.get('run_id')}")
    _out(f"任务     ：{record.get('task')}")
    _out(f"状态     ：{record.get('status')}")
    _out(f"停止原因 ：{record.get('stop_reason')}")
    _out(f"步数     ：{record.get('steps')}")
    _out(f"token    ：{record.get('total_tokens')}")
    _out(f"成本     ：{float(record.get('total_cost_usd') or 0.0):.6f} 美元")
    _out("最终答案 ：")
    _out(str(record.get("answer") or "（空）"))
    sli = record.get("sli") or {}
    if sli:
        _rule()
        _out(f"任务成功率   ：{sli.get('task_success_rate')}")
        _out(f"工具错误率   ：{sli.get('tool_error_rate')}")
        _out(f"无进展率     ：{sli.get('no_progress_rate')}")
        _out(f"每次成功成本 ：{sli.get('cost_per_success_usd')}")
    _out("=" * 72)
    return EXIT_OK


# --------------------------------------------------------------------------- 子命令：runbook


def cmd_runbook(args: argparse.Namespace) -> int:
    """打印值班手册：阈值、处置动作、升级路径。"""
    thresholds = build_thresholds(args)
    if getattr(args, "json", False):
        _out(json.dumps({
            "thresholds": thresholds.to_dict(),
            "actions": [action.to_dict() for action in ONCALL_ACTIONS],
        }, ensure_ascii=False, indent=2))
        return EXIT_OK

    _out("=" * 72)
    _out("值班手册（机器可读版）。完整手册见本章 README。")
    _out("=" * 72)
    _out("\n成本阈值：")
    _out(f"  告警线   ：预算的 {thresholds.warn_ratio:.0%}")
    _out(f"  降级线   ：预算的 {thresholds.degrade_ratio:.0%}")
    _out(f"  熔断线   ：连续失败 {thresholds.breaker_failures} 次，"
         f"冷却 {thresholds.breaker_cooldown_s:.0f} 秒")
    _out(f"  模型限流 ：{thresholds.model_rps:g} 请求/秒，突发 {thresholds.model_burst}")
    _out(f"  工具并发 ：{thresholds.tool_concurrency}")
    _out(f"  队列容量 ：{thresholds.queue_capacity}")

    _out("\n值班条目：")
    for index, action in enumerate(ONCALL_ACTIONS, start=1):
        _out(f"\n  {index}. {action.signal}")
        _out(f"     阈值：{action.threshold}")
        _out(f"     动作：{action.action}")
        _out(f"     升级：{action.escalate}")
    _out("\n" + "=" * 72)
    return EXIT_OK


# --------------------------------------------------------------------------- 子命令：demo


def cmd_demo(args: argparse.Namespace) -> int:
    """端到端演示：把本章的护栏逐个亮出来。

    七段：配置与密钥 → 限流 → 工具限流 → 队列背压 → 预算告警 → 熔断与降级
    → 完整任务回路。全程离线，不需要 API Key。
    """
    thresholds = build_thresholds(args)
    config = build_config(args)

    _out("=" * 72)
    _out("第 14 章上线护栏演示（全部离线，无需 API Key）")
    _out("=" * 72)

    # 1) 配置与密钥
    _out("\n[1] 配置与密钥")
    safe = config.to_dict()
    _out(f"    配置字段 {len(safe)} 个，其中不含 api_key："
         f"{'api_key' not in safe}")
    _out(f"    模型 {safe['model']}，live={safe['live']}，"
         f"成本预算 {safe['cost_budget']} 美元")

    # 2) 模型侧限流
    _out("\n[2] 模型侧限流（令牌桶）")
    bucket = TokenBucket(rate_per_s=thresholds.model_rps, burst=thresholds.model_burst)
    granted = sum(1 for _ in range(8) if bucket.acquire(1, timeout_s=0.0))
    _out(f"    速率 {thresholds.model_rps:g}/秒、突发 {thresholds.model_burst}："
         f"连发 8 次放行 {granted} 次，拒绝 {bucket.rejected} 次")
    _out(f"    令牌桶状态：{bucket.to_dict()}")

    # 3) 工具侧限流
    _out("\n[3] 工具侧限流（并发闸）")
    limiter = ToolRateLimiter(max_concurrency=thresholds.tool_concurrency)
    held: list[str] = []
    for name in ("read_budget", "echo", "add", "send_report", "submit_order"):
        if len(held) >= thresholds.tool_concurrency:
            break
        if limiter.acquire(name):
            held.append(name)
    blocked = not limiter.acquire("extra_tool")
    _out(f"    并发上限 {thresholds.tool_concurrency}：占住 {len(held)} 个，"
         f"再要一个被拒={blocked}")
    for name in held:
        limiter.release(name)
    _out(f"    限流器状态：{limiter.to_dict()}")

    # 4) 队列与背压
    _out("\n[4] 队列与背压")
    queue = BoundedQueue(capacity=2, wait_s=0.05)
    accepted = sum(1 for i in range(4) if queue.put(i))
    _out(f"    容量 2，塞 4 个：入队 {accepted} 个，丢弃 {queue.dropped} 个"
         f"（等待过 {queue.waited} 次）")
    _out(f"    队列状态：{queue.to_dict()}")

    # 5) 预算告警
    _out("\n[5] 预算告警")
    watcher = BudgetWatcher(cost_budget=0.50, token_budget=120_000, thresholds=thresholds)
    fired = watcher.observe(cost=0.42, tokens=30_000)
    for text in fired:
        _out(f"    {text}")
    _out(f"    再看一次同样的消耗：{len(watcher.observe(cost=0.42, tokens=30_000))} 条告警"
         f"（每档只报一次，不刷屏）")

    # 6) 熔断与降级
    _out("\n[6] 自动熔断与降级模型")
    breaker = CircuitBreaker(threshold=thresholds.breaker_failures, cooldown_s=0.0)
    for _ in range(thresholds.breaker_failures):
        breaker.record_failure()
    _out(f"    连续失败 {thresholds.breaker_failures} 次后状态：{breaker.state}"
         f"（冷却 {thresholds.breaker_cooldown_s:.0f} 秒后自动半开探测）")

    class _AlwaysRateLimited:
        """永远返回 429 的假客户端——用来演示降级。"""

        def __init__(self, name: str) -> None:
            self.name = name

        def complete(self, messages, tools=None, **kwargs):
            return LLMResponse(text="[llm] HTTP 429", finish_reason="error")

        def describe(self) -> str:
            return self.name

    class _AlwaysOK:
        def __init__(self, name: str) -> None:
            self.name = name

        def complete(self, messages, tools=None, **kwargs):
            return LLMResponse(text="降级模型的回答", usage=Usage(10, 5), finish_reason="stop")

        def describe(self) -> str:
            return self.name

    client = FallbackLLMClient(primary=_AlwaysRateLimited("主模型"),
                               fallback=_AlwaysOK("备用模型"),
                               breaker=CircuitBreaker(threshold=3, cooldown_s=0.0))
    first = client.complete([{"role": "user", "content": "hi"}])
    second = client.complete([{"role": "user", "content": "hi"}])
    _out(f"    主模型 429 → 切到备用模型：切换 {client.switches} 次")
    _out(f"    第一次回答「{first.text}」，第二次「{second.text}」"
         f"（降级后**粘住**，不再回主模型）")

    # 7) 完整回路
    _out("\n[7] 完整任务回路")
    tracer = None
    trace_dir = None
    if getattr(args, "trace", None):
        trace_dir = trace_dir_of(config, args.trace)
        tracer = build_tracer(config, trace_dir)
    agent, _registry, state = build_stack(config, args.task, thresholds, tracer=tracer)
    result = agent.run(args.task)
    _print_result(result, verbose=False)
    if tracer is not None and trace_dir is not None:
        tracer.export()
        run_id = state.snapshot().task_id
        path = write_run_record(trace_dir, run_id, {
            "run_id": run_id,
            "task": args.task,
            "status": result.status,
            "stop_reason": result.stop_reason,
            "answer": result.answer,
            "total_tokens": result.total_tokens,
            "total_cost_usd": result.total_cost,
            "steps": len(result.steps),
            "sli": tracer.sli(),
            "config": config.to_dict(),
        })
        _out(f"trace 已写入：{path}")

    _out("=" * 72)
    _out("演示结束。接真实模型：--live 并设置 LLM_API_KEY。")
    _out("=" * 72)
    return EXIT_OK if result.status == "completed" else status_exit_code(result)


# --------------------------------------------------------------------------- 参数解析


def _global_parser() -> argparse.ArgumentParser:
    """全局开关。

    所有选项都用 ``SUPPRESS`` 作默认值，这样"没给"和"给了默认值"能分开：
    子命令解析完之后，没在命令行上出现的选项不会用默认值覆盖主解析器的结果。
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--live", action="store_true", default=argparse.SUPPRESS,
                         help="切到真实 API（需要 LLM_API_KEY 或 OPENAI_API_KEY）")
    common.add_argument("--budget", type=int, default=argparse.SUPPRESS,
                         help="单任务 token 预算")
    common.add_argument("--cost", type=float, default=argparse.SUPPRESS,
                         help="单任务成本上限（美元）")
    common.add_argument("--max-steps", type=int, default=argparse.SUPPRESS,
                         help="步数上限")
    common.add_argument("--model", default=argparse.SUPPRESS, help="模型标识")
    common.add_argument("--fallback-model", default=argparse.SUPPRESS,
                         help="降级模型：主模型故障时切到它")
    common.add_argument("--trace", default=argparse.SUPPRESS,
                         help="trace 输出目录（同时是 trace 子命令的读取目录）")
    common.add_argument("--workspace", default=argparse.SUPPRESS, help="工作目录")
    common.add_argument("--price-in", type=float, default=argparse.SUPPRESS,
                         help="输入单价（美元 / 百万 token）")
    common.add_argument("--price-out", type=float, default=argparse.SUPPRESS,
                         help="输出单价（美元 / 百万 token）")
    common.add_argument("--warn-ratio", type=float, default=argparse.SUPPRESS,
                         help="预算告警线（占预算的比例，默认 0.8）")
    common.add_argument("--degrade-ratio", type=float, default=argparse.SUPPRESS,
                         help="降级与熔断线（占预算的比例，默认 0.9）")
    common.add_argument("--breaker-threshold", type=int, default=argparse.SUPPRESS,
                         help="连续失败多少次后熔断（默认 3）")
    common.add_argument("--model-rps", type=float, default=argparse.SUPPRESS,
                         help="模型侧限流（每秒请求数，默认 5）")
    common.add_argument("--tool-concurrency", type=int, default=argparse.SUPPRESS,
                         help="工具侧并发上限（默认 4）")
    common.add_argument("--queue-capacity", type=int, default=argparse.SUPPRESS,
                         help="队列容量（默认 32）")
    return common


def build_parser() -> argparse.ArgumentParser:
    """构造整个命令行解析器。"""
    common = _global_parser()
    parser = argparse.ArgumentParser(
        prog="dragon-agent",
        description="自建 Agent 的上线入口：跑任务、交互、列工具、跑评测、看 trace、查值班手册",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", metavar="<子命令>")

    run_parser = sub.add_parser("run", parents=[common], help="跑一次完整回路")
    run_parser.add_argument("task", help="任务描述")
    run_parser.set_defaults(func=cmd_run)

    chat_parser = sub.add_parser("chat", parents=[common], help="交互式")
    chat_parser.set_defaults(func=cmd_chat)

    tools_parser = sub.add_parser("tools", parents=[common], help="列出可用工具")
    tools_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    tools_parser.set_defaults(func=cmd_tools)

    eval_parser = sub.add_parser("eval", parents=[common], help="跑评测集")
    eval_parser.add_argument("--eval-set", default=None, help="JSONL 评测集路径")
    eval_parser.add_argument("--baseline", default=None, help="基线 JSON，用于回归对比")
    eval_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    eval_parser.set_defaults(func=cmd_eval)

    demo_parser = sub.add_parser("demo", parents=[common], help="端到端演示")
    demo_parser.add_argument("task", nargs="?", default="查一下项目预算，并告诉我总额",
                              help="演示用的任务")
    demo_parser.set_defaults(func=cmd_demo)

    trace_parser = sub.add_parser("trace", parents=[common], help="看某次运行的记录")
    trace_parser.add_argument("run_id", help="运行 id（trace 目录下的 runs/<run_id>.json）")
    trace_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    trace_parser.set_defaults(func=cmd_trace)

    runbook_parser = sub.add_parser("runbook", parents=[common], help="打印值班手册")
    runbook_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    runbook_parser.set_defaults(func=cmd_runbook)

    return parser


# --------------------------------------------------------------------------- 入口


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。**返回退出码**，不直接 ``sys.exit``。

    返回值让测试可以直接断言；``__main__`` 里才转成进程退出码。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        # stdin 也要统一：Windows 控制台默认不是 UTF-8，不改的话交互模式
        # 读进来的中文会变成乱码。
        if sys.stdin is not None and hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):  # pragma: no cover - 老 Python 或被替换过的流
        pass

    parser = build_parser()
    raw = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(raw)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    try:
        config = build_config(args)
    except ConfigError as exc:
        # 缺 API Key 属于启动期错误：说清楚缺什么、怎么给，然后非 0 退出。
        _err("")
        _err("=" * 72)
        _err(f"错误：{exc}")
        _err("")
        _err("配置方式（二选一）：")
        _err('  PowerShell : $env:LLM_API_KEY = "sk-..."')
        _err('  bash       : export LLM_API_KEY="sk-..."')
        _err("  自建服务用 LLM_BASE_URL 指定地址，OPENAI_API_KEY 同样有效。")
        _err("")
        _err("不加 --live 就能用 mock 模式跑通全部子命令，不需要 key。")
        _err("=" * 72)
        return EXIT_CONFIG

    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        _err("\n已中断。")
        return EXIT_FAILED
    except Exception as exc:
        _err(f"错误：{type(exc).__name__}: {exc}")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
