#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把第二卷补进附录 D《全书索引》的三个索引。

附录 D 叫"全书索引"，但原本只索引第一卷 40 章——读者拿着它查第二卷的
章节、项目和配套代码，全查不到。这里把三张表都补齐：

  一、章节速查表      追加第二卷 14 章
  三、项目索引        追加第二卷正文实际引用的仓库（从正文扫 URL 得来）
  五、配套代码目录索引 追加第二卷 14 章对应的 dragon-agent 模块

幂等：重复跑按"章号 / 项目名"判重，不会重复插入。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "_plan"
INDEX = ROOT / "vol1" / "appendix" / "D-全书索引.md"

sys.stdout.reconfigure(encoding="utf-8")

# 第二卷 14 章的「本章解决的核心问题」与配套模块
V2_ROWS = [
    ("立项：把想法变成一份可执行的需求",
     "把\"做个代码审查 Agent\"这句话变成可执行、可验收的规格：判据、范围、"
     "验证方式都要写死，否则评审永远在吵\"做对了算不算\"。",
     "`specs/check_spec.py`"),
    ("工程骨架：目录、配置与错误处理约定",
     "目录怎么分、配置从哪来、错误怎么分类、日志怎么写——决定了凌晨两点你能不能"
     "查到一个任务到底跑了什么、用的哪套配置。",
     "`dragon_agent/config.py` 等 3 个叶子模块"),
    ("模型接入抽象：让 mock 与真实 API 共存",
     "让 mock 与真实 API 返回**同一种**东西，业务代码就不必长出 isinstance 分支；"
     "否则测试里跑通的那半边分支，线上永远没人走过。",
     "`dragon_agent/llm.py`"),
    ("工具层：注册、校验、错误回灌与副作用",
     "工具失败是**结果**不是异常：参数校验、幻觉工具名、文件不存在、重复副作用，"
     "四类线上事故共用同一个根因。",
     "`dragon_agent/tools.py`"),
    ("上下文装配器：把提示词变成资源分配",
     "提示词不是拼出来的字符串，是要分配的预算：分层、排序、裁剪各占多少，"
     "决定了模型还看不看得见你写在最前面的约束。",
     "`dragon_agent/context.py`"),
    ("Agent 主循环：带预算守卫的 ReAct",
     "主循环不是\"循环\"，它是一组**停止准则**：步数上限、异常收敛、无进展检测、"
     "预算守卫，缺一条就烧一次钱。",
     "`dragon_agent/agent.py`"),
    ("状态与检查点：让任务可回放、可恢复",
     "把状态显式化：崩溃后能接着跑、事后能复现、副作用不重复执行——"
     "这三条各自对应一类看不见的代价。",
     "`dragon_agent/state.py`"),
    ("记忆模块：把\"记住\"做成可控的读写",
     "区分工作/情景/语义/程序性四类记忆，明确写入时机，"
     "并让\"该想起时想起了\"可被评测，而不只是看检索命中率。",
     "`dragon_agent/memory.py`"),
    ("护栏与权限：能力最小化",
     "工具分级、二次确认、注入检测、越权约束怎么落地，"
     "以及为什么这套防线不是保险箱——残余风险要诚实说出来。",
     "`dragon_agent/guardrails.py`"),
    ("编排层：从单 Agent 到多 Agent 的升级路径",
     "什么时候才该上多 Agent：并行性、上下文隔离、能力异构、权限隔离四判据全中"
     "才升级，选哪种拓扑、怎么熔断、怎么退回单 Agent。",
     "`dragon_agent/orchestration.py`"),
    ("协议接入：把你的 Agent 变成 MCP Server 与 A2A 节点",
     "把同一批工具同时暴露为 MCP 与内部调用，别维护两套实现；"
     "以及 A2A 的委托语义与协议演进怎么接才不被锁死。",
     "`dragon_agent/mcp_server.py`"),
    ("可观测性：接上 trace、指标与成本账",
     "trace、指标、成本账是三件事：采样会污染指标所以指标要全量算，"
     "脱敏必须在采集端做因为落盘即泄露。",
     "`dragon_agent/observability.py`"),
    ("评测与回归：让改动有依据",
     "建任务集与判定器，让\"改了一句工具描述\"这种改动能被归因；"
     "能用确定性断言的，绝不用模型判定。",
     "`dragon_agent/evaluation.py`"),
    ("上线：容器化、成本控制与值班手册",
     "前 13 章做完的是\"能跑\"，上线要回答\"跑在哪儿、跑多快、花多少、坏了谁管\"："
     "镜像、密钥、限流、成本护栏与值班手册。",
     "`dragon_agent/cli.py`"),
]

URL_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


def scan_v2_urls() -> dict[str, set[str]]:
    """扫第二卷正文，得出 {仓库路径: {出现该仓库的章}}。"""
    v2 = json.loads((PLAN / "chapters-vol2.json").read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for i, c in enumerate(v2["chapters"], 1):
        chap = f"第二卷第 {i} 章"
        for md in (ROOT / "vol2" / c["code_dir"]).glob("*.md"):
            text = md.read_text(encoding="utf-8")
            for m in URL_RE.finditer(text):
                owner, repo = m.group(1), m.group(2)
                repo = re.sub(r"\.git$", "", repo)
                out.setdefault(f"{owner}/{repo}", set()).add(chap)
    return out


def section_lines(lines: list[str], section: str) -> list[str]:
    """取某个 ## 小节内的所有行（不含标题行）。"""
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == section)
    except StopIteration:
        return []
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return lines[start + 1:end]


def insert_before(lines: list[str], section: str, rows: list[str]) -> int:
    """把 rows 插到指定 ## 小节的表格末尾。返回实际插入条数。"""
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == section)
    except StopIteration:
        print(f"  找不到小节：{section}")
        return 0
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    at = end
    while at > start and not lines[at - 1].strip():
        at -= 1
    lines[at:at] = rows
    return len(rows)


def main() -> int:
    lines = INDEX.read_text(encoding="utf-8").splitlines()
    added = 0

    # ---- 一、章节速查表 ----
    existing = {ln.split("|")[1].strip()
                for ln in section_lines(lines, "## 一、章节速查表")
                if ln.startswith("| 第") and len(ln.split("|")) > 2}
    rows = []
    for i, (title, core, code) in enumerate(V2_ROWS, 1):
        key = f"第二卷第 {i} 章"
        if key in existing:
            continue
        rows.append(f"| {key} | {title} | {core} | {code} |")
    n = insert_before(lines, "## 一、章节速查表", rows)
    added += n
    print(f"  一、章节速查表    +{n} 行")

    # ---- 三、项目索引 ----
    proj_rows = section_lines(lines, "## 三、项目索引")
    have = {ln.split("|")[1].strip().lower() for ln in proj_rows
            if ln.startswith("| ") and len(ln.split("|")) > 2}
    rows = []
    for path, chs in sorted(scan_v2_urls().items()):
        if path.lower() in have:
            continue
        name = path.split("/")[-1]
        if name.lower() in have:
            continue
        rows.append(f"| {name} | https://github.com/{path} | {'、'.join(sorted(chs))} |")
    n = insert_before(lines, "## 三、项目索引", rows)
    added += n
    print(f"  三、项目索引      +{n} 行")

    # ---- 五、配套代码目录索引 ----
    code_existing = {ln.split("|")[1].strip()
                     for ln in section_lines(lines, "## 五、配套代码目录索引")
                     if ln.startswith("| ") and len(ln.split("|")) > 2}
    rows = []
    for i, (title, _core, code) in enumerate(V2_ROWS, 1):
        key = f"第二卷第 {i} 章"
        if key in code_existing:
            continue
        rows.append(f"| {key} | `dragon-agent/{code.strip('`')}` | "
                    f"README.md、模块与配套测试、示例脚本 |")
    n = insert_before(lines, "## 五、配套代码目录索引", rows)
    added += n
    print(f"  五、代码目录索引  +{n} 行")

    INDEX.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n共新增 {added} 行 → {INDEX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
