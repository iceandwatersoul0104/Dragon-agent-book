#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — 工作 / 情景 / 语义 / 程序性 四类记忆的最小可运行实现

运行:
    python main.py                     # mock 模式, 无需 API Key
    python main.py --live              # 真实模型抽取 (OpenAI 兼容接口)
    python main.py --query "预算多少"    # 只跑一次检索

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
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

# 来源可信度: 用户陈述 > 工具返回 > 模型推断
SOURCE_RANK = {"user": 2, "tool": 1, "model": 0}


# ----------------------------------------------------------------- 数据结构

@dataclass
class Episode:
    """情景记忆: 一次具体任务的经历, 脱离原始情境不成立。"""
    id: str
    ts: float
    task_id: str
    trigger: str          # 触发这件事的用户表述
    outcome: str          # 结果
    tags: List[str]
    source: str = "run"


@dataclass
class Fact:
    """语义记忆: 脱离情境仍成立的事实 / 偏好 / 约束。"""
    key: str
    value: str
    source: str
    source_rank: int
    ts: float
    confidence: float = 0.7
    valid_until: Optional[float] = None
    version: int = 1


@dataclass
class Procedure:
    """程序性记忆: 怎么做事。候选多次成功后才升级为 active。"""
    name: str
    trigger_kw: List[str]
    steps: List[str]
    status: str = "candidate"     # candidate -> active
    success_count: int = 0
    version: int = 1
    ts: float = 0.0


# ----------------------------------------------------------------- 文本工具

def tokens(text: str) -> Set[str]:
    """极简分词: 中文按 2-gram, 英文数字按词。生产环境换成 embedding。"""
    text = (text or "").lower()
    out: Set[str] = set()
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) == 1:
            out.add(seg)
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
    out.update(re.findall(r"[a-z0-9_.]+", text))
    return out


def score(qtok: Set[str], text: str) -> float:
    tt = tokens(text)
    if not qtok or not tt:
        return 0.0
    return len(qtok & tt) / (len(qtok | tt) ** 0.5)


# ----------------------------------------------------------------- 记忆存储

class MemoryStore:
    def __init__(self, working_limit: int = 32):
        self.working_limit = working_limit
        self.working: Dict[str, dict] = {}          # 工作记忆: 内存, 随任务消亡
        self.episodes: List[Episode] = []           # 情景记忆: 只追加
        self.facts: Dict[str, Fact] = {}            # 语义记忆: 原地更新留版本
        self.procedures: Dict[str, Procedure] = {}  # 程序性记忆: 版本化

    # ----------------------------------------------------------- 写

    def write_working(self, key: str, value, task_id: str) -> None:
        self.working[key] = {"value": value, "ts": time.time(), "task_id": task_id}
        if len(self.working) > self.working_limit:
            oldest = min(self.working, key=lambda k: self.working[k]["ts"])
            del self.working[oldest]

    def write_episode(self, task_id, trigger, outcome, tags=None, source="run") -> Episode:
        ep = Episode(uuid.uuid4().hex[:8], time.time(), task_id, trigger, outcome,
                     list(tags or []), source)
        self.episodes.append(ep)
        return ep

    def write_fact(self, key, value, source="model", confidence=0.7, valid_until=None):
        """写入语义记忆。返回 (fact, status), status: created/updated/unchanged/rejected"""
        rank = SOURCE_RANK.get(source, 0)
        old = self.facts.get(key)
        if old is None:
            fact = Fact(key, value, source, rank, time.time(), confidence, valid_until)
            self.facts[key] = fact
            return fact, "created"
        if old.value == value:
            old.confidence = max(old.confidence, confidence)
            return old, "unchanged"
        if rank < old.source_rank:
            # 低可信来源不允许覆盖高可信来源, 但保留冲突痕迹
            return old, "rejected"
        old.value, old.source, old.source_rank = value, source, rank
        old.confidence, old.ts, old.version = confidence, time.time(), old.version + 1
        return old, "updated"

    def observe_success(self, name, trigger_kw, steps, threshold=3) -> Tuple[Procedure, str]:
        """同类任务连续成功 threshold 次才把候选流程升级为可用 playbook。"""
        p = self.procedures.get(name)
        if p is None:
            p = Procedure(name, list(trigger_kw), list(steps), "candidate", 1, 1, time.time())
            self.procedures[name] = p
            return p, "counted"
        p.success_count += 1
        if p.status == "candidate" and p.success_count >= threshold:
            p.status = "active"
            p.steps = list(steps)
            p.trigger_kw = list(trigger_kw)
            p.version += 1
            return p, "promoted"
        return p, "counted"

    # ----------------------------------------------------------- 读 / 检索

    def working_context(self, task_id: Optional[str] = None) -> str:
        items = [(k, v) for k, v in self.working.items()
                 if task_id is None or v["task_id"] == task_id]
        items.sort(key=lambda kv: kv[1]["ts"])
        return "\n".join(f"{k} = {v['value']}" for k, v in items)

    def match_procedures(self, query: str):
        q = (query or "").lower()
        hits = []
        for p in self.procedures.values():
            if p.status != "active":
                continue
            matched = [k for k in p.trigger_kw if k.lower() in q]
            if matched:
                hits.append((p, matched))
        return hits

    def retrieve(self, query: str, top_k: int = 3,
                 types: Tuple[str, ...] = ("fact", "episode")):
        qtok = tokens(query)
        hits = []
        if "fact" in types:
            for f in self.facts.values():
                s = score(qtok, f.key + " " + f.value)
                if s > 0:
                    hits.append({"type": "fact", "score": round(s, 3), "key": f.key,
                                 "text": f.value, "source": f.source, "ts": f.ts,
                                 "version": f.version})
        if "episode" in types:
            for e in self.episodes:
                s = score(qtok, e.trigger + " " + e.outcome + " " + " ".join(e.tags))
                if s > 0:
                    hits.append({"type": "episode", "score": round(s, 3), "key": e.id,
                                 "text": f"{e.trigger} → {e.outcome}",
                                 "source": e.source, "ts": e.ts})
        hits.sort(key=lambda h: -h["score"])
        return hits[:top_k]

    def build_context(self, query, task_id=None, top_k=3, budget=1200) -> str:
        blocks = []
        wm = self.working_context(task_id)
        if wm:
            blocks.append("【工作记忆】\n" + wm)
        procs = self.match_procedures(query)
        if procs:
            lines = []
            for p, matched in procs:
                lines.append(f"{p.name} (v{p.version} 命中 {','.join(matched)})")
                lines += [f"  {i + 1}. {s}" for i, s in enumerate(p.steps)]
            blocks.append("【程序性记忆】\n" + "\n".join(lines))
        hits = self.retrieve(query, top_k=top_k)
        facts = [h for h in hits if h["type"] == "fact"]
        eps = [h for h in hits if h["type"] == "episode"]
        if facts:
            blocks.append("【语义记忆】\n" + "\n".join(
                f"- {h['key']} = {h['text']} (来源:{h['source']} v{h['version']})" for h in facts))
        if eps:
            blocks.append("【情景记忆】\n" + "\n".join(
                f"- {h['text']} (来源:{h['source']})" for h in eps))
        return "\n\n".join(blocks)[:budget]   # 粗截断, 生产环境用真实 token 计数

    # ----------------------------------------------------------- 遗忘

    def forget(self, key: Optional[str] = None, scope: Optional[str] = None) -> List[str]:
        """显式遗忘: key 命中语义键 / 情景 id / 工作键; scope 清理某个任务的工作记忆。"""
        removed = []
        if key:
            if key in self.facts:
                del self.facts[key]
                removed.append(f"fact:{key}")
            if key in self.working:
                del self.working[key]
                removed.append(f"working:{key}")
            for e in list(self.episodes):
                if e.id == key:
                    self.episodes.remove(e)
                    removed.append(f"episode:{key}")
        if scope:
            for k in [k for k, v in self.working.items() if v["task_id"] == scope]:
                del self.working[k]
                removed.append(f"working:{k}")
        return removed

    def expire(self, episode_days: int = 30) -> Tuple[List[str], List[str]]:
        """TTL 到期: 情景记忆归档删除; 语义记忆降权而不直接删 (便于解释历史决策)。"""
        cutoff = time.time() - episode_days * 86400
        dropped = [e.id for e in self.episodes if e.ts < cutoff]
        self.episodes = [e for e in self.episodes if e.ts >= cutoff]
        expired = []
        for f in self.facts.values():
            if f.valid_until and f.valid_until < time.time():
                f.confidence = min(f.confidence, 0.2)
                expired.append(f.key)
        return dropped, expired


# ----------------------------------------------------------------- 事实抽取

MOCK_RULES = [
    (re.compile(r"预算[^，。；]{0,12}?(\d+(?:\.\d+)?)\s*万"),
     lambda m: ("预算", f"{m.group(1)}万元")),
    (re.compile(r"在\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:之)?前?上线"),
     lambda m: ("上线时间", f"{m.group(1)}月{m.group(2)}日")),
    (re.compile(r"我(?:比较)?喜欢([^，。！？；]{2,20})"),
     lambda m: ("偏好", m.group(1).strip())),
    (re.compile(r"我们?是([^，。！？；]{2,12}?)(?:部门|团队|公司)"),
     lambda m: ("身份", m.group(1).strip())),
]


def extract_mock(text: str) -> List[dict]:
    """mock 抽取器: 规则匹配, 无需 API Key。"""
    out, seen = [], set()
    for rx, fn in MOCK_RULES:
        m = rx.search(text or "")
        if not m:
            continue
        key, value = fn(m)
        if key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "value": value, "source": "user", "confidence": 0.9})
    return out


def extract_live(text: str, model: str, base_url: str, api_key: str) -> List[dict]:
    """真实模型抽取: OpenAI 兼容 chat/completions 接口。异常由调用方回退到 mock。"""
    import urllib.request
    prompt = (
        "从下面这段对话里抽取稳定事实, 只输出 JSON 数组, 每项形如 "
        '{"key":"预算","value":"8万元","source":"user","confidence":0.9}。'
        "规则: 只抽长期有效的事实 / 偏好 / 约束; 不要抽价格、库存、订单状态等时效数据; "
        "不确定的不要抽。\n\n对话:\n" + text
    )
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "你是严格的事实抽取器, 只输出 JSON 数组。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    m = re.search(r"\[.*\]", content, re.S)
    return json.loads(m.group(0)) if m else []


# ----------------------------------------------------------------- 演示

def demo(live: bool, model: str, base_url: str, api_key: str) -> None:
    store = MemoryStore()

    def extract(text: str) -> List[dict]:
        if live:
            try:
                return extract_live(text, model, base_url, api_key)
            except Exception as exc:
                print(f"[live 抽取失败, 回退 mock] {exc}")
                return extract_mock(text)
        return extract_mock(text)

    def hr(title: str) -> None:
        print("\n" + "=" * 10 + " " + title + " " + "=" * 10)

    # ---- 第 1 轮: 采购询价任务 ----
    hr("任务进行中 (工作记忆)")
    task_id = "T-001"
    user1 = ("你好，我是采购部门的人。这次预算 8 万以内，要在 9 月 30 日上线，"
             "我比较喜欢简洁的方案。")
    store.write_working("意图", "采购询价", task_id)
    store.write_working("报价结果", {"标准版": "7.5万", "企业版": "12万"}, task_id)
    print(store.working_context(task_id))

    hr("任务完成 -> 写入时刻")
    # 情景记忆: 任务完成才写, 不在每轮写
    store.write_episode(task_id, trigger="采购询价:预算8万,9月30日上线",
                        outcome="推荐标准版 7.5 万(在预算内)", tags=["询价", "采购"])
    # 语义记忆: 来源可信 + 信息足够才写
    for item in extract(user1):
        _, status = store.write_fact(item["key"], item["value"],
                                     source=item.get("source", "user"),
                                     confidence=item.get("confidence", 0.8))
        print(f"语义记忆写入 {item['key']} = {item['value']} -> {status}")
    # 工作记忆随任务结束清理
    print("清理工作记忆作用域:", store.forget(scope=task_id))

    # ---- 冲突写入 ----
    hr("冲突写入")
    fact, status = store.write_fact("预算", "可能需要 20 万", source="model", confidence=0.4)
    print(f"模型推断写入 -> {status}; 仍为 {fact.value} (来源 {fact.source})")
    fact, status = store.write_fact("预算", "12 万", source="user", confidence=0.95)
    print(f"用户改口写入 -> {status}; 现值 {fact.value} v{fact.version}")

    # ---- 程序性记忆: 连续成功 N 次才升级 ----
    hr("程序性记忆")
    steps = ["确认预算与交付时间", "给出两套方案并标出总价", "给出推荐项与理由"]
    for i in range(3):
        proc, status = store.observe_success("报价流程", ["报价", "询价", "多少钱"], steps)
        print(f"第 {i + 1} 次成功 -> {status} (status={proc.status}, count={proc.success_count})")

    # ---- 第 2 轮: 检索装配 ----
    hr("检索装配上下文")
    query = "我上次的预算是多少？按上次那个来报价"
    print(store.build_context(query, top_k=3))
    print("\n命中明细:")
    for h in store.retrieve(query, top_k=5):
        print(f"  [{h['type']}] score={h['score']} {h['text']} (来源:{h['source']})")

    # ---- TTL 到期 ----
    hr("TTL 到期")
    store.write_fact("限时折扣", "限时 8 折", source="tool", confidence=0.8,
                     valid_until=time.time() - 1)
    dropped, expired = store.expire()
    print("情景归档:", dropped, "| 语义降权:", expired)

    # ---- 显式遗忘 ----
    hr("显式遗忘")
    print("删除 偏好:", store.forget(key="偏好"))
    print("删除后检索 '我的偏好':",
          [(h["type"], h["text"]) for h in store.retrieve("我的偏好", top_k=5)])


def main() -> None:
    try:                                    # Windows 控制台默认不是 UTF-8, 避免中文乱码
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="四类记忆最小实现")
    ap.add_argument("--live", action="store_true", help="用真实模型抽取事实")
    ap.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    ap.add_argument("--base-url",
                    default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--query", default="", help="只跑一次检索")
    args = ap.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if args.live and not api_key:
        print("[--live 需要 OPENAI_API_KEY, 已回退 mock]")
        args.live = False

    if args.query:
        store = MemoryStore()
        store.write_fact("预算", "8万元", source="user", confidence=0.9)
        store.write_episode("T-001", "采购询价:预算8万", "推荐标准版 7.5 万", ["询价"])
        for h in store.retrieve(args.query, top_k=5):
            print(f"[{h['type']}] {h['score']}  {h['text']}  (来源:{h['source']})")
        return

    demo(live=args.live, model=args.model, base_url=args.base_url, api_key=api_key)


if __name__ == "__main__":
    main()
