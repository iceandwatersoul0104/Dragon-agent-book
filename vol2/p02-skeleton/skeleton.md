# 第 2 章 工程骨架：目录、配置与错误处理约定

## 一、本章要解决的问题

凌晨两点，一次任务超时，告警里只有四个字：任务失败。

值班的人打开日志，发现日志里全是 `print` 打出来的自由文本，没有任务标识；打开 trace，发现里面只有模型调用，没有错误类别——因为代码里所有的异常都叫 `RuntimeError`；想查当时跑的是哪套配置，发现 `os.environ.get("LLM_API_KEY")` 在三个文件里各写了一遍，改一次 `--live` 开关要动四个函数。

最后是靠一个时间戳把日志和 trace 对上的。对上了是运气。

问题的根子不在"这次任务为什么失败"，而在**没有骨架**：目录没分层、配置没有唯一入口、错误没有分类、日志没有规范。等到要排查的时候，这些代价一次性结账。

写代码之前先想清楚四件事，能省掉后面绝大部分返工：

1. **目录怎么分**——模块之间谁可以依赖谁；
2. **配置从哪来**——默认值、环境变量、命令行参数，冲突了听谁的；
3. **错误怎么分类**——同一类错误在同一个地方处理，而不是散落在 except 里；
4. **日志怎么写**——机器能读，一次任务的记录能串起来，密钥不会漏出去。

本章把这四件事定成骨架。骨架一旦定好，后面每一章都只是往里填东西，不用再回头改地基。

## 二、原理：机制拆解

### 2.1 四层与依赖方向

`dragon-agent` 的模块不是按功能分的，是按**"谁决定下一步做什么"**分的：

```
     cli.py · examples/ · 你的业务代码
                  │  依赖
                  ▼
     agent.py · orchestration.py · state.py · guardrails.py
                  │  依赖
        ┌─────────┴─────────┐
        ▼                   ▼
   tools.py              llm.py
   契约、校验、幂等        一次调用，封装不确定性
        │                   │
        └─────────┬─────────┘
                  ▼  依赖
   config.py · errors.py · logging_setup.py
   只依赖标准库，不依赖任何上层 —— 基础层（叶子）
```

按"谁决定下一步做什么"分出来的四层，加上三个叶子模块。

依赖方向**不可逆**：上层可以依赖下层，下层不知道上层的存在。这条规则的实际收益是：你可以单独替换任何一层，而不用改其他层。

- 想换成真实模型，只改模型层，控制流一个字不动；
- 想给工具加个新参数，只改工具层，编排层不感知；
- 想把日志换成 OpenTelemetry，只改基础层。

叶子模块（`config.py`、`errors.py`、`logging_setup.py`）是例外中的例外：**所有层都可以依赖它们，它们不依赖任何一层**。这是它们存在的唯一理由。

### 2.2 配置：单一入口、早失败、不可变

`config.py` 是整个工程唯一一个"知道环境变量和命令行参数"的地方。四个设计：

> 一个后加的例外：第 14 章的 `cli.py` 为成本阈值（告警线、熔断线、限流）**直接读了环境变量**，走的是"命令行 > 环境变量 > 内置默认值"。这不违反"配置只有一个来源"——`Config` 本身仍然只由 `Config.from_env_and_args()` 构造，cli 读的那几个阈值是独立的运行期档位，不进 `Config`。但它确实说明了一件事：**纪律要靠约定维持，不是靠类型系统保证的**。

1. **单一入口**：`Config.from_env_and_args()` 是唯一的构造入口。默认值 → 环境变量 → 命令行参数，命令行优先级最高。别的模块想读配置，只能拿到一个已经构造好的 `Config` 实例。

2. **早失败**：合法性检查放在 `__post_init__` 里，构造阶段就抛 `ConfigError`：

```python
    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ConfigError(f"max_steps 必须 ≥1，收到 {self.max_steps}")
        if self.token_budget < 1:
            raise ConfigError(f"token_budget 必须 ≥1，收到 {self.token_budget}")
        if self.cost_budget < 0:
            raise ConfigError(f"cost_budget 不能为负，收到 {self.cost_budget}")
        if self.live and not self.api_key:
            raise ConfigError("--live 需要设置 LLM_API_KEY（或 OPENAI_API_KEY）")
```

这叫**早失败（Fail Fast）**：配置不合法时，进程应该在启动的第一秒退出，而不是跑到第 47 步才发现预算是负的。代价是启动时多校验几微秒，收益是把一类"线上偶发、无法复现"的故障变成启动时必现的报错。

3. **不可变**：`Config` 是 `frozen=True` 的 dataclass。想改就重建一份：

```python
config = dataclasses.replace(Config.from_env_and_args([]), max_steps=6)
```

上面这行来自工程自带的 `examples/end_to_end_demo.py`。

为什么要冻结？因为配置会被写进 trace 和检查点。**如果配置能在运行中被改，trace 里记录的配置就未必是实际生效的那份**，排查时你会被自己的记录骗到。冻结把"配置即事实"变成可保证的性质。

4. **落盘表示不含密钥**：`to_dict()` 会把 `api_key` 摘掉。trace、检查点、错误详情都走 `to_dict()`，所以密钥不会因为"顺手把配置打印出来"而泄露。

### 2.3 错误分类：四类，五种策略

错误分类的价值不在于"分得好看"，而在于**同一类错误在同一个地方、用同一种方式处理**。分类的依据是"重试有没有意义、谁来决定"：

| 类别 | 典型例子 | 可重试 | 谁处理 | 策略 |
| --- | --- | --- | --- | --- |
| 模型错误 ModelError | 模型服务不可用、返回体无法解析 | 是 | 编排层 | 退避重试；重试用尽则降级 |
| 工具错误 ToolError | 工具名冲突（重复注册等致命错误） | 否 | 工具层 | 致命错误退出；参数错不走异常路径 |
| 编排错误 OrchestrationError | 状态机不变量被破坏 | 否 | 应用层 | 不重试，结束任务并保留已完成步骤 |
| 外部依赖错误 DependencyError | 网络超时、远端 5xx、文件锁住 | 是 | 工具层 / 编排层 | 退避重试；超上限转成失败结果 |

外加第五类：**配置错误 ConfigError**，它属于启动期，策略是"早失败，直接退出"，不进运行期。

这里有三条容易被忽略的原则：

**第一条：运行期的可预期失败，优先用返回值表达，不用异常。** 工具参数不合法返回 `ToolResult(ok=False)`，模型服务不可用返回 `finish_reason="error"` 的响应。原因是异常会打断控制流，而 Agent 的控制流必须把"失败"当成一等公民——第 8 章讲错误回灌时还会回到这里。

**第二条：异常在层内传播，在层边界转成返回值。** 工具函数内部可以抛 `DependencyError`（网络超时，稍后重试），工具层接住它，转成 `ToolResult(ok=False)` 回灌给模型。调用方永远只看到一种形状：

```python
            result = tools.invoke(call.name, call.arguments)
            observation = result.render()
```

工具层把 `DependencyError` 接住之后，`result.ok` 是 `False`，`result.render()` 给出的是一句给模型看的错误文本：

```text
[工具 DependencyError: 预算库 1999 不可用 ({'fiscal_year': '1999', 'source': 'budget-store'})]
```

**第三条：不造同名分身。** 工具层早就有 `tools.ToolError`（注册冲突这类致命错误）。如果 `errors.py` 再定义一个也叫 `ToolError` 的类，`except ToolError` 就只会抓住一半——而且要等到某天线上抛出另一种 ToolError 才会发现。所以 `errors.py` 用模块级 `__getattr__` 惰性导出**同一个类对象**：

```python
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
```

这样既保住了"叶子模块不依赖上层"，也没有第二个同名类。`errors.ToolError is tools.ToolError` 恒为真，测试里有一条断言专门守它。

### 2.4 日志规范：字段固定、trace_id 贯穿、密钥脱敏

**字段固定**：一行一条 JSON，固定字段是 `ts / level / logger / msg / trace_id`，其余字段按需扩展。自由文本日志只能给人看，而排查要靠机器筛：

```json
{"ts": "2026-11-08T03:21:30.952Z", "level": "INFO", "logger": "dragon_agent.hello", "msg": "run.finished", "trace_id": "03cbd896e49e4434b45743bc3c5d13a2", "steps": 3, "tool_calls": 2}
```

**trace_id 贯穿**：一次任务的所有日志必须能串起来。做法是用 `contextvars` 存一个当前 trace_id，而不是把它当参数层层传：

```python
_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dragon_trace_id", default=None
)

@contextlib.contextmanager
def trace_scope(trace_id: str | None = None) -> Iterator[str]:
    tid = trace_id or new_trace_id()
    token = _trace_id.set(tid)
    try:
        yield tid
    finally:
        _trace_id.reset(token)
```

写日志的地方不需要知道 trace_id 是什么，它从当前作用域里取；作用域退出时自动还原，嵌套也不会把外层的值冲掉。

**密钥脱敏**：脱敏必须发生在**写出去之前**，不能等到看日志时才遮。遮两处：键名命中敏感词就整值替换；消息正文里的密钥形态用正则兜底。

```python
SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "access_key", "secret_key",
    "authorization", "auth",
    "password", "passwd", "pwd",
    "secret", "token", "access_token", "refresh_token", "session_token",
    "cookie", "set_cookie",
    "private_key", "credential", "credentials",
})
```

整值替换、不留长度、不留前缀——"知道密钥前 4 位"本身就是泄露。

## 三、为什么这样设计：取舍与失效条件

### 3.1 什么时候**不该**建这套骨架

骨架是有成本的：文件变多、跳转变多、新人第一周不知道从哪读。下面几种情况，**先别建**：

1. **一次性脚本，跑完就删。** 直接写一个 `.py` 文件。给一个二十行的脚本建四层目录，是第一卷第 3 章说的过度工程化（Over-engineering）。
2. **原型期，还在试各种可能。** 这时候结构一定会变，先把想法跑通。原型转正时再按骨架重排——重排一次的成本，远低于在错误的结构上写三个月。
3. **纯离线、纯玩具、只给自己看。** 不接真实模型、不接真实数据、不给别人用，错误分类和日志规范带来的收益接近零。

反过来，**只要这个东西会被别人用、会长期存在、会接真实的外部依赖，骨架就值得**。判断标准还是那句老话：三个月后你还要回来改它，就该有骨架。

### 3.2 骨架的四种腐坏

骨架最常见的死法不是不用，是**用着用着就散了**：

| 腐坏形态 | 症状 | 后果 |
| --- | --- | --- |
| 分层退化成文件夹分类 | 编排层直接 `from .tools import _internal_helper` | 改一个模块，全仓测试挂 |
| 配置退化成全局变量 | 冒出 `config.max_steps = 6` 这种就地修改 | trace 里记录的配置和实际生效的不一致 |
| 异常退化成"什么都是 RuntimeError" | `except Exception as e: raise RuntimeError(str(e))` | 分类白做，重试策略只能一刀切 |
| 日志退化成 print | 调试完没删干净 | 日志没有结构、没有 trace_id，等于没有日志 |

对策还是那一条：**让机器来查**。第 2.2 节的"配置不可变"、`to_dict()` 不含密钥，本质上都是可测试的性质；第 2.3 节的"不造同名分身"，靠的是一条 `assertIs` 断言。靠自觉守不住的规矩，要写成断言。

### 3.3 分层的代价

说完好处，得说代价，否则这一节就变成了推销：

- **文件数变多**：十九个模块，每个都有 docstring，新人读代码的时间花在读注释上。
- **间接层变多**：想看清"一次调用发生了什么"，要从应用层读到编排层再读到模型层，中间还有上下文装配、检查点。调试链路比单体 Agent 长。
- **抽象会过期**：契约里定的接口，随着需求变化经常需要改。契约本身也得跟着改，改晚了，下游模块全在等。

这些代价是真实的，所以第二节那张分层图**不是给所有项目的**。它是给"要活很久的那个 Agent"的。判断标准：如果你的项目活不过一个月，分层的收益追不上成本。

## 四、动手：最小可实现

### 4.1 本章的工程产物

| 文件 | 作用 |
| --- | --- |
| `dragon_agent/errors.py` | 异常分类：四类错误 + 策略表 + 分类函数 |
| `dragon_agent/logging_setup.py` | 结构化日志：JSON 行格式、trace_id 贯穿、脱敏 |
| `examples/skeleton_hello.py` | 可运行的 hello-world 入口：配置 → 日志 → 模型 → 工具 |
| `tests/test_skeleton_basics.py` | 第一个测试：25 条断言，守住上面的骨架约定 |

**没有改动** `config.py`、`llm.py`、`tools.py`——它们已经写好了，本章只读不改。也没有改 `__init__.py`：那张导出表就是公共 API，改它必须同步改契约文档。

### 4.2 异常分类：errors.py

根类带三个字段：`kind`（分类）、`retryable`（能不能重试）、`details`（结构化上下文）。`kind` 和 `retryable` 定义在**类属性**上，所以按类判断和按字段判断结果一致：

```python
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
```

三个新子类各自只覆盖两个属性——第四类工具错误复用 `tools.ToolError`，不另起炉灶：

```python
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
```

策略表是可查询的，测试能对它断言——策略写成散落在各处的 if，就没人能保证它和 `retryable` 一致：

```python
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
```

### 4.3 结构化日志：logging_setup.py

JSON 格式化器把固定字段和扩展字段拼成一行：

```python
class JsonFormatter(logging.Formatter):
    """一行一条 JSON 的日志格式化器。

    固定字段：``ts``（UTC，毫秒精度）、``level``、``logger``、``msg``、
    ``trace_id``。其余记录在日志调用处的字段作为扩展字段输出。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
            "trace_id": getattr(record, "trace_id", None) or current_trace_id(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_") or key in payload:
                continue
            payload[key] = redact(value)
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)
```

注意 `redact` 是在**格式化的时候**调的——也就是在写出去之前。这个模块只管运行日志，不管 trace 与成本账，那是第 12 章 `observability.py` 的活；两者的关联点是同一个 `trace_id`。

### 4.4 hello-world 入口

`examples/skeleton_hello.py` 故意只依赖 `config` / `llm` / `tools` / `errors` / `logging_setup` 五个模块——编排层还没写，脚本里用一个最小的 ReAct 回路（问模型 → 执行工具 → 回灌观察 → 再问）演示分层是怎么搭的。

开头的早失败：

```python
    # 1) 配置：早失败。配置不合法就别进运行期。
    try:
        config = Config.from_env_and_args(argv)
    except ConfigError as exc:
        print(f"配置不合法，已退出：{exc}", file=sys.stderr)
        return 2
```

工具内部抛依赖错误，工具层接住并转成失败结果：

```python
    def read_budget(fiscal_year: str) -> dict:
        if fiscal_year != "2026":
            # 外部依赖不可用——该重试，不是模型的错。
            raise DependencyError(
                f"预算库 {fiscal_year} 不可用",
                details={"fiscal_year": fiscal_year, "source": "budget-store"},
            )
```

模型层则按"可重试"策略退避：

```python
            except ModelError as exc:
                policy = policy_for(exc)
                if not policy["retryable"] or attempt == self.max_retries:
                    log_event(
                        log, "model.degraded", level=40,
                        kind=classify(exc), message=exc.message,
                        attempts=self.attempts, strategy=policy["strategy"],
                    )
                    return LLMResponse(
                        text=f"[llm] {exc.message}", finish_reason="error"
                    )
```

### 4.5 第一个测试

`tests/test_skeleton_basics.py` 用标准库 `unittest`（不引 pytest，符合本工程"纯标准库"的约定），25 条断言全部写期望值：

```python
    def test_tool_error_is_not_duplicated(self):
        """errors.ToolError 与 tools.ToolError 必须是**同一个类**。

        造两个同名类会让 except 只抓住一半——这是骨架期就该定死的事。
        """
        self.assertIs(errors.ToolError, ToolError)

    def test_policy_matches_retryability(self):
        """策略表与类的 retryable 必须一致——不能让两处各写一份。"""
        for kind in ("model", "dependency"):
            self.assertTrue(errors.POLICY[kind]["retryable"], kind)
        for kind in ("tool", "orchestration", "config"):
            self.assertFalse(errors.POLICY[kind]["retryable"], kind)
```

### 4.6 实跑

```powershell
cd dragon-agent
python examples/skeleton_hello.py
```

stderr 上的日志（stdout 是程序自己的输出，两者分开）：

```text
{"ts": "2026-11-08T03:21:30.952Z", "level": "INFO", "logger": "dragon_agent.hello", "msg": "run.start", "trace_id": "03cbd896e49e4434b45743bc3c5d13a2", "task": "查一下 2026 年的项目预算，并告诉我总额；如果查不到就说明缺哪一年", "model": "mock-model", "live": false, "max_steps": 12, "token_budget": 120000}
{"ts": "2026-11-08T03:21:30.952Z", "level": "INFO", "logger": "dragon_agent.hello", "msg": "tool.finished", "trace_id": "03cbd896e49e4434b45743bc3c5d13a2", "tool": "read_budget", "ok": true, "duration_ms": 0, "observation": "{\"fiscal_year\": \"2026\", \"items\": {\"研发\": 620000, \"市场\": 250000, \"运维\": 130000}, \"total\": 1000000}"}
{"ts": "2026-11-08T03:21:30.952Z", "level": "INFO", "logger": "dragon_agent.hello", "msg": "tool.finished", "trace_id": "03cbd896e49e4434b45743bc3c5d13a2", "tool": "read_budget", "ok": false, "duration_ms": 0, "observation": "[工具 DependencyError: 预算库 1999 不可用 ({'fiscal_year': '1999', 'source': 'budget-store'})]"}
{"ts": "2026-11-08T03:21:30.952Z", "level": "INFO", "logger": "dragon_agent.hello", "msg": "run.finished", "trace_id": "03cbd896e49e4434b45743bc3c5d13a2", "steps": 3, "tool_calls": 2}
```

四行日志，一个 trace_id。第二次工具调用失败，但失败被回灌给了模型，模型据此在最终答案里说明"1999 年预算库不可用，未能查到"——**失败没有把任务带崩**。

测试：

```powershell
python -m unittest tests.test_skeleton_basics -v
```

```text
----------------------------------------------------------------------
Ran 25 tests in 0.009s

OK
```

退出码 0。（用例数固定是 25，耗时随机器而变。）

### 4.7 骨架怎么被上下游调用

| 骨架里的东西 | 谁来用 | 怎么用 |
| --- | --- | --- |
| `config.Config` | 所有层 | 唯一的配置来源；`to_dict()` 进 trace 和检查点 |
| `errors.DragonError` | 编排层、工具层 | `catch DragonError` 兜住**框架层**异常（模型 / 编排 / 外部依赖），按 `kind` 分派。注意它**不覆盖** `config.ConfigError`、`tools.ToolError`、`orchestration.PlanError`——这三个各自继承 `Exception`，属于更靠近调用方的参数与状态错误，不要指望被 `DragonError` 兜住 |
| `errors.classify()` / `policy_for()` | 编排层 | 决定这一类错误要不要重试、重试几次 |
| `errors.DependencyError` | 工具实现 | 在工具内部抛，工具层边界转成 `ToolResult(ok=False)` |
| `logging_setup.trace_scope()` | 应用层、编排层 | 包住整次任务，作用域内的日志自动带上 trace_id |
| `logging_setup.log_event()` | 所有层 | 记结构化事件，扩展字段自动脱敏 |
| `logging_setup` 的 trace_id | 第 12 章 `observability.py` | trace 与日志用同一个 trace_id 关联 |

## 五、开源实现对照

### Python 标准库 logging

- 文档：<https://docs.python.org/3/howto/logging.html>

`logging` 的分层是"logger → handler → formatter"，本工程的 `JsonFormatter` 就是挂在 `StreamHandler` 上的一个 formatter。**差异**：标准库只保证"记下来"，不保证"记成什么形状"；字段固定、trace_id 贯穿、脱敏这三件事，标准库不管，得自己写。`configure_logging()` 里那段"先摘掉旧 handler"的代码也不是多余的——标准库不会自动去重，重复配置就会打印两遍。

### structlog

- 仓库：<https://github.com/structlog/structlog>
- 许可证：MIT OR Apache-2.0（双许可）

它把结构化日志做成了标准做法：日志是 key-value 事件而不是自由文本，处理器可以自由组合（加时间戳、加 trace_id、脱敏各是一个 processor）。**差异**：structlog 是第三方依赖，本工程的契约要求纯标准库，所以本章手写了一个 230 行的最小版本（`logging_setup.py`）。真正值得借鉴的是它的**思路**——把脱敏、上下文注入都做成可组合的处理器，而不是散在调用点。

### Pydantic Settings

- 仓库：<https://github.com/pydantic/pydantic-settings>
- 许可证：MIT

环境变量与配置文件的读取、类型转换、校验，都用 Pydantic 模型声明，非法值在加载时就报错。**差异**：本工程的 `Config` 是手写的 `frozen` dataclass，没有类型转换（配置项都是 `int`/`float`/`str`，转换需求很小），也不读配置文件。两者共享的是同一个原则：**配置在入口处一次性解析并校验，之后全程是一个不可变对象**。

### tenacity

- 仓库：<https://github.com/jd/tenacity>
- 许可证：Apache-2.0

重试策略库：可配置重试次数、退避、只对特定异常重试、失败后回调。**差异**：本工程没有引它，因为可重试的错误只有两类，且退避逻辑很简单，直接写在 `ResilientLLM` 里更直白。等重试策略复杂起来（不同异常不同退避、熔断、抖动），再引它不迟。

### OpenTelemetry Python

- 仓库：<https://github.com/open-telemetry/opentelemetry-python>
- 许可证：Apache-2.0

跨厂商的追踪与指标标准，span 的父子关系、上下文传播（把 trace_id 跨进程传下去）都有统一约定。**差异**：本工程第 12 章的 `observability.py` 用的是 OpenTelemetry 的**属性命名约定**（`gen_ai.*`），但没有引它的 SDK——纯标准库实现 trace 与成本账。`contextvars` 存 trace_id 的做法，正是 OTel 上下文传播的标准手段。

### OWASP 日志安全速查表

- <https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html>

上面"哪些字段不能进日志"、攻击者会怎么利用日志里的敏感信息，有一份可查的清单。**差异**：本工程的 `SENSITIVE_KEYS` 是一份精简版，只有十几个常见键名；真实项目应该按自己的数据分类补全，并把**正文脱敏**也纳入检查——密钥未必只出现在键值对里。

## 六、坑与检查清单

### 目录与依赖

- [ ] 配置、异常、日志这类叶子模块不依赖任何上层模块
- [ ] 编排层只通过公开接口调用工具层与模型层，不 import 内部实现
- [ ] 应用层不直接调用模型层与工具层
- [ ] 依赖方向不可逆：改动下层不影响上层签名

### 配置

- [ ] 只有一个构造入口，别的模块拿不到"半配置"状态
- [ ] 配置不可变，要改就重建一份
- [ ] 非法配置在启动期就退出，而不是跑起来才炸
- [ ] 落盘表示（trace、检查点、错误详情）不含密钥
- [ ] 命令行参数优先于环境变量，环境变量优先于默认值

### 错误

- [ ] 每个错误都归到某一类，处理策略写在策略表里而不是散落的 if
- [ ] 运行期的可预期失败用返回值表达，不抛异常
- [ ] 异常在层内传播、在层边界转成返回值
- [ ] 不造同名分身：同名异常类全工程只有一个
- [ ] 未知异常有明确的兜底策略，不静默吞掉

### 日志

- [ ] 一行一条 JSON，固定字段是 ts / level / logger / msg / trace_id
- [ ] 一次任务一个 trace_id，作用域退出自动还原
- [ ] 脱敏在写出去之前，键名与正文都遮
- [ ] 日志走 stderr，程序输出走 stdout
- [ ] 日志里不塞完整 prompt 和完整工具输出——既泄露又烧存储

### 常见坑

- [ ] **用 print 调试**：调试完没删干净，最后日志和程序输出混在一起
- [ ] **把 trace_id 当参数层层传**：函数签名被污染，改一层要改一片
- [ ] **脱敏在读的时候做**：写出去的时候已经泄了
- [ ] **密钥进异常 message**：`str(exc)` 会被原样写进日志
- [ ] **except Exception 后直接 pass**：错误消失得无影无踪
- [ ] **配置用全局变量**：测试之间互相污染，并发下互相覆盖
- [ ] **同一个错误在三个地方各处理一次**：修一处忘两处

## 七、小结与延伸阅读

这一章没有写 Agent 的业务逻辑，只把地基打了四根桩。要点回顾：

1. **分层按"谁决定下一步做什么"划分，依赖方向不可逆。** 配置、异常、日志是叶子模块，所有层都能用，它们不依赖任何一层。
2. **配置三件套：单一入口、早失败、不可变。** 加上 `to_dict()` 不含密钥，配置就既安全又可信。
3. **错误分类是为了统一处理策略。** 四类运行期错误 + 一类启动期错误；运行期失败优先用返回值表达；同名类不许造分身。
4. **日志三件套：字段固定、trace_id 贯穿、写入前脱敏。** 排查一次任务靠的是这三样，不靠记性。
5. **骨架的代价是真实的**，只在"活得久"的系统上划算。规则靠自觉守不住，要写成断言。

下一章 `p03-llm-client` 开始做模型接入抽象：`MockLLM` 与真实 API 返回同一种 `LLMResponse`，让"能不能跑通"和"跑得对不对"这两件事分开。

**延伸阅读**

- 第一卷第 3 章《为什么需要架构：可靠性、复利与工程约束》——过度工程化的判据
- 第一卷第 8 章《工具调用契约：把 Function Calling 当作 API 工程》——错误回灌与参数校验
- 第一卷第 11 章《停止准则、步数上限与成本守卫》——预算守卫与失败分类的关系
- 第一卷第 33 章《可观测性：Trace、Span 与 Agent 专属指标》——第 12 章会展开
- 第一卷第 36 章《生产化检查清单与真实故障模式》——上线前该有哪些兜底
- Python logging HOWTO：<https://docs.python.org/3/howto/logging.html>
- structlog：<https://github.com/structlog/structlog>（MIT OR Apache-2.0）
- Pydantic Settings：<https://github.com/pydantic/pydantic-settings>（MIT）
- tenacity：<https://github.com/jd/tenacity>（Apache-2.0）
- OpenTelemetry Python：<https://github.com/open-telemetry/opentelemetry-python>（Apache-2.0）
- OWASP 日志安全速查表：<https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html>
