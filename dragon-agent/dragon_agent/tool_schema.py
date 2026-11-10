"""从类型注解生成 JSON Schema（可选的便利层）。

**先说清楚它不做什么**：它不生成工具描述。描述是模型真正读的那部分，
必须人写——生成器只能产出结构，产不出"这个参数什么时候该填、填什么格式"。

两种声明方式各有代价：

| 方式 | 好处 | 代价 |
| --- | --- | --- |
| 显式 JSON Schema（``tools.py`` 现在的做法） | 结构与描述写在一处，一眼能看出模型看到什么 | 字段多了啰嗦，两处容易不同步 |
| 类型注解生成（本模块） | 与函数签名一致，改签名自动改 schema | 生成的结构常常缺"什么时候用"的信息 |

``dragon-agent`` 选显式，因为工具的 schema 同时挑两副担子：**给模型看的契约**
和**执行前的校验依据**。两副担子都要求"写给人看"，注解生成器只擅长后者。
但字段特别多、而且描述能塞进 docstring 的工具，用本模块生成更省事。

生成的 schema 只是 draft 2020-12 的一个子集：``type`` / ``properties`` /
``required`` / ``enum`` / ``items``——正好是 ``tools.py`` 的校验器支持的那部分。
"""
from __future__ import annotations

import enum
import inspect
import types
import typing
from typing import Any, Callable

__all__ = ["schema_for", "schema_for_signature"]

#: Python 类型 → JSON Schema 的 ``type``。
_PRIMITIVES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _schema_for_type(annotation: Any) -> dict[str, Any]:
    """单个类型注解 → JSON Schema 片段。认不出来的退回 object。"""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {"type": "object"}

    if annotation in _PRIMITIVES:
        return {"type": _PRIMITIVES[annotation]}

    origin = typing.get_origin(annotation)

    # Literal["a", "b"] → 枚举
    if origin is typing.Literal:
        values = list(typing.get_args(annotation))
        kinds = {type(v) for v in values}
        kind = "string"
        if kinds == {int}:
            kind = "integer"
        elif kinds == {bool}:
            kind = "boolean"
        elif kinds == {float}:
            kind = "number"
        return {"type": kind, "enum": values}

    # Optional[X] / X | None → 取 X 的 schema（可选性体现在 required 上）
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _schema_for_type(args[0])
        return {"type": "object"}

    # list[X] / list → 数组
    if origin in (list, set, tuple):
        args = typing.get_args(annotation)
        items = _schema_for_type(args[0]) if args else {"type": "object"}
        return {"type": "array", "items": items}

    if origin is dict:
        return {"type": "object"}

    # 枚举类 → 枚举
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        values = [m.value for m in annotation]
        kind = "string"
        if values and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            kind = "integer"
        return {"type": kind, "enum": values}

    return {"type": "object"}


def schema_for_signature(fn: Callable[..., Any]) -> dict[str, Any]:
    """从函数签名生成 schema。

    解析不出类型注解时（比如引用了没导入的名字），返回空 schema 而不是抛异常——
    生成器是便利层，不该成为注册的障碍。
    """
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        annotation = hints.get(name, param.annotation)
        properties[name] = _schema_for_type(annotation)
        if param.default is inspect.Parameter.empty:
            required.append(name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def schema_for(fn: Callable[..., Any]) -> dict[str, Any]:
    """从函数签名生成 JSON Schema。"""
    return schema_for_signature(fn)
