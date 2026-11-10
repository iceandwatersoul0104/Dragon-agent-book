# ch18 TypeScript 生态最小可实现

对应《开源 Agent 架构技术全书》第 18 章《TypeScript 生态：Mastra、Vercel AI SDK 与 Agents SDK JS》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**：用 910 行代码把 TS 侧生态的三块核心机制各写一遍。

- **路线一 Vercel AI SDK 风格**：UI 优先的流式协议 + `useChat` 客户端状态机
- **路线二 Mastra 风格**：`Agent` / `Workflow` / `Memory` / `Eval` 四层 + 观测 span
- **路线三 沙箱策略对比**：Node 子进程 / Deno 权限模型 / WASM / 容器

> 说明：本目录是**教学用的最小复刻**，不是这三个库的移植版。
> 它用标准库复现了协议分片、消息状态机、工作流挂起恢复、记忆召回、评测打分等运行语义，
> 目的是让你看懂机制；生产项目请直接用对应库。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch18-ts-ecosystem
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

```bash
python main.py                    # 跑全部场景
python main.py --demo stream      # 只跑流式协议
python main.py --demo mastra      # 只跑 Mastra 四层
python main.py --demo sandbox     # 只跑沙箱策略对比
python main.py --trace            # 打印完整协议分片与 span 树
```

## 场景一：流式协议 + useChat 客户端状态机（stream）

服务端吐的是**协议分片序列**，不是一个字符串：

```
服务端吐出的协议分片（共 7 个）：
  text-delta   '我来查一下订单。'
  tool-call    lookup_order {"order_id": "A-91"}
  tool-result  '已发货，运单 SF7788，预计明天送达'
  text-delta   '订单 A-91 已发货，'
  text-delta   '运单 SF7788，'
  text-delta   '预计明天送达。'
  finish       {"inputTokens": 6, "outputTokens": 17, "steps": 2}

客户端 useChat 还原出的消息状态：
  消息 msg_1_part_1（role=assistant, status=complete）
    文本     ：我来查一下订单。订单 A-91 已发货，运单 SF7788，预计明天送达。
    工具调用 ：['lookup_order']
    工具结果 ：['已发货，运单 SF7788，预计明天送达']
    parts 数 ：6（文本和工具调用是同一条消息的 part）
```

**工具调用是消息的一个 part，不是独立消息**——UI 才好把它渲染成"正在调用 lookup_order"的卡片。
`useChat` 的全部价值就是这个状态机：吃分片、维护消息、标记完成。

## 场景二：Mastra 风格的四层（mastra）

```
第一次运行（走到审批就挂起）：
  挂起点     ：approval
  已算出     ：research=已发货，运单 SF7788，预计明天送达
人工答复 approve 后恢复：
  approved   ：True
  brief      ：简报：订单 A-91 已发货，运单 SF7788，预计明天送达

记忆召回（第二轮只说了「那退款呢？」）：
  [语义召回] 订单 A-92 什么时候退款？
  [最近消息] 订单 A-91 到哪了？
  [最近消息] 订单 A-91 已发货，运单 SF7788

评测：
  打分器     ：accuracy
  是否通过   ：True（score=1.00）
  理由       ：命中 2/2 条要点：已发货、运单
```

四点值得看：

1. **工作流会挂起**，人工答复后从挂起点继续往下跑（不是重头再来）；
2. **记忆分两块**——最近几条消息 + 语义上相关的旧消息，后者才是"跨轮记忆"；
3. **评测是可插拔的打分器**，rubric 命中几条算几条；
4. **观测是内建的**，每个步骤一个 span，不用自己埋点。

## 场景三：沙箱策略对比（sandbox）

```
操作            Node 子进程        Deno 权限模型       WASM 沙箱         容器 / 托管沙箱
------------------------------------------------------------------------------
read_file     放行              放行              拦截              放行
write_file    放行              放行              拦截              放行
net_request   放行              放行              拦截              放行
spawn_process 放行              拦截              拦截              放行
read_env      放行              拦截              拦截              拦截
alloc_memory  放行              放行              放行              放行
```

四档策略的边界：

- **Node 子进程**：没有权限模型。只有 `--max-old-space-size` 能限内存，靠超时杀进程；文件和网络完全敞开。
- **Deno 权限模型**：默认全禁，逐项 `--allow-read` / `--allow-write` / `--allow-net` / `--allow-run` 打开。能控文件和网络，仍限不住内存。
- **WASM 沙箱**：guest 只能调用宿主显式导出的能力，内存有上限。隔离最严，代价是要重写一批宿主 API 绑定。
- **容器 / 托管沙箱**：文件系统与网络按需挂载，资源用量可封顶。TS 生态最终大多落到这一层。

## 真实模型（`--live`）

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
`choices[0].message.content`。换供应商只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

live 模式下只有"模型返回什么"这一处换成真模型，**协议分片、客户端状态机、工作流挂起恢复、记忆召回、评测打分全部不变**——这正是要验证的：换模型之后，前端那一层的契约还稳不稳。

### mock 说明（重要）

内置的"模型"是按脚本返回固定内容的替身，不是真实模型。
它不代表任何真实模型的表现——真实模型的分片粒度、工具选择、措辞每次都可能不同。
要评估真模型的行为，用 `--live`。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--demo` | `all` | `all` / `stream` / `mastra` / `sandbox` |
| `--trace` | 关 | 打印完整协议分片与 span 树 |
| `--live` | 关 | 切真实模型 API |
| `--model` | 空 | 覆盖 `LLM_MODEL` |
| `--base-url` | 空 | 覆盖 `LLM_BASE_URL` |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `zod_schema()` | 从签名生成 JSON Schema——TS 里这活儿是 Zod 干的 |
| `Chunk` | 协议分片：`text-delta` / `tool-call` / `tool-result` / `finish` |
| `ToolCallingAgent.stream()` | 服务端：产出的不是文本，是分片序列 |
| `UseChat` | 客户端状态机：吃分片、维护消息、标记完成 |
| `Message.parts` | 工具调用是消息的 part，不是独立消息 |
| `Workflow` | Mastra 风格工作流：`.then()` / `.branch()` / 挂起 / 恢复 |
| `Workflow._execute()` | 从第 N 步继续跑——恢复后要接着往下走 |
| `Memory` | `resource` + `thread` + 最近消息 + 语义召回 |
| `accuracy_scorer()` | 可插拔打分器 |
| `Span` / `Tracer` | 观测 span 树 |
| `SANDBOX_POLICIES` | 四种隔离策略对同一组操作各放行什么 |

### 三个值得抄走的实现细节

1. **协议分片是唯一的契约**。服务端换模型、换框架，前端的 `useChat` 一行不用改——前提是分片类型不变。
2. **工具调用挂成消息的 part**。这样一条 assistant 消息里可以同时有文本、工具调用和工具结果，UI 才渲染得对。
3. **恢复后要接着往下跑**。挂起不是终止——`resume()` 把人工答复喂给那一步，然后继续跑后面的步骤和分支。忘了这一步，工作流会停在半路。

## 换成自己的场景时要改什么

1. 改 `Chunk` 的类型定义——这是你前后端之间的契约，改动要前后端同步；
2. 改 `UseChat.consume()` 的状态机逻辑——UI 的行为都在这儿；
3. 改 `build_mastra_workflow()` 换你的工作流步骤；
4. 改 `Memory.recall()` 的召回策略——TS 侧通常换成向量库 + embedding；
5. 改 `accuracy_scorer()` 换你的评测口径。

## 相关章节

- 第 9 章：Code-Act（代码执行的沙箱要求与风险）
- 第 12 章：LangChain 与 LangGraph（状态图路线，TS 侧对应 LangGraph.js）
- 第 14 章：AutoGen 与 OpenAI Agents SDK（Agents SDK JS 的 Python 兄弟）
- 第 19 章：框架选型决策树与常见反模式（前端驱动的项目该怎么选）
