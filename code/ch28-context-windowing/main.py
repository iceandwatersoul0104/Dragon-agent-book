#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch28-context-windowing / main.py
上下文窗口工程: 分层检索 / 历史压缩 / 价值淘汰 / Prompt Caching 布局

运行:
    python main.py                          # mock 模式, 无需 API Key
    python main.py --live                   # 真实 LLM 生成最终回答 (OpenAI 兼容接口)
    python main.py --query "现在预算是多少？"   # 用自定义问题替换最后一轮提问

依赖: Python 3.9+, 仅标准库, 免安装。
--live 环境变量:
    OPENAI_API_KEY   必需
    OPENAI_BASE_URL  可选, 默认 https://api.openai.com/v1
    OPENAI_MODEL     可选, 默认 gpt-4o-mini
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------- 常量

SYS_PROMPT = "你是项目实施助手，根据长期约束和历史记录回答，涉及金额与时间必须给出依据。"
TOOL_SPEC = "工具：search_docs(query) 查归档资料；query_order(id) 查订单；没有工具能查预算历史。"

# 演示语料：实施助手的多轮对话。speaker 为空表示这是用户提问。
TURNS = [
    ("采购", "我们是 Orion 项目的实施方，预算 8 万，要在 5 月上线，帮我列个方案。"),
    ("采购", "方案里加上灰度回滚，出问题要能退回上一版。"),
    ("采购", "我们用的是自研网关，E-4012 错误码上次是证书过期。"),
    ("采购", "预算能不能提到 12 万？"),
    ("", "上线前灰度要留多久？"),            # 提问 1：召回层答不了，下沉归档层
    ("采购", "顺便把最终方案再发一遍。"),     # 不改变任何长期约束
    ("", "现在预算是多少？"),                # 提问 2：压缩 / 淘汰 / 缓存对比
]

ARCHIVE_DOC = "Orion 标准交付周期为 6 周；灰度前须通过 E-4012 自检清单，回滚窗口 24 小时。"

# 关键事实清单：用来算压缩后的"事实保留率"
CRITICAL_FACTS = [("预算", "8"), ("预算", "12"), ("上线", "5"),
                  ("E-4012", "证书过期"), ("回滚", "回滚")]

# 槽位表：问句里的量词类型，以及回答它必须出现的单位
SLOTS: Dict[str, Tuple[List[str], List[str]]] = {
    "时长": (["多久", "几天", "多长时间", "多长"], ["天", "周", "个月", "小时"]),
    "金额": (["多少", "几万", "几个钱", "多少钱"], ["万", "元", "块"]),
    "时间点": (["什么时候", "哪天", "几号"], ["月", "日", "号"]),
}

HISTORY_MARKERS = ["最初", "一开始", "原来", "起初", "第一版", "历史上", "当初"]


# ------------------------------------------------------------------ 工具函数

def pseudo_tokens(text: str) -> List[str]:
    """伪分词：中文按字，拉丁按 4 字符。用于估算 token 数与缓存前缀比对。"""
    out: List[str] = []
    for seg in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_.,:/=\-]+", text or ""):
        if re.match(r"[\u4e00-\u9fff]", seg):
            out.extend(list(seg))
        else:
            out.extend(seg[i:i + 4] for i in range(0, len(seg), 4))
    return out


def count_tokens(text: str) -> int:
    return len(pseudo_tokens(text))


def overlap_score(query: str, text: str) -> float:
    """相关度：查询 token 覆盖率为主，Jaccard 为辅。"""
    a, b = set(pseudo_tokens(query)), set(pseudo_tokens(text))
    if not a or not b:
        return 0.0
    return round(0.7 * len(a & b) / len(a) + 0.3 * len(a & b) / len(a | b), 4)


def clauses(text: str) -> List[str]:
    """切成短小原子子句：短、单一主题，才谈得上被覆盖。"""
    return [c.strip() for c in re.split(r"[，。；！？\n]", text or "") if c.strip()]


def topic_of(text: str) -> Optional[str]:
    if "预算" in text or "额度" in text:
        return "预算"
    if "错误码" in text or re.search(r"[A-Z]-\d{3,}", text):
        return "错误码"
    if "回滚" in text:
        return "回滚"
    if "上线" in text:
        return "上线"
    return None


def budget_values(text: str) -> List[str]:
    return re.findall(r"(?:预算|额度)[^。；\n]{0,14}?(\d+(?:\.\d+)?)\s*万", text)


# ----------------------------------------------------------------- 上下文项

@dataclass
class Item:
    layer: str          # core / recall / archival / current
    text: str
    source: str = "user"
    turn: int = 0
    trust: float = 1.0
    stale: bool = False  # 已被新事实覆盖

    @property
    def tokens(self) -> int:
        return count_tokens(self.text)


@dataclass
class Segment:
    name: str
    text: str
    stable: bool = True


# ------------------------------------------------------------- 一、分层检索

class LayeredRetriever:
    """core（常驻约束）/ recall（对话历史）/ archival（归档资料）三层。"""

    def __init__(self):
        self.core: Dict[str, Item] = {}        # 主题槽位，同主题新值覆盖旧值
        self.recall: List[Item] = []
        self.archival: List[Item] = []
        self.superseded: Dict[str, int] = {}   # 被覆盖掉的旧值 -> 生效轮次

    def add_core(self, text: str, turn: int, source: str = "user") -> None:
        topic = topic_of(text)
        if topic is None:
            return
        old = self.core.get(topic)
        if old is not None:
            for v in budget_values(old.text):
                self.superseded.setdefault(v, old.turn)
        self.core[topic] = Item("core", text, source, turn)

    def add_recall(self, text: str, turn: int, source: str = "user") -> None:
        self.recall.append(Item("recall", text, source, turn))

    def is_stale(self, it: Item) -> bool:
        """旧值出现在更早的轮次里，说明它已经被同主题的新事实取代。"""
        for v in budget_values(it.text):
            if v in self.superseded and it.turn <= self.superseded[v]:
                return True
        return False

    def missing_slot(self, query: str, items: Sequence[Item]) -> Optional[str]:
        """缺口检测：问句要的量值类型，当前层里有没有出现。"""
        blob = " ".join(it.text for it in items)
        for name, (questions, units) in SLOTS.items():
            if any(q in query for q in questions) and not any(u in blob for u in units):
                return name
        return None

    def retrieve(self, query: str, recall_top_k: int = 3) -> Dict:
        core_hits = [self.core[k] for k in sorted(self.core)]
        scored = sorted(((overlap_score(query, it.text), it) for it in self.recall),
                        key=lambda x: -x[0])
        recall_hits = [it for s, it in scored if s > 0][:recall_top_k]
        gap = self.missing_slot(query, core_hits + recall_hits)
        return {
            "core": core_hits,
            "recall": recall_hits,
            "archival": list(self.archival) if gap else [],
            "gap": gap,
            "recall_score": round(scored[0][0], 3) if scored else 0.0,
        }


# ----------------------------------------------------------------- 二、压缩

def compress_sliding_window(items: Sequence[Item], keep_last: int = 2) -> List[Item]:
    """滑窗：只留最近 N 轮。最便宜，丢得最狠 —— 早期约束直接消失。"""
    return list(items[-keep_last:])


def compress_rolling_summary(items: Sequence[Item], state: Dict[str, str]) -> List[Item]:
    """滚动摘要：压成一句话，但只保留首次见到的值（粘性旧值 = 漂移来源）。
    摘要每轮整体重写，所以它也是缓存前缀里最不稳定的一段。"""
    first: Dict[str, str] = state.setdefault("first_values", {})
    for it in items:
        vals = budget_values(it.text)
        if vals:
            first.setdefault("预算", vals[0] + "万")
    parts = [f"{k} 是 {v}" for k, v in first.items()]
    if items:
        tail = clauses(items[-1].text)
        if tail:
            parts.append(f"最近提到：{tail[-1]}")
    text = "；".join(parts) + "。" if parts else ""
    return [Item("recall", text, "model", items[-1].turn if items else 0)]


def compress_extractive(items: Sequence[Item]) -> List[Item]:
    """抽取式压缩：只保留带约束信号的子句（数字/时间/错误码/必须/改成），丢掉寒暄。"""
    keep_re = re.compile(r"(\d|错误码|必须|不能|要|改成|提到|上线|回滚|额度)")
    out: List[Item] = []
    for it in items:
        for c in clauses(it.text):
            if keep_re.search(c):
                out.append(Item("recall", c, it.source, it.turn, it.trust))
    return out


def fact_retention(compressed: Sequence[Item]) -> Tuple[int, int, List[str]]:
    blob = " ".join(it.text for it in compressed).replace(" ", "")
    kept, lost = 0, []
    for key, val in CRITICAL_FACTS:
        if key in blob and val in blob:
            kept += 1
        else:
            lost.append(f"{key}={val}")
    return kept, len(CRITICAL_FACTS), lost


# ------------------------------------------------------------- 三、价值淘汰

@dataclass
class Scored:
    item: Item
    score: float
    parts: Dict[str, float]


class ValueEvictor:
    """给每条上下文打分，再按 token 预算淘汰。高分的是未闭合的约束，低分的是寒暄和旧事实。"""

    WEIGHTS = {"relevance": 0.35, "recency": 0.15, "obligation": 0.35, "trust": 0.15}

    def __init__(self, current_turn: int):
        self.current_turn = current_turn

    def score(self, it: Item, query: str) -> Scored:
        relevance = overlap_score(query, it.text)
        recency = 1.0 / (1.0 + max(0, self.current_turn - it.turn))
        obligation = 0.0 if it.stale else (
            1.0 if re.search(r"\d|必须|不能|改成|提到", it.text) else 0.0)
        trust = it.trust * (0.3 if it.stale else 1.0)
        total = (self.WEIGHTS["relevance"] * relevance
                 + self.WEIGHTS["recency"] * recency
                 + self.WEIGHTS["obligation"] * obligation
                 + self.WEIGHTS["trust"] * trust)
        return Scored(it, round(total, 4),
                      {"rel": relevance, "rec": round(recency, 3),
                       "obl": obligation, "trust": round(trust, 3)})

    def evict(self, items: Sequence[Item], query: str, budget: int
              ) -> Tuple[List[Item], List[Scored], int]:
        # 同一条文本可能同时出现在 core 和 recall，只留分数最高的那份
        best: Dict[str, Scored] = {}
        for it in items:
            s = self.score(it, query)
            if it.text not in best or s.score > best[it.text].score:
                best[it.text] = s
        scored = sorted(best.values(), key=lambda s: -s.score)
        kept: List[Item] = []
        dropped: List[Scored] = []
        used = 0
        for s in scored:
            if s.item.layer == "current" or used + s.item.tokens <= budget:
                kept.append(s.item)
                used += s.item.tokens
            else:
                dropped.append(s)
        kept.sort(key=lambda it: (it.turn, it.layer))     # 保持时间顺序
        return kept, dropped, used


# --------------------------------------------------------- 四、Prompt Caching

class PromptCache:
    """模拟前缀缓存：前缀 token 不变就命中，变了就从此处全部作废。"""

    def __init__(self):
        self.prev: Optional[List[str]] = None

    @staticmethod
    def _tokens(segs: Sequence[Segment]) -> List[str]:
        out: List[str] = []
        for sg in segs:
            out.append(f"<{sg.name}>")
            out.extend(pseudo_tokens(sg.text))
        return out

    def observe(self, segs: Sequence[Segment]) -> Tuple[Optional[float], int]:
        cur = self._tokens(segs)
        if self.prev is None:
            self.prev = cur
            return None, len(cur)
        n = 0
        while n < len(self.prev) and n < len(cur) and self.prev[n] == cur[n]:
            n += 1
        ratio = n / len(cur) if cur else 0.0
        self.prev = cur
        return ratio, len(cur)


def build_layout(layout: str, core_text: str, history_text: str,
                 retrieved_text: str, question: str) -> List[Segment]:
    """三种布局：检索结果放最前 / 放最后 / 放最后但历史每轮整体重写。"""
    if layout == "retrieval_first":
        return [Segment("检索结果", retrieved_text, stable=False),
                Segment("系统提示", SYS_PROMPT),
                Segment("工具定义", TOOL_SPEC),
                Segment("长期约束", core_text),
                Segment("历史", history_text),
                Segment("当前问题", question, stable=False)]
    if layout == "retrieval_last":
        return [Segment("系统提示", SYS_PROMPT),
                Segment("工具定义", TOOL_SPEC),
                Segment("长期约束", core_text),
                Segment("历史", history_text),
                Segment("检索结果", retrieved_text, stable=False),
                Segment("当前问题", question, stable=False)]
    return [Segment("系统提示", SYS_PROMPT),
            Segment("工具定义", TOOL_SPEC),
            Segment("长期约束", core_text),
            Segment("历史(每轮重写)", history_text),
            Segment("检索结果", retrieved_text, stable=False),
            Segment("当前问题", question, stable=False)]


# ------------------------------------------------------------------ mock 模型

def slot_of(question: str) -> Optional[str]:
    for name, (questions, _units) in SLOTS.items():
        if any(q in question for q in questions):
            return name
    return None


def mock_answer(items: Sequence[Item], question: str) -> str:
    """从最终上下文里取值：按问句要的槽位找，答案随上下文内容变化。"""
    blob = "\n".join(it.text for it in items)
    slot = slot_of(question)
    if slot == "金额":
        hits = budget_values(blob)
        if not hits:
            return "上下文里没有预算信息，我需要再查一次。"
        if any(m in question for m in HISTORY_MARKERS):
            return f"最初的预算是 {hits[0]}万。"
        return f"现在的预算是 {hits[-1]}万。"
    if slot == "时长":
        m = re.search(r"(\d+)\s*(周|天|个月|小时)", blob)
        if m:
            return f"灰度前建议留 {m.group(1)} {m.group(2)}。"
        return "上下文里没有时长信息，我需要查归档资料。"
    return "我没有在上下文里找到可直接引用的依据。"


def live_answer(prompt: str, model: str, base_url: str, api_key: str) -> str:
    """真实模型：把拼好的 prompt 原样发出去。"""
    import urllib.request
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYS_PROMPT},
                     {"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


# --------------------------------------------------------------------- 主流程

class ContextManager:
    def __init__(self):
        self.retr = LayeredRetriever()
        self.history: List[Item] = []
        self.summary_state: Dict[str, str] = {}
        self.turn = 0

    def ingest(self, speaker: str, text: str) -> None:
        self.turn += 1
        self.history.append(Item("recall", text, "user", self.turn))
        self.retr.add_recall(text, self.turn)
        for c in clauses(text):
            self.retr.add_core(c, self.turn)

    def run_turn(self, question: str) -> Dict:
        self.turn += 1
        self.history.append(Item("current", question, "user", self.turn))
        want_history = any(m in question for m in HISTORY_MARKERS)

        # ① 分层检索
        r = self.retr.retrieve(question)
        retrieved = r["core"] + r["recall"] + r["archival"]

        # ② 三种压缩策略
        hist = [it for it in self.history if it.layer == "recall"]
        strategies = {
            "滑窗(留最近2轮)": compress_sliding_window(hist, 2),
            "滚动摘要": compress_rolling_summary(hist, self.summary_state),
            "抽取式": compress_extractive(hist),
        }
        comparison = {}
        for name, comp in strategies.items():
            kept, total, lost = fact_retention(comp)
            comparison[name] = {
                "items": comp,
                "tokens": sum(it.tokens for it in comp),
                "kept": kept, "total": total, "lost": lost,
                "now": mock_answer(comp, "现在预算是多少？"),
                "then": mock_answer(comp, "最初的预算是多少？"),
            }

        # ③ 价值淘汰：旧事实默认不进上下文，问历史时才请回来
        candidates = retrieved + comparison["抽取式"]["items"]
        for it in candidates:
            it.stale = self.retr.is_stale(it)
        live_candidates = [it for it in candidates
                           if not it.stale or want_history]
        evictor = ValueEvictor(self.turn)
        kept_items, dropped, used = evictor.evict(live_candidates, question, budget=130)

        # ④ 组装 prompt
        core_text = "\n".join(self.retr.core[k].text for k in sorted(self.retr.core))
        hist_text = "\n".join(it.text for it in comparison["抽取式"]["items"])
        summary_text = "\n".join(it.text for it in comparison["滚动摘要"]["items"])
        retrieved_text = "\n".join(it.text for it in retrieved)
        layouts = {
            "retrieval_first": build_layout("retrieval_first", core_text, hist_text,
                                            retrieved_text, question),
            "retrieval_last": build_layout("retrieval_last", core_text, hist_text,
                                           retrieved_text, question),
            "history_rewritten": build_layout("history_rewritten", core_text, summary_text,
                                              retrieved_text, question),
        }
        return {
            "question": question,
            "retrieval": r,
            "comparison": comparison,
            "kept": kept_items,
            "dropped": dropped,
            "used": used,
            "layouts": layouts,
            "excluded_stale": [it for it in candidates
                               if it.stale and not want_history],
        }


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def demo(live: bool, model: str, base_url: str, api_key: str, single_query: str = "") -> None:
    mgr = ContextManager()
    mgr.retr.archival.append(Item("archival", ARCHIVE_DOC, "tool", 0, 0.9))
    cache = {name: PromptCache() for name in
             ("retrieval_first", "retrieval_last", "history_rewritten")}
    hits = {name: [] for name in cache}
    turns = TURNS if not single_query else TURNS[:-1] + [("", single_query)]

    for speaker, text in turns:
        if speaker:
            mgr.ingest(speaker, text)
            continue
        res = mgr.run_turn(text)

        hr(f"提问: {text}")

        print("\n[① 分层检索] core 常驻 -> recall 命中 -> 缺口检测下沉 archival")
        for it in res["retrieval"]["core"]:
            print(f"  core     | {it.text}")
        for it in res["retrieval"]["recall"]:
            print(f"  recall   | {it.text}")
        for it in res["retrieval"]["archival"]:
            print(f"  archival | {it.text}")
        gap = res["retrieval"]["gap"]
        print(f"  缺口检测: recall 最高分 {res['retrieval']['recall_score']} -> "
              f"{'缺少「' + gap + '」类量值，下沉 archival' if gap else '当前层可答，不触发 archival'}")

        print("\n[② 历史压缩] 三种算法对同一段历史")
        print(f"  {'策略':<16}{'tokens':>8}{'事实保留':>10}   问“现在”          问“最初”")
        for name, c in res["comparison"].items():
            print(f"  {name:<16}{c['tokens']:>8}{c['kept']:>6}/{c['total']:<3}"
                  f"   {c['now']:<18}  {c['then']}")
        for name, c in res["comparison"].items():
            if c["lost"]:
                print(f"  {name} 丢失: {c['lost']}")

        print("\n[③ 价值淘汰] 预算 130 tokens")
        for it in res["kept"]:
            print(f"  保留 | {it.layer:<8} | {it.text}")
        for s in res["dropped"]:
            print(f"  淘汰 | {s.item.layer:<8} | score={s.score:<7} | {s.item.text}")
        for it in res["excluded_stale"]:
            print(f"  排除 | {it.layer:<8} | 已被新事实覆盖 | {it.text}")
        print(f"  占用 {res['used']} tokens")

        print("\n[④ Prompt Caching] 三种布局的前缀命中率")
        for name, segs in res["layouts"].items():
            ratio, total = cache[name].observe(segs)
            if ratio is None:
                print(f"  {name:<18} 首轮建立缓存 (总 {total} tokens)")
            else:
                hits[name].append(ratio)
                avg = sum(hits[name]) / len(hits[name])
                print(f"  {name:<18} 本轮命中 {ratio * 100:5.1f}%  平均 {avg * 100:5.1f}%"
                      f"  (总 {total} tokens)")

        final_prompt = "\n".join(f"[{sg.name}]\n{sg.text}"
                                 for sg in res["layouts"]["retrieval_last"])
        if live:
            try:
                ans = live_answer(final_prompt, model, base_url, api_key)
            except Exception as exc:                        # noqa: BLE001
                print(f"\n[live 调用失败, 回退 mock] {exc}")
                ans = mock_answer(res["kept"], text)
        else:
            ans = mock_answer(res["kept"], text)
        print(f"\n[⑤ 最终回答] {ans}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="上下文窗口工程：检索/压缩/淘汰/缓存")
    ap.add_argument("--live", action="store_true", help="用真实 LLM 生成最终回答")
    ap.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    ap.add_argument("--base-url",
                    default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--query", default="", help="替换最后一轮提问")
    args = ap.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if args.live and not api_key:
        print("[--live 需要 OPENAI_API_KEY, 已回退 mock]")
        args.live = False

    demo(args.live, args.model, args.base_url, api_key, args.query)


if __name__ == "__main__":
    main()
