# 第 10 章 · 编排层：从单 Agent 到多 Agent 的升级路径

本卷第十章把"要不要拆成多 Agent"从一个感觉问题变成一个**判据问题**：先说清单 Agent 具体卡在哪一条，满足了才拆；拆的时候按代价选拓扑，并且一定要留退路。

- 正文：[`orchestration.md`](orchestration.md)
- 配套工程产物：`dragon-agent/` 下的一个文件（见下）

## 本章给了什么

1. **`dragon_agent/orchestration.py`（1046 行）**——契约第 9 节约定的编排层：
   - **升级判据**：`UpgradeCriteria`（并行性 / 上下文隔离 / 能力异构 / 权限隔离），四条**缺一不可**，判定是纯函数；
   - **拓扑选择**：`TOPOLOGIES` 五种拓扑的**代价表** + `recommend_topology()`，判据不满足就返回 `single`；
   - **Supervisor**：派单 → 回收 → 判定完成三段式，含依赖拓扑排序与并行分派；
   - **共享状态**：`SharedMemoryState` / `SharedDocumentState` / `MessagePassingState` 三种做法，同一套接口、不同的冲突处理；
   - **熔断四件套**：最大轮次、无进展检测、强制收敛、单 Agent 回退；
   - **默认是 `SingleAgent`**：`build_orchestrator()` 永远返回它，多 Agent 必须显式构造。
2. **`examples/orchestration_demo.py`**——实跑演示：八段场景，含升级判据、拓扑代价、Supervisor 三段式、共享状态对比、熔断四件套。
3. **`tests/test_orchestration.py`**——离线单元测试：37 条断言。

第 6 章的 `agent.py`、第 7 章的 `state.py`、第 4 章的 `tools.py` **一行都没改**——编排层只读它们、复用它们。

## 三个核心结论

### 1. 升级的依据是失败模式，不是任务规模

任务变大该分阶段装，不该分 Agent。多 Agent 真正解决的是四种具体失败：串行等待、上下文互相污染、能力异构、权限隔离。**一条都不占就别拆。**

```python
    def should_upgrade(self, required: Sequence[str] = UPGRADE_CRITERIA) -> bool:
        """该不该升级？

        默认要求**全部满足**——这是本章最反直觉、也最重要的一条：
        多 Agent 的复杂度是固定成本，收益是逐条兑现的，
        少一条就说明这笔账不划算。
        """
        return all(getattr(self, name) for name in required)
```

### 2. 多 Agent 永远要有一条退路

规划失败不是异常情况，是常态——模型输出的格式漂移是结构性的。所以回退路径用**同一个单 Agent**，不是另造一套。

```text
  (c) 单 Agent 回退：规划失败就退回单 Agent
      停因：规划失败：没能从编排者的输出里解析出子任务：'我觉得应该先查资料再算账'
      答案：单 Agent 的答案
      是否回退：True
```

### 3. 编排层和被编排的 Agent 之间有隐式契约

复用同一个有状态 Agent 之前，先知道它内部有哪些状态机。第 6 章的"无进展检测"在编排层会变成误杀：编排者被反复调用，第 4 次就会看到"连续三次同样的动作、同样空的观察"，判定没进展直接停下。修法是让每一次编排调用都独立。

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

## 怎么跑

```powershell
cd dragon-agent

# 1) 演示：离线跑通，退出码 0
python examples/orchestration_demo.py

# 2) 本章的测试：37 条断言
python -m unittest tests.test_orchestration

# 3) 全量测试
python -m unittest discover -s tests
```

不需要 API Key，不联网，不装任何第三方包。中文输出在 GBK 控制台上可能显示为乱码，加 `python -X utf8` 即可。

### 实跑结果

| 场景 | 结果 | 退出码 |
| --- | --- | --- |
| `python examples/orchestration_demo.py` | 八段场景全部自检通过 | 0 |
| `python -m unittest tests.test_orchestration` | `Ran 37 tests` / `OK` | 0 |
| `python -m unittest discover -s tests` | `Ran 391 tests` / `OK (skipped=6)` | 0 |

演示里的关键输出：

- 判据只满足两条 → `不满足判据（capability_heterogeneity、permission_isolation），留在单 Agent`；
- 四条全中 → 推荐拓扑 `supervisor`；只满足两条 → 推荐拓扑 `single`；
- Supervisor 三段式跑通：`轮数：1  用了 18 token`，两个子任务结论都被回收；
- 依赖生效：`算账` 的提示词里带着 `查资料` 的结论；
- 并行执行后结果顺序仍按计划：`['查资料', '算账']`；
- 共享状态三种做法的冲突差别一目了然（静默覆盖 / 留痕 / 不可覆盖）；
- 熔断四件套逐条触发：强制收敛、无进展检测、单 Agent 回退，以及**复用 Agent 不被自己的无进展检测误杀**。

## 局限（这一章不等于可以上线）

- **判据是设计阶段的工具，不是运行时的守卫。** 它不会自动拦住你写一个多 Agent 系统——它只是让"该不该拆"这个问题有答案。
- **规划依赖解析模型的 JSON 输出。** 格式漂移就会整次回退，代价是一轮模型调用加一次单 Agent 重跑。
- **共享状态在进程内。** 多 worker 部署要换成外部存储，本章的三个实现都不能直接跨进程。
- **并行用线程，且不共享 Agent 实例。** 线程安全靠"别共享"这条纪律保证，没有锁；真要共享，得先给状态层加并发保护。
- **没有跨进程的任务接管**：一个 worker 崩了，编排层不会把它那一支重新派出去。
- **熔断计数器的语义必须写进注释**：本章实现时就踩过一次 off-by-one（轮数在末尾自增，导致"跑了两轮"报成 3），熔断会因此提前一轮触发，最后一轮结论丢失。测试里直接断言了具体轮数。

## 与本书其它章节的关系

- 第二卷第 4 章 `p04-tool-layer`：worker 的工具集与凭据就是"能力异构"这条判据的落点。
- 第二卷第 6 章 `p06-agent-loop`：编排层复用主循环，也会被它的无进展检测误伤。
- 第二卷第 7 章 `p07-state-checkpoint`：`_ask()` 用的就是第 7 章的检查点与恢复。
- 第二卷第 9 章 `p09-guardrails`：权限隔离那条判据的落地。
- 第二卷第 12 章 `p12-observability`：编排的账（轮数、token、成本、停因）怎么落进 trace。
- 第一卷第 20 章《多 Agent 的动机、分工理论与何时不该上多 Agent》、第 23 章《协作拓扑与调度：Supervisor、层级、网状、辩论与黑板》、第 29 章《检查点、回放与多 Agent 共享状态》。
