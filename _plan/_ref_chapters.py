#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第二卷正文里的**第一卷交叉引用**核对。

第二卷大量出现「第一卷第 33 章《可观测性：Trace、Span 与 Agent 专属指标》」
这样的引用。章号或标题错一个，读者翻书就找不到——这种错误肉眼极难发现，
因为读起来完全通顺。

本脚本做三件事：
  1. 抽出第二卷正文里所有「第一卷第 N 章《标题》」引用；
  2. 与第一卷 40 章的真实章号 + 标题比对（真相源是 _plan/chapters.json）；
  3. 报告章号错、标题错、引用了不存在的章。

只读，不改任何文件。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "_plan"
VOL2 = ROOT / "vol2"

sys.stdout.reconfigure(encoding="utf-8")

# 「第一卷第 33 章《可观测性：…》」
REF_RE = re.compile(r"第一卷第\s*(\d+)\s*章\s*[《【]\s*([^》】]{2,60})\s*[》】]")
# 「第一卷第 6、26、29 章」这种并列写法
REF_MULTI_RE = re.compile(r"第一卷第\s*([\d、,，\s]+?)\s*章(?![《【])")


def load_vol1() -> dict[int, str]:
    """{章号: 标题}，取自 chapters.json 这个唯一真相源。"""
    spec = json.loads((PLAN / "chapters.json").read_text(encoding="utf-8"))
    out: dict[int, str] = {}
    if isinstance(spec, dict):
        spec = spec.get("chapters", [])
    for c in spec:
        no = c.get("no") or c.get("number")
        if no is None:
            m = re.search(r"(\d+)", str(c.get("id", "")))
            no = int(m.group(1)) if m else None
        title = c.get("title", "")
        if no is not None:
            out[int(no)] = title
    return out


def norm_title(t: str) -> str:
    """标题比对用的归一化：去掉空白与中英文标点差异。"""
    t = re.sub(r"\s+", "", t)
    t = t.replace("：", ":").replace("，", ",").replace("、", ",")
    return t.lower()


def main() -> int:
    vol1 = load_vol1()
    print(f"第一卷章目：{len(vol1)} 章\n")

    exact = 0
    short = 0
    problems: list[str] = []

    for md in sorted(VOL2.glob("p*/*.md")):
        chapter = md.parent.name
        text = md.read_text(encoding="utf-8")

        for m in REF_RE.finditer(text):
            no = int(m.group(1))
            quoted = m.group(2).strip()
            if no not in vol1:
                problems.append(f"  [{chapter}] 引用了不存在的章："
                                f"第一卷第 {no} 章《{quoted}》")
                continue
            real = vol1[no]
            if norm_title(quoted) == norm_title(real):
                exact += 1
            elif norm_title(real).startswith(norm_title(quoted)):
                # 简称：读者按书末索引仍能查到，不算错
                exact += 1
                short += 1
            else:
                problems.append(f"  [{chapter}] 第一卷第 {no} 章标题不符："
                                f"正文写《{quoted}》，实际是《{real}》")

        for m in REF_MULTI_RE.finditer(text):
            nums = [int(x) for x in re.findall(r"\d+", m.group(1))]
            for no in nums:
                if no not in vol1:
                    problems.append(f"  [{chapter}] 引用了不存在的章：第一卷第 {no} 章")

    print(f"带标题的引用核对正确 {exact} 处（其中 {short} 处为简称，读者按索引可查）")
    if problems:
        print(f"\n需要修正 {len(problems)} 处：")
        for p in problems:
            print(p)
    else:
        print("章号与标题全部对得上")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
