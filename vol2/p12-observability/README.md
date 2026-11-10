# 第 12 章 · 可观测性：接上 trace、指标与成本账

本卷第十二章把"跑起来"变成"看得见"：**结构化的 trace、全量的指标、拆得开的成本账、采集端就做的脱敏。**

- 正文：[`observability.md`](observability.md)
- 配套工程产物：`dragon-agent/` 下的两个新文件（见下）

## 本章给了什么

1. **`dragon_agent/observability.py`（941 行）**——trace 运行时（span 树）、SLI 指标、成本账、脱敏与采样、导出器，纯标准库。
2. **`examples/observability_demo.py`（391 行）**——八段演示：span 树、命名约定、脱敏、SLI 报表、成本账拆分、采样、导出、接开源栈。
3. **`tests/test_observability.py`（547 行）**——40 条契约断言。

**没有改动 `dragon_agent/agent.py`**——主循环一行不用改，埋点由 `Tracer` 自动插桩完成。

## 核心约定

> trace 的边界是**任务**，不是请求：任务 / 模型调用 / 工具调用三层 span 组成一棵树。
> 模型与 token 类属性一律 `gen_ai.*`（OTel GenAI 约定），**不用** `llm.*` 简写。
> SLI 从**全量计数器**算，不从采样后的 trace 算——采样过的成功率是有偏的。
> 脱敏在**采集端**做：工具参数只记摘要，密钥形态遮掉，全文不进默认路径。
> 采样按**任务**粒度：错误与慢任务必留，其余按比例。

## 五个必备 SLI

| 指标 | 算法 | 要点 |
| --- | --- | --- |
| 任务成功率 | 成功任务数 / 任务总数 | 判定在**任务层**，不能拿"没抛异常"顶替 |
| 步数分布 | 每任务步数的 p50 / p90 / max | 诊断指标，**不进 SLO** |
| 工具错误率 | 失败次数 / 调用次数 | **按工具名分桶**，并区分参数不合法与工具自身失败 |
| 无进展率 | 无进展任务数 / 任务总数 | Agent 专属指标，专治"在转圈但没报错" |
| 每次成功成本 | 总成本 / 成功任务数 | 比"每次调用成本"有意义——后者多绕圈反而显得便宜 |

## 怎么跑

```powershell
cd dragon-agent

# 1) 演示：八段演示 + 自检，退出码 0
python examples/observability_demo.py

# 2) 契约测试：40 个用例
python -m unittest tests.test_observability

# 3) 真跑一次真实模型（需要 LLM_API_KEY，缺 key 会清晰提示并非 0 退出）
python examples/observability_demo.py --live
```

不需要 API Key，不联网，不装任何第三方包。导出那一步写进临时目录，跑完即删。

### 实跑结果

| 场景 | 结果 | 退出码 |
| --- | --- | --- |
| `python examples/observability_demo.py` | 八段演示 + 自检 8/8 项通过 | 0 |
| `python -m unittest tests.test_observability` | `Ran 40 tests` / `OK` | 0 |

演示里几个关键数字：

- 一次任务产出 **4 个 span**：1 个 `invoke_agent` + 2 个 `gen_ai.chat` + 1 个 `execute_tool`，三层父子关系完整；
- 写错的键名 `llm.model` / `input_tokens` 自动归位成 `gen_ai.llm.model` / `gen_ai.input_tokens`，并被记进 `renamed_attributes`；
- 工具参数只留摘要：`keys=[api_key,path] sha256=600a4ff46afa`，参数值一个字都没进 trace；
- 三个场景（成功 / 工具参数不合法后改对 / 连续空转）：任务 **3** 个、成功 **2** 个、成功率 **0.6667**、工具错误率 **0.1667**、无进展率 **0.3333**、步数 p50=3、总成本 **0.005262** 美元、每次成功成本 **0.002631** 美元；
- 成本账按任务拆开是**三个**任务：正常完成 `0.001797`、工具失败后改对 `0.001980`、连续空转 `0.001485` 美元——空转那个不是最贵的，但一分钱产出没有，摊到**两个**成功任务上就是每次成功 `0.002631` 美元；
- 采样比例 0% → 留下 0 个 span，但 `task_count` 仍是全量的。

## 局限（这一章不等于可以上线）

- **只有一种出口：本地 JSONL 追加。** `JsonlExporter` 是 `SpanExporter` 唯一的实现（`observability.py:324-338`），没有 OTLP / HTTP / Collector，没有 flush、没有轮转、没有文件大小上限——接真实后端要自己写 exporter。
- **重复导出会产生重复记录。** `export()` 每次把 `spans()` 的全部内容再写一遍（`:726-731`），没有游标、没有已导出标记——同一个 tracer 导两次就是两倍数据。
- **SLI 计数器全在内存，进程一重启就清零。** 任务数、成功数、工具错误、步数序列都是普通实例属性（`:535-543`），没有持久化、没有时间窗口、没有滚动——`sli()` 只是对内存快照求值。
- **"错误与慢任务必留"这条约定只有接口，没有接线。** `ErrorOrSlowSampler.keep_because_slow()` 定义了却从未被调用（`:295-310`），采样决策发生在根 span **开始**的时候（`:581-584`），那时还不知道结果与耗时——实际跑的仍是纯比例采样，这条纪律要靠上层自己补。
- **脱敏只认精确键名。** `SENSITIVE_KEYS` 是全等集合（`logging_setup.py:52-59`），`user_token`、`api_key_x` 这类名字一个都遮不住；dict / list 类型的属性走 `redact()` 后**不截断**（`observability.py:219-220`），长数组会原样进 span。
- **自动插桩会改变控制流语义。** 补丁里 `except Exception: return original(self, task)`（`observability.py:927-928`）——主循环抛异常时整个任务会被**重跑一遍**，模型调用与副作用工具调用都再来一次；同一处也让失败的任务绕过了 `record_task`（`:924-925`），不进 SLI 分母，成功率会被系统性抬高。

## 与本书其它章节的关系

- 第二卷第 3 章 `p03-llm-client`——缓存感知的计价算法，本章直接复用。
- 第二卷第 4 章 `p04-tool-layer`——工具错误分桶与回灌语义。
- 第二卷第 11 章 `p11-protocols`——跨进程之后调用链更难复盘。
- 第一卷第 33 章《可观测性：Trace、Span 与 Agent 专属指标》——span 字段设计与 SLI 体系，本章是它的工程落点。
