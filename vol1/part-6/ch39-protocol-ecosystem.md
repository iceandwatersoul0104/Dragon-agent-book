# 第 39 章 协议与生态全景：MCP、A2A、AG-UI、Skills 与插件体系

## 一、本章要解决的问题

一个团队把自己的 Agent 能力摊在四个地方：工具函数直接写在业务代码里、内部规范塞进一段长 prompt、子代理用私有消息格式、前端靠轮询一个 status 接口。接第二个数据源时，这四套接口四种鉴权方式，联调花掉三周；接第三个时才发现一个更麻烦的问题——"这个 Agent 到底会什么"没法描述给外面。

接下来是三个绕不开的问题：

1. 外部系统怎么把"我能调哪些工具、能读哪些数据"告诉我的 Agent？
2. 我要委托另一个团队（甚至另一个供应商）的 Agent 干活，怎么描述任务、怎么知道它做完了？
3. 前端怎么在 Agent 干活时看见过程，而不是盯一个转圈？

三个问题落在三个不同的层上，各自需要一套约定。于是有了协议栈。

## 二、原理：机制拆解

### 2.1 五层分层图

```
  用户交互层    AG-UI（Agent User Interaction Protocol）
                 Agent -> 前端的事件流（文本 / 工具卡片 / 状态）
       ^
  能力封装层    Skills / 子代理 / 插件
                 把"怎么做"打包成可分发的资产
       ^
  委托执行层    A2A（Agent2Agent）
                 Agent <-> Agent（发现、委派、任务状态、artifact）
       ^
  工具上下文层  MCP（Model Context Protocol）
                 Agent <-> 工具与数据（tools / resources / prompts）
       ^
  模型 API 层   消息 / 工具调用 / 多模态 / 流式——唯一无法绕开的一层
```

这张图是分工，不是流水线。实际系统里 MCP 服务器常常同时被 Agent 和 Skills 调用；A2A 对端的 Agent 内部又是 MCP 客户端；插件则把多个层的资产打成同一个包。

每一层只回答一个问题：模型层回答"这一轮怎么说话"，MCP 回答"能力从哪来"，A2A 回答"活交给谁"，AG-UI 回答"过程怎么看"，能力封装层回答"经验怎么沉淀"。层次划清楚的最大好处是出问题时能定位到层，而不是笼统地说"AI 不好使"。

### 2.2 模型 API 层

最短也最没法替换的一层：一次对话的形状是消息 + 工具调用 + 流式输出。这一层不管能力从哪来，只管"这一轮我说了什么、调了什么、拿到什么"。

它也是协议化程度最低但最稳定的一层：厂商接口在细节上一直在变（消息块怎么合并、工具结果怎么回填、多模态怎么编码），但抽象形状几年没变。所以往上盖协议的时候，别把模型厂商的私有字段漏进协议里——那是换模型时最容易炸的地方。

### 2.3 MCP：把工具和上下文标准化

**规范现状（2026 年 11 月核实）**：最新稳定版是 `2026-07-28` 修订版，规范站在 modelcontextprotocol.io，仓库是 modelcontextprotocol/modelcontextprotocol。2025 年 12 月 9 日，Anthropic 把 MCP 捐给 Linux Foundation 下的 Agentic AI Foundation（AAIF），与 Block 的 goose、OpenAI 的 AGENTS.md 并列为创始项目，治理保持厂商中立。捐赠公告时的官方口径是每月 9700 万+ SDK 下载、10000 个活跃公共服务器，ChatGPT、Claude、Cursor、Gemini、Microsoft Copilot、Visual Studio Code 都有第一方支持；此外有官方 SDK、Inspector 调试器和官方 Registry。

**核心抽象**是三种原语加两种传输：

| 原语 | 是什么 | 谁决定用不用 |
|---|---|---|
| tools | 有副作用的动作：查、发、改 | 模型决定 |
| resources | 只读数据：文件、记录、文档 | 应用或模型决定 |
| prompts | 可复用的模板化入口 | 用户或应用决定 |

传输走 stdio（本地进程）或 Streamable HTTP（远程服务）。三种原语的区分很实际：把"读数据"伪装成 tool，模型就会拿它当动作反复调用；把"动作"做成 resource，模型永远不会主动调它。

**2026-07-28 这一版最值得记的四件事**：

1. **无状态化**：协议层的会话与初始化握手被移除，客户端可以先调 `server/discover` 了解对方支持哪些版本与能力，服务端因此能水平扩展，不再需要粘性会话或共享会话存储。
2. **多轮往返模式（MRTR，SEP-2322）**：以前是服务端发起请求（要参数、要用户输入）并等待，现在服务端直接返回 `resultType: "input_required"` 和 `inputRequests`，客户端把信息放进 `inputResponses` 后**重试原请求**。所有结果都必须带 `resultType`（`complete` 或 `input_required`）。服务端不再需要为挂起的请求维持状态。
3. **Tasks 移出核心**：进入官方扩展 `io.modelcontextprotocol/tasks`，用 `tasks/get` 轮询加 `tasks/update` 取代阻塞式的 `tasks/result`。
4. **弃用与生命周期**：Roots、Sampling、Logging 三个特性被标记弃用（SEP-2577），HTTP+SSE 传输也弃用——官方给的迁移方向很具体：目录和文件改用工具参数或资源 URI 传，采样改成直接调模型厂商 API，日志打到 stderr 或用 OpenTelemetry。同时确立了特性生命周期（Active / Deprecated / Removed）、至少 12 个月的弃用窗口和弃用特性登记表。

还有两条工程细节值得注意：`tools/list` 这类结果带 `ttlMs` 与 `cacheScope`，服务端**应当按确定性顺序**返回工具列表，因为工具顺序影响 prompt 缓存命中率；错误码也做了分区，`-32020` 到 `-32099` 保留给 MCP 规范自己用。

### 2.4 A2A：把"委托"标准化

**规范现状（核实）**：2025 年 4 月 9 日由 Google 发布，2025 年 6 月 23 日捐给 Linux Foundation，创始成员包括 AWS、Cisco、Google、Microsoft、Salesforce、SAP、ServiceNow，官方称已有超过 100 家科技公司支持。2026 年 3 月发布 v1.0，当前最新 release 是 v1.0.1。许可 Apache-2.0，官方 SDK 覆盖 Python、JavaScript、Java、C#、Go、Rust。

**核心抽象**三件套：Agent Card（能力名片：对方是谁、会哪些技能、在哪）、Task（一次委派的生命周期：submitted → working → completed）、Artifact（结果载体）。关键约束是**对方始终不透明**：不暴露内部记忆、工具和专有逻辑，只暴露"能做什么"和"做出来什么"。v1.0 阶段补上了两块：Signed Agent Card（加密身份验证，多 Agent 信任链的底座）和面向 Web 的架构（含 `application/a2a+json` 媒体类型、gRPC / JSON-RPC / HTTP+JSON 多种绑定）。

与 MCP 的关系一句话说清：**MCP 是 agent 对工具，A2A 是 agent 对 agent**，两者互补而不是竞争。

委托的安全边界靠三样东西兜住：Agent Card 里声明的能力范围（对方承诺能做什么）、任务级的输入输出约定（发什么、收什么 artifact）、以及 v1.0 引入的签名卡片（对方到底是谁）。缺一样，跨组织的委派就只是"把请求转给一个自己看不见的黑盒"。

### 2.5 AG-UI：把"过程"标准化

**规范现状（核实）**：仓库 ag-ui-protocol/ag-ui，MIT，最新发布 2026 年 11 月 6 日，官方 SDK 有 TypeScript、Python、.NET，规范目录下同时有 `spec/1.0` 与 `spec/draft`。

它的定位是 Agent 与前端之间的事件流约定，前端只按事件类型渲染，不需要理解 Agent 内部。官方 EventType 枚举目前有 29 个类型，可按族分：

- **运行与步骤**：RUN_STARTED / RUN_FINISHED / RUN_ERROR、STEP_STARTED / STEP_FINISHED；
- **文本**：TEXT_MESSAGE_START / _CONTENT / _END；
- **工具调用**：TOOL_CALL_START / _ARGS / _END / _RESULT，靠 `toolCallId` 把参数和结果对上；
- **状态**：STATE_SNAPSHOT（全量快照）、STATE_DELTA（**RFC 6902 JSON Patch** 增量）、MESSAGES_SNAPSHOT、ACTIVITY_SNAPSHOT / _DELTA；
- **推理**：REASONING_* 一族，旧的 THINKING_* 已弃用并将在 1.0.0 移除；
- **子代理**：SUBAGENT_STARTED / _FINISHED / _ERROR——大多数事件都能带 `subagentRunId` 做归属（RUN_* 除外，它描述整次运行）；
- **逃生舱**：RAW 与 CUSTOM。

公共字段是 `type` / `timestamp` / `rawEvent` / `metadata`。

事件流的价值在于把前端的集成成本从"每次改 Agent 内部结构都要改前端"压到"前端只认这二十多种事件"。子代理归属字段尤其实用：一次运行里既有主 Agent 的输出又有子代理的输出，带上 `subagentRunId` 就能分开展示，不必等全部结束才知道是谁做的。

### 2.6 Skills、子代理与插件：把"能力"打包

**Agent Skills** 已由 Anthropic 作为开放标准发布。一个 skill 就是一个目录：根目录必须有 SKILL.md（YAML frontmatter 里 `name` 必填、≤64 字符；`description` 必填、≤1024 字符，可选 `allowed-tools`、`model`），可以再带 `scripts/`、`references/`、`assets/`。

核心是**三级渐进披露**：

| 级别 | 内容 | 何时进上下文 | 量级 |
|---|---|---|---|
| 一 | name + description | 启动时全部加载 | 每个 skill 约百来个 token |
| 二 | SKILL.md 正文 | 命中触发时 | 建议控制在 500 行以内 |
| 三 | scripts / references / assets | 正文指向时才读 | 脚本经 shell 执行，只有输出进上下文 |

这一设计让"装一百个 skill"不会撑爆上下文：没触发的 skill 永远只占一句话。官方仓库是 anthropics/skills，安装方式是把 skill 目录复制或软链到目标 agent 的 skills 目录（npm 上的 `skills` 包提供了 `npx skills add owner/repo/skill` 这样的命令）；Claude Code 放在 `~/.claude/skills/`（个人）或 `.claude/skills/`（项目），也可以通过插件分发。

官方对 Skills 与 MCP 的分工说得很直接：Skills 教 Agent 更复杂的工作流程，而这些工作流程会用到 MCP 工具。一个负责"怎么做"，一个负责"能用什么"。

**子代理**买的是另一样东西：隔离的上下文窗口和独立的工具集，换来并行执行与上下文隔离，代价是协调开销。和 Skills 的分工是：Skills 是"同上下文里的程序性知识"，子代理是"另起一个上下文干活的分身"。**AGENTS.md** 则是一直加载的项目事实，和条件加载的 Skill 正好互补。

**插件**是把 MCP 服务器、Skills、子代理（以及前端资源）打成一个可安装、可版本化的单元。它的问题也在这里：插件能带可执行代码，装插件接近于装依赖。

## 三、为什么这样设计：取舍与失效条件

**每层都有失效条件，选型的第一条是"你的问题到底在哪一层"**：

- **MCP** 不是所有外部能力都该套。只读查询很合适；高频写操作要另外考虑幂等与事务；一个进程内的内部 HTTP 服务套协议纯属自找麻烦。
- **A2A** 在同代码库内调子进程时是纯负担——进程内调用更快、更可控、可调试。跨组织、跨供应商、跨信任边界，它才值回票价。
- **AG-UI** 在纯后端场景没有意义；但只要有前端，自己发明一套私有 SSE 格式的长期维护成本很高——AG-UI 已经有 TypeScript / Python / .NET SDK 和现成的框架适配包（如 ag-ui-openai-agents），造轮子的理由更弱。
- **Skills** 在一次性任务上不划算（写 SKILL.md 的时间比直接打提示词还长）；只有"反复做、团队共享、需要版本化和评审"时才回本。
- **插件** 在能力只有一个来源时是过度设计。

**选型速查表**：

| 你的问题 | 该停在哪一层 |
|---|---|
| 想让模型调用我写的三个函数 | 先写函数；等出现第二套客户端再考虑 MCP |
| 把内部系统接进来、多个 Agent 共用 | MCP |
| 要把活交给另一个团队的 Agent | A2A |
| 前端要展示进度、工具卡片、流式文本 | AG-UI |
| 这套流程我要做一百遍，还要让别人复用 | 写成 Skill；要跨工具分发就打成插件 |

**组合成本**：每加一层，就多一套鉴权模型、多一套超时与重试语义、多一套版本协商。协议不是免费的，选协议之前先算这笔账。

还有一条经验：协议层的价值随参与者数量增长。一个客户端一个服务端，自己写函数最快；三个客户端要接同一个数据源，协议立刻回本；跨公司协作时，没有协议基本没法谈。

**最常见的错误是为了看起来规范而套协议**：内部三个接口用 MCP 连，内部两个 Agent 用 A2A 连，前端再自己发明一套事件格式——最后维护三套鉴权、三套错误处理、三套文档。

## 四、动手：最小可实现

配套实现是一个纯标准库的五层栈，模拟"用户问上个月（2026-10）工资条"的完整链路，路径 `code/ch39-protocol-ecosystem/`。

- **模型 API 层**：模型每轮只输出一个 JSON 动作；
- **MCP 层**：`server/discover` / `tools/list` / `tools/call`，第一次调用缺 `month` 参数 → 返回 `resultType=input_required` + `inputRequests` → 客户端重试原请求并带上 `inputResponses`（MRTR）；
- **A2A 层**：读对方 Agent Card → `tasks/send` → 任务状态流转 → 拿回 artifact，对方全程不透明；
- **AG-UI 层**：整条链路变成 23 个事件，含 TOOL_CALL_*、STATE_SNAPSHOT、SUBAGENT_*；
- **Skills 层**：启动只加载三个 skill 的 name+description，命中后才读正文——实际加载的 skill 只有一个。

两重校验：事件流一致性（START/END 配对、RUN 有终态）与事实一致性（最终金额必须来自 A2A 的 artifact，模型不许自己编）。

一次请求穿过五层，实际报文长这样：

| 时刻 | 层 | 发生了什么 |
|---|---|---|
| t0 | 模型 API | 模型输出 `{"action":"skill_index"}` |
| t1 | Skills | 3 个 skill 的 name+description 进上下文，正文一个都没进 |
| t2 | MCP | `tools/call` → `resultType=input_required`，`inputRequests=[month]` |
| t3 | MCP | 重试同一请求并带上 `inputResponses{month}` → `resultType=complete` |
| t4 | A2A | 读 Agent Card → `tasks/send` → completed → 拿到 artifact |
| t5 | AG-UI | 23 个事件，前端渲染出文本、工具卡片和状态快照 |

```bash
cd code/ch39-protocol-ecosystem
python main.py          # mock 模型跑通五层
python main.py --live   # 换真实模型，工具与协议不变
```

## 五、开源实现对照

| 项目 | 层 | 解决的问题 | 状态 | 许可 |
|---|---|---|---|---|
| modelcontextprotocol/modelcontextprotocol | MCP | 工具与数据接入 | 规范最新稳定版 2026-07-28 | 仓库许可字段为混合（NOASSERTION） |
| modelcontextprotocol/servers | MCP | 参考实现 | 持续更新 | 各服务器各自许可（混合） |
| modelcontextprotocol/typescript-sdk | MCP | 官方 SDK | TypeScript 为主 | 见仓库 LICENSE |
| a2aproject/A2A | A2A | Agent 间委派 | v1.0.1（2026-05-28） | Apache-2.0 |
| ag-ui-protocol/ag-ui | AG-UI | Agent 到前端的事件流 | 最新发布 2026-11-06 | MIT |
| anthropics/skills | Skills | 能力打包与开放标准 | Anthropic 发布的开放标准 | 仓库未声明统一许可 |
| openai/AGENTS.md | 项目事实 | 永远加载的项目约定 | AAIF 创始项目 | 以仓库为准 |

## 六、坑与检查清单

- [ ] 把进程内调用硬套成协议，徒增鉴权与延迟。
- [ ] 自己发明私有事件格式，前端与第三方 Agent 框架互不认识。
- [ ] MCP 客户端与服务端版本不匹配，字段悄悄对不上。
- [ ] 忽略 `resultType`，把 `input_required` 当成正常结果处理，参数永远补不齐。
- [ ] 忽略弃用通知，照着老教程实现 Roots / Sampling / HTTP+SSE。
- [ ] 工具列表每次顺序不同，prompt 缓存全部失效。
- [ ] 不给 TTL 与 cacheScope，客户端只能盲目轮询。
- [ ] Skill 的 `description` 只写"是什么"不写"何时用"，永远不触发。
- [ ] Skill 正文塞满几千行不分拆，触发即烧 token。
- [ ] 把外部 Agent 当可信来源直接采信，不校验 Agent Card 签名。
- [ ] 委托任务不定义完成条件，对端返回半成品也当成功。
- [ ] 插件能带可执行代码，却不审计就安装。
- [ ] 事件流没有终态，前端永远转圈。
- [ ] 每层各自定义错误码和重试语义，故障时无法定位是哪一层出的问题。

## 七、小结与延伸阅读

协议分层解决的是"谁跟谁说话"：模型管一轮对话，MCP 管工具与数据，A2A 管 Agent 之间，AG-UI 管过程可见，Skills 与插件管能力怎么被打包和分发。

选型的顺序永远是自下而上：先把内部几个函数写好，确认它真的成了瓶颈，再考虑抽成协议。为了规范而规范，是这一层最常见的浪费。

生态的现实是：MCP 已经进入主流平台的第一方支持，基础能力标准化基本定型；A2A 走到 v1.0，跨组织的 Agent 协作开始有据可依；AG-UI 仍在快速演进，事件类型还在变；Skills 则是渗透最快、争议最小的一层——因为它不要求任何一方改造运行时。

延伸阅读：

- MCP 规范：https://modelcontextprotocol.io/specification/2026-07-28/changelog
- MCP 规范仓库：https://github.com/modelcontextprotocol/modelcontextprotocol
- MCP 参考服务器：https://github.com/modelcontextprotocol/servers
- MCP Registry：https://modelcontextprotocol.io/registry/about
- A2A 规范：https://github.com/a2aproject/A2A （主页：https://a2a-protocol.org）
- AG-UI：https://github.com/ag-ui-protocol/ag-ui
- Agent Skills：https://github.com/anthropics/skills
