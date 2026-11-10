#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch16 三条轻量路线最小可实现 —— 纯标准库，无需 API Key 就能跑通完整回路。

这个文件把三个框架的核心机制各写一遍，放在同一个文件里对照：

  路线一 PydanticAI 风格：依赖注入 + 输出类型契约 + 校验失败让模型重试
                          + 测试替身（不需要 API Key 就能跑测试）
  路线二 Agno 风格：      极简 Agent / Team / Workflow + 知识层
  路线三 smolagents 风格：CodeAgent（模型写代码）与 ToolCallingAgent（模型发 JSON）
                          两条路径的差别

三个场景都跑同一个任务：统计订单状态分布，找出金额最大的一单。

用法：
    python main.py                    # 跑全部场景
    python main.py --demo pydanticai  # 只跑类型驱动路线
    python main.py --demo agno        # 只跑极简路线
    python main.py --demo smolagents  # 只跑代码优先路线
    python main.py --demo compare     # 三条路线并排对比
    python main.py --trace            # 打印每一步的提示词与工具调用
    python main.py --live             # 切真实模型 API

--live 所需环境变量：
    LLM_API_KEY    必填，模型服务商的 API Key
    LLM_BASE_URL   可选，默认 https://api.openai.com/v1
    LLM_MODEL      可选，默认 gpt-4o-mini
端点需兼容 `POST {base}/chat/completions`：body 含 model/messages，
返回 choices[0].message.content。换供应商只改 base_url 与 model。

安全提示：代码智能体那一路会执行模型生成的 Python。本文件用受限的 builtins
白名单跑它，只够做算术和调用已注册的工具。真实项目请放进容器沙箱。
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import sys
import types
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
# 公共：一份订单数据和三个工具
# ==========================================================================


ORDERS: List[dict] = [
    {"id": "ord-42", "status": "shipped", "amount": 320.0, "customer": "acme"},
    {"id": "ord-43", "status": "pending", "amount": 89.5, "customer": "globex"},
    {"id": "ord-44", "status": "shipped", "amount": 1240.0, "customer": "acme"},
    {"id": "ord-45", "status": "cancelled", "amount": 15.0, "customer": "initech"},
    {"id": "ord-46", "status": "pending", "amount": 460.0, "customer": "globex"},
    {"id": "ord-47", "status": "shipped", "amount": 75.25, "customer": "initech"},
]


def fetch_orders(status: str = "") -> List[dict]:
    """按状态查订单；status 为空表示全部。"""
    if status:
        return [o for o in ORDERS if o["status"] == status]
    return list(ORDERS)


def fetch_customer(name: str) -> dict:
    """按客户名查客户档案。"""
    rows = [o for o in ORDERS if o["customer"] == name]
    return {"name": name, "orders": len(rows), "amount": sum(o["amount"] for o in rows)}


def est_tokens(text: str) -> int:
    """粗略 token 估算：中文按字，英文按 4 字符 1 token。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 4


# ==========================================================================
# 路线一：PydanticAI 风格 —— 依赖注入 + 输出类型契约
# ==========================================================================


@dataclass
class Deps:
    """依赖：不属于提示词、但工具需要的东西。

    数据库连接、HTTP 客户端、当前用户、配置——按次运行传进来，
    所以模块级只建一个 Agent，就能服务所有请求。
    """

    tenant: str
    orders: List[dict] = field(default_factory=list)


class ModelRetry(Exception):
    """工具或校验器判定这次不行，让模型带着理由重来。"""


@dataclass
class RunContext:
    """一次运行的上下文：依赖、已用 token、重试次数、消息历史。"""

    deps: Any
    usage: Dict[str, int] = field(default_factory=lambda: {"requests": 0, "tokens": 0})
    retry: int = 0
    messages: List[dict] = field(default_factory=list)
    model: Any = None


def schema_from_func(fn: Callable) -> dict:
    """从函数签名和 docstring 生成工具 schema。

    这就是"类型提示即契约"：模型看到的参数描述是从类型注解和 docstring
    里长出来的，不是另外手写一份。
    """
    import inspect

    sig = inspect.signature(fn)
    props: Dict[str, Any] = {}
    required: List[str] = []
    for name, param in sig.parameters.items():
        if name in ("self", "ctx", "context"):
            continue
        ptype = {str: "string", int: "integer", float: "number", bool: "boolean"}.get(
            param.annotation, "string"
        )
        props[name] = {"type": ptype, "description": name}
        if param.default is inspect.Parameter.empty:
            required.append(name)
    doc = (fn.__doc__ or "").strip().splitlines()
    return {
        "name": fn.__name__,
        "description": doc[0] if doc else fn.__name__,
        "parameters": {"type": "object", "properties": props, "required": required},
    }


def call_tool(fn: Callable, ctx: Optional["RunContext"], args: Dict[str, Any]) -> Any:
    """调用工具。第一个参数叫 ctx/context 的，自动把上下文注入进去。"""
    import inspect

    sig = inspect.signature(fn)
    if "ctx" in sig.parameters or "context" in sig.parameters:
        return fn(ctx, **args)
    return fn(**args)


@dataclass
class ModelRequest:
    messages: List[dict]
    tools: List[dict]
    output_hint: Any = None


@dataclass
class ModelResponse:
    content: str = ""
    tool_calls: List[dict] = field(default_factory=list)
    output: Any = None


class Model:
    """模型抽象：任何实现了 request 的东西都能当模型用。"""

    name = "base"

    def request(self, req: ModelRequest) -> ModelResponse:
        raise NotImplementedError


class ScriptedModel(Model):
    """按脚本行事的模型替身：先调工具，再给一个（可能不合格的）结果。

    这是"测试替身"的核心：把模型的每一步都钉死，测试就不用真调 API。
    """

    name = "scripted"

    def __init__(self, script: List[ModelResponse]) -> None:
        self.script = list(script)
        self.calls = 0
        self.seen: List[ModelRequest] = []

    def request(self, req: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.seen.append(req)
        if not self.script:
            return ModelResponse(content="（脚本用尽）")
        return self.script.pop(0)


@dataclass
class TypedAgent:
    """类型驱动的 Agent。

    deps_type  依赖的静态类型，RunContext[deps_type] 因此是有类型的
    output_type  产出的契约：不是 str 就是一个数据类，产出必须符合
    retries     校验失败或工具报错的重试预算
    """

    model: Model
    deps_type: Any = None
    output_type: Any = None
    instructions: str = ""
    retries: int = 1
    name: str = "typed-agent"
    _tools: Dict[str, Callable] = field(default_factory=dict)
    _tool_schemas: List[dict] = field(default_factory=list)
    _validators: List[Callable] = field(default_factory=list)

    def tool(self, fn: Callable) -> Callable:
        """注册工具。名字是函数名，描述是 docstring，参数来自类型注解。"""
        self._tools[fn.__name__] = fn
        self._tool_schemas.append(schema_from_func(fn))
        return fn

    def tool_plain(self, fn: Callable) -> Callable:
        return self.tool(fn)

    def output_validator(self, fn: Callable) -> Callable:
        self._validators.append(fn)
        return fn

    # -- 输出契约的校验：字段在不在、类型对不对 --

    def _validate_output(self, output: Any) -> Any:
        """按契约检查产出：先构造成目标类型，再查字段类型，最后跑语义校验。"""
        import dataclasses

        if self.output_type is None or output is None:
            return output
        if not dataclasses.is_dataclass(self.output_type):
            return output

        # 1) 构造成目标类型——这一步会暴露缺字段、多字段
        if isinstance(output, dict):
            fields = {f.name for f in dataclasses.fields(self.output_type)}
            missing = fields - set(output)
            if missing:
                raise ModelRetry("缺少字段：%s" % "、".join(sorted(missing)))
            extra = set(output) - fields
            if extra:
                raise ModelRetry("多出字段：%s" % "、".join(sorted(extra)))
            output = self.output_type(**output)
        elif not isinstance(output, self.output_type):
            raise ModelRetry(
                "输出类型不对：期望 %s，实际 %s" % (self.output_type.__name__, type(output).__name__)
            )

        # 2) 查每个字段的类型
        # 注意：开了 from __future__ import annotations 之后，字段上的类型
        # 是字符串，必须用 get_type_hints 还原成真正的类型，否则检查形同虚设。
        import typing

        hints = typing.get_type_hints(self.output_type)
        for name, want in hints.items():
            got = getattr(output, name, None)
            if want in (int, float, str, dict, list) and got is not None:
                if not isinstance(got, want):
                    raise ModelRetry(
                        "字段 %s 类型不对：期望 %s，实际 %s"
                        % (name, want.__name__, type(got).__name__)
                    )

        # 3) 跑语义校验器——类型系统查不了的部分
        for v in self._validators:
            v(output)
        return output

    def run_sync(self, prompt: str, deps: Any = None) -> "TypedResult":
        ctx = RunContext(deps=deps, model=self.model)
        messages: List[dict] = [{"role": "system", "content": self.instructions}]
        messages.append({"role": "user", "content": prompt})
        attempt = 0
        while True:
            attempt += 1
            if attempt > self.retries + 1:
                raise ModelRetry("超过重试预算 %d" % self.retries)
            req = ModelRequest(
                messages=list(messages),
                tools=list(self._tool_schemas),
                output_hint=self.output_type,
            )
            ctx.usage["requests"] += 1
            resp = self.model.request(req)
            ctx.usage["tokens"] += est_tokens(resp.content)

            if resp.tool_calls:
                messages.append({"role": "assistant", "content": resp.content})
                for call in resp.tool_calls:
                    fn = self._tools.get(call["name"])
                    if fn is None:
                        raise ModelRetry("未知工具 %s" % call["name"])
                    try:
                        result = call_tool(fn, ctx, call.get("args") or {})
                    except ModelRetry as exc:
                        messages.append(
                            {"role": "user", "content": "工具报错：%s，请改正后重试" % exc}
                        )
                        break
                    messages.append(
                        {"role": "tool", "name": call["name"], "content": json.dumps(result, ensure_ascii=False)}
                    )
                else:
                    continue
                continue

            # 有产出：按契约校验，不合格就带理由让模型重来
            try:
                output = self._validate_output(resp.output if resp.output is not None else resp.content)
            except ModelRetry as exc:
                messages.append(
                    {"role": "user", "content": "输出不合格：%s，请按契约重做" % exc}
                )
                continue
            return TypedResult(output=output, messages=messages, usage=ctx.usage, attempts=attempt)


@dataclass
class TypedResult:
    output: Any
    messages: List[dict]
    usage: Dict[str, int]
    attempts: int = 1


# ==========================================================================
# 路线二：Agno 风格 —— 极简 API + 知识层
# ==========================================================================


@dataclass
class RunResponse:
    """一次运行的产出。内容可以是字符串，也可以是 dict。"""

    content: Any
    agent: str
    tools_called: List[str] = field(default_factory=list)
    steps: int = 1


def _terms(text: str) -> List[str]:
    """分词：英文按词，中文按二元组。

    中文单字噪声太大（"的""是"到处都是），二元组在短文本检索里够用。
    """
    text = text.lower()
    out = [t for t in re.findall(r"[a-z0-9]+", text) if len(t) > 1]
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    out.extend(cjk[i : i + 2] for i in range(len(cjk) - 1))
    return out


class Knowledge:
    """知识层：把文档切块、建索引、检索。

    极简版：关键词打分。真实实现会换成向量库，接口不变。
    """

    def __init__(self, docs: List[dict], name: str = "kb") -> None:
        self.name = name
        self.chunks: List[dict] = []
        for d in docs:
            for i, part in enumerate(d["text"].split("。")):
                part = part.strip()
                if part:
                    self.chunks.append({"id": "%s#%d" % (d["id"], i), "text": part, "title": d["title"]})

    def search(self, query: str, top_k: int = 2) -> List[dict]:
        terms = set(_terms(query))
        scored = []
        for c in self.chunks:
            # 标题也算可检索内容——"订单状态说明"这类标题往往比正文更点题
            ct = set(_terms(c["title"] + " " + c["text"]))
            score = len(terms & ct)
            if score:
                scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:top_k]]


class MiniAgent:
    """极简 Agent：名字 + 模型 + 指令 + 工具 + 知识 + 会话库。"""

    def __init__(
        self,
        name: str,
        model: Model,
        instructions: str = "",
        tools: Optional[List[Callable]] = None,
        knowledge: Optional[Knowledge] = None,
        db: Optional[Dict[str, List[dict]]] = None,
    ) -> None:
        self.name = name
        self.model = model
        self.instructions = instructions
        self.tools = {t.__name__: t for t in (tools or [])}
        self.knowledge = knowledge
        self.db = db if db is not None else {}
        self.calls = 0

    def run(self, query: str, session_id: str = "default") -> RunResponse:
        self.calls += 1
        history = self.db.setdefault(session_id, [])
        # 极简策略：命中知识库就先检索，把结果拼进上下文
        context = ""
        if self.knowledge is not None:
            hits = self.knowledge.search(query)
            if hits:
                context = "\n".join("【%s】%s" % (h["title"], h["text"]) for h in hits)
        prompt = query if not context else "%s\n\n参考资料：\n%s" % (query, context)
        req = ModelRequest(
            messages=[
                {"role": "system", "content": self.instructions},
                {"role": "user", "content": prompt},
            ],
            tools=[schema_from_func(t) for t in self.tools.values()],
        )
        resp = self.model.request(req)
        tools_called: List[str] = []
        if resp.tool_calls:
            for call in resp.tool_calls:
                fn = self.tools.get(call["name"])
                if fn is not None:
                    resp.content = str(fn(**(call.get("args") or {})))
                    tools_called.append(call["name"])
        history.append({"role": "user", "content": query})
        history.append({"role": "assistant", "content": str(resp.content)})
        return RunResponse(content=resp.content, agent=self.name, tools_called=tools_called)


class MiniTeam:
    """极简团队：按成员名单路由，把活交给最合适的那个。"""

    def __init__(self, members: List[MiniAgent], mode: str = "route") -> None:
        self.members = members
        self.mode = mode
        self.calls = 0

    def run(self, query: str) -> RunResponse:
        self.calls += 1
        # 极简路由：谁的知识库命中得多就找谁
        best, best_score = self.members[0], -1
        for m in self.members:
            score = 0
            if m.knowledge is not None:
                score = len(m.knowledge.search(query))
            if score > best_score:
                best, best_score = m, score
        return best.run(query)


class MiniWorkflow:
    """极简工作流：步骤按顺序跑，状态在步骤之间传。"""

    def __init__(self, steps: List[Callable[[dict], dict]]) -> None:
        self.steps = steps

    def run(self, state: Optional[dict] = None) -> dict:
        st = dict(state or {})
        for step in self.steps:
            st.update(step(st) or {})
        return st


# ==========================================================================
# 路线三：smolagents 风格 —— 代码优先 vs 工具调用
# ==========================================================================


SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "divmod": divmod, "enumerate": enumerate, "filter": filter, "float": float,
    "format": format, "int": int, "isinstance": isinstance, "len": len,
    "list": list, "map": map, "max": max, "min": min, "print": print,
    "range": range, "reversed": reversed, "round": round, "set": set,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "True": True, "False": False, "None": None,
}


class CodeExecutionError(Exception):
    """受限沙箱里执行失败。"""


def run_sandboxed(code: str, namespace: Dict[str, Any]) -> str:
    """在受限环境里执行一段 Python，返回标准输出。

    只给白名单 builtins，不给 import、不给 open——够做算术和调工具，
    不够做别的。真实项目请用容器沙箱。
    """
    buf = io.StringIO()
    env: Dict[str, Any] = {"__builtins__": SAFE_BUILTINS}
    env.update(namespace)
    env["print"] = lambda *a, **k: print(*a, file=buf, **k)
    try:
        ast.parse(code)  # 先解析，语法错直接报
    except SyntaxError as exc:
        raise CodeExecutionError("语法错误：%s" % exc)
    try:
        exec(compile(code, "<agent>", "exec"), env)  # noqa: S102 - 受限沙箱
    except Exception as exc:  # noqa: BLE001
        raise CodeExecutionError("%s: %s" % (type(exc).__name__, exc))
    return buf.getvalue()


@dataclass
class Action:
    """模型这一步想干什么。"""

    kind: str  # "code" | "tool_call" | "final"
    code: str = ""
    tool_name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    answer: str = ""


class CodeAgent:
    """代码智能体：模型写一段 Python，执行，把输出喂回去。

    关键差别：模型在**一次**回复里就能写循环、调多个工具、做计算。
    工具调用智能体则是一个工具一次调用。
    """

    def __init__(self, tools: List[Callable], model: Model, max_steps: int = 4, name: str = "code-agent") -> None:
        self.tools = {t.__name__: t for t in tools}
        self.model = model
        self.max_steps = max_steps
        self.name = name
        self.calls = 0
        self.steps: List[dict] = []

    def run(self, task: str) -> str:
        self.calls = 0
        messages: List[dict] = [{"role": "user", "content": task}]
        for step in range(self.max_steps):
            self.calls += 1
            req = ModelRequest(
                messages=list(messages),
                tools=[schema_from_func(t) for t in self.tools.values()],
                output_hint=None,
            )
            action = self._plan(req)
            self.steps.append({"step": step + 1, "kind": action.kind, "code": action.code})
            if action.kind == "final":
                return action.answer
            if action.kind == "code":
                namespace = dict(self.tools)
                try:
                    out = run_sandboxed(action.code, namespace)
                except CodeExecutionError as exc:
                    messages.append({"role": "user", "content": "代码执行失败：%s" % exc})
                    continue
                # smolagents 的约定：代码里 print("Final Answer: ...") 就是最终答案，
                # 不必再让模型复述一遍——这是代码路径省一次模型往返的关键。
                if "Final Answer:" in out:
                    return out.split("Final Answer:", 1)[1].strip()
                messages.append({"role": "user", "content": "执行输出：\n%s" % out})
                continue
            if action.kind == "tool_call":
                fn = self.tools.get(action.tool_name)
                if fn is None:
                    messages.append({"role": "user", "content": "未知工具 %s" % action.tool_name})
                    continue
                result = fn(**action.args)
                messages.append(
                    {"role": "user", "content": "工具返回：%s" % json.dumps(result, ensure_ascii=False)}
                )
        return "（达到最大步数，未给出最终答案）"

    def _plan(self, req: ModelRequest) -> Action:
        raise NotImplementedError


class ToolCallingAgent:
    """工具调用智能体：模型发 JSON，一次一个工具。"""

    def __init__(self, tools: List[Callable], model: Model, max_steps: int = 4, name: str = "tool-agent") -> None:
        self.tools = {t.__name__: t for t in tools}
        self.model = model
        self.max_steps = max_steps
        self.name = name
        self.calls = 0
        self.tool_calls = 0
        self.steps: List[dict] = []

    def run(self, task: str) -> str:
        self.calls = 0
        self.tool_calls = 0
        messages: List[dict] = [{"role": "user", "content": task}]
        for step in range(self.max_steps):
            self.calls += 1
            req = ModelRequest(messages=list(messages), tools=[schema_from_func(t) for t in self.tools.values()])
            action = self._plan(req)
            self.steps.append({"step": step + 1, "kind": action.kind})
            if action.kind == "final":
                return action.answer
            fn = self.tools.get(action.tool_name)
            if fn is None:
                messages.append({"role": "user", "content": "未知工具 %s" % action.tool_name})
                continue
            result = fn(**action.args)
            self.tool_calls += 1
            messages.append(
                {"role": "user", "content": "工具返回：%s" % json.dumps(result, ensure_ascii=False)}
            )
        return "（达到最大步数，未给出最终答案）"

    def _plan(self, req: ModelRequest) -> Action:
        raise NotImplementedError


# ==========================================================================
# mock 模型：让三条路线跑同一个任务
# ==========================================================================

TASK = "统计订单各状态的数量，并找出金额最大的一单，告诉我订单号和金额。"


def make_scripted_model() -> ScriptedModel:
    """类型驱动路线：查工具 -> 交一个**类型不合格**的产出 -> 再交一个
    **语义不合格**的产出 -> 最后交一个合格的。

    两次纠错分别对应两道检查：字段类型、字段之间的自洽。
    """
    bad_type = ModelResponse(
        content="已统计",
        output={
            "total": 6,
            "status_counts": {"shipped": 3, "pending": 2, "cancelled": 1},
            "top_order_id": "ord-44",
            "top_amount": "1240.0",  # 字符串，类型检查会打回
        },
    )
    bad_total = ModelResponse(
        content="已统计",
        output={
            "total": 5,  # 与各状态数量之和对不上，语义校验会打回
            "status_counts": {"shipped": 3, "pending": 2, "cancelled": 1},
            "top_order_id": "ord-44",
            "top_amount": 1240.0,
        },
    )
    good = ModelResponse(
        content="已统计",
        output={
            "total": 6,
            "status_counts": {"shipped": 3, "pending": 2, "cancelled": 1},
            "top_order_id": "ord-44",
            "top_amount": 1240.0,
        },
    )
    return ScriptedModel(
        [
            ModelResponse(content="先查订单", tool_calls=[{"name": "fetch_all_orders", "args": {}}]),
            bad_type,
            bad_total,
            good,
        ]
    )


def make_agno_model() -> "AgnoLikeModel":
    return AgnoLikeModel()


class AgnoLikeModel(Model):
    """极简路线用的模型：查一次工具，直接给一段文字。"""

    name = "agno-like"

    def __init__(self) -> None:
        self.calls = 0

    def request(self, req: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            content="共 6 单：shipped 3 单、pending 2 单、cancelled 1 单；金额最大的是 ord-44，1240 元。",
            tool_calls=[],
        )


def make_code_model() -> ScriptedModel:
    """代码路线：模型写一段 Python，一次把活干完。"""

    code = (
        "orders = fetch_orders()\n"
        "counts = {}\n"
        "for o in orders:\n"
        "    counts[o['status']] = counts.get(o['status'], 0) + 1\n"
        "top = max(orders, key=lambda o: o['amount'])\n"
        "print('共 %d 单，%s；金额最大 %s，%.2f' % (len(orders), counts, top['id'], top['amount']))\n"
    )
    return ScriptedModel([ModelResponse(content=code), ModelResponse(content="统计完成")])


class ScriptedCodeAgent(CodeAgent):
    """按脚本给出动作的 CodeAgent。"""

    def __init__(self, tools: List[Callable], script: List[Action], **kw: Any) -> None:
        super().__init__(tools, ScriptedModel([]), **kw)
        self.script = script

    def _plan(self, req: ModelRequest) -> Action:
        if not self.script:
            return Action(kind="final", answer="（脚本用尽）")
        return self.script.pop(0)


class ScriptedToolAgent(ToolCallingAgent):
    """按脚本给出工具调用动作的 ToolCallingAgent。"""

    def __init__(self, tools: List[Callable], script: List[Action], **kw: Any) -> None:
        super().__init__(tools, ScriptedModel([]), **kw)
        self.script = script

    def _plan(self, req: ModelRequest) -> Action:
        if not self.script:
            return Action(kind="final", answer="（脚本用尽）")
        return self.script.pop(0)


# ==========================================================================
# 演示
# ==========================================================================


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


@dataclass
class OrderDigest:
    """输出的类型契约：字段、类型，一条都不能少。"""

    total: int
    status_counts: dict
    top_order_id: str
    top_amount: float


def build_typed_agent(model: Model) -> TypedAgent:
    agent = TypedAgent(
        model=model,
        deps_type=Deps,
        output_type=OrderDigest,
        instructions="你是订单分析助手。用 fetch_orders 查数据，然后按契约给出统计结果。",
        retries=3,
        name="order-digest",
    )

    @agent.tool
    def fetch_all_orders(ctx: RunContext) -> List[dict]:
        """按当前租户查订单列表。"""
        return ctx.deps.orders

    @agent.output_validator
    def check_total(digest: OrderDigest) -> OrderDigest:
        """总数必须等于各状态数量之和——这是语义校验，类型管不了。"""
        if digest.total != sum(digest.status_counts.values()):
            raise ModelRetry("total 与 status_counts 之和对不上")
        return digest

    return agent


def run_pydanticai(live: bool, trace: bool, args: Optional[argparse.Namespace] = None) -> None:
    hr("路线一：PydanticAI 风格 —— 依赖注入 + 输出类型契约")
    if live:
        model = build_live_model(args)
    else:
        model = make_scripted_model()
    agent = build_typed_agent(model)
    deps = Deps(tenant="acme", orders=ORDERS)

    result = agent.run_sync(TASK, deps=deps)

    print("任务：%s" % TASK)
    print("依赖：tenant=%s，orders=%d 条（靠 deps 传进来，不进提示词）" % (deps.tenant, len(deps.orders)))
    print("模型调用：%d 次，请求数 %d" % (result.attempts, result.usage["requests"]))
    print("\n对话里模型收到的纠错反馈：")
    for m in result.messages:
        if m["role"] == "user" and "不合格" in str(m.get("content", "")):
            print("  -> %s" % m["content"])
    print("\n最终产出（已经被校验器确认符合契约）：")
    print("  %s" % result.output)
    print("  类型：%s" % type(result.output).__name__)
    if trace:
        print("\n完整消息序列：")
        for m in result.messages:
            print("  [%s] %s" % (m["role"], str(m.get("content"))[:110]))

    corrections = sum(
        1 for m in result.messages if m["role"] == "user" and "不合格" in str(m.get("content", ""))
    )
    print(
        "\n要点：\n"
        "  1) 依赖（租户、数据）走 deps 注入，不进提示词——模块级一个 Agent 服务所有请求；\n"
        "  2) output_type 是契约：字段名、类型、缺一不可，产出不合格会被打回重做；\n"
        "  3) 校验器能查语义（total 要等于各状态之和），类型系统查不了；\n"
        "  4) 这次 %d 次模型调用里，有 %d 次是因为产出不合格被打回重做。"
        % (result.attempts, corrections)
    )


def run_agno(live: bool, trace: bool, args: Optional[argparse.Namespace] = None) -> None:
    hr("路线二：Agno 风格 —— 极简 API + 知识层")
    kb = Knowledge(
        [
            {"id": "d1", "title": "订单状态说明", "text": "shipped 表示已发货，pending 表示待处理，cancelled 表示已取消。"},
            {"id": "d2", "title": "退款政策", "text": "已取消的订单在 7 个工作日内原路退回，超过 500 元需要人工复核。"},
        ]
    )
    model = build_live_model(args) if live else make_agno_model()
    agent = MiniAgent(
        name="订单助手",
        model=model,
        instructions="你是订单助手。",
        tools=[fetch_orders],
        knowledge=kb,
        db={},
    )
    team = MiniTeam(members=[agent], mode="route")

    resp = team.run(TASK)
    print("任务：%s" % TASK)
    print("构造 Agent 的代码量：一个名字、一个模型、一条指令、两个可选参数（tools / knowledge）。")
    print("知识层命中：%s" % [c["title"] for c in kb.search(TASK)])
    print("\n产出（内容是自由文本，没有类型契约）：")
    print("  %s" % resp.content)
    print("\n会话库里存了 %d 条消息（同一个 session_id 再次调用会带上历史）" % len(agent.db.get("default", [])))
    if trace:
        print("\n模型收到的提示：")
        for m in [{"role": "system", "content": agent.instructions}]:
            pass
        print("  [system] %s" % agent.instructions)
    print(
        "\n要点：\n"
        "  1) API 极简：Agent(name, model, instructions, tools, knowledge, db) 六个字段；\n"
        "  2) 知识层是一等公民：知识库命中就自动拼进上下文，不用自己写检索；\n"
        "  3) 团队按知识命中路由成员；\n"
        "  4) 代价：产出是自由文本，没有类型契约，下游要自己解析——这是它和 PydanticAI 最大的分野。"
    )


def run_smolagents(live: bool, trace: bool, args: Optional[argparse.Namespace] = None) -> None:
    hr("路线三：smolagents 风格 —— 代码优先 vs 工具调用")
    code_action = Action(
        kind="code",
        code=(
            "orders = fetch_orders()\n"
            "counts = {}\n"
            "for o in orders:\n"
            "    counts[o['status']] = counts.get(o['status'], 0) + 1\n"
            "top = max(orders, key=lambda o: o['amount'])\n"
            "print('Final Answer: 共 %d 单，%s；金额最大 %s，%.2f' % (len(orders), counts, top['id'], top['amount']))\n"
        ),
    )
    code_agent: CodeAgent
    tool_agent: ToolCallingAgent
    if live:
        live_model = build_live_model(args)
        code_agent = LiveCodeAgent(tools=[fetch_orders], model=live_model, max_steps=4)
        tool_agent = LiveToolAgent(tools=[fetch_orders], model=live_model, max_steps=4)
    else:
        code_agent = ScriptedCodeAgent(
            tools=[fetch_orders],
            script=[code_action],
            max_steps=4,
        )
        tool_agent = ScriptedToolAgent(
            tools=[fetch_orders],
            script=[
                Action(kind="tool_call", tool_name="fetch_orders", args={}),
                Action(kind="final", answer="共 6 单，金额最大 ord-44，1240.00"),
            ],
            max_steps=4,
        )
    out_code = code_agent.run(TASK)
    out_tool = tool_agent.run(TASK)

    print("任务：%s\n" % TASK)
    print("【CodeAgent】模型写了一段 Python：")
    for line in code_action.code.splitlines() if not live else ["（live 模式：由真实模型现场生成）"]:
        print("  " + line)
    if not live:
        print("  执行输出即最终答案：共 6 单，{'shipped': 3, 'pending': 2, 'cancelled': 1}；金额最大 ord-44，1240.00")
    print("  模型调用 %d 次，最终回答：%s" % (code_agent.calls, out_code))

    print("\n【ToolCallingAgent】模型发 JSON 工具调用：")
    print('  {"name": "fetch_orders", "args": {}}')
    print("  模型调用 %d 次，工具调用 %d 次，最终回答：%s" % (tool_agent.calls, tool_agent.tool_calls, out_tool))

    if trace:
        print("\n执行轨迹：")
        for s in code_agent.steps:
            print("  CodeAgent  step %d: %s" % (s["step"], s["kind"]))
        for s in tool_agent.steps:
            print("  ToolAgent  step %d: %s" % (s["step"], s["kind"]))

    print(
        "\n要点：\n"
        "  1) CodeAgent 在**一次**回复里就把循环、多个工具调用和计算全写完——模型调用更少；\n"
        "  2) ToolCallingAgent 一个工具一次调用，简单可预测，但轮次多；\n"
        "  3) 两条路的产出都是自由文本，没有类型契约；\n"
        "  4) 代码路径必须进沙箱——本文件用受限 builtins 白名单，真实项目要用容器。"
    )


def run_compare(live: bool, trace: bool) -> None:
    hr("三条路线并排对比")
    # 路线一
    model = make_scripted_model()
    agent = build_typed_agent(model)
    r1 = agent.run_sync(TASK, deps=Deps(tenant="acme", orders=ORDERS))

    # 路线二
    agno_agent = MiniAgent("订单助手", make_agno_model(), "你是订单助手。", [fetch_orders], None, {})
    r2 = agno_agent.run(TASK)

    # 路线三
    code_agent = ScriptedCodeAgent(
        tools=[fetch_orders],
        script=[
            Action(
                kind="code",
                code="orders = fetch_orders()\nprint('共 %d 单' % len(orders))\n",
            ),
            Action(kind="final", answer="共 6 单，金额最大 ord-44，1240.00"),
        ],
    )
    r3 = code_agent.run(TASK)

    rows = [
        ("模型调用次数", "%d 次" % r1.attempts, "1 次", "%d 次" % code_agent.calls),
        ("产出类型", "OrderDigest（数据类）", "str（自由文本）", "str（自由文本）"),
        ("产出有校验", "有：类型 + 语义，不合格重做", "无", "无"),
        ("依赖怎么传", "deps 注入，不进提示词", "全局/构造时绑定", "全局/构造时绑定"),
        ("能离线测吗", "能：换测试替身即可", "能：换模型即可", "能：换模型即可"),
        ("多步编排", "自己写循环", "自己写 Workflow", "自己写循环"),
    ]
    print("%-14s %-26s %-22s %-20s" % ("维度", "PydanticAI 型", "Agno 型", "smolagents 型"))
    print("-" * 86)
    for k, a, b, c in rows:
        print("%-14s %-26s %-22s %-20s" % (k, a, b, c))
    print("-" * 86)
    print(
        "注：类型驱动路线那 %d 次调用里，有 2 次是模型**故意**交错产出后被校验器打回重做——\n"
        "     这正是输出契约的账：多几次往返，换下游不用写解析和防御。" % r1.attempts
    )
    print(
        "\n三条路的差别不在功能，在**你愿意把多少东西写进类型里**：\n"
        "  PydanticAI 型把产出、依赖、校验都写成类型，多写几行，少写一堆解析代码；\n"
        "  Agno 型最短，上手最快，代价是产出没有契约；\n"
        "  smolagents 型让模型写代码，单轮能干的活最多，代价是必须配沙箱。"
    )


# ==========================================================================
# 真实模型（--live）
# ==========================================================================


class LiveCodeAgent(CodeAgent):
    """live 模式下的 CodeAgent：让真模型写代码。

    注意：这里执行的是**真实模型生成**的代码。生产环境必须放进容器沙箱，
    本文件的受限 builtins 只够演示，不足以当安全边界。
    """

    def _plan(self, req: ModelRequest) -> Action:
        prompt = (
            "请用 Python 写一段代码完成下面的任务。\n"
            "可用的工具函数：%s\n"
            "要求：最后用 print('Final Answer: ...') 输出最终答案。\n"
            "只输出一个 python 代码块。\n\n任务：%s"
            % (
                "、".join(self.tools),
                req.messages[-1].get("content", ""),
            )
        )
        text = self.model._chat([{"role": "user", "content": prompt}])
        m = re.search(r"```(?:python)?\s*\n(.*?)```", text, flags=re.S)
        code = m.group(1) if m else text
        return Action(kind="code", code=code)


class LiveToolAgent(ToolCallingAgent):
    """live 模式下的 ToolCallingAgent：让真模型决定调哪个工具。"""

    def _plan(self, req: ModelRequest) -> Action:
        resp = self.model.request(req)
        if resp.tool_calls:
            call = resp.tool_calls[0]
            return Action(
                kind="tool_call",
                tool_name=str(call.get("name", "")),
                args=dict(call.get("args") or {}),
            )
        return Action(kind="final", answer=resp.content)


class LiveModel(Model):
    """真实模型：走 OpenAI 兼容的 chat completions 端点。"""

    name = "live"

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

    def request(self, req: ModelRequest) -> ModelResponse:
        self.calls += 1
        msgs = list(req.messages)
        if req.tools:
            msgs.append(
                {
                    "role": "user",
                    "content": "可用工具的 JSON schema：%s" % json.dumps(req.tools, ensure_ascii=False),
                }
            )
        text = self._chat(msgs)
        # 让模型回一段 JSON：{"content": ..., "tool_calls": [...], "output": {...}}
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            try:
                data = json.loads(m.group(0))
                return ModelResponse(
                    content=str(data.get("content") or ""),
                    tool_calls=list(data.get("tool_calls") or []),
                    output=data.get("output"),
                )
            except json.JSONDecodeError:
                pass
        return ModelResponse(content=text)


def build_live_model(args) -> LiveModel:
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        print("错误：--live 需要环境变量 LLM_API_KEY", file=sys.stderr)
        raise SystemExit(2)
    base_url = args.base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model = args.model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    return LiveModel(api_key, base_url, model)


# ==========================================================================
# CLI
# ==========================================================================


def main() -> int:
    ap = argparse.ArgumentParser(description="ch16 三条轻量路线最小可实现")
    ap.add_argument("--demo", choices=["all", "pydanticai", "agno", "smolagents", "compare"], default="all")
    ap.add_argument("--trace", action="store_true", help="打印提示词与工具调用")
    ap.add_argument("--live", action="store_true", help="切真实模型 API")
    ap.add_argument("--model", default=None, help="覆盖 LLM_MODEL")
    ap.add_argument("--base-url", default=None, help="覆盖 LLM_BASE_URL")
    args = ap.parse_args()

    # --live 需要凭据，先检查一次，免得跑到一半才报错
    if args.live:
        build_live_model(args)

    if args.demo in ("all", "pydanticai"):
        run_pydanticai(args.live, args.trace, args)
    if args.demo in ("all", "agno"):
        run_agno(args.live, args.trace, args)
    if args.demo in ("all", "smolagents"):
        run_smolagents(args.live, args.trace, args)
    if args.demo in ("all", "compare"):
        run_compare(args.live, args.trace)

    hr("跑完了")
    print("这个实现里有五个可以抄走的结论：")
    print("  1) 类型提示就是契约：工具 schema 从签名和 docstring 长出来，不用手写；")
    print("  2) 依赖走注入，不进提示词——模块级一个 Agent 服务所有请求；")
    print("  3) 输出契约能查形状也能查语义，不合格就把理由喂回模型让它重做；")
    print("  4) 极简路线的代价是产出没有类型契约，下游要自己解析；")
    print("  5) 代码智能体单轮能干更多活，但必须配沙箱——本文件的白名单只够做算术。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
