# 《开源 Agent 架构技术全书》

> 面向"准备自己动手造 Agent"的工程师的中文技术汇总书。
> 调研日期：2026 年 11 月。开源项目迭代很快，涉及具体 API 与版本时请以官方仓库为准。

## 本项目包含什么

| 目录 | 内容 |
| --- | --- |
| `vol1/` | **第一卷 · 架构原理与开源实现全景**（40 章 / 7 卷），每章固定七节结构 |
| `vol2/` | **第二卷 · 自建 Agent 实践手册**（14 章，主线工程 `dragon-agent/`） |
| `code/` | 与第一卷配套的可运行 Python 示例，mock 模式默认可离线运行，`--live` 切换真实 API |
| `dragon-agent/` | **第二卷的主线工程**：纯标准库实现的 Agent 框架，14 个模块 + 确定性测试 |
| `_plan/` | 本书的章节规格表与工作流脚本（可追溯的架构设计底稿） |

## 第一卷 · 架构原理与开源实现全景

### 导论：什么是 Agent，以及为什么需要架构

| 章 | 标题 | 配套代码 |
| --- | --- | --- |
| 01 | [Agent 的定义、边界与心智模型](vol1/part-0/ch01-definition.md) | — |
| 02 | [Agent 系统的最小解剖：四回路架构](vol1/part-0/ch02-anatomy.md) | [`ch02-anatomy/`](code/ch02-anatomy/) |
| 03 | [为什么需要架构：可靠性、复利与工程约束](vol1/part-0/ch03-why-architecture.md) | — |

### 单体 Agent 的核心模式

| 章 | 标题 | 配套代码 |
| --- | --- | --- |
| 04 | [ReAct：推理与行动交替的经典范式](vol1/part-1/ch04-react.md) | [`ch04-react/`](code/ch04-react/) |
| 05 | [提示增强技术：CoT、Few-shot、Self-ask 与 Least-to-Most](vol1/part-1/ch05-prompting.md) | [`ch05-prompting/`](code/ch05-prompting/) |
| 06 | [反思与自纠错：Reflexion、Self-Refine 与独立评审者](vol1/part-1/ch06-reflection.md) | [`ch06-reflection/`](code/ch06-reflection/) |
| 07 | [规划范式：Plan-and-Execute、ReWOO 与搜索型推理](vol1/part-1/ch07-planning.md) | [`ch07-planning/`](code/ch07-planning/) |
| 08 | [工具调用契约：把 Function Calling 当作 API 工程](vol1/part-1/ch08-tool-calling.md) | [`ch08-tool-calling/`](code/ch08-tool-calling/) |
| 09 | [代码即行动：Code-Act 与 Program-of-Thought](vol1/part-1/ch09-code-act.md) | [`ch09-code-act/`](code/ch09-code-act/) |
| 10 | [上下文工程：把上下文当作稀缺资源预算](vol1/part-1/ch10-context-engineering.md) | [`ch10-context-engineering/`](code/ch10-context-engineering/) |
| 11 | [停止准则、步数上限与成本守卫](vol1/part-1/ch11-stopping.md) | [`ch11-stopping/`](code/ch11-stopping/) |

### 开源编排框架横向解剖

| 章 | 标题 | 配套代码 |
| --- | --- | --- |
| 12 | [LangChain 与 LangGraph：状态图式编排范式](vol1/part-2/ch12-langgraph.md) | [`ch12-langgraph/`](code/ch12-langgraph/) |
| 13 | [LlamaIndex：数据驱动的 Agent 范式](vol1/part-2/ch13-llamaindex.md) | [`ch13-llamaindex/`](code/ch13-llamaindex/) |
| 14 | [AutoGen 与 OpenAI Agents SDK：对话式多 Agent 与轻量编排两条路线](vol1/part-2/ch14-autogen-agentsdk.md) | [`ch14-autogen-agentsdk/`](code/ch14-autogen-agentsdk/) |
| 15 | [CrewAI：角色—任务—流程心智模型](vol1/part-2/ch15-crewai.md) | [`ch15-crewai/`](code/ch15-crewai/) |
| 16 | [PydanticAI、Agno 与 smolagents：类型驱动与轻量路线](vol1/part-2/ch16-pydanticai-agno-smolagents.md) | [`ch16-light-frameworks/`](code/ch16-light-frameworks/) |
| 17 | [Semantic Kernel 与微软 Agent 栈](vol1/part-2/ch17-semantic-kernel.md) | — |
| 18 | [TypeScript 生态：Mastra、Vercel AI SDK 与 Agents SDK JS](vol1/part-2/ch18-ts-ecosystem.md) | [`ch18-ts-ecosystem/`](code/ch18-ts-ecosystem/) |
| 19 | [框架选型决策树与十个常见反模式](vol1/part-2/ch19-framework-selection.md) | — |

### 多 Agent 协作体系

| 章 | 标题 | 配套代码 |
| --- | --- | --- |
| 20 | [多 Agent 的动机、分工理论与何时不该上多 Agent](vol1/part-3/ch20-multi-agent-motivation.md) | — |
| 21 | [对话式协作：GroupChat 与 CAMEL 角色扮演](vol1/part-3/ch21-conversation-collab.md) | [`ch21-conversation-collab/`](code/ch21-conversation-collab/) |
| 22 | [SOP 流水线：MetaGPT、ChatDev 与 AgentVerse 的软件公司隐喻](vol1/part-3/ch22-sop-pipeline.md) | [`ch22-sop-pipeline/`](code/ch22-sop-pipeline/) |
| 23 | [协作拓扑与调度：Supervisor、层级、网状、辩论与黑板](vol1/part-3/ch23-orchestration-topology.md) | [`ch23-topology/`](code/ch23-topology/) |
| 24 | [Agent 间通信协议：MCP、A2A 与内部消息设计](vol1/part-3/ch24-inter-agent-protocol.md) | [`ch24-inter-agent-protocol/`](code/ch24-inter-agent-protocol/) |
| 25 | [多 Agent 失效模式与成本治理](vol1/part-3/ch25-multi-agent-failures.md) | [`ch25-multi-agent-failures/`](code/ch25-multi-agent-failures/) |

### 记忆、上下文与状态

| 章 | 标题 | 配套代码 |
| --- | --- | --- |
| 26 | [记忆分类学：工作、情景、语义与程序性记忆](vol1/part-4/ch26-memory-taxonomy.md) | [`ch26-memory-taxonomy/`](code/ch26-memory-taxonomy/) |
| 27 | [长期记忆实现：向量、摘要与实体图谱三条路线](vol1/part-4/ch27-long-term-memory.md) | [`ch27-long-term-memory/`](code/ch27-long-term-memory/) |
| 28 | [上下文窗口工程：检索、压缩、淘汰与分层](vol1/part-4/ch28-context-windowing.md) | [`ch28-context-windowing/`](code/ch28-context-windowing/) |
| 29 | [检查点、回放与多 Agent 共享状态](vol1/part-4/ch29-checkpoint-replay.md) | [`ch29-checkpoint-replay/`](code/ch29-checkpoint-replay/) |
| 30 | [Agentic RAG：让检索从预处理变成主动决策](vol1/part-4/ch30-agentic-rag.md) | [`ch30-agentic-rag/`](code/ch30-agentic-rag/) |

### 工程化：安全、可观测、评测与成本

| 章 | 标题 | 配套代码 |
| --- | --- | --- |
| 31 | [沙箱与执行隔离](vol1/part-5/ch31-sandbox.md) | [`ch31-sandbox/`](code/ch31-sandbox/) |
| 32 | [提示注入与越权防护](vol1/part-5/ch32-prompt-injection.md) | [`ch32-prompt-injection/`](code/ch32-prompt-injection/) |
| 33 | [可观测性：Trace、Span 与 Agent 专属指标](vol1/part-5/ch33-observability.md) | [`ch33-observability/`](code/ch33-observability/) |
| 34 | [评测体系：基准、任务集、LLM-as-Judge 与回归测试](vol1/part-5/ch34-evaluation.md) | [`ch34-evaluation/`](code/ch34-evaluation/) |
| 35 | [成本与性能优化](vol1/part-5/ch35-cost-performance.md) | [`ch35-cost-performance/`](code/ch35-cost-performance/) |
| 36 | [生产化检查清单与真实故障模式](vol1/part-5/ch36-production-checklist.md) | — |

### 前沿范式：代码智能体、GUI Agent、协议生态与自举

| 章 | 标题 | 配套代码 |
| --- | --- | --- |
| 37 | [代码智能体与仓库级自治](vol1/part-6/ch37-coding-agents.md) | [`ch37-coding-agents/`](code/ch37-coding-agents/) |
| 38 | [计算机使用与 GUI Agent](vol1/part-6/ch38-computer-use.md) | [`ch38-computer-use/`](code/ch38-computer-use/) |
| 39 | [协议与生态全景：MCP、A2A、AG-UI、Skills 与插件体系](vol1/part-6/ch39-protocol-ecosystem.md) | [`ch39-protocol-ecosystem/`](code/ch39-protocol-ecosystem/) |
| 40 | [自举与自我改进：AutoGPT 的教训与演化式改进](vol1/part-6/ch40-self-improvement.md) | — |

### 前置材料与附录

- [序言](vol1/00-序言.md)
- [阅读路线图](vol1/00-阅读路线图.md)
- [附录 A · 主流框架架构对比](vol1/appendix/A-主流框架架构对比.md)
- [附录 B · 开源项目速查表](vol1/appendix/B-开源项目速查表.md)
- [附录 C · 术语表](vol1/appendix/C-术语表.md)
- [附录 D · 全书索引](vol1/appendix/D-全书索引.md)
- [附录 E · 主编终审报告](vol1/appendix/E-主编终审报告.md)
- [附录 F · 第二卷主编终审报告](vol1/appendix/F-第二卷主编终审报告.md)

## 每章的统一体例

1. 本章要解决的问题 — 用具体场景或反例开场
2. 原理：机制拆解 — 拆到能照着实现的颗粒度
3. 为什么这样设计：取舍与失效条件 — 什么时候**不该**用这个方案
4. 动手：最小可实现 — 可运行的最小实现
5. 开源实现对照 — 真实项目的做法与差异
6. 坑与检查清单 — 可勾选的落地要点
7. 小结与延伸阅读 — 真实可访问的 URL

## 配套代码怎么跑

```powershell
# 单个示例（默认 mock 模式，不需要 API Key、不联网）
python .\code\ch04-react\main.py

# 切换到真实 API（需自行配置环境变量）
python .\code\ch04-react\main.py --live

# 批量跑通全部示例
python .\code\run_all.py
```

## 第二卷 · 自建 Agent 实践手册

> 准备动手写一个自己的 Agent 的工程师；读完第一卷后进入实践

第二卷沿着一条主线推进：**把 `dragon-agent` 这个工程从零搭出来**。14 章既是教程也是实现说明——正文里的每一段代码都能在工程目录里找到对应文件。

| 章 | 标题 | 对应工程模块 |
| --- | --- | --- |
| 01 | [立项：把想法变成一份可执行的需求](vol2/p01-scoping/scoping.md) | `specs/` |
| 02 | [工程骨架：目录、配置与错误处理约定](vol2/p02-skeleton/skeleton.md) | `config.py` |
| 03 | [模型接入抽象：让 mock 与真实 API 共存](vol2/p03-llm-client/llm-client.md) | `llm.py` |
| 04 | [工具层：注册、校验、错误回灌与副作用](vol2/p04-tool-layer/tool-layer.md) | `tools.py` |
| 05 | [上下文装配器：把提示词变成资源分配](vol2/p05-context-assembler/context-assembler.md) | `context.py` |
| 06 | [Agent 主循环：带预算守卫的 ReAct](vol2/p06-agent-loop/agent-loop.md) | `agent.py, state.py` |
| 07 | [状态与检查点：让任务可回放、可恢复](vol2/p07-state-checkpoint/state-checkpoint.md) | `state.py` |
| 08 | [记忆模块：把"记住"做成可控的读写](vol2/p08-memory/memory.md) | `memory.py` |
| 09 | [护栏与权限：能力最小化](vol2/p09-guardrails/guardrails.md) | `guardrails.py` |
| 10 | [编排层：从单 Agent 到多 Agent 的升级路径](vol2/p10-orchestration/orchestration.md) | `orchestration.py` |
| 11 | [协议接入：把你的 Agent 变成 MCP Server 与 A2A 节点](vol2/p11-protocols/protocols.md) | `mcp_server.py` |
| 12 | [可观测性：接上 trace、指标与成本账](vol2/p12-observability/observability.md) | `observability.py` |
| 13 | [评测与回归：让改动有依据](vol2/p13-evaluation/evaluation.md) | `evaluation.py` |
| 14 | [上线：容器化、成本控制与值班手册](vol2/p14-production/production.md) | `cli.py` |

### 主线工程 `dragon-agent/`

纯标准库实现的 Agent 框架，无第三方依赖，mock 模式离线可跑，`--live` 切真实 API。

```powershell
# 跑完整测试（不依赖网络）
cd .\dragon-agent; python -m unittest discover -s tests

# 跑一次完整回路
python -m dragon_agent.cli demo

# 列出可用工具
python -m dragon_agent.cli tools
```
