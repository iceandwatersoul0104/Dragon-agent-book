# 附录 D 全书索引

本索引以**已落盘的章节文件**为准整理：章号、标题取自各章正文首行，项目条目取自正文实际引用的仓库地址，论文条目取自正文实际引用的 arXiv 编号（标题已逐条与 arXiv 官方接口核对）。四种索引各有用途——按章号定位用速查表，按概念串起跨章线索用主题索引，按名字找实现用项目索引，找原始论文用论文索引。

## 一、章节速查表

| 章 | 标题 | 本章解决的核心问题 | 配套代码 |
|---|---|---|---|
| 第 1 章 | Agent 的定义、边界与心智模型 | 什么才算 Agent：给出可操作的判定标准，把 Agent 与工作流、链式调用、Copilot 的边界划清。 | 否 |
| 第 2 章 | Agent 系统的最小解剖：四回路架构 | 一个 Agent 系统该有哪些零件：用感知、规划、行动、反馈四回路加状态、记忆、工具、护栏四个横切面给出统一解剖模型。 | 是（`code/ch02-anatomy`） |
| 第 3 章 | 为什么需要架构：可靠性、复利与工程约束 | 为什么要上架构：论证单步可靠性天花板与错误累积，界定框架的收益边界与"什么时候不该上框架"。 | 否 |
| 第 4 章 | ReAct：推理与行动交替的经典范式 | ReAct 的契约怎么写：讲透轨迹格式、动作解析、观察回灌与循环检测，以及它与函数调用的层次关系。 | 是（`code/ch04-react`） |
| 第 5 章 | 提示增强技术：CoT、Few-shot、Self-ask 与 Least-to-Most | 提示技巧什么时候该换成架构：把 CoT、Few-shot、Self-ask、Least-to-Most 分门别类并给出各自的失效边界。 | 是（`code/ch05-prompting`） |
| 第 6 章 | 反思与自纠错：Reflexion、Self-Refine 与独立评审者 | 怎么让模型检查自己：对比带记忆重试、多轮改写、独立评审者三种反思形态，讲清代价与熔断。 | 是（`code/ch06-reflection`） |
| 第 7 章 | 规划范式：Plan-and-Execute、ReWOO 与搜索型推理 | 计划怎么不跑偏：讲清三类规划范式的机制差异，以及"计划过时"该怎么处理。 | 是（`code/ch07-planning`） |
| 第 8 章 | 工具调用契约：把 Function Calling 当作 API 工程 | 工具契约怎么写才不出事：把函数调用当 API 工程——描述、校验、并行、错误回灌、幂等。 | 是（`code/ch08-tool-calling`） |
| 第 9 章 | 代码即行动：Code-Act 与 Program-of-Thought | 能不能让模型直接写代码：论证代码即行动的表达力优势，以及沙箱隔离的硬性要求。 | 是（`code/ch09-code-act`） |
| 第 10 章 | 上下文工程：把上下文当作稀缺资源预算 | 上下文怎么当预算花：把上下文升格为可分配资源，讲分层、检索、压缩、淘汰与上下文腐化。 | 是（`code/ch10-context-engineering`） |
| 第 11 章 | 停止准则、步数上限与成本守卫 | 做不完的时候怎么办：设计四类停止条件、步数与预算上限、无进展检测与优雅降级。 | 是（`code/ch11-stopping`） |
| 第 12 章 | LangChain 与 LangGraph：状态图式编排范式 | 复杂 Agent 怎么编排：解剖状态图、归约语义、检查点、中断与人工介入的代价。 | 是（`code/ch12-langgraph`） |
| 第 13 章 | LlamaIndex：数据驱动的 Agent 范式 | 数据驱动的另一条路：说明 LlamaIndex 如何把 Agent 架在索引与检索之上，以及它的适用边界。 | 是（`code/ch13-llamaindex`） |
| 第 14 章 | AutoGen 与 OpenAI Agents SDK：对话式多 Agent 与轻量编排两条路线 | 对话式多 Agent 还是轻量编排：并置解剖 AutoGen 与 OpenAI Agents SDK 的设计哲学与迁移成本。 | 是（`code/ch14-autogen-agentsdk`） |
| 第 15 章 | CrewAI：角色—任务—流程心智模型 | 角色任务流程能不能撑起工程：评估 CrewAI 的表达力、可测试性与失败可解释性。 | 是（`code/ch15-crewai`） |
| 第 16 章 | PydanticAI、Agno 与 smolagents：类型驱动与轻量路线 | 小而美的框架怎么选：比较类型驱动、极简 API 与代码优先三条路线的取舍。 | 是（`code/ch16-light-frameworks`） |
| 第 17 章 | Semantic Kernel 与微软 Agent 栈 | 企业集成这条路怎么走：从插件、过滤器、记忆看 Semantic Kernel 与微软 Agent 栈的定位。 | 否 |
| 第 18 章 | TypeScript 生态：Mastra、Vercel AI SDK 与 Agents SDK JS | TS 侧值不值得用：对比 Mastra、Vercel AI SDK、Agents SDK JS 在流式与边缘部署上的优势与短板。 | 是（`code/ch18-ts-ecosystem`） |
| 第 19 章 | 框架选型决策树与十个常见反模式 | 我到底该用哪一套：给出选型决策树、能力对照表与十个常见反模式。 | 否 |
| 第 20 章 | 多 Agent 的动机、分工理论与何时不该上多 Agent | 什么时候不该上多 Agent：给出多 Agent 的真实收益、负收益与最小可行拆分原则。 | 否 |
| 第 21 章 | 对话式协作：GroupChat 与 CAMEL 角色扮演 | 多 Agent 怎么开会：拆解轮次调度、角色提示、终止条件与 token 放大系数。 | 是（`code/ch21-conversation-collab`） |
| 第 22 章 | SOP 流水线：MetaGPT、ChatDev 与 AgentVerse 的软件公司隐喻 | 能不能把公司流程编码给 Agent：分析以标准文档为共享状态的 SOP 流水线的收益与脆弱性。 | 是（`code/ch22-sop-pipeline`） |
| 第 23 章 | 协作拓扑与调度：Supervisor、层级、网状、辩论与黑板 | 多 Agent 怎么接线：比较五种拓扑的控制流、上下文共享范围与失败传播路径。 | 是（`code/ch23-topology`） |
| 第 24 章 | Agent 间通信协议：MCP、A2A 与内部消息设计 | Agent 之间怎么说话：厘清 MCP 与 A2A 的职责边界，以及内部消息契约怎么设计。 | 是（`code/ch24-inter-agent-protocol`） |
| 第 25 章 | 多 Agent 失效模式与成本治理 | 多 Agent 为什么会翻车：列举失效模式、检测信号与熔断手段，沉淀成可监控指标。 | 是（`code/ch25-multi-agent-failures`） |
| 第 26 章 | 记忆分类学：工作、情景、语义与程序性记忆 | "记忆"到底有几种：用工作、情景、语义、程序性四分类建立工程坐标系与读写接口。 | 是（`code/ch26-memory-taxonomy`） |
| 第 27 章 | 长期记忆实现：向量、摘要与实体图谱三条路线 | 长期记忆存在哪里：比较向量、摘要、实体图谱三条路线的机制、失效模式与成本。 | 是（`code/ch27-long-term-memory`） |
| 第 28 章 | 上下文窗口工程：检索、压缩、淘汰与分层 | 上下文窗口怎么经营：给出检索、压缩、淘汰、分层四种操作的算法、参数、指标与提示缓存的耦合。 | 是（`code/ch28-context-windowing`） |
| 第 29 章 | 检查点、回放与多 Agent 共享状态 | 崩了怎么续、错了怎么查：讲清快照与事件溯源、时间旅行调试、共享状态模式与重放安全。 | 是（`code/ch29-checkpoint-replay`） |
| 第 30 章 | Agentic RAG：让检索从预处理变成主动决策 | 检索要不要交给 Agent 决定：解释 Agentic RAG 的范式转变、引用校验与质量度量，以及何时退回朴素 RAG。 | 是（`code/ch30-agentic-rag`） |
| 第 31 章 | 沙箱与执行隔离 | 不可信代码在哪跑：建立威胁模型，给出从进程隔离到微虚机的隔离技术谱系与资源配额。 | 是（`code/ch31-sandbox`） |
| 第 32 章 | 提示注入与越权防护 | 被骗了怎么办：建立提示注入与越权的威胁模型，给出纵深防御方案与残余风险边界。 | 是（`code/ch32-prompt-injection`） |
| 第 33 章 | 可观测性：Trace、Span 与 Agent 专属指标 | 线上慢了怎么查：讲清 Agent 追踪的 span 语义、开源栈取舍与为 Agent 设计的 SLI。 | 是（`code/ch33-observability`） |
| 第 34 章 | 评测体系：基准、任务集、LLM-as-Judge 与回归测试 | 怎么证明改动是变好了：拆解主流基准的能力边界、自建任务集、判定器偏差与 CI 回归。 | 是（`code/ch34-evaluation`） |
| 第 35 章 | 成本与性能优化 | 钱花在哪了：建立成本模型，掌握提示缓存、模型路由、语义缓存与延迟优化手段。 | 是（`code/ch35-cost-performance`） |
| 第 36 章 | 生产化检查清单与真实故障模式 | 上线前该确认什么：把全书结论固化为可执行检查清单，并复盘真实故障的架构根因。 | 否 |
| 第 37 章 | 代码智能体与仓库级自治 | 代码 Agent 该长什么样：比较五种形态在检索、编辑、验证上的策略差异与真正起作用的工程要素。 | 是（`code/ch37-coding-agents`） |
| 第 38 章 | 计算机使用与 GUI Agent | 没有 API 怎么操作界面：讲清屏幕感知、动作空间与 grounding，以及 GUI Agent 的脆弱性。 | 是（`code/ch38-computer-use`） |
| 第 39 章 | 协议与生态全景：MCP、A2A、AG-UI、Skills 与插件体系 | 协议层怎么分层：画清模型 API、MCP、A2A、AG-UI、Skills 各自解决的问题与组合方式。 | 是（`code/ch39-protocol-ecosystem`） |
| 第 40 章 | 自举与自我改进：AutoGPT 的教训与演化式改进 | 让 Agent 自我改进可行吗：复盘自主循环、程序演化、模型自训练三条路线的机制与安全边界。 | 否 |
| 第二卷第 1 章 | 立项：把想法变成一份可执行的需求 | 把"做个代码审查 Agent"这句话变成可执行、可验收的规格：判据、范围、验证方式都要写死，否则评审永远在吵"做对了算不算"。 | `specs/check_spec.py` |
| 第二卷第 2 章 | 工程骨架：目录、配置与错误处理约定 | 目录怎么分、配置从哪来、错误怎么分类、日志怎么写——决定了凌晨两点你能不能查到一个任务到底跑了什么、用的哪套配置。 | `dragon_agent/config.py` 等 3 个叶子模块 |
| 第二卷第 3 章 | 模型接入抽象：让 mock 与真实 API 共存 | 让 mock 与真实 API 返回**同一种**东西，业务代码就不必长出 isinstance 分支；否则测试里跑通的那半边分支，线上永远没人走过。 | `dragon_agent/llm.py` |
| 第二卷第 4 章 | 工具层：注册、校验、错误回灌与副作用 | 工具失败是**结果**不是异常：参数校验、幻觉工具名、文件不存在、重复副作用，四类线上事故共用同一个根因。 | `dragon_agent/tools.py` |
| 第二卷第 5 章 | 上下文装配器：把提示词变成资源分配 | 提示词不是拼出来的字符串，是要分配的预算：分层、排序、裁剪各占多少，决定了模型还看不看得见你写在最前面的约束。 | `dragon_agent/context.py` |
| 第二卷第 6 章 | Agent 主循环：带预算守卫的 ReAct | 主循环不是"循环"，它是一组**停止准则**：步数上限、异常收敛、无进展检测、预算守卫，缺一条就烧一次钱。 | `dragon_agent/agent.py` |
| 第二卷第 7 章 | 状态与检查点：让任务可回放、可恢复 | 把状态显式化：崩溃后能接着跑、事后能复现、副作用不重复执行——这三条各自对应一类看不见的代价。 | `dragon_agent/state.py` |
| 第二卷第 8 章 | 记忆模块：把"记住"做成可控的读写 | 区分工作/情景/语义/程序性四类记忆，明确写入时机，并让"该想起时想起了"可被评测，而不只是看检索命中率。 | `dragon_agent/memory.py` |
| 第二卷第 9 章 | 护栏与权限：能力最小化 | 工具分级、二次确认、注入检测、越权约束怎么落地，以及为什么这套防线不是保险箱——残余风险要诚实说出来。 | `dragon_agent/guardrails.py` |
| 第二卷第 10 章 | 编排层：从单 Agent 到多 Agent 的升级路径 | 什么时候才该上多 Agent：并行性、上下文隔离、能力异构、权限隔离四判据全中才升级，选哪种拓扑、怎么熔断、怎么退回单 Agent。 | `dragon_agent/orchestration.py` |
| 第二卷第 11 章 | 协议接入：把你的 Agent 变成 MCP Server 与 A2A 节点 | 把同一批工具同时暴露为 MCP 与内部调用，别维护两套实现；以及 A2A 的委托语义与协议演进怎么接才不被锁死。 | `dragon_agent/mcp_server.py` |
| 第二卷第 12 章 | 可观测性：接上 trace、指标与成本账 | trace、指标、成本账是三件事：采样会污染指标所以指标要全量算，脱敏必须在采集端做因为落盘即泄露。 | `dragon_agent/observability.py` |
| 第二卷第 13 章 | 评测与回归：让改动有依据 | 建任务集与判定器，让"改了一句工具描述"这种改动能被归因；能用确定性断言的，绝不用模型判定。 | `dragon_agent/evaluation.py` |
| 第二卷第 14 章 | 上线：容器化、成本控制与值班手册 | 前 13 章做完的是"能跑"，上线要回答"跑在哪儿、跑多快、花多少、坏了谁管"：镜像、密钥、限流、成本护栏与值班手册。 | `dragon_agent/cli.py` |
## 二、主题索引

按主题词列出相关章节；一个主题跨章出现时，章号按正文讨论的先后排列。

| 主题 | 相关章节 |
|---|---|
| Agent 的判定标准 | 第 1 章、第 5 章、第 26 章 |
| 自主性分级与代价 | 第 1 章、第 31 章 |
| 四回路架构 | 第 1 章、第 2 章 |
| ReAct 范式 | 第 1 章、第 2 章、第 4 章、第 5 章、第 40 章 |
| 思维链与少样本提示 | 第 4 章、第 5 章 |
| 自问与由少到多 | 第 5 章 |
| 反思与自纠错 | 第 1 章、第 5 章、第 6 章、第 7 章、第 40 章 |
| 自我精修与独立评审者 | 第 6 章 |
| 计划-执行与计划过时 | 第 1 章、第 4 章、第 7 章 |
| ReWOO 与变量解耦 | 第 5 章、第 7 章、第 9 章 |
| 思维树与思维图 | 第 7 章 |
| 状态图与条件边 | 第 2 章、第 12 章、第 13 章、第 19 章、第 23 章、第 29 章 |
| 状态归约 | 第 2 章、第 12 章、第 13 章、第 18 章 |
| 中断与人工介入 | 第 12 章、第 13 章、第 15 章、第 23 章、第 29 章 |
| 停止准则 | 第 1 章、第 4 章、第 11 章、第 19 章、第 25 章、第 36 章 |
| 步数与递归上限 | 第 1 章、第 4 章、第 11 章、第 14 章、第 19 章、第 25 章 |
| 无进展检测 | 第 1 章、第 6 章、第 11 章、第 21 章、第 25 章、第 33 章 |
| 预算守卫与降级 | 第 11 章、第 25 章、第 35 章、第 36 章 |
| 上下文工程与上下文预算 | 第 4 章、第 10 章、第 28 章、第 33 章 |
| 上下文组装器 | 第 10 章 |
| 上下文腐化 | 第 10 章、第 28 章 |
| 提示缓存与前缀命中 | 第 10 章、第 28 章、第 35 章 |
| 稳定前缀组织 | 第 28 章、第 35 章、第 39 章 |
| 长上下文模型的影响 | 第 5 章、第 10 章、第 20 章、第 28 章 |
| 工作记忆 | 第 26 章、第 28 章 |
| 情景记忆 | 第 26 章 |
| 语义记忆 | 第 26 章、第 27 章、第 28 章、第 29 章 |
| 程序性记忆 | 第 26 章 |
| 记忆抽取与覆盖更新 | 第 26 章、第 27 章 |
| 向量检索与嵌入 | 第 13 章、第 27 章、第 30 章 |
| 切块与块大小 | 第 27 章、第 30 章 |
| 相似度盲区 | 第 8 章、第 26 章、第 27 章、第 28 章 |
| 滚动摘要与摘要漂移 | 第 21 章、第 27 章、第 28 章 |
| 滑窗与抽取式压缩 | 第 28 章 |
| 价值淘汰与打分 | 第 28 章 |
| 分层记忆 core / recall / archival | 第 14 章、第 28 章、第 29 章 |
| 缺口检测与下沉 | 第 14 章、第 28 章 |
| 实体图谱与时序知识图谱 | 第 27 章 |
| 边失效 | 第 27 章 |
| 双时间轴 bi-temporal | 第 26 章、第 27 章 |
| 检查点 | 第 12 章、第 26 章、第 28 章、第 29 章、第 36 章 |
| 事件溯源 | 第 29 章 |
| 状态快照 | 第 12 章、第 29 章 |
| 时间旅行调试 | 第 12 章、第 29 章 |
| 幂等键与去重 | 第 8 章、第 29 章、第 36 章 |
| 补偿事务 | 第 8 章、第 29 章、第 36 章 |
| 共享状态与一致性 | 第 22 章、第 29 章 |
| 工具调用契约 | 第 8 章、第 18 章、第 24 章、第 32 章 |
| 参数校验与错误回灌 | 第 8 章、第 36 章 |
| 并行工具调用 | 第 2 章、第 4 章、第 8 章、第 35 章 |
| Code-Act 与代码即行动 | 第 9 章 |
| Program-of-Thought | 第 9 章 |
| 沙箱与执行隔离 | 第 9 章、第 31 章、第 32 章、第 37 章 |
| 网络策略与资源配额 | 第 31 章、第 38 章 |
| 多 Agent 的收益与代价 | 第 20 章、第 25 章 |
| GroupChat 与发言选择 | 第 14 章、第 20 章、第 21 章、第 23 章、第 25 章 |
| 角色扮演与 inception prompt | 第 21 章、第 23 章、第 25 章 |
| SOP 共享文档 | 第 20 章、第 22 章、第 23 章 |
| 协作拓扑 | 第 20 章、第 23 章 |
| 辩论与黑板 | 第 2 章、第 20 章、第 23 章 |
| MCP 协议 | 第 8 章、第 24 章、第 39 章 |
| A2A 与 AgentCard | 第 17 章、第 20 章、第 24 章、第 39 章 |
| AG-UI 与 Skills | 第 39 章 |
| 多 Agent 失效模式 | 第 21 章、第 24 章、第 25 章、第 26 章 |
| 提示注入 | 第 32 章、第 37 章 |
| 记忆投毒 | 第 31 章、第 32 章、第 36 章 |
| 越权与数据外泄 | 第 31 章、第 32 章 |
| 护栏与纵深防御 | 第 14 章、第 32 章、第 36 章 |
| 可观测性与追踪 | 第 33 章 |
| Agent 专属指标 | 第 33 章、第 34 章、第 36 章 |
| 评测基准 | 第 8 章、第 34 章、第 37 章、第 38 章 |
| 自建任务集与黄金答案 | 第 34 章、第 36 章 |
| LLM-as-a-Judge 与偏差 | 第 6 章、第 34 章 |
| 回归测试与 CI | 第 33 章、第 34 章、第 36 章 |
| 成本模型与每次成功成本 | 第 20 章、第 33 章、第 35 章、第 36 章 |
| 模型路由与级联 | 第 35 章、第 36 章 |
| 语义缓存 | 第 9 章、第 35 章 |
| 代码智能体 | 第 9 章、第 31 章、第 33 章、第 37 章 |
| GUI Agent 与 grounding | 第 23 章、第 37 章、第 38 章 |
| 可访问性树 | 第 31 章、第 32 章、第 38 章 |
| 自举与自我改进 | 第 40 章 |
| 目标漂移与评估污染 | 第 1 章、第 7 章、第 40 章 |
| 生产化与灰度回滚 | 第 25 章、第 33 章、第 34 章、第 36 章 |
## 三、项目索引

项目名以英文仓库名为主，仓库地址取自各章正文实际引用。

| 项目 | 仓库 | 相关章节 |
|---|---|---|
| Voyager | https://github.com/MineDojo/Voyager | 第 1 章、第 2 章、第 40 章 |
| smolagents | https://github.com/huggingface/smolagents | 第 1 章、第 2 章、第 3 章、第 4 章、第 8 章、第 9 章、第 11 章、第 14 章、第 16 章、第 19 章 |
| generative_agents | https://github.com/joonspk-research/generative_agents | 第 1 章、第 2 章 |
| langgraph | https://github.com/langchain-ai/langgraph | 第 1 章、第 2 章、第 3 章、第 4 章、第 6 章、第 7 章、第 11 章、第 12 章、第 13 章、第 14 章、第 15 章、第 16 章、第 17 章、第 19 章、第 20 章、第 21 章、第 22 章、第 23 章、第 25 章、第 26 章、第 27 章、第 28 章、第 29 章、第 30 章 |
| autogen | https://github.com/microsoft/autogen | 第 1 章、第 2 章、第 3 章、第 14 章、第 15 章、第 17 章、第 19 章、第 20 章、第 21 章、第 22 章、第 23 章、第 24 章、第 25 章 |
| reflexion | https://github.com/noahshinn/reflexion | 第 1 章、第 2 章 |
| openai-agents-python | https://github.com/openai/openai-agents-python | 第 1 章、第 2 章、第 3 章、第 4 章、第 5 章、第 6 章、第 8 章、第 9 章、第 11 章、第 14 章、第 17 章、第 19 章、第 20 章、第 21 章、第 22 章、第 24 章、第 25 章、第 26 章、第 28 章、第 29 章、第 30 章、第 32 章、第 35 章、第 36 章 |
| pydantic-ai | https://github.com/pydantic/pydantic-ai | 第 1 章、第 2 章、第 3 章、第 5 章、第 8 章、第 16 章、第 19 章 |
| ReAct | https://github.com/ysymyth/ReAct | 第 1 章、第 2 章、第 4 章 |
| SWE-agent | https://github.com/SWE-agent/SWE-agent | 第 2 章、第 3 章、第 37 章 |
| langsmith-python | https://github.com/langchain-ai/langsmith-python | 第 3 章、第 8 章、第 10 章、第 11 章、第 12 章、第 32 章、第 33 章、第 34 章、第 35 章、第 36 章 |
| langchain | https://github.com/langchain-ai/langchain | 第 4 章、第 5 章、第 6 章、第 7 章、第 8 章、第 9 章、第 10 章、第 11 章、第 12 章、第 13 章、第 14 章、第 19 章、第 26 章、第 27 章、第 28 章、第 30 章 |
| dspy | https://github.com/stanfordnlp/dspy | 第 5 章、第 6 章、第 7 章、第 9 章、第 10 章、第 11 章 |
| crewAI | https://github.com/crewAIInc/crewAI | 第 6 章、第 15 章、第 19 章、第 20 章、第 21 章、第 22 章、第 23 章、第 25 章、第 26 章、第 28 章、第 29 章、第 30 章 |
| letta | https://github.com/letta-ai/letta | 第 6 章、第 10 章、第 11 章、第 26 章、第 27 章、第 28 章、第 29 章、第 30 章 |
| Plan-and-Solve-Prompting | https://github.com/AGI-Edgerunners/Plan-and-Solve-Prompting | 第 7 章 |
| llm-pddl | https://github.com/Cranial-XIX/llm-pddl | 第 7 章 |
| tree-of-thought-llm | https://github.com/princeton-nlp/tree-of-thought-llm | 第 7 章 |
| gorilla | https://github.com/gorilla-llm/gorilla | 第 8 章、第 34 章 |
| modelcontextprotocol | https://github.com/modelcontextprotocol/modelcontextprotocol | 第 8 章、第 24 章、第 39 章 |
| aider | https://github.com/Aider-AI/aider | 第 9 章、第 11 章、第 37 章 |
| OpenHands | https://github.com/All-Hands-AI/OpenHands | 第 9 章、第 31 章、第 33 章、第 36 章、第 37 章 |
| code-act | https://github.com/xingyaoww/code-act | 第 9 章 |
| graphiti | https://github.com/getzep/graphiti | 第 10 章、第 26 章、第 27 章、第 30 章 |
| ragflow | https://github.com/infiniflow/ragflow | 第 10 章、第 26 章、第 27 章、第 28 章、第 30 章 |
| langmem | https://github.com/langchain-ai/langmem | 第 10 章、第 12 章、第 26 章、第 27 章、第 28 章、第 29 章 |
| mem0 | https://github.com/mem0ai/mem0 | 第 10 章、第 26 章、第 27 章、第 28 章、第 29 章、第 30 章 |
| haystack | https://github.com/deepset-ai/haystack | 第 12 章、第 13 章 |
| llama_index | https://github.com/run-llama/llama_index | 第 12 章、第 13 章、第 16 章、第 19 章、第 26 章、第 27 章、第 28 章、第 30 章 |
| Qwen-Agent | https://github.com/QwenLM/Qwen-Agent | 第 13 章、第 20 章、第 22 章 |
| dify | https://github.com/langgenius/dify | 第 13 章 |
| agent-framework | https://github.com/microsoft/agent-framework | 第 14 章、第 15 章、第 17 章、第 19 章、第 20 章、第 23 章、第 24 章、第 25 章 |
| agno | https://github.com/agno-agi/agno | 第 15 章、第 16 章 |
| semantic-kernel | https://github.com/microsoft/semantic-kernel | 第 17 章 |
| langgraphjs | https://github.com/langchain-ai/langgraphjs | 第 18 章 |
| mastra | https://github.com/mastra-ai/mastra | 第 18 章、第 19 章 |
| typescript-sdk | https://github.com/modelcontextprotocol/typescript-sdk | 第 18 章 |
| openai-agents-js | https://github.com/openai/openai-agents-js | 第 18 章 |
| ai | https://github.com/vercel/ai | 第 18 章 |
| AgentVerse | https://github.com/OpenBMB/AgentVerse | 第 20 章、第 21 章、第 22 章 |
| ChatDev | https://github.com/OpenBMB/ChatDev | 第 20 章、第 21 章、第 22 章 |
| A2A | https://github.com/a2aproject/A2A | 第 20 章、第 24 章、第 39 章 |
| agentscope | https://github.com/agentscope-ai/agentscope | 第 20 章、第 21 章、第 22 章、第 23 章、第 25 章 |
| MetaGPT | https://github.com/geekan/MetaGPT | 第 20 章、第 21 章、第 22 章 |
| camel | https://github.com/camel-ai/camel | 第 21 章、第 23 章、第 25 章 |
| OpenAgents | https://github.com/xlang-ai/OpenAgents | 第 22 章 |
| ag2 | https://github.com/ag2ai/ag2 | 第 23 章 |
| adk-python | https://github.com/google/adk-python | 第 23 章、第 24 章 |
| swarm | https://github.com/openai/swarm | 第 23 章 |
| a2a-js | https://github.com/a2aproject/a2a-js | 第 24 章 |
| a2a-python | https://github.com/a2aproject/a2a-python | 第 24 章 |
| AgentNetworkProtocol | https://github.com/agent-network-protocol/AgentNetworkProtocol | 第 24 章 |
| acp | https://github.com/i-am-bee/acp | 第 24 章 |
| python-sdk | https://github.com/modelcontextprotocol/python-sdk | 第 24 章 |
| agentops | https://github.com/AgentOps-AI/agentops | 第 25 章、第 32 章、第 33 章、第 36 章 |
| openllmetry | https://github.com/traceloop/openllmetry | 第 25 章、第 31 章、第 33 章、第 35 章 |
| zep | https://github.com/getzep/zep | 第 26 章、第 27 章 |
| cognee | https://github.com/topoteretes/cognee | 第 26 章、第 27 章 |
| airflow | https://github.com/apache/airflow | 第 29 章 |
| temporal | https://github.com/temporalio/temporal | 第 29 章 |
| phoenix | https://github.com/Arize-ai/phoenix | 第 31 章、第 33 章、第 34 章、第 36 章 |
| claude-code | https://github.com/anthropics/claude-code | 第 31 章、第 32 章、第 33 章、第 36 章、第 37 章 |
| browser-use | https://github.com/browser-use/browser-use | 第 31 章、第 32 章、第 38 章 |
| stagehand | https://github.com/browserbase/stagehand | 第 31 章、第 32 章、第 38 章 |
| firecracker | https://github.com/firecracker-microvm/firecracker | 第 31 章 |
| gvisor | https://github.com/google/gvisor | 第 31 章 |
| kata-containers | https://github.com/kata-containers/kata-containers | 第 31 章 |
| langfuse | https://github.com/langfuse/langfuse | 第 31 章、第 33 章、第 34 章、第 35 章、第 36 章 |
| runc | https://github.com/opencontainers/runc | 第 31 章 |
| semantic-conventions | https://github.com/open-telemetry/semantic-conventions | 第 33 章、第 36 章 |
| SWE-bench | https://github.com/SWE-bench/SWE-bench | 第 34 章、第 37 章 |
| AgentBench | https://github.com/THUDM/AgentBench | 第 34 章 |
| tau-bench | https://github.com/sierra-research/tau-bench | 第 34 章 |
| webarena | https://github.com/web-arena-x/webarena | 第 34 章 |
| OSWorld | https://github.com/xlang-ai/OSWorld | 第 34 章、第 38 章 |
| MCPBench | https://github.com/zyy07/MCPBench | 第 34 章 |
| Roo-Code | https://github.com/RooCodeInc/Roo-Code | 第 37 章 |
| opencode | https://github.com/anomalyco/opencode | 第 37 章 |
| cline | https://github.com/cline/cline | 第 37 章 |
| Mind2Web | https://github.com/OSU-NLP-Group/Mind2Web | 第 38 章 |
| skyvern | https://github.com/Skyvern-AI/skyvern | 第 38 章 |
| UI-TARS-desktop | https://github.com/bytedance/UI-TARS-desktop | 第 38 章 |
| OmniParser | https://github.com/microsoft/OmniParser | 第 38 章 |
| playwright | https://github.com/microsoft/playwright | 第 38 章 |
| ag-ui | https://github.com/ag-ui-protocol/ag-ui | 第 39 章 |
| skills | https://github.com/anthropics/skills | 第 39 章 |
| servers | https://github.com/modelcontextprotocol/servers | 第 39 章 |
| AutoGPT | https://github.com/Significant-Gravitas/AutoGPT | 第 40 章 |
| ace | https://github.com/ace-agent/ace | 第 40 章 |
| dgm | https://github.com/jennyzzt/dgm | 第 40 章 |
| instructor | https://github.com/567-labs/instructor | 第二卷第 4 章 |
| litellm | https://github.com/BerriAI/litellm | 第二卷第 3 章 |
| Guardrails | https://github.com/NVIDIA-NeMo/Guardrails | 第二卷第 9 章 |
| inspect_ai | https://github.com/UKGovernmentBEIS/inspect_ai | 第二卷第 1 章、第二卷第 13 章 |
| anthropic-sdk-python | https://github.com/anthropics/anthropic-sdk-python | 第二卷第 3 章 |
| sqlite-vec | https://github.com/asg017/sqlite-vec | 第二卷第 8 章 |
| chroma | https://github.com/chroma-core/chroma | 第二卷第 8 章 |
| deepeval | https://github.com/confident-ai/deepeval | 第二卷第 13 章 |
| httpx | https://github.com/encode/httpx | 第二卷第 3 章 |
| spec-kit | https://github.com/github/spec-kit | 第二卷第 1 章 |
| tenacity | https://github.com/jd/tenacity | 第二卷第 2 章、第二卷第 3 章 |
| opentelemetry-python | https://github.com/open-telemetry/opentelemetry-python | 第二卷第 2 章 |
| evals | https://github.com/openai/evals | 第二卷第 1 章、第二卷第 13 章 |
| openai-guardrails-js | https://github.com/openai/openai-guardrails-js | 第二卷第 9 章 |
| openai-guardrails-python | https://github.com/openai/openai-guardrails-python | 第二卷第 9 章 |
| openai-python | https://github.com/openai/openai-python | 第二卷第 3 章 |
| jinja | https://github.com/pallets/jinja | 第二卷第 5 章 |
| promptfoo | https://github.com/promptfoo/promptfoo | 第二卷第 1 章、第二卷第 13 章 |
| pydantic | https://github.com/pydantic/pydantic | 第二卷第 4 章、第二卷第 7 章 |
| pydantic-settings | https://github.com/pydantic/pydantic-settings | 第二卷第 2 章 |
| pytest | https://github.com/pytest-dev/pytest | 第二卷第 13 章 |
| jsonschema | https://github.com/python-jsonschema/jsonschema | 第二卷第 4 章 |
| structlog | https://github.com/structlog/structlog | 第二卷第 2 章 |
| ragas | https://github.com/vibrantlabsai/ragas | 第二卷第 13 章 |
## 四、论文索引

每条给出正文引用的 arXiv 编号；编号与标题已逐条核对。

| 论文 | arXiv 编号 | 相关章节 |
|---|---|---|
| ReAct: Synergizing Reasoning and Acting in Language Models | arXiv:2210.03629 | 第 1 章、第 2 章、第 3 章、第 4 章 |
| Reflexion: Language Agents with Verbal Reinforcement Learning | arXiv:2303.11366 | 第 1 章、第 2 章、第 3 章、第 6 章 |
| Generative Agents: Interactive Simulacra of Human Behavior | arXiv:2304.03442 | 第 1 章、第 2 章、第 3 章 |
| Voyager: An Open-Ended Embodied Agent with Large Language Models | arXiv:2305.16291 | 第 1 章、第 2 章、第 3 章 |
| A Survey on Large Language Model based Autonomous Agents | arXiv:2308.11432 | 第 1 章、第 2 章、第 3 章 |
| AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation | arXiv:2308.08155 | 第 2 章、第 3 章 |
| SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering | arXiv:2405.15793 | 第 2 章、第 3 章 |
| Why Do Multi-Agent LLM Systems Fail? | arXiv:2503.13657 | 第 1 章、第 2 章、第 3 章 |
| Language Models are Few-Shot Learners | arXiv:2005.14165 | 第 5 章 |
| Calibrate Before Use: Improving Few-Shot Performance of Language Models | arXiv:2102.09690 | 第 5 章 |
| Rethinking the Role of Demonstrations: What Makes In-Context Learning Work? | arXiv:2202.12837 | 第 5 章 |
| Surface Form Competition: Why the Highest Probability Answer Isn't Always Right | arXiv:2104.08315 | 第 5 章 |
| Chain-of-Thought Prompting Elicits Reasoning in Large Language Models | arXiv:2201.11903 | 第 5 章 |
| Measuring and Narrowing the Compositionality Gap in Language Models | arXiv:2210.03350 | 第 5 章 |
| Least-to-Most Prompting Enables Complex Reasoning in Large Language Models | arXiv:2205.10625 | 第 5 章 |
| Lost in the Middle: How Language Models Use Long Contexts | arXiv:2307.03172 | 第 5 章、第 10 章 |
| Training Verifiers to Solve Math Word Problems | arXiv:2110.14168 | 第 6 章 |
| Self-Consistency Improves Chain of Thought Reasoning in Language Models | arXiv:2203.11171 | 第 6 章 |
| Self-Refine: Iterative Refinement with Self-Feedback | arXiv:2303.17651 | 第 6 章 |
| Let's Verify Step by Step | arXiv:2305.20050 | 第 6 章 |
| Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena | arXiv:2306.05685 | 第 6 章 |
| LLM+P: Empowering Large Language Models with Optimal Planning Proficiency | arXiv:2304.11477 | 第 7 章 |
| Plan-and-Solve Prompting: Improving Zero-Shot Chain-of-Thought Reasoning | arXiv:2305.04091 | 第 7 章 |
| Tree of Thoughts: Deliberate Problem Solving with Large Language Models | arXiv:2305.10601 | 第 7 章 |
| Graph of Thoughts: Solving Elaborate Problems with Large Language Models | arXiv:2308.09687 | 第 7 章 |
| ReWOO: Decoupling Reasoning from Observations for Efficient Augmented Language Models | arXiv:2305.18323 | 第 7 章 |
| Toolformer: Language Models Can Teach Themselves to Use Tools | arXiv:2302.04761 | 第 9 章 |
| PAL: Program-aided Language Models | arXiv:2211.10435 | 第 9 章 |
| When Do Program-of-Thoughts Work for Reasoning? | arXiv:2308.15452 | 第 9 章 |
| Executable Code Actions Elicit Better LLM Agents | arXiv:2402.01030 | 第 9 章 |
| MemGPT: Towards LLMs as Operating Systems | arXiv:2310.08560 | 第 26 章、第 27 章、第 28 章、第 29 章 |
| Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory | arXiv:2504.19413 | 第 27 章 |
| Diagnosing and Mitigating Context Rot in Long-horizon Search | arXiv:2606.29718 | 第 10 章 |
| Length Is Cheap, Competition Is Not: Attention Predicts Context Rot in Retrieval | arXiv:2611.04169 | 第 10 章 |
## 五、配套代码目录索引

| 章 | 代码目录 | 文件 |
|---|---|---|
| 第 2 章 | `code/ch02-anatomy` | README.md、main.py |
| 第 4 章 | `code/ch04-react` | README.md、main.py |
| 第 5 章 | `code/ch05-prompting` | README.md、main.py |
| 第 6 章 | `code/ch06-reflection` | README.md、main.py |
| 第 7 章 | `code/ch07-planning` | README.md、main.py |
| 第 8 章 | `code/ch08-tool-calling` | README.md、main.py |
| 第 9 章 | `code/ch09-code-act` | README.md、main.py |
| 第 10 章 | `code/ch10-context-engineering` | README.md、main.py |
| 第 11 章 | `code/ch11-stopping` | README.md、main.py |
| 第 12 章 | `code/ch12-langgraph` | README.md、main.py |
| 第 13 章 | `code/ch13-llamaindex` | README.md、main.py |
| 第 14 章 | `code/ch14-autogen-agentsdk` | README.md、main.py |
| 第 15 章 | `code/ch15-crewai` | README.md、main.py |
| 第 16 章 | `code/ch16-light-frameworks` | README.md、main.py |
| 第 18 章 | `code/ch18-ts-ecosystem` | README.md、main.py |
| 第 21 章 | `code/ch21-conversation-collab` | README.md、main.py |
| 第 22 章 | `code/ch22-sop-pipeline` | README.md、main.py |
| 第 23 章 | `code/ch23-topology` | README.md、main.py |
| 第 24 章 | `code/ch24-inter-agent-protocol` | README.md、main.py |
| 第 25 章 | `code/ch25-multi-agent-failures` | README.md、main.py |
| 第 26 章 | `code/ch26-memory-taxonomy` | README.md、main.py |
| 第 27 章 | `code/ch27-long-term-memory` | README.md、main.py |
| 第 28 章 | `code/ch28-context-windowing` | README.md、main.py |
| 第 29 章 | `code/ch29-checkpoint-replay` | README.md、main.py |
| 第 30 章 | `code/ch30-agentic-rag` | README.md、main.py |
| 第 31 章 | `code/ch31-sandbox` | README.md、audit.jsonl、main.py |
| 第 32 章 | `code/ch32-prompt-injection` | README.md、audit.jsonl、main.py |
| 第 33 章 | `code/ch33-observability` | README.md、main.py、traces.jsonl |
| 第 34 章 | `code/ch34-evaluation` | README.md、baseline.json、main.py |
| 第 35 章 | `code/ch35-cost-performance` | README.md、main.py |
| 第 37 章 | `code/ch37-coding-agents` | README.md、demo_repo、main.py |
| 第 38 章 | `code/ch38-computer-use` | README.md、main.py |
| 第 39 章 | `code/ch39-protocol-ecosystem` | README.md、main.py |
| 第二卷第 1 章 | `dragon-agent/specs/check_spec.py` | README.md、规格模板与检查器、示例脚本 |
| 第二卷第 2 章 | `dragon-agent/dragon_agent/config.py` 等 3 个叶子模块 | README.md、模块与配套测试、示例脚本 |
| 第二卷第 3 章 | `dragon-agent/dragon_agent/llm.py` | README.md、模块与配套测试、示例脚本 |
| 第二卷第 4 章 | `dragon-agent/dragon_agent/tools.py` | README.md、模块与配套测试、示例脚本 |
| 第二卷第 5 章 | `dragon-agent/dragon_agent/context.py` | README.md、模块与配套测试、示例脚本 |
| 第二卷第 6 章 | `dragon-agent/dragon_agent/agent.py` | README.md、模块与配套测试、示例脚本 |
| 第二卷第 7 章 | `dragon-agent/dragon_agent/state.py` | README.md、模块与配套测试、示例脚本 |
| 第二卷第 8 章 | `dragon-agent/dragon_agent/memory.py` | README.md、模块与配套测试、示例脚本 |
| 第二卷第 9 章 | `dragon-agent/dragon_agent/guardrails.py` | README.md、模块与配套测试、示例脚本 |
| 第二卷第 10 章 | `dragon-agent/dragon_agent/orchestration.py` | README.md、模块与配套测试、示例脚本 |
| 第二卷第 11 章 | `dragon-agent/dragon_agent/mcp_server.py` | README.md、模块与配套测试、示例脚本 |
| 第二卷第 12 章 | `dragon-agent/dragon_agent/observability.py` | README.md、模块与配套测试、示例脚本 |
| 第二卷第 13 章 | `dragon-agent/dragon_agent/evaluation.py` | README.md、模块与配套测试、示例脚本 |
| 第二卷第 14 章 | `dragon-agent/dragon_agent/cli.py` | README.md、模块与配套测试、示例脚本 |

所有示例代码均为纯 Python 标准库实现，可在没有 API Key 的环境直接运行；各目录下的 README 说明依赖、运行方式与 `--live` 所需的环境变量。
