# 第 14 章 AutoGen 与 OpenAI Agents SDK：对话式多 Agent 与轻量编排两条路线

## 一、本章要解决的问题

一次对外说明会的复盘。

任务很简单：上周订单量掉了 12%，要出一份能对外发的说明。分析师先看数，撰写者写稿，审核员把关。这活儿按理说就是"三个人依次过一遍"，但团队里为"该怎么组织这三个人"吵了两周。

第一版用 AutoGen，把三个角色丢进一个群聊，用关键词 `APPROVE` 收尾。能跑通，但维护它的人半年后自己都看不懂了——群聊里每个角色每轮都要重发全部对话历史，跑一轮烧掉的 token 比三个 Agent 顺序调用加起来还多。

第二版换成 OpenAI Agents SDK，写了三个 Agent，用交接（handoff）串起来。轻多了，token 也省了。但写到一半发现不对：有一个环节需要撰写者拿着分析师的结论问审核员"这个口径行不行"，审核员再问分析师"这个数对不对"——来回三趟。handoff 是一对一的传话，它做不了"开会"。

最后两条路线都用了一部分：能用交接的地方用交接，需要来回协商的地方留一个群聊。

这个组合后来成了默认做法：客服分诊走交接，审核口径拿不准就临时开一个三人小群聊，群里只放结论和待确认的那几条，不用把整个会话搬进去。

这件事背后是两个框架最根本的分歧，也是本章要拆的：**AutoGen 认为编排就是"大家开会"，Agents SDK 认为编排就是"一个人带着工具和下属"。** 分歧不在 API 长相，在对"多 Agent"这个词的默认理解。

## 二、原理：机制拆解

### 2.1 AutoGen 的两层：Core 与 AgentChat

AutoGen 0.4 是一次不兼容的重写，拆成了两个包，这个分层是理解它的钥匙。

**Core 层（`autogen_core`）** 是 actor 模型：`AgentRuntime` 管 Agent 的生命周期和消息投递，消息是类型化的，Agent 通过订阅（subscription）收消息，`RoutedAgent` 按消息类型路由。跑在单进程还是跨进程、跨语言，Core 层不关心。

**AgentChat 层（`autogen_agentchat`）** 是高阶 API：`AssistantAgent`（系统提示 + 模型客户端 + 工具）、Team（团队）、`TerminationCondition`（终止条件），以及预置好的几种团队。

关系很清楚：AgentChat 建在 Core 之上。日常用 AgentChat——它把"团队怎么开、什么时候停"这些高频模式封装好了；只有需要自定义运行时行为（自己投递消息、跨进程部署）时，才下沉到 Core。

分层的价值在于**留了后路**：先用 AgentChat 跑通，真遇到分布式问题还能往下钻一层，而不是推倒重来。

### 2.2 群聊：轮次控制与自动选择

群聊由三个零件组成：一份**共享历史**、一个**发言人选择器**、一组**终止条件**。每个 Agent 发言后，消息广播给所有人，信息完全对称——这是群聊最大的优点。

两种预置团队，差别只在"下一个谁发言"：

**RoundRobinGroupChat（轮询）**：固定顺序轮流，零额外调用。

**SelectorGroupChat（自动选人）**：每轮把历史交给一个选择器——可以是一个模型，也可以是你自己写的 `selector_func`（返回下一个发言人的名字，返回 `None` 就交回默认策略）。

配套代码里同一个任务，三种开法的实测数字：

| 开法 | 轮次 | LLM 调用 | 说明 |
| --- | --- | --- | --- |
| 轮询 | 5 | 5 | 其中 2 轮是浪费的：撰写者先要素材、审核员没稿可审 |
| 规则选人 | 3 | 3 | 零额外调用，还能跳过无效轮次 |
| 模型选人 | 3 | 6 | 最灵活，但每轮多一次调用 |

一句话记住这个取舍：**轮询省调用，自动选人省轮次。** 多花调用换少问错人，就是两者之间的全部距离。

终止条件是团队的刹车，AutoGen 给了好几种：`MaxMessageTermination`（条数上限）、`TextMentionTermination`（出现关键词就停，常当"完成信号"用）、以及它们的组合。实践中要**两个都配**——只靠关键词容易误触发和漏触发，只靠上限则可能跑出垃圾结论。

这里有个必须记住的默认值：`RoundRobinGroupChat` 和 `SelectorGroupChat` 的 `max_turns` 默认是 `None`，也就是**不设终止条件就会一直跑下去**（官方文档的原话是 "the group chat will run indefinitely"）。只有 `MagenticOneGroupChat` 的 `max_turns` 默认是 20。所以生产上必须显式声明自己的上限，别指望框架替你踩刹车。

还有一个容易被忽略的成本：群聊每一轮都要把**全部共享历史**重发给当前发言的 Agent。人越多、轮次越长，这块越贵。

所以群聊的规模经验值是三到八个人。再往上，要么分组嵌套，要么把"播报"和"讨论"分开——播报用交接，讨论用群聊。

### 2.3 Agents SDK 的五个抽象

**Agent**：名字 + 指令（系统提示）+ 工具 + 交接对象 + 护栏，可选结构化输出类型。就这些。

**Handoff（交接）**：这是最值得学的设计。**交接本质上是一种特殊的工具调用**——你在 `handoffs` 里列几个 Agent，SDK 就在模型的工具列表里生成对应的 `transfer_to_xxx` 工具，和你的函数工具并列。模型判断"这问题该找账单"时，它只是调了那个工具。

结果是：**路由逻辑交给了模型，你的代码里没有 if/else。** 代码只负责两件事——给一份清晰的 Agent 花名册，决定循环何时结束。代价是花名册的描述必须写清楚，否则模型选错人；`input_filter` 可以在交接时裁剪历史，让接班的人少看无关内容。

**Guardrail（护栏）**：输入护栏在调用大模型**之前**跑，输出护栏检查最终答案。判定结果是 `GuardrailFunctionOutput`，其中 `tripwire_triggered` 为真就中止整次运行——是抛异常，而不是返回一个你可能忘记检查的值。常见做法是在护栏里放一个小而便宜的分类模型：拦下了，贵模型一个 token 都没烧。

**Session（会话）**：`SQLiteSession` 这类会话存储，跨轮次自动带上历史，不用自己维护消息列表。

**追踪（Tracing）**：默认开启。每次运行、每次模型生成、每个工具调用、每次交接、每次护栏检查，都记成一个 span（跨度）。

而 Runner 的循环本身很短：

```
循环：
  调模型
  ├─ 有工具调用 -> 执行 -> 结果塞回历史 -> 继续
  ├─ 有交接     -> 换一个 Agent -> 继续
  └─ 都没有     -> 这就是最终输出
```

### 2.4 两条路线的隐含假设

| | A：会话式群聊 | B：单 Agent + 交接 |
| --- | --- | --- |
| 控制流在哪 | 发言权怎么分配 | 当前该谁上场 |
| 上下文形态 | 全员共享同一份历史 | 当前 Agent 的历史，可裁剪 |
| 终止方式 | 终止条件（关键词 / 上限） | 没有工具调用和交接就算结束 |
| 护栏 | 要自己在流程里加 | 输入 / 输出护栏是一等抽象 |
| 工具 | 要自己接 | 一等公民 |

配套代码里跑同一个客服任务：A 用了 2 次调用，但最后一轮送进模型的是 45 token 的**全量共享历史**；B 用了 3 次调用，但交接后专家只看到 `input_filter` 剪过的 23 token。调用次数少不代表便宜，每轮送进去多少东西才是账单的真正来源。

## 三、为什么这样设计：取舍与失效条件

先说各自的代价。

**AutoGen 的代价**：0.2 到 0.4 是**不兼容的重写**，老教程里的 `ConversableAgent`、`initiate_chat` 全部失效；新版本异步优先，所有 `run()` 都是协程；群聊每轮重发全量历史；参与者超过十几个，选人开销和上下文长度一起膨胀。

**Agents SDK 的代价**：它本质是**一个循环**，不是编排引擎——没有 DAG、没有并行分支、没有检查点和时间旅行；需要真正的多 Agent 对话时，每个 Agent 是一次独立的运行，共享上下文要自己拼；追踪默认发往 OpenAI 平台，Zero Data Retention 策略下用不了；评测循环、预算上限、审批策略这些都不在 SDK 里，得在外围补。

**迁移成本**是选型时最该算的一笔：

- **AutoGen → Agents SDK**：群聊要重写成"一个 Agent + 交接"，共享历史没了，信息对称这个优势也一起没了。能留下的：工具函数、提示词、护栏规则思路。
- **Agents SDK → AutoGen**：单 Agent 循环要塞进 Team 语义，"什么时候结束"得自己重新实现。能留下的：工具定义、交接关系、结构化输出模型。
- **两边都能留下的**：工具函数、结构化输出模型、护栏规则、评测集。
- **两边都没有的**：检查点、审批暂停、回滚重放。要这些，去第 12 章的 LangGraph。

什么时候**不该**上多 Agent：

1. **任务不需要协商**——一个 Agent 加几个工具就够，硬拆成三个只会让上下文翻倍；
2. **参与者超过 10 个**——群聊会从"协作"退化成"互相干扰"；
3. **流程是固定的**——那是状态图（第 12 章）或角色流水线（第 15 章）的事；
4. **对每轮延迟敏感**——群聊是串行的，轮次直接叠加到总耗时上；
5. **只是想显得高级**——为了"看起来像多 Agent"而拆三个角色，每个角色都要付一遍上下文的钱，而它们说的可能是同一件事。

## 四、动手：最小可实现

配套代码在 `code/ch14-autogen-agentsdk/`，`main.py` 用 1143 行纯标准库把两条路线各写一遍放在同一个文件里对照，**不需要 API Key 就能跑通完整回路**，内置 mock 模型。

依赖：Python 3.9 及以上，零第三方包，不需要 `pip install`。

```bash
cd code/ch14-autogen-agentsdk
python main.py                    # 跑全部三个场景
python main.py --demo groupchat   # 只跑群聊对比
python main.py --demo agentsdk    # 只跑交接 + 护栏 + 会话 + 追踪
python main.py --demo compare     # 只跑两条路线对比
python main.py --trace            # 打印追踪树
```

三个场景：

- **groupchat**：同一个任务，轮询 / 规则选人 / 模型选人各跑一遍，打印轮次与调用次数。
- **agentsdk**：四段——输入护栏拦截（模型调用 0 次）、工具调用加交接、输出护栏拦截、会话记忆（第二轮不给订单号，Agent 从历史里翻出来复用）。
- **compare**：两条路线跑同一个客服任务，逐项对比调用次数、上下文大小、终止方式、护栏。

几个关键机制在代码里长这样：

```python
# 1) 交接就是一次工具调用——循环里没有 if agent == "xxx"
if resp.handoff_to:
    current = target
    continue

# 2) 护栏在模型之前跑，拦下就是 0 次调用
for gr in agent.input_guardrails:
    out = gr(user_input)
    if out.tripwire_triggered:
        raise InputGuardrailTripwireTriggered(...)

# 3) 交接时裁剪历史，给接班的人少看点
if current.input_filter is not None:
    history = current.input_filter(history)

# 4) 终止条件是团队的刹车
term = AnyTermination(TextMentionTermination("APPROVE"), MaxMessageTermination(10))
```

切真实模型加 `--live`，需要三个环境变量：

| 变量 | 必填 | 默认值 |
| --- | --- | --- |
| `LLM_API_KEY` | 是 | — |
| `LLM_BASE_URL` | 否 | `https://api.openai.com/v1` |
| `LLM_MODEL` | 否 | `gpt-4o-mini` |

live 模式下只有"发言"和"这一轮做什么"两处换成真模型，**终止条件、护栏、会话、追踪、交接机制全部不变**——这正是要验证的：换模型之后，编排的行为是不是还是可预期的。

## 五、开源实现对照

- **AutoGen**（MIT（LICENSE-CODE）；根目录 LICENSE 为 CC-BY-4.0，https://github.com/microsoft/autogen）：本章路线 A 的参考实现。以可对话 Agent 与 GroupChat 支撑多 Agent 协作与代码执行；代码执行沙箱是它的独门功夫。
- **OpenAI Agents SDK**（MIT，https://github.com/openai/openai-agents-python）：本章路线 B 的参考实现。用 Agent、工具、交接、护栏与追踪拼装轻量多 Agent 工作流，抽象面小到能一个下午读完。
- **Microsoft Agent Framework**（MIT，https://github.com/microsoft/agent-framework）：微软把 AutoGen 和 Semantic Kernel 收到一起的产物，目标是统一 Agent、工作流与部署。选型时如果已经在微软技术栈里，它比在两个前身之间做选择更省事。
- **smolagents**（Apache-2.0，https://github.com/huggingface/smolagents）：第三种极简路线——让模型用 Python 代码块代替 JSON 工具调用。多 Agent 讨论不是它的主语，可以对照着看"交接"和"调工具"的另一种表达。
- **LangChain**（MIT，https://github.com/langchain-ai/langchain）与 **LangGraph**（MIT，https://github.com/langchain-ai/langgraph）：第三条线，第 12 章详述。前两者解决"多角色怎么配合"，LangGraph 解决"流程怎么走、怎么回滚"。

## 六、坑与检查清单

- [ ] 终止条件至少配两个：关键词 + 条数上限，别只靠一个。
- [ ] 群聊每轮重发全量历史，参与者超过 10 个要重新评估。
- [ ] 选人器优先用规则，能不调模型就不调——省下的是每轮一次调用。
- [ ] 交接对象的名字和描述写清楚，这是模型选人的唯一依据。
- [ ] 交接时用 `input_filter` 裁历史，别把全对话一股脑塞给接班的人。
- [ ] 输入护栏用小而便宜的模型，拦在贵模型之前。
- [ ] 护栏要抛异常中止运行，不要返回一个"你可能忘了检查"的值。
- [ ] 输出护栏查业务禁语和过度承诺，比如"保证""一定"。
- [ ] 每次运行都开追踪，模型调用、工具、交接、护栏都要有 span。
- [ ] 追踪默认发往 OpenAI 平台，有 ZDR 要求就自己接导出。
- [ ] `max_turns` / 条数上限必须设，防止模型和另一个 Agent 互相客套到天亮。
- [ ] 预算上限要在循环**里面**检查，事后看账单已经晚了。
- [ ] 群聊里的每个 Agent 拿到的都是全部历史，提示词里别写"只需要看第三条"。
- [ ] 迁移时先盘清楚哪些是业务逻辑（能留），哪些是编排机制（要重写）。

## 七、小结与延伸阅读

两条路线，差别不在 API 长相，在默认假设。AutoGen 假设编排是**开会**：共享历史、信息对称、控制流在发言权分配，代价是每轮全量重发和轮次膨胀。Agents SDK 假设编排是**带着工具和下属干活**：交接是一种工具调用、护栏跑在模型之前、会话自动带历史，代价是它只有一条循环，没有图、没有检查点。

选型就三问：**任务需不需要来回协商？需不需要工具？需不需要回滚和审批？** 第一个要，选 AutoGen；第二个要，两边都行，Agents SDK 更省事；第三个要，去第 12 章。三个都不要，就别上多 Agent——一个 Agent 加几个工具，是最划算的答案。

延伸阅读：

- AutoGen — https://github.com/microsoft/autogen
- OpenAI Agents SDK — https://github.com/openai/openai-agents-python
- Microsoft Agent Framework — https://github.com/microsoft/agent-framework
- smolagents — https://github.com/huggingface/smolagents
- LangChain — https://github.com/langchain-ai/langchain
- LangGraph — https://github.com/langchain-ai/langgraph
