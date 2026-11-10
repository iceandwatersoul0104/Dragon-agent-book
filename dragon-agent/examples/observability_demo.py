#!/usr/bin/env python3
"""可观测性实跑：span 树、命名约定、脱敏、SLI、成本账、采样、导出。

运行方式（工程根目录下）::

    python examples/observability_demo.py
    python examples/observability_demo.py --live     # 需要 LLM_API_KEY

默认全程离线：不联网、不需要 API Key、不写用户目录（导出那一步写进
临时目录，跑完即删）。

演示八件事：

1. **span 树**——任务 / 模型调用 / 工具调用三层的父子关系；
2. **命名约定**——模型与 token 相关属性一律落在 ``gen_ai.*`` 下，
   写错的键名自动归位并被记下来；
3. **脱敏**——工具参数只留摘要、密钥形态被遮、长文本截断；
4. **SLI 报表**——三个场景（成功、工具失败后重试成功、连续空转）跑出来的
   成功率、工具错误率、无进展率、每次成功成本、步数分布；
5. **成本账按维度拆分**——按模型、按任务各算一份；
6. **采样**——按任务粒度丢 trace，但**指标仍然全量**；
7. **导出**——JSON Lines，OTel 形状，可直接喂给 Collector；
8. **接开源栈**——OpenTelemetry 与 Langfuse 怎么接（只讲接线，不联网）。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time

# 允许不安装、直接在工程根目录跑这个脚本。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.agent import Agent
from dragon_agent.config import Config, ConfigError
from dragon_agent.context import ContextAssembler
from dragon_agent.llm import LLMResponse, MockLLM, ToolCall, Usage
from dragon_agent.observability import (
    AlwaysSampler,
    CostMeter,
    JsonlExporter,
    RatioSampler,
    Tracer,
    summarize_arguments,
)
from dragon_agent.state import StateStore
from dragon_agent.tools import ToolRegistry, ToolSpec

sys.stdout.reconfigure(encoding="utf-8")


def show(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# --------------------------------------------------------------------- 夹具


def build_tools() -> ToolRegistry:
    """三个工具：一个会成功，一个参数校验会失败，一个读不存在的文件。"""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="add", description="把两个整数相加。",
                 parameters={"type": "object",
                             "properties": {"a": {"type": "integer"},
                                            "b": {"type": "integer"}},
                             "required": ["a", "b"]}),
        lambda a, b: a + b,
    )
    registry.register(
        ToolSpec(name="read_secret", description="读一个带密钥的配置文件。",
                 parameters={"type": "object",
                             "properties": {"path": {"type": "string"}},
                             "required": ["path"]}),
        lambda path: {"content": "sk-live-abcdef0123456789", "path": path},
    )
    return registry


def build_agent(config: Config, llm, tools: ToolRegistry, tracer: Tracer) -> Agent:
    return Agent(config, llm, tools, ContextAssembler(config), StateStore(config),
                 tracer=tracer)


def make_config() -> Config:
    """单价表：让成本账里的数字是真算出来的，不是拍的。"""
    import dataclasses

    return dataclasses.replace(
        Config.from_env_and_args([]),
        model="mock-model",
        price_in_per_mtok=3.0,
        price_out_per_mtok=15.0,
    )


# --------------------------------------------------------------------- 1. span 树


def demo_span_tree() -> Tracer:
    show("1) span 树：任务 / 模型调用 / 工具调用")
    config = make_config()
    tracer = Tracer(config)
    llm = MockLLM(script=[
        LLMResponse(text="先算一下",
                     tool_calls=[ToolCall(name="add", arguments={"a": 2, "b": 3})],
                     usage=Usage(input_tokens=128, output_tokens=21),
                     finish_reason="tool_calls"),
        LLMResponse(text="2 + 3 = 5",
                    usage=Usage(input_tokens=196, output_tokens=34),
                    finish_reason="stop"),
    ])
    agent = build_agent(config, llm, build_tools(), tracer)
    agent.run("帮我算一下 2 + 3")

    spans = tracer.spans()
    by_id = {span.span_id: span for span in spans}
    for span in spans:
        depth = 0
        parent = span.parent
        while parent:
            depth += 1
            parent = by_id[parent].parent if parent in by_id else None
        indent = "  " * (depth + 1)
        print(f"{indent}{span.name}  [{span.status}]  {span.duration_ms:.2f}ms")
        for key in sorted(span.attributes):
            print(f"{indent}    {key} = {span.attributes[key]}")
    print()
    print(f"  共 {len(spans)} 个 span，trace_id={spans[0].trace_id[:12]}…")
    return tracer


# --------------------------------------------------------------------- 2. 命名约定


def demo_naming() -> None:
    show("2) 命名约定：模型/token 属性一律 gen_ai.*，写错了自动归位")
    tracer = Tracer(make_config())
    with tracer.span("gen_ai.chat", **{"llm.model": "mock-model",
                                       "tool.name": "add",
                                       "input_tokens": 128}):
        pass
    span = tracer.spans()[0]
    print("  写进去的键名：llm.model / tool.name / input_tokens")
    print(f"  落盘时的键名：{sorted(span.attributes)}")
    print(f"  改名记录：{tracer.sli()['renamed_attributes']}")
    print()
    print("  → 导出层永远拿得到标准属性，开发者也能知道自己哪里没对齐")


# --------------------------------------------------------------------- 3. 脱敏


def demo_redaction() -> None:
    show("3) 脱敏：采集端就做，事后过滤来不及")
    tracer = Tracer(make_config())
    with tracer.span("execute_tool", **{
        "tool.name": "read_secret",
        "tool.arguments": summarize_arguments({"path": "/etc/secret.json",
                                              "api_key": "sk-live-abcdef0123456789"}),
    }) as span:
        pass
    print(f"  工具参数摘要：{span.attributes['tool.arguments']}")
    print("    ↑ 只留键名与哈希，参数值一个字都没进 trace")
    with tracer.span("x", **{"tool.note": "token=sk-live-abcdef0123456789"}):
        pass
    print(f"  密钥形态的正文：{tracer.spans()[-1].attributes['tool.note']}")
    print("    ↑ 正则脱敏：密钥形态在写入前就被遮掉")


# --------------------------------------------------------------------- 4. SLI


def run_scenarios() -> Tracer:
    """三个场景：正常完成、工具失败后重试成功、连续空转。"""
    show("4) SLI 报表：三个场景跑出来的指标")
    config = make_config()
    tracer = Tracer(config)
    tools = build_tools()

    # 场景一：正常完成
    llm = MockLLM(script=[
        LLMResponse(text="先算一下",
                     tool_calls=[ToolCall(name="add", arguments={"a": 2, "b": 3})],
                     usage=Usage(input_tokens=128, output_tokens=21),
                     finish_reason="tool_calls"),
        LLMResponse(text="答案是 5",
                    usage=Usage(input_tokens=196, output_tokens=34),
                    finish_reason="stop"),
    ])
    build_agent(config, llm, tools, tracer).run("场景一：正常完成")

    # 场景二：工具参数不合法 → 模型自己改对 → 成功
    llm = MockLLM(script=[
        LLMResponse(text="算一下",
                     tool_calls=[ToolCall(name="add", arguments={"a": 2})],
                     usage=Usage(input_tokens=100, output_tokens=18),
                     finish_reason="tool_calls"),
        LLMResponse(text="补上 b",
                     tool_calls=[ToolCall(name="add", arguments={"a": 2, "b": 3})],
                     usage=Usage(input_tokens=110, output_tokens=19),
                     finish_reason="tool_calls"),
        LLMResponse(text="答案是 5",
                    usage=Usage(input_tokens=140, output_tokens=25),
                    finish_reason="stop"),
    ])
    build_agent(config, llm, tools, tracer).run("场景二：工具失败后重试成功")

    # 场景三：连续做同一个动作 → 无进展被拦下
    same = LLMResponse(text="再算一次",
                       tool_calls=[ToolCall(name="add", arguments={"a": 1, "b": 1})],
                       usage=Usage(input_tokens=90, output_tokens=15),
                       finish_reason="tool_calls")
    llm = MockLLM(script=[same] * 6)
    build_agent(config, llm, tools, tracer).run("场景三：连续空转")

    sli = tracer.sli()
    print(f"  任务总数      ：{sli['task_count']}")
    print(f"  成功任务      ：{sli['success_count']}")
    print(f"  任务成功率    ：{sli['task_success_rate']:.4f}")
    print(f"  工具调用/失败 ：{sli['tool_calls']} / {sli['tool_errors']}"
          f"（错误率 {sli['tool_error_rate']:.4f}）")
    print(f"  无进展任务    ：{sli['no_progress_count']}"
          f"（无进展率 {sli['no_progress_rate']:.4f}）")
    print(f"  步数分布      ：p50={sli['steps_per_task']['p50']}"
          f" p90={sli['steps_per_task']['p90']}"
          f" max={sli['steps_per_task']['max']}")
    print(f"  总成本        ：{sli['total_cost_usd']:.6f} 美元")
    print(f"  每次成功成本  ：{sli['cost_per_success_usd']:.6f} 美元")
    print()
    print("  → 步数分布是诊断指标，不进 SLO；SLO 只对成功率、成本、时延定")
    return tracer


# --------------------------------------------------------------------- 5. 成本账


def demo_cost(tracer: Tracer) -> None:
    show("5) 成本账：按维度拆分")
    ledger = tracer.meter.to_dict()
    print(f"  总成本：{ledger['total_cost_usd']:.6f} 美元"
          f"（{ledger['calls']} 次模型调用）")
    print(f"  token 合计：{ledger['tokens']['total']}"
          f"（输入 {ledger['tokens']['input_tokens']}"
          f" / 输出 {ledger['tokens']['output_tokens']}）")
    print()
    print("  按模型拆：")
    for model, bucket in ledger["by_model"].items():
        print(f"    {model:<12} {bucket['calls']} 次  {bucket['cost_usd']:.6f} 美元")
    print("  按任务拆：")
    for task, bucket in ledger["by_task"].items():
        print(f"    {task:<12} {bucket['calls']} 次  {bucket['cost_usd']:.6f} 美元")


# --------------------------------------------------------------------- 6. 采样


def demo_sampling() -> None:
    show("6) 采样：丢的是 trace，不是指标")
    config = make_config()
    tracer = Tracer(config, sampler=RatioSampler(0.0))
    llm = MockLLM(script=[
        LLMResponse(text="答", usage=Usage(input_tokens=10, output_tokens=5),
                    finish_reason="stop"),
    ])
    build_agent(config, llm, build_tools(), tracer).run("被采样丢掉的任务")
    sli = tracer.sli()
    print(f"  采样比例 0% → 留下的 span：{sli['span_count']}")
    print(f"  但任务计数仍然是全量的：task_count={sli['task_count']}"
          f" success_rate={sli['task_success_rate']}")
    print()
    print("  → 用采样 trace 算成功率，得到的是「被采样的任务的成功率」，有偏")
    print("  → 采样按**任务**粒度：要么整条留，要么整条丢，父 span 不会变孤儿")


# --------------------------------------------------------------------- 7. 导出


def demo_export(tracer: Tracer) -> None:
    show("7) 导出：JSON Lines，OTel 形状")
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "trace.jsonl"
        tracer.export(JsonlExporter(path))
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
        print(f"  写了 {len(lines)} 行到 trace.jsonl")
        first = json.loads(lines[0])
        print(f"  第一行的键：{sorted(first)}")
        print(f"  样例：{json.dumps(first, ensure_ascii=False)[:150]}…")
    print()
    print("  → 字段名对齐 OTel span 数据模型，导出层换后端不用改业务代码")


# --------------------------------------------------------------------- 8. 开源栈


def demo_oss_stack() -> None:
    show("8) 接开源栈：OpenTelemetry / Langfuse 怎么接")
    print("  本工程是纯标准库的，所以只给**接线方式**，不引入依赖。")
    print()
    print("  OpenTelemetry（把 span 送进 Collector）：")
    print("    1) pip install opentelemetry-sdk opentelemetry-exporter-otlp")
    print("    2) 用本工程的 to_otel() 载荷建 span，或直接换掉 SpanExporter")
    print("    3) 属性名已经是 gen_ai.*，Collector 侧的语义约定不用再翻译")
    print()
    print("  Langfuse（自托管，接 OTel 端点）：")
    print("    1) 起 Langfuse 实例，拿到它的 OTel 接入端点")
    print("    2) 把 OTLP exporter 指向那个端点，Bearer 走环境变量注入")
    print("    3) span 名字与属性不用改——这正是语义层对齐 OTel 的收益")
    print()
    print("  OpenLLMetry / Traceloop（自动插桩）：")
    print("    它们能自动把 LLM 调用变成 OTel span，省掉自己写埋点；")
    print("    但只覆盖「模型调用」这一层，agent.step、空转、委托这些语义")
    print("    仍然要自己补——所以本工程的自动插桩补的是这一层。")


# --------------------------------------------------------------------- live


def run_live(argv: list[str]) -> int:
    """--live：真跑一次真实模型，产出真实 span。"""
    config = Config.from_env_and_args(argv)
    from dragon_agent.llm import build_client

    tracer = Tracer(config)
    llm = build_client(config)
    agent = build_agent(config, llm, build_tools(), tracer)
    result = agent.run("用一句话说明 2 + 3 等于几，并调用一次 add 工具验证")
    print(f"  模型：{llm.describe()}")
    print(f"  答案：{result.answer}")
    print(f"  span 数：{len(tracer.spans())}")
    for span in tracer.spans():
        print(f"    {span.name} [{span.status}] {span.duration_ms:.1f}ms")
    print(f"  成本：{tracer.meter.total():.6f} 美元")
    return 0


# --------------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--live" in argv:
        try:
            return run_live(argv)
        except ConfigError as exc:
            print(f"配置不合法，已退出：{exc}", file=sys.stderr)
            print("提示：设好 LLM_API_KEY（或 OPENAI_API_KEY）再试。", file=sys.stderr)
            return 2

    checks: list[bool] = []

    tracer = demo_span_tree()
    names = [span.name for span in tracer.spans()]
    checks.append("invoke_agent" in names)
    checks.append("gen_ai.chat" in names)
    checks.append("execute_tool" in names)

    demo_naming()
    demo_redaction()

    sli_tracer = run_scenarios()
    sli = sli_tracer.sli()
    checks.append(sli["task_count"] == 3)
    checks.append(sli["success_count"] == 2)
    checks.append(sli["no_progress_count"] == 1)
    checks.append(sli["tool_errors"] >= 1)
    checks.append(sli["total_cost_usd"] > 0)

    demo_cost(sli_tracer)
    demo_sampling()
    demo_export(sli_tracer)
    demo_oss_stack()

    print()
    print("=" * 74)
    passed = sum(1 for c in checks if c)
    print(f"自检结果：{passed}/{len(checks)} 项通过"
          f" —— {'全部通过' if passed == len(checks) else '有项目未通过'}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
