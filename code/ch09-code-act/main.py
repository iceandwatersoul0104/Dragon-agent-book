#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch09 代码即行动（Code-Act）最小可实现 —— 纯标准库，无需 API Key 就能跑通完整回路。

演示内容：
  1) 表达力：模型写程序调工具，用循环筛选/排序/求和——工具调用表达不了的部分
  2) 状态保持：同一命名空间跨轮复用变量 candidates
  3) 输出注入：stdout / 最后一个表达式的值 / traceback 回灌给模型
  4) 沙箱：标准库白名单、无裸 open、网络与子进程被拦、行数预算与墙上时钟时限
  5) 幻觉 API：调用不存在的 slack.post_message -> NameError -> 错误回灌 -> 自行改正

用法：
    python main.py                      # 四轮 mock 演示
    python main.py --trace              # 打印每轮代码与回灌内容
    python main.py --timeout 1.0        # 墙上时钟时限（秒）
    python main.py --max-lines 5000     # 行数预算
    python main.py --live               # 切真实模型

--live 所需环境变量：
    LLM_API_KEY    必填，模型服务商的 API Key
    LLM_BASE_URL   可选，默认 https://api.openai.com/v1
    LLM_MODEL      可选，默认 gpt-4o-mini
端点需兼容 `POST {base}/chat/completions`：body 含 model/messages，
返回 choices[0].message.content。换供应商时只改 base_url 与 model。

安全提醒：本示例在主进程内 exec 代码，只做白名单与配额限制，不是真正的沙箱。
生产环境必须用容器/虚拟机 + seccomp + 只读根文件系统 + 资源限额隔离。
"""

from __future__ import annotations

import argparse
import builtins as _builtins
import contextlib
import importlib
import io
import json
import os
import re
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# --------------------------------------------------------------------------
# 1. 沙箱
# --------------------------------------------------------------------------


class SandboxError(Exception):
    """沙箱拦截的错误。要能被程序自己捕获，模型才能读到并降级。"""


SAFE_MODULES = {"math", "json", "re", "statistics", "itertools", "datetime",
                "string", "collections", "functools", "fractions", "decimal"}

SAFE_BUILTINS = [
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float",
    "format", "frozenset", "int", "isinstance", "issubclass", "len", "list", "map",
    "max", "min", "next", "pow", "print", "range", "repr", "reversed", "round",
    "set", "slice", "sorted", "str", "sum", "tuple", "zip",
]

WORKSPACE = os.path.join(tempfile.gettempdir(), "ch09_code_act_ws")


SANDBOX_BLOCKS = {"count": 0}


def safe_import(name: str, globals=None, locals=None, fromlist=(), level=0):
    """只允许白名单里的标准库。网络、进程、文件系统相关的模块都进不来。"""
    root = name.split(".")[0]
    if root not in SAFE_MODULES:
        SANDBOX_BLOCKS["count"] += 1
        raise SandboxError("模块 %s 不在允许列表里（可用：%s）"
                           % (name, "、".join(sorted(SAFE_MODULES))))
    return importlib.import_module(name)


def trim_traceback(tb: str) -> str:
    """只保留沙箱代码的帧。宿主文件的行号对模型没意义，还会泄漏内部结构。"""
    lines = tb.splitlines()
    start = None
    for i, ln in enumerate(lines):
        # 帧头形如 '  File "<agent_code>", line 1, in <module>'
        if re.match(r'^\s*File "<agent_code>"', ln):
            start = i
            break
    if start is None:
        return lines[-1] if lines else tb
    kept = [ln for ln in lines[start:] if "main.py" not in ln]
    return "\n".join(kept).strip()


def _resolve_workspace(path: str) -> str:
    target = os.path.normpath(os.path.join(WORKSPACE, path))
    if not target.startswith(WORKSPACE):
        raise SandboxError("路径越界，只能访问工作目录：%s" % WORKSPACE)
    return target


def workspace_read(path: str) -> str:
    """读工作目录内的文件。不提供裸 open。"""
    with open(_resolve_workspace(path), "r", encoding="utf-8") as fh:
        return fh.read()


def workspace_write(path: str, content: str) -> str:
    """写工作目录内的文件。不提供裸 open。"""
    target = _resolve_workspace(path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(content)
    return target


# --------------------------------------------------------------------------
# 2. 工具：以函数形式注入命名空间
# --------------------------------------------------------------------------

ORDERS: Dict[str, List[Dict[str, Any]]] = {
    "张伟": [
        {"id": "A1", "amount": 120.0, "status": "paid", "channel": "app"},
        {"id": "A2", "amount": 1500.0, "status": "paid", "channel": "web"},
        {"id": "A3", "amount": 80.0, "status": "paid", "channel": "app"},
        {"id": "A4", "amount": 900.0, "status": "refund", "channel": "web"},
        {"id": "A5", "amount": 300.0, "status": "paid", "channel": "app"},
    ]
}

CALL_LOG: List[Dict[str, Any]] = []
SENT: List[Dict[str, Any]] = []


def search_orders(customer: str) -> List[Dict[str, Any]]:
    """检索某个客户的历史订单。只读，无副作用。返回订单列表。"""
    CALL_LOG.append({"tool": "search_orders", "args": {"customer": customer}})
    return list(ORDERS.get(customer, []))


def write_report(path: str, content: str) -> str:
    """把内容写入工作目录下的文件。有副作用（落盘）。"""
    CALL_LOG.append({"tool": "write_report", "args": {"path": path}})
    workspace_write(path, content)
    return "已写入 %s（%d 字符）" % (path, len(content))


def send_report(to: str, content: str) -> str:
    """把报告内容发送给指定收件人。有外部副作用。"""
    CALL_LOG.append({"tool": "send_report", "args": {"to": to}})
    SENT.append({"to": to, "bytes": len(content)})
    return "已发送给 %s" % to


TOOLS: Dict[str, Callable[..., Any]] = {
    "search_orders": search_orders,
    "write_report": write_report,
    "send_report": send_report,
}


def tool_signatures() -> str:
    lines = []
    for name, fn in TOOLS.items():
        first = (fn.__doc__ or "").strip().splitlines()[0]
        lines.append("- %s%s: %s" % (name, _signature(fn), first))
    lines.append("- read_text(path): 读工作目录内文件")
    lines.append("- write_text(path, content): 写工作目录内文件")
    return "\n".join(lines)


def _signature(fn: Callable[..., Any]) -> str:
    import inspect
    return str(inspect.signature(fn))


# --------------------------------------------------------------------------
# 3. 执行器：受限命名空间 + 状态保持 + 输出注入 + 配额
# --------------------------------------------------------------------------


@dataclass
class ExecOutcome:
    stdout: str = ""
    value: Any = None
    error: str = ""
    lines: int = 0
    sandbox_blocked: bool = False


class CodeExecutor:
    """在受限命名空间里执行模型写的代码。

    - 状态保持：self.ns 跨轮复用，变量留着给下一轮用
    - 输出注入：stdout + 最后一个表达式的值 + 异常堆栈
    - 配额：行数预算 + 墙上时钟时限（靠 sys.settrace）
    """

    def __init__(self, max_lines: int = 5000, time_limit: float = 2.0) -> None:
        self.max_lines = max_lines
        self.time_limit = time_limit
        restricted = {name: getattr(_builtins, name) for name in SAFE_BUILTINS}
        # import 语句去 builtins 里找 __import__，不是 globals
        restricted["__import__"] = safe_import
        self.ns: Dict[str, Any] = {
            "__builtins__": restricted,
            "__import__": safe_import,
            "__name__": "agent_sandbox",
            "SandboxError": SandboxError,
            "read_text": workspace_read,
            "write_text": workspace_write,
        }
        self.ns.update(TOOLS)

    def run(self, code: str) -> ExecOutcome:
        buf = io.StringIO()
        deadline = time.perf_counter() + self.time_limit
        budget = {"lines": 0}

        def tracer(frame, event, arg):
            if event == "line":
                budget["lines"] += 1
                if budget["lines"] > self.max_lines:
                    raise SandboxError("超过行数预算（%d 行）" % self.max_lines)
                if time.perf_counter() > deadline:
                    raise SandboxError("超过时限（%.2f 秒）" % self.time_limit)
            return tracer

        outcome = ExecOutcome()
        try:
            with contextlib.redirect_stdout(buf):
                sys.settrace(tracer)
                try:
                    exec(compile(code, "<agent_code>", "exec"), self.ns)  # noqa: S102
                finally:
                    sys.settrace(None)
            outcome.stdout = buf.getvalue()
            outcome.value = self._last_value(code)
        except SandboxError as exc:
            sys.settrace(None)
            outcome.stdout = buf.getvalue()
            outcome.error = "SandboxError: %s" % exc
            outcome.sandbox_blocked = True
        except BaseException:
            sys.settrace(None)
            outcome.stdout = buf.getvalue()
            outcome.error = trim_traceback(traceback.format_exc())
        outcome.lines = budget["lines"]
        return outcome

    def _last_value(self, code: str) -> Any:
        """最后一个表达式的值。含调用的行不重复执行，避免副作用翻倍。"""
        lines = [ln for ln in code.strip().splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        if not lines:
            return None
        last = lines[-1].strip()
        if any(ch in last for ch in "=("):
            return None
        try:
            return eval(compile(last, "<last_expr>", "eval"), self.ns)  # noqa: S307
        except Exception:
            return None

    def observation(self, outcome: ExecOutcome) -> str:
        """输出注入：把执行结果整理成给模型看的观察。"""
        parts: List[str] = []
        if outcome.stdout.strip():
            parts.append(outcome.stdout.strip())
        if outcome.value is not None:
            parts.append("[value] %r" % (outcome.value,))
        if outcome.error:
            parts.append(outcome.error)
        return "\n".join(parts) if parts else "(无输出)"


# --------------------------------------------------------------------------
# 4. 模型接入
# --------------------------------------------------------------------------

TASK = ("给客户张伟出一份已支付大额订单的汇总：筛出金额大于 100 的已支付订单，"
        "按金额降序，写成 report.md，然后发给 boss@example.com。")

SYSTEM_PROMPT = """你是一个用 Python 代码完成任务的 Agent。
可用函数：
%s

可用标准库（白名单）：%s

规则：
1. 只输出一段 ```python 代码块，代码里直接调用上面的函数；
2. 需要写文件用 write_report 或 write_text，不要自己 open；
3. 不要 import 白名单之外的模块；
4. 每轮只写一段代码，观察结果后再写下一段。
"""


class MockLLM:
    """按剧本返回四段程序，不做真实推理。"""

    P1 = '''orders = search_orders(customer="张伟")
candidates = []
for o in orders:
    if o["status"] == "paid" and o["amount"] > 100:
        candidates.append(o)
candidates.sort(key=lambda o: -o["amount"])
total = 0
for o in candidates:
    total += o["amount"]
print("命中 %d 单，合计 %.1f" % (len(candidates), total))
'''

    P2 = '''try:
    import socket
    print("能联网：不该出现")
except SandboxError as e:
    print("沙箱拦截：%s" % e)

lines = []
for o in candidates:
    lines.append("%s: %.1f" % (o["id"], o["amount"]))
content = "\\n".join(lines)
write_report(path="report.md", content=content)
print("报告已生成")
'''

    P3 = '''slack.post_message(channel="#ops", text="日汇总")
'''

    P4 = '''send_report(to="boss@example.com", content=content)
print("已发送")
'''

    def __call__(self, prompt: str) -> str:
        if "已发送" in prompt:
            return ""                                   # 任务完成，不再重复副作用
        if "NameError" in prompt:
            code = self.P4
        elif "SandboxError" in prompt or "沙箱拦截" in prompt:
            code = self.P3
        elif "命中" in prompt:
            code = self.P2
        else:
            code = self.P1
        return "```python\n%s```" % code


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
                "messages": [{"role": "system", "content": SYSTEM_PROMPT
                              % (tool_signatures(), "、".join(sorted(SAFE_MODULES)))},
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


def extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*(.*?)```", text, flags=re.S)
    if m:
        return m.group(1).strip()
    return text.strip()


# --------------------------------------------------------------------------
# 5. 主循环
# --------------------------------------------------------------------------


@dataclass
class Turn:
    index: int
    code: str = ""
    observation: str = ""
    lines: int = 0
    sandbox_blocked: bool = False


def run(llm: Callable[[str], str], task: str, max_turns: int = 6,
        max_lines: int = 5000, time_limit: float = 2.0,
        trace: bool = False) -> List[Turn]:
    executor = CodeExecutor(max_lines=max_lines, time_limit=time_limit)
    history: List[str] = []
    turns: List[Turn] = []
    for index in range(1, max_turns + 1):
        prompt = task if not history else task + "\n\n上一轮执行结果：\n" + "\n".join(history)
        raw = llm(prompt)
        code = extract_code(raw)
        if not code.strip():
            turns.append(Turn(index, "", "(任务结束，模型不再输出代码)", 0, False))
            break
        outcome = executor.run(code)
        obs = executor.observation(outcome)
        turns.append(Turn(index, code, obs, outcome.lines, outcome.sandbox_blocked))
        if trace:
            print("\n" + "=" * 72)
            print("[turn %d] code:" % index)
            print(code)
            print("-- observation --")
            print(obs)
            print("=" * 72)
        history.append(obs)
    return turns


# --------------------------------------------------------------------------
# 6. CLI
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
        description="代码即行动最小可实现（mock 模式无需 API Key）")
    parser.add_argument("--task", default=TASK)
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--max-lines", type=int, default=5000, help="行数预算")
    parser.add_argument("--timeout", type=float, default=2.0, help="墙上时钟时限（秒）")
    parser.add_argument("--live", action="store_true", help="切真实模型 API")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--model", default="", help="覆盖 LLM_MODEL")
    parser.add_argument("--base-url", default="", help="覆盖 LLM_BASE_URL")
    parser.add_argument("--trace", action="store_true", help="打印每轮代码与回灌内容")
    args = parser.parse_args(argv)

    llm = build_llm(args)
    t0 = time.perf_counter()
    turns = run(llm, args.task, args.max_turns, args.max_lines, args.timeout, args.trace)
    elapsed = (time.perf_counter() - t0) * 1000.0

    print("=" * 72)
    print("turns           :", len(turns))
    print("total_lines     :", sum(t.lines for t in turns))
    print("tool_calls      :", len(CALL_LOG))
    print("sandbox_blocks  :", SANDBOX_BLOCKS["count"])
    print("elapsed_ms      : %.1f" % elapsed)
    print("-" * 72)
    for t in turns:
        print("turn %d | lines=%-3d | %s"
              % (t.index, t.lines, t.observation.replace("\n", " / ")))
    print("-" * 72)
    print("工具调用记录：")
    for call in CALL_LOG:
        print("  %s %s" % (call["tool"], json.dumps(call["args"], ensure_ascii=False)))
    print("外发记录：", SENT or "无")
    report = os.path.join(WORKSPACE, "report.md")
    print("工作目录：", WORKSPACE)
    print("report.md 是否生成：", os.path.exists(report))
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
