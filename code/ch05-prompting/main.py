#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch05 提示增强技术（CoT / Few-shot / Self-ask / Least-to-Most）最小可实现。

纯标准库，无需 API Key 就能跑通整条评测流水线：
    策略矩阵 -> 提示装配（纯函数） -> LLM 调用 -> 判分 -> 指标表

六种策略：direct / cot / fewshot / cot_fewshot / selfask / ltm
两个任务：一个需要中间计算，一个纯事实——用来观察 CoT 类技巧在两类任务上的差别。

用法：
    python main.py                       # mock 模式，6 策略 x 2 任务
    python main.py --strategies cot,ltm  # 只跑部分策略
    python main.py --repeat 3            # 每个任务重复 3 次，看均值与方差
    python main.py --trace               # 打印每个策略装配出的完整 prompt
    python main.py --live                # 切真实模型（需要环境变量）

--live 所需环境变量：
    LLM_API_KEY    必填，模型服务商的 API Key
    LLM_BASE_URL   可选，默认 https://api.openai.com/v1
    LLM_MODEL      可选，默认 gpt-4o-mini
端点需兼容 `POST {base}/chat/completions`：body 含 model/messages，
返回 choices[0].message.content。换供应商时只改 base_url 与 model。

重要：内置 mock 不是真在推理，它按提示里的特征标记模拟行为，
用来离线跑通并回归评测流水线，不代表任何真实模型的表现。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# 1. 任务与示例
# --------------------------------------------------------------------------


@dataclass
class Task:
    task_id: str
    prompt: str
    answer: str
    kind: str


TASKS: List[Task] = [
    Task(
        task_id="geometry",
        prompt="一个长方形的周长是 22 米，长是 8 米，它的面积是多少平方米？",
        answer="24",
        kind="reasoning",
    ),
    Task(
        task_id="fact",
        prompt="阿尔伯特·爱因斯坦出生于哪一年？",
        answer="1879",
        kind="fact",
    ),
]


@dataclass
class Exemplar:
    input_text: str
    output_text: str


# 示例只教"输出格式"，不教推理：两道都不需要中间计算
EXEMPLARS: List[Exemplar] = [
    Exemplar("3 个苹果和 4 个梨一共几个水果？", "答案：7"),
    Exemplar("这本书 15 元，那本 8 元，一共多少钱？", "答案：23"),
]

# --------------------------------------------------------------------------
# 2. 提示片段与装配（纯函数，可单测、可快照）
# --------------------------------------------------------------------------

TASK_TEMPLATE = """任务：{task}
输出要求：最后一行必须是 `答案：<最终答案>`，不要输出其他格式。"""

EXEMPLARS_HEADER = "以下是示例："

COT_INSTRUCTION = "Let's think step by step.（请先写出中间推理步骤，再给最终答案。）"

SELFASK_INSTRUCTION = (
    "Self-ask：在给出最终答案前，先自己提出并回答若干澄清性子问题"
    "（每个子问题一行，写清问题与答案），然后再作答。"
)

LTM_INSTRUCTION = (
    "Least-to-Most：先把问题拆成从简单到难的子问题，按顺序逐个求解"
    "（每步一行），再用前面的结果解后面的，最后作答。"
)

ALL_STRATEGIES = ["direct", "cot", "fewshot", "cot_fewshot", "selfask", "ltm"]


def format_examples(examples: List[Exemplar]) -> List[str]:
    """格式一致性是硬要求：所有示例用同一套标签与行序。"""
    out: List[str] = []
    for i, ex in enumerate(examples, 1):
        out.append("示例 %d\n输入：%s\n输出：%s" % (i, ex.input_text, ex.output_text))
    return out


def build_prompt(task: Task, examples: List[Exemplar], strategy: str) -> str:
    """把任务 + 示例 + 策略装配成一条 prompt。纯函数：同输入必得同输出。"""
    parts: List[str] = [TASK_TEMPLATE.format(task=task.prompt)]
    if strategy in ("fewshot", "cot_fewshot"):
        parts.append(EXEMPLARS_HEADER)
        parts.extend(format_examples(examples))
    if strategy in ("cot", "cot_fewshot"):
        parts.append(COT_INSTRUCTION)
    if strategy == "selfask":
        parts.append(SELFASK_INSTRUCTION)
    if strategy == "ltm":
        parts.append(LTM_INSTRUCTION)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# 3. 判分与指标
# --------------------------------------------------------------------------

ANSWER_RE = re.compile(r"答案\s*[:：]\s*(.+)")
FORMAT_RE = re.compile(r"答案\s*[:：]")


def extract_answer(text: str) -> str:
    """取最后一行 `答案：xxx`；没有就退回最后一行非空文本。"""
    matches = ANSWER_RE.findall(text)
    if matches:
        return matches[-1].strip()
    for line in reversed(text.strip().splitlines()):
        if line.strip():
            return line.strip()
    return ""


def is_correct(text: str, expected: str) -> bool:
    """数字按完整数字段匹配，避免 21 命中 210。"""
    return bool(re.search(r"(?<!\d)%s(?!\d)" % re.escape(expected), text))


def format_ok(text: str) -> bool:
    return bool(FORMAT_RE.search(text))


# --------------------------------------------------------------------------
# 4. 模型接入：MockLLM / LiveLLM
# --------------------------------------------------------------------------


class MockLLM:
    """按提示里的特征标记模拟模型行为，离线跑通评测流水线。

    这是行为模拟，不是推理：它不读懂题目，只看提示里有没有
    "step by step" / "Self-ask" / "Least-to-Most" / 示例块，
    然后返回该场景下的典型形态。
    """

    def __call__(self, prompt: str) -> str:
        low = prompt.lower()
        is_cot = "step by step" in low
        is_selfask = "self-ask" in low
        is_ltm = "least-to-most" in low
        has_exemplar = "以下是示例" in prompt
        structured = is_cot or is_selfask or is_ltm

        if "周长是 22" in prompt:
            if structured:
                lines: List[str] = []
                if is_selfask:
                    lines += [
                        "子问题 1：周长 22 米的矩形，长与宽之和是多少？",
                        "子问题 1 答案：11 米。",
                        "子问题 2：宽是多少？",
                        "子问题 2 答案：11 - 8 = 3 米。",
                        "子问题 3：面积是多少？",
                        "子问题 3 答案：8 × 3 = 24 平方米。",
                    ]
                elif is_ltm:
                    lines += [
                        "第一步（最简单）：长 + 宽 = 周长 / 2 = 22 / 2 = 11。",
                        "第二步：用上一步的结果，宽 = 11 - 8 = 3。",
                        "第三步（最难）：面积 = 长 × 宽 = 8 × 3 = 24。",
                    ]
                else:
                    lines += [
                        "第一步：长 + 宽 = 22 / 2 = 11。",
                        "第二步：宽 = 11 - 8 = 3。",
                        "第三步：面积 = 8 × 3 = 24。",
                    ]
                lines.append("答案：24")
                return "\n".join(lines)
            if has_exemplar:
                return "答案：22"          # 格式学到了，计算仍然错
            return "面积是 22 平方米"      # 既错又不守格式

        if "爱因斯坦" in prompt:
            if structured:
                return "步骤：确认爱因斯坦的出生年份。\n答案：1879"
            if has_exemplar:
                return "答案：1879"
            return "爱因斯坦出生于 1879 年。"

        return "答案：无法判断"


class LiveLLM:
    """真实模型：OpenAI 兼容的 chat completions 协议。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 60, temperature: float = 0.0) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    def __call__(self, prompt: str) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是一个严格按要求作答的助手。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError("HTTP %s from %s: %s" % (exc.code, self.url, detail)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("cannot reach %s: %s" % (self.url, exc.reason)) from exc
        return data["choices"][0]["message"]["content"] or ""


# --------------------------------------------------------------------------
# 5. 评测流水线
# --------------------------------------------------------------------------


@dataclass
class Row:
    strategy: str
    correct: int = 0
    total: int = 0
    fmt_ok: int = 0
    chars: int = 0
    millis: float = 0.0
    per_task: Dict[str, bool] = field(default_factory=dict)

    @property
    def acc(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def fmt_rate(self) -> float:
        return self.fmt_ok / self.total if self.total else 0.0

    @property
    def avg_chars(self) -> float:
        return self.chars / self.total if self.total else 0.0

    @property
    def avg_ms(self) -> float:
        return self.millis / self.total if self.total else 0.0


def evaluate(llm: Callable[[str], str], strategies: List[str],
             tasks: List[Task], examples: List[Exemplar],
             repeat: int = 1, trace: bool = False) -> List[Row]:
    rows: List[Row] = []
    for strategy in strategies:
        row = Row(strategy=strategy)
        for task in tasks:
            prompt = build_prompt(task, examples, strategy)
            if trace and task is tasks[0]:
                print("\n" + "=" * 72)
                print("[prompt:%s]" % strategy)
                print(prompt)
                print("=" * 72)
            for _ in range(repeat):
                t0 = time.perf_counter()
                out = llm(prompt)
                dt = (time.perf_counter() - t0) * 1000.0
                ok = is_correct(out, task.answer)
                row.total += 1
                row.correct += 1 if ok else 0
                row.fmt_ok += 1 if format_ok(out) else 0
                row.chars += len(out)
                row.millis += dt
                row.per_task[task.task_id] = ok
        rows.append(row)
    return rows


def print_table(rows: List[Row], tasks: List[Task]) -> None:
    headers = ["strategy", "acc", "fmt_rate", "avg_chars", "avg_ms"]
    for t in tasks:
        headers.append(t.task_id)
    widths = [max(len(h), 10) for h in headers]
    widths[0] = max(widths[0], max(len(r.strategy) for r in rows) + 1)

    def fmt_row(cells: List[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths))

    print(fmt_row(headers))
    print("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for r in rows:
        cells = [
            r.strategy,
            "%.2f" % r.acc,
            "%.2f" % r.fmt_rate,
            "%.1f" % r.avg_chars,
            "%.1f" % r.avg_ms,
        ]
        for t in tasks:
            cells.append("OK" if r.per_task.get(t.task_id) else "MISS")
        print(fmt_row(cells))


# --------------------------------------------------------------------------
# 6. CLI
# --------------------------------------------------------------------------


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def build_llm(args: argparse.Namespace) -> Callable[[str], str]:
    if not args.live:
        return MockLLM()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "--live 需要环境变量 LLM_API_KEY；"
            "可选 LLM_BASE_URL（默认 https://api.openai.com/v1）与 LLM_MODEL。\n"
        )
        raise SystemExit(2)
    base_url = args.base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model = args.model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    return LiveLLM(base_url, api_key, model, temperature=args.temperature)


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="提示增强技术评测器（mock 模式无需 API Key）")
    parser.add_argument("--strategies", default=",".join(ALL_STRATEGIES),
                        help="逗号分隔，默认全部六种")
    parser.add_argument("--tasks", default=",".join(t.task_id for t in TASKS),
                        help="逗号分隔，默认 geometry,fact")
    parser.add_argument("--repeat", type=int, default=1, help="每个任务重复次数")
    parser.add_argument("--live", action="store_true", help="切真实模型 API")
    parser.add_argument("--temperature", type=float, default=0.0, help="live 模式采样温度")
    parser.add_argument("--model", default="", help="覆盖 LLM_MODEL")
    parser.add_argument("--base-url", default="", help="覆盖 LLM_BASE_URL")
    parser.add_argument("--trace", action="store_true", help="打印每种策略装配出的 prompt")
    args = parser.parse_args(argv)

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    for s in strategies:
        if s not in ALL_STRATEGIES:
            parser.error("未知策略 %r，可选：%s" % (s, ", ".join(ALL_STRATEGIES)))

    wanted = {t.strip() for t in args.tasks.split(",") if t.strip()}
    tasks = [t for t in TASKS if t.task_id in wanted]
    if not tasks:
        parser.error("没有匹配的任务，可选：%s" % ", ".join(t.task_id for t in TASKS))

    llm = build_llm(args)
    rows = evaluate(llm, strategies, tasks, EXEMPLARS,
                    repeat=max(1, args.repeat), trace=args.trace)

    print("=" * 72)
    print("mode    :", "live" if args.live else "mock（行为模拟，非真实模型表现）")
    print("tasks   :", ", ".join(t.task_id for t in tasks))
    print("repeat  :", args.repeat)
    print("-" * 72)
    print_table(rows, tasks)
    print("-" * 72)
    print("acc = 正确率；fmt_rate = 末行 `答案：x` 的格式合规率；")
    print("avg_chars = 平均输出字符（成本代理）；avg_ms = 平均延迟毫秒。")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
