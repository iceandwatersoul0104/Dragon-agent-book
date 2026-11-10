#!/usr/bin/env python3
"""ch35 成本与性能优化 —— 最小可实现参考实现。

一个"成本-延迟实验室"，把书里的每一条变成可测量的开关：
    1. 成本模型：一次任务拆成 输入 / 输出 / 缓存读 / 缓存写 / 工具往返 五项。
    2. 提示缓存模拟器：前缀哈希匹配 + TTL，测命中率，演示"动态内容混进前缀 -> 命中归零"。
    3. 模型路由：按难度分级 + 级联（校验失败升级），统计升级占比与成本。
    4. 语义缓存：纯标准库的字符 bigram 余弦相似度，演示近似命中与上下文指纹校验。
    5. 延迟模型：流式只改善 TTFT；工具并行把求和变成取最大值。
    6. 对照实验：同一批任务跑"基线"与"优化后"，输出成本与延迟对比。

注意：所有数字都来自本文件的模拟与本地示意价目表，不是任何真实厂商的报价或基准。

依赖：仅 Python 标准库（>= 3.8）。默认 mock 模式不需要任何 API Key。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# 示意价目表：单位为每百万 token 的价格。
# 只用于演示成本计算逻辑，真实环境请换成自己的价目表并定期更新。
# ---------------------------------------------------------------------------

PRICE: Dict[str, Dict[str, float]] = {
    "small": {"input": 0.15, "output": 0.60, "cache_read": 0.015, "cache_write": 0.19},
    "large": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
}

MODEL_LATENCY = {
    "small": {"ttft": 0.25, "out_tps": 120.0},
    "large": {"ttft": 0.60, "out_tps": 45.0},
}

# 任务形态参数（token / 秒）
PREFIX_TOKENS = 1200        # 系统提示 + 工具定义 + few-shot
DYNAMIC_TOKENS = 40         # 每轮动态内容（时间戳、用户名、会话 ID）
HISTORY_PER_STEP = 420      # 每步新增的历史（对话 + 检索 + 上一轮工具返回）
SUMMARY_TOKENS = 200        # 裁剪后保留的摘要
TRIM_TURNS = 2              # 裁剪后保留最近几轮
TOOL_RESULT_TOKENS = 350    # 单次工具返回体


# ---------------------------------------------------------------------------
# 用量与成本
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    input_tokens: int = 0        # 常规输入（不含工具返回，避免重复计费）
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_roundtrip_tokens: int = 0   # 工具返回体，会作为下一轮输入被重新计费

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.tool_roundtrip_tokens += other.tool_roundtrip_tokens


def cost_of(usage: Usage, model: str) -> Dict[str, float]:
    price = PRICE[model]
    return {
        "input": usage.input_tokens * price["input"] / 1_000_000,
        "output": usage.output_tokens * price["output"] / 1_000_000,
        "cache_read": usage.cache_read_tokens * price["cache_read"] / 1_000_000,
        "cache_write": usage.cache_write_tokens * price["cache_write"] / 1_000_000,
        "tool_roundtrip": usage.tool_roundtrip_tokens * price["input"] / 1_000_000,
    }


def total_cost(cost: Dict[str, float]) -> float:
    return sum(cost.values())


# ---------------------------------------------------------------------------
# 提示缓存模拟器
# ---------------------------------------------------------------------------


class PrefixCache:
    """按前缀逐 token 匹配（这里用哈希近似）+ TTL 过期。"""

    def __init__(self, ttl_s: float = 300.0) -> None:
        self.ttl_s = ttl_s
        self.entries: Dict[str, Tuple[int, float]] = {}
        self.hits = 0
        self.misses = 0
        self.read_tokens = 0
        self.write_tokens = 0

    def access(self, prefix: str, tokens: int, now: float) -> str:
        key = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
        entry = self.entries.get(key)
        if entry is not None and entry[1] > now:
            self.hits += 1
            self.read_tokens += tokens
            return "read"
        self.misses += 1
        self.write_tokens += tokens
        self.entries[key] = (tokens, now + self.ttl_s)
        return "write"

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


# ---------------------------------------------------------------------------
# 语义缓存：字符 bigram bag-of-words + 余弦相似度（纯标准库）
# ---------------------------------------------------------------------------


def bigrams(text: str) -> Counter:
    clean = re.sub(r"\s+", "", text.lower())
    return Counter(clean[i:i + 2] for i in range(max(0, len(clean) - 1)))


def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class CacheEntry:
    query: str
    fingerprint: str      # 上下文指纹：模型/提示词/工具/知识库版本 + 会话上下文
    answer: str
    saved_cost: float


class SemanticCache:
    def __init__(self, threshold: float = 0.75) -> None:
        self.threshold = threshold
        self.entries: List[CacheEntry] = []
        self.hits = 0
        self.rejected = 0

    def lookup(self, query: str, fingerprint: str) -> Tuple[bool, float, str]:
        best_sim, best_entry = 0.0, None
        vec = bigrams(query)
        for entry in self.entries:
            sim = cosine(vec, bigrams(entry.query))
            if sim > best_sim:
                best_sim, best_entry = sim, entry
        if best_entry is None or best_sim < self.threshold:
            return False, best_sim, "相似度不足，判定为未命中"
        if best_entry.fingerprint != fingerprint:
            self.rejected += 1
            return False, best_sim, "上下文指纹不同（答案依赖会话或权限），拒绝复用"
        self.hits += 1
        return True, best_sim, "命中语义缓存"

    def store(self, query: str, fingerprint: str, answer: str, saved_cost: float) -> None:
        self.entries.append(CacheEntry(query, fingerprint, answer, saved_cost))


# ---------------------------------------------------------------------------
# 任务与配置
# ---------------------------------------------------------------------------


@dataclass
class MockTask:
    id: str
    difficulty: str            # easy | hard
    steps: int
    output_tokens_per_step: int
    tool_latencies: List[float] = field(default_factory=list)   # 同一轮内的工具耗时
    small_model_ok: bool = True   # 小模型能否通过校验（False 触发级联升级）


@dataclass
class Config:
    name: str
    trim: bool = False
    use_cache: bool = False
    router: bool = False       # easy -> small
    cascade: bool = False      # 小模型校验失败则升级大模型
    parallel_tools: bool = False
    dynamic_in_prefix: bool = False   # 演示：动态内容混进前缀
    cache_ttl_s: float = 300.0


def build_tasks() -> List[MockTask]:
    return [
        MockTask("T-01", "easy", 4, 220, [4.0, 3.5]),
        MockTask("T-02", "easy", 6, 240, [5.0, 4.2, 3.6]),
        MockTask("T-03", "hard", 12, 420, [6.0, 7.0, 5.0]),
        MockTask("T-04", "easy", 5, 200, [3.8], small_model_ok=False),   # 小模型过不了校验 -> 级联升级
        MockTask("T-05", "hard", 9, 380, [5.5, 4.4]),
    ]


# ---------------------------------------------------------------------------
# 模拟一次任务
# ---------------------------------------------------------------------------


@dataclass
class SimResult:
    task_id: str
    model: str
    usage: Usage
    cost: Dict[str, float]
    latency: float
    ttft: float
    escalations: int = 0


def route_model(task: MockTask, cfg: Config) -> str:
    if cfg.router and task.difficulty == "easy":
        return "small"
    return "large"


def simulate_once(task: MockTask, cfg: Config, now: float, cache: Optional[PrefixCache],
                  model: str) -> Tuple[Usage, float, float]:
    """按给定模型跑一遍任务，返回 (用量, 总延迟, TTFT)。"""
    usage = Usage()
    latency = 0.0
    ttft = 0.0
    for step in range(task.steps):
        # --- 前缀：稳定内容 + （可选）动态内容 ---
        prefix_tokens = PREFIX_TOKENS + (DYNAMIC_TOKENS if cfg.dynamic_in_prefix else 0)
        if cache is not None:
            prefix_key = "prefix-v1"
            if cfg.dynamic_in_prefix:
                # 每轮都变的动态内容混进前缀 -> 前缀哈希每轮都变 -> 永远不命中
                prefix_key = "prefix-v1|task={}|ts={}".format(task.id, step)
            kind = cache.access(prefix_key, prefix_tokens, now)
            if kind == "read":
                usage.cache_read_tokens += prefix_tokens
            else:
                usage.cache_write_tokens += prefix_tokens
        else:
            usage.input_tokens += prefix_tokens

        # --- 历史：不裁剪时随步数线性增长，叠加后是平方级 ---
        history_tokens = (step + 1) * HISTORY_PER_STEP
        if cfg.trim:
            history_tokens = SUMMARY_TOKENS + min(history_tokens, TRIM_TURNS * HISTORY_PER_STEP)
        usage.input_tokens += history_tokens

        # --- 工具返回：单独计一次（下一轮会作为输入重新发） ---
        if step < len(task.tool_latencies):
            usage.tool_roundtrip_tokens += TOOL_RESULT_TOKENS

        # --- 输出 ---
        usage.output_tokens += task.output_tokens_per_step

        # --- 延迟 ---
        lat = MODEL_LATENCY[model]
        step_ttft = lat["ttft"]
        step_total = step_ttft + task.output_tokens_per_step / lat["out_tps"]
        if step < len(task.tool_latencies):
            one_tool = task.tool_latencies[step]
            if cfg.parallel_tools:
                step_total += one_tool          # 并行 -> 取最大值
            else:
                step_total += one_tool * 3      # 串行三个同类工具 -> 求和
        if step == 0:
            ttft = step_ttft
        latency += step_total
    return usage, latency, ttft


def simulate(task: MockTask, cfg: Config, now: float,
             cache: Optional[PrefixCache] = None) -> SimResult:
    model = route_model(task, cfg)
    usage, latency, ttft = simulate_once(task, cfg, now, cache, model)
    escalations = 0

    # 级联：小模型跑完没通过校验 -> 整个任务用大模型重跑一次（钱付两遍，延迟也翻倍）
    if cfg.cascade and model == "small" and not task.small_model_ok:
        retry_usage, retry_latency, _ = simulate_once(task, cfg, now, cache, "large")
        usage.add(retry_usage)
        latency += retry_latency
        escalations = 1

    cost = cost_of(usage, model)
    if escalations:
        # 升级那部分的输出按大模型单价重算
        extra_out = (usage.output_tokens / 2) * (PRICE["large"]["output"] - PRICE["small"]["output"]) / 1_000_000
        cost["output"] += extra_out
    return SimResult(task.id, model, usage, cost, latency, ttft, escalations)


# ---------------------------------------------------------------------------
# 对照实验
# ---------------------------------------------------------------------------


def run_batch(tasks: List[MockTask], cfg: Config) -> Tuple[List[SimResult], Optional[PrefixCache]]:
    # 多个任务共享同一个缓存实例，模拟同一服务里的多轮会话
    cache = PrefixCache(cfg.cache_ttl_s) if cfg.use_cache else None
    results: List[SimResult] = []
    now = time.time()
    for task in tasks:
        results.append(simulate(task, cfg, now, cache))
    return results, cache


def print_batch(title: str, results: List[SimResult], cache: Optional[PrefixCache]) -> None:
    print("-" * 76)
    print(title)
    for r in results:
        print("  {} model={:<6} 输入 {:>6} 输出 {:>5} 缓存读 {:>6} 缓存写 {:>6} 工具往返 {:>6} "
              "| 成本 ${:.4f} | 延迟 {:>5.1f}s".format(
                  r.task_id, r.model, r.usage.input_tokens, r.usage.output_tokens,
                  r.usage.cache_read_tokens, r.usage.cache_write_tokens,
                  r.usage.tool_roundtrip_tokens, total_cost(r.cost), r.latency))
    total = sum(total_cost(r.cost) for r in results)
    avg_latency = sum(r.latency for r in results) / len(results) if results else 0.0
    print("  合计成本 ${:.4f}   平均延迟 {:.1f}s".format(total, avg_latency))
    if cache is not None:
        total_input = sum(r.usage.input_tokens + r.usage.cache_read_tokens + r.usage.cache_write_tokens
                          for r in results)
        cached = sum(r.usage.cache_read_tokens for r in results)
        print("  缓存命中率 {:.1%}（{}/{} 次），缓存 token 占输入 {:.1%}".format(
            cache.hit_rate, cache.hits, cache.hits + cache.misses,
            cached / total_input if total_input else 0.0))
    print()


# ---------------------------------------------------------------------------
# 语义缓存演示
# ---------------------------------------------------------------------------


def demo_semantic_cache() -> None:
    print("-" * 76)
    print("语义缓存演示（字符 bigram 余弦，纯标准库）")
    cache = SemanticCache(threshold=0.75)
    queries = [
        ("怎么申请退款", "ctx:anon|model:v1|prompt:v3|kb:2026-11", "首次查询，走模型"),
        ("退款怎么申请", "ctx:anon|model:v1|prompt:v3|kb:2026-11", "近似问题"),
        ("怎么申请退款", "ctx:vip-user-88|model:v1|prompt:v3|kb:2026-11", "同一问题，不同上下文"),
    ]
    for query, fingerprint, note in queries:
        hit, sim, reason = cache.lookup(query, fingerprint)
        print("  查询「{}」（{}）相似度 {:.3f} -> {}（{}）".format(
            query, note, sim, "命中" if hit else "未命中", reason))
        if not hit:
            cache.store(query, fingerprint, "答案-" + query, 0.004)
    print("  命中 {} 次，因上下文指纹不同拒绝复用 {} 次".format(cache.hits, cache.rejected))
    print()


# ---------------------------------------------------------------------------
# Live 模式
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """粗略估算：CJK 按字计，其他按 4 字符一个 token。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cjk
    return cjk + other // 4


def live_call(model: str, base_url: str, api_key: str, timeout: float = 60.0) -> Dict[str, Any]:
    body = {
        "model": model,
        "max_tokens": 256,
        "messages": [
            {"role": "system", "content": "你是一个简洁的助手。"},
            {"role": "user", "content": "用三句话说明什么是 Agent 的上下文窗口。"},
        ],
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    started = time.time()
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    elapsed = time.time() - started
    usage = payload.get("usage") or {}
    prompt_text = json.dumps(body["messages"], ensure_ascii=False)
    completion = payload["choices"][0]["message"].get("content") or ""
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or estimate_tokens(prompt_text)),
        "completion_tokens": int(usage.get("completion_tokens") or estimate_tokens(completion)),
        "elapsed": elapsed,
        "answer": completion,
    }


def run_live(model: str, base_url: str, api_key: str) -> None:
    print("-" *76)
    print("live 模式：真实 API 单次调用")
    result = live_call(model, base_url, api_key)
    # 真实模型不在示意价目表里，统一按 large 口径估算，并在输出里注明
    price = PRICE["large"]
    cost = (result["prompt_tokens"] * price["input"]
            + result["completion_tokens"] * price["output"]) / 1_000_000
    print("  prompt_tokens={} completion_tokens={} 用时 {:.2f}s".format(
        result["prompt_tokens"], result["completion_tokens"], result["elapsed"]))
    print("  按 large 口径估算成本 ${:.6f}（示意价目表，仅用于对照量级）".format(cost))
    print("  估算器对比：estimate_tokens() 给出 prompt≈{} completion≈{}".format(
        estimate_tokens(json.dumps([
            {"role": "system", "content": "你是一个简洁的助手。"},
            {"role": "user", "content": "用三句话说明什么是 Agent 的上下文窗口。"}],
            ensure_ascii=False)),
        estimate_tokens(result["answer"])))
    print()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ch35 成本与性能优化最小实现")
    parser.add_argument("--live", action="store_true", help="用真实 API 跑一次并按实际 token 算成本")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    args = parser.parse_args(argv)

    if args.live and not os.environ.get("OPENAI_API_KEY"):
        print("[!] --live 需要环境变量 OPENAI_API_KEY；未设置时可用 mock 模式跑通全回路。")
        return 2

    tasks = build_tasks()

    baseline_cfg = Config(name="基线", trim=False, use_cache=False, router=False,
                          cascade=False, parallel_tools=False, dynamic_in_prefix=False)
    optimized_cfg = Config(name="优化", trim=True, use_cache=True, router=True,
                            cascade=True, parallel_tools=True, dynamic_in_prefix=False)
    unstable_cfg = Config(name="前缀不稳", trim=True, use_cache=True, router=True,
                          cascade=True, parallel_tools=True, dynamic_in_prefix=True)

    print("=" * 76)
    print("成本-延迟对照实验（数字来自本文件的模拟与示意价目表，非真实厂商报价）")
    print("=" * 76)

    base_results, _ = run_batch(tasks, baseline_cfg)
    print_batch("[基线] 不裁剪 / 无缓存 / 全用大模型 / 工具串行", base_results, None)

    opt_results, opt_cache = run_batch(tasks, optimized_cfg)
    print_batch("[优化] 裁剪 + 缓存 + 路由(级联) + 工具并行", opt_results, opt_cache)

    bad_results, bad_cache = run_batch(tasks, unstable_cfg)
    print_batch("[反例] 同上，但把每轮动态内容放进前缀 -> 前缀每轮都变，缓存基本失效", bad_results, bad_cache)

    base_cost = sum(total_cost(r.cost) for r in base_results)
    opt_cost = sum(total_cost(r.cost) for r in opt_results)
    base_lat = sum(r.latency for r in base_results) / len(base_results)
    opt_lat = sum(r.latency for r in opt_results) / len(opt_results)
    escalations = sum(r.escalations for r in opt_results)
    small = sum(1 for r in opt_results if r.model == "small")

    print("=" * 76)
    print("小结")
    print("  成本 ${:.4f} -> ${:.4f}（降幅 {:.1%}）".format(base_cost, opt_cost,
                                                       (base_cost - opt_cost) / base_cost))
    print("  平均延迟 {:.1f}s -> {:.1f}s（降幅 {:.1%}）".format(base_lat, opt_lat,
                                                          (base_lat - opt_lat) / base_lat))
    print("  路由分布：small {} 个 / large {} 个，级联升级 {} 次".format(
        small, len(opt_results) - small, escalations))
    print("  提示：代价换来的是质量前提——小模型只用在有校验判据的任务上，")
    print("        否则本实验里的'小模型通过率'必须先被真实评测测出来。")
    print()

    demo_semantic_cache()

    if args.live:
        run_live(args.model, args.base_url, os.environ.get("OPENAI_API_KEY", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
