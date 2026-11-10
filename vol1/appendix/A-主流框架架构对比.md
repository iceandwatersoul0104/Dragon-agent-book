# 附录 A 主流框架架构对比

这一章把第 12 到 18 章拆过的框架放在一张表上横向比一遍。表里的每个仓库 URL 和许可证都逐一核对过；核对不上的直接不写。

**先说清楚这张表怎么用。** 它比的是**抽象层次**，不是"谁更强"。同样是 Agent 框架，有的把控制流交给一张状态图，有的把控制流交给一次对话，有的干脆不管控制流、只管把类型写清楚。这三件事没有优劣，只有适不适合你手上那个问题——第 19 章的决策树和十个反模式讲的就是怎么落到具体问题上。

## 一、主对比表

| 框架 | 仓库 URL | 许可证 | 核心抽象（一句话） | 架构模式 | 状态管理方式 | 人在环支持 | 可观测性接入方式 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| LangChain | https://github.com/langchain-ai/langchain | MIT | `Runnable` / LCEL，把模型、提示、工具、检索拼成一条链 | 链式组合式 | 无状态，链内隐式传值 | 无内建 | 需接 LangSmith 或自建回调 |
| LangGraph | https://github.com/langchain-ai/langgraph | MIT | `StateGraph`：节点、边、状态 schema 与通道归约 | 状态图式 | 显式状态 + 归约，检查点持久化 | `interrupt()` 暂停与恢复 | 需接 LangSmith |
| LangGraph.js | https://github.com/langchain-ai/langgraphjs | MIT | LangGraph 的 TypeScript 实现，概念与 Python 版一致 | 状态图式 | 同上 | 同上 | 需接 LangSmith |
| LlamaIndex | https://github.com/run-llama/llama_index | MIT | 索引 + 查询引擎 + 事件驱动的 Workflows | 数据优先 + 事件驱动 | 事件流 + 工作流状态 | Workflows 步骤可挂起等待 | 内建 tracing，可导出 |
| Haystack | https://github.com/deepset-ai/haystack | Apache-2.0 | 组件（Retriever / Generator / Agent）加显式管道 | 组件化管道 | 管道内变量显式传递 | 管道中可插入人工步骤 | 自带追踪与回调 |
| AutoGen | https://github.com/microsoft/autogen | MIT（LICENSE-CODE）；根目录 LICENSE 为 CC-BY-4.0 | 可对话 Agent 加 GroupChat 共享一段历史 | 对话式多 Agent | 共享消息历史，可序列化 | `UserProxyAgent` + 终止条件 | 内建 tracing |
| Microsoft Agent Framework | https://github.com/microsoft/agent-framework | MIT | Agent + 图式 Workflow + 中间件三层 | 图式工作流 | 工作流状态 + 检查点 | 人工审批暂停与恢复 | 内建 OpenTelemetry |
| OpenAI Agents SDK | https://github.com/openai/openai-agents-python | MIT | Agent + `Runner` 循环 + 交接与护栏 | 轻量循环 + 交接 | Session 会话历史 | 工具审批（`needsApproval`） | 内建 tracing |
| OpenAI Agents SDK JS | https://github.com/openai/openai-agents-js | MIT | 同上，另有 Realtime 语音与 Sandbox 沙箱 | 轻量循环 + 交接 | Session 会话历史 | 工具审批 | 内建 tracing |
| CrewAI | https://github.com/crewAIInc/crewAI | MIT | Agent（角色 / 目标 / 背景）+ Task + Crew | SOP 流水线 | 任务间经 `context` 传产出 | 任务级人工输入 / 层级 manager 审批 | 事件监听器与回调 |
| PydanticAI | https://github.com/pydantic/pydantic-ai | MIT | Agent + Pydantic 输出契约 + 依赖注入 | 类型驱动 | 无内建状态机，循环自己写 | 工具里抛 `ModelRetry` 触发重来 | Logfire（可选接入） |
| smolagents | https://github.com/huggingface/smolagents | Apache-2.0 | `CodeAgent` / `ToolCallingAgent` 加 Tool | 极简代码块式 | 无状态，步间变量写在代码里 | 无内建，需自行实现 | 弱，多半要自己埋点 |
| Agno | https://github.com/agno-agi/agno | Apache-2.0 | Agent / Team / Workflow 加 Knowledge 与 AgentOS | 角色式 + 工作流 | 会话库 + 记忆后端 | Workflow 步骤可挂起 | 内建遥测 |
| Semantic Kernel | https://github.com/microsoft/semantic-kernel | MIT | Kernel + Plugin / Function + Filter | 内核插件式 | Process 步骤状态 | Process 步骤可挂起与恢复 | 内建 OTel 日志 |
| Vercel AI SDK | https://github.com/vercel/ai | Apache-2.0（仓库含多份许可，GitHub API 报 NOASSERTION） | `streamText` / `generateText` 加 `useChat` 钩子 | UI 优先工具包 | 前端消息状态机（协议驱动） | 无内建 | 自行埋点或走网关侧可观测 |
| Mastra | https://github.com/mastra-ai/mastra | Apache-2.0，但 `ee/` 目录下的企业版组件另有许可 | Agent / Workflow / Memory / Eval 五件套 | TS 原生全栈框架 | 工作流状态 + 记忆后端 | Workflow 的 suspend / resume | 内建 OTel |
| Dify | https://github.com/langgenius/dify | Apache-2.0 加附加条件（Dify Open Source License） | 可视化工作流 + 知识库 | 低代码平台 | 平台侧工作流状态 | 平台内审批节点 | 平台自带运行轨迹 |

AutoGen 的许可要特别说明一句：`LICENSE-CODE` 是 MIT，但**根目录的 `LICENSE` 是 CC-BY-4.0**，两者并存。拿代码做二次开发看 `LICENSE-CODE`，引用文档和样例看根目录那份。

## 二、按抽象层次分组：每一组的共同代价

### 链式组合式：LangChain、Haystack、Vercel AI SDK

抽象是"把步骤拼成链"。这条路上框架做的事情最少，你对流程的控制也最直接。

共同代价是**控制流是隐式的**。链只能往前走，要循环、要分支、要从中间某一步重来，都得自己在链外面包一层循环和条件。每一跳送进模型的上下文也得手工裁剪——链不会替你算 token，也不会替你决定"这一步要不要带上前面那段历史"。

### 状态图式：LangGraph、LangGraph.js、Microsoft Agent Framework、Mastra

抽象是"显式状态 + 节点 + 边"，状态的变化全部写清楚，每个通道的合并规则也写清楚。

共同代价是**状态必须可序列化**，而且每个节点都要先想清楚"读哪些通道、写哪些通道"。节点内部随便改状态是不行的——这既是约束也是保护，因为并行分支的合并顺序因此不会变成一团糊涂。第二个代价是抽象层比链条厚：同一个"查一下再回答"的逻辑，在链里是三行，在图里可能要拆成两个节点加一条边。

### 对话式多 Agent：AutoGen

抽象是"共享一段对话，主持人决定下一个谁发言"。它最有价值的地方是信息对称——每个 Agent 看得见所有人说过的话。

共同代价是**轮次放大成本**：每一轮都要重发全量历史，人越多、话越长，这块越贵。收敛靠终止条件而不是结构保证——不配好终止条件，它会一直聊下去（第 14 章引了官方文档的原话：没有终止条件的群聊会无限跑）。

### SOP / 角色式：CrewAI、Agno、Dify

抽象是"角色 / 任务 / 流程"——用组织结构来描述要做的事，业务方看得懂，改得动。

共同代价是**只有流程固定时才划算**。`expected_output` 写的是期望，不是契约：框架不会拿它去验收产出，写得不够也不拦（第 15 章）。角色扮演还额外放大了这一点——"你是资深专家"这个设定本身就包含"你自己判断怎么干"。第二个代价是层级流程的 manager 要多花两次模型调用（分配 + 验收），成本直接三倍。

### 轻量循环、类型驱动与内核插件式：OpenAI Agents SDK、OpenAI Agents SDK JS、PydanticAI、smolagents、Semantic Kernel

这一组的共同点是**都不提供完整的编排层**。Agents SDK 的循环只有"调模型 → 调工具 → 交接 → 再调模型"，PydanticAI 干脆让你自己写循环，smolagents 连状态都没有。

换来的是两件事：**离模型更近**（PydanticAI 的输出契约直接用类型系统卡死，smolagents 让模型写代码从而一次能做更多事），以及**可控**（Semantic Kernel 的过滤器把日志、重试、内容安全挂在业务代码外面）。

代价就是：你得自己把循环、分支、并行、持久化搭起来。选这一组的前提是流程本身不复杂。

### 这些代价的量级

抽象层次不同，代价的量级也不同。第 14 章配套代码里跑过同一个群聊任务，三种开会方式的实测结果：

| 开会方式 | 轮次 | 模型调用次数 |
| --- | --- | --- |
| 轮询（RoundRobin） | 5 | 5 |
| 规则选人（`selector_func`） | 3 | 3 |
| 模型选人 | 3 | 6 |

轮询多出来的两轮是浪费的——按顺序问到了还没到时候的人。规则选人零额外调用地跳过了无效轮次；模型选人最灵活，但每轮多一次调用，轮次越多账越厚。**轮询省调用，自动选人省轮次**，这就是两者的全部距离。

另外两个可以直接量出来的数：AutoGen 的 `RoundRobinGroupChat` 和 `SelectorGroupChat` 的 `max_turns` 默认是 `None`，不设终止条件就会一直跑（官方文档原话是 "the group chat will run indefinitely"），只有 `MagenticOneGroupChat` 默认 20；CrewAI 的层级流程每个任务从 1 次模型调用涨到 3 次（分配 + 执行 + 验收）。

## 三、选型速查表

**选型不是排名。** 下面这张表回答的是"我的需求像哪一类"，不是"哪个框架更强"。同一个项目在需求变化后换框架是常事，但换框架的代价（抽象泄漏、重写编排、评测基线作废）往往被低估——第 19 章讲了这部分。

| 你的需求 | 优先看哪几个 | 为什么 |
| --- | --- | --- |
| 单 Agent 带几个工具 | OpenAI Agents SDK（带护栏与追踪）/ PydanticAI（带输出契约）/ Vercel AI SDK（前端同语言） | 循环本身只有几行，不需要编排层；挑你更在意护栏、类型契约还是前端流式 |
| 确定性流水线 | CrewAI / Agno / Haystack | 步骤固定时，"角色 + 任务"的表达最省事；Haystack 的组件还能单独拿去别处用 |
| 跨 Agent、跨系统协作 | MCP 解"调工具"，A2A 解"Agent 间通信"；编排交给 LangGraph 或 Agents SDK | 先分清要解决的是工具互通还是智能体互通，两者的消息模型完全不同 |
| 可恢复的长任务（跑几天、中间要人批、要能回滚重放） | LangGraph / Microsoft Agent Framework | 需要检查点与时间旅行，只有状态图这一组原生提供；其余框架的 Session 只是会话记忆 |
| 多角色协作 | AutoGen（共享对话）/ CrewAI（角色分工）/ OpenAI Agents SDK（显式交接） | 三种协作形态的上下文传递方式完全不同：要来回协商、要流水线、要可控移交 |
| 需要跑模型生成的代码 | smolagents（最小形态）/ OpenHands 或 Aider（完整平台） | 代码路径必须配沙箱；前者给的是机制，后者给的是带隔离环境的成品 |

## 四、这些框架什么时候不适用

最后说几条"别用"的情况，它们的价值在于提前拦住一次错误的架构决策。

**只要一次模型调用加一个工具，就别上任何框架。** 第 19 章的起步路径说得很直白：绝大多数项目停在"手写循环 + 几个工具"这一级就够了。框架是来解决你已经遇到的具体问题的，不是用来让架构图变好看的。

**流程固定到只有两三步，也别上图。** 状态图的价值在循环、分支和并行汇合；直线流程用图表达，是纯粹的额外开销。

**没有容器或托管沙箱，就别让模型执行代码。** Node 没有权限模型，子进程默认什么都能干（读文件、发请求、读环境变量里的密钥），Python 那边的 `subprocess` 加资源限制是一条更短的路。这不是框架选型问题，是安全问题。

**要求产出被多个下游稳定消费，就别选产出是自由文本的那几个。** CrewAI 的 `expected_output`、Agno 和 smolagents 的默认输出都是给模型看的提示，不是给代码用的契约。PydanticAI 的输出类型契约，或者任何带结构化输出（`output_type`）的框架，才经得起下游反复解析。

**框架正在被合并或停止维护时，新项目别押上去。** AutoGen 已经停止接收新功能，官方把方向指向了 Microsoft Agent Framework；Semantic Kernel 也是同样的处境。选型结论的有效期以季度计，半年前的对错今天可能反过来。

**团队里没人愿意维护框架抽象，就别引。** 框架的抽象会渗进业务代码：升级要改业务、测试要构造框架对象、不熟悉的人读不懂。这些成本比省下来的那点开发时间更贵。
