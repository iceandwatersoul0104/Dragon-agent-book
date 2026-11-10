"""记忆：读 / 写 / 遗忘 / 检索，以及来源、TTL、版本三条记账规则。

对应第二卷第 8 章。这一章要解决的是「让 Agent 记住东西，但记住得**可控**」。

四条硬约束（来自 ``_plan/_dragon_contract.md`` 第 7 节）
----------------------------------------------------------

1. **来源可信度 user > tool > model**。``model`` 来源的语义记忆写入**必须被拒绝或降级**。
2. **TTL 到期是降权不是删除**。过期条目仍然可以被召回，只是排序下沉。
3. **四类记忆**（工作 / 情景 / 语义 / 程序性）寿命、写入时机、冲突规则都不同。
4. **``TaskState`` 是唯一状态真相源**。本模块不持有任务状态的副本——
   工作记忆是调用方从 ``state`` 派生后写进来的**视图**，不是第二份状态。

两条最小实现路线
----------------

- ``VectorMemory``：切块 → 确定性哈希投影嵌入 → 余弦相似度 top-k。
- ``GraphMemory``：实体抽取 → 关系边（带有效期）→ 按实体检索。

两条路线共用同一套账本规则（来源、TTL、版本、遗忘），所以换路线不换语义。

嵌入函数的真相
--------------
``hash_embed()`` 是**确定性哈希投影（feature hashing）**，**不是语义模型**。
它只能复刻「按字面相似度排序」的行为，因此也忠实复刻了向量检索的三类盲区：
时间指代、实体串台、否定与条件。换上真的 embedding 模型，这三条盲区不会消失——
那是后端类型带来的结构性限制，不是模型好坏的问题（第一卷第 27 章）。

纯标准库：不联网、不需要 API Key、同样输入永远同样输出。
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

__all__ = [
    "MemoryItem",
    "Memory",
    "MemoryWriteRejected",
    "WriteDecision",
    "Relation",
    "MemorySetup",
    "MemoryCase",
    "MemoryEvalReport",
    "InMemoryMemory",
    "VectorMemory",
    "GraphMemory",
    "KINDS",
    "SOURCE_TRUST",
    "PROCEDURAL_STREAK",
    "TTL_DECAY",
    "RECENCY_HALF_LIFE",
    "EMBED_DIM",
    "write_decision",
    "evaluate_memory",
    "default_memory_cases",
    "default_memory_factory",
    "tokenize",
    "chunk_text",
    "hash_embed",
    "cosine",
    "extract_entities",
    "extract_relations",
]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 四类记忆。与第一卷第 26 章同名：工作 / 情景 / 语义 / 程序性。
KINDS: tuple[str, ...] = ("working", "episodic", "semantic", "procedural")

#: 来源可信度。user > tool > model。
SOURCE_TRUST: dict[str, float] = {
    "user": 1.0,
    "tool": 0.7,
    "model": 0.3,
}

#: 程序性记忆要连续成功多少次才允许固化。一次偶然成功固化出来的规则极难改。
PROCEDURAL_STREAK = 3

#: TTL 到期后的权重。**降权不是删除**——过期条目仍可召回，只是排后面。
TTL_DECAY = 0.25

#: 可召回性随时间的半衰期（秒）。一天。
RECENCY_HALF_LIFE = 86_400.0

#: 哈希投影的维度。
EMBED_DIM = 256


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


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


class MemoryWriteRejected(Exception):
    """写入被拒绝。

    两种来源：来源可信度不够（model 写语义记忆），或者低可信来源试图覆盖
    高可信来源已经写下的事实。
    """


@dataclass(frozen=True)
class WriteDecision:
    """一次写入的准入判定结果。"""

    allow: bool
    kind: str
    trust: float
    reason: str


@dataclass
class Relation:
    """一条带有效期的关系边。

    **边失效，而不是删边**：事实变了，正确做法是给旧边补一个失效时间，
    这样「现在的事实是什么」和「以前的事实是什么」能同时被回答。
    """

    subject: str
    predicate: str
    object: str
    item_id: str = ""
    created_at: float = 0.0
    valid_to: float | None = None

    def is_valid(self, at: float) -> bool:
        """在时刻 ``at`` 这条边是否成立（只看 valid time 这一条轴）。

        成立区间是 ``[created_at, valid_to)``。
        """
        if at < self.created_at:
            return False
        return self.valid_to is None or at < self.valid_to


@dataclass(frozen=True)
class MemorySetup:
    """评测样本里的一条前置写入。"""

    kind: str
    text: str
    source: str = "user"
    ttl: float | None = None


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


@dataclass
class MemoryEvalReport:
    """记忆评测报告。

    ``recall_rate`` 只回答「该想起时想起了吗」。``precision`` 和
    ``false_recall`` 才回答「有没有乱想」——只看命中率是不够的。
    """

    total: int
    recalled: int
    false_recall: int
    precision: float
    recall_rate: float
    missed: list[str] = field(default_factory=list)
    polluted: list[str] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "recalled": self.recalled,
            "false_recall": self.false_recall,
            "precision": round(self.precision, 4),
            "recall_rate": round(self.recall_rate, 4),
            "missed": list(self.missed),
            "polluted": list(self.polluted),
        }


# ---------------------------------------------------------------------------
# 写入时机的判断规则
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 文本工具：分词 / 切块 / 哈希嵌入 / 实体与关系抽取
# ---------------------------------------------------------------------------

_CJK = "\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
_LATIN_TOKEN = re.compile(r"[A-Za-z0-9_]+")
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")

#: 「X 的 Y 是 Z」——中文事实最常见的形状。
_REL_OWNED = re.compile(
    r"(?P<subj>[^，。；;,\n]{1,40}?)\s*的\s*(?P<attr>[^，。；;,\n]{1,30}?)\s*"
    r"(?:是|为|＝|=|:|：)\s*(?P<val>[^，。；;,\n]+)"
)
#: 「X 是 Z」——没有「的」时的退化形状。
_REL_PLAIN = re.compile(
    r"(?P<subj>[^，。；;,\n]{1,40}?)\s*(?:是|为|＝|=|:|：)\s*(?P<val>[^，。；;,\n]+)"
)

#: 拉丁专有名词：Orion、Argus。首字母大写 + 至少两个字符。
_LATIN_ENTITY = re.compile(
    r"(?<![A-Za-z0-9_])([A-Z][A-Za-z0-9_][A-Za-z0-9_\-]{1,})(?![a-z0-9_])"
)
#: 引号里的名字。
_QUOTED = re.compile(r"[「『\"“]([^」』\"”]{1,24})[」』\"”]")
#: 「XX 的」——中文里 XX 多半是实体。
_CJK_OWNER = re.compile(rf"([{_CJK}]{{2,8}})\s*的")


def tokenize(text: str) -> list[str]:
    """切成可比较的词元。

    拉丁按词切；**中文按字符二元组切**——没有分词器时的标准做法，
    「预算上限」会切成「预算/算上/上限」，不需要词典也能对上。
    """
    text = (text or "").lower()
    tokens: list[str] = []
    for m in _LATIN_TOKEN.finditer(text):
        tokens.append(m.group(0))
    for run in re.findall(rf"[{_CJK}]+", text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def chunk_text(text: str, max_chars: int = 120, overlap: int = 24) -> list[str]:
    """把长文本切成带重叠窗口的块。

    切块是向量路线的第一个决策点，也是最容易做砸的一步：

    - 块太大：一块里混进多个主题，谁都不像；
    - 块太小：前提和结论被切到两块里，检索永远命中半句话；
    - 重叠：用索引体积和重复命中换完整性。

    这里按句末标点切句，再聚到 ``max_chars`` 以内，相邻块保留 ``overlap``
    个字符的重叠。超长的单句会被硬切开，不会撑爆块。
    """
    sentences = [s.strip() for s in _SENT_SPLIT.split(text or "") if s and s.strip()]
    chunks: list[str] = []
    buf = ""
    for sentence in sentences:
        while len(sentence) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        if not buf:
            buf = sentence
        elif len(buf) + len(sentence) <= max_chars:
            buf += sentence
        else:
            chunks.append(buf)
            buf = (buf[-overlap:] + sentence) if overlap else sentence
    if buf:
        chunks.append(buf)
    return chunks


def hash_embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """确定性哈希投影（feature hashing）嵌入。

    每个词元散列到 ``dim`` 维里的一个桶，计数后做 L2 归一化。
    用 ``blake2b`` 而不是内置 ``hash()``，因为后者按进程随机加盐，
    跨进程就不可复现了。

    **它不是语义模型**：同义不同字会得到毫不相干的向量。
    它唯一能复刻的是「按字面相似度排序」这个行为——以及随之而来的盲区。
    """
    vec = [0.0] * dim
    for token, count in Counter(tokenize(text)).items():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        vec[int.from_bytes(digest, "big") % dim] += 1.0 + math.log(count)
    norm = math.sqrt(sum(v * v for v in vec))
    if not norm:
        return vec
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。``hash_embed`` 的输出已经 L2 归一化，点积即余弦。"""
    return sum(x * y for x, y in zip(a, b))


def extract_relations(text: str) -> list[Relation]:
    """从一句话里抽关系边（主体 / 谓词 / 客体）。

    规则抽取，只认两种形状：「X 的 Y 是 Z」和「X 是 Z」。
    它**抽不出隐含关系**，也**分不清「张工」和「张三」**——
    真做实体消歧得上模型。规则版的作用是：让图谱路线在没有模型的情况下
    仍然能跑通，并且行为完全可预测。
    """
    relations: list[Relation] = []
    for segment in re.split(r"[，。；;,\n]+", text or ""):
        segment = segment.strip()
        if not segment:
            continue
        m = _REL_OWNED.search(segment)
        if m:
            relations.append(Relation(
                subject=m.group("subj").strip(),
                predicate=m.group("attr").strip(),
                object=m.group("val").strip(),
            ))
            continue
        m = _REL_PLAIN.search(segment)
        if m:
            relations.append(Relation(
                subject=m.group("subj").strip(),
                predicate="是",
                object=m.group("val").strip(),
            ))
    return relations


def extract_entities(text: str) -> list[str]:
    """抽出实体：拉丁专有名词、引号里的名字、「XX 的」里的 XX。

    这是启发式，不是 NER。查不到实体时图谱路线会退回字面匹配——
    图谱的强项是「实体已知」的查询，实体未知就得靠别的路线。
    """
    entities: list[str] = []
    for m in _LATIN_ENTITY.finditer(text or ""):
        entities.append(m.group(1))
    for m in _QUOTED.finditer(text or ""):
        entities.append(m.group(1))
    for m in _CJK_OWNER.finditer(text or ""):
        entities.append(m.group(1))
    seen: set[str] = set()
    unique: list[str] = []
    for e in entities:
        if e and e not in seen:
            seen.add(e)
            unique.append(e)
    return unique


def fact_key(kind: str, text: str) -> str:
    """给一条记忆算「事实键」。

    **键相同 = 说的是同一件事**（同一主体 + 同一属性），新值可以覆盖旧值。
    规则抽键必然是近似的：同义改写（「项目预算是」/「预算是」）会算出两个键。
    真要精确，得让写入方显式给键，或者上实体抽取模型。
    """
    relations = extract_relations(text)
    if relations:
        first = relations[0]
        return f"{kind}|{first.subject.strip().lower()}|{first.predicate.strip().lower()}"
    return f"{kind}|__raw__|{text.strip().lower()}"


def _recency(age_seconds: float) -> float:
    """新鲜度权重。按半衰期指数衰减。"""
    if age_seconds <= 0:
        return 1.0
    return 0.5 ** (age_seconds / RECENCY_HALF_LIFE)


# ---------------------------------------------------------------------------
# 基线实现
# ---------------------------------------------------------------------------


class InMemoryMemory:
    """基线记忆实现：账本 + 字面匹配。

    这一层承载**所有记账规则**——来源可信度、TTL 降权、版本、遗忘——
    检索路线（向量 / 图谱）是它的子类，只换掉「怎么算匹配度」。

    构造参数故意宽松：``config`` 只是留着备用（给未来的持久化、路径），
    ``clock`` 允许注入假时钟，让 TTL 与新鲜度可以确定性地测。
    """

    def __init__(
        self,
        config: Any = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._clock = clock
        self._items: dict[str, MemoryItem] = {}
        self._history: dict[str, list[MemoryItem]] = {}
        self._current: dict[str, str] = {}
        self._keys: dict[str, str] = {}
        self._streaks: dict[str, int] = {}
        self._seq = 0

    # -------------------------------------------------------------- 公共接口

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

    # -------------------------------------------------------------- 附加能力

    def read(self, query: str = "", kind: str | None = None, limit: int = 20) -> list[MemoryItem]:
        """装配当前上下文。

        - **工作记忆**：全量注入（新到旧）——它是「这一轮正在用的东西」；
        - **程序性记忆**：按触发词匹配上才注入；
        - **情景与语义记忆**：不直接注入，必须走 ``recall()`` 检索。

        这就是短期与长期记忆的边界：**进上下文的是工作记忆和命中的规则，
        进存储的是事实与经历**。上下文窗口是每轮重读的工作区，不是仓库。
        """
        now = self._clock()
        pool: list[tuple[float, MemoryItem]] = []
        for item in self._items.values():
            if kind is not None:
                if item.kind != kind:
                    continue
            else:
                if item.kind not in ("working", "procedural"):
                    continue
                if item.kind == "procedural" and self._similarity(query, item) <= 0:
                    continue
            score = SOURCE_TRUST.get(item.source, 0.3) * _recency(now - item.created_at)
            if self.is_expired(item, now):
                score *= TTL_DECAY
            pool.append((score, item))
        pool.sort(key=lambda pair: (-pair[0], -pair[1].created_at, pair[1].id))
        return [replace(item, score=round(score, 6)) for score, item in pool[:limit]]

    def history(self, item_id: str | None = None) -> list[MemoryItem]:
        """被覆盖掉的旧版本。

        语义记忆原地更新，但旧版本要留着——事后能回答「当时为什么那么做」。
        """
        if item_id is None:
            return [v for versions in self._history.values() for v in versions]
        fact = self._keys.get(item_id)
        if fact is not None:
            return list(self._history.get(fact, []))
        for versions in self._history.values():
            if any(v.id == item_id for v in versions):
                return list(versions)
        return []

    def record_success(self, text: str = "", *, key: str | None = None) -> int:
        """记一次同类任务成功，返回连续成功次数。

        程序性记忆要连续成功 ``PROCEDURAL_STREAK`` 次才允许固化。
        """
        fact = key or fact_key("procedural", text)
        self._streaks[fact] = self._streaks.get(fact, 0) + 1
        return self._streaks[fact]

    def expires_at(self, item: MemoryItem) -> float | None:
        """条目的失效时刻；没有 TTL 就是 ``None``（永不过期）。"""
        return None if item.ttl is None else item.created_at + item.ttl

    def is_expired(self, item: MemoryItem, at: float | None = None) -> bool:
        """条目是否已过 TTL。**过期只是降权，不是删除。**"""
        expiry = self.expires_at(item)
        if expiry is None:
            return False
        now = self._clock() if at is None else at
        return now > expiry

    def stats(self) -> dict:
        """账本概览：各类记忆多少条、留了几个历史版本。"""
        counts = {kind: 0 for kind in KINDS}
        for item in self._items.values():
            counts[item.kind] = counts.get(item.kind, 0) + 1
        counts["total"] = len(self._items)
        counts["history"] = sum(len(v) for v in self._history.values())
        return counts

    # -------------------------------------------------------------- 内部钩子

    def _next_id(self) -> str:
        self._seq += 1
        return f"mem-{self._seq:05d}"

    def _index(self, item: MemoryItem) -> None:
        """建立检索索引。基线路线不需要索引，留空。"""

    def _unindex(self, item: MemoryItem) -> None:
        """拆掉检索索引。基线路线不需要索引，留空。"""

    def _on_supersede(self, previous: MemoryItem) -> None:
        """旧版本被新版本顶掉时的收尾。

        默认把旧版本的索引拆掉。**图谱路线要反过来**——旧边不是被删，
        而是被新的写入**失效**（补一个失效时间），这样「以前的事实是什么」
        仍然答得出来。
        """
        self._unindex(previous)

    def _similarity(self, query: str, item: MemoryItem) -> float:
        """匹配度，0 到 1。基线走字面重叠（Jaccard）。"""
        q = set(tokenize(query))
        t = set(tokenize(item.text))
        if not q or not t:
            return 0.0
        overlap = len(q & t)
        if not overlap:
            return 0.0
        return overlap / len(q | t)


# ---------------------------------------------------------------------------
# 路线一：向量
# ---------------------------------------------------------------------------


class VectorMemory(InMemoryMemory):
    """向量路线：切块 → 哈希投影嵌入 → 余弦相似度 top-k。

    ``hash_embed`` **不是语义模型**。它能复刻的只有「按字面相似度排序」，
    以及随之而来的三条盲区：

    1. **时间指代**：「一开始」「上次」不在文本里，就不在向量里；
    2. **实体串台**：两个项目的记录模板化程度一样，向量分不出是哪个；
    3. **否定与条件**：「预算是 8 万」和「预算不是 8 万」的向量几乎重合。

    应对办法不在嵌入模型上，在**元数据过滤 + 仲裁 + 重排**上——
    这也是为什么本模块把来源可信度写进得分，而不只是写进元数据。
    """

    def __init__(
        self,
        config: Any = None,
        *,
        clock: Callable[[], float] = time.time,
        dim: int = EMBED_DIM,
        chunk_chars: int = 120,
        overlap: int = 24,
    ) -> None:
        super().__init__(config, clock=clock)
        self.dim = dim
        self.chunk_chars = chunk_chars
        self.overlap = overlap
        self._vectors: dict[str, list[tuple[str, list[float]]]] = {}

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


# ---------------------------------------------------------------------------
# 路线二：实体图谱
# ---------------------------------------------------------------------------


class GraphMemory(InMemoryMemory):
    """实体图谱路线：实体抽取 → 关系边（带有效期）→ 按实体检索。

    和向量路线的分工：

    - 向量答「哪段话像这句话」；
    - 图谱答「**哪个事实现在成立**」。

    写新事实时，同主体同谓词的旧边被**失效**（补一个 ``valid_to``），
    而不是删掉——这样「现在的事实」和「以前的事实」能同时被回答。
    """

    def __init__(
        self,
        config: Any = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(config, clock=clock)
        self._edges: list[Relation] = []
        self._shadowed: dict[str, list[Relation]] = {}

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

    def _unindex(self, item: MemoryItem) -> None:
        self._edges = [e for e in self._edges if e.item_id != item.id]
        for old in self._shadowed.pop(item.id, []):
            old.valid_to = None

    def _on_supersede(self, previous: MemoryItem) -> None:
        """旧版本被顶掉时**不拆边**。

        旧边留着，由新写入去给它补失效时间——**边失效，而不是删边**。
        """
        return

    def _similarity(self, query: str, item: MemoryItem) -> float:
        entities = extract_entities(query)
        if not entities:
            # 实体未知就退回字面匹配——图谱的强项是实体已知的查询。
            return super()._similarity(query, item)
        best = 0.0
        for edge in self._edges:
            if edge.item_id != item.id:
                continue
            hits = sum(
                1 for e in entities
                if e.lower() in edge.subject.lower() or e.lower() in edge.object.lower()
            )
            if not hits:
                continue
            weight = 1.0 if edge.valid_to is None else 0.5
            best = max(best, (hits / len(entities)) * weight)
        return best

    def facts(self, query: str = "", *, at: float | None = None) -> list[Relation]:
        """当前成立的关系边。

        ``at`` 给定就是查那一刻的事实；不给就用当前时钟。
        """
        now = self._clock() if at is None else at
        entities = extract_entities(query) if query else []
        result: list[Relation] = []
        for edge in self._edges:
            if not edge.is_valid(now):
                continue
            if entities and not any(
                e.lower() in edge.subject.lower() or e.lower() in edge.object.lower()
                for e in entities
            ):
                continue
            result.append(edge)
        return result

    def edges(self) -> list[Relation]:
        """全部关系边（含已失效的）。"""
        return list(self._edges)


# ---------------------------------------------------------------------------
# 记忆评测
# ---------------------------------------------------------------------------


def evaluate_memory(
    cases: Iterable[MemoryCase],
    memory_factory: Callable[[MemoryCase], Memory],
) -> MemoryEvalReport:
    """跑一遍记忆评测。

    这不是「检索命中率」测试。命中率只问「索引能不能捞到东西」，
    这里问的是三件事：

    1. **该想起时想起了**——``expect`` 里的事实出现在 top-k 里；
    2. **不该想起时没乱想**——``forbid`` 里的内容没出现；
    3. **脏数据没进库**——被拒绝的写入压根不该被召回（``expect_empty``）。

    判定全部是关键字与规则，**不调 LLM**——保证确定性与离线可跑。
    ``memory_factory`` 接收当前用例，因为不同用例可能要走不同检索路线。
    """
    case_list = list(cases)
    total = len(case_list)
    recalled = 0
    false_recall = 0
    returned = 0
    relevant = 0
    missed: list[str] = []
    polluted: list[str] = []
    details: list[dict] = []

    for case in case_list:
        memory = memory_factory(case)
        for setup in case.setup:
            try:
                memory.write(setup.kind, setup.text, setup.source, setup.ttl)
            except MemoryWriteRejected:
                continue
        hits = memory.recall(case.query, k=case.k)
        blob = "\n".join(h.text for h in hits)

        if case.expect_empty:
            ok = not hits
        elif case.expect:
            ok = any(e in blob for e in case.expect)
        else:
            ok = bool(hits)

        bad = [f for f in case.forbid if f in blob]
        if ok:
            recalled += 1
        else:
            missed.append(case.id)
        if bad:
            false_recall += 1
            polluted.append(case.id)

        returned += len(hits)
        relevant += sum(
            1 for h in hits
            if not case.expect or any(e in h.text for e in case.expect)
        )
        details.append({
            "id": case.id,
            "query": case.query,
            "pass": ok,
            "returned": [h.text for h in hits],
            "missing": [e for e in case.expect if e not in blob],
            "forbidden_hit": bad,
        })

    return MemoryEvalReport(
        total=total,
        recalled=recalled,
        false_recall=false_recall,
        precision=(relevant / returned) if returned else 0.0,
        recall_rate=(recalled / total) if total else 0.0,
        missed=missed,
        polluted=polluted,
        details=details,
    )


def default_memory_factory(case: MemoryCase) -> Memory:
    """按用例指定的路线造一个记忆实例。"""
    return GraphMemory() if case.route == "graph" else VectorMemory()


def default_memory_cases() -> list[MemoryCase]:
    """默认评测集：8 条，覆盖四类记忆、三种来源、版本覆盖、TTL 与拒写。

    这份集合本身就是「该想起时想起了」的最小定义——
    改记忆模块之后先跑它，比盯着检索命中率靠谱。
    """
    return [
        MemoryCase(
            id="semantic-user-budget",
            setup=[MemorySetup("semantic", "Orion 项目预算是 12 万元", "user")],
            query="Orion 预算是多少",
            expect=["12 万元"],
        ),
        MemoryCase(
            id="entity-isolation",
            setup=[
                MemorySetup("semantic", "Orion 项目预算是 12 万元", "user"),
                MemorySetup("semantic", "Argus 项目预算是 5 万元", "user"),
            ],
            query="Orion 预算是多少",
            expect=["12 万元"],
            forbid=["5 万元"],
            route="graph",
        ),
        MemoryCase(
            id="tool-source-fact",
            setup=[MemorySetup("semantic", "运维预算上限是 30 万", "tool")],
            query="运维预算上限是多少",
            expect=["30 万"],
        ),
        MemoryCase(
            id="model-semantic-rejected",
            setup=[MemorySetup("semantic", "模型编造的用户偏好", "model")],
            query="用户偏好",
            expect_empty=True,
            forbid=["模型编造的用户偏好"],
        ),
        MemoryCase(
            id="episodic-event",
            setup=[MemorySetup("episodic", "3 月批的预算是 8 万元", "user")],
            query="3 月批的预算是多少",
            expect=["8 万元"],
        ),
        MemoryCase(
            id="working-note",
            setup=[MemorySetup("working", "下一步要查 2026 预算表", "model")],
            query="接下来要查什么",
            expect=["查 2026 预算表"],
        ),
        MemoryCase(
            id="version-supersede",
            setup=[
                MemorySetup("semantic", "Orion 项目预算是 8 万元", "user"),
                MemorySetup("semantic", "Orion 项目预算是 12 万元", "user"),
            ],
            query="Orion 预算是多少",
            expect=["12 万元"],
            forbid=["8 万元"],
        ),
        MemoryCase(
            id="tool-lower-trust-cannot-override",
            setup=[
                MemorySetup("semantic", "Orion 项目预算是 12 万元", "user"),
                MemorySetup("semantic", "Orion 项目预算是 99 万元", "tool"),
            ],
            query="Orion 预算是多少",
            expect=["12 万元"],
            forbid=["99 万元"],
        ),
    ]
