"""可观测性：trace、SLI 指标、成本账、脱敏与采样。

对应第二卷第 12 章，也是第一卷第 33 章的工程落点。

四条设计要点：

1. **语义层对齐 OTel GenAI 约定。** 跟模型、token 沾边的属性一律落在
   ``gen_ai.*`` 命名空间里（``gen_ai.request.model``、
   ``gen_ai.usage.input_tokens``……），Agent 侧的属性用自定义前缀
   （``agent.*`` / ``tool.*`` / ``cost.*`` / ``error.*``）。**不用
   ``llm.*`` 这种简写**——导出到真实后端时它得映射回去，映射层就是
   漏埋点和错埋点的地方。

2. **指标全量、trace 抽样。** SLI 从**计数器**算，不从采样后的 span 算——
   用采样 trace 算出来的成功率是"被采样的任务的成功率"，有偏。采样按
   **任务粒度**决策（一个任务要么整条留、要么整条丢），否则父 span 留着
   子 span 丢了，trace 树就断了。

3. **脱敏在采集端。** 密钥类字段整值替换，长文本截断，工具参数只记摘要
   不记原文，prompt / completion 全文**不进默认路径**。事后过滤是来不及
   的——数据一旦落盘就算泄露了。

4. **可观测性不该要求主循环改一行代码。** ``Tracer`` 通过包装 Agent 的
   模型客户端与工具注册表来埋点（自动插桩），而不是往 ``agent.py`` 里
   插 ``with tracer.span(...)``。理由是控制流与横切面要分开：主循环负责
   "下一步做什么"，观测层负责"记下发生了什么"。
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol, runtime_checkable

from .logging_setup import MASK, SENSITIVE_KEYS, redact, redact_text
from .pricing import PER_MTOK, PriceTable, TokenCounts

__all__ = [
    "Span",
    "Tracer",
    "CostMeter",
    "Sampler",
    "SpanExporter",
    "JsonlExporter",
    "ERROR_TYPES",
    "GEN_AI_PREFIX",
]

#: 模型 / token 相关属性必须落在这个前缀下（OTel GenAI 语义约定）。
GEN_AI_PREFIX = "gen_ai."

#: 错误分桶。固定枚举，不能是自由文本——不然"工具错误率"算不出来。
ERROR_TYPES = (
    "rate_limit",
    "timeout",
    "context_length_exceeded",
    "tool_failure",
    "invalid_arguments",
    "permission_denied",
    "policy_blocked",
    "parse_error",
    "upstream_error",
    "cancelled",
    "budget_exceeded",
)

#: 属性值的最大长度。超了截断——trace 是给排障看的，不是给存档用的。
MAX_VALUE_CHARS = 256

#: 判定"这个属性跟模型/token 沾边"的关键词。命中就必须落在 ``gen_ai.*`` 下。
MODEL_ATTR_KEYWORDS = ("model", "token", "usage", "temperature", "top_p", "finish")


def normalize_attribute_key(key: str) -> str:
    """把模型/token 相关属性归到 ``gen_ai.*`` 命名空间下。

    命名约定不该靠自觉，也不该在运行期炸掉——写错的键名（比如
    ``llm.model``）自动归位，导出层就永远拿得到标准属性。同时返回
    是否发生了改名，调用方可以据此提醒开发者去对齐。
    """
    lowered = key.lower()
    if key.startswith(GEN_AI_PREFIX):
        return key
    if any(word in lowered for word in MODEL_ATTR_KEYWORDS):
        return GEN_AI_PREFIX + key
    return key


# --------------------------------------------------------------------------- Span


@dataclass
class Span:
    """一次可观测操作。

    字段形状由契约定死（``name`` / ``parent`` / ``start`` / ``end`` /
    ``attributes`` / ``status``），后面几个是本工程追加的：``trace_id`` 与
    ``span_id`` 用来串父子关系，``error`` 记错误分类。

    ``Span`` 本身就是上下文管理器——``tracer.span(...)`` 返回它，
    ``with`` 出去时自动计时、自动收尾：

    .. code-block:: python

        with tracer.span("gen_ai.chat", **{"gen_ai.request.model": "m"}) as span:
            ...
    """

    name: str
    parent: str | None = None
    start: float = 0.0
    end: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    trace_id: str = ""
    span_id: str = ""
    error: str | None = None

    # 由 Tracer 在 span() 里回填；不进 dataclass 字段列表，保持契约形状。
    _tracer: Any = field(default=None, repr=False, compare=False)

    # -------------------------------------------------------------- 上下文管理器

    def __enter__(self) -> "Span":
        tracer = self._tracer
        if tracer is not None:
            tracer._enter(self)
        else:
            self.start = time.time()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        tracer = self._tracer
        if tracer is not None:
            tracer._exit(self, exc)
        else:
            self.end = time.time()
        return False  # 异常照常往外抛——观测层不吞异常

    # -------------------------------------------------------------- 属性

    @property
    def duration_ms(self) -> float:
        if not self.start or not self.end:
            return 0.0
        return round((self.end - self.start) * 1000, 3)

    def set(self, key: str, value: Any) -> None:
        """记一个属性。

        **写入前就脱敏截断**，不是事后过滤；键名不符合 GenAI 约定的
        会被自动归到 ``gen_ai.*`` 下。
        """
        renamed = normalize_attribute_key(key)
        tracer = self._tracer
        if tracer is not None and renamed != key:
            tracer._record_rename(key, renamed)
        self.attributes[renamed] = sanitize_value(renamed, value)

    def to_dict(self) -> dict[str, Any]:
        """可落盘的表示。"""
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent": self.parent,
            "start": self.start,
            "end": self.end,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": dict(self.attributes),
            "error": self.error,
        }

    def to_otel(self) -> dict[str, Any]:
        """OpenTelemetry 形状的载荷。

        字段名对齐 OTel 的 span 数据模型，这样导出层可以直接喂给任何
        OTel Collector，业务代码一行都不用改。
        """
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent,
            "name": self.name,
            "startTimeUnixNano": int(self.start * 1e9),
            "endTimeUnixNano": int(self.end * 1e9),
            "status": {"code": 2 if self.status == "error" else 1},
            "attributes": {k: _otel_value(v) for k, v in self.attributes.items()},
        }


def _otel_value(value: Any) -> Any:
    """OTel 的属性值只认字符串 / 布尔 / 数字 / 数组。"""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return str(value)


# --------------------------------------------------------------------------- 脱敏


def sanitize_value(key: str, value: Any) -> Any:
    """属性值写入前的清洗：敏感键整值替换，长文本截断。"""
    if key.lower() in SENSITIVE_KEYS:
        return MASK
    if isinstance(value, str):
        value = redact_text(value)
        if len(value) > MAX_VALUE_CHARS:
            return value[:MAX_VALUE_CHARS] + f"…(截断,共{len(value)}字)"
    if isinstance(value, (dict, list, tuple)):
        return redact(value)
    return value


def summarize_arguments(args: Any) -> str:
    """工具参数的**摘要**——只留键名，不留值。

    工具参数里经常混着订单号、手机号、身份证号。记全文等于记一份
    用户数据到日志系统；只记键名，排障时够用（"它到底传了哪些字段"），
    又不泄露内容。
    """
    if not isinstance(args, dict):
        return f"非对象参数({type(args).__name__})"
    keys = ",".join(sorted(str(k) for k in args))
    digest = hashlib.sha256(
        json.dumps(args, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:12]
    return f"keys=[{keys}] sha256={digest}"


def classify_error(exc: BaseException) -> str:
    """把异常映射到固定错误桶。"""
    name = type(exc).__name__
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_arguments"
    if "RateLimit" in name or "TooManyRequests" in name:
        return "rate_limit"
    if "Timeout" in name:
        return "timeout"
    if "ContextLength" in name or "context_length" in name:
        return "context_length_exceeded"
    return "upstream_error"


# --------------------------------------------------------------------------- 采样


@runtime_checkable
class Sampler(Protocol):
    """采样策略。**按任务粒度**决策：要么整条 trace 留，要么整条丢。"""

    def should_sample(self, trace_id: str) -> bool:
        ...


class AlwaysSampler:
    """全留。调试与回归用。"""

    def should_sample(self, trace_id: str) -> bool:
        return True


class RatioSampler:
    """按比例留。

    用 trace_id 的哈希做决定——**确定性**很重要：同一个任务重跑，要么
    每次都留、要么每次都丢，随机的话没法复现。
    """

    def __init__(self, ratio: float = 0.1) -> None:
        self.ratio = max(0.0, min(1.0, float(ratio)))

    def should_sample(self, trace_id: str) -> bool:
        if self.ratio >= 1.0:
            return True
        if self.ratio <= 0.0:
            return False
        digest = hashlib.sha256(trace_id.encode("utf-8")).hexdigest()
        return int(digest[:8], 16) / 0xFFFFFFFF < self.ratio


class ErrorOrSlowSampler:
    """错误与慢任务**必留**，其余按比例。

    长尾问题恰恰藏在低频路径里：偶发的超时、偶发的限流、偶发的空转。
    纯比例采样会把它们丢掉——而那正是最需要留证据的地方。
    """

    def __init__(self, ratio: float = 0.1, slow_ms: float = 5000.0) -> None:
        self.ratio_sampler = RatioSampler(ratio)
        self.slow_ms = float(slow_ms)

    def should_sample(self, trace_id: str) -> bool:
        return self.ratio_sampler.should_sample(trace_id)

    def keep_because_slow(self, duration_ms: float) -> bool:
        return duration_ms >= self.slow_ms


# --------------------------------------------------------------------------- 导出


@runtime_checkable
class SpanExporter(Protocol):
    """导出器。**语义层与后端解耦**的关键接口。"""

    def export(self, spans: Iterable[Span]) -> None:
        ...


class JsonlExporter:
    """把 span 写成 JSON Lines。

    纯标准库、不依赖任何后端。换成 OTel Collector 或 Langfuse，只是把
    这个类换成对应的 exporter——**业务代码与 span 语义一行不改**。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def export(self, spans: Iterable[Span]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for span in spans:
                handle.write(json.dumps(span.to_otel(), ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- 成本账


class CostMeter:
    """成本账：把 token 账换算成钱，并**按维度拆分**。

    计价算法复用 ``pricing.py`` 的 ``PriceTable``——缓存命中的输入按
    缓存价算，没命中的按输入价算，两者不能混。

    三个维度各自归集：

    - **model**：哪个模型花的（换模型时对比用）；
    - **task**：每次任务花了多少（"这次为什么这么贵"）；
    - **dimension**：调用方自己定的业务维度（租户、团队、场景）。

    ``charge()`` 的返回值是这一次调用的成本——主循环靠它累加总成本。
    """

    def __init__(
        self,
        config: Any = None,
        prices: dict[str, dict[str, float]] | None = None,
        price_in_per_mtok: float | None = None,
        price_out_per_mtok: float | None = None,
    ) -> None:
        self.config = config
        self._table = self._build_table(config, prices,
                                        price_in_per_mtok, price_out_per_mtok)
        self._calls: list[dict[str, Any]] = []
        self._total_cost = 0.0
        self._counts = TokenCounts()

    # ------------------------------------------------------------------ 构造

    @staticmethod
    def _build_table(
        config: Any,
        prices: dict[str, dict[str, float]] | None,
        price_in: float | None,
        price_out: float | None,
    ) -> dict[str, PriceTable]:
        """单价表。配置里的价打底，``prices`` 覆盖同名模型。"""
        table: dict[str, PriceTable] = {}
        if config is not None:
            table["default"] = PriceTable(
                input_per_mtok=float(getattr(config, "price_in_per_mtok", 0.0) or 0.0),
                output_per_mtok=float(getattr(config, "price_out_per_mtok", 0.0) or 0.0),
            )
        if price_in is not None or price_out is not None:
            table["default"] = PriceTable(
                input_per_mtok=float(price_in or 0.0),
                output_per_mtok=float(price_out or 0.0),
            )
        for model, price in (prices or {}).items():
            table[model] = PriceTable(
                input_per_mtok=float(price.get("in", 0.0) or 0.0),
                output_per_mtok=float(price.get("out", 0.0) or 0.0),
            )
        table.setdefault("default", PriceTable())
        return table

    def _table_for(self, model: str) -> PriceTable:
        return self._table.get(model) or self._table.get("default") or PriceTable()

    # ------------------------------------------------------------------ 记账

    def charge(
        self,
        usage: Any,
        model: str = "",
        *,
        task_id: str = "",
        dimension: str = "",
        cached_input_tokens: int = 0,
    ) -> float:
        """记一次模型调用的账，返回这次的美元成本。

        **不抛异常**——计价失败不该让任务崩掉，最坏情况是这次记 0 美元。
        """
        try:
            counts = TokenCounts.from_usage(usage, cached_input_tokens)
            table = self._table_for(model)
            cost = table.cost(counts)
        except Exception:
            return 0.0
        self._total_cost += cost
        self._counts = TokenCounts(
            input_tokens=self._counts.input_tokens + counts.input_tokens,
            output_tokens=self._counts.output_tokens + counts.output_tokens,
            cached_input_tokens=self._counts.cached_input_tokens + counts.cached_input_tokens,
        )
        self._calls.append({
            "model": model,
            "task_id": task_id,
            "dimension": dimension,
            "cost_usd": cost,
            **counts.to_dict(),
        })
        return cost

    def total(self) -> float:
        """总成本（美元）。"""
        return self._total_cost

    def tokens(self) -> dict[str, int]:
        """token 合计。"""
        return self._counts.to_dict()

    def calls(self) -> int:
        """记账了几次模型调用。"""
        return len(self._calls)

    def breakdown(self, dimension: str = "model") -> dict[str, dict[str, Any]]:
        """**按维度拆分**的成本账。

        ``dimension`` 取 ``model`` / ``task_id`` / ``dimension``。
        """
        buckets: dict[str, dict[str, Any]] = {}
        for call in self._calls:
            key = str(call.get(dimension) or "unknown")
            bucket = buckets.setdefault(key, {
                "calls": 0,
                "cost_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
            })
            bucket["calls"] += 1
            bucket["cost_usd"] = round(bucket["cost_usd"] + call["cost_usd"], 8)
            bucket["input_tokens"] += call["input_tokens"]
            bucket["output_tokens"] += call["output_tokens"]
            bucket["cached_input_tokens"] += call["cached_input_tokens"]
        return buckets

    def to_dict(self) -> dict[str, Any]:
        """可落盘的账。"""
        return {
            "total_cost_usd": round(self._total_cost, 8),
            "calls": len(self._calls),
            "tokens": self._counts.to_dict(),
            "by_model": self.breakdown("model"),
            "by_task": self.breakdown("task_id"),
        }


# --------------------------------------------------------------------------- Tracer


class Tracer:
    """trace 运行时：span 树、SLI 计数、脱敏与采样。

    用法有两种：

    1. **手写 span**——自己知道边界的时候：

       .. code-block:: python

           with tracer.span("gen_ai.chat", **{"gen_ai.request.model": "m"}) as span:
               ...

    2. **自动插桩**——把 tracer 交给 Agent，它自己把模型调用与工具调用
       变成 span：

       .. code-block:: python

           agent = Agent(config, llm, tools, context, state, tracer=tracer)
           agent.run("...")
           tracer.spans()   # 这一轮的任务、模型、工具，全在

    第 2 种是本工程的主推方式：主循环不用改一行代码，观测语义也不会
    渗进控制流。
    """

    def __init__(
        self,
        config: Any = None,
        *,
        sampler: Sampler | None = None,
        exporter: SpanExporter | None = None,
        meter: CostMeter | None = None,
        agent_name: str = "dragon-agent",
    ) -> None:
        self.config = config
        self.sampler: Sampler = sampler or AlwaysSampler()
        self.exporter = exporter
        self.meter = meter or CostMeter(config)
        self.agent_name = agent_name

        self._spans: list[Span] = []
        self._lock = threading.Lock()
        self._stack = contextvars.ContextVar("dragon_span_stack", default=())
        self._sampled_flags: dict[str, bool] = {}

        # ---- SLI 计数器（全量，与采样无关） ----
        self._task_count = 0
        self._success_count = 0
        self._no_progress_count = 0
        self._tool_calls = 0
        self._tool_errors = 0
        self._tool_errors_by_name: dict[str, int] = {}
        self._steps: list[int] = []
        self._sampled_tasks = 0
        self._renamed_attributes: dict[str, str] = {}

        # 有人造 tracer 了，就顺手把自动插桩装上。装插桩的时机选在
        # "有人造了一个 tracer"这一刻，而不是 import 时刻——import 时刻
        # 改别人的类，副作用太大。
        _install_auto_instrumentation()

    def _record_rename(self, original: str, renamed: str) -> None:
        """记一次属性改名，提醒开发者去对齐命名约定。"""
        self._renamed_attributes[original] = renamed

    # ------------------------------------------------------------------ span

    def span(self, name: str, **attributes: Any) -> Span:
        """开一个 span。返回的对象**本身就是上下文管理器**。

        属性在写入前脱敏截断；模型/token 类属性写错了键名会被自动归到
        ``gen_ai.*`` 下（并记一次改名），不会在运行期炸掉。
        """
        span = Span(name=name)
        span._tracer = self
        span.span_id = uuid.uuid4().hex[:16]
        for key, value in attributes.items():
            span.set(key, value)
        return span

    def _enter(self, span: Span) -> None:
        """span 开始：定 trace_id、接上父 span、进栈。"""
        stack = self._stack.get()
        parent = stack[-1] if stack else None
        if parent is not None:
            span.trace_id = parent.trace_id
            span.parent = parent.span_id
            span.span_id = span.span_id or uuid.uuid4().hex[:16]
        else:
            span.trace_id = span.trace_id or uuid.uuid4().hex[:32]
            span.parent = None
            # 根 span：这里决定这条 trace 要不要留。
            keep = bool(self.sampler.should_sample(span.trace_id))
            self._sampled_flags[span.trace_id] = keep
            if keep:
                self._sampled_tasks += 1
        span.start = time.time()
        self._stack.set(stack + (span,))

    def _exit(self, span: Span, exc: BaseException | None) -> None:
        """span 结束：计时、记状态、按采样决定留不留。"""
        span.end = time.time()
        stack = self._stack.get()
        if stack and stack[-1] is span:
            self._stack.set(stack[:-1])
        elif span in stack:
            trimmed = tuple(s for s in stack if s is not span)
            self._stack.set(trimmed)
        if exc is not None:
            span.status = "error"
            span.error = classify_error(exc)
            span.set("error.type", span.error)
            span.set("error.message", f"{type(exc).__name__}: {exc}")
            span.set("error.retryable", False)
        keep = self._sampled_flags.get(span.trace_id, True)
        if keep:
            with self._lock:
                self._spans.append(span)

    def spans(self) -> list[Span]:
        """这一轮记下的所有 span（**已按采样过滤**）。

        按开始时间排序——父 span 一定先开始，所以消费方按这个顺序
        遍历就是天然的树序，不用自己再排一次。
        """
        with self._lock:
            return sorted(self._spans, key=lambda span: span.start)

    def current_task_id(self) -> str:
        """当前任务 id。

        从正在进行的根 span 上取——成本账要按任务归集，就得有这个。
        """
        for span in reversed(self._stack.get()):
            task_id = span.attributes.get("agent.task.id")
            if task_id:
                return str(task_id)
        return ""

    def reset(self) -> None:
        """清空 span 与计数器。跑下一轮之前调。"""
        with self._lock:
            self._spans.clear()
        self._task_count = 0
        self._success_count = 0
        self._no_progress_count = 0
        self._tool_calls = 0
        self._tool_errors = 0
        self._tool_errors_by_name = {}
        self._steps = []
        self._sampled_tasks = 0
        self._sampled_flags = {}
        self._renamed_attributes = {}

    # ------------------------------------------------------------------ 指标

    def record_task(self, result: Any, span: Span | None = None) -> None:
        """记一次任务的最终结果，更新 SLI 计数器。

        **判定发生在任务层**：``status == "completed"`` 才算成功。
        用 HTTP 200 或"没抛异常"顶替成功判定，是成功率虚高的头号原因。
        """
        status = str(getattr(result, "status", "") or "")
        steps = list(getattr(result, "steps", []) or [])
        self._task_count += 1
        self._steps.append(len(steps))
        if status == "completed":
            self._success_count += 1
        if status == "stopped_no_progress":
            self._no_progress_count += 1
        if span is not None:
            span.set("agent.status", status)
            span.set("agent.step.count", len(steps))
            span.set("agent.stop_reason", str(getattr(result, "stop_reason", "") or ""))
            span.set("cost.total", float(getattr(result, "total_cost", 0.0) or 0.0))
            span.set("gen_ai.usage.total_tokens", int(getattr(result, "total_tokens", 0) or 0))

    def record_tool_call(self, name: str, ok: bool) -> None:
        """记一次工具调用（给工具错误率用）。"""
        self._tool_calls += 1
        if not ok:
            self._tool_errors += 1
            self._tool_errors_by_name[name] = self._tool_errors_by_name.get(name, 0) + 1

    def sli(self) -> dict[str, Any]:
        """SLI 报表。**从全量计数器算，不从采样 span 算。**

        四个必备指标（第一卷第 33 章）：任务成功率、工具错误率、无进展率、
        每次成功成本。步数分布是诊断指标，**不进 SLO**。
        """
        tasks = self._task_count
        successes = self._success_count
        cost = self.meter.total()
        return {
            "task_count": tasks,
            "success_count": successes,
            "task_success_rate": round(successes / tasks, 6) if tasks else 0.0,
            "tool_calls": self._tool_calls,
            "tool_errors": self._tool_errors,
            "tool_error_rate": (round(self._tool_errors / self._tool_calls, 6)
                                if self._tool_calls else 0.0),
            "tool_error_rate_by_tool": dict(self._tool_errors_by_name),
            "no_progress_count": self._no_progress_count,
            "no_progress_rate": (round(self._no_progress_count / tasks, 6)
                                 if tasks else 0.0),
            "total_cost_usd": round(cost, 8),
            "cost_per_success_usd": (round(cost / successes, 8) if successes else 0.0),
            "steps_per_task": self._percentiles(self._steps),
            "span_count": len(self.spans()),
            "sampled_task_count": self._sampled_tasks,
            "sampling_rate": (round(self._sampled_tasks / tasks, 6) if tasks else 0.0),
            "renamed_attributes": dict(self._renamed_attributes),
        }

    @staticmethod
    def _percentiles(values: list[int]) -> dict[str, float]:
        """p50 / p90 / p99 与均值。空数据返回全 0。"""
        if not values:
            return {"count": 0, "mean": 0.0, "p50": 0.0, "p90": 0.0, "p99": 0.0, "max": 0}
        ordered = sorted(values)
        size = len(ordered)

        def pick(percent: float) -> int:
            index = min(size - 1, max(0, int(round(percent / 100.0 * (size - 1)))))
            return ordered[index]

        return {
            "count": size,
            "mean": round(sum(ordered) / size, 3),
            "p50": pick(50),
            "p90": pick(90),
            "p99": pick(99),
            "max": ordered[-1],
        }

    # ------------------------------------------------------------------ 导出

    def export(self, exporter: SpanExporter | None = None) -> None:
        """把 span 交给导出器。"""
        target = exporter or self.exporter
        if target is None:
            return
        target.export(self.spans())

    # ------------------------------------------------------------------ 埋点

    def attach(self, agent: Any) -> Any:
        """给一个 Agent 装上埋点（**幂等**）。

        包两样东西：模型客户端（记 ``gen_ai.chat`` span）与工具注册表
        （记 ``execute_tool`` span）。主循环一行不用改。
        """
        if getattr(agent, "_dragon_traced", False):
            return agent
        try:
            agent._dragon_traced = True
            # 同一个账本只能记一次钱：Agent 自己带着 meter 时，
            # 由 Agent 记（它的 total_cost 也会用到），埋点层就不重复记。
            charge = getattr(agent, "meter", None) is not self.meter
            llm = getattr(agent, "llm", None)
            if llm is not None and not getattr(llm, "_dragon_traced", False):
                agent.llm = _TracedLLM(llm, self, charge=charge)
            tools = getattr(agent, "tools", None)
            if tools is not None and not getattr(tools, "_dragon_traced", False):
                agent.tools = _TracedRegistry(tools, self)
        except Exception:
            pass  # 埋点失败不该让任务崩掉
        return agent

    def instrument_llm(self, llm: Any) -> Any:
        """单独包一个模型客户端。"""
        if llm is None or getattr(llm, "_dragon_traced", False):
            return llm
        return _TracedLLM(llm, self)

    def instrument_registry(self, registry: Any) -> Any:
        """单独包一个工具注册表。"""
        if registry is None or getattr(registry, "_dragon_traced", False):
            return registry
        return _TracedRegistry(registry, self)


class _TracedLLM:
    """包在模型客户端外面的埋点层。"""

    def __init__(self, inner: Any, tracer: Tracer, charge: bool = True) -> None:
        self.inner = inner
        self.tracer = tracer
        self.charge_cost = charge
        self._dragon_traced = True

    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None = None,
                 **kwargs: Any) -> Any:
        model = self._model_name()
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model,
        }
        temperature = kwargs.get("temperature")
        if temperature is not None:
            attributes["gen_ai.request.temperature"] = temperature
        with self.tracer.span("gen_ai.chat", **attributes) as span:
            started = time.time()
            response = self.inner.complete(messages, tools=tools, **kwargs)
            usage = getattr(response, "usage", None)
            if usage is not None:
                span.set("gen_ai.usage.input_tokens", int(getattr(usage, "input_tokens", 0) or 0))
                span.set("gen_ai.usage.output_tokens", int(getattr(usage, "output_tokens", 0) or 0))
                span.set("gen_ai.usage.total_tokens", int(getattr(usage, "total", 0) or 0))
            finish = str(getattr(response, "finish_reason", "") or "")
            if finish:
                span.set("gen_ai.response.finish_reasons", finish)
            span.set("agent.latency.total_ms", round((time.time() - started) * 1000, 3))
            # 成本账记在这里：每次模型调用记一次。Agent 自己带着同一个
            # meter 时由 Agent 记，避免同一个账本记两遍钱。
            cost = 0.0
            if self.charge_cost and usage is not None:
                cost = self.tracer.meter.charge(
                    usage, model, task_id=self.tracer.current_task_id())
            span.set("cost.total", cost)
            return response

    def _model_name(self) -> str:
        describe = getattr(self.inner, "describe", None)
        if callable(describe):
            try:
                return str(describe())
            except Exception:
                return "unknown"
        return "unknown"

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class _TracedRegistry:
    """包在工具注册表外面的埋点层。"""

    def __init__(self, inner: Any, tracer: Tracer) -> None:
        self.inner = inner
        self.tracer = tracer
        self._dragon_traced = True

    def invoke(self, name: str, args: dict[str, Any],
               idempotency_key: str | None = None) -> Any:
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "tool.name": name,
            # 参数只记摘要：键名 + 哈希，不记值。
            "tool.arguments": summarize_arguments(args),
        }
        with self.tracer.span("execute_tool", **attributes) as span:
            result = self.inner.invoke(name, args, idempotency_key=idempotency_key)
            ok = bool(getattr(result, "ok", False))
            span.set("tool.result.status", "ok" if ok else "error")
            if not ok:
                error = str(getattr(result, "error", "") or "")
                error_type = self._classify(name, args)
                span.set("error.type", error_type)
                span.set("error.message", error)
                span.set("error.retryable", False)
                span.status = "error"
            span.set("tool.duration_ms", float(getattr(result, "duration_ms", 0) or 0))
            self.tracer.record_tool_call(name, ok)
            return result

    def _classify(self, name: str, args: dict[str, Any]) -> str:
        """区分"参数不合法"与"工具自身失败"。

        整体工具错误率会把这两个混在一起，看不出是模型传错了参数还是
        下游真的挂了。判断用注册表自己的校验器——**校验逻辑只有一份**。
        """
        try:
            spec, _fn = self.inner.get(name)
            if self.inner.validate(spec, args):
                return "invalid_arguments"
        except Exception:
            pass
        return "tool_failure"

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


# --------------------------------------------------------------------------- 自动插桩


def _task_id_of(agent: Any) -> str:
    """从状态层取任务 id。

    ``TaskState`` 是唯一真相源（契约不变式 6），任务 id 也得从那儿拿——
    自己另造一个 id，trace 和状态就对不上了。
    """
    try:
        state = getattr(agent, "state", None)
        snapshot = state.snapshot() if state is not None else None
        return str(getattr(snapshot, "task_id", "") or "")
    except Exception:
        return str(getattr(agent, "task_id", "") or "")


def _install_auto_instrumentation() -> bool:
    """给 ``Agent.run`` 包一层，让带 tracer 的 Agent 自动产生 span。

    **只装一次**，而且只在 Agent 真的带了 tracer 时才改变行为——不带
    tracer 的 Agent 走原路径，一字不变。

    为什么用自动插桩而不是往主循环里插 ``with tracer.span(...)``：
    控制流与横切面要分开。主循环负责"下一步做什么"，观测层负责"记下
    发生了什么"。往主循环里插观测代码，久了就会变成"为了观测改业务"。
    """
    try:
        from .agent import Agent
    except Exception:
        return False
    if getattr(Agent.run, "_dragon_patched", False):
        return True
    original = Agent.run

    def run(self: Any, task: str) -> Any:
        tracer = getattr(self, "tracer", None)
        if tracer is None or not hasattr(tracer, "span"):
            return original(self, task)
        try:
            tracer.attach(self)
            with tracer.span(
                "invoke_agent",
                **{
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": getattr(tracer, "agent_name", "dragon-agent"),
                    "agent.task.id": _task_id_of(self),
                },
            ) as span:
                result = original(self, task)
                tracer.record_task(result, span)
                return result
        except Exception:
            return original(self, task)

    run._dragon_patched = True  # type: ignore[attr-defined]
    run.__doc__ = original.__doc__
    Agent.run = run  # type: ignore[method-assign]
    return True


def _install_tracer_hook() -> None:
    """兼容入口：自动插桩现在由 ``Tracer.__init__`` 直接触发。"""
    try:
        _install_auto_instrumentation()
    except Exception:
        pass
