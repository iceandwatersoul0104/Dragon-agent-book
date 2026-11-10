"""批量工具调用：结果对齐、失败隔离，以及错误回灌的三种形态。

两件事：

**一、并行调用的结果对齐与失败隔离。**
模型一轮可能返回多个工具调用。执行完之后有两个硬要求：

- **对齐**：结果必须按调用顺序一一对应。调用方拿 ``results[i]`` 就一定是
  第 i 个调用的结果，不能靠"按名字猜"；
- **失败隔离**：一个失败不影响其他。模型想一次读三个文件，不能因为第一个
  不存在，后两个就不读了。

**二、错误回灌的三种形态。**
同样是"工具失败了"，模型需要的动作完全不同：

============  ==========================  ==========================
形态          什么时候                    模型该做什么
============  ==========================  ==========================
字段级        参数缺失、类型不对、枚举越界   改参数重试
语义级        权限不足、状态不允许、超额    换策略，不要硬试
环境级        文件不存在、网络不可用、5xx   重试或换条路
============  ==========================  ==========================

回灌文本必须让模型看出**是哪一种**。分不清的话，模型会对"文件不存在"
反复改参数，对"权限不足"反复重试——两种都是在烧 token。

**契约的缺口**：``tools.py`` 的 ``ToolResult.error`` 是一条自由文本，
上面的分类靠模式匹配。真正的做法是给 ``ToolResult`` 加一个 ``error_kind``
字段——那是契约改动，要先改 ``_plan/_dragon_contract.md``。
在改之前，模式匹配集中在本模块一处。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Sequence

from .llm import ToolCall
from .tools import ToolRegistry, ToolResult

__all__ = [
    "FEEDBACK_FIELD",
    "FEEDBACK_POLICY",
    "FEEDBACK_ENVIRONMENT",
    "FEEDBACK_HINTS",
    "classify_feedback",
    "render_feedback",
    "invoke_many",
]

#: 字段级：参数本身不对，改参数重试。
FEEDBACK_FIELD = "field"
#: 语义级：操作不被允许，换策略。
FEEDBACK_POLICY = "policy"
#: 环境级：外部条件不满足，重试或换路。
FEEDBACK_ENVIRONMENT = "environment"

#: 给模型看的一句话提示。
FEEDBACK_HINTS: dict[str, str] = {
    FEEDBACK_FIELD: "请修正参数后重新调用。",
    FEEDBACK_POLICY: "这个操作当前不可用，请换一种做法，或向用户说明情况。",
    FEEDBACK_ENVIRONMENT: "外部条件暂时不满足，可以稍后重试或改用其他来源。",
}

#: 字段级特征。
_FIELD_MARKERS = ("缺少必填参数", "应为", "只能取", "调用参数不匹配")

#: 语义级特征。
_POLICY_MARKERS = (
    "未知工具",
    "不可重放",
    "必须提供幂等键",
    "拒绝重复执行",
    "权限",
    "额度",
)


def classify_feedback(error: str | None) -> str:
    """把一条错误文本判成回灌形态。

    认不出来时归到环境级——这是最保守的选择：让模型重试总比让它改参数安全。
    """
    if not error:
        return FEEDBACK_ENVIRONMENT
    if any(marker in error for marker in _FIELD_MARKERS):
        return FEEDBACK_FIELD
    if any(marker in error for marker in _POLICY_MARKERS):
        return FEEDBACK_POLICY
    return FEEDBACK_ENVIRONMENT


def render_feedback(result: ToolResult) -> str:
    """把结果渲染成回灌给模型的文本。成功时就是原来的输出。"""
    if result.ok:
        return result.render()
    kind = classify_feedback(result.error)
    return f"[{kind}] {result.error} —— {FEEDBACK_HINTS[kind]}"


def _normalize(calls: Iterable[Any]) -> list[tuple[str, dict[str, Any]]]:
    """把 ``ToolCall`` 与 ``(name, args)`` 两种写法统一成后者。"""
    items: list[tuple[str, dict[str, Any]]] = []
    for call in calls:
        if isinstance(call, ToolCall):
            items.append((call.name, dict(call.arguments)))
        else:
            name, args = call
            items.append((name, dict(args or {})))
    return items


def invoke_many(
    registry: ToolRegistry,
    calls: Sequence[Any],
    idempotency_prefix: str | None = None,
    parallel: bool = False,
) -> list[ToolResult]:
    """执行一批工具调用，返回与调用**一一对应**的结果列表。

    ``calls`` 可以是 ``ToolCall`` 列表，也可以是 ``(name, args)`` 元组列表。

    - **对齐**：``results[i]`` 永远对应 ``calls[i]``；
    - **失败隔离**：某个调用失败只影响它自己的那一条结果；
    - **幂等键**：给了 ``idempotency_prefix`` 就按 ``prefix:序号`` 生成幂等键，
      同一批重跑不会重复执行副作用工具；不给就没有幂等保护，
      不可重放的副作用工具会直接拒绝执行。

    ``parallel=True`` 用线程池并发执行，但**返回顺序不变**——顺序是对齐契约
    的一部分，不取决于用不用线程。
    """
    items = _normalize(calls)

    def run(index: int, name: str, args: dict[str, Any]) -> ToolResult:
        key = f"{idempotency_prefix}:{index}" if idempotency_prefix else None
        try:
            return registry.invoke(name, args, idempotency_key=key)
        except Exception as exc:  # 兜底：任何异常都不该带崩整批
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    if not items:
        return []

    if not parallel:
        return [run(i, name, args) for i, (name, args) in enumerate(items)]

    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        futures = [pool.submit(run, i, name, args) for i, (name, args) in enumerate(items)]
        return [future.result() for future in futures]
