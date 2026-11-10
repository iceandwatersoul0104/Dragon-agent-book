"""需求规格检查器：把"规格写好了"变成"规格可验收"。

一份需求说明最容易出的问题不是写错，而是**写得太软**——"审查得准确"这种句子
读起来没问题，但没法写断言、没法回归、改了也不知道有没有变好。
这个检查器只做一件事：把规格里该有的硬信息逐条抠出来，缺了就报出来。

用法：

    python specs/check_spec.py specs/code-review-agent.spec.md          # 检查成品规格
    python specs/check_spec.py specs/SPEC_TEMPLATE.md --mode template    # 检查模板结构
    python specs/check_spec.py specs/code-review-agent.spec.md --json    # 给 CI 用的机器可读输出

退出码：0 = 通过；1 = 有错误；2 = 用法或读写错误。

纯标准库实现，不联网、不依赖任何第三方包。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- 常量

#: 规格必须有的章节。顺序就是填写顺序。
REQUIRED_SECTIONS: tuple[str, ...] = (
    "任务定义",
    "输入输出契约",
    "可用工具清单",
    "决策边界",
    "成功判据",
    "失败定义与退出状态",
    "明确不做的事",
    "评测与观测",
    "预算与约束",
)

#: 退出状态取值必须与 dragon_agent/agent.py 的 AgentResult.status 完全一致。
CONTRACT_STATUSES: tuple[str, ...] = (
    "completed",
    "stopped_budget",
    "stopped_no_progress",
    "max_steps",
    "error",
)

#: 副作用三档：只读 / 可逆写 / 不可逆写。
SIDE_EFFECT_TIERS: tuple[str, ...] = ("只读", "可逆写", "不可逆写")

#: 决策方只允许两种。
DECISION_MAKERS: tuple[str, ...] = ("LLM", "代码")

#: 占位符特征。规格里出现任何一个，都说明还有没填的地方。
PLACEHOLDER_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bTODO\b", "TODO"),
    (r"\bTBD\b", "TBD"),
    (r"待填", "待填"),
    (r"待定", "待定"),
    (r"\bXXX\b", "XXX"),
    (r"\?\?\?", "???"),
    (r"<[^<>\n]{1,40}>", "尖括号占位符"),
)

#: 成功判据的编号前缀，例如 "- **C1**：..."。
CRITERION_RE = re.compile(r"^\s*[-*]\s+\*\*(C\d+)\*\*")

#: 判据的验证方式标记。
VERIFY_MARKER = "验证方式："

#: 预算与约束里必须出现的三个键，对应 Config 的三个字段。
BUDGET_KEYS: tuple[str, ...] = ("max_steps", "token_budget", "cost_budget")

#: 只在成品模式才校验的检查项：模板里这些位置本来就该是空的。
CONTENT_ONLY_CHECKS: frozenset[str] = frozenset({"S2", "S3", "S6", "S7", "S8", "S9"})


# --------------------------------------------------------------------------- 数据结构


@dataclass
class Finding:
    """一条检查结论。``phase`` 决定它在哪种模式下会被报出来。"""

    rule: str
    phase: str          # "structure"（模板模式也查）/ "content"（仅成品模式查）
    level: str          # "error" / "warn"
    message: str
    line: int | None = None

    def render(self) -> str:
        where = f"第 {self.line} 行：" if self.line else ""
        return f"{where}{self.message}"


@dataclass
class CheckResult:
    """一项检查的汇总。"""

    rule: str
    title: str
    detail: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------- 解析工具


def normalize_title(title: str) -> str:
    """去掉章节名里的序号前缀，便于匹配。"""
    t = title.strip()
    t = re.sub(r"^[0-9]+[.、)）]\s*", "", t)
    t = re.sub(r"^[一二三四五六七八九十]+[、.．]\s*", "", t)
    return t.strip()


def parse_sections(text: str) -> tuple[dict[str, list[str]], list[int]]:
    """把 Markdown 拆成 ``{章节名: 正文行}``。同时返回出现重复的章节名。"""
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            current = normalize_title(m.group(1))
            if current not in sections:
                sections[current] = []
                order.append(current)
            continue
        if current is not None:
            sections[current].append(line)
    duplicates = [name for name in order if list(sections.keys()).count(name) > 1]
    return sections, sorted(set(duplicates))


def is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells)


def parse_tables(lines: list[str]) -> list[tuple[list[str], list[list[str]]]]:
    """抽取 Markdown 表格，返回 ``[(表头, 数据行), ...]``。"""
    raw: list[list[str]] = []
    current: list[list[str]] = []
    for line in lines:
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            current.append([c.strip() for c in s.strip("|").split("|")])
        else:
            if current:
                raw.append(current)
            current = []
    if current:
        raw.append(current)

    tables: list[tuple[list[str], list[list[str]]]] = []
    for block in raw:
        if len(block) >= 3 and is_separator_row(block[1]):
            tables.append((block[0], block[2:]))
    return tables


def find_table(tables, *keywords: str):
    """按表头关键字找表。找不到返回 None。"""
    for header, rows in tables:
        joined = " ".join(header)
        if all(k in joined for k in keywords):
            return header, rows
    return None


def column_index(header: list[str], *keywords: str) -> int | None:
    for i, cell in enumerate(header):
        if all(k in cell for k in keywords):
            return i
    return None


def bullets(lines: list[str]) -> list[tuple[int, str]]:
    """取出无序列表项，返回 ``[(行号, 内容)]``。"""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        m = re.match(r"^\s*[-*]\s+(.*\S)\s*$", line)
        if m:
            out.append((i, m.group(1)))
    return out


# --------------------------------------------------------------------------- 检查器


class SpecChecker:
    """按九项规则检查一份需求规格。"""

    def __init__(self, path: Path, text: str) -> None:
        self.path = path
        self.text = text
        self.lines = text.splitlines()
        self.sections, self.duplicates = parse_sections(text)

    # ------------------------------------------------------------------ 公共

    def body(self, name: str) -> list[str]:
        return self.sections.get(name, [])

    def body_text(self, name: str) -> str:
        return "\n".join(self.body(name))

    def tables(self, name: str):
        return parse_tables(self.body(name))

    def all_text(self) -> str:
        return self.text

    # ------------------------------------------------------------------ S1

    def check_sections(self) -> CheckResult:
        res = CheckResult("S1", "章节齐全", "")
        missing = [s for s in REQUIRED_SECTIONS if s not in self.sections]
        for s in missing:
            res.findings.append(
                Finding("S1", "structure", "error", f"缺少必需章节「{s}」")
            )
        for s in self.duplicates:
            res.findings.append(
                Finding("S1", "structure", "error", f"章节「{s}」重复出现，规格应一节一议")
            )
        res.detail = f"{len(REQUIRED_SECTIONS) - len(missing)}/{len(REQUIRED_SECTIONS)} 个必需章节"
        return res

    # ------------------------------------------------------------------ S2

    def check_placeholders(self) -> CheckResult:
        res = CheckResult("S2", "无占位符残留", "")
        hits = 0
        for i, line in enumerate(self.lines, start=1):
            for pattern, label in PLACEHOLDER_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    hits += 1
                    res.findings.append(
                        Finding("S2", "content", "error", f"残留占位符（{label}）", i)
                    )
                    break
        res.detail = f"{hits} 处占位符"
        return res

    # ------------------------------------------------------------------ S3

    def check_criteria(self) -> CheckResult:
        res = CheckResult("S3", "成功判据可验证", "")
        lines = self.body("成功判据")
        criteria: list[tuple[str, bool, int]] = []
        current: tuple[str, int, list[str]] | None = None

        for idx, line in enumerate(lines):
            m = CRITERION_RE.match(line)
            if m:
                if current is not None:
                    criteria.append(self._finish_criterion(current))
                current = (m.group(1), idx, [line])
            elif current is not None:
                current[2].append(line)
        if current is not None:
            criteria.append(self._finish_criterion(current))

        for name, has_verify, line_no in criteria:
            if not has_verify:
                res.findings.append(
                    Finding("S3", "content", "error",
                            f"成功判据 {name} 没有写「{VERIFY_MARKER}」，无法验收", line_no)
                )
        if len(criteria) < 3:
            res.findings.append(
                Finding("S3", "content", "error",
                        f"成功判据只有 {len(criteria)} 条，少于 3 条——不足以覆盖一个任务的主要失败模式")
            )
        ok_count = sum(1 for _, has_verify, _ in criteria if has_verify)
        res.detail = f"{len(criteria)} 条判据，其中 {ok_count} 条带验证方式"
        return res

    def _finish_criterion(self, block) -> tuple[str, bool, int]:
        name, line_no, chunk = block
        has_verify = any(VERIFY_MARKER in l for l in chunk)
        return name, has_verify, line_no + 1

    # ------------------------------------------------------------------ S4

    def check_decision_boundary(self) -> CheckResult:
        res = CheckResult("S4", "决策边界标注完整", "")
        found = find_table(self.tables("决策边界"), "步骤", "决策方")
        if found is None:
            res.findings.append(
                Finding("S4", "structure", "error", "「决策边界」里没有找到带「步骤」「决策方」两列的表格")
            )
            res.detail = "缺少决策边界表"
            return res
        header, rows = found
        idx = column_index(header, "决策方")
        counts = {k: 0 for k in DECISION_MAKERS}
        for r, row in enumerate(rows):
            line_no = self._row_line("决策边界", r)
            value = row[idx].strip() if idx is not None and idx < len(row) else ""
            if value.startswith("LLM"):
                counts["LLM"] += 1
            elif value.startswith("代码"):
                counts["代码"] += 1
            else:
                res.findings.append(
                    Finding("S4", "content", "error",
                            f"决策方只能是 LLM 或代码，实际写的是「{value or '空'}」", line_no)
                )
        for maker in DECISION_MAKERS:
            if counts[maker] == 0:
                res.findings.append(
                    Finding("S4", "content", "error",
                            f"决策边界里没有任何一行由 {maker} 决定——两类都必须有")
                )
        res.detail = f"LLM {counts['LLM']} 行，确定性代码 {counts['代码']} 行"
        return res

    # ------------------------------------------------------------------ S5

    def check_tools(self) -> CheckResult:
        res = CheckResult("S5", "工具清单规整", "")
        found = find_table(self.tables("可用工具清单"), "工具名")
        if found is None:
            res.findings.append(
                Finding("S5", "structure", "error", "「可用工具清单」里没有找到带「工具名」列的表格")
            )
            res.detail = "缺少工具清单表"
            return res
        header, rows = found
        idx_side = column_index(header, "副作用")
        names: list[str] = []
        tiers: dict[str, str] = {}
        for r, row in enumerate(rows):
            line_no = self._row_line("可用工具清单", r)
            name = row[0].strip() if row else ""
            if not name:
                res.findings.append(
                    Finding("S5", "content", "error", "工具名不能为空", line_no)
                )
                continue
            if name in names:
                res.findings.append(
                    Finding("S5", "content", "error", f"工具名「{name}」重复", line_no)
                )
            names.append(name)
            side = row[idx_side].strip() if idx_side is not None and idx_side < len(row) else ""
            if side not in SIDE_EFFECT_TIERS:
                res.findings.append(
                    Finding("S5", "content", "error",
                            f"工具「{name}」的副作用一栏只能是 {'/'.join(SIDE_EFFECT_TIERS)}，实际是「{side or '空'}」",
                            line_no)
                )
            else:
                tiers[name] = side
        if not rows:
            res.findings.append(
                Finding("S5", "content", "error", "工具清单是空的——没有工具的 Agent 不需要 Agent")
            )
        res.detail = f"{len(names)} 个工具，其中不可逆写 {sum(1 for t in tiers.values() if t == '不可逆写')} 个"
        return res

    # ------------------------------------------------------------------ S6

    def check_irreversible(self) -> CheckResult:
        """不可逆写工具必须被点名排除，或者由确定性代码把持。"""
        res = CheckResult("S6", "不可逆写操作受控", "")
        found = find_table(self.tables("可用工具清单"), "工具名")
        irreversible: list[str] = []
        if found:
            header, rows = found
            idx_side = column_index(header, "副作用")
            for row in rows:
                if idx_side is not None and idx_side < len(row):
                    side = row[idx_side].strip()
                    # 副作用一栏写错档位（比如写成"有副作用"）时，只要出现"不可逆"三个字，
                    # 也要按不可逆写处理——不能让"写错档位"变成"绕过检查"。
                    if side and ("不可逆" in side or side == "不可逆写") and row and row[0].strip():
                        irreversible.append(row[0].strip())
        if not irreversible:
            res.detail = "没有不可逆写工具"
            return res

        nogo_text = self.body_text("明确不做的事")
        boundary = find_table(self.tables("决策边界"), "步骤", "决策方") or ([], [])
        b_header, b_rows = boundary
        b_idx = column_index(b_header, "决策方") if b_header else None
        code_guarded: set[str] = set()
        for row in b_rows:
            if b_idx is None or b_idx >= len(row):
                continue
            if row[b_idx].strip().startswith("代码"):
                for name in irreversible:
                    if name in " ".join(row):
                        code_guarded.add(name)

        for name in irreversible:
            if name in nogo_text or name in code_guarded:
                continue
            res.findings.append(
                Finding("S6", "content", "error",
                        f"不可逆写工具「{name}」既没有写进「明确不做的事」，也没有在「决策边界」里由代码把持")
            )
        res.detail = f"{len(irreversible)} 个不可逆写工具，受控 {len(irreversible) - len(res.errors)} 个"
        return res

    # ------------------------------------------------------------------ S7

    def check_statuses(self) -> CheckResult:
        res = CheckResult("S7", "退出状态覆盖契约全集", "")
        text = self.body_text("失败定义与退出状态")
        for status in CONTRACT_STATUSES:
            if status not in text:
                res.findings.append(
                    Finding("S7", "content", "error",
                            f"没有定义退出状态 `{status}`——它与 AgentResult.status 一一对应，少一个就等于没定义失败")
                )
        res.detail = f"{len(CONTRACT_STATUSES) - len(res.errors)}/{len(CONTRACT_STATUSES)} 个退出状态"
        return res

    # ------------------------------------------------------------------ S8

    def check_non_goals(self) -> CheckResult:
        res = CheckResult("S8", "明确不做的事非空", "")
        items = bullets(self.body("明确不做的事"))
        if not items:
            res.findings.append(
                Finding("S8", "content", "error", "「明确不做的事」是空的——不写不做什么，后面所有加功能的争论都没有依据")
            )
        res.detail = f"{len(items)} 条不做的事"
        return res

    # ------------------------------------------------------------------ S9

    def check_budget(self) -> CheckResult:
        res = CheckResult("S9", "预算约束齐备", "")
        text = self.body_text("预算与约束")
        for key in BUDGET_KEYS:
            if key not in text:
                res.findings.append(
                    Finding("S9", "content", "error", f"没有写明 `{key}`——它对应 Config 的同名字段")
                )
        res.detail = f"{len(BUDGET_KEYS) - len(res.errors)}/{len(BUDGET_KEYS)} 个预算键"
        return res

    # ------------------------------------------------------------------ 辅助

    def _row_line(self, section: str, row_index: int) -> int | None:
        """在章节正文里找到第 N 行表格数据的行号（1 起）。"""
        seen = -1
        for i, line in enumerate(self.body(section), start=1):
            s = line.strip()
            if s.startswith("|") and s.endswith("|"):
                cells = [c.strip() for c in s.strip("|").split("|")]
                if is_separator_row(cells):
                    continue
                if seen == 0:  # 表头
                    seen = 1
                    continue
                seen += 1
                if seen - 1 == row_index:
                    return i
        return None

    # ------------------------------------------------------------------ 运行

    def run(self, mode: str) -> list[CheckResult]:
        checks = [
            self.check_sections(),
            self.check_placeholders(),
            self.check_criteria(),
            self.check_decision_boundary(),
            self.check_tools(),
            self.check_irreversible(),
            self.check_statuses(),
            self.check_non_goals(),
            self.check_budget(),
        ]
        if mode == "template":
            # 模板模式只看结构：占位符与取值合法性是"填完之后"才该关心的事。
            for c in checks:
                c.findings = [f for f in c.findings if f.phase == "structure"]
                if c.rule in CONTENT_ONLY_CHECKS:
                    c.detail = "内容校验，模板模式跳过"
        return checks


# --------------------------------------------------------------------------- 入口


def marker(ok: bool) -> str:
    """状态标记。终端编码装不下 emoji 时退回 ASCII，保证任何机器上都能跑。"""
    good, bad = "✅", "❌"
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        good.encode(encoding)
        bad.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return "[OK]" if ok else "[FAIL]"
    return good if ok else bad


def render_text(path: Path, mode: str, checks: list[CheckResult]) -> str:
    lines = [f"规格检查：{path}", f"模式：{mode}", ""]
    errors = 0
    for c in checks:
        lines.append(f"  {marker(c.ok)} {c.rule} {c.title}：{c.detail}")
        for f in c.findings:
            if f.level == "error":
                errors += 1
            lines.append(f"       - [{f.level}] {f.render()}")
    lines.append("")
    if errors:
        lines.append(f"结论：不通过（{len(checks)} 项检查，{errors} 个错误）")
    elif mode == "template":
        lines.append(f"结论：结构完整，可以开始填写（{len(checks)} 项结构检查，0 个错误）")
    else:
        lines.append(f"结论：通过（{len(checks)} 项检查，0 个错误）")
    return "\n".join(lines)


def render_json(path: Path, mode: str, checks: list[CheckResult]) -> str:
    payload = {
        "spec": str(path),
        "mode": mode,
        "ok": all(c.ok for c in checks),
        "checks": [
            {
                "rule": c.rule,
                "title": c.title,
                "detail": c.detail,
                "ok": c.ok,
                "findings": [
                    {"level": f.level, "message": f.message, "line": f.line}
                    for f in c.findings
                ],
            }
            for c in checks
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_spec",
        description="检查一份 Agent 需求规格是否可验收",
    )
    parser.add_argument("spec", help="规格文件路径")
    parser.add_argument(
        "--mode",
        choices=("spec", "template"),
        default="spec",
        help="spec=检查成品规格（默认）；template=只检查模板结构",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    args = parser.parse_args(argv)

    path = Path(args.spec)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读取失败：{exc}", file=sys.stderr)
        return 2

    checker = SpecChecker(path, text)
    checks = checker.run(args.mode)

    if args.json:
        print(render_json(path, args.mode, checks))
    else:
        print(render_text(path, args.mode, checks))

    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
