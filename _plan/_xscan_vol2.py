#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第二卷跨章扫描：重复段落、术语变体、空话套话。

第一卷的 `_editorial_scan.py` 里 E/F 两项（跨章重复、术语变体）逻辑可复用，
但它的 `all_docs()` 硬编码了 vol1 的路径，所以这里写一份针对 vol2 的。

检查项：
  A. 跨章大段重复：同一段（≥70 汉字、去掉代码块）出现在多个章节里
  B. 术语变体：同一概念在正文里出现了两种以上的叫法
  C. 空话套话：第一卷明令少用的营销词
  D. 术语表覆盖：正文用到但附录 C 术语表里没有登记的中文术语（自造叫法的苗头）

只读，不改任何文件。
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOL2 = ROOT / "vol2"
APPX_C = ROOT / "vol1" / "appendix" / "C-术语表.md"

sys.stdout.reconfigure(encoding="utf-8")

# B 术语变体：规范名 -> 正文里可能出现的其它叫法
#
# 只收**真会引发歧义**的替身。2026-11-08 首轮跑完后剔除过这些误报：
#   · "守卫" —— 正文里的守卫全是"预算守卫"/"成本守卫"，第一卷第 11 章章名
#     就是《停止准则、步数上限与成本守卫》，是既有术语，不是"护栏"的别称。
#   · "编排层"、"记忆层"、"记忆模块"、"长期记忆"、"幂等键"、"提示注入"
#     —— 这些是规范名的下位说法或具体实现，不是同义替换。
#   · "评估" —— 第一卷用的就是"评估污染"（Evaluation Contamination），
#     与第二卷的"评测"（evaluation 流程）不是同一件事，不算冲突。
VARIANTS = {
    "检查点": [("断点", ("Temporal", "持久化执行", "Durable Execution")),
               ("存档点", ()), ("快照点", ())],
    "护栏": [("防护栏", ()), ("安全栏", ()), ("闸门", ())],
    "编排": [("编排引擎", ())],
    "沙箱": [("沙盒", ()), ("隔离容器", ())],
    "注入": [("注入攻击", ()), ("注入面", ())],
}

# 已核实为合理用法的替身——讲外部项目时沿用它自己的术语，不算自造叫法。
#   "重放"：指 tools.py 的 replayable 属性（工具能不能重放），与"回放"
#           （replay 一次运行）是两个概念，混用反而更危险。
ALLOWED_PAIRS = {
    "重放": "：工具 replayable 属性，与「回放」是不同概念",
    "编排器": "：指代码里的 Orchestrator 实体，不是「编排」概念的下位说法",
    "编排层": "：第二卷第 10 章的正式章名，指编排这一层本身",
}

# C 空话套话：第一卷定下的少用词表
HOLLOW = ["赋能", "闭环", "范式革新", "生态位", "抓手", "拉通", "沉淀", "打法",
          "心智", "组合拳", "全方位", "多维度", "一站式", "无缝", "极致",
          "颠覆", "革命性", "重塑", "引领"]

# 命中处若落在引号、书名号或反引号里，多半是**具体语境或书名/章名引用**，
# 不是作者在用营销词（例如「故障单 FD-2291 已闭环」是业务句子，
# 《Agent 的定义、边界与心智模型》是第一卷章名）。这类不计问题。
QUOTED = re.compile(r"[`「」《》\"“”]")

# 正文里常见的"新造术语"苗头：形如 XXX化 / XXX器 / XXX层 的中文复合词
COMPOUND = re.compile(r"[一-鿿]{2,4}(?:化|器|层|流|域|面|体|式)(?![一-鿿])")


def load_docs() -> list[tuple[Path, str]]:
    docs = []
    for p in sorted(VOL2.glob("p*/*.md")):
        if p.name == "README.md":
            continue
        docs.append((p, p.read_text(encoding="utf-8")))
    return docs


def prose_only(text: str) -> str:
    """去掉代码块与表格行，只留正文段落。"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    keep = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("|") or s.startswith("```"):
            continue
        keep.append(s)
    return "\n".join(keep)


def main() -> int:
    docs = load_docs()
    print(f"受检文档 {len(docs)} 篇\n")

    # ---------------------------------------------------------- A 跨章重复
    para_map: dict[str, list[str]] = defaultdict(list)
    for p, t in docs:
        chapter = p.parent.name
        body = prose_only(t)
        for para in re.split(r"\n\s*\n", body):
            key = re.sub(r"\s+", "", para)
            if len(re.findall(r"[\u4e00-\u9fff]", key)) >= 70:
                para_map[key].append(chapter)
    dups = {k: v for k, v in para_map.items() if len(set(v)) > 1}

    print(f"=== A. 跨章重复段落：{len(dups)} 处 ===")
    for k, v in list(dups.items())[:10]:
        print(f"  {sorted(set(v))}  {k[:80]}")

    # ---------------------------------------------------------- B 术语变体
    print("\n=== B. 术语变体 ===")
    term_hits = 0
    for canon, alts in VARIANTS.items():
        for alt, allow_ctx in alts:
            hits = []
            allowed = []
            for p, t in docs:
                body = prose_only(t)
                if alt not in body:
                    continue
                if any(ctx in body for ctx in allow_ctx):
                    allowed.append(p.parent.name)
                else:
                    hits.append(p.parent.name)
            if hits:
                term_hits += 1
                print(f"  ✗ 「{canon}」vs「{alt}」  {hits}")
            if allowed:
                print(f"  ✓ 「{alt}」{alts and ''}"
                      f"（{'/'.join(allow_ctx)} 语境，沿用外部项目原词）  {allowed}")
    for word, why in ALLOWED_PAIRS.items():
        where = [p.parent.name for p, t in docs if word in prose_only(t)]
        if where:
            print(f"  ✓ 「{word}」{why}  {where}")
    if not term_hits:
        print("  （无）")

    # ---------------------------------------------------------- C 空话套话
    print("\n=== C. 空话套话 ===")
    hollow_hits = 0
    for p, t in docs:
        body = prose_only(t)
        for w in HOLLOW:
            start = 0
            while True:
                idx = body.find(w, start)
                if idx == -1:
                    break
                start = idx + len(w)
                around = body[max(0, idx - 30):idx + 30]
                if QUOTED.search(around):
                    continue  # 具体语境或书名引用，不计
                hollow_hits += 1
                print(f"  [{p.parent.name}] 「{w}」  …{around.strip()}…")
    if not hollow_hits:
        print("  （无）")

    # ---------------------------------------------------------- D 术语登记
    # 第二卷必然引入第一卷没有的工程术语（装配器、评分器、判定器……）。
    # 终稿时要把它们补进附录 C，否则读者查不到。
    #
    # 判据用全书体例本身：术语首次出现必须写成「中文（English）」。
    # 抓这个形式的中文词，看它在附录 C 里登没登记——比正则乱猜复合词准得多。
    print("\n=== D. 第二卷新术语 × 附录 C 登记状态 ===")
    glossary = ""
    if APPX_C.exists():
        glossary = APPX_C.read_text(encoding="utf-8")
    registered = {m.group(1) for m in
                  re.finditer(r"^\|\s*([^\s|]+)\s*\|", glossary, re.M)}
    TERM_PAIR = re.compile(r"([\u4e00-\u9fff]{2,6})（([A-Z][A-Za-z0-9 \.\-]{1,40})）")
    # 中文侧含这些虚词的多半是固定搭配（「……的规格（Spec）」），不是术语
    STOPWORD = re.compile(r"[的是用把而并或在从对与和为及等中后前上下]")
    cand: dict[str, set[str]] = defaultdict(set)
    for p, t in docs:
        body = prose_only(t)
        for m in TERM_PAIR.finditer(body):
            cn, en = m.group(1), m.group(2)
            if len(cn) < 2 or len(en) < 2:
                continue
            if STOPWORD.search(cn):
                continue
            cand[cn].add(p.parent.name)
    missing = {w: chs for w, chs in cand.items() if w not in registered}
    # 正则的中文侧最多吃 6 个字，会把「接成回归测试」这种动词+术语的组合
    # 整段抓下来，于是「回归测试」明明登记了也会被报成待补。
    # 判据：抓到的串只要有一个**后缀**（长度 2..6）已登记，就算已登记。
    def is_registered(word: str) -> bool:
        if word in registered:
            return True
        return any(word[i:] in registered
                   for i in range(1, len(word) - 1)
                   if len(word) - i >= 2)
    missing = {w: chs for w, chs in missing.items() if not is_registered(w)}
    if not missing:
        print("  （无待补）")
    else:
        for w, chs in sorted(missing.items(), key=lambda kv: -len(kv[1])):
            print(f"  {w:<10} {sorted(chs)}")
        print(f"  → 这些词需在终稿时补进附录 C 术语表（共 {len(missing)} 个）")

    print(f"\n合计：跨章重复 {len(dups)} 处，术语变体 {term_hits} 处，"
          f"空话 {hollow_hits} 处，待补登记术语 {len(missing)} 个")
    return 1 if (dups or term_hits or hollow_hits) else 0


if __name__ == "__main__":
    raise SystemExit(main())
