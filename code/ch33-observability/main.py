#!/usr/bin/env python3
"""ch33 可观测性 —— 最小可实现参考实现。

把四件事做成可运行的最小闭环：
    1. Trace/Span 运行时：trace 边界是"任务"，span 用 parent 串成因果树，
       自动计时，异常自动归类到固定错误枚举。
    2. LLM span：记模型、token、参数、首 token 延迟与总延迟，按本地价目表算成本。
    3. 脱敏与采样：字段级 allowlist + 正则脱敏；按任务粒度采样并记录采样率。
    4. SLI 计算：成功率、步数分布、工具错误率、无进展率、每次成功成本，
       全部来自全量计数指标，而不是从采样后的 trace 里聚合。

依赖：仅 Python 标准库（>= 3.8）。默认 mock 模式不需要任何 API Key。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# 错误分类与字段白名单
# ---------------------------------------------------------------------------

ERROR_BUCKETS = (
    "rate_limit", "timeout", "context_length_exceeded", "tool_failure",
    "invalid_arguments", "permission_denied", "policy_blocked", "parse_error",
    "upstream_error", "cancelled", "budget_exceeded", "no_progress", "unknown",
)

RETRYABLE_ERRORS = {"rate_limit", "timeout", "upstream_error", "tool_failure"}

# 允许记录的属性（allowlist 优于 denylist：不在表里的一律丢弃）
ALLOWED_ATTRS = {
    "llm.model", "llm.provider", "llm.usage.prompt_tokens", "llm.usage.completion_tokens",
    "llm.request.temperature", "llm.request.top_p", "llm.request.max_tokens",
    "llm.latency.first_token_ms", "llm.latency.total_ms", "llm.output.tool_calls",
    "cost.input", "cost.output", "cost.total",
    "retry.count", "retry.reason",
    "error.type", "error.message", "error.retryable",
    "tool.name", "tool.arguments", "tool.result.status",
    "agent.step.index", "agent.step.type", "agent.task.id", "agent.session.id",
    "agent.user.id", "agent.progress", "agent.no_progress_streak",
}

SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b1[3-9]\d{9}\b|\b\d{17}[\dXx]\b)"
)

# 示意价目表：单位为每百万 token 的价格，用于演示成本计算逻辑。
# 生产环境请换成自己的价目表并定期更新——厂商价格会变，差异也很大。
PRICE_TABLE: Dict[str, Dict[str, float]] = {
    "demo-fast": {"prompt": 0.15, "completion": 0.60},
    "demo-pro": {"prompt": 3.00, "completion": 15.00},
    "demo-live": {"prompt": 1.00, "completion": 3.00},
}
DEFAULT_PRICE = {"prompt": 1.00, "completion": 3.00}


def redact_text(text: str) -> str:
    return SECRET_RE.sub("[REDACTED]", text)


def redact_attrs(attrs: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """返回 (脱敏后的属性, 被丢弃的字段数)。"""
    out: Dict[str, Any] = {}
    dropped = 0
    for key, value in attrs.items():
        if key not in ALLOWED_ATTRS:
            dropped += 1
            continue
        if isinstance(value, str):
            out[key] = redact_text(value)[:500]
        else:
            out[key] = value
    return out, dropped


def classify_error(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, ValueError):
        return "invalid_arguments"
    if isinstance(exc, (ConnectionError, OSError)):
        return "upstream_error"
    return "unknown"


# ---------------------------------------------------------------------------
# Span / Tracer
# ---------------------------------------------------------------------------


def _hex(length: int) -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(length))


@dataclass
class Span:
    name: str
    span_id: str
    trace_id: str
    parent_id: Optional[str] = None
    attrs: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    start: float = 0.0
    end: float = 0.0
    status: str = "ok"
    error_type: str = ""

    @property
    def duration_ms(self) -> int:
        return int((self.end - self.start) * 1000)

    def add_event(self, name: str, **attrs: Any) -> None:
        self.events.append({"name": name, "at_ms": int((time.time() - self.start) * 1000),
                            "attrs": attrs})

    def to_row(self, base: float) -> Dict[str, Any]:
        attrs, _ = redact_attrs(self.attrs)
        return {
            "name": self.name,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "start_ms": int((self.start - base) * 1000),
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error_type": self.error_type,
            "attrs": attrs,
            "events": self.events,
        }


class Tracer:
    """trace 边界 = 一次任务；span 用 parent_id 串成因果树。"""

    def __init__(self) -> None:
        self.stack: List[Span] = []
        self.spans: List[Span] = []
        self.trace_id: str = ""

    @contextmanager
    def trace(self, task_id: str, session_id: str) -> Iterator[Span]:
        self.trace_id = _hex(32)
        span = Span("task_run", _hex(16), self.trace_id, None,
                    {"agent.task.id": task_id, "agent.session.id": session_id})
        span.start = time.time()
        self.stack.append(span)
        try:
            yield span
        finally:
            span.end = time.time()
            self.stack.pop()
            self.spans.append(span)

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        parent = self.stack[-1] if self.stack else None
        span = Span(name, _hex(16),
                    parent.trace_id if parent else self.trace_id,
                    parent.span_id if parent else None)
        span.start = time.time()
        span.attrs.update(attrs)
        self.stack.append(span)
        try:
            yield span
        except Exception as exc:  # noqa: BLE001 - 记录后原样抛出
            span.status = "error"
            span.error_type = classify_error(exc)
            span.attrs["error.type"] = span.error_type
            span.attrs["error.message"] = redact_text(str(exc))[:500]
            span.attrs["error.retryable"] = "true" if span.error_type in RETRYABLE_ERRORS else "false"
            raise
        finally:
            span.end = time.time()
            self.stack.pop()
            self.spans.append(span)


# ---------------------------------------------------------------------------
# 采样与导出
# ---------------------------------------------------------------------------


@dataclass
class SamplingPolicy:
    mode: str = "always"      # always | ratio | error_or_slow
    ratio: float = 0.5
    slow_ms: int = 2000

    def decide(self, spans: List[Span]) -> bool:
        """按任务粒度决策：要么整条 trace 留，要么整条丢。"""
        if self.mode == "always":
            return True
        if self.mode == "ratio":
            return random.random() < self.ratio
        if self.mode == "error_or_slow":
            for span in spans:
                # 最终状态是 ok 但中途失败过（重试），也算"出问题"的 trace
                if span.status == "error" or span.attrs.get("error.type"):
                    return True
                if span.attrs.get("retry.count"):
                    return True
                if any(e.get("name") == "tool.retry_scheduled" for e in span.events):
                    return True
            if spans:
                # 慢：看整条 trace 的墙钟跨度，而不是单个 span
                duration = (max(s.end for s in spans) - min(s.start for s in spans)) * 1000
                if duration > self.slow_ms:
                    return True
            return False
        return True


class JsonlExporter:
    def __init__(self, path: str) -> None:
        self.path = path
        self.exported = 0

    def export(self, trace_id: str, spans: List[Span], sampled: bool) -> None:
        if not spans:
            return
        base = min(s.start for s in spans)
        record = {
            "trace_id": trace_id,
            "task_id": spans[-1].attrs.get("agent.task.id", ""),
            "sampled": sampled,
            "spans": [s.to_row(base) for s in spans],
        }
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.exported += 1


# ---------------------------------------------------------------------------
# 指标（全量计数，与 trace 采样无关）
# ---------------------------------------------------------------------------


@dataclass
class Metrics:
    tasks_total: int = 0
    tasks_success: int = 0
    steps_per_task: List[int] = field(default_factory=list)
    tool_calls: Counter = field(default_factory=Counter)
    tool_errors: Counter = field(default_factory=Counter)
    tool_error_types: Dict[str, Counter] = field(default_factory=dict)
    tasks_no_progress: int = 0
    total_cost: float = 0.0
    cost_by_model: Counter = field(default_factory=Counter)
    llm_calls: int = 0
    traces_total: int = 0
    traces_sampled: int = 0

    def record_llm(self, model: str, cost: float) -> None:
        self.llm_calls += 1
        self.total_cost += cost
        self.cost_by_model[model] += cost

    def record_tool_error(self, tool: str, error_type: str) -> None:
        self.tool_errors[tool] += 1
        self.tool_error_types.setdefault(tool, Counter())[error_type] += 1


# ---------------------------------------------------------------------------
# 进度快照与无进展检测
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateSnapshot:
    new_files: frozenset
    observations: frozenset


def is_progress(prev: StateSnapshot, cur: StateSnapshot) -> bool:
    """只 diff 关键信号：新增文件、新的 observation 哈希。不做全量快照。"""
    return (cur.new_files != prev.new_files
            or cur.observations != prev.observations)


# ---------------------------------------------------------------------------
# LLM span
# ---------------------------------------------------------------------------


def price_of(model: str) -> Dict[str, float]:
    return PRICE_TABLE.get(model, DEFAULT_PRICE)


def record_llm(tracer: Tracer, metrics: Metrics, model: str, provider: str,
               prompt_tokens: int, completion_tokens: int, first_token_ms: int,
               total_ms: int, tool_calls: int = 0) -> None:
    price = price_of(model)
    cost_in = prompt_tokens * price["prompt"] / 1_000_000
    cost_out = completion_tokens * price["completion"] / 1_000_000
    with tracer.span(
        "llm.chat",
        **{
            "llm.model": model,
            "llm.provider": provider,
            "llm.usage.prompt_tokens": prompt_tokens,
            "llm.usage.completion_tokens": completion_tokens,
            "llm.request.temperature": 0.2,
            "llm.request.top_p": 1.0,
            "llm.request.max_tokens": 1024,
            "llm.latency.first_token_ms": first_token_ms,
            "llm.latency.total_ms": total_ms,
            "llm.output.tool_calls": tool_calls,
            "cost.input": round(cost_in, 8),
            "cost.output": round(cost_out, 8),
            "cost.total": round(cost_in + cost_out, 8),
        }
    ):
        pass
    metrics.record_llm(model, cost_in + cost_out)


# ---------------------------------------------------------------------------
# 工具调用
# ---------------------------------------------------------------------------


def run_tool(tracer: Tracer, metrics: Metrics, step: Dict[str, Any],
             state: StateSnapshot) -> StateSnapshot:
    """一次工具调用（含多次尝试）记在一个 span 上，重试用 retry.count 表达。"""
    tool_name = step["tool"]
    attempts = step.get("attempts") or [{"ok": True}]
    metrics.tool_calls[tool_name] += 1
    last_ok = bool(attempts[-1].get("ok"))
    with tracer.span("tool.call",
                     **{"tool.name": tool_name,
                        "tool.arguments": step.get("arguments", "{}"),
                        "tool.result.status": "ok" if last_ok else "error"}) as sp:
        if len(attempts) > 1:
            sp.attrs["retry.count"] = len(attempts) - 1
            sp.attrs["retry.reason"] = attempts[0].get("error", "unknown")
        for attempt in attempts[:-1]:
            err = attempt.get("error", "unknown")
            metrics.record_tool_error(tool_name, err)
            sp.add_event("tool.retry_scheduled", error_type=err)
        last = attempts[-1]
        if not last.get("ok"):
            sp.status = "error"
            sp.error_type = last.get("error", "tool_failure")
            sp.attrs["error.type"] = sp.error_type
            sp.attrs["error.retryable"] = "true" if sp.error_type in RETRYABLE_ERRORS else "false"
            metrics.record_tool_error(tool_name, sp.error_type)
            return state
        obs = last.get("observation")
        if obs:
            return StateSnapshot(state.new_files, state.observations | {obs})
    return state


# ---------------------------------------------------------------------------
# 跑一个任务
# ---------------------------------------------------------------------------


def run_task(tracer: Tracer, metrics: Metrics, scenario: Dict[str, Any]) -> Dict[str, Any]:
    task_id = scenario["task_id"]
    state = StateSnapshot(frozenset(), frozenset())
    streak = 0
    steps = 0
    success = False
    gave_up = False

    with tracer.trace(task_id=task_id, session_id="S-2026-11-07"):
        with tracer.span("agent.session", **{"agent.session.id": "S-2026-11-07"}):
            for step in scenario["steps"]:
                steps += 1
                kind = step["kind"]
                before = state
                with tracer.span("agent.step",
                                 **{"agent.step.index": steps, "agent.step.type": kind}) as sp:
                    if kind in ("think", "act"):
                        record_llm(tracer, metrics, step.get("model", "demo-fast"), "demo",
                                   step.get("prompt_tokens", 800),
                                   step.get("completion_tokens", 120),
                                   step.get("first_token_ms", 180),
                                   step.get("total_ms", 900),
                                   tool_calls=1 if kind == "act" else 0)
                    if kind == "act" and step.get("tool"):
                        state = run_tool(tracer, metrics, step, state)
                    if kind == "observe":
                        obs = step.get("observation", "")
                        if obs:
                            state = StateSnapshot(state.new_files, state.observations | {obs})
                        with tracer.span("retrieval.search",
                                         **{"tool.name": step.get("tool", "retrieve"),
                                            "tool.result.status": "ok"}):
                            pass
                    if kind == "finish":
                        success = bool(step.get("success"))

                    if kind != "finish":
                        progressed = is_progress(before, state)
                        streak = 0 if progressed else streak + 1
                        sp.attrs["agent.progress"] = "true" if progressed else "false"
                        sp.attrs["agent.no_progress_streak"] = streak
                        if streak >= 3:
                            sp.add_event("agent.no_progress_detected", streak=streak)
                            sp.attrs["error.type"] = "no_progress"
                            gave_up = True
                            break

            final_attrs: Dict[str, Any] = {}
            if gave_up:
                final_attrs["error.type"] = "no_progress"
            elif not success:
                final_attrs["error.type"] = "unknown"
            with tracer.span("agent.final_answer", **final_attrs):
                pass

    metrics.tasks_total += 1
    metrics.steps_per_task.append(steps)
    if success:
        metrics.tasks_success += 1
    if gave_up:
        metrics.tasks_no_progress += 1
    return {"task_id": task_id, "steps": steps, "success": success, "no_progress": gave_up}


# ---------------------------------------------------------------------------
# Mock 场景
# ---------------------------------------------------------------------------


def mock_scenarios() -> List[Dict[str, Any]]:
    return [
        {
            "task_id": "T-10231",
            "title": "退款咨询：正常完成",
            "steps": [
                {"kind": "think", "model": "demo-fast", "prompt_tokens": 820, "completion_tokens": 140},
                {"kind": "act", "model": "demo-fast", "prompt_tokens": 860, "completion_tokens": 95,
                 "tool": "search_tickets",
                 "attempts": [{"ok": True, "observation": "obs-ticket-1"}]},
                {"kind": "observe", "tool": "search_tickets", "observation": "obs-ticket-1"},
                {"kind": "think", "model": "demo-fast", "prompt_tokens": 910, "completion_tokens": 120},
                {"kind": "finish", "success": True},
            ],
        },
        {
            "task_id": "T-10255",
            "title": "工单查询：工具超时后重试成功",
            "steps": [
                {"kind": "think", "model": "demo-pro", "prompt_tokens": 1400, "completion_tokens": 180},
                {"kind": "act", "model": "demo-pro", "prompt_tokens": 1520, "completion_tokens": 160,
                 "tool": "query_db",
                 "attempts": [{"error": "timeout"}, {"ok": True, "observation": "obs-db-2"}]},
                {"kind": "observe", "tool": "query_db", "observation": "obs-db-2"},
                {"kind": "think", "model": "demo-pro", "prompt_tokens": 1600, "completion_tokens": 140},
                {"kind": "finish", "success": True},
            ],
        },
        {
            "task_id": "T-10301",
            "title": "资料收集：连续空转后放弃",
            "steps": [
                {"kind": "think", "model": "demo-fast", "prompt_tokens": 700, "completion_tokens": 110},
                {"kind": "act", "model": "demo-fast", "prompt_tokens": 720, "completion_tokens": 90,
                 "tool": "fetch_page",
                 "attempts": [{"ok": True, "observation": "obs-page-same"}]},
                {"kind": "observe", "tool": "fetch_page", "observation": "obs-page-same"},
                {"kind": "act", "model": "demo-fast", "prompt_tokens": 730, "completion_tokens": 88,
                 "tool": "fetch_page",
                 "attempts": [{"ok": True, "observation": "obs-page-same"}]},
                {"kind": "observe", "tool": "fetch_page", "observation": "obs-page-same"},
                {"kind": "finish", "success": False},
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Live 模式
# ---------------------------------------------------------------------------


def live_task(tracer: Tracer, metrics: Metrics, model: str, base_url: str,
              api_key: str, timeout: float = 60.0) -> Dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 256,
        "messages": [
            {"role": "system", "content": "你是一个只回答一句话的助手。"},
            {"role": "user", "content": "用一句话说明什么是 Agent 的上下文窗口。"},
        ],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    started = time.time()
    with tracer.trace(task_id="LIVE-" + _hex(4), session_id="S-live"):
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        total_ms = int((time.time() - started) * 1000)
        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        if not prompt_tokens and not completion_tokens:  # 没有 usage 时粗略估算
            prompt_tokens, completion_tokens = 64, 32
        record_llm(tracer, metrics, model, "live", prompt_tokens, completion_tokens,
                   int(total_ms * 0.4), total_ms)
        with tracer.span("agent.final_answer"):
            pass
    metrics.tasks_total += 1
    metrics.steps_per_task.append(1)
    metrics.tasks_success += 1
    return {"task_id": tracer.spans[-1].attrs.get("agent.task.id", "LIVE"),
            "steps": 1, "success": True, "no_progress": False}


# ---------------------------------------------------------------------------
# SLI 报表
# ---------------------------------------------------------------------------


def percentile(values: List[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * len(ordered) + 0.5)) - 1))
    return ordered[idx]


def print_sli(metrics: Metrics, exporter: JsonlExporter, results: List[Dict[str, Any]]) -> None:
    total_calls = sum(metrics.tool_calls.values())
    total_errors = sum(metrics.tool_errors.values())
    print("=" * 74)
    print("SLI 报表（全部来自全量计数指标，与 trace 采样无关）")
    print("=" * 74)

    rate = (metrics.tasks_success / metrics.tasks_total) if metrics.tasks_total else 0.0
    print("任务成功率        : {}/{} = {:.1%}".format(
        metrics.tasks_success, metrics.tasks_total, rate))

    print("步数分布(每任务)  : p50={} p90={} p99={} max={}".format(
        percentile(metrics.steps_per_task, 50), percentile(metrics.steps_per_task, 90),
        percentile(metrics.steps_per_task, 99),
        max(metrics.steps_per_task) if metrics.steps_per_task else 0))

    print("工具调用          : {} 次，错误 {} 次 ({:.1%})".format(
        total_calls, total_errors, (total_errors / total_calls) if total_calls else 0.0))
    for tool, count in metrics.tool_calls.most_common():
        errs = metrics.tool_errors.get(tool, 0)
        types = metrics.tool_error_types.get(tool)
        detail = ""
        if types:
            detail = "  错误类型: " + ", ".join("{}×{}".format(k, v) for k, v in types.items())
        print("    {:<18} 调用 {} 次 / 错误 {}{}".format(tool, count, errs, detail))

    no_progress_rate = (metrics.tasks_no_progress / metrics.tasks_total) if metrics.tasks_total else 0.0
    print("无进展率          : {}/{} = {:.1%}".format(
        metrics.tasks_no_progress, metrics.tasks_total, no_progress_rate))

    if metrics.tasks_success:
        print("每次成功成本      : ${:.6f}  (总成本 ${:.6f} / 成功 {} 个任务)".format(
            metrics.total_cost / metrics.tasks_success, metrics.total_cost, metrics.tasks_success))
    else:
        print("每次成功成本      : 无成功任务，总成本 ${:.6f}".format(metrics.total_cost))
    print("成本分布          : " + " | ".join(
        "{}: ${:.6f}".format(model, cost) for model, cost in metrics.cost_by_model.most_common()))

    print("模型调用          : {} 次".format(metrics.llm_calls))
    print("Trace 采样        : {} 条任务 trace，保留 {} 条（采样率 {:.1%}）".format(
        metrics.traces_total, metrics.traces_sampled,
        (metrics.traces_sampled / metrics.traces_total) if metrics.traces_total else 0.0))
    print("Trace 落盘        : {}  ({} 条)".format(exporter.path, exporter.exported))
    print("任务明细          : " + " | ".join(
        "{}: {}步/{}".format(r["task_id"], r["steps"], "成功" if r["success"] else "失败")
        for r in results))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ch33 可观测性最小实现")
    parser.add_argument("--live", action="store_true", help="用真实 API 跑一轮任务（需 OPENAI_API_KEY）")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "demo-live"))
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--audit", default="traces.jsonl", help="trace 落盘路径")
    parser.add_argument("--sampling", default="always", choices=["always", "ratio", "error_or_slow"],
                        help="采样策略（按任务粒度）")
    parser.add_argument("--ratio", type=float, default=0.5, help="ratio 模式下的保留比例")
    parser.add_argument("--slow-ms", type=int, default=2000, help="error_or_slow 模式下的慢 trace 阈值")
    args = parser.parse_args(argv)

    if args.live and not os.environ.get("OPENAI_API_KEY"):
        print("[!] --live 需要环境变量 OPENAI_API_KEY；未设置时可用 mock 模式跑通全回路。")
        return 2

    exporter = JsonlExporter(args.audit)
    sampler = SamplingPolicy(mode=args.sampling, ratio=args.ratio, slow_ms=args.slow_ms)
    metrics = Metrics()
    tracer = Tracer()
    results: List[Dict[str, Any]] = []

    if args.live:
        print("live 模式：真实 API 单轮任务")
        result = live_task(tracer, metrics, args.model, args.base_url,
                           os.environ.get("OPENAI_API_KEY", ""))
        results.append(result)
        spans = tracer.spans
        keep = sampler.decide(spans)
        metrics.traces_total += 1
        if keep:
            metrics.traces_sampled += 1
            exporter.export(tracer.trace_id, spans, True)
    else:
        for scenario in mock_scenarios():
            print("=" * 74)
            print("任务 {}  {}".format(scenario["task_id"], scenario["title"]))
            tracer.spans.clear()
            result = run_task(tracer, metrics, scenario)
            results.append(result)
            spans = tracer.spans
            keep = sampler.decide(spans)
            metrics.traces_total += 1
            if keep:
                metrics.traces_sampled += 1
                exporter.export(tracer.trace_id, spans, True)
            print("  步数={}  结果={}  span 数={}".format(
                result["steps"], "成功" if result["success"] else "失败", len(spans)))
            for span in spans:
                if span.name in ("llm.chat", "tool.call"):
                    detail = ", ".join("{}={}".format(k, v) for k, v in span.attrs.items()
                                       if k in ("llm.model", "cost.total", "tool.name",
                                                "error.type", "retry.count"))
                    if span.name == "llm.chat":
                        # span 的实测耗时与模型上报的延迟是两回事，都要看
                        detail += ", 上报延迟={}ms".format(span.attrs.get("llm.latency.total_ms"))
                    print("    {:<12} {:<5} 实测{:>5}ms  {}".format(
                        span.name, span.status, span.duration_ms, detail))

    print_sli(metrics, exporter, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
