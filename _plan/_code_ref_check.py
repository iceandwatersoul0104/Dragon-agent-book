#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第二卷正文代码块 × dragon-agent 仓库文件 的一致性核对。

第二卷的额外契约：**正文里的代码必须是 dragon-agent/ 里的真实文件，逐字一致**。
本脚本做机器可判定的部分：

  1. 抽出正文里每个 ``` 代码块；
  2. 推断它对应的仓库文件（块内出现的 dragon_agent/xxx.py 路径、
     或 from dragon_agent.X import / import dragon_agent.X）；
  3. 把代码块里的每一行在该文件中找（允许缩进差异，逐行判定）；
  4. 报告「完全命中 / 部分命中 / 找不到对应文件」。

判据说明：
  - 空行、注释行、`# ` 开头的分隔线不参与判定（它们可能为了讲解而增写）。
  - 判定是**逐行**的：只要块里有一行在该文件中不存在，就记为「不一致行」。
  - 找不到对应文件时只报告，不臆断——那通常意味着正文引用了仓库里没有的东西，
    需要人工看一眼。

只读，不改任何文件。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOL2 = ROOT / "vol2"
DRAGON = ROOT / "dragon-agent"

sys.stdout.reconfigure(encoding="utf-8")

CODE_BLOCK = re.compile(r"```(\w*)\n(.*?)```", re.S)
PATH_HINT = re.compile(r"dragon_agent/([a-z_]+\.py)")
IMPORT_HINT = re.compile(r"(?:from|import)\s+dragon_agent\.([a-z_]+)")

# 这些行不参与逐行比对
SKIP_LINE = re.compile(r"^\s*(#\s*[-=*]{3,}\s*$|[-=*]{3,}\s*$)")

# 反例标记：正文明确说这段是「反面教材」时，它本就不该出现在仓库里，
# 不能判成「正文与仓库不一致」。
ANTI_MARKERS = ("反面教材", "反面示例", "反例", "错误示范", "别这么写", "不要这样写",
                "不该出现在", "这样写是错的", "不要这么写", "反面例子")

# 示意块标记：ASCII 框线图、箭头流程、含省略号的签名摘要、伪代码。
# 这些是表达手段，不是仓库里的原样代码。
DIAGRAM_CHARS = set("┌┐└┘├┤┬┴┼─│╔╗╚╝║═▼▲◄►◀▶→←⇒⟶")
# 省略号：整行是 ...，或行尾是 ...（签名摘要写法）
ELLIPSIS_LINE = re.compile(r"\.\.\.\s*$")
ARROW_LINE = re.compile(r"──|──►|───|×|÷")


def classify(body: str, ctx_before: str, ctx_after: str = "") -> str:
    """给代码块分类：'code' / 'anti' / 'sketch'。"""
    around = ctx_before + "\n" + ctx_after
    if any(m in around for m in ANTI_MARKERS):
        return "anti"
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return "sketch"
    if any(ELLIPSIS_LINE.search(ln) for ln in lines):
        return "sketch"
    if any(set(ln) & DIAGRAM_CHARS for ln in lines):
        return "sketch"
    if any(ARROW_LINE.search(ln) for ln in lines):
        return "sketch"
    if "伪代码" in around or "pseudo" in body.lower():
        return "sketch"
    # 终端输出的键值行（ok=... error=...）不是源码
    if sum(1 for ln in lines if re.search(r"\w+=\S", ln)) >= max(1, len(lines) // 2):
        return "sketch"
    return "code"


def load_sources() -> dict[str, str]:
    """{相对路径: 文件内容} —— 覆盖包内模块、示例、规格检查器与测试。

    正文里引用的代码不一定在 dragon_agent/ 包里：第 1 章引的是
    specs/check_spec.py，第 2 章引的是 tests/ 里的断言。正文与仓库
    「逐字一致」的要求对它们一视同仁，所以全都纳入比对源。
    """
    out: dict[str, str] = {}
    for sub in ("dragon_agent", "examples", "specs", "tests"):
        d = DRAGON / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.py")):
            key = f"{sub}/{f.name}"
            out[key] = f.read_text(encoding="utf-8")
    return out


def norm(line: str) -> str:
    """比对用的归一化：去首尾空白、去行尾分号。"""
    return line.strip().rstrip(";").strip()


def candidate_modules(block: str, ctx_before: str) -> list[str]:
    """从代码块及其上文推断候选模块名。"""
    cands: list[str] = []
    for m in PATH_HINT.finditer(block):
        if m.group(1) not in cands:
            cands.append(m.group(1))
    for m in IMPORT_HINT.finditer(block):
        name = m.group(1)
        if f"{name}.py" not in cands:
            cands.append(f"{name}.py")
    for m in PATH_HINT.finditer(ctx_before):
        if m.group(1) not in cands:
            cands.append(m.group(1))
    return cands


def main() -> int:
    sources = load_sources()
    src_sets = {k: {norm(x) for x in v.splitlines()} for k, v in sources.items()}
    print(f"已加载源文件 {len(sources)} 个："
          f"{', '.join(sorted(sources))}\n")

    total_blocks = 0
    matched = 0
    partial = 0
    orphan = 0
    skipped = 0
    anti = 0
    sketch = 0
    problem_rows: list[str] = []

    for md in sorted(VOL2.glob("p*/*.md")):
        text = md.read_text(encoding="utf-8")
        chapter = md.parent.name
        blocks = list(CODE_BLOCK.finditer(text))
        if not blocks:
            continue
        b_ok = b_part = b_orphan = b_skip = b_anti = b_sketch = 0
        for m in blocks:
            lang = m.group(1)
            body = m.group(2)
            # 只有 Python 块才与源文件比对；json/yaml/toml/sh/text 是配置、
            # 命令或终端输出，不是 Python 源码，逐字一致性对它们无意义。
            if lang not in ("", "python", "py"):
                b_skip += 1
                skipped += 1
                continue
            ctx_before = text[max(0, m.start() - 900):m.start()]
            ctx_after = text[m.end():m.end() + 300]
            kind = classify(body, ctx_before, ctx_after)
            if kind == "anti":
                b_anti += 1
                anti += 1
                continue
            if kind == "sketch":
                b_sketch += 1
                sketch += 1
                continue
            total_blocks += 1
            lines = [ln for ln in body.splitlines()
                     if norm(ln) and not SKIP_LINE.match(ln)]
            if not lines:
                continue
            # 选覆盖最多行的那份源文件——不依赖正文里是否写了路径
            best_key, best_hits = None, 0
            for key, lineset in src_sets.items():
                hits = sum(1 for ln in lines if norm(ln) in lineset)
                if hits > best_hits:
                    best_key, best_hits = key, hits
            if best_key is None:
                b_orphan += 1
                orphan += 1
                problem_rows.append(
                    f"  [{chapter}] 代码块与仓库任何文件都对不上"
                    f"（{len(lines)} 行，首行：{norm(lines[0])[:60]}）")
                continue
            missing = [ln for ln in lines
                       if norm(ln) not in src_sets[best_key]]
            if missing:
                b_part += 1
                partial += 1
                detail = " ｜ ".join(repr(norm(x))[:56] for x in missing[:3])
                problem_rows.append(
                    f"  [{chapter}] 与 {best_key} 不一致 {len(missing)}/{len(lines)} 行"
                    f"\n      缺: {detail}")
            else:
                b_ok += 1
                matched += 1
        if blocks:
            print(f"  {chapter:<26} 需逐字一致 {b_ok + b_part + b_orphan:>3}  "
                  f"一致 {b_ok:>2}  不一致 {b_part:>2}  对不上 {b_orphan:>2}"
                  f"  ｜ 示意 {b_sketch:>2} 反例 {b_anti:>2} 非 Python {b_skip:>2}")

    print(f"\n合计：需逐字一致的 Python 块 {total_blocks}，完全一致 {matched}，"
          f"不一致 {partial}，对不上 {orphan}；"
          f"另有示意块 {sketch}、反例块 {anti}、非 Python 块 {skipped}（不参与比对）")
    if problem_rows:
        print(f"\n需要人工确认的条目（{len(problem_rows)}）：")
        for r in problem_rows:
            print(r)
    return 1 if (partial or orphan) else 0


if __name__ == "__main__":
    raise SystemExit(main())
