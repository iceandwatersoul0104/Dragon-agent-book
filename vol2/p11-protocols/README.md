# 第 11 章 · 协议接入：把你的 Agent 变成 MCP Server 与 A2A 节点

本卷第十一章把"自建 Agent"变成"别人能调用的服务"：**一套实现，两个出口；失败分两条通道；版本差异写成表。**

- 正文：[`protocols.md`](protocols.md)
- 配套工程产物：`dragon-agent/` 下的三个文件（见下）

## 本章给了什么

1. **`dragon_agent/mcp_server.py`（775 行）**——MCP server（JSON-RPC over stdio）+ A2A 节点（`AgentCard` / `Task` / `Artifact`），纯标准库：
   - **薄适配层**：`tools/list` 的 schema 直接来自注册表，`tools/call` 只调 `registry.invoke()`，协议层不重写校验与幂等；
   - **两种失败两条通道**：请求结构不对 → JSON-RPC `error`；工具跑过了但失败 → 结果里 `isError: true`；
   - **版本差异写成数据**：`PROTOCOL_PROFILES` 一张表装下四个修订的行为差异，新版本来了改表不改流程；
   - **A2A 节点**：`SendMessage` 立刻返回任务，工作丢后台线程，完成后产出物落在 `artifacts` 里。
2. **`examples/mcp_protocol_demo.py`（249 行）**——**真的拉起一个子进程**，把七件事跑一遍：握手与版本协商、`tools/list`、两种失败两条通道、幂等、一套工具两种暴露、A2A 完整委托。
3. **`tests/test_mcp_server.py`（671 行）**——55 条契约断言，全部离线。

**本章没有改动 `dragon_agent/tools.py`**——工具的校验、幂等、错误回灌已经在第 4 章定死了，本章只读不改。协议层是薄适配层。

## 核心约定

> 协议层是**薄适配层**：只做翻译，不含任何校验/幂等/业务规则。
> 失败分**两条通道**：请求结构不对 → JSON-RPC `error`；工具跑过了但失败 → 结果里 `isError: true`。
> 幂等键从 JSON-RPC 的 **request id** 派生——调用方在另一个进程里，重试同一个 id 就是同一次动作。
> 版本差异集中在 `PROTOCOL_PROFILES` 表里，新版本来了改表不改流程。

## 两种失败，两条通道

判据只有一个：**模型能不能自己纠正。**

| 失败类型 | 走哪条通道 | 模型能改吗 |
| --- | --- | --- |
| 方法不存在、工具不存在、参数不是对象 | JSON-RPC `error` | 不能——是调用方的 bug |
| 工具参数**取值**不对（缺必填、类型错） | 执行错误 `isError: true` | 能——看得见缺什么 |
| 工具内部抛异常 | 执行错误 `isError: true` | 能——看得见失败原因 |

把能纠正的失败包装成协议错误，模型就失去了自我纠正的机会——这是第 4 章"失败是结果不是异常"那条原则跨进程之后的样子。

## 怎么跑

```powershell
cd dragon-agent

# 1) 演示：真的起子进程，退出码 0
python examples/mcp_protocol_demo.py

# 2) 本章的契约测试：55 个用例
python -m unittest tests.test_mcp_server

# 3) 全量测试：确认没有打破其它模块
python -m unittest discover
```

不需要 API Key，不联网，不装任何第三方包，也不写用户目录。中文输出在 GBK 控制台上可能显示为乱码，加 `python -X utf8` 即可。

### 实跑结果

在本机（Windows 11 / Python 3.13.5）实跑，退出码均为 0：

| 场景 | 结果 | 退出码 |
| --- | --- | --- |
| `python examples/mcp_protocol_demo.py` | 七段演示 + 自检 **15/15** 项通过 | 0 |
| `python -m unittest tests.test_mcp_server` | `Ran 55 tests` / `OK` | 0 |
| `python -m unittest discover` | `Ran 391 tests` / `OK (skipped=3)` | 0 |
| `python _plan/_code_ref_check.py`（在书稿根目录） | 本章 6 段需逐字一致的代码块，**6 段全部一致** | 0 |

演示里的关键输出：

- 客户端要 `1999-01-01`（不存在的版本）→ server 回 `2025-11-25`，**不拒绝**；
- 工具不存在 → `{"code": -32602, "message": "未知工具：no_such_tool"}`；
- 参数不对 → `{"content": [{"type": "text", "text": "[工具 add] 缺少必填参数 b"}], "isError": true}`；
- 同一个 request id 重试 `submit_order` → 被拒绝，`幂等键 mcp:submit_order:100 已用过`；
- 内部直调 + 协议调用打同一个注册表 → `invocations` 为 `['add', 'add']`，工具只实现一次；
- A2A 状态推进 `submitted → working → completed`，产出物落在 `artifacts` 里。

## 支持的协议版本

本实现覆盖**握手家族**的四个修订：

| 版本 | 参数错误走哪条通道 | `structuredContent` | 批处理 |
| --- | --- | --- | --- |
| `2024-11-05` | 协议错误 | 否 | 否 |
| `2025-03-26` | 协议错误 | 否 | **是** |
| `2025-06-18` | 协议错误 | 是 | 否 |
| `2025-11-25` | **执行错误** | 是 | 否 |

每一行都对着官方 changelog 核过：`2025-03-26` 加批处理、加基于 OAuth 2.1 的授权框架；`2025-06-18` 去批处理、加结构化输出、把 MCP server 归类为 OAuth 资源服务器；`2025-11-25` 把输入校验错误从协议错误改成**工具执行错误**（SEP-1303）。

`2026-07-28` 是无状态修订（取消了初始化握手，改为每个请求在 `_meta` 里带版本，客户端先调 `server/discover`），与本实现的会话模型不同，**不在支持集合里**——这是边界，不是遗漏。

## 局限（这一章不等于可以上线）

这一章交付的是**协议适配层的设计与最小实现**，不是可以直接对外服务的产品。上线前，下面每一条都要自己补：

- **只覆盖 stdio 一种传输。** 没有 HTTP / Streamable HTTP，没有 TLS、没有鉴权、没有多用户隔离。stdio 的信任边界是"谁拉起了这个进程"；换成 HTTP 传输后，MCP 规范要求 server 当 OAuth 资源服务器（2025-06-18 起），本章一行都没实现。
- **幂等记录在进程内存里。** server 进程重启后，用过的幂等键就没了，同一个 request id 再来会**真的执行一遍**。"跨进程重试不会重复扣款"的前提是重试落回**同一个** server 进程。要做到进程重启也安全，得把键的记录落到数据库或 Redis——`tools.py` 的 `ToolRegistry` 本来就是进程内对象，本章没动它。
- **A2A 只是最小对象集。** 没有签名卡片（Signed Agent Card）、没有推送通知、没有流式（`SendStreamingMessage` / `SubscribeToTask`）、没有多轮往返；AgentCard 用的是 1.0 之前的扁平 `url` 字段，不是 v1.0 的 `supportedInterfaces[]`；任务状态用简写 `completed`，而按 proto 枚举名序列化的实现会输出 `TASK_STATE_COMPLETED`——接真实 SDK 前要按它的序列化形式对齐。
- **任务与产出物都在内存里。** A2A 的任务、历史、artifact 全在进程内存：进程一挂全没了。没有持久化、没有超时、没有接管、没有跨进程的任务迁移。
- **没有并发上限。** stdio 上逐行处理、请求串行；A2A 每个委托起一个后台线程，没有线程池、没有背压，一个客户端可以无限量地提交委托。
- **工具没有沙箱。** `read_text_file` 按传入路径直接读真实文件系统，路径不受根目录约束。把它经协议暴露出去，等于把 server 进程的文件读权限交给了协议客户端——接真实工具前这是必须先补的一层。
- **stdout 只跑协议帧。** 依赖框架的日志全部走 stderr（`logging_setup` 的默认行为）。任何第三方库往 stdout 打一行，客户端的解析就会错位。
- **协议层没有接可观测性。** 这里只记了 `mcp.*` / `a2a.*` 几个事件，trace、指标与成本账是空的——那一块属于第 12 章，本章不重复。

## 与本书其它章节的关系

- 第二卷第 4 章 `p04-tool-layer`：工具契约、错误回灌与幂等语义。本章只读不改，是那套语义跨进程之后的样子。
- 第二卷第 6 章 `p06-agent-loop`：本地调用时幂等键怎么生成（任务 + 步骤 + 工具名 + 参数摘要）；协议层换了个来源——JSON-RPC 的 request id。
- 第二卷第 7 章 `p07-state-checkpoint`：本地重放靠检查点，协议重试靠幂等键，是同一件事的两种实现。
- 第二卷第 12 章 `p12-observability`：协议层把 Agent 暴露出去之后，跨进程的调用链更难复盘——trace、指标与成本账怎么接上。
- 第一卷第 24 章《Agent 间通信协议：MCP、A2A 与内部消息设计》：两层协议的分工与误用场景，本章是它的工程落地。
- 第一卷第 39 章《协议与生态全景：MCP、A2A、AG-UI、Skills 与插件体系》：规范修订史与生态现状，本章那张版本表的事实依据。
