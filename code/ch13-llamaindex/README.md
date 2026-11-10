# ch13 数据驱动的 Agent 最小可实现

对应《开源 Agent 架构技术全书》第 13 章《LlamaIndex：数据驱动的 Agent 范式》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**：用 1217 行代码把 LlamaIndex 的两条主线写清楚——
**数据层**（文档 → 切块 → 索引 → 检索）与**编排层**（事件驱动工作流 → 多 Agent 交接）。

> 说明：本目录不是 LlamaIndex 的移植版，是一个**教学用的最小复刻**。
> 它用标准库复现了索引、混合检索、事件驱动工作流、多 Agent 交接的运行语义，
> 目的是让你看懂机制；生产项目请直接用 LlamaIndex。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）
- 事件驱动部分用标准库的 `asyncio`

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch13-llamaindex
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

```bash
python main.py                    # 跑全部三个场景
python main.py --demo coupling    # 只跑耦合形态对比
python main.py --demo workflow    # 只跑事件驱动工作流
python main.py --demo agents      # 只跑多 Agent 交接
python main.py --trace            # 打印完整事件流
```

内置一份 12 篇文档的内部知识库（运维手册 7 篇 + 制度 4 篇 + 限流降级 1 篇），
切块后 21 个节点。

## 场景一：检索作为内置能力 vs 检索作为工具（coupling）

同一个问题，两种接法：

| | 检索作为内置能力 | 检索作为工具 |
| --- | --- | --- |
| 谁决定查不查 | 系统，一定查 | 模型自己决定 |
| 库外问题 | 照样塞 top-k 片段进上下文 | 一次都不查 |
| 上下文成本 | 固定 | 按需 |

典型输出：

```
【库外问题】1 加 2 等于几？

-- 检索作为内置能力（一定会查） --
  检索次数   : 1
  注入上下文 : 3 条 / 约 159 token
  回答来源   : 检索结果
  回答       : 检索到的 3 条上下文与问题没有共同关键词，无法据此作答。

-- 检索作为工具（模型自己决定） --
  检索次数   : 0
  注入上下文 : 0 条 / 约 0 token
  回答来源   : 模型自身
  回答       : 3
```

这条对比是本章的核心：**内置形态的代价是固定的，工具形态的代价是可变的。**
反过来，内置形态在"模型自己忘了查"这件事上更稳——它没有忘记的选项。

### mock 说明（重要）

内置的"模型"是一套规则，不是真实模型：

- 需要查库的题（命中 ≥2 个词）→ 走检索；
- 检索片段与问题一个词都不重合 → 明说"上下文与问题无关"，不硬答；
- 简单算术 → 直接算。

真实模型的行为必然不同（尤其是"上下文无关时会不会硬答"这一点）。
要评估真模型的行为，用 `--live`。

## 场景二：事件驱动工作流（workflow）

```
事件流（7 个事件）：
   1. StartEvent(question=...)
   2. RetrieveEvent(source=vector, query=...)
   3. RetrieveEvent(source=bm25, query=...)
   4. CollectEvent(等待两路 RetrievedEvent)
   5. RetrievedEvent(source=vector, 3 条)
   6. RetrievedEvent(source=bm25, 3 条)
   7. SynthesizeEvent(3 条片段)
```

三个看点：

1. 两个 `RetrieveEvent` 同时发出，两路检索**并行**跑，谁先回来谁先进队列；
2. `CollectEvent` 用 `collect_events` 等两路 `RetrievedEvent` 到齐——这是唯一的汇合点；
3. 每一步的输入输出都是事件，所以整条链路就是一份可回放的事件日志。

## 场景三：多 Agent（agents）

路由 Agent 判断问题属于哪个领域，用 `HandoffEvent` 交给对应专家：

```
事件流（7 个事件）：
   1. StartEvent(question=...)
   2. HandoffEvent(target=runbook)
   3. AgentThinkEvent(agent=runbook, round=0)
   4. AgentToolEvent(agent=runbook, query=...)
   5. AgentObserveEvent(agent=runbook, 2 条)
   6. AgentThinkEvent(agent=runbook, round=1)
   7. AgentDoneEvent(agent=runbook_agent)
```

- 路由 Agent 只做一件事——判断问题该给谁；
- 交接靠 `HandoffEvent`，专家之间不直接调用；
- 专家内部的「想 → 调工具 → 看结果 → 再想」也是事件，不是循环里的共享变量；
- 每个专家有**自己那份**索引：运维手册的 Agent 看不到报销制度；
- 库外问题路由到 `general_agent`，它没有索引，直接答。

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

live 模式下有三处换成真实模型：

| 方法 | 真实模型要做的事 |
| --- | --- |
| `classify()` | 判断问题属于 runbook / policy / general，返回 JSON |
| `decide()` | 决定调不调检索工具，返回 JSON |
| `synthesize()` | 根据检索片段作答 |

**索引、检索、工作流、多 Agent 交接全部不变**——这正是要验证的：
换成真模型之后，数据层和编排层的行为是不是还是可预期的。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--demo` | `all` | `all` / `coupling` / `workflow` / `agents` |
| `--trace` | 关 | 打印完整事件流 |
| `--live` | 关 | 切真实模型 API |
| `--model` | 空 | 覆盖 `LLM_MODEL` |
| `--base-url` | 空 | 覆盖 `LLM_BASE_URL` |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `Document` / `Node` / `chunk_document()` | 数据接入与切块（带重叠，避免答案被切在边界上） |
| `tokens_of()` | 分词：英文按词、中文按二元组；单字符英文/数字丢弃 |
| `MockEmbedder` | 确定性假 embedding，用 `hashlib` 而非内置 `hash()`（后者跨进程会变） |
| `VectorIndex` / `BM25Index` | 两路索引：向量余弦 + 关键词 BM25 |
| `FusionRetriever` | RRF 倒数排名融合：只看名次，不看原始分数 |
| `run_builtin_retrieval()` | 检索作为内置能力 |
| `run_tool_retrieval()` | 检索作为工具 |
| `Event` 及其子类 | 类型化事件：Start / Retrieve / Retrieved / Collect / Synthesize / Handoff / Agent* / Stop |
| `Workflow.step()` | 按事件类型注册步骤 |
| `WorkflowContext.send_event()` | 发事件 |
| `WorkflowContext.collect_events()` | 等事件到齐——事件驱动里唯一的汇合点 |
| `WorkflowRunner` | 事件循环：取事件 → 交步骤 → 新事件回队列 |
| `SpecialistAgent.think()` | 专家的"想"：用自己那份索引判断要不要查 |
| `build_agent_workflow()` | 路由 + 交接的多 Agent 工作流 |

### 三个值得抄走的实现细节

1. **`collect_events` 用 `asyncio.Condition`，不用裸 `Event`**。Event 的「置位—清零」在多个等待者之间会丢唤醒信号，表现就是"偶尔超时"。Condition 的「持锁检查条件 → wait() 原子释放锁」才不漏事件。
2. **纯载荷事件也要登记为可收集**。`RetrievedEvent` 没有对应的步骤，但别的步骤要等它——所以事件在"处理完（哪怕没有处理器）"之后就要进 `collected`。
3. **专家判断"要不要查"用自己那份索引的命中数**。全局的命中数没有意义：运维手册的 Agent 拿着"报销制度"当依据去查，查到的是空的。

## 换成自己的场景时要改什么

1. 改 `CORPUS`：换成你的文档，并给每篇打上 `domain` 标签（路由靠它）；
2. 改 `chunk_document()` 的 `chunk_size` / `overlap`：长文档和短文档的合适值不一样；
3. 改 `MockEmbedder`：真接向量库时换成真的 embedding，检索部分不用动；
4. 改 `FusionRetriever`：两路、三路都行，改 `retrievers` 字典即可；
5. 改 `build_agent_workflow()`：加专家 = 加一份索引 + 加一个 `SpecialistAgent`，**不用改工作流**——这是事件驱动最实际的收益。

## 相关章节

- 第 10 章：上下文工程（检索结果往上下文里塞的预算问题）
- 第 12 章：LangChain 与 LangGraph（另一种编排范式：显式状态图）
- 第 14 章：AutoGen 与 OpenAI Agents SDK（对话式多 Agent 与轻量编排）
- 第 19 章：框架选型决策树与常见反模式（什么时候**不要**上 Agent 框架）
