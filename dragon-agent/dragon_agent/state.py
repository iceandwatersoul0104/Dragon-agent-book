"""任务状态：唯一真相源、检查点、事件溯源、恢复与回放。

设计要点（对应第二卷第 6、7 章）：

- **TaskState 是唯一真相源**。上下文装配器、记忆、编排都从这里读，
  谁都不许自己存一份副本——两份状态迟早会不一致。
- **每步写一次检查点**。崩溃后能从最后一个检查点恢复，从第几步接着跑。
- **事件溯源**。``record()`` 留下"发生了什么"，``events()`` 读回完整时间线。
  快照回答"现在是什么"，事件回答"怎么变成这样的"。
- **回放**。模型输出不可复现，所以先把响应录下来，再用录下来的响应重跑
  一遍、逐步对比——把"模型的随机性"从"控制流的正确性"里摘出来。
- **已执行动作登记**。工具层自己的幂等键缓存是进程内的，进程一崩就没了；
  跨进程恢复副作用去重，靠的是这份登记。

``snapshot()`` 返回的是**副本**：调用方拿到手随便改，都不会污染真相源。
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .llm import LLMResponse, ToolCall, Usage

__all__ = [
    "TaskState",
    "Checkpoint",
    "StateStore",
    "RecordingLLM",
    "RecordingRegistry",
    "ExecutedActions",
    "RunRecord",
    "ReplayResult",
    "record_run",
    "replay",
    "compare_steps",
    "restore_executed_actions",
]


@dataclass
class TaskState:
    """一次任务的当前状态。"""

    task_id: str
    status: str = "running"
    data: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    version: int = 0

    def to_dict(self) -> dict[str, Any]:
        """可序列化的表示。深拷贝——快照不该和真相源共享内部对象。"""
        return {
            "task_id": self.task_id,
            "status": self.status,
            "data": copy.deepcopy(self.data),
            "steps": copy.deepcopy(self.steps),
            "version": self.version,
        }


@dataclass
class Checkpoint:
    """某一时刻的状态快照，可以拿来恢复。"""

    task_id: str
    version: int
    snapshot: dict
    created_at: float


class StateStore:
    """任务状态的唯一真相源。

    构造参数刻意宽松：``config`` 只是被留着备用（给未来的持久化、路径），
    ``task_id`` 可以显式指定，不给就生成一个。
    """

    def __init__(self, config: Any = None, task_id: str | None = None) -> None:
        self.config = config
        self.task_id = task_id or f"task-{int(time.time() * 1000):x}"
        self._state = TaskState(task_id=self.task_id)
        self._events: list[dict] = []

    # ------------------------------------------------------------------ 读

    def snapshot(self) -> TaskState:
        """当前状态的**副本**。改它不会改到真相源。"""
        return TaskState(
            task_id=self._state.task_id,
            status=self._state.status,
            data=copy.deepcopy(self._state.data),
            steps=copy.deepcopy(self._state.steps),
            version=self._state.version,
        )

    def events(self) -> list[dict]:
        """事件溯源记录，按发生顺序。"""
        return [dict(event) for event in self._events]

    # ------------------------------------------------------------------ 写

    def update(self, **kwargs: Any) -> TaskState:
        """改状态字段（``status``、``data`` 等），返回改完的副本。"""
        for key, value in kwargs.items():
            if not hasattr(self._state, key):
                raise AttributeError(f"TaskState 没有字段 {key!r}")
            setattr(self._state, key, value)
        return self.snapshot()

    def record_step(self, step: dict) -> None:
        """记一步：进 ``steps``，同时留一条事件。"""
        payload = dict(step)
        self._state.steps.append(payload)
        self.record({
            "type": "step",
            "index": payload.get("index"),
            "action": payload.get("action"),
            "tokens": payload.get("tokens"),
            "cost": payload.get("cost"),
        })

    def record(self, event: dict) -> None:
        """记一条事件。事件是"发生了什么"，快照是"现在是什么"。"""
        self._events.append(dict(event))

    def checkpoint(self) -> Checkpoint:
        """写一次检查点：版本号 +1，返回可以恢复的快照。

        每一步结束都该调一次——这是"崩溃后从哪儿接着跑"的依据。
        """
        self._state.version += 1
        return Checkpoint(
            task_id=self._state.task_id,
            version=self._state.version,
            snapshot=self._state.to_dict(),
            created_at=time.time(),
        )

    def restore(self, cp: Checkpoint) -> TaskState:
        """从检查点恢复状态，返回恢复后的副本。"""
        data = cp.snapshot or {}
        self._state = TaskState(
            task_id=data.get("task_id") or cp.task_id,
            status=data.get("status", "running"),
            data=copy.deepcopy(data.get("data") or {}),
            steps=copy.deepcopy(data.get("steps") or []),
            version=int(data.get("version", cp.version)),
        )
        self.record({"type": "restore", "version": self._state.version})
        return self.snapshot()


# --------------------------------------------------------------------------- 录制


class RecordingLLM:
    """包在模型客户端外面，把每次返回的响应录下来。

    这是回放的**唯一前提**：模型响应不可复现（温度、供应商侧变化、并发），
    所以必须先录。录下来之后，回放才有可能做到"同样的输入 → 同样的结果"。
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.responses: list[LLMResponse] = []

    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None = None,
                 **kwargs: Any) -> LLMResponse:
        response = self.inner.complete(messages, tools=tools, **kwargs)
        self.responses.append(response)
        return response

    def describe(self) -> str:
        return self.inner.describe()


class RecordingRegistry:
    """包在工具注册表外面，记录每个**副作用动作**的执行。

    只记成功的副作用动作——失败的调用没造成影响，不需要登记。
    这里是鸭子类型：本模块不 import 工具层，保持依赖方向干净。
    """

    def __init__(self, inner: Any, executed: "ExecutedActions") -> None:
        self.inner = inner
        self.executed = executed

    def invoke(self, name: str, args: dict[str, Any],
               idempotency_key: str | None = None) -> Any:
        result = self.inner.invoke(name, args, idempotency_key=idempotency_key)
        if idempotency_key and getattr(result, "ok", False):
            self.executed.record(idempotency_key, {"tool": name, "output": getattr(result, "output", None)})
        return result

    def schemas(self) -> list[dict[str, Any]]:
        return self.inner.schemas()

    def get(self, name: str) -> Any:
        return self.inner.get(name)

    def __getattr__(self, name: str) -> Any:
        # 其余属性（invocations、names……）透传给内层注册表
        return getattr(self.inner, name)


# --------------------------------------------------------------------------- 已执行动作登记


class ExecutedActions:
    """已执行动作登记：记住哪些副作用**已经发生过**。

    工具层自己的幂等键缓存是**进程内**的——进程一崩就没了。
    崩溃恢复要跨进程，就要把这份登记落到状态层，重启后再装回工具层。
    """

    def __init__(self, entries: dict[str, dict[str, Any]] | None = None) -> None:
        self._entries: dict[str, dict[str, Any]] = dict(entries or {})

    def record(self, key: str, entry: dict[str, Any]) -> None:
        payload = dict(entry)
        payload.setdefault("at", time.time())
        self._entries[key] = payload

    def has(self, key: str) -> bool:
        return key in self._entries

    def get(self, key: str) -> dict[str, Any] | None:
        return self._entries.get(key)

    def entries(self) -> dict[str, dict[str, Any]]:
        return dict(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def to_dict(self) -> dict[str, Any]:
        """可落盘的表示。"""
        return {"entries": dict(self._entries)}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ExecutedActions":
        data = data or {}
        return cls(entries=dict(data.get("entries") or {}))


class _RecordedResult:
    """只带 ``output`` 的最小结果对象。

    工具层的幂等缓存只读 ``.output``，所以恢复时不需要真的构造
    ``ToolResult``——本模块也就不必 import 工具层。
    """

    def __init__(self, output: Any) -> None:
        self.output = output


def restore_executed_actions(registry: Any, executed: ExecutedActions) -> int:
    """把已执行动作登记装回工具注册表，返回装了几条。

    工具层没有公开的注入接口（``_seen_keys`` 是私有的），这里直接写它。
    这是刻意的取舍：跨进程恢复副作用去重，比等工具层加接口更急——
    装回去之后，``replayable=False`` 的工具见到用过的幂等键会**拒绝执行**，
    可重放的工具会直接返回上次的结果。
    """
    count = 0
    for key, entry in executed.entries().items():
        registry._seen_keys[key] = _RecordedResult((entry or {}).get("output"))
        count += 1
    return count


# --------------------------------------------------------------------------- 记录与回放


@dataclass
class RunRecord:
    """一次运行的完整记录。

    ``responses`` 是模型的每次响应——回放的原料；
    ``steps`` 是每一步的结果——对比的依据。
    """

    task: str
    responses: list[LLMResponse] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    final: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "responses": [r.to_dict() for r in self.responses],
            "steps": list(self.steps),
            "final": dict(self.final),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunRecord":
        """从落盘记录恢复。响应按 ``LLMResponse`` 的形状还原。"""
        responses: list[LLMResponse] = []
        for raw in data.get("responses") or []:
            usage = raw.get("usage") or {}
            responses.append(LLMResponse(
                text=raw.get("text", ""),
                tool_calls=[
                    ToolCall(name=call.get("name", ""),
                             arguments=dict(call.get("arguments") or {}))
                    for call in (raw.get("tool_calls") or [])
                ],
                usage=Usage(
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                ),
                finish_reason=raw.get("finish_reason", "stop"),
            ))
        return cls(
            task=data.get("task", ""),
            responses=responses,
            steps=list(data.get("steps") or []),
            final=dict(data.get("final") or {}),
        )


@dataclass
class ReplayResult:
    """回放的结果与差异。"""

    status: str = ""
    answer: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        """回放与原运行完全一致吗？"""
        return not self.differences

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "answer": self.answer,
            "steps": list(self.steps),
            "differences": list(self.differences),
            "identical": self.identical,
        }


def steps_as_dicts(steps: list[Any]) -> list[dict[str, Any]]:
    """把 ``AgentStep`` 列表变成可比较、可序列化的字典列表。"""
    out: list[dict[str, Any]] = []
    for step in steps:
        action = getattr(step, "action", None)
        out.append({
            "index": getattr(step, "index", 0),
            "thought": getattr(step, "thought", ""),
            "action": action.name if action is not None else None,
            "arguments": dict(action.arguments) if action is not None else {},
            "observation": getattr(step, "observation", ""),
            "tokens": getattr(step, "tokens", 0),
            "cost": getattr(step, "cost", 0.0),
        })
    return out


def record_run(build_agent: Callable[[Any], Any], task: str, llm: Any) -> RunRecord:
    """跑一次任务，把模型的每次响应和每一步都录下来。

    ``build_agent`` 接收一个模型客户端，返回一个 Agent——这样录制和回放
    走的是**同一套装配代码**，对比才有意义。
    """
    recorder = RecordingLLM(llm)
    agent = build_agent(recorder)
    result = agent.run(task)
    return RunRecord(
        task=task,
        responses=list(recorder.responses),
        steps=steps_as_dicts(result.steps),
        final={
            "answer": result.answer,
            "status": result.status,
            "stop_reason": result.stop_reason,
            "total_tokens": result.total_tokens,
            "total_cost": result.total_cost,
        },
    )


def compare_steps(original: list[dict[str, Any]],
                  replayed: list[dict[str, Any]]) -> list[str]:
    """逐步对比两次运行，返回差异描述列表。空列表表示完全一致。"""
    differences: list[str] = []
    if len(original) != len(replayed):
        differences.append(f"步数不同：原运行 {len(original)} 步，回放 {len(replayed)} 步")
    for index, (left, right) in enumerate(zip(original, replayed), start=1):
        for field_name in ("thought", "action", "observation", "tokens"):
            if left.get(field_name) != right.get(field_name):
                differences.append(
                    f"第 {index} 步的 {field_name} 不同："
                    f"{left.get(field_name)!r} != {right.get(field_name)!r}"
                )
    return differences


def replay(build_agent: Callable[[Any], Any], record: RunRecord,
           build_client: Callable[[list[LLMResponse]], Any]) -> ReplayResult:
    """用录下的响应重跑一遍，并逐步对比。

    回放**不重新调模型**：响应是录下来的，重放的是控制流。
    这正是它能定位问题的原因——结果不一致，只可能是控制流、工具或上下文变了。

    ``build_client`` 接收响应列表，返回一个按剧本返回它们的模型客户端。
    """
    agent = build_agent(build_client(list(record.responses)))
    result = agent.run(record.task)
    steps = steps_as_dicts(result.steps)
    differences = compare_steps(record.steps, steps)

    final = record.final or {}
    if final.get("answer") != result.answer:
        differences.append(
            f"最终答案不同：{final.get('answer')!r} != {result.answer!r}"
        )
    if final.get("status") != result.status:
        differences.append(
            f"结束状态不同：{final.get('status')!r} != {result.status!r}"
        )
    return ReplayResult(
        status=result.status,
        answer=result.answer,
        steps=steps,
        differences=differences,
    )
