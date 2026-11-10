#!/usr/bin/env python3
"""编排层实跑：该不该升级、选哪种拓扑、Supervisor 怎么跑、什么时候熔断。

运行方式（工程根目录下）::

    python examples/orchestration_demo.py

全程离线：MockLLM 剧本 + 假 worker，不联网、不需要 API Key、不写用户目录。

看八件事：

1. 四个升级判据：缺一个就别拆；
2. 五种拓扑各自的实现代价；
3. 默认编排器是单 Agent（对照组）；
4. Supervisor 三段式：派单 → 回收 → 判定完成；
5. 依赖顺序与并行；
6. 共享状态三种做法的冲突处理差别；
7. 熔断四件套：最大轮次、无进展、强制收敛、单 Agent 回退；
8. 编排的账：轮数、token、成本、停因。
"""
from __future__ import annotations

import json
import pathlib
import sys

# 允许不安装、直接在工程根目录跑这个脚本。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.agent import Agent, AgentResult
from dragon_agent.config import Config
from dragon_agent.context import ContextAssembler
from dragon_agent.llm import MockLLM
from dragon_agent.orchestration import (
    TOPOLOGIES,
    MessagePassingState,
    SharedDocumentState,
    SharedMemoryState,
    SingleAgent,
    SubTask,
    Supervisor,
    UpgradeCriteria,
    build_orchestrator,
    recommend_topology,
)
from dragon_agent.state import StateStore
from dragon_agent.tools import default_registry


def line(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def make_agent(*script) -> Agent:
    """造一个按剧本说话的 Agent。"""
    config = Config.from_env_and_args([])
    return Agent(
        config=config,
        llm=MockLLM(script=list(script)),
        tools=default_registry(),
        context=ContextAssembler(config=config),
        state=StateStore(config=config),
    )


class FakeWorker:
    """只会说一句固定答案的假 worker。"""

    def __init__(self, answer: str, tokens: int = 0, cost: float = 0.0) -> None:
        self.answer = answer
        self.tokens = tokens
        self.cost = cost
        self.prompts: list[str] = []

    def run(self, task: str) -> AgentResult:
        self.prompts.append(task)
        return AgentResult(
            answer=self.answer,
            status="completed",
            total_tokens=self.tokens,
            total_cost=self.cost,
            stop_reason="假 worker",
        )


PLAN = json.dumps([
    {"name": "查资料", "prompt": "去查一下 Orion 项目的预算是多少", "worker": "research"},
    {"name": "算账", "prompt": "根据预算算一下还能花多少", "worker": "calc",
     "depends_on": ["查资料"]},
], ensure_ascii=False)


def build_supervisor(planner_agent, workers, solo):
    """装一个 Supervisor：编排者 + worker + 共享状态 + 回退路径。

    回退路径用的是**同一个单 Agent**，不是另造一套——
    "退回"本身不该是又一次新的风险。
    """
    return Supervisor(
        planner_agent,
        workers,
        state=SharedDocumentState(),
        fallback=SingleAgent(solo),
        max_rounds=2,
    )


def main() -> int:
    ok = True

    # ------------------------------------------------------------- 1
    line("1) 升级判据：缺一个都别上多 Agent")
    partial = UpgradeCriteria(parallel=True, context_isolation=True)
    print(f"  只满足两条：{partial.reason()}")
    print(f"  缺的是      ：{partial.missing()}")
    full = UpgradeCriteria(True, True, True, True)
    print(f"  四条全中    ：{full.reason()}")
    if partial.should_upgrade() or not full.should_upgrade():
        print("  判据判定不对——这是 bug")
        ok = False

    # ------------------------------------------------------------- 2
    line("2) 五种拓扑，各自的实现代价")
    for name, spec in TOPOLOGIES.items():
        print(f"  [{name}] {spec.label}")
        print(f"      共享状态: {spec.shared_state}")
        print(f"      派单代价: {spec.dispatch_cost}")
        print(f"      收敛代价: {spec.converge_cost}")
        print(f"      适合    : {spec.fits}")
        print(f"      什么时候崩: {spec.breaks}")
    print()
    print(f"  四条全中 → 推荐拓扑：{recommend_topology(full)}")
    print(f"  只满足两条 → 推荐拓扑：{recommend_topology(partial)}")
    if recommend_topology(partial) != "single":
        print("  判据不满足时不该推荐多 Agent 拓扑——这是 bug")
        ok = False

    # ------------------------------------------------------------- 3
    line("3) 默认编排器是单 Agent（对照组）")
    default = build_orchestrator(Config.from_env_and_args([]))
    print(f"  build_orchestrator() 返回：{type(default).__name__}")
    if not isinstance(default, SingleAgent):
        print("  默认必须是 SingleAgent——这是 bug")
        ok = False
    solo = SingleAgent(make_agent(MockLLM.say("单 Agent 的答案", in_tokens=12, out_tokens=4)))
    solo_result = solo.run("查一下 Orion 的预算")
    print(f"  单 Agent 跑同一个任务：{solo_result.answer}")
    print(f"  花了 {solo_result.total_tokens} token")

    # ------------------------------------------------------------- 4
    line("4) Supervisor 三段式：派单 → 回收 → 判定完成")
    planner_agent = make_agent(
        MockLLM.say(PLAN, in_tokens=18, out_tokens=12),
        MockLLM.say("Orion 预算 100 万，还能花 60 万。", in_tokens=20, out_tokens=9),
    )
    research = FakeWorker("Orion 项目预算是 100 万", tokens=11, cost=0.01)
    calc = FakeWorker("还能花 60 万", tokens=7, cost=0.02)
    supervisor = build_supervisor(
        planner_agent,
        {"research": research, "calc": calc},
        solo,
    )
    result = supervisor.run("查一下 Orion 的预算还剩多少")
    print(f"  最终答案：{result.answer}")
    print(f"  结束状态：{result.status}")
    print(f"  停    因：{result.stop_reason}")
    print("  回收到的子任务结论：")
    for outcome in supervisor.last_report.outcomes:
        print(f"    - [{outcome.name}]（{outcome.worker}，{outcome.status}）：{outcome.answer}")
    print(f"  轮数：{supervisor.last_report.rounds}  用了 {result.total_tokens} token")
    if result.status != "completed" or len(supervisor.last_report.outcomes) != 2:
        print("  三段式没跑通——这是 bug")
        ok = False

    # ------------------------------------------------------------- 5
    line("5) 依赖顺序与并行")
    print("  算账依赖查资料：查资料的结论必须先写进共享状态。")
    calc_prompt = calc.prompts[0] if calc.prompts else ""
    if "已有结论" in calc_prompt and "100 万" in calc_prompt:
        print("  算账拿到的提示词里带着查资料的结论：")
        for row in calc_prompt.splitlines():
            if row.startswith("- ") or row.startswith("你负责"):
                print(f"    {row}")
    else:
        print("  依赖没生效——这是 bug")
        ok = False

    parallel_supervisor = Supervisor(
        make_agent(
            MockLLM.say(PLAN, in_tokens=18, out_tokens=12),
            MockLLM.say("并行做完的结论", in_tokens=20, out_tokens=9),
        ),
        {"research": FakeWorker("资料"), "calc": FakeWorker("数字")},
        parallel=True,
        fallback=solo,
    )
    parallel_supervisor.run("并行跑一遍")
    order = [o.name for o in parallel_supervisor.last_report.outcomes]
    print(f"  并行执行后结果顺序仍按计划：{order}")
    if order != ["查资料", "算账"]:
        print("  并行结果的顺序不稳定——回放会受影响")
        ok = False

    # ------------------------------------------------------------- 6
    line("6) 共享状态：三种做法，冲突处理完全不同")
    memory = SharedMemoryState()
    document = SharedDocumentState()
    mailbox = MessagePassingState()
    for state, tag in ((memory, "共享内存"), (document, "共享文档"), (mailbox, "消息传递")):
        state.write("预算", "100 万", author="research")
        state.write("预算", "80 万", author="calc")
    print(f"  三个状态都读到最新值：{memory.read('预算')} / {document.read('预算')} / {mailbox.read('预算')}")
    print(f"  共享内存的冲突：{memory.conflicts()}  ← 覆盖是静默的")
    print(f"  共享文档的冲突：{document.conflicts()}  ← 留痕，冲突看得见")
    print(f"  共享文档全文：")
    for row in document.document().splitlines():
        print(f"    {row}")
    mailbox.deliver("calc", "结论", "还能花 60 万", author="research")
    print(f"  消息传递的信箱：{mailbox.inbox('calc')}")
    print(f"  取走后：{mailbox.take('calc')} → 信箱 {mailbox.inbox('calc')}")
    if memory.conflicts() != ["预算"] or document.conflicts() != ["预算"]:
        print("  冲突记录不对——这是 bug")
        ok = False

    # ------------------------------------------------------------- 7
    line("7) 熔断四件套")
    print("  (a) 最大轮次：轮数用完就强制收敛，丢弃「还要继续」")
    forced = Supervisor(
        make_agent(
            MockLLM.say(PLAN, in_tokens=18, out_tokens=12),
            MockLLM.say("继续：补充|再补一条数据\n中间结论", in_tokens=20, out_tokens=9),
        ),
        {"research": FakeWorker("资料"), "calc": FakeWorker("数字")},
        max_rounds=1,
        fallback=solo,
    )
    forced_result = forced.run("任务")
    print(f"      停因：{forced_result.stop_reason}")
    print(f"      轮数：{forced.last_report.rounds}")
    if "强制收敛" not in forced_result.stop_reason:
        print("      没有触发强制收敛——这是 bug")
        ok = False

    print()
    print("  (b) 无进展检测：连续两轮结果完全相同就停")
    stalled = Supervisor(
        make_agent(
            MockLLM.say(PLAN, in_tokens=18, out_tokens=12),
            MockLLM.say("继续：补充|再补一条\n中间", in_tokens=20, out_tokens=9),
            MockLLM.say("继续：补充|再补一条\n中间", in_tokens=20, out_tokens=9),
            MockLLM.say("继续：补充|再补一条\n中间", in_tokens=20, out_tokens=9),
        ),
        {"research": FakeWorker("资料"), "calc": FakeWorker("数字")},
        max_rounds=5,
        fallback=solo,
    )
    stalled_result = stalled.run("任务")
    print(f"      停因：{stalled_result.stop_reason}")
    if "无进展" not in stalled_result.stop_reason:
        print("      没有触发无进展检测——这是 bug")
        ok = False

    print()
    print("  (c) 单 Agent 回退：规划失败就退回单 Agent")
    broken = Supervisor(
        make_agent(MockLLM.say("我觉得应该先查资料再算账")),
        {"research": FakeWorker("资料")},
        fallback=solo,
    )
    broken_result = broken.run("任务")
    print(f"      停因：{broken_result.stop_reason}")
    print(f"      答案：{broken_result.answer}")
    print(f"      是否回退：{broken.last_report.fell_back}")
    if not broken.last_report.fell_back or "单 Agent 的答案" not in broken_result.answer:
        print("      回退没生效——这是 bug")
        ok = False

    print()
    print("  (d) 编排者被反复调用时，步骤历史不能共享")
    reused = make_agent(
        MockLLM.say(PLAN, in_tokens=18, out_tokens=12),
        MockLLM.say("继续：补充|再补一条\n中间一", in_tokens=20, out_tokens=9),
        MockLLM.say("继续：补充|再补一条\n中间二", in_tokens=20, out_tokens=9),
        MockLLM.say("最终结论：全部搞定", in_tokens=20, out_tokens=9),
    )
    reuse_supervisor = Supervisor(
        reused,
        {"research": FakeWorker("资料"), "calc": FakeWorker("数字")},
        max_rounds=5,
        fallback=solo,
    )
    reuse_result = reuse_supervisor.run("任务")
    print(f"      编排者被调用 {len(reused.llm.calls)} 次（1 次规划 + 3 次汇总）")
    print(f"      轮数：{reuse_supervisor.last_report.rounds}")
    print(f"      停因：{reuse_result.stop_reason}")
    print(f"      答案：{reuse_result.answer}")
    print("      复用有状态 Agent 会撞上主循环自己的无进展检测——所以每次调用都要重置步骤历史")
    if "无进展" in reuse_result.stop_reason or not reuse_result.answer:
        print("      编排者被自己的无进展检测干掉了——这是 bug")
        ok = False

    # ------------------------------------------------------------- 8
    line("8) 编排的账")
    report = supervisor.last_report.to_dict()
    for key in ("rounds", "fell_back", "stop_reason", "topology", "shared_state",
                "total_tokens", "total_cost"):
        print(f"  {key:<14} {report[key]}")
    print(f"  子任务数        {len(report['outcomes'])}")
    if report["rounds"] != 1 or report["total_tokens"] != 18:
        print("  账对不上——这是 bug")
        ok = False

    print()
    print("=" * 74)
    print("自检：" + ("全部通过" if ok else "有断言没过"))
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
