# 第 6 章 · Agent 主循环：带预算守卫的 ReAct

本卷第六章把"问模型 → 调工具 → 再问"写成一个**可靠的**回路：五种停止结局、一个固定优先级、三种预算、一个接口。

- 正文：[`agent-loop.md`](agent-loop.md)
- 配套工程产物：`dragon-agent/` 下的四个文件（见下）

## 本章给了什么

1. **`dragon_agent/state.py`**——任务状态机：`TaskState`（唯一真相源）、`Checkpoint`、事件溯源、恢复。
2. **`dragon_agent/agent.py`**——ReAct 主循环、停止准则、无进展检测、预算守卫、优雅降级。
3. **`tests/test_agent.py`**——24 条确定性断言，覆盖每条停止路径。
4. **`tests/test_state.py`**——14 条状态层断言。

## 停止准则

五种结局，优先级固定：

**完成 → 异常 → 预算 → 无进展 → 步数上限**

| `status` | 什么时候 |
| --- | --- |
| `completed` | 模型给出了最终答案 |
| `error` | 模型调用失败，或任务内部出了意外 |
| `stopped_budget` | token / 时间 / 美元任一预算到顶 |
| `stopped_no_progress` | 连续几步没有进展 |
| `max_steps` | 撞上步数上限 |

同时满足多个条件时排在前面的胜出——最后一步给出答案同时撞上限，**算完成**。所以预算检查放在每轮开头（问模型之前），完成判定紧跟响应之后。

## 无进展检测

三个信号，任意一个连续 3 次成立就停：

| 信号 | 判定 |
| --- | --- |
| 动作重复 | 连续几步是同一个动作（名字 + 参数都相同） |
| 观察不变 | 连续几步拿到完全相同的观察 |
| 状态无变化 | 连续几步没有新的成功结果（比的是成功计数，不是状态版本号） |

## 预算守卫

token / 时间 / 美元共用一个接口，主循环只会问"还能花吗"和"为什么停了"：

```python
class BudgetGuard:
    kind = "budget"
    def charge(self, tokens: int, cost: float, elapsed_s: float) -> None: ...
    def exhausted(self) -> bool: ...
    def reason(self) -> str: ...
```

加第四种预算（比如"最多调工具 20 次"）只要多写一个子类，主循环一个字都不用改。

## 怎么跑

```powershell
cd dragon-agent

# 1) 端到端演示：走完整条回路，退出码 0
python examples/end_to_end_demo.py

# 2) 本章的测试：24 + 14 条
python -m unittest tests.test_agent tests.test_state
```

不需要 API Key，不联网，不装任何第三方包。

### 实跑结果

| 场景 | 结果 | 退出码 |
| --- | --- | --- |
| `python examples/end_to_end_demo.py` | 两步轨迹 + 成本账 + `completed` | 0 |
| `python -m unittest tests.test_agent` | `Ran 24 tests` / `OK` | 0 |
| `python -m unittest tests.test_state` | `Ran 14 tests` / `OK` | 0 |

## 降级与副作用

- 工具失败**不中断循环**——错误回灌给模型，让它下一轮改对；
- 主循环**绝不给工具加重试**（模型层可以退避重试，工具层不行：不可逆动作重试就是重复扣款）；
- 副作用工具带**确定的幂等键**（任务 + 步骤 + 工具 + 参数），只读工具不给（否则会掩盖重复劳动）；
- 护栏两道关方向**故意相反**：工具前护栏坏了就不执行（有副作用，关不住就别动），输出护栏坏了就放行（没有副作用，拦下来只会白白毁掉一次成功）。

## 兼容说明

本模块同时满足工程线 Bot 在 `tests/test_invariants.py` 里预先写好的不变式：`TestRunNeverRaises`、`TestStopPriority`、`TestSingleSourceOfTruth`、`TestContextDeterminism` 共 16 条全过。

护栏相关的四条不变式需要 `dragon_agent/guardrails.py`（第 9 章的模块）才能跑；本章用本地假护栏在自己的测试里覆盖了同样行为。

## 局限（这一章不等于可以上线）

- **"永不抛异常"这句在收尾处破了一次。** 兜底的 `except Exception` 在 `agent.py:342-344`，但收尾的 `self.state.update(status=status)` 写在 try 之外（`:346`）——状态层一坏，异常照常冒泡给调用方。
- **预算只在轮次之间结算。** 守卫在每轮开头问一次（`agent.py:258`），charge 在模型返回之后（`:284`）；工具调用又是无超时的阻塞调用（`tools.py:150-187`）——一个卡死的步骤可以把预算烧穿而不会被打断。
- **成本预算默认永远不触发。** `Config` 的单价默认是 0.0，`from_env_and_args()` 这条入口也不读任何价格来源（`config.py:44-45`、`:83-93`）——从它拿到的配置，总成本恒为 0，`cost_budget` 只是摆设。
- **无进展检测读的是状态层累积的步，不是本次运行的步。** 它取 `self.state.snapshot().steps`（`agent.py:471`）——复用同一个 `StateStore` 跑第二个任务时，上一任务的记录会立刻把新任务判成无进展，**一步都不走**。
- **状态、检查点、幂等登记全在内存，"崩溃后能恢复"没实现。** `checkpoint()` 只返回一个内存对象、不落盘（`state.py:128-139`），`restore()` 只吃内存对象（`:141-152`），`restore_executed_actions()` 也没有接进主循环（`:260-272`）；主循环拿到的检查点当场就丢掉了（`agent.py:381`）。
- **一轮里的多个工具调用只记第一个。** `AgentStep.action` 取 `resp.tool_calls[0]`（`agent.py:335`），其余调用的名字与参数不进状态——而"动作重复"这个信号正是拿 `action` + `arguments` 算的（`:479-485`），多调用场景下会误判。

## 与本书其它章节的关系

- 第二卷第 7 章 `p07-state-checkpoint`：事件溯源、崩溃恢复、跨进程回放。
- 第二卷第 9 章 `p09-guardrails`：护栏的生产实现（第 6 章只定义了接口与调用位置）。
- 第二卷第 10 章 `p10-orchestration`：单 Agent 到多 Agent 的升级判据。
- 第一卷第 4、6、11、29、32、35 章：ReAct、反思、停止准则、检查点与回放、提示注入、成本优化。
