#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch11 停止准则、步数上限与成本守卫 最小可实现 —— 纯标准库，无需 API Key 就能跑通完整回路。

演示内容：
  1) 四类停止条件：显式完成 / 预算耗尽 / 无进展 / 异常终止，判定全部由确定性代码做
  2) 无进展三信号：动作重复、观察不变、状态无变化
  3) 预算守卫：token 预算、时间预算、美元成本上限的统一抽象
  4) 超时降级：停止时返回部分结果，而不是抛异常
  5) 模型说"完成"不算数——还要过完成条件校验

用法：
    python main.py                      # 跑全部六个场景
    python main.py --scenario budget --max-tokens 500
    python main.py --max-steps 3
    python main.py --trace              # 打印每轮动作、观察、预算余额与停止判定
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
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# 1. 数据结构
# --------------------------------------------------------------------------


@dataclass
class Action:
    name: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Step:
    action: Action
    observation: str = ""
    state_hash: str = ""
    in_tokens: int = 0
    out_tokens: int = 0
    elapsed: float = 0.0
    error: str = ""


@dataclass
class StopDecision:
    reason: str
    detail: str = ""


@dataclass
class Result:
    status: str = "partial"          # completed / partial / failed
    stop_reason: str = ""
    detail: str = ""
    rounds: int = 0
    tokens: int = 0
    usd: float = 0.0
    answer: str = ""
    steps: List[Step] = field(default_factory=list)
    partial: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 2. 预算守卫：token / 时间 / 美元，统一抽象
# --------------------------------------------------------------------------


class Budget:
    """一次运行最多允许消耗多少资源。美元由价格表算，单价不写死在代码里。"""

    def __init__(self, max_tokens: Optional[int] = None, max_seconds: Optional[float] = None,
                 max_usd: Optional[float] = None, price_in_per_1k: float = 0.001,
                 price_out_per_1k: float = 0.002) -> None:
        self.max_tokens = max_tokens
        self.max_seconds = max_seconds
        self.max_usd = max_usd
        self.price_in_per_1k = price_in_per_1k
        self.price_out_per_1k = price_out_per_1k
        self.tokens = 0
        self.seconds = 0.0
        self.usd = 0.0

    def charge(self, in_tokens: int, out_tokens: int, elapsed: float) -> None:
        self.tokens += in_tokens + out_tokens
        self.seconds += elapsed
        self.usd += (in_tokens / 1000.0) * self.price_in_per_1k + \
                   (out_tokens / 1000.0) * self.price_out_per_1k

    def check(self) -> Optional[str]:
        """任一维度越界即停。返回越界说明。"""
        if self.max_tokens is not None and self.tokens > self.max_tokens:
            return "token 预算耗尽（%d > %d）" % (self.tokens, self.max_tokens)
        if self.max_seconds is not None and self.seconds > self.max_seconds:
            return "时间预算耗尽（%.2fs > %.2fs）" % (self.seconds, self.max_seconds)
        if self.max_usd is not None and self.usd > self.max_usd:
            return "成本预算耗尽（$%.6f > $%.6f）" % (self.usd, self.max_usd)
        return None

    def snapshot(self) -> str:
        return "tokens=%d(%.0f%%) $%.6f %.2fs" % (
            self.tokens,
            100.0 * self.tokens / self.max_tokens if self.max_tokens else 0.0,
            self.usd, self.seconds)


# --------------------------------------------------------------------------
# 3. 无进展检测：三个信号
# --------------------------------------------------------------------------


def normalize_observation(text: str) -> str:
    """规范化后再比较：压空白、忽略大小写。

    注意不要把数字抹掉——带时间戳的观察每次都不同，那属于"观察在变"，
    要靠状态指纹判断有没有卡住。
    """
    return re.sub(r"\s+", " ", text.strip().lower())


class ProgressDetector:
    def __init__(self, repeat_limit: int = 3, observation_limit: int = 3,
                 state_limit: int = 3, window: int = 8) -> None:
        self.repeat_limit = repeat_limit
        self.observation_limit = observation_limit
        self.state_limit = state_limit
        self.action_counts: Counter = Counter()
        self.obs_window: deque = deque(maxlen=window)
        self.state_window: deque = deque(maxlen=window)

    def check(self, step: Step) -> Optional[str]:
        # 信号一：动作重复
        key = (step.action.name,
               json.dumps(step.action.args, sort_keys=True, ensure_ascii=False))
        self.action_counts[key] += 1
        if self.action_counts[key] >= self.repeat_limit:
            return "动作重复：%s 出现 %d 次" % (step.action.name, self.action_counts[key])

        # 信号二：观察不变
        self.obs_window.append(normalize_observation(step.observation))
        if len(self.obs_window) >= self.observation_limit and \
                len(set(self.obs_window)) == 1:
            return "观察不变：最近 %d 次观察内容相同" % len(self.obs_window)

        # 信号三：状态无变化
        self.state_window.append(step.state_hash)
        if len(self.state_window) >= self.state_limit and \
                len(set(self.state_window)) == 1:
            return "状态无变化：最近 %d 次环境状态指纹相同" % len(self.state_window)
        return None


# --------------------------------------------------------------------------
# 4. 停止守卫：四类停止条件与优先级
# --------------------------------------------------------------------------


class StopGuard:
    def __init__(self, budget: Budget, progress: ProgressDetector, max_steps: int,
                 completion_check: Callable[[Step], bool]) -> None:
        self.budget = budget
        self.progress = progress
        self.max_steps = max_steps
        self.completion_check = completion_check

    def check(self, step: Step, steps_done: int) -> Optional[StopDecision]:
        # 1) 显式完成：模型说完成只是候选，还要过完成条件校验
        if step.action.name == "finish":
            if self.completion_check(step):
                return StopDecision("completed", step.observation)
        # 2) 异常终止
        if step.error:
            return StopDecision("error", step.error)
        # 3) 预算耗尽
        exhausted = self.budget.check()
        if exhausted:
            return StopDecision("budget_exhausted", exhausted)
        # 4) 无进展
        stalled = self.progress.check(step)
        if stalled:
            return StopDecision("no_progress", stalled)
        # 5) 步数上限
        if steps_done >= self.max_steps:
            return StopDecision("max_steps", "达到步数上限 %d" % self.max_steps)
        return None


# --------------------------------------------------------------------------
# 5. 环境（本地确定性模拟）
# --------------------------------------------------------------------------


class MockEnv:
    """模拟环境：返回观察与状态指纹。

    state_hash 相同意味着"世界没有变化"；它由环境给出，Agent 只做比较。
    """

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.counter = 0
        self.artifact: Optional[str] = None

    def execute(self, action: Action) -> Tuple[str, str]:
        if self.scenario == "error" and action.name != "finish":
            raise RuntimeError("下游服务不可用：连接被重置")
        if action.name == "finish":
            return ("任务结束", self._hash())
        if self.scenario == "repeat":
            obs = "命中 2 条相关结果：条款 A、条款 B"
            self.counter += 1
            return (obs, self._hash())
        if self.scenario == "stale":
            # 动作在变、状态在变，但观察内容始终一样
            obs = "无新增内容"
            self.counter += 1
            return (obs, self._hash())
        if self.scenario == "frozen":
            # 动作不同、观察不同（时间在变），但世界状态纹丝不动
            self.counter += 1
            obs = "读取成功，当前时间 %d" % (int(time.time()) + self.counter)
            return (obs, self._hash())          # 状态指纹恒定
        # normal / budget
        if action.name == "read_file":
            self.counter += 1
            return ("已读取 data.csv，共 3 行记录", self._hash())
        if action.name == "list_dir":
            self.counter += 1
            return ("目录含 2 个文件", self._hash())
        if action.name == "search":
            self.counter += 1
            return ("命中 2 条：违约责任条款、解除条件", self._hash())
        if action.name == "write_report":
            self.artifact = action.args.get("path", "report.md")
            self.counter += 1
            return ("已写入 %s" % self.artifact, self._hash())
        self.counter += 1
        return ("ok", self._hash())

    def _hash(self) -> str:
        if self.scenario == "frozen":
            return hashlib.sha1(b"frozen").hexdigest()[:8]
        return hashlib.sha1(str(self.counter).encode()).hexdigest()[:8]


# --------------------------------------------------------------------------
# 6. 模型接入
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一个执行任务的 Agent。每轮只输出一个 JSON：
{"action": "<动作名>", "args": {...}}
可用动作：read_file(path)、list_dir(path)、search(query)、write_report(path, content)、finish(answer)。
任务真正做完才输出 finish。"""

NORMAL_PLAN = [
    ("read_file", {"path": "data.csv"}),
    ("list_dir", {"path": "."}),
    ("search", {"query": "违约责任"}),
    ("write_report", {"path": "report.md", "content": "违约责任条款汇总"}),
    ("finish", {"answer": "已汇总 2 条违约责任条款并写入 report.md"}),
]

STALE_PLAN = [
    ("read_file", {"path": "a.txt"}),
    ("list_dir", {"path": "b"}),
    ("git_diff", {"repo": "c"}),
    ("run_tests", {"repo": "d"}),
]

FROZEN_PLAN = [
    ("read_file", {"path": "a.txt"}),
    ("list_dir", {"path": "b"}),
    ("search", {"query": "q"}),
    ("git_diff", {"repo": "c"}),
]


class MockLLM:
    """按场景脚本返回动作，不做真实推理。"""

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.index = 0

    def __call__(self, prompt: str) -> str:
        if self.scenario == "repeat":
            action = Action("search", {"query": "违约责任"})
        elif self.scenario == "stale":
            action = Action(*STALE_PLAN[self.index % len(STALE_PLAN)])
        elif self.scenario == "frozen":
            action = Action(*FROZEN_PLAN[self.index % len(FROZEN_PLAN)])
        elif self.scenario == "error":
            action = Action("read_file", {"path": "data.csv"})
        else:                                   # normal / budget
            action = Action(*NORMAL_PLAN[min(self.index, len(NORMAL_PLAN) - 1)])
        self.index += 1
        return json.dumps({"action": action.name, "args": action.args}, ensure_ascii=False)


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


def parse_action(text: str) -> Action:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return Action("finish", {"answer": text.strip()[:200]})
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return Action("finish", {"answer": text.strip()[:200]})
    return Action(raw.get("action", "finish"), raw.get("args", {}))


# --------------------------------------------------------------------------
# 7. Agent 主循环
# --------------------------------------------------------------------------


class Agent:
    def __init__(self, llm: Callable[[str], str], env: MockEnv, guard: StopGuard,
                 max_steps: int = 8, trace: bool = False) -> None:
        self.llm = llm
        self.env = env
        self.guard = guard
        self.max_steps = max_steps
        self.trace = trace

    def _completion_check(self, step: Step) -> bool:
        """模型说"完成了"只是候选：产物必须真的存在。"""
        return self.env.artifact is not None

    def run(self, task: str) -> Result:
        result = Result()
        history: List[str] = []
        for _ in range(self.max_steps):
            result.rounds += 1
            prompt = task + ("\n\n历史：\n" + "\n".join(history) if history else "")
            t0 = time.perf_counter()
            raw = self.llm(prompt)
            out_tokens = max(1, len(raw) // 4)
            in_tokens = max(1, len(prompt) // 4)
            action = parse_action(raw)

            step = Step(action=action, in_tokens=in_tokens, out_tokens=out_tokens)
            try:
                obs, state_hash = self.env.execute(action)
                step.observation = obs
                step.state_hash = state_hash
            except Exception as exc:
                step.error = "%s: %s" % (type(exc).__name__, exc)
                step.elapsed = (time.perf_counter() - t0) * 1000.0 / 1000.0
                self.guard.budget.charge(in_tokens, out_tokens, step.elapsed)
                result.steps.append(step)
                result.tokens = self.guard.budget.tokens
                result.usd = self.guard.budget.usd
                decision = self.guard.check(step, result.rounds)
                result.stop_reason = decision.reason if decision else "max_steps"
                result.detail = decision.detail if decision else "达到步数上限"
                result.status = "failed"
                result.partial = self._partial(result, result.stop_reason, result.detail,
                                               status="failed")
                return result

            step.elapsed = (time.perf_counter() - t0) * 1000.0 / 1000.0
            self.guard.budget.charge(in_tokens, out_tokens, step.elapsed)
            result.steps.append(step)
            result.tokens = self.guard.budget.tokens
            result.usd = self.guard.budget.usd
            history.append("%s -> %s" % (action.name, step.observation))

            if self.trace:
                print("  [step %d] %s %s | obs=%s | %s"
                      % (result.rounds, action.name, action.args, step.observation[:40],
                         self.guard.budget.snapshot()))

            decision = self.guard.check(step, result.rounds)
            if decision is not None:
                result.stop_reason = decision.reason
                result.detail = decision.detail
                if decision.reason == "completed":
                    result.status = "completed"
                    result.answer = str(action.args.get("answer", step.observation))
                else:
                    result.status = "partial"
                    result.partial = self._partial(result, decision.reason, decision.detail)
                return result

        result.stop_reason = "max_steps"
        result.detail = "达到步数上限 %d" % self.max_steps
        result.status = "partial"
        result.partial = self._partial(result, result.stop_reason, result.detail)
        return result

    def _partial(self, result: Result, reason: str, detail: str,
                 status: str = "partial") -> Dict[str, Any]:
        """降级返回：做完多少 + 卡在哪 + 下一步建议。"""
        done = [s.action.name for s in result.steps if not s.error]
        facts = [s.observation for s in result.steps if s.observation and not s.error]
        return {
            "status": status,
            "stop_reason": reason,
            "detail": detail,
            "completed_actions": done,
            "facts_collected": facts[-3:],
            "artifact": self.env.artifact,
            "next_step": "由上游或人工接手剩余步骤；已完成的产物可直接使用。",
        }


# --------------------------------------------------------------------------
# 8. CLI
# --------------------------------------------------------------------------

SCENARIOS = ["normal", "repeat", "stale", "frozen", "budget", "error"]


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def build_llm(args: argparse.Namespace, scenario: str) -> Callable[[str], str]:
    if not args.live:
        return MockLLM(scenario)
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "--live 需要环境变量 LLM_API_KEY；"
            "可选 LLM_BASE_URL（默认 https://api.openai.com/v1）与 LLM_MODEL。\n")
        raise SystemExit(2)
    base_url = args.base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model = args.model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    return LiveLLM(base_url, api_key, model, temperature=args.temperature)


def run_scenario(scenario: str, args: argparse.Namespace) -> Result:
    budget = Budget(
        max_tokens=args.max_tokens or None,
        max_seconds=args.max_seconds or None,
        max_usd=args.max_usd or None,
        price_in_per_1k=args.price_in,
        price_out_per_1k=args.price_out,
    )
    if scenario == "budget" and not args.max_tokens:
        budget.max_tokens = 100              # 预算场景给一个很紧的上限
    progress = ProgressDetector(repeat_limit=args.repeat_limit,
                                 observation_limit=args.repeat_limit,
                                 state_limit=args.repeat_limit)
    env = MockEnv(scenario)
    guard = StopGuard(budget, progress, args.max_steps,
                       completion_check=lambda step: env.artifact is not None)
    agent = Agent(build_llm(args, scenario), env, guard,
                   max_steps=args.max_steps, trace=args.trace)
    return agent.run(TASK)


TASK = "汇总 data.csv 里的违约责任条款，写成 report.md，然后结束。"


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="停止准则与成本守卫最小可实现（mock 模式无需 API Key）")
    parser.add_argument("--scenario", default="all", choices=SCENARIOS + ["all"])
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=0, help="token 预算，0 表示不限")
    parser.add_argument("--max-seconds", type=float, default=0.0, help="时间预算（秒）")
    parser.add_argument("--max-usd", type=float, default=0.0, help="美元预算")
    parser.add_argument("--price-in", type=float, default=0.001,
                        help="示例单价：每千输入 token 的美元价（走配置，不写死在逻辑里）")
    parser.add_argument("--price-out", type=float, default=0.002,
                        help="示例单价：每千输出 token 的美元价")
    parser.add_argument("--repeat-limit", type=int, default=3,
                        help="连续几次同样信号算作无进展")
    parser.add_argument("--live", action="store_true", help="切真实模型 API")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--model", default="", help="覆盖 LLM_MODEL")
    parser.add_argument("--base-url", default="", help="覆盖 LLM_BASE_URL")
    parser.add_argument("--trace", action="store_true", help="打印每轮动作与预算余额")
    args = parser.parse_args(argv)

    scenarios = SCENARIOS if args.scenario == "all" else [args.scenario]
    results: List[Tuple[str, Result]] = []
    for scenario in scenarios:
        if args.trace:
            print("\n=== scenario: %s ===" % scenario)
        results.append((scenario, run_scenario(scenario, args)))

    print("=" * 72)
    print("%-10s %-18s %6s %8s %10s %8s" %
          ("scenario", "stop_reason", "rounds", "tokens", "usd", "status"))
    for scenario, res in results:
        print("%-10s %-18s %6d %8d %10.6f %8s"
              % (scenario, res.stop_reason, res.rounds, res.tokens, res.usd, res.status))
    print("=" * 72)
    for scenario, res in results:
        if res.partial:
            print("\n[%s] 部分结果（降级返回）:" % scenario)
            print(json.dumps(res.partial, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
