#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch15 CrewAI 角色—任务—流程心智模型最小可实现 —— 纯标准库，无需 API Key 就能跑通完整回路。

这个文件用标准库复刻 CrewAI 的心智模型：

  Agent（角色 + 目标 + 背景）+ Task（描述 + 期望产出 + 上下文）+ Crew + Process
  Process.sequential    任务按顺序跑，前面的产出通过 context 传给后面
  Process.hierarchical  引入 manager：分配 -> 执行 -> 验收，每个任务多两次调用
  Flow                  @start / @listen / @router 事件驱动，把 Crew 装进确定性的控制流

四个演示场景：

  crew        顺序 Crew：角色三件套怎么拼成提示词、上下文怎么传
  expected    expected_output 只是提示，不是契约——产出不合格 Crew 照样报成功
  hierarchical 顺序 vs 层级：manager 带来的额外调用成本
  flow        Flow 包 Crew：确定性分支与人工闸门写在代码里

用法：
    python main.py                    # 跑全部场景
    python main.py --demo crew        # 只跑顺序 Crew
    python main.py --demo expected    # 只跑期望产出的"软约束"演示
    python main.py --demo hierarchical # 只跑顺序 vs 层级
    python main.py --demo flow        # 只跑 Flow
    python main.py --trace            # 打印每个 Agent 收到的完整提示词
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
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文结果乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ==========================================================================
# 1. 三个基本件：Agent / Task / Tool
# ==========================================================================


@dataclass
class Tool:
    """一个工具，挂在 Agent 上。"""

    name: str
    description: str
    fn: Callable[..., Any]


@dataclass
class Task:
    """一件活儿：描述（可带占位符）+ 期望产出 + 干活的 Agent + 上下文来源。

    context 里放的是**任务对象**，不是字符串——框架在运行前把那些任务的
    实际产出拼进本题的提示词里。这是 CrewAI 传上下文的方式。
    """

    description: str
    expected_output: str
    agent: Optional["Agent"] = None
    context: List["Task"] = field(default_factory=list)
    output: Optional[str] = None
    output_pydantic: Optional[type] = None

    def render(self, inputs: Dict[str, Any]) -> str:
        """把描述里的 {topic} 这类占位符替换掉。"""
        try:
            return self.description.format(**inputs)
        except KeyError as exc:
            raise KeyError("任务描述里的占位符 %s 没有对应输入" % exc) from exc


@dataclass
class Agent:
    """一个角色：角色名 + 目标 + 背景。

    这三件套直接决定系统提示词怎么写——CrewAI 的整套心智模型就压在这里。
    allow_delegation 打开后，这个角色额外拿到"把活儿派给同事"的工具。
    """

    role: str
    goal: str
    backstory: str
    tools: List[Tool] = field(default_factory=list)
    allow_delegation: bool = False
    max_iter: int = 3

    @staticmethod
    def _interp(text: str, inputs: Dict[str, Any]) -> str:
        """把文本里的 {topic} 这类占位符替换掉；没有占位符就原样返回。"""
        if "{" not in text:
            return text
        try:
            return text.format(**inputs)
        except KeyError as exc:
            raise KeyError("提示词里的占位符 %s 没有对应输入" % exc) from exc

    def build_prompt(self, task: Task, inputs: Dict[str, Any], context_text: str) -> str:
        """把角色三件套、任务描述、期望产出、上下文拼成一条提示词。

        这条提示词就是 CrewAI 的全部"编排信息"——没有别的隐藏配置。
        """
        parts: List[str] = []
        parts.append("你是%s。" % self.role)
        parts.append("目标：%s" % self._interp(self.goal, inputs))
        parts.append("背景：%s" % self.backstory)
        if self.tools:
            parts.append("可用工具：%s" % "、".join(t.name for t in self.tools))
        if self.allow_delegation:
            parts.append("你可以使用 Ask question to coworker / Delegate work to coworker 与同事协作。")
        if context_text:
            parts.append("参考上下文（来自前置任务的产出）：\n%s" % context_text)
        parts.append("任务描述：%s" % task.render(inputs))
        parts.append("期望产出：%s" % task.expected_output)
        return "\n".join(parts)


@dataclass
class TaskOutput:
    """一个任务的产出。"""

    task: Task
    raw: str
    agent: str
    prompt: str = ""


@dataclass
class CrewOutput:
    """一次 kickoff 的整体产出。"""

    raw: str
    tasks: List[TaskOutput]
    llm_calls: int
    manager_calls: int = 0


class Process:
    SEQUENTIAL = "sequential"
    HIERARCHICAL = "hierarchical"


# ==========================================================================
# 2. Crew：组装与运行
# ==========================================================================


class Crew:
    """一组角色 + 一组任务 + 一种流程。

    组装模型很直白：agents 和 tasks 是两个平铺的列表，process 决定它们
    怎么被驱动。没有图，没有节点，没有边。
    """

    def __init__(
        self,
        agents: List[Agent],
        tasks: List[Task],
        process: str = Process.SEQUENTIAL,
        manager_agent: Optional[Agent] = None,
    ) -> None:
        self.agents = agents
        self.tasks = tasks
        self.process = process
        self.manager_agent = manager_agent
        self.last_prompts: List[Tuple[str, str]] = []

    # -- 上下文拼装 --

    @staticmethod
    def _context_text(task: Task, inputs: Dict[str, Any]) -> str:
        """把 task.context 里那些任务的产出拼起来。"""
        chunks: List[str] = []
        for ctx_task in task.context:
            if ctx_task.output:
                chunks.append("【%s 的产出】\n%s" % (ctx_task.render(inputs), ctx_task.output))
        return "\n\n".join(chunks)

    def _agent_for(self, task: Task) -> Optional[Agent]:
        if task.agent is not None:
            return task.agent
        return self.agents[0] if self.agents else None

    def kickoff(self, inputs: Optional[Dict[str, Any]] = None, llm: Optional["LLMBase"] = None) -> CrewOutput:
        inputs = inputs or {}
        llm = llm or MockLLM()
        self.last_prompts = []
        if self.process == Process.HIERARCHICAL:
            return self._kickoff_hierarchical(inputs, llm)
        return self._kickoff_sequential(inputs, llm)

    def _kickoff_sequential(self, inputs: Dict[str, Any], llm: "LLMBase") -> CrewOutput:
        """顺序流程：任务按列表顺序跑，每个任务一次模型调用。"""
        outputs: List[TaskOutput] = []
        calls = 0
        for task in self.tasks:
            agent = self._agent_for(task)
            if agent is None:
                continue
            prompt = agent.build_prompt(task, inputs, self._context_text(task, inputs))
            self.last_prompts.append((agent.role, prompt))
            raw = llm.complete(prompt, agent.role, task)
            calls += 1
            task.output = raw
            outputs.append(TaskOutput(task=task, raw=raw, agent=agent.role, prompt=prompt))
        return CrewOutput(raw=outputs[-1].raw if outputs else "", tasks=outputs, llm_calls=calls)

    def _kickoff_hierarchical(self, inputs: Dict[str, Any], llm: "LLMBase") -> CrewOutput:
        """层级流程：manager 分配 -> 成员执行 -> manager 验收。

        每个任务多两次模型调用（分配 + 验收），这是层级的固有成本。
        """
        if self.manager_agent is None:
            raise ValueError("层级流程必须给 manager_agent 或 manager_llm")
        manager = self.manager_agent
        outputs: List[TaskOutput] = []
        calls = 0
        manager_calls = 0

        for task in self.tasks:
            # 1) manager 分配：按角色挑人
            assign_prompt = manager.build_prompt(
                task, inputs, "可用成员及其角色：\n%s" % self._roster()
            )
            self.last_prompts.append(("manager", assign_prompt))
            assignee_role = llm.assign(assign_prompt, self._roster(), task.render(inputs))
            manager_calls += 1
            calls += 1

            agent = self._agent_by_role(assignee_role) or self._agent_for(task)
            if agent is None:
                continue

            # 2) 成员执行
            prompt = agent.build_prompt(task, inputs, self._context_text(task, inputs))
            self.last_prompts.append((agent.role, prompt))
            raw = llm.complete(prompt, agent.role, task)
            calls += 1
            task.output = raw

            # 3) manager 验收
            review_prompt = "验收下面这份产出，判断是否满足期望产出「%s」。只回答 PASS 或 FAIL。\n\n%s" % (
                task.expected_output,
                raw,
            )
            self.last_prompts.append(("manager", review_prompt))
            verdict = llm.review(review_prompt)
            manager_calls += 1
            calls += 1

            outputs.append(
                TaskOutput(
                    task=task,
                    raw=raw,
                    agent=agent.role,
                    prompt=prompt,
                )
            )
        return CrewOutput(
            raw=outputs[-1].raw if outputs else "",
            tasks=outputs,
            llm_calls=calls,
            manager_calls=manager_calls,
        )

    def _roster(self) -> List[str]:
        return [a.role for a in self.agents]

    def _agent_by_role(self, role: str) -> Optional[Agent]:
        for a in self.agents:
            if a.role == role:
                return a
        return None


# ==========================================================================
# 3. 模型层：mock 与 live
# ==========================================================================


class LLMBase:
    """模型要会三件事：干活、分配、验收。"""

    def complete(self, prompt: str, role: str, task: Task) -> str:
        raise NotImplementedError

    def assign(self, prompt: str, roster: Sequence[str], task_text: str = "") -> str:
        """manager 派活儿：按角色挑人。

        task_text 是任务描述，只拿它来打分——提示词里还带着成员名单，
        "研究员"里就含"研究"，拿整条提示词匹配会误判。
        """
        raise NotImplementedError

    def review(self, prompt: str) -> str:
        raise NotImplementedError


class MockLLM(LLMBase):
    """离线 mock：按角色人设返回产出。

    它不代表任何真实模型的表现，只保证没有 API Key 也能把流程跑通。
    """

    def __init__(self, underdeliver: bool = False) -> None:
        self.calls = 0
        self.assign_calls = 0
        self.review_calls = 0
        # 故意少交：用来演示 expected_output 并不强制
        self.underdeliver = underdeliver
        self.personas: Dict[str, Callable[[str, Task], str]] = {}
        # 角色 -> 任务描述里的关键词，mock manager 靠它派活儿
        self.assign_keywords: Dict[str, List[str]] = {
            "研究员": ["调研", "研究"],
            "撰稿人": ["写", "简报"],
            "审校": ["审核", "校对"],
        }

    def complete(self, prompt: str, role: str, task: Task) -> str:
        self.calls += 1
        persona = self.personas.get(role)
        if persona is None:
            return "[%s] 已完成：%s" % (role, task.description)
        return persona(prompt, task)

    def assign(self, prompt: str, roster: Sequence[str], task_text: str = "") -> str:
        self.assign_calls += 1
        # 规则派活儿：按任务描述里的关键词打分，谁命中多派谁。
        # 只看任务描述，不看整条提示词——名单里的"研究员"本身含"研究"两个字。
        text = task_text or prompt
        best_role, best_score = "", 0
        for role, words in self.assign_keywords.items():
            if role not in roster:
                continue
            score = sum(1 for w in words if w in text)
            if score > best_score:
                best_role, best_score = role, score
        if best_role:
            return best_role
        for r in roster:
            if r and r in text:
                return r
        return roster[0] if roster else ""

    def review(self, prompt: str) -> str:
        self.review_calls += 1
        return "PASS"


class MockPersonas:
    """各角色的人设。"""

    @staticmethod
    def researcher(prompt: str, task: Task) -> str:
        if "调研" in task.description or "研究" in task.description:
            return (
                "调研结论（5 条）：\n"
                "1. 多 Agent 编排正在从'写死流程'转向'模型选路'；\n"
                "2. 工具调用协议趋于统一，跨语言生态互相借鉴；\n"
                "3. 状态管理的难点从'存什么'转向'怎么回溯'；\n"
                "4. 评测缺口大于工程缺口，公开可复现的基准仍然稀缺；\n"
                "5. 成本控制开始下沉到框架层，按 Agent 设预算成为常见做法。"
            )
        return "[研究员] 已整理材料。"

    @staticmethod
    def writer(prompt: str, task: Task) -> str:
        return (
            "简报：近期开源 Agent 编排框架出现明显分工——"
            "状态图路线解决流程控制，数据框架解决检索质量，"
            "轻量循环路线解决成本与可读性。三个方向没有替代关系，"
            "取决于你的难点落在控制流还是数据上。"
        )

    @staticmethod
    def reviewer(prompt: str, task: Task) -> str:
        return "审校意见：结论与调研一致，建议对外表述时补一句适用范围。"

    @staticmethod
    def lazy_researcher(prompt: str, task: Task) -> str:
        """只交 2 条，远少于期望的 5 条。"""
        return "调研结论（2 条）：\n1. 编排框架在分工；\n2. 评测仍是缺口。"


def build_llm(live: bool, args, underdeliver: bool = False) -> LLMBase:
    if not live:
        llm = MockLLM(underdeliver=underdeliver)
        llm.personas["研究员"] = MockPersonas.lazy_researcher if underdeliver else MockPersonas.researcher
        llm.personas["撰稿人"] = MockPersonas.writer
        llm.personas["审校"] = MockPersonas.reviewer
        return llm

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        print("错误：--live 需要环境变量 LLM_API_KEY", file=sys.stderr)
        raise SystemExit(2)
    base_url = args.base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model = args.model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    return LiveLLM(api_key, base_url, model)


class LiveLLM(LLMBase):
    """真实模型：走 OpenAI 兼容的 chat completions 端点。"""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _chat(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
        }
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

    def complete(self, prompt: str, role: str, task: Task) -> str:
        return self._chat(prompt).strip()

    def assign(self, prompt: str, roster: Sequence[str], task_text: str = "") -> str:
        raw = self._chat(
            "从下面的成员里挑一个最适合干这活的，只回答角色名：\n%s\n\n%s"
            % ("、".join(roster), prompt)
        )
        for r in roster:
            if r and r in raw:
                return r
        return roster[0] if roster else ""

    def review(self, prompt: str) -> str:
        raw = self._chat(prompt).strip().upper()
        return "PASS" if "PASS" in raw else "FAIL"


# ==========================================================================
# 4. Flow：事件驱动的确定性控制流
# ==========================================================================


class Flow:
    """事件驱动的工作流层：@start / @listen / @router。

    和 Crew 的区别很清楚：Flow 里没有角色扮演，每一步都是普通 Python
    函数，分支写在代码里而不是交给模型。Crew 负责中间那段开放式的活，
    Flow 负责前后的确定性控制。
    """

    def __init__(self) -> None:
        self.methods: Dict[str, Callable[["Flow"], Any]] = {}
        self.listeners: Dict[str, List[str]] = {}
        self.router_of: Dict[str, str] = {}
        self.state: Dict[str, Any] = {}
        self.trace: List[Tuple[str, Any]] = []

    def start(self, fn: Callable[["Flow"], Any]) -> Callable[["Flow"], Any]:
        self.methods[fn.__name__] = fn
        self.listeners.setdefault("__start__", []).append(fn.__name__)
        return fn

    def listen(self, trigger: str):
        def deco(fn):
            self.methods[fn.__name__] = fn
            self.listeners.setdefault(trigger, []).append(fn.__name__)
            return fn

        return deco

    def router(self, trigger: str):
        def deco(fn):
            self.methods[fn.__name__] = fn
            self.listeners.setdefault(trigger, []).append(fn.__name__)
            self.router_of[fn.__name__] = trigger
            return fn

        return deco

    def run(self, **init: Any) -> Dict[str, Any]:
        self.state.update(init)
        pending: List[Tuple[str, Any]] = [("__start__", None)]
        fired: set = set()
        while pending:
            trigger, payload = pending.pop(0)
            for name in list(self.listeners.get(trigger, [])):
                if name in fired:
                    continue
                fired.add(name)
                result = self.methods[name](self)
                self.trace.append((name, result))
                pending.append((name, result))
                # router 方法的返回值是一个标签，标签本身也是触发器
                if name in self.router_of:
                    pending.append((str(result), None))
        return self.state


# ==========================================================================
# 5. 演示
# ==========================================================================


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def build_demo_crew(llm: LLMBase) -> Tuple[Crew, Agent, Agent]:
    """研究员 -> 撰稿人，两个任务，第二个依赖第一个的产出。"""
    researcher = Agent(
        role="研究员",
        goal="把 {topic} 的关键事实查清楚，每条都要有出处",
        backstory="在数据平台做了六年，只信能追到原始记录的数字。",
        tools=[Tool(name="search", description="检索资料", fn=lambda q: "")],
    )
    writer = Agent(
        role="撰稿人",
        goal="把研究结论写成一段读得懂、读得完的简报",
        backstory="做过三年技术编辑，讨厌堆术语。",
    )
    research_task = Task(
        description="调研 {topic} 最近的进展",
        expected_output="5 条要点，每条带出处",
        agent=researcher,
    )
    write_task = Task(
        description="根据调研结果写一份 {topic} 简报",
        expected_output="一段 300 字以内的简报",
        agent=writer,
        context=[research_task],
    )
    crew = Crew(
        agents=[researcher, writer],
        tasks=[research_task, write_task],
        process=Process.SEQUENTIAL,
    )
    return crew, research_task, write_task


def show_prompts(crew: Crew, trace: bool) -> None:
    if not trace:
        return
    print("\n每个角色实际收到的提示词：")
    for role, prompt in crew.last_prompts:
        print("\n--- %s ---" % role)
        for line in prompt.splitlines():
            print("  " + line)


def run_crew(live: bool, trace: bool) -> None:
    hr("场景一：顺序 Crew —— 角色三件套怎么变成提示词")
    llm = build_llm(live, argparse.Namespace(model=None, base_url=None))
    crew, research_task, write_task = build_demo_crew(llm)
    inputs = {"topic": "开源 Agent 编排框架"}

    result = crew.kickoff(inputs=inputs, llm=llm)

    print("任务 1：%s" % research_task.description.format(**inputs))
    print("  期望产出：%s" % research_task.expected_output)
    print("  执行者  ：%s" % result.tasks[0].agent)
    print("  实际产出：")
    for line in result.tasks[0].raw.splitlines():
        print("    " + line)

    print("\n任务 2：%s" % write_task.description.format(**inputs))
    print("  期望产出：%s" % write_task.expected_output)
    print("  执行者  ：%s" % result.tasks[1].agent)
    print("  上下文来源：任务 1（通过 context=[...] 传入）")
    print("  实际产出：%s" % result.tasks[1].raw)

    print("\n模型调用次数：%d（每个任务一次）" % result.llm_calls)
    show_prompts(crew, trace)

    print(
        "\n要点：\n"
        "  1) 角色三件套 + 任务描述 + 期望产出 + 上下文，合成一条提示词——没有别的隐藏配置；\n"
        "  2) 上下文靠 context=[前一个任务] 传，前一个任务的实际产出被拼进后一个的提示词；\n"
        "  3) 期望产出是提示词里的一句话，它会影响产出，但不强制产出。"
    )


def check_expected_output(task: Task, raw: str) -> Tuple[bool, str]:
    """自己写一道校验：产出里要有期望的条数。

    这就是"软约束"要变"硬约束"必须补的那一步。
    """
    m = re.search(r"(\d+)\s*条", task.expected_output)
    if not m:
        return True, "期望产出里没有可校验的条数，跳过"
    want = int(m.group(1))
    got = len(re.findall(r"^\s*\d+\.", raw, flags=re.M))
    if got >= want:
        return True, "条数 %d >= 期望 %d，通过" % (got, want)
    return False, "条数 %d < 期望 %d，不通过" % (got, want)


def run_expected(live: bool, trace: bool) -> None:
    hr("场景二：expected_output 只是提示，不是契约")
    llm = build_llm(live, argparse.Namespace(model=None, base_url=None), underdeliver=True)
    crew, research_task, _write = build_demo_crew(llm)
    result = crew.kickoff(inputs={"topic": "开源 Agent 编排框架"}, llm=llm)

    print("期望产出：%s" % research_task.expected_output)
    print("实际产出：")
    for line in result.tasks[0].raw.splitlines():
        print("  " + line)
    print("\nCrew 的判断：任务成功产出，无报错（结果里 %d 个任务都有输出）" % len(result.tasks))

    ok, reason = check_expected_output(research_task, result.tasks[0].raw)
    print("自己加一道校验：%s" % reason)

    print(
        "\n这是 CrewAI 心智模型最容易踩的一脚：\n"
        "  expected_output 会被写进提示词当'合格线'，但框架不会拿它去验收产出。\n"
        "  角色扮演还额外放大了这一点——模型有权决定自己怎么干，写得不够也不拦。\n"
        "  要变成硬约束，只能自己补：给任务挂一个 guardrail，或者在 Flow 里写死校验。"
    )


def run_hierarchical(live: bool, trace: bool) -> None:
    hr("场景三：顺序 vs 层级 —— manager 带来的额外成本")
    inputs = {"topic": "开源 Agent 编排框架"}

    # 顺序
    llm_seq = build_llm(live, argparse.Namespace(model=None, base_url=None))
    crew_seq, _, _ = build_demo_crew(llm_seq)
    res_seq = crew_seq.kickoff(inputs=inputs, llm=llm_seq)

    # 层级
    llm_h = build_llm(live, argparse.Namespace(model=None, base_url=None))
    researcher = Agent(
        role="研究员",
        goal="把 {topic} 的关键事实查清楚，每条都要有出处",
        backstory="在数据平台做了六年，只信能追到原始记录的数字。",
    )
    writer = Agent(
        role="撰稿人",
        goal="把研究结论写成一段读得懂、读得完的简报",
        backstory="做过三年技术编辑，讨厌堆术语。",
    )
    manager = Agent(
        role="项目经理",
        goal="把活儿分给合适的人，并验收产出",
        backstory="带过三个研发团队，最烦的是活儿派错人。",
        allow_delegation=True,
    )
    t1 = Task(description="调研 {topic} 最近的进展", expected_output="5 条要点，每条带出处", agent=researcher)
    t2 = Task(
        description="根据调研结果写一份 {topic} 简报",
        expected_output="一段 300 字以内的简报",
        agent=writer,
        context=[t1],
    )
    crew_h = Crew(
        agents=[researcher, writer],
        tasks=[t1, t2],
        process=Process.HIERARCHICAL,
        manager_agent=manager,
    )
    res_h = crew_h.kickoff(inputs=inputs, llm=llm_h)

    print("%-18s %-16s %-16s" % ("维度", "顺序 sequential", "层级 hierarchical"))
    print("-" * 56)
    print("%-18s %-16s %-16s" % ("任务数", str(len(res_seq.tasks)), str(len(res_h.tasks))))
    print("%-18s %-16s %-16s" % ("模型调用总数", str(res_seq.llm_calls), str(res_h.llm_calls)))
    print("%-18s %-16s %-16s" % ("其中 manager 调用", "0", str(res_h.manager_calls)))
    print("%-18s %-16s %-16s" % ("平均每个任务", "%.1f 次" % (res_seq.llm_calls / max(len(res_seq.tasks), 1)),
                                  "%.1f 次" % (res_h.llm_calls / max(len(res_h.tasks), 1))))
    print("-" * 56)

    print("\n层级流程里 manager 的两次派活结果：")
    for i, out in enumerate(res_h.tasks, 1):
        print("  任务 %d -> %s" % (i, out.agent))

    print("\n层级流程每个任务多两次调用：一次分配、一次验收。")
    print("换来的是：派活儿这件事由 manager 决定，不写在代码里；")
    print("也带来的是：manager 派错人时，多出来的那两次调用纯属浪费，且没人拦。")

    show_prompts(crew_h, trace)


def build_demo_flow(llm: LLMBase) -> Tuple[Flow, Crew]:
    """Flow 里包 Crew：确定性分支 + 人工闸门在代码里。"""

    researcher = Agent(
        role="研究员",
        goal="把 {topic} 的关键事实查清楚，每条都要有出处",
        backstory="在数据平台做了六年，只信能追到原始记录的数字。",
    )
    writer = Agent(
        role="撰稿人",
        goal="把研究结论写成一段读得懂、读得完的简报",
        backstory="做过三年技术编辑，讨厌堆术语。",
    )
    research_task = Task(
        description="调研 {topic} 最近的进展",
        expected_output="5 条要点，每条带出处",
        agent=researcher,
    )
    write_task = Task(
        description="根据调研结果写一份 {topic} 简报",
        expected_output="一段 300 字以内的简报",
        agent=writer,
        context=[research_task],
    )
    research_crew = Crew(agents=[researcher], tasks=[research_task], process=Process.SEQUENTIAL)
    write_crew = Crew(agents=[writer], tasks=[write_task], process=Process.SEQUENTIAL)

    flow = Flow()

    @flow.start
    def prepare(self) -> str:
        self.state["topic"] = "开源 Agent 编排框架"
        return "prepared"

    @flow.listen("prepare")
    def do_research(self) -> str:
        out = research_crew.kickoff(inputs={"topic": self.state["topic"]}, llm=llm)
        self.state["research"] = out.raw
        self.state["research_task"] = research_task
        return "researched"

    @flow.router("do_research")
    def gate(self) -> str:
        # 确定性判断写在代码里，不交给模型
        ok, reason = check_expected_output(research_task, self.state.get("research", ""))
        self.state["gate"] = reason
        return "enough" if ok else "insufficient"

    @flow.listen("enough")
    def do_write(self) -> str:
        out = write_crew.kickoff(inputs={"topic": self.state["topic"]}, llm=llm)
        self.state["brief"] = out.raw
        return "written"

    @flow.listen("insufficient")
    def need_more(self) -> str:
        self.state["brief"] = "材料不足，先补齐再出简报。"
        return "halted"

    return flow, research_crew


def run_flow(live: bool, trace: bool) -> None:
    hr("场景四：Flow 包 Crew —— 确定性控制流放在代码里")
    llm = build_llm(live, argparse.Namespace(model=None, base_url=None))
    flow, _ = build_demo_flow(llm)
    state = flow.run()

    print("执行轨迹：")
    for name, result in flow.trace:
        print("  %-14s -> %s" % (name, result))
    print("\n闸门判定：%s" % state.get("gate"))
    print("最终简报：%s" % state.get("brief"))
    print(
        "\n要点：\n"
        "  1) Flow 的每一步都是普通 Python 函数，分支和校验写在代码里，可断言、可单测；\n"
        "  2) Crew 被当成一个黑盒步骤调用——开放式的活交给角色，确定性的活留给代码；\n"
        "  3) 闸门那一刀就是上一节缺失的校验：期望产出在这里被真正检查了一次。"
    )


# ==========================================================================
# 6. CLI
# ==========================================================================


def main() -> int:
    ap = argparse.ArgumentParser(description="ch15 CrewAI 心智模型最小可实现")
    ap.add_argument("--demo", choices=["all", "crew", "expected", "hierarchical", "flow"], default="all")
    ap.add_argument("--trace", action="store_true", help="打印每个 Agent 收到的完整提示词")
    ap.add_argument("--live", action="store_true", help="切真实模型 API")
    ap.add_argument("--model", default=None, help="覆盖 LLM_MODEL")
    ap.add_argument("--base-url", default=None, help="覆盖 LLM_BASE_URL")
    args = ap.parse_args()

    if args.demo in ("all", "crew"):
        run_crew(args.live, args.trace)
    if args.demo in ("all", "expected"):
        run_expected(args.live, args.trace)
    if args.demo in ("all", "hierarchical"):
        run_hierarchical(args.live, args.trace)
    if args.demo in ("all", "flow"):
        run_flow(args.live, args.trace)

    hr("跑完了")
    print("这个实现里有五个可以抄走的结论：")
    print("  1) 角色三件套（角色/目标/背景）决定提示词，任务描述和期望产出拼在同一段里；")
    print("  2) 上下文靠 context=[前一个任务] 传，传的是实际产出，不是摘要；")
    print("  3) expected_output 是提示不是契约，框架不拿它验收产出；")
    print("  4) 层级流程每个任务多两次模型调用（分配 + 验收），成本要算进去；")
    print("  5) Flow 是补丁也是出路：把确定性的部分收进代码，把开放式的留给 Crew。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
