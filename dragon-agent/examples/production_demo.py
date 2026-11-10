#!/usr/bin/env python3
"""第 14 章上线演练：把"上线前该验的东西"逐条验一遍。

运行方式（工程根目录下）::

    python examples/production_demo.py

全程离线：不需要 API Key，不联网，不装任何第三方包。

八段内容：

1. **容器化清单自检**——直接读仓库里的 ``Dockerfile`` / ``compose.yaml`` /
   ``k8s.yaml`` / ``.dockerignore``，检查只读根、非 root、最小出网这些
   约束**真的写在了清单里**。清单写了不等于做到了，但清单没写就一定没做。
2. **配置与密钥**——配置能不能落盘、密钥会不会跟着落盘。
3. **模型侧限流**——令牌桶的突发与匀速。
4. **工具侧限流**——并发闸。
5. **队列与背压**——满了怎么办。
6. **成本护栏**——按真实单价跑一次任务，算出成本，看告警线在哪。
7. **自动熔断与降级**——主模型挂掉时会发生什么。
8. **值班手册**——信号、阈值、动作、升级路径。

最后给出自检结论，退出码 0 表示全过。
"""
from __future__ import annotations

import pathlib
import sys

# 允许不安装、直接在工程根目录跑这个脚本。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认不是 UTF-8，中文输出会炸——这里统一一下。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # pragma: no cover - 老 Python 或被替换过的 stdout
    pass

from dragon_agent.agent import Agent
from dragon_agent.cli import (
    ONCALL_ACTIONS,
    BoundedQueue,
    BudgetWatcher,
    CircuitBreaker,
    CostThresholds,
    FallbackLLMClient,
    TokenBucket,
    ToolRateLimiter,
    build_script_llm,
)
from dragon_agent.config import Config
from dragon_agent.context import ContextAssembler
from dragon_agent.llm import LLMResponse, Usage
from dragon_agent.state import StateStore
from dragon_agent.tools import ToolRegistry

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    """记一条自检结论。"""
    CHECKS.append((name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {name}" + (f" —— {detail}" if detail else ""))


def _read(relative: str) -> str:
    path = ROOT / relative
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- 1. 清单自检


def check_manifests() -> None:
    """读清单文件，检查四条硬约束有没有写进去。"""
    print("\n[1] 容器化清单自检")

    dockerfile = _read("Dockerfile")
    check("Dockerfile 存在", bool(dockerfile))
    check("构建与运行阶段分层", "AS builder" in dockerfile and "AS runtime" in dockerfile)
    check("运行期不写 .pyc（只读根的前提）",
          "PYTHONDONTWRITEBYTECODE=1" in dockerfile)
    check("非 root 用户（uid 10001）", "USER 10001:10001" in dockerfile)
    check("健康检查存在", "HEALTHCHECK" in dockerfile)

    compose = _read("deploy/compose.yaml")
    check("compose 开启只读根", "read_only: true" in compose)
    check("compose 指定非 root 用户", "10001:10001" in compose)
    check("compose 丢弃全部 capabilities", "cap_drop" in compose and "ALL" in compose)
    check("compose 禁止提权", "no-new-privileges" in compose)
    check("compose 有资源限额", "resources" in compose and "limits" in compose)

    k8s = _read("deploy/k8s.yaml")
    check("k8s 声明只读根", "readOnlyRootFilesystem: true" in k8s)
    check("k8s 声明非 root", "runAsNonRoot: true" in k8s)
    check("k8s 不挂载 ServiceAccount token",
          "automountServiceAccountToken: false" in k8s)
    check("k8s 有出网白名单（NetworkPolicy）",
          "kind: NetworkPolicy" in k8s and "egress" in k8s)
    check("k8s 出网网段仍是占位符（上线前必须替换）",
          "<模型服务网段>" in k8s,
          "占位符还在——不换掉等于这条规则没写")

    dockerignore = _read(".dockerignore")
    check(".dockerignore 排除 .env", ".env" in dockerignore)
    check(".dockerignore 排除密钥文件", "*.key" in dockerignore or "secrets/" in dockerignore)


# --------------------------------------------------------------------------- 2. 配置与密钥


def check_config() -> None:
    """配置能落盘、密钥不落盘。"""
    print("\n[2] 配置与密钥")
    config = Config.from_env_and_args([])
    payload = config.to_dict()
    check("to_dict() 不含 api_key", "api_key" not in payload)
    check("to_dict() 可 JSON 序列化", _is_jsonable(payload))
    print(f"       可落盘的配置字段：{len(payload)} 个，模型 {payload['model']}，"
          f"成本预算 {payload['cost_budget']} 美元")


def _is_jsonable(value: object) -> bool:
    import json

    try:
        json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return False
    return True


# --------------------------------------------------------------------------- 3-5. 限流与背压


def check_limits() -> None:
    """模型侧限流、工具侧限流、队列背压。"""
    print("\n[3] 模型侧限流（令牌桶）")
    bucket = TokenBucket(rate_per_s=5.0, burst=10)
    granted = sum(1 for _ in range(14) if bucket.acquire(1, timeout_s=0.0))
    check("突发额度用尽后开始拒绝", granted == 10, f"放行 {granted} 次，拒绝 {bucket.rejected} 次")
    print(f"       桶状态：{bucket.to_dict()}")

    print("\n[4] 工具侧限流（并发闸）")
    limiter = ToolRateLimiter(max_concurrency=4)
    held = []
    for name in ("read_budget", "echo", "add", "send_report", "submit_order"):
        if len(held) >= 4:
            break
        if limiter.acquire(name):
            held.append(name)
    blocked = not limiter.acquire("extra")
    check("超过并发上限的调用被拒", blocked, f"占住 {len(held)} 个")
    for name in held:
        limiter.release(name)

    print("\n[5] 队列与背压")
    queue = BoundedQueue(capacity=3, wait_s=0.05)
    accepted = sum(1 for i in range(6) if queue.put(i))
    check("队列满后开始丢弃", queue.dropped == 3, f"入队 {accepted} 个，丢弃 {queue.dropped} 个")
    print(f"       队列状态：{queue.to_dict()}")


# --------------------------------------------------------------------------- 6. 成本护栏


def check_cost_guard() -> None:
    """按真实单价跑一次任务，看成本与告警线。"""
    print("\n[6] 成本护栏")
    # 每百万 token：输入 3 美元、输出 15 美元（仅作演示用的单价，不是任何
    # 服务商的报价）。
    config = Config.from_env_and_args([])
    config = config.__class__(**{**config.to_dict(), "workspace": config.workspace,
                                 "price_in_per_mtok": 3.0, "price_out_per_mtok": 15.0})

    registry = ToolRegistry()

    @registry.tool(
        name="read_budget",
        description="读取项目预算表。",
        parameters={"type": "object",
                    "properties": {"fiscal_year": {"type": "string"}},
                    "required": ["fiscal_year"]},
    )
    def read_budget(fiscal_year: str) -> dict:
        return {"fiscal_year": fiscal_year, "total": 1_000_000}

    llm = build_script_llm([
        {"text": "先读预算表", "tool": "read_budget", "args": {"fiscal_year": "2026"}},
        {"text": "项目 2026 年预算总额为 100 万元。"},
    ])
    agent = Agent(config=config, llm=llm, tools=registry,
                  context=ContextAssembler(config=config), state=StateStore(config=config))
    result = agent.run("查一下 2026 年的项目预算")

    watcher = BudgetWatcher(cost_budget=0.50, token_budget=120_000, thresholds=CostThresholds())
    alerts = watcher.observe(result.total_cost, result.total_tokens)

    check("任务跑完并给出答案", result.status == "completed", result.status)
    check("成本账非零（单价生效了）", result.total_cost > 0.0,
          f"{result.total_cost:.6f} 美元 / {result.total_tokens} token")
    check("未超预算时不该告警", not alerts)
    print(f"       本次成本 {result.total_cost:.6f} 美元，token {result.total_tokens}")

    # 把预算调到极低，逼出告警——验证告警线真的会响。
    tight = BudgetWatcher(cost_budget=result.total_cost / 2, thresholds=CostThresholds())
    fired = tight.observe(result.total_cost, result.total_tokens)
    check("超预算时触发告警", len(fired) == 1, fired[0] if fired else "")
    again = tight.observe(result.total_cost, result.total_tokens)
    check("同一档位不重复告警", len(again) == 0)


# --------------------------------------------------------------------------- 7. 熔断与降级


def check_breaker_and_fallback() -> None:
    """熔断打开后拒绝请求；主模型限流时降级。"""
    print("\n[7] 自动熔断与降级模型")

    class _RateLimited:
        def complete(self, messages, tools=None, **kwargs):
            return LLMResponse(text="[llm] HTTP 429", finish_reason="error")

        def describe(self):
            return "primary"

    class _Working:
        def complete(self, messages, tools=None, **kwargs):
            return LLMResponse(text="备用模型的回答", usage=Usage(10, 5), finish_reason="stop")

        def describe(self):
            return "fallback"

    class _BadRequest:
        def complete(self, messages, tools=None, **kwargs):
            return LLMResponse(text="[llm] HTTP 400", finish_reason="error")

        def describe(self):
            return "primary"

    breaker = CircuitBreaker(threshold=3, cooldown_s=30.0)
    for _ in range(3):
        breaker.record_failure()
    check("连续失败到顶后熔断打开", breaker.state == "open")
    check("熔断打开后拒绝新请求", not breaker.allow())

    client = FallbackLLMClient(primary=_RateLimited(), fallback=_Working(),
                               breaker=CircuitBreaker(threshold=3))
    first = client.complete([{"role": "user", "content": "hi"}])
    client.complete([{"role": "user", "content": "hi"}])
    check("限流触发降级", client.degraded and client.switches == 1)
    check("降级后粘住，不再回主模型", "已降级" in client.describe())
    check("降级后拿到的是备用模型的答案", first.text == "备用模型的回答")

    strict = FallbackLLMClient(primary=_BadRequest(), fallback=_Working(),
                               breaker=CircuitBreaker(threshold=3))
    strict.complete([{"role": "user", "content": "hi"}])
    check("不可重试的故障不降级", not strict.degraded)


# --------------------------------------------------------------------------- 8. 值班手册


def show_runbook() -> None:
    """值班手册的信号、阈值、动作、升级路径。"""
    print("\n[8] 值班手册")
    check("每条信号都有阈值、动作与升级路径",
          all(a.threshold and a.action and a.escalate for a in ONCALL_ACTIONS))
    for index, action in enumerate(ONCALL_ACTIONS, start=1):
        print(f"  {index}. {action.signal}")
        print(f"     阈值：{action.threshold}")
        print(f"     动作：{action.action}")
        print(f"     升级：{action.escalate}")


# --------------------------------------------------------------------------- 主流程


def main() -> int:
    print("=" * 74)
    print("第 14 章上线演练：容器化清单、配置密钥、限流背压、成本护栏、值班手册")
    print("=" * 74)

    check_manifests()
    check_config()
    check_limits()
    check_cost_guard()
    check_breaker_and_fallback()
    show_runbook()

    failed = [name for name, passed, _ in CHECKS if not passed]
    print("\n" + "=" * 74)
    print(f"自检结论：{len(CHECKS) - len(failed)} / {len(CHECKS)} 项通过")
    if failed:
        print("未通过：")
        for name in failed:
            print(f"  - {name}")
    else:
        print("全部通过。")
    print("=" * 74)
    print("\n提示：接真实模型请设 LLM_API_KEY 后加 --live；缺 key 时 CLI 会以")
    print("      退出码 3 明确提示，不会静默失败。")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
