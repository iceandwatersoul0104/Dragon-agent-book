"""编排层：从单 Agent 到多 Agent 的升级路径。

对应第二卷第 10 章。这一章要回答的问题很具体：

    **什么时候才值得把一个 Agent 拆成多个？**

答案不是"任务变复杂了"。多 Agent 的代价是真实的：上下文被切成几份，
信息在交接处磨损，token 账单翻倍，出问题时你要在几个 trace 里找原因。
所以本章的顺序是：**先给判据，判据不满足就别拆**；满足了就再挑拓扑。

四个判据（缺一个都别上多 Agent）
--------------------------------

1. **并行性**——子任务之间真的互不依赖吗？串行能做的，多 Agent 只是多了开销。
2. **上下文隔离**——子任务需要看到的上下文是不是互相干扰？
   （一个子任务塞进另一个子任务的大段背景，两者都会变笨。）
3. **能力异构**——子任务需要**不同的工具或不同的模型**吗？
   同一个 Agent 拿同一套工具重复跑三遍，不是多 Agent，是自嗨。
4. **权限隔离**——子任务需要**不同的授权范围**吗？
   这一条最常被忽略：读公开资料的 Agent 和能扣款的 Agent，
   不该共用一个进程里的一份凭据。

四个都满足，才升级。只满足一两个，通常有更省事的解法
（拆上下文、加缓存、把步骤拆细但仍在同一个 Agent 里）。

四段机制
--------

1. **升级判据**（``UpgradeCriteria``）——纯函数，给结论不给建议。
2. **拓扑选择**（``TopologySpec`` / ``recommend_topology``）——四种拓扑各自的实现代价。
3. **Supervisor**——派单、回收、判定完成，三段式。
4. **共享状态**——共享内存 / 共享文档 / 消息传递，三种代价不同的做法。

外加一组**熔断**：最大轮次、无进展检测、强制收敛、单 Agent 回退。
多 Agent 最常见的失败不是"某个子 Agent 失败了"，而是**永远不收敛**——
一轮轮地派工，一轮轮地回来，账单见底。所以熔断不是可选项。

默认必须是 ``SingleAgent``：多 Agent 是显式选择，不是默认行为。
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from .agent import Agent, AgentResult
from .config import Config
from .context import ContextAssembler
from .llm import build_client
from .state import StateStore
from .tools import default_registry

__all__ = [
    "SubTask",
    "Orchestrator",
    "SingleAgent",
    "Supervisor",
    "UpgradeCriteria",
    "UPGRADE_CRITERIA",
    "TopologySpec",
    "TOPOLOGIES",
    "TOPOLOGY_SINGLE",
    "TOPOLOGY_SUPERVISOR",
    "TOPOLOGY_HIERARCHICAL",
    "TOPOLOGY_MESH",
    "TOPOLOGY_BLACKBOARD",
    "SharedState",
    "SharedMemoryState",
    "SharedDocumentState",
    "MessagePassingState",
    "TaskOutcome",
    "OrchestrationReport",
    "PlanError",
    "parse_plan",
    "parse_continuations",
    "recommend_topology",
    "build_default_agent",
    "build_orchestrator",
]


# ---------------------------------------------------------------------------
# 拓扑常量
# ---------------------------------------------------------------------------

#: 不升级：仍然是一个 Agent。
TOPOLOGY_SINGLE = "single"
#: 编排者派单：中央节点拆任务、派给 worker、回收汇总。
TOPOLOGY_SUPERVISOR = "supervisor"
#: 层级：编排者下面还有编排者，形成树。
TOPOLOGY_HIERARCHICAL = "hierarchical"
#: 网状：Agent 之间直接对话，没有中心。
TOPOLOGY_MESH = "mesh"
#: 黑板：所有 Agent 往同一块共享文档里写，谁都能读。
TOPOLOGY_BLACKBOARD = "blackboard"


# ---------------------------------------------------------------------------
# 升级判据
# ---------------------------------------------------------------------------

#: 四个判据的名字。**缺一个都别上多 Agent。**
UPGRADE_CRITERIA: tuple[str, ...] = (
    "parallel",
    "context_isolation",
    "capability_heterogeneity",
    "permission_isolation",
)


@dataclass
class UpgradeCriteria:
    """多 Agent 的升级判据。

    每个字段是一个**可以当面回答的问题**，不是一个模糊的印象：

    - ``parallel``：子任务之间真的互不依赖吗？
    - ``context_isolation``：子任务需要看到的上下文是不是互相干扰？
    - ``capability_heterogeneity``：子任务需要不同的工具或不同的模型吗？
    - ``permission_isolation``：子任务需要不同的授权范围吗？

    判定是纯函数：同样的判据永远给同样的结论，不看模型怎么想。
    """

    parallel: bool = False
    context_isolation: bool = False
    capability_heterogeneity: bool = False
    permission_isolation: bool = False

    def met(self) -> list[str]:
        """满足了的判据。"""
        return [name for name in UPGRADE_CRITERIA if getattr(self, name)]

    def missing(self) -> list[str]:
        """没满足的判据——这一串就是"先别拆"的清单。"""
        return [name for name in UPGRADE_CRITERIA if not getattr(self, name)]

    def should_upgrade(self, required: Sequence[str] = UPGRADE_CRITERIA) -> bool:
        """该不该升级？

        默认要求**全部满足**——这是本章最反直觉、也最重要的一条：
        多 Agent 的复杂度是固定成本，收益是逐条兑现的，
        少一条就说明这笔账不划算。
        """
        return all(getattr(self, name) for name in required)

    def reason(self, required: Sequence[str] = UPGRADE_CRITERIA) -> str:
        """一句人能读懂的结论。"""
        if self.should_upgrade(required):
            return "四个判据全部满足，可以升级为多 Agent"
        missing = [name for name in required if not getattr(self, name)]
        return "不满足判据（" + "、".join(missing) + "），留在单 Agent"


# ---------------------------------------------------------------------------
# 拓扑
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopologySpec:
    """一种拓扑的实现代价。

    选拓扑不是选"哪个更先进"，是选"哪个的代价你能付"。
    所以这张表记的是代价，不是优点。
    """

    name: str
    label: str
    #: 共享状态怎么做。
    shared_state: str
    #: 派单的代价。
    dispatch_cost: str
    #: 收敛的代价。
    converge_cost: str
    #: 适合什么。
    fits: str
    #: 什么时候会崩。
    breaks: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "label": self.label,
            "shared_state": self.shared_state,
            "dispatch_cost": self.dispatch_cost,
            "converge_cost": self.converge_cost,
            "fits": self.fits,
            "breaks": self.breaks,
        }


TOPOLOGIES: dict[str, TopologySpec] = {
    TOPOLOGY_SINGLE: TopologySpec(
        name=TOPOLOGY_SINGLE,
        label="单 Agent",
        shared_state="不需要——状态本来就在一个进程里",
        dispatch_cost="零：不拆就不派单",
        converge_cost="零：不汇总就一个答案",
        fits="绝大多数任务",
        breaks="上下文互相干扰时；单次任务太长时",
    ),
    TOPOLOGY_SUPERVISOR: TopologySpec(
        name=TOPOLOGY_SUPERVISOR,
        label="Supervisor（编排者）",
        shared_state="编排者持有，worker 通过共享状态读结论",
        dispatch_cost="一次规划调用 + 一次分派；编排者要看得懂全部子任务",
        converge_cost="一次汇总调用；信息在汇总处会磨损",
        fits="子任务可并行、能力域清晰、需要统一口径的汇总",
        breaks="编排者本身成了瓶颈；子任务数一多，摘要就失真",
    ),
    TOPOLOGY_HIERARCHICAL: TopologySpec(
        name=TOPOLOGY_HIERARCHICAL,
        label="层级",
        shared_state="分层共享；下层只看得见上层给它的摘要",
        dispatch_cost="每层一次规划；层数越多，调用越多",
        converge_cost="逐层向上汇总，误差逐层累积",
        fits="子任务本身还能再拆，且有天然的领域划分",
        breaks="层数一多就难调试：出问题不知道是哪一层想歪了",
    ),
    TOPOLOGY_MESH: TopologySpec(
        name=TOPOLOGY_MESH,
        label="网状",
        shared_state="没有中心状态，只有消息",
        dispatch_cost="没有规划成本，但对话轮数不可预测",
        converge_cost="**最难收敛**：没有中心判断「完成」",
        fits="子任务之间需要来回讨论才能定的事",
        breaks="容易无限对话；成本上限很难设",
    ),
    TOPOLOGY_BLACKBOARD: TopologySpec(
        name=TOPOLOGY_BLACKBOARD,
        label="黑板（共享文档）",
        shared_state="一块共享文档，所有人写、所有人读",
        dispatch_cost="低：不用规划，谁有能力谁上",
        converge_cost="要自己判断「黑板写满了没有」",
        fits="子任务可并行、共享同一份中间产物（代码、检索结果）",
        breaks="共享文档会变长；冲突写在文档里没人仲裁",
    ),
}


def recommend_topology(criteria: UpgradeCriteria,
                       required: Sequence[str] = UPGRADE_CRITERIA) -> str:
    """按判据推荐拓扑。

    判据不满 → 返回 ``single``。这是本章最重要的一次"不作为"。

    ``required`` 可以放宽（比如只要求"并行 + 上下文隔离"），
    但放宽意味着**主动放弃**某些判据——调用方得自己说清楚放弃的是哪一条。
    """
    if not criteria.should_upgrade(required):
        return TOPOLOGY_SINGLE
    if criteria.parallel and criteria.context_isolation and not criteria.capability_heterogeneity:
        # 子任务同质、要共享同一份中间产物 → 黑板最省事。
        return TOPOLOGY_BLACKBOARD
    if criteria.capability_heterogeneity and criteria.permission_isolation:
        # 能力域与授权范围都不同 → 编排者集中派单最稳。
        return TOPOLOGY_SUPERVISOR
    # 其余情况：编排者 + 可选的层级拆分。
    return TOPOLOGY_HIERARCHICAL


# ---------------------------------------------------------------------------
# 子任务
# ---------------------------------------------------------------------------


@dataclass
class SubTask:
    """派给某个 worker 的一段任务。

    契约里的两个字段（``name`` / ``prompt``）一个没动，后面三个是增量：
    谁来做、依赖谁、需要哪些共享状态里的结论。
    """

    name: str
    prompt: str
    #: 交给哪个 worker（``workers`` 的键）。空 = 交给唯一那个。
    worker: str = ""
    #: 依赖的其他子任务名；它们跑完才能跑。
    depends_on: tuple[str, ...] = ()
    #: 需要注入的共享状态键（依赖子任务的结论一般写这里）。
    inputs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "prompt": self.prompt,
            "worker": self.worker,
            "depends_on": list(self.depends_on),
            "inputs": list(self.inputs),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubTask":
        return cls(
            name=str(data.get("name") or "").strip(),
            prompt=str(data.get("prompt") or "").strip(),
            worker=str(data.get("worker") or "").strip(),
            depends_on=tuple(data.get("depends_on") or ()),
            inputs=tuple(data.get("inputs") or ()),
        )


@dataclass
class TaskOutcome:
    """一个子任务的执行结果。"""

    name: str
    worker: str
    answer: str = ""
    status: str = "completed"
    tokens: int = 0
    cost: float = 0.0
    steps: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "worker": self.worker,
            "answer": self.answer,
            "status": self.status,
            "tokens": self.tokens,
            "cost": self.cost,
            "steps": self.steps,
            "error": self.error,
        }


@dataclass
class OrchestrationReport:
    """一次编排的账。

    除了最终答案，还要能回答三个问题：拆成了几块、跑了几轮、
    为什么停下来（正常收敛 / 撞上限 / 无进展 / 回退）。
    """

    rounds: int = 0
    outcomes: list[TaskOutcome] = field(default_factory=list)
    fell_back: bool = False
    stop_reason: str = ""
    topology: str = TOPOLOGY_SINGLE
    shared_state: str = ""
    total_tokens: int = 0
    total_cost: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "fell_back": self.fell_back,
            "stop_reason": self.stop_reason,
            "topology": self.topology,
            "shared_state": self.shared_state,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
        }


# ---------------------------------------------------------------------------
# 共享状态：三种做法
# ---------------------------------------------------------------------------


@runtime_checkable
class SharedState(Protocol):
    """多 Agent 之间共享状态的公共接口。

    三种实现（共享内存 / 共享文档 / 消息传递）都满足这三个方法，
    差别在**冲突怎么处理**、**读一次要不要付代价**、**能不能跨进程**。
    """

    def write(self, key: str, value: Any, author: str = "") -> None:
        """写一条结论。"""
        ...

    def read(self, key: str, default: Any = None) -> Any:
        """读某个键最新的值。"""
        ...

    def history(self) -> list[dict]:
        """全部写入记录，按发生顺序。"""
        ...


class SharedMemoryState:
    """共享内存：进程内的一个字典。

    **代价**：读最快（O(1)），但冲突是**静默**的——后写的直接盖掉先写的，
    没人知道发生过覆盖。适合子任务之间几乎不写同一个键的场景。
    """

    kind = "shared_memory"

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._log: list[dict] = []

    def write(self, key: str, value: Any, author: str = "") -> None:
        self._values[key] = value
        self._log.append({"key": key, "value": value, "author": author})

    def read(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def history(self) -> list[dict]:
        return [dict(item) for item in self._log]

    def conflicts(self) -> list[str]:
        """哪些键被写过不止一次——静默覆盖的证据。"""
        seen: dict[str, int] = {}
        for item in self._log:
            seen[item["key"]] = seen.get(item["key"], 0) + 1
        return [key for key, count in seen.items() if count > 1]


class SharedDocumentState:
    """共享文档：一块追加式的公共黑板。

    **代价**：每次写都留痕，冲突**看得见**（`history()` 里能看到同一个键的两个版本），
    但文档会变长，读全量越来越贵。
    """

    kind = "shared_document"

    def __init__(self) -> None:
        self._sections: list[dict] = []
        self._latest: dict[str, Any] = {}

    def write(self, key: str, value: Any, author: str = "") -> None:
        self._sections.append({"key": key, "value": value, "author": author})
        self._latest[key] = value

    def read(self, key: str, default: Any = None) -> Any:
        return self._latest.get(key, default)

    def history(self) -> list[dict]:
        return [dict(item) for item in self._sections]

    def document(self) -> str:
        """整块文档的文本形态。"""
        lines = []
        for index, section in enumerate(self._sections, start=1):
            author = section.get("author") or "?"
            lines.append(f"[{index}] {author} 写下 {section.get('key')}：{section.get('value')}")
        return "\n".join(lines)

    def conflicts(self) -> list[str]:
        seen: dict[str, int] = {}
        for item in self._sections:
            seen[item["key"]] = seen.get(item["key"], 0) + 1
        return [key for key, count in seen.items() if count > 1]


class MessagePassingState:
    """消息传递：没有共享变量，只有信箱。

    **代价**：写不能覆盖——消息是**发给某个人的**，收件人收一次就少一份。
    代价是没有全局视图：想知道"现在总共有什么结论"，得把信箱都翻一遍。
    """

    kind = "message_passing"

    def __init__(self) -> None:
        self._boxes: dict[str, list[dict]] = {}
        self._log: list[dict] = []

    def write(self, key: str, value: Any, author: str = "") -> None:
        """写到公共信箱（没有指定收件人时）。"""
        message = {"key": key, "value": value, "author": author}
        self._boxes.setdefault("*", []).append(message)
        self._log.append(dict(message))

    def read(self, key: str, default: Any = None) -> Any:
        """读某个键最新的公共消息（不消费）。"""
        for message in reversed(self._boxes.get("*", [])):
            if message["key"] == key:
                return message["value"]
        return default

    def history(self) -> list[dict]:
        return [dict(item) for item in self._log]

    def deliver(self, recipient: str, key: str, value: Any, author: str = "") -> None:
        """给某个收件人发一条消息。"""
        message = {"key": key, "value": value, "author": author, "to": recipient}
        self._boxes.setdefault(recipient, []).append(message)
        self._log.append(dict(message))

    def inbox(self, recipient: str) -> list[dict]:
        """某个收件人的信箱（不消费）。"""
        return [dict(item) for item in self._boxes.get(recipient, [])]

    def take(self, recipient: str) -> list[dict]:
        """取走并清空某个收件人的信箱——消息只能读一次。"""
        box = self._boxes.get(recipient, [])
        self._boxes[recipient] = []
        return [dict(item) for item in box]


# ---------------------------------------------------------------------------
# 计划解析
# ---------------------------------------------------------------------------


class PlanError(Exception):
    """计划不可用——没解析出子任务，或依赖成环。"""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _json_candidates(text: str) -> list[str]:
    """从一段文本里取出所有可能的 JSON 片段。"""
    candidates: list[str] = []
    for match in _FENCE_RE.finditer(text or ""):
        candidates.append(match.group(1).strip())
    candidates.append((text or "").strip())
    return [c for c in candidates if c]


def parse_plan(text: str) -> list[SubTask]:
    """把编排者的输出解析成子任务列表。

    容忍三种写法：裸数组、```json 包裹的数组、一个对象或多个对象。
    解析不出来就抛 :class:`PlanError`——由调用方决定是回退还是报错，
    解析器自己不猜。
    """
    for candidate in _json_candidates(text):
        parsed: Any = None
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        items: list[dict] = []
        if isinstance(parsed, list):
            items = [x for x in parsed if isinstance(x, dict)]
        elif isinstance(parsed, dict):
            items = [parsed]
        if not items:
            continue
        subtasks = [SubTask.from_dict(item) for item in items]
        subtasks = [s for s in subtasks if s.name and s.prompt]
        if subtasks:
            return subtasks
    raise PlanError(f"没能从编排者的输出里解析出子任务：{str(text)[:120]!r}")


_CONTINUE_RE = re.compile(r"^\s*(?:继续|CONTINUE)\s*[:：]\s*(.+)$")


def parse_continuations(text: str) -> list[SubTask]:
    """解析编排者请求的追加子任务。

    约定：每行一条，格式 ``继续：<子任务名>|<给执行者的指令>``。
    解析不出来就返回空列表——**空列表就是收敛信号**。
    """
    subtasks: list[SubTask] = []
    for line in (text or "").splitlines():
        match = _CONTINUE_RE.match(line)
        if not match:
            continue
        payload = match.group(1).strip()
        if "|" in payload:
            name, prompt = payload.split("|", 1)
        else:
            name, prompt = payload, payload
        name = name.strip()
        prompt = prompt.strip()
        if name and prompt:
            subtasks.append(SubTask(name=name, prompt=prompt))
    return subtasks


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------


@runtime_checkable
class Orchestrator(Protocol):
    """编排器的公共接口。``run`` 吃一句话，吐一个 ``AgentResult``。"""

    def run(self, task: str) -> AgentResult:
        ...


class SingleAgent(Orchestrator):
    """只有一个 Agent —— 这是默认实现，也是多 Agent 的对照组。

    它存在的意义不只是"省事"：多 Agent 的每一次收益主张，都要拿它当对照组。
    "多 Agent 比单 Agent 好"这句话，只有在同一批任务上比出来才算数。

    构造参数是**一个已经装配好的 Agent**——编排层不负责装配模型与工具，
    那是从第 3、4 章的职责。
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.last_report = OrchestrationReport(topology=TOPOLOGY_SINGLE)

    def run(self, task: str) -> AgentResult:
        """把任务原样交给那个 Agent。**永不抛异常。**"""
        try:
            result = self.agent.run(task)
        except Exception as exc:  # pragma: no cover - Agent 自己承诺不抛
            result = AgentResult(
                answer="",
                status="error",
                stop_reason=f"单 Agent 执行异常：{type(exc).__name__}: {exc}",
            )
        self.last_report = OrchestrationReport(
            rounds=1,
            outcomes=[TaskOutcome(
                name="single",
                worker="single",
                answer=result.answer,
                status=result.status,
                tokens=result.total_tokens,
                cost=result.total_cost,
                steps=len(result.steps),
            )],
            stop_reason=result.stop_reason,
            topology=TOPOLOGY_SINGLE,
            total_tokens=result.total_tokens,
            total_cost=result.total_cost,
        )
        return result


class Supervisor(Orchestrator):
    """编排者：把任务拆给多个子 Agent，再汇总。

    三段式，和人带项目一个节奏：

    1. **派单**——规划出一批子任务（``SubTask``），按依赖排好序；
    2. **回收**——每个子任务跑完，结论写进共享状态；
    3. **判定完成**——编排者拿着全部结论合成最终答案；
       它说"还要再做 X"就再开一轮，说不出就收敛。

    四组熔断（少一个都会出事）：

    - **最大轮次**——轮数是硬上限；
    - **无进展检测**——连续两轮结果完全相同就停；
    - **强制收敛**——轮数用完时，丢弃"还要继续"的请求，用现有结论收尾；
    - **单 Agent 回退**——规划失败或编排者出错时，把任务整个交回单 Agent。

    最后这条最要紧：**多 Agent 永远要有一条退路**。
    """

    def __init__(
        self,
        supervisor: Any,
        workers: dict[str, Any] | None = None,
        *,
        state: SharedState | None = None,
        planner: Callable[[str], list[SubTask]] | None = None,
        fallback: Any = None,
        max_rounds: int = 2,
        parallel: bool = False,
        max_workers: int = 4,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.workers: dict[str, Any] = dict(workers or {})
        self.state: SharedState = state if state is not None else SharedMemoryState()
        #: 可选的规划函数。给了就用它，不调模型——测试与回放靠它保持确定性。
        self.planner = planner
        #: 回退路径：一个 ``Orchestrator``（通常是 ``SingleAgent``）。
        self.fallback = fallback
        self.max_rounds = max(1, int(max_rounds))
        self.parallel = bool(parallel)
        self.max_workers = max(1, int(max_workers))
        self._clock = clock or time.monotonic
        self.last_report = OrchestrationReport(
            topology=TOPOLOGY_SUPERVISOR,
            shared_state=getattr(self.state, "kind", ""),
        )

    # ------------------------------------------------------------------ 主流程

    def run(self, task: str) -> AgentResult:
        """跑一次编排。**永不抛异常**——出错就回退到单 Agent。"""
        started = self._clock()
        outcomes: list[TaskOutcome] = []
        rounds = 0
        stop_reason = ""
        fell_back = False
        answer = ""
        status = "error"

        try:
            pending = self._plan(task)
            rounds = 0
            done: dict[str, TaskOutcome] = {}
            last_signature: tuple[Any, ...] | None = None

            while True:
                rounds += 1
                executed = self._execute(pending, task, done)
                outcomes.extend(executed)
                for outcome in executed:
                    done[outcome.name] = outcome

                synthesis = self._converge(task, outcomes)
                more = parse_continuations(synthesis)
                signature = tuple(sorted((o.name, o.answer) for o in executed))

                if not more:
                    answer = synthesis
                    status = "completed"
                    stop_reason = "所有子任务完成，编排者收敛"
                    break

                # 无进展：连续两轮的结果完全一样，继续派只是烧钱。
                if signature == last_signature:
                    answer = synthesis
                    status = "completed"
                    stop_reason = "连续两轮结果完全相同，判定无进展，停止派单"
                    break

                # 强制收敛：轮数用完，丢弃"还要继续"，用现有结论收尾。
                if rounds >= self.max_rounds:
                    answer = synthesis
                    status = "completed"
                    stop_reason = f"达到最大轮次 {self.max_rounds}，强制收敛"
                    break

                last_signature = signature
                pending = more

        except PlanError as exc:
            fell_back = True
            stop_reason = f"规划失败：{exc}"
            answer, status = self._fallback(task)
        except Exception as exc:  # 编排层任何意外都退回单 Agent
            fell_back = True
            stop_reason = f"编排异常：{type(exc).__name__}: {exc}"
            answer, status = self._fallback(task)

        total_tokens = sum(o.tokens for o in outcomes)
        total_cost = sum(o.cost for o in outcomes)
        self.last_report = OrchestrationReport(
            rounds=rounds,
            outcomes=outcomes,
            fell_back=fell_back,
            stop_reason=stop_reason,
            topology=TOPOLOGY_SUPERVISOR,
            shared_state=getattr(self.state, "kind", ""),
            total_tokens=total_tokens,
            total_cost=total_cost,
        )
        _ = started
        return AgentResult(
            answer=answer,
            status=status,
            steps=[],
            total_tokens=total_tokens,
            total_cost=total_cost,
            stop_reason=stop_reason,
        )

    # ------------------------------------------------------------------ 第一段：派单

    def _plan(self, task: str) -> list[SubTask]:
        """派单：拿到子任务清单，并按依赖排好序。

        规划有两个来源：外部给的 ``planner`` 函数（确定性，测试用），
        或者问编排者本人（真实路径，要解析它的输出）。
        """
        if self.planner is not None:
            subtasks = list(self.planner(task))
            if not subtasks:
                raise PlanError("规划函数返回了空计划")
        else:
            prompt = self._planning_prompt(task)
            result = self._ask(self.supervisor, prompt)
            subtasks = parse_plan(result.answer)

        ordered = self._order(subtasks)
        if not ordered:
            raise PlanError("计划里没有可执行的子任务")
        return ordered

    def _planning_prompt(self, task: str) -> str:
        """给编排者的规划提示词。"""
        workers = "、".join(self.workers.keys()) or "（未指定 worker）"
        return (
            "把下面这个任务拆成 2-4 个可以独立执行的子任务。\n"
            "只输出一个 JSON 数组，不要任何解释文字。每个元素形如：\n"
            '{"name": "子任务名", "prompt": "给执行者的完整指令", "worker": "能力域名"}\n'
            f"可用的 worker：{workers}\n\n"
            f"任务：{task}"
        )

    @staticmethod
    def _order(subtasks: list[SubTask]) -> list[SubTask]:
        """按 ``depends_on`` 拓扑排序。

        依赖成环是**规划出错**，直接抛 :class:`PlanError`——
        带着环往下跑只会死循环。
        """
        by_name = {s.name: s for s in subtasks}
        ordered: list[SubTask] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(subtask: SubTask, trail: list[str]) -> None:
            if subtask.name in visited:
                return
            if subtask.name in visiting:
                raise PlanError("依赖成环：" + " → ".join(trail + [subtask.name]))
            visiting.add(subtask.name)
            for dep in subtask.depends_on:
                if dep in by_name:
                    visit(by_name[dep], trail + [subtask.name])
            visiting.discard(subtask.name)
            visited.add(subtask.name)
            ordered.append(subtask)

        for subtask in subtasks:
            visit(subtask, [])
        return ordered

    # ------------------------------------------------------------------ 第二段：回收

    def _execute(self, subtasks: list[SubTask], task: str,
                 done: dict[str, TaskOutcome]) -> list[TaskOutcome]:
        """执行这一批子任务。

        依赖**在这一批内部**也要满足：``算账`` 依赖 ``查资料``，
        而两个都在本轮计划里时，``查资料`` 必须先跑完，``算账`` 才能拿到它的结论。
        所以执行是"按依赖就绪度分批"：就绪的那一组跑完，结论落进 ``done``，
        下一组才可能被解锁。

        没有互相依赖的一组可以并行跑（``parallel=True``）。
        并行结果的**顺序仍然按计划顺序**整理，所以回放与测试不受影响。
        """
        outcomes: list[TaskOutcome] = []
        remaining = list(subtasks)

        while remaining:
            ready = [s for s in remaining if all(d in done for d in s.depends_on)]
            if not ready:
                break
            for subtask in ready:
                remaining.remove(subtask)

            if self.parallel and len(ready) > 1:
                with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                    futures = [pool.submit(self._run_one, s, task, done) for s in ready]
                    group = [f.result() for f in futures]
                order = {s.name: i for i, s in enumerate(ready)}
                group = sorted(group, key=lambda o: order.get(o.name, 0))
            else:
                group = [self._run_one(s, task, done) for s in ready]

            for outcome in group:
                done[outcome.name] = outcome
            outcomes.extend(group)

        # 剩下的子任务：依赖永远满足不了（依赖了不在计划里的子任务，或成环）。
        for subtask in remaining:
            outcome = TaskOutcome(
                name=subtask.name,
                worker=self._worker_name(subtask),
                status="error",
                error=f"依赖未完成：{subtask.depends_on}",
            )
            done[subtask.name] = outcome
            outcomes.append(outcome)
        return outcomes

    def _run_one(self, subtask: SubTask, task: str,
                 done: dict[str, TaskOutcome]) -> TaskOutcome:
        """跑一个子任务：拼提示词 → 调 worker → 写共享状态。"""
        worker_name = self._worker_name(subtask)
        worker = self.workers.get(worker_name)
        if worker is None:
            return TaskOutcome(
                name=subtask.name,
                worker=worker_name,
                status="error",
                error=f"没有名为 {worker_name!r} 的 worker",
            )

        prompt = self._subtask_prompt(subtask, task, done)
        result = self._ask(worker, prompt)
        if not isinstance(result, AgentResult):
            return TaskOutcome(
                name=subtask.name,
                worker=worker_name,
                status="error",
                error=f"worker 返回的不是 AgentResult：{type(result).__name__}",
            )

        outcome = TaskOutcome(
            name=subtask.name,
            worker=worker_name,
            answer=result.answer,
            status=result.status,
            tokens=result.total_tokens,
            cost=result.total_cost,
            steps=len(result.steps),
        )
        # 结论写进共享状态：下一个子任务能读到。
        self.state.write(subtask.name, outcome.answer, author=worker_name)
        return outcome

    def _worker_name(self, subtask: SubTask) -> str:
        if subtask.worker:
            return subtask.worker
        if len(self.workers) == 1:
            return next(iter(self.workers))
        return "?"

    def _subtask_prompt(self, subtask: SubTask, task: str,
                        done: dict[str, TaskOutcome]) -> str:
        """给 worker 的提示词：原始任务 + 这一段活 + 可用的已有结论。"""
        lines = [f"原始任务：{task}", "", f"你负责的子任务：{subtask.name}", subtask.prompt]

        inputs = list(subtask.inputs) or list(subtask.depends_on)
        available: list[str] = []
        for key in inputs:
            value = self.state.read(key)
            if value is None:
                prior = done.get(key)
                value = prior.answer if prior else None
            if value is not None:
                available.append(f"- {key}：{value}")
        if available:
            lines.extend(["", "已有结论（可以直接用，不必重做）：", *available])

        return "\n".join(lines)

    # ------------------------------------------------------------------ 调用 Agent

    def _ask(self, agent: Any, prompt: str) -> AgentResult:
        """跑一次 agent，但**不共享上一次的步骤历史**。

        这是编排层最容易踩的坑：编排者会被反复调用（规划一次、汇总一次、
        下一轮再来一次）。如果沿用同一个有状态 Agent，第 4 次调用就会撞上
        主循环自己的**无进展检测**——它看到连续三次"同样的动作、同样空的观察"，
        就判定这次没进展，直接停。

        那不是编排层要的判断，是上一次运行的残留。
        所以每一次编排调用都是**独立的一次**：跑之前留个检查点，跑完恢复。
        """
        state = getattr(agent, "state", None)
        checkpoint = None
        if state is not None:
            try:
                checkpoint = state.checkpoint()
            except Exception:
                checkpoint = None
        try:
            result = agent.run(prompt)
        except Exception as exc:
            return AgentResult(
                answer="",
                status="error",
                stop_reason=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if checkpoint is not None:
                try:
                    state.restore(checkpoint)
                except Exception:
                    pass
        if not isinstance(result, AgentResult):
            return AgentResult(answer="", status="error",
                               stop_reason=f"返回的不是 AgentResult：{type(result).__name__}")
        return result

    # ------------------------------------------------------------------ 第三段：判定完成

    def _converge(self, task: str, outcomes: list[TaskOutcome]) -> str:
        """回收全部结论，让编排者合成最终答案。"""
        prompt = (
            "下面是几个子 Agent 的执行结论。请合成一个最终答案。\n"
            "只输出结论本身，不要复述过程。\n"
            "如果确实还有必要做的事，每行写一条：继续：<子任务名>|<给执行者的指令>\n"
            "如果没有，就直接给结论。\n\n"
            f"原始任务：{task}\n\n"
            "子任务结论：\n"
        )
        for outcome in outcomes:
            prompt += f"- [{outcome.name}]（{outcome.worker}，{outcome.status}）：{outcome.answer}\n"
        result = self._ask(self.supervisor, prompt)
        answer = (result.answer or "").strip()
        if not answer:
            return self._join(outcomes)
        return answer

    @staticmethod
    def _join(outcomes: list[TaskOutcome]) -> str:
        """汇总失败时的兜底答案——总比空答案好。"""
        return "\n".join(f"- [{o.name}]：{o.answer}" for o in outcomes)

    # ------------------------------------------------------------------ 回退

    def _fallback(self, task: str) -> tuple[str, str]:
        """回退到单 Agent。**没有回退路径时，返回空答案而不是崩。**"""
        if self.fallback is None:
            return "", "error"
        try:
            result = self.fallback.run(task)
        except Exception as exc:
            return "", f"error"
        return result.answer, result.status


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def build_default_agent(config: Config | None = None) -> Agent:
    """按配置造一个默认的单 Agent。

    ``--live`` 时走真实 API，否则走 mock——和第 3 章的 ``build_client`` 一致。
    """
    cfg = config or Config.from_env_and_args([])
    return Agent(
        config=cfg,
        llm=build_client(cfg),
        tools=default_registry(),
        context=ContextAssembler(config=cfg),
        state=StateStore(config=cfg),
    )


def build_orchestrator(config: Config | None = None, agent: Any = None,
                       **kwargs: Any) -> Orchestrator:
    """**默认编排器工厂：返回 ``SingleAgent``。**

    这是契约里最重要的一条约束——多 Agent 是显式选择，不是默认行为。
    想用多 Agent，请显式构造 :class:`Supervisor`。
    """
    if agent is None:
        agent = build_default_agent(config)
    return SingleAgent(agent)
