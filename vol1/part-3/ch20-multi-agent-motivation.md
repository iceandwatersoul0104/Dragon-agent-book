# 第 20 章 多 Agent 的动机、分工理论与何时不该上多 Agent

## 一、本章要解决的问题

去年春天我接手一个"线上告警自动定位"的小工具。需求很简单：告警进来，抓最近 30 分钟的相关日志，判断是哪个服务出的问题，输出三句话结论。一个模型一次调用就能干完。

我当时觉得"这活儿值得上多 Agent"，于是拆成三个 Agent：检索 Agent 拉日志、诊断 Agent 判断、写稿 Agent 组织语言，外加一个 Supervisor 派单和汇总。上线第一天就出两个问题。

一是慢。单体一次调用首字延迟 3 秒；拆完是 4 次模型调用（检索、诊断、写稿、汇总），延迟到 11 秒。

二是贵，而且错。检索 Agent 返回的是它看过 200 行日志之后的摘要，诊断 Agent 拿到的只有摘要。那行关键的 WARN 在摘要时被归并掉了，结论指向错的根因——日志明明在上下文里，却因为被压缩过一次而丢了。

那天晚上我把三个 Agent 合回一个，prompt 重写三行，慢和丢信息一起消失。

这件事之后我立了条规矩：**拆分不是默认动作，它得先赢过一张单子。**

一份 200 行日志、一次调用就能干完的活，凭什么拆成 4 次调用？收益到底从哪来，代价又是谁在付？把这两笔账摆开，才谈得上"什么时候该拆"这个工程问题。

## 二、原理：机制拆解

### 2.1 四种真实收益来源

多 Agent 不是"人多力量大"的直觉，它有四个能算账的收益来源：

| 收益来源 | 收益形态 | 成立条件 | 常见误判 |
| --- | --- | --- | --- |
| 并行探索（parallel exploration） | 墙钟时间 Σtᵢ → max(tᵢ) | 子问题互不依赖 | 把有依赖的步骤当成可并行 |
| 上下文隔离（context isolation） | 主上下文只留结论 | 噪声大、结论小 | 以为总 token 也变少了 |
| 能力异构（heterogeneous capability） | 不同模型/工具各司其职 | 单模型做不到或太贵 | 换个 prompt 就叫异构 |
| 权限与信任边界隔离（trust boundary isolation） | 不可信内容进不了主上下文 | 输入不可信 | 该用沙箱工具，却拆成多个 Agent |

这张表是全章的骨架，判据、代码、清单都挂在这四行上。四条共同点是：**收益都是可测量的**，不是"看起来更专业"。

```
                  ┌──────────── 主 Agent（持有最终上下文）────────────┐
                  │  只有：任务描述 + 各子 Agent 的结论（几百 token）   │
                  └────▲───────────────────────────────┬─────────────┘
                       │ 结论（结构化、有长度上限）      │ 派单
       ┌───────────────┼───────────────────────────────┼────────────────┐
  ┌────▼─────┐   ┌────▼─────┐                 ┌───────▼───────┐   ┌─────▼─────┐
  │ 子Agent A │   │ 子Agent B │                 │ 子Agent C      │   │ 子Agent D │
  │ 通用模型  │   │ 通用模型  │                 │ 沙箱权限       │   │ 确定性工具│
  │ 独立 32k  │   │ 独立 32k  │                 │ 独立 32k       │   │ 独立 32k  │
  └───────────┘   └───────────┘                 └────────────────┘   └───────────┘
    ①并行探索        ②上下文隔离                   ④信任边界隔离        ③能力异构
```

### 2.2 上下文隔离的定量视角

一行应用日志（时间戳 + 级别 + 服务名 + 一句话，偶尔带一段堆栈）大致 20–40 token；三层堆栈的异常行能到 60–100 token。按 25 token/行估，200 行日志约 5000 token；下面示例脚本用同一口径跑出来是 4354 token（中文按字、其余按 3.5 字符/token 估算），如果 200 行里有一半带堆栈，总量会落到 12000–20000 token。

5000 token 塞进 32k 窗口占 16%，塞进 128k 窗口占 4%。光看窗口占用，好像没什么大不了。真正的问题不是塞不下，是**注意力被稀释**：

- 模型要在 5000 token 里捞出那 2–5 行真正相关的 ERROR，其余 4995 行都是噪声。长上下文里"完整但无关"的材料本身就会把结论带偏。
- 每轮工具调用都要把历史重新喂一遍。同一份日志被读 3 遍，是 15000 token 的输入开销，不是 5000。
- 输出会被日志措辞污染：日志里写 timeout，结论就容易写成"超时问题"，而真实根因是连接池耗尽。

对比两种做法（数字来自配套脚本的实测）：

```
单体做法：上下文随材料线性膨胀
┌───────────────── 主 Agent 上下文 ─────────────────┐
│ system 40 │ 任务 25 │ 日志 4354 │ 输出 14           │
│                 合计 4433 token                        │
└───────────────────────────────────────────────────────┘

隔离做法：主上下文只留结论
┌──── 主 Agent 上下文 ────┐   ┌── 子 Agent 上下文（独立 32k）──┐
│ system 24 │ 任务+诊断 50 │   │ system 40 │ 任务 25 │ 日志 4354  │
│   合计 133 token          │   │             → 输出 30            │
└───────────────────────────┘   └─────────────────────────────────┘
   主上下文降到 1/33                 噪声关在门里，不进下一步
```

关键点：**子 Agent 的输入 token 并没消失**，还是那 4354。消失的是"噪声跟着主 Agent 进入下游每一步"。如果后面还要再推理 3 轮，隔离的节省会乘以 3——这才是隔离的定量收益。

### 2.3 成本模型

把账写成公式。设 Pᵢ 为第 i 个 Agent 的输入 token（系统提示 + 任务 + 上下文 + 上游结论），Oᵢ 为输出 token，Mᵢⱼ 为 i 与 j 之间传递的消息 token，rᵢ 为第 i 个 Agent 的平均重试次数：

```
C_单 = P₀ + O₀
C_多 = Σᵢ(Pᵢ + Oᵢ) + Σ_{i→j} Mᵢⱼ + Σᵢ rᵢ·(Pᵢ + Oᵢ)
```

两笔结构性成本要先记住。

**协调税。** 每个子 Agent 都要重复一遍系统提示和任务描述。基础提示按 600 token 算，拆成 5 个子 Agent，光重复就是 3000 token 的固定开销。如果任务本身的信息量只有 2000 token，这笔税比干活还贵——这条直接决定了"小任务别拆"。

**结论税。** 子 Agent 的结论如果不加约束地写，500 token 的日志摘要可能回传 800 token 的散文，每次转述都在放大。压住它的办法是规定返回结构（字段固定、长度上限），而这条投入每个子 Agent 都要付一遍。

还有一笔常被漏掉的：**墙钟时间**。串行 N 个 Agent 就是 N 倍延迟，再加派单与汇总。延迟敏感的场景里，多 Agent 的时间账常常比 token 账更致命。

## 三、为什么这样设计：取舍与失效条件

### 3.1 四种负收益

- **协调开销（coordination overhead）**：派单、等待、汇总、冲突裁决。常见做法是加一个 Supervisor 或路由 Agent，它自己就是一次额外模型调用，外加一份要长期维护的路由规则。
- **错误传播（error propagation）**：A 的错误结论进入 B 的上下文，B 基于错误继续推理，还能写得更自信。上游错一次，下游把错误放大，还附赠一段看起来很专业的推理过程。
- **不可观测（poor observability）**：单体出问题，看一眼 trace 就知道哪一步偏了。多 Agent 出问题，你面对的是 N 条轨迹、一个汇总结论，和一份没人维护的日志。
- **成本翻倍（cost multiplier）**：不只是 token，还有延迟（串行 N 倍）、费用（本卷实测：同一个任务换上下文策略，token 相差 2.9–4.0 倍，见第 21、23 章），以及维护编排代码的人力。

### 3.2 八道判断题

这八个场景都来自真实项目，答案不是"看情况"：

| # | 场景 | 选择 | 理由 |
| --- | --- | --- | --- |
| 1 | 从 200 行报错日志定位根因，写 3 句结论 | 流水线 | 阶段清晰（检索→判断→成文），但没必要并行 |
| 2 | 给一个 800 行模块的 12 个函数补测试 | 多 Agent | 12 个函数互不依赖，可并行，墙钟收益明显 |
| 3 | 每天早上把固定看板的三个数字写进日报 | 单 Agent | 流程固定、上下文固定、高频调用，协调成本大于收益 |
| 4 | 审 6 份互不相关的合规合同，各出风险清单 | 多 Agent | 低耦合 + 可并行 + 上下文隔离，三项收益全中 |
| 5 | 写六章术语必须一致的技术书 | 单 Agent | 全局一致性要求高，拆开必然术语漂移 |
| 6 | 线上告警，30 秒内判断要不要回滚 | 单 Agent | 延迟预算太紧，派单加汇总就吃掉大半 |
| 7 | 从 500 个工单里筛出 30 条登录失败并归类 | 流水线 | 先用规则粗筛再让模型精读，避免 500 条全进上下文 |
| 8 | 让产品/开发/测试/运维四个角色辩论方案 | 多 Agent | 多样性本身就是目的，这笔钱是明账 |

第 7 题补一句：如果规则粗筛不靠谱，退化方案才是并行多 Agent——先把便宜的确定性手段用尽。

还有一类"看着该拆却不该拆"的：从用户上传的 PDF 里提取脚本、在沙箱里跑。它确实需要信任隔离，但不需要多 Agent——**信任边界是工具属性，不是拓扑属性**。一个带沙箱工具的单 Agent 就够了（沙箱见第 31 章）。

四条红线，碰到就别拆：

1. 子任务之间必须共享同一份大上下文才能推进——拆了必然反复传递同一份东西。
2. 任务只调用一次、结果直接给用户看——单体在成本和延迟上都是赢的。
3. 调用频率高到协调开销肉眼可见（每天上千次）——先做缓存和模板化。
4. 你还没跑过单 Agent 的效果基线——没有基线就没有判断依据。

### 3.3 最小可行拆分原则

1. **只拆到能被独立验收的粒度。** 每个子 Agent 必须有单独可测的产物（一份候选列表、一个补丁、一条诊断）。否则你既不知道它做没做对，出错时也定位不到。
2. **先拆上下文，再拆决策。** 上下文隔离几乎总是低风险高收益；把决策权拆开（谁说了算）会立刻引入冲突裁决和错误传播。先做第一种。
3. **一次只引入一个变量。** 同时换模型、改 prompt、加 Agent，你永远不知道收益来自哪儿。
4. **每个子 Agent 的返回必须有结构约束**（字段固定、长度上限）。没有结构约束，token 账会在第二次迭代就失控。
5. **保留不拆的那条路径。** 单 Agent 版本要留着，用开关切换，跑同一批 case 比 token、延迟、正确率三个指标。没有这套东西，拆分就永远靠感觉。

## 四、动手：最小可实现

本章的示例代码内嵌在本节（没有单独的 code/ 目录），自包含、可以直接跑。保存为 `split_advisor.py`：

**依赖**：Python 3.9+，零第三方包。

**运行**：

```bash
python split_advisor.py          # mock 模式，不需要 API Key、不联网
python split_advisor.py --logs 500
python split_advisor.py --live   # 切真实模型
```

脚本干两件事：一是给任务画像打分，给出单 Agent / 流水线 / 多 Agent 的建议与理由；二是用同一份日志对比单体与三段流水线（检索→诊断→成文）的 token 账。

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch20 多 Agent 的动机与拆分判据 —— 最小可实现，纯标准库，无需 API Key 即可跑通。

演示两件事：
  1) 拆分判据：给任务画像打分，输出 单 Agent / 流水线 / 多 Agent 的建议与理由；
  2) 成本对照：同一份 200 行日志，单 Agent 一次读完 vs 三段流水线（检索→诊断→成文），
     对比总 token、主上下文 token 与耗时。

用法：
    python split_advisor.py
    python split_advisor.py --logs 500
    python split_advisor.py --live

--live 所需环境变量：
    LLM_API_KEY    必填，模型服务商的 API Key
    LLM_BASE_URL   可选，默认 https://api.openai.com/v1
    LLM_MODEL      可选，默认 gpt-4o-mini
端点需兼容 `POST {base}/chat/completions`：body 含 model/messages，
返回 choices[0].message.content。换供应商时只改 base_url 与 model。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List

# --------------------------------------------------------------------------
# 1. token 计量
# --------------------------------------------------------------------------


def approx_tokens(text: str) -> int:
    """粗估 token 数：中文按字计，其余按 ~3.5 字符/token。

    服务端返回 usage 时以 usage 为准，这里只用于 mock 与缺 usage 时的估算。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + int((len(text) - cjk) / 3.5) + 1


@dataclass
class Call:
    role: str
    in_tokens: int
    out_tokens: int

    @property
    def total(self) -> int:
        return self.in_tokens + self.out_tokens


class TokenMeter:
    """记录每一次调用的 token。

    main_context 的定义：最后一次调用所持有的输入 + 输出。
    对单体来说它就是全部，对流水线来说它就是最终那一环看到的东西。
    """

    def __init__(self) -> None:
        self.calls: List[Call] = []

    def reset(self) -> None:
        self.calls.clear()

    def add(self, role: str, in_tokens: int, out_tokens: int) -> None:
        self.calls.append(Call(role, in_tokens, out_tokens))

    @property
    def total(self) -> int:
        return sum(c.total for c in self.calls)

    @property
    def main_context(self) -> int:
        return self.calls[-1].total if self.calls else 0


# --------------------------------------------------------------------------
# 2. 模型接入：默认 mock，--live 切真实
# --------------------------------------------------------------------------

MOCK_REPLIES = {
    "sre": "候选行 137/138：connection pool exhausted after 5s，active=200 waiters=180。",
    "retriever": "137: connection pool exhausted after 5s active=200 waiters=180\n"
                 "138: retrying request to db-primary, backoff=200ms",
    "diagnoser": "根因是连接池耗尽：db-primary 延迟触发重试，200 个连接被占满，新请求排队。",
    "writer": "order-svc 变慢的根因是连接池耗尽——db-primary 延迟引发重试，"
              "200 个活跃连接没有释放，新请求只能排队等连接。",
}


class MockLLM:
    """按角色返回固定回复。回复是 canned 的，但 token 计量照常累计。"""

    name = "mock"

    def __init__(self) -> None:
        self.meter = TokenMeter()

    def chat(self, role: str, system: str, user: str) -> str:
        reply = MOCK_REPLIES.get(role, "[%s] mock 回复" % role)
        self.meter.add(role, approx_tokens(system) + approx_tokens(user), approx_tokens(reply))
        return reply


class LiveLLM:
    """真实模型：OpenAI 兼容的 chat completions。"""

    name = "live"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 60) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.meter = TokenMeter()

    def chat(self, role: str, system: str, user: str) -> str:
        body = {"model": self.model, "temperature": 0.0,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.api_key}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError("HTTP %s: %s" % (exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("cannot reach %s: %s" % (self.url, exc.reason)) from exc
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        self.meter.add(role,
                       usage.get("prompt_tokens", approx_tokens(system + user)),
                       usage.get("completion_tokens", approx_tokens(text)))
        return text


# --------------------------------------------------------------------------
# 3. 拆分判据：规则是确定的，不交给模型判断
# --------------------------------------------------------------------------


@dataclass
class TaskProfile:
    parallelizable: int = 0     # 可独立并行的子问题数，0-3
    context_pressure: int = 0   # 输入上下文量级，0-3
    skill_gap: int = 0          # 是否需要不同模型/工具，0-3
    trust_gap: int = 0          # 是否跨信任边界，0-3
    coupling: int = 0           # 子任务耦合度，0-3，越大越紧
    latency_sensitive: bool = False  # 延迟预算是否紧张


def advise(p: TaskProfile) -> Dict[str, str]:
    """返回建议拓扑（single / pipeline / multi）与理由。"""
    gains: List[str] = []
    if p.skill_gap >= 2:
        gains.append("需要不同模型或工具，单 Agent 覆盖不了")
    if p.trust_gap >= 2:
        gains.append("跨信任边界，需要沙箱隔离")
    if p.parallelizable >= 2 and p.coupling <= 1:
        gains.append("%d 个子问题可并行，墙钟时间能省下来" % p.parallelizable)
    if p.context_pressure >= 2:
        gains.append("上下文压力大，需要把噪声关进子 Agent")

    if p.coupling >= 3:
        return {"topology": "single", "reason": "子任务耦合过紧，协调开销大于收益"}
    if p.latency_sensitive:
        return {"topology": "single", "reason": "延迟敏感，拆分带来的额外调用会吃掉时间预算"}
    if not gains:
        return {"topology": "single", "reason": "无异构、无信任边界、上下文不拥挤"}
    if p.context_pressure >= 2 and p.parallelizable <= 1:
        return {"topology": "pipeline", "reason": "；".join(gains)}
    return {"topology": "multi", "reason": "；".join(gains)}


# 8 个真实场景，结论与正文第 3.2 节的判断题一一对应
SAMPLES = [
    ("从 200 行日志定位根因", TaskProfile(context_pressure=2)),
    ("给 12 个函数补单测", TaskProfile(parallelizable=3, context_pressure=2)),
    ("每天写固定格式日报", TaskProfile()),
    ("审 6 份互不相关的合同", TaskProfile(parallelizable=3, context_pressure=3)),
    ("写术语必须一致的技术书", TaskProfile(context_pressure=2, coupling=3)),
    ("30 秒内判断要不要回滚", TaskProfile(context_pressure=2, latency_sensitive=True)),
    ("500 条工单筛出 30 条并归类", TaskProfile(context_pressure=3, parallelizable=1)),
    ("四个角色辩论一个方案", TaskProfile(parallelizable=2, skill_gap=2, coupling=2)),
]


# --------------------------------------------------------------------------
# 4. 成本对照：同一份日志，单 Agent vs 三段流水线
# --------------------------------------------------------------------------

TASK = "定位 order-svc 昨天下午变慢的根因，输出不超过 120 字。"
SYS_SRE = "你是资深 SRE，只依据给定材料下判断，不臆造材料里没有的事实。"
SYS_WRITER = "你是技术写作助手，把诊断结论压缩成给值班同学看的一段话。"


def make_logs(n: int) -> str:
    """造一份可复现的服务日志：绝大多数正常行，少数几行是真正的信号。"""
    rows = []
    hit = int(n * 0.685)
    for i in range(n):
        level, msg = "INFO", "GET /api/v1/orders/%d 200 in %dms" % (i % 7, 12 + i % 40)
        if i == hit:
            level, msg = "ERROR", "connection pool exhausted after 5s active=200 waiters=180"
        elif i == hit + 1:
            level, msg = "WARN", "retrying request to db-primary, backoff=200ms"
        rows.append("2026-03-11T10:%02d:11.482Z %-5s [order-svc] %s" % (i % 60, level, msg))
    return "\n".join(rows)


@dataclass
class RunResult:
    topology: str
    calls: int
    total_tokens: int
    main_context: int
    seconds: float
    answer: str = ""


def run_single(llm, logs: str) -> RunResult:
    """单体：一次调用把日志全读进去。"""
    llm.meter.reset()
    t0 = time.perf_counter()
    answer = llm.chat("sre", SYS_SRE, "任务：%s\n\n日志：\n%s" % (TASK, logs))
    return RunResult("单 Agent", len(llm.meter.calls), llm.meter.total,
                     llm.meter.main_context, time.perf_counter() - t0, answer)


def run_pipeline(llm, logs: str) -> RunResult:
    """流水线：检索 → 诊断 → 成文，噪声留在子 Agent 的上下文里。"""
    llm.meter.reset()
    t0 = time.perf_counter()
    cands = llm.chat("retriever", SYS_SRE,
                     "从日志里挑出与变慢相关的行，只返回行号和原文，不要解释。\n\n日志：\n%s" % logs)
    diag = llm.chat("diagnoser", SYS_SRE, "任务：%s\n\n候选行：\n%s" % (TASK, cands))
    answer = llm.chat("writer", SYS_WRITER, "任务：%s\n\n诊断结论：\n%s" % (TASK, diag))
    return RunResult("三段流水线", len(llm.meter.calls), llm.meter.total,
                     llm.meter.main_context, time.perf_counter() - t0, answer)


# --------------------------------------------------------------------------
# 5. CLI
# --------------------------------------------------------------------------


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _pad(text: str, width: int) -> str:
    """按显示宽度左对齐补空格：CJK 字符占两列。"""
    w = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - w)


def build_llm(args: argparse.Namespace):
    if not args.live:
        return MockLLM()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "--live 需要环境变量 LLM_API_KEY；"
            "可选 LLM_BASE_URL（默认 https://api.openai.com/v1）与 LLM_MODEL。\n")
        raise SystemExit(2)
    return LiveLLM(args.base_url, api_key, args.model)


def main() -> None:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description="多 Agent 拆分判据与成本对照")
    ap.add_argument("--live", action="store_true", help="切真实模型（需 LLM_API_KEY）")
    ap.add_argument("--logs", type=int, default=200, help="日志行数，默认 200")
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "gpt-4o-mini"))
    args = ap.parse_args()

    llm = build_llm(args)
    logs = make_logs(args.logs)
    print("模型：%s｜日志规模：%d 行，约 %d token\n"
          % (llm.name, args.logs, approx_tokens(logs)))

    results = [run_single(llm, logs), run_pipeline(llm, logs)]
    print("%s %s %s %s %s" % (_pad("方案", 14), _pad("调用", 8),
                               _pad("总token", 12), _pad("主上下文", 14), "耗时"))
    for r in results:
        print("%s %s %s %s %6.3fs"
              % (_pad(r.topology, 14), _pad(str(r.calls), 8),
                 _pad(str(r.total_tokens), 12), _pad(str(r.main_context), 14), r.seconds))
    print("\n主上下文 = 最后一次调用持有的输入 + 输出。"
          "拆分让总 token 上升，让主上下文变干净——值不值，取决于后面还要不要多轮推理。")

    print("\n拆分建议：")
    print("%s %s %s" % (_pad("场景", 30), _pad("建议", 12), "理由"))
    for name, profile in SAMPLES:
        r = advise(profile)
        print("%s %s %s" % (_pad(name, 30), _pad(r["topology"], 12), r["reason"]))


if __name__ == "__main__":
    main()
```

mock 模式输出（`--logs 200`）：

```
模型：mock｜日志规模：200 行，约 4354 token

方案           调用     总token      主上下文       耗时
单 Agent       1        4433         4433            0.000s
三段流水线     3        4697         133             0.000s

主上下文 = 最后一次调用持有的输入 + 输出。
```

三个要注意的地方。

**第一，绝对数字不重要，相对关系才重要。** mock 的 token 是估算值，真实模型以服务端 usage 为准；但"总 token 上升约 6%、主上下文从 4433 降到 133"这个结构是稳的。`--logs 500` 时日志涨到 10868 token，单体总输入 10947、流水线 11211，而流水线的主上下文依然是 133——噪声全部留在了子 Agent 里。

**第二，判据要写成规则，不能交给模型判断。** 模型给的拆分建议听起来永远合理，但它没法向你保证 token 会变多还是变少；规则至少能被单测覆盖，而且能直接对应第 3.2 节那八道题——脚本打印的八条建议与那一节的表格完全一致。

**第三，单体和多 Agent 必须能一键切换。** 生产里保留两条路径，用开关跑同一批 case，比 token、延迟、正确率。这是唯一能把"我觉得多 Agent 更好"变成数据的方式。

`--live` 需要的环境变量与全书其它章节一致：

```bash
export LLM_API_KEY=sk-xxx
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=gpt-4o-mini
python split_advisor.py --live
```

端点需兼容 `POST {base}/chat/completions`：body 含 model/messages，返回 choices[0].message.content。换供应商时只改 base_url 与 model。

## 五、开源实现对照

按 2.1 节那四行收益来源对号入座：

- **AutoGen**（MIT，代码部分为 LICENSE-CODE，仓库根目录 LICENSE 为 CC-BY-4.0，https://github.com/microsoft/autogen）：可对话 Agent 与 GroupChat 是"并行探索 + 对话式分工"最直白的实现，调度器负责选下一个发言者——调度规则本身就是 3.1 节协调开销的主要来源。
- **CrewAI**（MIT，https://github.com/crewAIInc/crewAI）：用角色、任务、流程三层把"分工"写进配置。适合对照 2.1 节第一行：它的流程（Process）默认是串行的，说明框架作者也认为"角色多"不等于"要并行"。
- **MetaGPT**（MIT，https://github.com/geekan/MetaGPT）、**ChatDev**（Apache-2.0，https://github.com/OpenBMB/ChatDev）、**AgentVerse**（Apache-2.0，https://github.com/OpenBMB/AgentVerse）：把软件公司的 SOP 编码成 Agent 流水线，是 3.2 节第 1 题那种"阶段清晰"的极端形态。注意它们共享的是**标准文档**而不是聊天记录，这正是 SOP 流水线躲开协调税的关键手段（见第 22 章）。
- **Qwen-Agent**（Apache-2.0，https://github.com/QwenLM/Qwen-Agent）：把函数调用、MCP、代码解释器与 RAG 收在同一个 Agent 内部，是"能力异构"的另一种解法——不拆 Agent，而是给一个 Agent 挂不同工具。两种解法各自的代价值得对照着看。
- **LangGraph**（MIT，https://github.com/langchain-ai/langgraph）：把控制流显式写成状态图与检查点。谁调用谁、什么时候聚合，都是节点和边而不是隐式约定，协调开销被压进框架里，而不是散落在提示词中（见第 23 章）。
- **Microsoft Agent Framework**（MIT，https://github.com/microsoft/agent-framework）：融合 AutoGen 与 Semantic Kernel，统一 Agent、工作流与部署，可以对照看框架层怎么把上面这些取舍变成配置项。
- **AgentScope**（Apache-2.0，https://github.com/agentscope-ai/agentscope）：面向多 Agent 协作的框架，配 Studio 做可视化调试与追踪，正好补 3.1 节说的"不可观测"这块短板。
- **OpenAI Agents SDK**（MIT，https://github.com/openai/openai-agents-python）：用 Agent、工具、交接（handoff）、护栏与追踪拼装轻量工作流。交接的语义值得注意：它默认是"把控制权交出去"，而不是"多个 Agent 同时讨论"——前者延迟可控，后者才是第 21 章 GroupChat 的场景。
- **A2A Protocol**（Apache-2.0，https://github.com/a2aproject/A2A）：Linux Foundation 治理的 Agent 间通信协议，含 Agent Card 与 Task。当多 Agent 分处不同进程甚至不同团队时，消息契约与信任边界要单独设计（见第 24 章）。

许可证之外的具体 API 与版本，请以官方仓库为准——这类框架迭代很快，本书只描述它们的架构取向。

## 六、坑与检查清单

拆多 Agent 之前，逐条打勾：

- [ ] 我已经跑通单 Agent 版本，并有至少 20 条真实 case 的基线（token、延迟、正确率）。
- [ ] 我说得出这次拆分命中了哪几种收益（并行 / 隔离 / 异构 / 信任边界），而不是"这样更高级"。
- [ ] 每个子 Agent 有可独立验收的产物，能单独判断它做没做对。
- [ ] 子 Agent 之间的返回是结构化的，字段固定、长度有上限。
- [ ] 保留了不拆的那条路径（开关或配置项），可以随时切回来对比。
- [ ] 我算过协调税：基础提示在子 Agent 数量上重复了多少 token。
- [ ] 端到端延迟预算能容纳多一次派单和一次汇总。
- [ ] 出错时我能看到每条子 Agent 的轨迹，而不是只看到一个汇总结论。
- [ ] 重试策略不会因为多了一层失败点而无限放大调用次数。
- [ ] 子任务之间不需要共享同一份大上下文才能推进。
- [ ] 任务不是高频到协调开销肉眼可见（若是，先做缓存与模板化）。
- [ ] 不可信输入是用沙箱工具挡住的，没有误当成"必须拆 Agent"。
- [ ] 我没有顺手用多 Agent 去掩盖别的毛病（比如顺带重写了日志采集）。

## 七、小结与延伸阅读

多 Agent 不是默认形态，它是一次有代价的拓扑选择。四种真实收益——并行探索、上下文隔离、能力异构、信任边界隔离——每一种都有明确的成立条件；四种负收益——协调开销、错误传播、不可观测、成本翻倍——在条件不满足时立刻兑现。

最实用的一条经验是：**先把噪声关进子 Agent，再考虑把决策权拆开。** 前者低风险、几乎总有收益；后者会立刻带来冲突裁决和错误传播。日常默认答案仍然是单 Agent 或一条流水线，多 Agent 要用数据说服你——就像配套脚本做的那样。

延伸阅读（均为官方仓库地址）：

- AutoGen — https://github.com/microsoft/autogen
- CrewAI — https://github.com/crewAIInc/crewAI
- MetaGPT — https://github.com/geekan/MetaGPT
- ChatDev — https://github.com/OpenBMB/ChatDev
- AgentVerse — https://github.com/OpenBMB/AgentVerse
- Qwen-Agent — https://github.com/QwenLM/Qwen-Agent
- LangGraph — https://github.com/langchain-ai/langgraph
- Microsoft Agent Framework — https://github.com/microsoft/agent-framework
- AgentScope — https://github.com/agentscope-ai/agentscope
- OpenAI Agents SDK — https://github.com/openai/openai-agents-python
- A2A Protocol — https://github.com/a2aproject/A2A
