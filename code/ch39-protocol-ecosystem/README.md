# ch39 协议与生态全景 —— 配套示例

《开源 Agent 架构技术全书》第 39 章的参考实现。一个纯标准库的"五层协议栈"，把 Agent 接入生态时最常打交道的五层各演一遍。

场景：用户问"上个月（2026-10）的工资条是多少？"

| 层 | 本例中发生了什么 | 报文形态 |
|---|---|---|
| 模型 API 层 | 模型每轮只输出一个 JSON 动作 | `{"action": "mcp_call", ...}` |
| 能力封装层（Skills） | 启动只加载 3 个 skill 的 name+description；命中后才读 SKILL.md 正文 | 索引行 → 正文 |
| 工具上下文层（MCP） | `server/discover` / `tools/list` / `tools/call`；缺参时返回 `resultType=input_required`，客户端重试原请求并带 `inputResponses` | JSON-RPC 信封 |
| 委托执行层（A2A） | 读对方 Agent Card → `tasks/send` → submitted/working/completed → 拿回 artifact | Agent Card / Task / Artifact |
| 用户交互层（AG-UI） | 23 个事件：RUN_* / TEXT_MESSAGE_* / TOOL_CALL_* / STATE_SNAPSHOT / SUBAGENT_* | 事件流 |

## 依赖

- Python **3.8+**，**只用标准库**，无需 `pip install`；
- 默认 mock 模式**不需要任何 API Key**；
- `--live` 才需要 `OPENAI_API_KEY`（任意 OpenAI 兼容端点）。

## 安装

```bash
cd code/ch39-protocol-ecosystem
python -c "import sys; print(sys.version)"
```

Windows 下用 `py -3` 代替 `python`。没有安装步骤——不依赖第三方包。

## 运行

```bash
# 1) 默认：mock 模型跑通五层，打印每层报文、事件流与分层归因
python main.py

# 2) 真实模型（协议、工具、校验与 mock 模式完全一致）
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.openai.com/v1     # 可选，任意 OpenAI 兼容端点
export OPENAI_MODEL=gpt-4o-mini                       # 可选
python main.py --live
```

主要参数：`--max-steps`、`--live`、`--model`、`--base-url`。

退出码：`0` = 事件流一致性与事实校验均通过；`1` = 有校验项不过；`2` = 参数/环境不对。

## 本例演示的四个协议要点

| 要点 | 在代码里怎么体现 |
|---|---|
| MCP 的多轮往返（MRTR） | `McpServer._call_tool()` 缺 `month` 时返回 `resultType=input_required` + `inputRequests`；客户端重试时把 `inputResponses` 带上（`Orchestrator.do_mcp_call`） |
| MCP 结果的类型区分 | 所有结果都带 `resultType`（`complete` / `input_required`），客户端不能把前者当后者 |
| A2A 的不透明委派 | `A2AAgent` 只给 Agent Card、任务状态和 artifact，不暴露内部工具与记忆；卡片带签名标记 |
| AG-UI 的事件契约 | `EventStream.emit()` 产出统一事件；`validate()` 检查 TOOL_CALL_*/TEXT_MESSAGE_*/SUBAGENT_* 的 START/END 配对，以及 RUN 是否有终态 |
| Skills 的渐进披露 | `SkillRegistry.index()` 只给 name+description；`load()` 才返回正文；程序结束时打印实际加载了几个 skill（本例是 1 个） |

## 校验是两层，不是一层

1. **事件流一致性**：每个 `*_START` 都有配对的 `*_END`，且 `RUN_STARTED` 必须有 `RUN_FINISHED` 或 `RUN_ERROR`；
2. **事实一致性**：最终答案里的金额必须能从 A2A 返回的 artifact 里正则解析出来——如果模型自己编了数字，这个校验就会挂。

第二层是重点：协议跑通不等于活干对了。

## 与正文的对应关系

| 正文位置 | 代码位置 |
|---|---|
| 2.2 模型 API 层 | `MockModel` / `LiveModel` 的单动作协议 |
| 2.3 MCP 层 | `McpServer.handle()`、`McpServer._call_tool()` |
| 2.4 A2A 层 | `A2AAgent.agent_card()`、`A2AAgent.send_task()` |
| 2.5 AG-UI 层 | `EventStream.emit()`、`EventStream.validate()` |
| 2.6 Skills 层 | `SkillRegistry.index()` / `SkillRegistry.load()` |
| 3 选型速查表 | `Orchestrator.trace` 的分层归因输出 |
| 4 动手 | `Orchestrator.run()` 主循环 + `Orchestrator.verify()` |

## 已知限制

- 全部是本地模拟：MCP 没有真的起 HTTP 或 stdio 服务，A2A 没有真的跨进程；
- MCP 只实现了发现、列工具、调工具三个方法，没实现 resources、prompts、鉴权与订阅；
- A2A 的任务状态流转是一步到位的，真实实现里状态是异步推进的；
- AG-UI 事件是直接构造的，没有 SSE 传输、没有 AG-UI 中间件；
- Skills 目录是内存里的 dataclass，不是真的 SKILL.md 文件；三级披露只演示到第二级；
- mock 模型是固定剧本，用来演示协议交互，不具备泛化能力；
- 观察用 `role: user` 回灌而非 `role: tool`，是为了兼容只实现基础 chat/completions 的端点。
