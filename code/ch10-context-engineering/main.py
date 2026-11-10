#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch10 上下文工程最小可实现 —— 纯标准库，无需 API Key 就能跑通完整回路。

演示内容：
  1) 朴素拼装：全部内容直接拼接 -> 超出预算，关键约束落在中段
  2) 上下文组装器：分层 -> 压缩/淘汰/排序/按需加载 -> 缓存友好顺序 -> 预算报告
  3) 上下文腐化缓解：硬约束被结构化提取并前置/复述，位置从中段变成头尾

用法：
    python main.py                      # 默认预算 6000，对照朴素与组装两种排布
    python main.py --budget 4000        # 收紧预算，看降级动作
    python main.py --budget 100000      # 放宽预算，看检索排序的影响
    python main.py --layout naive       # 只跑朴素拼装
    python main.py --layout assembled   # 只跑组装器
    python main.py --trace              # 打印组装后的完整上下文
    python main.py --live               # 切真实模型

--live 所需环境变量：
    LLM_API_KEY    必填，模型服务商的 API Key
    LLM_BASE_URL   可选，默认 https://api.openai.com/v1
    LLM_MODEL      可选，默认 gpt-4o-mini
端点需兼容 `POST {base}/chat/completions`：body 含 model/messages，
返回 choices[0].message.content。换供应商时只改 base_url 与 model。

mock 说明：内置"模型"是一个窗口模型——只可靠读取上下文开头 20% 与结尾 20%，
用来近似"长上下文里中间信息被忽略"的现象。它不代表任何真实模型的表现。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# 1. token 估算（近似，不联网）
# --------------------------------------------------------------------------

# 关键约束只出现在历史第 8 轮——朴素拼装时它就落在中段
CONSTRAINT_KEYWORD = "不要修改数据库 schema"


def est_tokens(text: str) -> int:
    """粗略估算：CJK 字符按 1 token，ASCII 按 4 字符 1 token。

    真实 tokenizer 与此有出入，但相对比较够用——组装器要的是"哪块大"。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 4


# --------------------------------------------------------------------------
# 2. 层与块
# --------------------------------------------------------------------------


@dataclass
class Layer:
    name: str
    priority: int
    budget: int
    strategy: str
    blocks: List[str] = field(default_factory=list)
    before: int = 0
    after: int = 0
    action: str = "keep"


@dataclass
class Assembly:
    prompt: str
    layers: List[Layer]
    total_before: int
    total_after: int
    budget: int
    cache_prefix: int
    constraint_pos: str
    constraint_kept: bool


# --------------------------------------------------------------------------
# 3. 上下文组装器
# --------------------------------------------------------------------------

# 约束句的抽取：前缀非贪婪，避免把整行都吞进来（"上限"两个字可能出现在长句中间）
CONSTRAINT_RE = re.compile(r"[^。；\n]{0,16}?(?:不要|禁止|必须|上限|不超过|至少|截止|只能)[^。；\n]{0,40}")
ACTION_RE = re.compile(r"^\s*Action:\s*(.+)$", re.M)


class ContextAssembler:
    """把上下文当作预算来分配：分层 -> 按策略收敛 -> 缓存友好顺序拼接 -> 报告。"""

    def __init__(self, budget: int = 6000, layers: Optional[List[Layer]] = None) -> None:
        self.budget = budget
        self.layers = layers or [
            Layer("system", 0, 800, "keep"),
            Layer("tools", 1, 300, "load_on_demand"),
            Layer("task", 2, 600, "keep"),
            Layer("retrieval", 3, 2000, "rank_then_truncate"),
            Layer("history", 4, 3000, "compress_then_evict"),
            Layer("reminder", 5, 200, "keep"),
        ]
        self.by_name = {l.name: l for l in self.layers}

    # -- 各层策略 --------------------------------------------------------

    def _apply_tools(self, layer: Layer, task: str) -> None:
        """按需加载：只留下这一步用得上的工具定义。"""
        keep = [b for b in layer.blocks
                if any(word in b for word in TOOL_KEYWORDS if word in task)]
        if len(keep) < 2:
            keep = layer.blocks[:2]
        total = len(layer.blocks)
        layer.blocks = keep
        layer.action = "按需加载：保留 %d / %d 个工具" % (len(keep), total)

    def _apply_retrieval(self, layer: Layer, query_terms: List[str]) -> None:
        """先按相关度排序，再截断。"""
        def score(block: str) -> int:
            return sum(1 for t in query_terms if t and t in block)

        ranked = sorted(layer.blocks, key=score, reverse=True)
        total = len(layer.blocks)
        kept: List[str] = []
        used = 0
        for block in ranked:
            cost = est_tokens(block)
            if kept and used + cost > layer.budget:
                continue
            kept.append(block)
            used += cost
            if used >= layer.budget:
                break
        layer.blocks = kept
        layer.action = "按相关度排序后截断：保留 %d / %d 条" % (len(kept), total)

    def _apply_history(self, layer: Layer) -> None:
        """先压缩（结构化提取），再淘汰；永远保留最近两轮原文。"""
        recent = layer.blocks[-2:]
        old = layer.blocks[:-2]
        digest = compress_history(old)
        blocks = ([digest] if digest else []) + recent
        if sum(est_tokens(b) for b in blocks) > layer.budget:
            # 还超：截断摘要，并只保留最后一轮原文
            room = max(0, layer.budget - est_tokens(recent[-1])) if recent else layer.budget
            blocks = ([digest[: max(0, room * 2)]] if digest and room > 0 else []) + recent[-1:]
        layer.blocks = blocks
        layer.action = "压缩 %d 轮为结构化摘要 + 淘汰中段，保留最近 2 轮" % len(old)

    # -- 组装 ------------------------------------------------------------

    def assemble(self, task: str, history: List[str], retrieval: List[str],
                 tools: List[str], constraint_keyword: str = CONSTRAINT_KEYWORD
                 ) -> Assembly:
        query_terms = task_terms(task)
        constraint_text = extract_constraints(history)

        self.by_name["system"].blocks = [SYSTEM_PROMPT]
        self.by_name["tools"].blocks = list(tools)
        self.by_name["task"].blocks = [task] + ([constraint_text] if constraint_text else [])
        self.by_name["retrieval"].blocks = list(retrieval)
        self.by_name["history"].blocks = list(history)
        self.by_name["reminder"].blocks = (
            [REMINDER_TEMPLATE.format(c="schema")] if constraint_text else [])

        for layer in self.layers:
            layer.before = sum(est_tokens(b) for b in layer.blocks)

        # 按优先级依次处理超预算的层
        for layer in sorted(self.layers, key=lambda l: l.priority):
            used = sum(est_tokens(b) for b in layer.blocks)
            if used <= layer.budget:
                layer.after = used
                continue
            if layer.strategy == "load_on_demand":
                self._apply_tools(layer, task)
            elif layer.strategy == "rank_then_truncate":
                self._apply_retrieval(layer, query_terms)
            elif layer.strategy == "compress_then_evict":
                self._apply_history(layer)
            layer.after = sum(est_tokens(b) for b in layer.blocks)

        # 全局预算收敛：从优先级最低的层开始削
        total = sum(l.after for l in self.layers)
        for layer in sorted(self.layers, key=lambda l: -l.priority):
            if total <= self.budget:
                break
            if layer.name == "history" and len(layer.blocks) > 1:
                layer.blocks = layer.blocks[-1:]
                layer.action += "；全局预算收敛：只留最后一轮"
            elif layer.name == "retrieval" and layer.blocks:
                layer.blocks = layer.blocks[:1]
                layer.action += "；全局预算收敛：只留 top-1"
            layer.after = sum(est_tokens(b) for b in layer.blocks)
            total = sum(l.after for l in self.layers)

        # 缓存友好顺序：稳定块在前，变动块在后
        stable_names = {"system", "tools", "task"}
        parts: List[str] = []
        cache_prefix = 0
        prefix_open = True
        for layer in self.layers:
            for block in layer.blocks:
                chunk = "[%s]\n%s" % (layer.name, block)
                parts.append(chunk)
                if prefix_open:
                    if layer.name in stable_names:
                        cache_prefix += est_tokens(chunk)
                    else:
                        prefix_open = False
        prompt = "\n\n".join(parts)
        total_before = sum(l.before for l in self.layers)
        total_after = sum(l.after for l in self.layers)
        pos, kept = constraint_position(prompt, constraint_keyword)
        return Assembly(prompt, self.layers, total_before, total_after, self.budget,
                        cache_prefix, pos, kept)


def compress_history(turns: List[str]) -> str:
    """结构化提取：只留约束、动作与结论，丢掉过程叙述。

    摘要式压缩会丢细节还会编，结构化提取保真得多——
    Agent 下一步要的是状态，不是故事。
    """
    constraints: List[str] = []
    actions: List[str] = []
    for turn in turns:
        for m in CONSTRAINT_RE.finditer(turn):
            c = m.group(0).strip()
            if c and c not in constraints:
                constraints.append(c)
        for m in ACTION_RE.finditer(turn):
            a = m.group(1).strip()
            if a and a not in actions:
                actions.append(a)
    out: List[str] = ["[历史摘要]"]
    if constraints:
        out.append("约束：" + "；".join(constraints))
    if actions:
        out.append("动作：" + "；".join(actions[:8]))
    if len(out) == 1:
        return ""
    return "\n".join(out)


def extract_constraints(history: List[str]) -> str:
    """把历史里的硬约束单独拎出来——它们不能随历史一起被淘汰。"""
    found: List[str] = []
    for turn in history:
        for m in CONSTRAINT_RE.finditer(turn):
            c = m.group(0).strip()
            if c and c not in found:
                found.append(c)
    if not found:
        return ""
    return "[关键约束]\n" + "\n".join(found)


def task_terms(task: str) -> List[str]:
    """从任务里取检索关键词：中文按 2-gram，英文按单词。"""
    terms = set(re.findall(r"[A-Za-z_]{3,}", task))
    zh = re.sub(r"[^\u4e00-\u9fff]", "", task)
    for i in range(len(zh) - 1):
        terms.add(zh[i:i + 2])
    return [t for t in terms if t]


def constraint_position(prompt: str, keyword: str) -> Tuple[str, bool]:
    """关键事实在组装后上下文的相对位置。"""
    idx = prompt.find(keyword)
    if idx == -1:
        return "缺失", False
    rel = idx / max(1, len(prompt))
    if rel < 0.2:
        return "头部（前 20%）", True
    if rel > 0.8:
        return "尾部（后 20%）", True
    return "中段（20%～80%）", True


# --------------------------------------------------------------------------
# 4. 演示数据
# --------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "你是一个合同审阅助手。回答要给出依据，缺依据就说缺。"
    "涉及生产库结构变更、对外发送、金额确认的动作，必须先确认硬约束。"
)

REMINDER_TEMPLATE = "【再确认一次】本任务硬约束：{c}，除非用户明确批准，否则一律只读。"

TOOL_KEYWORDS = ["search", "write_report", "read_file", "query_db", "send_mail",
                 "create_ticket", "list_dir", "git_diff", "run_tests", "calc",
                 "translate", "ocr"]

TOOL_SPECS = [
    'search(query: str) -> str | 检索合同库，按关键词返回命中条款的片段、出处与合同编号；'
    '只读，无副作用；检索不到时返回空列表而不是报错。',
    'write_report(path: str, content: str) -> str | 把审阅报告写入工作目录下的文件；'
    '有落盘副作用，路径必须落在工作目录内；覆盖已有文件前需人工确认。',
    'read_file(path: str) -> str | 读取工作目录内的文本文件；只读；文件不存在时抛异常。',
    'query_db(table: str) -> list | 查询业务库表，返回字典列表；只读连接，禁止 DDL 与 DML；'
    '单次返回行数上限由数据层控制。',
    'send_mail(to: str, body: str) -> str | 发送邮件给收件人；不可撤销的外部副作用，'
    '同一收件人短时间重复发送会被网关拦截。',
    'create_ticket(title: str, body: str) -> str | 在工单系统创建条目；不可撤销，需要填对项目号。',
    'list_dir(path: str) -> list | 列出目录内容；只读；不递归。',
    'git_diff(repo: str) -> str | 返回仓库相对基线的差异；只读；大仓库建议先 list_dir 缩小范围。',
    'run_tests(repo: str) -> str | 执行测试并返回结果摘要；会消耗计算资源，超时会被中断。',
    'calc(expr: str) -> number | 计算算术表达式；纯函数，不支持变量与函数调用。',
    'translate(text: str, lang: str) -> str | 翻译文本；只读；长文本会分段处理。',
    'ocr(file: str) -> str | 识别图片中的文字；只读；对低清图片准确率下降。',
]

TASK = ("审阅与「华辰科技」的服务合同，找出违约责任相关条款，"
        "用 write_report 写一份报告写到 report.md，并说明本次交付是否需要变更生产库结构。")

HISTORY: List[str] = []
for i in range(1, 21):
    if i == 8:
        HISTORY.append(
            "第 %d 轮：助手调用 Action: query_db(table='contracts')，"
            "Observation: 命中三份合同，其中两份包含违约责任条款，一份是框架协议。\n"
            "用户补充：硬约束——本任务期间不要修改数据库 schema，"
            "在我明确批准之前一律只读，所有查询走只读视图；"
            "如果审阅过程中发现需要补录字段，先写成建议给我，不要自己去改。\n"
            "助手：明白，后续只做只读查询，不会触碰生产库结构，"
            "发现字段缺失的部分我会整理成待办清单发给你确认。" % i)
    elif i % 3 == 0:
        HISTORY.append(
            "第 %d 轮：助手调用 Action: search(query='合同 违约 责任 %d')，"
            "Observation: 检索到若干条款，其中相关度较高的两条集中在违约金计算与解除条件，"
            "其余命中项是格式模板，与本次标的无关。\n"
            "助手：这两条需要重点核对，我继续看附件里的补充协议，"
            "并把与主合同冲突的表述单独标出来，避免后面合并条款时漏掉。" % (i, i))
    else:
        HISTORY.append(
            "第 %d 轮：助手逐条核对合同正文，记录了交付期限、验收标准与付款节奏，"
            "并把每一条与往期同类合同做了对比，标注了三条措辞异常的地方："
            "一处把“工作日”写成了“自然日”，一处验收标准没有量化，还有一处责任上限被写成了全额；"
            "同时更新了合同要素表，把每份合同的编号、金额、生效日期填齐。\n"
            "助手：本轮没有新的发现，继续下一份；"
            "等全部核对完我再汇总成一份审阅意见，过程中不改任何原始记录。" % i)

RETRIEVAL: List[str] = []
for i in range(1, 31):
    if i == 7:
        RETRIEVAL.append(
            "[合同条款] 华辰科技服务合同 第 8 条 违约责任：逾期交付按日万分之五计违约金，"
            "累计上限不超过合同总额百分之二十；连续三十日未交付的，采购方可解除合同并要求返还预付款；"
            "因不可抗力导致的迟延需在五个工作日内书面通知并提供证明，否则不构成免责事由。")
    elif i == 19:
        RETRIEVAL.append(
            "[变更管理流程] 生产库结构变更须提交评审单并由业务负责人审批，"
            "未获批准不得执行任何 DDL；紧急变更需事后补审，"
            "变更窗口固定在周日凌晨，期间禁止任何写入操作；"
            "涉及历史数据的回填脚本必须先在预发环境跑通并留存执行记录。")
    else:
        RETRIEVAL.append(
            "[参考资料 %d] 与本次合同审阅无关的常规文档：报销流程说明、考勤与假期规定、"
            "年度体检安排、会议室预订规则、打印机使用说明、"
            "以及上一年度的团建通知与费用标准，仅供检索索引，不含合同条款。" % i)

# --------------------------------------------------------------------------
# 5. 模型接入
# --------------------------------------------------------------------------

QUESTION = "根据以上上下文，本次是否可以直接变更生产库结构？只回答：是/否，并说明依据。"


class MockWindowModel:
    """窗口模型：只可靠读取开头 20% 与结尾 20%。

    这是对"长上下文里中间信息被忽略"的现象的近似模拟，不是任何真实模型的行为。
    """

    def __init__(self, keyword: str = CONSTRAINT_KEYWORD) -> None:
        self.keyword = keyword

    def __call__(self, prompt: str) -> str:
        head = prompt[: max(1, int(len(prompt) * 0.2))]
        tail = prompt[max(0, int(len(prompt) * 0.8)):]
        if self.keyword in head or self.keyword in tail:
            return "否。依据：上下文写明本任务期间不要修改数据库 schema，未获批准前一律只读。"
        return "否……（没有找到关于生产库结构的硬约束，无法确认依据。）"


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
        body = {"model": self.model, "temperature": self.temperature,
                "messages": [{"role": "user",
                              "content": prompt + "\n\n" + QUESTION}]}
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.api_key}, method="POST")
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
# 6. 朴素拼装（对照组）
# --------------------------------------------------------------------------


def naive_assemble() -> str:
    parts = [SYSTEM_PROMPT, "\n\n".join(TOOL_SPECS), TASK,
             "\n\n".join(RETRIEVAL), "\n\n".join(HISTORY)]
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# 7. 报告
# --------------------------------------------------------------------------


def print_report(title: str, assembly: Optional[Assembly], prompt: str,
                 answer: str, keyword: str, budget: int = 6000) -> None:
    print("=" * 72)
    print(title)
    print("=" * 72)
    if assembly is None:
        pos, _kept = constraint_position(prompt, keyword)
        total = est_tokens(prompt)
        print("context tokens :", total)
        print("是否超预算     :",
              "是（超出 %d tokens）" % (total - budget) if total > budget else "否")
        print("关键约束位置   :", pos)
    else:
        print("%-12s %8s %8s  %s" % ("layer", "before", "after", "action"))
        for layer in assembly.layers:
            print("%-12s %8d %8d  %s" % (layer.name, layer.before, layer.after, layer.action))
        print("-" * 72)
        print("total          : %d -> %d  (budget=%d)"
              % (assembly.total_before, assembly.total_after, assembly.budget))
        print("cache_prefix   : %d tokens（稳定前缀，可缓存）" % assembly.cache_prefix)
        print("关键约束位置   :", assembly.constraint_pos)
    print("-" * 72)
    print("模型回答       :", answer.replace("\n", " "))
    print("=" * 72)


# --------------------------------------------------------------------------
# 8. CLI
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
        return MockWindowModel()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "--live 需要环境变量 LLM_API_KEY；"
            "可选 LLM_BASE_URL（默认 https://api.openai.com/v1）与 LLM_MODEL。\n")
        raise SystemExit(2)
    base_url = args.base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model = args.model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    return LiveLLM(base_url, api_key, model, temperature=args.temperature)


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="上下文工程最小可实现（mock 模式无需 API Key）")
    parser.add_argument("--budget", type=int, default=6000, help="总 token 预算")
    parser.add_argument("--layout", default="both", choices=["naive", "assembled", "both"])
    parser.add_argument("--live", action="store_true", help="切真实模型 API")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--model", default="", help="覆盖 LLM_MODEL")
    parser.add_argument("--base-url", default="", help="覆盖 LLM_BASE_URL")
    parser.add_argument("--trace", action="store_true", help="打印组装后的完整上下文")
    args = parser.parse_args(argv)

    llm = build_llm(args)

    if args.layout in ("naive", "both"):
        naive = naive_assemble()
        answer = llm(naive)
        print_report("朴素拼装（不做任何管理）", None, naive, answer,
                     CONSTRAINT_KEYWORD, args.budget)
        if args.trace:
            print(naive)

    if args.layout in ("assembled", "both"):
        assembler = ContextAssembler(budget=args.budget)
        assembly = assembler.assemble(TASK, HISTORY, RETRIEVAL, TOOL_SPECS,
                                      constraint_keyword=CONSTRAINT_KEYWORD)
        answer = llm(assembly.prompt)
        print_report("上下文组装器", assembly, assembly.prompt, answer,
                     CONSTRAINT_KEYWORD, args.budget)
        if args.trace:
            print(assembly.prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
