"""第 13 章评测模块的契约测试。

覆盖五组行为：

1. **判定器**：确定性断言 / 结构化校验 / 模型裁判适配器；
2. **评测器**：跑用例、判对错、异常不外溢、报告自洽；
3. **覆盖矩阵与抽样**：矩阵缺口、分层抽样的可复现性；
4. **基线与对比**：回归识别、噪声地板、新增用例不算回归；
5. **CI 门禁与归档**：阈值判定、不可复现报告被拒、失败样本落盘。

全程离线：用假 runner 与假判官，不调模型、不联网。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

try:
    from ._compat import make_config
except ImportError:  # pragma: no cover - 直接以脚本方式运行
    from _compat import make_config  # type: ignore[no-redef]

from dragon_agent.evaluation import (
    AssertionJudge,
    Comparison,
    EvalCase,
    EvalReport,
    Evaluator,
    GateConfig,
    JsonSchema,
    LLMJudge,
    StructuredJudge,
    archive_failures,
    compare_reports,
    coverage_gaps,
    coverage_report,
    diff_reports,
    extract_json,
    gate,
    load_baseline,
    sample_cases,
    save_baseline,
    type_matches,
)


# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------


class FakeResult:
    """带 answer 属性的假结果——模拟 AgentResult。"""

    def __init__(self, answer: str, total_tokens: int = 0, total_cost: float = 0.0):
        self.answer = answer
        self.total_tokens = total_tokens
        self.total_cost = total_cost


class FakeRunner:
    """按任务给答案的假 runner。带 run() 方法，走 Agent 那种调用形态。"""

    def __init__(self, answers: dict[str, str]):
        self.answers = answers
        self.tasks: list[str] = []

    def run(self, task: str) -> FakeResult:
        self.tasks.append(task)
        return FakeResult(self.answers.get(task, ""))


def make_cases() -> list[EvalCase]:
    return [
        EvalCase(id="c1", task="项目预算是多少", expect=["100万"], forbid=["200万"]),
        EvalCase(id="c2", task="上季度预算是多少", expect=["90万"], forbid=["100万"]),
    ]


# ---------------------------------------------------------------------------
# 1. 判定器
# ---------------------------------------------------------------------------


class TestAssertionJudge(unittest.TestCase):
    """确定性断言：子串 + 正则 + 可接受答案。"""

    def setUp(self):
        self.judge = AssertionJudge()

    def test_all_expectations_present_passes(self):
        case = EvalCase(id="c1", task="t", expect=["100万"], forbid=["200万"])
        result = self.judge.judge(case, "预算是 100万")
        self.assertTrue(result.passed, result.reason)

    def test_missing_expectation_fails(self):
        case = EvalCase(id="c1", task="t", expect=["100万"])
        result = self.judge.judge(case, "我算不出来")
        self.assertFalse(result.passed)
        self.assertIn("100万", result.reason)

    def test_forbidden_content_fails(self):
        case = EvalCase(id="c1", task="t", expect=["100万"], forbid=["200万"])
        result = self.judge.judge(case, "预算是 100万，200万是旧数据")
        self.assertFalse(result.passed)
        self.assertIn("200万", result.reason)

    def test_regex_expectation(self):
        case = EvalCase(id="c1", task="t", expect_pattern=[r"\d+\s*万"])
        self.assertTrue(self.judge.judge(case, "预算 100万").passed)
        self.assertFalse(self.judge.judge(case, "预算一百万").passed)

    def test_regex_forbidden(self):
        case = EvalCase(id="c1", task="t", forbid_pattern=[r"密码|password"])
        self.assertFalse(self.judge.judge(case, "你的 password 是 123").passed)
        self.assertTrue(self.judge.judge(case, "预算是 100万").passed)

    def test_accept_group_accepts_any_synonym(self):
        """可接受答案：同义表述命中其一即通过。"""
        case = EvalCase(id="c1", task="t", accept=[["一百万", "100万"]])
        self.assertTrue(self.judge.judge(case, "预算是 100万").passed)
        self.assertTrue(self.judge.judge(case, "预算是 一百万").passed)
        self.assertFalse(self.judge.judge(case, "预算是 90万").passed)

    def test_case_without_assertions_is_not_applicable(self):
        """没有断言的用例不该被默认判定器"悄悄判过"。"""
        case = EvalCase(id="c1", task="t")
        result = self.judge.judge(case, "随便说点什么")
        self.assertFalse(result.applied)

    def test_judgement_is_deterministic(self):
        """同一输入两次判定必须完全一致——这是它能进 CI 的前提。"""
        case = EvalCase(id="c1", task="t", expect=["100万"], forbid=["200万"])
        first = self.judge.judge(case, "预算是 100万")
        second = self.judge.judge(case, "预算是 100万")
        self.assertEqual(first.passed, second.passed)
        self.assertEqual(first.reason, second.reason)


class TestStructuredJudge(unittest.TestCase):
    """结构化校验：JSON + schema。"""

    def setUp(self):
        self.judge = StructuredJudge()

    def test_case_without_schema_is_not_applicable(self):
        result = self.judge.judge(EvalCase(id="c1", task="t"), "随便")
        self.assertFalse(result.applied)

    def test_valid_json_passes(self):
        case = EvalCase(id="c1", task="t", json_schema=JsonSchema(
            required=["sku", "qty"], types={"sku": "str", "qty": "int"}))
        result = self.judge.judge(case, '{"sku": "A1", "qty": 3}')
        self.assertTrue(result.passed, result.reason)

    def test_missing_required_field_fails(self):
        case = EvalCase(id="c1", task="t", json_schema=JsonSchema(required=["sku", "qty"]))
        result = self.judge.judge(case, '{"sku": "A1"}')
        self.assertFalse(result.passed)
        self.assertIn("qty", result.reason)

    def test_wrong_type_fails(self):
        case = EvalCase(id="c1", task="t", json_schema=JsonSchema(types={"qty": "int"}))
        result = self.judge.judge(case, '{"qty": "3"}')
        self.assertFalse(result.passed)

    def test_boolean_is_not_an_integer(self):
        """JSON 里 true 不是 1——类型判定必须区分。"""
        case = EvalCase(id="c1", task="t", json_schema=JsonSchema(types={"qty": "int"}))
        self.assertFalse(self.judge.judge(case, '{"qty": true}').passed)

    def test_value_mismatch_fails(self):
        case = EvalCase(id="c1", task="t", json_schema=JsonSchema(values={"status": "ok"}))
        self.assertFalse(self.judge.judge(case, '{"status": "failed"}').passed)

    def test_json_embedded_in_text_is_extracted(self):
        case = EvalCase(id="c1", task="t", json_schema=JsonSchema(required=["sku"]))
        result = self.judge.judge(case, '结果如下：{"sku": "A1"} 完毕')
        self.assertTrue(result.passed, result.reason)

    def test_non_json_answer_fails(self):
        case = EvalCase(id="c1", task="t", json_schema=JsonSchema(required=["sku"]))
        result = self.judge.judge(case, "我不知道")
        self.assertFalse(result.passed)


class TestExtractJson(unittest.TestCase):
    """JSON 抽取的三条路径。"""

    def test_plain_json(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_block(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_embedded_object(self):
        self.assertEqual(extract_json('前缀 {"a": 1} 后缀'), {"a": 1})

    def test_garbage_returns_none(self):
        self.assertIsNone(extract_json("完全不是 JSON"))


class TestTypeMatches(unittest.TestCase):
    """类型名到 Python 类型的映射。"""

    def test_known_types(self):
        self.assertTrue(type_matches(1, "int"))
        self.assertTrue(type_matches(1.5, "float"))
        self.assertTrue(type_matches(1, "number"))
        self.assertTrue(type_matches("x", "str"))
        self.assertTrue(type_matches(True, "bool"))
        self.assertTrue(type_matches([], "list"))
        self.assertTrue(type_matches({}, "dict"))
        self.assertTrue(type_matches(object(), "any"))

    def test_unknown_type_name_is_false(self):
        self.assertFalse(type_matches(1, "不存在的类型"))


class TestLLMJudgeAdapter(unittest.TestCase):
    """模型裁判适配器：判定结果要带不可复现标记。"""

    def test_passing_judge(self):
        judge = LLMJudge(lambda case, answer: True)
        result = judge.judge(EvalCase(id="c1", task="t"), "答案")
        self.assertTrue(result.passed)
        self.assertTrue(result.nondeterministic)

    def test_failing_judge_returns_reason(self):
        judge = LLMJudge(lambda case, answer: "说得不对")
        result = judge.judge(EvalCase(id="c1", task="t"), "答案")
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "说得不对")

    def test_judge_exception_becomes_failure(self):
        def boom(case, answer):
            raise RuntimeError("判官挂了")

        judge = LLMJudge(boom)
        result = judge.judge(EvalCase(id="c1", task="t"), "答案")
        self.assertFalse(result.passed)
        self.assertIn("判官挂了", result.reason)


# ---------------------------------------------------------------------------
# 2. 评测器
# ---------------------------------------------------------------------------


class TestEvaluatorRun(unittest.TestCase):
    """跑用例、判对错。"""

    def test_run_counts_pass_and_fail(self):
        cases = make_cases()
        runner = FakeRunner({"项目预算是多少": "预算是 100万"})
        evaluator = Evaluator(make_config(), runner)
        report = evaluator.run(cases)
        self.assertEqual(report.total, 2)
        self.assertEqual(report.passed, 1)
        self.assertEqual(report.failures, ["c2"])

    def test_failure_reason_is_recorded(self):
        cases = make_cases()
        evaluator = Evaluator(make_config(), FakeRunner({}))
        report = evaluator.run(cases)
        self.assertIn("c1", report.details)
        self.assertIn("100万", report.details["c1"])

    def test_case_results_match_failures(self):
        evaluator = Evaluator(make_config(), FakeRunner({"项目预算是多少": "预算是 100万"}))
        report = evaluator.run(make_cases())
        self.assertEqual(report.case_results["c1"], True)
        self.assertEqual(report.case_results["c2"], False)

    def test_runner_is_called_once_per_case(self):
        cases = make_cases()
        runner = FakeRunner({"项目预算是多少": "预算是 100万"})
        Evaluator(make_config(), runner).run(cases)
        self.assertEqual(runner.tasks, ["项目预算是多少", "上季度预算是多少"])

    def test_plain_callable_runner_is_supported(self):
        evaluator = Evaluator(make_config(), lambda task: "预算是 100万")
        report = evaluator.run([make_cases()[0]])
        self.assertEqual(report.passed, 1)

    def test_self_check_mode_uses_case_answer(self):
        """不注入 runner 时，用例自带的 answer 当被测答案。"""
        cases = [
            EvalCase(id="c1", task="t", expect=["100万"], answer="预算是 100万"),
            EvalCase(id="c2", task="t", expect=["100万"], answer="不知道"),
        ]
        report = Evaluator(make_config()).run(cases)
        self.assertEqual(report.passed, 1)
        self.assertEqual(report.failures, ["c2"])

    def test_runner_exception_does_not_escape(self):
        """一条用例炸了只算这条失败，不影响整轮。"""

        class Boom:
            def run(self, task: str):
                raise RuntimeError("炸了")

        report = Evaluator(make_config(), Boom()).run(make_cases())
        self.assertEqual(report.total, 2)
        self.assertEqual(report.passed, 0)
        self.assertEqual(len(report.failures), 2)
        self.assertIn("炸了", report.details["c1"])

    def test_invalid_runner_raises_type_error(self):
        evaluator = Evaluator(make_config(), object())
        report = evaluator.run(make_cases())
        self.assertEqual(report.passed, 0)
        self.assertIn("run 方法", report.details["c1"])

    def test_report_pass_rate(self):
        evaluator = Evaluator(make_config(), FakeRunner({"项目预算是多少": "预算是 100万"}))
        report = evaluator.run(make_cases())
        self.assertAlmostEqual(report.pass_rate, 0.5)

    def test_empty_case_list(self):
        report = Evaluator(make_config(), FakeRunner({})).run([])
        self.assertEqual(report.total, 0)
        self.assertEqual(report.pass_rate, 0.0)

    def test_tokens_and_cost_are_accumulated(self):
        class Costly:
            def run(self, task: str):
                return FakeResult("预算是 100万", total_tokens=120, total_cost=0.002)

        cases = [EvalCase(id="c1", task="t", expect=["100万"])]
        report = Evaluator(make_config(), Costly()).run(cases)
        self.assertEqual(report.total_tokens, 120)
        self.assertAlmostEqual(report.total_cost, 0.002)

    def test_run_is_repeatable(self):
        """同一批用例跑两次，判定结果必须一样。"""
        cases = make_cases()
        runner = FakeRunner({"项目预算是多少": "预算是 100万"})
        evaluator = Evaluator(make_config(), runner)
        first = evaluator.run(cases)
        second = evaluator.run(cases)
        self.assertEqual(first.passed, second.passed)
        self.assertEqual(first.failures, second.failures)


class TestEvaluatorCompare(unittest.TestCase):
    """compare() 把回归填进报告。"""

    def test_compare_marks_regressions(self):
        baseline = EvalReport(total=2, passed=2, failures=[],
                               case_results={"c1": True, "c2": True})
        evaluator = Evaluator(make_config(), FakeRunner({"项目预算是多少": "预算是 100万"}))
        evaluator.run(make_cases())
        merged = evaluator.compare(baseline)
        self.assertEqual(merged.regression_vs_baseline, ["c2"])


class TestNoAssertionCase(unittest.TestCase):
    """没有断言的用例不能算通过。"""

    def test_case_without_assertions_fails(self):
        report = Evaluator(make_config(), FakeRunner({"t": "任何答案"})).run(
            [EvalCase(id="c1", task="t", answer="任何答案")])
        self.assertEqual(report.passed, 0)
        self.assertIn("断言", report.details["c1"])


class TestNondeterministicReport(unittest.TestCase):
    """用了模型裁判的报告要带不可复现标记。"""

    def test_report_is_marked(self):
        evaluator = Evaluator(make_config(), FakeRunner({"项目预算是多少": "预算是 100万"}),
                              judges=[LLMJudge(lambda case, answer: True)])
        report = evaluator.run(make_cases())
        self.assertTrue(report.nondeterministic)


# ---------------------------------------------------------------------------
# 3. 覆盖矩阵与抽样
# ---------------------------------------------------------------------------


class TestCoverage(unittest.TestCase):
    """覆盖矩阵。"""

    def setUp(self):
        self.cases = [
            EvalCase(id="c1", task="t", expect=["x"], tags=["tool_use"], difficulty="easy"),
            EvalCase(id="c2", task="t", expect=["x"], tags=["tool_use"], difficulty="hard"),
            EvalCase(id="c3", task="t", expect=["x"], difficulty="easy"),
        ]

    def test_matrix_counts(self):
        report = coverage_report(self.cases)
        self.assertEqual(report.total, 3)
        self.assertEqual(report.cells["tool_use"]["easy"], 1)
        self.assertEqual(report.cells["tool_use"]["hard"], 1)
        self.assertEqual(report.untagged, 1)

    def test_gaps_are_reported(self):
        gaps = coverage_gaps(self.cases, ["tool_use", "multi_step"], ["easy", "hard"])
        self.assertIn("multi_step/easy", gaps)
        self.assertIn("multi_step/hard", gaps)
        self.assertNotIn("tool_use/easy", gaps)

    def test_axes_fill_missing_cells_with_zero(self):
        report = coverage_report(self.cases, (["tool_use"], ["easy", "hard"]))
        self.assertEqual(report.cells["tool_use"]["easy"], 1)
        self.assertEqual(report.gaps, [])


class TestSampling(unittest.TestCase):
    """分层抽样。"""

    def setUp(self):
        self.cases = [
            EvalCase(id=f"c{i}", task="t", expect=["x"], difficulty=d)
            for i, d in enumerate(["easy", "medium", "hard"] * 4)
        ]

    def test_sample_size(self):
        self.assertEqual(len(sample_cases(self.cases, 5)), 5)

    def test_sample_is_reproducible(self):
        """同一个 seed 必须选出一模一样的子集。"""
        first = [c.id for c in sample_cases(self.cases, 6, seed=7)]
        second = [c.id for c in sample_cases(self.cases, 6, seed=7)]
        self.assertEqual(first, second)

    def test_different_seed_may_differ(self):
        first = [c.id for c in sample_cases(self.cases, 4, seed=1)]
        second = [c.id for c in sample_cases(self.cases, 4, seed=2)]
        self.assertNotEqual(first, second)

    def test_sampling_preserves_difficulty_mix(self):
        """分层抽样不能把某一档难度全丢掉。"""
        picked = sample_cases(self.cases, 6)
        difficulties = {c.difficulty for c in picked}
        self.assertEqual(len(difficulties), 3)

    def test_k_larger_than_set_returns_all(self):
        self.assertEqual(len(sample_cases(self.cases, 100)), len(self.cases))

    def test_k_zero_returns_empty(self):
        self.assertEqual(sample_cases(self.cases, 0), [])


# ---------------------------------------------------------------------------
# 4. 基线与对比
# ---------------------------------------------------------------------------


class TestCompareAndDiff(unittest.TestCase):
    """基线对比。"""

    def _baseline(self) -> EvalReport:
        return EvalReport(total=3, passed=3, failures=[],
                          case_results={"c1": True, "c2": True, "c3": True})

    def _current(self, results: dict[str, bool]) -> EvalReport:
        failures = sorted(cid for cid, ok in results.items() if not ok)
        return EvalReport(total=len(results), passed=len(results) - len(failures),
                          failures=failures, case_results=results)

    def test_regression_identified(self):
        current = self._current({"c1": True, "c2": False, "c3": True})
        merged = compare_reports(current, self._baseline())
        self.assertEqual(merged.regression_vs_baseline, ["c2"])

    def test_no_regression_when_all_still_pass(self):
        current = self._current({"c1": True, "c2": True, "c3": True})
        self.assertEqual(compare_reports(current, self._baseline()).regression_vs_baseline, [])

    def test_newly_passing_case_is_not_a_regression(self):
        current = self._current({"c1": True, "c2": True, "c3": True})
        diff = diff_reports(current, self._baseline())
        self.assertEqual(diff.newly_failing, [])
        self.assertEqual(diff.newly_passing, [])

    def test_added_case_is_not_a_regression(self):
        """基线里没有的新用例只是新增覆盖，不算回归。"""
        current = self._current({"c1": True, "c2": True, "c3": True, "c4": False})
        diff = diff_reports(current, self._baseline())
        self.assertEqual(diff.newly_failing, [])
        self.assertEqual(diff.added, ["c4"])

    def test_removed_case_is_reported(self):
        current = self._current({"c1": True, "c2": True})
        diff = diff_reports(current, self._baseline())
        self.assertEqual(diff.removed, ["c3"])

    def test_noise_floor_is_one_case_weight(self):
        """噪声地板 = 一条用例在通过率里的权重。"""
        current = self._current({"c1": True, "c2": True, "c3": True, "c4": True})
        diff = diff_reports(current, self._baseline())
        self.assertAlmostEqual(diff.noise_floor, 0.25)

    def test_pass_rate_delta(self):
        current = self._current({"c1": True, "c2": False, "c3": False})
        diff = diff_reports(current, self._baseline())
        self.assertAlmostEqual(diff.pass_rate_delta, -2 / 3)

    def test_regression_is_significant_even_below_noise_floor(self):
        """一条用例翻转就是真回归——百分比小不等于信号小。"""
        current = self._current({"c1": True, "c2": False, "c3": True})
        diff = diff_reports(current, self._baseline())
        self.assertTrue(diff.significant)
        self.assertIn("c2", diff.newly_failing)

    def test_fluctuation_within_noise_floor_is_not_significant(self):
        """同一批用例谁也没翻转，只是总数变了——这种波动不算信号。"""
        baseline = EvalReport(total=2, passed=2, failures=[], case_results={"c1": True})
        current = EvalReport(total=2, passed=1, failures=[], case_results={"c1": True})
        diff = diff_reports(current, baseline)
        self.assertFalse(diff.significant)
        self.assertIn("噪声地板", diff.note)

    def test_note_mentions_regression(self):
        current = self._current({"c1": False, "c2": True, "c3": True})
        diff = diff_reports(current, self._baseline())
        self.assertIn("从通过变成不通过", diff.note)


# ---------------------------------------------------------------------------
# 5. CI 门禁与归档
# ---------------------------------------------------------------------------


class TestGate(unittest.TestCase):
    """CI 门禁。"""

    def _report(self, passed: int, total: int, failures: list[str],
                results: dict[str, bool] | None = None, **kw) -> EvalReport:
        return EvalReport(total=total, passed=passed, failures=failures,
                          case_results=results or {}, **kw)

    def test_clean_run_passes(self):
        result = gate(self._report(2, 2, []))
        self.assertTrue(result.ok)
        self.assertEqual(result.reasons, [])

    def test_new_regression_blocks(self):
        baseline = EvalReport(total=2, passed=2, failures=[],
                              case_results={"c1": True, "c2": True})
        current = EvalReport(total=2, passed=1, failures=["c2"],
                             case_results={"c1": True, "c2": False})
        result = gate(current, baseline)
        self.assertFalse(result.ok)
        self.assertTrue(any("新增回归" in r for r in result.reasons))

    def test_allowed_regression_count(self):
        baseline = EvalReport(total=2, passed=2, failures=[],
                              case_results={"c1": True, "c2": True})
        current = EvalReport(total=2, passed=1, failures=["c2"],
                             case_results={"c1": True, "c2": False})
        result = gate(current, baseline, GateConfig(max_new_regressions=1))
        self.assertTrue(result.ok)

    def test_min_pass_rate_blocks(self):
        result = gate(self._report(1, 2, ["c2"]), None, GateConfig(min_pass_rate=0.9))
        self.assertFalse(result.ok)
        self.assertTrue(any("通过率" in r for r in result.reasons))

    def test_nondeterministic_report_is_rejected_by_default(self):
        report = self._report(2, 2, [], nondeterministic=True)
        result = gate(report)
        self.assertFalse(result.ok)
        self.assertTrue(any("不可复现" in r for r in result.reasons))

    def test_nondeterministic_report_can_be_allowed(self):
        report = self._report(2, 2, [], nondeterministic=True)
        result = gate(report, None, GateConfig(require_deterministic=False))
        self.assertTrue(result.ok)

    def test_cost_delta_blocks(self):
        baseline = EvalReport(total=2, passed=2, failures=[], total_cost=0.01)
        current = EvalReport(total=2, passed=2, failures=[], total_cost=0.50)
        result = gate(current, baseline, GateConfig(max_cost_delta=0.10))
        self.assertFalse(result.ok)
        self.assertTrue(any("成本" in r for r in result.reasons))

    def test_gate_without_baseline_only_checks_absolute_thresholds(self):
        result = gate(self._report(2, 2, []), None, GateConfig(min_pass_rate=0.5))
        self.assertTrue(result.ok)

    def test_comparison_is_attached_to_result(self):
        baseline = EvalReport(total=2, passed=2, failures=[],
                              case_results={"c1": True, "c2": True})
        current = EvalReport(total=2, passed=1, failures=["c2"],
                             case_results={"c1": True, "c2": False})
        result = gate(current, baseline)
        self.assertIsInstance(result.comparison, Comparison)
        self.assertEqual(result.comparison.newly_failing, ["c2"])


class TestArchiveFailures(unittest.TestCase):
    """失败样本自动归档。"""

    def test_failures_are_written(self):
        cases = make_cases()
        report = EvalReport(total=2, passed=1, failures=["c2"],
                            details={"c2": "缺少必须出现的内容：'90万'"})
        with tempfile.TemporaryDirectory() as tmp:
            written = archive_failures(report, cases, Path(tmp))
            self.assertEqual(len(written), 1)
            payload = json.loads(written[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["id"], "c2")
            self.assertIn("90万", payload["reason"])
            self.assertEqual(payload["case"]["task"], "上季度预算是多少")

    def test_no_failures_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = archive_failures(EvalReport(total=1, passed=1, failures=[]),
                                       make_cases(), Path(tmp))
            self.assertEqual(written, [])

    def test_unsafe_case_id_becomes_safe_filename(self):
        """用例 id 里的路径分隔符不能变成目录穿越。"""
        cases = [EvalCase(id="../evil", task="t", expect=["x"])]
        report = EvalReport(total=1, passed=0, failures=["../evil"])
        with tempfile.TemporaryDirectory() as tmp:
            written = archive_failures(report, cases, Path(tmp))
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0].parent, Path(tmp))

    def test_regression_flag_is_recorded(self):
        cases = make_cases()
        report = EvalReport(total=2, passed=1, failures=["c2"],
                            regression_vs_baseline=["c2"])
        with tempfile.TemporaryDirectory() as tmp:
            written = archive_failures(report, cases, Path(tmp))
            payload = json.loads(written[0].read_text(encoding="utf-8"))
            self.assertTrue(payload["regression_vs_baseline"])


class TestBaselinePersistence(unittest.TestCase):
    """基线的存取。"""

    def test_round_trip(self):
        report = EvalReport(total=2, passed=1, failures=["c2"],
                            details={"c2": "坏了"}, case_results={"c1": True, "c2": False},
                            total_tokens=42, total_cost=0.01)
        with tempfile.TemporaryDirectory() as tmp:
            path = save_baseline(report, Path(tmp) / "baseline.json")
            loaded = load_baseline(path)
        self.assertEqual(loaded.total, 2)
        self.assertEqual(loaded.passed, 1)
        self.assertEqual(loaded.failures, ["c2"])
        self.assertEqual(loaded.details["c2"], "坏了")
        self.assertEqual(loaded.total_tokens, 42)
        self.assertAlmostEqual(loaded.total_cost, 0.01)

    def test_missing_baseline_returns_empty_report(self):
        loaded = load_baseline(Path(tempfile.gettempdir()) / "不存在的基线.json")
        self.assertEqual(loaded.total, 0)
        self.assertEqual(loaded.passed, 0)


class TestCaseSerialization(unittest.TestCase):
    """用例的序列化——任务集要能存进仓库当基线。"""

    def test_round_trip(self):
        case = EvalCase(
            id="c1", task="t", expect=["100万"], forbid=["200万"],
            accept=[["一百万", "100万"]], expect_pattern=[r"\d+万"],
            forbid_pattern=[r"password"], json_schema=JsonSchema(required=["sku"]),
            difficulty="hard", tags=["tool_use"], answer="预算是 100万",
        )
        restored = EvalCase.from_dict(case.to_dict())
        self.assertEqual(restored.id, case.id)
        self.assertEqual(restored.expect, case.expect)
        self.assertEqual(restored.accept, case.accept)
        self.assertEqual(restored.difficulty, case.difficulty)
        self.assertEqual(restored.tags, case.tags)
        self.assertEqual(restored.json_schema.required, ["sku"])


if __name__ == "__main__":
    unittest.main()
