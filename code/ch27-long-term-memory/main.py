#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch27-long-term-memory / main.py
向量 / 摘要 / 实体图谱 —— 三条长期记忆后端路线的最小可实现

运行:
    python main.py                                  # mock 模式, 无需 API Key
    python main.py --live                           # 真实 LLM 做事实抽取 (OpenAI 兼容接口)
    python main.py --query "项目预算是多少？"          # 对同一问题跑三条路线

依赖: Python 3.9+, 仅标准库, 免安装。
--live 环境变量:
    OPENAI_API_KEY   必需
    OPENAI_BASE_URL  可选, 默认 https://api.openai.com/v1
    OPENAI_MODEL     可选, 默认 gpt-4o-mini

设计说明:
  * 三条后端实现同一个 MemoryBackend 协议(ingest / search / describe),
    便于横向替换; 真实项目里向量库和图数据库都是外部服务, 这里用纯内存替身。
  * mock 的 embedding 是确定性哈希投影, 不是语义模型 —— 它恰好复刻了
    向量路线最要命的问题: 只能按字面/词形相似度排序, 不理解"哪个更新了哪个"。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple

DIM = 1024

# 查询词 -> 语义谓词 的别名表(图谱查询用)
PRED_ALIASES: Dict[str, List[str]] = {
    "预算": ["预算", "额度", "金额", "批了", "多少钱", "费用", "花了"],
    "上线时间": ["上线", "交付", "截止", "什么时候"],
    "故障码": ["错误码", "报错", "故障", "异常", "什么码"],
}

# 时间意图关键词
EARLIEST_MARKERS = ["一开始", "最初", "最初定", "起初", "原来", "第一次", "最初报"]
CURRENT_MARKERS = ["现在", "目前", "最新", "当前", "改成", "调成"]


# --------------------------------------------------------------------- 工具

def ts(day: str) -> float:
    """'2026-03-02' -> epoch 秒"""
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def fmt(t: Optional[float]) -> str:
    if t is None:
        return "∞"
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d")


def tokens(text: str) -> List[str]:
    """中文按 2-gram, 拉丁/数字按词。"""
    text = (text or "").lower()
    out: List[str] = []
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) == 1:
            out.append(seg)
        else:
            out += [seg[i:i + 2] for i in range(len(seg) - 1)]
    out += re.findall(r"[a-z0-9_.]+", text)
    return out


def embed(text: str, dim: int = DIM) -> List[float]:
    """确定性哈希 embedding(feature hashing + L2 归一化)。
    生产环境请换成真正的 embedding 模型; 这里用它复刻"只看相似度"的盲区。"""
    vec = [0.0] * dim
    counts: Dict[str, int] = {}
    for tok in tokens(text):
        counts[tok] = counts.get(tok, 0) + 1
    for tok, c in counts.items():
        digest = hashlib.md5(tok.encode("utf-8")).digest()
        vec[int.from_bytes(digest[:4], "big") % dim] += 1.0 + math.log(c)
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def chunk_text(text: str, max_chars: int = 60, overlap: int = 12) -> List[str]:
    """按句末标点切句, 聚合到 max_chars, 相邻块保留 overlap 字符的重叠窗口。"""
    sentences = [s for s in re.split(r"(?<=[。！？!?；;\n])", text) if s.strip()]
    chunks: List[str] = []
    buf = ""
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
    # 仍然超长的块按窗口切开, 保留重叠
    out: List[str] = []
    for c in chunks:
        while len(c) > max_chars:
            out.append(c[:max_chars])
            c = c[max_chars - overlap:] if overlap else c[max_chars:]
        if c.strip():
            out.append(c.strip())
    return out


# ----------------------------------------------------------------- 路线一: 向量

@dataclass
class Chunk:
    doc_id: str
    text: str
    meta: Dict
    vec: List[float]


class VectorStore:
    """切块 -> embedding -> 余弦检索。只能回答"哪段文本和这句话像"。"""

    name = "向量检索"

    def __init__(self, dim: int = DIM):
        self.chunks: List[Chunk] = []

    def ingest(self, doc_id: str, text: str, meta: Optional[Dict] = None,
               max_chars: int = 60) -> int:
        meta = dict(meta or {})
        meta.setdefault("ts", time.time())
        for c in chunk_text(text, max_chars):
            self.chunks.append(Chunk(doc_id, c, meta, embed(c)))
        return len(self.chunks)

    def search(self, query: str, top_k: int = 3, entity: Optional[str] = None,
               since: Optional[float] = None, until: Optional[float] = None,
               must_contain: Optional[Sequence[str]] = None
               ) -> List[Tuple[float, Chunk]]:
        qv = embed(query)
        hits: List[Tuple[float, Chunk]] = []
        for ch in self.chunks:
            if entity is not None and ch.meta.get("entity") != entity:
                continue
            t = ch.meta.get("ts", 0)
            if since is not None and t < since:
                continue
            if until is not None and t >= until:
                continue
            if must_contain and not any(a in ch.text for a in must_contain):
                continue
            hits.append((cosine(qv, ch.vec), ch))
        # 同分时按时间倒序 —— 典型的"永远召回最新那条"的默认行为
        hits.sort(key=lambda x: (-x[0], -x[1].meta.get("ts", 0)))
        return hits[:top_k]


# ----------------------------------------------------------------- 路线二: 摘要

class SummaryStore:
    """滚动摘要 + 结构化事实台账。

    摘要故意做成"有损且粘手"的: 只保留首次见到的数值, 后续变化不更新 ——
    这正是摘要漂移的成因。台账(ledger)是工程上的解药。
    """

    name = "摘要与压缩"

    def __init__(self):
        self.turns: List[Tuple[float, str, str]] = []     # (ts, entity, text)
        self.digest: str = ""                              # 有损散文摘要
        self.ledger: Dict[Tuple[str, str], Dict] = {}      # (entity, predicate) -> 记录
        self.drift: List[str] = []                         # 漂移记录

    def ingest(self, entity: str, text: str, at: float,
               facts: Sequence[Tuple[str, str]], source: str = "user") -> None:
        self.turns.append((at, entity, text))
        for pred, obj in facts:
            key = (entity, pred)
            rec = self.ledger.get(key)
            if rec is None:
                self.ledger[key] = {"value": obj, "first_value": obj,
                                    "history": [], "at": at}
            elif rec["value"] != obj:
                rec["history"].append((rec["value"], at))
                self.drift.append(
                    f"{entity}.{pred}: 事实已从 {rec['value']} 改为 {obj}，"
                    f"但摘要里仍写着 {rec['first_value']}")
                rec["value"] = obj
        self.digest = self._compress()

    def _compress(self) -> str:
        """把对话压成一句话: 只留话题与首次见到的值, 变化历史全部丢掉。"""
        if not self.ledger:
            return ""
        parts = []
        for (ent, pred), rec in self.ledger.items():
            parts.append(f"{ent} 的 {pred} 是 {rec['first_value']}")
        return "；".join(parts) + "。"

    def search(self, query: str) -> List[str]:
        """摘要检索: 只能按话题命中, 命中的文本里往往没有可用的具体数值。"""
        hits = []
        for (ent, pred), rec in self.ledger.items():
            if ent in query or any(a in query for a in PRED_ALIASES.get(pred, [])):
                hits.append(f"{ent} 的 {pred} 是 {rec['first_value']}")
        return hits

    def current(self, entity: str, predicate: str) -> Optional[str]:
        rec = self.ledger.get((entity, predicate))
        return rec["value"] if rec else None


# ----------------------------------------------------------------- 路线三: 图谱

@dataclass
class Edge:
    eid: str
    subject: str
    predicate: str
    object: str
    valid_from: float          # 事件时间: 这条事实在现实中何时成立
    valid_to: Optional[float]  # 事件时间: 何时被新事实取代(None = 仍然成立)
    created_at: float          # 事务时间: 系统何时知道这件事
    invalidated_at: Optional[float]
    source: str

    @property
    def alive(self) -> bool:
        return self.valid_to is None


class GraphStore:
    """时序知识图谱。事实变化时不是删旧边, 而是给旧边打失效时间。"""

    name = "实体图谱"

    def __init__(self):
        self.edges: List[Edge] = []
        self.entities: Set[str] = set()

    def assert_fact(self, subject: str, predicate: str, obj: str, valid_from: float,
                    source: str = "user") -> Tuple[Edge, str]:
        self.entities.add(subject)
        for e in self.edges:
            if (e.subject == subject and e.predicate == predicate and e.alive):
                if e.object == obj:
                    return e, "noop"
                # 旧事实失效: 保留, 补上失效时间, 供历史查询
                e.valid_to = valid_from
                e.invalidated_at = valid_from
        edge = Edge(uuid.uuid4().hex[:8], subject, predicate, obj, valid_from,
                    None, valid_from, None, source)
        self.edges.append(edge)
        return edge, "created"

    def query(self, subject: Optional[str] = None, predicate: Optional[str] = None,
              as_of: Optional[float] = None, knew_at: Optional[float] = None
              ) -> List[Edge]:
        out = []
        for e in self.edges:
            if subject is not None and e.subject != subject:
                continue
            if predicate is not None and e.predicate != predicate:
                continue
            if as_of is not None:                      # 事件时间轴
                if e.valid_from > as_of:
                    continue
                if e.valid_to is not None and e.valid_to <= as_of:
                    continue
            if knew_at is not None:                   # 事务时间轴
                if e.created_at > knew_at:
                    continue
            out.append(e)
        out.sort(key=lambda e: e.valid_from)
        return out

    def current(self, subject: str, predicate: str) -> Optional[Edge]:
        edges = [e for e in self.query(subject, predicate) if e.alive]
        return edges[-1] if edges else None


# --------------------------------------------------------------- 事实抽取(mock)

MOCK_RULES = [
    (re.compile(r"预算(?:是|改成|调到|调整为)\s*(\d+(?:\.\d+)?)\s*万"), "预算",
     lambda m: f"{m.group(1)}万元"),
    (re.compile(r"([A-Z]-\d{3,})\s*错误码"), "故障码", lambda m: m.group(1)),
    (re.compile(r"错误码(?:是|为|:|：)?\s*([A-Z]-\d{3,})"), "故障码", lambda m: m.group(1)),
    (re.compile(r"(\d{1,2})\s*月\s*上线"), "上线时间", lambda m: f"{m.group(1)}月"),
]


def extract_mock(text: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for rx, pred, fn in MOCK_RULES:
        m = rx.search(text or "")
        if not m or pred in seen:
            continue
        seen.add(pred)
        out.append((pred, fn(m)))
    return out


def extract_live(text: str, model: str, base_url: str, api_key: str) -> List[Tuple[str, str]]:
    """真实模型抽取 (OpenAI 兼容 chat/completions)。异常由调用方回退到 mock。"""
    import urllib.request
    prompt = (
        '从下面这句话里抽取稳定事实, 只输出 JSON 数组, 每项形如 '
        '{"predicate":"预算","object":"8万元"}。'
        "只抽长期有效的事实; 价格/库存/订单状态这类时效数据不要抽; 不确定的不要抽。\n\n"
        "句子:\n" + text
    )
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "你是事实抽取器, 只输出 JSON 数组。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    m = re.search(r"\[.*\]", content, re.S)
    items = json.loads(m.group(0)) if m else []
    return [(it["predicate"], str(it["object"])) for it in items if "predicate" in it]


# ----------------------------------------------------------------- 混合内存

CORPUS = [
    dict(doc="chat-001", entity="Orion", day="2026-03-02", source="user",
         text="我们是 Orion 项目的实施方，3 月的预算是 8 万，要 5 月上线。"),
    dict(doc="chat-002", entity="Orion", day="2026-04-11", source="user",
         text="Orion 的预算改成 12 万了，走企业版。"),
    dict(doc="chat-003", entity="Orion", day="2026-05-06", source="user",
         text="Orion 灰度时遇到 E-4012 错误码，排查后确认是证书过期。"),
    dict(doc="chat-004", entity="Argus", day="2026-05-20", source="user",
         text="Argus 项目的预算是 6 万，先做试点。"),
    dict(doc="sop-quote", entity="Orion", day="2026-02-01", source="tool",
         text="报价流程第一步：确认预算与交付时间，再给两套方案并标出总价，最后给出推荐项与理由。"),
]


class HybridMemory:
    """三条路线并存 + 统一的仲裁口径。"""

    def __init__(self, live: bool = False, model: str = "", base_url: str = "",
                 api_key: str = ""):
        self.vector = VectorStore()
        self.summary = SummaryStore()
        self.graph = GraphStore()
        self.live = live
        self.model, self.base_url, self.api_key = model, base_url, api_key

    def _extract(self, text: str) -> List[Tuple[str, str]]:
        if self.live:
            try:
                return extract_live(text, self.model, self.base_url, self.api_key)
            except Exception as exc:                      # noqa: BLE001
                print(f"[live 抽取失败, 回退 mock] {exc}")
                self.live = False
        return extract_mock(text)

    def ingest(self, doc_id: str, entity: str, text: str, day: str,
               source: str = "user") -> None:
        at = ts(day)
        facts = self._extract(text)
        meta = {"entity": entity, "ts": at, "day": day, "source": source, "doc": doc_id}
        self.vector.ingest(doc_id, text, meta)
        self.summary.ingest(entity, text, at, facts, source)
        for pred, obj in facts:
            self.graph.assert_fact(entity, pred, obj, at, source)

    def query(self, q: str, top_k: int = 3) -> Dict:
        ents = [e for e in sorted(self.graph.entities) if e.lower() in q.lower()]
        preds = [p for p, aliases in PRED_ALIASES.items() if any(a in q for a in aliases)]

        vec_hits = self.vector.search(q, top_k=top_k)
        vec_scoped = self.vector.search(q, top_k=top_k, entity=ents[0]) if ents else []
        sum_hits = self.summary.search(q)

        edges: List[Edge] = []
        for p in preds:
            edges += self.graph.query(subject=ents[0] if ents else None, predicate=p)
        edges.sort(key=lambda e: (e.subject, e.valid_from))

        earliest = any(m in q for m in EARLIEST_MARKERS)
        current = any(m in q for m in CURRENT_MARKERS)
        chosen: Optional[Edge] = None
        if edges:
            if earliest:
                chosen = edges[0]
            elif current:
                alive = [e for e in edges if e.alive]
                chosen = alive[-1] if alive else edges[-1]
            else:
                alive = [e for e in edges if e.alive]
                chosen = alive[-1] if alive else edges[-1]

        evidence = []
        if chosen is not None:
            evidence = self.vector.search(
                q, top_k=2, entity=chosen.subject,
                since=chosen.valid_from,
                until=chosen.valid_to if chosen.valid_to is not None else None,
                must_contain=[chosen.predicate] + PRED_ALIASES.get(chosen.predicate, []),
            )

        return {
            "question": q,
            "entities": ents,
            "predicates": preds,
            "vector": vec_hits,
            "vector_scoped": vec_scoped,
            "summary": sum_hits,
            "digest": self.summary.digest,
            "edges": edges,
            "chosen": chosen,
            "evidence": evidence,
            "drift": list(self.summary.drift),
        }


# --------------------------------------------------------------------- 演示

def build(live: bool, model: str, base_url: str, api_key: str) -> HybridMemory:
    mem = HybridMemory(live, model, base_url, api_key)
    for item in CORPUS:
        mem.ingest(item["doc"], item["entity"], item["text"], item["day"], item["source"])
    return mem


def report(mem: HybridMemory, q: str) -> Dict:
    res = mem.query(q)
    print("=" * 74)
    print(f"问题: {q}")
    print("=" * 74)

    print("\n[向量路线] 只按相似度排序的 top-3")
    for score, ch in res["vector"]:
        print(f"  {score:.3f}  {ch.meta['entity']:<6} {ch.meta['day']}  {ch.text}")
    if res["entities"]:
        print("  -- 加了 entity 元数据过滤之后 --")
        for score, ch in res["vector_scoped"]:
            print(f"  {score:.3f}  {ch.meta['entity']:<6} {ch.meta['day']}  {ch.text}")
    print("  看不到: 哪个项目、哪个时间点、哪个版本更新了哪个")

    print("\n[摘要路线] 滚动摘要")
    print(f"  摘要原文: {res['digest']}")
    for h in res["summary"]:
        print(f"  命中: {h}")
    for d in res["drift"]:
        print(f"  漂移: {d}")
    print("  看不到: 具体数值被压缩掉, 变化历史被吃掉")

    print("\n[图谱路线] 实体 + 时序边")
    for e in res["edges"]:
        state = "当前有效" if e.alive else f"已失效于 {fmt(e.valid_to)}"
        print(f"  ({e.subject}, {e.predicate}, {e.object}) "
              f"有效 {fmt(e.valid_from)} ~ {fmt(e.valid_to)}  [{state}]")
    if res["edges"]:
        asof = ts("2026-04-01")
        for subj in sorted({e.subject for e in res["edges"]}):
            pred = next(e.predicate for e in res["edges"] if e.subject == subj)
            past = mem.graph.query(subject=subj, predicate=pred, as_of=asof)
            knew = mem.graph.query(subject=subj, predicate=pred, knew_at=asof)
            print(f"  bi-temporal[{subj}.{pred}]: 2026-04-01 当时的有效值 = "
                  f"{[x.object for x in past] or '无'}；"
                  f"系统当日已知的值 = {[x.object for x in knew] or '无'}")
    print("  看得到: 实体、谓词、版本先后、失效时间")

    print("\n[混合] 图谱定事实, 向量补证据, 摘要补脉络")
    if res["chosen"] is not None:
        e = res["chosen"]
        print(f"  答案: {e.subject} 的 {e.predicate} = {e.object} "
              f"(有效 {fmt(e.valid_from)} ~ {fmt(e.valid_to)})")
        for score, ch in res["evidence"]:
            print(f"  证据({score:.3f}): {ch.text}")
        print(f"  脉络: {res['digest']}")
        if len(res["entities"]) == 0 and len({x.subject for x in res["edges"]}) > 1:
            print(f"  ⚠ 提问没有指明项目, 候选有 "
                  f"{sorted({x.subject for x in res['edges']})}, 需要先消歧")
    else:
        print("  图谱里没有可用事实, 只能回落到向量/摘要")
    return res


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="三条长期记忆后端路线对照")
    ap.add_argument("--live", action="store_true", help="用真实 LLM 抽取事实")
    ap.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    ap.add_argument("--base-url",
                    default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--query", default="", help="对指定问题跑三条路线")
    args = ap.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if args.live and not api_key:
        print("[--live 需要 OPENAI_API_KEY, 已回退 mock]")
        args.live = False

    mem = build(args.live, args.model, args.base_url, api_key)

    if args.query:
        report(mem, args.query)
        return

    print("语料: 3 条 Orion 记录 + 1 条 Argus 记录 + 1 段通用报价 SOP")
    report(mem, "项目预算是多少？")
    report(mem, "Orion 一开始的预算是多少？")
    report(mem, "Orion 现在的预算是多少？")


if __name__ == "__main__":
    main()
