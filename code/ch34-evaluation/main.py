#!/usr/bin/env python3
"""ch34 评测体系 —— 最小可实现参考实现。

包含一个能真正演示"评测怎么用"的最小闭环：
    1. 任务集：6 条任务，覆盖三个难度层，黄金答案写成"关键事实清单 + 禁止项"。
    2. 被测系统：mock Agent 按任务质量作答；--live 用真实 API 作答。
    3. 判定器：关键事实覆盖（程序化断言）+ 一个可控偏差的 mock Judge，
       能开关位置偏差、长度偏好、自偏好。
    4. 偏差演示：同一答案交换顺序判两次，统计 judge 不稳定率，
       再演示"位置交换取严"如何压住结论翻转。
    5. CI 门禁：与 baseline.json 对比，通过率跌幅超阈值则退出码非 0，
       并列出具体回归的任务清单。

依赖：仅 Python 标准库（>= 3.8）。默认 mock 模式不需要任何 API Key。
"""

from __future__ import annotations

import argparse
import json
import os
import random
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


# ---------------------------------------------------------------------------
# 任务集
# ---------------------------------------------------------------------------


@dataclass
class Task:
    id: str
    difficulty: str                 # easy | medium | hard
    tags: List[str]
    question: str
    context: List[str]              # 环境里可用的事实
    key_facts: List[str]            # 必须包含的信息点
    must_not: List[str] = field(default_factory=list)   # 禁止出现的内容
    omit: List[str] = field(default_factory=list)        # mock agent 要漏掉的事实
    violate: List[str] = field(default_factory=list)     # mock agent 要违反的禁止项
    prompt_tokens: int = 600
    completion_tokens: int = 120


def build_tasks() -> List[Task]:
    return [
        Task(
            id="refund-001", difficulty="easy", tags=["单轮", "退款"],
            question="用户问订单 A1001 的退款什么时候到账？",
            context=["订单号 A1001", "退款原路退回", "到账时效 3 个工作日"],
            key_facts=["A1001", "3 个工作日"],
            must_not=["今天一定到账"],
            prompt_tokens=380, completion_tokens=70,
        ),
        Task(
            id="refund-002", difficulty="easy", tags=["单轮", "退款"],
            question="用户问订单 A1002 的退款进度。",
            context=["订单号 A1002", "退款原路退回", "到账时效 5 个工作日"],
            key_facts=["A1002", "5 个工作日"],
            omit=["到账时效 5 个工作日"],          # 漏掉关键事实 -> 明确失败
            prompt_tokens=390, completion_tokens=60,
        ),
        Task(
            id="policy-001", difficulty="medium", tags=["规则", "多事实"],
            question="用户问退款规则，客服要说明适用范围、到账时效和例外情况。",
            context=["适用范围：订单完成后 30 天内", "到账时效 3 个工作日",
                     "例外：预售商品不适用"],
            key_facts=["30 天内", "3 个工作日", "预售商品不适用"],
            omit=["例外：预售商品不适用"],          # 2/3 命中 -> 边界样本
            prompt_tokens=760, completion_tokens=150,
        ),
        Task(
            id="policy-002", difficulty="medium", tags=["规则", "多事实"],
            question="用户问退款规则，客服要说明适用范围、到账时效和例外情况。",
            context=["适用范围：订单完成后 30 天内", "到账时效 3 个工作日",
                     "例外：预售商品不适用"],
            key_facts=["30 天内", "3 个工作日", "预售商品不适用"],
            prompt_tokens=780, completion_tokens=160,
        ),
        Task(
            id="edge-001", difficulty="hard", tags=["边界", "拒答"],
            question="用户要求承诺具体到账日期，客服要拒绝承诺并说明时效。",
            context=["到账时效 3 个工作日", "退款原路退回"],
            key_facts=["3 个工作日", "原路退回"],
            must_not=["今天一定到账", "保证明天到账"],
            omit=["退款原路退回"],                   # 漏事实 + 乱承诺 -> 明确失败
            violate=["保证明天到账"],
            prompt_tokens=900, completion_tokens=140,
        ),
        Task(
            id="multi-001", difficulty="hard", tags=["多轮", "多事实"],
            question="用户连续追问，要求给出处理方案、时限、联系方式和查询入口。",
            context=["处理方案：退款原路退回", "时限 3 个工作日",
                     "联系方式：客服热线 400-800-1234", "查询入口：我的订单页",
                     "到账说明：以银行入账时间为准"],
            key_facts=["原路退回", "3 个工作日", "400-800-1234", "我的订单页",
                       "以银行入账时间为准"],
            omit=["联系方式：客服热线 400-800-1234", "到账说明：以银行入账时间为准"],
            prompt_tokens=1150, completion_tokens=190,
        ),
    ]


# ---------------------------------------------------------------------------
# 判定器
# ---------------------------------------------------------------------------


def fact_coverage(answer: str, key_facts: List[str]) -> Tuple[List[str], List[str]]:
    """黄金答案不写成标准句子，而是"必须包含的信息点"清单。"""
    low = answer.lower()
    hit = [f for f in key_facts if f.lower() in low]
    missing = [f for f in key_facts if f.lower() not in low]
    return hit, missing


@dataclass
class JudgeConfig:
    position_bias: bool = True      # 交换顺序会改变结论
    length_bias: bool = True        # 更长的答案更容易得高分
    self_preference: bool = False   # judge 与被测同源时给高分
    judge_family: str = "judge-A"
    agent_family: str = "agent-B"
    threshold: float = 7.0
    length_threshold: int = 100     # 超过这个长度的答案会拿到长度加分


class MockJudge:
    """刻意带偏差的 mock judge，用来演示偏差与缓解手段。"""

    def __init__(self, config: JudgeConfig) -> None:
        self.config = config

    def score(self, task: Task, answer: str, swapped: bool = False) -> Dict[str, Any]:
        hit, missing = fact_coverage(answer, task.key_facts)
        total = max(1, len(task.key_facts))
        score = 10.0 * len(hit) / total
        violated = [m for m in task.must_not if m.lower() in answer.lower()]
        if violated:
            score -= 3.0
        if self.config.length_bias and len(answer) > self.config.length_threshold:
            score += 0.8
        if self.config.self_preference and self.config.judge_family == self.config.agent_family:
            score += 0.8
        if self.config.position_bias:
            score += -0.6 if swapped else 0.6
        score = max(0.0, min(10.0, score))
        return {
            "score": round(score, 2),
            "pass": score >= self.config.threshold,
            "hit_facts": hit,
            "missing_facts": missing,
            "violated": violated,
        }


def judge_with_swap(judge: MockJudge, task: Task, answer: str) -> Dict[str, Any]:
    """位置偏差演示：同一份答案，换个顺序判分可能不同。"""
    first = judge.score(task, answer)
    second = judge.score(task, answer, swapped=True)
    stable = first["pass"] == second["pass"]
    return {
        "first": first,
        "second": second,
        "stable": stable,
        # 缓解手段：位置交换后取严（两次都通过才算通过）
        "strict_pass": first["pass"] and second["pass"],
    }


# ---------------------------------------------------------------------------
# 被测系统
# ---------------------------------------------------------------------------


class MockAgent:
    """确定性 mock：按任务预设的缺陷作答，用来产出不同质量的答案。"""

    def __init__(self, style: str = "plain") -> None:
        self.style = style

    def answer(self, task: Task) -> str:
        parts = [f for f in task.context if f not in task.omit]
        parts += task.violate
        text = "；".join(parts)
        if self.style == "verbose":
            # 信息完全没多，只是更啰嗦——用来演示长度偏好
            text += "。" + "非常感谢您的理解与支持，我们会持续改进服务体验，祝您生活愉快！" * 3
        return text


class LiveAgent:
    """--live：用真实 API 作答，判定器保持不变。"""

    def __init__(self, model: str, base_url: str, api_key: str) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key

    def answer(self, task: Task) -> str:
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content":
                    "你是客服助手。只能使用【已知信息】里的事实作答，"
                    "信息不足要说明不确定，不要编造，不要承诺具体到账日期。"},
                {"role": "user", "content":
                    "【已知信息】\n" + "\n".join(task.context) + "\n\n【问题】\n" + task.question},
            ],
        }
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# 成本
# ---------------------------------------------------------------------------

# 示意价目表（每百万 token），仅用于演示成本口径
PRICE_TABLE = {"agent-B": {"prompt": 0.15, "completion": 0.60},
               "agent-A": {"prompt": 3.00, "completion": 15.00}}
DEFAULT_PRICE = {"prompt": 0.15, "completion": 0.60}


def estimate_cost(tasks: List[Task], agent_family: str) -> float:
    price = PRICE_TABLE.get(agent_family, DEFAULT_PRICE)
    total = 0.0
    for task in tasks:
        total += (task.prompt_tokens * price["prompt"] + task.completion_tokens * price["completion"]) / 1_000_000
    return total


# ---------------------------------------------------------------------------
# 评测主流程
# ---------------------------------------------------------------------------


def run_evaluation(tasks: List[Task], agent: Any, judge: MockJudge,
                   agent_family: str) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    for task in tasks:
        answer = agent.answer(task)
        judged = judge_with_swap(judge, task, answer)
        results.append({
            "task_id": task.id,
            "difficulty": task.difficulty,
            "tags": task.tags,
            "answer": answer,
            "score": judged["first"]["score"],
            "pass": judged["first"]["pass"],
            "strict_pass": judged["strict_pass"],
            "stable": judged["stable"],
            "missing_facts": judged["first"]["missing_facts"],
            "violated": judged["first"]["violated"],
        })
    passed = sum(1 for r in results if r["pass"])
    strict = sum(1 for r in results if r["strict_pass"])
    unstable = sum(1 for r in results if not r["stable"])
    cost = estimate_cost(tasks, agent_family)
    by_difficulty: Dict[str, Dict[str, int]] = {}
    for r in results:
        slot = by_difficulty.setdefault(r["difficulty"], {"total": 0, "pass": 0})
        slot["total"] += 1
        slot["pass"] += 1 if r["pass"] else 0
    return {
        "agent_family": agent_family,
        "tasks_version": "tasks-v1",
        "total": len(results),
        "pass_rate": passed / len(results) if results else 0.0,
        "strict_pass_rate": strict / len(results) if results else 0.0,
        "judge_unstable_rate": unstable / len(results) if results else 0.0,
        "cost_total": round(cost, 6),
        "cost_per_success": round(cost / passed, 6) if passed else None,
        "by_difficulty": by_difficulty,
        "per_task": {r["task_id"]: {"pass": r["pass"], "strict_pass": r["strict_pass"],
                                   "score": r["score"], "stable": r["stable"]}
                     for r in results},
        "results": results,
    }


def print_report(report: Dict[str, Any]) -> None:
    print("=" * 78)
    print("评测报告")
    print("=" * 78)
    print("{:<12} {:<7} {:>6} {:>7} {:>6}  {}".format(
        "任务", "难度", "得分", "判定", "稳定", "缺失事实 / 违规"))
    for r in report["results"]:
        flag = "通过" if r["pass"] else "失败"
        stable = "是" if r["stable"] else "否"
        note = ",".join(r["missing_facts"]) if r["missing_facts"] else "-"
        if r["violated"]:
            note += " | 违规: " + ",".join(r["violated"])
        print("{:<12} {:<7} {:>6} {:>7} {:>6}  {}".format(
            r["task_id"], r["difficulty"], r["score"], flag, stable, note))
    print("-" * 78)
    print("单次判定通过率      : {}/{} = {:.1%}".format(
        report["total"] and sum(1 for r in report["results"] if r["pass"]),
        report["total"], report["pass_rate"]))
    print("位置交换取严后通过率: {:.1%}".format(report["strict_pass_rate"]))
    print("judge 不稳定率      : {:.1%}（同一答案换顺序判，结论翻转的比例）".format(
        report["judge_unstable_rate"]))
    for level, slot in sorted(report["by_difficulty"].items()):
        print("  难度 {}: {}/{} = {:.1%}".format(
            level, slot["pass"], slot["total"],
            slot["pass"] / slot["total"] if slot["total"] else 0.0))
    print("总成本              : ${:.6f}".format(report["cost_total"]))
    print("每次成功成本        : {}".format(
        "${:.6f}".format(report["cost_per_success"]) if report["cost_per_success"] else "无成功任务"))


# ---------------------------------------------------------------------------
# 基线对比与 CI 门禁
# ---------------------------------------------------------------------------


def compare_baseline(report: Dict[str, Any], baseline: Dict[str, Any],
                     threshold: float) -> Tuple[bool, List[str]]:
    regressions: List[str] = []
    for task_id, old in baseline.get("per_task", {}).items():
        new = report["per_task"].get(task_id)
        if new is None:
            continue
        if old.get("pass") and not new.get("pass"):
            regressions.append(task_id)
    drop = baseline.get("pass_rate", 0.0) - report["pass_rate"]
    failed = drop > threshold
    return failed, regressions


def save_baseline(path: str, report: Dict[str, Any], meta: Dict[str, Any]) -> None:
    payload = dict(report)
    payload["results"] = []  # 基线不存答案全文，只存判定
    payload["meta"] = meta
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def load_baseline(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ch34 评测体系最小实现")
    parser.add_argument("--baseline", default="baseline.json", help="基线文件路径")
    parser.add_argument("--threshold", type=float, default=0.02, help="允许的通过率跌幅")
    parser.add_argument("--update-baseline", action="store_true", help="用本次结果覆盖基线")
    parser.add_argument("--agent-style", default="plain", choices=["plain", "verbose"],
                        help="verbose=回答更啰嗦，用来演示长度偏好")
    parser.add_argument("--position-bias", dest="position_bias", action="store_true", default=True)
    parser.add_argument("--no-position-bias", dest="position_bias", action="store_false")
    parser.add_argument("--length-bias", dest="length_bias", action="store_true", default=True)
    parser.add_argument("--no-length-bias", dest="length_bias", action="store_false")
    parser.add_argument("--self-preference", action="store_true", help="judge 与被测同源（演示自偏好）")
    parser.add_argument("--live", action="store_true", help="用真实 API 作答（需 OPENAI_API_KEY）")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    args = parser.parse_args(argv)

    if args.live and not os.environ.get("OPENAI_API_KEY"):
        print("[!] --live 需要环境变量 OPENAI_API_KEY；未设置时可用 mock 模式跑通全回路。")
        return 2

    tasks = build_tasks()
    judge = MockJudge(JudgeConfig(
        position_bias=args.position_bias,
        length_bias=args.length_bias,
        self_preference=args.self_preference,
        judge_family="judge-A",
        agent_family="agent-A" if args.self_preference else "agent-B",
    ))

    if args.live:
        agent: Any = LiveAgent(args.model, args.base_url, os.environ.get("OPENAI_API_KEY", ""))
        agent_family = "agent-A"
    else:
        agent = MockAgent(style=args.agent_style)
        agent_family = "agent-B"

    report = run_evaluation(tasks, agent, judge, agent_family)
    print_report(report)

    meta = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "judge": {"position_bias": args.position_bias, "length_bias": args.length_bias,
                  "self_preference": args.self_preference, "family": judge.config.judge_family},
        "agent_style": args.agent_style,
        "mode": "live" if args.live else "mock",
    }

    baseline = load_baseline(args.baseline)
    if args.update_baseline or baseline is None:
        save_baseline(args.baseline, report, meta)
        print("-" * 78)
        print("基线已写入：{}（首次运行或 --update-baseline）".format(args.baseline))
        return 0

    failed, regressions = compare_baseline(report, baseline, args.threshold)
    drop = baseline.get("pass_rate", 0.0) - report["pass_rate"]
    print("-" * 78)
    print("基线对比：基线通过率 {:.1%} -> 本次 {:.1%}（跌幅 {:.1%}，阈值 {:.1%}）".format(
        baseline.get("pass_rate", 0.0), report["pass_rate"], drop, args.threshold))
    if regressions:
        print("回归任务清单（基线通过 -> 本次失败）：")
        for task_id in regressions:
            print("  - " + task_id)
    else:
        print("没有任务从通过变为失败。")
    if failed:
        print("[门禁] 通过率跌幅超过阈值，CI 应当拦截。")
        return 1
    print("[门禁] 通过率跌幅在阈值内，放行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
