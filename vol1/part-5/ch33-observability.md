# 第 33 章 可观测性：Trace、Span 与 Agent 专属指标

## 一、本章要解决的问题

线上一个客服 Agent，任务平均耗时 40 秒、平均 100 次模型调用。某天早上 P95 涨到 3 分钟，错误率纹丝不动，CPU、内存、网关 5xx 全部正常。值班同学盯着 Grafana 看了半小时，因为传统面板上根本没有能解释这件事的指标——没有报错，没有异常请求，只是变慢了。

更糟的是第三天，业务方反馈"答案质量下降"，但系统视角一切健康：HTTP 200，成功率 99%。真相是 Agent 在一个工单上绕了 12 步，其中 6 步在重复读同一个页面，最后给了一个基于第 2 步旧数据的答案。全链路没有任何异常。

这三件事暴露了 Agent 可观测性的根本困难：

1. **请求-响应模型不成立**。传统 trace 一次请求一个 span；Agent 一次任务可能是几十次模型调用加几十次工具调用，跨越几十秒到几分钟，中间还可能有人工审批等上几小时。
2. **服务边界是动态的**。Agent 会不会调某个工具、调哪个子 Agent、检索什么库，都是运行时决定的，静态拓扑图没有意义。
3. **失败不等于异常**。"没做成"不是 500：绕圈、空转、答非所问、用过期数据，全都是 HTTP 200。
4. **成本的单位变了**。传统服务盯 CPU 和内存；Agent 的主要成本是 token，主要延迟是任务端到端时长和"第一次有效进展"之前的空转。

所以 Agent 可观测性的目标不是"看住服务"，而是回答产品问题：**任务成功率为什么掉了？哪一步最贵？哪一步在空转？换掉哪个模型能省多少钱？**

## 二、原理：机制拆解

### 2.1 Trace 边界要重新定义

传统追踪（tracing）里 trace = 一次 HTTP 请求。Agent 场景必须换：**trace = 一次任务（task / run）**，一个跨度（span）= 任务里的一步，嵌套关系用 parent span 表达。

```
trace: task_run (一次任务)
└── span agent.session                     会话
    ├── span agent.step  (index=1, type=think)
    │   └── span llm.chat                  模型调用
    ├── span agent.step  (index=2, type=act)
    │   ├── span llm.chat                  模型调用（含工具选择）
    │   └── span tool.call  name=search    工具调用
    │       └── span tool.http             工具的子 span
    ├── span agent.step  (index=3, type=observe)
    │   └── span retrieval.search          检索
    ├── span agent.handoff  to=researcher  交接给子 Agent
    │   └── span agent.run  (子 Agent 自己的 trace 片段)
    ├── span agent.tool_error  name=search 工具失败
    └── span agent.final_answer            终态
```

一条硬规则：**子 Agent 是一个 span，它内部的步骤是它的子 span**，不要给子 Agent 另开一条互不相干的 trace，否则跨 Agent 的因果链就断了。

### 2.2 Span 命名规范

用 `namespace.entity.action` 三段式，小写、点分、全局唯一：

- Agent 层：`agent.run`、`agent.step`、`agent.plan`、`agent.act`、`agent.handoff`、`agent.final_answer`
- 模型层：`llm.chat`、`llm.embedding`、`llm.rerank`
- 工具层：`tool.call`、`tool.http`、`tool.db`、`tool.shell`、`tool.browser`
- 检索层：`retrieval.search`、`retrieval.rerank`

两条纪律：**span 名里绝不拼参数**（`tool.call:search_q=xxx` 会直接把时序库打爆）；**高基数字段一律放属性**。

事件（event）是时间点，没有持续时间，用于表达"发生了一件事"：

- `agent.approval_requested`、`agent.approval_granted`、`agent.approval_rejected`
- `agent.injection_detected`、`agent.budget_exceeded`
- `agent.no_progress_detected`、`agent.retry_scheduled`

跨进程传递上下文时，用 W3C traceparent 风格的头把 trace_id 与 span_id 传下去，下游服务建的 span 以它为 parent。跨 Agent 的交接（`agent.handoff`）也要记清 from / to，否则子 Agent 烧的 token 算不到调用方头上，账单永远对不齐。

### 2.3 Span 字段设计

模型调用相关的属性，OpenTelemetry 的 GenAI 语义约定（Semantic Conventions for Generative AI）已经给了标准名。对齐它，导出层才能换后端而不改业务代码：

| 标准属性 | 用途 | 常见坑 |
|---|---|---|
| `gen_ai.operation.name` / `gen_ai.provider.name` | 操作类型与厂商，都是必填 | operation.name 用约定值：`chat`、`generate_content` |
| `gen_ai.request.model` / `gen_ai.response.model` | 成本归因、模型对比 | 别用对外别名当标识，要能对上账单 |
| `gen_ai.usage.input_tokens` / `output_tokens` | 成本、上下文预算 | input_tokens 要把缓存命中的那部分也算进去 |
| `gen_ai.usage.cache_read.input_tokens` / `cache_creation.input_tokens` | 缓存命中率 | 读与写要分开记，否则算不出命中率 |
| `gen_ai.request.temperature` / `top_p` / `max_tokens` | 复现问题 | 缺了就无法解释"同样的输入两次结果不同" |
| `gen_ai.response.finish_reasons` | 停止原因 | 只看 finish_reason 才发现得了"答案被截断" |

Agent 侧目前只有 `gen_ai.agent.name` / `gen_ai.agent.id` 这类基础属性，步、交接、空转还没有统一约定，本文用自定义命名，接入时按自己的规范登记：

| 自定义属性 | 用途 | 常见坑 |
|---|---|---|
| `agent.step.index` / `agent.step.type` | 步数分布、空转检测 | 步类型固定为 think/act/observe/finish |
| `agent.task.id` / `agent.session.id` / `agent.user.id` | 串联与聚合 | 用户 ID 要哈希 |
| `tool.name` / `tool.arguments` / `tool.result.status` | 工具健康度 | 参数和结果要脱敏与截断 |
| `llm.latency.first_token_ms` / `total_ms` | 体感延迟 | 首 token 延迟和总延迟是两个用户体验，必须分开 |
| `cost.input` / `cost.output` / `cost.total` | 每次成功成本 | 用本地价格表算，不要直接抄平台账单 |
| `retry.count` / `retry.reason` | 区分抖动与真故障 | 重试记在同一个 span 上，不要新开 span |
| `error.type` / `error.message` / `error.retryable` | 错误率统计 | 类型必须是固定枚举，不能是自由文本 |

> 本文配套代码 `code/ch33-observability/` 为了让示例好读，把 `gen_ai.request.model` 一类属性简写成了 `llm.model`，接真实后端时按上表映射回去。

错误类型建议固定这几个桶：`rate_limit`、`timeout`、`context_length_exceeded`、`tool_failure`、`invalid_arguments`、`permission_denied`、`policy_blocked`、`parse_error`、`upstream_error`、`cancelled`、`budget_exceeded`。只有分桶了，"工具错误率"和"空转率"才算是得出来。

### 2.4 为 Agent 设计的 SLI

这是与传统 APM 差别最大的地方。下面七个指标，前五个是必备。

1. **任务成功率**（Task Success Rate）。分母是任务数，分子是"判定为成功"的任务数。判定必须发生在任务层，不能用 HTTP 200 代替；可用规则 + 人工抽检 + LLM Judge 组合（见第 34 章）。
2. **步数分布**（Steps per Task）。p50/p90/p99 加上"每步成功率"。步数分布的漂移比成功率更早暴露问题——成功率是滞后指标。
3. **工具错误率**（Tool Error Rate）。按工具名分桶，并区分"工具自身失败"和"参数不合法"。整体错误率会掩盖某个具体工具的退化。
4. **无进展率**（No-progress Rate）。连续 N 步没有产生任何状态变化（新文件、新工具成功、observation 内容变化）的任务占比。这是 Agent 专属指标，专治"在转圈但没报错"。
5. **每次成功成本**（Cost per Successful Task）。总 token 成本除以成功任务数。比"每次调用成本"有意义得多——后者会因为多绕圈而显得更便宜。
6. **首次有效进展时延**（Time to First Useful Action）。从任务开始到第一次有效动作的时长，衡量冷启动、检索效率与工具握手开销。
7. **人工介入率**（Human Intervention Rate）。触发审批或降级到人工的任务占比。

给 SLO 的一条提醒：**不要对"步数"定 SLO**。步数少可能是高效，也可能是模型没想清楚；步数多可能是任务复杂，也可能是空转。步数适合做告警阈值和漂移检测，适合当诊断指标，不适合当考核指标。SLO 只对成功率、成本、时延定。

### 2.5 追踪与隐私

- **脱敏在采集端做**。看板端再脱敏也没用，明文已经落库了。
- **字段级 allowlist 优于 denylist**。允许记录哪些字段，比禁止记录哪些要可靠得多。
- **prompt 全文不进默认路径**。默认记录长度、哈希、前缀片段、内容分类标签，全文进单独的受限存储，配更短的 TTL 和更严的访问控制。
- **按任务采样，不按 span 采样**。采样一个任务就带着它全部的 span 一起采或一起丢。
- **采样会让成功率有偏**。所以成功率、步数、成本这些 SLI 必须来自全量计数指标（counter / histogram），而不是从采样后的 trace 里聚合；要么至少记录采样率并在计算时加权。
- **留存期分级**：trace 短（按天计）、指标长（按月计）、审计日志按合规要求单独存。留存期到期的 trace 要能自动删干净。

### 2.6 三个信号的分工

告警看指标（低成本、低基数、长期聚合），排查看 trace（因果链），细节看日志。排障顺序是：指标发现异常 → trace 找到是哪一步 → 日志看那一步内部发生了什么。

告警设计有三条实用经验：

- **告警建立在 SLI 上，不建立在 span 上**。"任务成功率跌破 SLO"是有效告警，"某个 span 变慢"多半是噪音。
- **一次只发一件事**。同一条链路上多个指标一起报警会让人麻木，最后整条规则被静音。
- **阈值要基于历史基线与同环比**，不要拍脑袋定绝对值。步数、成本、延迟的基线都会随业务季节性漂移。

## 三、为什么这样设计：取舍与失效条件

1. **不要把 Agent 当普通微服务接 APM。** HTTP span 只能告诉你"这次模型调用花了 3 秒"，告诉不了"模型调了 30 次才收敛"。`agent.step` 这一层必须自己定义，框架不会替你生成。
2. **全量 trace 的成本会压垮存储。** 一个任务 100+ span，百万任务/天就是上亿 span。按任务采样，不要按 span 采样；再配"轻量指标全量、trace 抽样"的双轨。
3. **LLM Judge 判成功率有成本和延迟**。不要对每个任务都过 Judge：关键路径全判，长尾抽检，规则能定的一律用规则兜底。
4. **"无进展率"要状态快照，代价不低。** 不要每步做全量 diff，只 diff 关键信号：新增文件列表、工具成功与失败、observation 的哈希。
5. **记录完整 prompt 能 debug，但代价是钱和合规风险。** 分级存储 + 明确 TTL + 访问审计，别在默认路径上记全文。
6. **采样会污染指标**。用采样的 trace 算成功率，得到的是"被采样的任务的成功率"，可能有偏。成功率从全量指标算。
7. **指标标签基数会爆炸。** 不要把 prompt、文件名、工具参数做成 label，只保留低基数维度（模型名、工具名、结果状态）。
8. **平台绑定是负债。** 语义层尽量对齐 OpenTelemetry 的 GenAI 语义约定，导出层保持可换，否则换后端就得改代码。
9. **别只盯单次调用延迟。** 用户体验是任务端到端时长与首次有效进展时延，单次调用延迟降了但空转变多，端到端反而更慢。
10. **看板不要贪多。** 每个 span 属性都做成一张图，结果是没有一张图有人看。真正每天盯的不超过五张：成功率、每次成功成本、端到端时延分布、工具错误率、空转率；其余留给排障时按需下钻。

## 四、动手：最小可实现

参考实现 `code/ch33-observability/`，纯标准库，不需要 API Key 就能跑通。它把上面四件事做成一个可运行的最小实现：

1. **Trace/Span 运行时**：W3C 风格的 trace_id / span_id / parent_span_id，嵌套上下文管理器，自动计时，异常自动归类到错误枚举。
2. **LLM span**：记录模型、token（prompt/completion）、参数、首 token 延迟与总延迟，用本地价格表算成本。
3. **脱敏与采样**：字段级 allowlist + 正则脱敏；采样策略支持 always / ratio / error_or_slow 三类，按任务粒度决策并记录实际采样率。
4. **SLI 计算**：从全量计数指标（不是从采样 trace）算出任务成功率、步数分布、工具错误率、无进展率、每次成功成本。

Mock 场景跑三个任务：一次正常完成、一次工具失败后重试成功、一次连续空转。跑完打印 SLI 报表。`--live` 打开后用真实 API 跑一轮单任务并记录真实 span。

```python
# 核心数据结构（节选，全文见 code/ch33-observability/main.py）
@dataclass
class Span:
    name: str
    span_id: str
    trace_id: str
    parent_id: Optional[str] = None
    attrs: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    start: float = 0.0
    end: float = 0.0
    status: str = "ok"

    @property
    def duration_ms(self) -> int:
        return int((self.end - self.start) * 1000)
```

```python
# 无进展检测：只 diff 关键信号，不做全量快照
def is_progress(prev: StateSnapshot, cur: StateSnapshot) -> bool:
    return (cur.new_files != prev.new_files
            or cur.observations != prev.observations)
```

## 五、开源实现对照

| 方案 | 强项 | 代价 | 适合 |
|---|---|---|---|
| **Langfuse**（核心 MIT，EE 目录企业许可） | 自托管，内置 LLM 领域概念（trace / generation / observation），提示管理与评估一体 | 核心 MIT 但 EE 目录是商业许可；自托管要自己运维数据库 | 想自己掌控数据、又不想从零设计 span 语义 |
| **Arize Phoenix**（Elastic License 2.0） | 走 OpenTelemetry 原生，能接既有 APM；内置嵌入评估 | 许可证不是 MIT，商用前要确认条款；Agent 语义仍要自己补 | 已有 OTel 体系，想把 Agent 塞进统一后端 |
| **OpenLLMetry / Traceloop**（Apache-2.0） | 自动插桩把 LLM 调用转成 OTel span，改造成本最低 | 只覆盖"模型调用"这一层，步、交接、空转这些 Agent 语义还得自己加 | 快速拿到基础追踪 |
| **AgentOps**（MIT） | Agent 语义贴近，会话回放开箱即用 | 生态与后端选项相对少 | 需要快速看 Agent 全过程 |
| **LangSmith SDK**（MIT） | 追踪、数据集、评估一体，回归测试链路完整 | SDK 是 MIT，但平台是商业托管产品 | 已有 LangChain 系技术栈 |

对应仓库：

- Langfuse — https://github.com/langfuse/langfuse
- Arize Phoenix — https://github.com/Arize-ai/phoenix
- OpenLLMetry — https://github.com/traceloop/openllmetry
- AgentOps — https://github.com/AgentOps-AI/agentops
- LangSmith SDK — https://github.com/langchain-ai/langsmith-python
- OpenTelemetry GenAI 语义约定 — https://github.com/open-telemetry/semantic-conventions

被观测对象侧的对照：**Claude Code**（专有商业条款，https://github.com/anthropics/claude-code ）与 **OpenHands**（MIT，https://github.com/All-Hands-AI/OpenHands ）都是典型的多步 Agent，前者跑在终端里、后者跑在容器沙箱里，但它们的"一次任务 = 几十步"的结构完全一样——你的观测模型必须能表达这种结构，而不是只表达一次请求。

选型顺序上的实话：**先用 OpenLLMetry 之类的自动插桩把 LLM 调用变成标准 span 拿到 80% 的价值**，再补 `agent.step` 与事件语义补上剩下的 20%；后端在早期不重要，能导出标准格式比自带一堆私有字段重要得多。

## 六、坑与检查清单

**Trace 设计**

- [ ] trace 边界是任务/会话，不是 HTTP 请求
- [ ] span 名遵循统一命名规范，全局唯一
- [ ] 高基数字段（prompt、参数、文件名）放属性或日志，不放 span 名
- [ ] 子 Agent 用 parent span 串成同一棵因果树
- [ ] 错误类型是固定枚举，不是自由文本
- [ ] 重试记在同一个 span 上（`retry.count`），不新开 span

**字段**

- [ ] 记了 prompt/completion token 与本地成本估算
- [ ] 记了模型参数（temperature、top_p、max_tokens）
- [ ] 首 token 延迟与总延迟分开记
- [ ] 步序号与步类型（think/act/observe/finish）都记了
- [ ] 工具参数与结果做了脱敏与长度截断

**SLI**

- [ ] 有任务级成功判定，不是拿 HTTP 200 顶替
- [ ] 成功率来自全量计数指标，不是采样后的 trace
- [ ] 步数分布有 p50/p90，能看出漂移
- [ ] 工具错误率按工具名分桶
- [ ] 有无进展率或等价的空转检测
- [ ] 有"每次成功成本"，不只看单次 token 花费
- [ ] 步骤数只做诊断与告警，不进 SLO

**隐私与成本**

- [ ] 脱敏在采集端完成，不是看板端
- [ ] prompt 全文不进默认存储，全文另存且 TTL 更短
- [ ] 按任务采样而不是按 span 采样，采样率被记录下来
- [ ] trace、指标、审计三类数据各有留存期，过期能自动删
- [ ] 指标标签基数受控，没有把参数拼进去

**可操作性**

- [ ] 告警建立在指标上，trace 只用于排查
- [ ] 有"最贵的 10 个任务"视图
- [ ] 有失败任务的步数回放视图
- [ ] 语义层对齐 OTel GenAI 约定，换后端不用改业务代码

## 七、小结与延伸阅读

1. **Agent 的 trace 边界是任务，不是请求**；`agent.step` 这一层必须自己定义。
2. **错误不是异常，成功率不是 200**。要任务级判定，要固定错误分桶，要无进展率。
3. **指标全量、trace 抽样**：SLI 从计数指标算，trace 用来定位问题；采样率必须被记录。
4. **脱敏在采集端**，prompt 全文不进默认路径，采样按任务粒度。
5. **先拿到标准格式再谈后端**：语义层对齐 OTel GenAI 约定，导出层保持可换。
6. **可观测性的产出是决策，不是数据。** 每个指标都要能回答"下一步改什么"；答不上来的指标就该删掉。

延伸阅读：

- OpenTelemetry GenAI 语义约定 — https://github.com/open-telemetry/semantic-conventions
- OTel GenAI 语义约定文档 — https://opentelemetry.io/docs/specs/semconv/gen-ai/
- Langfuse — https://github.com/langfuse/langfuse
- Arize Phoenix — https://github.com/Arize-ai/phoenix
- OpenLLMetry — https://github.com/traceloop/openllmetry
- AgentOps — https://github.com/AgentOps-AI/agentops
- LangSmith SDK — https://github.com/langchain-ai/langsmith-python
- Claude Code — https://github.com/anthropics/claude-code
- OpenHands — https://github.com/All-Hands-AI/OpenHands
