# ch14 两条编排路线最小可实现

对应《开源 Agent 架构技术全书》第 14 章《AutoGen 与 OpenAI Agents SDK：对话式多 Agent 与轻量编排两条路线》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**：用 1143 行代码把两条路线各写一遍，放在同一个文件里对照。

- **路线 A（AutoGen 风格）**：会话式多 Agent。Core 层的消息与 actor，AgentChat 层的 Team（`RoundRobinGroupChat` / `SelectorGroupChat`）与终止条件。
- **路线 B（OpenAI Agents SDK 风格）**：单 Agent + 交接。`Agent` / `Handoff` / `Guardrail` / `Session` / `Tracing` 五个抽象。

> 说明：本目录不是这两个框架的移植版，是**教学用的最小复刻**。
> 它用标准库复现了两种编排形态的运行语义，目的是让你看懂机制；
> 生产项目请直接用 AutoGen 或 openai-agents。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch14-autogen-agentsdk
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

```bash
python main.py                    # 跑全部三个场景
python main.py --demo groupchat   # 只跑群聊对比（轮询 vs 自动选人）
python main.py --demo agentsdk    # 只跑 Agents SDK 路线（交接 + 护栏 + 会话 + 追踪）
python main.py --demo compare     # 只跑两条路线对比
python main.py --trace            # 打印完整追踪树
```

## 场景一：轮询群聊 vs 自动选人群聊（groupchat）

同一个任务，三种开会方式：

```
【轮询 RoundRobin（写→审→析）】
  [writer] 我需要先有分析结论才能动笔，目前没有可用素材。
  [reviewer] 没有可审的稿件，先让撰写者出稿。
  [analyst] 分析结论：上周订单量环比下降 12%...
  [writer] 报告初稿：本周订单量环比下降 12%...
  [reviewer] 结论与初稿一致，可以发布。APPROVE
  -> 轮次 5，LLM 调用 5 次

【自动选人 Selector（规则选人）】
  [analyst] 分析结论：...
  [writer] 报告初稿：...
  [reviewer] 结论与初稿一致，可以发布。APPROVE
  -> 轮次 3，LLM 调用 3 次

【自动选人 Selector（模型选人）】
  （同上三步）
  -> 轮次 3，LLM 调用 6 次
```

**这就是全部的取舍**：

| | 轮次 | LLM 调用 | 说明 |
| --- | --- | --- | --- |
| 轮询 | 5 | 5 | 不花选人的调用，但按顺序问不该问的人 |
| 规则选人 | 3 | 3 | 零额外调用，还能跳过无效轮次 |
| 模型选人 | 3 | 6 | 最灵活，但每轮多一次调用——轮次越多，账越厚 |

轮询省调用，自动选人省轮次。多花调用换少问错人，就是这两者之间的全部距离。

## 场景二：Agents SDK 路线（agentsdk）

四个小演示，对应五个抽象：

```
【1】输入护栏拦截
  被拦截：输入护栏拦截了 triage 的请求
  期间模型调用次数：0（一次都没调用）

【2】工具调用 + 交接
  最终由谁作答：billing_specialist
  交接路径    ：triage -> billing_specialist
  工具调用    ：1 次
  模型调用    ：3 次

【3】输出护栏拦截
  被拦截：输出护栏拦截了 billing_specialist 的答案

【4】会话记忆
  第 1 轮：订单当前状态：已发货，运单 SF7788，预计明天送达。
  第 2 轮（没给订单号）：订单当前状态：已发货，运单 SF7788，预计明天送达。
```

第 4 步里第二轮用户只说了"那退款呢？"，Agent 从会话历史里翻出了订单号 `A-91` 并复用——这就是 `Session` 的价值。

`--trace` 还会打印追踪树：

```
run [agent]  agent=triage
  input_guardrail [guardrail]  tripped=False
  generation [model]  agent=triage, turn=1
  tool_call [tool]  name=lookup_order, args={"order_id": "A-91"}
  generation [model]  agent=triage, turn=2
  handoff [handoff]  frm=triage, to=billing_specialist
  generation [model]  agent=billing_specialist, turn=3
  output_guardrail [guardrail]  tripped=False
```

## 场景三：两条路线对比（compare）

```
维度                       路线 A：会话式群聊                路线 B：单 Agent + 交接
模型调用次数                   2 次                          3 次
轮次 / 循环次数                2 轮                          3 次
工具调用                     无（要自己接）                   1 次
交接                       无                          triage -> billing_specialist
最后一轮送进模型的上下文          全员共享历史 约 45 token        裁剪后专家上下文 约 23 token
上下文形态                   所有人都看得见全部对话             当前 Agent 的历史，可裁剪
终止方式                    终止条件（关键词 / 上限）           没有工具调用和交接就算结束
护栏                       要自己在流程里加                输入 / 输出护栏是一等抽象
```

上下文那一行是两条路线最实在的差别：群聊每一轮都要重发全部共享历史；
交接路线可以用 `input_filter` 只给接班的人看它该看的。

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

live 模式有两处换成真实模型：

- **群聊路线**：每个 Agent 的一次发言 = 一次普通对话补全；
- **Agents SDK 路线**：模型返回 JSON，说明这一轮是"说点什么"、"调哪个工具"还是"交给谁"。

**终止条件、护栏、会话、追踪、交接机制全部不变**——这正是要验证的。

### mock 说明（重要）

内置的"模型"是一套规则，不是真实模型：

- 群聊里，每个角色按"历史里有没有我的产出标记"决定说什么；
- Agents SDK 路线里，分诊 Agent 查一次订单就交接给计费专家；
- 模型选人器按"上一位是谁"选下一位，模拟模型读上下文后的判断。

真实模型的行为必然不同（尤其是选人准不准、护栏该拦的拦不拦得住）。
要评估真模型的行为，用 `--live`。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--demo` | `all` | `all` / `groupchat` / `agentsdk` / `compare` |
| `--trace` | 关 | 打印完整追踪树 |
| `--live` | 关 | 切真实模型 API |
| `--model` | 空 | 覆盖 `LLM_MODEL` |
| `--base-url` | 空 | 覆盖 `LLM_BASE_URL` |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `Message` / `RoutedAgent` | Core 层：消息与 actor |
| `TerminationCondition` | 终止条件基类 |
| `MaxMessageTermination` | 消息数上限——最后一道保险 |
| `TextMentionTermination` | 关键词终止——当"完成信号"用 |
| `AnyTermination` | 多条件组合 |
| `RoundRobinGroupChat` | 轮询群聊：固定顺序，零额外调用 |
| `SelectorGroupChat` | 自动选人群聊：规则或模型选人 |
| `Tool` / `Agent` | Agents SDK 的两个基本件 |
| `handoff_names()` | 交接在模型眼里就是 `transfer_to_xxx` 工具 |
| `GuardrailFunctionOutput` | 护栏判定结果 |
| `InputGuardrailTripwireTriggered` | 输入护栏拦截 |
| `OutputGuardrailTripwireTriggered` | 输出护栏拦截 |
| `MaxTurnsExceeded` | 循环轮数上限 |
| `Session` | 会话记忆，四个方法 |
| `Span` / `Tracer` | 追踪树 |
| `Runner` | Agent 循环：调模型 → 执行工具 → 跟随交接 → 再调模型 |
| `Agent.input_filter` | 交接时裁剪历史，给接班的人少看点 |

### 三个值得抄走的实现细节

1. **交接就是一次工具调用**。`Runner` 里没有 `if agent == "billing"` 这种分支——模型说"交给计费专家"，循环就换个 Agent 继续。路由逻辑归模型，代码只负责"给一份花名册"和"决定何时结束"。
2. **护栏在模型之前跑**。输入护栏拦下请求时，模型调用次数是 **0**——一次 token 都没烧。这比在提示词里写"请不要做 X"可靠得多。
3. **群聊的上下文是全量共享，交接路线的上下文可以裁剪**。`input_filter` 就是那个剪刀。

## 换成自己的场景时要改什么

1. 改 `build_chat_agents()` / `build_service_desk_agents()`：换角色和人设；
2. 改 `rule_selector()` / `llm_selector()`：换选人逻辑；
3. 改 `build_agentsdk_agents()`：换工具、护栏规则和交接关系；
4. 改 `MockModel.personas`：换每个 Agent 的行为策略；
5. 真接模型时，把 `LiveModel` 换成自己的实现，**`Runner` 和群聊执行器一行都不用改**——它们只认"模型返回什么"，不认模型是谁。

## 相关章节

- 第 11 章：停止准则、步数上限与成本守卫（终止条件怎么设）
- 第 12 章：LangChain 与 LangGraph（第三种编排：显式状态图）
- 第 15 章：CrewAI（角色—任务—流程的心智模型）
- 第 19 章：框架选型决策树与常见反模式（多 Agent 到底要不要上）
