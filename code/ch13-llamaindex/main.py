#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch13 数据驱动的 Agent 最小可实现 —— 纯标准库，无需 API Key 就能跑通完整回路。

这个文件用标准库复刻 LlamaIndex 的两条主线：

  1) 数据层：文档 -> 切块 -> 索引（向量 / BM25）-> 融合检索 -> 查询
  2) 编排层：事件驱动工作流（Workflows），步进式执行 + 多 Agent 交接

三个演示场景：

  coupling  耦合形态对比：检索作为「内置能力」vs 检索作为「工具」
  workflow  事件驱动工作流：两路检索并行 -> collect_events 汇聚 -> 合成
  agents    多 Agent：路由 Agent 分诊 -> 专家 Agent 各自检索作答

用法：
    python main.py                    # 跑全部场景
    python main.py --demo coupling    # 只跑耦合形态对比
    python main.py --demo workflow    # 只跑事件驱动工作流
    python main.py --demo agents      # 只跑多 Agent
    python main.py --trace            # 打印完整事件流
    python main.py --live             # 切真实模型 API

--live 所需环境变量：
    LLM_API_KEY    必填，模型服务商的 API Key
    LLM_BASE_URL   可选，默认 https://api.openai.com/v1
    LLM_MODEL      可选，默认 gpt-4o-mini
端点需兼容 `POST {base}/chat/completions`：body 含 model/messages，
返回 choices[0].message.content。换供应商只改 base_url 与 model。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文结果乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ==========================================================================
# 1. 数据层：文档 -> 节点 -> 索引 -> 检索
# ==========================================================================


@dataclass
class Document:
    """原始文档。LlamaIndex 里这一层对应各种 Reader / 文档加载器。"""

    doc_id: str
    title: str
    text: str
    domain: str = "general"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Node:
    """切块之后的检索单元。索引里存的是它，不是原始文档。"""

    node_id: str
    doc_id: str
    title: str
    text: str
    domain: str = "general"


def tokens_of(text: str) -> List[str]:
    """分词：英文按词，中文按二元组。

    中文单字噪声太大（"的""是"到处都是），二元组在短文本检索里够用。
    单个字符的英文/数字直接丢掉——"1""2"这类 token 会让不相干的文档
    莫名其妙地"命中"，把检索结果搅乱。
    """
    text = text.lower()
    out = [t for t in re.findall(r"[a-z0-9_]+", text) if len(t) > 1]
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    out.extend(cjk[i : i + 2] for i in range(len(cjk) - 1))
    return out


def chunk_document(doc: Document, chunk_size: int = 90, overlap: int = 25) -> List[Node]:
    """把文档切成带重叠的块。

    重叠是为了避免答案正好被切在块边界上——这是最常见的"明明库里有却检索不到"。
    能在标点处断开就断开，读起来更完整。
    """
    nodes: List[Node] = []
    text = doc.text
    start = 0
    idx = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            cut = max(
                text.rfind("。", start, end),
                text.rfind("；", start, end),
                text.rfind("，", start, end),
            )
            if cut > start + chunk_size // 2:
                end = cut + 1
        nodes.append(
            Node(
                node_id="%s#%d" % (doc.doc_id, idx),
                doc_id=doc.doc_id,
                title=doc.title,
                text=text[start:end].strip(),
                domain=doc.domain,
            )
        )
        idx += 1
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return nodes


class MockEmbedder:
    """确定性的假 embedding：把 token 哈希进固定维度的向量。

    用 hashlib 而不是内置 hash()——内置 hash 对字符串是随机化的，
    跨进程结果会变，检索就不可复现了。
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        toks = tokens_of(text)
        for t in toks:
            h = hashlib.md5(t.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if h[4] % 2 == 0 else -1.0
            vec[idx] += sign * (1.0 + math.log(1 + len(t)))
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class VectorIndex:
    """向量索引：给每个节点算一个向量，按余弦相似度取 top-k。"""

    def __init__(self, nodes: List[Node], embedder: Optional[MockEmbedder] = None) -> None:
        self.nodes = nodes
        self.embedder = embedder or MockEmbedder()
        self.vectors = [self.embedder.embed(n.text) for n in nodes]

    def search(self, query: str, top_k: int = 3) -> List[Tuple[Node, float]]:
        qv = self.embedder.embed(query)
        scored = [(self.nodes[i], cosine(qv, self.vectors[i])) for i in range(len(self.nodes))]
        scored.sort(key=lambda x: -x[1])
        return [(n, s) for n, s in scored[:top_k] if s > 0]


class BM25Index:
    """关键词索引：BM25 打分。"""

    def __init__(self, nodes: List[Node], k1: float = 1.5, b: float = 0.75) -> None:
        self.nodes = nodes
        self.k1 = k1
        self.b = b
        self.docs = [tokens_of(n.text) for n in nodes]
        self.avgdl = (sum(len(d) for d in self.docs) / len(self.docs)) if self.docs else 0.0
        df: Counter = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        self.idf = {
            t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()
        }
        self.vocab = set(self.idf)

    def search(self, query: str, top_k: int = 3) -> List[Tuple[Node, float]]:
        q = tokens_of(query)
        scored: List[Tuple[float, int]] = []
        for i, d in enumerate(self.docs):
            tf = Counter(d)
            s = 0.0
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * (len(d) / (self.avgdl or 1)))
                s += self.idf.get(t, 0.0) * (f * (self.k1 + 1)) / denom
            scored.append((s, i))
        scored.sort(key=lambda x: -x[0])
        return [(self.nodes[i], s) for s, i in scored[:top_k] if s > 0]

    def overlap(self, query: str) -> int:
        """查询里有几个词落进了词表——用来判断"这个问题库里有没有答案"。"""
        return len({t for t in tokens_of(query) if t in self.vocab})


class FusionRetriever:
    """混合检索：向量与 BM25 各排一版，用 RRF（倒数排名融合）合并。

    RRF 不看原始分数，只看名次，所以两种打分尺度不一致也能合。
    """

    def __init__(self, retrievers: Dict[str, Any], k: int = 60) -> None:
        self.retrievers = retrievers
        self.k = k

    def retrieve(self, query: str, top_k: int = 3) -> List[Node]:
        per: Dict[str, List[Node]] = {}
        for name, r in self.retrievers.items():
            per[name] = [n for n, _ in r.search(query, top_k=max(top_k, 5))]
        scores: Dict[str, float] = defaultdict(float)
        node_of: Dict[str, Node] = {}
        for name, ranked in per.items():
            for rank, node in enumerate(ranked):
                scores[node.node_id] += 1.0 / (self.k + rank + 1)
                node_of[node.node_id] = node
        order = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        return [node_of[nid] for nid, _ in order]


def est_tokens(text: str) -> int:
    """粗略 token 估算：中文按字，英文按 4 字符 1 token。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 4


# ==========================================================================
# 2. 语料：一份内部知识库
# ==========================================================================

CORPUS: List[Document] = [
    Document(
        "runbook-pool",
        "连接池配置",
        "订单服务的数据库连接池上限是 200，单连接超时 3 秒。连接池打满时请求会在获取连接阶段排队，"
        "表现为 p99 延迟从 80ms 涨到 1800ms，而应用 CPU 并没有升高。排查时先看连接池的 active "
        "和 pending 两个指标，pending 持续大于 0 说明是数据库侧慢，而不是连接池配置太小。"
        "临时扩容连接池只能撑 10 分钟，根因通常是慢 SQL 持锁。",
        domain="runbook",
    ),
    Document(
        "runbook-slowsql",
        "慢查询排查",
        "慢查询排查三步：开慢查询日志、确认执行计划、检查索引选择性。订单表按 create_time 分区"
        "但没有按 tenant_id 建索引，导致跨租户查询全表扫描。执行计划里 type=ALL、扫描行数超过 "
        "20 万时，先补索引再谈优化 SQL 写法。",
        domain="runbook",
    ),
    Document(
        "runbook-deploy",
        "发布与回滚",
        "灰度发布分三批：1%、10%、100%，每批观察 15 分钟。发布窗口为周二和周四 14:00 到 17:00，"
        "周五不发布。回滚触发条件：错误率超过基线 3 倍持续 5 分钟，或 p99 延迟超过 1200ms。"
        "回滚命令是 deploy rollback --to last-good，操作人需要在发布单上留下记录。",
        domain="runbook",
    ),
    Document(
        "runbook-alert",
        "告警分级",
        "告警分三级：P0 影响交易主链路，5 分钟内响应；P1 影响部分功能，30 分钟内响应；"
        "P2 无交易影响，次日处理。值班表每周一换一次，交接时必须确认 P0 告警通道电话能打通，"
        "打不通就走备用通道并立刻上报。",
        domain="runbook",
    ),
    Document(
        "runbook-kafka",
        "消息队列积压",
        "消费积压超过 10 万条时，先确认下游依赖是否正常，再看消费者是否在频繁重试。毒消息会让"
        "同一个分区反复重放，临时做法是把该分区消息转存后跳过，处理完再回灌，并补上幂等校验。",
        domain="runbook",
    ),
    Document(
        "runbook-hotkey",
        "缓存热 key",
        "热 key 指单个 key 的 QPS 超过 1 万。订单详情页的 sku:{id} 是典型热 key，促销时单 key "
        "QPS 能到 8 万。解决方案是本地缓存加随机过期时间，再不行就做 key 打散，把 sku:{id} "
        "拆成 sku:{id}:{0..9} 十个副本。",
        domain="runbook",
    ),
    Document(
        "runbook-errcode",
        "网关错误码",
        "网关错误码对照：40001 参数校验失败，40003 签名过期，50001 下游超时，50002 限流触发，"
        "50301 服务降级中。客户端遇到 50001 默认重试两次，收到 50301 不重试，直接读缓存里的"
        "降级数据。",
        domain="runbook",
    ),
    Document(
        "runbook-limit",
        "限流与降级",
        "限流按租户维度配置，默认单租户 1000 QPS，超限返回 50302。降级开关按依赖逐级打开："
        "先关推荐，再关评价，最后关积分。降级期间对外公告必须写明影响范围和预计恢复时间。",
        domain="runbook",
    ),
    Document(
        "policy-expense",
        "差旅报销",
        "差旅报销需在行程结束后 30 天内提交，超过 60 天不予受理。单张发票金额超过 1000 元需要"
        "附行程单，住宿费每人每晚上限 600 元，超出部分由本人承担，特殊情况需部门总监书面批准。",
        domain="policy",
    ),
    Document(
        "policy-leave",
        "请假制度",
        "年假按入职周年计算，满 1 年 5 天，满 3 年 10 天，满 5 年 15 天。病假需要三级以上"
        "医院证明，全年累计超过 30 天的需部门总监批准。事假不计入年假额度。",
        domain="policy",
    ),
    Document(
        "policy-vpn",
        "VPN 与权限",
        "VPN 账号由 IT 统一发放，有效期一年，续期需重新提交申请并通过安全培训考试。生产数据库"
        "只读账号申请后 48 小时开通，写权限需双人审批，且每次使用都要走审计日志。",
        domain="policy",
    ),
    Document(
        "policy-release",
        "发布规范",
        "未经审批的发布一律回滚。发布前必须完成单元测试与集成测试，回归用例通过率低于 95% "
        "不允许发布。发布单必须写明影响范围和回滚方案，缺一项就不放行。",
        domain="policy",
    ),
]


def build_indexes(
    docs: Sequence[Document], embedder: Optional[MockEmbedder] = None
) -> Tuple[List[Node], VectorIndex, BM25Index, FusionRetriever]:
    nodes: List[Node] = []
    for d in docs:
        nodes.extend(chunk_document(d))
    vector = VectorIndex(nodes, embedder)
    bm25 = BM25Index(nodes)
    fusion = FusionRetriever({"vector": vector, "bm25": bm25})
    return nodes, vector, bm25, fusion


# ==========================================================================
# 3. 模型层：mock 与 live
# ==========================================================================


class LLMBase:
    """Agent 只需要模型做四件事，接口就这四招。"""

    def classify(self, question: str) -> str:
        """路由：把问题分到一个领域标签。"""
        raise NotImplementedError

    def decide(self, question: str, history: List[dict], overlap: Optional[int] = None) -> dict:
        """决定下一步：调用检索工具，还是直接作答。

        overlap 是"这个问题在库里能命中多少词"的提示，由调用方用自己那份
        索引算好——不同专家看的索引不同，命中情况自然不一样。

        返回 {"action": "search", "query": ...} 或 {"action": "answer", "answer": ...}
        """
        raise NotImplementedError

    def synthesize(self, question: str, snippets: List[Node]) -> str:
        """根据检索到的片段作答。"""
        raise NotImplementedError

    def answer_plain(self, question: str) -> str:
        """不检索，直接作答。"""
        raise NotImplementedError


class MockLLM(LLMBase):
    """离线 mock：按规则作答。

    规则很朴素，但能跑出真实的结构性差异：
      - 需要查库的题��� -> 走检索
      - 库里没有的题   -> 直接作答
      - 检索片段与问题无交集 -> 明说"无关"，不硬答
    """

    RUNBOOK_WORDS = [
        "连接池", "慢查询", "索引", "发布", "回滚", "灰度", "告警", "积压",
        "缓存", "热key", "错误码", "限流", "降级", "延迟", "超时", "连接", "部署",
    ]
    POLICY_WORDS = [
        "报销", "差旅", "住宿", "发票", "请假", "年假", "病假", "VPN",
        "权限", "申请", "审批", "规范", "制度", "试用期", "转正",
    ]

    def __init__(self, bm25: Optional[BM25Index] = None) -> None:
        self.bm25 = bm25

    def classify(self, question: str) -> str:
        r = sum(1 for w in self.RUNBOOK_WORDS if w in question)
        p = sum(1 for w in self.POLICY_WORDS if w in question)
        if r == 0 and p == 0:
            return "general"
        return "runbook" if r >= p else "policy"

    def decide(self, question: str, history: List[dict], overlap: Optional[int] = None) -> dict:
        already = any(h.get("action") == "search" for h in history)
        if overlap is None:
            overlap = self.bm25.overlap(question) if self.bm25 is not None else 0
        if not already and overlap >= 2:
            return {"action": "search", "query": question}
        nodes = self._from_history(history)
        if nodes:
            return {"action": "answer", "answer": self.synthesize(question, nodes)}
        # 没查过库：模型用自己的知识直接答，不要硬套检索结果
        return {"action": "answer", "answer": self.answer_plain(question)}

    @staticmethod
    def _from_history(history: List[dict]) -> List[Node]:
        for h in reversed(history):
            if h.get("action") == "search" and h.get("nodes"):
                return h["nodes"]
        return []

    def synthesize(self, question: str, snippets: List[Node]) -> str:
        if not snippets:
            return "没有检索到任何上下文，无法作答。"
        qtok = set(tokens_of(question))
        best, best_score = None, -1.0
        for node in snippets:
            score = len(qtok & set(tokens_of(node.text)))
            if score > best_score:
                best, best_score = node, score
        if best is None or best_score == 0:
            # 上下文与问题一个词都不重合——明说无关，不硬答
            return "检索到的 %d 条上下文与问题没有共同关键词，无法据此作答。" % len(snippets)
        sentence = self._best_sentence(question, best.text)
        return "依据《%s》：%s" % (best.title, sentence)

    @staticmethod
    def _best_sentence(question: str, text: str) -> str:
        qtok = set(tokens_of(question))
        parts = [s for s in re.split(r"[。；]", text) if s.strip()]
        if not parts:
            return text.strip()
        best, score = parts[0], -1
        for s in parts:
            sc = len(qtok & set(tokens_of(s)))
            if sc > score:
                best, score = s, sc
        return best.strip().rstrip("，") + "。"

    def answer_plain(self, question: str) -> str:
        m = re.search(r"(\d+)\s*(加|减|乘|除)\s*(\d+)", question)
        if m:
            a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
            val = {"加": a + b, "减": a - b, "乘": a * b, "除": (a // b if b else 0)}[op]
            return "%d" % val
        return "这个问题不需要查知识库，直接作答即可。"


class LiveLLM(LLMBase):
    """真实模型：走 OpenAI 兼容的 chat completions 端点。"""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _chat(self, prompt: str, system: str = "") -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self.model, "messages": messages, "temperature": 0.0}
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_json(text: str) -> Any:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[:-3]
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        return json.loads(text)

    def classify(self, question: str) -> str:
        raw = self._chat(
            "把下面的问题归到一个领域，只返回 JSON："
            '{"domain":"runbook|policy|general"}\n'
            "runbook=技术运维手册类，policy=公司制度类，general=与内部知识库无关。\n\n问题：%s" % question
        )
        data = self._parse_json(raw)
        domain = str(data.get("domain", "general"))
        return domain if domain in ("runbook", "policy", "general") else "general"

    def decide(self, question: str, history: List[dict], overlap: Optional[int] = None) -> dict:
        raw = self._chat(
            "你可以使用一个知识库检索工具 search。判断是否需要检索。\n"
            "只返回 JSON：{\"action\":\"search\",\"query\":\"检索词\"} "
            "或 {\"action\":\"answer\",\"answer\":\"最终回答\"}\n"
            "需要查内部知识库就调 search，不需要的就直接答。\n\n问题：%s" % question
        )
        data = self._parse_json(raw)
        action = str(data.get("action", "answer"))
        if action == "search":
            return {"action": "search", "query": str(data.get("query") or question)}
        return {"action": "answer", "answer": str(data.get("answer") or "")}

    def synthesize(self, question: str, snippets: List[Node]) -> str:
        if not snippets:
            return "没有检索到任何上下文，无法作答。"
        ctx = "\n".join("- 《%s》%s" % (n.title, n.text) for n in snippets)
        return self._chat(
            "只根据下面的上下文回答问题，引用来源；上下文与问题无关就直说无关。\n\n"
            "问题：%s\n上下文：\n%s" % (question, ctx)
        ).strip()

    def answer_plain(self, question: str) -> str:
        return self._chat("直接回答这个问题，不需要检索知识库。\n\n问题：%s" % question).strip()


def build_llm(live: bool, args, bm25: Optional[BM25Index]) -> LLMBase:
    if not live:
        return MockLLM(bm25)
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        print("错误：--live 需要环境变量 LLM_API_KEY", file=sys.stderr)
        raise SystemExit(2)
    base_url = args.base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model = args.model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    return LiveLLM(api_key, base_url, model)


# ==========================================================================
# 4. 耦合形态：检索作为工具 vs 检索作为内置能力
# ==========================================================================


@dataclass
class Answer:
    question: str
    text: str
    searches: int
    context_tokens: int
    context_count: int
    source: str  # "retrieval" / "model"


def run_builtin_retrieval(llm: LLMBase, fusion: FusionRetriever, question: str, top_k: int = 3) -> Answer:
    """检索作为内置能力：先查库，把片段拼进上下文，模型只能看着它答。

    特点是「一定会查」——不管问题问的是什么。
    """
    nodes = fusion.retrieve(question, top_k=top_k)
    ctx = "\n".join("《%s》%s" % (n.title, n.text) for n in nodes)
    text = llm.synthesize(question, nodes)
    return Answer(
        question=question,
        text=text,
        searches=1,
        context_tokens=est_tokens(ctx),
        context_count=len(nodes),
        source="retrieval",
    )


def run_tool_retrieval(llm: LLMBase, fusion: FusionRetriever, question: str, top_k: int = 3) -> Answer:
    """检索作为工具：模型自己决定要不要查、查什么。

    特点是「按需查」——不查也是合法路径。
    """
    history: List[dict] = []
    searches = 0
    nodes: List[Node] = []
    for _ in range(3):  # 最多三轮，和第 11 章的步数上限是一个意思
        decision = llm.decide(question, history)
        if decision.get("action") == "search":
            searches += 1
            query = decision.get("query") or question
            nodes = fusion.retrieve(query, top_k=top_k)
            history.append({"action": "search", "query": query, "nodes": nodes})
            continue
        answer_text = decision.get("answer") or ""
        if not answer_text:
            answer_text = llm.synthesize(question, nodes)
        ctx = "\n".join("《%s》%s" % (n.title, n.text) for n in nodes)
        return Answer(
            question=question,
            text=answer_text,
            searches=searches,
            context_tokens=est_tokens(ctx) if nodes else 0,
            context_count=len(nodes),
            source="retrieval" if nodes else "model",
        )
    ctx = "\n".join("《%s》%s" % (n.title, n.text) for n in nodes)
    return Answer(
        question=question,
        text=llm.synthesize(question, nodes),
        searches=searches,
        context_tokens=est_tokens(ctx),
        context_count=len(nodes),
        source="retrieval",
    )


# ==========================================================================
# 5. 事件驱动工作流（Workflows）
# ==========================================================================


@dataclass(eq=False)
class Event:
    """工作流里流转的最小单位。事件是类型化的，这是它和「共享状态」最大的区别。"""

    type: str = "event"


@dataclass(eq=False)
class StartEvent(Event):
    question: str = ""
    type: str = "start"


@dataclass(eq=False)
class RetrieveEvent(Event):
    query: str = ""
    source: str = ""
    type: str = "retrieve"


@dataclass(eq=False)
class RetrievedEvent(Event):
    source: str = ""
    nodes: List[Node] = field(default_factory=list)
    type: str = "retrieved"


@dataclass(eq=False)
class CollectEvent(Event):
    question: str = ""
    type: str = "collect"


@dataclass(eq=False)
class SynthesizeEvent(Event):
    question: str = ""
    snippets: List[Node] = field(default_factory=list)
    type: str = "synthesize"


@dataclass(eq=False)
class HandoffEvent(Event):
    target: str = ""
    question: str = ""
    type: str = "handoff"


@dataclass(eq=False)
class AgentThinkEvent(Event):
    agent: str = ""
    question: str = ""
    history: List[dict] = field(default_factory=list)
    round: int = 0
    type: str = "agent_think"


@dataclass(eq=False)
class AgentToolEvent(Event):
    agent: str = ""
    query: str = ""
    question: str = ""
    history: List[dict] = field(default_factory=list)
    round: int = 0
    type: str = "agent_tool"


@dataclass(eq=False)
class AgentObserveEvent(Event):
    agent: str = ""
    nodes: List[Node] = field(default_factory=list)
    question: str = ""
    history: List[dict] = field(default_factory=list)
    round: int = 0
    type: str = "agent_observe"


@dataclass(eq=False)
class AgentDoneEvent(Event):
    agent: str = ""
    answer: str = ""
    type: str = "agent_done"


@dataclass(eq=False)
class StopEvent(Event):
    result: Any = None
    type: str = "stop"


class WorkflowContext:
    """步骤处理器拿到的上下文：能发事件，也能等事件。"""

    def __init__(self, runner: "WorkflowRunner") -> None:
        self.runner = runner
        self.sent: List[Event] = []

    def send_event(self, evt: Event) -> None:
        """发出一个事件，触发后续步骤。"""
        self.sent.append(evt)

    async def collect_events(self, types: Sequence[str]) -> List[Event]:
        """等待若干类型的事件到齐，返回它们。

        这是事件驱动模型里唯一的"汇合点"——并行分支靠它对齐，
        不需要共享一个可变状态对象。
        """
        return await self.runner.collect_events(types)


class Workflow:
    """工作流：按事件类型注册步骤。"""

    def __init__(self) -> None:
        self.handlers: Dict[str, Any] = {}

    def step(self, *event_types):
        """把一个函数注册成某些事件类型的处理器。

        事件类型可以传字符串，也可以传事件类（LlamaIndex 就是传类），
        这里统一折算成 type 字符串做 key。
        """

        def deco(fn):
            for t in event_types:
                key = getattr(t, "type", t) if isinstance(t, type) else t
                self.handlers[key] = fn
            return fn

        return deco


class WorkflowRunner:
    """极简事件循环：取出事件 -> 交给对应步骤 -> 把新发出的事件放回队列。

    步骤之间不共享状态，只通过事件通信。这是事件驱动与"共享上下文"
    最实际的差别：加一个分支不用改别人的代码。
    """

    def __init__(self, workflow: Workflow, timeout: float = 5.0, max_steps: int = 200) -> None:
        self.workflow = workflow
        self.timeout = timeout
        self.max_steps = max_steps
        self.queue: List[Event] = []
        self.collected: Dict[str, List[Event]] = defaultdict(list)
        self.log: List[Event] = []
        self.result: Optional[StopEvent] = None
        self._cond = asyncio.Condition()
        self._errors: List[BaseException] = []

    async def _emit(self, evt: Event) -> None:
        self.log.append(evt)
        self.queue.append(evt)

    async def collect_events(self, types: Sequence[str]) -> List[Event]:
        """等若干类型的事件到齐。

        用 Condition 而不是裸 Event：Event 的「置位-清零」在多个等待者
        之间会丢唤醒信号，Condition 的「持锁检查条件 -> wait() 原子释放锁」
        才不会漏事件。
        """
        async with self._cond:
            while True:
                got: List[Event] = []
                missing: List[str] = []
                for t in types:
                    bucket = self.collected.get(t)
                    if bucket:
                        got.append(bucket.pop(0))
                    else:
                        missing.append(t)
                if not missing:
                    return got
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=self.timeout)
                except asyncio.TimeoutError:
                    raise TimeoutError("等待事件超时: %s" % missing)

    async def _dispatch(self, evt: Event) -> None:
        handler = self.workflow.handlers.get(evt.type)
        ctx = WorkflowContext(self)
        result = None
        try:
            if handler is not None:
                result = await handler(ctx, evt)
        except BaseException as exc:  # noqa: BLE001
            self._errors.append(exc)
            raise
        # 事件"到齐"：不管有没有处理器，都登记为可被 collect 到的。
        # 像 RetrievedEvent 这种纯载荷事件，本身就不需要步骤。
        async with self._cond:
            self.collected[evt.type].append(evt)
            self._cond.notify_all()
        for e in ctx.sent:
            await self._emit(e)
        if isinstance(result, StopEvent):
            self.result = result

    def _on_done(self, task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._errors.append(exc)

    async def run(self, start: Event) -> Optional[StopEvent]:
        await self._emit(start)
        tasks: set = set()
        steps = 0
        while True:
            if self._errors:
                raise self._errors[0]
            if self.queue:
                evt = self.queue.pop(0)
                steps += 1
                if steps > self.max_steps:
                    raise RuntimeError("事件步数超限，可能存在死循环")
                task = asyncio.create_task(self._dispatch(evt))
                tasks.add(task)
                task.add_done_callback(self._on_done)
                task.add_done_callback(tasks.discard)
                continue
            if not tasks:
                break
            done, pending = await asyncio.wait(set(tasks), return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if not t.cancelled() and t.exception() is not None:
                    raise t.exception()
            tasks = set(pending)
        return self.result


def build_fusion_workflow(llm: LLMBase, fusion: FusionRetriever) -> Workflow:
    """检索融合工作流：两路检索并行 -> 汇聚 -> 合成。"""

    wf = Workflow()

    @wf.step(StartEvent)
    async def start(ctx: WorkflowContext, ev: StartEvent):
        ctx.send_event(RetrieveEvent(query=ev.question, source="vector"))
        ctx.send_event(RetrieveEvent(query=ev.question, source="bm25"))
        ctx.send_event(CollectEvent(question=ev.question))

    @wf.step(RetrieveEvent)
    async def retrieve(ctx: WorkflowContext, ev: RetrieveEvent):
        nodes = fusion.retrieve(ev.query, top_k=3)
        ctx.send_event(RetrievedEvent(source=ev.source, nodes=nodes))

    @wf.step(CollectEvent)
    async def collect(ctx: WorkflowContext, ev: CollectEvent):
        got = await ctx.collect_events(["retrieved", "retrieved"])
        snippets: List[Node] = []
        for g in got:
            snippets.extend(g.nodes)
        seen, uniq = set(), []
        for n in snippets:
            if n.node_id not in seen:
                seen.add(n.node_id)
                uniq.append(n)
        ctx.send_event(SynthesizeEvent(question=ev.question, snippets=uniq[:4]))

    @wf.step(SynthesizeEvent)
    async def synthesize(ctx: WorkflowContext, ev: SynthesizeEvent):
        return StopEvent(result=llm.synthesize(ev.question, ev.snippets))

    return wf


# ==========================================================================
# 6. 多 Agent：路由 + 专家（AgentWorkflow 风格）
# ==========================================================================


class SpecialistAgent:
    """一个专家 Agent：自带一份索引，拿到问题后自己决定要不要检索。

    它对外只认事件：ThinkEvent 进，决定发 ToolEvent 或 DoneEvent。
    判断"要不要查"用的是**自己那份**索引的命中情况——
    运维手册的 Agent 看不到报销制度，查了也是白查。
    """

    def __init__(
        self,
        name: str,
        llm: LLMBase,
        fusion: Optional[FusionRetriever],
        bm25: Optional[BM25Index] = None,
        top_k: int = 2,
    ):
        self.name = name
        self.llm = llm
        self.fusion = fusion
        self.bm25 = bm25
        self.top_k = top_k

    def search(self, query: str) -> List[Node]:
        if self.fusion is None:
            return []
        return self.fusion.retrieve(query, top_k=self.top_k)

    def think(self, question: str, history: List[dict]) -> dict:
        """专家的"想"这一步：查，还是不查。"""
        already = any(h.get("action") == "search" for h in history)
        overlap = 0
        if not already and self.bm25 is not None:
            overlap = self.bm25.overlap(question)
        decision = self.llm.decide(question, history, overlap)
        if decision.get("action") == "search" and self.fusion is not None:
            return {"action": "search", "query": decision.get("query") or question}
        answer = decision.get("answer")
        if not answer:
            nodes = []
            for h in reversed(history):
                if h.get("action") == "search" and h.get("nodes"):
                    nodes = h["nodes"]
                    break
            answer = (
                self.llm.synthesize(question, nodes)
                if nodes
                else self.llm.answer_plain(question)
            )
        return {"action": "answer", "answer": answer}


def build_agent_workflow(llm: LLMBase, specialists: Dict[str, SpecialistAgent]) -> Workflow:
    """路由 Agent + 专家 Agent 的交接工作流。"""

    wf = Workflow()

    @wf.step(StartEvent)
    async def start(ctx: WorkflowContext, ev: StartEvent):
        domain = llm.classify(ev.question)
        ctx.send_event(HandoffEvent(target=domain, question=ev.question))

    @wf.step(HandoffEvent)
    async def handoff(ctx: WorkflowContext, ev: HandoffEvent):
        ctx.send_event(
            AgentThinkEvent(agent=ev.target, question=ev.question, history=[], round=0)
        )

    @wf.step(AgentThinkEvent)
    async def think(ctx: WorkflowContext, ev: AgentThinkEvent):
        agent = specialists.get(ev.agent)
        if agent is None:
            ctx.send_event(AgentDoneEvent(agent=ev.agent, answer="没有对应的专家 Agent"))
            return
        decision = agent.think(ev.question, ev.history)
        if decision.get("action") == "search":
            ctx.send_event(
                AgentToolEvent(
                    agent=ev.agent,
                    query=decision.get("query") or ev.question,
                    question=ev.question,
                    history=ev.history,
                    round=ev.round,
                )
            )
        else:
            ctx.send_event(AgentDoneEvent(agent=agent.name, answer=decision.get("answer", "")))

    @wf.step(AgentToolEvent)
    async def tool(ctx: WorkflowContext, ev: AgentToolEvent):
        agent = specialists.get(ev.agent)
        nodes = agent.search(ev.query) if agent else []
        history = list(ev.history) + [{"action": "search", "query": ev.query, "nodes": nodes}]
        ctx.send_event(
            AgentObserveEvent(
                agent=ev.agent,
                nodes=nodes,
                question=ev.question,
                history=history,
                round=ev.round + 1,
            )
        )

    @wf.step(AgentObserveEvent)
    async def observe(ctx: WorkflowContext, ev: AgentObserveEvent):
        ctx.send_event(
            AgentThinkEvent(
                agent=ev.agent,
                question=ev.question,
                history=ev.history,
                round=ev.round,
            )
        )

    @wf.step(AgentDoneEvent)
    async def done(ctx: WorkflowContext, ev: AgentDoneEvent):
        return StopEvent(result={"agent": ev.agent, "answer": ev.answer})

    return wf


def build_specialists(llm: LLMBase, docs: Sequence[Document]) -> Dict[str, SpecialistAgent]:
    """按领域切分知识库，每个领域一个专家 Agent。

    key 是领域标签（路由用的），name 是展示名。
    """
    out: Dict[str, SpecialistAgent] = {}
    for domain in ("runbook", "policy"):
        subset = [d for d in docs if d.domain == domain]
        _nodes, _v, bm25, fusion = build_indexes(subset)
        out[domain] = SpecialistAgent("%s_agent" % domain, llm, fusion, bm25)
    # general 专家不挂索引——它负责"库里没有"的问题
    out["general"] = SpecialistAgent("general_agent", llm, None, None)
    return out


# ==========================================================================
# 7. 演示
# ==========================================================================


QUESTIONS = [
    ("技术问题", "连接池打满导致 p99 延迟涨到 1800ms 怎么排查？"),
    ("制度问题", "差旅住宿费超标了还能报销吗？"),
    ("库外问题", "1 加 2 等于几？"),
]


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def show_answer_row(a: Answer) -> None:
    print("  问题       : %s" % a.question)
    print("  检索次数   : %d" % a.searches)
    print("  注入上下文 : %d 条 / 约 %d token" % (a.context_count, a.context_tokens))
    print("  回答来源   : %s" % ("检索结果" if a.source == "retrieval" else "模型自身"))
    print("  回答       : %s" % a.text)


def run_coupling(llm: LLMBase, fusion: FusionRetriever, trace: bool) -> None:
    hr("场景一：检索作为内置能力 vs 检索作为工具")
    rows = []
    for label, question in QUESTIONS:
        print("\n【%s】%s" % (label, question))
        print("\n-- 检索作为内置能力（一定会查） --")
        builtin = run_builtin_retrieval(llm, fusion, question)
        show_answer_row(builtin)
        print("\n-- 检索作为工具（模型自己决定） --")
        tool = run_tool_retrieval(llm, fusion, question)
        show_answer_row(tool)
        rows.append((label, builtin, tool))
        if trace:
            print()

    print("\n" + "-" * 78)
    print("对比汇总：")
    print("%-10s %-28s %-28s" % ("题型", "内置能力", "工具形态"))
    print("-" * 78)
    for label, b, t in rows:
        bs = "查 %d 次 / %d token / %s" % (b.searches, b.context_tokens, b.source)
        ts = "查 %d 次 / %d token / %s" % (t.searches, t.context_tokens, t.source)
        print("%-10s %-28s %-28s" % (label, bs, ts))
    print("-" * 78)
    print(
        "内置能力的代价是固定的：不管问题问什么，都要把 top-k 片段塞进上下文。\n"
        "工具形态的代价是可变的：库外的问题一次都不查，省下的就是那几百 token。\n"
        "反过来，内置形态在「模型自己忘了查」这件事上更稳——它没有忘记的选项。"
    )


def describe_event(evt: Event) -> str:
    if isinstance(evt, StartEvent):
        return "StartEvent(question=%s)" % evt.question
    if isinstance(evt, RetrieveEvent):
        return "RetrieveEvent(source=%s, query=%s)" % (evt.source, evt.query)
    if isinstance(evt, RetrievedEvent):
        return "RetrievedEvent(source=%s, %d 条)" % (evt.source, len(evt.nodes))
    if isinstance(evt, CollectEvent):
        return "CollectEvent(等待两路 RetrievedEvent)"
    if isinstance(evt, SynthesizeEvent):
        return "SynthesizeEvent(%d 条片段)" % len(evt.snippets)
    if isinstance(evt, HandoffEvent):
        return "HandoffEvent(target=%s)" % evt.target
    if isinstance(evt, AgentThinkEvent):
        return "AgentThinkEvent(agent=%s, round=%d)" % (evt.agent, evt.round)
    if isinstance(evt, AgentToolEvent):
        return "AgentToolEvent(agent=%s, query=%s)" % (evt.agent, evt.query)
    if isinstance(evt, AgentObserveEvent):
        return "AgentObserveEvent(agent=%s, %d 条)" % (evt.agent, len(evt.nodes))
    if isinstance(evt, AgentDoneEvent):
        return "AgentDoneEvent(agent=%s)" % evt.agent
    if isinstance(evt, StopEvent):
        return "StopEvent"
    return "%s" % evt.type


def show_event_log(log: Sequence[Event]) -> None:
    print("事件流（%d 个事件）：" % len(log))
    for i, evt in enumerate(log):
        print("  %2d. %s" % (i + 1, describe_event(evt)))


async def _run_workflow(llm: LLMBase, fusion: FusionRetriever, question: str) -> Tuple[str, list]:
    wf = build_fusion_workflow(llm, fusion)
    runner = WorkflowRunner(wf)
    stop = await runner.run(StartEvent(question=question))
    result = stop.result if stop else ""
    return result, runner.log


def run_workflow(llm: LLMBase, fusion: FusionRetriever, trace: bool) -> None:
    hr("场景二：事件驱动工作流（Workflows）")
    question = QUESTIONS[0][1]
    result, log = asyncio.run(_run_workflow(llm, fusion, question))
    print("问题：%s" % question)
    print()
    show_event_log(log)
    print("\n最终回答：%s" % result)
    print(
        "\n看点：\n"
        "  1) 两个 RetrieveEvent 是同时发出去的，两路检索并行跑，谁先回来谁先进队列；\n"
        "  2) CollectEvent 用 collect_events 等两路 RetrievedEvent 到齐——这是唯一的汇合点；\n"
        "  3) 每一步的输入输出都是事件，所以整条链路就是一份可回放的事件日志。"
    )


async def _run_agents(llm: LLMBase, specialists: Dict[str, SpecialistAgent], question: str) -> Tuple[Any, list]:
    wf = build_agent_workflow(llm, specialists)
    runner = WorkflowRunner(wf)
    stop = await runner.run(StartEvent(question=question))
    return (stop.result if stop else None), runner.log


def run_agents(llm: LLMBase, specialists: Dict[str, SpecialistAgent], trace: bool) -> None:
    hr("场景三：多 Agent —— 路由 + 专家交接")
    for label, question in QUESTIONS:
        result, log = asyncio.run(_run_agents(llm, specialists, question))
        print("\n【%s】%s" % (label, question))
        show_event_log(log)
        if isinstance(result, dict):
            print("  接手 Agent : %s" % result.get("agent"))
            print("  最终回答   : %s" % result.get("answer"))
        print()
    print(
        "看点：\n"
        "  1) 路由 Agent 只做一件事——判断问题该给谁；\n"
        "  2) 交接靠 HandoffEvent，专家之间不直接调用；\n"
        "  3) 专家内部的「想 -> 调工具 -> 看结果 -> 再想」也是事件，不是循环里的共享变量；\n"
        "  4) 库外问题路由到 general_agent，它没有索引，直接答。"
    )


# ==========================================================================
# 8. CLI
# ==========================================================================


def main() -> int:
    ap = argparse.ArgumentParser(description="ch13 数据驱动的 Agent 最小可实现")
    ap.add_argument("--demo", choices=["all", "coupling", "workflow", "agents"], default="all")
    ap.add_argument("--trace", action="store_true", help="打印完整事件流")
    ap.add_argument("--live", action="store_true", help="切真实模型 API")
    ap.add_argument("--model", default=None, help="覆盖 LLM_MODEL")
    ap.add_argument("--base-url", default=None, help="覆盖 LLM_BASE_URL")
    args = ap.parse_args()

    nodes, vector, bm25, fusion = build_indexes(CORPUS)
    llm = build_llm(args.live, args, bm25)

    print("知识库：%d 篇文档 -> 切块 %d 个节点" % (len(CORPUS), len(nodes)))
    print("索引   ：向量 %d 维（哈希 embedding）、BM25、RRF 融合" % vector.embedder.dim)

    if args.demo in ("all", "coupling"):
        run_coupling(llm, fusion, args.trace)
    if args.demo in ("all", "workflow"):
        run_workflow(llm, fusion, args.trace)
    if args.demo in ("all", "agents"):
        run_agents(llm, build_specialists(llm, CORPUS), args.trace)

    hr("跑完了")
    print("这个实现里有四个可以抄走的结论：")
    print("  1) 数据层和 Agent 层是分开的：文档 -> 切块 -> 索引 -> 检索，换检索方式不用动 Agent；")
    print("  2) 检索作为工具和作为内置能力，差别不在效果而在成本和自由度；")
    print("  3) 事件驱动的工作流里，汇合靠 collect_events，不靠共享状态；")
    print("  4) 多 Agent 的交接就是一个事件——加专家不用改别人的代码。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
