# 第 5 章 · 上下文装配器：把提示词变成资源分配

本卷第五章把提示词从**字符串拼接**换成**预算分配**：五个区段、各有优先级、各有配额，超了按规则裁，裁了什么要记账。

- 正文：[`context-assembler.md`](context-assembler.md)
- 配套工程产物：`dragon-agent/` 下的三个文件（见下）

## 本章给了什么

1. **`dragon_agent/context.py`**——装配器，严格按契约实现：
   - `ContextBlock`（layer / text / tokens / priority）
   - `BudgetReport`（per_layer / total / budget / dropped）
   - `AssembledContext`（blocks + report，`render()` 纯拼接）
   - `ContextAssembler`（`assemble(task, history, retrieval, tools)`）
2. **`tests/test_context.py`**——29 条单元断言。
3. **`examples/context_budget_demo.py`**——实跑演示：预算怎么分、裁剪顺序、可复现性。

## 四条硬约束

| 约束 | 怎么保证 |
| --- | --- |
| 稳定块（system / tools）在前 | `LAYER_ORDER` 是模块常量，渲染顺序固定 |
| `render()` 完全确定 | 不读时间、不读随机数、不依赖字典遍历顺序 |
| 装配器是纯函数 | 不改入参、不改状态，不同实例结果一致 |
| 裁了要记账 | `BudgetReport.dropped` 写清楚丢了什么 |

## 预算分配规则

1. **固定块先扣**——`system` 与 `tools` 完整保留，从不裁（它们是提示缓存的前缀）；
2. **任务优先**——`task` 先拿到额度；
3. **剩下按配额分**——`retrieval` 与 `history` 比例 **6 : 4**，防止某一层吃光窗口；
4. **富余额度按优先级回流**——没人用完的配额让给还装不下的层，检索优先于历史。

裁剪顺序固定：**history → retrieval → task**。

## 怎么跑

```powershell
cd dragon-agent

# 1) 演示：离线跑通，退出码 0
python examples/context_budget_demo.py

# 2) 单元测试：29 个用例
python -m unittest tests.test_context
```

不需要 API Key，不联网，不装任何第三方包。

### 实跑结果

| 场景 | 结果 | 退出码 |
| --- | --- | --- |
| `python examples/context_budget_demo.py` | 两档预算装配 + 确定性自检全部通过 | 0 |
| `python -m unittest tests.test_context` | `Ran 29 tests` / `OK` | 0 |

演示里的实际数字（4000 token 预算）：

| 区段 | token | 优先级 |
| --- | --- | --- |
| system | 101 | 100 |
| tools | 11 | 90 |
| task | 18 | 80 |
| retrieval | 46 | 50 |
| history | 25 | 10 |

把预算压到 120 token 时，`system` 与 `tools` 一点没少（112 token 固定块），剩下 8 token 全给了任务；报告里写明：任务被截断、检索丢 3 段、历史淘汰 3 条。

## 兼容说明

本模块同时满足工程线 Bot 在 `tests/test_invariants.py` 里预先写好的四条上下文不变式（确定性、跨实例一致、不改入参、稳定块在前）——`python -m unittest tests.test_invariants.TestContextDeterminism` 4 条全过。

## 局限（这一章不等于可以上线）

- **token 是估算，不是分词。** `estimate_tokens()` 按"CJK 每字 1 个、其余每 4 字符 1 个"折算（`context.py:98-110`）——它要的是稳定而不是精确，误差会原样变成预算误差；真要对齐模型窗口上限，得换成真实 tokenizer。
- **固定块永不裁，所以预算可以被击穿而不报警。** system 与 tools 是硬开销，剩余额度被 `max(0, ...)` 夹住（`context.py:267-271`），两者之和超过总预算时不报错、也不写进 `dropped`——报告里只有 `total` 与 `budget` 两个数，超没超得自己比。
- **预算小于固定块时，任务正文整段丢掉。** 任务配额被压到 0，`truncate_to_tokens(text, 0)` 直接返回空串（`context.py:149-150`、`:276-287`）——模型这一轮根本收不到任务，而账上只有一句"预算已被固定块吃光"。
- **优先级数字不参与任何淘汰决策。** 裁剪顺序仍然是 history → retrieval → task，但那是配额与装配流程写死的（`context.py:289-320`），`LAYER_PRIORITY` 全模块只在构造 `ContextBlock` 时被读一次（`:341`）——改优先级表不会改变任何行为。
- **"压缩"是关键词筛选，对英文近乎空操作。** `_SIGNAL_WORDS` 是写死的十几个中文词（`context.py:92-95`），断句正则不含 `.`（`:89`）——英文整段被当成一句，压缩后原样返回；中文若一句都不含信号词，就只剩第一句（`:134-135`）。
- **检索层在生产路径上是空的。** 主循环调用装配时写死了 `retrieval=[]`（`agent.py:273-275`），这一层要自己接检索源；章内也没有摘要式压缩——装不下的检索块是整段丢弃而不是截断（`context.py:425-429`）。

## 与本书其它章节的关系

- 第二卷第 6 章 `p06-agent-loop`：主循环每一步前装配一次上下文；预算守卫记的是模型真实返回的 `usage`，不是这个估算值。
- 第二卷第 12 章 `p12-observability`：`BudgetReport` 进 trace，裁剪事件可观测。
- 第二卷第 13 章 `p13-evaluation`：装配结果确定，才能做回归对比。
- 第一卷第 10、28、30、33、35 章：上下文工程、四种操作的失效条件、Agentic RAG、可观测性、上下文经济学。
