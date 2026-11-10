"""异常分类：框架里"什么错、按什么策略处理"的唯一口径。

设计要点（对应第二卷第 2 章）：

1. **所有框架异常都继承 :class:`DragonError`**，带三个字段：
   ``kind``（分类）、``retryable``（能不能重试）、``details``（结构化上下文）。
   上层要区分处理策略时，只看这三个字段，不看异常类名。

2. **运行期的可预期失败，优先用返回值表达，不用异常。**
   工具参数不合法返回 ``ToolResult(ok=False)``，模型服务不可用返回
   ``finish_reason="error"`` 的响应——两者都不抛异常。理由是异常会打断
   控制流，而 Agent 的控制流必须把"失败"当成一等公民。
   异常留给两类情况：框架内部不变量被破坏（编程错误），以及
   配置不合法（启动期错误，直接早失败）。

3. **异常在层内传播，在层边界转成返回值。**
   工具函数内部可以抛 :class:`DependencyError`（网络超时，稍后重试），
   ``ToolRegistry.invoke`` 会把它接住，转成 ``ToolResult(ok=False)`` 回灌
   给模型。调用方永远看到同一种形状。

4. **不造同名分身。** 工具层的致命错误已经有 ``tools.ToolError``。
   本文件不再定义一个也叫 ``ToolError`` 的类——两个同名类会让
   ``except ToolError`` 抓不住其中一半。这里的 ``ToolError`` 通过
   模块级 ``__getattr__`` 惰性导出，拿到的是**同一个类对象**。
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "DragonError",
    "ModelError",
    "OrchestrationError",
    "DependencyError",
    "ToolError",
    "classify",
    "policy_for",
    "POLICY",
]


#: 分类 -> 处理策略。策略是人话，但它是**可查询的**，测试能对它断言。
POLICY: dict[str, dict[str, Any]] = {
    "model": {
        "retryable": True,
        "strategy": "退避重试；连续失败则降级或以 status=error 结束任务",
    },
    "tool": {
        "retryable": False,
        "strategy": "参数错回灌给模型自纠；工具注册冲突等致命错误直接退出",
    },
    "orchestration": {
        "retryable": False,
        "strategy": "不重试；结束任务并保留已完成的步骤",
    },
    "dependency": {
        "retryable": True,
        "strategy": "退避重试；超过重试上限转成失败结果，不冒泡成任务崩溃",
    },
    "config": {
        "retryable": False,
        "strategy": "启动期早失败：打印清晰提示并退出，不进入运行期",
    },
    "unknown": {
        "retryable": False,
        "strategy": "按未知错误处理：记日志并结束任务，不自动重试",
    },
}


class DragonError(Exception):
    """框架异常的根类。

    ``kind`` 是分类标识，``retryable`` 是能不能重试——两者都定义在类上，
    因此**按类判断**和**按字段判断**得到同一个答案。子类覆盖类属性，
    实例可以在构造时再覆盖一次（同一类错误在具体场景下重试性不同）。
    """

    kind: str = "unknown"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        if retryable is not None:
            self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        """可落盘的表示。**不要**把密钥类字段塞进 details。"""
        return {
            "type": type(self).__name__,
            "kind": self.kind,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
        }

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} ({self.details})"
        return self.message


class ModelError(DragonError):
    """模型层的致命错误。

    注意边界：模型服务不可用、返回体无法解析这类**运行期**故障，
    ``llm.py`` 会降级成一条 ``finish_reason="error"`` 的响应，不抛异常。
    这里覆盖的是"再降级也没意义"的情况——比如客户端根本构造不出来。
    """

    kind = "model"
    retryable = True


class OrchestrationError(DragonError):
    """编排层的错误：状态机不变量被破坏，控制流没法继续。

    不可重试——重试一个坏了的状态机只会再坏一次。
    """

    kind = "orchestration"
    retryable = False


class DependencyError(DragonError):
    """外部依赖不可用：网络超时、远端 5xx、文件锁住。

    可重试——这类错误往往自己会好。
    """

    kind = "dependency"
    retryable = True


def __getattr__(name: str) -> Any:
    """惰性导出 ``tools.ToolError``。

    用 PEP 562 的模块级 ``__getattr__`` 而不是直接 import，有两个好处：
    一是本文件仍然是**叶子模块**（不依赖工具层），保持依赖方向干净；
    二是避免造出第二个同名 ``ToolError`` 类。
    """
    if name == "ToolError":
        from .tools import ToolError as _ToolError

        return _ToolError
    raise AttributeError(f"dragon_agent.errors 没有 {name!r}")


def classify(exc: BaseException) -> str:
    """把任意异常映射到分类标识。

    优先用异常自带的 ``kind``；对历史遗留的 ``tools.ToolError``
    做一次惰性 isinstance 判断。返回 ``"unknown"`` 表示没归类，
    上层按"不重试、记日志、结束任务"处理。
    """
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str) and kind in POLICY:
        return kind
    from .tools import ToolError as _ToolError

    if isinstance(exc, _ToolError):
        return "tool"
    from .config import ConfigError as _ConfigError

    if isinstance(exc, _ConfigError):
        return "config"
    return "unknown"


def policy_for(exc: BaseException) -> dict[str, Any]:
    """查某个异常该怎么处理。"""
    return dict(POLICY.get(classify(exc), POLICY["unknown"]))
