#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch21 对话式协作：GroupChat 与 CAMEL 角色扮演 最小可实现 —— 纯标准库，无需 API Key 就能跑通完整回路。

演示内容：
  1) CAMEL 式 inception prompt：任务 + 角色设定 + 输入输出格式，一次拼装出可用的提示
  2) GroupChat 轮次控制：auto / round_robin / manual / callable 四种 speaker selection
  3) 对话协议：结构化发言、终止词 [DONE]、打断标记 [INTERRUPT:名字]
  4) 上下文策略：shared 全量共享 / partitioned 分区 / summary 摘要 的 token 对照
  5) token 放大系数：按"内容下界"实测每个方案的总花费，以及一个单 Agent 的对照行

用法：
    python main.py                                # 成本对照 + 协议演示
    python main.py --turns 20 --mode shared       # 指定轮数与上下文策略
    python main.py --selection auto --trace       # 让模型自己选下一个发言的人
    python main.py --live                         # 切真实模型

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
from typing import Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# 1. 角色与消息
# --------------------------------------------------------------------------

TASK = "为客服团队做一套退款与赔付工单的自动核对报表，要求口径统一、可追溯、每日八点前出数。"

# 格式约定同时充当"对话协议"：发言格式、终止词、打断标记
FORMAT_SPEC = (
    "每次只输出你自己的一段发言，100-150 字，不要替对方说话；"
    "需要对方回应时结尾写 [ASK:<对方名字>]；"
    "认为讨论已经收敛时结尾写 [DONE]；"
    "需要对方立刻接手时开头写 [INTERRUPT:<对方名字>]。"
)


@dataclass
class Role:
    name: str
    persona: str      # 角色设定：inception prompt 的一部分
    goal: str         # 角色目标：这个角色在这场对话里要什么
    style: str = ""   # 说话风格约束


@dataclass
class Message:
    turn: int
    speaker: str
    content: str


@dataclass
class TurnCost:
    turn: int
    speaker: str
    prompt_tokens: int
    completion_tokens: int
    note: str = ""


ROLES: List[Role] = [
    Role(
        name="需求分析师",
        persona="负责把用户场景和验收口径讲清楚，讨厌模糊需求",
        goal="让验收标准可执行，避免范围失控",
        style="先讲场景，再讲边界，最后给优先级",
    ),
    Role(
        name="架构师",
        persona="负责技术方案与容量估算，讨厌没有回滚路径的设计",
        goal="在现有技术栈内给出可运维、可回滚的方案",
        style="先讲约束，再讲方案，最后讲代价",
    ),
    Role(
        name="测试工程师",
        persona="负责找出会上线出事的分支，讨厌只测正常路径",
        goal="把故障场景覆盖完整，不接受抽样验证",
        style="先讲口径，再讲异常分支，最后提确认项",
    ),
]

# 确定性 mock：每个角色 5 段固定发言，按该角色的发言次数取用
SCRIPT: Dict[str, List[str]] = {
    "需求分析师": [
        "先把用户场景说清楚。我们要解决的是客服每天人工核对退款单的场景，痛点不在查询慢，而在口径不统一导致对不上账。"
        "第一版我建议只覆盖退款和赔付两类工单，其他类型先不支持，把范围收窄才能把字段对齐做扎实。",
        "我补充一下非功能要求。报表要在每天上午八点前出，延迟可以接受到小时级；数据保留三十天就够，历史归档先不进这期。"
        "还有一点，所有导出都要能追溯到具体单号，否则出问题时没人敢用这个功能。",
        "关于优先级，我把需求排成三档：第一档是当日必须能用的退款核对，第二档是赔付汇总，第三档是自定义筛选。"
        "前两档的字段我们已经在现有系统里有了，第三档需要新建配置页，工作量差不多翻倍。",
        "刚才架构师说的写路径合并我理解，但用户侧不能感知到延迟堆积超过十秒，这个要写进验收标准。"
        "我建议加一个实时进度条，让核对的人知道还剩多少条，而不是等结果的时候干瞪眼。",
        "我认为需求已经收敛了。验收方式就是拿上个月的真实工单跑一遍，核对结果和人工核对完全一致才算通过。"
        "口径统一之后剩下的都是实现细节，我这边没有补充意见了。",
    ],
    "架构师": [
        "从现有约束看，先把写路径与读路径拆开：写路径走异步队列合并落库，读路径直接查索引副本。"
        "这样峰值流量由数据库承担，业务接口只负责受理。代价是读一致性会有秒级延迟，需要在产品说明里写清楚。",
        "关于选型，我不建议引入新的中间件。现有消息队列能满足合并写入的需求，再加一个组件就要多一套部署和监控。"
        "我们把消费端做成幂等的，用业务单号做去重键，这样重投也不会产生重复记录。",
        "我算了一下成本。如果按全量表关联做对账，凌晨那批要扫两千多万行，改成按分片增量对账之后只处理当天变更的记录，"
        "资源占用降到十分之一以下。这个结论基于现有数据量估的，真实数字要等压测确认。",
        "测试工程师提的幂等问题我的方案里已经覆盖了：消费端按单号去重，失败的消息进死信队列人工处理。"
        "但重试策略要设上限，最多三次，间隔拉长，否则下游抖动的时候会雪崩，这一点请在测试用例里覆盖。",
        "我这边没有阻塞项。剩下的就是把索引重建的窗口定下来，建议放在周日凌晨低峰期做，并且提前一天在群里同步。"
        "方案确认之后我直接出接口文档。",
    ],
    "测试工程师": [
        "我先确认验收口径。要测的不是功能能不能点通，而是数据对不对。我准备用上个月的真实工单做一次回放，"
        "对比系统输出和人工核对结果，逐单比对而不是抽样。同时要覆盖重复提交和乱序到达两种情况。",
        "我打断一下。刚才说的一致性延迟需要给一个明确上限，超过就要告警，否则运维看到延迟曲线波动会直接打电话过来。"
        "我的建议是超过五秒就先返回处理中，再给一个查询进度的接口，而不是让用户干等。",
        "关于性能测试，我要说明一个前提：如果压测数据量只有线上的一成，那得出的结论没有意义。"
        "我要用生产环境脱敏后的真实量级跑，重点看两个指标——凌晨批处理任务的总耗时，以及它对在线接口的影响比例。",
        "我同意用业务单号做幂等键，但补一条：要模拟消息重复投递和消费进程中途被杀这两种情况。"
        "只测正常重试是不够的，真实故障往往发生在不该重试的地方。另外死信队列要有人看，否则就是个黑洞。",
        "我这边测试用例已经覆盖主要路径。等接口文档出来我就补齐异常分支的用例，有两个点需要架构师确认："
        "重建索引期间旧索引是否还能读，以及对账任务的失败重跑是否幂等。",
    ],
}

# 主持人（单 Agent 对照行）：按同一套发言序列轮转输出，产出同样的内容
HOST_ROLE = Role(
    name="主持人",
    persona="一个人扮演三个角色完成这场讨论",
    goal="在同样的内容下，比较不同上下文策略的花费",
    style="依次扮演需求分析师、架构师、测试工程师",
)


# --------------------------------------------------------------------------
# 2. inception prompt：CAMEL 式角色扮演提示
# --------------------------------------------------------------------------


def inception_prompt(role: Role, others: List[Role], task: str, format_spec: str) -> str:
    """拼装 CAMEL 式 inception prompt。

    四个固定部分：任务描述 / 角色设定 / 对话对象 / 输入输出格式。
    角色扮演之所以有效，靠的就是把"你要干什么"换成"你是谁、你在跟谁聊"。
    """
    counterpart = "、".join("%s（%s）" % (r.name, r.persona) for r in others) or "无"
    return (
        "你正在参与一场多角色讨论。\n\n"
        "【任务】%s\n\n"
        "【你的角色】你是%s。%s。你的风格：%s。你的目标：%s。\n\n"
        "【对话对象】%s\n\n"
        "【输出格式】%s\n\n"
        "【讨论规则】\n"
        "1. 只输出你自己的一段发言，不要替对方说话；\n"
        "2. 不要重复别人已经说过的内容，要接着往下推进；\n"
        "3. 与你有分歧时直接说出来，不要附和；\n"
        "4. 严格使用上面的输出格式标记。"
        % (task, role.name, role.persona, role.style, role.goal, counterpart, format_spec)
    )


# --------------------------------------------------------------------------
# 3. token 计量与模型接入
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
        """一次请求记一次调用——一个请求可能分多次 add 累加 token。"""
        self.calls += 1

    def add(self, p: int, c: int) -> None:
        self.prompt_tokens += p
        self.completion_tokens += c

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class MockLLM:
    """确定性 mock：按角色取脚本里的发言，不做真实推理。

    scenario="control" 时会在指定位置插入 [INTERRUPT:名字] 与 [DONE]，用于演示协议；
    scenario="plain" 时不带控制标记，用于成本对照，保证各方案的内容完全一致。
    """

    name = "mock"

    def __init__(self, scenario: str = "plain") -> None:
        self.scenario = scenario
        self.meter = TokenMeter()
        self._per_role: Dict[str, int] = {}
        self._global = 0

    def chat(self, role: str, system: str, user: str) -> str:
        self.meter.record_call()
        self.meter.add(approx_tokens(system) + approx_tokens(user), 0)
        if role == "moderator":
            # 选人用的仲裁调用：从角色列表里挑一个名字出来
            names = [n for n in ("需求分析师", "架构师", "测试工程师") if n in system]
            pick = names[self._global % len(names)] if names else "架构师"
            self._global += 1
            self.meter.add(0, approx_tokens(pick))
            return pick

        if role == HOST_ROLE.name:
            # 单 Agent 对照行：轮转输出同一套发言序列
            idx = self._global
            self._global += 1
            speaker = ROLES[idx % len(ROLES)].name
            count = idx // len(ROLES)
            text = SCRIPT[speaker][min(count, len(SCRIPT[speaker]) - 1)]
        else:
            count = self._per_role.get(role, 0)
            self._per_role[role] = count + 1
            text = SCRIPT[role][min(count, len(SCRIPT[role]) - 1)]

        if self.scenario == "control":
            if role == "测试工程师" and count == 2:
                text = "[INTERRUPT:架构师] " + text
            elif role == "架构师" and count == 4:
                text = "[DONE] " + text
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
# 4. GroupChat 引擎
# --------------------------------------------------------------------------


class GroupChat:
    """以对话为协作单位的多 Agent 引擎。

    轮次控制：谁说话（selection）→ 说什么（inception prompt + 可见历史）→ 是否终止。
    上下文策略：shared / partitioned / summary 三种可见历史范围。
    """

    def __init__(self, roles: List[Role], llm, task: str = TASK,
                 selection: str = "round_robin", history_mode: str = "shared",
                 max_turns: int = 20, termination: Tuple[str, ...] = ("[DONE]",),
                 interrupt_tag: str = "[INTERRUPT:", summary_chars: int = 200,
                 manual_order: Optional[List[str]] = None,
                 picker: Optional[Callable[[int, List[Message]], str]] = None) -> None:
        self.roles = roles
        self.llm = llm
        self.task = task
        self.selection = selection
        self.history_mode = history_mode
        self.max_turns = max_turns
        self.termination = termination
        self.interrupt_tag = interrupt_tag
        self.summary_chars = summary_chars
        self.manual_order = manual_order or [r.name for r in roles]
        self.picker = picker
        self.messages: List[Message] = []
        self.costs: List[TurnCost] = []
        self.stop_reason = ""
        self._next_speaker: Optional[str] = None

    # ---- 选人 ----

    def _role_by_name(self, name: str) -> Role:
        for r in self.roles:
            if r.name == name:
                return r
        return self.roles[0]

    def _select_speaker(self) -> str:
        turn = len(self.messages)
        if self._next_speaker:                      # 打断优先于任何调度策略
            name, self._next_speaker = self._next_speaker, None
            return name
        if self.selection == "round_robin":
            return self.roles[turn % len(self.roles)].name
        if self.selection == "manual":
            # 手动选人：真实场景由人来点，这里用预置顺序代替交互
            return self.manual_order[turn % len(self.manual_order)]
        if self.selection == "callable":
            assert self.picker is not None, "selection=callable 需要传入 picker"
            return self.picker(turn, self.messages)
        if self.selection == "auto":
            # 自动选人：本身就是一次模型调用，代价必须记进账里
            names = "、".join(r.name for r in self.roles)
            system = "你是讨论主持人，只输出下一个发言者的名字。可选：%s" % names
            user = self._render_history(self.roles[0].name)
            raw = self.llm.chat("moderator", system, user)
            for r in self.roles:
                if r.name in raw:
                    return r.name
            return self.roles[turn % len(self.roles)].name
        raise ValueError("未知 selection: %s" % self.selection)

    # ---- 可见历史 ----

    def _rollup(self) -> str:
        """滚动摘要：把已发生的发言压成一行，模拟摘要器。"""
        if not self.messages:
            return ""
        parts = ["%s: %s" % (m.speaker, m.content[:24]) for m in self.messages]
        text = "；".join(parts)
        return text[:self.summary_chars] + ("…" if len(text) > self.summary_chars else "")

    def _render_history(self, role_name: str) -> str:
        if self.history_mode == "shared":
            return "\n".join("[%d] %s：%s" % (i + 1, m.speaker, m.content)
                             for i, m in enumerate(self.messages))
        if self.history_mode == "partitioned":
            own = [m for m in self.messages if m.speaker == role_name]
            others = [m for m in self.messages if m.speaker != role_name]
            lines = ["[我说的] %s：%s" % (m.speaker, m.content) for m in own]
            if others:
                lines.append("[最近一条他人发言] %s：%s" % (others[-1].speaker, others[-1].content))
            return "\n".join(lines)
        if self.history_mode == "summary":
            lines = ["[会议摘要] %s" % self._rollup()]
            lines += ["[最近] %s：%s" % (m.speaker, m.content) for m in self.messages[-2:]]
            return "\n".join(lines)
        raise ValueError("未知 history_mode: %s" % self.history_mode)

    # ---- 主循环 ----

    def run(self) -> List[Message]:
        for _ in range(self.max_turns):
            name = self._select_speaker()
            role = self._role_by_name(name)
            others = [r for r in self.roles if r.name != role.name]
            system = inception_prompt(role, others, self.task, FORMAT_SPEC)
            user = self._render_history(role.name)
            if user:
                user += "\n\n"
            user += "现在轮到你（%s）发言。" % role.name

            before_prompt = self.llm.meter.prompt_tokens
            before_completion = self.llm.meter.completion_tokens
            content = self.llm.chat(role.name, system, user)
            prompt_tokens = self.llm.meter.prompt_tokens - before_prompt
            completion_tokens = self.llm.meter.completion_tokens - before_completion

            turn = len(self.messages) + 1
            self.messages.append(Message(turn, role.name, content))
            note = ""
            self.costs.append(TurnCost(turn, role.name, prompt_tokens, completion_tokens, note))

            # 打断：让指定角色立刻接手
            if self.interrupt_tag in content:
                target = content.split(self.interrupt_tag, 1)[1].split("]", 1)[0].strip()
                if any(r.name == target for r in self.roles):
                    self._next_speaker = target
                    self.costs[-1].note = "打断 → %s" % target

            # 终止词
            if any(term in content for term in self.termination):
                self.stop_reason = "terminated"
                self.costs[-1].note = (self.costs[-1].note + " / " if self.costs[-1].note else "") + "命中终止词"
                break
        else:
            self.stop_reason = "max_turns"
        return self.messages


# --------------------------------------------------------------------------
# 5. 成本核算
# --------------------------------------------------------------------------


@dataclass
class CostReport:
    label: str
    turns: int
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    lower_bound: int
    stop_reason: str = ""

    @property
    def amplification(self) -> float:
        """放大系数 A = 总花费 / 内容下界。

        内容下界 = 产出同样内容的输出 token + 一次基础提示。
        A=1 表示每个 token 都花在了内容上，没有任何重复。
        """
        return self.total_tokens / self.lower_bound if self.lower_bound else 0.0


def run_and_report(label: str, chat: GroupChat, base: int) -> CostReport:
    chat.run()
    meter = chat.llm.meter
    lower_bound = meter.completion_tokens + base
    return CostReport(label=label, turns=len(chat.messages), calls=meter.calls,
                      prompt_tokens=meter.prompt_tokens,
                      completion_tokens=meter.completion_tokens,
                      total_tokens=meter.total,
                      lower_bound=lower_bound,
                      stop_reason=chat.stop_reason)


def compare_modes(turns: int) -> List[CostReport]:
    """同一套发言内容，四种上下文方案的成本对照。

    四行的产出完全相同：同样数量、同样文本的发言，变的只是每个发言者能看到多少历史。
    """
    base = approx_tokens(inception_prompt(ROLES[0], ROLES[1:], TASK, FORMAT_SPEC))
    reports = []
    for mode, label in (("shared", "3 人 · 共享全量历史"),
                        ("partitioned", "3 人 · 分区历史"),
                        ("summary", "3 人 · 共享摘要")):
        llm = MockLLM(scenario="plain")
        chat = GroupChat(ROLES, llm, selection="round_robin", history_mode=mode,
                         max_turns=turns)
        reports.append(run_and_report(label, chat, base))

    # 单 Agent 对照行：一个人扮演三个角色，同样用摘要上下文，产出同样的发言
    llm = MockLLM(scenario="plain")
    chat = GroupChat([HOST_ROLE], llm, selection="round_robin", history_mode="summary",
                     max_turns=turns)
    reports.append(run_and_report("单 Agent 顺序扮演（摘要上下文）", chat, base))
    return reports


# --------------------------------------------------------------------------
# 6. CLI
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


def build_llm(args: argparse.Namespace, scenario: str = "plain"):
    if not args.live:
        return MockLLM(scenario=scenario)
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "--live 需要环境变量 LLM_API_KEY；"
            "可选 LLM_BASE_URL（默认 https://api.openai.com/v1）与 LLM_MODEL。\n")
        raise SystemExit(2)
    return LiveLLM(args.base_url, api_key, args.model)


def demo_protocol(args: argparse.Namespace) -> None:
    """协议演示：四种 speaker selection + 终止词 + 打断。"""
    print("=" * 78)
    print("A. inception prompt（以架构师为例）")
    print("=" * 78)
    print(inception_prompt(ROLES[1], [ROLES[0], ROLES[2]], TASK, FORMAT_SPEC))

    print("=" * 78)
    print("B. 四种 speaker selection")
    print("=" * 78)
    for sel in ("round_robin", "auto", "manual", "callable"):
        llm = build_llm(args, scenario="control")
        picker = (lambda turn, msgs: ROLES[(turn + 1) % len(ROLES)].name) if sel == "callable" else None
        chat = GroupChat(ROLES, llm, task=TASK, selection=sel, history_mode="summary",
                         max_turns=args.turns, picker=picker,
                         manual_order=["测试工程师", "架构师", "需求分析师", "架构师"])
        chat.run()
        order = " → ".join(m.speaker for m in chat.messages)
        print("\n[%s] 发言顺序：%s" % (sel, order))
        print("    调用 %d 次｜终止原因 %s｜最后一条 %s"
              % (llm.meter.calls, chat.stop_reason,
                 ("命中终止词" if chat.stop_reason == "terminated" else "达到轮次上限")))
        for cost in chat.costs:
            if cost.note:
                print("    第 %d 轮 %s：%s" % (cost.turn, cost.speaker, cost.note))


def demo_cost(args: argparse.Namespace) -> None:
    """成本对照：同一套内容，四种上下文方案。"""
    print("=" * 78)
    print("C. 上下文策略与 token 放大系数")
    print("=" * 78)
    reports = compare_modes(args.turns)
    print("\n%s %s %s %s %s %s"
          % (_pad("方案", 30), _pad("轮次", 7), _pad("调用", 7),
             _pad("总token", 10), _pad("内容下界", 10), "放大系数 A"))
    for r in reports:
        print("%s %s %s %s %s %6.1fx"
              % (_pad(r.label, 30), _pad(str(r.turns), 7), _pad(str(r.calls), 7),
                 _pad(str(r.total_tokens), 10), _pad(str(r.lower_bound), 10), r.amplification))

    shared = next(r for r in reports if r.label.startswith("3 人 · 共享"))
    summary = next(r for r in reports if r.label.startswith("3 人 · 共享摘要"))
    single = next(r for r in reports if r.label.startswith("单 Agent"))
    print("\n共享全量历史比共享摘要多花 %.1f 倍；"
          "一个人带摘要（%s）比三个人共享全量历史（%s）省 %.1f 倍。"
          % (shared.total_tokens / summary.total_tokens,
             single.total_tokens, shared.total_tokens, shared.total_tokens / single.total_tokens))
    print("结论：贵的不是参与者数量，是共享了多少历史。")


def demo_trace(args: argparse.Namespace) -> None:
    """单次会话明细：看 prompt 是怎么一轮轮变长的。"""
    print("=" * 78)
    print("D. 单次会话明细（%s 模式）" % args.mode)
    print("=" * 78)
    llm = build_llm(args, scenario="control")
    chat = GroupChat(ROLES, llm, task=TASK, selection=args.selection,
                     history_mode=args.mode, max_turns=args.turns)
    chat.run()
    print("\n%s %s %s %s %s"
          % (_pad("轮次", 7), _pad("发言者", 14), _pad("prompt", 10),
             _pad("输出", 9), "备注"))
    for cost in chat.costs:
        print("%s %s %s %s %s"
              % (_pad(str(cost.turn), 7), _pad(cost.speaker, 14),
                 _pad(str(cost.prompt_tokens), 10), _pad(str(cost.completion_tokens), 9),
                 cost.note))
    print("\n总调用 %d 次｜总 token %d｜终止原因 %s"
          % (llm.meter.calls, llm.meter.total, chat.stop_reason))
    turns_total = sum(c.prompt_tokens for c in chat.costs)
    overhead = llm.meter.prompt_tokens - turns_total
    if overhead > 0:
        print("其中选人（仲裁）开销 %d token，占总输入 %.1f%%"
              % (overhead, 100.0 * overhead / max(1, llm.meter.prompt_tokens)))


def main() -> None:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description="ch21 对话式协作：GroupChat 与 CAMEL 角色扮演")
    ap.add_argument("--turns", type=int, default=60, help="最大发言次数，默认 60")
    ap.add_argument("--mode", default="shared",
                    choices=["shared", "partitioned", "summary"], help="上下文策略")
    ap.add_argument("--selection", default="round_robin",
                    choices=["round_robin", "auto", "manual", "callable"], help="选人策略")
    ap.add_argument("--live", action="store_true", help="切真实模型（需 LLM_API_KEY）")
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "gpt-4o-mini"))
    ap.add_argument("--trace", action="store_true", help="打印单次会话的逐轮明细")
    args = ap.parse_args()

    demo_protocol(args)
    demo_cost(args)
    if args.trace:
        demo_trace(args)


if __name__ == "__main__":
    main()
