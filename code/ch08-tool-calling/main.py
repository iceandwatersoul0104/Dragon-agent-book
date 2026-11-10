#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch08 工具调用契约最小可实现 —— 纯标准库，无需 API Key 就能跑通完整回路。

一次跑清四件事：
  1) 参数校验与错误回灌：校验失败 -> 按 raw / structured / suggestion 三种形式回灌给模型
  2) 并行与依赖：参数引用前序调用 id 的调用必须串行，其余并发执行
  3) 结果对齐：并发完成顺序被打乱，回灌时仍按模型给出的调用顺序
  4) 幂等与不可重放：写工具用幂等键去重重试；标注 replayable=false 的工具禁止自动重试

用法：
    python main.py                                  # 默认 suggestion 形式回灌
    python main.py --error-form raw
    python main.py --error-form structured
    python main.py --trace                          # 打印每批调用、回灌载荷与重试记录
    python main.py --live                           # 切真实模型

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
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# 1. 契约：工具定义就是接口
# --------------------------------------------------------------------------


@dataclass
class ToolSpec:
    name: str
    description: str
    params: Dict[str, Any]
    returns: str
    handler: Callable[..., Dict[str, Any]]
    side_effect: bool = False
    replayable: bool = True
    key_builder: Optional[Callable[[Dict[str, Any]], str]] = None
    delay: float = 0.0


class ToolFailure(Exception):
    """工具失败：code 稳定，message 面向模型，detail 给开发者。"""

    def __init__(self, message: str, code: str = "TOOL_ERROR",
                 retryable: bool = False, field_name: Optional[str] = None,
                 allowed: Optional[List[Any]] = None, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.field_name = field_name
        self.allowed = allowed
        self.detail = detail


# --------------------------------------------------------------------------
# 2. 运行时校验：类型、必填、枚举
# --------------------------------------------------------------------------


def validate(spec: ToolSpec, args: Dict[str, Any]) -> List[Dict[str, Any]]:
    errors: List[Dict[str, Any]] = []
    props = spec.params.get("properties", {})
    for name in spec.params.get("required", []):
        if name not in args:
            errors.append({"code": "MISSING_PARAM", "field": name,
                           "message": "缺少必填参数 %s" % name, "retryable": False})
    for name, rule in props.items():
        if name not in args:
            continue
        value = args[name]
        want = rule.get("type")
        if want == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            errors.append({"code": "TYPE_MISMATCH", "field": name,
                           "message": "%s 必须是数字" % name,
                           "allowed": ["number"], "retryable": False})
        elif want == "string" and not isinstance(value, str):
            errors.append({"code": "TYPE_MISMATCH", "field": name,
                           "message": "%s 必须是字符串" % name,
                           "allowed": ["string"], "retryable": False})
        if "enum" in rule and value not in rule["enum"]:
            errors.append({"code": "INVALID_ENUM", "field": name,
                           "message": "%s 取值非法：%r，应为 %s 之一"
                                      % (name, value, "、".join(map(str, rule["enum"]))),
                           "allowed": rule["enum"], "retryable": False})
    return errors


# --------------------------------------------------------------------------
# 3. 回灌载荷：raw / structured / suggestion
# --------------------------------------------------------------------------


def build_error_payload(form: str, errors: List[Dict[str, Any]],
                        detail: str = "") -> Dict[str, Any]:
    first = errors[0]
    if form == "raw":
        # 只给一句话，模型要自己猜怎么改
        return {"error": first["message"]}
    payload = {
        "error": {
            "code": first["code"],
            "field": first.get("field"),
            "message": first["message"],
            "retryable": first.get("retryable", False),
        }
    }
    if first.get("allowed"):
        payload["error"]["allowed"] = first["allowed"]
    if detail:
        payload["error"]["detail"] = detail[:200]      # 截断，只给开发者看
    if form == "suggestion":
        field_name = first.get("field") or "参数"
        allowed = first.get("allowed")
        if first["code"] == "INVALID_ENUM" and allowed:
            payload["suggestion"] = "把 %s 改成 %s（可选：%s）" % (
                field_name, allowed[0], "、".join(map(str, allowed)))
        elif first["code"] == "MISSING_PARAM":
            payload["suggestion"] = "补上 %s 参数" % field_name
        elif first["code"] == "TYPE_MISMATCH":
            payload["suggestion"] = "把 %s 改成 %s 类型" % (field_name, allowed[0] if allowed else "正确")
    return payload


# --------------------------------------------------------------------------
# 4. 模拟服务端：幂等、瞬时错误、不可重放
# --------------------------------------------------------------------------

SERVER_LOCK = threading.Lock()
SERVER_STATE: Dict[str, Any] = {
    "idempotency": {},      # key -> 已执行结果
    "requests": {},         # key -> 服务端收到的请求次数
    "executions": {},       # key -> 真正执行的次数（幂等后应恒为 1）
    "transfers": [],        # 扣款流水
}


def _key_for(spec: ToolSpec, args: Dict[str, Any]) -> str:
    if spec.key_builder:
        return spec.key_builder(args)
    return "%s:%s" % (spec.name, json.dumps(args, sort_keys=True, ensure_ascii=False))


def h_get_weather(args: Dict[str, Any]) -> Dict[str, Any]:
    city = args["city"]
    unit = args.get("unit", "celsius")
    temp_c = 18.0 if city == "上海" else 20.0
    value = temp_c if unit == "celsius" else temp_c * 9 / 5 + 32
    return {"city": city, "temp_c": temp_c, "value": value, "unit": unit}


def h_get_exchange_rate(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"base": args["base"], "quote": args["quote"], "rate": 7.1,
            "date": args.get("date", "2024-06-01")}


def h_compute_savings(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"value": round(float(args["temp_c"]) * float(args["rate"]), 2)}


def h_send_report(args: Dict[str, Any]) -> Dict[str, Any]:
    key = _key_for(TOOLS["send_report"], args)
    with SERVER_LOCK:
        SERVER_STATE["requests"][key] = SERVER_STATE["requests"].get(key, 0) + 1
        request_no = SERVER_STATE["requests"][key]
        if key in SERVER_STATE["idempotency"]:
            # 幂等命中：直接返回上次结果，不再执行一次
            return dict(SERVER_STATE["idempotency"][key], deduped=True)
        if request_no == 1:
            # 第一次尝试遇到瞬时错误（上游超时）——这是可重试的
            raise ToolFailure("上游通知服务超时", code="UPSTREAM_TIMEOUT",
                              retryable=True, detail="trace-id=abc123 node=notify-2")
        result = {"sent_to": args["to"], "bytes": len(args.get("content", "")),
                  "date": args.get("date", "")}
        SERVER_STATE["idempotency"][key] = result
        SERVER_STATE["executions"][key] = SERVER_STATE["executions"].get(key, 0) + 1
        return result


def h_transfer_funds(args: Dict[str, Any]) -> Dict[str, Any]:
    key = _key_for(TOOLS["transfer_funds"], args)
    with SERVER_LOCK:
        SERVER_STATE["requests"][key] = SERVER_STATE["requests"].get(key, 0) + 1
        # 扣款是副作用操作：一旦超时，状态未知。
        # 服务端同样拒绝无幂等键的重复请求——真实网关会返回"疑似重复"，
        # 而不是替你在不确定的状态上再扣一次。
        raise ToolFailure("扣款网关超时，转账状态未知，需人工核对",
                          code="GATEWAY_TIMEOUT", retryable=True,
                          detail="transfer may or may not have happened")


TOOLS: Dict[str, ToolSpec] = {
    "get_weather": ToolSpec(
        name="get_weather",
        description="查询指定城市的当前天气与温度。只读，不产生任何副作用。",
        params={"type": "object", "properties": {
            "city": {"type": "string", "description": "城市名，如 上海"},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"],
                     "description": "温度单位，缺省 celsius"}},
            "required": ["city"]},
        returns="{\"city\": string, \"temp_c\": number, \"value\": number, \"unit\": string}",
        handler=h_get_weather, delay=0.05),
    "get_exchange_rate": ToolSpec(
        name="get_exchange_rate",
        description="查询指定币种对在参考日期的兑换率。只读，不产生任何副作用。",
        params={"type": "object", "properties": {
            "base": {"type": "string", "description": "基准币种 ISO 4217 代码，如 USD"},
            "quote": {"type": "string", "description": "目标币种 ISO 4217 代码，如 CNY"},
            "date": {"type": "string", "description": "参考日期 YYYY-MM-DD"}},
            "required": ["base", "quote"]},
        returns="{\"base\": string, \"quote\": string, \"rate\": number, \"date\": string}",
        handler=h_get_exchange_rate, delay=0.01),
    "compute_savings": ToolSpec(
        name="compute_savings",
        description="按温度与汇率计算一个派生数值。纯函数，无副作用。",
        params={"type": "object", "properties": {
            "temp_c": {"type": "number", "description": "摄氏温度"},
            "rate": {"type": "number", "description": "汇率"}},
            "required": ["temp_c", "rate"]},
        returns="{\"value\": number}",
        handler=h_compute_savings, delay=0.0),
    "send_report": ToolSpec(
        name="send_report",
        description="把日报内容发送给指定收件人。会产生外部副作用（真的发出去）。",
        params={"type": "object", "properties": {
            "to": {"type": "string", "description": "收件人邮箱"},
            "content": {"type": "string", "description": "日报正文"},
            "date": {"type": "string", "description": "报告日期 YYYY-MM-DD"}},
            "required": ["to", "content"]},
        returns="{\"sent_to\": string, \"bytes\": number, \"date\": string}",
        handler=h_send_report, side_effect=True, replayable=True,
        key_builder=lambda a: "send_report:%s:%s" % (a.get("date", ""), a.get("to", ""))),
    "transfer_funds": ToolSpec(
        name="transfer_funds",
        description="从运营账户向指定账户划款。不可逆的副作用操作。",
        params={"type": "object", "properties": {
            "to_account": {"type": "string", "description": "目标账户"},
            "amount": {"type": "number", "description": "金额，单位元"}},
            "required": ["to_account", "amount"]},
        returns="{\"transferred\": number, \"to\": string}",
        handler=h_transfer_funds, side_effect=True, replayable=False,
        key_builder=lambda a: "transfer_funds:%s:%s" % (a.get("to_account", ""), a.get("amount", ""))),
}


# --------------------------------------------------------------------------
# 5. 依赖引用与调度
# --------------------------------------------------------------------------

REF_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\}\}")


def refs_of(args: Dict[str, Any]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for value in args.values():
        if isinstance(value, str):
            out.extend(REF_RE.findall(value))
    return out


def resolve_args(args: Dict[str, Any], results: Dict[str, Any]) -> Dict[str, Any]:
    """把 {{call_1.temp_c}} 换成前序调用的真实返回值。

    results 里存的是 CallOutcome：只有成功的调用才拿得到 payload。
    """
    out: Dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str):
            def sub(m: "re.Match[str]") -> str:
                oc = results.get(m.group(1))
                if oc is None or not oc.ok:
                    return ""
                payload = oc.payload
                if not isinstance(payload, dict):
                    return ""
                return str(payload.get(m.group(2), ""))
            out[k] = REF_RE.sub(sub, v)
        else:
            out[k] = v
    return out


def failed_deps(args: Dict[str, Any], results: Dict[str, Any]) -> List[str]:
    """哪些被引用的调用没成功。依赖失败要明确报出来，
    而不是让它变成下游一个莫名其妙的类型错误。"""
    bad: List[str] = []
    for cid, _ in refs_of(args):
        oc = results.get(cid)
        if oc is None or not oc.ok:
            bad.append(cid)
    return bad


def coerce_args(spec: ToolSpec, args: Dict[str, Any]) -> Dict[str, Any]:
    """引用解析后做一次类型归一。

    依赖值被替换进来时总是字符串，"18.0" 会被数字字段判成类型错误。
    """
    props = spec.params.get("properties", {})
    out = dict(args)
    for name, rule in props.items():
        if name not in out:
            continue
        value = out[name]
        if rule.get("type") == "number" and isinstance(value, str):
            try:
                out[name] = float(value) if ("." in value or "e" in value.lower()) else int(value)
            except ValueError:
                pass                      # 保持原值，交给校验器报错
        elif rule.get("type") == "string" and not isinstance(value, str):
            out[name] = str(value)
    return out


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class CallOutcome:
    call: ToolCall
    ok: bool
    payload: Dict[str, Any]
    attempts: int = 1
    idempotency_key: str = ""
    note: str = ""


def execute_call(call: ToolCall, results: Dict[str, Any], error_form: str,
                 trace: bool) -> CallOutcome:
    spec = TOOLS.get(call.name)
    if spec is None:
        return CallOutcome(call, False,
                           {"error": {"code": "UNKNOWN_TOOL",
                                      "message": "未知工具 %s" % call.name}})

    args = coerce_args(spec, resolve_args(call.arguments, results))

    broken = failed_deps(call.arguments, results)
    if broken:
        payload = build_error_payload(error_form, [{
            "code": "UNRESOLVED_DEPENDENCY", "field": None,
            "message": "依赖的调用未成功：%s" % ",".join(broken),
            "retryable": False}])
        if trace:
            print("  [dep] %s 依赖失败：%s" % (call.id, ",".join(broken)))
        return CallOutcome(call, False, payload, note="dependency_failed")

    errors = validate(spec, args)
    if errors:
        payload = build_error_payload(error_form, errors)
        if trace:
            print("  [validate] %s -> %s" % (call.id, json.dumps(payload, ensure_ascii=False)))
        return CallOutcome(call, False, payload, note="validation")

    key = _key_for(spec, args) if spec.side_effect else ""
    attempts = 0
    last_error: Optional[ToolFailure] = None
    max_attempts = 2 if (spec.side_effect and spec.replayable) else 1
    while attempts < max_attempts:
        attempts += 1
        try:
            if spec.delay:
                time.sleep(spec.delay)
            payload = spec.handler(args)
            if trace:
                print("  [call] %s %s attempts=%d key=%s"
                      % (call.id, call.name, attempts, key or "-"))
            return CallOutcome(call, True, payload, attempts=attempts, idempotency_key=key)
        except ToolFailure as exc:
            last_error = exc
            if not exc.retryable:
                break
            if spec.side_effect and not spec.replayable:
                # 不可重放：状态未知，重试可能造成二次副作用
                payload = build_error_payload(error_form, [{
                    "code": "NON_REPLAYABLE", "field": None,
                    "message": "%s 失败且不可自动重试：%s（重试可能造成重复副作用）"
                               % (call.name, exc.message),
                    "retryable": False}], exc.detail)
                if trace:
                    print("  [guard] %s %s 不可重放 -> 不自动重试" % (call.id, call.name))
                return CallOutcome(call, False, payload, attempts=attempts,
                                   idempotency_key=key, note="non_replayable")
            if attempts < max_attempts:
                if trace:
                    print("  [retry] %s %s 第 %d 次失败(%s)，用同一幂等键重试: %s"
                          % (call.id, call.name, attempts, exc.code, key))
                continue
    assert last_error is not None
    payload = build_error_payload(error_form, [{
        "code": last_error.code, "field": last_error.field_name,
        "message": last_error.message, "retryable": last_error.retryable}],
        last_error.detail)
    return CallOutcome(call, False, payload, attempts=attempts,
                       idempotency_key=key, note="failed")


def run_batches(calls: List[ToolCall], error_form: str, max_parallel: int = 4,
                trace: bool = False, batch_start: int = 0
                ) -> Tuple[Dict[str, CallOutcome], List[str]]:
    """按参数引用关系分批：每批内并发，批与批之间串行。"""
    results: Dict[str, CallOutcome] = {}
    pending = list(calls)
    log: List[str] = []
    batch_no = batch_start
    while pending:
        ready = [c for c in pending
                 if all(r[0] in results for r in refs_of(c.arguments))]
        blocked = [c for c in pending if c not in ready]
        if not ready:
            for c in blocked:
                results[c.id] = CallOutcome(c, False, {"error": {
                    "code": "UNRESOLVED_DEPENDENCY",
                    "message": "依赖的调用未成功：%s" % ",".join(r[0] for r in refs_of(c.arguments))}})
            break
        batch_no += 1
        log.append("batch %d: %s（%d 个%s）"
                   % (batch_no, ", ".join(c.id + ":" + c.name for c in ready),
                      len(ready), "，并行" if len(ready) > 1 else ""))
        with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
            outcomes = list(pool.map(
                lambda c: execute_call(c, results, error_form, trace), ready))
        # 结果按 id 归档；失败的调用也占位，保证回灌时不错位
        for oc in outcomes:
            results[oc.call.id] = oc
        pending = [c for c in pending if c.id not in results]
    return results, log


def build_tool_message(calls: List[ToolCall], results: Dict[str, CallOutcome]) -> str:
    """回灌给模型：严格按模型给出的调用顺序，失败项也占位。"""
    lines = []
    for c in calls:                       # 顺序 = 模型请求的顺序
        oc = results.get(c.id)
        if oc is None:
            lines.append(json.dumps({"id": c.id, "name": c.name, "error": "未执行"},
                                    ensure_ascii=False))
        else:
            payload = oc.payload if oc.ok else {"id": c.id, "name": c.name, **oc.payload}
            if oc.ok:
                payload = {"id": c.id, "name": c.name, "result": oc.payload}
            lines.append(json.dumps(payload, ensure_ascii=False))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 6. 模型接入
# --------------------------------------------------------------------------

TASK = ("查询上海今天的天气和 USD/CNY 汇率，算出组合数值，把日报发给 boss@example.com，"
        "最后从运营账户 ops-001 向 ops-002 划 500 元。")

SYSTEM_PROMPT = """你是一个会用工具的助手。可用工具：
- get_weather(city, unit?) 查询城市天气，只读
- get_exchange_rate(base, quote, date?) 查询汇率，只读
- compute_savings(temp_c, rate) 纯计算
- send_report(to, content, date?) 发送日报，有副作用
- transfer_funds(to_account, amount) 划款，不可逆

请一次给出全部工具调用，输出 JSON 数组，每项形如
{"id":"call_1","name":"工具名","arguments":{...}}。
如果某个调用的参数依赖别的调用结果，用 {{call_id.字段名}} 引用。"""

INITIAL_CALLS = [
    ToolCall("call_1", "get_weather", {"city": "上海", "unit": "kelvin"}),
    ToolCall("call_2", "get_exchange_rate", {"base": "USD", "quote": "CNY"}),
    ToolCall("call_3", "compute_savings",
             {"temp_c": "{{call_1.temp_c}}", "rate": "{{call_2.rate}}"}),
    ToolCall("call_4", "send_report",
             {"to": "boss@example.com", "content": "{{call_3.value}}", "date": "2024-06-01"}),
    ToolCall("call_5", "transfer_funds", {"to_account": "ops-002", "amount": 500}),
]


class MockLLM:
    """按提示特征返回预设调用序列，不做真实推理。

    脚本化叙事：
      第 1 轮 —— 只读工具 + 发日报，其中 get_weather 的 unit 写错（kelvin）
      第 2 轮 —— 从回灌里看到校验错误，修正参数，并把不可逆的划款放到最后
      第 3 轮 —— 看到不可重放错误，停止调用，交回人工
    """

    def __call__(self, prompt: str) -> str:
        # 三种回灌形式里 code 可能被丢掉，所以按错误文案判断更稳
        if "不可自动重试" in prompt:
            return json.dumps([], ensure_ascii=False)      # 停下来交给人
        calls = [ToolCall(c.id, c.name, dict(c.arguments))
                 for c in INITIAL_CALLS if c.id != "call_5"]
        if "kelvin" in prompt:
            # 收到了校验错误：修参数，并把不可逆操作放在最后
            for c in calls:
                if c.name == "get_weather":
                    c.arguments["unit"] = "celsius"
            calls.append(ToolCall("call_5", "transfer_funds",
                                  {"to_account": "ops-002", "amount": 500}))
        return json.dumps([{"id": c.id, "name": c.name, "arguments": c.arguments}
                           for c in calls], ensure_ascii=False)


class LiveLLM:
    """真实模型：OpenAI 兼容的 chat completions 协议。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 60, temperature: float = 0.0) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    def __call__(self, prompt: str) -> str:
        body = {"model": self.model, "temperature": self.temperature,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": prompt}]}
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.api_key}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError("HTTP %s from %s: %s" % (exc.code, self.url, detail)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("cannot reach %s: %s" % (self.url, exc.reason)) from exc
        return data["choices"][0]["message"]["content"] or ""


def parse_calls(text: str) -> List[ToolCall]:
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    calls = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or "name" not in item:
            continue
        calls.append(ToolCall(item.get("id", "call_%d" % (i + 1)), item["name"],
                               item.get("arguments", {})))
    return calls


# --------------------------------------------------------------------------
# 7. 主循环
# --------------------------------------------------------------------------


@dataclass
class RunResult:
    rounds: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    validation_errors: int = 0
    retries: int = 0
    batches: List[str] = field(default_factory=list)
    transcripts: List[str] = field(default_factory=list)
    guard_hits: List[str] = field(default_factory=list)
    final_message: str = ""
    status: str = "done"


def run(llm: Callable[[str], str], error_form: str, max_rounds: int = 4,
        max_parallel: int = 4, trace: bool = False) -> RunResult:
    res = RunResult()
    history: List[str] = []
    for _ in range(max_rounds):
        res.rounds += 1
        prompt = TASK if not history else TASK + "\n\n上一轮工具返回：\n" + "\n".join(history)
        raw = llm(prompt)
        res.model_calls += 1
        calls = parse_calls(raw)
        if not calls:
            res.final_message = "模型没有给出更多工具调用，停止。"
            break
        results, batch_log = run_batches(calls, error_form, max_parallel, trace,
                                        batch_start=len(res.batches))
        res.batches.extend(batch_log)
        for oc in results.values():
            res.tool_calls += 1
            if oc.note == "validation":
                res.validation_errors += 1
            if oc.attempts > 1:
                res.retries += 1
            if oc.note == "non_replayable":
                res.guard_hits.append("%s (%s) 未自动重试，状态未知" % (oc.call.id, oc.call.name))
        message = build_tool_message(calls, results)
        res.transcripts.append(message)
        history.append(message)
        if trace:
            print("\n[tool message -> model]\n" + message)
    if not res.final_message:
        res.final_message = "工具调用结束。"
    return res


# --------------------------------------------------------------------------
# 8. CLI
# --------------------------------------------------------------------------


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def build_llm(args: argparse.Namespace) -> Callable[[str], str]:
    if not args.live:
        return MockLLM()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "--live 需要环境变量 LLM_API_KEY；"
            "可选 LLM_BASE_URL（默认 https://api.openai.com/v1）与 LLM_MODEL。\n")
        raise SystemExit(2)
    base_url = args.base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model = args.model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    return LiveLLM(base_url, api_key, model, temperature=args.temperature)


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="工具调用契约最小可实现（mock 模式无需 API Key）")
    parser.add_argument("--error-form", default="suggestion",
                        choices=["raw", "structured", "suggestion"],
                        help="校验失败时的回灌形式")
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument("--max-parallel", type=int, default=4, help="并发上限")
    parser.add_argument("--live", action="store_true", help="切真实模型 API")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--model", default="", help="覆盖 LLM_MODEL")
    parser.add_argument("--base-url", default="", help="覆盖 LLM_BASE_URL")
    parser.add_argument("--trace", action="store_true", help="打印回灌载荷与重试记录")
    args = parser.parse_args(argv)

    llm = build_llm(args)
    t0 = time.perf_counter()
    res = run(llm, args.error_form, args.max_rounds, args.max_parallel, args.trace)
    elapsed = (time.perf_counter() - t0) * 1000.0

    print("=" * 72)
    print("error_form  :", args.error_form)
    print("status      :", res.status)
    print("rounds      :", res.rounds)
    print("model_calls :", res.model_calls)
    print("tool_calls  :", res.tool_calls)
    print("validation_errors :", res.validation_errors)
    print("retries     :", res.retries)
    print("elapsed_ms  : %.1f" % elapsed)
    print("-" * 72)
    print("批次划分（批内并行，批间串行）：")
    for b in res.batches:
        print("  " + b)
    if res.guard_hits:
        print("不可重放守卫：")
        for g in res.guard_hits:
            print("  " + g)
    print("-" * 72)
    print("服务端幂等账本：")
    for key, n in SERVER_STATE["executions"].items():
        print("  %s -> 请求 %d 次，实际执行 %d 次"
              % (key, SERVER_STATE["requests"].get(key, 0), n))
    for key, n in SERVER_STATE["requests"].items():
        if key not in SERVER_STATE["executions"]:
            print("  %s -> 请求 %d 次，实际执行 0 次（未落库）" % (key, n))
    if not SERVER_STATE["requests"]:
        print("  （无）")
    print("扣款流水：", SERVER_STATE["transfers"] or "无（未执行，转账状态未知）")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
