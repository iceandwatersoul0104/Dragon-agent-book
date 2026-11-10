# 第 27 章 长期记忆实现：向量、摘要与实体图谱三条路线

## 一、本章要解决的问题

先看一个具体的翻车。

某项目助手把历次对话切块后写进向量库，接的是一套成熟的 embedding 和检索服务。上线后被追问的第一个问题是："Orion 这个项目一开始批的预算是多少？"

Agent 答：12 万元。

真实情况是：3 月批的是 8 万，4 月改成 12 万。"一开始"指的是 3 月那一次。Agent 给的是库里最新的一块文本——因为切好的块长得几乎一样，向量分数只差 0.01，检索层没有任何依据知道"一开始"指的是哪一块。

同一套系统里还有一类更隐蔽的错误。用户问"项目预算是多少"，没点名是哪个项目。检索 top-1 返回的是另一个项目 Argus 的记录——两个项目的表述模板化程度一致，谁的块更短谁的分数就更高。

这两个错误都不是模型不聪明，也不是 embedding 不够好。它们是**后端类型**带来的结构性限制：语义相似度只能告诉你"哪段话像这句话"，而"哪个项目""哪个时间点""哪个版本更新了哪个"根本不在文本里，向量也就无从表达。

于是问题变成了三个：长期记忆到底存在哪种形态里？每种形态能回答什么、答不了什么？真实项目里怎么把它们组合起来。

## 二、原理：机制拆解

### 2.1 向量路线：切块、相似度与它的盲区

标准流程没有任何玄学：

```
写入:  原始文本 ──切块──► chunk[0..n] ──embedding──► 向量 ──► 索引
                                                        │
查询:  query ──embedding──► q ──余弦相似度 top-k──────┘
                       └──► 命中: 一组文本块 + 分数, 没有了
```

切块（chunking）是这条路线的第一个决策点，也是最容易做砸的一步：

- **块太大**：一块里混进多个主题，向量算出来是这些主题的平均值，谁都不像，检索命中了也定位不到具体那句话。
- **块太小**：前提和结论被切到两块里。"预算是"一块，"12 万"另一块，两块单独拿出来都没有语义——检索永远命中半句话。
- **重叠窗口**：用索引体积和重复命中换完整性，是常见的折中。重叠要多大取决于你关心的最短事实有多长。
- **元数据必须和块一起落库**：实体、时间戳、来源、事实类型。事后补不回来——切块时没记的实体信息，检索时再猜就晚了。后面所有过滤都建在这层元数据上。

embedding 真正表达不了的东西，可以归成三类盲区：

1. **时间指代**。"一开始""上次""最早那次"不在任何一块文本里，因此不在任何向量里。两块只差一个月的记录几乎同向量，谁新谁旧、谁覆盖谁，向量无从判断。检索层只能按时间倒序兜底，于是"永远召回最新那条"成了默认行为。
2. **实体串台**。两个项目的记录模板化程度一样，向量分不出"哪个项目"。加上块长度的稀释效应，短块天然占便宜——这会系统性地偏向信息量更少的那条。
3. **否定与条件**。"预算是 8 万"和"预算不是 8 万"的向量距离很小；"12 万，含税"和"12 万，不含税"几乎重合。把这类句子切块存进向量库，等于把语义边界交给了 embedding，而它并不理解否定。

所以"语义相似"不等于"该被想起"。一条记忆值得被召回，至少要同时满足三件事：

- **相关**：说的确实是这件事；
- **必要**：不取它就会做错；
- **可执行**：取回来能改变接下来的动作。

向量只处理第一件。第二、第三件要靠元数据过滤（实体、时间窗）、新鲜度与置信度门槛、结果重排（rerank）、以及来源可信度仲裁。工程上常见的错误是只做第一件，然后把 top-k 直接塞进提示词——那不是记忆系统，是搜索引擎。

### 2.2 摘要与压缩路线：虚拟上下文分页与摘要漂移（summary drift）

MemGPT（现为 Letta）提出的思路不是"定期把历史压成一段话"，而是把上下文窗口当成内存来看待：

```
┌──────────── 上下文窗口 (≈DRAM, 小而贵) ────────────┐
│  系统提示 + 当前任务 + 最近几轮 + 换入的片段          │
└───────────┬──────────────────────────────▲──────────┘
            │ 换出: 写入外部存储              │ 换入: 检索后读回
            ▼                              │
┌──────────── 外部存储 (≈磁盘, 大而便宜) ───────────┐
│  核心记忆区 / 归档区(长文档) / 历史对话区          │
└───────────────────────────────────────────────────┘
```

关键差别在**换入换出的决策由谁做**：不是外部策略定时触发，而是模型自己调用工具去读写这些区块。它因此比"每 N 轮摘要一次"强在三处——按需，不会浪费；可逆，原文还在，模型能回头查；而且模型能感知到自己缺了什么，缺什么就换入什么。

代价是压缩本身。摘要有两种典型损伤：

- **链式累积**：滚动摘要是对上一版摘要再摘要，误差沿链放大，第 20 轮的摘要和第 1 轮的对话可能已经没关系了。
- **粘性旧值**（更常见也更隐蔽）：摘要生成时保留了某个数值，之后事实变了，但没人回头改摘要文本。结果是事实已经是 12 万，摘要里仍写着 8 万，两边长期共存，谁也没发现。

还有一类不可逆损伤：摘要覆盖原文后，具体数字、否定词、例外条款一起没了。这类信息一旦丢失，没有任何工程手段能恢复。

工程上的缓解手段有三条，代价都不高：第一，**把会变的事实从散文里拿出来**，放进结构化台账，摘要只保留不可变的脉络；第二，**摘要永远带原文指针**，需要细节时能回查；第三，**抽样做一致性校验**，拿摘要里的数值和台账比对，不一致就告警。

### 2.3 实体图谱路线：时序知识图谱（temporal knowledge graph）

第三条路线把记忆表示成实体和关系，并且给边装上时间。

```
   (Orion, 预算, 8万元)   valid: 2026-03-02 ──────► 2026-04-11
                          created: 2026-03-02    invalidated: 2026-04-11
   (Orion, 预算, 12万元)  valid: 2026-04-11 ──────────────────────► ∞
                          created: 2026-04-11
```

两个设计要点：

**边失效，而不是删边。** 事实变了，正确做法不是"追加一条新的"，也不是"把旧的删掉"，而是让新边生效、给旧边补上失效时间。这样"现在的事实是什么"和"以前的事实是什么"能同时被回答。Graphiti 的做法就是增量抽取实体与关系，写入新事实时对已有边做失效处理，而不是重建整张图。

**bi-temporal：两条时间轴。** 一条是事件时间（valid time）——这件事在现实中何时成立；另一条是事务时间（transaction time）——系统何时知道这件事。为什么两条都要：回填历史数据、或者纠正一条写错的记录时，两条轴会同时移动。只保留一条，"当时的世界是什么样"和"当时系统知道什么"就永远分不开，审计和重放必然出错。

成本也很实在：抽取实体和关系要调模型，写入放大严重；查询要遍历图再做文本匹配；实体消歧（"张工"是不是"张三"）是最脏的一块；而且图谱擅长结构化事实，不擅长"把原文找出来给你看"——引用还得回原文。

## 三、为什么这样设计：取舍与失效条件

### 3.1 每条路线什么时候不该用

向量路线：不要把它当唯一真源。它的 top-k 结果在结构上是"一批文本块"，没有真值语义。凡是需要"哪个事实现在成立"的场景（预算、额度、状态、负责人），向量都不该单独拍板。高时效、强一致的数据同样不该进向量库——这一点和第 26 章的结论一致：记忆副本必然过期，过期还是静默的。

摘要路线：不要在没有原文指针的前提下只留摘要；不要用它回答需要逐字保真的问题（合同条款、错误码、参数名、任何带否定或例外的句子）；更不要把滚动摘要当成可更新的状态——它不是状态，它是状态的投影。

图谱路线：不要为一次性文档问答建图谱，抽取成本远超收益；不要在写入高频、查询低频的场景建图谱，那是纯成本；实体消歧没解决之前，不要上自动抽取加自动建边，那会稳定地产出一张错图；也别指望图谱给原文证据，它给的是结构化事实，引用要回原文找。

### 3.2 选型建议

| 问题类型 | 首选 | 补充 |
|---|---|---|
| 事实查询："预算多少 / 错误码是啥" | 图谱 | 向量补证据原文 |
| 模糊回忆："上次那件事" | 向量 | 摘要补脉络 |
| 原文查证：合同条款、日志 | 向量 | 图谱给实体锚点 |
| 变化历史："一开始定多少 / 后来改没改" | 图谱（有效期） | 摘要给背景 |
| 长对话脉络 | 摘要 | 图谱给锚点 |
| 一次性文档问答 | 都不建，走常规 RAG | — |

### 3.3 混合方案与仲裁口径

工程上真正稳的形态是三层并存，各自干自己擅长的事：

```
query ──► ① 图谱: 实体 + 谓词 + 时间意图 ──► 锁定事实集合与有效期
          ② 向量: 用图谱给出的实体与时间窗做过滤 ──► 召回证据原文
          ③ 摘要: 补脉络、背景、不可变的上下文
          ④ 仲裁: 用户显式陈述 > 工具返回 > 图谱推导 > 摘要推断
          ⑤ 出口: 答案 + 有效期 + 证据句 + 来源
```

三条落地要点：

1. **三层必须共享同一套 id**：entity、谓词、时间戳、doc_id 要能互相对上，否则拼不成一条链。
2. **仲裁优先级要写死在代码里**，不能让模型临场判断"哪个更可信"。模型的判断在有冲突时恰恰最不可靠。
3. **代价分布要提前想清楚**。定性地说：写入成本向量最低、摘要居中、图谱最高；查询延迟向量最低；存储占用向量最大；可解释性和"哪个事实现在成立"这类问题的回答能力，图谱最强。混合方案的成本主要在写入侧和实体消歧上。

## 四、动手：最小可实现

下面这个实现把三条路线放进同一个内存里，用同一份语料喂进去，然后对同一个问题打印三份答案。依赖只有 Python 3.9+ 标准库，mock 模式内置规则抽取，不需要任何 API Key；加 `--live` 切换到真实模型抽取。完整文件见 `code/ch27-long-term-memory/main.py`，下面摘的是四条主路径，省略了分词、计数等辅助定义。

```python
# ---- 路线一: 向量。切块 -> embedding -> 余弦 top-k ----
def chunk_text(text, max_chars=60, overlap=12):
    """按句末标点切句, 聚合到 max_chars, 相邻块保留重叠窗口。"""
    sentences = [s for s in re.split(r"(?<=[。！？!?；;\n])", text) if s.strip()]
    chunks, buf = [], ""
    for s in sentences:
        if not buf:
            buf = s
        elif len(buf) + len(s) <= max_chars:
            buf += s
        else:
            chunks.append(buf.strip())
            buf = (s[-overlap:] + s) if overlap else s
    if buf.strip():
        chunks.append(buf.strip())
    return chunks

def embed(text, dim=1024):
    """确定性哈希 embedding(feature hashing + L2 归一化)。生产环境换真模型。"""
    vec = [0.0] * dim
    for tok, c in Counter(_tokens(text)).items():
        h = hashlib.md5(tok.encode()).digest()
        vec[int.from_bytes(h[:4], "big") % dim] += 1.0 + math.log(c)
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]

# ---- 路线二: 摘要。摘要粘住首次值, 台账存真值 ----
class SummaryStore:
    def ingest(self, entity, text, at, facts):
        for pred, obj in facts:
            rec = self.ledger.setdefault((entity, pred),
                                         {"value": obj, "first_value": obj, "history": []})
            if rec["value"] != obj:
                rec["history"].append((rec["value"], at))
                self.drift.append(f"{entity}.{pred}: 事实已改为 {obj}, 摘要仍是 {rec['first_value']}")
                rec["value"] = obj
        self.digest = self._compress()          # 只留话题 + 首次值 = 漂移源

    def _compress(self):
        return "；".join(f"{e} 的 {p} 是 {r['first_value']}"
                         for (e, p), r in self.ledger.items()) + "。"

# ---- 路线三: 图谱。新事实生效 -> 旧边打失效时间, 不删除 ----
class GraphStore:
    def assert_fact(self, subject, predicate, obj, valid_from):
        for e in self.edges:
            if (e.subject, e.predicate) == (subject, predicate) and e.valid_to is None:
                if e.object == obj:
                    return e
                e.valid_to = valid_from            # 事件时间轴: 旧事实何时失效
                e.invalidated_at = valid_from      # 事务时间轴: 系统何时知道它失效
        edge = Edge(subject, predicate, obj, valid_from, None, valid_from, None)
        self.edges.append(edge)
        return edge

    def query(self, subject=None, predicate=None, as_of=None, knew_at=None):
        """as_of 走事件时间轴, knew_at 走事务时间轴。"""
        out = []
        for e in self.edges:
            if subject and e.subject != subject:        continue
            if predicate and e.predicate != predicate:  continue
            if as_of is not None and not (e.valid_from <= as_of and
                                          (e.valid_to is None or e.valid_to > as_of)):
                continue
            if knew_at is not None and e.created_at > knew_at:
                continue
            out.append(e)
        return sorted(out, key=lambda e: e.valid_from)

# ---- 混合: 图谱定事实, 向量补证据, 摘要补脉络 ----
def answer(mem, q):
    ents  = [e for e in mem.graph.entities if e.lower() in q.lower()]
    preds = [p for p, aliases in PRED_ALIASES.items() if any(a in q for a in aliases)]
    edges = []
    for p in preds:
        edges += mem.graph.query(subject=ents[0] if ents else None, predicate=p)
    earliest = any(m in q for m in ["一开始", "最初", "最初定", "原来", "第一次"])
    alive = [e for e in edges if e.valid_to is None]
    chosen = edges[0] if earliest and edges else (alive[-1] if alive else None)
    evidence = mem.vector.search(q, top_k=2, entity=chosen.subject,
                                 since=chosen.valid_from, until=chosen.valid_to,
                                 must_contain=[chosen.predicate]) if chosen else []
    return chosen, evidence, mem.summary.digest
```

跑起来是这样：

```
$ python main.py
问题: Orion 一开始的预算是多少？

[向量路线] 只按相似度排序的 top-3
  0.316  Orion  2026-04-11  Orion 的预算改成 12 万了，走企业版。      <- 错: 给了最新那条
  0.306  Orion  2026-03-02  我们是 Orion 项目的实施方，3 月的预算是 8 万，要 5 月上线。
  0.302  Argus  2026-05-20  Argus 项目的预算是 6 万，先做试点。

[摘要路线] 滚动摘要
  摘要原文: Orion 的 预算 是 8万元；Orion 的 上线时间 是 5月；...
  漂移: Orion.预算: 事实已从 8万元 改为 12万元，但摘要里仍写着 8万元

[图谱路线] 实体 + 时序边
  (Orion, 预算, 8万元)  有效 2026-03-02 ~ 2026-04-11  [已失效于 2026-04-11]
  (Orion, 预算, 12万元) 有效 2026-04-11 ~ ∞          [当前有效]

[混合] 图谱定事实, 向量补证据, 摘要补脉络
  答案: Orion 的 预算 = 8万元 (有效 2026-03-02 ~ 2026-04-11)
  证据(0.306): 我们是 Orion 项目的实施方，3 月的预算是 8 万，要 5 月上线。
```

同一个文件里还有第二个问题"项目预算是多少？"，向量路线的 top-1 是另一个项目 Argus 的记录，图谱路线则老老实实列出两个项目的三条边，混合层据此提示"提问没有指明项目，需要先消歧"。

## 五、开源实现对照

- **Mem0** — https://github.com/mem0ai/mem0 ：独立记忆层的代表。核心做法是用模型从对话中抽取事实，对同一对象同一属性的新事实做新增、更新或删除的判定；更新时旧事实被标记失效并保留历史，每条事实带元数据。它把记忆管理从提示词工程搬到了独立模块。争议也在这里：它的论文（arXiv:2504.19413）在 LOCOMO 基准上报告过相对全量上下文的准确率与成本优势，这组数字随后被竞品 Zep 公开质疑，Mem0 又发文反驳，分歧集中在对照基线怎么实现、系统提示是否一致、延迟口径怎么算。工程含义很直接——引用这类论文里的百分比时，应当作"某种设置下的一次测量"，而不是可外推的规格。
- **Letta（MemGPT）** — https://github.com/letta-ai/letta ：分页思想的落地。记忆是 Agent 通过工具自主读写的区块，换入换出的决策权在模型手里，代价是记忆策略变成模型行为的一部分，难以回归测试。
- **Graphiti** — https://github.com/getzep/graphiti ：时序图谱的代表性实现，增量抽取、边失效、bi-temporal 的两条时间轴都落在边上；也是把"事实变化"表达成时间区间而不是覆盖的标准答案。
- **Zep** — https://github.com/getzep/zep ：官方仓库现在以示例与集成代码为主，落地形态要看它的商业产品。
- **Cognee** — https://github.com/topoteretes/cognee ：用小模型把原始数据加工成可检索的知识图谱，思路是"检索质量问题先在写入端解决"。
- **LangMem** — https://github.com/langchain-ai/langmem ：给 LangChain 提供长期记忆的写入与检索工具，并按语义、情景、程序三类划分，相当于把"选哪条路线"做成了调用时的参数。
- **LangGraph** — https://github.com/langchain-ai/langgraph ：检查点（checkpoint）机制是另一种记忆——状态快照而非事实，适合工作流恢复与回放，不适合当语义记忆用。
- **LlamaIndex**（https://github.com/run-llama/llama_index ）、**LangChain**（https://github.com/langchain-ai/langchain ）、**RAGFlow**（https://github.com/infiniflow/ragflow ）：分别代表索引中心的检索框架、LCEL 组合式管线，以及把上下文质量在预处理端解决的路线，都不是长期记忆层。

对照下来有一条明显的趋势：事实类记忆正从"向量库 + 提示词"里挪到带元数据的结构化存储；向量从唯一检索手段退化成"找原文证据"的组件；图谱负责"哪个事实现在成立"，摘要负责"上下文脉络"，向量负责"模糊召回"。

## 六、坑与检查清单

**写入**

- [ ] 每条记录带 entity、predicate、ts、source、doc_id，缺一不可
- [ ] 事实变化走"新边生效 + 旧边失效"，既不删除也不单纯追加
- [ ] 切块时把元数据一起落库，事后补不回来
- [ ] 摘要带原文指针，不做唯一副本
- [ ] 会变的事实抽成结构化字段，不留在散文摘要里

**检索**

- [ ] 先按元数据过滤，再按相似度排序，顺序不能反
- [ ] top-k 带时间窗口，并至少保留一个历史版本
- [ ] 向量召回结果不直接当答案，要过重排与仲裁
- [ ] 同一属性存在多版本时，返回有效期而不是只给一个值
- [ ] 提问未指明实体时，返回候选集合并要求消歧，不静默选一条

**一致性与成本**

- [ ] 摘要与结构化台账做定期一致性比对，不一致就告警
- [ ] 统计写入放大：抽取、嵌入、建边各自的调用次数与耗时
- [ ] 记录每次回答用了哪些事实、哪些证据块、来源是谁
- [ ] 有一组回归用例，覆盖"事实变更之后，旧问答仍可复现"

## 七、小结与延伸阅读

三条路线不是三选一。向量擅长"找到像的话"，摘要擅长"压出脉络"，图谱擅长"说清哪个事实现在成立"。真正决定记忆系统好不好用的，不是选了哪条路线，而是有没有把"该想起什么"这件事从相似度手里拿回来，自己拿着时间、实体和来源去判断。

延伸阅读：

- Letta（MemGPT）: https://github.com/letta-ai/letta
- Mem0: https://github.com/mem0ai/mem0
- LangMem: https://github.com/langchain-ai/langmem
- Graphiti: https://github.com/getzep/graphiti
- Cognee: https://github.com/topoteretes/cognee
- LangGraph: https://github.com/langchain-ai/langgraph
- Mem0 论文: https://arxiv.org/abs/2504.19413
- MemGPT 论文（Letta 前身的工作）: https://arxiv.org/abs/2310.08560
