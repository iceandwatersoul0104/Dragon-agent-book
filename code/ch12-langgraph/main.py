#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch12 状态图式编排最小可实现 —— 纯标准库，无需 API Key 就能跑通完整回路。

这个文件用标准库复刻 LangGraph 的三个核心语义：

  1) 状态图：节点 / 静态边 / 条件边 + 状态 schema 与归约（reducer）语义
  2) 检查点：每步存快照，可回溯（fork）、可修正（update_state）
  3) 中断：节点内 interrupt() 挂起，人工审批后从该节点重新执行

三个演示场景：

  triage  事故分诊：线性流程 + 条件边 + 审批中断 + 驳回返工循环
  merge   并行合并：两个分支同时写同一个 channel，add 归约 vs overwrite 覆盖
  fork    时间旅行：回滚到审批前的检查点，人工修正状态后重放另一条分支

用法：
    python main.py                    # 跑全部场景
    python main.py --demo triage      # 只跑事故分诊
    python main.py --demo merge       # 只跑并行合并
    python main.py --demo fork        # 只跑时间旅行
    python main.py --approver reject  # 审批人一律驳回，看返工与升级路径
    python main.py --max-rounds 1     # 返工几轮后升级给人
    python main.py --trace            # 打印每一步的节点与写入
    python main.py --live             # 切真实模型 API

--live 所需环境变量：
    LLM_API_KEY    必填，模型服务商的 API Key
    LLM_BASE_URL   可选，默认 https://api.openai.com/v1
    LLM_MODEL      可选，默认 gpt-4o-mini
端点需兼容 `POST {base}/chat/completions`：body 含 model/messages，
返回 choices[0].message.content。换供应商只改 base_url 与 model。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文结果乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


START = "__start__"
END = "__end__"


# ==========================================================================
# 1. 归约函数（reducer）—— 状态图并发的关键
# ==========================================================================


def overwrite(old: Any, new: Any) -> Any:
    """默认归约：新值覆盖旧值。

    对应 LangGraph 里没有指定 reducer 的 channel：state[key] = new_value。
    """
    return new


def add(old: Any, new: Any) -> List[Any]:
    """累加归约：两个列表拼接，等价于 operator.add。

    这是并行分支合并的默认解法：分支 A 写 [a1, a2]，分支 B 写 [b1, b2]，
    归约之后是 [a1, a2, b1, b2]，谁都不丢。
    """
    return list(old or []) + list(new or [])


def add_messages(old: List[dict], new: List[dict]) -> List[dict]:
    """消息归约：按 id 合并，同 id 就地替换并保留原来的位置。

    没有 id 的消息按出现顺序分配匿名 id，因此重复写入不会产生重复项。
    """
    merged: Dict[str, dict] = {}
    order: List[str] = []
    for m in list(old or []) + list(new or []):
        mid = m.get("id")
        if mid is None:
            mid = "__anon_%d" % len(order)
        if mid not in merged:
            order.append(mid)
        merged[mid] = m
    return [merged[k] for k in order]


@dataclass
class Channel:
    """一个状态通道：名字 + 归约函数 + 初始值。"""

    name: str
    reducer: Callable[[Any, Any], Any] = overwrite
    initial: Any = None
    factory: Optional[Callable[[], Any]] = None

    def empty(self) -> Any:
        if self.factory is not None:
            return self.factory()
        return self.initial


def ch(
    name: str,
    reducer: Callable[[Any, Any], Any] = overwrite,
    initial: Any = None,
    factory: Optional[Callable[[], Any]] = None,
) -> Channel:
    """建一个 channel 的小helper。"""
    return Channel(name=name, reducer=reducer, initial=initial, factory=factory)


class State:
    """状态 schema：一组 channel 的集合。

    每个 channel 自带归约语义——这正是"归约比覆盖更适合并发合并"的落点：
    合并规则写在 schema 里，而不是散落在每个节点的返回值上。
    """

    def __init__(self, channels: Dict[str, Channel]):
        self.channels = dict(channels)

    def names(self) -> List[str]:
        return list(self.channels)


# ==========================================================================
# 2. 中断机制（interrupt / human-in-the-loop）
# ==========================================================================


class GraphInterrupt(Exception):
    """节点内部调用 interrupt() 时抛出，由引擎捕获。"""

    def __init__(self, payload: Any):
        super().__init__(payload)
        self.payload = payload


@dataclass
class RunContext:
    """一次执行（或一次恢复）期间的上下文。"""

    resume_values: List[Any] = field(default_factory=list)
    interrupt_payloads: List[Any] = field(default_factory=list)


_CTX: ContextVar[Optional[RunContext]] = ContextVar("run_ctx", default=None)


def interrupt(payload: Any = None) -> Any:
    """在节点内部挂起，等待人工介入。

    语义（和 LangGraph 一致）：
      - 第一次调用：抛出 GraphInterrupt，整个节点的执行被丢弃，流程停在当前节点；
      - 恢复执行时：节点从第一行重新跑，interrupt() 直接返回人工给的答复。
      - 所以 interrupt() 之前写的任何东西都会重写一遍——节点必须幂等。

    多个 interrupt 依次消耗 resume 列表里的值（resume 传 list 时按序取）。
    """
    ctx = _CTX.get()
    if ctx is None:
        raise RuntimeError("interrupt() 只能在节点函数内部调用")
    if ctx.resume_values:
        return ctx.resume_values.pop(0)
    raise GraphInterrupt(payload)


# ==========================================================================
# 3. 检查点（checkpointer）—— 快照、时间旅行、状态修正
# ==========================================================================


@dataclass
class Checkpoint:
    """一次超步（superstep）之后的状态快照。"""

    id: str
    thread_id: str
    values: Dict[str, Any]
    next: Tuple[str, ...]
    step: int
    metadata: Dict[str, Any] = field(default_factory=dict)


def _new_id() -> str:
    return "ckpt-%s" % uuid.uuid4().hex[:12]


class MemorySaver:
    """内存版 checkpointer：thread_id -> 检查点列表（按写入顺序）。"""

    def __init__(self) -> None:
        self._threads: Dict[str, List[Checkpoint]] = {}

    def put(self, cp: Checkpoint) -> None:
        self._threads.setdefault(cp.thread_id, []).append(cp)

    def list(self, thread_id: str) -> List[Checkpoint]:
        return list(self._threads.get(thread_id, []))

    def latest(self, thread_id: str) -> Optional[Checkpoint]:
        lst = self._threads.get(thread_id)
        return lst[-1] if lst else None

    def get(self, thread_id: str, checkpoint_id: str) -> Optional[Checkpoint]:
        for cp in self._threads.get(thread_id, []):
            if cp.id == checkpoint_id:
                return cp
        return None

    def clear(self, thread_id: str) -> None:
        self._threads.pop(thread_id, None)


# ==========================================================================
# 4. 状态图：构建器 + 执行引擎
# ==========================================================================


class StateGraph:
    """图构建器：先声明状态 schema，再挂节点和边。"""

    def __init__(self, schema: State, entry: str = START):
        self.schema = schema
        self.entry = entry
        self.nodes: Dict[str, Callable[[dict], dict]] = {}
        self.edges: Dict[str, List[str]] = defaultdict(list)
        self.cond_edges: Dict[str, Tuple[Callable[[dict], str], Optional[Dict[str, str]]]] = {}

    def add_node(self, name: str, fn: Callable[[dict], dict]) -> "StateGraph":
        if name in (START, END):
            raise ValueError("%s 是保留名" % name)
        self.nodes[name] = fn
        return self

    def add_edge(self, src: str, dst: str) -> "StateGraph":
        self.edges[src].append(dst)
        return self

    def add_conditional_edges(
        self,
        src: str,
        router: Callable[[dict], str],
        path_map: Optional[Dict[str, str]] = None,
    ) -> "StateGraph":
        """条件边：router 读状态返回一个键，再按 path_map 落到具体节点。

        router 必须是状态的纯函数——它在每次超步结束后被调用，
        有副作用的 router 会让图的行为不可复现。
        """
        self.cond_edges[src] = (router, path_map)
        return self

    def compile(self, checkpointer: Optional[MemorySaver] = None) -> "CompiledGraph":
        return CompiledGraph(self, checkpointer)


class CompiledGraph:
    """编译后的可执行图。

    执行模型（简化版 Pregel 超步）：
      一个超步 = 一组可以并行执行的节点。
      同一超步里的节点读到的是同一份输入状态，各自的写入按 channel 的
      归约函数合并；合并结果才成为下一超步的输入。
    """

    def __init__(self, builder: StateGraph, checkpointer: Optional[MemorySaver] = None):
        self.schema = builder.schema
        self.nodes = dict(builder.nodes)
        self.entry = builder.entry
        self.edges = {k: list(v) for k, v in builder.edges.items()}
        self.cond_edges = dict(builder.cond_edges)
        self.checkpointer = checkpointer
        self.trace: List[dict] = []
        self._preds = self._compute_static_preds()

    # ---------------- 拓扑：找出"必须等齐"的节点 ----------------

    def _possible_successors(self, node: str) -> List[str]:
        """一个节点所有可能的去向（静态边 + 条件边的全部候选目标）。"""
        out = list(self.edges.get(node, []))
        if node in self.cond_edges:
            _router, path_map = self.cond_edges[node]
            if path_map:
                out.extend(path_map.values())
        return out

    def _compute_static_preds(self) -> Dict[str, Set[str]]:
        """算出每个节点"在拓扑上"可能的前置节点。

        注意这只是拓扑关系，不是运行时的等待条件——真正的等待条件见
        _next_frontier：只有真正被路由到过的前置节点才需要等。
        """
        preds: Dict[str, Set[str]] = {n: set() for n in self.nodes}
        sources = [START] + list(self.nodes)
        for u in sources:
            for v in self._possible_successors(u):
                if v in preds and u != START:
                    preds[v].add(u)
        return preds

    # ---------------- 状态操作 ----------------

    def _initial_values(self) -> Dict[str, Any]:
        return {name: c.empty() for name, c in self.schema.channels.items()}

    def _apply_updates(self, values: Dict[str, Any], updates: Optional[dict]) -> Dict[str, Any]:
        out = dict(values)
        for key, val in (updates or {}).items():
            if key not in self.schema.channels:
                raise KeyError("状态 schema 里没有这个 channel: %s" % key)
            c = self.schema.channels[key]
            out[key] = c.reducer(out.get(key, c.empty()), val)
        return out

    def _actual_successors(self, node: str, values: Dict[str, Any]) -> List[str]:
        out = list(self.edges.get(node, []))
        if node in self.cond_edges:
            router, path_map = self.cond_edges[node]
            key = router(values)
            if path_map:
                target = path_map.get(key, path_map.get(str(key)))
                if target:
                    out.append(target)
            else:
                out.append(key)
        return out

    # ---------------- 执行 ----------------

    def invoke(
        self,
        input: Optional[dict] = None,
        config: Optional[dict] = None,
        resume: Any = None,
    ) -> Dict[str, Any]:
        """执行或恢复一次图。

        input      初始状态更新（启动新线程时用），走各 channel 的归约
        config     {"configurable": {"thread_id": ..., "checkpoint_id": ...}}
                   带 checkpoint_id 就是从那个历史检查点分叉（时间旅行）
        resume     人工答复；传 list 则按序供给多个 interrupt
        """
        conf = config or {}
        configurable = conf.get("configurable", {}) or {}
        thread_id = configurable.get("thread_id", "default")
        ckpt_id = configurable.get("checkpoint_id")

        resume_values: List[Any] = []
        if resume is not None:
            resume_values = list(resume) if isinstance(resume, list) else [resume]

        self.trace = []

        # --- 定位起点 ---
        if self.checkpointer is None:
            values = self._initial_values()
            if input:
                values = self._apply_updates(values, input)
            return self._run(values, 0, (self.entry,), resume_values, thread_id, {}, {}, "loop")

        if ckpt_id:
            base = self.checkpointer.get(thread_id, ckpt_id)
            if base is None:
                raise KeyError("找不到检查点 %s" % ckpt_id)
            values = copy.deepcopy(base.values)
            step = base.step
            frontier = tuple(base.next) or (self.entry,)
            act = dict(base.metadata.get("activated", {}))
            done = dict(base.metadata.get("completed_count", {}))
            source = "fork"
        else:
            latest = self.checkpointer.latest(thread_id)
            if latest is not None and input is None:
                values = copy.deepcopy(latest.values)
                step = latest.step
                frontier = tuple(latest.next)
                act = dict(latest.metadata.get("activated", {}))
                done = dict(latest.metadata.get("completed_count", {}))
                source = "loop"
                if latest.metadata.get("pending_interrupt"):
                    if not resume_values:
                        # 还挂着：原样返回当前状态与待审批内容
                        return self._interrupt_result(values, latest)
                    # 恢复：节点从头重跑
                    frontier = (latest.metadata.get("interrupted_node", frontier[0]),)
            else:
                values = self._initial_values()
                if input:
                    values = self._apply_updates(values, input)
                step = 0
                frontier = (self.entry,)
                act = {}
                done = {}
                source = "loop"

        return self._run(values, step, frontier, resume_values, thread_id, act, done, source)

    def _run(
        self,
        values: Dict[str, Any],
        step: int,
        frontier: Tuple[str, ...],
        resume_values: List[Any],
        thread_id: str,
        act: Dict[str, int],
        done: Dict[str, int],
        source: str,
    ) -> Dict[str, Any]:
        ctx = RunContext(resume_values=list(resume_values))
        token = _CTX.set(ctx)
        try:
            while frontier:
                writes: Dict[str, List[str]] = {}
                node_results: Dict[str, dict] = {}

                # --- 超步：同一波节点并行跑，读的是同一份输入状态 ---
                for node in frontier:
                    fn = self.nodes.get(node)
                    if fn is None:
                        continue
                    try:
                        out = fn(values)
                    except GraphInterrupt as exc:
                        # 节点中断：这一节点之前的写入全部作废，流程停在它身上
                        cp = Checkpoint(
                            id=_new_id(),
                            thread_id=thread_id,
                            values=copy.deepcopy(values),
                            next=(node,),
                            step=step,
                            metadata={
                                "source": "interrupt",
                                "writes": writes,
                                "completed": sorted(done),
                                "activated": dict(act),
                                "completed_count": dict(done),
                                "pending_interrupt": True,
                                "interrupt_payload": exc.payload,
                                "interrupted_node": node,
                                "next": [node],
                            },
                        )
                        self._save(cp)
                        self.trace.append(
                            {"step": step, "node": node, "writes": writes, "interrupt": exc.payload}
                        )
                        return self._interrupt_result(values, cp)
                    node_results[node] = out or {}
                    writes[node] = list(node_results[node].keys())

                # --- 归约：把各节点的写入按 channel 合并 ---
                for node, upd in node_results.items():
                    values = self._apply_updates(values, upd)
                    done[node] = done.get(node, 0) + 1

                step += 1

                # --- 计算下一波 ---
                next_frontier = self._next_frontier(frontier, values, act, done)

                cp = Checkpoint(
                    id=_new_id(),
                    thread_id=thread_id,
                    values=copy.deepcopy(values),
                    next=tuple(next_frontier),
                    step=step,
                    metadata={
                        "source": source,
                        "writes": writes,
                        "completed": sorted(done),
                        "activated": dict(act),
                        "completed_count": dict(done),
                        "next": list(next_frontier),
                    },
                )
                self._save(cp)
                self.trace.append(
                    {"step": step, "node": "+".join(frontier), "writes": writes, "next": list(next_frontier)}
                )
                frontier = tuple(next_frontier)
        finally:
            _CTX.reset(token)
        return values

    def _next_frontier(
        self,
        completed_nodes: Sequence[str],
        values: Dict[str, Any],
        act: Dict[str, int],
        done: Dict[str, int],
    ) -> List[str]:
        """下一波可以跑的节点。

        判据两条，缺一不可：

        1) 有"未完成的工作"：被路由到的次数 > 已完成次数。
           次数相等说明上一轮就跑完了，不再重复调度；次数大于说明是
           被再次路由过来的（环、分支回流），要再跑一次。

        2) 该等的前置节点都等到了：只看**真正被路由到过**的前置节点。
           拓扑上连着但本次路径没走到的分支（比如被驳回后走了另一支），
           不会被算进来——否则"两条互斥分支汇合到同一个节点"就会永远等不到。
        """
        for node in completed_nodes:
            for target in self._actual_successors(node, values):
                if target == END or target not in self.nodes:
                    continue
                act[target] = act.get(target, 0) + 1

        ready: List[str] = []
        for target in self.nodes:
            if act.get(target, 0) <= done.get(target, 0):
                continue  # 没有待办的新工作
            blocking = [
                p
                for p in self._preds.get(target, set())
                if act.get(p, 0) > 0 and done.get(p, 0) == 0
            ]
            if blocking:
                continue  # 还有前置节点在飞
            if target not in ready:
                ready.append(target)
        return ready

    def _save(self, cp: Checkpoint) -> None:
        if self.checkpointer is not None:
            self.checkpointer.put(cp)

    @staticmethod
    def _interrupt_result(values: Dict[str, Any], cp: Checkpoint) -> Dict[str, Any]:
        out = dict(values)
        out["__interrupt__"] = [cp.metadata.get("interrupt_payload")]
        return out

    # ---------------- 检查点相关的公开 API ----------------

    def get_state(self, config: Optional[dict] = None) -> Optional[Checkpoint]:
        if self.checkpointer is None:
            return None
        conf = (config or {}).get("configurable", {}) or {}
        thread_id = conf.get("thread_id", "default")
        ckpt_id = conf.get("checkpoint_id")
        if ckpt_id:
            return self.checkpointer.get(thread_id, ckpt_id)
        return self.checkpointer.latest(thread_id)

    def get_state_history(self, config: Optional[dict] = None) -> List[Checkpoint]:
        """检查点历史，最新的在前。"""
        if self.checkpointer is None:
            return []
        conf = (config or {}).get("configurable", {}) or {}
        thread_id = conf.get("thread_id", "default")
        return list(reversed(self.checkpointer.list(thread_id)))

    def update_state(
        self, config: Optional[dict], values: dict, as_node: Optional[str] = None
    ) -> Checkpoint:
        """人工修正状态：基于某个检查点生成一个新检查点（fork）。

        典型用途：Agent 算错了，运营手工改掉某个字段，然后从改动处继续跑。
        """
        if self.checkpointer is None:
            raise RuntimeError("update_state 需要 checkpointer")
        conf = (config or {}).get("configurable", {}) or {}
        thread_id = conf.get("thread_id", "default")
        ckpt_id = conf.get("checkpoint_id")
        base = (
            self.checkpointer.get(thread_id, ckpt_id)
            if ckpt_id
            else self.checkpointer.latest(thread_id)
        )
        if base is None:
            raise KeyError("没有可修正的检查点")

        new_values = self._apply_updates(copy.deepcopy(base.values), values)
        cp = Checkpoint(
            id=_new_id(),
            thread_id=thread_id,
            values=new_values,
            next=base.next,
            step=base.step + 1,
            metadata={
                "source": "update",
                "writes": {as_node or "__manual__": list(values.keys())},
                "completed": base.metadata.get("completed", []),
                "activated": base.metadata.get("activated", {}),
                "completed_count": base.metadata.get("completed_count", {}),
                "next": list(base.next),
            },
        )
        self.checkpointer.put(cp)
        return cp


# ==========================================================================
# 5. 模型层：mock 与 live
# ==========================================================================

_MSG_SEQ = [0]


def msg(role: str, content: str, **extra: Any) -> dict:
    """造一条带唯一 id 的消息——add_messages 归约靠 id 去重。"""
    _MSG_SEQ[0] += 1
    m = {"id": "m%03d" % _MSG_SEQ[0], "role": role, "content": content}
    m.update(extra)
    return m


class LLMBase:
    """Agent 只需要模型做三件事，接口就这三招。"""

    def plan(self, task: str) -> List[dict]:
        raise NotImplementedError

    def draft(self, task: str, evidence: List[dict]) -> str:
        raise NotImplementedError

    def summarize(self, task: str, evidence: List[dict], draft: str, decision: str) -> str:
        raise NotImplementedError


class MockLLM(LLMBase):
    """离线 mock：按固定规则返回结构化结果。

    它不代表任何真实模型的表现，只保证没有 API Key 也能把流程跑通。
    """

    def plan(self, task: str) -> List[dict]:
        return [
            {"step": 1, "action": "fetch_metrics", "why": "先看指标确认影响面"},
            {"step": 2, "action": "fetch_logs", "why": "再看日志定位根因"},
            {"step": 3, "action": "draft", "why": "基于证据写处置方案"},
        ]

    def draft(self, task: str, evidence: List[dict]) -> str:
        lines = ["针对「%s」的处置草案：" % task]
        for e in evidence:
            lines.append("- %s：%s" % (e["source"], e["content"]))
        lines.append("- 建议动作：回滚最近一次发布，并把异常实例数从 12 降到 0。")
        return "\n".join(lines)

    def summarize(self, task: str, evidence: List[dict], draft: str, decision: str) -> str:
        head = "已批准并执行。" if decision == "approved" else "未获批准，仅输出建议。"
        return "%s证据 %d 条，任务「%s」结束。" % (head, len(evidence), task)


class LiveLLM(LLMBase):
    """真实模型：走 OpenAI 兼容的 chat completions 端点。"""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _chat(self, prompt: str, system: str = "") -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self.model, "messages": messages, "temperature": 0.0}
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_json(text: str) -> Any:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[:-3]
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        return json.loads(text)

    def plan(self, task: str) -> List[dict]:
        raw = self._chat(
            "为下面的运维故障制定排查计划，只返回 JSON："
            '{"steps":[{"step":1,"action":"fetch_metrics|fetch_logs|draft","why":"..."}]}\n\n任务：%s' % task
        )
        data = self._parse_json(raw)
        steps = data.get("steps") or []
        return steps if isinstance(steps, list) else []

    def draft(self, task: str, evidence: List[dict]) -> str:
        ev = "\n".join("- %s：%s" % (e["source"], e["content"]) for e in evidence)
        return self._chat(
            "根据下面的证据写一份处置草案（不超过 5 行，不要编号解释）：\n任务：%s\n证据：\n%s"
            % (task, ev)
        ).strip()

    def summarize(self, task: str, evidence: List[dict], draft: str, decision: str) -> str:
        head = "已批准并执行。" if decision == "approved" else "未获批准，仅输出建议。"
        return self._chat(
            "%s请用两句话总结任务「%s」的收尾情况，证据 %d 条。" % (head, task, len(evidence))
        ).strip()


def build_llm(live: bool, args) -> LLMBase:
    if not live:
        return MockLLM()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        print("错误：--live 需要环境变量 LLM_API_KEY", file=sys.stderr)
        raise SystemExit(2)
    base_url = args.base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model = args.model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    return LiveLLM(api_key, base_url, model)


# ==========================================================================
# 6. 工具（mock 数据源）
# ==========================================================================

TOOLS: Dict[str, Callable[[str], str]] = {
    "fetch_metrics": lambda task: "错误率 3.2%（基线 0.4%），p99 延迟 1800ms，受影响实例 12/40",
    "fetch_logs": lambda task: "最近一次发布 v2.4.1 后出现大量 NullPointerException，堆栈指向连接池配置",
    "apply_fix": lambda draft: "已回滚 v2.4.1 并扩容 2 个实例，错误率回到 0.3%",
}


# ==========================================================================
# 7. 场景一：事故分诊（triage）—— 线性 + 条件边 + 中断 + 返工循环
# ==========================================================================


def build_triage_graph(llm: LLMBase, max_rounds: int = 2) -> CompiledGraph:
    schema = State(
        {
            "task": ch("task", overwrite, ""),
            "plan": ch("plan", overwrite, factory=list),
            "evidence": ch("evidence", add, factory=list),
            "draft": ch("draft", overwrite, ""),
            "decision": ch("decision", overwrite, ""),
            "rounds": ch("rounds", overwrite, 0),
            "result": ch("result", overwrite, ""),
            "messages": ch("messages", add_messages, factory=list),
        }
    )
    g = StateGraph(schema)

    def node_plan(state: dict) -> dict:
        steps = llm.plan(state["task"])
        return {
            "plan": steps,
            "messages": [msg("assistant", "已制定 %d 步排查计划" % len(steps))],
        }

    def node_collect(state: dict) -> dict:
        evidence = [
            {"source": name, "content": TOOLS[name](state["task"])}
            for name in ("fetch_metrics", "fetch_logs")
        ]
        return {
            "evidence": evidence,
            "messages": [msg("tool", "采集到 %d 条证据" % len(evidence))],
        }

    def node_draft(state: dict) -> dict:
        text = llm.draft(state["task"], state["evidence"])
        return {
            "draft": text,
            "messages": [msg("assistant", "已生成处置草案")],
        }

    def node_approval(state: dict) -> dict:
        # 第一次进来会抛出中断；恢复时这里从头重跑，interrupt() 直接拿到答复
        answer = interrupt(
            {
                "type": "approval",
                "draft": state["draft"],
                "evidence_count": len(state["evidence"]),
                "round": state["rounds"] + 1,
            }
        )
        text = str(answer or "").strip().lower()
        approved = text in ("approve", "approved", "y", "yes", "ok", "同意", "批准")
        decision = "approved" if approved else "rejected"
        return {
            "decision": decision,
            "messages": [msg("human", "审批结果：%s" % decision)],
        }

    def node_revise(state: dict) -> dict:
        rounds = int(state["rounds"]) + 1
        return {
            "rounds": rounds,
            "messages": [msg("assistant", "方案被驳回，进入第 %d 轮修改" % rounds)],
        }

    def node_apply(state: dict) -> dict:
        out = TOOLS["apply_fix"](state["draft"])
        return {"result": out, "messages": [msg("tool", "已执行处置动作")]}

    def node_report(state: dict) -> dict:
        text = llm.summarize(state["task"], state["evidence"], state["draft"], state["decision"])
        return {"result": text, "messages": [msg("assistant", text)]}

    def route_approval(state: dict) -> str:
        return "approve" if state["decision"] == "approved" else "reject"

    def route_revise(state: dict) -> str:
        return "retry" if int(state["rounds"]) < max_rounds else "escalate"

    g.add_node("plan", node_plan)
    g.add_node("collect", node_collect)
    g.add_node("draft", node_draft)
    g.add_node("approval", node_approval)
    g.add_node("revise", node_revise)
    g.add_node("apply", node_apply)
    g.add_node("report", node_report)

    g.add_edge(START, "plan")
    g.add_edge("plan", "collect")
    g.add_edge("collect", "draft")
    g.add_edge("draft", "approval")
    g.add_conditional_edges(
        "approval", route_approval, {"approve": "apply", "reject": "revise"}
    )
    g.add_conditional_edges(
        "revise", route_revise, {"retry": "draft", "escalate": "report"}
    )
    g.add_edge("apply", "report")
    g.add_edge("report", END)

    return g.compile(MemorySaver())


# ==========================================================================
# 8. 场景二：并行合并（merge）—— 归约 vs 覆盖
# ==========================================================================


def build_merge_graph(evidence_reducer: Callable[[Any, Any], Any]) -> CompiledGraph:
    schema = State(
        {
            "task": ch("task", overwrite, ""),
            "evidence": ch("evidence", evidence_reducer, factory=list),
            "summary": ch("summary", overwrite, ""),
        }
    )
    g = StateGraph(schema)

    def node_metrics(state: dict) -> dict:
        return {"evidence": [{"source": "metrics", "content": "错误率 3.2%，p99 1800ms"}]}

    def node_logs(state: dict) -> dict:
        return {"evidence": [{"source": "logs", "content": "NullPointerException @ 连接池"}]}

    def node_join(state: dict) -> dict:
        ev = state["evidence"]
        return {"summary": "合并到 %d 条证据：%s" % (len(ev), " / ".join(e["source"] for e in ev))}

    g.add_node("metrics", node_metrics)
    g.add_node("logs", node_logs)
    g.add_node("join", node_join)

    g.add_edge(START, "metrics")
    g.add_edge(START, "logs")
    g.add_edge("metrics", "join")
    g.add_edge("logs", "join")
    g.add_edge("join", END)

    return g.compile(MemorySaver())


# ==========================================================================
# 9. 演示
# ==========================================================================


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def show_trace(graph: CompiledGraph) -> None:
    print("执行轨迹（每个超步一行）：")
    for t in graph.trace:
        if t.get("interrupt") is not None:
            print("  step %-2d  节点 %-12s  << 中断，等待人工答复" % (t["step"], t["node"]))
        else:
            writes = ", ".join(
                "%s{%s}" % (n, ",".join(w)) for n, w in (t.get("writes") or {}).items()
            )
            print("  step %-2d  节点 %-12s  写入 %s" % (t["step"], t["node"], writes or "-"))


def show_history(graph: CompiledGraph, thread_id: str, limit: int = 20) -> None:
    hist = graph.get_state_history({"configurable": {"thread_id": thread_id}})
    print("检查点历史（最新的在前，共 %d 条）：" % len(hist))
    for cp in hist[:limit]:
        meta = cp.metadata
        tag = meta.get("source", "loop")
        nxt = ",".join(meta.get("next", [])) or "-"
        note = ""
        if meta.get("pending_interrupt"):
            note = "  [挂起于 %s]" % meta.get("interrupted_node")
        print(
            "  %-8s step=%-2d source=%-9s next=%-14s%s"
            % (cp.id[-6:], cp.step, tag, nxt, note)
        )


def make_approver(policy: str, max_rounds: int):
    """按策略生成人工答复序列。"""
    calls = {"n": 0}

    def approver(payload: dict) -> str:
        calls["n"] += 1
        if policy == "approve":
            return "approve"
        if policy == "reject":
            return "reject"
        if policy == "reject-once":
            return "reject" if calls["n"] == 1 else "approve"
        return "reject"

    return approver


def run_triage(llm: LLMBase, approver_policy: str, max_rounds: int, trace: bool) -> None:
    hr("场景一：事故分诊 —— 条件边 + 中断审批 + 驳回返工")
    graph = build_triage_graph(llm, max_rounds=max_rounds)
    thread = "triage-1"
    config = {"configurable": {"thread_id": thread}}
    approver = make_approver(approver_policy, max_rounds)

    state = graph.invoke({"task": "支付网关错误率突增"}, config)
    guard = 0
    while "__interrupt__" in state and guard < 10:
        guard += 1
        payload = state["__interrupt__"][0]
        print("\n[人工介入] 待审批内容：")
        for k, v in payload.items():
            print("  %-14s %s" % (k, str(v).replace("\n", "\n" + " " * 17)))
        answer = approver(payload)
        print("[人工介入] 答复：%s" % answer)
        state = graph.invoke(None, config, resume=answer)

    print("\n最终状态：")
    print("  decision = %s" % state.get("decision"))
    print("  rounds   = %s" % state.get("rounds"))
    print("  evidence = %d 条" % len(state.get("evidence") or []))
    print("  result   = %s" % state.get("result"))
    if trace:
        show_trace(graph)
    show_history(graph, thread)


def run_merge(trace: bool) -> None:
    hr("场景二：并行合并 —— reducer(add) vs overwrite")
    for label, reducer in (("add 归约（累加）", add), ("overwrite 覆盖", overwrite)):
        graph = build_merge_graph(reducer)
        state = graph.invoke({"task": "并行取证"}, {"configurable": {"thread_id": "merge-" + label[:3]}})
        ev = state.get("evidence") or []
        print("\n%s：" % label)
        print("  证据条数 = %d" % len(ev))
        print("  证据内容 = %s" % " / ".join(e["source"] for e in ev))
        print("  join 输出 = %s" % state.get("summary"))
        if trace:
            show_trace(graph)
    print(
        "\n同一张图，只换 channel 的归约函数，结果就完全不同：\n"
        "  覆盖语义下，后写的分支把先写的分支整个吃掉了；\n"
        "  累加语义下，两个分支的产出都保住了。"
    )


def run_fork(llm: LLMBase, approver_policy: str, max_rounds: int, trace: bool) -> None:
    hr("场景三：时间旅行 —— 回滚检查点 + 人工修正状态后重放")
    graph = build_triage_graph(llm, max_rounds=max_rounds)
    thread = "fork-1"
    config = {"configurable": {"thread_id": thread}}
    approver = make_approver(approver_policy, max_rounds)

    state = graph.invoke({"task": "支付网关错误率突增"}, config)
    guard = 0
    while "__interrupt__" in state and guard < 10:
        guard += 1
        answer = approver(state["__interrupt__"][0])
        print("  第 %d 次审批答复：%s" % (guard, answer))
        state = graph.invoke(None, config, resume=answer)

    print("\n分支 A（原始走向）收尾：")
    print("  decision = %s  rounds = %s" % (state.get("decision"), state.get("rounds")))
    print("  draft    = %s" % str(state.get("draft", "")).splitlines()[-1])
    print("  result   = %s" % state.get("result"))

    # 找到"第一次进入审批之前"的那个检查点（按时间正序取最早的）
    base = None
    for cp in reversed(graph.get_state_history(config)):
        if not cp.metadata.get("pending_interrupt") and "approval" in cp.metadata.get("next", []):
            base = cp
            break
    if base is None:
        print("没有找到可回滚的检查点")
        return

    print("\n回滚到审批前的检查点 %s（step=%d）" % (base.id[-6:], base.step))
    print("  当时的 draft：%s" % str(base.values.get("draft", "")).splitlines()[0][:60])

    # 人工修正：直接改掉 draft 并预置审批结果
    fork_cp = graph.update_state(
        {"configurable": {"thread_id": thread, "checkpoint_id": base.id}},
        {"draft": "人工修正版：先回滚 v2.4.1，再扩容 2 个实例，24 小时内复盘。", "decision": "approved"},
        as_node="manual",
    )
    print("  人工修正生成新检查点 %s（source=update）" % fork_cp.id[-6:])

    fork_state = graph.invoke(
        None,
        {"configurable": {"thread_id": thread, "checkpoint_id": fork_cp.id}},
        resume="approve",
    )
    print("\n分支 B（修正后重放）收尾：")
    print("  decision = %s" % fork_state.get("decision"))
    print("  draft    = %s" % str(fork_state.get("draft", "")).splitlines()[-1])
    print("  result   = %s" % fork_state.get("result"))
    if trace:
        show_trace(graph)
    show_history(graph, thread)


# ==========================================================================
# 10. CLI
# ==========================================================================


def main() -> int:
    ap = argparse.ArgumentParser(description="ch12 状态图式编排最小可实现")
    ap.add_argument("--demo", choices=["all", "triage", "merge", "fork"], default="all")
    ap.add_argument(
        "--approver",
        choices=["approve", "reject", "reject-once", "reject"],
        default="reject-once",
        help="人工审批策略：approve 一律批准 / reject 一律驳回 / reject-once 先驳回一次再批准",
    )
    ap.add_argument("--max-rounds", type=int, default=2, help="驳回后最多返工几轮，超了就升级")
    ap.add_argument("--trace", action="store_true", help="打印每一步的节点与写入")
    ap.add_argument("--live", action="store_true", help="切真实模型 API")
    ap.add_argument("--model", default=None, help="覆盖 LLM_MODEL")
    ap.add_argument("--base-url", default=None, help="覆盖 LLM_BASE_URL")
    args = ap.parse_args()

    llm = build_llm(args.live, args)

    if args.demo in ("all", "triage"):
        run_triage(llm, args.approver, args.max_rounds, args.trace)
    if args.demo in ("all", "merge"):
        run_merge(args.trace)
    if args.demo in ("all", "fork"):
        run_fork(llm, args.approver, args.max_rounds, args.trace)

    hr("跑完了")
    print("这个实现里有三个可以抄走的结论：")
    print("  1) 并行分支要合并，先想清楚 channel 用 add 还是 overwrite——归约语义写在 schema 里，不在节点里；")
    print("  2) 每步一个检查点，时间旅行就是挑一个旧检查点 fork 出去；")
    print("  3) interrupt() 让节点整个重跑，所以中断点之前的副作用必须幂等。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
