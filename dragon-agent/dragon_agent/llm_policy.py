"""模型调用的重试与退避：包在任意 ``LLMClient`` 外面。

``llm.py`` 的 ``OpenAICompatClient`` 把网络故障降级成一条
``finish_reason="error"`` 的响应，**不抛异常**。这是对的——一次模型故障
不该让整个任务崩掉。但"降级"和"放弃"之间还差几步：限流了要退避重试，
5xx 要重试，超时可能重试也可能请求其实已经到达了服务端。
本模块补上这几步。

**失败分类只能从响应文本里认**：``llm.py`` 把 HTTP 状态码拍扁成了字符串
（``"[llm] HTTP 429"``）。这是**契约的缺口**——正确的做法是让
``LLMResponse`` 带一个 ``status_code`` 字段，但那要先改
``_plan/_dragon_contract.md``。在改之前，模式匹配集中放在
:func:`classify_failure` 一处：判断逻辑只写一遍，改契约时也只改这一处。

重试不抛异常：重试次数用尽后，把最后那条错误响应原样返回。
调用方看到的东西和不用重试时**完全一样**——重试是透明的。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .llm import LLMResponse

__all__ = [
    "FAILURE_NONE",
    "FAILURE_RATE_LIMITED",
    "FAILURE_SERVER_ERROR",
    "FAILURE_TIMEOUT",
    "FAILURE_TRANSPORT",
    "FAILURE_BAD_RESPONSE",
    "RETRYABLE_FAILURES",
    "classify_failure",
    "RetryPolicy",
    "ResilientLLMClient",
    "request_fingerprint",
]

#: 没有失败。
FAILURE_NONE = "none"
#: 被限流（HTTP 429）。
FAILURE_RATE_LIMITED = "rate_limited"
#: 服务端错误（HTTP 5xx）。
FAILURE_SERVER_ERROR = "server_error"
#: 超时（HTTP 408 或超时类异常）。
FAILURE_TIMEOUT = "timeout"
#: 传输层断了（连不上、连接被重置）。
FAILURE_TRANSPORT = "transport"
#: 响应本身不对（没有 choices、不是 JSON）。
FAILURE_BAD_RESPONSE = "bad_response"

#: 值得重试的失败类别。参数不合法这类错误重试多少次都一样。
RETRYABLE_FAILURES = frozenset({
    FAILURE_RATE_LIMITED,
    FAILURE_SERVER_ERROR,
    FAILURE_TIMEOUT,
    FAILURE_TRANSPORT,
})

_HTTP_RE = re.compile(r"HTTP (\d{3})")


def classify_failure(resp: LLMResponse) -> str:
    """把一条响应判成失败类别。

    ``finish_reason`` 不是 ``"error"`` 就直接算成功——这条判断和
    ``llm.py`` 的降级约定严格对齐。
    """
    if resp.finish_reason != "error":
        return FAILURE_NONE

    text = resp.text or ""
    m = _HTTP_RE.search(text)
    if m:
        code = int(m.group(1))
        if code == 429:
            return FAILURE_RATE_LIMITED
        if code == 408:
            return FAILURE_TIMEOUT
        if 500 <= code < 600:
            return FAILURE_SERVER_ERROR
        return FAILURE_BAD_RESPONSE

    low = text.lower()
    if "timeout" in low or "timed out" in low:
        return FAILURE_TIMEOUT
    if "urlerror" in low or "connection" in low or "reset by peer" in low:
        return FAILURE_TRANSPORT
    return FAILURE_BAD_RESPONSE


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略：最多试几次、等多久。

    退避是**指数**的：第 1 次失败等 ``base_delay_s``，第 2 次等两倍，
    第 3 次等四倍，封顶 ``max_delay_s``。抖动（jitter）用来打散并发请求
    同时重试造成的尖峰——不加抖动，一批请求会整齐地一起重试，一起再失败。
    """

    max_attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 5.0
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts 必须 ≥1，收到 {self.max_attempts}")
        if self.base_delay_s < 0 or self.max_delay_s < 0:
            raise ValueError("延迟不能为负")
        if self.jitter_ratio < 0:
            raise ValueError(f"jitter_ratio 不能为负，收到 {self.jitter_ratio}")

    def delay_for(self, attempt: int, jitter: float = 0.0) -> float:
        """第 ``attempt`` 次失败之后（从 1 起算）该等多久。

        ``jitter`` 取值在 ``[-1, 1]``，用来加噪。测试注入固定的 jitter，
        结果就完全确定。
        """
        raw = self.base_delay_s * (2 ** max(0, attempt - 1))
        raw = min(raw, self.max_delay_s)
        if self.jitter_ratio:
            raw += raw * self.jitter_ratio * max(-1.0, min(1.0, jitter))
        return max(0.0, raw)


def request_fingerprint(messages: list[dict[str, Any]], tools: Any = None) -> str:
    """请求指纹：同样的输入得到同样的指纹。

    用途有两个：成本账按指纹去重（同一个请求不该被计两次钱），
    以及需要幂等的场景（同一个指纹对应同一次请求，重试不该产生第二笔）。
    """
    blob = json.dumps(
        {"messages": messages, "tools": tools},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class ResilientLLMClient:
    """给任意 ``LLMClient`` 套上重试与退避。

    ``sleep`` 与 ``jitter`` 都可以注入：测试因此完全确定，不用真的等。
    ``dedupe=True`` 时，同样的请求指纹只发一次——注意这假设了
    "同样的输入得到同样的输出"，温度大于 0 时这个假设不成立。
    """

    def __init__(
        self,
        inner: Any,
        policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: float = 0.0,
        dedupe: bool = False,
    ) -> None:
        self.inner = inner
        self.policy = policy or RetryPolicy()
        self._sleep = sleep or time.sleep
        self.jitter = jitter
        self.dedupe = dedupe
        self.attempts = 0
        self.history: list[dict[str, Any]] = []
        self._cache: dict[str, LLMResponse] = {}

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        fingerprint = request_fingerprint(messages, tools) if self.dedupe else ""
        if fingerprint and fingerprint in self._cache:
            self.history.append({"attempt": 0, "failure": FAILURE_NONE, "cached": True})
            return self._cache[fingerprint]

        last: LLMResponse | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            self.attempts += 1
            resp = self.inner.complete(messages, tools=tools, **kwargs)
            failure = classify_failure(resp)
            self.history.append({
                "attempt": attempt,
                "failure": failure,
                "text": (resp.text or "")[:200],
            })

            if failure == FAILURE_NONE:
                if fingerprint:
                    self._cache[fingerprint] = resp
                return resp

            last = resp
            if failure not in RETRYABLE_FAILURES:
                break  # 重试也没用：参数错、响应不对
            if attempt == self.policy.max_attempts:
                break

            delay = self.policy.delay_for(attempt, self.jitter)
            if delay > 0:
                self._sleep(delay)

        # 重试用尽：原样返回最后一条错误响应，调用方看不出重试发生过。
        assert last is not None
        return last

    def describe(self) -> str:
        return self.inner.describe()


@dataclass
class AttemptLog:
    """一次调用的尝试记录。给 trace 和测试用。"""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def failures(self) -> list[str]:
        return [e["failure"] for e in self.entries]

    def attempts(self) -> int:
        return len(self.entries)


def iter_failures(responses: Iterable[LLMResponse]) -> list[str]:
    """给一串响应，返回每一条的失败类别。测试里常用。"""
    return [classify_failure(r) for r in responses]
