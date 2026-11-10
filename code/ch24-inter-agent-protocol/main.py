#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch24 Agent 间通信协议：MCP、A2A 与内部消息设计 最小可实现
—— 纯标准库，无需 API Key 就能跑通完整回路。

演示内容：
  1) MCP 对象模型：server / client / tools / resources / prompts，
     以及工具 schema 如何直接变成 Function Calling 的 tools 参数
  2) A2A 核心抽象：AgentCard / Task / Message / Artifact，委托与流式推进
  3) 两者组合：MCP 负责供能（上下文供给层），A2A 负责委托（委托执行层）
  4) 内部消息契约：自然语言消息 vs 结构化 envelope 的解析成功率与开销，
     以及契约演进时的向后兼容

用法：
    python main.py                      # 跑全部演示
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
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

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
# 2. MCP 对象模型
# --------------------------------------------------------------------------


@dataclass
class Tool:
    """可调用的动作：有名字、有描述、有入参 schema。"""
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Any = None


@dataclass
class Resource:
    """可读取的上下文：由 URI 寻址，不一定有副作用。"""
    uri: str
    name: str
    mime_type: str
    content: str


@dataclass
class Prompt:
    """可复用的提示模板：带参数，渲染成一段上下文。"""
    name: str
    description: str
    arguments: List[str]
    template: str


class McpServer:
    """MCP 服务端：对外暴露 tools / resources / prompts 三类原语。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.tools: Dict[str, Tool] = {}
        self.resources: Dict[str, Resource] = {}
        self.prompts: Dict[str, Prompt] = {}

    def register_tool(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def register_resource(self, res: Resource) -> None:
        self.resources[res.uri] = res

    def register_prompt(self, prompt: Prompt) -> None:
        self.prompts[prompt.name] = prompt

    def list_tools(self) -> List[Dict[str, Any]]:
        return [{"name": t.name, "description": t.description,
                 "inputSchema": t.input_schema} for t in self.tools.values()]

    def list_resources(self) -> List[Dict[str, Any]]:
        return [{"uri": r.uri, "name": r.name, "mimeType": r.mime_type}
                for r in self.resources.values()]

    def list_prompts(self) -> List[Dict[str, Any]]:
        return [{"name": p.name, "description": p.description,
                 "arguments": p.arguments} for p in self.prompts.values()]

    def call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        tool = self.tools.get(name)
        if tool is None:
            return {"content": [{"type": "text", "text": "未知工具：%s" % name}],
                    "isError": True}
        try:
            text = tool.handler(**args)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as exc:
            return {"content": [{"type": "text",
                                 "text": "工具执行失败：%s" % exc}], "isError": True}

    def read_resource(self, uri: str) -> Dict[str, Any]:
        res = self.resources.get(uri)
        if res is None:
            return {"contents": [], "isError": True}
        return {"contents": [{"uri": res.uri, "mimeType": res.mime_type,
                              "text": res.content}], "isError": False}

    def render_prompt(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self.prompts.get(name)
        if p is None:
            return {"messages": [], "isError": True}
        missing = [a for a in p.arguments if a not in args]
        if missing:
            return {"messages": [], "isError": True}
        text = p.template
        for k, v in args.items():
            text = text.replace("{%s}" % k, str(v))
        return {"messages": [{"role": "user", "content": {"type": "text", "text": text}}],
                "isError": False}


class McpClient:
    """MCP 客户端：发现能力、调用工具、读资源、取提示。"""

    def __init__(self, server: McpServer) -> None:
        self.server = server

    def list_tools(self) -> List[Dict[str, Any]]:
        return self.server.list_tools()

    def list_resources(self) -> List[Dict[str, Any]]:
        return self.server.list_resources()

    def list_prompts(self) -> List[Dict[str, Any]]:
        return self.server.list_prompts()

    def tools_for_function_calling(self) -> List[Dict[str, Any]]:
        """把 MCP 工具 schema 转成 Function Calling 的 tools 参数。

        这就是 MCP 与 Function Calling 的接口点：MCP 负责"能力从哪来、怎么描述、
        怎么被找到"，Function Calling 负责"模型怎么选、怎么填参数"。
        """
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["inputSchema"]}}
                for t in self.list_tools()]

    def call(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        return self.server.call_tool(name, args)

    def read(self, uri: str) -> Dict[str, Any]:
        return self.server.read_resource(uri)

    def prompt(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        return self.server.render_prompt(name, args)


# --------------------------------------------------------------------------
# 3. A2A 核心抽象
# --------------------------------------------------------------------------


@dataclass
class AgentSkill:
    id: str
    name: str
    description: str
    tags: List[str] = field(default_factory=list)


@dataclass
class AgentCard:
    """能力发现的载体：身份、端点、能力声明与技能清单。"""
    name: str
    description: str
    version: str
    url: str
    capabilities: Dict[str, Any] = field(default_factory=dict)
    skills: List[AgentSkill] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "version": self.version, "url": self.url,
                "capabilities": self.capabilities,
                "skills": [{"id": s.id, "name": s.name,
                            "description": s.description, "tags": s.tags}
                           for s in self.skills]}


@dataclass
class Message:
    """消息：对话与委托的基本单元。"""
    message_id: str
    role: str                       # user | agent
    parts: List[Dict[str, Any]]
    task_id: str = ""
    context_id: str = ""
    kind: str = "message"           # message | status | artifact


@dataclass
class Artifact:
    """产物：任务最终交付的东西，可以被引用、下载、复用。"""
    artifact_id: str
    name: str
    parts: List[Dict[str, Any]]


@dataclass
class Task:
    """任务：有状态机，可跨消息推进。"""
    id: str
    context_id: str
    status: str = "submitted"       # submitted | working | input-required | completed | failed
    history: List[Message] = field(default_factory=list)
    artifacts: List[Artifact] = field(default_factory=list)


# --------------------------------------------------------------------------
# 4. mock 与真实模型
# --------------------------------------------------------------------------


class MockLLM:
    name = "mock"

    def __init__(self) -> None:
        self.meter = TokenMeter()

    def chat(self, role: str, system: str, user: str) -> str:
        self.meter.record_call()
        self.meter.add(approx_tokens(system) + approx_tokens(user), 0)
        text = ("远端处理结果：客户 C001 有 3 笔待核对订单，"
                "其中订单 A123 的退款金额 200.00 元与台账 180.00 元不一致，"
                "差额 20.00 元，原因是跨月退款未同步台账。")
        self.meter.add(0, approx_tokens(text))
        return text


class LiveLLM:
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
# 5. 场景：退款核对 Agent + MCP 服务端
# --------------------------------------------------------------------------

ORDERS = {
    "C001": [{"order_id": "A123", "amount_cents": 20000, "ledger_cents": 18000,
              "status": "refund_pending"}],
}


def build_mcp_server() -> McpServer:
    server = McpServer("refund-ops")

    def query_orders(customer_id: str, status: str = "refund_pending") -> str:
        rows = [o for o in ORDERS.get(customer_id, []) if o["status"] == status]
        return json.dumps({"customer_id": customer_id, "orders": rows},
                          ensure_ascii=False)

    def compare_ledger(order_id: str, amount_cents: int) -> str:
        for rows in ORDERS.values():
            for o in rows:
                if o["order_id"] == order_id:
                    diff = o["amount_cents"] - o["ledger_cents"]
                    return json.dumps({"order_id": order_id, "diff_cents": diff,
                                       "match": diff == 0}, ensure_ascii=False)
        raise ValueError("订单不存在：%s" % order_id)

    server.register_tool(Tool(
        name="query_orders",
        description="按客户号查询待核对的退款订单列表",
        input_schema={"type": "object",
                      "properties": {"customer_id": {"type": "string"},
                                     "status": {"type": "string"}},
                      "required": ["customer_id"]},
        handler=query_orders))
    server.register_tool(Tool(
        name="compare_ledger",
        description="把订单退款金额与系统台账逐笔比对，返回差额",
        input_schema={"type": "object",
                      "properties": {"order_id": {"type": "string"},
                                     "amount_cents": {"type": "integer"}},
                      "required": ["order_id", "amount_cents"]},
        handler=compare_ledger))
    server.register_resource(Resource(
        uri="orders://customer/C001/raw", name="C001 原始订单",
        mime_type="application/json",
        content=json.dumps(ORDERS["C001"], ensure_ascii=False)))
    server.register_resource(Resource(
        uri="policy://refund/domestic", name="境内退款政策",
        mime_type="text/markdown",
        content="境内退款：金额与台账不一致时，以业务单据为准，差额需人工复核。"))
    server.register_prompt(Prompt(
        name="refund_review",
        description="退款差异复核提示模板",
        arguments=["customer_id", "policy"],
        template="你是退款核对员。客户号 {customer_id} 的待核对订单如下政策执行：{policy}。"
                 "只输出差异结论。"))
    return server


REFUND_CARD = AgentCard(
    name="refund-review-agent",
    description="负责核对退款订单与系统台账，产出差异结论",
    version="1.2.0",
    url="http://127.0.0.1:9101/agents/refund-review",
    capabilities={"streaming": True, "pushNotifications": False,
                  "stateTransitionHistory": True},
    skills=[
        AgentSkill(id="refund.reconcile", name="退款对账",
                   description="核对退款订单金额与台账，输出差额与原因",
                   tags=["refund", "reconcile"]),
        AgentSkill(id="refund.policy", name="退款政策问答",
                   description="按区域回答退款政策问题", tags=["refund", "policy"]),
    ])


class RemoteAgent:
    """远端 Agent：通过 MCP 客户端取数据，按 A2A 语义推进任务。"""

    def __init__(self, card: AgentCard, mcp: McpClient, llm) -> None:
        self.card = card
        self.mcp = mcp
        self.llm = llm

    def handle(self, message: Message) -> Iterator[Dict[str, Any]]:
        """处理一条消息，流式产出状态更新。"""
        task = Task(id=message.task_id or "task-1",
                    context_id=message.context_id or "ctx-1",
                    status="working", history=[message])
        yield {"task_id": task.id, "status": "working", "note": "已接收委托，正在取数"}
        yield {"task_id": task.id, "status": "working", "note": "已通过 MCP 拉取订单与政策"}

        # 远端自己用 MCP 取工具与资源（这是"供能层"）
        orders = json.loads(self.mcp.call("query_orders", {"customer_id": "C001"})["content"][0]["text"])
        policy = self.mcp.read("policy://refund/domestic")["contents"][0]["text"]
        prompt = self.mcp.prompt("refund_review",
                                 {"customer_id": "C001", "policy": policy})["messages"][0]["content"]["text"]
        first = orders["orders"][0]
        compared = json.loads(self.mcp.call("compare_ledger",
                                            {"order_id": first["order_id"],
                                             "amount_cents": first["amount_cents"]})["content"][0]["text"])
        answer = self.llm.chat("远端 Agent", prompt,
                                "订单：%s；比对结果：%s" % (json.dumps(orders, ensure_ascii=False),
                                                     json.dumps(compared, ensure_ascii=False)))
        yield {"task_id": task.id, "status": "working", "note": "差异结论已生成"}

        artifact = Artifact(artifact_id="art-1", name="refund-diff",
                            parts=[{"kind": "text", "text": answer}])
        task.artifacts.append(artifact)
        task.status = "completed"
        yield {"task_id": task.id, "status": "completed",
               "artifact_id": artifact.artifact_id}


# --------------------------------------------------------------------------
# 6. 内部消息契约：自然语言 vs 结构化
# --------------------------------------------------------------------------


@dataclass
class Envelope:
    """结构化消息信封：版本号 + 类型 + 载荷。"""
    v: int
    kind: str
    payload: Dict[str, Any]
    reply_to: str = ""
    trace_id: str = ""


class ContractParser:
    """按契约解析消息。两种实现：正则猜 vs 按 schema 取。"""

    def __init__(self) -> None:
        self.natural_ok = 0
        self.natural_fail = 0
        self.structured_ok = 0
        self.structured_fail = 0
        self.natural_bytes = 0
        self.structured_bytes = 0
        self.natural_tokens = 0
        self.structured_tokens = 0

    # --- 自然语言：从句子里猜参数 ---

    def parse_natural(self, text: str) -> Optional[Dict[str, Any]]:
        self.natural_bytes += len(text.encode("utf-8"))
        self.natural_tokens += approx_tokens(text)
        cid = re.search(r"客户\s*([A-Za-z0-9]+)", text)
        amt = re.search(r"(\d+(?:\.\d+)?)\s*元", text)
        if not cid or not amt:
            self.natural_fail += 1
            return None
        self.natural_ok += 1
        return {"customer_id": cid.group(1),
                "amount_cents": int(float(amt.group(1)) * 100)}

    # --- 结构化：按信封取字段 ---

    def parse_structured(self, env: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raw = json.dumps(env, ensure_ascii=False)
        self.structured_bytes += len(raw.encode("utf-8"))
        self.structured_tokens += approx_tokens(raw)
        if env.get("v", 0) != 1:
            self.structured_fail += 1
            return None
        payload = env.get("payload") or {}
        if "customer_id" not in payload or "amount_cents" not in payload:
            self.structured_fail += 1
            return None
        self.structured_ok += 1
        return {"customer_id": payload["customer_id"],
                "amount_cents": payload["amount_cents"]}


# --------------------------------------------------------------------------
# 7. 演示
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


def demo_mcp(args: argparse.Namespace) -> McpServer:
    print("=" * 78)
    print("A. MCP 对象模型：tools / resources / prompts")
    print("=" * 78)
    server = build_mcp_server()
    client = McpClient(server)
    print("tools：")
    for t in client.list_tools():
        print("  %-16s %s" % (t["name"], t["description"]))
        print("  %-16s 入参 schema：%s" % ("", json.dumps(t["inputSchema"], ensure_ascii=False)))
    print("resources：")
    for r in client.list_resources():
        print("  %-32s %s" % (r["uri"], r["name"]))
    print("prompts：")
    for p in client.list_prompts():
        print("  %-16s 参数 %s" % (p["name"], "、".join(p["arguments"])))

    print("\nMCP 工具 schema → Function Calling 的 tools 参数：")
    for spec in client.tools_for_function_calling():
        fn = spec["function"]
        print("  %s：%s" % (fn["name"], json.dumps(fn["parameters"], ensure_ascii=False)))
    print("\n工具调用结果包成 role=tool 的消息：")
    result = client.call("query_orders", {"customer_id": "C001"})
    print("  " + json.dumps({"role": "tool", "name": "query_orders",
                             "content": result["content"][0]["text"]}, ensure_ascii=False))
    print("\n读资源：%s" % client.read("policy://refund/domestic")["contents"][0]["text"])
    return server


def demo_a2a(args: argparse.Namespace, server: McpServer) -> None:
    print("=" * 78)
    print("B. A2A：AgentCard 能力发现 + 委托 + 流式推进")
    print("=" * 78)
    print("AgentCard（能力发现的载体）：")
    print(json.dumps(REFUND_CARD.to_json(), ensure_ascii=False, indent=2))

    client = McpClient(server)
    llm = build_llm(args)
    agent = RemoteAgent(REFUND_CARD, client, llm)

    message = Message(message_id="msg-1", role="user",
                       parts=[{"kind": "text", "text": "核对客户 C001 的退款与台账差异"}],
                       task_id="task-1", context_id="ctx-1")
    print("\n委托与流式推进：")
    for update in agent.handle(message):
        print("  status=%-10s %s" % (update["status"], update.get("note", update.get("artifact_id", ""))))
    print("\n产出物 Artifact：%s" % "art-1（退款差异结论，可被引用与复用）")


def demo_layers(args: argparse.Namespace, server: McpServer) -> None:
    print("=" * 78)
    print("C. 两层职责：MCP 供能，A2A 委托")
    print("=" * 78)
    print("""
┌──────────────────────────────────────────────┐
│ 你的 Agent（客户端）                          │
│   读 AgentCard → 发现能力                     │
│   组装 Message → 委托 Task                    │
└───────────────┬──────────────────────────────┘
                │ A2A：委托、状态推进、产出物
┌───────────────▼──────────────────────────────┐
│ 远端 Agent（A2A Server）                       │
│   处理 Task → 产出 Artifact                    │
│   作为 MCP client 取工具与资源                 │
└───────────────┬──────────────────────────────┘
                │ MCP：tools / resources / prompts
┌───────────────▼──────────────────────────────┐
│ MCP Server                                    │
└──────────────────────────────────────────────┘
""")
    client = McpClient(server)
    print("本次委托里，远端 Agent 用 MCP 做了三件事：")
    print("  1) call(query_orders) 取订单   → %s"
          % client.call("query_orders", {"customer_id": "C001"})["content"][0]["text"])
    print("  2) read(policy://refund/domestic) 取政策")
    print("  3) prompt(refund_review) 取提示模板 → %s"
          % client.prompt("refund_review",
                          {"customer_id": "C001",
                           "policy": "境内退款以业务单据为准"})["messages"][0]["content"]["text"][:48])


def demo_contract(args: argparse.Namespace) -> None:
    print("=" * 78)
    print("D. 内部消息契约：自然语言 vs 结构化")
    print("=" * 78)
    parser = ContractParser()

    natural = [
        "请给客户 C001 算一笔 200 元退款并核对台账",
        "帮 C001 也算一下退款",           # 金额缺失，正则解不出来
    ]
    structured = [
        {"v": 1, "kind": "refund.request", "reply_to": "cb-1", "trace_id": "t-1",
         "payload": {"customer_id": "C001", "amount_cents": 20000}},
        {"v": 1, "kind": "refund.request", "reply_to": "cb-2", "trace_id": "t-2",
         "payload": {"customer_id": "C001", "amount_cents": 20000}},
    ]

    for text in natural:
        got = parser.parse_natural(text)
        print("  自然语言：%-34s → %s" % (text, got if got else "解析失败"))
    for env in structured:
        got = parser.parse_structured(env)
        print("  结构化  ：%-34s → %s" % (json.dumps(env["payload"], ensure_ascii=False), got))

    print("\n%s %s %s %s %s" % (_pad("编码方式", 16), _pad("解析成功", 12),
                                 _pad("解析率", 10), _pad("字节数", 10), "token"))
    print("%s %s %s %s %s" % (_pad("自然语言", 16),
                               _pad("%d/%d" % (parser.natural_ok, parser.natural_ok + parser.natural_fail), 12),
                               _pad("%.0f%%" % (100.0 * parser.natural_ok /
                                                max(1, parser.natural_ok + parser.natural_fail)), 10),
                               _pad(str(parser.natural_bytes), 10), parser.natural_tokens))
    print("%s %s %s %s %s" % (_pad("结构化", 16),
                               _pad("%d/%d" % (parser.structured_ok,
                                               parser.structured_ok + parser.structured_fail), 12),
                               _pad("%.0f%%" % (100.0 * parser.structured_ok /
                                                max(1, parser.structured_ok + parser.structured_fail)), 10),
                               _pad(str(parser.structured_bytes), 10), parser.structured_tokens))
    print("\n结构化消息更贵（字段名、信封、字段名重复），换来的是解析确定；"
          "自然语言便宜，但要为每一处歧义留兜底。")

    # 契约演进：加一个可选字段，旧消息仍然能解析
    evolved = {"v": 1, "kind": "refund.request",
               "payload": {"customer_id": "C001", "amount_cents": 20000,
                           "reason": "customer_request"},  # 新增字段
               "reply_to": "cb-3", "trace_id": "t-3"}
    got = parser.parse_structured(evolved)
    print("\n契约演进：新增可选字段 reason 后，旧解析器仍能解析 → %s" % got)
    print("注意：版本号 v 才是真正的兼容边界，字段可选性靠约定，v 变了就得同时升级两端。")


def main() -> None:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description="ch24 Agent 间通信协议：MCP、A2A 与内部消息设计")
    ap.add_argument("--live", action="store_true", help="切真实模型（需 LLM_API_KEY）")
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "gpt-4o-mini"))
    args = ap.parse_args()

    server = demo_mcp(args)
    demo_a2a(args, server)
    demo_layers(args, server)
    demo_contract(args)


if __name__ == "__main__":
    main()
