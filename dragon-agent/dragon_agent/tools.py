"""工具层：注册、参数校验、执行、错误回灌、幂等。

设计要点（对应第一卷第 8 章）：
- 工具描述就是给模型看的提示词，写不清楚等于把猜错的活儿推给模型。
- 参数校验失败**不抛异常**，返回 ``ToolResult(ok=False)``，由上层回灌给模型自己改。
- 副作用工具用幂等键去重；``replayable=False`` 的工具在重试时必须拒绝执行。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .llm import ToolCall


class ToolError(Exception):
    """工具层内部的致命错误（注册冲突等）。参数错误不走异常路径。"""


@dataclass
class ToolSpec:
    """一个工具的契约。``parameters`` 是 JSON Schema（draft 2020-12 的子集）。"""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    side_effect: bool = False
    replayable: bool = True


@dataclass
class ToolResult:
    """工具执行结果。``ok=False`` 时 ``error`` 是给模型看的话术。"""

    ok: bool
    output: Any = None
    error: str | None = None
    duration_ms: int = 0
    idempotent_hit: bool = False

    def render(self) -> str:
        """回灌给模型的文本。错误也回灌——模型只有看到错误才有机会改对。"""
        if self.ok:
            if isinstance(self.output, str):
                return self.output
            try:
                return json.dumps(self.output, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return str(self.output)
        return f"[工具 {self.error}]" if self.error else "[工具执行失败]"


class ToolRegistry:
    """工具注册表。

    幂等语义：``invoke`` 带 ``idempotency_key`` 时，同一个 key 第二次调用直接返回
    第一次的结果且**不再执行函数**。但如果工具声明 ``replayable=False``，
    同一个 key 再次调用会被**拒绝执行**并返回错误——因为这类工具（比如划款）
    的副作用不可回退，自动重试等于重复扣款。
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._fns: dict[str, Callable[..., Any]] = {}
        self._seen_keys: dict[str, ToolResult] = {}
        self._invocations: list[str] = []

    # ------------------------------------------------------------------ 注册

    def register(self, spec: ToolSpec, fn: Callable[..., Any]) -> None:
        if spec.name in self._specs:
            raise ToolError(f"工具 {spec.name} 已注册")
        self._specs[spec.name] = spec
        self._fns[spec.name] = fn

    def tool(self, name: str, description: str, parameters: dict[str, Any] | None = None,
             side_effect: bool = False, replayable: bool = True):
        """装饰器形式的注册。"""

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.register(ToolSpec(name=name, description=description,
                                   parameters=parameters or {"type": "object", "properties": {}},
                                   side_effect=side_effect, replayable=replayable), fn)
            return fn

        return deco

    def get(self, name: str) -> tuple[ToolSpec, Callable[..., Any]]:
        if name not in self._specs:
            raise ToolError(f"未注册的工具：{name}")
        return self._specs[name], self._fns[name]

    def schemas(self) -> list[dict[str, Any]]:
        """喂给 ``llm.complete(tools=...)`` 的参数列表。"""
        out = []
        for spec in self._specs.values():
            out.append({
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            })
        return out

    @property
    def names(self) -> list[str]:
        return list(self._specs.keys())

    @property
    def invocations(self) -> list[str]:
        """按执行顺序记录的工具调用名——用于测试断言"到底执行了几次"。"""
        return list(self._invocations)

    # ------------------------------------------------------------------ 执行

    def validate(self, spec: ToolSpec, args: dict[str, Any]) -> str | None:
        """按 JSON Schema 的必要部分校验参数。返回错误信息，合法则返回 None。

        只实现三类检查：类型（object/string/number/integer/boolean/array）、
        必填字段、枚举。这已经能拦住绝大多数模型幻觉参数。
        """
        schema = spec.parameters or {}
        props: dict[str, Any] = schema.get("properties", {}) or {}
        required: list[str] = list(schema.get("required", []) or [])
        types = {
            "string": str, "number": (int, float), "integer": int,
            "boolean": bool, "array": list, "object": dict,
        }

        for key in required:
            if key not in args:
                return f"缺少必填参数 {key}"

        for key, value in args.items():
            if key not in props:
                continue  # 多余参数不致命，忽略即可
            expect = props[key].get("type")
            if expect in types:
                py_t = types[expect]
                if expect in ("number", "integer") and isinstance(value, bool):
                    return f"参数 {key} 应为 {expect}，实际是 boolean"
                if not isinstance(value, py_t):
                    return f"参数 {key} 应为 {expect}，实际是 {type(value).__name__}"
            enum = props[key].get("enum")
            if enum and value not in enum:
                return f"参数 {key} 只能取 {enum} 之一，收到 {value!r}"
        return None

    def invoke(self, name: str, args: dict[str, Any],
               idempotency_key: str | None = None) -> ToolResult:
        """执行一个工具。**不抛异常**，所有失败都变成 ``ToolResult(ok=False)``。"""
        if name not in self._specs:
            return ToolResult(ok=False, error=f"未知工具 {name}")
        spec, fn = self._specs[name], self._fns[name]

        if idempotency_key:
            if idempotency_key in self._seen_keys:
                if not spec.replayable:
                    return ToolResult(
                        ok=False,
                        error=f"{name} 不可重放，幂等键 {idempotency_key} 已用过，拒绝重复执行",
                    )
                return ToolResult(ok=True, output=self._seen_keys[idempotency_key].output,
                                  idempotent_hit=True)
        else:
            # 没给幂等键：不可重放的副作用工具直接拒绝执行
            if spec.side_effect and not spec.replayable:
                return ToolResult(ok=False, error=f"{name} 是不可重放的副作用工具，必须提供幂等键")

        err = self.validate(spec, args)
        if err:
            return ToolResult(ok=False, error=err)

        started = time.time()
        try:
            out = fn(**args)
        except TypeError as e:
            return ToolResult(ok=False, error=f"调用参数不匹配：{e}")
        except Exception as e:  # 工具内部异常也要回灌给模型
            return ToolResult(ok=False, error=f"{type(e).__name__}: {e}",
                              duration_ms=int((time.time() - started) * 1000))
        res = ToolResult(ok=True, output=out, duration_ms=int((time.time() - started) * 1000))
        if idempotency_key:
            self._seen_keys[idempotency_key] = res
        self._invocations.append(name)
        return res


def default_registry() -> ToolRegistry:
    """一组内置示例工具：读文件、算术、带副作用的"下单"。"""
    from pathlib import Path

    reg = ToolRegistry()

    @reg.tool(
        name="read_text_file",
        description="读取工作目录下某个文本文件的全部内容。用于查看项目里的配置或源码。",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    def read_text_file(path: str) -> str:
        p = Path(path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"文件不存在：{path}")
        return p.read_text(encoding="utf-8")

    @reg.tool(
        name="add",
        description="把两个整数相加，返回它们的和。",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    )
    def add(a: int, b: int) -> int:
        return a + b

    @reg.tool(
        name="submit_order",
        description="提交一张订单。**会产生不可撤销的副作用**，必须带幂等键。",
        parameters={
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "qty": {"type": "integer"},
            },
            "required": ["sku", "qty"],
        },
        side_effect=True,
        replayable=False,
    )
    def submit_order(sku: str, qty: int) -> dict[str, Any]:
        return {"order_id": f"ORD-{sku}-{qty}", "sku": sku, "qty": qty, "status": "submitted"}

    @reg.tool(
        name="send_report",
        description="发送日报。幂等工具：同一个幂等键重复调用不会重复发送。",
        parameters={
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
        side_effect=True,
        replayable=True,
    )
    def send_report(content: str) -> dict[str, Any]:
        return {"sent": True, "chars": len(content)}

    return reg
