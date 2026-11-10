# 第 18 章 TypeScript 生态：Mastra、Vercel AI SDK 与 Agents SDK JS

## 一、本章要解决的问题

一个 Next.js 产品要加 AI 功能。需求很具体：聊天界面要边生成边显示，要能渲染"正在查询订单"这类中间状态，要能流式输出结构化结果，上线跑在边缘。

麻烦在前端和后端用同一种语言。Python 侧的 Agent 框架更成熟——LangGraph、CrewAI、smolagents、PydanticAI，工具、文档、中文资料、踩坑记录都在那边。但选 Python 就要单独部署一套模型服务，处理跨语言序列化，前后端两套类型、两套测试工具。

反过来，选 TS 也有真实代价：Python 在数据处理、数值计算、模型微调上的生态优势是压倒性的；执行不可信代码时，Node 没有权限模型这件事没有现成解。

所以这一章要回答的不是"TS 能不能做 Agent"，而是三个更具体的问题：**TS 侧生态现在长到了什么程度、它的机制和 Python 那几套有什么不同、什么时候该选哪边。**

## 二、原理：机制拆解

### 2.1 Vercel AI SDK：在模型和 UI 之间隔一层协议

`ai` 包分两层。

**Core 层**：`streamText` / `generateText` / `streamObject` / `generateObject` 四个生成函数；工具用 `tool({description, parameters, execute})` 定义，参数 schema 用 Zod 写——**同一份 schema 既用于校验，又生成模型看到的 JSON Schema**；`maxSteps` 控制工具循环的轮数；供应商有各自的包（`@ai-sdk/openai` 之类），换模型只改一个字符串。

**UI 层**：`useChat` / `useCompletion` / `useObject` 三个钩子，把流式响应变成 UI 状态。

关键在**协议**。服务端吐出来的不是一个字符串，是一串带类型的分片：

```
data: {"type":"text-delta","delta":"订单"}
data: {"type":"tool-call","toolCallId":"call_1","toolName":"lookup_order","args":{...}}
data: {"type":"tool-result","toolCallId":"call_1","result":"已发货..."}
data: {"type":"finish","usage":{...}}
```

`useChat` 本质是个状态机：收到 `text-delta` 就把文本拼进当前消息；收到 `tool-call` 就把它挂成当前消息的一个 part；收到 `finish` 才把消息标记为完成。

**关键细节：工具调用是消息的一个 part，不是独立消息。** 这一条决定了前端能不能把它渲染成一张"正在调用 lookup_order"的卡片，而不是一条突兀的系统消息。这是第 8 章的工具调用契约落到 UI 的最后一跳。

这套设计的三个后果：模型和 UI 之间只有协议这一层契约，换模型不动前端；前端需要的全部状态（文本、工具调用、工具结果、用量）都在协议里；想做得更花哨——让模型返回一段 UI 描述、前端直接渲染——也在协议的延长线上。

这最后一条叫生成式 UI（generative UI）。工具调用既然已经是消息的一部分，前端在渲染"正在调用 lookup_order"这张卡片时，顺手渲染模型返回的别的结构，用的是同一套机制——不需要另开一条通道。

### 2.2 Mastra：TS 原生的完整框架

五个核心：**Agent / Workflow / Memory / RAG / Eval**。

**RAG** 那一层把常见动作抽象成了动词：`.chunk()` 切块、`embed()` 嵌入、`.upsert()` 入库、`.query()` 检索、`rerank()` 重排——跨文档类型和向量库统一。第 13 章讲的那套数据层的东西，在这里是现成的。

**Agent** 是个构造对象，指令、工具、模型、记忆都是字段，`agent.generate()` / `agent.stream()` 两个入口。有个细节说明它的定位：**模型直接来自 AI SDK**（`model: openai('gpt-4-turbo')`）——Mastra 建在 AI SDK 之上，不重写供应商层。

**Workflow** 是显式控制流：`.step()` / `.then()` 串起来，`.branch()` 分支，`.after()` 汇合，`when: {...}` 写条件，`.suspend()` / `.resume()` 做挂起和恢复。比第 12 章的状态图轻——顺序 + 分支 + 挂起，没有归约那层抽象。代价是复杂的循环和状态合并它不擅长，那仍然是状态图的地盘。

**Memory** 分两块设计，这点值得单独记：**最近 N 条消息**（`lastMessages`）加**语义上相关的旧消息**（`semanticRecall`，配向量库和 embedding）。用 `resource`（用户或实体）和 `thread`（会话）两级标识分组。和第 16 章说的"会话记忆"是同一件事，只是 TS 侧把它做成了框架的一等公民。

**Eval** 是可插拔的打分器。准确性、忠实度、相关性各一个，内置了一批能直接用的，也能自己写。

**观测是内建的**：工作流和 Agent 都发 OTel span，不用自己埋点。

还有 **Mastra Dev**——本地开发环境，`npm run dev` 就能起，可以聊天、看状态和记忆、调试工具调用、看 trace 和评测结果，本地存储用 libsql，不用 Docker。

### 2.3 Agents SDK JS 与 MCP TypeScript SDK 的现状

**Agents SDK JS**（`@openai/agents`）是第 14 章那套东西的 TS 对应物：Agent、Handoff、Guardrail、Session、Tracing 概念一致，工具用 Zod schema 定义。它比 Python 版多两块：

- **Realtime Agent**：`@openai/agents/realtime`，WebRTC / WebSocket / SIP 三种传输，浏览器里直接语音对话。浏览器端另有单独的 `@openai/agents-realtime` 包，不把重依赖打进客户端 bundle。
- **Sandbox Agent**：给 Agent 配一个文件工作区和沙箱环境，跑长任务、跑命令、跨步骤保留工作区状态。

运行时要求要说清楚：Node.js 22+、Deno、Bun；Cloudflare Workers 是实验性支持（需要 `nodejs_compat`）。

**MCP TypeScript SDK** 是官方 TS SDK，main 分支已经是 v2，对应 2026-07-28 的规范。它的许可有点特别：存量代码是 MIT，新贡献走 Apache-2.0。

这一条对 TS 团队的意义不小：**MCP 在 TS 侧是一等公民**，工具接入方式从一开始就在架构底层，而不是事后补的适配器。

### 2.4 TS 的短板：沙箱与本地执行

这是 TS 侧最实在的短板，Python 那边没有对应的问题。

**Node 没有权限模型**。`child_process.spawn` 起的子进程：文件随便读、网络随便发、能再起进程、能读环境变量（密钥就在里面）。能限制的只有 `--max-old-space-size`（内存上限）和超时（杀掉进程）。而 `worker_threads` 看着像隔离，其实不是——它和主线程共享同一个进程，文件权限和环境变量一样共享。

四条出路：

| 方案 | 隔离强度 | 代价 |
| --- | --- | --- |
| Node 子进程 + 资源限制 | 弱 | 几乎没有 |
| Deno 权限模型 | 中 | 默认全禁、逐项 `--allow-read` / `--allow-write` / `--allow-net` / `--allow-run` 打开；但仍限不住内存 |
| WASM 沙箱 | 强 | 宿主 API 要显式绑定，要重写一批胶水 |
| 容器 / 托管沙箱 | 强且实用 | 多一层部署 |

现实答案是最后一行。Agents SDK JS 就是这么给的：本地沙箱客户端覆盖 macOS 和 Linux，Windows 走 Docker 或托管客户端——而沙箱 Agent 本身还是 beta。

**这正是 TS 相对 Python 的一处真实劣势**：Python 的 `subprocess` 加资源限制是一条更短的路径，数据和科学生态也压倒性地强。

## 三、为什么这样设计：取舍与失效条件

选 TS 的三个理由：

1. **全栈一致**。一种语言、一套类型、一个部署。模型服务、前端、后端的工具函数共享类型定义和测试工具——"前后端对不上字段"这类问题在编译期就能暴露；
2. **流式优先**。AI 产品的体验一半在流式。TS 侧从框架到钩子都是围着流式设计的；
3. **边缘部署**。AI SDK 最贴边缘运行时，在 Workers 上跑流式响应是原生路径。

选 Python 的三个理由：

1. **数据与科学**。pandas、numpy、各种训练与微调工具，TS 侧没有替代品；
2. **沙箱与本地执行**（上一节）；
3. **生态成熟度**。LangGraph、CrewAI、smolagents、PydanticAI 的第一手用例、中文资料、踩坑记录，Python 侧明显更多。

具体选型：

| 你的处境 | 选它 |
| --- | --- |
| 只是在产品里加一个聊天框 | Vercel AI SDK 就够，别上框架 |
| 要做 Agent 产品（记忆、工作流、评测） | Mastra |
| 要和 Python 侧已有服务共享一套图模型 | LangGraph.js |
| 需要 OpenAI 原生能力（语音、托管工具、托管沙箱） | Agents SDK JS |

**不该选 TS 的四种情况**：任务的主要工作在数据处理和数值计算；需要执行不可信代码但环境里没有容器可用；团队的数据与算法能力在 Python、TS 只是前端；要用的那个 Python 框架的成熟特性（比如检查点和时间旅行）在 TS 侧还没跟上。

一条经验：**TS 侧最容易犯的错，是拿前端框架的思路做 Agent。** 前端框架擅长的是"把状态映射成 UI"，而 Agent 真正的难点是"状态在多步之间怎么变、失败了从哪继续、怎么回滚"。这部分 TS 生态还在补齐——第 12 章讲的状态图在 TS 侧的对应物是 LangGraph.js，而它跟随 Python 版本，新特性总是晚一步。

还有一条：**先把交互层定下来，再选框架。** 流式协议长什么样、消息是什么形状、工具调用在界面上怎么显示——这三样定了，框架后面换成谁都不影响前端。反过来先选框架，很容易被框架的抽象牵着走，最后为了迁就框架改了本来挺好用的交互。

## 四、动手：最小可实现

配套代码在 `code/ch18-ts-ecosystem/`，`main.py` 用 909 行纯标准库复刻上面三块机制，**不需要 API Key 就能跑通完整回路**，内置 mock 模型。

依赖：Python 3.9 及以上，零第三方包，不需要 `pip install`。

```bash
cd code/ch18-ts-ecosystem
python main.py                    # 跑全部场景
python main.py --demo stream      # 只跑流式协议
python main.py --demo mastra      # 只跑 Mastra 四层
python main.py --demo sandbox     # 只跑沙箱策略对比
```

三个场景：

- **stream**：服务端吐 7 个协议分片，客户端 `useChat` 状态机还原出消息——重点看"工具调用是消息的一个 part"这件事怎么体现在数据上；
- **mastra**：工作流走到审批挂起、人工答复后继续往下跑；记忆把"最近消息"和"语义相关的旧消息"分开召回；评测打分器给出通过与否和理由；观测打出 span 树；
- **sandbox**：四种隔离策略对同一组六个操作各放行什么，最后点明"alloc_memory 一栏都是放行，但含义不同"。

几个关键机制在代码里长这样：

```python
# 1) 协议分片是唯一的契约
Chunk(type="text-delta", delta="订单")
Chunk(type="tool-call", tool_call_id="call_1", tool_name="lookup_order", args={...})

# 2) useChat 状态机：工具调用挂成当前消息的 part
if c.type in (TEXT_DELTA, TOOL_CALL):
    if self._current is None:
        self._current = Message(id=..., role="assistant")
    self._current.parts.append({...})

# 3) 挂起与恢复：恢复后要接着往下跑，不是重头再来
def resume(self, state, value, tracer):
    idx = state["__suspended_index"]
    out = self._run_step(self.steps[idx], state, value, tracer)
    return self._execute(state, idx + 1, None, tracer)
```

切真实模型加 `--live`，需要三个环境变量：

| 变量 | 必填 | 默认值 |
| --- | --- | --- |
| `LLM_API_KEY` | 是 | — |
| `LLM_BASE_URL` | 否 | `https://api.openai.com/v1` |
| `LLM_MODEL` | 否 | `gpt-4o-mini` |

live 模式下只有"模型返回什么"这一处换成真模型，**协议分片、客户端状态机、工作流挂起恢复、记忆召回、评测打分全部不变**——这正是要验证的：换模型之后，前端那一层的契约还稳不稳。

## 五、开源实现对照

- **Vercel AI SDK**（Apache-2.0，https://github.com/vercel/ai）：本章路线一的参考实现。模型供应商抽象 + 流式 UI 工具包，`streamText` / `generateText` / `useChat` / `useCompletion` 都在这里。它是"应用工具包"，不是 Agent 框架——不提供工作流、记忆、评测。
- **Mastra**（Apache-2.0，但 `ee/` 目录下的企业版组件另有许可，https://github.com/mastra-ai/mastra）：本章路线二的参考实现。Agent、Workflow、Memory、RAG、Eval 五件套，建在 AI SDK 之上，工作流内建 OTel 日志，本地开发环境用 libsql。
- **OpenAI Agents SDK JS**（https://github.com/openai/openai-agents-js）：第 14 章那套原语在 TS 侧的对应物，比 Python 版多了 Realtime（语音）和 Sandbox（文件工作区）两个方向。
- **MCP TypeScript SDK**（https://github.com/modelcontextprotocol/typescript-sdk）：官方 TS SDK，main 为 v2，对应 2026-07-28 规范；存量 MIT，新贡献 Apache-2.0。TS 侧工具接入的标准方式。
- **LangGraph.js**（https://github.com/langchain-ai/langgraphjs）：第 12 章状态图路线的 TS 移植版。能力对得上 Python 版，代价是跟随——新特性总是晚一步。

## 六、坑与检查清单

- [ ] 协议分片类型是前后端契约，改动要两边同步，最好先定版本。
- [ ] 工具调用挂成消息的 part，别另起一条消息——UI 才渲染得对。
- [ ] `useChat` 的状态机自己测一遍：断线重连、空响应、finish 丢失这三种情况都要覆盖。
- [ ] `maxSteps` 要设上限，工具循环失控是烧钱最常见的方式。
- [ ] Zod schema 里的字段描述就是模型看到的参数说明，认真写。
- [ ] Mastra 的 Memory 生产环境要换成 Postgres 之类的持久化后端，别用内存存储。
- [ ] 内存存储的 file URL 在 serverless 上会失败，第一次部署前先换掉。
- [ ] 语义召回的 topK 和消息范围要一起调，光看命中率会误判。
- [ ] 评测在 CI 里跑一个采样集，不要每次全量跑——评测本身也要钱。
- [ ] 观测接自己的 OpenTelemetry 后端，别只看框架自带的面板。
- [ ] 框架版本钉死：TS 侧发版快，破坏性变更频繁。
- [ ] 需要跑不可信代码时，确认环境里有容器或托管沙箱；Node 子进程不算隔离。
- [ ] `worker_threads` 不要当安全边界用，它和主线程共享进程。
- [ ] 环境变量别整体透传给子进程，密钥要单独剔除。
- [ ] 边缘部署前逐个依赖查运行时要求——向量库 SDK 常常要 Node 运行时。

## 七、小结与延伸阅读

TS 侧的生态这些年补得挺快，但补的顺序很能说明问题：**先补 UI（AI SDK 的协议与钩子），再补框架（Mastra 的四层），最后补沙箱（还是 beta）。** 这个顺序恰恰是产品需求出现的顺序——先是"聊天框要流畅"，然后是"这功能要能维护"，最后才是"要让它跑别人写的代码"。

三个机制值得记住：**协议分片是模型和 UI 之间唯一的契约**；**Agent / Workflow / Memory / Eval 是分层的，各管一段**；**记忆是最近消息加语义相关旧消息两块，不是单一缓冲区**。

选型上就一句话：**产品的交互形态决定语言，任务的工作内容决定语言。** 交互是聊天框、流式、要跟现有前端共享类型——选 TS。工作是数据清洗、数值计算、跑不可信代码——选 Python。两边都要，就拆开：交互层 TS，业务层 Python，中间用 MCP 或 HTTP 连——这恰好是 MCP 在 TS 侧做成一等公民的原因。

延伸阅读：

- Vercel AI SDK — https://github.com/vercel/ai
- Mastra — https://github.com/mastra-ai/mastra
- OpenAI Agents SDK JS — https://github.com/openai/openai-agents-js
- MCP TypeScript SDK — https://github.com/modelcontextprotocol/typescript-sdk
- LangGraph.js — https://github.com/langchain-ai/langgraphjs
