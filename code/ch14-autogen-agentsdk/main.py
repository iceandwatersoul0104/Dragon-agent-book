#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch14 两条编排路线最小可实现 —— 纯标准库，无需 API Key 就能跑通完整回路。

路线 A（AutoGen 风格）：会话式多 Agent
    - Core 层：消息 + actor + 路由（消息广播给所有参与者）
    - AgentChat 层：Team（RoundRobinGroupChat / SelectorGroupChat）+ 终止条件
    - 轮次控制：固定轮转 vs 按内容自动选人

路线 B（OpenAI Agents SDK 风格）：单 Agent + 交接 + 护栏
    - Agent / Handoff / Guardrail / Session / Tracing 五个抽象
    - Runner 循环：调模型 -> 执行工具 -> 跟随交接 -> 再调模型

三个演示场景：

  groupchat  轮询群聊 vs 自动选人群聊：轮次与 LLM 调用次数的取舍
  agentsdk   交接 + 输入/输出护栏 + 会话记忆 + 追踪树
  compare    同一个任务在两条路线下各跑一遍，逐项对比

用法：
    python main.py                    # 跑全部场景
    python main.py --demo groupchat   # 只跑群聊对比
    python main.py --demo agentsdk    # 只跑 Agents SDK 路线
    python main.py --demo compare     # 只跑两条路线对比
    python main.py --trace            # 打印完整追踪树
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
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文结果乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ==========================================================================
# 路线 A · Core 层：消息与 actor
# ==========================================================================


@dataclass
class Message:
    """一条消息。群聊里所有参与者看到的是同一份历史。"""

    source: str
    content: str
    topic: str = "default"


class RoutedAgent:
    """按消息类型路由的 actor：Core 层的最小单位。

    一个 agent 订阅若干消息类型，收到就产出一条回信。
    """

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description or name
        self.received: List[Message] = []

    def on_message(self, msg: Message) -> Optional[str]:
        """处理消息；返回 None 表示不发言。"""
        raise NotImplementedError


# ==========================================================================
# 路线 A · AgentChat 层：终止条件
# ==========================================================================


class TerminationCondition:
    """团队的刹车。每轮结束后检查一次。"""

    def __init__(self) -> None:
        self.terminated = False

    def check(self, messages: Sequence[Message]) -> bool:
        raise NotImplementedError

    @property
    def reason(self) -> str:
        return "unknown"


class MaxMessageTermination(TerminationCondition):
    """消息条数上限——最后一道保险，永远要配一个。"""

    def __init__(self, max_messages: int) -> None:
        super().__init__()
        self.max_messages = max_messages

    def check(self, messages: Sequence[Message]) -> bool:
        if len(messages) >= self.max_messages:
            self.terminated = True
            return True
        return False

    @property
    def reason(self) -> str:
        return "达到最大消息数 %d" % self.max_messages


class TextMentionTermination(TerminationCondition):
    """出现指定关键词就停——用来当"完成信号"。"""

    def __init__(self, keyword: str) -> None:
        super().__init__()
        self.keyword = keyword

    def check(self, messages: Sequence[Message]) -> bool:
        for m in messages:
            if self.keyword in m.content:
                self.terminated = True
                return True
        return False

    @property
    def reason(self) -> str:
        return "出现终止关键词 %r" % self.keyword


class AnyTermination(TerminationCondition):
    """多个条件的组合：任一满足就停。"""

    def __init__(self, *conditions: TerminationCondition) -> None:
        super().__init__()
        self.conditions = conditions

    def check(self, messages: Sequence[Message]) -> bool:
        for c in self.conditions:
            if c.check(messages):
                self.terminated = True
                self._hit = c
                return True
        return False

    @property
    def reason(self) -> str:
        hit = getattr(self, "_hit", None)
        return hit.reason if hit else "unknown"


# ==========================================================================
# 路线 A · AgentChat 层：群聊团队
# ==========================================================================


@dataclass
class ChatResult:
    messages: List[Message]
    turns: int
    llm_calls: int
    stop_reason: str
    context_tokens: int = 0


class GroupChat:
    """群聊基类：共享一份消息历史，逐轮选一个发言人。"""

    def __init__(
        self,
        participants: List[RoutedAgent],
        termination_condition: Optional[TerminationCondition] = None,
        max_turns: int = 20,
    ) -> None:
        self.participants = participants
        self.termination = termination_condition or MaxMessageTermination(max_turns)
        self.max_turns = max_turns

    # -- 子类实现：下一个发言人 --
    def select_next(self, history: List[Message], turn: int) -> Optional[RoutedAgent]:
        raise NotImplementedError

    def run(self, task: str, topic: str = "default") -> ChatResult:
        history: List[Message] = [Message(source="user", content=task, topic=topic)]
        llm_calls = 0
        turn = 0
        while turn < self.max_turns:
            turn += 1
            speaker = self.select_next(history, turn - 1)
            if speaker is None:
                break
            llm_calls += 1
            content = speaker.on_message(history[-1]) or ""
            history.append(Message(source=speaker.name, content=content, topic=topic))
            if self.termination.check(history):
                break
        return ChatResult(
            messages=history,
            turns=turn,
            llm_calls=llm_calls,
            stop_reason=self.termination.reason,
        )


class RoundRobinGroupChat(GroupChat):
    """轮询：按固定顺序轮流发言，每轮每人一次。

    零额外 LLM 调用——顺序是写死的，不需要谁来决定。
    """

    def select_next(self, history: List[Message], turn: int) -> Optional[RoutedAgent]:
        return self.participants[turn % len(self.participants)]


class SelectorGroupChat(GroupChat):
    """自动选人：由一个选择器函数决定下一个发言人。

    选择器可以是规则函数（selector_func），也可以是模型（多一次 LLM 调用）。
    """

    def __init__(
        self,
        participants: List[RoutedAgent],
        selector_func: Callable[[List[Message]], Optional[str]],
        termination_condition: Optional[TerminationCondition] = None,
        max_turns: int = 20,
        llm_cost: int = 0,
    ) -> None:
        super().__init__(participants, termination_condition, max_turns)
        self.selector_func = selector_func
        self.llm_cost = llm_cost  # 选人本身要花几次 LLM 调用

    def select_next(self, history: List[Message], turn: int) -> Optional[RoutedAgent]:
        name = self.selector_func(history)
        if name is None:
            return None
        for p in self.participants:
            if p.name == name:
                return p
        return self.participants[0]


# ==========================================================================
# 路线 B · 五个抽象：Tool / Agent / Handoff / Guardrail / Session / Tracing
# ==========================================================================


@dataclass
class Tool:
    """一个工具。名字、描述、参数 schema、函数体。"""

    name: str
    description: str
    fn: Callable[..., Any]
    schema: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GuardrailFunctionOutput:
    """护栏的判定结果。tripwire_triggered 为真就中止整次运行。"""

    output_info: Any = None
    tripwire_triggered: bool = False


class InputGuardrailTripwireTriggered(Exception):
    """输入护栏拦下了这次运行。"""


class OutputGuardrailTripwireTriggered(Exception):
    """输出护栏拦下了这次运行的最终答案。"""


class MaxTurnsExceeded(Exception):
    """轮数超过上限，循环被强行终止。"""


def est_tokens(text: str) -> int:
    """粗略 token 估算：中文按字，英文按 4 字符 1 token。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 4


def history_tokens(history: Sequence[dict]) -> int:
    return sum(est_tokens(str(h.get("content", ""))) for h in history)


@dataclass
class Agent:
    """一个 Agent：指令 + 工具 + 交接对象 + 护栏。

    input_filter 是交接时的历史裁剪器：接班的那个 Agent 不一定需要
    看到全部对话——把无关的部分剪掉，是这条路线省上下文的手段。
    """

    name: str
    instructions: str
    tools: List[Tool] = field(default_factory=list)
    handoffs: List["Agent"] = field(default_factory=list)
    input_guardrails: List[Callable[[str], GuardrailFunctionOutput]] = field(default_factory=list)
    output_guardrails: List[Callable[[str], GuardrailFunctionOutput]] = field(default_factory=list)
    input_filter: Optional[Callable[[List[dict]], List[dict]]] = None
    output_type: Optional[str] = None

    def handoff_names(self) -> List[str]:
        return ["transfer_to_%s" % h.name.replace(" ", "_").lower() for h in self.handoffs]


class Session:
    """会话记忆：跨轮次存对话历史。

    实现了 agents.memory.Session 协议的那几个方法：读、写、弹出、清空。
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.items: List[dict] = []

    def get_items(self, limit: Optional[int] = None) -> List[dict]:
        return self.items[-limit:] if limit else list(self.items)

    def add_items(self, items: List[dict]) -> None:
        self.items.extend(items)

    def pop_item(self) -> Optional[dict]:
        return self.items.pop() if self.items else None

    def clear_session(self) -> None:
        self.items.clear()


@dataclass
class Span:
    """追踪中的一段。组成一棵树。"""

    name: str
    kind: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    children: List["Span"] = field(default_factory=list)


class Tracer:
    """追踪器：每次运行生成一棵 span 树。"""

    def __init__(self) -> None:
        self.root: Optional[Span] = None
        self._stack: List[Span] = []

    def start(self, span_name: str, span_kind: str, **attrs: Any) -> Span:
        """开一个 span。参数名故意不叫 name/kind，否则和 **attrs 里的键撞车。"""
        span = Span(name=span_name, kind=span_kind, attributes=dict(attrs))
        if self._stack:
            self._stack[-1].children.append(span)
        else:
            self.root = span
        self._stack.append(span)
        return span

    def end(self) -> None:
        if self._stack:
            self._stack.pop()

    def render(self, root: Optional[Span] = None, indent: int = 0) -> None:
        node = root or self.root
        if node is None:
            print("  （没有追踪记录）")
            return
        extra = ""
        if node.attributes:
            extra = "  " + ", ".join("%s=%s" % (k, v) for k, v in node.attributes.items())
        print("  %s%s [%s]%s" % ("  " * indent, node.name, node.kind, extra))
        for c in node.children:
            self.render(c, indent + 1)


@dataclass
class ModelResponse:
    """模型这一轮返回的东西：文本、工具调用、或者交接。"""

    content: str = ""
    tool_calls: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    handoff_to: Optional[str] = None


@dataclass
class RunResult:
    final_output: str
    agent: str
    turns: int
    llm_calls: int
    tool_calls: int
    context_tokens: int = 0
    handoffs: List[str] = field(default_factory=list)


class Runner:
    """Agent 循环：调模型 -> 有工具就执行 -> 有交接就换人 -> 再调模型。

    这就是 Agents SDK 的全部编排逻辑，核心其实很短。
    """

    def __init__(self, model: "ModelBase", tracer: Optional[Tracer] = None) -> None:
        self.model = model
        self.tracer = tracer or Tracer()

    def run(
        self,
        agent: Agent,
        user_input: str,
        session: Optional[Session] = None,
        max_turns: int = 10,
    ) -> RunResult:
        tracer = self.tracer
        tracer.start("run", "agent", agent=agent.name)
        try:
            # ---- 输入护栏：在调用大模型之前拦截 ----
            for gr in agent.input_guardrails:
                out = gr(user_input)
                if out.tripwire_triggered:
                    tracer.start("input_guardrail", "guardrail", tripped=True)
                    tracer.end()
                    raise InputGuardrailTripwireTriggered(
                        "输入护栏拦截了 %s 的请求" % agent.name
                    )
            tracer.start("input_guardrail", "guardrail", tripped=False)
            tracer.end()

            history: List[dict] = []
            if session is not None:
                history.extend(session.get_items())
            history.append({"role": "user", "content": user_input})
            if session is not None:
                # 一次运行只记一条用户输入——哪怕这一轮循环转了三圈
                session.add_items([{"role": "user", "content": user_input}])

            current = agent
            turns = 0
            llm_calls = 0
            tool_calls = 0
            handoffs: List[str] = []

            while True:
                turns += 1
                if turns > max_turns:
                    tracer.end()
                    raise MaxTurnsExceeded("超过 max_turns=%d" % max_turns)

                tracer.start("generation", "model", agent=current.name, turn=turns)
                ctx_tokens = history_tokens(history)
                resp = self.model.respond(current, history)
                llm_calls += 1
                tracer.end()

                # ---- 交接：换一个 Agent 接手 ----
                if resp.handoff_to:
                    target = next((h for h in current.handoffs if h.name == resp.handoff_to), None)
                    if target is None:
                        raise ValueError("未知交接目标: %s" % resp.handoff_to)
                    tracer.start("handoff", "handoff", frm=current.name, to=target.name)
                    tracer.end()
                    handoffs.append("%s -> %s" % (current.name, target.name))
                    history.append(
                        {
                            "role": "assistant",
                            "content": "转交给 %s" % target.name,
                            "handoff": target.name,
                        }
                    )
                    # 交接时可以裁剪历史：接班的不需要看到全部对话
                    if current.input_filter is not None:
                        history = current.input_filter(history)
                    current = target
                    continue

                # ---- 工具调用：执行完把结果塞回历史 ----
                if resp.tool_calls:
                    for tool_name, args in resp.tool_calls:
                        tool = next((t for t in current.tools if t.name == tool_name), None)
                        if tool is None:
                            raise ValueError("未知工具: %s" % tool_name)
                        tracer.start("tool_call", "tool", name=tool_name, args=json.dumps(args, ensure_ascii=False))
                        result = tool.fn(**args)
                        tracer.end()
                        tool_calls += 1
                        history.append(
                            {"role": "tool", "name": tool_name, "content": str(result)}
                        )
                    continue

                # ---- 最终输出：过输出护栏 ----
                final = resp.content
                for gr in current.output_guardrails:
                    out = gr(final)
                    if out.tripwire_triggered:
                        tracer.start("output_guardrail", "guardrail", tripped=True)
                        tracer.end()
                        raise OutputGuardrailTripwireTriggered(
                            "输出护栏拦截了 %s 的答案" % current.name
                        )
                tracer.start("output_guardrail", "guardrail", tripped=False)
                tracer.end()

                history.append({"role": "assistant", "content": final})
                if session is not None:
                    session.add_items([{"role": "assistant", "content": final}])
                return RunResult(
                    final_output=final,
                    agent=current.name,
                    turns=turns,
                    llm_calls=llm_calls,
                    tool_calls=tool_calls,
                    context_tokens=ctx_tokens,
                    handoffs=handoffs,
                )
        finally:
            tracer.end()


class _NullCtx:
    """占位用的空上下文（保留给将来的 with 语法扩展）。"""

    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


# ==========================================================================
# 模型层：mock 与 live
# ==========================================================================


class ModelBase:
    """模型只要会一件事：看当前 Agent 和历史，返回这一轮要做什么。"""

    def respond(self, agent: Agent, history: List[dict]) -> ModelResponse:
        raise NotImplementedError


class MockModel(ModelBase):
    """离线 mock：按 agent 人设 + 历史决定行为。

    它不代表任何真实模型的表现，只保证没有 API Key 也能把两条路线跑通。
    """

    def __init__(self) -> None:
        self.calls = 0
        # agent 名字 -> 人设。人设拿到 (agent, 历史)，返回这一轮要做什么。
        self.personas: Dict[str, Callable[[Agent, List[dict]], ModelResponse]] = {
            "triage": self.triage,
            "billing_specialist": self.billing,
        }

    def respond(self, agent: Agent, history: List[dict]) -> ModelResponse:
        self.calls += 1
        persona = self.personas.get(agent.name)
        if persona is None:
            return ModelResponse(content="%s：我这边没有额外补充。" % agent.name)
        return persona(agent, history)

    # ---- 群聊人设 ----
    # 注意：判断依据用各角色自己的输出标记，不用"结论"这种通用词——
    # 任务描述里出现"结论"就会造成误判。

    @staticmethod
    def analyst(history: List[Message]) -> str:
        texts = [m.content for m in history]
        if not any("分析结论：" in t or "补充结论：" in t for t in texts):
            return "分析结论：上周订单量环比下降 12%，主要来自华东区大客户取消订单，非系统故障。"
        return "补充结论：进一步拆分后确认是客户主动取消，不是我们的问题，可以对外说明。"

    @staticmethod
    def writer(history: List[Message]) -> str:
        has_conclusion = any(
            "分析结论：" in m.content or "补充结论：" in m.content for m in history
        )
        if not has_conclusion:
            return "我需要先有分析结论才能动笔，目前没有可用素材。"
        return "报告初稿：本周订单量环比下降 12%，原因为华东区大客户主动取消订单，平台侧无异常，已同步客户经理。"

    @staticmethod
    def reviewer(history: List[Message]) -> str:
        has_draft = any("报告初稿" in m.content for m in history)
        if not has_draft:
            return "没有可审的稿件，先让撰写者出稿。"
        if any("补充结论：" in m.content for m in history):
            return "补充结论已到位，报告可以发布。APPROVE"
        return "结论与初稿一致，可以发布。APPROVE"

    # ---- Agents SDK 人设 ----

    @staticmethod
    def triage(agent: Agent, history: List[dict]) -> ModelResponse:
        # 会话里能翻到订单号就复用，翻不到就用输入里的
        order_id = _find_order_id(history)
        if not order_id:
            return ModelResponse(content="请补充订单号，形如 A-91。")
        already_looked_up = any(h.get("role") == "tool" for h in history)
        if already_looked_up:
            # 查过了 -> 交接给计费专家（交接在 SDK 里就是一种特殊的工具调用）
            return ModelResponse(
                content="订单信息已查到，转交计费专家。",
                handoff_to="billing_specialist",
            )
        return ModelResponse(
            content="查一下订单 %s 的状态。" % order_id,
            tool_calls=[("lookup_order", {"order_id": order_id})],
        )

    @staticmethod
    def billing(agent: Agent, history: List[dict]) -> ModelResponse:
        tool_msgs = [h for h in history if h.get("role") == "tool"]
        status = tool_msgs[-1]["content"] if tool_msgs else "未知"
        if "已退款" in status:
            return ModelResponse(content="该订单已退款，保证 3 个工作日内到账。")
        return ModelResponse(content="订单当前状态：%s。" % status)


def _find_order_id(history: List[dict]) -> Optional[str]:
    for h in reversed(history):
        m = re.search(r"A-\d+", str(h.get("content", "")))
        if m:
            return m.group(0)
    return None


class LiveModel(ModelBase):
    """真实模型：走 OpenAI 兼容的 chat completions 端点。"""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _chat(self, messages: List[dict]) -> str:
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

    def respond(self, agent: Agent, history: List[dict]) -> ModelResponse:
        """让模型返回 JSON，描述这一轮要做什么。

        handoff_to 用交接目标的名字，SDK 里它表现为 transfer_to_xxx 工具调用。
        """
        tools_desc = [t.name for t in agent.tools]
        handoffs_desc = agent.handoff_names()
        prompt = (
            "你是一个 Agent 循环里的模型。根据对话历史决定这一轮做什么，只返回 JSON：\n"
            '{"content":"要说的话","tool_calls":[["工具名",{参数}]],"handoff_to":null}\n'
            "可用工具：%s\n可用交接对象：%s\n\n对话历史：\n%s"
            % (
                tools_desc or "（无）",
                handoffs_desc or "（无）",
                "\n".join("- %s: %s" % (h.get("role"), h.get("content")) for h in history),
            )
        )
        raw = self._chat([{"role": "user", "content": prompt}])
        data = self._parse_json(raw)
        tool_calls = []
        for tc in data.get("tool_calls") or []:
            if isinstance(tc, list) and len(tc) == 2:
                tool_calls.append((str(tc[0]), dict(tc[1] or {})))
            elif isinstance(tc, dict) and "name" in tc:
                tool_calls.append((str(tc["name"]), dict(tc.get("args") or {})))
        handoff_to = data.get("handoff_to")
        return ModelResponse(
            content=str(data.get("content") or ""),
            tool_calls=tool_calls,
            handoff_to=str(handoff_to) if handoff_to else None,
        )

    def chat_agent(self, agent: Agent, history: List[Message]) -> str:
        """群聊路线：一次普通对话补全。"""
        msgs = [{"role": "system", "content": agent.instructions}]
        for m in history:
            role = "assistant" if m.source == agent.name else "user"
            msgs.append({"role": role, "content": "%s: %s" % (m.source, m.content)})
        return self._chat(msgs).strip()


def build_model(live: bool, args) -> Tuple[ModelBase, Optional[LiveModel]]:
    if not live:
        return MockModel(), None
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        print("错误：--live 需要环境变量 LLM_API_KEY", file=sys.stderr)
        raise SystemExit(2)
    base_url = args.base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model = args.model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    live_model = LiveModel(api_key, base_url, model)
    return live_model, live_model


# ==========================================================================
# 场景一：群聊 —— 轮询 vs 自动选人
# ==========================================================================


class ChatAgent(RoutedAgent):
    """群聊参与者：基于完整历史产出一条回信。

    群聊执行器会带上完整历史调用 chat_agent_respond，
    所以 on_message 在这条路线里不直接使用。
    """

    def __init__(self, name: str, persona: Callable[[List[Message]], str], description: str = "") -> None:
        super().__init__(name, description)
        self.persona = persona

    def on_message(self, msg: Message) -> Optional[str]:
        return self.persona([msg])


def make_chat_agent(name: str, persona: Callable[[List[Message]], str], description: str, live: Optional[LiveModel]) -> RoutedAgent:
    """构造一个群聊参与者。

    persona 收到完整历史，返回这一轮的话。live 模式下改走真实模型。
    """
    if live is not None:
        class _LiveAgent(RoutedAgent):
            def on_message(self, msg: Message) -> Optional[str]:
                return live.chat_agent(self, [msg])

        agent: RoutedAgent = _LiveAgent(name, description)
    else:
        agent = ChatAgent(name, persona, description)
    # 让 persona 能看到完整历史
    agent.persona = persona  # type: ignore[attr-defined]
    return agent


def chat_agent_respond(agent: RoutedAgent, history: List[Message]) -> str:
    """按 agent 的人设，基于完整历史生成回应。"""
    persona = getattr(agent, "persona")
    return persona(history)


class GroupChatRunner:
    """带完整历史视角的群聊执行器。"""

    def __init__(
        self,
        participants: List[RoutedAgent],
        termination: Optional[TerminationCondition] = None,
        max_turns: int = 20,
        selector_func: Optional[Callable[[List[Message]], Optional[str]]] = None,
        selector_llm_cost: int = 0,
    ) -> None:
        self.participants = participants
        self.termination = termination or MaxMessageTermination(max_turns)
        self.max_turns = max_turns
        self.selector_func = selector_func
        self.selector_llm_cost = selector_llm_cost

    def run(self, task: str) -> ChatResult:
        history: List[Message] = [Message(source="user", content=task)]
        llm_calls = 0
        turn = 0
        ctx_tokens = 0
        while turn < self.max_turns:
            turn += 1
            if self.selector_func is not None:
                llm_calls += self.selector_llm_cost
                name = self.selector_func(history)
                if name is None:
                    break
                speaker = next((p for p in self.participants if p.name == name), None)
            else:
                speaker = self.participants[(turn - 1) % len(self.participants)]
            if speaker is None:
                break
            llm_calls += 1
            # 群聊的关键：每一轮都要把全部共享历史重发给这个 Agent
            ctx_tokens = sum(est_tokens(m.content) for m in history)
            content = chat_agent_respond(speaker, history)
            history.append(Message(source=speaker.name, content=content))
            if self.termination.check(history):
                break
        return ChatResult(
            messages=history,
            turns=turn,
            llm_calls=llm_calls,
            stop_reason=self.termination.reason,
            context_tokens=ctx_tokens,
        )


def build_chat_agents(live: Optional[LiveModel]) -> List[RoutedAgent]:
    m = MockModel()
    agents = [
        make_chat_agent("writer", m.writer, "负责把结论写成报告", live),
        make_chat_agent("reviewer", m.reviewer, "负责审核报告", live),
        make_chat_agent("analyst", m.analyst, "负责数据分析", live),
    ]
    return agents


def rule_selector(history: List[Message]) -> Optional[str]:
    """规则版选人器（对应 AutoGen 的 selector_func）。

    没有分析结论 -> 让分析师上；有结论没初稿 -> 让撰写者上；
    有初稿 -> 让审核员上。
    """
    texts = [m.content for m in history]
    has_draft = any("报告初稿" in t for t in texts)
    has_conclusion = any("分析结论：" in t or "补充结论：" in t for t in texts)
    if has_draft:
        return "reviewer"
    if has_conclusion:
        return "writer"
    return "analyst"


def llm_selector(history: List[Message]) -> Optional[str]:
    """模型版选人器：每轮多花一次 LLM 调用。

    这里用"上一位是谁"来模拟模型读上下文后的判断——
    真实的模型读的是完整历史，但结论一致：谁刚说完，下一个就该是谁接。
    """
    last = history[-1]
    if last.source == "writer":
        return "reviewer"
    if last.source == "analyst":
        return "writer"
    return "analyst"


def run_groupchat(live: Optional[LiveModel], trace: bool) -> None:
    hr("场景一：轮询群聊 vs 自动选人群聊")
    task = "分析上周订单量下滑的原因，并给出可以对外的结论。"

    print("任务：%s\n" % task)

    configs = [
        ("轮询 RoundRobin（写→审→析）", None, 0),
        ("自动选人 Selector（规则选人）", rule_selector, 0),
        ("自动选人 Selector（模型选人）", llm_selector, 1),
    ]

    for label, selector, cost in configs:
        agents = build_chat_agents(live)
        term = AnyTermination(TextMentionTermination("APPROVE"), MaxMessageTermination(10))
        runner = GroupChatRunner(
            participants=agents,
            termination=term,
            max_turns=10,
            selector_func=selector,
            selector_llm_cost=cost,
        )
        result = runner.run(task)
        print("\n【%s】" % label)
        for m in result.messages:
            tag = "用户" if m.source == "user" else m.source
            print("  [%s] %s" % (tag, m.content))
        print("  -> 轮次 %d，LLM 调用 %d 次，终止原因：%s"
              % (result.turns, result.llm_calls, result.stop_reason))

    print(
        "\n取舍很清楚：\n"
        "  轮询不花选人的调用，但会按顺序问不该问的人（审核员在有稿之前先发言）；\n"
        "  规则选人零额外调用，且能跳过无效轮次；\n"
        "  模型选人最灵活，但每轮多一次 LLM 调用——轮次越多，账越厚。"
    )


# ==========================================================================
# 场景二：Agents SDK 路线 —— 交接 + 护栏 + 会话 + 追踪
# ==========================================================================


def build_agentsdk_agents(model: ModelBase) -> Tuple[Agent, Agent]:
    """分诊 Agent + 计费专家 Agent。"""

    def lookup_order(order_id: str) -> str:
        """按订单号查状态。"""
        table = {
            "A-91": "已发货，运单 SF7788，预计明天送达",
            "A-92": "已退款，退款金额 320 元",
        }
        return table.get(order_id, "订单不存在")

    def no_full_refund(user_input: str) -> GuardrailFunctionOutput:
        """策略：全额退款不做自动处理，必须转人工。"""
        flagged = "全额退款" in user_input
        return GuardrailFunctionOutput(
            output_info={"flagged": flagged, "rule": "no_full_refund"},
            tripwire_triggered=flagged,
        )

    def no_guarantee(final: str) -> GuardrailFunctionOutput:
        """输出不能对到账时间做承诺。"""
        flagged = "保证" in final
        return GuardrailFunctionOutput(
            output_info={"flagged": flagged, "rule": "no_guarantee"},
            tripwire_triggered=flagged,
        )

    def handoff_filter(history: List[dict]) -> List[dict]:
        """交接给计费专家时裁剪历史：只留用户诉求和查到的结果。

        群聊路线里每个人都看得见全部对话；这条路线可以只给接班的人
        看它该看的——这是上下文成本上的真实差别。
        """
        kept = [h for h in history if h.get("role") == "tool"]
        user_msgs = [h for h in history if h.get("role") == "user"]
        if user_msgs:
            kept.insert(0, user_msgs[0])
        return kept

    billing = Agent(
        name="billing_specialist",
        instructions="你负责订单与账单类问题。回答要简短，不要做时间承诺。",
        output_guardrails=[no_guarantee],
    )
    triage = Agent(
        name="triage",
        instructions="你负责分诊。查订单状态交给工具，涉及账单细节就交接给计费专家。",
        tools=[Tool(name="lookup_order", description="按订单号查订单状态", fn=lookup_order)],
        handoffs=[billing],
        input_guardrails=[no_full_refund],
        input_filter=handoff_filter,
    )
    return triage, billing


def run_agentsdk(live: bool, trace: bool) -> None:
    hr("场景二：Agents SDK 路线 —— 交接 + 护栏 + 会话 + 追踪")
    model, live_model = build_model(live, argparse.Namespace(model=None, base_url=None))
    if live:
        print("（live 模式：走真实模型）")
    triage, billing = build_agentsdk_agents(model)

    # --- 1) 输入护栏：全额退款直接拦下，模型一次都没被调用 ---
    print("\n【1】输入护栏拦截")
    before = model.calls
    tracer = Tracer()
    runner = Runner(model, tracer)
    try:
        runner.run(triage, "帮我把订单 A-92 全额退款到银行卡", max_turns=5)
        print("  没有拦截（不符合预期）")
    except InputGuardrailTripwireTriggered as exc:
        print("  被拦截：%s" % exc)
        print("  期间模型调用次数：%d（一次都没调用）" % (model.calls - before))
    if trace:
        tracer.render()

    # --- 2) 正常流程：工具 -> 交接 -> 最终输出 ---
    print("\n【2】工具调用 + 交接")
    before = model.calls
    tracer = Tracer()
    runner = Runner(model, tracer)
    result = runner.run(triage, "订单 A-91 到哪了？", max_turns=6)
    print("  最终由谁作答：%s" % result.agent)
    print("  交接路径    ：%s" % (" -> ".join(result.handoffs) if result.handoffs else "无"))
    print("  工具调用    ：%d 次" % result.tool_calls)
    print("  模型调用    ：%d 次" % result.llm_calls)
    print("  最终答案    ：%s" % result.final_output)
    if trace:
        print("  追踪树：")
        tracer.render()

    # --- 3) 输出护栏：含"保证"的答案被拦下 ---
    print("\n【3】输出护栏拦截")
    tracer = Tracer()
    runner = Runner(model, tracer)
    try:
        runner.run(triage, "订单 A-92 什么时候退款到账？", max_turns=6)
        print("  没有拦截（不符合预期）")
    except OutputGuardrailTripwireTriggered as exc:
        print("  被拦截：%s" % exc)
    if trace:
        tracer.render()

    # --- 4) 会话记忆：第二轮不重复给订单号 ---
    print("\n【4】会话记忆")
    session = Session("user-42")
    tracer = Tracer()
    runner = Runner(model, tracer)
    r1 = runner.run(triage, "订单 A-91 到哪了？", session=session, max_turns=6)
    r2 = runner.run(triage, "那退款呢？", session=session, max_turns=6)
    print("  第 1 轮：%s" % r1.final_output)
    print("  第 2 轮（没给订单号）：%s" % r2.final_output)
    print("  会话里存了 %d 条消息" % len(session.items))
    print("  第 2 轮复用了历史里的订单号 -> 这就是 Session 的价值")


# ==========================================================================
# 场景三：两条路线对比
# ==========================================================================


def build_service_desk_agents(live: Optional[LiveModel]) -> List[RoutedAgent]:
    """对比场景用的群聊：一个查订单的，一个审回复的。"""

    def service(history: List[Message]) -> str:
        return "订单 A-91 状态：已发货，运单 SF7788，预计明天送达。退款已处理，到账时间以支付渠道为准。"

    def auditor(history: List[Message]) -> str:
        return "回复与订单信息一致，没有承诺到账时间，可以发送。APPROVE"

    return [
        make_chat_agent("客服", service, "查询订单并回复客户", live),
        make_chat_agent("审核", auditor, "审核对外回复", live),
    ]


def service_selector(history: List[Message]) -> Optional[str]:
    """客服说完就轮到审核；其余情况都是客服先上。"""
    return "审核" if history[-1].source == "客服" else "客服"


def run_compare(live: bool) -> None:
    hr("场景三：同一个任务，两条路线")
    task = "客户问：订单 A-91 到哪了？"

    # ---- 路线 A：群聊 ----
    _, live_a = build_model(live, argparse.Namespace(model=None, base_url=None))
    agents = build_service_desk_agents(live_a)
    term = AnyTermination(TextMentionTermination("APPROVE"), MaxMessageTermination(8))
    runner_a = GroupChatRunner(
        agents, term, max_turns=8, selector_func=service_selector
    )
    res_a = runner_a.run(task)

    # ---- 路线 B：Agents SDK ----
    model_b, live_b = build_model(live, argparse.Namespace(model=None, base_url=None))
    triage, billing = build_agentsdk_agents(model_b)
    tracer = Tracer()
    runner_b = Runner(model_b, tracer)
    res_b = runner_b.run(triage, task, max_turns=6)

    print("任务：%s\n" % task)
    print("路线 A 的对话：")
    for m in res_a.messages:
        tag = "用户" if m.source == "user" else m.source
        print("  [%s] %s" % (tag, m.content))
    print("路线 B 的追踪：")
    tracer.render()
    print()

    rows = [
        ("模型调用次数", "%d 次" % res_a.llm_calls, "%d 次" % res_b.llm_calls),
        ("轮次 / 循环次数", "%d 轮" % res_a.turns, "%d 次" % res_b.turns),
        ("工具调用", "无（要自己接）", "%d 次" % res_b.tool_calls),
        ("交接", "无", " -> ".join(res_b.handoffs) or "无"),
        (
            "最后一轮送进模型的上下文",
            "全员共享历史 约 %d token" % res_a.context_tokens,
            "裁剪后专家上下文 约 %d token" % res_b.context_tokens,
        ),
        ("上下文形态", "所有人都看得见全部对话", "当前 Agent 的历史，可裁剪"),
        ("终止方式", "终止条件（关键词 / 上限）", "没有工具调用和交接就算结束"),
        ("护栏", "要自己在流程里加", "输入 / 输出护栏是一等抽象"),
    ]
    print("%-24s %-32s %-32s" % ("维度", "路线 A：会话式群聊", "路线 B：单 Agent + 交接"))
    print("-" * 92)
    for k, a, b in rows:
        print("%-24s %-32s %-32s" % (k, a, b))
    print("-" * 92)
    print(
        "\n两条路线对编排的隐含假设不一样：\n"
        "  A 假设编排就是「大家开会」，控制流在发言权怎么分配；\n"
        "  B 假设编排就是「一个人带着工具和下属」，控制流在当前该谁上场。\n"
        "  A 的每轮上下文是全量共享历史；B 可以用 input_filter 只给接班的人看该看的。\n"
        "  任务需要来回协商，选 A；任务是「分诊 + 查 + 答」，选 B。"
    )


# ==========================================================================
# CLI
# ==========================================================================


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    ap = argparse.ArgumentParser(description="ch14 两条编排路线最小可实现")
    ap.add_argument("--demo", choices=["all", "groupchat", "agentsdk", "compare"], default="all")
    ap.add_argument("--trace", action="store_true", help="打印完整追踪树")
    ap.add_argument("--live", action="store_true", help="切真实模型 API")
    ap.add_argument("--model", default=None, help="覆盖 LLM_MODEL")
    ap.add_argument("--base-url", default=None, help="覆盖 LLM_BASE_URL")
    args = ap.parse_args()

    if args.demo in ("all", "groupchat"):
        _, live_model = build_model(args.live, args)
        run_groupchat(live_model, args.trace)
    if args.demo in ("all", "agentsdk"):
        run_agentsdk(args.live, args.trace)
    if args.demo in ("all", "compare"):
        run_compare(args.live)

    hr("跑完了")
    print("这个实现里有五个可以抄走的结论：")
    print("  1) 群聊里所有 Agent 看到同一份历史，信息对称是它最大的优点，也是它的成本来源；")
    print("  2) 轮询省调用，自动选人省轮次——两者的取舍就是「多花调用」换「少问错人」；")
    print("  3) 交接就是一种特殊工具调用，模型选谁，代码里没有 if/else；")
    print("  4) 护栏在模型之前拦，比在提示词里写「请不要」可靠；")
    print("  5) 追踪树是免费的：每次运行、每个工具、每次交接都该有一棵。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
