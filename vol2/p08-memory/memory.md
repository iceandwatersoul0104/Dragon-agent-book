# 第 8 章 记忆模块：把“记住”做成可控的读写

## 一、本章要解决的问题

某个 Agent 项目上线第二周，产品经理在群里贴了一句话：

> 它记得我说过的话，但它记错了我改过的话。

拆开是两个方向相反的 bug。

"记得说过的话"没做到，是**检索**的问题：事实躺在库里，提问时没捞上来。第一卷第 27 章讲过原因——向量只认字面相似度，用户说"上个月报的故障"，库里存的是"故障单 FD-2291 已闭环"，两段文本一个字都不重合。

"记错了我改过的话"，是**写入**的问题：用户先把预算改成 8 万，Agent 库里有 5 万和 8 万两条记录，都是"预算"打头、长度相近、分数只差 0.01。两条一起进了 top-5，矛盾事实同时躺在上下文里，没人负责仲裁。

第三类没人提，但已经在生产上发生了：Agent "记得"用户从没说过的话。每轮结束让模型摘要一段写进向量库，模型会顺手补全——它补了句"该用户偏好深色界面"，此后每次都"记得"。这条一旦落库，再没人能证明它是编的。

这三件事没有一个能靠调大 top-k 或换个 embedding 模型解决。它们是**设计问题**：所有人都在用"记忆"这一个词，指着一堆性质完全不同的东西——寿命不同、写入时机不同、冲突规则不同、检索方式也不同。

所以这章不是"接一个向量库"，而是把"记住"拆成四个动词、把记忆拆成四类，再给每次写入定一条准入规则：**写**（写哪一类、来源可不可信）、**读**（什么进上下文）、**检索**（怎么捞回该捞的那条）、**遗忘**（用户说"忘掉"时怎么真的忘掉）。

配套工程产物是 `dragon_agent/memory.py`：纯标准库、不联网、不需要 API Key，给出向量与实体图谱两条最小实现，以及一套能跑的评测。

## 二、原理：机制拆解

### 2.1 四个动词，一条写入口

第一卷第 26 章有一句要紧的话：**读和写必须同一个入口**。三个库各写各的，最后没人说得清这个 Agent 到底记得什么。

本章的账长这样：

```
                 ┌──────────────── 写 write ────────────────┐
                 │ kind / text / source / ttl               │
                 │   ↓ 先过准入规则 write_decision()         │
                 │   ↓ 算事实键 → 版本仲裁 → 落账本          │
                 ▼                                           │
   ┌──────────────────────────────────────────────┐        │
   │            账本 items / history                │◄───────┘
   │  working / episodic / semantic / procedural   │
   └──────────────────────────────────────────────┘
        │                                          │
        │ retrieve                                 │ read
        ▼                                          ▼
   检索 recall(query)                        装配 read(query)
   匹配度×可信度×新鲜度×TTL                    工作记忆全量 + 命中的规则
        │                                          │
        └────────► 上下文（context.py 的 retrieval 层）◄────┘
                                │
                          遗忘 forget(item_id)
```

三条要点：**写入是唯一改变账本的动作**，入口只有一个 `write()`，准入、版本仲裁、索引维护全在里面，调用方绕不过去；**读和检索是两条路**，`read()` 装配上下文、`recall()` 按查询找事实，混成一条的后果是每次请求都把整个库塞进提示词；**遗忘必须能真的删**，没有遗忘的记忆库最终必然变成噪音池。

### 2.2 四类记忆，四套规则

工作记忆（Working Memory）、情景记忆（Episodic Memory）、语义记忆（Semantic Memory）、程序性记忆（Procedural Memory）在工程上不是同一种东西。本章沿用第一卷第 26 章的命名：

| 类型 | 装什么 | 进上下文还是进存储 | 写入时机 | 冲突规则 |
|---|---|---|---|---|
| `working` 工作记忆 | 意图、草稿、工具返回值 | **进上下文**（全量） | 随手写 | 不冲突，任务结束即弃 |
| `episodic` 情景记忆 | 事件 + 结果 + 时间 | 进存储 | **任务完成点写一次** | 只追加，不修改 |
| `semantic` 语义记忆 | 事实、偏好、约束 | 进存储 | 来源可信才写 | 原地更新，留旧版本 |
| `procedural` 程序性记忆 | 步骤、SOP、工具模板 | 进存储（命中才注入） | **连续成功 N 次**才固化 | 按版本发布 |

最容易做错的是情景记忆的写入时机：**任务没跑完不要写**，没跑完你不知道结果，写进去的全是半成品。

程序性记忆的准入最反直觉：一次成功不能固化。"这次改代码前跑了测试"是巧合，"改代码前要跑测试"是规则——一字之差，库里必须是两条记录。规则版在 `write_decision()` 里写死：连续成功 3 次（`PROCEDURAL_STREAK`）才放行。

### 2.3 写入时机的判断规则

准入规则必须是一个**纯函数**，不碰外部状态就能给结论，这样才测得动、看得懂。

```python
def write_decision(kind: str, source: str, *, streak: int = 0) -> WriteDecision:
    """判断一条写入**该不该落库**。

    规则表：

    ==========  ========  ==========================================
    kind        source    结果
    ==========  ========  ==========================================
    任意        未知      拒绝（来源不在可信度表里）
    semantic    model     **拒绝**——模型推断的事实一旦进长期记忆，
                         会被当成用户说过的话反复引用
    procedural  model     降级为 episodic（模型推断的"怎么做"只是
                         "某次这么做过"，不构成规则）
    episodic    model     降级：可信度按 0.3 记，排序自然沉底
    working     model     放行——工作记忆是草稿，不进长期存储
    procedural  user/tool 连续成功 ``PROCEDURAL_STREAK`` 次才放行
    其它        user/tool 放行
    ==========  ========  ==========================================
    """
    if kind not in KINDS:
        return WriteDecision(False, kind, 0.0, f"未知的记忆类型 {kind!r}")
    if source not in SOURCE_TRUST:
        return WriteDecision(False, kind, 0.0, f"未知的来源 {source!r}")

    trust = SOURCE_TRUST[source]

    if source == "model":
        if kind == "semantic":
            return WriteDecision(
                False, kind, 0.0,
                "模型来源的语义记忆被拒绝：推断出来的事实会被当成用户陈述反复引用",
            )
        if kind == "procedural":
            return WriteDecision(
                True, "episodic", trust,
                "模型来源的程序性记忆降级为情景记忆",
            )
        return WriteDecision(True, kind, trust, "模型来源降级：可信度按 0.3 计入")

    if kind == "procedural" and streak < PROCEDURAL_STREAK:
        return WriteDecision(
            False, kind, trust,
            f"程序性记忆需连续成功 {PROCEDURAL_STREAK} 次才固化，当前 {streak} 次",
        )

    return WriteDecision(True, kind, trust, "允许写入")
```

来源可信度表就三个数：

```python
SOURCE_TRUST: dict[str, float] = {
    "user": 1.0,
    "tool": 0.7,
    "model": 0.3,
}
```

`model` 来源的三种待遇理由各不相同：**语义记忆拒绝**——模型推断的事实一旦落库，就会被当成用户亲口说过的话反复引用，而且没人能再摘掉，它是自我强化的幻觉；**程序性记忆降级为情景记忆**——模型推断的"怎么做"只是"某次这么做过"，不构成规则；**工作记忆与情景记忆放行但降权**——草稿和事件本来就该记，只是可信度按 0.3 记，排序自然沉底。

### 2.4 短期与长期的边界

一个常见的偷懒做法是："上下文窗口就是短期记忆，外部数据库就是长期记忆"。第一卷第 26 章专门驳过这条：上下文窗口是**每轮都要重读的工作区**，瓶颈在注意力与信噪比，不在容量。叫它"长期记忆"会让人误以为塞进去就等于记住了。

本章的边界按"要不要每轮重读"划：

```
每轮都要重读 ──► working（全量）+ procedural（触发词命中）
只用检索取回 ──► episodic + semantic
```

对应到代码，`read()` 和 `recall()` 的分工是硬的：

```python
    def read(self, query: str = "", kind: str | None = None, limit: int = 20) -> list[MemoryItem]:
        """装配当前上下文。

        - **工作记忆**：全量注入（新到旧）——它是「这一轮正在用的东西」；
        - **程序性记忆**：按触发词匹配上才注入；
        - **情景与语义记忆**：不直接注入，必须走 ``recall()`` 检索。

        这就是短期与长期记忆的边界：**进上下文的是工作记忆和命中的规则，
        进存储的是事实与经历**。上下文窗口是每轮重读的工作区，不是仓库。
        """
```

还有一条跨模块不变式：**`TaskState` 是唯一状态真相源**。记忆模块不持有任务状态的副本，工作记忆是调用方从 `state` 派生后写进来的**视图**。两份状态迟早会不一致，而不一致的时候没人知道该信谁。

`memory.py` 里没有任何一行引用 `state.py`——这正是这条不变式的代码形态：记忆模块连 `TaskState` 的类型都不 import，谈不上持有副本。约束靠 `tests/test_invariants.py` 的 `TestSingleSourceOfTruth` 兜底。

### 2.5 得分：四个因子相乘

检索不是"取相似度最高的 k 条"，而是四个因子相乘：

```
score = similarity × trust × recency × ttl_weight
          匹配度      可信度    新鲜度      TTL 权重
```

- **similarity**：由检索路线决定（字面重叠 / 余弦相似度 / 实体命中率）。
- **trust**：直接取 `SOURCE_TRUST[source]`。
- **recency**：按半衰期指数衰减，半衰期取一天（`RECENCY_HALF_LIFE = 86_400.0`）。
- **ttl_weight**：过期条目乘 `TTL_DECAY = 0.25`，**不是 0**。

把可信度写进得分（而不是只写进元数据）是有意的：写进元数据要靠上层自觉过滤；写进得分意味着上层就算忘了，模型来源的条目也会自动排在后面。

### 2.6 两条检索路线

同一套账本规则，两种匹配方式：

```
                       ┌───────────────────────────┐
   query ─────────────►│  write()：准入 + 版本仲裁   │
                       └───────────────────────────┘
                                      │
              ┌───────────────────────┴───────────────────────┐
              ▼                                               ▼
   ┌──────────────────────┐                     ┌──────────────────────┐
   │  VectorMemory        │                     │  GraphMemory         │
   │  切块 → 嵌入 → 余弦   │                     │  实体 → 关系边 → 命中 │
   │  答：哪段话像这句话   │                     │  答：哪个事实现在成立 │
   └──────────────────────┘                     └──────────────────────┘
              │                                               │
              └───────────────► recall(query) ◄───────────────┘
```

向量路线答"哪段话像这句话"，图谱路线答"**哪个事实现在成立**"。第一条向量能答，第二条答不了——这是后端类型的结构性限制，不是模型好坏的问题。

## 三、为什么这样设计：取舍与失效条件

### 3.1 拒绝 model 来源的语义记忆，代价在哪

代价是真实的：用户说"记一下，以后都按这个来"，如果这句话被模型转述过一轮，就再也写不进去了。

所以拒绝要精确到"**模型来源**"，不能模糊成"AI 说的话"。用户亲口说的 `user` 一路放行，工具返回的结构化字段 `tool` 放行但可信度打个 0.7，只有 `model` 被拦；而且拦的是**语义记忆**——用户说"以后都按这个来"，来源是 user，不受影响。

真要记模型推断的事实，正确做法不是放宽来源策略，而是**把它标成推断**：写进情景记忆，保留来源标记，检索时分数自然低于用户亲口说的版本。

### 3.2 TTL 降权不删除

"到期就删"看起来更干净，但删了就没法回答"当时我们记的是什么"。合规场景里过期事实必须可追溯。

更麻烦的是：**删除不可逆，降权可逆**。用户说"那条记错了"的时候，你要的是"把它排到最后"，不是"它已经不在了"。代价是库里会积灰，所以过期条目还需要一条归档或清理策略。

### 3.3 规则抽事实键必然是近似的

版本仲裁要判断"这两条说的是同一件事"，就要一个事实键。本章用规则抽：

```python
def fact_key(kind: str, text: str) -> str:
    """给一条记忆算「事实键」。

    **键相同 = 说的是同一件事**（同一主体 + 同一属性），新值可以覆盖旧值。
    规则抽键必然是近似的：同义改写（「项目预算是」/「预算是」）会算出两个键。
    真要精确，得让写入方显式给键，或者上实体抽取模型。
    """
```

"Orion 项目预算是 8 万元"和"Orion 项目预算是 12 万元"能对上，键是 `semantic|orion 项目预算|是`。但"Orion 预算调整为 12 万元"未必——`调整` 不是 `是`，主体谓词都变了，会变成另一条记忆，**旧值不会被覆盖**。

这就是"规则抽取 + 规则仲裁"的天花板。出路只有两条：让写入方显式给键（本章的 `write(..., key="...")` 留了这个口子），或上实体抽取模型做键归一。**别假装规则版本能覆盖语义版本。**

### 3.4 哈希投影不是语义模型，换模型也不能解决盲区

本章的向量路线用确定性哈希投影（feature hashing）做嵌入：

```python
def hash_embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """确定性哈希投影（feature hashing）嵌入。

    每个词元散列到 ``dim`` 维里的一个桶，计数后做 L2 归一化。
    用 ``blake2b`` 而不是内置 ``hash()``，因为后者按进程随机加盐，
    跨进程就不可复现了。

    **它不是语义模型**：同义不同字会得到毫不相干的向量。
    它唯一能复刻的是「按字面相似度排序」这个行为——以及随之而来的盲区。
    """
```

**这句话必须说清楚**：它不是语义模型，同义不同字对不上。但它有教学价值——它精确复刻了"按字面相似度排序"这个行为，所以也**忠实复刻了向量检索的三条盲区**：

1. **时间指代**："一开始""上次""最早那次"不在文本里，就不在向量里。
2. **实体串台**：两个项目的记录模板化程度一样，向量分不出是哪个。
3. **否定与条件**："预算是 8 万"和"预算不是 8 万"的向量几乎重合。

换上真的 embedding 模型，第 1、3 条会缓解，**第 2 条不会**——"是哪个项目"根本不在文本里。解法在嵌入模型之外：元数据过滤、实体锚定、来源仲裁、结果重排。第 4.8 节的实跑输出把第 2 条摆到了台面上。

### 3.5 什么时候根本不该上记忆模块

这是本章最该被抄走的一节。三种情况下加记忆层是净亏损：

1. **任务本身无状态**。"把这段话翻译成英文"、"算一下这个矩阵"——上一轮的结论对这一轮毫无影响。
2. **一次性文档问答**。语料固定、不变、不跨会话复用，走常规 RAG 就够。
3. **任务生命周期就一次调用**。工作记忆的收益来自"多轮之间保持一致"，单轮任务里它一分钱不值。

判断动作很简单：**先问"下一轮我需要记得什么"，答不上来就别建。**

### 3.6 图谱路线什么时候不该上

第 27 章的三条结论直接适用：不要为一次性文档问答建图谱，抽取成本远超收益；不要在**写入高频、查询低频**的场景建图谱，那是纯成本；**实体消歧没解决之前**，不要上自动抽取加自动建边，那会稳定地产出一张错图——错图比没有图更糟，因为它看起来像个答案。

本章的 `GraphMemory` 用规则抽实体和关系，质量比不上模型，但行为**完全可预测**——在还没决定要不要上模型抽取的阶段这是优点：先跑通链路，再决定换不换抽取引擎。

### 3.7 评测：为什么"检索命中率"是个坏指标

"召回率 95%"听起来很好，但它只回答了"索引能不能捞到东西"。真正要问的是"**该想起时想起了吗**"，而这句话里藏着两个更难的维度：**该想起的时候**，期望的事实出现在 top-k 里了吗；**不该想起的时候**，旧版本、别人的项目、模型编的东西，**没有**混进结果吗？

只报第一个数字的系统会翻车：库里同时有 5 万和 12 万两条预算记录，检索器把两条都捞上来，命中率 100%，精确率 50%，而 Agent 拿到的答案有一半概率是错的。

本章的评测同时报三个数：**想起率**（该想起时想起）、**精确率**（捞回来的有多少相关）、**误想率**（不该想起时乱想）。少一个就漏一半。

## 四、动手：最小可实现

配套工程：`dragon-agent/dragon_agent/memory.py`，严格按契约 `_plan/_dragon_contract.md` 第 7 节实现。纯标准库，不联网，不需要 API Key。

### 4.1 契约要求的数据结构

契约把 `MemoryItem` 的 8 个字段写死了，多一个都算破坏契约：

```python
@dataclass
class MemoryItem:
    """一条记忆。

    字段与契约第 7 节**逐字一致**。``score`` 的含义分两段：

    - 落库时是**可信度基线**（``SOURCE_TRUST[source]``）；
    - 检索返回时是**综合得分**（匹配度 × 可信度 × 新鲜度 × TTL 权重）。

    存储里的那条不会被检索结果改写——检索返回的是副本。
    """

    id: str
    kind: str
    text: str
    source: str
    score: float = 0.0
    ttl: float | None = None
    created_at: float = 0.0
    version: int = 1


@runtime_checkable
class Memory(Protocol):
    """记忆的公共接口。"""

    def write(self, kind: str, text: str, source: str, ttl=None) -> MemoryItem:
        """写入一条记忆。返回落库后的条目。"""
        ...

    def recall(self, query: str, kind: str | None = None, k: int = 5) -> list[MemoryItem]:
        """检索：返回与查询最相关的至多 k 条。"""
        ...

    def forget(self, item_id: str) -> bool:
        """显式遗忘。命中返回 True，没命中返回 False。"""
        ...
```

`ttl` 是**相对存活秒数**（从 `created_at` 起算），不是绝对时间戳。这点值得单独说：绝对时间戳写起来直观，但没法在测试里造"已过期"的条目——除非你真的去 `sleep`。本章用可注入的时钟（`clock`）解决它。

### 4.2 写入：准入 + 版本仲裁

```python
    def write(
        self,
        kind: str,
        text: str,
        source: str,
        ttl: float | None = None,
        *,
        key: str | None = None,
    ) -> MemoryItem:
        """写入一条记忆。

        ``ttl`` 是**相对存活秒数**（从 ``created_at`` 起算），不是绝对时间戳。
        ``key`` 可以显式指定事实键；不给就按规则抽取。

        语义与程序性记忆是**原地更新**：同一个事实键上，新值覆盖旧值，
        旧值进历史（版本号 +1）。低可信来源不能覆盖高可信来源——
        那会让两条矛盾的事实同时躺在库里（第一卷第 26 章的真实事故）。
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("记忆内容不能为空")

        fact = key or fact_key(kind, text)
        decision = write_decision(kind, source, streak=self._streaks.get(fact, 0))
        if not decision.allow:
            raise MemoryWriteRejected(decision.reason)

        now = self._clock()
        stored_kind = decision.kind
        version = 1
        previous_id = self._current.get(fact)
        if previous_id is not None and stored_kind in ("semantic", "procedural"):
            previous = self._items.get(previous_id)
            if previous is not None:
                old_trust = SOURCE_TRUST.get(previous.source, 0.3)
                if decision.trust < old_trust:
                    raise MemoryWriteRejected(
                        f"已有更高可信度的记忆（来源 {previous.source}）：{previous.text}"
                    )
                version = previous.version + 1
                self._items.pop(previous_id, None)
                self._history.setdefault(fact, []).append(previous)
                self._on_supersede(previous)

        item = MemoryItem(
            id=self._next_id(),
            kind=stored_kind,
            text=text,
            source=source,
            score=decision.trust,
            ttl=ttl,
            created_at=now,
            version=version,
        )
        self._items[item.id] = item
        self._keys[item.id] = fact
        self._current[fact] = item.id
        self._index(item)
        return item
```

三件事在这一个方法里做完：**准入**（`write_decision`）、**版本仲裁**（同键覆盖 + 低可信不许覆盖高可信）、**落库**（含历史留档）。

`decision.trust < old_trust` 那一段把"用户说的 > 工具返回的"从口号变成了写路径上的硬规则：用户亲口说了 12 万，工具后来返回 99 万，这次写入**直接被拒**——因为两条矛盾的事实同时躺在库里，正是第一节那个投诉的成因。

### 4.3 检索：四个因子相乘

```python
    def recall(self, query: str, kind: str | None = None, k: int = 5) -> list[MemoryItem]:
        """检索：返回与查询最相关的至多 ``k`` 条。

        得分 = 匹配度 × 来源可信度 × 新鲜度 × TTL 权重。
        返回的是**副本**——存储里那条的 ``score`` 不会被改写。
        """
        if k <= 0:
            return []
        now = self._clock()
        scored: list[tuple[float, MemoryItem]] = []
        for item in self._items.values():
            if kind is not None and item.kind != kind:
                continue
            similarity = self._similarity(query, item)
            if similarity <= 0:
                continue
            score = (
                similarity
                * SOURCE_TRUST.get(item.source, 0.3)
                * _recency(now - item.created_at)
            )
            if self.is_expired(item, now):
                score *= TTL_DECAY
            scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], -pair[1].created_at, pair[1].id))
        return [replace(item, score=round(score, 6)) for score, item in scored[:k]]
```

`replace(item, score=...)` 返回**副本**。存储里那条的 `score` 始终是可信度基线，不被检索改写——否则第一次检索就把库里的排序依据搅乱了。

### 4.4 遗忘

```python
    def forget(self, item_id: str) -> bool:
        """显式遗忘。命中返回 True；没命中（包括已被覆盖的旧版本）返回 False。"""
        item = self._items.pop(item_id, None)
        removed = item is not None
        if item is not None:
            fact = self._keys.get(item_id)
            if self._current.get(fact) == item_id:
                self._current.pop(fact, None)
            self._unindex(item)
        for fact, versions in list(self._history.items()):
            kept = [v for v in versions if v.id != item_id]
            if len(kept) != len(versions):
                removed = True
                if kept:
                    self._history[fact] = kept
                else:
                    self._history.pop(fact, None)
        return removed
```

用户按下"忘掉"，语义必须彻底：当前条目从账本移除，索引同步拆掉，历史版本里的同 id 也一并清掉。**留在检索结果里就是没忘掉。**

### 4.5 向量路线：切块 + 哈希投影

```python
    def _index(self, item: MemoryItem) -> None:
        chunks = chunk_text(item.text, self.chunk_chars, self.overlap)
        self._vectors[item.id] = [(c, hash_embed(c, self.dim)) for c in chunks]

    def _unindex(self, item: MemoryItem) -> None:
        self._vectors.pop(item.id, None)

    def _similarity(self, query: str, item: MemoryItem) -> float:
        chunks = self._vectors.get(item.id) or []
        if not chunks:
            return super()._similarity(query, item)
        query_vector = hash_embed(query, self.dim)
        return max((cosine(query_vector, vector) for _, vector in chunks), default=0.0)
```

切块发生在**索引层**，写入单位仍是一条记忆一个 `MemoryItem`。这是刻意的：契约里的 `write()` 返回一个 `MemoryItem`，如果切块发生在写入层，一条长文本就要返回多个条目，接口立刻变味。

`max()` 取块级最高分：一条长记忆只要有一块命中就算命中。这就是重叠窗口的意义——前提和结论被切到两块时，至少有一块能捞上来。

### 4.6 图谱路线：边失效，而不是删边

```python
    def _index(self, item: MemoryItem) -> None:
        now = self._clock()
        for relation in extract_relations(item.text):
            for old in self._edges:
                if (
                    old.subject == relation.subject
                    and old.predicate == relation.predicate
                    and old.valid_to is None
                ):
                    old.valid_to = now
                    self._shadowed.setdefault(item.id, []).append(old)
            self._edges.append(Relation(
                subject=relation.subject,
                predicate=relation.predicate,
                object=relation.object,
                item_id=item.id,
                created_at=now,
            ))
```

写"Orion 预算是 12 万元"时，旧的那条边不是被删，是被补上一个 `valid_to`。于是两个问题都能答："现在预算是多少"只看 `valid_to is None` 的边；"一开始批的是多少"按时间点查，那条旧边的有效区间还在。

配套的钩子也反过来了——图谱路线覆盖旧版本时**不拆边**：

```python
    def _on_supersede(self, previous: MemoryItem) -> None:
        """旧版本被顶掉时**不拆边**。

        旧边留着，由新写入去给它补失效时间——**边失效，而不是删边**。
        """
        return
```

### 4.7 记忆评测

评测判据不调 LLM，纯关键字与规则，保证确定性与离线可跑：

```python
@dataclass
class MemoryCase:
    """一条记忆评测样本。

    三类判定一起构成「该想起时想起了」：

    - ``expect``：必须被召回的内容，**命中任意一条**即算想起；
    - ``expect_empty``：这条**不该**想起任何东西（写进去的脏数据、该被拒的写入）；
    - ``forbid``：不该出现在结果里的内容（旧版本、别人的项目）。
    """

    id: str
    setup: list[MemorySetup]
    query: str
    expect: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    expect_empty: bool = False
    k: int = 5
    #: 用哪条检索路线：``vector``（默认）或 ``graph``。
    #: 同一句查询，两条路线给出的答案可能不一样——这本身就是评测要测的东西。
    route: str = "vector"
```

`route` 字段是刻意加的：默认评测集里有一条 `entity-isolation` 用例，专门测"查 Orion 别把 Argus 捞出来"，它标了 `route="graph"`——因为**向量路线答不了这题**。评测集不是用来证明自己那套实现没毛病的，是用来把毛病的边界画出来。

### 4.8 实跑

`examples/memory_demo.py` 全离线。真实输出（节选）：

```text
1) 来源把关：模型写语义记忆被拒
  拒绝：模型来源的语义记忆被拒绝：推断出来的事实会被当成用户陈述反复引用
  工作记忆照常写入：kind=working  可信度基线=0.3

2) 版本：用户覆盖工具，旧版本留档
  旧版本 v1：Orion 项目预算是 8 万元
  新版本 v2：Orion 项目预算是 12 万元
  历史里还留着：v1 Orion 项目预算是 8 万元
  工具来源想覆盖，被拒：已有更高可信度的记忆（来源 user）：Orion 项目预算是 12 万元

3) TTL：到期降权，不是删除
  条目 mem-00001：到期前 0.5071 → 到期后 0.1267  已过期=True
  条目 mem-00002：到期前 0.3381 → 到期后 0.3377  已过期=False
  库里还剩 2 条语义记忆——过期条目没被抹掉

5) 两条路线：同一句查询，两种答案
  查询：Orion 预算是多少
  向量路线（哈希投影，只认字面）：
    0.5071  Orion 项目预算是 12 万元
    0.3381  Argus 项目预算是 5 万元
  图谱路线（实体 + 关系边）：
    1.0000  Orion 项目预算是 12 万元
  图谱路线现在成立的关系边：
    (Orion 项目预算) --是--> (12 万元)

6) 记忆评测：想起率、精确率、误想率
  用例总数        8
  该想起时想起    8  想起率 100.00%
  精确率          100.00%
  误想次数        0
```

第 5 段是本章最重要的一张图：**向量路线把 Argus 也捞出来了**，0.3381 分，排在 Orion 后面。运气好时答案还对，运气不好就答错——这正是第一节"记错了我改过的话"的同类故障，只是这次发生在实体维度上。

第 3 段是另一张：过期条目的分从 0.5071 掉到 0.1267（大致是 `TTL_DECAY = 0.25` 乘上新鲜度衰减），但它**还在库里**。

### 4.9 怎么被上下游调用

先说清楚一件事：**`memory.py` 在当前工程里是一个自洽的模块，没有被 `agent.py` 接进主循环。** `dragon_agent/agent.py` 里搜不到一处 `memory` 引用，`agent.py:274` 装配上下文时写的是 `retrieval=[]`。这不是漏写，是本章的边界：记忆层先独立跑通、单独评测，接进主循环是另一章的事。

要接的时候，接线点长这样：

```
state.py  ──派生──►  memory.write("working", ..., source="tool")
                          │
                          ▼
                     memory.read(query)      ── 工作记忆 + 命中的规则
                          │
                          ▼
context.py  assemble(task, history, retrieval, tools)
                          │  retrieval 那一层
                          ▼
                     memory.recall(query, kind="semantic", k=5)
                          │
                          ▼
agent.py  每步循环里调用；任务完成点写一条 episodic
```

- **上游（`state.py`）**：任务状态的唯一真相源。工具返回值进账本时来源标 `tool`，用户输入标 `user`。
- **旁路（`context.py`）**：`assemble()` 的 `retrieval` 参数收的就是 `memory.recall()` 的结果——这个参数是现成的（`context.py:252`），接上就行。
- **下游（`agent.py`）**：每步开始前读一次工作记忆；任务跑完、拿到结果之后写一条情景记忆。**任务没跑完不要写**。

三条接线现在都还是"应该这么接"，不是"已经这么接"。本章的实跑演示（`examples/memory_demo.py`）也印证了这一点——它自己 new 一个 `VectorMemory`，全程没经过 `agent.py`。

## 五、开源实现对照

下面是本模块在真实项目里的对应物。许可证来自各仓库的 LICENSE 文件或 GitHub 仓库页标记。

| 项目 | 仓库 | 许可证 | 和本章的关系 |
|---|---|---|---|
| Letta (MemGPT) | https://github.com/letta-ai/letta | Apache-2.0 | Agent 当常驻服务，自主读写记忆——对应第 2.4 节 |
| Mem0 | https://github.com/mem0ai/mem0 | Apache-2.0 | 独立记忆层，用 LLM 抽取并更新用户事实 |
| LangMem | https://github.com/langchain-ai/langmem | MIT | 自主写入与检索长期记忆的工具 |
| Graphiti | https://github.com/getzep/graphiti | Apache-2.0 | 增量构建带时间有效期的实体关系图谱记忆——对应第 4.6 节 |
| Zep | https://github.com/getzep/zep | Apache-2.0 | 官方仓库现以示例与集成代码为主 |
| Cognee | https://github.com/topoteretes/cognee | Apache-2.0 | 把数据加工成可检索的知识图谱记忆 |
| LlamaIndex | https://github.com/run-llama/llama_index | MIT | 以数据连接器与索引为中心，提供检索与工作流引擎 |
| chroma | https://github.com/chroma-core/chroma | Apache-2.0 | 开源嵌入数据库，托管向量索引与元数据过滤 |
| sqlite-vec | https://github.com/asg017/sqlite-vec | Apache-2.0 / MIT 双许可 | 纯 C 写的 SQLite 向量扩展 |

两处值得单独指出：**Graphiti 的"增量建图 + 边失效"**和第 4.6 节是同一个思路，本章用两条正则实现了它的骨架，代价是抽取质量差很多；**chroma 与 sqlite-vec 解决的是不同问题**——chroma 是专门的向量库，sqlite-vec 是扩展，让你在已有的 SQLite 库里加向量能力，不必再引一个服务。**记忆模块最常见的失败原因是引入了运维上没人会维护的新组件。**

本章没有引用任何一家生产向量库的具体吞吐数字或价格——那些数字随版本变，需要时去仓库和官方文档查。

## 六、坑与检查清单

**准入与来源**

- [ ] `model` 来源的**语义记忆**写入被拒绝或明确降级，不是默默写进去
- [ ] `model` 来源的**工作记忆**仍然能写——草稿也要能记
- [ ] 低可信来源不能覆盖高可信来源
- [ ] 程序性记忆不是一次成功就固化（有连续成功计数器）
- [ ] 情景记忆在**任务完成点**才写，不是每轮写
- [ ] 未知 `kind`、未知 `source` 被拒，不是当成合法值

**边界与生命周期**

- [ ] 工作记忆进上下文，情景与语义记忆只进存储
- [ ] TTL 到期是**降权**不是删除，过期条目仍能被召回
- [ ] 过期条目有清理/归档策略（否则库里积灰）
- [ ] `read()` 与 `recall()` 是两条路，没有混成一条
- [ ] 记忆模块**不持有 `TaskState` 的副本**

**检索质量**

- [ ] 长文本切块有重叠窗口，元数据随块一起落库
- [ ] 嵌入函数是什么，说得清；哈希投影**不能**对外宣称是语义检索
- [ ] 向量路线有元数据过滤作为补救（实体、时间窗）
- [ ] 实测过"查 A 会不会把 B 捞出来"
- [ ] "哪个事实现在成立"的问题有图谱路线兜底，或有明确的仲裁口径

**版本与遗忘**

- [ ] 同一事实键上，新值覆盖旧值，旧版本留档且可查
- [ ] 矛盾事实不会同时出现在同一次召回里
- [ ] 显式遗忘后检索不到（含历史版本）
- [ ] 被覆盖的旧版本被遗忘时，历史里的同 id 也一并清掉

**评测**

- [ ] 评测同时报**想起率、精确率、误想数**，不是只报检索命中率
- [ ] 评测集里有"不该想起"的负样本
- [ ] 判定器**不调 LLM**，能离线跑、能进 CI
- [ ] 改记忆模块之后先跑评测集，再看单条 demo

## 七、小结与延伸阅读

这章真正想留下的不是"接了个向量库"，而是三个判断：

1. **"记住"是四个动词，不是一个功能**。写、读、检索、遗忘，少一个这系统就会退化成一个只进不出的垃圾堆。
2. **记忆分四类，规则完全不同**。寿命、写入时机、冲突仲裁、检索方式四样都不一样。塞进一张表用同一套策略处理，是第一卷第 26 章那三类投诉的共同根因。
3. **写路径比读路径更值钱**。检索失败可以调参数，写入时把编造的事实、矛盾的旧值、一次偶然的成功固化下来，损害不可逆。所以本章把可信度、版本、TTL 全做在写路径上。

还有一条负向结论比正面结论更重要：**大多数 Agent 不需要记忆模块**。任务无状态、一次性文档问答、单轮调用——这三种情况下加记忆层是净亏损。先问"下一轮我需要记得什么"，答不上来就别建。

延伸阅读：

- 第一卷第 26 章《记忆分类学》——本章四类记忆的分类依据，以及认知科学能借什么、不能借什么。
- 第一卷第 27 章《长期记忆实现：向量、摘要与实体图谱三条路线》——三条路线的完整对比，本章只实现了其中两条的最小骨架。
- 第一卷第 28 章《上下文窗口工程：检索、压缩、淘汰与分层》——短期与长期边界的另一半。
- 第一卷第 30 章《Agentic RAG》——检索结果怎么进上下文，与本章的 `recall()` 直接对接。
- Letta (MemGPT) https://github.com/letta-ai/letta —— 让模型自己决定换入换出记忆的最完整实现。
- Graphiti https://github.com/getzep/graphiti —— 增量建图与边失效的工程实现。
- chroma https://github.com/chroma-core/chroma —— 现成的向量存储与元数据过滤。
