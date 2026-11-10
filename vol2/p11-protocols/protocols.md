# 第 11 章 协议接入：把你的 Agent 变成 MCP Server 与 A2A 节点

## 一、本章要解决的问题

同一个工具函数，两种接法。

周三上午，你把 `read_text_file` 和 `add` 写进了 `ToolRegistry`；下午，隔壁组的人问："能不能让我们那个 IDE 客户端直接调你们的工具？"你说好，用 MCP server 暴露出去。周四，运维组又问："能不能让我们的对账 Agent 把活派给你们？"你又加了一层 A2A。

到了周五，你发现注册表里已经有五个工具了——其中两个是 MCP 那条路上新加的，而它们的参数校验逻辑和 `tools.py` 里那份**不一样**：MCP 版允许字符串形式的数字，本地版不允许。于是模型在本地调用时会收到"参数类型错误"，走协议调用时却能跑通，同一个工具在两条路上行为不一致。查起来才发现，那两个 MCP 版工具还各自实现了一遍幂等逻辑，重试时会重复扣款。

问题不在 MCP，也不在 A2A。问题在于**你把协议的适配层写成了第二份实现**。

协议接入真正要解决的是三件事：

1. **一套实现，两个出口**——工具只写一遍，内部直调和协议调用打的是同一个注册表；
2. **失败怎么穿过进程边界**——本地调用抛异常就够了，跨进程之后失败必须变成对方看得懂的形状；
3. **协议会演进**——今天写死的字段名，明天可能就是过时的。

这三条，本章用 `dragon_agent/mcp_server.py` 逐条落地。

## 二、原理：机制拆解

### 2.1 MCP server 的最小实现：一个注册表，四个方法

MCP server 说到底只做四件事：列工具、收调用、回结果、回错误。

协议层是**薄适配层**。第 4 章的工具层已经把参数校验、错误回灌、幂等三件事定死了，协议层不该重做一遍——它只负责把线格式翻译成一次 `registry.invoke()` 调用，再把结果翻译回去。所以 `tools/list` 只有这么几行，schema 直接来自注册表：

```python
    def _mcp_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """列工具：schema 直接来自注册表，**不维护第二份**。

        内部叫 ``parameters``，MCP 线格式叫 ``inputSchema``——同一个
        JSON Schema，换了个字段名。
        """
        tools = []
        for schema in self.tools.schemas():
            tools.append({
                "name": schema["name"],
                "description": schema["description"],
                "inputSchema": schema["parameters"],
            })
        return {"tools": tools}
```

注意 `parameters` → `inputSchema` 这个改名。这是"一份实现、两个出口"的具体形态：内部那份 schema 给模型做 Function Calling 用，MCP 这份给协议客户端用，它们**是同一个 JSON Schema 对象**，只是线上字段名不同。如果你在 MCP 层另写一份，模型看到的和客户端看到的就是两个世界了。

### 2.2 错误怎么表达：两条通道，判据是"模型能不能自己改"

这是本章最要紧的一节，也是最容易被做错的地方。一个失败有三种可能，它们必须走**不同的通道**：

| 失败类型 | 走哪条通道 | 长什么样 |
| --- | --- | --- |
| 请求结构不对（方法不存在、工具不存在、参数不是对象） | JSON-RPC `error` | `{"code": -32601, "message": "未知方法：xxx"}` |
| 工具参数**取值**不对（缺必填、类型错） | 执行错误 | `{"content": [...], "isError": true}` |
| 工具内部抛异常（文件不存在、下游挂了） | 执行错误 | `{"content": [...], "isError": true}` |

这张表按 **2025-11-25 及以后**的修订写。在那之前，工具参数取值不对报的是**协议错误**——同一个失败，换一版协议就换一条通道。这是 2.5 节那张版本表要解决的第一个差异。

**判据是：模型能不能自己纠正。** 第一类模型纠正不了——它连方法名都写错了，再回一次还是错的，那是调用方的 bug，该用协议错误让调用方看见。后两类模型能纠正：它能读到"缺少必填参数 b"，然后补上 `b` 重试。把它包装成协议错误，模型就看不到该改什么。

实现上，这个分界靠一次额外的输入校验划出来：

```python
        # 先做一次**输入校验**，只为分清"参数取值不对"和"工具执行失败"
        # 这两种错误——它们该走不同的通道。校验逻辑仍然只有 tools.py 一份。
        input_error = self.tools.validate(spec, arguments)
        if input_error and not profile.tool_input_error_is_execution_error:
            raise _JsonRpcError(JSONRPC_ERRORS["invalid_params"],
                                f"{name} 参数不合法：{input_error}")

        # 副作用工具的幂等键：拿 JSON-RPC 的 request id 派生。
        # 同一个 id 重试就是同一次动作，跨进程也不会重复执行。
        key = f"mcp:{name}:{request_id}" if spec.side_effect else None
        result = self.tools.invoke(name, arguments, idempotency_key=key)

        if not result.ok:
            text = f"[工具 {name}] {result.error}"
            log_event(log, "mcp.tool_failed", tool=name, error=result.error)
            return {"content": [{"type": "text", "text": text}], "isError": True}
```

注意那句注释：**校验逻辑仍然只有 `tools.py` 一份**。协议层调的是 `registry.validate()` 和 `registry.invoke()`，它自己不写任何校验规则，只是多问了一句"这次失败属于哪一类"。

而幂等键从 request id 派生，是跨进程场景下的救命设计。本地调用时幂等键由 Agent 主循环按"任务 + 步骤 + 工具名 + 参数摘要"生成（第 6 章）；到了协议层，调用方是外部进程，它不认你的任务 id，但 JSON-RPC 的 request id 天然就是"同一次请求"的标识。客户端拿同一个 id 重试，幂等键就一样，`submit_order` 直接拒绝重复执行——**跨进程重试不会重复扣款**。

但这里有一句必须说清的话：**幂等键的记录（哪些键用过了）存在 server 进程的内存里**。客户端在同一段 stdio 会话里重试，键会命中；**server 进程重启之后记录就没了**，同一个 id 再来一次会真的执行一遍。要做到"连进程重启都安全"，得把键的记录落到共享存储（数据库或 Redis）里——本章没做这件事，`tools.py` 的 `ToolRegistry` 本来就是进程内对象。

### 2.3 MCP server 与本地函数调用差在哪

三处差别，都是本地调用不存在的问题：

**跨进程。** 本地调用是函数返回，跨进程是 JSON 文本往返。于是：类型要能被 JSON 表达；异常不能直接传，得翻译；编码必须显式指定——Windows 上管道默认走系统代码页（GBK/CP936），协议帧里全是中文工具描述，不切 UTF-8 客户端读到的就是乱码。本工程的 `_force_utf8` 就是为这件事存在的。

**鉴权。** 本地函数调用没有鉴权这回事，谁能 import 就能调。stdio 传输下的 MCP 同样没有——**信任边界是"谁拉起了这个进程"**。这不是疏漏：进程边界本身就是权限边界。而换成 HTTP 传输，鉴权就成了必须自己解决的大问题，MCP 规范在 2025-06-18 修订里把 MCP server 重新归类为 OAuth 2.1 资源服务器就是这个原因。


**会话。** 本地调用没有会话概念。stdio 下一个进程就是一段会话——`initialize` 握手、协商版本、然后才允许业务方法。而 2026-07-28 修订把初始化握手整个移除了：每个请求自带协议版本与客户端能力（放在 `_meta` 里），客户端可以先调 `server/discover` 问服务端支持哪些版本，服务端因此能水平扩展，不再需要粘性会话。所以"会话"本身也是协议演进的一部分。
### 2.4 A2A 的委托语义：名片、任务、产物

MCP 解决"我的 Agent 怎么拿到能力和数据"，A2A 解决"我的 Agent 怎么把一整件事交给另一个 Agent 去办"。两者是**两层**，不是竞争关系。

A2A 的三个核心对象各管一件事：

- **AgentCard**：能力名片——我是谁、会哪些技能、在哪。真实部署里它挂在一个 well-known 路径下：A2A v1.0 的标准路径是 `/.well-known/agent-card.json`（内容类型 `application/a2a+json`），1.0 之前是 `/.well-known/agent.json`——不少已部署的客户端仍然只认后者，接入前照着你用的 SDK 版本核对。卡片是**承诺不是保证**：它描述能力，不描述质量、延迟与失败行为。
- **Task**：一次委派的生命周期。`submitted → working → completed`，也可能 `failed` 或 `canceled`。用 `contextId` 把同一段上下文下的多次委托串起来。
- **Artifact**：任务的产出物。**它独立于消息存在**——消息是过程（发完就没意义了），产出物是结果（要能存档、引用、算作交付物）。

关键差别在**同步与异步**。本地函数调用是"调用 → 返回结果"，A2A 的 `SendMessage` 是"提交 → 立刻返回一个任务 → 干活在后台 → 客户端轮询 `GetTask` 或取消"。一件要跑几分钟的活，不能用一个会超时的 RPC 装。

顺带说一句方法名：A2A v1.0 的 JSON-RPC 方法名是 `SendMessage` / `GetTask` / `ListTasks` / `CancelTask` 这类 **PascalCase 的 RPC 名**，直接取自规范里的 `a2a.proto`；1.0 之前的斜杠形式（如 `message/send`）已经下线，两者**线格式不兼容**——不是"过时"，是接不上。本章的 `A2ANode` 用的就是 v1.0 的那套名字。

`A2ANode` 的实现就体现了异步这一点——任务建好后立刻返回，工作丢给后台线程：

```python
        worker = threading.Thread(target=self._run_task, args=(task_id, text),
                                  name=f"a2a-{task_id}", daemon=True)
        worker.start()
        return task.to_dict()
```

跑完之后，产出物落在 `artifacts` 里，而不是混在消息历史中：

```python
            if status == "completed":
                task.state = "completed"
                artifact = Artifact(
                    id=self._next_id("art"),
                    name="answer",
                    parts=[{"kind": "text", "text": answer}],
                )
                task.artifacts.append(artifact)
```

### 2.5 版本与兼容性：把差异写成数据，不写成 if

协议会演进，这是既定事实。官方 schema 目录里目前有五个定版修订（外加一个 draft）：`2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25`、`2026-07-28`。

问题是：新版本可能带来**行为差异**。比如工具参数校验失败报成哪种错误——2025-11-25 之前是协议错误，之后改成执行错误，好让模型能看着错误改参数重试（上游措辞见该修订 changelog 的 SEP-1303：输入校验错误应作为**工具执行错误**返回，而不是协议错误）。再比如批处理，2025-03-26 加进来、2025-06-18 又去掉。

如果这些差异写成散落在流程里的 `if version >= "..."`，每来一个新版本就要改一遍主流程，改到最后谁也不敢动。正确做法是**把差异写成一张表**——版本号是日期串，字典序就是时间序，判断起来很省心：

```python
@dataclass(frozen=True)
class ProtocolProfile:
    """某个协议版本下的行为差异。

    版本号是日期串（``YYYY-MM-DD``），所以字典序比较就是时间序比较——
    版本判断可以直接写 ``profile.version >= "2025-11-25"``。
    """

    version: str
    tool_input_error_is_execution_error: bool = False
    supports_structured_content: bool = False
    supports_batching: bool = False
```

主流程只查表，不判断版本。新版本来了，加一行表项；某个能力要退役，把那个布尔量改成 `False` 就行。

版本协商本身也要设计。客户端要了一个你不认识的版本，你该怎么办？**回一个你支持的版本，让客户端自己决定**：

```python
        requested = params.get("protocolVersion")
        if isinstance(requested, str) and requested in self.supported_versions:
            negotiated = requested
        else:
            # 客户端要了一个我们不认识的版本：回自己最新的那个，
            # 由**客户端**决定要不要继续。这是版本演进的正确接法。
            negotiated = self.supported_versions[-1] if self.supported_versions \
                else LATEST_PROTOCOL_VERSION
```

拒绝等于把客户端挡在门外；回一个版本、把决定权交回去，才是可演进的接法。前提是能力声明诚实——客户端拿到 `protocolVersion` 就该知道你在讲哪一版。

还要说一句诚实的话：本章实现覆盖的是**握手家族**的四个修订。`2026-07-28` 是无状态修订，取消了初始化握手，与本实现的会话模型不同，所以**不在支持集合里**。这不是遗漏，是边界——知道自己不说什么，和知道自己说什么一样重要。

## 三、为什么这样设计：取舍与失效条件

### 3.1 薄适配层付了什么代价

薄适配层不是免费的。代价有三笔：

**多一跳。** 本地调用是内存里的函数调用，协议调用多了一次序列化、一次进程边界、一次往返。

**错误信息在翻译中失真。** 本地调用时模型读到完整错误文本；跨进程后只剩一句 `"[工具 add] 缺少必填参数 b"`。细节丢了，换来的是模型能纠正。

**协议成了新的耦合点。** 两套路径同时存在时，内部路径会慢慢领先于协议路径，然后两边分叉——这正是第一节那个事故的根源。

### 3.2 什么时候不该这么做

这一节比前面都重要，因为**协议用错了比不用协议更糟**。

**同一进程内的两个函数，不要包装成协议。** 套上协议换来的是序列化和一次往返，换回的是没有任何新能力的复杂度。

**只有一个工具、且永远跟着同一个 Agent 时，不要上 MCP。** MCP 的价值在"跨进程、跨团队、可复用"，独占场景付了成本，一分钱好处没拿到。

**任务只有一步、调用完就结束，不要上 A2A。** 委托的状态机、产出物、取消语义全是白付——你为"办一件事"造了一套"办很多件事"的机制。

**只有一个客户端时，不要上 A2A。** A2A 解决的是"我不知道该把活派给谁"，名片机制的价值在于**多个候选 Agent 之间做路由**。只有一个 Agent 时，名片是自问自答。

**服务端不支持推流，卡片就别写 `streaming: true`。** 卡片与实现漂移是委托场景里最常见的失效：客户端读了卡片以为能流式拿进度，结果拿到的是一个死等的请求。

### 3.3 几个必须防的失效模式

- **两套实现**：协议层重写校验或幂等逻辑 → 两条路行为分叉。判据：协议层除了翻译，不该有任何业务判断。
- **协议版本写死**：客户端一升级就连不上。必须是"支持一个集合，协商时选一个"。
- **副作用工具没幂等键**：跨进程重试是常态，没有幂等键就是重复扣款。

## 四、动手：最小可实现

配套工程在 `dragon-agent/` 下，纯标准库、无需 API Key、不联网。

- `dragon_agent/mcp_server.py`——MCP server（JSON-RPC over stdio）+ A2A 节点。
- `examples/mcp_protocol_demo.py`——真的拉起一个子进程，把七件事跑一遍。
- `tests/test_mcp_server.py`——55 条契约断言。

**依赖**：Python 3.9+，零第三方包。

**运行**：

```powershell
cd dragon-agent

# 1) 演示：真的起子进程，退出码 0
python examples/mcp_protocol_demo.py

# 2) 契约测试：55 个用例
python -m unittest tests.test_mcp_server
```

一次跑清七件事：起子进程、握手与版本协商、`tools/list`、两种失败两条通道、幂等、一套工具两种暴露、A2A 完整委托。

stdio 传输的骨架只有几行——**每行一条消息，读到 EOF 收工**：

```python
    def serve_stdio(self, stdin: Any = None, stdout: Any = None) -> int:
        """读 stdin 写 stdout：每行一条 JSON-RPC 消息。

        **stdout 只跑协议帧。** 任何一行多余输出都会让客户端的解析错位，
        所以日志必须走 stderr。

        读到 EOF 返回 0。
        """
```

A2A 侧的核心是委托的异步性。`SendMessage` 建好任务就返回，工作丢后台：

```python
        worker = threading.Thread(target=self._run_task, args=(task_id, text),
                                  name=f"a2a-{task_id}", daemon=True)
        worker.start()
        return task.to_dict()
```

跑一遍演示，关键输出长这样：

```text
  initialize → {"protocolVersion": "2025-11-25", "capabilities": {"tools": {"listChanged": false}}, "serverInfo": {"name": "dragon-agent-mcp", "version": "1.0"}}
  客户端要 1999-01-01 → server 回 2025-11-25（不拒绝，让客户端自己决定）
  工具不存在 → JSON-RPC error：{"code": -32602, "message": "未知工具：no_such_tool", ...}
  参数不对 → 执行错误：{"content": [{"type": "text", "text": "[工具 add] 缺少必填参数 b"}], "isError": true}
  同一个 id 重试 → isError=True
    拒绝原因：[工具 submit_order] submit_order 不可重放，幂等键 mcp:submit_order:100 已用过，拒绝重复执行
  状态推进：submitted → working → completed
  Artifact（办完了的凭证）：[{"artifactId": "art-3", "name": "answer", "parts": [{"kind": "text", "text": "核对 2026-03 的退款：差异合计 132 元"}]}]
```

## 五、开源实现对照

### MCP 规范仓库

- 仓库：<https://github.com/modelcontextprotocol/modelcontextprotocol>
- 许可：仓库根目录 LICENSE 原文写明——MCP 项目**正在从 MIT 转向 Apache-2.0**：已取得 relicensing 同意的贡献按 Apache-2.0 授权，未取得同意的仍适用原许可。仓库页的许可证字段因此显示为 NOASSERTION（混合），引用时要以仓库内 LICENSE 文件为准。

协议本体与 schema 的出处，`schema/` 目录里按定版修订分目录存放，是核对"某个版本到底有什么"的权威位置。**差异**：规范是文档与 schema，不是可运行的代码。本章的 `PROTOCOL_PROFILES` 表是照着历次修订的行为差异整理的——写这张表时必须逐版翻 changelog，不能凭印象。本章那张表逐版翻的就是官方 changelog：<https://modelcontextprotocol.io/specification/2025-03-26/changelog>、<https://modelcontextprotocol.io/specification/2025-06-18/changelog>、<https://modelcontextprotocol.io/specification/2025-11-25/changelog>、<https://modelcontextprotocol.io/specification/2026-07-28/changelog>。

### MCP Python SDK

- 仓库：<https://github.com/modelcontextprotocol/python-sdk>
- 许可：MIT

官方的 Python 侧 client/server 实现。**差异**：SDK 把握手、批处理、能力协商、传输都替你做了。本章刻意手写一遍 JSON-RPC，正是为了看清这些细节长什么样——尤其是"版本协商回退"和"两种失败两条通道"，不手写一遍基本体会不到为什么要这么设计。

### A2A Protocol

- 仓库：<https://github.com/a2aproject/A2A>
- 许可：Apache-2.0

Agent 间委派协议，AgentCard、Task、Artifact 的语义以它为准。**差异**：A2A 的官方 SDK 覆盖 Python、JavaScript、Java、C#、Go、Rust，绑定方式包括 gRPC 与 JSON-RPC。本章的 `A2ANode` 只实现了 JSON-RPC 绑定和最小对象集，没有实现签名卡片、推送通知与多轮往返——签名卡片（Signed Agent Card）与推送通知是 **v1.0 阶段**补上的能力，不是 v1.0 之后；真要用得接官方 SDK。

### a2a-python 与 a2a-js

- 仓库：<https://github.com/a2aproject/a2a-python>、<https://github.com/a2aproject/a2a-js>
- 许可：Apache-2.0

两套官方 SDK，委托、状态推进与产物的落地细节看这里。**差异**：SDK 的任务状态枚举用的是协议名（`TASK_STATE_COMPLETED` 这类 proto 风格的名字，序列化时按原样输出），本章实现用的是协议文档里的简写（`completed`）。接真实 SDK 时要按它的序列化形式对齐——这正是"协议差异要显式处理"的又一个例子。

## 六、坑与检查清单

### 薄适配层

- [ ] 协议层只做翻译，不含任何校验/幂等/业务规则
- [ ] `tools/list` 的 schema 直接来自注册表，没有第二份
- [ ] 工具执行只走 `registry.invoke()` 一条路径
- [ ] 内部直调与协议调用的行为**逐字一致**

### 错误表达

- [ ] 请求结构错误 → JSON-RPC `error`
- [ ] 工具参数取值错误 → `isError: true` 的执行错误
- [ ] 工具内部异常 → `isError: true` 的执行错误
- [ ] 能纠正的失败**没有**被包装成协议错误
- [ ] 两者的分界有测试断言

### 幂等

- [ ] 副作用工具的幂等键从稳定的请求标识派生
- [ ] 同一个标识重试 → 工具**没有**真执行第二次
- [ ] 不同标识 → 是不同的动作，该执行就执行
- [ ] 只读工具不生成幂等键

### 版本与兼容

- [ ] 支持一个版本**集合**，不是硬编码单个版本
- [ ] 客户端要了不认识的版本 → 回一个支持的版本，不拒绝
- [ ] 版本差异集中在表里，主流程没有散落的版本判断
- [ ] 新增版本只改表，不改主流程
- [ ] 明确声明自己**不支持**哪些版本

### 传输与编码

- [ ] stdout 只跑协议帧，日志全走 stderr
- [ ] 标准流显式切 UTF-8（Windows 上尤其重要）
- [ ] 通知（无 id）不返回响应
- [ ] 非法 JSON 行返回 parse error，且 `id` 为 null
- [ ] 未知方法返回 method not found，不崩

### A2A

- [ ] 卡片里的能力声明与实现**对得上**
- [ ] `SendMessage` 立刻返回任务，不阻塞
- [ ] 任务有完整状态机，终态不可再推进
- [ ] 产出物落在 `artifacts` 里，独立于消息历史
- [ ] 任务 id 节点内唯一（别用时间戳）
- [ ] 委托失败 → `failed` 状态，不是崩溃

### 常见坑

- [ ] **在协议层重写工具校验**：两条路行为分叉的根源
- [ ] **把能纠正的失败报成协议错误**：模型看不到该改什么
- [ ] **副作用工具没有幂等键**：跨进程重试重复扣款
- [ ] **版本写死**：客户端一升级就连不上
- [ ] **stdout 打日志**：客户端解析错位
- [ ] **Windows 上不切 UTF-8**：中文工具描述变乱码
- [ ] **用时间戳当任务 id**：同一毫秒内撞车，委托凭空消失

## 七、小结与延伸阅读

这一章把"自建 Agent"变成"别人能调用的服务"。要点回顾：

1. **协议层是薄适配层。** 工具只写一遍，schema 一份，执行一条路径。协议层多写一行业务判断，两条路就开始分叉。
2. **失败分两条通道，判据是"模型能不能自己纠正"。** 能纠正的走执行错误（`isError: true`），纠正不了的走协议错误——前者模型看得见、能改；后者模型白看。
3. **幂等键要跨进程稳定。** 本地靠任务 id，协议层靠 request id。没有它，重试就是重复扣款。
4. **A2A 的三件套各管一件事。** AgentCard 是能力名片，Task 是生命周期，Artifact 是交付物。产出物独立于消息，才谈得上存档与复用。
5. **版本差异写成表。** 新版本来了加一行表项，而不是在主流程里加一个 `if`。

下一章 `p12-observability` 接着讲可观测性：协议层把 Agent 暴露出去之后，跨进程的调用链更难复盘——trace、指标与成本账该怎么接上。

**延伸阅读**

- 第一卷第 24 章《Agent 间通信协议：MCP、A2A 与内部消息设计》——两层协议的分工与误用场景
- 第一卷第 39 章《协议与生态全景》——`2026-07-28` 修订的无状态化与 Tasks 外移
- 第二卷第 4 章 `p04-tool-layer`——工具契约、错误回灌与幂等语义
- 第二卷第 6 章 `p06-agent-loop`——本地调用时幂等键是怎么生成的
- MCP 规范仓库：<https://github.com/modelcontextprotocol/modelcontextprotocol>
- MCP Python SDK：<https://github.com/modelcontextprotocol/python-sdk>
- A2A Protocol：<https://github.com/a2aproject/A2A>
- a2a-python：<https://github.com/a2aproject/a2a-python>
