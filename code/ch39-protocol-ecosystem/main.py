#!/usr/bin/env python3
"""ch39 协议与生态全景 —— 最小可实现参考实现。

一个纯标准库的"五层协议栈"，把 Agent 接入生态时最常打交道的五层各演一遍：

    模型 API 层     一次对话的形状：消息 + 工具调用；模型每轮只输出一个 JSON 动作
    工具上下文层   MCP：server/discover、tools/list、tools/call；参数缺失时返回
                    resultType="input_required" + inputRequests，客户端重试原请求
                    并带上 inputResponses（MRTR 多轮往返模式）
    委托执行层     A2A：agent card 发现对方能力 → tasks/send 委派 → 任务状态流转
                    → 拿到 artifact；对方始终是不透明的
    用户交互层     AG-UI：把整个过程变成事件流（RUN_*/TEXT_MESSAGE_*/TOOL_CALL_*/
                    STATE_SNAPSHOT/SUBAGENT_*），前端只按事件类型渲染
    能力封装层     Skills：启动只加载 name+description（第一级），命中后才读 SKILL.md
                    正文（第二级），正文里引用的脚本/参考资料按需再加载（第三级）

场景：用户问"上个月（2026-10）的工资条是多少"。
全程打印每一层的实际报文（JSON-RPC 信封 / 事件流），最后做两层校验：
    1) 事件流一致性：START/END 配对、RUN 有终态；
    2) 事实一致性：最终答案里的金额来自 A2A 返回的 artifact，不是模型编的。

依赖：仅 Python 标准库（>= 3.8）。默认 mock 模式不需要任何 API Key。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass


USER_QUESTION = "上个月（2026-10）的工资条是多少？"

LAYER_MODEL = "模型 API 层"
LAYER_SKILL = "能力封装层"
LAYER_MCP = "工具上下文层(MCP)"
LAYER_A2A = "委托执行层(A2A)"
LAYER_AGUI = "用户交互层(AG-UI)"

LAYER_ORDER = [LAYER_MODEL, LAYER_SKILL, LAYER_MCP, LAYER_A2A, LAYER_AGUI]


# ---------------------------------------------------------------------------
# 能力封装层：Skills（三级渐进披露）
# ---------------------------------------------------------------------------


@dataclass
class Skill:
    name: str
    description: str
    body: str


class SkillRegistry:
    """Skills 的渐进披露：启动只给 name+description，命中才读正文。"""

    def __init__(self) -> None:
        self.skills = [
            Skill(
                name="payroll-query",
                description="查询员工工资条与实发金额；当用户问工资、薪资、奖金、扣款时使用。",
                body=(
                    "# 工资条查询\n\n"
                    "## 步骤\n"
                    "1. 先用 MCP 工具 payroll.query 查工资条；该工具要求 month 参数。\n"
                    "2. 若工具返回 input_required，按 inputRequests 补齐参数后重试同一请求。\n"
                    "3. 本地 MCP 只知道工资条在财务系统；若返回 needs_delegation，走 A2A 委派。\n"
                ),
            ),
            Skill(
                name="expense-reimburse",
                description="提交差旅报销单；当用户要报销、提交发票、填报销单时使用。",
                body="# 报销\n\n填写表单并上传发票，然后提交。\n",
            ),
            Skill(
                name="onboarding-checklist",
                description="新人入职清单；当用户提到入职、领电脑、开通账号时使用。",
                body="# 入职清单\n\n1. 领电脑 2. 开通账号 3. 找导师\n",
            ),
        ]
        self.loaded: List[str] = []

    def index(self) -> List[str]:
        """第一级：只有 name 和 description 进入上下文。"""
        return [f"{s.name} — {s.description}" for s in self.skills]

    def load(self, name: str) -> str:
        """第二级：命中后读 SKILL.md 正文。"""
        for skill in self.skills:
            if skill.name == name:
                if name not in self.loaded:
                    self.loaded.append(name)
                return skill.body
        raise KeyError(name)


# ---------------------------------------------------------------------------
# 工具上下文层：MCP
# ---------------------------------------------------------------------------


class McpServer:
    """一个最小 MCP 服务器：发现、列工具、调工具。

    关键：参数缺失时不报错，而是返回 resultType="input_required" + inputRequests，
    客户端在重试原请求时用 inputResponses 补齐（MRTR 多轮往返）。
    """

    def __init__(self) -> None:
        self.req_id = 0
        self.requests: List[Dict[str, Any]] = []

    def _envelope(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.req_id += 1
        return {"jsonrpc": "2.0", "id": self.req_id, "method": method, "params": params}

    def handle(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        envelope = self._envelope(method, params)
        self.requests.append(envelope)
        if method == "server/discover":
            result = {
                "resultType": "complete",
                "protocolVersion": "2026-07-28",
                "serverInfo": {"name": "hr-local", "version": "0.4.0"},
                "capabilities": {"tools": {"listChanged": True}},
            }
        elif method == "tools/list":
            result = {
                "resultType": "complete",
                "tools": [{
                    "name": "payroll.query",
                    "description": "按月份查工资条；返回工资条所在系统与实发金额",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"month": {"type": "string", "description": "工资月份 YYYY-MM"}},
                        "required": ["month"],
                    },
                }],
                "ttlMs": 60000,
                "cacheScope": "private",
            }
        elif method == "tools/call":
            result = self._call_tool(params)
        else:
            result = {"resultType": "complete", "error": f"未知方法 {method}"}
        return {"jsonrpc": "2.0", "id": envelope["id"], "result": result}

    def _call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name", "")
        args = dict(params.get("arguments") or {})
        responses = params.get("inputResponses") or {}
        args.update(responses)          # MRTR：重试时由客户端补齐
        if name != "payroll.query":
            return {"resultType": "complete", "content": f"未知工具 {name}"}
        if not args.get("month"):
            return {
                "resultType": "input_required",
                "inputRequests": [{
                    "name": "month",
                    "description": "工资月份，格式 YYYY-MM",
                    "required": True,
                }],
            }
        return {
            "resultType": "complete",
            "content": f"本地系统只存了 {args['month']} 的工资条索引，明细在财务 Agent 侧，需要委派。",
            "needs_delegation": True,
            "delegate_to": "finance-agent",
        }


# ---------------------------------------------------------------------------
# 委托执行层：A2A
# ---------------------------------------------------------------------------


class A2AAgent:
    """一个最小 A2A 对端：agent card + 任务生命周期 + artifact。

    对端始终不透明：不暴露内部记忆、工具或专有逻辑，只给结果。
    """

    def __init__(self) -> None:
        self.card_data = {
            "name": "finance-agent",
            "description": "财务系统 Agent，持有工资条、报销单与个税计算",
            "version": "1.2.0",
            "url": "https://finance.internal.example/a2a",
            "skills": [
                {"id": "payslip", "name": "工资条查询", "description": "按月份返回工资条明细"},
                {"id": "tax", "name": "个税计算", "description": "按年度累计预扣预缴计算个税"},
            ],
            "protocolVersion": "1.0",
        }
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.task_seq = 0
        self.signed = True       # v1.0 起支持 Signed Agent Card

    def agent_card(self) -> Dict[str, Any]:
        return dict(self.card_data)

    def send_task(self, message: str, skill: str = "payslip") -> Dict[str, Any]:
        self.task_seq += 1
        task_id = f"task-{self.task_seq}"
        task = {
            "id": task_id,
            "contextId": "ctx-payroll",
            "status": "submitted",
            "history": [{"status": "submitted", "message": message}],
        }
        self.tasks[task_id] = task
        # 状态流转：submitted -> working -> completed
        task["status"] = "working"
        task["history"].append({"status": "working"})
        task["status"] = "completed"
        task["history"].append({"status": "completed"})
        task["artifacts"] = [{
            "artifactId": "a-1",
            "name": "payslip",
            "parts": [{
                "kind": "text",
                "text": "月份 2026-10｜应发 18642.50｜实发 18236.80｜个税已代扣 405.70",
            }],
        }]
        return task


# ---------------------------------------------------------------------------
# 用户交互层：AG-UI 事件流
# ---------------------------------------------------------------------------


class EventStream:
    """AG-UI 风格的事件流：前端只按事件类型渲染，不理解 Agent 内部。"""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        self.seq = 0

    def emit(self, event_type: str, **payload: Any) -> Dict[str, Any]:
        self.seq += 1
        event = {"type": event_type, "timestamp": self.seq}
        event.update(payload)
        self.events.append(event)
        return event

    # -- 便捷方法 ---------------------------------------------------------
    def run_started(self, thread_id: str, run_id: str, input_text: str) -> None:
        self.emit("RUN_STARTED", threadId=thread_id, runId=run_id, input=input_text)

    def text(self, message_id: str, content: str) -> None:
        self.emit("TEXT_MESSAGE_START", messageId=message_id)
        for chunk in re.findall(r".{1,24}", content) or [content]:
            self.emit("TEXT_MESSAGE_CONTENT", messageId=message_id, delta=chunk)
        self.emit("TEXT_MESSAGE_END", messageId=message_id)

    def tool_call(self, call_id: str, name: str, args: Dict[str, Any], result: str,
                  subagent_run_id: Optional[str] = None) -> None:
        extra = {"subagentRunId": subagent_run_id} if subagent_run_id else {}
        self.emit("TOOL_CALL_START", toolCallId=call_id, toolCallName=name, **extra)
        self.emit("TOOL_CALL_ARGS", toolCallId=call_id, delta=json.dumps(args, ensure_ascii=False), **extra)
        self.emit("TOOL_CALL_END", toolCallId=call_id, **extra)
        self.emit("TOOL_CALL_RESULT", toolCallId=call_id, content=result, **extra)

    def state(self, snapshot: Dict[str, Any]) -> None:
        self.emit("STATE_SNAPSHOT", snapshot=snapshot)

    def subagent(self, run_id: str, name: str, description: str) -> None:
        self.emit("SUBAGENT_STARTED", subagentRunId=run_id, name=name, description=description)

    def subagent_done(self, run_id: str, result: str) -> None:
        self.emit("SUBAGENT_FINISHED", subagentRunId=run_id, result=result)

    def run_finished(self, thread_id: str, run_id: str, result: Any) -> None:
        self.emit("RUN_FINISHED", threadId=thread_id, runId=run_id, result=result)

    # -- 校验 -------------------------------------------------------------
    def validate(self) -> List[str]:
        """事件流一致性：START/END 配对、RUN 有终态。"""
        problems: List[str] = []
        pairs = [
            ("TOOL_CALL_START", "TOOL_CALL_END"),
            ("TEXT_MESSAGE_START", "TEXT_MESSAGE_END"),
            ("SUBAGENT_STARTED", "SUBAGENT_FINISHED"),
        ]
        for start, end in pairs:
            s = sum(1 for e in self.events if e["type"] == start)
            t = sum(1 for e in self.events if e["type"] == end)
            if s != t:
                problems.append(f"{start} 出现 {s} 次，{end} 出现 {t} 次，未配对")
        if not any(e["type"] == "RUN_STARTED" for e in self.events):
            problems.append("缺少 RUN_STARTED")
        if not any(e["type"] in ("RUN_FINISHED", "RUN_ERROR") for e in self.events):
            problems.append("缺少 RUN_FINISHED / RUN_ERROR")
        return problems


# ---------------------------------------------------------------------------
# 编排：把五层串起来
# ---------------------------------------------------------------------------


@dataclass
class Trace:
    """记录每一步属于哪一层，方便最后画分层归因表。"""
    rows: List[Tuple[str, str, str]] = field(default_factory=list)

    def add(self, layer: str, action: str, detail: str) -> None:
        self.rows.append((layer, action, detail))


class Orchestrator:
    def __init__(self, verbose: bool = True) -> None:
        self.skills = SkillRegistry()
        self.mcp = McpServer()
        self.a2a = A2AAgent()
        self.ui = EventStream()
        self.trace = Trace()
        self.verbose = verbose
        self.thread_id = "thread-1"
        self.run_id = "run-1"
        self.call_seq = 0
        self.state: Dict[str, Any] = {"stage": "init"}
        self.month = ""
        self.payslip = ""
        self.mrtr_used = False

    def log(self, layer: str, action: str, detail: str, payload: Any = None) -> None:
        self.trace.add(layer, action, detail)
        if self.verbose:
            print(f"\n[{layer}] {action}")
            print(f"    {detail}")
            if payload is not None:
                text = json.dumps(payload, ensure_ascii=False)
                print(f"    报文：{text[:220]}")

    # -- 动作实现 ---------------------------------------------------------
    def do_skill_index(self) -> str:
        index = self.skills.index()
        self.log(LAYER_SKILL, "skill_index",
                 f"只加载 name+description（{len(index)} 个 skill，正文一律不读）", index)
        return "\n".join(index)

    def do_read_skill(self, name: str) -> str:
        body = self.skills.load(name)
        self.log(LAYER_SKILL, "read_skill",
                 f"命中 {name}，现在才读 SKILL.md 正文（第二级披露）", body)
        return body

    def do_mcp_call(self, tool: str, args: Dict[str, Any],
                    input_responses: Optional[Dict[str, Any]] = None) -> str:
        self.call_seq += 1
        call_id = f"call-{self.call_seq}"
        params: Dict[str, Any] = {"name": tool, "arguments": args}
        if input_responses:
            # MRTR：重试原请求，用 inputResponses 补齐缺参
            params["inputResponses"] = input_responses
        resolved = {**args, **(input_responses or {})}
        envelope = self.mcp.handle("tools/call", params)
        result = envelope["result"]
        if result.get("resultType") == "input_required":
            self.mrtr_used = True
            need = ", ".join(r["name"] for r in result.get("inputRequests", []))
            self.log(LAYER_MCP, "tools/call",
                     f"参数不足：返回 resultType=input_required，需要 {need}；"
                     f"客户端将重试原请求并带上 inputResponses", envelope)
            self.ui.tool_call(call_id, tool, args, f"input_required: {need}")
            self.ui.state({"stage": "needs_input", "required": need})
            return json.dumps(result, ensure_ascii=False)
        text = str(result.get("content", ""))
        self.log(LAYER_MCP, "tools/call",
                 "参数补齐，结果 complete" + ("，但需要委派财务 Agent" if result.get("needs_delegation") else ""),
                 envelope)
        self.ui.tool_call(call_id, tool, resolved, text)
        self.month = resolved.get("month", "")
        self.state["month"] = self.month
        return json.dumps(result, ensure_ascii=False)

    def do_a2a_discover(self) -> str:
        card = self.a2a.agent_card()
        self.log(LAYER_A2A, "agent_card",
                 f"读对方卡片：{card['name']}，能力 {len(card['skills'])} 项，"
                 f"协议 {card['protocolVersion']}，签名 {self.a2a.signed}", card)
        return json.dumps(card, ensure_ascii=False)

    def do_a2a_send(self, message: str) -> str:
        run_id = "subagent-1"
        self.ui.subagent(run_id, "finance-agent", "受托查询工资条")
        task = self.a2a.send_task(message)
        artifact_text = task["artifacts"][0]["parts"][0]["text"]
        self.payslip = artifact_text
        self.ui.tool_call(f"call-a2a-{task['id']}", "tasks/send", {"message": message},
                          artifact_text, subagent_run_id=run_id)
        self.ui.subagent_done(run_id, artifact_text)
        self.log(LAYER_A2A, "tasks/send",
                 "→ ".join(h["status"] for h in task["history"]) + f"，artifact：{artifact_text}",
                 {"id": task["id"], "status": task["status"]})
        return json.dumps(task, ensure_ascii=False)

    def do_done(self, summary: str) -> str:
        answer = summary
        self.ui.text("msg-1", answer)
        self.ui.state({"stage": "done", "month": self.month, "payslip": self.payslip})
        self.ui.run_finished(self.thread_id, self.run_id, {"summary": summary})
        self.log(LAYER_AGUI, "run_finished", "前端收到完整事件流，可渲染文本、工具卡片与状态")
        return summary

    # -- 主循环 -----------------------------------------------------------
    def run(self, model: Any, max_steps: int = 16) -> Dict[str, Any]:
        self.ui.run_started(self.thread_id, self.run_id, USER_QUESTION)
        self.log(LAYER_MODEL, "model_call", "模型每轮只输出一个 JSON 动作")
        history: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"用户问题：{USER_QUESTION}"},
        ]
        for step in range(1, max_steps + 1):
            action = model.next(history)
            kind = str(action.get("action", ""))
            if kind == "done":
                obs = self.do_done(str(action.get("summary", "")))
                history.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
                history.append({"role": "user", "content": obs})
                break
            if kind == "skill_index":
                obs = self.do_skill_index()
            elif kind == "read_skill":
                obs = self.do_read_skill(str(action.get("name", "")))
            elif kind == "mcp_call":
                obs = self.do_mcp_call(str(action.get("tool", "")),
                                        dict(action.get("args") or {}),
                                        action.get("inputResponses"))
            elif kind == "a2a_discover":
                obs = self.do_a2a_discover()
            elif kind == "a2a_send":
                obs = self.do_a2a_send(str(action.get("message", "")))
            else:
                obs = f"错误：未知动作 {kind}"
            history.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
            history.append({"role": "user", "content": obs})
        return self.verify()

    # -- 校验 -------------------------------------------------------------
    def verify(self) -> Dict[str, Any]:
        problems = self.ui.validate()
        amount = ""
        match = re.search(r"实发\s*([0-9]+\.[0-9]+)", self.payslip)
        if match:
            amount = match.group(1)
        else:
            problems.append("没有从 A2A artifact 中解析出实发金额")
        if not self.mrtr_used:
            problems.append("没有走过一次 input_required -> 重试的 MRTR 回路")
        if self.month != "2026-10":
            problems.append(f"月份不正确：{self.month!r}")
        return {
            "problems": problems,
            "amount": amount,
            "events": len(self.ui.events),
            "skills_loaded": list(self.skills.loaded),
        }


# ---------------------------------------------------------------------------
# 模型：mock 与 live
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一个编排 Agent。每轮只输出一个 JSON 动作，不要输出解释文字。

可用动作：
{"action": "skill_index"}                       列出可用 skill 的 name+description
{"action": "read_skill", "name": "payroll-query"}  读取某个 skill 的 SKILL.md 正文
{"action": "mcp_call", "tool": "payroll.query", "args": {}, "inputResponses": {"month": "2026-10"}}
{"action": "a2a_discover"}                      读取对方 Agent Card
{"action": "a2a_send", "message": "查询 2026-10 工资条"}
{"action": "done", "summary": "给用户的最终回答"}

规则：
1. 先 skill_index 再 read_skill，按描述判断该用哪个 skill。
2. MCP 工具缺参数时，看清 inputRequests，重试原请求时用 inputResponses 补齐。
3. 工具说需要委派，就先 a2a_discover 再 a2a_send。
4. 最终金额必须来自 A2A 返回的 artifact，不要自己编。
"""

MOCK_ACTIONS: List[Dict[str, Any]] = [
    {"action": "skill_index"},
    {"action": "read_skill", "name": "payroll-query"},
    {"action": "mcp_call", "tool": "payroll.query", "args": {}},
    {"action": "mcp_call", "tool": "payroll.query", "args": {},
     "inputResponses": {"month": "2026-10"}},
    {"action": "a2a_discover"},
    {"action": "a2a_send", "message": "查询 2026-10 工资条明细"},
    {"action": "done", "summary": "2026-10 工资条：应发 18642.50，实发 18236.80（数据来自财务 Agent 的 artifact）。"},
]


class MockModel:
    """离线模型：按剧本驱动五层调用。"""

    def __init__(self) -> None:
        self.i = 0

    def next(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        if self.i >= len(MOCK_ACTIONS):
            return {"action": "done", "summary": "剧本结束"}
        action = MOCK_ACTIONS[self.i]
        self.i += 1
        return action


class LiveModel:
    """--live：调用真实 API（OpenAI 兼容的 chat/completions）。"""

    def __init__(self, model: str, base_url: str, api_key: str) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def next(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        payload = {"model": self.model, "messages": messages, "temperature": 0.0}
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"模型接口返回 {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"连不上模型接口 {self.base_url}: {exc.reason}") from exc
        content = data["choices"][0]["message"].get("content") or ""
        return parse_action(content)


def parse_action(content: str) -> Dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"模型输出里找不到 JSON 动作：{content[:200]!r}")
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict) or "action" not in data:
        raise ValueError(f"模型输出缺少 action 字段：{content[:200]!r}")
    return data


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def render_timeline(events: List[Dict[str, Any]]) -> str:
    lines = ["", "AG-UI 事件流（前端看到的就是这些）："]
    for event in events:
        detail = ""
        if "delta" in event:
            detail = f" delta={event['delta'][:40]!r}"
        elif "content" in event:
            detail = f" content={str(event['content'])[:60]!r}"
        elif "toolCallName" in event:
            detail = f" tool={event['toolCallName']}"
        elif "snapshot" in event:
            detail = f" snapshot={json.dumps(event['snapshot'], ensure_ascii=False)[:70]}"
        elif "subagentRunId" in event:
            detail = f" subagent={event['subagentRunId']}"
        lines.append(f"  {event['timestamp']:>2}  {event['type']}{detail}")
    return "\n".join(lines)


def render_layers(trace: Trace) -> str:
    lines = ["", "分层归因（这一步属于哪一层）："]
    for layer in LAYER_ORDER:
        actions = [f"{a}（{d}）" for lay, a, d in trace.rows if lay == layer]
        if actions:
            lines.append(f"  {layer}: " + " → ".join(actions))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ch39 协议与生态全景 —— 最小可实现")
    parser.add_argument("--max-steps", type=int, default=16, help="最大步数")
    parser.add_argument("--live", action="store_true", help="用真实 API（需 OPENAI_API_KEY）")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    args = parser.parse_args(argv)

    if args.live and not os.environ.get("OPENAI_API_KEY"):
        print("[!] --live 需要环境变量 OPENAI_API_KEY；不设置时可用 mock 模式跑通全回路。")
        return 2

    orch = Orchestrator()
    model: Any = LiveModel(args.model, args.base_url, os.environ.get("OPENAI_API_KEY", "")) \
        if args.live else MockModel()

    print(f"用户问题：{USER_QUESTION}")
    print("=" * 64)
    started = time.time()
    result = orch.run(model, max_steps=args.max_steps)
    elapsed = time.time() - started

    print("=" * 64)
    print(render_timeline(orch.ui.events))
    print(render_layers(orch.trace))
    print("-" * 64)
    print(f"事件总数：{result['events']}｜实际加载的 skill：{result['skills_loaded']}｜"
          f"用时 {elapsed:.1f}s｜模式：{'live' if args.live else 'mock'}")
    print("校验：")
    print(f"  事件流一致性：{'通过' if not result['problems'] else '不通过'}")
    print(f"  MRTR 回路（input_required -> 重试）：{'已发生' if orch.mrtr_used else '未发生'}")
    print(f"  事实一致性（金额取自 A2A artifact）：{result['amount'] or '未取到'}")
    if result["problems"]:
        for problem in result["problems"]:
            print(f"  [!!] {problem}")
        return 1
    print("[OK] 五层栈跑通，事件流与事实校验均通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
