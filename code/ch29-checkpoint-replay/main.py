#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch29-checkpoint-replay / main.py
检查点、回放与多 Agent 共享状态 —— 最小可实现

运行:
    python main.py                          # mock 模式, 无需 API Key
    python main.py --live                   # 真实 LLM 生成最终答复 (OpenAI 兼容接口)
    python main.py --only time-travel       # 只跑其中一部分: replay / time-travel / shared

依赖: Python 3.9+, 仅标准库, 免安装。
--live 环境变量:
    OPENAI_API_KEY   必需
    OPENAI_BASE_URL  可选, 默认 https://api.openai.com/v1
    OPENAI_MODEL     可选, 默认 gpt-4o-mini
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------- 场景

BUDGET = 3500
ITEMS = [("widget", 2, 1200), ("gadget", 1, 800)]

STEP_PLAN = ("planner", "plan", None)
STEP_APPROVE = ("approver", "approve", None)
STEP_REPLY = ("reply", "final", None)


def step_quote(name: str) -> Tuple[str, str, str]:
    return ("buyer", "quote", name)


ALL_STEPS: List[Tuple[str, str, Optional[str]]] = [
    STEP_PLAN,
    step_quote("widget"),
    step_quote("gadget"),
    STEP_APPROVE,
    STEP_REPLY,
]


class Crash(Exception):
    """模拟进程崩溃。"""


# ----------------------------------------------------------------- 状态与事件

@dataclass
class Event:
    step: int
    op: str            # set
    path: List[str]
    value: Any


class Store:
    """工作状态 + 事件日志。每次写入同时产生一个事件, 事件即可重放的状态。"""

    def __init__(self):
        self.data: Dict[str, Any] = {}
        self.events: List[Event] = []

    def set(self, step: int, path: Sequence[str], value: Any) -> None:
        cur = self.data
        for p in list(path)[:-1]:
            cur = cur.setdefault(p, {})
        cur[path[-1]] = copy.deepcopy(value)
        self.events.append(Event(step, "set", list(path), value))

    def get(self, path: Sequence[str], default: Any = None) -> Any:
        cur: Any = self.data
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur

    def snapshot(self) -> Dict[str, Any]:
        return copy.deepcopy(self.data)


def apply_events(events: Sequence[Event]) -> Dict[str, Any]:
    """事件溯源: 从零开始重放事件, 重建任意时刻的状态。"""
    state: Dict[str, Any] = {}
    for ev in events:
        cur = state
        for p in ev.path[:-1]:
            cur = cur.setdefault(p, {})
        cur[ev.path[-1]] = copy.deepcopy(ev.value)
    return state


# --------------------------------------------------------------- 检查点存储

class CheckpointStore:
    """两种检查点模式。

    snapshot: 每步存整份状态, 能一步恢复, 但存储随步数与状态体积双重增长。
    event:    每步只存事件, 存储小, 恢复要重放, 但可重放性与审计能力最强。
    """

    def __init__(self, mode: str):
        self.mode = mode
        self.records: List[Dict] = []

    def write(self, step: int, store: Store) -> None:
        if self.mode == "snapshot":
            self.records.append({"step": step, "state": store.snapshot()})
        else:
            new_events = [e for e in store.events if e.step == step]
            self.records.append({"step": step,
                                 "events": [{"op": e.op, "path": e.path, "value": e.value}
                                            for e in new_events]})

    def state_at(self, step: int) -> Dict[str, Any]:
        """回放到指定步之后的状态。"""
        if self.mode == "snapshot":
            rec = next(r for r in self.records if r["step"] == step)
            return copy.deepcopy(rec["state"])
        events: List[Event] = []
        for rec in self.records:
            if rec["step"] > step:
                break
            for e in rec["events"]:
                events.append(Event(rec["step"], e["op"], e["path"], e["value"]))
        return apply_events(events)

    def bytes_used(self) -> int:
        return len(json.dumps(self.records, ensure_ascii=False).encode("utf-8"))

    def last_step(self) -> int:
        return max((r["step"] for r in self.records), default=-1)


# ------------------------------------------------------------- 工具与幂等

@dataclass
class Effect:
    key: str
    result: Dict[str, Any]


class ToolRunner:
    """带幂等键的工具执行器。同一幂等键只真正执行一次, 重放时复用结果。"""

    def __init__(self):
        self.effects: Dict[str, Effect] = {}
        self.calls = 0
        self.real_executions = 0
        self.hits = 0

    def call(self, name: str, key: str, fn) -> Dict[str, Any]:
        self.calls += 1
        if key in self.effects:
            self.hits += 1
            return copy.deepcopy(self.effects[key].result)
        self.real_executions += 1
        result = fn()
        self.effects[key] = Effect(key, copy.deepcopy(result))
        return result


# ------------------------------------------------------------------- 工作流

@dataclass
class RunResult:
    store: Store
    answer: str
    crashed_at: Optional[int] = None


def run_flow(tools: ToolRunner, ckpt: CheckpointStore, price_override: Optional[Dict[str, int]] = None,
             crash_after_tool: Optional[str] = None, skip_through: Optional[int] = None,
             start_state: Optional[Dict[str, Any]] = None) -> RunResult:
    """跑完整工作流。

    price_override: 时间旅行用, 改一个输入看结论怎么变。
    skip_through:   跳过编号 <= 该值的步骤(状态由 start_state 给出), 用于恢复/分支。
    """
    store = Store()
    if start_state is not None:
        store.data = copy.deepcopy(start_state)
    price_override = price_override or {}

    step_no = 0
    for agent, action, arg in ALL_STEPS:
        step_no += 1
        if skip_through is not None and step_no <= skip_through:
            continue

        if action == "plan":
            store.set(step_no, ["plan"], {"items": [n for n, _q, _p in ITEMS],
                                          "budget": BUDGET})
            ckpt.write(step_no, store)

        elif action == "quote":
            name = arg or ""
            qty, price = next((q, p) for n, q, p in ITEMS if n == name)
            price = price_override.get(name, price)

            # 幂等键必须覆盖影响输出的全部输入, 否则改价重跑会拿到旧结果
            result = tools.call("reserve_stock", f"reserve:{name}:{price}",
                                lambda qty=qty, price=price: {"reserved": qty,
                                                              "unit_price": price})
            store.set(step_no, ["quotes", name], result)
            if crash_after_tool == name:
                raise Crash(f"步骤 {step_no} 在工具执行后、写入检查点前中断")
            ckpt.write(step_no, store)

        elif action == "approve":
            quotes = store.get(["quotes"], {}) or {}
            total = sum(q["reserved"] * q["unit_price"] for q in quotes.values())
            budget = store.get(["plan", "budget"], BUDGET)
            store.set(step_no, ["decision"], {"approved": total <= budget,
                                             "total": total, "budget": budget})
            ckpt.write(step_no, store)

        elif action == "final":
            decision = store.get(["decision"], {}) or {}
            quotes = store.get(["quotes"], {}) or {}
            detail = "，".join(f"{n} x{q['reserved']} 单价 {q['unit_price']}"
                               for n, q in quotes.items())
            if decision.get("approved"):
                text = (f"已批准：{detail}，合计 {decision['total']} 元，"
                        f"预算 {decision['budget']} 元。")
            else:
                text = (f"驳回：{detail}，合计 {decision['total']} 元，"
                        f"超出预算 {decision['budget']} 元。")
            store.set(step_no, ["answer"], text)
            ckpt.write(step_no, store)
            return RunResult(store, text)

    return RunResult(store, "", crashed_at=step_no)


# --------------------------------------------------------- 演示一: 存储成本

def demo_storage(steps: int = 60) -> Dict:
    """模拟一段长流程: 状态随步数增长时, 两种检查点的存储开销差多少。"""
    snap = CheckpointStore("snapshot")
    evt = CheckpointStore("event")
    store = Store()
    for i in range(1, steps + 1):
        store.set(i, ["log", f"entry-{i}"], "x" * 40)      # 状态每步都在长
        store.set(i, ["state", "phase"], i)
        snap.write(i, store)
        evt.write(i, store)
    return {"steps": steps, "snapshot_bytes": snap.bytes_used(), "event_bytes": evt.bytes_used()}


# ------------------------------------------------------- 演示二: 时间旅行

def diff_states(a: Dict[str, Any], b: Dict[str, Any], path: str = "") -> List[Tuple[str, Any, Any]]:
    out: List[Tuple[str, Any, Any]] = []
    keys = set()
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a) | set(b)
        for k in sorted(keys):
            out += diff_states(a.get(k, {}), b.get(k, {}), f"{path}.{k}" if path else str(k))
    elif a != b:
        out.append((path, a, b))
    return out


def demo_time_travel() -> None:
    print("\n" + "=" * 72)
    print("时间旅行: 从任意历史步重跑, 并对比分支结果")
    print("=" * 72)

    tools = ToolRunner()
    ckpt = CheckpointStore("event")
    base = run_flow(tools, ckpt)
    print(f"原始分支答案: {base.answer}")
    print(f"检查点步数: {[r['step'] for r in ckpt.records]}")

    # 回到第 1 步之后(计划已定、报价还没开始), 把 widget 单价改成 1800 再跑
    pivot = 1
    state_at_pivot = ckpt.state_at(pivot)
    sandbox = ToolRunner()          # 分支跑在沙箱里: 副作用不落库, 可以自由改输入
    branch = run_flow(sandbox, ckpt, price_override={"widget": 1800},
                      start_state=state_at_pivot, skip_through=pivot)
    print(f"分支答案(回到 step={pivot} 后把 widget 单价 1200 改成 1800): {branch.answer}")

    print("\n分支差异:")
    for path, old, new in diff_states(base.store.snapshot(), branch.store.snapshot()):
        print(f"  {path}: {old}  ->  {new}")

    print(f"\n分支副作用统计(沙箱): 调用 {sandbox.calls} 次 / "
          f"真实执行 {sandbox.real_executions} 次 / 幂等命中 {sandbox.hits} 次")
    print("  分支在沙箱里跑, 不污染真实库存; 幂等键带了单价, 改价重跑不会被旧结果顶掉")


# --------------------------------------------------------- 演示三: 崩溃恢复

def demo_recovery() -> None:
    print("\n" + "=" * 72)
    print("恢复语义: 崩溃后重放是否安全")
    print("=" * 72)

    tools = ToolRunner()
    ckpt = CheckpointStore("event")
    crashed_at = None
    try:
        run_flow(tools, ckpt, crash_after_tool="gadget")
    except Crash as exc:
        crashed_at = str(exc)
    print(f"崩溃点: {crashed_at}")
    print(f"崩溃时最后一份检查点: step={ckpt.last_step()} "
          f"(gadget 的检查点没写成, 恢复时要重跑这一步)")
    print(f"崩溃前副作用: 调用 {tools.calls} 次 / 真实执行 {tools.real_executions} 次")

    # 恢复: 从最后一份检查点继续, 同一个 ToolRunner(保留幂等表)
    last = ckpt.last_step()
    state = ckpt.state_at(last)
    result = run_flow(tools, ckpt, start_state=state, skip_through=last)
    print(f"恢复后答案: {result.answer}")
    print(f"恢复后副作用: 调用 {tools.calls} 次 / 真实执行 {tools.real_executions} 次 / "
          f"幂等命中 {tools.hits} 次")
    print("  库存预留总共只发生 2 次(每个物料一次), 重放没有造成重复扣减")


# --------------------------------------------- 演示四: 多 Agent 共享状态

def demo_shared_state() -> None:
    print("\n" + "=" * 72)
    print("多 Agent 共享状态: 四种模式")
    print("=" * 72)

    rows: List[Dict[str, Any]] = []

    # 1) 共享内存: 一个 dict, 无版本
    mem: Dict[str, Any] = {}
    mem["owner"] = "A"
    mem["owner"] = "B"                      # 后写覆盖, 静默丢更新
    rows.append({"模式": "共享内存", "丢失更新": mem["owner"] == "B", "冲突检测": False,
                 "往返轮数": 1, "读到": mem["owner"], "状态可回放": False})

    # 2) 共享文档: 按 agent 分区写入, 内容都保留, 但没有"最后写入者"语义
    doc: Dict[str, Any] = {"a": [], "b": [], "revisions": []}
    doc["a"].append({"owner": "A", "rev": 1})
    doc["b"].append({"owner": "B", "rev": 1})
    doc["revisions"].append({"rev": 1, "by": "a"})
    doc["revisions"].append({"rev": 2, "by": "b"})
    rows.append({"模式": "共享文档", "丢失更新": False, "冲突检测": False,
                 "往返轮数": 2, "读到": "A/B 并存", "状态可回放": True})

    # 3) 消息传递: 写变成发消息, 下游按到达顺序处理, 只留最后一条
    queue: List[Tuple[str, str]] = []
    queue.append(("a", "owner=A"))
    queue.append(("b", "owner=B"))
    inbox = [m for m in queue if m[0] in ("a", "b")]
    final_msg = inbox[-1][1]
    rows.append({"模式": "消息传递", "丢失更新": final_msg == "owner=B", "冲突检测": False,
                 "往返轮数": 3, "读到": final_msg, "状态可回放": True})

    # 4) 共享存储: 带版本号的 KV, 读到旧版本时冲突被显式检出
    kv = SharedKV()
    kv.put("owner", "A")                 # A 写入, 版本 1
    a_view = kv.get("owner")             # A 读到的版本
    b_view = kv.get("owner")             # B 也读到了版本 1
    kv.put("owner", "A2", expected=a_view["version"])   # A 基于版本 1 写入成功
    conflict = False
    try:
        kv.put("owner", "B", expected=b_view["version"])  # B 基于版本 1 写入 -> 冲突
    except ConflictError:
        conflict = True
        kv.put("owner", "B", expected=kv.get("owner")["version"])   # 读新版本后重试成功
    rows.append({"模式": "共享存储", "丢失更新": False, "冲突检测": conflict,
                 "往返轮数": 3, "读到": kv.get("owner")["value"], "状态可回放": False})

    print(f"  {'模式':<10}{'丢失更新':<10}{'冲突检测':<10}{'往返轮数':<10}{'读到':<12}状态可回放")
    for r in rows:
        print(f"  {r['模式']:<10}{str(r['丢失更新']):<10}{str(r['冲突检测']):<10}"
              f"{r['往返轮数']:<10}{str(r['读到']):<12}{r['状态可回放']}")
    print("\n  共享存储的写入带版本号: 读到的版本不是当前版本就抛 ConflictError,")
    print("  冲突从'静默丢数据'变成'显式失败可重试'。")


class ConflictError(Exception):
    """共享存储的写冲突: 读到的版本已经不是当前版本。"""


class SharedKV:
    """带版本号的共享 KV。写入要带上读到的版本, 版本对不上就报冲突。"""

    def __init__(self):
        self.data: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str) -> Dict[str, Any]:
        return copy.deepcopy(self.data[key])

    def put(self, key: str, value: Any, expected: Optional[int] = None) -> None:
        cur = self.data.get(key)
        cur_version = cur["version"] if cur else 0
        if expected is not None and expected != cur_version:
            raise ConflictError(f"{key}: 读到的版本 {expected}, 当前版本 {cur_version}")
        self.data[key] = {"value": value, "version": cur_version + 1}


# ------------------------------------------------------------------ 答复生成

def live_reply(prompt: str, model: str, base_url: str, api_key: str) -> str:
    import urllib.request
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": "你是采购审批助手，用一句话给出结论并列出依据。"},
                     {"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


# --------------------------------------------------------------------- 演示

def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def demo(live: bool, model: str, base_url: str, api_key: str, only: str = "") -> None:
    print("场景: 采购审批, 三个角色(planner / buyer / approver) 共享同一份任务状态")

    if only in ("", "storage"):
        hr("检查点的存储成本: 快照 vs 事件")
        info = demo_storage(60)
        print(f"  {info['steps']} 步, 状态随步数增长")
        print(f"  快照模式总字节: {info['snapshot_bytes']}")
        print(f"  事件模式总字节: {info['event_bytes']}")
        print(f"  倍数: {info['snapshot_bytes'] / max(1, info['event_bytes']):.1f}x")
        print("  快照每步存整份状态 -> 双重增长; 事件只存变化 -> 线性")

    if only in ("", "replay"):
        hr("回放一致性: 两种模式回放到同一步, 状态应该一致")
        snap = CheckpointStore("snapshot")
        evt = CheckpointStore("event")
        s_snap, s_evt = Store(), Store()
        for i, (agent, action, arg) in enumerate(ALL_STEPS, start=1):
            s_snap.set(i, ["phase"], action)
            s_evt.set(i, ["phase"], action)
            snap.write(i, s_snap)
            evt.write(i, s_evt)
        same = snap.state_at(3) == evt.state_at(3)
        print(f"  step=3 时两种模式的状态一致: {same}")

    if only in ("", "time-travel"):
        demo_time_travel()

    if only in ("", "recovery"):
        demo_recovery()

    if only in ("", "shared"):
        demo_shared_state()

    if only == "":
        hr("最终答复 (mock / live)")
        tools = ToolRunner()
        ckpt = CheckpointStore("event")
        res = run_flow(tools, ckpt)
        print(f"  mock: {res.answer}")
        if live:
            prompt = json.dumps(res.store.snapshot(), ensure_ascii=False)
            try:
                print(f"  live: {live_reply(prompt, model, base_url, api_key)}")
            except Exception as exc:                      # noqa: BLE001
                print(f"  [live 调用失败, 回退 mock] {exc}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="检查点 / 回放 / 多 Agent 共享状态")
    ap.add_argument("--live", action="store_true", help="用真实 LLM 生成最终答复")
    ap.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    ap.add_argument("--base-url",
                    default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--only", default="",
                    choices=["", "storage", "replay", "time-travel", "recovery", "shared"])
    args = ap.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if args.live and not api_key:
        print("[--live 需要 OPENAI_API_KEY, 已回退 mock]")
        args.live = False

    demo(args.live, args.model, args.base_url, api_key, args.only)


if __name__ == "__main__":
    main()
