# 第 12 章 可观测性：接上 trace、指标与成本账

## 一、本章要解决的问题

上线第三天，值班群里连着三个问题：

- "昨天跑失败的任务有多少？"——没人答得上来。日志里只有一行行"开始处理"和"处理完成"，没有"完成"和"失败"的**区分**。
- "哪个工具最常出错？"——更答不上来。工具报错和模型报错在日志里长得一模一样，都是一行 ERROR。
- "这个月账单为什么涨了？"——财务给了总数，工程这边给不出拆分：哪个模型花的、哪类任务花的、成功的任务平均花了多少，全都不知道。

三个问题的共同点：**系统在自己内部发生了什么，没人知道**。日志是给人看的流水，trace 是给机器复盘的结构化记录，指标是给看板做判断的计数——这三样东西不是一回事，缺一样就有一类问题答不上来。

更隐蔽的是第四个问题，它还没人问出来：某次运行烧了 0.5 美元，比平时多十倍。等有人发现的时候，那个月的账单已经超了。

可观测性要解决的就是这四件事：

1. **结构化**：每次运行留下可复盘的 trace，而不是几行看不懂的日志；
2. **指标**：成功率、工具错误率、无进展率、每次成功成本——**从全量计数算**；
3. **成本账**：每次任务归集 token 与金额，能按模型、按任务拆开看；
4. **安全**：哪些字段根本不能进日志，以及怎么采样而不把长尾问题一起丢掉。

这四件事本章用 `dragon_agent/observability.py` 落地。

## 二、原理：机制拆解

### 2.1 span 语义：三层结构与父子关系

Agent 的 trace 和普通微服务的 trace 最大区别是：**它的边界是任务，不是请求**。一次任务可能是几十次模型调用、十几个工具调用，中间还有嵌套的 Agent 调用。你需要能回答"这次任务一共花了几步、哪一步最慢、哪个工具拖了后腿"。

所以 span 是一棵树，三层：

```text
invoke_agent          ← 任务 span：这次任务最终成了还是没成
├── gen_ai.chat       ← 模型调用 span：用了哪个模型、多少 token
├── execute_tool      ← 工具调用 span：调了哪个工具、成功还是失败
└── gen_ai.chat
```

嵌套 Agent 是同一棵树的延伸：子 Agent 的 span 挂在父 span 下，`trace_id` 相同、`parent` 指回上一层。**一次任务的全部开销必须能顺着这棵树汇总到根上**——不然子 Agent 烧的 token 算不到调用方头上，账单永远对不齐。

父子关系靠上下文管理器建立。`tracer.span()` 返回的对象本身就是上下文管理器，`with` 出去时自动计时、自动接上父节点：

```python
    def span(self, name: str, **attributes: Any) -> Span:
        """开一个 span。返回的对象**本身就是上下文管理器**。

        属性在写入前脱敏截断；模型/token 类属性写错了键名会被自动归到
        ``gen_ai.*`` 下（并记一次改名），不会在运行期炸掉。
        """
        span = Span(name=name)
        span._tracer = self
        span.span_id = uuid.uuid4().hex[:16]
        for key, value in attributes.items():
            span.set(key, value)
        return span
```

不用 `try/finally` 也不用手动 `end()`——异常时 span 会被标成 `error`，异常照常往外抛。**观测层不吞异常**，这是它和业务代码的边界。

### 2.2 命名约定：语义层对齐 OTel GenAI

属性名不是随便起的。跟模型、token 沾边的属性必须落在 `gen_ai.*` 命名空间下，这是 OpenTelemetry 的 GenAI 语义约定。写成了 `llm.model`，导出到真实后端时就得映射回去——**映射层就是漏埋点和错埋点的地方**。

Agent 侧的属性行业还没有统一约定，用自定义前缀即可（`agent.*` / `tool.*` / `cost.*` / `error.*`）。

约定怎么落地？靠自觉不行，靠运行期抛异常也不行——一次拼错的键名不该让任务崩掉。正确做法是**自动归位**：

```python
def normalize_attribute_key(key: str) -> str:
    """把模型/token 相关属性归到 ``gen_ai.*`` 命名空间下。

    命名约定不该靠自觉，也不该在运行期炸掉——写错的键名（比如
    ``llm.model``）自动归位，导出层就永远拿得到标准属性。同时返回
    是否发生了改名，调用方可以据此提醒开发者去对齐。
    """
    lowered = key.lower()
    if key.startswith(GEN_AI_PREFIX):
        return key
    if any(word in lowered for word in MODEL_ATTR_KEYWORDS):
        return GEN_AI_PREFIX + key
    return key
```

注意最后一句：改名这件事**要被记录下来**（`sli()["renamed_attributes"]`）。自动归位保证了导出的正确性，但开发者还得知道自己哪里没对齐——否则约定会慢慢烂掉。

### 2.3 关键指标：五个各算什么，怎么算

| 指标 | 分子 / 分母 | 为什么要它 |
| --- | --- | --- |
| 任务成功率 | 判定成功的任务数 / 任务总数 | **判定必须发生在任务层**，不能拿"没抛异常"或 HTTP 200 顶替 |
| 步数分布 | 每个任务的步数的 p50 / p90 / max | 比成功率更早暴露问题——成功率是滞后指标 |
| 工具错误率 | 工具失败次数 / 工具调用次数 | 要**按工具名分桶**，整体错误率会掩盖某个具体工具的退化 |
| 无进展率 | 无进展任务数 / 任务总数 | Agent 专属指标，专治"在转圈但没报错" |
| 每次成功成本 | 总成本 / 成功任务数 | 比"每次调用成本"有意义——后者会因为多绕圈而显得更便宜 |

这五个指标有一个共同点：**它们都是从全量计数器算出来的，不是从 trace 算的**。

```python
    def sli(self) -> dict[str, Any]:
        """SLI 报表。**从全量计数器算，不从采样 span 算。**

        四个必备指标（第一卷第 33 章）：任务成功率、工具错误率、无进展率、
        每次成功成本。步数分布是诊断指标，**不进 SLO**。
        """
        tasks = self._task_count
        successes = self._success_count
        cost = self.meter.total()
        return {
            "task_count": tasks,
            "success_count": successes,
            "task_success_rate": round(successes / tasks, 6) if tasks else 0.0,
```

为什么强调"全量"？因为 trace 是要采样的，而**采样会污染指标**。用 10% 的 trace 算成功率，得到的是"被采样的那 10% 任务的成功率"，可能正好偏向那些你留了 trace 的场景。成功率必须来自全量计数器。

另外一条纪律：**步数不进 SLO**。步数少可能是高效，也可能是模型没想清楚；步数多可能是任务复杂，也可能是空转。它适合做告警阈值和漂移检测，不适合当考核指标。

### 2.4 成本账：按维度拆开算

"这次任务花了多少钱"和"这个月为什么涨了"是两个不同的问题。前者要一个总数，后者要**拆分**——按模型、按任务、按调用方自己定的业务维度。

`CostMeter` 的计价算法复用第 3 章的 `pricing.py`：缓存命中的输入按缓存价算，没命中的按输入价算，两者不能混。成本账自己维护三份归集：

```python
    def breakdown(self, dimension: str = "model") -> dict[str, dict[str, Any]]:
        """**按维度拆分**的成本账。

        ``dimension`` 取 ``model`` / ``task_id`` / ``dimension``。
        """
```

单价来自配置而不是写死在代码里——模型价格几个月就变一次，写死等于埋雷。调价是改配置，不是改代码、不是重新发版。

还有一个容易翻车的细节：**同一个账本不能记两遍钱**。Agent 主循环自己会算成本（它要报 `total_cost`），埋点层也会算成本（它要归集 token）。两者指向同一本账时会记两遍——所以埋点层接 Agent 时会先看一眼：Agent 自己带着 meter，就由 Agent 记，埋点层不重复记。

### 2.5 脱敏与采样：采集端就做，事后过滤来不及

**脱敏必须在写入前做。** 数据一旦落盘就算泄露了，事后过滤追不回来——备份、日志索引、告警快照里全都有那份未脱敏的副本。

工具参数是重灾区。订单号、手机号、身份证号经常就躺在参数里。记全文等于往日志系统里塞一份用户数据；只记键名，排障时够用（"它到底传了哪些字段"），又不泄露内容：

```python
def summarize_arguments(args: Any) -> str:
    """工具参数的**摘要**——只留键名，不留值。

    工具参数里经常混着订单号、手机号、身份证号。记全文等于记一份
    用户数据到日志系统；只记键名，排障时够用（"它到底传了哪些字段"），
    又不泄露内容。
    """
```

除了参数值本身，还有三件事要处理：**密钥形态的正文**（`sk-xxx`、`Bearer xxx`）用正则遮掉；**过长的文本**截断（trace 是给排障看的，不是给存档用的）；**prompt 与 completion 全文不进默认路径**——记录它们能 debug，但那是钱和合规风险双份代价。

采样则是另一侧的取舍：**全量 trace 的成本会压垮存储**。一个任务几十个 span，百万任务/天就是上千万 span。

采样有三条纪律：

1. **按任务粒度**，不按 span 粒度。要么整条 trace 留，要么整条丢——否则父 span 留着子 span 丢了，trace 树就断了。
2. **错误与慢任务必留**。长尾问题恰恰藏在低频路径里：偶发的超时、偶发的限流、偶发的空转。纯比例采样会把它们丢掉，而那正是最需要留证据的地方。**但这条纪律在本章只是接口、没接线**：`ErrorOrSlowSampler.keep_because_slow()` 定义了却从未被调用——采样决策发生在根 span **开始**的时候，那时还不知道这次任务会不会失败、跑了多久。实际跑的仍是纯比例采样，要真正做到「错误与慢任务必留」，得由上层在任务结束时按结果补判。知道这条纪律和把它接上是两回事。
3. **决策要确定性**。同一个任务重跑，要么每次都留、要么每次都丢。用 trace_id 的哈希做判断，随机的话没法复现问题。

## 三、为什么这样设计：取舍与失效条件

### 3.1 自动插桩 vs 手工埋点

本工程选了**自动插桩**：Tracer 包装 Agent 的模型客户端与工具注册表，而不是往主循环里插 `with tracer.span(...)`。

理由是分层。控制流与横切面要分开：主循环负责"下一步做什么"，观测层负责"记下发生了什么"。往主循环里插观测代码，短期看没问题，久了就会变成"为了观测改业务"——最后主循环里全是 `if tracer is not None`。

代价是：自动插桩只能看到**边界**上的调用（一次模型调用、一次工具调用），看不到主循环内部的语义（比如"这一步为什么改主意"）。需要更细的语义时，还是得手工补一个 span。

### 3.2 什么时候不该这么做

**不要给一次性脚本加可观测性。** 跑一次就扔的东西，埋点代码比业务代码还多。判据是"这个任务会被重复跑吗？会被别人问'昨天那次为什么慢'吗？"

**不要在指标没想清楚之前先埋点。** 埋了一堆属性却没人看，等于白埋。**每个指标都要能回答"下一步改什么"**——答不上来的就该删。

**不要拿步数定 SLO。** 前面说过：步数少可能是高效，步数多可能是复杂。它是诊断指标。

**不要在 trace 里存全文。** prompt 全文确实好 debug，但默认路径上记全文 = 钱和合规风险双份代价。要记就分级存储 + 明确 TTL + 访问审计。

**不要用采样后的 trace 算指标。** 指标全量、trace 抽样——两轨并行。

### 3.3 几个必须防的失效模式

- **判定用"没抛异常"**：成功率会虚高得离谱。任务级的"成功"必须有明确判据（状态、产出物、业务校验）。
- **错误不分桶**：错误类型是自由文本，"工具错误率"和"空转率"就算不出来。分桶必须是固定枚举。
- **工具错误率不按工具名分桶**：整体错误率会把某个具体工具的退化抹平。
- **成本记两遍**：Agent 与埋点层各记一次同一个账本，账单直接翻倍。
- **采样按 span 粒度**：trace 树出现孤儿节点，下钻时链路断掉。
- **敏感字段事后过滤**：落盘那一刻就泄露了。

## 四、动手：最小可实现

配套工程在 `dragon-agent/` 下，纯标准库、无需 API Key、不联网。

- `dragon_agent/observability.py`——trace 运行时、SLI 指标、成本账、脱敏与采样。
- `examples/observability_demo.py`——八段演示，把上面每件事跑一遍。
- `tests/test_observability.py`——40 条契约断言。

**依赖**：Python 3.9+，零第三方包。

**运行**：

```powershell
cd dragon-agent

# 1) 演示：八段演示 + 自检，退出码 0
python examples/observability_demo.py

# 2) 契约测试：40 个用例
python -m unittest tests.test_observability

# 3) 真跑一次真实模型（需要 LLM_API_KEY）
python examples/observability_demo.py --live
```

跑一遍演示，关键输出长这样：

```text
  invoke_agent  [ok]  0.52ms
      agent.status = completed
      agent.step.count = 2
      agent.task.id = task-1a1bb485239
      cost.total = 0.001797
      gen_ai.agent.name = dragon-agent
      gen_ai.usage.total_tokens = 379
    gen_ai.chat  [ok]  0.04ms
        gen_ai.request.model = mock-model
        gen_ai.response.finish_reasons = tool_calls
        gen_ai.usage.input_tokens = 128
        gen_ai.usage.output_tokens = 21
    execute_tool  [ok]  0.02ms
        gen_ai.tool.name = add
        tool.arguments = keys=[a,b] sha256=11b6ee598608
        tool.result.status = ok
  任务总数      ：3
  成功任务      ：2
  任务成功率    ：0.6667
  工具调用/失败 ：6 / 1（错误率 0.1667）
  无进展任务    ：1（无进展率 0.3333）
  步数分布      ：p50=3 p90=3 max=3
  总成本        ：0.005262 美元
  每次成功成本  ：0.002631 美元
```

三个场景（正常完成、工具参数不合法后改对、连续空转）跑出来的数字。成本账按任务拆开是**三**个任务：正常完成那个两次调用 `0.001797` 美元，工具失败后改对那个三次调用 `0.001980` 美元，连续空转那个三次调用 `0.001485` 美元。

值得看的是这三个数的关系：空转任务本身**不是最贵的**（它的调用次数和第二个任务一样多，单价却更低），但它一分钱产出都没有。分母一变，结论就变了——三个任务总共花了 `0.005262` 美元，摊到**两个**成功任务上就是每次成功 `0.002631` 美元，而不是按任务数摊的 `0.001754`。**每次成功成本这个指标存在的理由就在这儿**：它让"白跑的那部分钱"显形，而不是被平均掉。

自动插桩的实现只有薄薄一层：包住 `Agent.run`，跑的时候开一个任务 span，并把模型客户端与工具注册表换成带埋点的包装：

```python
    def run(self: Any, task: str) -> Any:
        tracer = getattr(self, "tracer", None)
        if tracer is None or not hasattr(tracer, "span"):
            return original(self, task)
        try:
            tracer.attach(self)
            with tracer.span(
                "invoke_agent",
                **{
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": getattr(tracer, "agent_name", "dragon-agent"),
                    "agent.task.id": _task_id_of(self),
                },
            ) as span:
                result = original(self, task)
                tracer.record_task(result, span)
                return result
```

任务 id 从状态层取（`TaskState` 是唯一真相源），不从观测层另造一个——另造一个，trace 和状态就对不上了。

## 五、开源实现对照

### OpenTelemetry GenAI 语义约定

- 仓库：<https://github.com/open-telemetry/semantic-conventions>
- 许可：Apache-2.0

规定 GenAI span、事件与属性命名的跨厂商约定。**差异**：它是**约定**不是实现——它告诉你属性该叫什么，不告诉你怎么埋点、怎么算 SLI、怎么脱敏。本章的 `gen_ai.*` 命名直接对齐它，所以换后端时业务代码一行不改。

### Langfuse

- 仓库：<https://github.com/langfuse/langfuse>
- 许可：MIT(核心) + EE 目录企业许可

自托管的 Agent 追踪、评估与提示管理平台。**差异**：它内置 LLM 领域概念（trace / generation / observation），省掉自己设计 span 语义；但核心 MIT 而 EE 目录是商业许可，自托管还要自己运维数据库。

### Arize Phoenix

- 仓库：<https://github.com/Arize-ai/phoenix>
- 许可：Elastic License 2.0（SPDX `Elastic-2.0`）

基于 OpenTelemetry 的 LLM 追踪与嵌入评估。**差异**：走 OTel 原生，能接既有 APM；许可证不是 MIT，商用前要确认条款，Agent 语义仍要自己补。

### OpenLLMetry / Traceloop

- 仓库：<https://github.com/traceloop/openllmetry>
- 许可：Apache-2.0

自动插桩把 LLM 调用转成 OpenTelemetry span。**差异**：只覆盖"模型调用"这一层，步、交接、空转这些 Agent 语义还得自己加。本章的自动插桩补的正是这一层——**工具调用与任务级语义，仍然得自己定义**。

### AgentOps

- 仓库：<https://github.com/AgentOps-AI/agentops>
- 许可：MIT

记录 Agent 运行轨迹、错误与会话回放的 SDK。**差异**：Agent 语义贴近，会话回放开箱即用；生态与后端选项相对少。

### LangSmith SDK

- 仓库：<https://github.com/langchain-ai/langsmith-python>
- 许可：MIT

LangSmith 平台客户端：追踪、数据集与评估。**差异**：SDK 是 MIT，但平台是商业托管产品——数据要出本机，这是选型时要先想清楚的事。

**最小接线方式**：本工程是纯标准库的，所以只给接线路径，不引入依赖。OpenTelemetry 侧用 `opentelemetry-sdk` + `opentelemetry-exporter-otlp`，把 `Span.to_otel()` 的载荷喂进去即可；Langfuse 侧把它自己的 OTel 端点配给 OTLP exporter。两种接法下 **span 名字与属性都不用改**——这正是语义层对齐 OTel 的收益。

## 六、坑与检查清单

### span 与命名

- [ ] span 有父子关系，嵌套 Agent 的 span 挂在父 span 下
- [ ] 任务 id 从状态层取，不另造
- [ ] 模型/token 属性一律 `gen_ai.*`，**没有** `llm.*` 简写
- [ ] 写错的键名自动归位，且**被记录下来**
- [ ] span 按开始时间排序，消费方拿到就是树序
- [ ] 异常在 span 上标成 `error`，但**照常往外抛**

### 指标

- [ ] 成功判定发生在**任务层**，不是"没抛异常"
- [ ] SLI 从**全量计数器**算，不从采样 trace 算
- [ ] 工具错误率**按工具名分桶**
- [ ] 错误类型是固定枚举，不是自由文本
- [ ] 有无进展率或等价的空转检测
- [ ] 步数只做诊断与告警，**不进 SLO**

### 成本账

- [ ] 单价来自配置，不写死在代码里
- [ ] 缓存命中的输入不重复计费
- [ ] 同一个账本**不会记两遍钱**
- [ ] 能按模型、按任务拆开看
- [ ] 计价失败不抛异常（最坏记 0 美元）

### 脱敏与采样

- [ ] 脱敏在**采集端**，不是事后过滤
- [ ] 工具参数只记摘要，不记值
- [ ] 密钥形态的正文被正则遮掉
- [ ] 长文本被截断
- [ ] prompt / completion 全文不进默认路径
- [ ] 采样按**任务**粒度，不产生孤儿 span
- [ ] 错误与慢任务必留
- [ ] 采样决策是确定性的

### 常见坑

- [ ] **拿"没抛异常"当成功**：成功率虚高
- [ ] **错误不分桶**：错误率算不出来
- [ ] **成本记两遍**：账单直接翻倍
- [ ] **用采样 trace 算指标**：成功率有偏
- [ ] **按 span 粒度采样**：trace 树断链
- [ ] **敏感字段事后过滤**：落盘即泄露
- [ ] **往主循环里插埋点**：观测代码渗进业务逻辑

## 七、小结与延伸阅读

这一章把"跑起来"变成"看得见"。要点回顾：

1. **trace 的边界是任务，不是请求。** 任务 / 模型调用 / 工具调用三层 span 组成一棵树，嵌套 Agent 顺着这棵树汇总开销。
2. **语义层对齐 OTel GenAI 约定。** 模型与 token 属性一律 `gen_ai.*`；写错了自动归位并被记录，导出层不用改业务代码。
3. **指标全量、trace 抽样。** 成功率、工具错误率、无进展率、每次成功成本全部从计数器算——采样过的 trace 算出来的成功率是有偏的。
4. **脱敏在采集端。** 工具参数只留摘要，密钥形态遮掉，全文不进默认路径。数据落盘那一刻，事后过滤就来不及了。
5. **可观测性不该要求主循环改一行代码。** 自动插桩守住控制流与横切面的边界；代价是主循环内部的语义仍要手工补。

下一章 `p13-evaluation` 接着讲评测：指标告诉你"现在多糟"，评测告诉你"改完好了没有"。

**延伸阅读**

- 第一卷第 33 章《可观测性：Trace、Span 与 Agent 专属指标》——span 字段设计与 SLI 体系
- 第二卷第 3 章 `p03-llm-client`——缓存感知的计价算法
- 第二卷第 4 章 `p04-tool-layer`——工具错误分桶与回灌语义
- 第二卷第 11 章 `p11-protocols`——跨进程之后调用链更难复盘
- OpenTelemetry GenAI 语义约定：<https://github.com/open-telemetry/semantic-conventions>
- Langfuse：<https://github.com/langfuse/langfuse>
- Arize Phoenix：<https://github.com/Arize-ai/phoenix>
- OpenLLMetry：<https://github.com/traceloop/openllmetry>
- AgentOps：<https://github.com/AgentOps-AI/agentops>
- LangSmith SDK：<https://github.com/langchain-ai/langsmith-python>
