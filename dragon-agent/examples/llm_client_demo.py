#!/usr/bin/env python3
"""模型接入抽象的四个动作：脚本化 mock、确定性回放、重试退避、成本核算。

运行方式（工程根目录下）::

    python examples/llm_client_demo.py

全程离线：不联网、不需要 API Key。演示四件事：

1. **脚本化 mock**：``MockLLM`` 按剧本返回响应，用完就循环最后一条；
2. **确定性回放**：同一份剧本跑两遍，结果逐字相同；
3. **重试与退避**：限流、5xx、超时各自被识别成不同的失败类别，
   只有该重试的才重试，退避序列可预测；
4. **成本核算**：缓存命中与未命中分开计价。

另外做一次**契约自检**：mock 和真实形状的响应，必须是同一种对象。
"""
from __future__ import annotations

import pathlib
import sys

# 允许不安装、直接在工程根目录跑这个脚本。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.llm import LLMResponse, MockLLM, ToolCall, Usage
from dragon_agent.llm_policy import (
    FAILURE_BAD_RESPONSE,
    FAILURE_RATE_LIMITED,
    FAILURE_SERVER_ERROR,
    FAILURE_TIMEOUT,
    RetryPolicy,
    ResilientLLMClient,
    classify_failure,
)
from dragon_agent.pricing import PriceTable, TokenCounts


# --------------------------------------------------------------------- 1. 剧本


def build_script() -> list[LLMResponse]:
    """模型在这个任务上会做的两次决策。"""
    return [
        LLMResponse(
            text="先查预算表",
            tool_calls=[ToolCall(name="read_budget", arguments={"fiscal_year": "2026"})],
            usage=Usage(input_tokens=128, output_tokens=21),
            finish_reason="tool_calls",
        ),
        LLMResponse(
            text="预算总额 100 万元。",
            usage=Usage(input_tokens=196, output_tokens=34),
            finish_reason="stop",
        ),
    ]


# --------------------------------------------------------------------- 2. 回放


def replay_twice() -> tuple[list[dict], list[dict]]:
    """同一份剧本跑两遍，返回两次的响应摘要。"""
    summaries = []
    for _ in range(2):
        llm = MockLLM(script=build_script())
        run = []
        for _ in range(2):
            resp = llm.complete([{"role": "user", "content": "查预算"}])
            run.append(resp.to_dict())
        summaries.append(run)
    return summaries[0], summaries[1]


# --------------------------------------------------------------------- 3. 重试


class FlakyClient:
    """按剧本吐错误的假客户端：前几次失败，之后成功。"""

    def __init__(self, failures: list[str]):
        self.failures = list(failures)
        self.calls = 0

    def complete(self, messages, tools=None, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.failures:
            text = self.failures.pop(0)
            return LLMResponse(text=text, finish_reason="error")
        return LLMResponse(text="成功了", usage=Usage(input_tokens=10, output_tokens=2))

    def describe(self) -> str:
        return "flaky-client"


def retry_demo() -> dict:
    """演示三种失败各自的处理。"""
    waited: list[float] = []

    def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    policy = RetryPolicy(max_attempts=3, base_delay_s=0.2, max_delay_s=5.0, jitter_ratio=0.0)

    # 限流：重试
    limited = FlakyClient(["[llm] HTTP 429", "[llm] HTTP 429"])
    client = ResilientLLMClient(limited, policy=policy, sleep=fake_sleep)
    resp = client.complete([{"role": "user", "content": "hi"}])
    rate_limited = {
        "attempts": client.attempts,
        "waited": list(waited),
        "final_failure": classify_failure(resp),
        "text": resp.text,
    }

    # 5xx：重试
    waited.clear()
    server = FlakyClient(["[llm] HTTP 503"])
    client2 = ResilientLLMClient(server, policy=policy, sleep=fake_sleep)
    resp2 = client2.complete([{"role": "user", "content": "hi"}])
    server_error = {
        "attempts": client2.attempts,
        "waited": list(waited),
        "final_failure": classify_failure(resp2),
        "text": resp2.text,
    }

    # 响应不对：不重试（重试多少次都一样）
    waited.clear()
    bad = FlakyClient(["[llm] 响应里没有 choices"])
    client3 = ResilientLLMClient(bad, policy=policy, sleep=fake_sleep)
    resp3 = client3.complete([{"role": "user", "content": "hi"}])
    bad_response = {
        "attempts": client3.attempts,
        "waited": list(waited),
        "final_failure": classify_failure(resp3),
        "text": resp3.text,
    }

    # 超时：重试到用尽，然后原样返回最后一条错误
    waited.clear()
    timing_out = FlakyClient(["[llm] TimeoutError: timed out"] * 3)
    client4 = ResilientLLMClient(timing_out, policy=policy, sleep=fake_sleep)
    resp4 = client4.complete([{"role": "user", "content": "hi"}])
    timeout = {
        "attempts": client4.attempts,
        "waited": list(waited),
        "final_failure": classify_failure(resp4),
        "text": resp4.text,
    }

    return {
        "rate_limited": rate_limited,
        "server_error": server_error,
        "bad_response": bad_response,
        "timeout": timeout,
    }


# --------------------------------------------------------------------- 4. 成本


def cost_demo() -> dict:
    """缓存命中与未命中分开计价。

    单价是**示例值**，不是任何真实模型的价格——真实价格从配置进来，
    写死在代码里就是埋雷。
    """
    prices = PriceTable(
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cached_input_per_mtok=0.3,
    )
    counts = TokenCounts(input_tokens=100_000, output_tokens=2_000, cached_input_tokens=80_000)

    cached = prices.cost(counts)
    no_cache = prices.cost(TokenCounts(
        input_tokens=counts.input_tokens,
        output_tokens=counts.output_tokens,
    ))
    return {
        "prices": prices,
        "counts": counts,
        "with_cache_usd": cached,
        "without_cache_usd": no_cache,
        "saved_usd": no_cache - cached,
        "breakdown": prices.breakdown(counts),
    }


# --------------------------------------------------------------------- 5. 契约自检


def contract_selfcheck() -> dict:
    """mock 与真实形状的响应，必须是同一种对象。"""
    from dragon_agent.llm import OpenAICompatClient

    # 真实客户端解析一条 OpenAI 兼容响应（不联网，只调解析函数）
    real_client = OpenAICompatClient(model="demo-model", api_key="dummy")
    raw = {
        "choices": [{
            "message": {
                "content": "预算总额 100 万元。",
                "tool_calls": [],
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 196, "completion_tokens": 34},
    }
    real_resp = real_client._parse(raw)

    mock_resp = MockLLM(script=build_script()).complete(
        [{"role": "user", "content": "查预算"}]
    )

    same_type = type(real_resp) is type(mock_resp)
    same_fields = set(real_resp.to_dict()) == set(mock_resp.to_dict())
    return {
        "same_type": same_type,
        "same_fields": same_fields,
        "real_type": type(real_resp).__name__,
        "mock_type": type(mock_resp).__name__,
        "fields": sorted(mock_resp.to_dict()),
        "real_usage": real_resp.usage.to_dict(),
    }


# --------------------------------------------------------------------- 入口


def main() -> int:
    print("=" * 74)
    print("1) 脚本化 mock：剧本用完后循环最后一条")
    print("=" * 74)
    llm = MockLLM(script=build_script())
    for i in range(3):
        resp = llm.complete([{"role": "user", "content": "查预算"}])
        print(f"  第 {i + 1} 次：finish_reason={resp.finish_reason}"
              f"  usage={resp.usage.to_dict()}")

    print()
    print("=" * 74)
    print("2) 确定性回放：同一份剧本跑两遍，结果逐字相同")
    print("=" * 74)
    first, second = replay_twice()
    same = first == second
    print(f"  两次结果相同：{same}")
    print(f"  第 1 次第 1 步：{first[0]['finish_reason']} / {first[0]['usage']}")
    print(f"  第 2 次第 1 步：{second[0]['finish_reason']} / {second[0]['usage']}")

    print()
    print("=" * 74)
    print("3) 重试与退避：只有该重试的才重试")
    print("=" * 74)
    retries = retry_demo()
    labels = {
        "rate_limited": "限流 HTTP 429",
        "server_error": "服务端错误 HTTP 503",
        "bad_response": "响应不对（没有 choices）",
        "timeout": "超时（重试到用尽）",
    }
    for key, label in labels.items():
        item = retries[key]
        waited = ", ".join(f"{d:.1f}s" for d in item["waited"]) or "（没等）"
        print(f"  {label}")
        print(f"    尝试 {item['attempts']} 次，等待：{waited}")
        print(f"    最终失败类别：{item['final_failure']}")
        print(f"    返回文本：{item['text']}")

    print()
    print("=" * 74)
    print("4) 成本核算：缓存命中与未命中分开算")
    print("=" * 74)
    cost = cost_demo()
    print(f"  输入 {cost['counts'].input_tokens} token"
          f"（其中 {cost['counts'].cached_input_tokens} 命中缓存），"
          f"输出 {cost['counts'].output_tokens} token")
    print(f"  有缓存：{cost['with_cache_usd']:.6f} 美元")
    print(f"  无缓存：{cost['without_cache_usd']:.6f} 美元")
    print(f"  省下  ：{cost['saved_usd']:.6f} 美元")
    for key, value in cost["breakdown"].items():
        print(f"    {key}：{value}")

    print()
    print("=" * 74)
    print("5) 契约自检：mock 与真实形状必须是同一种响应")
    print("=" * 74)
    check = contract_selfcheck()
    print(f"  类型相同：{check['same_type']}"
          f"（真实={check['real_type']}，mock={check['mock_type']}）")
    print(f"  字段集合相同：{check['same_fields']}")
    print(f"  字段：{check['fields']}")
    print(f"  真实响应的用量：{check['real_usage']}")

    print()
    print("=" * 74)
    ok = (
        same
        and check["same_type"]
        and check["same_fields"]
        and retries["rate_limited"]["attempts"] == 3
        and retries["bad_response"]["attempts"] == 1
        and retries["timeout"]["attempts"] == 3
    )
    print(f"自检结果：{'全部通过' if ok else '有项目未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
