# TODO：Agent 名称 TODO

> 规格版本：TODO ｜ 状态：草稿 ｜ 负责人：TODO ｜ 最后更新：TODO
>
> 本文件是**需求规格模板**，不是需求本身。逐段替换所有 `TODO` 后，另存为
> `specs/<agent-name>.spec.md`，然后跑一次检查器：
>
> ```powershell
> python specs/check_spec.py specs/<agent-name>.spec.md
> ```
>
> 检查器会告诉你还差什么（缺章节、判据没有验证方式、还有占位符）。

## 任务定义

> 一段话说清楚：这个 Agent 为谁、解决什么问题、在什么触发条件下运行。
> 写不出这一段，说明需求还没想清楚，先别写代码。

- 服务对象：TODO
- 要解决的问题：TODO
- 触发方式：TODO
- 完成的定义（业务层面）：TODO

## 输入输出契约

> 输入要有类型和必填项；输出要给出结构化样例。**"返回一段文本"不算契约。**

输入：

```json
{
  "TODO": "TODO"
}
```

输出：

```json
{
  "TODO": "TODO"
}
```

字段约束：

- TODO：字段的取值范围、必填与否、缺省行为
- TODO

## 可用工具清单

> 只列这个 Agent 真正会调用的工具。工具描述就是给模型看的提示词（工具描述即提示词）。
> 副作用一栏用统一的三档：只读 / 可逆写 / 不可逆写。

| 工具名 | 作用 | 关键参数 | 副作用 | 失败时行为 |
| --- | --- | --- | --- | --- |
| TODO | TODO | TODO | 只读 | TODO |
| TODO | TODO | TODO | 可逆写 | TODO |
| TODO | TODO | TODO | 不可逆写 | TODO |

## 决策边界

> 这一节决定 Agent 的可靠性上限。**判断"下一步做什么"可以交给 LLM，**
> **"能不能做""做得对不对"必须留给确定性代码。**
>
> 两类都必须有。全交给 LLM 是最常见的错误。

| 步骤 | 决策方 | 说明 |
| --- | --- | --- |
| TODO | LLM | TODO |
| TODO | 代码 | TODO |

## 成功判据

> 每条判据必须**可观测、可复现、能写成断言**。
> "审查得准确"不是判据，"每条高危发现都带 `文件路径:行号` 且行号落在实际改动行内"才是。
> 每条都要写清**怎么验证**，后面第 13 章会把它们接成回归测试。

- **C1**：TODO
  - 验证方式：TODO
- **C2**：TODO
  - 验证方式：TODO
- **C3**：TODO
  - 验证方式：TODO

## 失败定义与退出状态

> 失败也是契约。退出状态取值必须与 `dragon_agent/agent.py` 的 `AgentResult.status` 一致，
> 少一个就等于没定义失败。

| 退出状态 | 触发条件 | 调用方动作 |
| --- | --- | --- |
| `completed` | TODO | TODO |
| `stopped_budget` | TODO | TODO |
| `stopped_no_progress` | TODO | TODO |
| `max_steps` | TODO | TODO |
| `error` | TODO | TODO |

## 明确不做的事

> 这一节是整份规格里最省钱的部分。写清楚不做什么，后面所有关于"要不要加功能"的争论都有依据。
> 注意：**不可逆写的工具，要么在这里被点名排除，要么在"决策边界"里由代码把持。**

- TODO
- TODO

## 评测与观测

> 判据要能变成指标，指标要能在 trace 里看到。

- 任务集：TODO（至少覆盖多少条、难度怎么分层）
- 指标：TODO（任务成功率 / 工具错误率 / 无进展率 / 每次成功成本）
- 判定方式：TODO（优先用确定性断言，不要用 LLM 判定）
- 基线：TODO（跟什么比，比多少算有意义）

## 预算与约束

| 约束 | 取值 | 来源 |
| --- | --- | --- |
| 步数上限 `max_steps` | TODO | `Config.max_steps` |
| token 预算 `token_budget` | TODO | `Config.token_budget` |
| 成本上限 `cost_budget` | TODO | `Config.cost_budget` |
| 超时 `timeout_s` | TODO | `Config.timeout_s` |
