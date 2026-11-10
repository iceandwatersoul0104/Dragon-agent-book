# 第 6 章 Agent 主循环：带预算守卫的 ReAct

## 一、本章要解决的问题

主循环是整个 Agent 里**看起来最简单、实际上最容易写坏**的部分。`while True` 加个模型调用，十分钟就能写完。但真正上线的那一版，值班记录是这样的：

- 第一次接真实模型，任务跑到第 8 步还在转圈——**没有步数上限**，烧完 3 美元才被人发现；
- 模型把参数写错了，工具层抛出异常，异常一路冒到接口层，用户看到的是 **500**；
- 模型卡在同一个工具上死循环，每次都调、每次都失败，**没有任何机制发现它没进展**；
- 有一条历史消息每步都被重放，单步多花 500 token，12 步的任务多花了 6000 token。

四条问题的根子是同一个：**主循环不是"循环"，它是一组停止准则。**

"什么时候停"这件事，必须在写循环之前就想清楚。因为停止准则决定了另外三件事：预算怎么算、失败怎么降级、测试怎么写。本章把它们一次定完。

## 二、原理：机制拆解

### 2.1 循环骨架

主循环只有一条路径，每个环节的职责都来自前面几章：

```
   任务描述
      │
      ▼
  ┌───────────────────┐
  │ 装配上下文        │ ← 第 5 章：分预算、裁剪、稳定前缀
  └─────────┬─────────┘
            ▼
  ┌───────────────────┐
  │ 问模型            │ ← 第 3 章：一次调用，返回 LLMResponse
  └─────────┬─────────┘
            │
     有工具调用？──没有──▶ 答案生成完毕 ──▶ 停止（completed）
            │是
            ▼
  ┌───────────────────┐
  │ 执行工具          │ ← 第 4 章：校验、回灌、幂等
  └─────────┬─────────┘
            ▼
  ┌───────────────────┐
  │ 观察回灌 + 记状态 │ ← 第 7 章：检查点、事件溯源
  └─────────┬─────────┘
            │
            └──────────▶ 回到装配上下文
```

关键在于：**每一轮开始前先问一遍停止准则**。预算在问模型之前检查——预算用完了还去问模型，等于白花钱。

### 2.2 停止准则：五种结局，一个优先级

五种结局：

| `status` | 什么时候 | 谁负责 |
| --- | --- | --- |
| `completed` | 模型给出了最终答案 | 调用方消费结果 |
| `error` | 模型调用失败，或任务内部出了意外 | 调用方决定重试还是转人工 |
| `stopped_budget` | token / 时间 / 美元任一预算到顶 | 调用方放宽预算或缩小范围 |
| `stopped_no_progress` | 连续几步没有进展 | 调用方转人工，或换策略 |
| `max_steps` | 撞上步数上限 | 调用方消费已完成的部分 |

**优先级固定：完成 → 异常 → 预算 → 无进展 → 步数上限。**

同时满足多个条件时，排在前面的胜出。最容易被搞错的是"完成"和"步数上限"：模型在第 12 步给出了答案，同时这一步也撞上了 12 步的上限——**算完成**，不是撞上限。因为上限是"没做完就被打断"，而这一步做完了。

所以预算检查放在**每轮开头**（问模型之前），完成判定放在**拿到响应之后立刻**。这个位置关系就是优先级的全部实现：

```python
            while True:
                # ---- 停止准则：预算 → 无进展 → 步数上限 ----
                if self.budget.exhausted():
                    status, stop_reason = "stopped_budget", self.budget.reason()
                    break
                reason = self._no_progress_reason()
                if reason:
                    status = "stopped_no_progress"
                    stop_reason = f"检测到无进展：{reason}"
                    break
                if len(steps) >= max_steps:
                    status = "max_steps"
                    stop_reason = f"达到步数上限 {max_steps}"
                    break
```

### 2.3 无进展检测：三个信号

"没进展"不是一件事，是三件不同的事。第一卷第 11 章把它们拆开，本章逐个实现：

| 信号 | 怎么判定 | 典型场景 |
| --- | --- | --- |
| 动作重复 | 连续 N 步是同一个动作（名字和参数都一样） | 模型死循环调同一个工具 |
| 观察不变 | 连续 N 步拿到的观察完全相同 | 反复读同一个文件 |
| 状态无变化 | 连续 N 步没有新的成功结果 | 工具一直失败，模型一直没换办法 |

```python
    def _no_progress_reason(self) -> str | None:
        """三个信号：动作重复、观察不变、状态无变化。

        任意一个信号连续出现 ``no_progress_threshold`` 次就判定无进展。
        """
        n = self.no_progress_threshold
        ...
        # 信号一：动作重复（名字和参数都相同）
        signatures = [
            (step.get("action"), json.dumps(step.get("arguments") or {}, sort_keys=True,
                                          ensure_ascii=False, default=str))
            for step in recent
        ]
        if len(set(signatures)) == 1 and signatures[0][0]:
            return "连续执行同一个动作"
```

三个信号**任意一个**成立就停。理由是：这三个信号各自都足以说明"再转下去也是一样的"。只靠一个信号会漏判（模型换了个工具名但参数一样，照样是死循环）。

阈值取 **3**：连续 3 步出现同一个信号。太小会误杀正常的多步操作（同一个工具连续查三张表是合法的），太大则要多烧好几步的钱。

注意"状态无变化"这个信号用的不是"状态快照没变"——状态每步都在写（版本号在涨），那样永远判定为"有变化"。它比的是**成功结果的计数**：工具一直失败，成功计数不涨，那就是世界没变。

### 2.4 预算守卫：一个接口，三种预算

token、时间、美元是三种完全不同的预算，但如果主循环里写三套 if，就每加一种预算都要改主循环。所以它们共用一个接口：

```python
class BudgetGuard:
    """预算守卫的统一接口。

    三种预算（token、时间、美元）共用这一套接口：主循环只会问
    "还能花吗"和"为什么停了"，不关心背后是哪一种预算。加一种新预算，
    主循环一个字都不用改。
    """

    kind = "budget"

    def __init__(self) -> None:
        self.spent = 0.0

    def charge(self, tokens: int, cost: float, elapsed_s: float) -> None:
        """记一次消耗。"""

    def exhausted(self) -> bool:
        """预算用完了吗？"""
        return False

    def reason(self) -> str:
        """用完了就说清楚：哪一种预算、用了多少、上限多少。"""
        return ""
```

三个具体实现各自只回答"还能花吗"：

```python
class TokenBudgetGuard(BudgetGuard):
    """token 预算。"""

    kind = "token"

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = int(limit)
        self.spent = 0

    def charge(self, tokens: int, cost: float, elapsed_s: float) -> None:
        self.spent += int(tokens or 0)

    def exhausted(self) -> bool:
        return self.spent >= self.limit

    def reason(self) -> str:
        return f"token 预算耗尽：已用 {self.spent}，上限 {self.limit}"
```

多个预算合成一个：

```python
class CompositeBudgetGuard(BudgetGuard):
    """把多个预算合成一个。任何一个到顶就停，停的时候报最具体的那一个。"""

    kind = "composite"

    def __init__(self, guards: list[BudgetGuard] | None = None) -> None:
        super().__init__()
        self.guards: list[BudgetGuard] = list(guards or [])

    def exhausted(self) -> bool:
        return any(guard.exhausted() for guard in self.guards)
```

主循环里只有两行和预算有关：`charge`（记一次消耗）和 `exhausted`（还能花吗）。加第四种预算（比如"最多调工具 20 次"）只要多写一个 `BudgetGuard` 子类，主循环不用动。

### 2.5 优雅降级：失败也是一等公民

主循环里**没有一行 `try/except` 包裹工具调用**。工具失败不会中断循环，因为工具层已经把它变成了一条失败结果：

```python
        key = self._idempotency_key(call, index) if self._has_side_effect(call.name) else None
        result = self.tools.invoke(call.name, call.arguments, idempotency_key=key)
        ok = bool(getattr(result, "ok", False))
        if ok:
            self._progress += 1
        return result.render(), ok
```

错误文本被当成"观察"回灌给模型，模型下一轮就会换个参数、换个工具。这是第 4 章"错误回灌"在主循环里的落点。

护栏是两道关，位置很关键：

- **工具前**：被拦下的工具**一次都不能执行**，原因回灌给模型；
- **输出后**：被拦下的内容**不能进答案**。

```python
    def _blocked_reason(self, text: str) -> str:
        """输出护栏：返回拦截原因；没被拦就返回空串。

        护栏自己抛异常时**不拦截**——把一次本来成功的任务变成失败，比放过
        一段文本更糟。但异常要留下痕迹：记进状态层的事件流，别让它悄悄过去。

        这和工具前的护栏正好相反：工具前的护栏坏了就**不执行**（有副作用，
        关不住就别动）；输出护栏坏了就**放行**（没有副作用，拦下来只会
        白白毁掉一次成功）。
        """
```

这两条的方向**故意相反**，理由是副作用：工具调用会改变世界，关不住就不该动；答案是返回值，拦下来只是毁掉一次本来成功的结果。

### 2.6 幂等键：不可逆的动作只做一次

主循环**不重试工具**。但它必须给副作用工具带幂等键，否则第 4 章的规则会直接拒绝执行：

```python
    def _idempotency_key(self, call: ToolCall, index: int) -> str:
        """副作用工具的幂等键：任务 + 步骤 + 工具名 + 参数摘要。

        同一个任务从同一个检查点重跑，会拿到同一个键——不可逆的工具因此
        不会被执行第二次。
        """
```

键里有**步骤序号**，所以同一任务里的两次不同调用不会互相撞；键又是**确定的**，所以同一个检查点重跑会拿到同一个键——不可逆的工具因此不会执行第二次。

只读工具**不给**键：给了就意味着"同样的调用只算一次"，会掩盖真实的重复劳动，也会让无进展检测失灵。

### 2.7 每步写检查点

一步结束做三件事：记历史、记状态、写检查点。

```python
    def _finish_step(self, step: AgentStep, history: list[dict]) -> None:
        """一步结束：记历史、记状态、写检查点。

        检查点**每步一次**——这是崩溃后能接着跑的依据。
        """
```

`state.py` 里的 `TaskState` 是**任务状态**的唯一真相源：谁也不许自己存一份副本——两份状态迟早会不一致，而且不一致的时候没人知道该信谁。

这里要把边界说清楚，否则"唯一真相源"会被读成一个更大的承诺。主循环装配上下文时传的是 `task=task, history=history, retrieval=[]`（`agent.py:273-275`）：`task` 是这次运行的入参，`history` 是主循环里的局部列表，**两者都不是从 `TaskState` 里读出来的**。`TaskState` 负责的是 `status`、`data`、`steps` 这几项——每一步的思考、动作、观察都记在 `steps` 里，恢复之后靠它们把 history 重建出来（见第 7 章）。

## 三、为什么这样设计：取舍与失效条件

### 3.1 什么时候**不该**有主循环

- **流程固定、每步确定。** 那是工作流（第 1 章的伪需求判据），写代码更稳。
- **一次调用就能答完。** 直接调模型，不用循环。
- **"下一步做什么"由人来定。** 那是人机协作界面，不是 Agent。

主循环的收益来自"**让模型决定下一步**"。没有这件事，循环只是多绕一圈。

### 3.2 主循环的五种失效方式

| 失效方式 | 症状 | 后果 |
| --- | --- | --- |
| 异常冒泡 | 工具抛异常直接冲到接口层 | 用户看到 500 |
| 停止准则顺序错 | 撞上限时优先报"完成"，或反之 | 结果对不上账 |
| 无进展阈值太低 | 正常的多步操作被判成卡死 | 任务被提前掐断 |
| 重试副作用工具 | 自动重试一次扣款 | 真扣了两次钱 |
| 每步重装上下文 | 每步都把历史全量重放 | token 成本翻几倍 |

第一种靠"工具层不抛异常"和"run() 包一层 try"挡住。第二种靠**位置**挡住：完成判定紧跟在响应之后，预算判定在循环开头——代码顺序就是优先级。第三种靠阈值 3 和"任意信号成立才停"。

第四种是本章最容易犯的错：**不要在主循环里给工具加重试**。第 3 章讲过，模型层可以退避重试（同一个请求重发，服务端可能重复计费但不会重复执行副作用），工具层不行——工具的副作用是真实的业务动作，重试就是重复扣款。模型层重试和工具层重试是两回事。

第五种是第 5 章上下文复用的延伸：稳定前缀（system + tools）在多轮之间不变，缓存能命中；但历史每轮都在长，装配器会按预算裁它。

### 3.3 两个阈值的取舍

**步数上限**：定得太小，复杂任务做不完；定得太大，一次跑飞要烧掉整个预算。经验值是 8 到 15——普通任务 3 到 5 步就够，复杂的 12 步左右该收手。

**无进展阈值**：3 是通用值。特殊任务可以调：探索性任务（模型需要多试几次）调到 5；线上收费类任务调到 2，早停早省钱。

两个阈值都应该**可配置**，而不是写死在代码里。本章把它们放在类属性上（`no_progress_threshold`），配置化是第 14 章的活。

## 四、动手：最小可实现

### 4.1 本章的工程产物

| 文件 | 作用 |
| --- | --- |
| `dragon_agent/state.py` | 任务状态机：唯一真相源、检查点、事件溯源、恢复 |
| `dragon_agent/agent.py` | ReAct 主循环、停止准则、预算守卫、优雅降级 |
| `tests/test_agent.py` | 主循环的确定性测试：24 条断言 |
| `tests/test_state.py` | 状态层单元测试：14 条断言 |

### 4.2 状态层

三个数据结构，三个方法：

```python
@dataclass
class TaskState:
    """一次任务的当前状态。"""

    task_id: str
    status: str = "running"
    data: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    version: int = 0
```

```python
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
```

`snapshot()` 返回的是**副本**：调用方拿到手随便改，都不会污染真相源。这是"唯一真相源"能成立的前提。

### 4.3 主循环

完整的一次循环就这么长：

```python
                # ---- 问模型（第 3 章的活） ----
                resp: LLMResponse = self.llm.complete(messages, tools=schemas)
                tokens = resp.usage.total
                cost = self._cost_of(resp)
                total_tokens += tokens
                total_cost += cost
                self.budget.charge(tokens, cost, self._clock() - started)

                if resp.finish_reason == "error":
                    status = "error"
                    stop_reason = f"模型调用失败：{resp.text}"
                    break
```

而整个 `run()` 外面只包了一层 `try`：

```python
        except Exception as exc:  # 任何意外都变成结果，不冒泡
            status = "error"
            stop_reason = f"任务异常终止：{type(exc).__name__}: {exc}"
```

这就是"`run()` 永不抛异常"的全部实现——**不是每个环节都 try，而是最外层兜住**。局部 try 用来做"降级"（工具失败继续跑），最外层 try 用来做"转换"（意外变成结果）。

### 4.4 实跑

工程里自带的端到端演示，跑的就是这条回路：

```powershell
cd dragon-agent
python examples/end_to_end_demo.py
```

```text
--- 轨迹 ------------------------------------------------------------
[第 1 步]
  思考   ：先读预算表
  动作   ：read_budget({'fiscal_year': '2026'})
  观察   ：read_budget: {"fiscal_year": "2026", "items": {"研发": 620000, "市场": 250000, "运维": 130000}, "total": 1000000}
  用量   ：149 tokens / 0.000000 美元
[第 2 步]
  思考   ：项目 2026 年预算总额为 100 万元，其中研发 62 万、市场 25 万、运维 13 万。
  动作   ：（无动作）
  观察   ：（空）
  用量   ：230 tokens / 0.000000 美元
------------------------------------------------------------------------

最终答案：
项目 2026 年预算总额为 100 万元，其中研发 62 万、市场 25 万、运维 13 万。
```

### 4.5 测试策略：用 mock 模型驱动每条路径

主循环的测试要覆盖**每一种结局**，而不是"跑一次不崩"。关键是用 `MockLLM` 的剧本把模型的行为完全定死：

```python
    def test_budget_exhausted_stops(self):
        """预算耗尽：一步就把 token 预算烧穿，下一轮就停。"""
        llm = MockLLM(script=[
            MockLLM.call("echo", {"text": "x"}, in_tokens=5000, out_tokens=100),
            MockLLM.say("不该走到这里", in_tokens=1, out_tokens=1),
        ])
        agent, _registry, _state = build_stack(llm, token_budget=1000, max_steps=10)
        result = agent.run("很贵的活")
        self.assertEqual(result.status, "stopped_budget")
        self.assertIn("token", result.stop_reason)
        self.assertEqual(len(result.steps), 1)
```

这段测试同时断言了三件事：状态是"预算耗尽"、原因里点明了是哪种预算、**只走了一步**（剧本里第二条根本不该被消费）。

每条停止路径一条测试，加起来是这一章最有价值的交付：

| 路径 | 怎么造出来 |
| --- | --- |
| 正常完成 | 剧本第一条就是最终答案 |
| 预算耗尽 | 第一条响应就带超大 token 数 |
| 无进展 | 剧本里连着 8 条一模一样的工具调用 |
| 步数上限 | 每次动作都不一样，撞到上限 |
| 工具报错后继续 | 第一条调用一个会抛异常的工具，第二条给答案 |
| 不抛异常 | 换一个一调用就炸的假模型 |

另外还要测**停止优先级**——同时满足两个条件时哪个赢：

```python
    def test_completed_beats_budget(self):
        """完成 > 预算：给出答案那一步把预算烧穿，仍然算完成。"""
        llm = MockLLM(script=[MockLLM.say("答完了", in_tokens=5000, out_tokens=500)])
        agent, _registry, _state = build_stack(llm, token_budget=1000, max_steps=5)
        result = agent.run("很贵的答案")
        self.assertEqual(result.status, "completed")
```

这类测试的价值在于：它们把"优先级"这条约定**钉死**了。将来有人调整代码顺序，这条测试立刻会红。

### 4.6 骨架怎么被上下游调用

| 骨架里的东西 | 谁来用 | 怎么用 |
| --- | --- | --- |
| `Agent.run()` | 应用层、编排层 | 跑一次任务，拿到结果对象 |
| `AgentResult.status` | 应用层 | 决定是消费结果、换策略，还是转人工 |
| `AgentResult.stop_reason` | 应用层、日志 | 说清楚为什么停 |
| `AgentStep` | trace | 每一步的思考、动作、观察、用量 |
| `BudgetGuard` | 编排层 | 加一种预算只需多写一个子类 |
| `StateStore` | 编排层、检查点 | 每步写一次，崩溃后能恢复 |
| `guards`（护栏） | 第 9 章 | 工具前拦、输出后拦 |

## 五、开源实现对照

### ReAct

- 论文：<https://arxiv.org/abs/2210.03629>

把"推理"和"行动"交替起来的原始范式：思考 → 行动 → 观察 → 再思考。**差异**：论文里的循环靠提示词约束终止，是"模型自己会停"。本章的主循环靠**外部的停止准则**兜底——模型不停，循环也会停。这是工程实现和原始论文最大的分歧，也是可靠性的来源。

### smolagents

- 仓库：<https://github.com/huggingface/smolagents>
- 许可证：Apache-2.0

极简 Agent 库，代码即行动（Code-Act），主循环短得惊人。**差异**：它把"停止"很大程度上交给模型和代码执行结果；本章把停止准则做成显式的、可测试的五种结局。smolagents 适合快速原型和玩具任务，本章这套适合**失败代价高**的场景。

### OpenAI Agents SDK

- 仓库：<https://github.com/openai/openai-agents-python>
- 许可证：MIT

轻量多 Agent 工作流，内置 tracing、护栏和交接。**差异**：它的 `Runner` 也做预算控制（`max_turns`），但停止原因是"轮次用完"这一种；本章把停止拆成五种结局，每种都带一句人能读懂的 `stop_reason`。差别在于**可观测性**：线上出事时，"是撞上限了还是卡住了"这两个答案指向完全不同的排查方向。

### LangGraph

- 仓库：<https://github.com/langchain-ai/langgraph>
- 许可证：MIT

用显式状态图编排 Agent，循环是图里的一个节点，条件边负责决定走向。**差异**：LangGraph 把"下一步去哪"交给开发者画图决定，可控性最强、表达力也最强；本章的主循环是一条固定路径，停止由准则决定。任务流程复杂、需要人工介入分支时，LangGraph 那条路更合适；流程就是"问-答-调工具-再问"时，本章这条更省心。

## 六、坑与检查清单

### 停止准则

- [ ] 五种结局都有明确的 `status` 和一句人能读懂的 `stop_reason`
- [ ] 优先级是 完成 → 异常 → 预算 → 无进展 → 步数上限
- [ ] 预算检查在**问模型之前**
- [ ] 完成判定紧跟在**拿到响应之后**
- [ ] `run()` 最外层兜住所有意外，转成 `status="error"`

### 无进展检测

- [ ] 三个信号都实现了：动作重复、观察不变、状态无变化
- [ ] 任意一个信号连续 N 次成立就停（N 默认 3）
- [ ] 阈值可配置，不写死在业务逻辑里
- [ ] "状态无变化"比的是**成功结果计数**，不是状态版本号

### 预算

- [ ] 三种预算共用一个接口，主循环只问"还能花吗"
- [ ] 每种预算都报得清：哪一种、用了多少、上限多少
- [ ] 多个预算合成一个时，报最具体的那一个
- [ ] 步数上限有默认值，也能在配置里调

### 降级与副作用

- [ ] 工具失败不中断循环：错误回灌给模型
- [ ] 主循环**不给工具加重试**
- [ ] 副作用工具带确定的幂等键（任务 + 步骤 + 工具 + 参数）
- [ ] 只读工具不给幂等键（否则会掩盖重复劳动）
- [ ] 护栏拦下的工具一次都不执行，原因回灌
- [ ] 护栏拦下的输出不进答案，也不进状态

### 常见坑

- [ ] **工具异常直接冒泡**：用户看到 500，而不是模型的自我纠正机会
- [ ] **把"完成"排在步数上限之后**：最后一步做完的任务被报成"被中断"
- [ ] **在主循环里重试工具**：不可逆动作执行两次，等于重复扣款
- [ ] **无进展阈值是 1**：正常的连续两步就被判成卡死
- [ ] **把状态快照的版本号当"进展"**：版本每步都在涨，信号永远不成立
- [ ] **每步把历史全量重放**：token 成本翻几倍
- [ ] **只有 try/except 没有 status**：调用方分不清"失败"和"没做完"

## 七、小结与延伸阅读

这一章把"问模型 → 调工具 → 再问"写成了一个**可靠的**回路。要点回顾：

1. **主循环的本质是停止准则。** 五种结局、一个固定优先级，代码顺序就是优先级。
2. **预算在问模型之前查。** 预算用完了还去问模型，等于白花钱。
3. **无进展检测看三个信号**：动作重复、观察不变、状态无变化，任意一个连续 3 次成立就停。
4. **三种预算共用一个接口**，加新预算不用改主循环。
5. **降级优先于中断**：工具失败回灌给模型让它自己改；主循环**绝不给工具加重试**。
6. **护栏的两道关方向相反**：工具前坏了就不执行，输出后坏了就放行——因为前者有副作用。

下一章 `p07-state-checkpoint` 把状态与检查点讲透：事件溯源、崩溃恢复、跨进程回放。

**延伸阅读**

- 第一卷第 4 章《ReAct：推理与行动交替的经典范式》——原始范式
- 第一卷第 6 章《反思与自纠错：Reflexion、Self-Refine 与独立评审者》——把失败写进记忆再重试
- 第一卷第 11 章《停止准则、步数上限与成本守卫》——四种停止准则与无进展的三个信号
- 第一卷第 29 章《检查点、回放与多 Agent 共享状态》——恢复与时间旅行
- 第一卷第 32 章《提示注入与越权防护》——护栏的威胁模型
- 第一卷第 35 章《成本与性能优化》——预算守卫怎么定
- ReAct：<https://arxiv.org/abs/2210.03629>
- smolagents：<https://github.com/huggingface/smolagents>（Apache-2.0）
- OpenAI Agents SDK：<https://github.com/openai/openai-agents-python>（MIT）
- LangGraph：<https://github.com/langchain-ai/langgraph>（MIT）
