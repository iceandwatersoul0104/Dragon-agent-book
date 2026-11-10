"""测试用的构造适配层。

契约（``_plan/_dragon_contract.md``）把**方法签名**写死了，但有几处
``__init__`` 的形参没有写死：``ContextAssembler`` / ``StateStore`` /
``CostMeter`` / ``Evaluator`` 在契约里只出现了类型，没出现形参列表。
两位业务编委并行实现时，各人选的形参可能不同——这里做一次
「按签名挑参数」的适配，让测试断言的是**行为**，而不是某个构造器的
形参顺序。

这层**不含任何业务判断**：它只负责把实例造出来。
一旦某个构造签名稳定下来，这里的候选列表可以收窄。
"""
from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import inspect
import pathlib
import sys
from types import SimpleNamespace
from typing import Any, Callable, Iterable

# 允许从任意目录调用 `python -m unittest discover` 或直接跑测试文件。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.config import Config  # noqa: E402
from dragon_agent.llm import LLMResponse, MockLLM  # noqa: E402
from dragon_agent.tools import ToolRegistry, ToolSpec  # noqa: E402

# ---------------------------------------------------------------------------
# 通用适配
# ---------------------------------------------------------------------------


def _construct(cls: Any, candidates: Iterable[dict[str, Any]]) -> Any:
    """按 ``__init__`` 的签名挑一组能用的构造参数。

    ``candidates`` 按优先级排列；第一组「所有形参都被签名接受」的胜出。
    """
    try:
        sig = inspect.signature(cls)
    except (TypeError, ValueError):  # pragma: no cover - 内建类型没有签名
        return cls()
    params = sig.parameters
    accepts_any = any(p.kind is p.VAR_KEYWORD for p in params.values())

    fallback: dict[str, Any] | None = None
    for cand in candidates:
        if not cand:
            continue
        if accepts_any:
            if fallback is None or len(cand) > len(fallback):
                fallback = cand
            continue
        if all(name in params for name in cand):
            return cls(**cand)
    if fallback is not None:
        try:
            return cls(**fallback)
        except TypeError:  # pragma: no cover - 兜底失败就退回无参构造
            pass
    return cls()


def module_available(module_name: str) -> bool:
    """``dragon_agent.<module_name>`` 是否已经落地。

    测试据此决定**真跑**还是**跳过**：依赖尚未实现的模块的用例跳过，
    不报错——等模块到位后（集成终验）把 skip 撤掉即可全部真跑。
    只依赖已落地模块的用例一律真跑，不许 skip。
    """
    try:
        return importlib.util.find_spec(f"dragon_agent.{module_name}") is not None
    except (ImportError, ValueError):  # pragma: no cover - 包本身都导入不了
        return False


def find_concrete_class(module_name: str, required_methods: tuple[str, ...],
                        exclude: tuple[str, ...] = ()) -> list[Any]:
    """在模块里找出实现了指定方法的**具体类**（排除 Protocol）。"""
    module = importlib.import_module(f"dragon_agent.{module_name}")
    found: list[Any] = []
    for name, obj in vars(module).items():
        if not isinstance(obj, type):
            continue
        if obj.__module__ != module.__name__:
            continue
        if name in exclude:
            continue
        if getattr(obj, "_is_protocol", False):  # Protocol 本身不算实现
            continue
        if all(callable(getattr(obj, m, None)) for m in required_methods):
            found.append(obj)
    return found


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def make_config(**overrides: Any) -> Config:
    """造一个 mock 模式的配置。``Config`` 是 frozen 的，用 replace 覆盖字段。"""
    cfg = Config.from_env_and_args([])
    return dataclasses.replace(cfg, **overrides) if overrides else cfg


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def make_registry() -> tuple[ToolRegistry, dict[str, int]]:
    """一组确定性工具，外加一个「每个函数被调用了几次」的计数表。

    ``ToolRegistry.invocations`` 只记录**执行成功**的调用；工具内部抛异常时
    它不会被记录，所以要断言「有没有被重试」必须自己数函数调用次数。
    """
    reg = ToolRegistry()
    calls = {"add": 0, "echo": 0, "charge": 0, "flaky": 0}

    def add(a: int, b: int) -> int:
        calls["add"] += 1
        return a + b

    def echo(text: str) -> str:
        calls["echo"] += 1
        return text

    def charge(amount: int) -> dict[str, Any]:
        calls["charge"] += 1
        return {"charged": amount}

    def flaky(amount: int) -> str:
        calls["flaky"] += 1
        raise RuntimeError("下游超时")

    reg.register(
        ToolSpec(
            name="add",
            description="两个整数相加，返回它们的和。",
            parameters={
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
        ),
        add,
    )
    reg.register(
        ToolSpec(
            name="echo",
            description="原样返回一段文本。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        echo,
    )
    reg.register(
        ToolSpec(
            name="charge",
            description="扣款。副作用且不可重放。",
            parameters={
                "type": "object",
                "properties": {"amount": {"type": "integer"}},
                "required": ["amount"],
            },
            side_effect=True,
            replayable=False,
        ),
        charge,
    )
    reg.register(
        ToolSpec(
            name="flaky",
            description="总是失败的工具。副作用且不可重放。",
            parameters={
                "type": "object",
                "properties": {"amount": {"type": "integer"}},
                "required": ["amount"],
            },
            side_effect=True,
            replayable=False,
        ),
        flaky,
    )
    return reg, calls


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


def scripted_llm(*responses: LLMResponse) -> MockLLM:
    """按剧本返回响应的假客户端。"""
    return MockLLM(script=list(responses))


class ExplodingLLM:
    """每次调用都抛异常的假客户端——用来测「run() 永不抛异常给调用方」。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        raise RuntimeError("模型炸了")

    def describe(self) -> str:
        return "exploding-model"


# ---------------------------------------------------------------------------
# 上下文 / 状态 / Agent
# ---------------------------------------------------------------------------


def make_context(config: Config) -> Any:
    from dragon_agent.context import ContextAssembler

    return _construct(ContextAssembler, [
        {"config": config, "token_budget": config.token_budget},
        {"config": config},
        {"token_budget": config.token_budget},
        {"budget": config.token_budget},
        {},
    ])


def make_state(config: Config, task_id: str = "test-task") -> Any:
    from dragon_agent.state import StateStore

    return _construct(StateStore, [
        {"config": config, "task_id": task_id},
        {"config": config},
        {"task_id": task_id},
        {},
    ])


def make_memory(config: Config | None = None) -> Any:
    """找出 memory 模块里的具体实现并造一个。找不到返回 None。"""
    for cls in find_concrete_class("memory", ("write", "recall", "forget"),
                                   exclude=("Memory",)):
        try:
            return _construct(cls, [{"config": config}, {}])
        except TypeError:  # pragma: no cover - 换下一个候选
            continue
    return None


def make_guardrail_engine(config: Config | None = None) -> Any:
    """找出 guardrails 模块里的具体实现并造一个。找不到返回 None。"""
    for cls in find_concrete_class("guardrails", ("before_tool", "after_output"),
                                   exclude=("GuardrailEngine",)):
        try:
            return _construct(cls, [{"config": config}, {}])
        except TypeError:  # pragma: no cover
            continue
    return None


def make_agent(config: Config, llm: Any, registry: ToolRegistry,
               context: Any, state: Any, guards: Any = None,
               tracer: Any = None, meter: Any = None) -> Any:
    """按签名组装 Agent。契约里的形参名和顺序是基准，签名不同就按名字对上。

    ``tracer`` / ``meter`` 是**可选**注入项：只有 Agent 的签名里真有这个
    形参、且调用方明确给了值，才会传进去——这样测试既能拿到 Agent 自己
    的 trace，也不依赖它一定暴露某个属性名。
    """
    from dragon_agent.agent import Agent

    slots: dict[str, Any] = {
        "config": config,
        "llm": llm,
        "tools": registry,
        "tool_registry": registry,
        "context": context,
        "context_assembler": context,
        "assembler": context,
        "state": state,
        "state_store": state,
        "store": state,
        "guards": guards,
        "guardrails": guards,
        "guard": guards,
        "tracer": tracer,
        "observability": tracer,
        "meter": meter,
        "cost_meter": meter,
    }
    kwargs: dict[str, Any] = {}
    try:
        sig = inspect.signature(Agent.__init__)
    except (TypeError, ValueError):  # pragma: no cover
        sig = None
    if sig is not None:
        for name, p in sig.parameters.items():
            if name == "self" or p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
                continue
            value = slots.get(name)
            if value is None:
                if name in ("memory", "mem"):
                    mem = make_memory(config)
                    if mem is not None:
                        kwargs[name] = mem
                continue
            # 必填形参必须传；可选形参只有调用方明确给了才传
            if p.default is p.empty:
                kwargs[name] = value
            elif value is not None:
                kwargs[name] = value
    try:
        return Agent(**kwargs)
    except TypeError:
        # 兜底：按契约里的位置顺序来一遍。
        return Agent(config, llm, registry, context, state)


def make_stack(*, config: Config | None = None, llm: Any = None,
               registry: ToolRegistry | None = None, calls: dict[str, int] | None = None,
               guards: Any = None, tracer: Any = None, meter: Any = None,
               **config_overrides: Any) -> SimpleNamespace:
    """一次组装出「配置 + 假模型 + 工具 + 上下文 + 状态 + Agent」这条链。"""
    cfg = config or make_config(**config_overrides)
    if registry is None:
        registry, calls = make_registry()
    if calls is None:
        calls = {}
    if llm is None:
        llm = scripted_llm()
    context = make_context(cfg)
    state = make_state(cfg)
    agent = make_agent(cfg, llm, registry, context, state, guards, tracer, meter)
    return SimpleNamespace(config=cfg, llm=llm, registry=registry, calls=calls,
                            context=context, state=state, agent=agent, tracer=tracer)


# ---------------------------------------------------------------------------
# 可观测性 / 评测 / 编排
# ---------------------------------------------------------------------------


def make_tracer(config: Config | None = None, name: str = "test-tracer") -> Any:
    from dragon_agent.observability import Tracer

    return _construct(Tracer, [{"name": name}, {"config": config}, {}])


def make_cost_meter(config: Config | None = None) -> Any:
    """造一个 CostMeter。模块没就位就返回 None——它只是成本账的交叉核对裁判。"""
    try:
        from dragon_agent.observability import CostMeter
    except ImportError:
        return None

    prices = {}
    if config is not None:
        prices = {"default": {"in": config.price_in_per_mtok,
                              "out": config.price_out_per_mtok}}
    try:
        return _construct(CostMeter, [
            {"config": config},
            {"prices": prices},
            {"price_in_per_mtok": getattr(config, "price_in_per_mtok", 0.0),
             "price_out_per_mtok": getattr(config, "price_out_per_mtok", 0.0)},
            {},
        ])
    except TypeError:  # pragma: no cover - 构造器形状不认识就放弃交叉核对
        return None


def make_evaluator(config: Config | None = None) -> Any:
    from dragon_agent.evaluation import Evaluator

    return _construct(Evaluator, [{"config": config}, {}])


def find_orchestrator_factory() -> Callable[..., Any] | None:
    """找出「默认编排器」的工厂函数。找不到返回 None。"""
    try:
        module = importlib.import_module("dragon_agent.orchestration")
    except ImportError:  # pragma: no cover
        return None
    for name in ("build_orchestrator", "default_orchestrator", "make_orchestrator",
                 "create_orchestrator", "orchestrator_for", "get_orchestrator"):
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None
