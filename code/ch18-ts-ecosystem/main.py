#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch18 TypeScript 生态最小可实现 —— 纯标准库，无需 API Key 就能跑通完整回路。

这个文件用标准库复刻 TS 侧生态的三块核心机制：

  路线一 Vercel AI SDK 风格：UI 优先的流式协议 + useChat 客户端状态机
  路线二 Mastra 风格：      Agent / Workflow / Memory / Eval 四层 + 观测 span
  路线三 沙箱策略对比：      Node 子进程 / Deno 权限模型 / WASM 隔离

三个演示场景：

  stream    服务端吐协议分片 -> 客户端状态机还原消息（含工具调用作为消息的一部分）
  mastra    四层分层：工作流分支与挂起恢复、线程记忆与语义召回、评测打分
  sandbox   三种隔离策略对同一组操作各放行什么

用法：
    python main.py                    # 跑全部场景
    python main.py --demo stream      # 只跑流式协议
    python main.py --demo mastra      # 只跑 Mastra 四层
    python main.py --demo sandbox     # 只跑沙箱策略对比
    python main.py --trace            # 打印完整协议分片与 span 树
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
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文结果乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ==========================================================================
# 公共：类型化工具（Zod 风格 schema 从签名长出来）
# ==========================================================================


_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def zod_schema(fn: Callable) -> Dict[str, Any]:
    """从函数签名生成 JSON Schema——TS 里这活儿是 Zod 干的。

    工具的名字是函数名，描述是 docstring，参数来自类型注解。
    类型提示即契约：签名改了，schema 自动跟着改。
    """
    import inspect

    sig = inspect.signature(fn)
    props: Dict[str, Any] = {}
    required: List[str] = []
    for name, param in sig.parameters.items():
        if name in ("self", "ctx"):
            continue
        json_type = _TYPE_MAP.get(param.annotation, "string")
        props[name] = {"type": json_type}
        if param.default is inspect.Parameter.empty:
            required.append(name)
    doc = (fn.__doc__ or "").strip().splitlines()
    return {
        "name": fn.__name__,
        "description": doc[0] if doc else fn.__name__,
        "parameters": {"type": "object", "properties": props, "required": required},
    }


def est_tokens(text: str) -> int:
    """粗略 token 估算：中文按字，英文按 4 字符 1 token。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 4


# ==========================================================================
# 路线一：Vercel AI SDK 风格 —— 协议 + 客户端状态机
# ==========================================================================

# 协议分片类型——服务端与客户端之间唯一的契约
TEXT_DELTA = "text-delta"
TOOL_CALL = "tool-call"
TOOL_RESULT = "tool-result"
FINISH = "finish"


@dataclass
class Chunk:
    """一个协议分片。"""

    type: str
    delta: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    result: Any = None
    usage: Dict[str, int] = field(default_factory=dict)

    def to_sse(self) -> str:
        """序列化成 SSE 一行。"""
        return "data: %s" % json.dumps(self.__dict__, ensure_ascii=False)


@dataclass
class Message:
    """客户端消息。注意 parts 是"部分"的列表——工具调用是消息的一部分，不是独立消息。"""

    id: str
    role: str
    parts: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "streaming"  # streaming | complete

    @property
    def text(self) -> str:
        return "".join(p["delta"] for p in self.parts if p["type"] == TEXT_DELTA)

    @property
    def tool_calls(self) -> List[Dict[str, Any]]:
        return [p for p in self.parts if p["type"] == TOOL_CALL]

    @property
    def tool_results(self) -> List[Dict[str, Any]]:
        return [p for p in self.parts if p["type"] == TOOL_RESULT]


def _pieces(text: str, size: int = 12) -> List[str]:
    """把一段文本切成流式分片。

    真实的 SDK 吐的是 token 粒度的 delta；这里按标点优先断开，
    长度上限 size——既能体现"边生成边吐"，输出也不会碎得没法看。
    """
    out: List[str] = []
    i = 0
    while i < len(text):
        j = min(i + size, len(text))
        if j < len(text):
            cut = max(text.rfind(c, i, j) for c in "，。！？、；,.!? ")
            if cut > i:
                j = cut + 1
        out.append(text[i:j])
        i = j
    return out


class ToolCallingAgent:
    """服务端：调模型、跑工具、把一切变成协议分片吐出去。

    这就是 streamText 的形状——**产出的不是一段文本，是一个分片序列**。
    """

    def __init__(
        self,
        model: "ModelBase",
        tools: Sequence[Callable],
        system: str = "",
        max_steps: int = 4,
    ) -> None:
        self.model = model
        self.tools = {t.__name__: t for t in tools}
        self.tool_schemas = [zod_schema(t) for t in tools]
        self.system = system
        self.max_steps = max_steps

    def stream(self, prompt: str) -> List[Chunk]:
        """返回分片序列。真实实现这里是 async generator。"""
        chunks: List[Chunk] = []
        messages: List[dict] = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": prompt},
        ]
        for _ in range(self.max_steps):
            resp = self.model.respond(messages, self.tool_schemas)
            for piece in _pieces(resp.get("text", "")):
                chunks.append(Chunk(type=TEXT_DELTA, delta=piece))
            calls = resp.get("tool_calls") or []
            if not calls:
                chunks.append(
                    Chunk(
                        type=FINISH,
                        usage={
                            "inputTokens": est_tokens(prompt),
                            "outputTokens": sum(est_tokens(c) for c in [resp.get("text", "")]),
                            "steps": len([c for c in chunks if c.type == TOOL_CALL]) + 1,
                        },
                    )
                )
                return chunks
            for i, call in enumerate(calls):
                call_id = "call_%d" % (i + 1)
                chunks.append(
                    Chunk(
                        type=TOOL_CALL,
                        tool_call_id=call_id,
                        tool_name=call["name"],
                        args=call.get("args") or {},
                    )
                )
                fn = self.tools.get(call["name"])
                if fn is None:
                    result = "未知工具 %s" % call["name"]
                else:
                    result = fn(**(call.get("args") or {}))
                chunks.append(Chunk(type=TOOL_RESULT, tool_call_id=call_id, result=result))
                messages.append({"role": "assistant", "content": resp.get("text", "")})
                messages.append(
                    {"role": "tool", "name": call["name"], "content": str(result)}
                )
        chunks.append(Chunk(type=FINISH, usage={"steps": self.max_steps}))
        return chunks


class UseChat:
    """客户端钩子：一个吃分片、维护消息状态的纯状态机。

    useChat 的价值不在"发请求"，在**把协议翻译成 UI 状态**：
    文本拼进当前消息，工具调用挂成当前消息的一个 part，
    finish 到了才把消息标记为完成。
    """

    def __init__(self, id_: str = "msg_1") -> None:
        self.messages: List[Message] = []
        self._current: Optional[Message] = None
        self._seq = 0
        self.usage: Dict[str, int] = {}
        self._id = id_

    def _next_id(self) -> str:
        self._seq += 1
        return "%s_part_%d" % (self._id, self._seq)

    def consume(self, chunks: Iterable[Chunk]) -> List[Message]:
        for c in chunks:
            if c.type in (TEXT_DELTA, TOOL_CALL):
                # 文本和工具调用都挂在"当前这条 assistant 消息"上
                if self._current is None:
                    self._current = Message(id=self._next_id(), role="assistant")
                    self.messages.append(self._current)
                if c.type == TEXT_DELTA:
                    self._current.parts.append({"type": TEXT_DELTA, "delta": c.delta})
                else:
                    self._current.parts.append(
                        {
                            "type": TOOL_CALL,
                            "tool_call_id": c.tool_call_id,
                            "tool_name": c.tool_name,
                            "args": c.args,
                        }
                    )
            elif c.type == TOOL_RESULT:
                # 工具结果同样挂在当前消息上——不是另起一条消息
                if self._current is None:
                    self._current = Message(id=self._next_id(), role="assistant")
                    self.messages.append(self._current)
                self._current.parts.append(
                    {"type": TOOL_RESULT, "tool_call_id": c.tool_call_id, "result": c.result}
                )
            elif c.type == FINISH:
                if self._current is not None:
                    self._current.status = "complete"
                self.usage = dict(c.usage)
        return self.messages


# ==========================================================================
# 路线二：Mastra 风格 —— Agent / Workflow / Memory / Eval 四层
# ==========================================================================


@dataclass
class Span:
    """观测 span。OpenTelemetry 的形状。"""

    name: str
    kind: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    children: List["Span"] = field(default_factory=list)


class Tracer:
    """极简 tracer。TS 侧通常直接接 OTel SDK。"""

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

    def render(self, node: Optional[Span] = None, indent: int = 0) -> None:
        node = node or self.root
        if node is None:
            return
        extra = ""
        if node.attributes:
            extra = "  " + ", ".join("%s=%s" % (k, v) for k, v in node.attributes.items())
        print("  %s%s [%s]%s" % ("  " * indent, node.name, node.kind, extra))
        for c in node.children:
            self.render(c, indent + 1)


@dataclass
class StepOutput:
    """步骤的输出，也是下一步的输入。TS 里这一步是 Zod schema 校验过的。"""

    data: Dict[str, Any] = field(default_factory=dict)
    suspend: bool = False
    resume_value: Any = None


@dataclass
class Step:
    """工作流的一步。"""

    name: str
    run: Callable[[Dict[str, Any], Optional[Any]], StepOutput]
    schema: Dict[str, Any] = field(default_factory=dict)


class Workflow:
    """Mastra 风格的工作流：显式控制流 + 挂起/恢复。

    和第 12 章的状态图比，它更"顺序 + 分支"，少一层状态归约的抽象。
    """

    def __init__(self, name: str, trigger_schema: Dict[str, Any]) -> None:
        self.name = name
        self.trigger_schema = trigger_schema
        self.steps: List[Step] = []
        self.branches: Dict[str, List[Step]] = {}
        self._router: Optional[Step] = None

    def then(self, step: Step) -> "Workflow":
        self.steps.append(step)
        return self

    def branch(self, router: Step, mapping: Dict[str, List[Step]]) -> "Workflow":
        """按 router 的返回值选一组后续步骤。"""
        self._router = router
        self.branches = mapping
        return self

    def run(self, trigger: Dict[str, Any], tracer: Optional[Tracer] = None) -> Dict[str, Any]:
        tracer = tracer or Tracer()
        state: Dict[str, Any] = dict(trigger)
        tracer.start("workflow", "workflow", name=self.name)
        try:
            return self._execute(state, 0, None, tracer)
        finally:
            tracer.end()

    def _execute(
        self,
        state: Dict[str, Any],
        start_index: int,
        resume_value: Optional[Any],
        tracer: Tracer,
    ) -> Dict[str, Any]:
        """从第 start_index 步开始跑。resume_value 只给那一步用。"""
        for i, step in enumerate(self.steps):
            if i < start_index:
                continue
            value = resume_value if (i == start_index and resume_value is not None) else None
            out = self._run_step(step, state, value, tracer)
            state.update(out.data)
            if out.suspend:
                state["__suspended_at"] = step.name
                state["__suspended_index"] = i
                return state
        if self._router is not None:
            out = self._run_step(self._router, state, None, tracer)
            state.update(out.data)
            label = state.pop("__route__", "")
            for step in self.branches.get(label, []):
                sout = self._run_step(step, state, None, tracer)
                state.update(sout.data)
                if sout.suspend:
                    state["__suspended_at"] = step.name
                    return state
        return state

    def resume(self, state: Dict[str, Any], value: Any, tracer: Optional[Tracer] = None) -> Dict[str, Any]:
        """从挂起点恢复——把人工的答复喂给那一步，然后**继续往下跑**。"""
        tracer = tracer or Tracer()
        tracer.start("workflow.resume", "workflow", name=self.name)
        try:
            idx = int(state.get("__suspended_index", 0))
            step = self.steps[idx] if idx < len(self.steps) else None
            if step is None:
                return state
            out = self._run_step(step, state, value, tracer)
            state.update(out.data)
            state.pop("__suspended_at", None)
            state.pop("__suspended_index", None)
            return self._execute(state, idx + 1, None, tracer)
        finally:
            tracer.end()

    @staticmethod
    def _run_step(
        step: Step, state: Dict[str, Any], resume_value: Optional[Any], tracer: Tracer
    ) -> StepOutput:
        tracer.start("step", "step", name=step.name)
        try:
            return step.run(state, resume_value)
        finally:
            tracer.end()


class Memory:
    """Mastra 风格的记忆：资源 + 线程 + 语义召回。

    记两件事：最近几条消息（lastMessages）、语义上相关的旧消息（semanticRecall）。
    """

    def __init__(self) -> None:
        self._store: Dict[str, List[dict]] = defaultdict(list)

    def save(self, resource: str, thread: str, role: str, content: str) -> None:
        self._store["%s::%s" % (resource, thread)].append({"role": role, "content": content})

    def recall(
        self,
        resource: str,
        thread: str,
        query: str,
        last_messages: int = 4,
        semantic_top_k: int = 2,
    ) -> List[dict]:
        items = self._store.get("%s::%s" % (resource, thread), [])
        recent = items[-last_messages:] if last_messages else []
        qtok = set(_terms(query))
        scored: List[Tuple[float, dict]] = []
        for it in items[: max(0, len(items) - last_messages)]:
            overlap = len(qtok & set(_terms(it["content"])))
            if overlap:
                scored.append((overlap, it))
        scored.sort(key=lambda x: -x[0])
        related = [it for _, it in scored[:semantic_top_k]]
        # 先放相关的旧消息，再放最近的——顺序即时间线
        return related + recent


def _terms(text: str) -> List[str]:
    text = text.lower()
    out = [t for t in re.findall(r"[a-z0-9]+", text) if len(t) > 1]
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    out.extend(cjk[i : i + 2] for i in range(len(cjk) - 1))
    return out


@dataclass
class EvalResult:
    """评测结果。"""

    name: str
    passed: bool
    score: float
    reason: str


def accuracy_scorer(rubric: List[str], context: str, output: str) -> EvalResult:
    """一个简单的准确性打分器：rubric 里的每条要点，命中一条算一条。

    TS 侧 Mastra 的 Eval 也是同样的形状——打分器是可插拔的，
    准确率、忠实度、相关性各一个。
    """
    hits = [r for r in rubric if r and r in output]
    score = len(hits) / len(rubric) if rubric else 0.0
    reason = "命中 %d/%d 条要点：%s" % (
        len(hits),
        len(rubric),
        "、".join(hits) if hits else "无",
    )
    return EvalResult(name="accuracy", passed=score >= 0.99, score=score, reason=reason)


# ==========================================================================
# 路线三：沙箱策略对比
# ==========================================================================


OPS = [
    ("read_file", "读任意路径的文件"),
    ("write_file", "写任意路径的文件"),
    ("net_request", "发起网络请求"),
    ("spawn_process", "再起一个子进程"),
    ("read_env", "读进程环境变量（含密钥）"),
    ("alloc_memory", "申请超大内存把宿主撑爆"),
]


@dataclass
class Policy:
    """一种隔离策略。"""

    name: str
    allowed: Dict[str, bool]
    note: str


SANDBOX_POLICIES: List[Policy] = [
    Policy(
        name="Node 子进程",
        allowed={op: True for op, _ in OPS},
        note="没有权限模型。只有 --max-old-space-size 能限内存，靠超时杀进程；文件和网络完全敞开。",
    ),
    Policy(
        name="Deno 权限模型",
        allowed={
            "read_file": True,
            "write_file": True,
            "net_request": True,
            "spawn_process": False,
            "read_env": False,
            "alloc_memory": True,
        },
        note="默认全禁，逐项 --allow-read/--allow-write/--allow-net/--allow-run 打开。能控文件和网络，仍限不住内存。",
    ),
    Policy(
        name="WASM 沙箱",
        allowed={
            "read_file": False,
            "write_file": False,
            "net_request": False,
            "spawn_process": False,
            "read_env": False,
            "alloc_memory": True,
        },
        note="guest 只能调用宿主显式导出的能力，内存有上限。隔离最严，代价是要重写一批宿主 API 绑定。",
    ),
    Policy(
        name="容器 / 托管沙箱",
        allowed={
            "read_file": True,
            "write_file": True,
            "net_request": True,
            "spawn_process": True,
            "read_env": False,
            "alloc_memory": True,
        },
        note="文件系统与网络按需挂载，资源用量可封顶。TS 生态最终大多落到这一层。",
    ),
]


# ==========================================================================
# 模型层：mock 与 live
# ==========================================================================


class ModelBase:
    def respond(self, messages: List[dict], tools: List[dict]) -> dict:
        raise NotImplementedError


class MockModel(ModelBase):
    """离线 mock：按脚本返回。"""

    def __init__(self, script: Optional[List[dict]] = None) -> None:
        self.script = list(script or [])
        self.calls = 0

    def respond(self, messages: List[dict], tools: List[dict]) -> dict:
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return {"text": "（脚本用尽）"}


class LiveModel(ModelBase):
    """真实模型：走 OpenAI 兼容的 chat completions 端点。"""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.calls = 0

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

    def respond(self, messages: List[dict], tools: List[dict]) -> dict:
        self.calls += 1
        msgs = list(messages)
        if tools:
            msgs.append(
                {
                    "role": "user",
                    "content": "可用工具的 JSON schema：%s" % json.dumps(tools, ensure_ascii=False),
                }
            )
        raw = self._chat(msgs)
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if m:
            try:
                data = json.loads(m.group(0))
                return {
                    "text": str(data.get("content") or ""),
                    "tool_calls": list(data.get("tool_calls") or []),
                }
            except json.JSONDecodeError:
                pass
        return {"text": raw}


def build_model(live: bool, args) -> ModelBase:
    if not live:
        return MockModel(
            [
                {"text": "我来查一下订单。", "tool_calls": [{"name": "lookup_order", "args": {"order_id": "A-91"}}]},
                {"text": "订单 A-91 已发货，运单 SF7788，预计明天送达。"},
            ]
        )
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        print("错误：--live 需要环境变量 LLM_API_KEY", file=sys.stderr)
        raise SystemExit(2)
    base_url = args.base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model = args.model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    return LiveModel(api_key, base_url, model)


# ==========================================================================
# 工具
# ==========================================================================


def lookup_order(order_id: str) -> str:
    """按订单号查订单状态。"""
    return {
        "A-91": "已发货，运单 SF7788，预计明天送达",
        "A-92": "已退款，退款金额 320 元",
    }.get(order_id, "订单不存在")


def refund(order_id: str) -> str:
    """发起退款。"""
    return "已受理 %s 的退款申请" % order_id


# ==========================================================================
# 演示
# ==========================================================================


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def run_stream(live: bool, trace: bool, args) -> None:
    hr("场景一：流式协议 + useChat 客户端状态机")
    model = build_model(live, args)
    agent = ToolCallingAgent(
        model=model,
        tools=[lookup_order, refund],
        system="你是订单助手。",
    )

    chunks = agent.stream("订单 A-91 到哪了？")

    print("服务端吐出的协议分片（共 %d 个）：" % len(chunks))
    for c in chunks:
        detail = ""
        if c.type == TEXT_DELTA:
            detail = repr(c.delta)
        elif c.type == TOOL_CALL:
            detail = "%s %s" % (c.tool_name, json.dumps(c.args, ensure_ascii=False))
        elif c.type == TOOL_RESULT:
            detail = repr(c.result)
        elif c.type == FINISH:
            detail = json.dumps(c.usage, ensure_ascii=False)
        print("  %-12s %s" % (c.type, detail))

    chat = UseChat()
    messages = chat.consume(chunks)

    print("\n客户端 useChat 还原出的消息状态：")
    for m in messages:
        print("  消息 %s（role=%s, status=%s）" % (m.id, m.role, m.status))
        print("    文本     ：%s" % m.text)
        print("    工具调用 ：%s" % [tc["tool_name"] for tc in m.tool_calls])
        print("    工具结果 ：%s" % [tr["result"] for tr in m.tool_results])
        print("    parts 数 ：%d（文本和工具调用是同一条消息的 part）" % len(m.parts))
    print("  usage：%s" % chat.usage)

    print(
        "\n要点：\n"
        "  1) 服务端和客户端之间只有协议分片这一层契约，模型换成谁都无所谓；\n"
        "  2) 工具调用是**消息的一个 part**，不是另起一条消息——UI 才好把它渲染成\n"
        "     '正在调用 lookup_order' 的卡片；\n"
        "  3) useChat 的全部价值就是这个状态机：吃分片、维护消息、标记完成。"
    )


def build_mastra_workflow(memory: Memory, tracer: Tracer) -> Workflow:
    """Mastra 风格的四层拼装。"""

    def step_research(state: Dict[str, Any], resume: Optional[Any]) -> StepOutput:
        rows = lookup_order(state.get("order_id", ""))
        return StepOutput(data={"research": rows})

    def step_gate(state: Dict[str, Any], resume: Optional[Any]) -> StepOutput:
        # 这一步需要人批：挂起，等 resume
        if resume is None:
            return StepOutput(data={}, suspend=True)
        if resume == "approve":
            return StepOutput(data={"approved": True})
        return StepOutput(data={"approved": False, "note": "被驳回"})

    def step_write(state: Dict[str, Any], resume: Optional[Any]) -> StepOutput:
        text = "简报：订单 %s %s" % (state.get("order_id"), state.get("research"))
        return StepOutput(data={"brief": text})

    def step_report(state: Dict[str, Any], resume: Optional[Any]) -> StepOutput:
        return StepOutput(data={"report": "未批准，未执行任何写操作"})

    def step_router(state: Dict[str, Any], resume: Optional[Any]) -> StepOutput:
        label = "write" if state.get("approved") else "report"
        return StepOutput(data={"__route__": label})

    wf = Workflow("订单简报", trigger_schema={"order_id": "string"})
    wf.then(Step("research", step_research))
    wf.then(Step("approval", step_gate))
    wf.branch(
        Step("router", step_router),
        {
            "write": [Step("write", step_write)],
            "report": [Step("report", step_report)],
        },
    )
    return wf


def run_mastra(live: bool, trace: bool, args) -> None:
    hr("场景二：Mastra 风格的四层 —— Workflow / Memory / Eval / 观测")
    memory = Memory()
    tracer = Tracer()

    # --- Workflow：顺序 + 挂起 + 分支 ---
    wf = build_mastra_workflow(memory, tracer)
    state = wf.run({"order_id": "A-91"}, tracer)
    print("第一次运行（走到审批就挂起）：")
    print("  挂起点     ：%s" % state.get("__suspended_at"))
    print("  已算出     ：research=%s" % state.get("research"))

    state = wf.resume(state, "approve", tracer)
    print("人工答复 approve 后恢复：")
    print("  approved   ：%s" % state.get("approved"))
    print("  brief      ：%s" % state.get("brief"))

    # --- Memory：线程记忆 + 语义召回 ---
    # 先记两轮旧对话，再记一轮新的——查询时"最近消息"和"语义相关的旧消息"才会分开
    memory.save("user_42", "thread_1", "user", "订单 A-92 什么时候退款？")
    memory.save("user_42", "thread_1", "assistant", "订单 A-92 已退款，退款金额 320 元")
    memory.save("user_42", "thread_1", "user", "订单 A-91 到哪了？")
    memory.save("user_42", "thread_1", "assistant", "订单 A-91 已发货，运单 SF7788")
    recalled = memory.recall("user_42", "thread_1", "那退款呢？", last_messages=2, semantic_top_k=1)
    print("\n记忆召回（第二轮只说了「那退款呢？」）：")
    for r in recalled:
        tag = "语义召回" if "退款" in r["content"] else "最近消息"
        print("  [%s] %s" % (tag, r["content"]))

    # --- Eval：打分 ---
    result = accuracy_scorer(
        rubric=["已发货", "运单"],
        context="订单状态",
        output=state.get("brief", ""),
    )
    print("\n评测：")
    print("  打分器     ：%s" % result.name)
    print("  是否通过   ：%s（score=%.2f）" % (result.passed, result.score))
    print("  理由       ：%s" % result.reason)

    print("\n观测 span 树：")
    tracer.render()

    print(
        "\n要点：\n"
        "  1) 四层是分开的：Agent 管模型与工具，Workflow 管控制流（含挂起/恢复），\n"
        "     Memory 管上下文，Eval 管质量闸门；\n"
        "  2) 工作流比状态图轻：顺序 + 分支 + 挂起，没有归约那层抽象；\n"
        "  3) 记忆分两块：最近几条消息 + 语义上相关的旧消息，后者才是'跨轮记忆'；\n"
        "  4) 观测是内建能力——每个步骤一个 span，不用自己埋点。"
    )


def run_sandbox(live: bool, trace: bool, args) -> None:
    hr("场景三：TS 在沙箱与本地执行上的短板与四条出路")
    op_names = [op for op, _ in OPS]
    header = "%-14s" % "操作"
    for p in SANDBOX_POLICIES:
        header += "%-16s" % p.name
    print(header)
    print("-" * len(header))
    for i, (op, desc) in enumerate(OPS):
        row = "%-14s" % op
        for p in SANDBOX_POLICIES:
            row += "%-16s" % ("放行" if p.allowed.get(op) else "拦截")
        print(row)
    print("-" * len(header))
    print(
        "注：alloc_memory 一栏都是「放行」，但含义不同——WASM 有 guest 线性内存上限，\n"
        "    容器有 cgroup 限额；只有 Node 是「申请到 OOM 为止」，且 OOM 掉的是宿主进程。"
    )
    print("\n各策略的边界：")
    for p in SANDBOX_POLICIES:
        print("  【%s】%s" % (p.name, p.note))

    print(
        "\n要点：\n"
        "  1) Node 的子进程默认什么都能干——没有权限模型，只有 --max-old-space-size\n"
        "     能限内存，超时能杀进程；文件、网络、环境变量全敞开；\n"
        "  2) worker_threads 不是安全边界：它和主线程共享同一个进程，内存和文件权限一样共享；\n"
        "  3) Deno 的权限模型是最小改动的一档（默认全禁，逐项打开），但仍限不住内存；\n"
        "  4) WASM 隔离最严，代价是宿主 API 要显式绑定；\n"
        "  5) 现实里 TS 生态最终大多落到容器或托管沙箱——OpenAI Agents SDK 的沙箱 Agent\n"
        "     也是这么给的：本地客户端覆盖 macOS/Linux，Windows 走 Docker 或托管。\n"
        "     这正是 TS 相比 Python 的一处真实短板：Python 那边的 subprocess + 资源限制\n"
        "     是一条更短的路径。"
    )


# ==========================================================================
# CLI
# ==========================================================================


def main() -> int:
    ap = argparse.ArgumentParser(description="ch18 TypeScript 生态最小可实现")
    ap.add_argument("--demo", choices=["all", "stream", "mastra", "sandbox"], default="all")
    ap.add_argument("--trace", action="store_true", help="打印完整协议分片与 span 树")
    ap.add_argument("--live", action="store_true", help="切真实模型 API")
    ap.add_argument("--model", default=None, help="覆盖 LLM_MODEL")
    ap.add_argument("--base-url", default=None, help="覆盖 LLM_BASE_URL")
    args = ap.parse_args()

    if args.live:
        build_model(True, args)  # 先检查凭据，别跑到一半才报错

    if args.demo in ("all", "stream"):
        run_stream(args.live, args.trace, args)
    if args.demo in ("all", "mastra"):
        run_mastra(args.live, args.trace, args)
    if args.demo in ("all", "sandbox"):
        run_sandbox(args.live, args.trace, args)

    hr("跑完了")
    print("这个实现里有五个可以抄走的结论：")
    print("  1) UI 优先的生态，模型和 UI 之间只隔一层协议分片——换模型不动前端；")
    print("  2) 工具调用是消息的一个 part，不是独立消息，UI 才渲染得对；")
    print("  3) Agent / Workflow / Memory / Eval 分层，是 TS 侧框架的共同形状；")
    print("  4) 记忆 = 最近消息 + 语义相关旧消息，两块要分开设计；")
    print("  5) TS 的沙箱是短板：Node 没有权限模型，最后多半落到容器或托管沙箱。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
