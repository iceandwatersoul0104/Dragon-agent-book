# -*- coding: utf-8 -*-
"""附录与前置材料的完整性体检：表格列数一致性、列数异常、空表、重复表头。"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"D:\Creater\Dragon_agent\agent-book")
VOL1 = ROOT / "vol1"

files = sorted(VOL1.glob("00-*.md")) + sorted((VOL1 / "appendix").glob("*.md"))
problems = []
for p in files:
    lines = p.read_text(encoding="utf-8").splitlines()
    tables = []
    cur = []
    for i, ln in enumerate(lines, 1):
        if ln.startswith("|"):
            cur.append((i, ln))
        else:
            if len(cur) >= 2:
                tables.append(cur)
            cur = []
    if len(cur) >= 2:
        tables.append(cur)
    for t in tables:
        widths = []
        for i, ln in t:
            # 去掉首尾竖线后按 | 分列
            cells = [c for c in ln.strip().strip("|").split("|")]
            widths.append((i, len(cells), ln))
        n = widths[0][1]
        bad = [(i, w, ln[:70]) for i, w, ln in widths if w != n]
        if bad:
            problems.append((p.name, widths[0][0], n, bad))
        # 分隔行检查
        if len(widths) > 1 and not re.match(r"^\|[\s:|-]+\|$", widths[1][2].strip()):
            problems.append((p.name, widths[1][0], n, [(widths[1][0], widths[1][1], "分隔行格式异常")]))
    # 空表：只有表头没有数据行
    for t in tables:
        if len(t) < 3 and t[0][0] == t[-1][0]:
            problems.append((p.name, t[0][0], 0, [("只有表头，无数据行")]))

print(f"检查 {len(files)} 篇附录/前置材料")
if not problems:
    print("全部表格结构正常（列数一致、分隔行合法、无空表）")
for f, line, n, bad in problems:
    print(f"  {f}:{line}  期望 {n} 列")
    for b in bad[:4]:
        print(f"      L{b[0]} ({b[1]} 列) {b[2]}")
