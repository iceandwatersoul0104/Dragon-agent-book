"""计价：把 token 账换算成钱，缓存命中与未命中分开算。

**为什么不把单价写死在代码里**：模型价格几个月就变一次，写死等于埋雷。
单价表从配置进来（``Config.price_in_per_mtok`` 等），本模块只负责**算法**——
这样调价是改配置，不是改代码、不是重新发版。

缓存命中的账怎么算：

- ``cached_input_tokens`` 是 ``input_tokens`` 的**子集**，不是并列的一项；
- 未命中的输入 = ``input_tokens - cached_input_tokens``，按输入单价算；
- 命中的输入 = ``cached_input_tokens``，按缓存单价算（通常明显更低）；
- 输出 token 按输出单价算。

**契约的缺口**：``llm.py`` 的 ``Usage`` 只有 input / output 两个字段，
而真实 API 返回的 ``usage.prompt_tokens_details.cached_tokens`` 目前被丢掉了
（见 ``llm.py`` 的 ``_parse``）。要把缓存命中真正纳入成本账，得先给
``Usage`` 加字段——那是契约改动，按规矩要先改
``_plan/_dragon_contract.md``。本模块先把计价逻辑备好，
那次扩展只需要加字段，不用重写算法。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["TokenCounts", "PriceTable"]

#: 一百万（单价表的计价单位）。
PER_MTOK = 1_000_000


@dataclass(frozen=True)
class TokenCounts:
    """一次调用的 token 账。

    ``cached_input_tokens`` 是 ``input_tokens`` 的子集——缓存命中的那些输入
    token **也计入** ``input_tokens``。算钱时必须先减出来，否则会重复计费。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cached_input_tokens"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} 不能为负，收到 {value}")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError(
                f"cached_input_tokens（{self.cached_input_tokens}）是 input_tokens"
                f"（{self.input_tokens}）的子集，不能超过它"
            )

    @property
    def uncached_input_tokens(self) -> int:
        """没命中缓存、需要按输入原价计费的 token 数。"""
        return self.input_tokens - self.cached_input_tokens

    @property
    def total(self) -> int:
        """本账的 token 合计。与 ``Usage.total`` 同口径：输入加输出。"""
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "total": self.total,
        }

    @classmethod
    def from_usage(cls, usage: Any, cached_input_tokens: int = 0) -> "TokenCounts":
        """从 ``llm.Usage`` 构造。

        ``cached_input_tokens`` 目前要由调用方显式给出——``Usage`` 里还没有
        这个字段（见模块 docstring 里的契约缺口说明）。
        """
        return cls(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cached_input_tokens=int(cached_input_tokens or 0),
        )


@dataclass(frozen=True)
class PriceTable:
    """某个模型的单价表，单位是**美元 / 百万 token**。

    ``cached_input_per_mtok`` 为 ``None`` 表示"缓存价与输入价相同"——
    有的服务不区分，另一些服务缓存价低得多。
    """

    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    cached_input_per_mtok: float | None = None

    @property
    def effective_cached_price(self) -> float:
        return (
            self.input_per_mtok
            if self.cached_input_per_mtok is None
            else self.cached_input_per_mtok
        )

    def cost(self, counts: TokenCounts) -> float:
        """这一次调用的成本（美元）。"""
        return (
            counts.uncached_input_tokens * self.input_per_mtok
            + counts.cached_input_tokens * self.effective_cached_price
            + counts.output_tokens * self.output_per_mtok
        ) / PER_MTOK

    def breakdown(self, counts: TokenCounts) -> dict[str, Any]:
        """可落盘的明细：每一项各算了多少钱。"""
        uncached = counts.uncached_input_tokens * self.input_per_mtok / PER_MTOK
        cached = counts.cached_input_tokens * self.effective_cached_price / PER_MTOK
        output = counts.output_tokens * self.output_per_mtok / PER_MTOK
        return {
            "uncached_input_tokens": counts.uncached_input_tokens,
            "cached_input_tokens": counts.cached_input_tokens,
            "output_tokens": counts.output_tokens,
            "cost_uncached_input_usd": round(uncached, 8),
            "cost_cached_input_usd": round(cached, 8),
            "cost_output_usd": round(output, 8),
            "cost_total_usd": round(uncached + cached + output, 8),
        }
