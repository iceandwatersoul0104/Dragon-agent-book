# `dragon-agent` 模块接口契约

第二卷 14 章是**并行写作**的，如果不先把模块间的接口定死，各章的代码会互相不兼容——到第 10 章才发现第 3 章的返回值形状不一样，返工成本极高。

本文档是 `dragon-agent` 工程的地基。**所有模块只允许通过下面列出的公开接口互相调用**，不许跨模块偷读内部状态、不许私自改签名。签名要改，先改这份文档。

## 设计原则（来自第一卷）

1. **控制流与不确定性分离**：`agent.py` 决定"下一步做什么"，`llm.py` 只负责"一次调用"。模型的不确定性被关在 `llm.py` 里。
2. **一次迭代三份账**：`context.py` 管进上下文的预算，`state.py` 管状态的唯一真相，`observability.py` 管成本与 trace。
3. **工具是 API 工程，不是提示词工程**：工具契约、参数校验、错误回灌、幂等在 `tools.py` 里定死。
4. **每个横切面都可替换**：`guardrails.py`、`memory.py`、`observability.py` 都用 Protocol 定义，测试时能塞假实现。

## 依赖方向（不可逆）

```
        cli.py
          |
     orchestration.py
          |
       agent.py  <---- guardrails.py
        |  |  \
        |  |   +---- state.py ---- memory.py
        |  +-------- context.py
        +----------- tools.py
                      |
                    llm.py
                      |
                   config.py
```

下层不知道上层的存在。`config.py` 被所有模块依赖，但**不依赖任何模块**。

---

## 1. `config.py` —— 统一配置

```python
@dataclass(frozen=True)
class Config:
    model: str = "mock-model"
    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.0
    max_steps: int = 12              # 步数上限
    token_budget: int = 120_000      # 单任务 token 预算
    cost_budget: float = 0.50        # 单任务成本上限（美元）
    timeout_s: int = 60
    live: bool = False               # --live 开关
    workspace: Path = ...
    trace_path: Path | None = None

    @classmethod
    def from_env_and_args(cls, argv=None) -> "Config": ...
    def to_dict(self) -> dict: ...          # 落盘用，不含密钥
```

**约束**：`to_dict()` 绝不包含 api_key；`from_env_and_args` 是唯一的构造入口。

## 2. `llm.py` —— 模型接入抽象

```python
@dataclass
class ToolCall:
    name: str
    arguments: dict

@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    @property
    def total(self) -> int: ...

@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    finish_reason: str = "stop"

class LLMClient(Protocol):
    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 **kwargs) -> LLMResponse: ...
    def describe(self) -> str: ...          # 供 trace 记录模型标识

class MockLLM:
    """按剧本返回响应。剧本由 tests 注入。"""
    def __init__(self, script: list[LLMResponse] | None = None): ...

class OpenAICompatClient:
    """走 /chat/completions 的 OpenAI 兼容接口。缺 key 时构造即抛 ConfigError。"""
```

**约束**：mock 与真实客户端返回**同一种** `LLMResponse`。任何业务模块不得 `isinstance` 判断客户端类型。

## 3. `tools.py` —— 工具层

```python
@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict              # JSON Schema
    side_effect: bool = False
    replayable: bool = True       # 幂等可重放；False 的工具禁止自动重试

@dataclass
class ToolResult:
    ok: bool
    output: Any
    error: str | None = None
    duration_ms: int = 0

class ToolRegistry:
    def register(self, spec: ToolSpec, fn: Callable[..., Any]) -> None: ...
    def get(self, name: str) -> tuple[ToolSpec, Callable]: ...
    def schemas(self) -> list[dict]: ...       # 喂给 llm.complete 的 tools 参数
    def invoke(self, name: str, args: dict,
               idempotency_key: str | None = None) -> ToolResult: ...
```

**约束**：
- 参数校验失败**不抛异常**，返回 `ToolResult(ok=False, error=...)`，由上层回灌给模型。
- `replayable=False` 的工具在 `idempotency_key` 已用过的重试中**必须拒绝执行**。
- 校验与错误消息的格式在 `tools.py` 内统一，上层不重复实现。

## 4. `context.py` —— 上下文装配器

```python
@dataclass
class ContextBlock:
    layer: str      # system / tools / task / history / retrieval
    text: str
    tokens: int
    priority: int

@dataclass
class BudgetReport:
    per_layer: dict[str, int]
    total: int
    budget: int
    dropped: list[str]

@dataclass
class AssembledContext:
    blocks: list[ContextBlock]
    report: BudgetReport
    def render(self) -> str: ...

class ContextAssembler:
    def assemble(self, task: str, history: list[dict],
                 retrieval: list[str], tools: list[dict]) -> AssembledContext: ...
```

**约束**：稳定块（system / tools）在前，保证提示缓存前缀命中。`render()` 的输出必须对同一输入**完全确定**，否则缓存命中率无法复现。

## 5. `agent.py` —— 主循环

```python
@dataclass
class AgentStep:
    index: int
    thought: str
    action: ToolCall | None
    observation: str
    tokens: int
    cost: float

@dataclass
class AgentResult:
    answer: str
    status: str          # completed / stopped_budget / stopped_no_progress / max_steps / error
    steps: list[AgentStep]
    total_tokens: int
    total_cost: float
    stop_reason: str

class Agent:
    def __init__(self, config: Config, llm: LLMClient,
                 tools: ToolRegistry, context: ContextAssembler,
                 state: "StateStore", guards: "GuardrailEngine | None" = None): ...
    def run(self, task: str) -> AgentResult: ...
```

**约束**：
- `run()` **永不抛异常给调用方**，失败转成 `AgentResult.status="error"`。
- 停止优先级固定：完成 → 异常 → 预算 → 无进展 → 步数上限（与第一卷第 11 章一致）。
- 每步结束写一次 `state.checkpoint()`。

## 6. `state.py` —— 状态与检查点

```python
@dataclass
class TaskState:
    task_id: str
    status: str = "running"
    data: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    version: int = 0

@dataclass
class Checkpoint:
    task_id: str
    version: int
    snapshot: dict
    created_at: float

class StateStore:
    def snapshot(self) -> TaskState: ...
    def checkpoint(self) -> Checkpoint: ...
    def restore(self, cp: Checkpoint) -> TaskState: ...
    def record(self, event: dict) -> None: ...
    def events(self) -> list[dict]: ...       # 事件溯源
```

**约束**：`TaskState` 是唯一真相源；`context.py` 与 `memory.py` 不得各自维护一份状态副本。

## 7. `memory.py` —— 记忆

```python
@dataclass
class MemoryItem:
    id: str
    kind: str            # working / episodic / semantic / procedural
    text: str
    source: str          # user / tool / model
    score: float = 0.0
    ttl: float | None = None
    created_at: float = 0.0
    version: int = 1

class Memory(Protocol):
    def write(self, kind: str, text: str, source: str, ttl=None) -> MemoryItem: ...
    def recall(self, query: str, kind: str | None = None, k: int = 5) -> list[MemoryItem]: ...
    def forget(self, item_id: str) -> bool: ...
```

**约束**：来源可信度 `user > tool > model`；`model` 来源的语义记忆写入**必须被拒绝或降级**。TTL 到期是降权不是删除。

## 8. `guardrails.py` —— 护栏

```python
@dataclass
class Decision:
    allow: bool
    reason: str = ""
    require_confirm: bool = False

class GuardrailEngine(Protocol):
    def before_tool(self, name: str, args: dict) -> Decision: ...
    def after_output(self, text: str) -> Decision: ...
```

**约束**：护栏只能**收紧**不能放宽——`before_tool` 返回 allow=False 时，调用方必须中止该次工具调用。护栏的判定不依赖 LLM。

## 9. `orchestration.py` —— 编排层

```python
@dataclass
class SubTask:
    name: str
    prompt: str

class Orchestrator(Protocol):
    def run(self, task: str) -> AgentResult: ...

class SingleAgent(Orchestrator):
    """只有一个 Agent —— 这是默认实现，也是多 Agent 的对照组。"""

class Supervisor(Orchestrator):
    """把任务拆给多个子 Agent，再汇总。拆与不拆的判据见第二卷第 10 章。"""
```

**约束**：默认必须是 `SingleAgent`。多 Agent 是显式选择，不是默认行为。

## 10. `mcp_server.py` —— 协议接入

```python
class MCPServer:
    """把工具注册表暴露为 MCP server（JSON-RPC over stdio）。"""
    def __init__(self, tools: ToolRegistry, name: str, version: str): ...
    def handle(self, request: dict) -> dict: ...      # 单条 JSON-RPC
    def serve_stdio(self) -> int: ...                 # 读 stdin 写 stdout
```

**约束**：协议层是**薄适配层**，不实现业务逻辑。工具的校验与执行仍然走 `tools.py`。

## 11. `observability.py` —— 可观测性

```python
@dataclass
class Span:
    name: str
    parent: str | None
    start: float
    end: float
    attributes: dict
    status: str = "ok"

class Tracer:
    def span(self, name: str, **attributes) -> Span: ...   # 上下文管理器
    def spans(self) -> list[Span]: ...
    def sli(self) -> dict: ...      # 成功率 / 工具错误率 / 无进展率 / 每次成功成本

class CostMeter:
    def charge(self, usage: Usage, model: str) -> float: ...
    def total(self) -> float: ...
```

**约束**：属性名用 OTel GenAI 约定（`gen_ai.*`、`gen_ai.usage.*`），不是自定义的 `llm.*`。

## 12. `evaluation.py` —— 评测

```python
@dataclass
class EvalCase:
    id: str
    task: str
    expect: list[str]        # 必须出现在答案里的关键事实
    forbid: list[str] = field(default_factory=list)

@dataclass
class EvalReport:
    total: int
    passed: int
    failures: list[str]
    regression_vs_baseline: list[str]

class Evaluator:
    def run(self, cases: list[EvalCase]) -> EvalReport: ...
    def compare(self, baseline: EvalReport) -> EvalReport: ...
```

**约束**：判定器**不用 LLM**（用关键字与规则），保证确定性与可离线运行。

## 13. `cli.py` —— 命令行入口

```
python -m dragon_agent.cli run "帮我查一下项目预算"     # 跑一次完整回路
python -m dragon_agent.cli chat                          # 交互式
python -m dragon_agent.cli tools                          # 列出可用工具
python -m dragon_agent.cli trace <run_id>                 # 看某次 trace
python -m dragon_agent.cli eval                           # 跑评测集
python -m dragon_agent.cli demo                           # 端到端演示
```

全局开关：`--live`（切真实 API）、`--budget`、`--max-steps`、`--trace`。

## 14. `tests/` —— 测试与回归基线

- **不依赖网络**。全部用 `MockLLM` 与假工具。
- 每个模块至少一个单元测试；`agent.py` 与 `orchestration.py` 要有端到端测试。
- 关键行为写成**断言**而不是"跑完不崩就算过"：停止准则、预算守卫、幂等、护栏拦截、上下文预算、评测判据都要有明确的期望值。
- 有一条 `test_end_to_end.py`：从输入到答案，走完整条链路并断言最终状态与成本账。

---

## 跨模块不变式（测试必须覆盖）

| 不变式 | 涉及模块 |
| --- | --- |
| `Agent.run()` 不抛异常 | agent |
| 停止优先级：完成 > 异常 > 预算 > 无进展 > 步数 | agent, state |
| 工具参数校验失败不抛异常 | tools, agent |
| `replayable=False` 的工具不自动重试 | tools |
| 同一输入的 `ContextAssembler.render()` 完全确定 | context |
| `TaskState` 是唯一状态真相源 | state, context, memory |
| `model` 来源的语义记忆写入被拒 | memory |
| 护栏只能收紧不能放宽 | guardrails, agent |
| 默认编排器是 `SingleAgent` | orchestration |
| trace 属性用 `gen_ai.*` 命名 | observability |
| 评测判定器不含 LLM 调用 | evaluation |
| `Config.to_dict()` 不含密钥 | config |
