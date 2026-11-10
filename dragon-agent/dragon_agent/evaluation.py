"""评测与回归：让每一次改动都有依据，而不是靠"感觉变好了"。

这一层回答四个问题：

1. **测什么**——任务集（用例、覆盖矩阵、难度分层、黄金答案与可接受答案）；
2. **怎么判**——判定器（确定性断言优先，结构化校验其次，LLM 判定最后且必须显式开启）；
3. **和谁比**——基线与对比（哪几条用例翻转了、噪声地板在哪）；
4. **怎么卡**——CI 门禁（新增回归条数、通过率下限、成本增量、失败样本自动归档）。

两条纪律贯穿全模块：

- **判定不调模型**。本模块不导入任何模型客户端，判定只用规则与关键字。
  想用模型当裁判，走 ``LLMJudge`` 并显式注入判官函数——那是**用户的决定**，
  不是本模块的默认行为；用了它产出的报告会带 ``nondeterministic`` 标记，
  CI 门禁默认不认这种报告。
- **判定过程不抛异常**。一个用例炸了只算这条用例失败，不影响整轮评测。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

__all__ = [
    "EvalCase",
    "JsonSchema",
    "CheckResult",
    "Judge",
    "AssertionJudge",
    "StructuredJudge",
    "LLMJudge",
    "EvalReport",
    "Comparison",
    "CoverageReport",
    "GateConfig",
    "GateResult",
    "Evaluator",
    "coverage_report",
    "coverage_gaps",
    "sample_cases",
    "compare_reports",
    "diff_reports",
    "gate",
    "archive_failures",
    "save_baseline",
    "load_baseline",
]


# ---------------------------------------------------------------------------
# 用例：任务集的最小单元
# ---------------------------------------------------------------------------


@dataclass
class JsonSchema:
    """答案必须是 JSON 时，用它描述结构要求。

    只支持三种检查：必填字段、字段类型、字段取值。结构化校验的价值在于
    "答案长什么样"是可判定的，而不是"答案好不好"——后者交给人或者 LLM 裁判。
    """

    required: list[str] = field(default_factory=list)
    types: dict[str, str] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalCase:
    """一条评测用例。

    字段分三组：

    - **判据**（``expect`` / ``forbid`` / ``expect_pattern`` / ``forbid_pattern`` /
      ``json_schema``）：答案必须满足什么。
    - **可接受答案**（``accept``）：同一事实的多种合法表述，每组命中任一即通过。
      黄金答案是 ``expect`` 里的字面事实，可接受答案是它的同义改写集合。
    - **元信息**（``difficulty`` / ``tags`` / ``answer``）：用来做覆盖矩阵与
      分层抽样；``answer`` 只在**自检模式**下使用（没有 runner 时拿它当被测答案）。

    ``expect`` 与 ``forbid`` 是契约里定死的两个字段，其余都是可选扩展。
    """

    id: str
    task: str
    expect: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)

    #: 可接受答案：每组是一组同义表述，命中任意一个即视为该组通过。
    accept: list[list[str]] = field(default_factory=list)
    #: 必须匹配的正则（``expect`` 的正则版本）。
    expect_pattern: list[str] = field(default_factory=list)
    #: 命中即失败的正则。
    forbid_pattern: list[str] = field(default_factory=list)
    #: 结构化校验要求；为 None 表示这条用例不做结构校验。
    json_schema: JsonSchema | None = None
    #: 难度分层：easy / medium / hard。
    difficulty: str = "core"
    #: 覆盖矩阵的维度标签，例如 "tool_use" / "multi_step" / "refusal"。
    tags: list[str] = field(default_factory=list)
    #: 自检模式下的被测答案；有 runner 时忽略。
    answer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "task": self.task,
            "expect": list(self.expect),
            "forbid": list(self.forbid),
            "accept": [list(g) for g in self.accept],
            "expect_pattern": list(self.expect_pattern),
            "forbid_pattern": list(self.forbid_pattern),
            "difficulty": self.difficulty,
            "tags": list(self.tags),
        }
        if self.json_schema is not None:
            data["json_schema"] = {
                "required": list(self.json_schema.required),
                "types": dict(self.json_schema.types),
                "values": dict(self.json_schema.values),
            }
        if self.answer is not None:
            data["answer"] = self.answer
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvalCase":
        schema = data.get("json_schema")
        json_schema = None
        if schema:
            json_schema = JsonSchema(
                required=list(schema.get("required") or []),
                types=dict(schema.get("types") or {}),
                values=dict(schema.get("values") or {}),
            )
        return cls(
            id=str(data.get("id", "")),
            task=str(data.get("task", "")),
            expect=list(data.get("expect") or []),
            forbid=list(data.get("forbid") or []),
            accept=[list(g) for g in (data.get("accept") or [])],
            expect_pattern=list(data.get("expect_pattern") or []),
            forbid_pattern=list(data.get("forbid_pattern") or []),
            json_schema=json_schema,
            difficulty=str(data.get("difficulty", "core")),
            tags=list(data.get("tags") or []),
            answer=data.get("answer"),
        )


# ---------------------------------------------------------------------------
# 判定器
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """一次判定的结果。

    ``applied=False`` 表示这个判定器对这条用例不适用（比如结构化校验遇到
    自由文本答案）——不适用不等于通过，判定器会把它跳过。
    """

    passed: bool
    reason: str = ""
    applied: bool = True
    #: True 表示这条判定依赖模型，报告会被标记为不可复现。
    nondeterministic: bool = False


@runtime_checkable
class Judge(Protocol):
    """判定器协议：给一条用例和一个答案，返回判定结果。"""

    name: str

    def judge(self, case: EvalCase, answer: str) -> CheckResult:
        ...


class AssertionJudge:
    """确定性断言：子串 + 正则。

    这是**默认判定器**，也是能用的地方就该用的判定器——同一条输入永远得到
    同一条判定，可以进 CI，可以当基线。

    规则：

    - ``expect`` 里每一条都必须出现在答案中；
    - ``forbid`` 里任何一条出现在答案中即失败；
    - ``expect_pattern`` 每条正则都必须匹配；
    - ``forbid_pattern`` 每条正则都不能匹配；
    - ``accept`` 每组至少命中一个（组内是同义表述）。
    """

    name = "assertion"

    def judge(self, case: EvalCase, answer: str) -> CheckResult:
        checks = (
            list(case.expect)
            + list(case.expect_pattern)
            + list(case.forbid)
            + list(case.forbid_pattern)
            + [g for g in case.accept if g]
        )
        if not checks:
            return CheckResult(passed=False, reason="用例没有任何断言", applied=False)

        text = answer or ""
        for item in case.expect:
            if item not in text:
                return CheckResult(False, f"缺少必须出现的内容：{item!r}")
        for pattern in case.expect_pattern:
            if not re.search(pattern, text):
                return CheckResult(False, f"不匹配要求的正则：{pattern!r}")
        for item in case.forbid:
            if item in text:
                return CheckResult(False, f"出现了禁止出现的内容：{item!r}")
        for pattern in case.forbid_pattern:
            if re.search(pattern, text):
                return CheckResult(False, f"匹配了禁止的正则：{pattern!r}")
        for group in case.accept:
            if not group:
                continue
            if not any(item in text for item in group):
                return CheckResult(False, f"没有命中任何可接受表述：{group!r}")
        return CheckResult(True, "全部断言通过")


class StructuredJudge:
    """结构化校验：答案必须是 JSON，且满足 schema。

    覆盖矩阵里凡是"输出格式是契约的一部分"的用例，都该用这个判定器——
    它比 LLM 裁判便宜一个数量级，而且不会飘。
    """

    name = "structured"

    def judge(self, case: EvalCase, answer: str) -> CheckResult:
        schema = case.json_schema
        if schema is None:
            return CheckResult(True, "用例没有结构要求", applied=False)

        payload = extract_json(answer or "")
        if payload is None:
            return CheckResult(False, "答案里找不到合法 JSON")
        if not isinstance(payload, dict):
            return CheckResult(False, f"JSON 顶层应该是对象，实际是 {type(payload).__name__}")

        for key in schema.required:
            if key not in payload:
                return CheckResult(False, f"JSON 缺少必填字段：{key!r}")
        for key, expected in schema.types.items():
            if key not in payload:
                continue
            actual = type(payload[key]).__name__
            if not type_matches(payload[key], expected):
                return CheckResult(False, f"字段 {key!r} 类型应为 {expected}，实际是 {actual}")
        for key, expected in schema.values.items():
            if key not in payload:
                continue
            if payload[key] != expected:
                return CheckResult(False, f"字段 {key!r} 取值应为 {expected!r}，实际是 {payload[key]!r}")
        return CheckResult(True, "结构校验通过")


class LLMJudge:
    """用模型当裁判——**必须显式注入判官函数**，且默认不参与门禁。

    为什么不放在默认判定器里：模型裁判有采样偏差、位置偏差、长度偏好，
    同一个答案两次判可能不同。它的结论适合"参考"，不适合"卡流水线"。

    用了它产出的报告会带 ``nondeterministic=True``，CI 门禁默认拒绝这种报告
    （除非显式 ``require_deterministic=False``）。

    注入的判官函数签名：``fn(case, answer) -> bool | str``。
    返回 True 视为通过，返回字符串视为不通过（字符串是理由）。
    """

    name = "llm"

    def __init__(self, judge_fn: Callable[[EvalCase, str], bool | str]) -> None:
        self._judge_fn = judge_fn

    def judge(self, case: EvalCase, answer: str) -> CheckResult:
        try:
            outcome = self._judge_fn(case, answer)
        except Exception as exc:  # 判官炸了算这条用例失败，不炸整轮
            return CheckResult(False, f"判官异常：{type(exc).__name__}: {exc}",
                               nondeterministic=True)
        if isinstance(outcome, str):
            return CheckResult(False, outcome, nondeterministic=True)
        return CheckResult(bool(outcome), "模型判定通过" if outcome else "模型判定不通过",
                           nondeterministic=True)


def type_matches(value: Any, expected: str) -> bool:
    """按名字判断类型是否匹配。布尔不算整数——JSON 里 ``true`` 不是数字。"""
    if expected == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "float":
        return isinstance(value, float) or (isinstance(value, int) and not isinstance(value, bool))
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "str":
        return isinstance(value, str)
    if expected == "bool":
        return isinstance(value, bool)
    if expected == "list":
        return isinstance(value, list)
    if expected == "dict":
        return isinstance(value, dict)
    if expected == "any":
        return True
    return False


def extract_json(text: str) -> Any | None:
    """从一段文本里抠出 JSON。

    顺序尝试：整段解析 → ```` ```json ```` 代码块 → 第一段 ``{...}`` / ``[...]``。
    抠不出来返回 None，判定器据此判失败。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        pass
    fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except (TypeError, ValueError):
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except (TypeError, ValueError):
                continue
    return None


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


@dataclass
class EvalReport:
    """一轮评测的结果。

    前四个字段是契约定死的；其余是本模块补充的可观测信息。
    """

    total: int = 0
    passed: int = 0
    failures: list[str] = field(default_factory=list)
    regression_vs_baseline: list[str] = field(default_factory=list)

    #: 每条用例的通过与否（id → bool）。
    case_results: dict[str, bool] = field(default_factory=dict)
    #: 失败原因（id → 原因）。
    details: dict[str, str] = field(default_factory=dict)
    #: 是否用过模型裁判。用过就不保证可复现。
    nondeterministic: bool = False
    #: 本轮累计 token 与成本（runner 能给就给，给不了就是 0）。
    total_tokens: int = 0
    total_cost: float = 0.0

    @property
    def pass_rate(self) -> float:
        """通过率。用例数为 0 时算 0。"""
        return (self.passed / self.total) if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failures": list(self.failures),
            "regression_vs_baseline": list(self.regression_vs_baseline),
            "case_results": dict(self.case_results),
            "details": dict(self.details),
            "nondeterministic": self.nondeterministic,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvalReport":
        return cls(
            total=int(data.get("total", 0)),
            passed=int(data.get("passed", 0)),
            failures=list(data.get("failures") or []),
            regression_vs_baseline=list(data.get("regression_vs_baseline") or []),
            case_results={str(k): bool(v) for k, v in (data.get("case_results") or {}).items()},
            details={str(k): str(v) for k, v in (data.get("details") or {}).items()},
            nondeterministic=bool(data.get("nondeterministic", False)),
            total_tokens=int(data.get("total_tokens", 0)),
            total_cost=float(data.get("total_cost", 0.0)),
        )


@dataclass
class Comparison:
    """当前结果与基线的差异摘要。

    ``noise_floor`` 是**噪声地板**：一条用例在通过率里占的权重。
    小任务集上，1 条用例就等于好几个百分点的变化——百分比在小样本里不稳定，
    真正稳定的信号是**具体哪几条用例翻转了**。
    """

    baseline_passed: int = 0
    current_passed: int = 0
    baseline_total: int = 0
    current_total: int = 0
    pass_rate_delta: float = 0.0
    noise_floor: float = 0.0
    newly_failing: list[str] = field(default_factory=list)
    newly_passing: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    significant: bool = False
    note: str = ""


@dataclass
class CoverageReport:
    """覆盖矩阵：标签 × 难度 → 用例条数。"""

    cells: dict[str, dict[str, int]] = field(default_factory=dict)
    total: int = 0
    untagged: int = 0
    gaps: list[str] = field(default_factory=list)


@dataclass
class GateConfig:
    """CI 门禁的阈值。

    - ``max_new_regressions``：允许新增回归的条数，默认 0——一条都不许。
    - ``min_pass_rate``：通过率下限；None 表示不检查。
    - ``max_cost_delta``：相对基线的成本增量上限（美元）；None 表示不检查。
    - ``require_deterministic``：是否拒绝含模型裁判的报告，默认拒绝。
    """

    max_new_regressions: int = 0
    min_pass_rate: float | None = None
    max_cost_delta: float | None = None
    require_deterministic: bool = True


@dataclass
class GateResult:
    """门禁判定结果。``ok=False`` 时 ``reasons`` 说明每一条原因。"""

    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    report: EvalReport | None = None
    comparison: Comparison | None = None


# ---------------------------------------------------------------------------
# 评测器
# ---------------------------------------------------------------------------


class Evaluator:
    """跑任务集、判对错、和基线比。

    构造方式有两种：

    - ``Evaluator(config)``——不注入 runner，走**自检模式**：用用例自带的
      ``answer`` 当被测答案。此时这个对象只做判定，适合写纯断言的单测。
    - ``Evaluator(config, runner)``——注入 runner，用它产出答案。

    ``runner`` 可以是两种形态：

    - 一个可调用对象 ``fn(task) -> str``；
    - 一个带 ``run(task)`` 方法的对象（Agent、编排器都长这样），返回值可以是
      字符串，也可以是带 ``answer`` 属性的结果对象。

    判定器默认是 ``[AssertionJudge(), StructuredJudge()]``——**确定性优先**。
    想加模型裁判就显式传 ``judges=[...]``，报告会因此带上不可复现标记。
    """

    def __init__(self, config: Any = None, runner: Any = None,
                 judges: list[Any] | None = None) -> None:
        self.config = config
        self.runner = runner
        self.judges: list[Any] = list(judges) if judges is not None else [
            AssertionJudge(),
            StructuredJudge(),
        ]
        self._last_report: EvalReport | None = None

    # ------------------------------------------------------------------ 主流程

    def run(self, cases: list[EvalCase]) -> EvalReport:
        """跑一轮评测。**不抛异常**：单条用例出错只算这条失败。"""
        report = EvalReport()
        for case in cases:
            try:
                answer, tokens, cost = self._answer_for(case)
            except Exception as exc:
                report.case_results[case.id] = False
                report.details[case.id] = f"取答案时异常：{type(exc).__name__}: {exc}"
                report.failures.append(case.id)
                continue

            passed, reason, nondet = self._judge(case, answer)
            report.case_results[case.id] = passed
            report.total_tokens += tokens
            report.total_cost += cost
            if nondet:
                report.nondeterministic = True
            if passed:
                report.passed += 1
            else:
                report.failures.append(case.id)
                report.details[case.id] = reason
        report.total = len(cases)
        self._last_report = report
        return report

    def compare(self, baseline: EvalReport) -> EvalReport:
        """把最近一次结果和基线比，返回带 ``regression_vs_baseline`` 的报告。"""
        current = self._last_report
        if current is None:
            current = EvalReport()
        return compare_reports(current, baseline)

    # ------------------------------------------------------------------ 内部

    def _answer_for(self, case: EvalCase) -> tuple[str, int, float]:
        """拿到被测答案。

        有 runner 用 runner 的结果，没有 runner 就用用例自带的 ``answer``
        （自检模式）。runner 返回带 ``answer`` 属性的对象时取那个属性。
        """
        if self.runner is None:
            if case.answer is None:
                return "", 0, 0.0
            return case.answer, 0, 0.0

        result = self._call_runner(case.task)
        answer = getattr(result, "answer", None)
        if answer is None:
            answer = result if isinstance(result, str) else str(result)
        tokens = int(getattr(result, "total_tokens", 0) or 0)
        cost = float(getattr(result, "total_cost", 0.0) or 0.0)
        return str(answer), tokens, cost

    def _call_runner(self, task: str) -> Any:
        """调用 runner——兼容「有 run 方法」和「本身就是可调用对象」两种形态。"""
        run_method = getattr(self.runner, "run", None)
        if callable(run_method):
            return run_method(task)
        if callable(self.runner):
            return self.runner(task)
        raise TypeError("runner 既没有 run 方法，也不可调用")

    def _judge(self, case: EvalCase, answer: str) -> tuple[bool, str, bool]:
        """让所有适用的判定器依次判定。

        返回 ``(是否通过, 原因, 是否用过模型裁判)``。没有判定器适用时判失败——
        一条没有断言的用例不该算通过。
        """
        reasons: list[str] = []
        hints: list[str] = []
        nondet = False
        applied = 0
        for judge in self.judges:
            try:
                result = judge.judge(case, answer)
            except Exception as exc:
                reasons.append(f"判定器 {getattr(judge, 'name', '?')} 异常："
                               f"{type(exc).__name__}: {exc}")
                continue
            if not result.applied:
                # 不适用的判定器不算通过，但它常常是问题的线索——留着当提示。
                if result.reason:
                    hints.append(result.reason)
                continue
            applied += 1
            if result.nondeterministic:
                nondet = True
            if not result.passed:
                reasons.append(result.reason or "判定不通过")
        if applied == 0:
            detail = f"（{'；'.join(hints)}）" if hints else ""
            return False, f"没有任何判定器适用于这条用例{detail}", nondet
        if reasons:
            return False, "；".join(reasons), nondet
        return True, "通过", nondet


# ---------------------------------------------------------------------------
# 覆盖矩阵与抽样
# ---------------------------------------------------------------------------


def coverage_report(cases: list[EvalCase],
                    axes: tuple[list[str], list[str]] | None = None) -> CoverageReport:
    """算覆盖矩阵。

    ``axes`` 是 ``(标签轴, 难度轴)``；不给的话标签轴取实际出现过的标签，
    难度轴取实际出现过的难度。给了就把矩阵补成完整的矩形，缺口进 ``gaps``。
    """
    cells: dict[str, dict[str, int]] = {}
    untagged = 0
    for case in cases:
        if not case.tags:
            untagged += 1
        for tag in case.tags or [""]:
            row = cells.setdefault(tag, {})
            row[case.difficulty] = row.get(case.difficulty, 0) + 1

    gaps: list[str] = []
    if axes is not None:
        tag_axis, diff_axis = axes
        # 补成完整矩形：缺的格子显式记 0，缺口才是可见的。
        for tag in tag_axis:
            row = cells.setdefault(tag, {})
            for diff in diff_axis:
                row.setdefault(diff, 0)
        for tag in tag_axis:
            for diff in diff_axis:
                if cells.get(tag, {}).get(diff, 0) == 0:
                    gaps.append(f"{tag}/{diff}")

    return CoverageReport(cells=cells, total=len(cases), untagged=untagged, gaps=gaps)


def coverage_gaps(cases: list[EvalCase], tags: list[str], difficulties: list[str]) -> list[str]:
    """覆盖矩阵里的空格——"标签 × 难度"没被覆盖到的组合。"""
    return coverage_report(cases, (tags, difficulties)).gaps


def sample_cases(cases: list[EvalCase], k: int, seed: int = 0) -> list[EvalCase]:
    """分层抽样：按难度轮流取，保持难度配比。

    抽样必须**可复现**：同一个 seed 选出来的必须是同一批用例。
    所以这里不用 ``random``，而是用 ``seed + 用例 id`` 的哈希排序——
    跨进程、跨机器、跨 Python 版本都一致。
    """
    if k <= 0 or not cases:
        return []
    if k >= len(cases):
        return list(cases)

    buckets: dict[str, list[EvalCase]] = {}
    for case in cases:
        buckets.setdefault(case.difficulty, []).append(case)
    for group in buckets.values():
        group.sort(key=lambda c: _stable_key(seed, c.id))

    order = sorted(buckets)
    picked: list[EvalCase] = []
    while len(picked) < k:
        progressed = False
        for difficulty in order:
            group = buckets[difficulty]
            if group:
                picked.append(group.pop(0))
                progressed = True
                if len(picked) == k:
                    break
        if not progressed:
            break
    return picked


def _stable_key(seed: int, case_id: str) -> str:
    """稳定排序键——同一个 seed 与 id 永远得到同一个键。"""
    return hashlib.sha256(f"{seed}:{case_id}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 基线与对比
# ---------------------------------------------------------------------------


def compare_reports(current: EvalReport, baseline: EvalReport) -> EvalReport:
    """把当前报告和基线报告合并：填上 ``regression_vs_baseline``。

    回归 = **基线里通过、现在不通过**的用例。注意只有两边都跑过的用例
    才算回归；新加的用例（基线里没有）不算回归，它只是新增覆盖。
    """
    merged = EvalReport.from_dict(current.to_dict())
    base_results = baseline.case_results
    cur_results = current.case_results

    common = [cid for cid in cur_results if cid in base_results]
    newly_failing = [cid for cid in common if base_results[cid] and not cur_results[cid]]
    merged.regression_vs_baseline = sorted(newly_failing)
    return merged


def diff_reports(current: EvalReport, baseline: EvalReport) -> Comparison:
    """更细的差异摘要：谁翻转了、通过率变了多少、这个幅度算不算信号。

    **噪声地板**：一条用例在通过率里的权重（1/用例数）。任务集只有 20 条时，
    1 条用例就是 5 个百分点——百分比在小样本上不稳定，所以门禁卡的是
    "新增回归几条"，而不是"通过率掉了百分之几"。
    """
    base_results = baseline.case_results
    cur_results = current.case_results
    common = [cid for cid in cur_results if cid in base_results]

    newly_failing = sorted(cid for cid in common if base_results[cid] and not cur_results[cid])
    newly_passing = sorted(cid for cid in common if not base_results[cid] and cur_results[cid])
    added = sorted(cid for cid in cur_results if cid not in base_results)
    removed = sorted(cid for cid in base_results if cid not in cur_results)

    baseline_rate = baseline.pass_rate
    current_rate = current.pass_rate
    delta = current_rate - baseline_rate
    floor = (1.0 / current.total) if current.total else 0.0

    significant = bool(newly_failing) or abs(delta) > floor
    if newly_failing:
        note = f"{len(newly_failing)} 条用例从通过变成不通过"
    elif newly_passing:
        note = f"{len(newly_passing)} 条用例从不通过变成通过"
    elif added or removed:
        note = "用例集合变了，没有同口径的翻转可比较"
    else:
        note = "没有用例翻转"
    if floor and abs(delta) <= floor and not (newly_failing or newly_passing):
        note += "；通过率变化不超过噪声地板，视为波动"

    return Comparison(
        baseline_passed=baseline.passed,
        current_passed=current.passed,
        baseline_total=baseline.total,
        current_total=current.total,
        pass_rate_delta=delta,
        noise_floor=floor,
        newly_failing=newly_failing,
        newly_passing=newly_passing,
        added=added,
        removed=removed,
        significant=significant,
        note=note,
    )


# ---------------------------------------------------------------------------
# CI 门禁与失败样本归档
# ---------------------------------------------------------------------------


def gate(current: EvalReport, baseline: EvalReport | None = None,
         config: GateConfig | None = None) -> GateResult:
    """判断这一轮能不能放行。

    门禁的判据按"稳定性"排序：

    1. **新增回归条数**——最硬的信号，一条就是一条；
    2. **不可复现的报告**——含模型裁判的报告默认不放行；
    3. **通过率下限**——粗粒度的兜底；
    4. **成本增量**——防止"跑得更久更贵也算改进"。
    """
    cfg = config or GateConfig()
    reasons: list[str] = []
    comparison: Comparison | None = None

    if cfg.require_deterministic and current.nondeterministic:
        reasons.append("报告包含模型裁判的结果，不可复现，默认不放行")

    if baseline is not None:
        comparison = diff_reports(current, baseline)
        if len(comparison.newly_failing) > cfg.max_new_regressions:
            reasons.append(
                f"新增回归 {len(comparison.newly_failing)} 条，超过允许的 "
                f"{cfg.max_new_regressions} 条：{comparison.newly_failing}"
            )
        if cfg.max_cost_delta is not None:
            delta_cost = current.total_cost - baseline.total_cost
            if delta_cost > cfg.max_cost_delta:
                reasons.append(f"成本增加 {delta_cost:.6f} 美元，超过上限 {cfg.max_cost_delta:.6f}")

    if cfg.min_pass_rate is not None and current.pass_rate < cfg.min_pass_rate:
        reasons.append(f"通过率 {current.pass_rate:.3f} 低于下限 {cfg.min_pass_rate:.3f}")

    return GateResult(ok=not reasons, reasons=reasons, report=current, comparison=comparison)


def archive_failures(report: EvalReport, cases: list[EvalCase], out_dir: str | Path) -> list[Path]:
    """把失败样本归档成 JSON 文件，一条用例一个文件。

    失败的样本是**最值钱的产物**——它告诉你这次改动到底哪里坏了。
    自动归档的意义是：CI 红了的时候，样本已经在产物目录里躺着了，
    不用再手动去捞、去复现。
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    by_id = {case.id: case for case in cases}
    written: list[Path] = []
    for case_id in report.failures:
        case = by_id.get(case_id)
        payload = {
            "id": case_id,
            "reason": report.details.get(case_id, ""),
            "case": case.to_dict() if case is not None else None,
            "regression_vs_baseline": case_id in report.regression_vs_baseline,
        }
        path = directory / f"{_safe_name(case_id)}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        written.append(path)
    return written


def _safe_name(case_id: str) -> str:
    """把用例 id 变成安全的文件名——路径分隔符之类的都不能留。"""
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", case_id).strip("_")
    return cleaned or "case"


def save_baseline(report: EvalReport, path: str | Path) -> Path:
    """把报告存成基线文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return target


def load_baseline(path: str | Path) -> EvalReport:
    """读回基线文件。文件不存在时返回一个空报告——当作"第一次跑"。"""
    source = Path(path)
    if not source.exists():
        return EvalReport()
    return EvalReport.from_dict(json.loads(source.read_text(encoding="utf-8")))
