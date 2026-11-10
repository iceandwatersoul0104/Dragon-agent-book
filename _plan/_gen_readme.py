# -*- coding: utf-8 -*-
"""从 chapters.json 重新生成 agent-book/README.md。

原 README 由一次性脚本生成，遗留两个 bug：
  1. 「配套代码」列把整个 chapter 对象插值成 $(@{id=ch12; ...}.code_dir)
  2. 部分章节的 md 文件名与 chapters.json 的 id-slug 规范不一致（链接会 404）

本脚本是唯一真相源（chapters.json）的确定性投影，可重复运行。
用法：python _gen_readme.py
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"D:\Creater\Dragon_agent\agent-book")
PLAN = ROOT / "_plan"

PART_TITLES = [
    ("part-0", "导论：什么是 Agent，以及为什么需要架构"),
    ("part-1", "单体 Agent 的核心模式"),
    ("part-2", "开源编排框架横向解剖"),
    ("part-3", "多 Agent 协作体系"),
    ("part-4", "记忆、上下文与状态"),
    ("part-5", "工程化：安全、可观测、评测与成本"),
    ("part-6", "前沿范式：代码智能体、GUI Agent、协议生态与自举"),
]

APPENDIX = [
    ("序言", "vol1/00-序言.md"),
    ("阅读路线图", "vol1/00-阅读路线图.md"),
    ("附录 A · 主流框架架构对比", "vol1/appendix/A-主流框架架构对比.md"),
    ("附录 B · 开源项目速查表", "vol1/appendix/B-开源项目速查表.md"),
    ("附录 C · 术语表", "vol1/appendix/C-术语表.md"),
    ("附录 D · 全书索引", "vol1/appendix/D-全书索引.md"),
    ("附录 E · 主编终审报告", "vol1/appendix/E-主编终审报告.md"),
    ("附录 F · 第二卷主编终审报告", "vol1/appendix/F-第二卷主编终审报告.md"),
]


def main():
    data = json.loads((PLAN / "chapters.json").read_text(encoding="utf-8"))
    chs = data["chapters"] if isinstance(data, dict) and "chapters" in data else data
    by_part = {p: [c for c in chs if c["part"] == p] for p, _ in PART_TITLES}

    L = []
    L.append("# 《开源 Agent 架构技术全书》\n")
    L.append('> 面向"准备自己动手造 Agent"的工程师的中文技术汇总书。')
    L.append("> 调研日期：2026 年 11 月。开源项目迭代很快，涉及具体 API 与版本时请以官方仓库为准。\n")

    L.append("## 本项目包含什么\n")
    L.append("| 目录 | 内容 |")
    L.append("| --- | --- |")
    L.append("| `vol1/` | **第一卷 · 架构原理与开源实现全景**（40 章 / 7 卷），每章固定七节结构 |")
    L.append("| `vol2/` | **第二卷 · 自建 Agent 实践手册**（14 章，主线工程 `dragon-agent/`） |")
    L.append("| `code/` | 与第一卷配套的可运行 Python 示例，mock 模式默认可离线运行，`--live` 切换真实 API |")
    L.append("| `dragon-agent/` | **第二卷的主线工程**：纯标准库实现的 Agent 框架，14 个模块 + 确定性测试 |")
    L.append("| `_plan/` | 本书的章节规格表与工作流脚本（可追溯的架构设计底稿） |")
    L.append("")

    L.append("## 第一卷 · 架构原理与开源实现全景\n")
    for part, title in PART_TITLES:
        L.append(f"### {title}\n")
        L.append("| 章 | 标题 | 配套代码 |")
        L.append("| --- | --- | --- |")
        for c in by_part[part]:
            num = c["id"].replace("ch", "")
            fname = f"{c['id']}-{c['slug']}.md"
            link = f"vol1/{part}/{fname}"
            cd = c.get("code_dir")
            code_cell = f"[`{cd}/`](code/{cd}/)" if cd else "—"
            L.append(f"| {num} | [{c['title']}]({link}) | {code_cell} |")
        L.append("")

    L.append("### 前置材料与附录\n")
    for name, path in APPENDIX:
        L.append(f"- [{name}]({path})")
    L.append("")

    L.append("## 每章的统一体例\n")
    L.append("1. 本章要解决的问题 — 用具体场景或反例开场")
    L.append("2. 原理：机制拆解 — 拆到能照着实现的颗粒度")
    L.append("3. 为什么这样设计：取舍与失效条件 — 什么时候**不该**用这个方案")
    L.append("4. 动手：最小可实现 — 可运行的最小实现")
    L.append("5. 开源实现对照 — 真实项目的做法与差异")
    L.append("6. 坑与检查清单 — 可勾选的落地要点")
    L.append("7. 小结与延伸阅读 — 真实可访问的 URL")
    L.append("")

    L.append("## 配套代码怎么跑\n")
    L.append("```powershell")
    L.append("# 单个示例（默认 mock 模式，不需要 API Key、不联网）")
    L.append(r"python .\code\ch04-react\main.py")
    L.append("")
    L.append("# 切换到真实 API（需自行配置环境变量）")
    L.append(r"python .\code\ch04-react\main.py --live")
    L.append("")
    L.append("# 批量跑通全部示例")
    L.append(r"python .\code\run_all.py")
    L.append("```")
    L.append("")

    # ------------------------------------------------------------ 第二卷
    v2_path = PLAN / "chapters-vol2.json"
    if v2_path.exists():
        v2 = json.loads(v2_path.read_text(encoding="utf-8"))
        v2chs = v2["chapters"]
        L.append("## 第二卷 · 自建 Agent 实践手册\n")
        L.append(f"> {v2.get('audience','')}\n")
        L.append("第二卷沿着一条主线推进：**把 `dragon-agent` 这个工程从零搭出来**。"
                 "14 章既是教程也是实现说明——正文里的每一段代码都能在工程目录里找到对应文件。\n")
        L.append("| 章 | 标题 | 对应工程模块 |")
        L.append("| --- | --- | --- |")
        mod_of = {
            "p01-scoping": "specs/", "p02-skeleton": "config.py", "p03-llm-client": "llm.py",
            "p04-tool-layer": "tools.py", "p05-context-assembler": "context.py",
            "p06-agent-loop": "agent.py, state.py", "p07-state-checkpoint": "state.py",
            "p08-memory": "memory.py", "p09-guardrails": "guardrails.py",
            "p10-orchestration": "orchestration.py", "p11-protocols": "mcp_server.py",
            "p12-observability": "observability.py", "p13-evaluation": "evaluation.py",
            "p14-production": "cli.py",
        }
        for i, c in enumerate(v2chs, 1):
            link = f"vol2/{c['code_dir']}/{c['slug']}.md"
            mod = mod_of.get(c["code_dir"], "—")
            L.append(f"| {i:02d} | [{c['title']}]({link}) | `{mod}` |")
        L.append("")
        L.append("### 主线工程 `dragon-agent/`\n")
        L.append("纯标准库实现的 Agent 框架，无第三方依赖，mock 模式离线可跑，`--live` 切真实 API。\n")
        L.append("```powershell")
        L.append("# 跑完整测试（不依赖网络）")
        L.append(r"cd .\dragon-agent; python -m unittest discover -s tests")
        L.append("")
        L.append("# 跑一次完整回路")
        L.append(r"python -m dragon_agent.cli demo")
        L.append("")
        L.append("# 列出可用工具")
        L.append(r"python -m dragon_agent.cli tools")
        L.append("```")
        L.append("")

    out = "\n".join(L)
    (ROOT / "README.md").write_text(out, encoding="utf-8", newline="\n")

    # 自检：README 里的每个 md 链接是否指向存在的文件（或尚未写出的目标）
    missing = []
    for part, _ in PART_TITLES:
        for c in by_part[part]:
            p = ROOT / "vol1" / part / f"{c['id']}-{c['slug']}.md"
            if not p.exists():
                missing.append(str(p.relative_to(ROOT)))
    if v2_path.exists():
        for c in json.loads(v2_path.read_text(encoding="utf-8"))["chapters"]:
            p = ROOT / "vol2" / c["code_dir"] / f"{c['slug']}.md"
            if not p.exists():
                missing.append(str(p.relative_to(ROOT)))
    print(f"README 已重新生成：{len(out)} 字符，{len(chs)} 章")
    print(f"尚待落盘的章节文件：{len(missing)} 个")
    if missing:
        print("  " + "、".join(missing))


if __name__ == "__main__":
    main()
