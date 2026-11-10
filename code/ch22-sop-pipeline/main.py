#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch22 SOP 流水线：MetaGPT、ChatDev 与 AgentVerse 的软件公司隐喻 最小可实现
—— 纯标准库，无需 API Key 就能跑通完整回路。

演示内容：
  1) 阶段契约：每个阶段声明"读哪些标准文档、写哪些标准文档"，下游只认契约
  2) 标准文档作为共享状态：版本化、审计日志、回放、失效判定
  3) 评审回路：评审打回指定阶段，带返工次数上限
  4) 成本对照：契约式输入（只读该读的） vs 全量共享输入（什么都读）
  5) 上游变更 → 下游失效 → 增量重跑（只重跑脏掉的那几段）
  6) SOP 思想迁移到非软件任务：换一套阶段契约，同一个引擎照跑

用法：
    python main.py                      # 跑全部演示
    python main.py --trace              # 打印每一步的完整提示
    python main.py --live               # 切真实模型

--live 所需环境变量：
    LLM_API_KEY    必填，模型服务商的 API Key
    LLM_BASE_URL   可选，默认 https://api.openai.com/v1
    LLM_MODEL      可选，默认 gpt-4o-mini
端点需兼容 `POST {base}/chat/completions`：body 含 model/messages，
返回 choices[0].message.content。换供应商时只改 base_url 与 model。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# --------------------------------------------------------------------------
# 1. token 计量
# --------------------------------------------------------------------------


def approx_tokens(text: str) -> int:
    """粗估 token 数：中文按字计，其余按 ~3.5 字符/token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + int((len(text) - cjk) / 3.5) + 1


class TokenMeter:
    def __init__(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def reset(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def record_call(self) -> None:
        self.calls += 1

    def add(self, p: int, c: int) -> None:
        self.prompt_tokens += p
        self.completion_tokens += c

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# --------------------------------------------------------------------------
# 2. 标准文档：流水线的共享状态
# --------------------------------------------------------------------------


@dataclass
class Artifact:
    name: str
    version: int
    content: str
    producer: str
    inputs: Dict[str, int] = field(default_factory=dict)  # 输入文档 -> 当时的版本
    round: int = 0
    at: float = 0.0


class ArtifactStore:
    """标准文档仓库：当前版本 + 全部历史版本 + 审计日志。

    这是"共享文档作为状态载体"的落点：
      - 可审计：每份文档记录了是谁、读着哪几个版本、产的；
      - 可缓存：文档有稳定版本号，下游命中缓存就不用重算；
      - 可回放：历史版本全在，可以把任意一次运行还原出来；
      - 可判失效：上游版本一变，下游立刻知道自己是脏的。
    """

    def __init__(self) -> None:
        self.current: Dict[str, Artifact] = {}
        self.history: Dict[str, List[Artifact]] = {}
        self.audit: List[Dict[str, Any]] = []

    def put(self, art: Artifact) -> Artifact:
        old = self.current.get(art.name)
        art.version = (old.version + 1) if old else 1
        art.at = time.time()
        self.current[art.name] = art
        self.history.setdefault(art.name, []).append(art)
        return art

    def get(self, name: str) -> Optional[Artifact]:
        return self.current.get(name)

    def versions(self, name: str) -> List[int]:
        return [a.version for a in self.history.get(name, [])]

    def stale_set(self) -> Set[str]:
        """失效判定：输入版本对不上，或输入本身已失效，则本文档也失效。

        传递闭包——PRD 改了，API 脏，API 脏了，实现与测试跟着脏。
        """
        stale: Set[str] = set()
        changed = True
        while changed:
            changed = False
            for name, art in self.current.items():
                if name in stale:
                    continue
                for dep, ver in art.inputs.items():
                    cur = self.current.get(dep)
                    if cur is None or cur.version != ver or dep in stale:
                        stale.add(name)
                        changed = True
                        break
        return stale


# --------------------------------------------------------------------------
# 3. 阶段契约
# --------------------------------------------------------------------------


@dataclass
class Stage:
    name: str
    role: str
    reads: List[str]                 # 读哪些标准文档
    writes: List[str]                # 写哪些标准文档
    instruction: str                 # 这个阶段要干什么
    format_spec: str = ""            # 产物格式约束
    is_review: bool = False          # 是否是评审阶段
    rework_to: Optional[str] = None  # 打回到哪个阶段


OUTPUT_FORMAT = (
    '输出严格为一个 JSON 对象：{"artifacts": {"<文档名>": "<文档全文>"}}。'
    '文档全文用 Markdown，不要加解释。'
)


class Pipeline:
    """SOP 流水线引擎：阶段按顺序执行，靠标准文档交接，评审可打回。"""

    def __init__(self, stages: List[Stage], llm, store: ArtifactStore,
                 max_rework: int = 2, read_all: bool = False,
                 trace: bool = False) -> None:
        self.stages = stages
        self.llm = llm
        self.store = store
        self.max_rework = max_rework
        self.read_all = read_all          # True 时每个阶段都读全部文档（对照组）
        self.trace = trace
        self.rework_count = 0
        self.stop_reason = ""

    def _stage_by_name(self, name: str) -> Optional[Stage]:
        for s in self.stages:
            if s.name == name:
                return s
        return None

    def _build_prompt(self, stage: Stage, seeds: Dict[str, str]) -> Tuple[str, Dict[str, int]]:
        """拼装阶段提示，并记录这一步读了哪些文档的哪个版本。"""
        reads: Dict[str, int] = {}
        blocks: List[str] = []
        names = list(self.store.current.keys()) if self.read_all else list(stage.reads)
        for name in names:
            art = self.store.get(name)
            if art is None:
                continue
            reads[name] = art.version
            blocks.append("----- 输入文档：%s（v%d，由 %s 产出）-----\n%s"
                          % (name, art.version, art.producer, art.content))
        for name in stage.reads:
            if name in seeds:
                blocks.append("----- 输入需求：%s -----\n%s" % (name, seeds[name]))

        prompt = ("【阶段】%s\n【角色】%s\n\n【任务】%s\n\n"
                  "【输出格式】%s\n\n【可用输入】\n%s"
                  % (stage.name, stage.role, stage.instruction,
                     stage.format_spec or OUTPUT_FORMAT,
                     "\n\n".join(blocks) if blocks else "（无）"))
        return prompt, reads

    def _parse(self, raw: str, stage: Stage) -> Dict[str, Any]:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                payload = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                payload.setdefault("artifacts", {})
                return payload
        # 解析失败就把原文当成产物，别让流水线直接崩
        return {"artifacts": {name: raw for name in stage.writes}}

    def run(self, seeds: Dict[str, str],
            only_stages: Optional[Set[str]] = None) -> Dict[str, Any]:
        """执行流水线。only_stages 非空时只跑这些阶段（增量重跑）。"""
        idx = 0
        executed: List[str] = []
        self.executed = executed
        while idx < len(self.stages):
            stage = self.stages[idx]
            if only_stages and not (set(stage.writes) & only_stages):
                idx += 1
                continue

            prompt, reads = self._build_prompt(stage, seeds)
            if self.trace:
                print("\n" + "=" * 70 + "\n[提示] %s\n" % stage.name + "=" * 70)
                print(prompt)

            before_prompt = self.llm.meter.prompt_tokens
            before_completion = self.llm.meter.completion_tokens
            raw = self.llm.chat(stage.role, stage.instruction, prompt)
            prompt_tokens = self.llm.meter.prompt_tokens - before_prompt
            completion_tokens = self.llm.meter.completion_tokens - before_completion
            payload = self._parse(raw, stage)

            written = []
            for name in stage.writes:
                content = payload.get("artifacts", {}).get(name, "")
                art = self.store.put(Artifact(name=name, version=0, content=content,
                                              producer=stage.name, inputs=dict(reads),
                                              round=self.rework_count + 1))
                written.append("%s@v%d" % (name, art.version))

            verdict = payload.get("verdict", "")
            self.store.audit.append({
                "stage": stage.name, "role": stage.role,
                "read": ["%s@v%d" % (k, v) for k, v in reads.items()],
                "wrote": written, "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens, "verdict": verdict,
            })
            executed.append(stage.name)

            if stage.is_review and verdict == "rework":
                target = payload.get("rework_stage") or stage.rework_to
                if self.rework_count >= self.max_rework:
                    self.stop_reason = "rework_limit"
                    break
                self.rework_count += 1
                target_idx = next((i for i, s in enumerate(self.stages) if s.name == target), None)
                if target_idx is None:
                    self.stop_reason = "bad_rework_target"
                    break
                idx = target_idx
                continue

            idx += 1
        else:
            self.stop_reason = "completed"
        if not self.stop_reason:
            self.stop_reason = "completed"
        return {"executed": executed, "rework": self.rework_count,
                "stop_reason": self.stop_reason}


# --------------------------------------------------------------------------
# 4. mock 与真实模型
# --------------------------------------------------------------------------

PRD = """# PRD：退款与赔付工单自动核对报表

## 背景
客服每天手工核对退款与赔付工单，口径不统一导致对不上账，月底集中对账要三天。

## 目标
每日 08:00 前出报表，逐单可追溯，人工核对零差异。

## 用户故事
作为客服主管，我希望导出当天待核对的工单，一眼看出金额不一致的单子。

## 验收标准
1. 取昨天 00:00-24:00 的工单，按订单号去重；
2. 标记退款金额与系统台账不一致的工单，并给出差异原因；
3. 导出 CSV，含工单号、退款金额、台账金额、差异值、原因、核对状态。

## 非目标
- 本期不做自定义筛选与历史归档查询；
- 本期不接入结算系统。
"""

API = """# 接口定义 v1

## GET /api/v1/reconcile/tickets?date=YYYY-MM-DD&status=pending&page=1&page_size=100
用途：取指定日期待核对的工单列表（按订单号去重）
返回：{ "items": [ { "ticket_id", "order_no", "refund_amount", "ledger_amount", "diff", "reason", "status" } ] }
错误码：400 date 格式非法；401 未登录；403 无工单权限；429 触发限流

## POST /api/v1/reconcile/export
用途：导出核对结果 CSV
入参：{ "date": "YYYY-MM-DD" }
返回：{ "file_url", "rows", "generated_at" }
错误码：400 参数缺失；409 当日核对尚未完成；500 导出失败

## 数据模型
Ticket { ticket_id, order_no, refund_amount, ledger_amount, diff, reason, status }
金额一律用「分」的整数表示，比较前不做浮点换算。
"""

TASKS = """# 任务清单
- [ ] T1 实现 GET /api/v1/reconcile/tickets 的查询、分页与按订单号去重
- [ ] T2 金额以分为单位做整数比较，diff = 退款金额 - 台账金额
- [ ] T3 生成差异原因（台账缺失 / 金额不一致 / 状态未回写）
- [ ] T4 实现 POST /api/v1/reconcile/export，异步生成 CSV 并落对象存储
- [ ] T5 补齐错误码、分页越界校验与工单权限校验
"""

CODE = """# 实现要点
def list_tickets(date, status="pending", page=1, page_size=100):
    rows = ticket_repo.query(date=date, status=status)
    merged = {}
    for r in rows:                      # 按订单号去重，保留最新一条
        merged[r.order_no] = r
    items = []
    for r in merged.values():
        diff = r.refund_amount_cents - r.ledger_amount_cents
        items.append({
            "ticket_id": r.ticket_id, "order_no": r.order_no,
            "refund_amount": r.refund_amount_cents,
            "ledger_amount": r.ledger_amount_cents,
            "diff": diff, "reason": classify(diff, r), "status": r.status,
        })
    return {"items": items[page_size * (page - 1): page_size * page]}
"""

TESTS = """# 测试用例
1. 同一订单号 200 条工单 → 去重后 150 条，金额以分为单位比较
2. 退款金额与台账一致 → diff=0，reason 为空
3. 台账缺失 → diff 等于退款金额，reason="台账缺失"
4. date 参数非法 → 400；page 越界 → 400
5. 当日核对未完成时调用导出 → 409
6. 无工单权限 → 403；限流 → 429
"""

REVIEW_REWORK = """# 评审报告（第 1 轮）
结论：打回
问题：T1 的分页接口没有定义 page、page_size 与越界错误码，前端无法处理；
     另外金额口径未在接口层注明单位，容易被调用方按元理解。
目标阶段：工程师
"""

REVIEW_APPROVE = """# 评审报告（第 2 轮）
结论：通过
问题：页码参数与 400 越界错误码已补齐，金额单位已在接口层注明；
     用例 4、6 覆盖了越界与权限分支。
"""


def make_software_stages() -> List[Stage]:
    return [
        Stage(
            name="产品经理", role="产品经理",
            reads=["需求"], writes=["prd"],
            instruction="把用户需求整理成一份可验收的 PRD，包含背景、目标、用户故事、"
                        "验收标准与非目标。验收标准必须能逐条测。",
        ),
        Stage(
            name="架构师", role="架构师",
            reads=["prd"], writes=["api"],
            instruction="依据 PRD 写出接口定义：方法与路径、入参出参、错误码、数据模型。"
                        "金额一律注明单位。不要写实现代码。",
        ),
        Stage(
            name="工程师", role="工程师",
            reads=["api"], writes=["tasks", "code"],
            instruction="把接口定义拆成可执行的任务清单，并给出核心实现。"
                        "任务清单里的每条都要能对应到接口里的一个字段或一个错误码。",
        ),
        Stage(
            name="测试工程师", role="测试工程师",
            reads=["api", "code"], writes=["tests"],
            instruction="依据接口定义与实现写测试用例，覆盖正常路径与异常分支，"
                        "每条用例写明输入与预期输出。",
        ),
        Stage(
            name="评审", role="评审",
            reads=["prd", "api", "tests"], writes=["review"],
            instruction="对照 PRD 的验收标准评审接口定义与测试用例。"
                        "不通过就写 verdict=rework 并指定 rework_stage，通过就写 verdict=approve。",
            format_spec=OUTPUT_FORMAT + ' 额外字段：{"verdict": "approve|rework", '
                                         '"rework_stage": "<阶段名>", "issues": ["..."]}',
            is_review=True, rework_to="工程师",
        ),
    ]


class MockLLM:
    """确定性 mock：按角色返回固定的标准文档，不做真实推理。"""

    name = "mock"

    def __init__(self, payloads: Dict[str, Any]) -> None:
        self.payloads = payloads
        self.meter = TokenMeter()
        self.state: Dict[str, Any] = {}

    def chat(self, role: str, system: str, user: str) -> str:
        self.meter.record_call()
        self.meter.add(approx_tokens(system) + approx_tokens(user), 0)
        payload = self.payloads.get(role)
        if callable(payload):
            payload = payload(self.state)
        if payload is None:
            payload = {"artifacts": {}}
        text = json.dumps(payload, ensure_ascii=False)
        self.meter.add(0, approx_tokens(text))
        return text


def software_payloads() -> Dict[str, Any]:
    def review(state: Dict[str, Any]) -> Dict[str, Any]:
        n = state.get("review_round", 0)
        state["review_round"] = n + 1
        if n == 0:
            return {"artifacts": {"review": REVIEW_REWORK},
                    "verdict": "rework", "rework_stage": "工程师", "issues": ["缺分页参数与越界错误码"]}
        return {"artifacts": {"review": REVIEW_APPROVE},
                "verdict": "approve", "rework_stage": None, "issues": []}

    return {
        "产品经理": {"artifacts": {"prd": PRD}},
        "架构师": {"artifacts": {"api": API}},
        "工程师": {"artifacts": {"tasks": TASKS, "code": CODE}},
        "测试工程师": {"artifacts": {"tests": TESTS}},
        "评审": review,
    }


class LiveLLM:
    """真实模型：OpenAI 兼容的 chat completions。"""

    name = "live"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 60) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.meter = TokenMeter()

    def chat(self, role: str, system: str, user: str) -> str:
        self.meter.record_call()
        body = {"model": self.model, "temperature": 0.2,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.api_key}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError("HTTP %s: %s" % (exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("cannot reach %s: %s" % (self.url, exc.reason)) from exc
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        self.meter.add(usage.get("prompt_tokens", approx_tokens(system + user)),
                       usage.get("completion_tokens", approx_tokens(text)))
        return text


# --------------------------------------------------------------------------
# 5. 演示
# --------------------------------------------------------------------------


def _pad(text: str, width: int) -> str:
    """按显示宽度左对齐补空格：CJK 字符占两列。"""
    w = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - w)


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def build_llm(args: argparse.Namespace, payloads=None):
    if not args.live:
        return MockLLM(payloads or {})
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "--live 需要环境变量 LLM_API_KEY；"
            "可选 LLM_BASE_URL（默认 https://api.openai.com/v1）与 LLM_MODEL。\n")
        raise SystemExit(2)
    return LiveLLM(args.base_url, api_key, args.model)


def demo_software(args: argparse.Namespace) -> Tuple[ArtifactStore, Pipeline]:
    """A/B 段：软件流水线跑一遍。"""
    stages = make_software_stages()
    llm = build_llm(args, software_payloads())
    store = ArtifactStore()
    pipeline = Pipeline(stages, llm, store, max_rework=2, trace=args.trace)

    print("=" * 78)
    print("A. 阶段契约（下游只认契约，不认上游的聊天记录）")
    print("=" * 78)
    print("%s %s %s %s" % (_pad("阶段", 14), _pad("角色", 14), _pad("读", 18), "写"))
    for s in stages:
        print("%s %s %s %s" % (_pad(s.name, 14), _pad(s.role, 14),
                               _pad("、".join(s.reads) or "—", 18), "、".join(s.writes)))

    print("=" * 78)
    print("B. 执行审计（谁读了哪个版本、产了什么、用了多少 token）")
    print("=" * 78)
    pipeline.run({"需求": "客服每天要手工核对退款和赔付工单，口径不统一导致对不上账，"
                           "希望每天早上自动出一份能追溯到单号的核对报表。"})
    print("%s %s %s %s %s" % (_pad("阶段", 14), _pad("读", 24), _pad("写", 16),
                               _pad("token", 10), "结论"))
    for entry in store.audit:
        cost = entry["prompt_tokens"] + entry["completion_tokens"]
        print("%s %s %s %s %s"
              % (_pad(entry["stage"], 14), _pad("、".join(entry["read"]) or "—", 24),
                 _pad("、".join(entry["wrote"]), 16), _pad(str(cost), 10),
                 entry["verdict"] or "—"))
    print("\n终止原因 %s｜返工 %d 次｜模型调用 %d 次｜总 token %d"
          % (pipeline.stop_reason, pipeline.rework_count, llm.meter.calls, llm.meter.total))
    return store, pipeline


def demo_cost(args: argparse.Namespace) -> None:
    """C 段：契约式输入 vs 全量共享输入。"""
    print("=" * 78)
    print("C. 成本对照：只读该读的 vs 什么都读")
    print("=" * 78)
    stages = make_software_stages()
    seeds = {"需求": "客服每天要手工核对退款和赔付工单，口径不统一导致对不上账，"
                     "希望每天早上自动出一份能追溯到单号的核对报表。"}

    rows = []
    for read_all, label in ((False, "契约式输入（只读声明的输入）"),
                            (True, "全量共享输入（每阶段都读全部文档）")):
        llm = MockLLM(software_payloads())
        store = ArtifactStore()
        pipeline = Pipeline(stages, llm, store, max_rework=2, read_all=read_all)
        pipeline.run(seeds)
        rows.append((label, llm.meter.calls, llm.meter.prompt_tokens,
                     llm.meter.total))
    base = rows[0][3]
    for label, calls, prompt_tokens, total in rows:
        print("%s %s %s %s"
              % (_pad(label, 30), _pad("%d 次" % calls, 10),
                 _pad("输入 %d" % prompt_tokens, 16), "总 token %d" % total))
    print("\n全量共享比契约式多花 %.1f 倍：SOP 的成本优势不在模型，在契约。"
          % (rows[1][3] / max(1, base)))


def demo_staleness(args: argparse.Namespace, store: ArtifactStore) -> None:
    """D 段：上游变更导致下游失效，再增量重跑。"""
    print("=" * 78)
    print("D. 上游变更 → 下游失效 → 增量重跑")
    print("=" * 78)
    print("文档版本谱系：")
    for name in store.history:
        print("  %-8s v%s" % (name, "、v".join(str(v) for v in store.versions(name))))

    # 模拟：产品经理改了 PRD
    edited = PRD.replace("本期不做自定义筛选与历史归档查询；",
                          "本期不做自定义筛选与历史归档查询；新增：支持按渠道筛选；")
    store.put(Artifact(name="prd", version=0, content=edited, producer="产品经理（人工修改）",
                       inputs={}, round=0))
    stale = store.stale_set()
    print("\n改了 PRD 之后，失效的文档：%s" % ("、".join(sorted(stale)) or "无"))

    llm = build_llm(args, software_payloads())
    if hasattr(llm, "state"):
        # 增量重跑这一轮直接放行评审，演示的是"只重跑脏掉的那几段"
        llm.state["review_round"] = 1
    pipeline = Pipeline(make_software_stages(), llm, store, max_rework=2)
    before = llm.meter.total
    pipeline.run({"需求": "客服每天要手工核对退款和赔付工单，口径不统一导致对不上账，"
                           "希望每天早上自动出一份能追溯到单号的核对报表。"},
                 only_stages=stale)
    print("增量重跑执行了：%s｜本次花费 %d token"
          % ("、".join(pipeline.executed), llm.meter.total - before))
    print("产物版本：%s" % "；".join("%s v%s" % (n, "、v".join(str(v) for v in store.versions(n)))
                                    for n in ("prd", "api", "code", "review")))
    print("可回放：prd 一共有 %d 个历史版本，随时能取回任意一版对比。"
          % len(store.versions("prd")))


def demo_non_software(args: argparse.Namespace) -> None:
    """E 段：同一套引擎，换成非软件任务的阶段契约。"""
    print("=" * 78)
    print("E. 迁移到非软件任务：换契约，不换引擎")
    print("=" * 78)
    stages = [
        Stage(name="调研员", role="调研员", reads=["主题"], writes=["brief"],
              instruction="围绕主题做一份调研简报：现状、主要问题、三个可选方向及各自代价。"),
        Stage(name="方案师", role="方案师", reads=["brief"], writes=["proposal"],
              instruction="依据调研简报给出一份可执行方案：选哪个方向、分几步做、需要什么资源。"),
        Stage(name="风控评审", role="风控评审", reads=["proposal"], writes=["review2"],
              instruction="评审方案的风险与前置条件。不通过就写 verdict=rework 并指定 rework_stage。",
              format_spec=OUTPUT_FORMAT + ' 额外字段：{"verdict": "approve|rework", '
                                           '"rework_stage": "<阶段名>", "issues": ["..."]}',
              is_review=True, rework_to="方案师"),
    ]
    payloads = {
        "调研员": {"artifacts": {"brief": "# 门店会员系统调研简报\n\n现状：3 家门店用纸质登记，"
                                        "会员数据无法汇总，营销只能靠短信群发。\n"
                                        "问题：数据不集中、发券不可控、复购无法统计。\n"
                                        "方向 A：上 SaaS 会员系统，快但数据在别人手里；\n"
                                        "方向 B：自建 + 小程序，投入大但数据自持；\n"
                                        "方向 C：先接第三方只读接口，验证需求后再自建。"}},
        "方案师": {"artifacts": {"proposal": "# 会员系统选型方案\n\n选方向 C：先接第三方只读接口。\n"
                                           "第 1 步两周，接只读接口并跑通会员查询；\n"
                                           "第 2 步一周，做数据同步与门店核对；\n"
                                           "第 3 步两周，上线发券闭环。\n"
                                           "需要资源：1 名后端、1 名数据、第三方接口费。"}},
        "风控评审": lambda st: (st.update({"n": st.get("n", 0) + 1}) or
                                {"artifacts": {"review2": "# 风控评审\n\n结论：打回\n"
                                               "问题：第三方接口的数据延迟没有约定，同步频率也缺失，"
                                               "第三步发券闭环可能用到过期会员状态。\n目标阶段：方案师\n"},
                                 "verdict": "rework", "rework_stage": "方案师", "issues": ["缺数据延迟约定"]}
                                if st.get("n", 0) == 1 else
                                {"artifacts": {"review2": "# 风控评审\n\n结论：通过\n"
                                               "问题：已补充数据延迟 SLA（15 分钟）与同步频率（每小时）。\n"},
                                 "verdict": "approve", "rework_stage": None, "issues": []}),
    }
    llm = build_llm(args, payloads)
    store = ArtifactStore()
    pipeline = Pipeline(stages, llm, store, max_rework=2)
    pipeline.run({"主题": "为线下三家门店选型一套会员系统"})
    print("%s %s %s %s" % (_pad("阶段", 12), _pad("读", 12), _pad("写", 14), "结论"))
    for entry in store.audit:
        print("%s %s %s %s"
              % (_pad(entry["stage"], 12), _pad("、".join(entry["read"]) or "—", 12),
                 _pad("、".join(entry["wrote"]), 14), entry["verdict"] or "—"))
    print("\n终止原因 %s｜返工 %d 次｜总 token %d"
          % (pipeline.stop_reason, pipeline.rework_count, llm.meter.total))


def main() -> None:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description="ch22 SOP 流水线：标准文档作为共享状态")
    ap.add_argument("--live", action="store_true", help="切真实模型（需 LLM_API_KEY）")
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "gpt-4o-mini"))
    ap.add_argument("--trace", action="store_true", help="打印每一步的完整提示")
    args = ap.parse_args()

    store, _ = demo_software(args)
    demo_cost(args)
    demo_staleness(args, store)
    demo_non_software(args)


if __name__ == "__main__":
    main()
