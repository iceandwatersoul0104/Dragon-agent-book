"""Agent 主循环：ReAct 回路 + 停止准则 + 预算守卫。

控制流只有一条：

    装配上下文 → 问模型 → 执行工具 → 回灌观察 → 记状态 → 再问

不确定性被关在模型层（``llm.py``），循环本身是确定性的控制流。

四组机制：

1. **停止准则**：完成 → 异常 → 预算 → 无进展 → 步数上限，**优先级固定**。
   同时满足多个条件时，排在前面的那个胜出——比如最后一步刚好给出答案、
   也刚好撞上步数上限，算"完成"。
2. **无进展检测**：三个信号——动作重复、观察不变、状态无变化。
3. **预算守卫**：token、时间、美元三种预算共用同一个接口，
   主循环只会问"还能花吗"和"为什么停了"。
4. **优雅降级**：工具失败不中断循环，错误回灌给模型让它自己改。

**run() 永不向调用方抛异常**：任何意外都变成 ``AgentResult.status="error"``，
调用方永远拿到一个结果对象。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMResponse, ToolCall

__all__ = [
    "AgentStep",
    "AgentResult",
    "Agent",
    "BudgetGuard",
    "TokenBudgetGuard",
    "CostBudgetGuard",
    "TimeBudgetGuard",
    "CompositeBudgetGuard",
    "build_budget_guard",
]

#: 百万 token——单价表的计价单位。
PER_MTOK = 1_000_000


# --------------------------------------------------------------------------- 预算守卫


class BudgetGuard:
    """预算守卫的统一接口。

    三种预算（token、时间、美元）共用这一套接口：主循环只会问
    "还能花吗"和"为什么停了"，不关心背后是哪一种预算。加一种新预算，
    主循环一个字都不用改。
    """

    kind = "budget"

    def __init__(self) -> None:
        self.spent = 0.0

    def charge(self, tokens: int, cost: float, elapsed_s: float) -> None:
        """记一次消耗。"""

    def exhausted(self) -> bool:
        """预算用完了吗？"""
        return False

    def reason(self) -> str:
        """用完了就说清楚：哪一种预算、用了多少、上限多少。"""
        return ""


class TokenBudgetGuard(BudgetGuard):
    """token 预算。"""

    kind = "token"

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = int(limit)
        self.spent = 0

    def charge(self, tokens: int, cost: float, elapsed_s: float) -> None:
        self.spent += int(tokens or 0)

    def exhausted(self) -> bool:
        return self.spent >= self.limit

    def reason(self) -> str:
        return f"token 预算耗尽：已用 {self.spent}，上限 {self.limit}"


class CostBudgetGuard(BudgetGuard):
    """美元预算。"""

    kind = "cost"

    def __init__(self, limit_usd: float) -> None:
        super().__init__()
        self.limit = float(limit_usd)

    def charge(self, tokens: int, cost: float, elapsed_s: float) -> None:
        self.spent += float(cost or 0.0)

    def exhausted(self) -> bool:
        return self.spent >= self.limit

    def reason(self) -> str:
        return f"成本预算耗尽：已用 {self.spent:.6f} 美元，上限 {self.limit:.6f} 美元"


class TimeBudgetGuard(BudgetGuard):
    """时间预算（墙钟秒）。"""

    kind = "time"

    def __init__(self, limit_s: float) -> None:
        super().__init__()
        self.limit = float(limit_s)

    def charge(self, tokens: int, cost: float, elapsed_s: float) -> None:
        self.spent = float(elapsed_s or 0.0)

    def exhausted(self) -> bool:
        return self.spent >= self.limit

    def reason(self) -> str:
        return f"时间预算耗尽：已用 {self.spent:.1f} 秒，上限 {self.limit:.1f} 秒"


class CompositeBudgetGuard(BudgetGuard):
    """把多个预算合成一个。任何一个到顶就停，停的时候报最具体的那一个。"""

    kind = "composite"

    def __init__(self, guards: list[BudgetGuard] | None = None) -> None:
        super().__init__()
        self.guards: list[BudgetGuard] = list(guards or [])

    def charge(self, tokens: int, cost: float, elapsed_s: float) -> None:
        for guard in self.guards:
            guard.charge(tokens, cost, elapsed_s)

    def exhausted(self) -> bool:
        return any(guard.exhausted() for guard in self.guards)

    def reason(self) -> str:
        for guard in self.guards:
            if guard.exhausted():
                return guard.reason()
        return ""


def build_budget_guard(config: Any) -> CompositeBudgetGuard:
    """按配置装配预算守卫。配置里没写的预算就不装。"""
    guards: list[BudgetGuard] = []
    token_budget = getattr(config, "token_budget", None)
    if token_budget:
        guards.append(TokenBudgetGuard(token_budget))
    cost_budget = getattr(config, "cost_budget", None)
    if cost_budget:
        guards.append(CostBudgetGuard(cost_budget))
    timeout_s = getattr(config, "timeout_s", None)
    if timeout_s:
        guards.append(TimeBudgetGuard(timeout_s))
    return CompositeBudgetGuard(guards)


# --------------------------------------------------------------------------- 结果


@dataclass
class AgentStep:
    """一步：模型想了什么、做了什么、看到了什么、花了多少。"""

    index: int
    thought: str = ""
    action: ToolCall | None = None
    observation: str = ""
    tokens: int = 0
    cost: float = 0.0


@dataclass
class AgentResult:
    """一次任务的结果。

    ``status`` 取值固定为：``completed`` / ``stopped_budget`` /
    ``stopped_no_progress`` / ``max_steps`` / ``error``。
    """

    answer: str = ""
    status: str = "error"
    steps: list[AgentStep] = field(default_factory=list)
    total_tokens: int = 0
    total_cost: float = 0.0
    stop_reason: str = ""


# --------------------------------------------------------------------------- 主循环


class Agent:
    """ReAct 主循环。

    依赖全是注入进来的：配置、模型、工具、上下文、状态、护栏。
    护栏用鸭子类型调用（``before_tool`` 返回带 ``allow`` / ``reason`` 的对象），
    所以本模块不 import ``guardrails``，那一层可以晚点再写。
    """

    #: 连续多少步出现同一个信号，就判定为"无进展"。
    no_progress_threshold = 3

    def __init__(
        self,
        config: Any,
        llm: Any,
        tools: Any,
        context: Any,
        state: Any,
        guards: Any = None,
        tracer: Any = None,
        meter: Any = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.tools = tools
        self.context = context
        self.state = state
        self.guards = guards
        self.tracer = tracer
        self.meter = meter
        self.budget = build_budget_guard(config)
        self._clock = time.monotonic
        self._progress = 0  # 成功拿到结果的次数——"世界有没有变化"的度量

    # ------------------------------------------------------------------ 主流程

    def run(self, task: str) -> AgentResult:
        """跑一次任务。**永不抛异常**，失败转成 ``status="error"``。"""
        started = self._clock()
        steps: list[AgentStep] = []
        history: list[dict] = []
        total_tokens = 0
        total_cost = 0.0
        answer = ""
        status = "error"
        stop_reason = ""
        try:
            self.state.update(status="running")
            max_steps = max(1, int(getattr(self.config, "max_steps", 12) or 12))

            while True:
                # ---- 停止准则：预算 → 无进展 → 步数上限 ----
                if self.budget.exhausted():
                    status, stop_reason = "stopped_budget", self.budget.reason()
                    break
                reason = self._no_progress_reason()
                if reason:
                    status = "stopped_no_progress"
                    stop_reason = f"检测到无进展：{reason}"
                    break
                if len(steps) >= max_steps:
                    status = "max_steps"
                    stop_reason = f"达到步数上限 {max_steps}"
                    break

                # ---- 装配上下文（第 5 章的活） ----
                schemas = self.tools.schemas()
                assembled = self.context.assemble(
                    task=task, history=history, retrieval=[], tools=schemas
                )
                messages = [{"role": "system", "content": assembled.render()}]

                # ---- 问模型（第 3 章的活） ----
                resp: LLMResponse = self.llm.complete(messages, tools=schemas)
                tokens = resp.usage.total
                cost = self._cost_of(resp)
                total_tokens += tokens
                total_cost += cost
                self.budget.charge(tokens, cost, self._clock() - started)

                if resp.finish_reason == "error":
                    status = "error"
                    stop_reason = f"模型调用失败：{resp.text}"
                    break

                # ---- 完成：给出最终答案 ----
                if not resp.has_tool_calls:
                    blocked = self._blocked_reason(resp.text)
                    if blocked:
                        # 被拦下的内容不能进答案，也不能进状态——两步都别留。
                        step = AgentStep(
                            index=len(steps) + 1,
                            thought="[输出被护栏拦截]",
                            action=None,
                            observation="",
                            tokens=tokens,
                            cost=cost,
                        )
                        steps.append(step)
                        self._finish_step(step, history)
                        answer = "[输出被护栏拦截]"
                        status = "error"
                        stop_reason = f"输出被护栏拦截：{blocked}"
                        break
                    step = AgentStep(
                        index=len(steps) + 1,
                        thought=resp.text,
                        action=None,
                        observation="",
                        tokens=tokens,
                        cost=cost,
                    )
                    steps.append(step)
                    self._finish_step(step, history)
                    answer = resp.text
                    status = "completed"
                    stop_reason = "模型给出了最终答案"
                    break

                # ---- 行动：执行工具（第 4 章的活） ----
                observations: list[str] = []
                ok_any = False
                for call in resp.tool_calls:
                    observation, ok = self._execute(call, len(steps) + 1)
                    observations.append(f"{call.name}: {observation}")
                    ok_any = ok_any or ok
                step = AgentStep(
                    index=len(steps) + 1,
                    thought=resp.text,
                    action=resp.tool_calls[0],
                    observation="\n".join(observations),
                    tokens=tokens,
                    cost=cost,
                )
                steps.append(step)
                self._finish_step(step, history)
        except Exception as exc:  # 任何意外都变成结果，不冒泡
            status = "error"
            stop_reason = f"任务异常终止：{type(exc).__name__}: {exc}"

        self.state.update(status=status)
        return AgentResult(
            answer=answer,
            status=status,
            steps=steps,
            total_tokens=total_tokens,
            total_cost=total_cost,
            stop_reason=stop_reason,
        )

    # ------------------------------------------------------------------ 一步结束

    def _finish_step(self, step: AgentStep, history: list[dict]) -> None:
        """一步结束：记历史、记状态、写检查点。

        检查点**每步一次**——这是崩溃后能接着跑的依据。
        """
        if step.thought:
            history.append({"role": "assistant", "content": step.thought})
        if step.observation:
            name = step.action.name if step.action else "工具"
            history.append({
                "role": "user",
                "content": f"工具 {name} 返回：{step.observation}",
            })
        self.state.record_step({
            "index": step.index,
            "thought": step.thought,
            "action": step.action.name if step.action else None,
            "arguments": dict(step.action.arguments) if step.action else {},
            "observation": step.observation,
            "tokens": step.tokens,
            "cost": step.cost,
            "progress": self._progress,
        })
        self.state.checkpoint()

    # ------------------------------------------------------------------ 工具执行

    def _execute(self, call: ToolCall, index: int) -> tuple[str, bool]:
        """执行一次工具调用，返回 ``(给模型看的文本, 是否成功)``。

        三道关：护栏 → 参数校验 → 执行。任何一道没过，**都不抛异常**，
        错误文本回灌给模型，让它自己改。
        """
        if self.guards is not None:
            try:
                decision = self.guards.before_tool(call.name, call.arguments)
            except Exception as exc:
                return f"[护栏异常] {type(exc).__name__}: {exc}", False
            if not getattr(decision, "allow", True):
                reason = getattr(decision, "reason", "") or "护栏拒绝了这次调用"
                return f"[护栏拒绝] {reason}", False

        key = self._idempotency_key(call, index) if self._has_side_effect(call.name) else None
        result = self.tools.invoke(call.name, call.arguments, idempotency_key=key)
        ok = bool(getattr(result, "ok", False))
        if ok:
            self._progress += 1
        return result.render(), ok

    def _has_side_effect(self, name: str) -> bool:
        """这个工具有副作用吗？——决定要不要给幂等键。"""
        try:
            spec, _fn = self.tools.get(name)
        except Exception:
            return False
        return bool(getattr(spec, "side_effect", False))

    def _blocked_reason(self, text: str) -> str:
        """输出护栏：返回拦截原因；没被拦就返回空串。

        护栏自己抛异常时**不拦截**——把一次本来成功的任务变成失败，比放过
        一段文本更糟。但异常要留下痕迹：记进状态层的事件流，别让它悄悄过去。

        这和工具前的护栏正好相反：工具前的护栏坏了就**不执行**（有副作用，
        关不住就别动）；输出护栏坏了就**放行**（没有副作用，拦下来只会
        白白毁掉一次成功）。
        """
        if self.guards is None:
            return ""
        try:
            decision = self.guards.after_output(text)
        except Exception as exc:
            if self.state is not None:
                try:
                    self.state.record({
                        "type": "guard_error",
                        "stage": "after_output",
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                except Exception:
                    pass
            return ""
        if getattr(decision, "allow", True):
            return ""
        return getattr(decision, "reason", "") or "输出被护栏拦截"

    def _idempotency_key(self, call: ToolCall, index: int) -> str:
        """副作用工具的幂等键：任务 + 步骤 + 工具名 + 参数摘要。

        同一个任务从同一个检查点重跑，会拿到同一个键——不可逆的工具因此
        不会被执行第二次。
        """
        args = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(args.encode("utf-8")).hexdigest()[:12]
        task_id = ""
        if self.state is not None:
            try:
                task_id = self.state.snapshot().task_id
            except Exception:
                task_id = ""
        return f"{task_id}:{index}:{call.name}:{digest}"

    # ------------------------------------------------------------------ 无进展检测

    def _no_progress_reason(self) -> str | None:
        """三个信号：动作重复、观察不变、状态无变化。

        任意一个信号连续出现 ``no_progress_threshold`` 次就判定无进展。
        """
        n = self.no_progress_threshold
        if self.state is None:
            return None
        try:
            recorded = self.state.snapshot().steps
        except Exception:
            return None
        if len(recorded) < n:
            return None
        recent = recorded[-n:]

        # 信号一：动作重复（名字和参数都相同）
        signatures = [
            (step.get("action"), json.dumps(step.get("arguments") or {}, sort_keys=True,
                                          ensure_ascii=False, default=str))
            for step in recent
        ]
        if len(set(signatures)) == 1 and signatures[0][0]:
            return "连续执行同一个动作"

        # 信号二：观察不变
        observations = [step.get("observation") or "" for step in recent]
        if len(set(observations)) == 1 and observations[0]:
            return "连续拿到相同的观察"

        # 信号三：状态无变化（成功的观察没有新增）
        markers = [step.get("progress") for step in recent]
        if len(set(markers)) == 1 and markers[0] is not None:
            return "状态没有变化"

        return None

    # ------------------------------------------------------------------ 成本

    def _cost_of(self, resp: LLMResponse) -> float:
        """这一步花了多少钱。有成本计量器就用它，否则按配置的单价表算。"""
        if self.meter is not None and hasattr(self.meter, "charge"):
            try:
                return float(self.meter.charge(resp.usage, getattr(self.config, "model", "")))
            except Exception:
                pass
        usage = resp.usage
        price_in = float(getattr(self.config, "price_in_per_mtok", 0.0) or 0.0)
        price_out = float(getattr(self.config, "price_out_per_mtok", 0.0) or 0.0)
        return (usage.input_tokens * price_in + usage.output_tokens * price_out) / PER_MTOK
