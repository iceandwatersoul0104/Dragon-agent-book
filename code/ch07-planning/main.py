#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch07 规划范式最小可实现 —— 纯标准库，无需 API Key 就能跑通完整回路。

三组对照：
  1) plan_execute + patch   先规划后执行，计划过时做局部修补
  2) plan_execute + replan  先规划后执行，计划过时做全量重规划
  3) rewoo                  变量引用预解耦，执行期不需要模型
  4) search                 搜索型推理（分层展开 + 评估函数） vs 贪心单路径

用法：
    python main.py                       # 跑 plan_execute + patch
    python main.py --form rewoo
    python main.py --form plan_execute --policy replan
    python main.py --demo search
    python main.py --demo all
    python main.py --live                # 切真实模型

--live 所需环境变量：
    LLM_API_KEY    必填，模型服务商的 API Key
    LLM_BASE_URL   可选，默认 https://api.openai.com/v1
    LLM_MODEL      可选，默认 gpt-4o-mini
端点需兼容 `POST {base}/chat/completions`：body 含 model/messages，
返回 choices[0].message.content。换供应商时只改 base_url 与 model。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# 1. 计划：结构化对象，能打印、能改、能存
# --------------------------------------------------------------------------


@dataclass
class Step:
    tool: str
    args: Dict[str, str] = field(default_factory=dict)
    label: str = ""


@dataclass
class Plan:
    steps: List[Step] = field(default_factory=list)

    def render(self) -> str:
        lines = []
        for i, s in enumerate(self.steps, 1):
            args = ", ".join('%s="%s"' % (k, v) for k, v in s.args.items())
            lines.append("%s(%s)  -> #E%d" % (s.tool, args, i))
        return "\n".join(lines)

    def patch(self, index: int, **kw: str) -> "Plan":
        """局部修补：只改这一步的参数，其余原样保留。"""
        new = Plan([copy.deepcopy(s) for s in self.steps])
        new.steps[index].args.update(kw)
        return new


STEP_RE = re.compile(r"^\s*([a-z_]+)\s*\((.*)\)\s*$")
ARG_RE = re.compile(r'([a-z_]+)\s*=\s*"([^"]*)"|([a-z_]+)\s*=\s*(\S+)')


def parse_step(line: str) -> Optional[Step]:
    m = STEP_RE.match(line)
    if not m:
        return None
    tool, raw_args = m.group(1), m.group(2)
    args: Dict[str, str] = {}
    for a in ARG_RE.finditer(raw_args):
        if a.group(1):
            args[a.group(1)] = a.group(2)
        else:
            args[a.group(3)] = a.group(4)
    return Step(tool=tool, args=args)


def parse_plan(text: str) -> Plan:
    """一行一步，格式 tool(arg="value")；不合法的行直接忽略。"""
    steps = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        step = parse_step(line)
        if step:
            steps.append(step)
    return Plan(steps)


# --------------------------------------------------------------------------
# 2. 工具环境（确定性，可复现）
# --------------------------------------------------------------------------


class ToolError(Exception):
    """工具执行失败 = 计划过时的触发信号之一。"""


ADDRESS_BOOK = {"manager@example.com": "王经理", "team@example.com": "项目组"}

# 通讯录查询：老板这个别名没登记，需要换个收件人
DIRECTORY = {"boss@example.com": "manager@example.com"}

TOOLS: Dict[str, Callable[..., str]] = {}


def tool_read_csv(path: str) -> str:
    if path != "data.csv":
        raise ToolError("文件不存在: %s" % path)
    return "rows: 电商 120, 门店 64, 企业 31"


def tool_aggregate(rows: str) -> str:
    if not rows:
        raise ToolError("聚合失败：输入为空")
    return "日报汇总: 电商 120 单, 门店 64 单, 企业 31 单"


def tool_write_report(path: str, content: str) -> str:
    return "已写入 %s（%d 字符）" % (path, len(content))


def tool_send_email(to: str, body: str) -> str:
    if to not in ADDRESS_BOOK:
        raise ToolError("收件人不在通讯录: %s" % to)
    return "已发送给 %s（%s）" % (to, ADDRESS_BOOK[to])


TOOLS = {
    "read_csv": tool_read_csv,
    "aggregate": tool_aggregate,
    "write_report": tool_write_report,
    "send_email": tool_send_email,
}


def resolve_arg(value: str, values: Dict[str, str]) -> str:
    """把 #E1 这样的变量引用换成上一步的真实返回值。"""
    if value.startswith("#E"):
        return values.get(value, "")
    return value


def run_step(step: Step, values: Dict[str, str]) -> str:
    fn = TOOLS.get(step.tool)
    if fn is None:
        raise ToolError("未知工具: %s" % step.tool)
    kwargs = {k: resolve_arg(v, values) for k, v in step.args.items()}
    return fn(**kwargs)


# --------------------------------------------------------------------------
# 3. 提示与模型接入
# --------------------------------------------------------------------------

PIPELINE_TASK = ("读取 data.csv，统计各类型订单数，写成日报写到 report.md，"
                  "然后发送给 boss@example.com。")

TOOL_SPEC = ("read_csv(path) -> rows；aggregate(rows) -> summary；"
             "write_report(path, content) -> ok；send_email(to, body) -> ok")

PLAN_PROMPT = """把下面的任务拆成步骤，一行一步，格式 tool(arg="value")。
变量引用用 #E1、#E2 表示上一步的输出。
可用工具：%s

任务：%s"""

REPLAN_PROMPT = """刚才的计划执行失败了，错误信息：
%s

已完成到第 %d 步。请给出修正后的完整计划，一行一步，格式 tool(arg="value")。
可用工具：%s

任务：%s"""

RESOLVE_PROMPT = """这一步是：%s
已知的变量取值：%s
请给出这一步的实际调用，格式 tool(arg="value")。"""


class MockLLM:
    """按提示特征返回预设内容，不做真实推理。"""

    PLAN = "\n".join([
        'read_csv(path="data.csv")',
        'aggregate(rows="#E1")',
        'write_report(path="report.md", content="#E2")',
        'send_email(to="boss@example.com", body="#E3")',
    ])

    REPLAN = "\n".join([
        'read_csv(path="data.csv")',
        'aggregate(rows="#E1")',
        'write_report(path="report.md", content="#E2")',
        'send_email(to="manager@example.com", body="#E3")',
    ])

    def __call__(self, role: str, prompt: str) -> str:
        if role == "plan":
            return self.PLAN
        if role == "replan":
            return self.REPLAN
        if role == "resolve":
            # 原样回显这一步（真实模型在这里把 #E 填成实参）
            m = re.search(r"这一步是：(.+)", prompt)
            return m.group(1).strip() if m else ""
        if role == "search":
            # 模型给出一个偏好顺序；第一位是它最想选的
            return "深圳, 上海, 北京, 杭州, 成都"
        return ""


class LiveLLM:
    """真实模型：OpenAI 兼容的 chat completions 协议。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 60, temperature: float = 0.0) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    def __call__(self, role: str, prompt: str) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError("HTTP %s from %s: %s" % (exc.code, self.url, detail)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("cannot reach %s: %s" % (self.url, exc.reason)) from exc
        return data["choices"][0]["message"]["content"] or ""


# --------------------------------------------------------------------------
# 4. 两种执行范式
# --------------------------------------------------------------------------


@dataclass
class RunResult:
    form: str
    policy: str
    status: str = "not_solved"
    llm_calls: int = 0
    tool_calls: int = 0
    steps_done: int = 0
    plan_len: int = 0
    stop_reason: str = ""
    plan_text: str = ""
    events: List[str] = field(default_factory=list)


def local_patch(step: Step, error: Exception) -> Optional[Dict[str, str]]:
    """局部修补：确定性地改这一步的参数，不调用模型。

    这里模拟"工具契约层的修复"——收件人不在通讯录，就查通讯录换一个。
    """
    if step.tool == "send_email" and "通讯录" in str(error):
        alt = DIRECTORY.get(step.args.get("to", ""))
        if alt:
            return {"to": alt}
    return None


class Planner:
    """plan_execute 与 rewoo 的执行器，差别只在执行期要不要模型在线。"""

    def __init__(self, llm: Callable[[str, str], str], form: str, policy: str = "patch",
                 max_steps: int = 12, trace: bool = False) -> None:
        self.llm = llm
        self.form = form
        self.policy = policy
        self.max_steps = max_steps
        self.trace = trace

    def run(self, task: str = PIPELINE_TASK) -> RunResult:
        res = RunResult(form=self.form, policy=self.policy)
        plan_text = self.llm("plan", PLAN_PROMPT % (TOOL_SPEC, task))
        res.llm_calls += 1
        plan = parse_plan(plan_text)
        res.plan_text = plan.render()
        res.plan_len = len(plan.steps)
        if self.trace:
            print("\n[plan]\n" + res.plan_text)

        values: Dict[str, str] = {}
        i = 0
        pass_steps = 0          # 本轮计划已完成几步（全量重规划后归零）
        while i < len(plan.steps) and res.steps_done < self.max_steps:
            step = plan.steps[i]

            if self.form == "plan_execute":
                # 执行期模型在线：把变量引用填成实参（这一步就是成本所在）
                known = "; ".join("%s=%s" % (k, v) for k, v in values.items()) or "（无）"
                resolved_text = self.llm("resolve", RESOLVE_PROMPT % (step.tool, known))
                res.llm_calls += 1
                resolved = parse_step(resolved_text.splitlines()[0]) if resolved_text.strip() else None
                effective = resolved or step
            else:
                # rewoo：解释器直接解引用，执行期没有模型
                effective = step

            try:
                out = run_step(effective, values)
                res.tool_calls += 1
                values["#E%d" % (i + 1)] = out
                res.steps_done += 1
                pass_steps += 1
                res.events.append("step %d %s -> ok" % (i + 1, effective.tool))
                i += 1
            except ToolError as exc:
                res.tool_calls += 1
                res.events.append("step %d %s -> FAIL(%s)" % (i + 1, effective.tool, exc))
                # ---- 计划过时：三条处理路径 ----
                if self.policy == "patch":
                    fix = local_patch(step, exc)
                    if fix:
                        plan = plan.patch(i, **fix)
                        res.events.append("  -> 局部修补: %s" % fix)
                        continue
                if self.policy == "patch":
                    res.stop_reason = "patch_not_applicable"
                    res.status = "not_solved"
                    break
                # 全量重规划：带上已完成结果作为事实，重新生成整条计划
                plan_text = self.llm("replan", REPLAN_PROMPT % (exc, i + 1, TOOL_SPEC, task))
                res.llm_calls += 1
                plan = parse_plan(plan_text)
                res.plan_text = plan.render()
                res.plan_len = len(plan.steps)
                res.events.append("  -> 全量重规划（已完成的结果作废，从头再跑）")
                values = {}
                pass_steps = 0
                i = 0
                continue
        else:
            if not res.stop_reason:
                res.stop_reason = "plan_finished"

        if res.stop_reason == "plan_finished":
            res.status = "solved" if pass_steps == res.plan_len else "not_solved"
        return res


# --------------------------------------------------------------------------
# 5. 搜索型推理：分层展开 + 评估函数
# --------------------------------------------------------------------------

CITIES = [("北京", 3200, 3), ("上海", 2800, 4), ("成都", 1800, 1),
          ("深圳", 3600, 5), ("杭州", 2400, 2)]
BUDGET = 5000
WEST_CITY = "成都"
CITY_BY_NAME = {n: (n, c, k) for n, c, k in CITIES}


def evaluate(path: List[Tuple[str, int, int]], final: bool) -> Tuple[bool, int]:
    """评估函数：预算内、含西部城市（最后一步才检查），收益=能见的客户数。"""
    cost = sum(c for _, c, _ in path)
    clients = sum(k for _, _, k in path)
    if cost > BUDGET:
        return False, 0
    if final and not any(n == WEST_CITY for n, _, _ in path):
        return False, 0
    return True, clients


def greedy_path(order: List[str]) -> Tuple[Optional[List[str]], int, str]:
    """贪心：只走一条路径，不回溯（链式思考的样子）。"""
    path: List[str] = []
    evals = 0
    for depth in range(2):
        remain = [n for n in order if n not in path]
        if not remain:
            break
        pick = remain[0]
        path.append(pick)
        ok, _ = evaluate([CITY_BY_NAME[n] for n in path], final=(depth == 1))
        evals += 1
        if not ok:
            return None, evals, "路径 %s 走死" % "→".join(path)
    return path, evals, "路径 %s" % "→".join(path)


def beam_search(order: List[str], k: int = 2, max_depth: int = 2
                ) -> Tuple[Optional[List[str]], int, str]:
    """树/图搜索：每层展开多个候选，用评估函数剪枝。"""
    frontier: List[Tuple[List[str], int]] = [([], 0)]
    evals = 0
    for depth in range(max_depth):
        nxt: List[Tuple[List[str], int]] = []
        for path, _ in frontier:
            for name in order:
                if name in path:
                    continue
                cand = path + [name]
                ok, score = evaluate([CITY_BY_NAME[n] for n in cand],
                                     final=(depth == max_depth - 1))
                evals += 1
                if ok:
                    nxt.append((cand, score))
        nxt.sort(key=lambda x: -x[1])
        frontier = nxt[:k]
        if not frontier:
            return None, evals, "所有分支都不可行"
    best_path, best_score = max(frontier, key=lambda x: x[1])
    return best_path, evals, "最优组合 %s，客户数 %d" % ("→".join(best_path), best_score)


def run_search_demo(llm: Callable[[str, str], str]) -> List[str]:
    """返回若干行报告文本。"""
    prompt = ("可选城市（费用, 能见客户数）：" +
              "；".join("%s(%d,%d)" % (n, c, k) for n, c, k in CITIES) +
              "。预算 5000，必须含成都，选两个城市。请按偏好顺序给出城市名，逗号分隔。")
    raw = llm("search", prompt)
    # 按模型给出的顺序取候选（按在文本中出现的先后排序）
    hits = sorted((raw.index(n), n) for n in CITY_BY_NAME if n in raw)
    order = [n for _, n in hits] or [n for n, _, _ in sorted(CITIES, key=lambda x: -x[2])]

    g_path, g_evals, g_note = greedy_path(order)
    b_path, b_evals, b_note = beam_search(order)
    lines = [
        "=" * 72,
        "搜索型推理 vs 贪心单路径",
        "=" * 72,
        "候选顺序     : %s" % "→".join(order),
        "贪心单路径   : 评估 %d 次 | %s | 结果=%s"
        % (g_evals, g_note, "可行方案 " + "→".join(g_path) if g_path else "无可行方案"),
        "分层搜索     : 评估 %d 次 | %s | 结果=%s"
        % (b_evals, b_note, "可行方案 " + "→".join(b_path) if b_path else "无可行方案"),
        "-" * 72,
        "搜索多花 %d 次评估，换来贪心没找到的可行解。" % (b_evals - g_evals),
        "=" * 72,
    ]
    return lines


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


def build_llm(args: argparse.Namespace) -> Callable[[str, str], str]:
    if not args.live:
        return MockLLM()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "--live 需要环境变量 LLM_API_KEY；"
            "可选 LLM_BASE_URL（默认 https://api.openai.com/v1）与 LLM_MODEL。\n"
        )
        raise SystemExit(2)
    base_url = args.base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model = args.model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    return LiveLLM(base_url, api_key, model, temperature=args.temperature)


def print_run(res: RunResult, elapsed_ms: float) -> None:
    print("=" * 72)
    print("form        :", res.form)
    print("policy      :", res.policy)
    print("status      :", res.status)
    print("stop_reason :", res.stop_reason)
    print("llm_calls   :", res.llm_calls)
    print("tool_calls  :", res.tool_calls)
    print("steps_done  :", res.steps_done)
    print("elapsed_ms  : %.1f" % elapsed_ms)
    print("-" * 72)
    for e in res.events:
        print("  " + e)
    print("=" * 72)


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="规划范式最小可实现（mock 模式无需 API Key）")
    parser.add_argument("--form", default="plan_execute",
                        choices=["plan_execute", "rewoo"],
                        help="执行范式：先规划后执行 / 变量引用预解耦")
    parser.add_argument("--policy", default="patch", choices=["patch", "replan"],
                        help="计划过时的处理：局部修补 / 全量重规划")
    parser.add_argument("--demo", default="run", choices=["run", "search", "all"],
                        help="跑哪一组演示")
    parser.add_argument("--task", default=PIPELINE_TASK)
    parser.add_argument("--live", action="store_true", help="切真实模型 API")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--model", default="", help="覆盖 LLM_MODEL")
    parser.add_argument("--base-url", default="", help="覆盖 LLM_BASE_URL")
    parser.add_argument("--trace", action="store_true", help="打印计划与事件")
    args = parser.parse_args(argv)

    llm = build_llm(args)

    if args.demo == "search":
        for line in run_search_demo(llm):
            print(line)
        return 0

    runs: List[RunResult] = []
    combos = ([("plan_execute", "patch"), ("plan_execute", "replan"), ("rewoo", "patch")]
              if args.demo == "all" else [(args.form, args.policy)])
    for form, policy in combos:
        planner = Planner(llm, form, policy, trace=args.trace)
        t0 = time.perf_counter()
        res = planner.run(args.task)
        res_ll = (time.perf_counter() - t0) * 1000.0
        if args.demo == "all":
            print_run(res, res_ll)
        runs.append(res)

    if args.demo == "all":
        print("\n" + "=" * 72)
        print("form / policy            llm_calls  tool_calls  status")
        for r in runs:
            print("%-24s %9d  %10d  %s"
                  % ("%s / %s" % (r.form, r.policy), r.llm_calls, r.tool_calls, r.status))
        print("=" * 72)
    else:
        print_run(runs[0], 0.0)

    if args.demo == "all":
        for line in run_search_demo(llm):
            print(line)
    return 0 if all(r.status == "solved" for r in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
