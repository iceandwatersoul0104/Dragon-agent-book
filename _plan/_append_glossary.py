#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把第二卷引入的新术语按主题归入附录 C 术语表。

不新开一节——术语表本来就是按主题组织的，第二卷的词各有归属：
编排/回放属架构与控制流，描述即提示词属工具与执行，签名卡片属协议，
跨度/采样/脱敏属可观测与安全，评测/判定器属评测。

「首现章」列写"第二卷第 N 章"，与第一卷的"第 N 章"明确区分——
两卷各有独立编号，只写章号会指向两处。

幂等：重复跑不会重复插入（按中文术语判重）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GLOSSARY = ROOT / "vol1" / "appendix" / "C-术语表.md"

sys.stdout.reconfigure(encoding="utf-8")

# 节标题 -> [(中文术语, 英文原文, 缩写, 一句话释义, 首现章)]
NEW_TERMS: dict[str, list[tuple[str, str, str, str, str]]] = {
    "## 一、架构与控制流": [
        ("编排", "Orchestration", "—",
         "把若干 Agent 或若干步骤组织成能协同完成任务的控制结构", "第二卷第 10 章"),
        ("编排器", "Orchestrator", "—",
         "承接编排逻辑的对象，负责派单、回收与判定完成", "第二卷第 10 章"),
        ("监督者", "Supervisor", "—",
         "多 Agent 拓扑里负责分派任务与验收结果的那个角色", "第二卷第 10 章"),
        ("回放", "Replay", "—",
         "用录下来的模型响应重跑一遍，比对控制流与工具行为是否稳定", "第二卷第 7 章"),
        ("持久化执行", "Durable Execution", "—",
         "由平台托管工作流状态与重试，失败后从断点自动接着跑", "第二卷第 7 章"),
        ("早失败", "Fail Fast", "—",
         "参数或配置不合法时在入口处直接报错，不留到运行期", "第二卷第 2 章"),
    ],
    "## 三、工具与执行": [
        ("描述即提示词", "Description as Prompt", "—",
         "把工具说明写成给模型看的契约，让参数与约束随调用一起传过去", "第二卷第 4 章"),
    ],
    "## 四、多 Agent 与协议": [
        ("签名卡片", "AgentCard", "—",
         "A2A 协议里声明一个 Agent 身份、能力与端点的名片对象", "第二卷第 11 章"),
        ("过度代理", "Over-Agency", "—",
         "给 Agent 的权限超过任务真正所需，越权动作即使被拦下也说明设计有问题",
         "第二卷第 9 章"),
    ],
    "## 五、安全与评测": [
        ("跨度", "Span", "—",
         "一次可观测操作的时间区间，带父子嵌套与属性", "第二卷第 12 章"),
        ("采样", "Sampling", "—",
         "只保留一部分 trace 以压住存储成本，指标则另走全量计数", "第二卷第 12 章"),
        ("脱敏", "Redaction", "—",
         "写入日志与 trace 之前把密钥等敏感字段替换掉", "第二卷第 12 章"),
        ("评测", "Evaluation", "—",
         "用固定任务集与判定器衡量系统表现的过程", "第二卷第 13 章"),
        ("判定器", "Judge", "—",
         "给一条产出打分的规则，可以是确定性断言，也可以是另一个模型", "第二卷第 13 章"),
    ],
}


def main() -> int:
    text = GLOSSARY.read_text(encoding="utf-8")
    lines = text.splitlines()
    added = 0
    skipped = 0

    for section, terms in NEW_TERMS.items():
        if section not in text:
            print(f"  找不到节标题：{section}")
            continue
        # 该节的行区间：从标题行到下一个 "## " 之前
        try:
            start = next(i for i, ln in enumerate(lines) if ln.strip() == section)
        except StopIteration:
            print(f"  找不到节标题：{section}")
            continue
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].startswith("## "):
                end = i
                break
        # 节内已登记的中文术语
        existing = {ln.split("|")[1].strip()
                    for ln in lines[start:end] if ln.startswith("|") and len(ln.split("|")) > 2}
        new_rows = []
        for cn, en, abbr, desc, first in terms:
            if cn in existing:
                skipped += 1
                continue
            new_rows.append(f"| {cn} | {en} | {abbr} | {desc} | {first} |")
        if not new_rows:
            continue
        # 插到该节最后一行表格之后（末尾的空行之前）
        insert_at = end
        while insert_at > start and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines[insert_at:insert_at] = new_rows
        added += len(new_rows)
        print(f"  {section}  +{len(new_rows)} 条")

    GLOSSARY.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n新增 {added} 条，已存在跳过 {skipped} 条 → {GLOSSARY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
