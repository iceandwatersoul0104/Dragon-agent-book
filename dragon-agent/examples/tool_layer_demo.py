#!/usr/bin/env python3
"""工具层实跑：注册、校验回灌、三种回灌形态、幂等与副作用、并行对齐。

运行方式（工程根目录下）::

    python examples/tool_layer_demo.py

全程离线：不联网、不需要 API Key、不写用户目录（只读一个不存在的文件）。

演示六件事：

1. **模型看到什么**——``schemas()`` 的输出；
2. **参数校验失败**——不抛异常，变成一句给模型看的话；
3. **工具内部异常**——同样不抛异常，也回灌；
4. **三种回灌形态**——字段级 / 语义级 / 环境级，各自提示模型做什么；
5. **幂等与副作用**——可重放工具命中幂等键不再执行；不可重放工具直接拒绝；
6. **并行调用的结果对齐与失败隔离**。
"""
from __future__ import annotations

import pathlib
import sys

# 允许不安装、直接在工程根目录跑这个脚本。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.tools import ToolRegistry, default_registry
from dragon_agent.tool_runner import (
    FEEDBACK_ENVIRONMENT,
    FEEDBACK_FIELD,
    FEEDBACK_POLICY,
    classify_feedback,
    invoke_many,
    render_feedback,
)
from dragon_agent.tool_schema import schema_for


def show(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main() -> int:
    reg = default_registry()

    # ---------------------------------------------------------------- 1
    show("1) 模型看到什么：schemas()")
    for schema in reg.schemas():
        params = schema["parameters"]
        required = params.get("required", [])
        print(f"  {schema['name']}")
        print(f"    描述：{schema['description']}")
        print(f"    参数：{list(params.get('properties', {}))}，必填 {required}")

    # ---------------------------------------------------------------- 1b
    show("1b) 写法二：从类型注解生成 schema（可选便利层）")

    def submit_order(sku: str, qty: int) -> dict:
        """提交一张订单。**会产生不可撤销的副作用**，必须带幂等键。"""
        return {"sku": sku, "qty": qty}

    generated = schema_for(submit_order)
    demo_reg = ToolRegistry()
    demo_reg.tool(name="submit_order", description=submit_order.__doc__,
                  parameters=generated)
    print(f"  生成出来的 schema：{generated}")
    print("  描述从哪来：函数 docstring——生成器不写描述")

    # ---------------------------------------------------------------- 2
    show("2) 参数校验失败：回灌，不抛异常")
    cases = [
        ("缺少必填参数", ("add", {"a": 1})),
        ("类型不对", ("add", {"a": "x", "b": 2})),
        ("多余参数", ("add", {"a": 1, "b": 2, "extra": "?"})),
        ("未知工具", ("no_such_tool", {})),
    ]
    for label, (name, args) in cases:
        result = reg.invoke(name, args)
        print(f"  [{label}] {name}({args})")
        print(f"    ok={result.ok}  error={result.error}")
        print(f"    回灌文本：{render_feedback(result)}")

    # ---------------------------------------------------------------- 3
    show("3) 工具内部异常：同样不抛异常")
    result = reg.invoke("read_text_file", {"path": "不存在的文件.txt"})
    print(f"  ok={result.ok}  error={result.error}")
    print(f"  回灌文本：{render_feedback(result)}")
    print(f"  实际执行的工具：{reg.invocations}  ← 校验失败的工具没有真的执行")

    # ---------------------------------------------------------------- 4
    show("4) 三种回灌形态")
    samples = [
        ("字段级", reg.invoke("add", {"a": 1})),
        ("语义级", reg.invoke("submit_order", {"sku": "X", "qty": 1})),
        ("环境级", reg.invoke("read_text_file", {"path": "不存在的文件.txt"})),
    ]
    for label, res in samples:
        kind = classify_feedback(res.error)
        print(f"  [{label}] 判定={kind}")
        print(f"    {render_feedback(res)}")

    # ---------------------------------------------------------------- 5
    show("5) 幂等与副作用")
    before = len(reg.invocations)

    # 可重放的副作用工具：同一个幂等键调用两次
    first = reg.invoke("send_report", {"content": "今日进展"}, idempotency_key="k1")
    second = reg.invoke("send_report", {"content": "今日进展"}, idempotency_key="k1")
    print("  send_report（可重放）")
    print(f"    第一次：ok={first.ok}  output={first.output}  idempotent_hit={first.idempotent_hit}")
    print(f"    第二次：ok={second.ok}  output={second.output}  idempotent_hit={second.idempotent_hit}")
    print(f"    实际执行次数：{len(reg.invocations) - before}（第二次没有真的发送）")

    # 不可重放的副作用工具：同一个幂等键调用两次
    before = len(reg.invocations)
    third = reg.invoke("submit_order", {"sku": "SKU-1", "qty": 2}, idempotency_key="k2")
    fourth = reg.invoke("submit_order", {"sku": "SKU-1", "qty": 2}, idempotency_key="k2")
    print("  submit_order（不可重放）")
    print(f"    第一次：ok={third.ok}  output={third.output}")
    print(f"    第二次：ok={fourth.ok}  error={fourth.error}")
    print(f"    实际执行次数：{len(reg.invocations) - before}（第二次被拒绝执行）")

    # 不给幂等键就调用不可重放工具
    nokey = reg.invoke("submit_order", {"sku": "SKU-2", "qty": 1})
    print(f"  submit_order 不给幂等键：ok={nokey.ok}  error={nokey.error}")

    # ---------------------------------------------------------------- 6
    show("6) 并行调用：结果对齐 + 失败隔离")
    calls = [
        ("read_text_file", {"path": "不存在-a.txt"}),   # 失败
        ("add", {"a": 1, "b": 2}),                      # 成功
        ("read_text_file", {"path": "不存在-b.txt"}),   # 失败
        ("add", {"a": 10, "b": 20}),                    # 成功
    ]
    results = invoke_many(reg, calls)
    print(f"  发起 {len(calls)} 个调用，返回 {len(results)} 个结果")
    for i, ((name, args), res) in enumerate(zip(calls, results)):
        status = "成功" if res.ok else "失败"
        detail = res.output if res.ok else res.error
        print(f"    [{i}] {name} → {status}：{detail}")
    ok_count = sum(1 for r in results if r.ok)
    print(f"  成功 {ok_count} 个，失败 {len(results) - ok_count} 个——失败没有影响其他调用")

    # 顺序要对：results[i] 对应 calls[i]
    aligned = results[1].output == 3 and results[3].output == 30
    print(f"  结果与调用一一对齐：{aligned}")

    # ---------------------------------------------------------------- 总结
    print()
    print("=" * 74)
    ok = (
        results[0].ok is False
        and results[1].ok is True
        and results[2].ok is False
        and results[3].ok is True
        and aligned
        and second.idempotent_hit is True
        and fourth.ok is False
        and classify_feedback(reg.invoke("add", {"a": 1}).error) == FEEDBACK_FIELD
        and classify_feedback(nokey.error) == FEEDBACK_POLICY
        and classify_feedback(reg.invoke("read_text_file", {"path": "x"}).error) == FEEDBACK_ENVIRONMENT
    )
    print(f"自检结果：{'全部通过' if ok else '有项目未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
