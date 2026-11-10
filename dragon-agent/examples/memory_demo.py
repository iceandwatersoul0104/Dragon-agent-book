#!/usr/bin/env python3
"""记忆模块实跑：来源怎么把关、版本怎么留、TTL 怎么降权、评测怎么判。

运行方式（工程根目录下）::

    python examples/memory_demo.py

全程离线：不联网、不需要 API Key、不写用户目录。

看六件事：

1. 模型来源的语义记忆被拒（而不是悄悄写进去）；
2. 高可信覆盖低可信，旧版本留档；
3. TTL 到期是降权不是删除；
4. 显式遗忘之后检索不到；
5. 向量路线与图谱路线对同一句查询给出不同答案（实体串台盲区）；
6. 记忆评测报告：想起率、精确率、误想率。
"""
from __future__ import annotations

import pathlib
import sys

# 允许不安装、直接在工程根目录跑这个脚本。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.memory import (
    GraphMemory,
    MemoryWriteRejected,
    VectorMemory,
    default_memory_cases,
    default_memory_factory,
    evaluate_memory,
)


class Clock:
    """可推进的假时钟——让 TTL 演示可复现。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def line(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main() -> int:
    ok = True

    # ------------------------------------------------------------- 1
    line("1) 来源把关：模型写语义记忆被拒")
    memory = VectorMemory()
    try:
        memory.write("semantic", "用户偏好深色界面", source="model")
        print("  没有拦住——这是 bug")
        ok = False
    except MemoryWriteRejected as exc:
        print(f"  拒绝：{exc}")

    item = memory.write("working", "临时草稿：下一步查预算", source="model")
    print(f"  工作记忆照常写入：kind={item.kind}  可信度基线={item.score}")

    # ------------------------------------------------------------- 2
    line("2) 版本：用户覆盖工具，旧版本留档")
    memory = VectorMemory()
    first = memory.write("semantic", "Orion 项目预算是 8 万元", source="user")
    second = memory.write("semantic", "Orion 项目预算是 12 万元", source="user")
    print(f"  旧版本 v{first.version}：{first.text}")
    print(f"  新版本 v{second.version}：{second.text}")
    for old in memory.history(second.id):
        print(f"  历史里还留着：v{old.version} {old.text}")

    try:
        memory.write("semantic", "Orion 项目预算是 99 万元", source="tool")
        print("  工具来源竟然盖掉了用户——这是 bug")
        ok = False
    except MemoryWriteRejected as exc:
        print(f"  工具来源想覆盖，被拒：{exc}")

    # ------------------------------------------------------------- 3
    line("3) TTL：到期降权，不是删除")
    clock = Clock()
    memory = VectorMemory(clock=clock)
    memory.write("semantic", "Orion 项目预算是 12 万元", source="user", ttl=60)
    memory.write("semantic", "Argus 项目预算是 5 万元", source="user")

    before = {h.id: h.score for h in memory.recall("Orion 预算是多少", k=5)}
    clock.advance(120)
    after = {h.id: h.score for h in memory.recall("Orion 预算是多少", k=5)}
    for hid, score in before.items():
        expired = memory.is_expired(memory._items[hid])
        print(f"  条目 {hid}：到期前 {score:.4f} → 到期后 {after.get(hid, 0.0):.4f}"
              f"  已过期={expired}")
    print(f"  库里还剩 {memory.stats()['semantic']} 条语义记忆——过期条目没被抹掉")
    if not (after and all(after[h] < before[h] for h in before)):
        ok = False

    # ------------------------------------------------------------- 4
    line("4) 遗忘：显式忘掉就检索不到")
    memory = VectorMemory()
    item = memory.write("semantic", "Orion 项目预算是 12 万元", source="user")
    print(f"  遗忘前检索到 {len(memory.recall('Orion 预算是多少'))} 条")
    memory.forget(item.id)
    left = memory.recall("Orion 预算是多少")
    print(f"  遗忘后检索到 {len(left)} 条")
    ok = ok and not left

    # ------------------------------------------------------------- 5
    line("5) 两条路线：同一句查询，两种答案")
    query = "Orion 预算是多少"
    vector = VectorMemory()
    vector.write("semantic", "Orion 项目预算是 12 万元", source="user")
    vector.write("semantic", "Argus 项目预算是 5 万元", source="user")
    graph = GraphMemory()
    graph.write("semantic", "Orion 项目预算是 12 万元", source="user")
    graph.write("semantic", "Argus 项目预算是 5 万元", source="user")

    print(f"  查询：{query}")
    print("  向量路线（哈希投影，只认字面）：")
    for h in vector.recall(query, kind="semantic", k=5):
        print(f"    {h.score:.4f}  {h.text}")
    print("  图谱路线（实体 + 关系边）：")
    for h in graph.recall(query, kind="semantic", k=5):
        print(f"    {h.score:.4f}  {h.text}")
    print("  图谱路线现在成立的关系边：")
    for edge in graph.facts("Orion"):
        print(f"    ({edge.subject}) --{edge.predicate}--> ({edge.object})")

    # ------------------------------------------------------------- 6
    line("6) 记忆评测：想起率、精确率、误想率")
    report = evaluate_memory(default_memory_cases(), default_memory_factory)
    print(f"  用例总数        {report.total}")
    print(f"  该想起时想起    {report.recalled}  想起率 {report.recall_rate:.2%}")
    print(f"  精确率          {report.precision:.2%}")
    print(f"  误想次数        {report.false_recall}")
    print(f"  漏想            {report.missed or '（无）'}")
    print(f"  乱想            {report.polluted or '（无）'}")
    for detail in report.details:
        mark = "过" if detail["pass"] else "挂"
        print(f"    [{mark}] {detail['id']:<34} 召回 {len(detail['returned'])} 条")

    ok = ok and report.recalled == report.total and report.false_recall == 0

    print()
    print(f"自检结果：{'全部通过' if ok else '有项目未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
