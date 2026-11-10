# 第 24 章 Agent 间通信协议：MCP、A2A 与内部消息设计

## 一、本章要解决的问题

去年我们接过一次退款对账的需求：客户号、订单号、金额，三个字段，全都要准。

我们的 Agent 跟公司的退款系统之间，用的是 MCP，接工具很顺。可到了"我的 Agent 要把整件事委托给对账 Agent"这一步，我们图省事，让两边用自然语言聊：

> Agent A：请给客户 C001 算一笔 200 元退款并核对台账。
> Agent B：好的，正在处理。
> Agent A：帮 C001 也算一下退款。

第三条消息里没写金额。Agent B 猜不出来，对话卡在那儿，最后是人工进去补了一句金额才继续。

事后复盘出的结论有点扎心：**Agent 之间传的不是话，是契约。** 自然语言能跑通的那部分，是运气好。

还有第二个问题让我们分不清方向：公司内部一堆系统通过 MCP 暴露工具，我们自己的 Agent 也想对外提供能力、能接受别人的委托。MCP 和 A2A 两个协议都在讲"Agent 怎么通信"，但它们根本不是一回事——一个是给 Agent 灌工具和数据的，一个是把一整件事托付出去的。混用会付出真金白银的代价。

边界必须划清楚，否则代价会落在延迟、状态机和错误处理上：MCP 管什么、A2A 管什么、两者怎么叠，以及自己在内部设计消息契约时该怎么取舍。

## 二、原理：机制拆解

### 2.1 MCP 的对象模型

MCP（Model Context Protocol）的结构很干净：一个 server 对多个 client，server 对外暴露三类原语。

| 原语 | 是什么 | 寻址方式 | 有副作用吗 |
| --- | --- | --- | --- |
| tools（工具） | 可调用的动作，有入参 schema | 名字 | 有 |
| resources（资源） | 可读取的上下文 | URI | 没有 |
| prompts（提示） | 可复用的提示模板，参数化渲染 | 名字 + 参数 | 没有 |

分成三类不是为了概念整齐，是因为这三类东西的**失败方式完全不同**：工具调用会改数据、可能失败、可能超时；资源读取只是取一份内容，读不到就是读不到；提示模板是给模型的一段引导，参数填错只是渲染错。

一个真实的 server 长这样：

```
tools:
  query_orders     按客户号查询待核对的退款订单列表
                   入参 schema：{"customer_id": "string", "status": "string"}
  compare_ledger   把订单退款金额与系统台账逐笔比对，返回差额
                   入参 schema：{"order_id": "string", "amount_cents": "integer"}
resources:
  orders://customer/C001/raw    C001 原始订单
  policy://refund/domestic      境内退款政策
prompts:
  refund_review     参数 customer_id、policy
```

**MCP 与 Function Calling 的关系，是这一节最该记住的一句话：Function Calling 是模型侧的"选择与填参"机制，MCP 是"能力从哪来、怎么描述、怎么被找到"的机制。** 两者不冲突，是上下游。

它们的接口点非常具体：MCP 工具的 JSON Schema 可以直接变成 Function Calling 的 `tools` 参数；工具的执行结果直接变成一条 `role=tool` 的消息。配套脚本里就是这么转的：

```python
def tools_for_function_calling(self):
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["inputSchema"]}}
            for t in self.list_tools()]
```

所以你会发现：用了 MCP 之后，你仍然用 Function Calling 让模型选工具；MCP 替你解决的是"工具清单从哪来、schema 从哪来、怎么不用把几十个工具硬编码在提示里"。

### 2.2 A2A 的核心抽象

A2A（Agent2Agent Protocol）由 Google 发起，2025 年 6 月捐赠给 Linux Foundation，成为供应商中立的社区治理项目；规范在 2026 年 3 月进入 1.0 稳定版。它的四个核心对象，各管一件事：

- **AgentCard**：这个 Agent 是谁、能干什么、在哪。支持流式吗？会推送通知吗？有哪些技能？——**能力发现的载体**。
- **Task**：一件被委托的事。有状态机：`submitted → working → completed`（也可能 `input-required` 等你补信息，或 `failed`）。用 `contextId` 把同一段上下文下的多次委托串起来。
- **Message**：过程中的对话与指令。带 role、parts，并关联到 taskId 与 contextId。
- **Artifact**：任务的产出物。**它独立于消息存在**，可以被引用、下载、复用、存档。

把 Artifact 从 Message 里分出来，是这个协议设计上很见功力的一笔：消息是过程（发完就没意义了），产物是结果（生命周期长得多）。一份差异报告要能存进工单系统、能被后续流程引用、能算作交付物，它就不能只是一条聊天记录。

委托的完整流程长这样：

```
客户端                                远端 Agent
  │  ① 读 AgentCard，发现能力            │
  │────────────────────────────────────►│
  │  ② 发 Message（带 taskId/contextId）  │
  │────────────────────────────────────►│
  │                       ③ 状态更新：working │
  │◄────────────────────────────────────│
  │                       ④ 状态更新：working │
  │◄────────────────────────────────────│
  │                       ⑤ 状态更新：completed + artifact_id
  │◄────────────────────────────────────│
  │  ⑥ 取 Artifact                     │
  │────────────────────────────────────►│
```

**流式推进这件事比看起来重要。** 一个对账任务可能要跑几分钟，如果客户端只能"发出去然后等结果"，就没法做进度展示、没法中途取消、没法把中间状态落盘。状态更新把长任务从"一个超时的 RPC"变成了"一个可观测的过程"。

### 2.3 分工：上下文供给层 vs 委托执行层

一句话判据：

- **MCP 解决"我的 Agent 怎么拿到能力和数据"**——上下文供给层。
- **A2A 解决"我的 Agent 怎么把一件完整的事交给另一个 Agent 去办"**——委托执行层。

叠起来是这样：

```
┌──────────────────────────────────────────────┐
│ 你的 Agent（A2A Client）                      │
│   读 AgentCard → 发现能力                     │
│   组装 Message → 委托 Task                    │
└───────────────┬──────────────────────────────┘
                │ A2A：委托、状态推进、产出物
┌───────────────▼──────────────────────────────┐
│ 远端 Agent（A2A Server，同时是 MCP Client）    │
│   处理 Task → 产出 Artifact                    │
│   通过 MCP 取工具与资源                        │
└───────────────┬──────────────────────────────┘
                │ MCP：tools / resources / prompts
┌───────────────▼──────────────────────────────┐
│ MCP Server：公司的退款系统、政策库、工单库      │
└──────────────────────────────────────────────┘
```

两层互不感知：你的 Agent 不需要知道远端 Agent 内部用了哪些工具；远端 Agent 也不需要知道你把它当成委托目标。这正是分层协议的价值——每一层只对相邻层负责。

两个常见误用值得点名：

- **拿 A2A 当函数调用用。** 为了调一个本地函数，套上完整委托语义：建任务、推状态、传产物。延迟、状态机、错误处理全白付。
- **拿 MCP 当 Agent 协议用。** MCP 里没有"任务"这个概念，也没有多轮委托和产出物。你没法用 MCP 说"请替我把这件事办完"。

### 2.4 内部消息契约：结构化还是自然语言

回到开头那两条消息。我们实测了两种编码：

| 编码方式 | 解析成功 | 解析率 | 字节数 | token |
| --- | --- | --- | --- | --- |
| 自然语言 | 1/2 | 50% | 83 | 28 |
| 结构化 | 2/2 | 100% | 264 | 76 |

结论有点反直觉：**结构化消息更贵**——字段名要写两遍，信封本身也要钱——但它换来的是解析确定。自然语言便宜，可每一处歧义你都得留兜底，而兜底就是失败率。

所以判据不是"哪个更专业"，而是**参数的取值空间有限还是开放**：

- 金额、订单号、客户号、日期、状态——取值有限、必须精确 → **结构化**。这类字段用自然语言传，等于把一个校验问题变成一个理解问题。
- "帮我看看这个方案有什么问题"——意图本身就是开放的 → **自然语言**，因为你要的就是模型的语义理解。

工程上最常用的是折中：**外层用固定信封（版本号、类型、trace_id、回复地址），内层 payload 按字段性质选结构化还是自由文本。** 配套脚本里的信封就是这样：

```python
{"v": 1, "kind": "refund.request", "reply_to": "cb-1", "trace_id": "t-1",
 "payload": {"customer_id": "C001", "amount_cents": 20000}}
```

还有一条容易被忽略的：**版本号才是真正的兼容边界。** 新增一个可选字段，旧解析器照样能吃（脚本里验证过）；但 `v` 一旦变了，就必须两端同时升级。所以版本号不是形式主义，它是"什么时候需要做兼容期"的唯一依据。

### 2.5 AgentCard 的工程价值

卡片最常被当成"介绍页"，其实它是这套体系里最实用的一块基建。

**它是路由的依据。** 委托之前先读卡片，看 skills 和 tags，就知道该把活交给谁、这个 Agent 到底会不会做。这让"选谁"从拍脑袋变成了查表。

**它是解耦点。** 卡片上写 `url`，调用方就知道去哪儿找它；写 `version`，就知道行为变没变。部署细节、服务发现、灰度版本，都被挡在卡片后面了。

**它是一份可发布的能力说明书。** 因为字段是结构化的，卡片可以直接进注册中心、可以被搜索、可以被路由网关读。自由文本的"我会什么"介绍做不到这件事。

但要清楚它的边界：**卡片是承诺，不是保证。** 它描述能力，不描述质量、不描述延迟、不描述失败行为。所以读了卡片之后，超时策略、降级策略、错误语义还是得自己在客户端定义。卡片说"我会算退款对账"，不代表它十分钟内一定算得完。

## 三、为什么这样设计：取舍与失效条件

先说这两个协议各自的代价。

**MCP 的代价是"多一跳"和"工具选择退化"。** 即使在本地，server 也要是个独立进程；跨网络还要算上延迟。更隐蔽的是：工具挂得越多，模型选错的概率越高。把公司所有工具一次性挂上去，模型会在几十个描述相近的工具里挑花眼。工具集要按场景裁剪，不是越多越强。

**A2A 的代价是状态与信任。** 一旦跨进程、跨组织，就要处理任务持久化、超时、取消、幂等、重试，还要处理认证与授权——一个可被别人调用的接口就是一条新的信任边界。

内部契约的代价前面算过了：结构化更贵，自然语言更脆。

什么时候**不该**用这两个协议：

- **同一进程内的两个函数** → 直接调用，别包装成委托。
- **单体内的多 Agent**（我们第 21、23 章讲的那些） → 内部消息契约就够，不需要 A2A 的完整语义。
- **只有一两个工具、且永远跟着同一个 Agent** → 直接写函数调用，MCP 的价值在"跨进程、跨团队、可复用"，独占场景没有这些。
- **任务只有一步、调用完就结束** → 委托的状态机、产出物全是浪费。

几个必须防的失效模式：

- **卡片与实现漂移**：卡片说支持流式，实现里没有；卡片说支持某技能，实际报错。卡片要有版本与过期机制，不能只写不维护。
- **委托超时**：远端慢或挂了，客户端不能无限等。要有超时、取消和降级。
- **契约漂移**：字段改了对方没改，数据静默错位。版本号加契约测试是底线。
- **自然语言静默错解**：最危险的一种——不报错，只是把金额解析错了。对关键字段做结构化 + 校验。
- **权限过大**：MCP server 拿到的权限常常远大于单个工具真正需要的，按最小权限配。

## 四、动手：最小可实现

代码在 `code/ch24-inter-agent-protocol/`，纯标准库、无需 API Key。

**依赖**：Python 3.9+，零第三方包。

**运行**：

```bash
cd code/ch24-inter-agent-protocol
python main.py          # A~D 四段演示
python main.py --live   # 切真实模型
```

一次跑清四件事：MCP 三类原语与 Function Calling 参数的转换、A2A 的委托与流式推进、两层的职责划分与组合、以及内部消息契约的解析率与开销实测。

典型输出（节选）：

```
MCP 工具 schema → Function Calling 的 tools 参数：
  query_orders：{"type": "object", "properties": {"customer_id": {"type": "string"}, ...}}

委托与流式推进：
  status=working    已接收委托，正在取数
  status=working    已通过 MCP 拉取订单与政策
  status=working    差异结论已生成
  status=completed  art-1

编码方式         解析成功     解析率     字节数     token
自然语言         1/2          50%        83         28
结构化           2/2          100%       264        76
```

### 4.1 关键实现片段

MCP 侧的三类原语与客户端发现能力：

```python
class McpClient:
    def tools_for_function_calling(self):
        """MCP 负责能力从哪来，Function Calling 负责模型怎么选。"""
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["inputSchema"]}}
                for t in self.list_tools()]
```

A2A 侧的任务状态推进：

```python
task = Task(id=message.task_id, context_id=message.context_id,
            status="working", history=[message])
yield {"task_id": task.id, "status": "working", "note": "已接收委托，正在取数"}
# ... 中间产物
task.artifacts.append(Artifact(artifact_id="art-1", name="refund-diff",
                                parts=[{"kind": "text", "text": answer}]))
task.status = "completed"
yield {"task_id": task.id, "status": "completed", "artifact_id": task.artifacts[-1].artifact_id}
```

内部契约的两个实现，一眼能看出差别：

```python
# 自然语言：从句子里猜参数，歧义在运行时暴露
cid = re.search(r"客户\s*([A-Za-z0-9]+)", text)
amt = re.search(r"(\d+(?:\.\d+)?)\s*元", text)
if not cid or not amt:
    return None                      # 猜不出来就卡住

# 结构化：按信封取字段，歧义在开发期暴露
if env.get("v", 0) != 1:
    return None
payload = env.get("payload") or {}
if "customer_id" not in payload or "amount_cents" not in payload:
    return None
```

三个提醒：

**协议实现里最该抄的是"三类原语分开"这个决定。** 把只读上下文也做成工具，模型就会为了一次"读"去生成调用参数，既费 token 又容易出错。

**AgentCard 里的 `capabilities` 一定要和实现对齐。** 卡片写 `streaming: true` 而服务端不支持推流，客户端的进度条就是死的。

**契约演进要有测试。** 脚本里演示的是"新增可选字段旧解析器还能吃"；但一旦 `v` 变了，两端必须同时升级——这件事要进 CI，靠人记不住。

## 五、开源实现对照

- **MCP 规范仓库**（https://github.com/modelcontextprotocol/modelcontextprotocol）：协议本体与 schema 的出处，仓库的许可证字段不是标准 SPDX，引用时以仓库内 LICENSE 文件为准。
- **MCP Python SDK**（MIT，https://github.com/modelcontextprotocol/python-sdk）：Python 侧的 client/server 实现，本章的 MCP 对象模型可以对照着看官方的类划分。
- **A2A Protocol**（Apache-2.0，https://github.com/a2aproject/A2A）：规范与核心对象定义，AgentCard、Task、Message、Artifact 的语义以它为准。
- **a2a-python**（Apache-2.0，https://github.com/a2aproject/a2a-python）与 **a2a-js**（Apache-2.0，https://github.com/a2aproject/a2a-js）：两套官方 SDK，委托、状态推进与产物的落地细节看这里。
- **ACP**（Apache-2.0，https://github.com/i-am-bee/acp）：另一套面向 Agent 间通信的开放协议方案，可以拿来对照 A2A 在任务模型上的取舍。
- **ANP**（Apache-2.0，https://github.com/agent-network-protocol/AgentNetworkProtocol）：面向 Agent 网络的协议思路，把"Agent 在网络上互相发现与协作"这件事推到更远一层。
- **OpenAI Agents SDK**（MIT，https://github.com/openai/openai-agents-python）：交接（handoff）是 A2A 委托语义在单进程内的轻量版——同样有"把控制权交出去"，但不引入任务状态机与跨进程契约。
- **Google ADK**（Apache-2.0，https://github.com/google/adk-python）：同时接了 MCP 与 A2A 两边——仓库里有 MCP 工具接入，也有专门把远端 A2A Agent 当子代理用的 remote_a2a_agent，适合对照"两层怎么叠"的具体做法。
- **Microsoft Agent Framework**（MIT，https://github.com/microsoft/agent-framework）：统一 Agent、工作流与部署，协议接入方式可以对照着看。
- **AutoGen**（MIT，代码部分为 LICENSE-CODE，仓库根目录 LICENSE 为 CC-BY-4.0，https://github.com/microsoft/autogen）：GroupChat 是"消息"这种内部契约的最典型实现（见第 21 章）。

许可证之外的具体 API 与版本，请以官方仓库为准。

## 六、坑与检查清单

接协议之前，逐条打勾：

- [ ] 我先判断了是否真的需要跨进程或跨组织（同进程内我直接调函数）。
- [ ] tools / resources / prompts 三类原语分清了，没有全塞成 tools。
- [ ] MCP 工具的 schema 能直接转成 Function Calling 参数，没有另写一套映射。
- [ ] AgentCard 里有身份、端点、版本、能力声明与技能清单。
- [ ] 卡片有版本与过期机制，不会和实现悄悄漂移。
- [ ] 委托有超时、取消与降级，不会无限等一个远端。
- [ ] 金额、ID 这类字段走结构化，没有用自然语言传。
- [ ] 消息信封带版本号与 trace_id。
- [ ] 契约变更有测试，`v` 一变两端同步升级。
- [ ] 工具集按场景裁剪过，不是把公司所有工具都挂上。
- [ ] MCP server 按最小权限配置。
- [ ] 我没有拿 A2A 当函数调用用，也没有拿 MCP 当 Agent 协议用。
- [ ] 产出物（Artifact）能脱离对话单独存档与复用。

## 七、小结与延伸阅读

MCP 与 A2A 不是竞争关系，是两层关系：MCP 是上下文供给层，解决"我的 Agent 怎么拿到能力和数据"；A2A 是委托执行层，解决"我的 Agent 怎么把一整件事交给另一个 Agent 去办"。前者没有任务状态机，后者没有工具发现——各管各的，叠起来才完整。

内部消息契约的判据最实用：**参数取值空间有限、必须精确，就走结构化；意图开放，就走自然语言。** 实测里结构化消息贵一倍多（76 token vs 28），换来的是 100% 的解析率。自然语言不是不能用，是不该用来承载金额和单号。

AgentCard 是这套体系里性价比最高的一块基建：它让"选谁""在哪儿""能不能流式"都变成查表而不是猜测。但要记住它是承诺不是保证——超时、降级、错误语义，仍然得客户端自己定义。

延伸阅读（均为官方仓库地址）：

- MCP 规范 — https://github.com/modelcontextprotocol/modelcontextprotocol
- MCP Python SDK — https://github.com/modelcontextprotocol/python-sdk
- A2A Protocol — https://github.com/a2aproject/A2A
- a2a-python — https://github.com/a2aproject/a2a-python
- a2a-js — https://github.com/a2aproject/a2a-js
- ACP — https://github.com/i-am-bee/acp
- ANP — https://github.com/agent-network-protocol/AgentNetworkProtocol
- OpenAI Agents SDK — https://github.com/openai/openai-agents-python
- Google ADK — https://github.com/google/adk-python
- Microsoft Agent Framework — https://github.com/microsoft/agent-framework
- AutoGen — https://github.com/microsoft/autogen
