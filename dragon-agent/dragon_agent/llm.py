"""模型接入抽象：mock 与真实 API 共存，业务模块看不出区别。

设计要点：
- ``LLMResponse`` 是唯一的数据形状，mock 与真实客户端返回同一种对象。
- 任何业务模块都**不允许**用 ``isinstance`` 判断客户端类型——那会把 mock 与真实
  的行为差异泄漏到控制流里，测试就测不出线上问题。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolCall:
    """模型请求调用一个工具。``arguments`` 是已解析的字典（不是 JSON 字符串）。"""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": dict(self.arguments)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolCall":
        return cls(name=d.get("name", ""), arguments=dict(d.get("arguments") or {}))


@dataclass
class Usage:
    """token 用量。``total`` 是属性而不是字段，避免两个地方各算一遍算出不同值。"""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)

    def to_dict(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "total": self.total}


@dataclass
class LLMResponse:
    """一次模型调用的结果。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "usage": self.usage.to_dict(),
            "finish_reason": self.finish_reason,
        }


@runtime_checkable
class LLMClient(Protocol):
    """模型客户端协议。``complete`` 是唯一的调用入口。"""

    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None = None,
                 **kwargs: Any) -> LLMResponse:
        ...

    def describe(self) -> str:
        """可读的模型标识，写进 trace 用。"""
        ...


class MockLLM:
    """按剧本返回响应的假客户端。

    剧本用完之后循环使用最后一条——这样写测试时不必关心调用了几次。
    ``script`` 为空时，每次都返回一条空的 stop 响应。
    """

    def __init__(self, script: list[LLMResponse] | None = None, name: str = "mock-model"):
        self.script: list[LLMResponse] = list(script or [])
        self.name = name
        self.calls: list[dict[str, Any]] = []
        self._cursor = 0

    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None = None,
                 **kwargs: Any) -> LLMResponse:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self.script:
            return LLMResponse(text="", usage=Usage(input_tokens=0, output_tokens=0))
        idx = min(self._cursor, len(self.script) - 1)
        self._cursor += 1
        return self.script[idx]

    def describe(self) -> str:
        return self.name

    # ---------------------------------------------------------- 剧本构造辅助

    @staticmethod
    def say(text: str, in_tokens: int = 0, out_tokens: int = 0) -> LLMResponse:
        return LLMResponse(text=text, usage=Usage(in_tokens, out_tokens), finish_reason="stop")

    @staticmethod
    def call(name: str, arguments: dict[str, Any] | None = None,
             in_tokens: int = 0, out_tokens: int = 0) -> LLMResponse:
        return LLMResponse(
            text="",
            tool_calls=[ToolCall(name=name, arguments=dict(arguments or {}))],
            usage=Usage(in_tokens, out_tokens),
            finish_reason="tool_calls",
        )


class OpenAICompatClient:
    """走 OpenAI 兼容的 ``/chat/completions`` 接口。

    缺 api_key 时构造即抛 ``ConfigError``——不要等到发请求才失败。
    网络错误、返回非 JSON、返回体不含 choices，都会退化成一条带错误文本的
    ``LLMResponse``，而不是抛异常：一次模型故障不该让整个任务崩掉。
    """

    def __init__(self, model: str, api_key: str, base_url: str = "https://api.openai.com/v1",
                 timeout_s: int = 60, temperature: float = 0.0):
        if not api_key:
            from .config import ConfigError
            raise ConfigError("OpenAICompatClient 需要 api_key")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.temperature = temperature
        self._calls = 0

    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None = None,
                 **kwargs: Any) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "dragon-agent/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return LLMResponse(text=f"[llm] HTTP {e.code}", finish_reason="error")
        except Exception as e:  # 网络层任何异常都不该冒泡成任务失败
            return LLMResponse(text=f"[llm] {type(e).__name__}: {e}", finish_reason="error")

        return self._parse(data)

    def _parse(self, data: dict[str, Any]) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            return LLMResponse(text="[llm] 响应里没有 choices", finish_reason="error")
        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (TypeError, ValueError):
                args = {"__raw__": raw_args}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args))
        usage_raw = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_raw.get("prompt_tokens") or 0),
            output_tokens=int(usage_raw.get("completion_tokens") or 0),
        )
        finish = choices[0].get("finish_reason") or ("tool_calls" if calls else "stop")
        return LLMResponse(text=text, tool_calls=calls, usage=usage, finish_reason=finish)

    def describe(self) -> str:
        return f"{self.base_url}#{self.model}"


def build_client(config) -> LLMClient:
    """按配置选客户端。``--live`` 走真实 API，否则走 mock。"""
    if getattr(config, "live", False):
        return OpenAICompatClient(
            model=config.model,
            api_key=config.api_key or "",
            base_url=config.base_url,
            timeout_s=config.timeout_s,
            temperature=config.temperature,
        )
    return MockLLM(name=config.model)
