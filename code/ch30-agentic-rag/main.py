#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch30-agentic-rag / main.py
Agentic RAG: 让检索从预处理变成主动决策 —— 最小可实现

运行:
    python main.py                          # mock 模式, 无需 API Key
    python main.py --live                   # 真实 LLM 做 HyDE 假设与答案措辞
    python main.py --question "订单 250 元，运费多少？电池续航衰减算保修吗？"
    python main.py --only compare           # 只看某个环节: compare / naive / agentic / metrics

依赖: Python 3.9+, 仅标准库, 免安装。
--live 环境变量:
    OPENAI_API_KEY   必需
    OPENAI_BASE_URL  可选, 默认 https://api.openai.com/v1
    OPENAI_MODEL     可选, 默认 gpt-4o-mini
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

DIM = 1024

# --------------------------------------------------------------------- 语料

DOCS = [
    dict(id="d1", title="零售退货政策",
         text="个人零售客户签收商品 7 天内可以无理由退货，商品需保持完好。"
              "定制商品、已拆封的音像制品不支持无理由退货。"
              "退货申请需要在签收后 7 天内在订单页提交。"),
    dict(id="d2", title="运费与包邮规则",
         text="单笔订单实付金额满 299 元包邮，未满收取 12 元运费。"
              "退货产生的退货运费由买家承担，因质量问题导致的退货除外，运费由卖家承担。"
              "偏远地区可能加收 8 元。"),
    dict(id="d3", title="企业客户采购合同",
         text="企业客户签约后享有 30 天试用期，试用期内可全额退款，试用订单不适用无理由退货条款。"
              "试用期内产生的运费由卖方承担。"),
    dict(id="d4", title="保修条款",
         text="整机保修 12 个月，主要零部件保修 24 个月，人为损坏不在保修范围。"
              "电池续航衰减属于正常损耗，不属于质量问题，不予保修。"
              "电池本身保修 6 个月。"),
    dict(id="d5", title="大客户服务承诺",
         text="年度采购额超过 50 万元的企业客户配备专属客户经理，"
              "售后问题 2 小时内响应，重大故障 4 小时内给出临时方案。"),
]

QUESTIONS = [
    dict(q="签收以后多久可以无理由退货？", gold=["d1"]),
    dict(q="退货的运费由谁承担？", gold=["d2"]),
    dict(q="企业客户试用期想退款，运费和保修分别怎么处理？", gold=["d3", "d4"]),
    dict(q="订单 250 元，运费多少？电池续航衰减算保修吗？", gold=["d2", "d4"]),
    dict(q="我们公司年采购 60 万，售后问题多久响应？", gold=["d5"]),
]

# 意图维度: 用于查询分解。specific 覆盖 general(运费 + 退货同时出现时只保留运费)
ASPECTS: Dict[str, List[str]] = {
    "退货": ["退货", "无理由"],
    "退款": ["退款"],
    "试用": ["试用", "合同", "企业客户"],
    "运费": ["运费", "包邮", "邮费", "快递费"],
    "保修": ["保修", "质保", "续航", "电池"],
    "响应": ["响应", "客户经理"],
}
GENERAL = {"退货", "退款"}
SPECIFIC = {"运费", "保修", "响应", "试用"}


# ------------------------------------------------------------------ 工具函数

def tokens(text: str) -> List[str]:
    text = (text or "").lower()
    out: List[str] = []
    for seg in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_.]+", text):
        if re.match(r"[\u4e00-\u9fff]", seg):
            out += list(seg) if len(seg) == 1 else [seg[i:i + 2] for i in range(len(seg) - 1)]
        else:
            out.append(seg)
    return out


def embed(text: str, dim: int = DIM) -> List[float]:
    """确定性哈希 embedding。生产环境换真模型, 这里只用来排序。"""
    vec = [0.0] * dim
    counts: Dict[str, int] = {}
    for t in tokens(text):
        counts[t] = counts.get(t, 0) + 1
    for t, c in counts.items():
        h = hashlib.md5(t.encode("utf-8")).digest()
        vec[int.from_bytes(h[:4], "big") % dim] += 1.0 + math.log(c)
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def score(query: str, text: str) -> float:
    q, d = set(tokens(query)), set(tokens(text))
    if not q or not d:
        return 0.0
    return round(0.75 * len(q & d) / len(q) + 0.25 * len(q & d) / len(q | d), 4)


def sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[。！？])", text) if s.strip()]


def pick_clause(question: str, keywords: Sequence[str]) -> str:
    """取出与该维度相关的那半句, 作为子查询的素材。"""
    for clause in re.split(r"[，？?；;。]", question):
        if any(k in clause for k in keywords):
            return clause.strip()
    return question.strip()


def decompose(question: str) -> List[str]:
    """规则版查询分解: 按子句识别意图维度, 每个维度生成一条子查询。

    一个子句里既有具体维度(运费)又有宽泛维度(退货)时, 只保留具体的那个,
    否则会生成一条没人问过的子查询。
    """
    subs: List[str] = []
    pending = ""                       # 没有意图维度的前置定语, 留给后面那个子句
    for clause in re.split(r"[，？?；;。]", question):
        clause = clause.strip()
        if not clause:
            continue
        found = [name for name, kws in ASPECTS.items() if any(k in clause for k in kws)]
        if not found:
            pending = (pending + clause) if pending else clause
            continue
        specific = [n for n in found if n in SPECIFIC]
        if specific:
            found = specific
        else:
            found = [n for n in found if n in GENERAL] or found
        for name in found:
            sub = f"{pending}{clause} {name}".strip()
            pending = ""
            if sub not in subs:
                subs.append(sub)
    if pending and not subs:
        subs.append(pending)
    return subs or [question]


def hyde_mock(question: str) -> str:
    """mock 版 HyDE: 用规则拼一个"假设性答案", 让查询带上答案里的措辞。"""
    guesses = {
        "退货": "签收 7 天内可以无理由退货，商品需保持完好",
        "退款": "试用期内可全额退款",
        "试用": "企业客户签约后有 30 天试用期",
        "运费": "未满 299 元收 12 元运费，退货运费由买家承担",
        "保修": "整机保修 12 个月，电池续航衰减不予保修",
        "响应": "大客户配备专属客户经理，售后 2 小时内响应",
    }
    parts = [guesses[n] for n in ASPECTS if n in question and n in guesses]
    if not parts:
        return question
    return "（假设性答案）" + "；".join(parts)


# ------------------------------------------------------------------- 检索器

@dataclass
class Hit:
    doc_id: str
    title: str
    text: str
    score: float
    span: str = ""          # 被引用的句子


class Retriever:
    def __init__(self, docs: Sequence[dict]):
        self.docs = list(docs)
        self.vecs = {d["id"]: embed(d["title"] + "。" + d["text"]) for d in self.docs}
        self.by_id = {d["id"]: d for d in self.docs}

    def search(self, query: str, top_k: int = 3) -> List[Hit]:
        """排序按"这一句有多贴题"来算, 而不是整篇文档。

        整篇文档打分会被长度带偏: 长文档命中更多关键词, 归一化后分数反而更低。
        """
        hits = []
        for d in self.docs:
            span, sc = self.best_span(d["id"], query)
            if sc > 0:
                hits.append(Hit(d["id"], d["title"], d["text"], round(sc, 4), span))
        hits.sort(key=lambda h: -h.score)
        return hits[:top_k]

    def best_span(self, doc_id: str, query: str) -> Tuple[str, float]:
        """在文档里挑与查询最相关的一句, 作为可核查的引用片段。

        问期限、金额、几什么时候这类问题时, 含数字的句子优先:
        答案句通常带期限、金额、天数这类硬信息。
        """
        ask_number = bool(re.search(r"多久|几天|多少|几|什么时候|多长", query))
        best, best_s = "", 0.0
        for s in sentences(self.by_id[doc_id]["text"]):
            sc = score(query, s)
            if ask_number and re.search(r"\d", s):
                sc += 0.12
            if sc > best_s:
                best, best_s = s, sc
        return best, best_s


# ----------------------------------------------------------------- 答案结构

@dataclass
class Assertion:
    text: str
    citation: str = ""      # 文档 id
    span: str = ""          # 被引用的原文片段
    supported: bool = False
    aspect: str = ""        # 这条断言回答的是哪个意图维度


@dataclass
class Answer:
    text: str
    assertions: List[Assertion]
    retrieved: List[str]
    calls: int = 0
    rounds: int = 0
    rewrites: int = 0


def verify(assertion: Assertion) -> bool:
    """引用校验, 两条:

    1) 断言必须真的来自被引用的片段(片段为空 = 凭空写的, 引用幻觉);
    2) 片段必须包含这条断言声称的意图维度, 否则就是答非所问。
    """
    if not assertion.citation or not assertion.span:
        return False
    if assertion.aspect and assertion.aspect not in assertion.span:
        return False
    return True


def hallucination_ratio(answer: Answer) -> float:
    """忠实度: 答案里有多少实词不在任何被引用片段中出现(引用标记本身不算)。"""
    cited = " ".join(a.span for a in answer.assertions if a.span)
    if not cited:
        return 1.0
    ct = set(tokens(cited))
    body = re.sub(r"（来源：[^）]*）", "", answer.text)
    at = [t for t in set(tokens(body))
          if re.match(r"[\u4e00-\u9fff]", t) or t.isdigit()]
    if not at:
        return 0.0
    return round(1 - sum(1 for t in at if t in ct) / len(at), 4)


# ------------------------------------------------------------- 朴素 RAG 管线

def naive_pipeline(question: str, retriever: Retriever) -> Answer:
    """固定流水线: 检索一次 -> 拿最相关文档 -> 拼答案 -> 统一挂引用。"""
    hits = retriever.search(question, top_k=3)
    assertions: List[Assertion] = []
    if hits:
        top = hits[0]
        span, _ = retriever.best_span(top.doc_id, question)
        assertions.append(Assertion(span, top.doc_id, span, verify(Assertion(span, top.doc_id, span))))
        # 典型的"顺手加一句": 引用挂着, 但证据里根本没有
        tail = "以上规则同样适用于其他所有场景，如有疑问可随时联系专属客服。"
        assertions.append(Assertion(tail, top.doc_id, "", False))
    text = " ".join(a.text for a in assertions)
    return Answer(text, assertions, [h.doc_id for h in hits], calls=1, rounds=1)


# ----------------------------------------------------------- Agentic RAG 管线

def agentic_pipeline(question: str, retriever: Retriever, hyde_fn=None,
                     budget: int = 8) -> Answer:
    """自主决策的检索循环: 分解 -> 多路检索(含 HyDE) -> 取证 -> 校验 -> 必要时改写再检索。"""
    hyde_fn = hyde_fn or hyde_mock
    calls = 0
    rewrites = 0
    rounds = 0
    retrieved: Dict[str, Hit] = {}
    sub_best: List[Tuple[str, Hit]] = []        # (子查询, 该子查询命中的最佳文档)

    subs = decompose(question)
    rounds += 1
    sub_best: List[Tuple[str, str, Hit]] = []     # (子查询, 意图维度, 最佳文档)
    for sub in subs:
        aspect = sub.rsplit(" ", 1)[-1]
        local: List[Hit] = []
        for q in (sub, hyde_fn(sub)):               # 原查询 + HyDE 假设性答案
            calls += 1
            for hit in retriever.search(q, top_k=2):
                local.append(hit)
                if hit.doc_id not in retrieved or hit.score > retrieved[hit.doc_id].score:
                    retrieved[hit.doc_id] = hit
        if local:
            sub_best.append((sub, aspect, max(local, key=lambda h: h.score)))

    # 组织答案: 同一篇文档可以分别为不同维度提供引用(键是 文档 + 意图维度)
    assertions: List[Assertion] = []
    for sub, aspect, hit in sub_best:
        span, _ = retriever.best_span(hit.doc_id, sub)
        if any((a.citation, a.aspect) == (hit.doc_id, aspect) for a in assertions):
            continue
        a = Assertion(span, hit.doc_id, span, aspect=aspect)
        a.supported = verify(a)
        assertions.append(a)

    # 校验: 没被证据支撑的断言 -> 用缺失的维度词改写查询, 再检索一轮
    for _ in range(2):
        bad = [a for a in assertions if not a.supported]
        if not bad or calls >= budget:
            break
        rounds += 1
        for a in bad:
            clause = a.text
            q = f"{a.aspect} {clause}"               # 补上缺失的维度词重新构造查询
            calls += 1
            rewrites += 1
            for h in retriever.search(q, top_k=3):
                if h.doc_id not in retrieved or h.score > retrieved[h.doc_id].score:
                    retrieved[h.doc_id] = h
                span, _ = retriever.best_span(h.doc_id, q)
                if a.aspect in span and not any(x.citation == h.doc_id for x in assertions):
                    a.text, a.span, a.citation = span, span, h.doc_id
                    a.supported = verify(a)
                    break
        assertions = [a for a in assertions if a.span]

    text = " ".join(f"{a.text}（来源：{a.citation}）" for a in assertions if a.supported)
    return Answer(text, assertions, list(retrieved), calls=calls, rounds=rounds,
                  rewrites=rewrites)


# ------------------------------------------------------------------ 评估

def evaluate(name: str, answers: List[Answer], questions: List[dict]) -> Dict:
    """三个质量指标 + 一个成本指标。

    命中率: 答案引用的文档覆盖了应引用文档的比例(top-k 里检索到不算, 用上了才算)。
    支撑率: 有证据支撑的断言占比。
    忠实度: 答案内容全部出自被引用片段的比例。
    检索次数: 平均每题花了几次检索, 即成本。
    """
    hit = 0.0
    support_num = support_den = 0
    faith = 0.0
    calls = 0
    for ans, q in zip(answers, questions):
        cited = {a.citation for a in ans.assertions if a.supported}
        gold = set(q["gold"])
        hit += len(cited & gold) / len(gold) if gold else 0.0
        for a in ans.assertions:
            support_den += 1
            if a.supported:
                support_num += 1
        faith += (1 - hallucination_ratio(ans))
        calls += ans.calls
    n = len(questions)
    return {
        "方案": name,
        "命中率": round(hit / n, 3),
        "支撑率": round(support_num / max(1, support_den), 3),
        "忠实度": round(faith / n, 3),
        "检索次数": round(calls / n, 1),
    }


# ------------------------------------------------------------------ mock LLM

def live_hyde(question: str, model: str, base_url: str, api_key: str) -> str:
    prompt = (f"请针对下面的问题写一段 40 字以内的假设性答案(用于检索扩写), 不要解释:\n{question}")
    return _chat(prompt, model, base_url, api_key) or hyde_mock(question)


def live_polish(evidence: str, model: str, base_url: str, api_key: str) -> str:
    prompt = (f"只根据下面的证据用两句话回答, 每句末尾注明引用的文档编号:\n{evidence}")
    return _chat(prompt, model, base_url, api_key) or evidence


def _chat(prompt: str, model: str, base_url: str, api_key: str) -> str:
    import urllib.request
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


# --------------------------------------------------------------------- 演示

def hr(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def demo(live: bool, model: str, base_url: str, api_key: str,
         only: str = "", custom_question: str = "") -> None:
    retriever = Retriever(DOCS)
    hyde_fn = hyde_mock
    if live:
        def hyde_fn(q: str) -> str:          # noqa: F811
            try:
                return live_hyde(q, model, base_url, api_key)
            except Exception:               # noqa: BLE001
                return hyde_mock(q)

    if only in ("", "naive", "compare", "agentic", "metrics"):
        qs = ([dict(q=custom_question, gold=[])] if custom_question else QUESTIONS)
    else:
        qs = QUESTIONS

    if only in ("", "naive", "compare"):
        hr("朴素 RAG: 固定流水线, 检索一次就下结论")
        naive_answers = []
        for q in qs:
            ans = naive_pipeline(q["q"], retriever)
            naive_answers.append(ans)
            print(f"\nQ: {q['q']}")
            print(f"  检索({ans.calls} 次): {ans.retrieved}")
            for a in ans.assertions:
                mark = "有支撑" if a.supported else "无支撑(引用幻觉)"
                print(f"  - {a.text}（来源：{a.citation}）[{mark}]")
        print("\n  问题: 只检索一次就作答, 多问题只答一半, 而且会顺手加一句没有依据的结论。")

    if only in ("", "agentic", "compare"):
        hr("Agentic RAG: 分解 -> 多路检索 -> 校验 -> 必要时改写再检索")
        agent_answers = []
        for q in qs:
            ans = agentic_pipeline(q["q"], retriever, hyde_fn)
            agent_answers.append(ans)
            print(f"\nQ: {q['q']}")
            print(f"  子查询: {decompose(q['q'])}")
            print(f"  检索({ans.calls} 次, {ans.rounds} 轮, 改写 {ans.rewrites} 次): {ans.retrieved}")
            for a in ans.assertions:
                mark = "有支撑" if a.supported else "无支撑"
                print(f"  - {a.text}（来源：{a.citation}）[{mark}]")
            print(f"  最终答复: {ans.text}")

    if only in ("", "metrics", "compare"):
        hr("评估: 命中率 / 支撑率 / 忠实度 / 检索次数")
        naive_answers = [naive_pipeline(q["q"], retriever) for q in QUESTIONS]
        agent_answers = [agentic_pipeline(q["q"], retriever, hyde_fn) for q in QUESTIONS]
        rows = [evaluate("朴素 RAG", naive_answers, QUESTIONS),
                evaluate("Agentic RAG", agent_answers, QUESTIONS)]
        print(f"  {'方案':<14}{'命中率':<10}{'支撑率':<10}{'忠实度':<10}检索次数")
        for r in rows:
            print(f"  {r['方案']:<14}{r['命中率']:<10}{r['支撑率']:<10}"
                  f"{r['忠实度']:<10}{r['检索次数']}")
        print("\n  代价: Agentic 每题多花几次检索与一轮模型调用, 延迟和 token 成本更高。")

    if only == "" and custom_question:
        hr("自定义问题")
        ans = agentic_pipeline(custom_question, retriever, hyde_fn)
        print(f"  子查询: {decompose(custom_question)}")
        print(f"  检索: {ans.retrieved}")
        print(f"  答复: {ans.text}")
        if live:
            try:
                print(f"  live 措辞: {live_polish(ans.text, model, base_url, api_key)}")
            except Exception as exc:          # noqa: BLE001
                print(f"  [live 调用失败] {exc}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Agentic RAG: 检索从预处理变成主动决策")
    ap.add_argument("--live", action="store_true", help="用真实 LLM 做 HyDE 与措辞")
    ap.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    ap.add_argument("--base-url",
                    default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--only", default="",
                    choices=["", "naive", "agentic", "metrics", "compare"])
    ap.add_argument("--question", default="", help="额外跑一个自定义问题")
    args = ap.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if args.live and not api_key:
        print("[--live 需要 OPENAI_API_KEY, 已回退 mock]")
        args.live = False

    demo(args.live, args.model, args.base_url, api_key, args.only, args.question)


if __name__ == "__main__":
    main()
