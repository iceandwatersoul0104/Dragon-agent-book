# 第 26 章 记忆分类学：工作、情景、语义与程序性记忆

## 一、本章要解决的问题

先看一个真实事故。某团队给内部客服 Agent 加"记忆"：每轮对话后让模型摘要一段写进向量库（vector store），提问时检索 top-5 拼进提示词（prompt）。上线三周，投诉集中在三类：

1. 用户问"我上个月报过的故障修好了吗"，Agent 一问三不知。摘要确实在库里，但两句话用词不重叠，检索没捞上来。
2. 用户把预算从 5 万改成 8 万，Agent 仍按 5 万报价。两条高度相似的预算记录一起进了 top-5，矛盾事实同时进上下文，没有仲裁。
3. Agent 偶尔"记得"用户从没说过的事：某次摘要时模型自己补了句"该用户偏好深色界面"，此后每次都"记得"。

这三件事没有一个能靠调大 top-k 或换个 embedding 模型解决。根因在设计：把所有东西都叫"记忆"，再用同一套存储、检索和淘汰策略去对待它们。

而工作记忆（Working Memory）、情景记忆（Episodic Memory）、语义记忆（Semantic Memory）、程序性记忆（Procedural Memory）在工程上根本不是同一种东西：寿命、写入时机、冲突规则、检索方式都不同。本章要做的是给"记忆"做一次工程分类：把认知科学的划分翻译成可落地的存储形态，并给出每一类的接口、生命周期和失效条件。

## 二、原理：机制拆解

### 2.1 判定标准：三个问题

对一条信息 X，按顺序问三个问题：

```
读/写一条信息 X 时:

Q1  X 的有效期是否绑定在"当前这次任务 / 当前这段对话"上?
      是 ────────────────────────────────► 工作记忆
      否 ▼
Q2  把"在什么时候、为了哪个任务、和谁一起"删掉，X 还成立吗?
      不成立（"那次重启后就好了"）───────► 情景记忆
      成立（"预算上限 8 万"）────────────► 语义记忆

Q3  X 描述的是"怎么做事"而不是"是什么事"吗?
      是 ────────────────────────────────► 程序性记忆
```

Q1、Q2 是时间轴上的分类（活多久、脱离情境是否成立），Q3 是内容形态上的分类。一句"新员工入职要走 IT 工单"既是语义事实又是程序性步骤，拆成两条写即可。

### 2.2 工程映射

| 类型 | 内容形态与存储 | 生命周期 | 写入 |
|---|---|---|---|
| 工作记忆 | 意图、工具返回值 → 内存 dict / state 字段 | 秒到分钟，任务结束丢弃 | 随手写，有硬上限 |
| 情景记忆 | 事件 + 结果 + 标签 + 时间戳 → JSONL / 关系库 | 天到月，可归档 | 任务完成点写一次 |
| 语义记忆 | 事实、偏好、约束 → KV（来源 / 置信度 / 有效期） | 月到年，更新留版本 | 来源可信才写 |
| 程序性记忆 | 步骤、SOP、工具模板 → 版本化作业手册（playbook） | 版本级 | 连续成功 N 次才升级 |

用户说"记住"时其实是三种需求：记住事实（语义）、记住经历（情景）、记住技能（程序性），剩下的"别忘了我现在算到哪"才是工作记忆。

三条生命周期规则：工作记忆随请求消亡，不跨会话；情景记忆只追加不修改，要改就追加一条修订（留着是为了能解释"当时为什么那么做"）；语义记忆原地更新但留旧版本；程序性记忆按版本发布。

### 2.3 认知科学能借什么，不能借什么

能借的只有三样：Tulving 1972 年的情景 / 语义二分（就是上面 Q2 那条判据）、工作记忆容量有限（提醒你工作内存是稀缺资源，要优先装正在被使用的量，这直接推出淘汰策略）、程序性记忆与陈述性记忆分离（支持把"怎么做"从事实库里拆出来单独管理）。

不能借的有五点，恰好都是常见坑：

- 人脑不是向量数据库，"神经连接"只是修辞不是机制。
- "短期记忆 = 上下文窗口（context window），长期记忆 = 外部数据库"是错误映射：上下文窗口是每轮都要重读的工作区，瓶颈在注意力与信噪比而非容量，叫它"长期记忆"会让人误以为塞进去就等于记住了。
- "7±2"之类的容量数字不能当工程参数，放几条取决于任务与内容密度。
- "睡眠中巩固记忆"不能等价成"跑个离线任务"：去重、去冲突、再巩固是工程设计，不是生物模拟。
- 最要紧的一条：认知科学给的是分类启发和判据，不是可移植的算法。它的价值在于逼你为每一类分别设计存储、生命周期、写入时机和冲突仲裁。

### 2.4 记忆系统的四个动词

```
        ┌──────────── 写 (write) ────────────┐
        │  携带: type / source / ts / scope   │
        │  异步落盘, 不阻塞主链路             │
        ▼
  ┌────────────────────────────────────────────────┐
  │  读 (read)        写 (write)                   │
  │     ▲                   │                       │
  │     │                   ▼                       │
  │  检索 (retrieve) ──► 遗忘 (forget)              │
  │  query/type/top_k   TTL / 显式删除 / 归档降级    │
  └────────────────────────────────────────────────┘
```

读（read）：装配当前上下文——工作记忆全量注入，程序性记忆按触发词匹配，语义与情景记忆检索后取 top-k。

写（write）：必须带类型、来源、时间戳、作用域且异步执行；写入是唯一改变系统状态的动作，入口必须唯一。

遗忘（forget）：最常被忽略的动词。三种机制：显式删除、TTL 到期、访问衰减。没有遗忘的记忆库最终必然变成噪音池。

检索（retrieve）：统一入口，参数是 type / query / top_k / budget，返回必须带来源与时间戳——模型得知道一条记忆是用户亲口说的还是自己推断的。

读和写必须同一个入口。最常见的架构错误是三个库各写各的，最后没人说得清这个 Agent 记得什么。

### 2.5 写入时机

- **情景记忆在任务完成点写，不是每轮写。** 任务没跑完你不知道结果，写进去就是噪音。
- **语义记忆有两个准入条件**：来源可信（用户明确陈述或工具返回的结构化字段）、信息足够去冲突（主体、属性、值至少齐）。模型推断默认不写。
- **工作记忆随手写**，但要有硬上限和淘汰规则（LRU + TTL + 作用域清理）。
- **程序性记忆不能一次成功就固化**：同类任务连续成功 N 次才升级成 playbook，否则一次偶然成功会固化成规则，后面极难改。
- **删除也有时机**：用户要求删立刻删；TTL 到期时语义记忆降权、情景记忆归档而不是抹掉——留着是为了能解释历史决策。

## 三、为什么这样设计：取舍与失效条件

### 3.1 拆成四份的代价

四类记忆意味着四次写入决策、四处一致性维护、四种调试路径，团队小、任务单一时收益为零。

不用拆：单 Agent、单任务、上下文装得下全部状态、没有跨会话需求——一个 state 对象加一份历史列表就够。

必须拆：有跨会话记忆；多个角色共享同一份事实；上下文装不下；记忆内容要可审计。

### 3.2 每类记忆"什么时候不该用"

工作记忆不要当数据库用。跨请求存活的东西放工作记忆会串味——A 用户的中间结果出现在 B 用户对话里，这是安全事故。作用域必须绑定 user_id / session_id。

情景记忆不要建在一次性应用里。"问一次就走"的场景（单文档问答、翻译、格式转换）建它是净亏：多一次写入延迟，多一份日志，还污染检索。这类任务的"记忆"应该是任务级变量。

语义记忆不要缓存强时效数据。库存、价格、订单状态、余额、权限都是快变量，记忆副本必然过期，而且过期是静默的——Agent 会拿三个月前的价格去报价，用户看到的是一个自信的错数字。判据很简单：只放慢变量，快变量实时查源系统。语义记忆也不是权限系统，"我可以看这个库"是陈述不是授权。

程序性记忆不要装易变的接口细节。第三方库改了个参数名，固化的 playbook 会持续给出错误步骤，而且因为它被高频注入上下文，错误放大速度远超普通文本。规则是：playbook 装稳定的工作流与判断标准，API 细节留在工具 schema 里。也别写成散文：几十条自然语言步骤塞进系统提示，模型对中后段步骤的遵循率会明显下降，出问题定位不到是哪一步坏了。

### 3.3 典型失效模式

- **类型混淆**：同一条信息既作语义事实写入又被裹进情景摘要，改一处忘另一处，两个库开始矛盾。
- **检索没有类型通道**：不知道要什么类型只能按相似度全捞，无关事件挤掉真正有用的语义事实。
- **冲突静默覆盖**：新值直接盖旧值不留仲裁记录，或新旧都留着让模型自己挑。
- **记忆膨胀**：情景记忆只增不减，检索质量随时间衰减，top-k 被同一批老事件占满。
- **噪声固化**：错误的记忆被检索进上下文，模型围绕它继续推理，错误被放大并写回记忆库，自我强化。这是最危险的失效，因为错误不再是偶发。

### 3.4 一条经验判断

如果你说不出这条信息属于哪一类，多半它不该进记忆系统：要么它是一次性的中间量，要么它应该实时查源数据。这条判断比架构图更能防止过度设计。

## 四、动手：最小可实现

下面这个实现覆盖四类记忆的完整链路：写、读、检索、遗忘，外加写入时机的示范。mock 模式用规则抽事实，不需要任何 API Key；加 `--live` 切到真实模型。

依赖：Python 3.9+，只用标准库。

代码同时放在 `code/ch26-memory-taxonomy/`，其中 `main.py` 可直接运行（`python main.py` 走 mock，`--live` 切真实模型），README 里列了完整的运行命令、输出解读与局限说明。

```python
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
```

跑起来是这样：

```
$ python main.py
========== 任务完成 -> 写入时刻 ==========
语义记忆写入 预算 = 8万元 -> created
语义记忆写入 上线时间 = 9月30日 -> created
语义记忆写入 偏好 = 简洁的方案 -> created
语义记忆写入 身份 = 采购 -> created
清理工作记忆作用域: ['working:意图', 'working:报价结果']
...
```

加 `--live` 时走 OpenAI 兼容的 chat/completions 接口做事实抽取，模型名用 `OPENAI_MODEL`、地址用 `OPENAI_BASE_URL` 指定；抽取失败自动回退 mock。

演示里埋了四种典型情况：模型推断写入语义记忆被拒绝；用户改口后高可信来源成功覆盖并留下版本号；程序性流程连续成功三次才升级为 active；显式删除后检索结果立刻消失。另有一条过了 TTL 的"限时 8 折"被自动降权——这类快变量本来就不该进语义记忆。

生产化时只需替换两处：事实抽取换成真实模型，词面打分换成向量或混合检索，四个动词的接口不用动。

## 五、开源实现对照

对照几类实现分别把哪类记忆做成了什么形态。

- **Letta（MemGPT）** — https://github.com/letta-ai/letta ：记忆是 Agent 通过工具自主读写的区块；代价是记忆策略变成模型行为的一部分，难以回归测试。
- **Mem0** — https://github.com/mem0ai/mem0 ：典型的语义记忆层，从对话抽取事实并在变化时更新或失效旧值；缓存价格和库存会踩 3.2 那条坑。
- **LangMem** — https://github.com/langchain-ai/langmem ：给 LangChain 提供长期记忆的写入与检索工具，并直接按语义 / 情景 / 程序三类划分——本卷里与本章坐标系最贴近的一个。
- **Graphiti** — https://github.com/getzep/graphiti ：增量构建带时间有效期的实体关系图谱，边上带事件时间与写入时间两套时间轴，也是情景记忆从 JSONL 升级到可查询结构的典型路径。
- **Zep** — https://github.com/getzep/zep ：官方仓库现在以示例与集成代码为主，落地形态要看它的商业产品。
- **Cognee** — https://github.com/topoteretes/cognee ：用小模型把原始数据加工成可检索的知识图谱记忆，思路是"检索质量问题先在写入端解决"。
- **LangGraph** — https://github.com/langchain-ai/langgraph ：显式状态图加检查点（checkpoint），工作记忆是被显式管理的 state。
- **OpenAI Agents SDK** — https://github.com/openai/openai-agents-python ：session 机制对应会话级工作记忆。
- **CrewAI** — https://github.com/crewAIInc/crewAI ：早期把记忆拆成短期、长期、实体三块，现在收敛成一个 `Memory` 类，记忆按 scope 树组织，召回时把语义相似度、新鲜度与重要度混合打分。
- **LangChain** — https://github.com/langchain-ai/langchain ：历史消息管理是最朴素的工作记忆实现。
- **LlamaIndex** — https://github.com/run-llama/llama_index ：Memory 抽象更接近语义记忆接口。
- **RAGFlow** — https://github.com/infiniflow/ragflow ：把上下文质量问题在预处理端解决。

规律很清楚：工作记忆由框架状态承担，情景与语义记忆由独立记忆层承担，程序性记忆最常做成工具 schema 或知识库。

## 六、坑与检查清单

上线前逐条打勾。

**分类与写入**

- [ ] 每条记忆都能归入四类之一；归不进去的，先想它是不是该实时查源
- [ ] 写入携带 type / source / ts / scope 四个字段，缺一不可
- [ ] 来源分级明确：用户陈述 > 工具返回 > 模型推断，模型推断不进语义记忆
- [ ] 情景记忆在任务完成点写，不在每轮写
- [ ] 程序性记忆需连续成功 N 次才升级
- [ ] 冲突写入有仲裁与留痕，不静默覆盖

**生命周期与遗忘**

- [ ] 工作记忆有硬上限和淘汰规则，任务结束显式清理作用域
- [ ] 工作记忆作用域绑定 user_id / session_id，跨会话不串味
- [ ] 语义记忆有有效期或版本，冲突时旧值可追溯
- [ ] 情景记忆有保留期与归档策略，不只增不减
- [ ] 实现了显式删除：用户要求忘掉某条信息能删干净
- [ ] 强时效数据（价格、库存、订单、余额）不进语义记忆缓存

**检索与使用**

- [ ] 检索有类型通道，按类型分别取 top-k，不全库按相似度捞
- [ ] 检索结果带来源与时间戳，能区分"用户说的"和"模型猜的"
- [ ] 单次注入的记忆有总量上限，宁可少而准
- [ ] 程序性记忆按触发词命中后注入指令区，不整库倒进提示词
- [ ] 写入与读取同一入口，能打印出"这次用了哪几条记忆"

**可观测**

- [ ] 每次调用能导出当轮命中的记忆清单，便于复盘
- [ ] 有指标：写入量、命中量、命中后被模型忽略的比例
- [ ] 有一条回归用例：改写记忆后，行为确实随之改变

## 七、小结与延伸阅读

记忆分类的价值不在于像人脑，而在于把一类东西塞进一类容器后，你终于能分别回答四个问题：存在哪、活多久、什么时候写、冲突了怎么办。四类记忆不是四套框架，是四个需要独立回答的工程问题。

最容易犯的错是反过来：先搭一个向量库，再把所有东西往里塞。正确顺序是先判断信息类型再决定存储形态；判断下来属于"快变量"的需求，答案就是"别存，去查"。

延伸阅读：

- Letta（MemGPT）: https://github.com/letta-ai/letta
- Mem0: https://github.com/mem0ai/mem0
- LangMem: https://github.com/langchain-ai/langmem
- Graphiti: https://github.com/getzep/graphiti
- LangGraph: https://github.com/langchain-ai/langgraph
- OpenAI Agents SDK: https://github.com/openai/openai-agents-python
- MemGPT 论文（Letta 前身的工作）: https://arxiv.org/abs/2310.08560
- Tulving, E. (1972). Episodic and semantic memory. In *Organization of Memory*.
- Baddeley, A. D., & Hitch, G. (1974). Working memory. In *The Psychology of Learning and Motivation*.
- Squire, L. R. (2004). Memory systems of the brain: a brief history and current perspective. *Neurobiology of Learning, Memory, and Cognition*.
