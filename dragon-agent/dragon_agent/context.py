"""上下文装配器：把提示词变成资源分配。

这一层回答一个问题：**这一次请求的上下文窗口，每一段装什么、装多少、
超了先砍谁。**

四个操作，顺序固定：

1. **分层**——把输入切成 system / tools / task / retrieval / history 五层，
   每层挂一个优先级；
2. **检索**——从检索候选里挑与任务最相关的，相关的排前面；
3. **压缩**——装不下的文档做抽取式压缩，只留含约束信号的句子；
4. **淘汰**——按优先级丢弃，历史从最旧的开始丢。

三条硬约束（来自契约）：

- **稳定块在前**：system 与 tools 永远排在最前面。它们的字节在多轮之间
  不变，提示缓存的前缀才能命中。装配顺序固定为
  ``system → tools → task → retrieval → history``。
- **完全确定**：同样的输入，``render()`` 的输出逐字相同。这里不读时间、
  不读随机数、不依赖字典遍历顺序——所以排序都要有稳定的并列名次规则。
- **纯函数**：不修改调用方传进来的列表，重复调用不改变任何外部状态。

token 数是**估算**的：纯标准库、没有分词器，所以按"CJK 每字 1 个、
其余每 4 字符 1 个"折算。它不需要精确，只需要**稳定**——预算分配要的是
同一份输入每次算出同一个数。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = [
    "ContextBlock",
    "BudgetReport",
    "AssembledContext",
    "ContextAssembler",
    "estimate_tokens",
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_TOKEN_BUDGET",
    "LAYER_ORDER",
    "LAYER_PRIORITY",
]

# --------------------------------------------------------------------------- 常量

LAYER_SYSTEM = "system"
LAYER_TOOLS = "tools"
LAYER_TASK = "task"
LAYER_RETRIEVAL = "retrieval"
LAYER_HISTORY = "history"

#: 渲染顺序 = 块顺序。稳定块（system / tools）在前，提示缓存前缀才命中。
LAYER_ORDER: tuple[str, ...] = (
    LAYER_SYSTEM,
    LAYER_TOOLS,
    LAYER_TASK,
    LAYER_RETRIEVAL,
    LAYER_HISTORY,
)

#: 每层的优先级。数字越大越不会被裁。
LAYER_PRIORITY: dict[str, int] = {
    LAYER_SYSTEM: 100,
    LAYER_TOOLS: 90,
    LAYER_TASK: 80,
    LAYER_RETRIEVAL: 50,
    LAYER_HISTORY: 10,
}

#: 检索层与历史层瓜分剩余预算的比例。检索优先于历史。
RETRIEVAL_WEIGHT = 0.6
HISTORY_WEIGHT = 0.4

#: 与 ``Config.token_budget`` 的默认值一致。
DEFAULT_TOKEN_BUDGET = 120_000

#: 默认系统提示。它同时是"关键约束"——所以它排在整个上下文的最前面。
DEFAULT_SYSTEM_PROMPT = """你是 dragon-agent 的执行助手。必须遵守：
1. 事实只能来自工具输出，不要凭空编造。
2. 工具失败时读懂回灌的错误并修正调用，不要重复同一个失败调用。
3. 信息不足就说明缺什么，不要猜。
4. 只做任务要求的动作，不顺手做别的。"""

#: CJK 字符（含全角标点与假名）。按 1 个 token 折算。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\u3000-\u303f\uff00-\uffef]")

#: 句子切分：中英文句末标点与换行。
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;\n])")

#: 约束信号词。含这些信号的句子在压缩时会被保留。
_SIGNAL_WORDS: tuple[str, ...] = (
    "必须", "禁止", "不得", "只能", "需要", "要求", "注意", "警告",
    "上限", "不能", "不可", "至少", "最多", "不可逆",
)


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数。

    规则：CJK 字符每个算 1 个 token；其余字符每 4 个算 1 个（向上取整）。

    这是估算，不是分词器的结果——但它**确定**：同样的文本永远算出同一个数。
    预算分配需要的正是这种确定性。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def _terms(text: str) -> set[str]:
    """分词：CJK 逐字，ASCII 按词。用于检索打分。"""
    lowered = text.lower()
    terms = {c for c in lowered if _CJK_RE.match(c)}
    terms.update(re.findall(r"[a-z0-9_]+", lowered))
    return terms


def compress(text: str) -> str:
    """抽取式压缩：只保留含约束信号的句子。

    不改写、不摘要——只**挑**。挑出来的句子原文不变，所以压缩结果仍然
    可以被断言、被回放。没有任何句子含信号时，保留第一句（至少给模型
    一个抓手），这个规则同样确定。
    """
    if not text:
        return ""
    sentences = [s for s in _SENTENCE_RE.split(text) if s.strip()]
    if not sentences:
        return text
    kept = [s for s in sentences if any(w in s for w in _SIGNAL_WORDS)]
    if not kept:
        kept = sentences[:1]
    return "".join(kept).strip()


#: 截断标记。截断时它会占掉一点预算，所以算长度时必须先扣掉。
TRUNCATION_MARKER = "…（已截断）"


def truncate_to_tokens(text: str, limit: int) -> str:
    """按 token 预算截断文本。截断处补一个省略标记。

    截断是确定性的：按字符比例折算，不看任何外部状态。标记本身也占 token，
    所以先把它从预算里扣掉——否则截完会超限。
    """
    if limit <= 0:
        return ""
    tokens = estimate_tokens(text)
    if tokens <= limit:
        return text
    room = max(0, limit - estimate_tokens(TRUNCATION_MARKER))
    total = len(text)
    if total == 0:
        return ""
    keep = max(1, int(total * room / max(1, tokens)))
    out = text[:keep].rstrip() + TRUNCATION_MARKER
    # 保险：折算是近似的，可能还差一点，再削几个字符。
    while estimate_tokens(out) > limit and keep > 1:
        keep = max(1, keep - max(1, estimate_tokens(out) - limit))
        out = text[:keep].rstrip() + TRUNCATION_MARKER
    return out


# --------------------------------------------------------------------------- 数据结构


@dataclass
class ContextBlock:
    """装配结果里的一段上下文。"""

    layer: str
    text: str
    tokens: int
    priority: int


@dataclass
class BudgetReport:
    """这一轮装配的账：各层实际占多少、超出多少、丢了什么。"""

    per_layer: dict[str, int] = field(default_factory=dict)
    total: int = 0
    budget: int = 0
    dropped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_layer": dict(self.per_layer),
            "total": self.total,
            "budget": self.budget,
            "dropped": list(self.dropped),
        }


@dataclass
class AssembledContext:
    """装配好的上下文：块 + 账。"""

    blocks: list[ContextBlock] = field(default_factory=list)
    report: BudgetReport = field(default_factory=BudgetReport)

    def render(self) -> str:
        """渲染成给模型的文本。

        同样的块永远渲染出同样的字符串——这是提示缓存命中率可复现的前提。
        """
        return "\n\n".join(f"[{b.layer}]\n{b.text}" for b in self.blocks)

    def block_for(self, layer: str) -> ContextBlock | None:
        """按层取块。"""
        for b in self.blocks:
            if b.layer == layer:
                return b
        return None


# --------------------------------------------------------------------------- 装配器


class ContextAssembler:
    """按预算把上下文拼起来。

    构造参数刻意宽松：``config`` 给出预算，``token_budget`` 可以直接覆盖；
    两者都不给就用默认值。测试与工具可以只给一个数字。
    """

    def __init__(
        self,
        config: Any = None,
        token_budget: int | None = None,
        *,
        system_prompt: str | None = None,
    ) -> None:
        self.config = config
        if token_budget is not None:
            self.token_budget = int(token_budget)
        elif config is not None:
            self.token_budget = int(getattr(config, "token_budget", 0) or 0) or DEFAULT_TOKEN_BUDGET
        else:
            self.token_budget = DEFAULT_TOKEN_BUDGET
        self.system_prompt = DEFAULT_SYSTEM_PROMPT if system_prompt is None else system_prompt

    # ------------------------------------------------------------------ 主入口

    def assemble(
        self,
        task: str,
        history: list[dict] | None = None,
        retrieval: list[str] | None = None,
        tools: list[dict] | None = None,
    ) -> AssembledContext:
        """装配一次上下文。**不修改任何入参。**"""
        history = list(history or [])
        retrieval = list(retrieval or [])
        tools = list(tools or [])

        # 1) 分层
        system_text = self.system_prompt
        tools_text = self._render_tools(tools)
        task_text = str(task or "")
        retrieval_texts = self._select_retrieval(task_text, retrieval)
        history_items = self._prepare_history(history)

        # 2) 固定块：完整保留，不裁。它们是提示缓存的前缀。
        system_tokens = estimate_tokens(system_text)
        tools_tokens = estimate_tokens(tools_text)
        fixed_tokens = system_tokens + tools_tokens
        variable_budget = max(0, self.token_budget - fixed_tokens)
        dropped: list[str] = []

        # 3) 任务优先：它是这一轮要完成的事。
        task_tokens = estimate_tokens(task_text)
        task_quota = min(task_tokens, variable_budget)
        if variable_budget == 0 and task_text:
            dropped.append("task: 预算已被固定块吃光，任务未能进入上下文")
        if task_tokens > task_quota:
            task_text = compress(task_text)
            task_tokens = estimate_tokens(task_text)
            if task_tokens > task_quota:
                task_text = truncate_to_tokens(task_text, task_quota)
                task_tokens = estimate_tokens(task_text)
                dropped.append(f"task: 压缩后仍超预算，截断到 {task_tokens} token")
            else:
                dropped.append("task: 压缩后放进预算")

        # 4) 剩余额度的分配：先按配额保底，再把没人用的额度按优先级让出去。
        #    配额防止某一层吃光窗口；优先级决定"谁先拿到富余的额度"。
        remaining = max(0, variable_budget - task_tokens)
        ret_quota = int(remaining * RETRIEVAL_WEIGHT / (RETRIEVAL_WEIGHT + HISTORY_WEIGHT))
        hist_quota = remaining - ret_quota

        need_retr = sum(estimate_tokens(c) for c in retrieval_texts)
        need_hist = sum(cost for _, cost in history_items)
        alloc_retr = min(need_retr, ret_quota)
        alloc_hist = min(need_hist, hist_quota)
        space = remaining - alloc_retr - alloc_hist
        if space > 0:
            give = min(need_retr - alloc_retr, space)
            alloc_retr += give
            space -= give
        if space > 0:
            give = min(need_hist - alloc_hist, space)
            alloc_hist += give
            space -= give

        # 5) 检索层：按相关度依次装。
        kept_retrieval, retrieval_tokens, retrieval_dropped = self._fit_retrieval(
            retrieval_texts, alloc_retr
        )
        dropped.extend(retrieval_dropped)
        retrieval_text = "\n".join(f"- {c}" for c in kept_retrieval)

        # 6) 历史层：从最新往回装，装不下的旧对话整条淘汰。
        kept_history, history_tokens, evicted = self._fit_history(history_items, alloc_hist)
        if evicted:
            dropped.append(f"history: 淘汰 {evicted} 条较早的对话")
        history_text = "\n".join(kept_history)

        # 6) 拼块。空块不进 blocks，但仍然在账上。
        texts = {
            LAYER_SYSTEM: system_text,
            LAYER_TOOLS: tools_text,
            LAYER_TASK: task_text,
            LAYER_RETRIEVAL: retrieval_text,
            LAYER_HISTORY: history_text,
        }
        blocks: list[ContextBlock] = []
        per_layer: dict[str, int] = {}
        for layer in LAYER_ORDER:
            text = texts[layer]
            tokens = estimate_tokens(text)
            per_layer[layer] = tokens
            if text:
                blocks.append(ContextBlock(
                    layer=layer,
                    text=text,
                    tokens=tokens,
                    priority=LAYER_PRIORITY[layer],
                ))

        total = sum(per_layer.values())
        report = BudgetReport(
            per_layer=per_layer,
            total=total,
            budget=self.token_budget,
            dropped=dropped,
        )
        return AssembledContext(blocks=blocks, report=report)

    # ------------------------------------------------------------------ 操作一：分层

    def _render_tools(self, tools: Sequence[dict]) -> str:
        """工具层渲染成**名册**：名字 + 参数（带 * 的是必填）。

        只放名册，不放完整 schema——完整 schema 走 API 的 ``tools`` 参数，
        同一段描述在一次请求里出现两遍就是白烧 token。
        """
        lines: list[str] = []
        for spec in tools:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("name") or "").strip()
            if not name:
                continue
            params = spec.get("parameters") or {}
            if not isinstance(params, dict):
                params = {}
            props = params.get("properties") or {}
            required = list(params.get("required") or [])
            names = list(props.keys()) if isinstance(props, dict) else []
            if not names:
                names = required
            args = ", ".join(f"{p}*" if p in required else str(p) for p in names)
            lines.append(f"- {name}({args})")
        return "\n".join(lines)

    def _prepare_history(self, history: list[dict]) -> list[tuple[str, int]]:
        """把历史渲染成文本并算出各自的 token。顺序不变（最旧在前）。"""
        items: list[tuple[str, int]] = []
        for entry in history:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("role") or "user")
            content = str(entry.get("content") or "")
            text = f"[{role}] {content}"
            items.append((text, estimate_tokens(text)))
        return items

    # ------------------------------------------------------------------ 操作二：检索

    def _select_retrieval(self, task: str, candidates: list[str]) -> list[str]:
        """按与任务的相关度给检索候选排序。

        打分 = 任务词表与候选词表的交集大小。**并列时按原始顺序**，
        所以排序完全确定。
        """
        query_terms = _terms(task)
        scored: list[tuple[int, int, str]] = []
        for index, chunk in enumerate(candidates):
            text = str(chunk)
            score = len(query_terms & _terms(text))
            scored.append((-score, index, text))
        scored.sort()
        return [text for _, _, text in scored]

    # ------------------------------------------------------------------ 操作三、四

    def _fit_retrieval(self, chunks: list[str], budget: int) -> tuple[list[str], int, list[str]]:
        """按相关度依次把检索块装进预算。

        单块自己都装不下时，先试着压缩（抽取式）；压缩后还装不下就整块丢掉。
        返回 ``(保留的块, 实际 token, 丢弃说明)``。
        """
        kept: list[str] = []
        used = 0
        dropped: list[str] = []
        for chunk in chunks:
            room = budget - used
            if room <= 0:
                dropped.append("retrieval: 丢弃 1 段（检索额度已用完）")
                continue
            candidate = compress(chunk) if estimate_tokens(chunk) > room else chunk
            cost = estimate_tokens(candidate)
            if cost > room:
                dropped.append(f"retrieval: 丢弃 1 段（{estimate_tokens(chunk)} token 放不下）")
                continue
            kept.append(candidate)
            used += cost
        return kept, used, dropped

    def _fit_history(self, items: list[tuple[str, int]], budget: int) -> tuple[list[str], int, int]:
        """从最新往回装，装不下的旧对话整条淘汰。

        返回 ``(保留的文本, 实际 token, 淘汰条数)``。保留的文本仍按最旧在前排列。
        """
        kept: list[str] = []
        used = 0
        for text, cost in reversed(items):
            if used + cost > budget:
                break
            kept.append(text)
            used += cost
        kept.reverse()
        return kept, used, len(items) - len(kept)

    # ------------------------------------------------------------------ 工具

    def budgets(self) -> dict[str, int]:
        """当前预算配置——给 trace 与调试用。"""
        return {"token_budget": self.token_budget}
