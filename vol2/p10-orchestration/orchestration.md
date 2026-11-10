# 第 10 章 编排层：从单 Agent 到多 Agent 的升级路径

## 一、本章要解决的问题

先讲一个翻车现场。

某团队做了一个"竞品财报分析" Agent：一个 Agent 读网页、抽取指标、出一张对比表。跑得挺好。

后来需求变了：要同时分析 20 家公司、每家 3 个来源、还要交叉验证口径。团队判断"任务变大了，得多几个 Agent"，于是拆成 5 个 Agent 并行跑，最后汇总。

结果三件事同时发生：**token 账单涨了 3 倍**；**答案反而更差了**——5 份摘要拼起来，关键的毛利率差异被抹平了；**同一个数字在两份报告里对不上**，因为两个 Agent 查的是不同的页面版本。最后结论是"多 Agent 不好用"，团队退回单 Agent，然后撞上了上下文上限。

判断错在**升级的理由**。他们看到的是"任务变大了"，而任务变大恰恰是**不该**升级的信号——单 Agent 上下文装不下，该做的是分阶段、分文件处理，不是分 Agent。

多 Agent 真正能解决的，是四种具体的失败：

1. **串行等待**：子任务之间互不依赖，串着做是白等；
2. **上下文互相污染**：两个子任务需要的背景资料完全不同，塞在一个上下文里互相干扰；
3. **能力异构**：子任务需要不同的工具、不同的模型（一个要检索、一个要算数、一个要写代码）；
4. **权限隔离**：子任务需要不同的授权范围（查公开资料的不该碰得到扣款的凭据）。

**这四条一条都不占，就别拆。** 拆了以后你只是把"一个笨 Agent"换成了"五个互相传话的笨 Agent"，外加一份难维护的编排代码。

所以这章的顺序是：**先给判据，判据不满足就别动**；满足了就再挑拓扑；然后写 Supervisor、共享状态和熔断。配套工程产物是 `dragon_agent/orchestration.py`，纯标准库、不联网、不需要 API Key。

## 二、原理：机制拆解

### 2.1 四个判据：缺一个都别拆

判据必须是**可以当面回答的问题**，不是模糊的印象。"这个任务比较复杂"不是判据，"这两个子任务需要不同的凭据吗"才是。

```python
#: 四个判据的名字。**缺一个都别上多 Agent。**
UPGRADE_CRITERIA: tuple[str, ...] = (
    "parallel",
    "context_isolation",
    "capability_heterogeneity",
    "permission_isolation",
)
```

判定是纯函数，同样的判据永远给同样的结论——它不看模型怎么想，因为"该不该升级"每分钟可能问一次，不能每次都调一次模型。

```python
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
```

"全中才算"是刻意的。多 Agent 的复杂度（编排代码、共享状态、熔断、调试）是**固定成本**，收益却要逐条兑现才够本。满足三条不满足第四条，说明你其实想要的是"换个更聪明的模型"或"把上下文分阶段装"。

判据不满足时，正确动作是**不动**。所以判定函数要能说出缺哪一条：

```text
  只满足两条：不满足判据（capability_heterogeneity、permission_isolation），留在单 Agent
  四条全中    ：四个判据全部满足，可以升级为多 Agent
```

### 2.2 拓扑：四种选择，四种代价

选拓扑不是选"哪个更先进"，是选"哪个的代价你能付"。这张表记的是代价，不是优点。

| 拓扑 | 共享状态 | 派单代价 | 收敛代价 | 什么时候崩 |
|---|---|---|---|---|
| 单 Agent | 不需要 | 零 | 零 | 上下文互相干扰；单次任务太长 |
| Supervisor | 编排者持有 | 一次规划 + 一次分派 | 一次汇总；信息在汇总处磨损 | 编排者成瓶颈；子任务一多摘要失真 |
| 层级 | 分层共享 | 每层一次规划 | 逐层向上汇总，误差累积 | 层数一多难调试：不知哪层想歪 |
| 网状 | 只有消息 | 无规划成本，但对话轮数不可预测 | **最难收敛**：没有中心判断"完成" | 容易无限对话；成本上限难设 |
| 黑板 | 一块共享文档 | 低：不用规划 | 要自己判断"写满了没有" | 文档会变长；冲突没人仲裁 |

这张表在代码里也是数据，不是注释——`TOPOLOGIES` 是一个 `dict[str, TopologySpec]`，拓扑推荐函数直接读它。

### 2.3 Supervisor：派单、回收、判定完成

Supervisor 是多 Agent 里最常见、也最不容易写坏的拓扑，因为它是**有中心的**：一个编排者说了算。

三段式，和人带项目一个节奏：

1. **派单**——规划出一批子任务（`SubTask`），按依赖排好序；
2. **回收**——每个子任务跑完，结论写进共享状态；
3. **判定完成**——编排者拿着全部结论合成最终答案；它说"还要再做 X"就再开一轮，说不出就收敛。

第三段是三段式里最容易被忽略、也最关键的一段。**"做完没有"必须有人判定**，而且必须是同一个有全局视角的人判定——否则每个子 Agent 都说"我这块做完了"，整体却还是半成品。

```python
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
```

"还要继续"的信号是编排者输出里的 `继续：<子任务名>|<指令>` 行。**解析不出来就是收敛信号**——空列表意味着编排者没话说，编排层就停。这个方向的默认值很重要：编排者沉默时默认结束，而不是默认继续。

派单之后是依赖调度。依赖**在这一批内部**也必须满足：`算账` 依赖 `查资料`，两个都在本轮计划里时，`查资料` 必须先跑完，结论落进共享状态，`算账` 才拿得到。

```python
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
```

并行那一支跑完后**还要按计划顺序重排**：线程完成顺序是不确定的，如果结果顺序跟着线程走，回放就没法做了。

### 2.4 共享状态：三种做法，三种冲突处理

多个 Agent 要交换结论，就有三种做法，差别不在性能，在**冲突怎么处理**。

| 做法 | 写 | 读 | 冲突 | 适合 |
|---|---|---|---|---|
| 共享内存 | 覆盖 | O(1) | **静默**——后写的盖掉先写的，没人知道 | 子任务几乎不写同一个键 |
| 共享文档 | 追加 | 扫最新 | **留痕**——同一键的两个版本都在文档里 | 结论要能追溯、要给人看 |
| 消息传递 | 发给某个信箱 | 看信箱 | 不会覆盖——消息只能读一次 | 需要定向传递、给特定的人 |

共享内存最省事，也最容易出事：两个 Agent 写同一个键，后者无声地盖掉前者，事后谁也说不清用的是哪个版本。所以本章的实现给**共享内存与共享文档**两种状态都留了 `conflicts()`——**能报出冲突的共享状态，比报不出来的更值得用**。（消息传递那种没有 `conflicts()`：它本来就不覆盖，只追加，所以"冲突"这个概念在它那儿不成立。）

```python
class MessagePassingState:
    """消息传递：没有共享变量，只有信箱。

    **代价**：写不能覆盖——消息是**发给某个人的**，收件人收一次就少一份。
    代价是没有全局视图：想知道"现在总共有什么结论"，得把信箱都翻一遍。
    """

    kind = "message_passing"

    def __init__(self) -> None:
        self._boxes: dict[str, list[dict]] = {}
        self._log: list[dict] = []

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
```

实跑里三种状态的行为差别一目了然：

```text
  三个状态都读到最新值：80 万 / 80 万 / 80 万
  共享内存的冲突：['预算']  ← 覆盖是静默的
  共享文档的冲突：['预算']  ← 留痕，冲突看得见
  共享文档全文：
    [1] research 写下 预算：100 万
    [2] calc 写下 预算：80 万
  消息传递的信箱：[{'key': '结论', 'value': '还能花 60 万', ...}]
  取走后：[{'key': '结论', 'value': '还能花 60 万', ...}] → 信箱 []
```

### 2.5 熔断：多 Agent 唯一不能省的稳定性机制

多 Agent 最常见的失败不是"某个子 Agent 挂了"，而是**永远不收敛**：一轮轮派工，一轮轮回来，账单见底。所以四道熔断缺一不可：

- **最大轮次**——轮数是硬上限；
- **无进展检测**——连续两轮结果完全相同就停；
- **强制收敛**——轮数用完时，丢弃"还要继续"的请求，用现有结论收尾；
- **单 Agent 回退**——规划失败或编排者出错时，把任务整个交回单 Agent。

最后这条最要紧：**多 Agent 永远要有一条退路**。规划失败不是异常情况，是常态——模型的输出格式不稳定是结构性的，不是靠提示词能修好的。

```python
def build_orchestrator(config: Config | None = None, agent: Any = None,
                       **kwargs: Any) -> Orchestrator:
    """**默认编排器工厂：返回 ``SingleAgent``。**

    这是契约里最重要的一条约束——多 Agent 是显式选择，不是默认行为。
    想用多 Agent，请显式构造 :class:`Supervisor`。
    """
    if agent is None:
        agent = build_default_agent(config)
    return SingleAgent(agent)
```

## 三、为什么这样设计：取舍与失效条件

**为什么默认是单 Agent。** 因为多 Agent 的收益需要**对照**才能看见，而"对照"意味着你得先有一个单 Agent 版本。所以 `SingleAgent` 不是一个降级方案，它是**基准线**：多 Agent 的每一次收益主张，都要拿它当尺子量。

**为什么判据是"全中"而不是"多数"。** 多数表决听着合理，但它默认了"上下文隔离"和"权限隔离"是可有可无的。实际上这两条恰恰收益最大：上下文隔离让每个子 Agent 更准，权限隔离让每个子 Agent 更安全。牺牲它们换来的并行，本质上是"用更差的判断换更快的速度"。

**什么时候不该用多 Agent**——这一段比前面都重要：

1. **任务变大了，不等于该拆。** 上下文装不下是**分阶段**的问题（分文件、分批、分阶段装），不是分 Agent 的问题。拆 Agent 之后每个 Agent 的上下文更小，但**汇总**会把细节抹掉——第一节那个财报案例就是这么翻的车。
2. **子任务之间有依赖，就别并行。** `查资料 → 算账 → 写报告` 是一条链，拆成三个 Agent 只是把顺序执行伪装成并行，还多付两次上下文交接的钱。这种情况该在**一个** Agent 里用多步工具调用。
3. **子任务同质、工具相同、只是数量多**——批量处理一百份文档，用一个 Agent 循环一百次，比开十个 Agent 便宜得多。
4. **需要严格一致的输出格式。** 多 Agent 各写各的，格式一致性要靠汇总层兜，兜不住的概率不低。
5. **延迟敏感且不可预排的交互式任务。** 多 Agent 的规划调用是额外的一次模型往返。

**两个真实缺陷的教训**（这两条是实现这一章时真踩到的，不是推演）：

- **轮次计数器差一。** 原本在每轮**末尾**把 `rounds` 加一，于是"跑了两轮"报出来是 3。熔断逻辑用 `rounds >= max_rounds` 判断，一旦计数偏大，**熔断会比预期早一轮触发**——多 Agent 的最后一轮结论就丢了。修法是把计数挪到每轮**开头**：`rounds` 的语义从"将要跑的轮次"变成"已经跑过的轮数"。教训：**熔断计数器的语义必须写进注释，并且要有断言守着**。本章的测试直接断言"轮数 == 1"和"轮数 == 2"，就是为这个。
- **复用同一个 Agent 会撞上它自己的无进展检测。** 第 6 章的主循环有一个"连续 3 步动作重复/观察不变/状态不变就判定无进展"的保护。编排者会被反复调用（规划一次、汇总一次、下一轮再来一次），如果沿用同一个有状态 Agent，第 4 次调用就会看到"连续三次同样的动作、同样空的观察"，**直接判定没进展并停下**。更糟的是它返回空答案，编排层会以为"编排者说完成了"，于是拿一份空结论收尾。

  修法是让**每一次编排调用都是独立的一次**：跑之前留个检查点，跑完恢复。

```python
    def _ask(self, agent: Any, prompt: str) -> AgentResult:
        """跑一次 agent，但**不共享上一次的步骤历史**。

        这是编排层最容易踩的坑：编排者会被反复调用（规划一次、汇总一次、
        下一轮再来一次）。如果沿用同一个有状态 Agent，第 4 次调用就会撞上
        主循环自己的**无进展检测**——它看到连续三次"同样的动作、同样空的观察"，
        就判定这次没进展，直接停。

        那不是编排层要的判断，是上一次运行的残留。
        所以每一次编排调用都是**独立的一次**：跑之前留个检查点，跑完恢复。
        """
```

教训比代码更值钱：**编排层和被编排的 Agent 之间有隐式契约**——你复用了一个有状态对象，就得知道它内部有哪些状态机。第 6 章的无进展检测是保护措施，但在编排层它变成了误杀。

**其他失效条件**：并行线程共享同一个 Agent 是不安全的（主循环的状态写入没有加锁）；共享状态在进程内，跨 worker 就要换成外部存储；规划靠解析模型的 JSON 文本，格式漂移就会让整次编排回退——所以必须有回退路径，而且回退路径要**用同一个单 Agent**，否则"退回"本身也是一次新的风险。

## 四、动手：最小可实现

### 4.1 判定完成：汇总的那一次调用

```python
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
```

注意最后那个兜底：**汇总失败时用子任务结论拼一个答案**，总比交一份空答案上去强。

### 4.2 装一个 Supervisor

```python
def build_supervisor(planner_agent, workers, solo):
    """装一个 Supervisor：编排者 + worker + 共享状态 + 回退路径。

    回退路径用的是**同一个单 Agent**，不是另造一套——
    "退回"本身不该是又一次新的风险。
    """
    return Supervisor(
        planner_agent,
        workers,
        state=SharedDocumentState(),
        fallback=SingleAgent(solo),
        max_rounds=2,
    )
```

`workers` 的键就是子任务里的 `worker` 字段，所以"能力异构"在代码里就落成了**不同的 worker 挂不同的工具和凭据**。`fallback` 传的是 `SingleAgent`——和单 Agent 路径**同一个实例**，不是另造一套。装好之后 `supervisor.run(任务)` 就和 `single.run(任务)` 一样用了。

### 4.3 实跑

`examples/orchestration_demo.py` 全离线，退出码 0。关键输出（节选）：

```text
1) 升级判据：缺一个都别上多 Agent
  只满足两条：不满足判据（capability_heterogeneity、permission_isolation），留在单 Agent
  四条全中    ：四个判据全部满足，可以升级为多 Agent

2) 五种拓扑，各自的实现代价
  四条全中 → 推荐拓扑：supervisor
  只满足两条 → 推荐拓扑：single

4) Supervisor 三段式：派单 → 回收 → 判定完成
  最终答案：Orion 预算 100 万，还能花 60 万。
  结束状态：completed
  停    因：所有子任务完成，编排者收敛
  回收到的子任务结论：
    - [查资料]（research，completed）：Orion 项目预算是 100 万
    - [算账]（calc，completed）：还能花 60 万
  轮数：1  用了 18 token

5) 依赖顺序与并行
  算账拿到的提示词里带着查资料的结论：
    你负责的子任务：算账
    - 查资料：Orion 项目预算是 100 万
  并行执行后结果顺序仍按计划：['查资料', '算账']

7) 熔断四件套
  (a) 最大轮次：停因：达到最大轮次 1，强制收敛
  (b) 无进展检测：停因：连续两轮结果完全相同，判定无进展，停止派单
  (c) 单 Agent 回退：停因：规划失败：没能从编排者的输出里解析出子任务：…
      答案：单 Agent 的答案
      是否回退：True
  (d) 编排者被反复调用时，步骤历史不能共享
      编排者被调用 4 次（1 次规划 + 3 次汇总）
      轮数：3
      停因：所有子任务完成，编排者收敛

8) 编排的账
  rounds         1
  fell_back      False
  stop_reason    所有子任务完成，编排者收敛
  topology       supervisor
  shared_state   shared_document
  total_tokens   18
  total_cost     0.03
```

第 7 段的 (b) 和 (d) 是本章最值得看的两行：(b) 证明熔断真的会触发，(d) 证明**熔断不会误触发**——编排者被调用了 4 次，答案照样正常产出，没被它自己的无进展检测干掉。

## 五、开源实现对照

| 项目 | 仓库 | 许可证 | 和本章的关系 |
|---|---|---|---|
| AutoGen | https://github.com/microsoft/autogen | MIT（LICENSE-CODE）；根目录 LICENSE 为 CC-BY-4.0 | 以可对话 Agent 与 GroupChat 支撑多 Agent 协作——对应第 2.3 节的"判定完成"由对话轮次承担。**仓库已宣布进入维护模式**（只收 bug 修复与文档），官方把新功能导向 Agent Framework |
| LangGraph | https://github.com/langchain-ai/langgraph | MIT | 用显式状态图编排可循环、可恢复的工作流——对应第 2.4 节的共享状态与第 2.5 节的熔断 |
| CrewAI | https://github.com/crewAIInc/crewAI | MIT | 用角色、任务与流程模板组织 Agent 团队——对应第 2.2 节的"能力异构"与角色分工 |
| Microsoft Agent Framework | https://github.com/microsoft/agent-framework | MIT | AutoGen 与 Semantic Kernel 的后继，统一 Agent、工作流与部署 |

四处值得单独指出：

- **AutoGen 把"完成判定"放进对话轮次。** 参与对话的 Agent 互相回复，直到某一方认为可以结束。这比本章的"一次汇总调用"更灵活，但也更难设上限——对话轮数不可预测，所以它必须配一个明确的轮数硬闸。本章把上限做成了 `max_rounds`，并配了无进展检测和强制收敛。选型时还要注意它已进维护模式：读文档、看示例仍然可用，要开工新项目应当从 Agent Framework 起步。
- **LangGraph 的状态图把熔断做成了图上的边。** 循环可以走，但走到某个节点就可以终止。它的教训和本章一致：**循环必须有出口**。区别是 LangGraph 由开发者画图决定出口在哪，本章用统一的轮次与收敛判定来管。
- **CrewAI 的角色分工就是"能力异构"的工程化。** 每个角色有自己的工具和目标描述——这正是 `workers` 字典在代码里的含义：不同的键挂不同的工具与凭据。
- **这四个项目里没有一个把"该不该升级"做成判据。** 它们都假定你已经决定用多 Agent 了。这不是缺陷——判据是**架构决策**的工具，属于设计阶段而不是运行阶段。但也意味着这条纪律得由你自己写进代码：默认返回 `SingleAgent`，让升级成为一个显式动作。

## 六、坑与检查清单

**升级判据**

- [ ] 四个判据全部满足才升级，不满足时能说出缺哪一条
- [ ] 任务变大、上下文装不下，**不是**升级理由
- [ ] 子任务有依赖链就不并行，改在同一个 Agent 里多步调用
- [ ] 默认编排器是 `SingleAgent`，多 Agent 必须显式构造

**拓扑**

- [ ] 拓扑表记的是**代价**不是优点
- [ ] 网状拓扑必须配硬性轮数上限，否则会无限对话
- [ ] 层级拓扑层数要少，黑板拓扑的文档要有裁剪策略

**Supervisor**

- [ ] 派单 → 回收 → 判定完成三段齐全，**完成判定有且只有一处**
- [ ] 依赖在**同一批内部**也要满足，不能推给下一轮
- [ ] 并行结果按计划顺序重排，不按线程完成顺序
- [ ] 汇总失败时用子任务结论兜底，不交空答案
- [ ] 解析"还要继续"失败（空列表）时默认收敛，不是默认继续

**共享状态**

- [ ] 三种做法都实现同一套接口（`write` / `read` / `history`），换实现不换调用方
- [ ] 共享内存与共享文档能报出**冲突**，别让覆盖静默发生（消息传递不覆盖，不需要）
- [ ] 消息传递的信箱能区分"读了"和"消费了"

**熔断**

- [ ] 最大轮次、无进展检测、强制收敛、单 Agent 回退四件齐全
- [ ] 轮次计数器的语义写进注释，并有用例断言具体轮数
- [ ] 回退路径用的是**同一个单 Agent**，不是另造一套
- [ ] 每次编排调用都是独立的一次，不共享上一次的步骤历史
- [ ] 并行线程之间不共享同一个 Agent 实例
- [ ] 编排的账（轮数、token、成本、停因）能被读出来

## 七、小结与延伸阅读

这章想留下的不是"多 Agent 怎么搭"，而是三个判断：

1. **升级的依据是失败模式，不是任务规模。** 任务变大该分阶段，能力/上下文/权限/并行这四条才是该拆的信号。
2. **多 Agent 的复杂度是固定成本，收益要逐条兑现。** 所以判据要"全中"，拓扑要按代价选，熔断一个都不能省。
3. **编排层和被编排的 Agent 之间有隐式契约。** 复用有状态对象之前，先知道它内部有哪些状态机——第 6 章的无进展检测在编排层会变成误杀。

还有一条负向结论：**大多数 Agent 不需要多 Agent。** 先问"单 Agent 具体卡在哪一条"，答不上来就别拆。

延伸阅读：

- 第一卷第 20 章《多 Agent 的动机、分工理论与何时不该上多 Agent》——协作模式的全景，本章只讲升级路径与工程实现。
- 第一卷第 23 章《协作拓扑与调度：Supervisor、层级、网状、辩论与黑板》——拓扑选择的完整讨论。
- 第一卷第 29 章《检查点、回放与多 Agent 共享状态》——共享状态落到检查点与回放上的做法。
- 第二卷第 6 章《Agent 主循环：ReAct、停止准则与预算守卫》——编排层要复用它，也会被它的无进展检测误伤。
- 第二卷第 9 章《护栏与权限：能力最小化》——权限隔离这条判据的落地。
- AutoGen：https://github.com/microsoft/autogen ——以对话轮次承担完成判定。
- LangGraph：https://github.com/langchain-ai/langgraph ——用状态图把熔断做成图上的边。
- CrewAI：https://github.com/crewAIInc/crewAI ——角色化分工的工程化实现。
