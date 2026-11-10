#!/usr/bin/env python3
"""上下文装配器实跑：预算怎么分、超了先砍谁、结果可不可复现。

运行方式（工程根目录下）::

    python examples/context_budget_demo.py

全程离线：不联网、不需要 API Key、不写用户目录。

看四件事：

1. 装配结果长什么样（块 + 账）；
2. 预算充足时为什么不丢东西；
3. 预算紧张时按什么顺序裁（历史 → 检索 → 任务截断）；
4. 同样输入两次渲染是否逐字相同。
"""
from __future__ import annotations

import pathlib
import sys

# 允许不安装、直接在工程根目录跑这个脚本。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.context import ContextAssembler

TASK = "查一下 2026 年的项目预算，并告诉我总额"

HISTORY = [
    {"role": "user", "content": "先看看预算"},
    {"role": "assistant", "content": "好的，我去查"},
    {"role": "user", "content": "顺便看看上季度"},
]

RETRIEVAL = [
    "预算表：研发 62 万，市场 25 万，运维 13 万，总计 100 万。",
    "上季度预算：90 万。",
    "预算相关：总计 100 万。",
]

TOOLS = [
    {
        "name": "read_budget",
        "description": "读取指定财年的预算表。",
        "parameters": {
            "type": "object",
            "properties": {"fiscal_year": {"type": "string"}},
            "required": ["fiscal_year"],
        },
    },
    {
        "name": "add",
        "description": "把两个整数相加。",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
]


def show_report(title: str, assembler: ContextAssembler) -> None:
    assembled = assembler.assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)
    print(f"  预算 {assembled.report.budget}，实际用掉 {assembled.report.total}")
    for layer in ("system", "tools", "task", "retrieval", "history"):
        print(f"    {layer:<10} {assembled.report.per_layer.get(layer, 0):>4} token")
    print(f"  丢弃：{assembled.report.dropped or '（没有丢任何东西）'}")
    return assembled


def main() -> int:
    # ------------------------------------------------------------ 1
    assembled = show_report("1) 预算充足（4000 token）", ContextAssembler(token_budget=4000))
    print()
    print("--- 块与优先级 " + "-" * 52)
    for block in assembled.blocks:
        head = block.text.splitlines()[0][:46]
        print(f"  [{block.layer}] 优先级 {block.priority:>3}  {block.tokens:>4} token  {head}")
    print()
    print("--- 渲染结果（前 12 行） " + "-" * 40)
    for line in assembled.render().splitlines()[:12]:
        print(f"  {line}")
    print("  …")

    # ------------------------------------------------------------ 2
    tight = show_report("2) 预算紧张（120 token）", ContextAssembler(token_budget=120))
    print()
    print("--- 裁剪之后剩下的历史与检索 " + "-" * 36)
    for layer in ("retrieval", "history"):
        block = tight.block_for(layer)
        print(f"  [{layer}] {'（空，被裁光了）' if block is None else block.text}")

    # ------------------------------------------------------------ 3
    print()
    print("=" * 74)
    print("3) 确定性：同样输入渲染两次")
    print("=" * 74)
    a = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
    b = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
    same = a.render() == b.render()
    print(f"  两次渲染逐字相同：{same}")
    print(f"  稳定前缀（渲染开头）：{a.render().splitlines()[0]}")

    ok = same and a.report.total <= a.report.budget and tight.report.total <= tight.report.budget
    print()
    print(f"自检结果：{'全部通过' if ok else '有项目未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
