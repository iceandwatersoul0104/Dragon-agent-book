# ch15 CrewAI 角色—任务—流程心智模型最小可实现

对应《开源 Agent 架构技术全书》第 15 章《CrewAI：角色—任务—流程心智模型》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**：用 829 行代码复刻 CrewAI 的四个基本件
（`Agent` / `Task` / `Process` / `Crew`）以及 `Flows` 的事件驱动控制层。

> 说明：本目录不是 CrewAI 的移植版，是一个**教学用的最小复刻**。
> 它用标准库复现了角色提示词拼装、上下文传递、manager 派活儿与验收、
> Flow 的 `@start` / `@listen` / `@router` 机制，目的是让你看懂机制；
> 生产项目请直接用 CrewAI。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch15-crewai
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

```bash
python main.py                     # 跑全部四个场景
python main.py --demo crew         # 只跑顺序 Crew
python main.py --demo expected     # 只跑 expected_output 的"软约束"演示
python main.py --demo hierarchical # 只跑顺序 vs 层级
python main.py --demo flow         # 只跑 Flow
python main.py --trace             # 打印每个角色收到的完整提示词
```

## 场景一：顺序 Crew（crew）

两个任务：调研 → 写简报。第二个任务用 `context=[第一个任务]` 接收前者的产出。

```
任务 1：调研 开源 Agent 编排框架 最近的进展
  期望产出：5 条要点，每条带出处
  执行者  ：研究员
  实际产出：
    调研结论（5 条）：
    1. 多 Agent 编排正在从'写死流程'转向'模型选路'；
    ...

任务 2：根据调研结果写一份 开源 Agent 编排框架 简报
  期望产出：一段 300 字以内的简报
  执行者  ：撰稿人
  上下文来源：任务 1（通过 context=[...] 传入）

模型调用次数：2（每个任务一次）
```

`--trace` 会打印两个角色各自收到的完整提示词，能看到上下文是怎么被拼进去的：

```
你是撰稿人。
目标：把研究结论写成一段读得懂、读得完的简报
背景：做过三年技术编辑，讨厌堆术语。
参考上下文（来自前置任务的产出）：
【根据调研结果写一份 开源 Agent 编排框架 简报 的产出】
调研结论（5 条）：
...
任务描述：根据调研结果写一份 开源 Agent 编排框架 简报
期望产出：一段 300 字以内的简报
```

## 场景二：expected_output 只是提示，不是契约（expected）

研究员故意只交 2 条（期望 5 条）：

```
期望产出：5 条要点，每条带出处
实际产出：
  调研结论（2 条）：
  1. 编排框架在分工；
  2. 评测仍是缺口。

Crew 的判断：任务成功产出，无报错（结果里 2 个任务都有输出）
自己加一道校验：条数 2 < 期望 5，不通过
```

**框架不拿 `expected_output` 去验收产出。** 配套代码里的 `check_expected_output()`
演示了正确的补救姿势：只数条数，不判断内容对不对——因为语义判断本来就断言不了。

## 场景三：顺序 vs 层级（hierarchical）

```
维度                 顺序 sequential    层级 hierarchical
--------------------------------------------------------
任务数                2                2
模型调用总数             2                6
其中 manager 调用      0                4
平均每个任务             1.0 次            3.0 次
--------------------------------------------------------

层级流程里 manager 的两次派活结果：
  任务 1 -> 研究员
  任务 2 -> 撰稿人
```

层级流程每个任务三步：**分配 → 执行 → 验收**，比顺序流程多两次模型调用。
派活儿不再写在代码里，代价是成本三倍，而且 manager 派错人没人拦。

## 场景四：Flow 包 Crew（flow）

```
执行轨迹：
  prepare        -> prepared
  do_research    -> researched
  gate           -> enough
  do_write       -> written

闸门判定：条数 5 >= 期望 5，通过
```

`@router` 方法的返回值是一个标签，标签本身是触发器，`@listen("标签")` 据此分支。
校验逻辑写在普通 Python 函数里，可断言、可单测；Crew 被当成一个黑盒步骤调用。

## 真实模型（`--live`）

需要环境变量：

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_API_KEY` | 是 | — | 模型服务商的 API Key |
| `LLM_BASE_URL` | 否 | `https://api.openai.com/v1` | OpenAI 兼容的 chat completions 端点 |
| `LLM_MODEL` | 否 | `gpt-4o-mini` | 模型名 |

```bash
export LLM_API_KEY=sk-xxxxxxxx
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=gpt-4o-mini
python main.py --live
```

Windows PowerShell：

```powershell
$env:LLM_API_KEY="sk-xxxxxxxx"
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="gpt-4o-mini"
python main.py --live
```

端点需兼容 `POST {base}/chat/completions`：请求体含 `model` 与 `messages`，返回体取
`choices[0].message.content`。换供应商只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

live 模式下有三处换成真实模型：

| 方法 | 真实模型要做的事 |
| --- | --- |
| `complete()` | 按角色提示词完成任务 |
| `assign()` | manager 挑人，回答角色名 |
| `review()` | manager 验收，回答 PASS / FAIL |

**角色三件套、上下文拼装、流程驱动、Flow 的分支全部不变**。

### mock 说明（重要）

内置的"模型"是按角色人设返回固定文本的规则，不是真实模型。
它不代表任何真实模型的表现——真实模型写出来的要点条数、措辞每次都可能不同。
要评估真模型的行为，用 `--live`。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--demo` | `all` | `all` / `crew` / `expected` / `hierarchical` / `flow` |
| `--trace` | 关 | 打印每个角色收到的完整提示词 |
| `--live` | 关 | 切真实模型 API |
| `--model` | 空 | 覆盖 `LLM_MODEL` |
| `--base-url` | 空 | 覆盖 `LLM_BASE_URL` |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `Agent` | 角色 + 目标 + 背景；`build_prompt()` 拼提示词 |
| `Task` | 描述 + 期望产出 + 执行者 + 上下文来源 |
| `Process` | `SEQUENTIAL` / `HIERARCHICAL` |
| `Crew` | 组装：agents + tasks + process |
| `Crew._kickoff_sequential()` | 顺序流程，每个任务一次调用 |
| `Crew._kickoff_hierarchical()` | 层级流程：分配 → 执行 → 验收 |
| `Crew._context_text()` | 把 `context` 里那些任务的产出拼起来 |
| `TaskOutput` / `CrewOutput` | 产出结构 |
| `check_expected_output()` | 自己补的那道校验：只数条数 |
| `Flow` | `@start` / `@listen` / `@router`，标签即触发器 |
| `MockLLM` / `LiveLLM` | 模型层：mock 走规则，live 走 API |

### 三个值得抄走的实现细节

1. **提示词拼装是全部**。角色三件套 + 工具 + 上下文 + 任务描述 + 期望产出，合成一条提示词。没有别的隐藏配置——这既是优点（可读）也是限制（只有这几个旋钮）。
2. **上下文传的是完整产出**。`context` 里放的是任务对象，框架在跑下一个任务前把前者的实际产出拼进去。不做摘要，所以链长了 token 线性涨。
3. **manager 派活儿要看任务描述，不是整条提示词**。提示词里带着成员名单，"研究员"本身就含"研究"两个字，匹配整条提示词会误判。

## 换成自己的场景时要改什么

1. 改 `Agent` 的 `role` / `goal` / `backstory`——这三行就是提示词主体；
2. 改 `Task` 的 `description`（带 `{占位符}`）和 `expected_output`（写成可数的形状）；
3. 改 `MockPersonas` 换各角色的产出策略；
4. 改 `check_expected_output()` 换你的校验规则——**只写形状，不写语义**；
5. 改 `build_demo_flow()` 换你的 Flow 分支，Crew 部分不用动。

## 相关章节

- 第 11 章：停止准则、步数上限与成本守卫（层级流程的三倍成本怎么控）
- 第 12 章：LangChain 与 LangGraph（需要检查点、回滚、审批暂停时的选择）
- 第 14 章：AutoGen 与 OpenAI Agents SDK（会话式编排与角色式编排的对照）
- 第 19 章：框架选型决策树与常见反模式（"一个人的 Crew"这种反模式）
