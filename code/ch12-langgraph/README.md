# ch12 状态图式编排最小可实现

对应《开源 Agent 架构技术全书》第 12 章《LangChain 与 LangGraph：状态图式编排范式》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**：用 1092 行代码复刻了状态图编排的三个核心语义——
**归约（reducer）**、**检查点（checkpointer）**、**中断（interrupt）**。

> 说明：本目录不是 LangGraph 的移植版，而是一个**教学用的最小复刻**。
> 它用标准库把 LangGraph 的运行语义写清楚（超步、归约合并、检查点链、中断重跑），
> 目的是让你看懂机制；生产项目请直接用 LangGraph。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch12-langgraph
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

```bash
python main.py                    # 跑全部三个场景
python main.py --demo triage      # 只跑事故分诊（条件边 + 中断 + 返工循环）
python main.py --demo merge       # 只跑并行合并（add 归约 vs overwrite 覆盖）
python main.py --demo fork        # 只跑时间旅行（回滚检查点 + 人工修正后重放）
python main.py --approver reject  # 审批人一律驳回，看返工与升级路径
python main.py --max-rounds 1     # 返工几轮后升级给人
python main.py --trace            # 打印每一步的节点与写入
```

### 场景一：事故分诊（triage）

流程：`plan → collect → draft → approval →（批准）apply → report`，
`approval` 被驳回则走 `revise → draft` 返工，超过 `--max-rounds` 升级到 `report`。

典型输出：

```
[人工介入] 待审批内容：
  type           approval
  draft          针对「支付网关错误率突增」的处置草案：
                 - fetch_metrics：错误率 3.2%（基线 0.4%），p99 延迟 1800ms，受影响实例 12/40
                 - fetch_logs：最近一次发布 v2.4.1 后出现大量 NullPointerException，堆栈指向连接池配置
  evidence_count 2
  round          1
[人工介入] 答复：reject

[人工介入] 答复：approve

最终状态：
  decision = approved
  rounds   = 1
  evidence = 2 条
  result   = 已批准并执行。证据 2 条，任务「支付网关错误率突增」结束。

检查点历史（最新的在前，共 12 条）：
  927362   step=10 source=loop      next=-
  97bcf4   step=9  source=loop      next=report
  590a3f   step=8  source=loop      next=revise
  a95f1d   step=7  source=interrupt next=approval        [挂起于 approval]
  93e63c   step=7  source=loop      next=approval
  ffc45e   step=6  source=loop      next=draft
  6adbea   step=5  source=loop      next=revise
  43171d   step=4  source=interrupt next=approval        [挂起于 approval]
  ...
```

注意 `source=interrupt` 那一行：**审批挂起也是一个检查点**，流程停在那里等外部输入。

### 场景二：并行合并（merge）

同一张图，只换 channel 的归约函数：

```
add 归约（累加）：
  证据条数 = 2
  证据内容 = metrics / logs

overwrite 覆盖：
  证据条数 = 1
  证据内容 = logs
```

覆盖语义下，后写的分支把先写的分支整个吃掉了；累加语义下两个分支的产出都保住了。
这就是"归约比覆盖更适合并发合并"的最短证明。

### 场景三：时间旅行（fork）

1. 先跑完原始走向（驳回一次再批准）；
2. 挑出"第一次进入审批之前"的检查点；
3. `update_state()` 人工把 draft 改成人工修正版、并预置 `decision=approved`，生成一个 `source=update` 的新检查点；
4. 从这个新检查点重放，得到另一条分支。

两条分支的检查点共存于同一个 thread 的历史里，原始轨迹一个字节没改。

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

live 模式下只有 `plan()` / `draft()` / `summarize()` 三处换成真实模型，
**编排逻辑、归约、检查点、中断语义完全不变**——这正是想验证的东西：
换成真模型之后，流程还是不是按同样的状态在走。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--demo` | `all` | `all` / `triage` / `merge` / `fork` |
| `--approver` | `reject-once` | `approve` 一律批准 / `reject` 一律驳回 / `reject-once` 先驳回一次再批准 |
| `--max-rounds` | `2` | 驳回后最多返工几轮，超了就升级给人 |
| `--trace` | 关 | 打印每一步的节点与写入 |
| `--live` | 关 | 切真实模型 API |
| `--model` | 空 | 覆盖 `LLM_MODEL` |
| `--base-url` | 空 | 覆盖 `LLM_BASE_URL` |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `overwrite()` / `add()` / `add_messages()` | 归约函数：覆盖、累加、按 id 合并消息 |
| `Channel` / `ch()` / `State` | 状态 schema：通道 + 归约 + 初始值 |
| `interrupt()` / `GraphInterrupt` | 中断：挂起与恢复 |
| `Checkpoint` / `MemorySaver` | 检查点：快照存储、按 thread 取历史 |
| `StateGraph` | 构建器：`add_node` / `add_edge` / `add_conditional_edges` |
| `CompiledGraph._run()` | 执行引擎：超步循环、归约合并、落检查点 |
| `CompiledGraph._next_frontier()` | 调度：谁能跑、谁要等 |
| `CompiledGraph.update_state()` | 状态修正：人工改状态后生成新检查点 |
| `build_triage_graph()` | 场景一：条件边 + 中断 + 返工循环 |
| `build_merge_graph()` | 场景二：并行分支 + 归约对比 |
| `MockLLM` / `LiveLLM` | 模型层：mock 走规则，live 走 API |

### 三个值得抄走的实现细节

1. **调度判据**（`_next_frontier`）不看静态拓扑，只看两件事：这个节点有没有"待办的新工作"
   （被路由次数 > 已完成次数），以及它真正走到的前置节点是不是都跑完了。
   静态拓扑会把"两条互斥分支汇合到同一个节点"永远卡住——这正是这个实现里最容易踩的坑。
2. **中断即丢弃**：节点抛中断时，它此前对状态的写入全部作废，状态停在它执行之前。
   恢复时节点从头重跑，所以 `interrupt()` 之前的副作用必须幂等。
3. **归约写在 schema 里，不在节点里**：节点只管返回增量，合并规则由 channel 的归约函数决定。
   换掉归约函数就能换掉并发语义，不需要改任何节点代码。

## 换成自己的场景时要改什么

1. 改 `build_triage_graph()` 里的 `schema`——每个 channel 的**归约函数**是首要决定；
2. 节点函数签名统一是 `fn(state) -> dict`，返回的是**增量**不是全量状态；
3. 条件边的路由函数必须是状态的纯函数（不发请求、不写文件），否则图不可复现；
4. 真实项目里把 `MemorySaver` 换成 SQLite 或 Postgres 的 checkpointer，接口保持一致；
5. mock 的 `TOOLS` 字典换成你的真实工具，节点逻辑不用动。

## 相关章节

- 第 11 章：停止准则、步数上限与成本守卫（图的终止条件该怎么设）
- 第 13 章：LlamaIndex（数据驱动视角下的另一种编排）
- 第 19 章：框架选型决策树与常见反模式（什么时候**不要**用状态图）
