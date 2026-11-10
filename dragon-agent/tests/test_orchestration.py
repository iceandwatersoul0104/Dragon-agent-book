"""编排层���单元测试。

守六件必须成立的事：

1. **升级判据**——四个判据缺一不可，缺了要说得清缺哪一条；
2. **默认是单 Agent**——多 Agent 是显式选择，不是默认行为；
3. **Supervisor 三段式**——派单、回收、判定完成，每一段都有断言；
4. **共享状态三种做法**——共享内存 / 共享文档 / 消息传递，冲突处理各不相同；
5. **熔断四件套**——最大轮次、无进展检测、强制收敛、单 Agent 回退；
6. **确定性**——同样的输入永远同样的编排结果。

全部离线：MockLLM 剧本 + 假 worker，不联网、不调模型、不需要 API Key。
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.agent import Agent, AgentResult
from dragon_agent.context import ContextAssembler
from dragon_agent.llm import MockLLM
from dragon_agent.orchestration import (
    TOPOLOGY_BLACKBOARD,
    TOPOLOGY_SINGLE,
    TOPOLOGY_SUPERVISOR,
    UPGRADE_CRITERIA,
    MessagePassingState,
    Orchestrator,
    PlanError,
    SharedDocumentState,
    SharedMemoryState,
    SharedState,
    SingleAgent,
    SubTask,
    Supervisor,
    TopologySpec,
    UpgradeCriteria,
    build_orchestrator,
    parse_continuations,
    parse_plan,
    recommend_topology,
)
from dragon_agent.state import StateStore
from dragon_agent.tools import default_registry

try:
    from ._compat import make_config
except ImportError:  # pragma: no cover - 直接以脚本方式运行
    from _compat import make_config  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------


def make_agent(*script, registry=None):
    """造一个按剧本说话的 Agent。"""
    config = make_config()
    llm = MockLLM(script=list(script))
    return Agent(
        config=config,
        llm=llm,
        tools=registry if registry is not None else default_registry(),
        context=ContextAssembler(config=config),
        state=StateStore(config=config),
    )


class FakeAgent:
    """只会说一句固定答案的假 worker。"""

    def __init__(self, answer: str, status: str = "completed", tokens: int = 0,
                 cost: float = 0.0) -> None:
        self.answer = answer
        self.status = status
        self.tokens = tokens
        self.cost = cost
        self.prompts: list[str] = []

    def run(self, task: str) -> AgentResult:
        self.prompts.append(task)
        return AgentResult(
            answer=self.answer,
            status=self.status,
            total_tokens=self.tokens,
            total_cost=self.cost,
            stop_reason="假 worker",
        )


PLAN_JSON = json.dumps([
    {"name": "查资料", "prompt": "去查一下 Orion 的预算是多少", "worker": "research"},
    {"name": "算账", "prompt": "根据查到的预算算一下还能花多少", "worker": "calc"},
], ensure_ascii=False)


# ---------------------------------------------------------------------------
# 一、升级判据
# ---------------------------------------------------------------------------


class TestUpgradeCriteria(unittest.TestCase):
    def test_all_four_criteria_are_required(self):
        """四个判据缺一不可——少一条就不该升级。"""
        self.assertEqual(len(UPGRADE_CRITERIA), 4)
        for name in UPGRADE_CRITERIA:
            self.assertFalse(UpgradeCriteria().should_upgrade(), f"{name} 不该被默认满足")

    def test_missing_one_criterion_blocks_upgrade(self):
        for name in UPGRADE_CRITERIA:
            kwargs = {n: True for n in UPGRADE_CRITERIA}
            kwargs[name] = False
            criteria = UpgradeCriteria(**kwargs)
            self.assertFalse(criteria.should_upgrade())
            self.assertIn(name, criteria.missing())

    def test_all_met_allows_upgrade(self):
        criteria = UpgradeCriteria(True, True, True, True)
        self.assertTrue(criteria.should_upgrade())
        self.assertEqual(criteria.missing(), [])
        self.assertIn("可以升级", criteria.reason())

    def test_reason_names_the_missing_criteria(self):
        reason = UpgradeCriteria(parallel=True).reason()
        self.assertIn("不满足判据", reason)
        for name in ("context_isolation", "capability_heterogeneity", "permission_isolation"):
            self.assertIn(name, reason)

    def test_criteria_decision_is_deterministic(self):
        criteria = UpgradeCriteria(parallel=True, context_isolation=True)
        self.assertEqual(criteria.reason(), criteria.reason())
        self.assertEqual(criteria.should_upgrade(), criteria.should_upgrade())


# ---------------------------------------------------------------------------
# 二、拓扑选择
# ---------------------------------------------------------------------------


class TestTopology(unittest.TestCase):
    def test_all_topologies_record_their_cost(self):
        """选拓扑不是选"哪个更先进"，是选"哪个的代价你能付"。"""
        for name in ("single", "supervisor", "hierarchical", "mesh", "blackboard"):
            spec = TopologySpec(**{
                "name": name, "label": name, "shared_state": "s", "dispatch_cost": "d",
                "converge_cost": "c", "fits": "f", "breaks": "b",
            })
            self.assertTrue(spec.dispatch_cost.strip())
            self.assertTrue(spec.converge_cost.strip())

    def test_recommend_single_when_criteria_not_met(self):
        self.assertEqual(recommend_topology(UpgradeCriteria()), TOPOLOGY_SINGLE)
        self.assertEqual(recommend_topology(UpgradeCriteria(parallel=True)), TOPOLOGY_SINGLE)

    def test_recommend_supervisor_for_heterogeneous_permissions(self):
        criteria = UpgradeCriteria(True, True, True, True)
        self.assertEqual(recommend_topology(criteria), TOPOLOGY_SUPERVISOR)

    def test_recommend_blackboard_for_homogeneous_parallel_work(self):
        """同质并行 + 上下文隔离 → 黑板最省事。

        这要**主动放宽**判据：放弃"能力异构"与"权限隔离"，
        因为黑板的前提就是大家干一样的活、用同一份中间产物。
        """
        criteria = UpgradeCriteria(parallel=True, context_isolation=True)
        topology = recommend_topology(criteria, required=("parallel", "context_isolation"))
        self.assertEqual(topology, TOPOLOGY_BLACKBOARD)
        # 不放宽就不该升级——默认门槛是四条全中。
        self.assertEqual(recommend_topology(criteria), TOPOLOGY_SINGLE)

    def test_mesh_is_available_but_has_no_recommendation(self):
        """网状的收敛代价最高——本章不推荐它，但表里要有它的位置。"""
        from dragon_agent.orchestration import TOPOLOGIES

        self.assertIn("mesh", TOPOLOGIES)
        self.assertIn("最难收敛", TOPOLOGIES["mesh"].converge_cost)


# ---------------------------------------------------------------------------
# 三、默认是单 Agent
# ---------------------------------------------------------------------------


class TestDefaultIsSingleAgent(unittest.TestCase):
    def test_single_agent_is_the_default_orchestrator(self):
        default = build_orchestrator(make_config())
        self.assertIsInstance(default, SingleAgent)

    def test_single_agent_delegates_to_the_agent(self):
        agent = make_agent(MockLLM.say("编排器答", in_tokens=2, out_tokens=2))
        orchestrator = SingleAgent(agent)
        result = orchestrator.run("跑一个任务")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, "编排器答")

    def test_single_agent_never_raises(self):
        class Exploding:
            def run(self, task):
                raise RuntimeError("炸了")

        result = SingleAgent(Exploding()).run("任务")
        self.assertEqual(result.status, "error")
        self.assertTrue(result.stop_reason)

    def test_single_agent_and_supervisor_are_siblings(self):
        self.assertTrue(issubclass(SingleAgent, Orchestrator))
        self.assertTrue(issubclass(Supervisor, Orchestrator))
        self.assertFalse(issubclass(SingleAgent, Supervisor))


# ---------------------------------------------------------------------------
# 四、Supervisor：派单、回收、判定完成
# ---------------------------------------------------------------------------


class TestSupervisorDispatch(unittest.TestCase):
    def _supervisor(self, plan_json=PLAN_JSON, synthesis="最终结论：预算 100 万", **kwargs):
        planner_agent = make_agent(
            MockLLM.say(plan_json, in_tokens=10, out_tokens=5),
            MockLLM.say(synthesis, in_tokens=10, out_tokens=5),
        )
        workers = {
            "research": FakeAgent("Orion 预算是 100 万", tokens=11, cost=0.01),
            "calc": FakeAgent("还能花 60 万", tokens=7, cost=0.02),
        }
        kwargs.setdefault("fallback", SingleAgent(make_agent(MockLLM.say("单 Agent 兜底"))))
        return Supervisor(planner_agent, workers, **kwargs), workers

    def test_dispatch_collect_and_converge(self):
        """三段式跑通：派单 → 回收 → 判定完成。"""
        supervisor, workers = self._supervisor()
        result = supervisor.run("查一下 Orion 的预算还剩多少")
        self.assertEqual(result.status, "completed")
        self.assertIn("最终结论", result.answer)
        self.assertEqual(len(supervisor.last_report.outcomes), 2)
        self.assertEqual(supervisor.last_report.rounds, 1)
        self.assertFalse(supervisor.last_report.fell_back)

    def test_worker_answers_reach_the_shared_state(self):
        """回收的结论要写进共享状态——下一个子任务才读得到。"""
        supervisor, _ = self._supervisor()
        supervisor.run("任务")
        self.assertEqual(supervisor.state.read("查资料"), "Orion 预算是 100 万")
        self.assertEqual(supervisor.state.read("算账"), "还能花 60 万")

    def test_dependencies_are_respected(self):
        """依赖顺序：算账必须在查资料之后。"""
        supervisor, workers = self._supervisor(
            plan_json=json.dumps([
                {"name": "算账", "prompt": "算账", "worker": "calc", "depends_on": ["查资料"]},
                {"name": "查资料", "prompt": "查资料", "worker": "research"},
            ], ensure_ascii=False)
        )
        supervisor.run("任务")
        research_prompt = workers["research"].prompts[0]
        calc_prompt = workers["calc"].prompts[0]
        self.assertIn("已有结论", calc_prompt)
        self.assertIn("Orion 预算是 100 万", calc_prompt)
        self.assertNotIn("已有结论", research_prompt)

    def test_cyclic_dependencies_are_rejected(self):
        """依赖成环是规划出错，不能带着环往下跑。"""
        supervisor, _ = self._supervisor(
            plan_json=json.dumps([
                {"name": "A", "prompt": "a", "worker": "research", "depends_on": ["B"]},
                {"name": "B", "prompt": "b", "worker": "calc", "depends_on": ["A"]},
            ], ensure_ascii=False)
        )
        result = supervisor.run("任务")
        self.assertTrue(supervisor.last_report.fell_back)
        self.assertIn("规划失败", result.stop_reason)

    def test_unknown_worker_is_reported_not_crashed(self):
        supervisor, _ = self._supervisor(
            plan_json=json.dumps([
                {"name": "查资料", "prompt": "查", "worker": "不存在的能力域"},
            ], ensure_ascii=False)
        )
        result = supervisor.run("任务")
        self.assertEqual(result.status, "completed")
        outcome = supervisor.last_report.outcomes[0]
        self.assertEqual(outcome.status, "error")
        self.assertIn("没有名为", outcome.error)

    def test_token_and_cost_are_aggregated(self):
        supervisor, _ = self._supervisor()
        result = supervisor.run("任务")
        self.assertEqual(result.total_tokens, 18)
        self.assertAlmostEqual(result.total_cost, 0.03)

    def test_explicit_planner_function_is_used(self):
        """给了 planner 就不问模型要计划——测试与回放靠它保持确定性。"""
        planner_agent = make_agent(MockLLM.say("汇总用的答案"))
        supervisor = Supervisor(
            planner_agent,
            {"research": FakeAgent("答案")},
            planner=lambda task: [SubTask(name="查资料", prompt="查", worker="research")],
        )
        result = supervisor.run("任务")
        self.assertEqual(result.status, "completed")
        asked_for_plan = any(
            "只输出一个 JSON 数组" in json.dumps(call["messages"], ensure_ascii=False)
            for call in planner_agent.llm.calls
        )
        self.assertFalse(asked_for_plan, "给了 planner 就该跳过规划调用")


class TestSupervisorConvergence(unittest.TestCase):
    def test_continuation_starts_a_new_round(self):
        """编排者说"还要再做一件事"，就再开一轮。"""
        planner_agent = make_agent(
            MockLLM.say(PLAN_JSON, in_tokens=10, out_tokens=5),
            MockLLM.say("继续：补充|再补一条数据\n中间结论", in_tokens=10, out_tokens=5),
            MockLLM.say("最终结论", in_tokens=10, out_tokens=5),
        )
        workers = {
            "research": FakeAgent("查到的资料"),
            "calc": FakeAgent("算出来的数"),
        }
        supervisor = Supervisor(planner_agent, workers, max_rounds=3)
        result = supervisor.run("任务")
        self.assertEqual(supervisor.last_report.rounds, 2)
        self.assertEqual(len(supervisor.last_report.outcomes), 3)
        self.assertIn("最终结论", result.answer)

    def test_max_rounds_forces_convergence(self):
        """轮数用完就强制收敛——丢弃"还要继续"，用现有结论收尾。"""
        planner_agent = make_agent(
            MockLLM.say(PLAN_JSON, in_tokens=10, out_tokens=5),
            MockLLM.say("继续：补充|再补一条\n中间结论", in_tokens=10, out_tokens=5),
        )
        workers = {"research": FakeAgent("查到的资料"), "calc": FakeAgent("算出来的数")}
        supervisor = Supervisor(planner_agent, workers, max_rounds=1)
        result = supervisor.run("任务")
        self.assertEqual(supervisor.last_report.rounds, 1)
        self.assertIn("强制收敛", result.stop_reason)
        self.assertEqual(result.status, "completed")

    def test_no_progress_detection_stops_the_loop(self):
        """连续两轮结果完全相同 = 没进展，继续派只是烧钱。"""
        planner_agent = make_agent(
            MockLLM.say(PLAN_JSON, in_tokens=10, out_tokens=5),
            MockLLM.say("继续：补充|再补一条\n中间结论", in_tokens=10, out_tokens=5),
            MockLLM.say("继续：补充|再补一条\n中间结论", in_tokens=10, out_tokens=5),
        )
        workers = {"research": FakeAgent("查到的资料"), "calc": FakeAgent("算出来的数")}
        supervisor = Supervisor(planner_agent, workers, max_rounds=5)
        result = supervisor.run("任务")
        self.assertIn("无进展", result.stop_reason)

    def test_plan_failure_falls_back_to_single_agent(self):
        """规划失败要退回单 Agent——多 Agent 永远要有一条退路。"""
        planner_agent = make_agent(MockLLM.say("这不是 JSON，也解析不出子任务"))
        fallback = SingleAgent(make_agent(MockLLM.say("单 Agent 兜底的答案")))
        supervisor = Supervisor(planner_agent, {"research": FakeAgent("x")}, fallback=fallback)
        result = supervisor.run("任务")
        self.assertTrue(supervisor.last_report.fell_back)
        self.assertIn("单 Agent 兜底的答案", result.answer)
        self.assertEqual(result.status, "completed")

    def test_supervisor_never_raises(self):
        class ExplodingWorker:
            def run(self, task):
                raise RuntimeError("worker 炸了")

        planner_agent = make_agent(
            MockLLM.say(PLAN_JSON),
            MockLLM.say("汇总"),
        )
        supervisor = Supervisor(planner_agent, {"research": ExplodingWorker()})
        result = supervisor.run("任务")
        self.assertIsInstance(result, AgentResult)
        self.assertEqual(supervisor.last_report.outcomes[0].status, "error")

    def test_parallel_dispatch_keeps_plan_order(self):
        """并行跑完，结果顺序仍然按计划顺序——回放才不受影响。"""
        planner_agent = make_agent(
            MockLLM.say(PLAN_JSON, in_tokens=10, out_tokens=5),
            MockLLM.say("最终结论", in_tokens=10, out_tokens=5),
        )
        workers = {
            "research": FakeAgent("资料"),
            "calc": FakeAgent("数字"),
        }
        supervisor = Supervisor(planner_agent, workers, parallel=True)
        supervisor.run("任务")
        self.assertEqual([o.name for o in supervisor.last_report.outcomes], ["查资料", "算账"])


# ---------------------------------------------------------------------------
# 五、共享状态：三种做法
# ---------------------------------------------------------------------------


class TestSharedState(unittest.TestCase):
    def test_all_three_implement_the_protocol(self):
        for state in (SharedMemoryState(), SharedDocumentState(), MessagePassingState()):
            self.assertIsInstance(state, SharedState)

    def test_shared_memory_overwrites_silently(self):
        """共享内存：读最快，但覆盖是**静默**的。"""
        state = SharedMemoryState()
        state.write("k", "甲", author="a")
        state.write("k", "乙", author="b")
        self.assertEqual(state.read("k"), "乙")
        self.assertEqual(state.conflicts(), ["k"])

    def test_shared_document_keeps_every_version(self):
        """共享文档：留痕，冲突看得见。"""
        state = SharedDocumentState()
        state.write("k", "甲", author="a")
        state.write("k", "乙", author="b")
        self.assertEqual(state.read("k"), "乙")
        self.assertEqual(len(state.history()), 2)
        self.assertIn("甲", state.document())
        self.assertIn("乙", state.document())
        self.assertEqual(state.conflicts(), ["k"])

    def test_message_passing_delivers_to_a_mailbox(self):
        """消息传递：写不能覆盖——消息是发给某个人的。"""
        state = MessagePassingState()
        state.deliver("worker-a", "结论", "预算 100 万", author="worker-b")
        self.assertEqual(len(state.inbox("worker-a")), 1)
        self.assertEqual(state.inbox("worker-a")[0]["value"], "预算 100 万")
        self.assertEqual(state.take("worker-a")[0]["key"], "结论")
        self.assertEqual(state.inbox("worker-a"), [], "消息只能读一次")

    def test_message_passing_has_no_global_view(self):
        """没有全局视图：想知道总共有什么，得翻信箱。"""
        state = MessagePassingState()
        state.deliver("a", "k1", "v1")
        state.deliver("b", "k2", "v2")
        self.assertIsNone(state.read("k2"), "别的信箱里的消息读不到")
        self.assertEqual(len(state.history()), 2)


# ---------------------------------------------------------------------------
# 六、计划解析
# ---------------------------------------------------------------------------


class TestPlanParsing(unittest.TestCase):
    def test_parses_plain_json_array(self):
        subtasks = parse_plan(PLAN_JSON)
        self.assertEqual(len(subtasks), 2)
        self.assertEqual(subtasks[0].name, "查资料")
        self.assertEqual(subtasks[0].worker, "research")

    def test_parses_fenced_json(self):
        text = "好的，这是计划：\n```json\n" + PLAN_JSON + "\n```\n希望有帮助。"
        subtasks = parse_plan(text)
        self.assertEqual(len(subtasks), 2)

    def test_unparsable_plan_raises(self):
        with self.assertRaises(PlanError):
            parse_plan("我觉得应该先查资料，然后再算账。")

    def test_continuation_lines_are_parsed(self):
        subtasks = parse_continuations("结论在这\n继续：补充|再补一条数据\n继续：核查|核一下数")
        self.assertEqual(len(subtasks), 2)
        self.assertEqual(subtasks[0].name, "补充")
        self.assertEqual(subtasks[0].prompt, "再补一条数据")

    def test_no_continuation_means_converged(self):
        self.assertEqual(parse_continuations("这就是最终结论。"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
