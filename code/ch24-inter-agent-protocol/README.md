# ch24 Agent 间通信协议：MCP、A2A 与内部消息设计 最小可实现

对应《开源 Agent 架构技术全书》第 24 章《Agent 间通信协议：MCP、A2A 与内部消息设计》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**。一次跑清四件事：

- **MCP 对象模型**：server / client / tools / resources / prompts 三类原语，
  以及工具 schema 如何直接变成 Function Calling 的 `tools` 参数；
- **A2A 核心抽象**：AgentCard / Task / Message / Artifact，委托与流式推进；
- **两层职责**：MCP 是上下文供给层，A2A 是委托执行层，演示它们怎么叠；
- **内部消息契约**：自然语言 vs 结构化 envelope 的解析成功率、字节与 token 开销，
  以及契约演进时的向后兼容。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch24-inter-agent-protocol
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

### mock 模式（默认，无需 API Key）

```bash
python main.py          # A~D 四段演示
python main.py --live   # 切真实模型
```

### 典型输出

**A. MCP 对象模型**

```
tools：
  query_orders     按客户号查询待核对的退款订单列表
                   入参 schema：{"type": "object", "properties": {"customer_id": {"type": "string"}, ...}}
  compare_ledger   把订单退款金额与系统台账逐笔比对，返回差额
resources：
  orders://customer/C001/raw       C001 原始订单
  policy://refund/domestic         境内退款政策
prompts：
  refund_review    参数 customer_id、policy

MCP 工具 schema → Function Calling 的 tools 参数：
  query_orders：{"type": "object", "properties": {"customer_id": {"type": "string"}, ...}}

工具调用结果包成 role=tool 的消息：
  {"role": "tool", "name": "query_orders", "content": "{\"customer_id\": \"C001\", ...}"}
```

**B. A2A：AgentCard + 委托 + 流式推进**

```
委托与流式推进：
  status=working    已接收委托，正在取数
  status=working    已通过 MCP 拉取订单与政策
  status=working    差异结论已生成
  status=completed  art-1

产出物 Artifact：art-1（退款差异结论，可被引用与复用）
```

**C. 两层职责**

```
┌──────────────────────────────────────────────┐
│ 你的 Agent（客户端）                          │
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
│ MCP Server                                    │
└──────────────────────────────────────────────┘
```

**D. 内部消息契约**

```
编码方式         解析成功     解析率     字节数     token
自然语言         1/2          50%        83         28
结构化           2/2          100%       264        76

结构化消息更贵（字段名、信封、字段名重复），换来的是解析确定；
自然语言便宜，但要为每一处歧义留兜底。

契约演进：新增可选字段 reason 后，旧解析器仍能解析
注意：版本号 v 才是真正的兼容边界，字段可选性靠约定，v 变了就得同时升级两端。
```

### mock 说明

内置模型不做真实推理，返回固定的差异结论。**机制全部真实**：三类原语的区分与寻址、
schema 到 Function Calling 参数的转换、AgentCard 的能力发现、Task 状态机与流式推进、
Artifact 与消息分离、消息契约的两种解析实现与兼容性验证。

token 数是粗估值（中文按字、其余按 3.5 字符/token）。协议实现不依赖模型输出格式，
所以 live 与 mock 的结构完全一致，变的只是远端结论的文字。

### 真实模型（`--live`）

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
`choices[0].message.content`。换供应商时只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--live` | 关 | 切真实模型 API |
| `--model` / `--base-url` | 环境变量 | 覆盖 `LLM_MODEL` / `LLM_BASE_URL` |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `Tool` / `Resource` / `Prompt` | MCP 三类原语：动作、上下文、提示模板 |
| `McpServer` | 服务端：注册与分发 tools / resources / prompts |
| `McpClient` | 客户端：发现能力、调用、读资源、取提示 |
| `McpClient.tools_for_function_calling()` | MCP 与 Function Calling 的接口点 |
| `AgentCard` / `AgentSkill` | 能力发现：身份、端点、版本、能力与技能 |
| `Task` / `Message` / `Artifact` | A2A 核心抽象：状态机、过程、产物 |
| `RemoteAgent.handle()` | 委托处理与流式状态推进 |
| `Envelope` | 内部结构化消息信封：版本、类型、载荷、trace_id |
| `ContractParser.parse_natural()` | 自然语言解析：歧义在运行时暴露 |
| `ContractParser.parse_structured()` | 结构化解析：歧义在开发期暴露 |

## 换成自己的场景时要改什么

1. 改 `build_mcp_server()`：注册你自己的工具、资源与提示模板。
   **只读上下文请做成 resource，不要做成 tool**——模型不该为了一次"读"去生成调用参数；
2. 改 `REFUND_CARD`：卡片上的 `capabilities` 必须和实现对齐，
   写 `streaming: true` 而服务端不支持推流，客户端的进度条就是死的；
3. 改 `RemoteAgent.handle()`：把状态更新当成一等公民——长任务要能展示进度、取消、落盘；
4. 改 `Envelope`：**版本号 `v` 是唯一的兼容边界**。新增可选字段旧解析器能吃，
   `v` 一变就必须两端同时升级，这件事要进 CI；
5. 改 `ContractParser`：关键字段（金额、ID、日期）一律走结构化并做校验。
   自然语言里最危险的不是解析失败，是**静默解析错**——不报错，只是金额错了；
6. **别过度协议化**：同进程内的两个函数直接调用；单体内的多 Agent 用内部消息契约就够，
   A2A 的任务状态机与跨进程语义是纯开销。

## 相关章节

- 第 8 章：工具调用契约（schema 设计与参数校验的通用原则）
- 第 21 章：对话式协作（内部消息契约的最简形态）
- 第 23 章：协作拓扑与调度（委托关系怎么排）
- 第 31 章：沙箱与执行隔离（MCP server 的权限边界）
- 第 32 章：提示注入与越权防护（跨信任边界调用的风险）
