# 附录 B 开源项目速查表

本表收录正文提到过的项目，按**主要用途**分组。读者可以按“我要解决什么问题”倒查，而不是从头读框架文档。

**表里的每一行都经过联网核实**：仓库 URL 真实可达，许可证取自仓库的 LICENSE 文件或 GitHub API 返回的 `spdx_id`；无法确认的写「未核实」，不做猜测。主语言取自仓库语言统计。

表中的「章」是本书讲到该项目的章节号。清单只收录正文出现过的项目，未收录不代表项目不好用。

## 速查表

### 链式组合与通用框架

以 Runnable / 链 / 工作流抽象拼装能力，Agent 由组合得到。共同代价是**抽象层数一多，出错时要在多层之间来回定位**。

| 项目 | 仓库 | 许可证 | 主语言 | 一句话定位 | 章 |
| --- | --- | --- | --- | --- | --- |
| PydanticAI | [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai) | MIT | Python | 以 Pydantic 模型定义输入输出，类型安全地注入模型与依赖，输出契约最完整 | 1、2、3、5、8、16、19 |
| smolagents | [huggingface/smolagents](https://github.com/huggingface/smolagents) | Apache-2.0 | Python | 极简 Agent 库，让模型输出 Python 代码块而不是结构化动作，工具以函数形式调用 | 1、2、3、4、8、9、11、14、16、19 |
| LangChain | [langchain-ai/langchain](https://github.com/langchain-ai/langchain) | MIT | Python | 以 Runnable/LCEL 组合模型、提示、工具与检索，Agent 由链拼装 | 4、5、6、7、8、9、10、11、12、13、14、19、26、27、28、30 |
| DSPy | [stanfordnlp/dspy](https://github.com/stanfordnlp/dspy) | MIT | Python | 用声明式签名描述任务，再自动优化指令与示例——提示是被评测集优化出来的产物 | 5、6、7、9、10、11 |
| LlamaIndex | [run-llama/llama_index](https://github.com/run-llama/llama_index) | MIT | Python | 以数据连接器与索引为中心，为 Agent 提供检索与工作流引擎，含 Workflows 与 AgentWorkflow | 12、13、16、19、26、27、28、30 |
| Microsoft Agent Framework | [microsoft/agent-framework](https://github.com/microsoft/agent-framework) | MIT | Python | 融合 AutoGen 与 Semantic Kernel，统一 Agent、工作流与部署 | 14、15、17、19、20、23、24、25 |
| Agno | [agno-agi/agno](https://github.com/agno-agi/agno) | Apache-2.0 | Python | 以 Agent、Team、Workflow 组织多 Agent，AgentOS 承载持久运行 | 15、16 |
| Semantic Kernel | [microsoft/semantic-kernel](https://github.com/microsoft/semantic-kernel) | MIT | C# | 以插件、技能、记忆与计划为企业应用接入 LLM，Python/.NET/Java 三套 SDK 并排 | 17 |

### 状态图与显式编排

把控制流画成显式的状态机，循环与恢复都看得见。共同代价是**状态归约语义要自己定**（并行分支合并时谁覆盖谁）。

| 项目 | 仓库 | 许可证 | 主语言 | 一句话定位 | 章 |
| --- | --- | --- | --- | --- | --- |
| LangGraph | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | MIT | Python | 用显式状态图与检查点编排可循环、可恢复的 Agent 工作流 | 1、2、3、4、6、7、11、12、13、14、15、16、17、19、20、21、22、23、25、26、27、28、29、30 |
| Haystack | [deepset-ai/haystack](https://github.com/deepset-ai/haystack) | Apache-2.0 | Python | 把检索、生成与 Agent 拆成组件，用显式管道控制数据流向，强调显式控制检索与路由 | 12、13 |

### 多 Agent 协作

把一次任务拆给多个角色，能力异构或上下文隔离。共同代价是**协调开销随参与者数量上升**，且失败路径会被放大。

| 项目 | 仓库 | 许可证 | 主语言 | 一句话定位 | 章 |
| --- | --- | --- | --- | --- | --- |
| AutoGen | [microsoft/autogen](https://github.com/microsoft/autogen) | MIT（LICENSE-CODE）；根目录 LICENSE 为 CC-BY-4.0 | Python | 以可对话 Agent 与 GroupChat 支撑多 Agent 协作与代码执行 | 1、2、3、14、15、17、19、20、21、22、23、24、25 |
| OpenAI Agents SDK (Python) | [openai/openai-agents-python](https://github.com/openai/openai-agents-python) | MIT | Python | 用 Agent、工具、交接（handoff）、护栏与追踪拼装轻量工作流 | 1、2、3、4、5、6、8、9、11、14、17、19、20、21、22、24、25、26、28、29、30、32、35、36 |
| CrewAI | [crewAIInc/crewAI](https://github.com/crewAIInc/crewAI) | MIT | Python | 以角色、任务与流程模板组织 Agent 团队，流程只有 sequential 与 hierarchical 两个取值 | 6、15、19、20、21、22、23、25、26、28、29、30 |
| Qwen-Agent | [QwenLM/Qwen-Agent](https://github.com/QwenLM/Qwen-Agent) | Apache-2.0 | Python | 把函数调用、MCP、代码解释器与 RAG 收在同一个 Agent 内部，不拆 Agent 而给一个 Agent 挂不同工具 | 13、20、22 |
| AgentScope | [agentscope-ai/agentscope](https://github.com/agentscope-ai/agentscope) | Apache-2.0 | Python | 面向多 Agent 协作的框架，配 Studio 做可视化调试与追踪 | 20、21、22、23、25 |
| AG2 | [ag2ai/ag2](https://github.com/ag2ai/ag2) | Apache-2.0 | Python | AutoGen 的社区分支，GroupChat 与嵌套对话等实验性拓扑可以在这里对照着看 | 23 |
| OpenAI Swarm | [openai/swarm](https://github.com/openai/swarm) | MIT | Python | 用交接（handoff）把控制权从一个 Agent 交给另一个 | 23 |
| Google ADK (Python) | [google/adk-python](https://github.com/google/adk-python) | Apache-2.0 | Python | 同时接了 MCP 与 A2A 两边——仓库里有 MCP 工具接入 | 23、24 |

### SOP 流水线与角色扮演

把人类团队流程编码成固定阶段与固定角色。共同代价是**流程写死在代码里**，遇到没预设的分支就僵。

| 项目 | 仓库 | 许可证 | 主语言 | 一句话定位 | 章 |
| --- | --- | --- | --- | --- | --- |
| AgentVerse | [OpenBMB/AgentVerse](https://github.com/OpenBMB/AgentVerse) | Apache-2.0 | Python | 把软件公司的 SOP 编码成 Agent 流水线，共享的是标准文档而不是聊天记录 | 20、21、22 |
| MetaGPT | [geekan/MetaGPT](https://github.com/geekan/MetaGPT) | MIT | Python | 把软件公司的 SOP 直接编码成 Agent 流水线，PRD、接口定义、任务清单作为阶段产物交接 | 21、22 |
| ChatDev | [OpenBMB/ChatDev](https://github.com/OpenBMB/ChatDev) | Apache-2.0 | Python | 用 CEO、CTO、程序员、测试这类强角色分工，阶段之间靠标准文档交接而不是聊天记录 | 21、22 |
| CAMEL | [camel-ai/camel](https://github.com/camel-ai/camel) | Apache-2.0 | Python | inception prompt 范式的出处 | 21、23、25 |
| OpenAgents | [xlang-ai/OpenAgents](https://github.com/xlang-ai/OpenAgents) | Apache-2.0 | Python | 沿 AgentVerse 路线继续做的开放平台，把角色、工具与流程放到一起配置 | 22 |

### 记忆与知识图谱

给 Agent 装上跨会话的长期记忆。共同代价是**写入即承诺**——写进去的事实会一直影响后续回答，除非有明确的遗忘与失效机制。

| 项目 | 仓库 | 许可证 | 主语言 | 一句话定位 | 章 |
| --- | --- | --- | --- | --- | --- |
| Letta (MemGPT) | [letta-ai/letta](https://github.com/letta-ai/letta) | Apache-2.0 | 未核实 | 把记忆当一等问题，Reflexion 的"带记忆重试"在这里是原生能力而不是外挂 | 6、10、11、26、27、28、29、30 |
| Mem0 | [mem0ai/mem0](https://github.com/mem0ai/mem0) | Apache-2.0 | Python | 用 LLM 抽取并更新用户事实的独立记忆层 | 10、26、27、28、29、30 |
| LangMem | [langchain-ai/langmem](https://github.com/langchain-ai/langmem) | MIT | Python | 给 LangChain 提供自主写入与检索长期记忆的工具 | 10、12、26、27、28、29 |
| Graphiti | [getzep/graphiti](https://github.com/getzep/graphiti) | Apache-2.0 | Python | 增量构建带时间有效期的实体关系图谱记忆 | 10、26、27、30 |
| Zep | [getzep/zep](https://github.com/getzep/zep) | Apache-2.0 | Python | 官方 Zep 仓库，现以示例与集成代码为主 | 26、27 |
| Cognee | [topoteretes/cognee](https://github.com/topoteretes/cognee) | Apache-2.0 | Python | 用小模型把数据加工成可检索的知识图谱记忆 | 26、27 |

### 检索与 RAG

把文档接进回答。共同代价是**召回即上限**，检索没召回的内容，模型再强也答不出来。

| 项目 | 仓库 | 许可证 | 主语言 | 一句话定位 | 章 |
| --- | --- | --- | --- | --- | --- |
| RAGFlow | [infiniflow/ragflow](https://github.com/infiniflow/ragflow) | Apache-2.0 | Go | 以深度文档解析构建上下文，再由 Agent 基于证据作答 | 10、26、27、28、30 |

### 代码智能体与仓库自治

让 Agent 在真实代码仓库里读写并跑验证。共同代价是**必须先解决沙箱与权限边界**，否则一次误操作就是事故。

| 项目 | 仓库 | 许可证 | 主语言 | 一句话定位 | 章 |
| --- | --- | --- | --- | --- | --- |
| Aider | [Aider-AI/aider](https://github.com/Aider-AI/aider) | Apache-2.0 | Python | 编码场景里的另一条路——每步提交一个 git checkpoint，失败直接回滚，用"可回滚"代替"抛异常" | 9、11、37 |
| OpenHands | [All-Hands-AI/OpenHands](https://github.com/All-Hands-AI/OpenHands) | MIT | TypeScript | 在 Docker 沙箱里执行模型写的代码与命令，是"沙箱是硬要求"的工程样板 | 9、31、33、36、37 |
| Claude Code | [anthropics/claude-code](https://github.com/anthropics/claude-code) | 专有商业条款 | TypeScript | 权限确认与执行前拦截，可作为工具审批设计的参照 | 31、32、33、36、37 |
| OpenCode | [anomalyco/opencode](https://github.com/anomalyco/opencode) | MIT | TypeScript | Go 写的终端编码智能体，模型无关且客户端-服务端分离 | 37 |
| Cline | [cline/cline](https://github.com/cline/cline) | Apache-2.0 | TypeScript | IDE 侧边栏智能体，每步 git checkpoint 可回滚 | 37 |

### 可观测性与追踪

记录每次调用的实际输入输出，用于定位问题与算账。共同代价是**采集端要做脱敏**，且 trace 量级远大于 SLI 需要的量。

| 项目 | 仓库 | 许可证 | 主语言 | 一句话定位 | 章 |
| --- | --- | --- | --- | --- | --- |
| LangSmith SDK | [langchain-ai/langsmith-python](https://github.com/langchain-ai/langsmith-python) | MIT | Python | 追踪、数据集与评估。图编排让每一步可回放，追踪能把"这次跑成了什么样"落成可查询的数据，两者是一套的 | 3、8、10、11、12、32、33、34、35、36 |
| OpenLLMetry | [traceloop/openllmetry](https://github.com/traceloop/openllmetry) | Apache-2.0 | Python | 基于 OpenTelemetry 语义的 LLM 追踪 | 25、31、33、35 |
| AgentOps | [AgentOps-AI/agentops](https://github.com/AgentOps-AI/agentops) | MIT | Python | 面向 Agent 运行的追踪与成本看板，适合对照"把成本按角色归因"怎么做 | 25、32、33、36 |
| Langfuse | [langfuse/langfuse](https://github.com/langfuse/langfuse) | MIT(核心) + EE 目录企业许可 | TypeScript | 自托管的 Agent 追踪、评估与提示管理平台 | 31、33、34、35、36 |
| Phoenix | [Arize-ai/phoenix](https://github.com/Arize-ai/phoenix) | Elastic License 2.0 | Python | 追踪与回放，用于验证"出问题时能查到什么" | 31、33、34、36 |
| OTel GenAI 语义约定 | [open-telemetry/semantic-conventions](https://github.com/open-telemetry/semantic-conventions) | Apache-2.0 | Jinja | 让"模型指纹"、token 与延迟字段在不同后端之间可比 | 33、36 |

### 评测与基准

给 Agent 出一道可判分的卷子。共同代价是**公开基准只能当体检用**，上线判据只能来自自建任务集。

| 项目 | 仓库 | 许可证 | 主语言 | 一句话定位 | 章 |
| --- | --- | --- | --- | --- | --- |
| SWE-bench | [SWE-bench/SWE-bench](https://github.com/SWE-bench/SWE-bench) | MIT | Python | 真实 GitHub issue 的修复成功率基准 | 34、37 |
| tau-bench | [sierra-research/tau-bench](https://github.com/sierra-research/tau-bench) | MIT | Python | 模拟用户与领域规则下的多轮客服任务评测 | 34 |
| WebArena | [web-arena-x/webarena](https://github.com/web-arena-x/webarena) | Apache-2.0 | Python | 在真实可复现网站上完成浏览下单等自主任务 | 34 |
| OSWorld | [xlang-ai/OSWorld](https://github.com/xlang-ai/OSWorld) | Apache-2.0 | Python | 真实桌面环境中多模态 GUI 操作任务评测 | 34、38 |
| MCPBench | [zyy07/MCPBench](https://github.com/zyy07/MCPBench) | Apache-2.0 | Python | 同模型同配置下横向对比 MCP 服务器质量 | 34 |

### 计算机使用与 GUI

没有结构化接口时，退到操作像素。共同代价是**慢且脆弱**，能用 API 或 DOM 就别用像素。

| 项目 | 仓库 | 许可证 | 主语言 | 一句话定位 | 章 |
| --- | --- | --- | --- | --- | --- |
| browser-use | [browser-use/browser-use](https://github.com/browser-use/browser-use) | MIT | Python | 用无障碍树给 LLM 描述页面元素，自主点击输入完成任务 | 31、32、38 |
| Stagehand | [browserbase/stagehand](https://github.com/browserbase/stagehand) | MIT | TypeScript | 把 act/observe/extract 与确定性 Playwright API 混搭 | 31、32、38 |
| UI-TARS Desktop | [bytedance/UI-TARS-desktop](https://github.com/bytedance/UI-TARS-desktop) | Apache-2.0 | TypeScript | 基于 UI-TARS 视觉模型的本地 GUI 智能体 | 38 |
| OmniParser | [microsoft/OmniParser](https://github.com/microsoft/OmniParser) | CC-BY-4.0 | Jupyter Notebook | 把屏幕截图解析成结构化可交互元素，支撑纯视觉 GUI 智能体 | 38 |
| Playwright | [microsoft/playwright](https://github.com/microsoft/playwright) | Apache-2.0 | TypeScript | 跨 Chromium/Firefox/WebKit 的统一 API 与自动等待机制 | 38 |
| Skyvern | [Skyvern-AI/skyvern](https://github.com/Skyvern-AI/skyvern) | AGPL-3.0 | Python | 视觉模型直接看渲染页面，用多智能体群完成表单类流程 | 38 |

### 协议与互操作

让 Agent 与 Agent、Agent 与外部工具互相认识。共同代价是**每加一层就多一套鉴权、超时与版本协商**。

| 项目 | 仓库 | 许可证 | 主语言 | 一句话定位 | 章 |
| --- | --- | --- | --- | --- | --- |
| MCP TypeScript SDK | [modelcontextprotocol/typescript-sdk](https://github.com/modelcontextprotocol/typescript-sdk) | 存量 MIT，新贡献 Apache-2.0 | TypeScript | 官方 TS SDK，main 为 v2，对应 2026-07-28 规范 | 18 |
| A2A Protocol | [a2aproject/A2A](https://github.com/a2aproject/A2A) | Apache-2.0 | Shell | Linux Foundation 治理的 Agent 间通信协议，含 Agent Card 与 Task 两套核心对象 | 20、24、39 |
| MCP Python SDK | [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) | MIT | Python | MCP 的 Python 侧 client/server 实现，可对照官方类划分理解对象模型 | 24 |
| A2A JS SDK | [a2aproject/a2a-js](https://github.com/a2aproject/a2a-js) | Apache-2.0 | TypeScript | 两套官方 SDK，委托、状态推进与产物的落地细节看这里 | 24 |
| Agent Network Protocol | [agent-network-protocol/AgentNetworkProtocol](https://github.com/agent-network-protocol/AgentNetworkProtocol) | Apache-2.0 | HTML | 面向 Agent 网络的协议思路，把"Agent 在网络上互相发现与协作"这件事推到更远一层 | 24 |
| ACP (Agent Communication Protocol) | [i-am-bee/acp](https://github.com/i-am-bee/acp) | Apache-2.0 | Python | 另一套面向 Agent 间通信的开放协议方案，可以拿来对照 A2A 在任务模型上的取舍 | 24 |
| AG-UI | [ag-ui-protocol/ag-ui](https://github.com/ag-ui-protocol/ag-ui) | MIT | TypeScript | 事件驱动的 Agent-前端协议，约 16 类标准事件 | 39 |

### TypeScript 生态

前端团队直接在浏览器与边缘运行时上做 Agent。共同代价是**生态比 Python 侧薄**，且类型契约要自己守住。

| 项目 | 仓库 | 许可证 | 主语言 | 一句话定位 | 章 |
| --- | --- | --- | --- | --- | --- |
| Vercel AI SDK | [vercel/ai](https://github.com/vercel/ai) | Apache-2.0 | TypeScript | 模型供应商抽象加流式 UI 工具包，含 streamText / generateText / useChat，是应用工具包而非编排框架 | 18 |

## 选型时容易被忽略的三件事

**一、许可证的差别会在组合时放大。** 单看一个仓库的许可证意义有限：AutoGen 是 `LICENSE-CODE` 为 MIT、根目录 `LICENSE` 为 CC-BY-4.0 的双许可；Mastra 的 `ee/` 目录另有条款；Langfuse 核心是 MIT、企业版目录另计；Phoenix 是 Elastic License 2.0（不是 MIT）；Dify 是 Apache-2.0 加附加条件（GitHub 因此报 NOASSERTION）；Skyvern 是 AGPL-3.0。当你把两个项目放进同一条链路时，取严不取宽——AGPL 传染性条款遇到闭源商业集成就是硬伤。**动笔前先把链路上的每个仓库的 LICENSE 原文拉下来读一遍。**

**二、维护活跃度要看提交频率，也要看 issue 关闭情况。** 框架的核心能力半年不变，但适配层（新的模型 API、新的协议版本）会持续消耗维护者精力。一个半年没动的适配层，意味着新模型上线时你得自己打补丁。项目是否归档也要看——本书正文提到 Roo Code 的仓库时已核实其处于归档状态。

**三、生态锁定成本在迁移那天才显现。** LangChain 生态的 LangGraph、LangMem、LangSmith 分属不同仓库但共享同一套抽象习惯；AutoGen 与 Semantic Kernel 融合成 Microsoft Agent Framework 时也是同理。选型时问一句：**如果这个项目明年不维护了，我的代码要改多少？** 把 Agent 的控制流、状态、工具契约这几层握在自己手里、把框架只当编排层用，是降低这个成本的常见做法。
