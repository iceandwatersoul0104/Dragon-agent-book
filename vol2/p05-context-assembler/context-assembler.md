# 第 5 章 上下文装配器：把提示词变成资源分配

## 一、本章要解决的问题

第一个版本的 Agent，提示词是这么拼的：

```python
prompt = SYSTEM + json.dumps(history) + "\n".join(retrieved_docs) + TOOLS_JSON
```

（反面教材——这就是"拼字符串"的写法，它没有预算、没有顺序、没有账。）

第一周挺好用的。第三周接了 RAG，检索结果 30k token；第五周加了 5 个工具，工具定义 4k；第七周成本账单变成 6 倍，效果**反而变差**——review 的人说"它开始忽略我写在最前面的约束了"。

约束确实写在最前面，但它被埋在 34k token 的中间，而且每次请求的前缀都不一样，提示缓存一次也没命中。

问题的本质是：**把提示词当字符串拼，而它其实是一份预算。**

上下文窗口（Context Window）是有价的：每一段内容都在抢同一份额度，而且抢的顺序决定了模型先看见什么、后看见什么。拼字符串的时候没有人管这件事，于是有两条必然的结果：

- 额度不够时，要么截断在某个字符上（把 JSON 截成半截），要么干脆塞进去让服务端报错；
- 内容变多时，最重要的约束反而被埋在中间。

这一章要把"拼字符串"换成"分配预算"：**五个区段，各有优先级，各有配额，超了按规则裁，裁了什么要记账。**

## 二、原理：机制拆解

### 2.1 五个区段与优先级

上下文不是一坨，是五段：

| 区段 | 内容 | 优先级 | 能裁吗 |
| --- | --- | --- | --- |
| `system` | 系统提示与关键约束 | 100 | **不能** |
| `tools` | 工具名册 | 90 | **不能** |
| `task` | 这一轮要完成的任务 | 80 | 压缩后截断 |
| `retrieval` | 检索到的证据 | 50 | 压缩、丢段 |
| `history` | 历史对话 | 10 | 从最旧的开始丢 |

优先级就是**预算紧张时谁先让路**。系统约束让路了，模型就没有规矩可言；历史让路了，模型只是"忘了点事"，还能继续。

裁剪的顺序固定为 `history → retrieval → task`。这个顺序不是拍脑袋定的，是从优先级表读出来的。

### 2.2 稳定块在前：提示缓存的物理要求

提示缓存（Prompt Caching）的机制是：服务端复用**相同前缀**的 token。所以前缀必须**逐字节稳定**。

这带来一个硬约束：**系统提示和工具定义必须排在最前面，而且不能被裁**。它们在多轮之间不变，前缀才命中。

实跑装配一次，块顺序是这样的：

```text
--- 块与优先级 ----------------------------------------------------
  [system] 优先级 100   101 token  你是 dragon-agent 的执行助手。必须遵守：
  [tools] 优先级  90    11 token  - read_budget(fiscal_year*)
  [task] 优先级  80    18 token  查一下 2026 年的项目预算，并告诉我总额
  [retrieval] 优先级  50    46 token  - 预算表：研发 62 万，市场 25 万，运维 13 万，总计 100 万。
  [history] 优先级  10    25 token  [user] 先看看预算
```

渲染出来的开头就是 `[system]`：

```text
--- 渲染结果（前 12 行） ----------------------------------------
  [system]
  你是 dragon-agent 的执行助手。必须遵守：
  1. 事实只能来自工具输出，不要凭空编造。
  2. 工具失败时读懂回灌的错误并修正调用，不要重复同一个失败调用。
  3. 信息不足就说明缺什么，不要猜。
  4. 只做任务要求的动作，不顺手做别的。
  
  [tools]
  - read_budget(fiscal_year*)
  - add(a*, b*)
```

注意工具层只放**名册**（名字 + 参数），不放完整 schema——完整 schema 走 API 的 `tools` 参数。同一段描述在一次请求里出现两遍，就是白烧 token。

### 2.3 预算怎么分

装配器拿到的总预算 `token_budget`，按四步花：

1. **固定块先扣**：`system` 与 `tools` 完整保留，从不裁。它们是提示缓存的前缀，动了前缀就等于放弃缓存。
2. **任务优先**：`task` 是这一轮要完成的事，先给它。
3. **剩下的按配额分**：`retrieval` 与 `history` 瓜分剩余额度，比例 **6 : 4**。配额的作用是**防止某一层吃光窗口**——检索结果再多，也不能把对话历史挤没了。
4. **富余额度按优先级回流**：某个层没把配额用完（比如检索结果本来就少），富余的额度让给还装不下的层，**优先级高的先拿**（检索优先于历史）。

实跑两次装配，预算分别是 4000 和 120：

```text
1) 预算充足（4000 token）
  预算 4000，实际用掉 201
    system      101 token
    tools        11 token
    task         18 token
    retrieval    46 token
    history      25 token
  丢弃：（没有丢任何东西）
```

```text
2) 预算紧张（120 token）
  预算 120，实际用掉 120
    system      101 token
    tools        11 token
    task          8 token
    retrieval     0 token
    history       0 token
  丢弃：['task: 压缩后仍超预算，截断到 8 token', 'retrieval: 丢弃 1 段（检索额度已用完）', 'retrieval: 丢弃 1 段（检索额度已用完）', 'retrieval: 丢弃 1 段（检索额度已用完）', 'history: 淘汰 3 条较早的对话']
```

注意 `system` 和 `tools` 在预算只有 120 token 时**一点没少**——它们是 112 token 的固定块，剩下的 8 token 全部让给了任务。裁剪的报告里写得很清楚：任务被截断、检索被丢光、历史被淘汰三条。

这就是"预算分配"的全部含义：**钱不够的时候，先保证规矩，再保证任务，最后才是证据和记忆。**

### 2.4 四种操作，顺序固定

装配不是一步到位，是四步流水线：

```
  输入
    │
    ▼
  ① 分层    输入切成五层，每层挂优先级
    │
    ▼
  ② 检索    检索候选按与任务的相关度排序
    │
    ▼
  ③ 压缩    装不下的文档做抽取式压缩
    │
    ▼
  ④ 淘汰    按优先级丢弃，历史从最旧的开始丢
    │
    ▼
  上下文 + 预算报告
```

**① 分层**就是给五段内容各挂一个优先级，逻辑本身很短。

**② 检索**是给候选打分排序。打分规则必须确定：

```python
    def _select_retrieval(self, task: str, candidates: list[str]) -> list[str]:
        """按与任务的相关度给检索候选排序。

        打分 = 任务词表与候选词表的交集大小。**并列时按原始顺序**，
        所以排序完全确定。
        """
        query_terms = _terms(task)
        scored: list[tuple[int, int, str]] = []
        for index, chunk in enumerate(candidates):
            text = str(chunk)
            score = len(query_terms & _terms(text))
            scored.append((-score, index, text))
        scored.sort()
        return [text for _, _, text in scored]
```

打分相同时按原始下标排——这就是"并列名次规则"。少了它，`sort` 对相等元素的顺序就依赖实现细节，渲染结果可能每次不一样。

**③ 压缩**是抽取式的：只挑句子，**不改写**。

```python
def compress(text: str) -> str:
    """抽取式压缩：只保留含约束信号的句子。

    不改写、不摘要——只**挑**。挑出来的句子原文不变，所以压缩结果仍然
    可以被断言、被回放。没有任何句子含信号时，保留第一句（至少给模型
    一个抓手），这个规则同样确定。
    """
```

关键在"挑"而不是"改"。改写式压缩（摘要）会引入新的话，压缩结果就没法断言了——第 13 章要拿它做回归测试。抽取式压缩只做减法，句子原文不变。

**④ 淘汰**分两种：文档类（检索结果）超了额度，就压缩、然后丢段；对话类（历史）超了额度，就从**最旧**的开始整条丢。

```python
    def _fit_history(self, items: list[tuple[str, int]], budget: int) -> tuple[list[str], int, int]:
        """从最新往回装，装不下的旧对话整条淘汰。

        返回 ``(保留的文本, 实际 token, 淘汰条数)``。保留的文本仍按最旧在前排列。
        """
```

从最新往回装，比"从头往后装到装不下"更合理：对话的最近几轮通常才是相关的，旧的全砍掉不如保留最近的。

### 2.5 token 怎么算

纯标准库，没有分词器，所以 token 数是**估算**的：

```python
def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数。

    规则：CJK 字符每个算 1 个 token；其余字符每 4 个算 1 个（向上取整）。

    这是估算，不是分词器的结果——但它**确定**：同样的文本永远算出同一个数。
    预算分配需要的正是这种确定性。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4
```

这个估算对中英混排够用：CJK 实际大约 1 字 1 token，英文大约 4 字符 1 token。它不精确，但**它确定**——同样的文本永远算出同一个数。

预算分配需要的是确定性，不是精确性。一个会随时间变化的"精确 token 数"，会让同一个输入在不同时刻装配出不同的结果，第 13 章的回归测试直接失效。

### 2.6 装配结果可观测

每一次装配都产出一份账：

```python
@dataclass
class BudgetReport:
    """这一轮装配的账：各层实际占多少、超出多少、丢了什么。"""

    per_layer: dict[str, int] = field(default_factory=dict)
    total: int = 0
    budget: int = 0
    dropped: list[str] = field(default_factory=list)
```

四个字段各管一件事：`per_layer` 是各层的实际占用，`total` 是合计，`budget` 是上限，`dropped` 是**丢了什么**。

`dropped` 是这份报告里最值钱的字段。它把"上下文被裁了"从一件看不见的事，变成一行可以进 trace、可以进告警、可以写进回归测试断言的事：

```text
  丢弃：['task: 压缩后仍超预算，截断到 8 token', 'retrieval: 丢弃 1 段（检索额度已用完）', 'retrieval: 丢弃 1 段（检索额度已用完）', 'retrieval: 丢弃 1 段（检索额度已用完）', 'history: 淘汰 3 条较早的对话']
```

## 三、为什么这样设计：取舍与失效条件

### 3.1 什么时候**不需要**装配器

- **上下文一直很小。** 任务固定、工具就两三个、没有历史对话——直接拼字符串更简单。
- **上下文是固定模板。** 每次请求都一样，不需要"分配"，只需要"拼接"。
- **上下文由上游完全决定。** 比如你只是把别人的 prompt 转发给模型。

装配器解决的是**"内容会变、而且会互相挤"**的问题。没有这个问题，它就是一层没用的间接。

### 3.2 装配器的五种失效方式

| 失效方式 | 症状 | 后果 |
| --- | --- | --- |
| 约束被裁 | system 层被截断 | 模型开始不守规矩 |
| 估算偏差 | 报告说没超，实际超了 | 服务端报错，或静默截断 |
| 压缩改坏语义 | 摘要式压缩把否定句翻成肯定句 | 模型做了相反的事 |
| 稳定前缀被破坏 | 每轮 system 层内容不同 | 提示缓存永不命中 |
| 顺序错了 | 任务被埋在检索结果后面 | 模型抓不住重点 |

第一种靠"固定块永不裁"挡住。第五种靠**固定装配顺序**挡住——`LAYER_ORDER` 是模块里的一个常量，不是运行时决定的。第四种靠"system 内容只由构造函数决定"挡住。

第二种只能靠预留余量：真实 token 数比估算高时，要么留 10% 余量，要么在服务端返回超限时降级。**这一点装配器管不了**，必须诚实地说出来。

第三种是本章选抽取式压缩的原因：只挑不改，就不会把语义改反。

### 3.3 估算的代价

用估算而不是真分词器，代价是：

- 预算会有偏差，英文密集的文档可能被低估；
- 不同供应商的分词规则不同，同一段文本在不同模型上实际 token 数不一样。

所以生产环境里，**预算表里的数字应该是"最坏情况"**——宁可估得偏大，裁早一点，也不要撞窗口上限。

## 四、动手：最小可实现

### 4.1 本章的工程产物

| 文件 | 作用 |
| --- | --- |
| `dragon_agent/context.py` | 装配器：`ContextBlock` / `BudgetReport` / `AssembledContext` / `ContextAssembler` |
| `tests/test_context.py` | 单元测试：29 条断言 |
| `examples/context_budget_demo.py` | 实跑演示：预算怎么分、裁剪顺序、可复现性 |

### 4.2 数据结构

三个数据结构，四条约束：

```python
@dataclass
class ContextBlock:
    """装配结果里的一段上下文。"""

    layer: str
    text: str
    tokens: int
    priority: int
```

```python
@dataclass
class AssembledContext:
    """装配好的上下文：块 + 账。"""

    blocks: list[ContextBlock] = field(default_factory=list)
    report: BudgetReport = field(default_factory=BudgetReport)

    def render(self) -> str:
        """渲染成给模型的文本。

        同样的块永远渲染出同样的字符串——这是提示缓存命中率可复现的前提。
        """
        return "\n\n".join(f"[{b.layer}]\n{b.text}" for b in self.blocks)
```

`render()` 只有一行拼接，没有时间、没有随机数、没有字典遍历顺序——所以它对同一组块永远给出同一个字符串。

### 4.3 装配主流程

主流程的关键是**先算账，再装内容**：

```python
        # 4) 剩余额度的分配：先按配额保底，再把没人用的额度按优先级让出去。
        #    配额防止某一层吃光窗口；优先级决定"谁先拿到富余的额度"。
        remaining = max(0, variable_budget - task_tokens)
        ret_quota = int(remaining * RETRIEVAL_WEIGHT / (RETRIEVAL_WEIGHT + HISTORY_WEIGHT))
        hist_quota = remaining - ret_quota

        need_retr = sum(estimate_tokens(c) for c in retrieval_texts)
        need_hist = sum(cost for _, cost in history_items)
        alloc_retr = min(need_retr, ret_quota)
        alloc_hist = min(need_hist, hist_quota)
        space = remaining - alloc_retr - alloc_hist
        if space > 0:
            give = min(need_retr - alloc_retr, space)
            alloc_retr += give
            space -= give
        if space > 0:
            give = min(need_hist - alloc_hist, space)
            alloc_hist += give
            space -= give
```

这段代码读起来不短，但它把两条规则明明白白摆在那里：**配额是上限，优先级是富余额度的分配顺序**。

### 4.4 单元测试

测试要断言**期望值**，不是"跑完不崩就算过"。四条核心断言：

```python
    def test_same_input_renders_identically(self):
        assembler = ContextAssembler(token_budget=4000)
        first = assembler.assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        second = assembler.assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertEqual(first.render(), second.render())
```

```python
    def test_per_layer_sums_to_total(self):
        assembled = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        report = assembled.report
        self.assertEqual(sum(report.per_layer.values()), report.total)
        self.assertEqual(report.budget, 4000)
```

```python
    def test_free_space_flows_to_the_higher_priority_layer(self):
        """配额用不完的富余额度按优先级让出去——检索层优先于历史。

        这里的预算刚好让检索层超配额一点：没有富余额度回流的话，
        就会白白丢掉一段检索结果。
        """
```

这一条是写测试时才发现的 bug：最初的实现里检索层被硬卡在配额上，哪怕历史层根本用不完那点额度，也会白白丢掉一段检索结果。**规则写对了不等于实现写对了**，差额得靠断言逼出来。

### 4.5 实跑

```powershell
cd dragon-agent
python examples/context_budget_demo.py
python -m unittest tests.test_context
```

测试：

```text
----------------------------------------------------------------------
Ran 29 tests in 0.004s

OK
```

退出码 0。全部离线。

### 4.6 骨架怎么被上下游调用

| 骨架里的东西 | 谁来用 | 怎么用 |
| --- | --- | --- |
| `ContextAssembler.assemble()` | 编排层 | 每一步之前装配一次上下文 |
| `AssembledContext.render()` | 模型层 | 拼进 `messages`，或作为 `system` 段 |
| `BudgetReport.per_layer` | trace、成本账 | 每层占多少，进 span 属性 |
| `BudgetReport.dropped` | 告警、回归测试 | "上下文被裁了"变成可观测事件 |
| `LAYER_ORDER` | 编排层 | 稳定前缀在前的保证 |
| `estimate_tokens()` | 装配器内部 | 算每一层的 token 占用（估算，确定但不是分词结果） |

## 五、开源实现对照

### Anthropic Prompt Caching

- 文档：<https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching>

官方文档里把提示缓存的工作方式讲得很清楚：**前缀命中才计价**，缓存部分按更低的单价计，而且有最小长度与 TTL 的限制。**差异**：文档讲的是"怎么命中"，本章讲的是"**怎么保证前缀不变**"——排序、裁剪、渲染的任何一处不稳定，缓存就永远不命中。本章的 `render()` 之所以要纯到极致，就是为了喂饱这个机制。

### Letta (MemGPT)

- 仓库：<https://github.com/letta-ai/letta>
- 许可证：Apache-2.0

把上下文当成**可分页的内存**：核心记忆常驻，检索到的内容按需换入换出。**差异**：Letta 的分页由 Agent 自己驱动（模型决定换入什么）；本章的装配是**装配器决定**的，模型不参与。分工不同：Letta 让模型管"我需要什么"，本章让代码管"只能装这么多"。后者更可控，前者更灵活。

### LangGraph

- 仓库：<https://github.com/langchain-ai/langgraph>
- 许可证：MIT

用显式状态图编排 Agent，状态里带着消息与摘要。**差异**：LangGraph 的摘要（compaction）是**状态机的一个节点**——触发条件、压缩策略都写在图里。本章的压缩是装配器里的一步，随时生效，不需要额外的图节点。代价是压缩策略写死在代码里，不能按状态切换。

### Jinja2

- 仓库：<https://github.com/pallets/jinja>
- 许可证：BSD-3-Clause

Python 最常用的模板引擎，提示词拼接的事实标准之一。**差异**：Jinja2 解决的是"**怎么把变量填进模板**"，本章解决的是"**填哪些变量、填多少**"。两件事正交——本章的装配结果完全可以用 Jinja2 渲染。反过来，Jinja2 不会替你决定预算。

## 六、坑与检查清单

### 结构

- [ ] 五个区段固定：system / tools / task / retrieval / history
- [ ] 每个区段有明确的优先级
- [ ] 装配顺序是常量，不在运行时决定
- [ ] 稳定块（system / tools）在前，且永不裁剪
- [ ] system 层的内容只由构造函数决定，不随任务变化

### 预算

- [ ] 固定块（system + tools）先从预算里扣
- [ ] 任务优先于检索与历史
- [ ] 检索与历史有配额（6:4），防某一层吃光窗口
- [ ] 富余额度按优先级回流
- [ ] 裁剪顺序：history → retrieval → task
- [ ] 预算表里的数字留了余量（估算偏保守）

### 确定性

- [ ] `render()` 对同一输入逐字相同
- [ ] 不同装配器实例结果一致（没有隐藏状态）
- [ ] 排序有稳定的并列名次规则
- [ ] 不读时间、不读随机数、不改入参
- [ ] token 估算是确定性的（同样的文本永远同一个数）

### 可观测

- [ ] `per_layer` 的和等于 `total`
- [ ] 每一层的 token 数等于对应块的 token 数
- [ ] `dropped` 说清楚丢了什么，不是"超预算"
- [ ] 裁剪事件能进 trace，也能进告警

### 常见坑

- [ ] **拼字符串**：没有预算、没有顺序、没有账
- [ ] **把完整工具 schema 塞进提示**：和 API 的 `tools` 参数重复，白烧 token
- [ ] **system 层每轮都变**：提示缓存永不命中
- [ ] **压缩用改写**：否定句可能被翻成肯定句
- [ ] **历史从头往后装**：留下最旧的、丢掉最新的
- [ ] **超预算就截断整个提示**：把 JSON 截成半截，模型直接解析失败
- [ ] **不检查 `dropped`**：上下文被裁了没人知道，效果变差也查不出原因

## 七、小结与延伸阅读

这一章把提示词从字符串变成了预算。要点回顾：

1. **上下文是资源分配，不是字符串拼接。** 五个区段、各有优先级、超了按规则裁。
2. **稳定块在前是物理要求。** system 与 tools 逐字节稳定，提示缓存才命中——它们永远不裁。
3. **配额防饿死，优先级定顺序。** 配额保证每层有位置，优先级决定富余额度给谁、预算紧张时谁先让路。
4. **确定性比精确更重要。** token 估算可以不准，但必须永远算出同一个数；`render()` 必须逐字可复现。
5. **裁剪要记账。** `dropped` 把"上下文被裁了"变成可观测、可告警、可断言的事。

下一章 `p06-agent-loop` 做 Agent 主循环：把第 3 章的模型调用、第 4 章的工具调用、第 5 章的上下文装配串成一条回路，并加上预算守卫与停止准则。

**延伸阅读**

- 第一卷第 10 章《上下文工程：把上下文当作稀缺资源预算》——分层、检索、压缩、淘汰四种操作
- 第一卷第 28 章《上下文窗口工程：检索、压缩、淘汰与分层》——每种操作什么时候不该用
- 第一卷第 30 章《Agentic RAG：让检索从预处理变成主动决策》——检索该由谁决定
- 第一卷第 33 章《可观测性：Trace、Span 与 Agent 专属指标》——裁剪事件进 trace
- 第一卷第 35 章《成本与性能优化》——上下文经济学与成本
- Anthropic Prompt Caching：<https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching>
- Letta (MemGPT)：<https://github.com/letta-ai/letta>（Apache-2.0）
- LangGraph：<https://github.com/langchain-ai/langgraph>（MIT）
- Jinja2：<https://github.com/pallets/jinja>（BSD-3-Clause）
