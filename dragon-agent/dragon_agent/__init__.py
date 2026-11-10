"""dragon-agent —— 《开源 Agent 架构技术全书》第二卷《自建 Agent 实践手册》的配套工程。

这是一个**纯标准库**实现的 Agent 框架：不装任何第三方包，不需要 API Key
就能跑通「规划 → 行动 → 观察 → 停止」的完整回路。

对外承诺
--------
本模块 ``__all__`` 里的名字是稳定公共接口；其余一律视为内部实现。
模块之间只允许通过这些公开接口互相调用——这条规则的完整表述见
``agent-book/_plan/_dragon_contract.md``。

为什么用惰性导出
----------------
14 个模块是并行搭起来的。惰性导入（PEP 562）让 ``import dragon_agent``
在任何一个子模块还没就位时也能成功，导入错误被推迟到真正访问那个名字
的时候才暴露——这样定位问题更准，也不会因为一个模块的导入错误把整包
的测试一起拖垮。
"""
from __future__ import annotations

import importlib
from typing import Any

__version__ = "1.0"

# 公共名字 -> 所在子模块。
# 改这张表等于改公共 API，必须同步改契约文档。
_EXPORTS: dict[str, str] = {
    # config.py —— 统一配置
    "Config": "config",
    "ConfigError": "config",
    # llm.py —— 模型接入抽象
    "ToolCall": "llm",
    "Usage": "llm",
    "LLMResponse": "llm",
    "LLMClient": "llm",
    "MockLLM": "llm",
    "OpenAICompatClient": "llm",
    "build_client": "llm",
    # tools.py —— 工具层
    "ToolSpec": "tools",
    "ToolResult": "tools",
    "ToolRegistry": "tools",
    "ToolError": "tools",
    "default_registry": "tools",
    # context.py —— 上下文装配器
    "ContextBlock": "context",
    "BudgetReport": "context",
    "AssembledContext": "context",
    "ContextAssembler": "context",
    # agent.py —— 主循环
    "AgentStep": "agent",
    "AgentResult": "agent",
    "Agent": "agent",
    # state.py —— 状态与检查点
    "TaskState": "state",
    "Checkpoint": "state",
    "StateStore": "state",
    # memory.py —— 记忆
    "MemoryItem": "memory",
    "Memory": "memory",
    # guardrails.py —— 护栏
    "Decision": "guardrails",
    "GuardrailEngine": "guardrails",
    # orchestration.py —— 编排层
    "SubTask": "orchestration",
    "Orchestrator": "orchestration",
    "SingleAgent": "orchestration",
    "Supervisor": "orchestration",
    # mcp_server.py —— MCP 协议接入
    "MCPServer": "mcp_server",
    # observability.py —— 可观测性
    "Span": "observability",
    "Tracer": "observability",
    "CostMeter": "observability",
    # evaluation.py —— 评测
    "EvalCase": "evaluation",
    "EvalReport": "evaluation",
    "Evaluator": "evaluation",
}

__all__ = sorted(_EXPORTS)

# 子模块能不能导入，结果缓存起来（只算一次）。
_LOADABLE: dict[str, bool] = {}


def _module_loadable(module_name: str) -> bool:
    """子模块是否可导入。

    ``__dir__`` 要靠它决定露出哪些名字：暴露一个取不到的名字，会让
    ``unittest`` 的模块扫描在 ``getattr`` 上直接炸掉——它不会替你
    兜住 ``AttributeError``。所以**取不到的名字一律不露**。
    """
    if module_name not in _LOADABLE:
        try:
            importlib.import_module(f".{module_name}", __name__)
        except Exception:  # noqa: BLE001 - 导入失败只影响“露不露名”，不该炸掉 dir()
            _LOADABLE[module_name] = False
        else:
            _LOADABLE[module_name] = True
    return _LOADABLE[module_name]


def __getattr__(name: str) -> Any:
    """惰性取出公共 API。

    子模块缺失或导入失败时，错误信息要指明是哪个名字、哪个模块，
    而不是抛一个裸的 ImportError。
    """
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"dragon_agent 没有导出 {name!r}；公共 API 见 __all__")
    try:
        module = importlib.import_module(f".{module_name}", __name__)
    except ImportError as exc:
        raise AttributeError(
            f"dragon_agent.{name} 需要 {module_name}.py，但导入失败：{exc}"
        ) from exc
    value = getattr(module, name)
    globals()[name] = value  # 缓存住，后续访问不再走 import
    return value


def __dir__() -> list[str]:
    """只露出**取得到**的公共 API。

    取不到的名字不露，否则任何会遍历 ``dir()`` 的工具
    （``unittest`` 的模块扫描就是其中一个）都会在 ``getattr`` 上撞到
    ``AttributeError``，整个测试收集直接失败。
    """
    names = set(globals())
    for name, module_name in _EXPORTS.items():
        if _module_loadable(module_name):
            names.add(name)
    return sorted(names)
