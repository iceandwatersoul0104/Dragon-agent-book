#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch23 协作拓扑与调度：Supervisor、层级、网状、辩论与黑板 最小可实现
—— 纯标准库，无需 API Key 就能跑通完整回路。

演示内容：
  1) 五种拓扑跑同一个任务，对比控制流、调用数、token 与上下文共享范围
  2) Supervisor：集中调度（一个中心决定下一步）与去中心化网状的差别
  3) 层级：深度与扇出如何决定成本（带公式与实测）
  4) 辩论与黑板：对抗和共享空间什么时候真的有用
  5) 失效注入：同一个角色失败，五种拓扑的失败传播路径各不相同
  6) 选型三维度：任务可分解度、对抗性需求、可验证性

用法：
    python main.py                      # 跑全部演示
    python main.py --fail 分析员        # 注入一次失败，看五种拓扑怎么处理
    python main.py --depth 3 --fanout 3 # 层级成本模型
    python main.py --live               # 切真实模型

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
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

TASK = "从本季度运营摘要里找出三个关键问题，并写成一页给管理层看的结论。"

BRIEF = ("运营摘要：Q3 获客成本环比 +38%；退款率从 4.1% 升到 6.7%；"
         "华东仓平均出库时长从 2.1 天变成 3.4 天；新客首月复购率 22%。")

WORKER_ROLE = {
    "检索员": "从素材里摘出与问题相关的数字与事实，不要解释",
    "分析员": "把事实归因，给出三个关键问题，每个问题一句话",
    "撰写员": "把结论写成给管理层看的一页纸，不要过程",
    "审核员": "逐条核对结论有没有素材支撑，只指出不成立的",
}

MOCK_WORKER = {
    "检索员": "事实：Q3 获客成本环比 +38%；退款率从 4.1% 升到 6.7%；"
              "华东仓平均出库时长 2.1 天变 3.4 天；新客首月复购率 22%。",
    "分析员": "三个关键问题：获客成本上涨来自渠道竞价而不是素材效率；"
              "退款率上升集中在 Q3 新客；出库变慢与促销期备货策略有关。",
    "撰写员": "Q3 的问题不在需求侧，而在获客效率、新客留存与履约能力三处。"
              "建议：按周重分配渠道预算、新客首单权益改造、按周滚动备货。",
    "审核员": "审核通过：三个结论分别对应获客成本、退款率、出库时长三组数字，"
              "均可在素材中找到支撑。",
}

SUPERVISOR_PLAN = [
    {"next": "检索员", "handoff": "请从运营摘要里摘出与问题相关的数字与事实。"},
    {"next": "分析员", "handoff": "事实：获客成本 +38%、退款率 4.1%→6.7%、出库 2.1→3.4 天。"},
    {"next": "撰写员", "handoff": "三个关键问题：获客成本源于渠道竞价；退款率集中在新客；出库慢源于促销备货。"},
    {"next": "审核员", "handoff": "请核对结论有没有素材支撑。"},
]

# 各类调度节点各自的派单计划：中心一个，层级里根与组各一份
MOCK_SUPERVISOR_PLANS = {
    "监督者": SUPERVISOR_PLAN,
    "根调度器": [
        {"next": "资料组调度器", "handoff": "先做资料与分析，再做成稿与审核。"},
        {"next": "成稿组调度器", "handoff": "资料与分析已完成，请写结论并审核。"},
    ],
    "资料组调度器": [
        {"next": "检索员", "handoff": "请从运营摘要里摘出与问题相关的数字与事实。"},
        {"next": "分析员", "handoff": "事实：获客成本 +38%、退款率 4.1%→6.7%、出库 2.1→3.4 天。"},
    ],
    "成稿组调度器": [
        {"next": "撰写员", "handoff": "三个关键问题：获客成本源于渠道竞价；退款率集中在新客；出库慢源于促销备货。"},
        {"next": "审核员", "handoff": "请核对结论有没有素材支撑。"},
    ],
}


# --------------------------------------------------------------------------
# 1. token 计量
# --------------------------------------------------------------------------


def approx_tokens(text: str) -> int:
    """粗估 token 数：中文按字计，其余按 ~3.5 字符/token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + int((len(text) - cjk) / 3.5) + 1


class TokenMeter:
    def __init__(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def reset(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def record_call(self) -> None:
        self.calls += 1

    def add(self, p: int, c: int) -> None:
        self.prompt_tokens += p
        self.completion_tokens += c

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# --------------------------------------------------------------------------
# 2. 模型接入
# --------------------------------------------------------------------------


class AgentFailure(RuntimeError):
    """注入的失败：模拟某个 Agent 调用失败。"""


class MockLLM:
    """确定性 mock：worker 返回固定产出，监督者按固定计划派单。"""

    name = "mock"

    def __init__(self) -> None:
        self.meter = TokenMeter()
        self.state: Dict[str, int] = {}

    def chat(self, role: str, system: str, user: str) -> str:
        self.meter.record_call()
        self.meter.add(approx_tokens(system) + approx_tokens(user), 0)
        if role in MOCK_SUPERVISOR_PLANS:
            plans = MOCK_SUPERVISOR_PLANS[role]
            idx = self.state.get(role, 0)
            self.state[role] = idx + 1
            if idx < len(plans):
                payload = dict(plans[idx])
                payload.setdefault("done", False)
            else:
                payload = {"done": True, "final": "全部环节已完成，结论见上文。"}
            text = json.dumps(payload, ensure_ascii=False)
        elif role == "裁判":
            self.state["judge"] = self.state.get("judge", 0) + 1
            text = ("裁决：采纳正方结论——获客成本上涨来自渠道竞价而非素材效率；"
                    "反方提出的'季节性波动'缺少素材支撑，不采纳。")
        elif role == "正方" or role == "反方":
            self.state[role] = self.state.get(role, 0) + 1
            if role == "正方":
                text = ("立论：获客成本 +38% 主要来自渠道竞价效率下降，而不是素材衰减。"
                        "依据：退款率与复购率同步恶化，说明拉来的量本身质量下降，"
                        "渠道出价上涨推高了单位有效获客成本。")
            else:
                text = ("质疑：季节性波动同样是合理解释。促销期集中投放会抬高竞价，"
                        "出库变慢也会拉长退款周期，两者都会推高退款率；"
                        "在缺少分渠道投放数据前，不该断言是渠道效率问题。")
        elif role in MOCK_WORKER:
            text = MOCK_WORKER[role]
        else:
            text = "[%s] mock 输出" % role
        self.meter.add(0, approx_tokens(text))
        return text


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
        self.meter.record_call()
        body = {"model": self.model, "temperature": 0.2,
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
        self.meter.add(usage.get("prompt_tokens", approx_tokens(system + user)),
                       usage.get("completion_tokens", approx_tokens(text)))
        return text


# --------------------------------------------------------------------------
# 3. 拓扑的公共骨架
# --------------------------------------------------------------------------


@dataclass
class Result:
    name: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    foreign_tokens: int = 0      # 各 Agent 看到的他人内容总量
    answer: str = ""
    ok: bool = True
    failed_agent: str = ""
    failure_path: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Topology:
    """拓扑基类：跑同一个任务，产出可比的指标。"""

    name = ""
    control_flow = ""
    sharing = ""

    def __init__(self, llm, fail_agent: str = "", max_retry: int = 1) -> None:
        self.llm = llm
        self.fail_agent = fail_agent
        self.max_retry = max_retry

    # --- 调用一个 agent，失败注入在这里统一处理 ---

    def _call(self, role: str, foreign: str, instruction: str = "") -> Tuple[str, int]:
        """返回 (产出, 其中他人内容的 token 数)。失败按 AgentFailure 抛出。"""
        if self.fail_agent == role:
            raise AgentFailure("%s 调用失败（注入）" % role)
        system = instruction or ("你是%s。%s" % (role, WORKER_ROLE.get(role, "按要求输出")))
        user = "任务：%s\n素材：%s" % (TASK, BRIEF)
        if foreign:
            user += "\n\n他处传来的内容：\n%s" % foreign
        before_prompt = self.llm.meter.prompt_tokens
        before_completion = self.llm.meter.completion_tokens
        text = self.llm.chat(role, system, user)
        self.last_prompt = self.llm.meter.prompt_tokens - before_prompt
        self.last_completion = self.llm.meter.completion_tokens - before_completion
        return text, approx_tokens(foreign)

    # --- 各拓扑实现 run() ---

    def run(self) -> Result:
        raise NotImplementedError


# --------------------------------------------------------------------------
# 4. 五种拓扑
# --------------------------------------------------------------------------


class SupervisorTopology(Topology):
    """集中调度：中心节点决定下一步交给谁，并把上下文压缩后下发。"""

    name = "Supervisor（集中调度）"
    control_flow = "中心决定下一步 → 单个执行者 → 回到中心"
    sharing = "中心看全部；执行者只看中心压缩后的一段"

    def run(self) -> Result:
        res = Result(self.name)
        accumulated: List[str] = []
        outputs: Dict[str, str] = {}
        step = 0
        while True:
            # 中心：看全部累积产出，决定下一步
            foreign = "\n".join(accumulated)
            try:
                raw, f_tokens = self._call(
                    "监督者", foreign,
                    "你是调度者。看已有产出，决定下一个该谁做。"
                    '输出 JSON：{"next": "<角色名>", "handoff": "<给该角色的简短交接>", "done": false}；'
                    '全部做完后输出 {"done": true, "final": "<最终结论>"}。'
                    '可选角色：检索员、分析员、撰写员、审核员。')
            except AgentFailure as exc:
                res.ok = False
                res.failed_agent = "监督者"
                res.failure_path = "中心节点失败 → 整条链中止"
                res.notes.append(str(exc))
                break
            res.foreign_tokens += f_tokens
            payload = self._parse(raw)
            if payload.get("done"):
                res.answer = payload.get("final", "")
                break
            nxt = payload.get("next", "")
            handoff = payload.get("handoff", "")
            if not nxt:
                break
            # 执行者：只看到中心压缩后的交接
            try:
                out, f_tokens = self._call(nxt, handoff)
            except AgentFailure as exc:
                res.ok = False
                res.failed_agent = nxt
                res.failure_path = "调度层捕获 → 重试 %d 次 → 中止（已产出部分保留）" % self.max_retry
                res.notes.append(str(exc))
                retried = False
                for _ in range(self.max_retry):
                    try:
                        out, f_tokens = self._call(nxt, handoff)
                        retried = True
                        break
                    except AgentFailure:
                        continue
                if not retried:
                    break
            res.foreign_tokens += f_tokens
            outputs[nxt] = out
            accumulated.append("%s：%s" % (nxt, out))
            step += 1
            if step > 12:
                break
        res.calls = self.llm.meter.calls
        res.prompt_tokens = self.llm.meter.prompt_tokens
        res.completion_tokens = self.llm.meter.completion_tokens
        if not res.answer:
            res.answer = "\n".join(accumulated)
        return res

    @staticmethod
    def _parse(raw: str) -> Dict[str, Any]:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                out = json.loads(raw[start:end + 1])
                if isinstance(out, dict):
                    return out
            except json.JSONDecodeError:
                pass
        return {"done": True, "final": raw}


class HierarchicalTopology(Topology):
    """层级：根调度器 → 组调度器 → 执行者。深度与扇出决定成本。"""

    name = "层级（Hierarchical）"
    control_flow = "根调度器 → 组调度器 → 执行者，两级扇出"
    sharing = "根看各组结论；组看本组产出；执行者只看本组交接"

    SUPERVISOR_INSTRUCTION = (
        "你是调度者，决定下一步交给谁。"
        '输出 JSON：{"next": "<下一节点名>", "handoff": "<交接内容>", "done": false}；'
        '全部做完后输出 {"done": true, "final": "<结论>"}。')

    def run(self) -> Result:
        res = Result(self.name)
        group_summaries: List[str] = []
        try:
            while True:
                raw, f_tokens = self._call("根调度器", "\n".join(group_summaries),
                                           self.SUPERVISOR_INSTRUCTION)
                res.foreign_tokens += f_tokens
                payload = self._parse(raw)
                if payload.get("done"):
                    res.answer = payload.get("final", "\n".join(group_summaries))
                    break
                group = payload.get("next", "")
                if not group:
                    break
                group_summaries.append(self._run_group(group, res))
                if not res.ok:
                    break
        except AgentFailure as exc:
            res.ok = False
            res.failed_agent = "根调度器"
            res.failure_path = "根调度器失败 → 整链中止"
            res.notes.append(str(exc))
        res.calls = self.llm.meter.calls
        res.prompt_tokens = self.llm.meter.prompt_tokens
        res.completion_tokens = self.llm.meter.completion_tokens
        return res

    def _run_group(self, group: str, res: Result) -> str:
        """组调度器决定组内成员的执行顺序。"""
        produced: List[str] = []
        while True:
            try:
                raw, f_tokens = self._call(group, "\n".join(produced), self.SUPERVISOR_INSTRUCTION)
            except AgentFailure as exc:
                res.ok = False
                res.failed_agent = group
                res.failure_path = "组调度器失败 → 本组中止 → 整链中止"
                res.notes.append(str(exc))
                return "%s：%s" % (group, "；".join(produced))
            res.foreign_tokens += f_tokens
            payload = self._parse(raw)
            if payload.get("done"):
                break
            member = payload.get("next", "")
            if not member:
                break
            try:
                out, f_tokens = self._call(member, payload.get("handoff", ""))
            except AgentFailure as exc:
                res.ok = False
                res.failed_agent = member
                res.failure_path = "组调度器捕获 → 本组中止 → 整链中止（已产出部分保留）"
                res.notes.append(str(exc))
                return "%s：%s" % (group, "；".join(produced))
            res.foreign_tokens += f_tokens
            produced.append("%s：%s" % (member, out))
        return "%s → %s" % (group, "；".join(produced))

    @staticmethod
    def _parse(raw: str) -> Dict[str, Any]:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                out = json.loads(raw[start:end + 1])
                if isinstance(out, dict):
                    return out
            except json.JSONDecodeError:
                pass
        return {"done": True, "final": raw}


class MeshTopology(Topology):
    """网状：没有中心，Agent 之间直接投递消息，谁收到谁回。"""

    name = "网状（Mesh，去中心化）"
    control_flow = "消息总线直接投递，没有中心节点"
    sharing = "只看到寄给自己的消息；信息靠转发扩散"

    ROUTE = ["检索员", "分析员", "撰写员", "审核员", "撰写员"]

    def run(self) -> Result:
        res = Result(self.name)
        inbox: Dict[str, List[str]] = {}
        last = ""
        for sender in self.MESH_SENDER:
            target = self.MESH_SENDER[(self.MESH_SENDER.index(sender) + 1) % len(self.MESH_SENDER)]
            foreign = "\n".join(inbox.get(sender, []))
            try:
                out, f_tokens = self._call(sender, foreign)
            except AgentFailure as exc:
                res.ok = True          # 网状拓扑不会因为一个节点挂掉而中止
                res.failed_agent = sender
                res.failure_path = "失败被吞 → 其余节点照常跑 → 结论缺一块，无人察觉"
                res.notes.append(str(exc))
                inbox.setdefault(target, []).append("[%s 失败，未提供内容]" % sender)
                continue
            res.foreign_tokens += f_tokens
            inbox.setdefault(target, []).append("%s：%s" % (sender, out))
            last = out
        res.answer = last or "\n".join("".join(v) for v in inbox.values())
        res.calls = self.llm.meter.calls
        res.prompt_tokens = self.llm.meter.prompt_tokens
        res.completion_tokens = self.llm.meter.completion_tokens
        return res

    MESH_SENDER = ["检索员", "分析员", "撰写员", "审核员"]


class DebateTopology(Topology):
    """辩论：正反双方论证，裁判裁决。"""

    name = "辩论（Debate）"
    control_flow = "正反各说 → 交叉反驳 → 裁判裁决"
    sharing = "辩手看到对方上一轮；裁判看到全部"

    def run(self) -> Result:
        res = Result(self.name)
        args: Dict[str, List[str]] = {"正方": [], "反方": []}
        for rnd in range(2):
            for side in ("正方", "反方"):
                other = "反方" if side == "正方" else "正方"
                foreign = args[other][-1] if args[other] else ""
                try:
                    out, f_tokens = self._call(side, foreign,
                                               "你是辩论的%s，一轮发言只讲一个论点。"
                                               % ("正方" if side == "正方" else "反方"))
                except AgentFailure as exc:
                    res.ok = True
                    res.failed_agent = side
                    res.failure_path = "一方缺阵 → 裁判只看单边论证 → 裁决仍会给出，但依据不完整"
                    res.notes.append(str(exc))
                    continue
                res.foreign_tokens += f_tokens
                args[side].append(out)
        try:
            verdict, f_tokens = self._call("裁判", "\n".join(args["正方"] + args["反方"]),
                                           "你是裁判，听完双方论证后裁决，说明采纳哪一方、为什么。")
            res.foreign_tokens += f_tokens
            res.answer = verdict
        except AgentFailure as exc:
            res.ok = False
            res.failed_agent = "裁判"
            res.failure_path = "裁判失败 → 无裁决 → 只能交出原始论证"
            res.notes.append(str(exc))
            res.answer = "\n".join(args["正方"] + args["反方"])
        res.calls = self.llm.meter.calls
        res.prompt_tokens = self.llm.meter.prompt_tokens
        res.completion_tokens = self.llm.meter.completion_tokens
        return res


class BlackboardTopology(Topology):
    """黑板：所有 Agent 读写同一块共享工作区，不直接对话。"""

    name = "黑板（Blackboard）"
    control_flow = "Agent 轮流入场，读黑板 → 写黑板，直到黑板凑齐"
    sharing = "所有人都看整块黑板（越写越大）"

    REQUIRED = ["facts", "issues", "draft", "checked"]
    WRITER = {"检索员": "facts", "分析员": "issues", "撰写员": "draft", "审核员": "checked"}

    def run(self) -> Result:
        res = Result(self.name)
        board: Dict[str, str] = {}
        for role, key in self.WRITER.items():
            foreign = "\n".join("%s: %s" % (k, v) for k, v in board.items())
            try:
                out, f_tokens = self._call(role, foreign,
                                           "你负责黑板上的 %s 字段，读现有内容后补写这一栏，不要改别人的栏。" % key)
            except AgentFailure as exc:
                res.ok = False
                res.failed_agent = role
                res.failure_path = "黑板缺栏 → 完备性检查不通过 → 达到轮次上限 → 返回部分结果"
                res.notes.append(str(exc))
                break
            res.foreign_tokens += f_tokens
            board[key] = out
        missing = [k for k in self.REQUIRED if k not in board]
        if missing and res.ok:
            res.ok = False
            res.failure_path = "黑板缺栏 %s → 未凑齐" % "、".join(missing)
        res.answer = "\n".join("%s: %s" % (k, v) for k, v in board.items())
        res.calls = self.llm.meter.calls
        res.prompt_tokens = self.llm.meter.prompt_tokens
        res.completion_tokens = self.llm.meter.completion_tokens
        return res


# --------------------------------------------------------------------------
# 5. 演示
# --------------------------------------------------------------------------


def _pad(text: str, width: int) -> str:
    """按显示宽度左对齐补空格：CJK 字符占两列。"""
    w = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - w)


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


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


def all_topologies(llm, fail_agent: str) -> List[Result]:
    results = []
    for cls in (SupervisorTopology, HierarchicalTopology, MeshTopology,
                DebateTopology, BlackboardTopology):
        llm.meter.reset()
        llm.state.clear()
        topo = cls(llm, fail_agent=fail_agent)
        results.append(topo.run())
    return results


def demo_compare(args: argparse.Namespace) -> List[Result]:
    print("=" * 84)
    print("A. 五种拓扑跑同一个任务（健康基线，不注入失败）")
    print("=" * 84)
    llm = build_llm(args)
    results = all_topologies(llm, "")
    print("%s %s %s %s %s %s"
          % (_pad("拓扑", 22), _pad("调用", 7), _pad("输入token", 11),
             _pad("总token", 10), _pad("他人内容", 11), "结果"))
    for r in results:
        print("%s %s %s %s %s %s"
              % (_pad(r.name, 22), _pad(str(r.calls), 7), _pad(str(r.prompt_tokens), 11),
                 _pad(str(r.total_tokens), 10), _pad(str(r.foreign_tokens), 11),
                 "成功" if r.ok else "部分结果"))
    print("\n控制流与共享范围：")
    for cls in (SupervisorTopology, HierarchicalTopology, MeshTopology,
                DebateTopology, BlackboardTopology):
        print("  %-22s %s" % (cls.name, cls.control_flow))
        print("  %-22s 共享：%s" % ("", cls.sharing))
    return results


def demo_failure(args: argparse.Namespace) -> None:
    print("=" * 84)
    print("B. 失效注入：让「%s」失败，看五种拓扑的失败传播路径" % (args.fail or "无"))
    print("=" * 84)
    llm = build_llm(args)
    results = all_topologies(llm, args.fail)
    for r in results:
        print("\n[%s] %s" % (r.name, "成功" if r.ok else "部分结果"))
        if r.failed_agent:
            print("    失败角色：%s" % r.failed_agent)
            print("    传播路径：%s" % r.failure_path)
        else:
            print("    传播路径：无失败")


def demo_hierarchy(args: argparse.Namespace) -> None:
    print("=" * 84)
    print("C. 层级成本模型：深度与扇出")
    print("=" * 84)
    print("完全展开时，一棵深度 d、扇出 f 的调度树，最坏情况的调用数：")
    print("    calls(d, f) = 1 + f + f^2 + ... + f^d = (f^(d+1) - 1) / (f - 1)\n")
    print("%s %s %s" % (_pad("深度 d", 12), _pad("扇出 f", 10), "最坏调用数"))
    for depth in (1, 2, 3):
        for fanout in (2, 3):
            calls = sum(fanout ** i for i in range(depth + 1))
            print("%s %s %s" % (_pad(str(depth), 12), _pad(str(fanout), 10),
                                "%d 次" % calls))
    print("\n同一批 %d 个叶子任务，换深度与扇出的差别（每个节点一次调用）：" % 9)
    print("%s %s %s" % (_pad("拓扑", 22), _pad("调用数", 10), "说明"))
    print("%s %s %s" % (_pad("扁平：1 个调度器 + 9 个执行者", 22), _pad("10", 10),
                        "一次中心决策，最省"))
    print("%s %s %s" % (_pad("两层：1 + 3 组 + 9 执行者", 22), _pad("13", 10),
                        "多两次组内决策，换来上下文隔离"))
    print("%s %s %s" % (_pad("三层：1 + 3 + 9 + 27", 22), _pad("40", 10),
                        "深度换来的隔离，通常不值这个倍数"))
    print("\n结论：深度每加一层，调用数按扇出倍数放大。上下文隔离的收益要盖得住这个倍数才值得。")


def demo_selection(args: argparse.Namespace) -> None:
    print("=" * 84)
    print("D. 选型三维度")
    print("=" * 84)
    print("%s %s %s %s %s" % (_pad("任务特征", 26), _pad("可分解度", 10),
                              _pad("对抗性", 9), _pad("可验证性", 10), "推荐拓扑"))
    rows = [
        ("流水线式产出（文档/代码）", "高", "低", "高", "Supervisor 或层级"),
        ("独立子任务（各查各的库）", "高", "低", "低", "扁平并行，不必多 Agent"),
        ("方案需要被挑错", "中", "高", "低", "辩论"),
        ("多角色共享一份工作区", "中", "低", "高", "黑板"),
        ("无中心可自治推进", "低", "中", "低", "网状"),
        ("全流程固定且要审计", "高", "低", "高", "SOP 流水线（第 22 章）"),
    ]
    for r in rows:
        print("%s %s %s %s %s" % (_pad(r[0], 26), _pad(r[1], 10), _pad(r[2], 9),
                                 _pad(r[3], 10), r[4]))


def main() -> None:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description="ch23 协作拓扑与调度")
    ap.add_argument("--fail", default="分析员", help="注入失败的角色名，空串表示不注入")
    ap.add_argument("--depth", type=int, default=2, help="层级深度（成本模型用）")
    ap.add_argument("--fanout", type=int, default=3, help="层级扇出（成本模型用）")
    ap.add_argument("--live", action="store_true", help="切真实模型（需 LLM_API_KEY）")
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "gpt-4o-mini"))
    args = ap.parse_args()

    demo_compare(args)
    demo_failure(args)
    demo_hierarchy(args)
    demo_selection(args)


if __name__ == "__main__":
    main()
