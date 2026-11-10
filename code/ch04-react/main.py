#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch04 ReAct 最小可实现 —— 纯标准库，无第三方依赖，无 API Key 也能跑通完整回路。

跑通的是 ReAct 的五个构件：
  1) 轨迹格式  Thought / Action / Action Input / Observation
  2) 动作解析  正则 + 容错（抽不出动作返回 None，不抛异常）
  3) 观察回灌  截断 + 保留标签净化（工具输出是不可信数据）
  4) 循环检测  同一 (action, args) 重复计数
  5) 停止条件  finish / 步数上限 / 循环检测 / 连续解析失败上限

用法：
    python main.py                                  # mock 模式，自带脚本化回复与假工具
    python main.py --task "..."                     # 换任务
    python main.py --scenario loop                  # 演示循环检测
    python main.py --scenario drift                 # 演示格式漂移中止
    python main.py --scenario recover               # 演示格式自我纠正
    python main.py --live                           # 切真实模型（需要环境变量）

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
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

# --------------------------------------------------------------------------
# 1. 保留标签与解析规则
# --------------------------------------------------------------------------

RESERVED = ("Thought:", "Action:", "Action Input:", "Observation:")

ACTION_RE = re.compile(r"Action\s*:\s*([A-Za-z_][A-Za-z0-9_]*)", re.I)
INPUT_RE = re.compile(
    r"Action\s*Input\s*:\s*(.*?)(?=\n\s*(?:Thought|Action|Observation)\s*:|\Z)",
    re.S | re.I,
)


@dataclass
class Action:
    name: str
    args: Any


@dataclass
class Tool:
    name: str
    description: str
    args: Dict[str, str]
    func: Optional[Callable[..., Any]]


def parse_action(text: str, valid_names: List[str]) -> Optional[Action]:
    """从模型输出里抽出一个动作；抽不出返回 None，交由上层降级。

    策略：找全部 Action 匹配，从后往前挑第一个命中已注册工具名的
    （模型可能先想一个动作再改主意，最后一个才是最终意图）。
    参数按字符串处理，遇到 { 开头尝试 json.loads，失败退回原字符串。
    """
    hits = list(ACTION_RE.finditer(text))
    for m in reversed(hits):
        name = m.group(1)
        if name not in valid_names:
            continue
        seg = text[m.end():]
        m2 = INPUT_RE.search(seg)
        raw = m2.group(1).strip() if m2 else ""
        args: Any = raw
        if raw.startswith("{"):
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = raw
        return Action(name=name, args=args)
    return None


def sanitize_observation(text: Any, limit: int) -> str:
    """观察是不可信数据：先毁掉保留标签的语义，再截断。

    工具返回里如果出现 `Action: xxx`，原样回灌会被模型当成自己的动作执行，
    轨迹当场被劫持，所以行首命中保留标签的一律改写前缀。
    """
    lines: List[str] = []
    for line in str(text).splitlines():
        stripped = line.strip()
        if any(stripped.startswith(tag) for tag in RESERVED):
            line = "[tool-output] " + stripped
        lines.append(line)
    out = "\n".join(lines)
    if len(out) > limit:
        head = limit // 2
        out = out[:head] + "\n...[truncated]...\n" + out[-(limit - head):]
    return out


# --------------------------------------------------------------------------
# 2. 工具：mock 数据与两个假工具
# --------------------------------------------------------------------------

KB: Dict[str, str] = {
    "albert einstein": (
        "Albert Einstein (14 March 1879 - 18 April 1955) was a German-born theoretical "
        "physicist. He was born in Ulm, in the Kingdom of Wuerttemberg in the German Empire."
    ),
    "germany capital": (
        "The capital of the German Empire (1871-1918) was Berlin. Berlin remained the seat "
        "of government after the empire became the Weimar Republic in 1918."
    ),
    "planet mars": (
        "Mars is the fourth planet from the Sun. It has a thin atmosphere and is known for "
        "its reddish appearance caused by iron oxide on the surface."
    ),
    "python language": (
        "Python is a high-level, general-purpose programming language created by Guido van "
        "Rossum and first released in 1991."
    ),
}


def _overlap(query: str, key: str) -> int:
    q = query.lower()
    return sum(1 for w in key.split() if w in q)


def tool_search(query: str) -> str:
    """按关键词检索百科，返回摘要文本。"""
    hits = [(key, val) for key, val in KB.items() if _overlap(query, key) > 0]
    if not hits:
        return "[no-result] no encyclopedia entry matches %r" % query
    hits.sort(key=lambda kv: -_overlap(query, kv[0]))
    return "\n".join("- %s: %s" % (key, val) for key, val in hits[:2])


def tool_lookup(entity: str) -> str:
    """按实体名查询百科条目，返回首段。"""
    best_key, best_score = None, 0
    for key in KB:
        score = _overlap(entity, key)
        if score > best_score:
            best_key, best_score = key, score
    if best_key is None:
        return "[no-entry] no entry named %r" % entity
    return KB[best_key]


TOOLS: List[Tool] = [
    Tool("search", "按关键词检索百科，返回摘要文本。",
         {"query": "str"}, tool_search),
    Tool("lookup", "按实体名查询百科条目，返回首段。",
         {"entity": "str"}, tool_lookup),
    Tool("finish", "结束任务并给出最终答案。",
         {"answer": "str"}, None),
]
TOOL_NAMES = [t.name for t in TOOLS]
TOOL_MAP = {t.name: t for t in TOOLS}


# --------------------------------------------------------------------------
# 3. 提示模板
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一个可以使用工具回答问题的 Agent。

可用工具：
- search(query: str) -> str    按关键词检索百科，返回摘要文本
- lookup(entity: str) -> str   按实体名查询百科条目，返回首段
- finish(answer: str) -> str   结束任务并给出最终答案

每一轮严格按以下格式输出，且只输出一轮：
Thought: <你的推理，说明当前已知信息和下一步意图>
Action: <工具名>
Action Input: <工具参数>

注意：不要自己写 Observation，Observation 由系统回填。
不确定的信息要靠工具查，不要凭记忆编。"""

FEWSHOT = """示例：

Thought: 需要先查一下目标的出生信息。
Action: search
Action Input: Albert Einstein
Observation: Albert Einstein (14 March 1879 - 18 April 1955) was a German-born theoretical physicist. He was born in Ulm, in the Kingdom of Wuerttemberg in the German Empire.
Thought: 已确认出生年份是 1879 年。接下来确认 1879 年德国的首都。
Action: search
Action Input: Germany capital
Observation: The capital of the German Empire (1871-1918) was Berlin. Berlin remained the seat of government after the empire became the Weimar Republic in 1918.
Thought: 两个事实都拿到了，可以收尾。
Action: finish
Action Input: 爱因斯坦出生于 1879 年，当时德意志帝国的首都是柏林。"""

TRANSCRIPT_MARK = "[[TRANSCRIPT]]"

DEFAULT_TASK = "阿尔伯特·爱因斯坦出生于哪一年？当时德国的首都是哪里？"


def build_prompt(task: str, transcript: List[Dict[str, str]]) -> str:
    parts = [FEWSHOT, "", "Task: " + task, "", TRANSCRIPT_MARK]
    for item in transcript:
        if item["kind"] == "assistant":
            parts.append(item["text"].strip())
        else:
            parts.append("Observation: " + item["text"].strip())
    return "\n".join(parts)


def render_transcript(transcript: List[Dict[str, str]]) -> str:
    out: List[str] = []
    for item in transcript:
        if item["kind"] == "assistant":
            out.append(item["text"].strip())
        else:
            out.append("Observation: " + item["text"].strip())
    return "\n\n".join(out)


# --------------------------------------------------------------------------
# 4. 模型接入：MockLLM / LiveLLM
# --------------------------------------------------------------------------


class MockLLM:
    """脚本化的假模型：根据当前轨迹状态决定下一轮输出，不需要 API Key。

    scenario:
      normal   正常三步：查 A -> 查 B -> finish
      loop     反复调同一个动作，用来触发循环检测
      drift    始终不输出合法 Action，用来触发格式漂移中止
      recover  第一轮格式不对，第二轮自我纠正后正常推进
    """

    def __init__(self, scenario: str = "normal") -> None:
        self.scenario = scenario
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        tail = prompt.split(TRANSCRIPT_MARK)[-1]
        # 只数真正的工具观察，格式错误与守卫提示不算证据
        obs_count = sum(
            1 for line in tail.splitlines()
            if line.startswith("Observation:")
            and "[format-error]" not in line
            and "[guard]" not in line
        )

        if self.scenario == "loop":
            return ("Thought: 我再查一次，应该就能拿到需要的数字了。\n"
                    "Action: search\n"
                    "Action Input: Albert Einstein")

        if self.scenario == "drift":
            return ("我大概需要先了解一下爱因斯坦的生平，然后再看看德意志帝国的首都，"
                    "这样才能回答问题。让我再想想。")

        if self.scenario == "recover" and self.calls == 1:
            # 第一轮忘了输出 Action 字段，被系统纠错后第二轮补上
            return "我先整理一下思路：这需要查外部资料，不能凭记忆回答。"

        if obs_count == 0:
            return ("Thought: 任务有两个事实要查：出生年份和当时的首都。先检索出生信息。\n"
                    "Action: search\n"
                    "Action Input: Albert Einstein")

        if obs_count == 1:
            return ("Thought: 观察里出现了出生年份，接下来确认 1879 年德意志帝国的首都。\n"
                    "Action: search\n"
                    "Action Input: Germany capital")

        return ("Thought: 两个观察都拿到了，事实已确认，可以收尾。\n"
                "Action: finish\n"
                "Action Input: 爱因斯坦出生于 1879 年（3 月 14 日），"
                "当时德意志帝国的首都是柏林。")


class LiveLLM:
    """真实模型：走 OpenAI 兼容的 chat completions 协议。"""

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
                {"role": "system", "content": SYSTEM_PROMPT},
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
# 5. ReAct 引擎
# --------------------------------------------------------------------------


class ReActAgent:
    def __init__(self, llm: Callable[[str], str], tools: List[Tool],
                 max_steps: int = 8, obs_limit: int = 600,
                 repeat_limit: int = 2, max_parse_fail: int = 2) -> None:
        self.llm = llm
        self.tools = {t.name: t for t in tools}
        self.max_steps = max_steps
        self.obs_limit = obs_limit
        self.repeat_limit = repeat_limit
        self.max_parse_fail = max_parse_fail
        self.counts: Counter = Counter()

    def _coerce_args(self, args: Any, tool: Tool) -> Dict[str, Any]:
        """把模型给的参数对齐到工具签名：字符串按第一个参数名塞进去。"""
        declared = list(tool.args.keys())
        if isinstance(args, dict):
            return {k: args[k] for k in declared if k in args}
        if isinstance(args, str) and declared:
            return {declared[0]: args}
        return {}

    def run(self, task: str, trace: bool = False) -> Dict[str, Any]:
        transcript: List[Dict[str, str]] = []
        parse_fail = 0
        status = "max_steps"
        answer: Any = None
        steps = 0

        for _ in range(self.max_steps):
            steps += 1
            prompt = build_prompt(task, transcript)
            if trace:
                print("\n" + "=" * 72)
                print("[prompt]\n" + prompt)
                print("=" * 72)

            raw = self.llm(prompt)
            action = parse_action(raw, list(self.tools.keys()))

            if action is None:
                parse_fail += 1
                transcript.append({"kind": "assistant", "text": raw})
                transcript.append({
                    "kind": "observation",
                    "text": "[format-error] 未检测到合法的 Action 字段，"
                            "请严格按 Thought / Action / Action Input 格式输出。",
                })
                if parse_fail >= self.max_parse_fail:
                    status = "parse_failed"
                    break
                continue

            parse_fail = 0
            transcript.append({"kind": "assistant", "text": raw})

            if action.name == "finish":
                status = "finished"
                answer = action.args
                break

            key = (action.name,
                   json.dumps(action.args, ensure_ascii=False, sort_keys=True, default=str))
            self.counts[key] += 1
            if self.counts[key] > self.repeat_limit:
                status = "loop_detected"
                transcript.append({
                    "kind": "observation",
                    "text": "[guard] 动作 %s 重复 %d 次，循环检测中止。"
                            % (action.name, self.counts[key]),
                })
                break

            tool = self.tools.get(action.name)
            if tool is None or tool.func is None:
                raw_obs = "[tool-error] unknown tool %r" % action.name
            else:
                try:
                    raw_obs = tool.func(**self._coerce_args(action.args, tool))
                except Exception as exc:  # 工具报错要变成观察，而不是崩掉整个循环
                    raw_obs = "[tool-error] %s: %s" % (type(exc).__name__, exc)

            transcript.append({
                "kind": "observation",
                "text": sanitize_observation(raw_obs, self.obs_limit),
            })

        return {
            "status": status,
            "answer": answer,
            "steps": steps,
            "transcript": transcript,
            "text": render_transcript(transcript),
        }


# --------------------------------------------------------------------------
# 6. CLI
# --------------------------------------------------------------------------


def build_llm(args: argparse.Namespace) -> Callable[[str], str]:
    if not args.live:
        return MockLLM(args.scenario)
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "--live 需要环境变量 LLM_API_KEY；"
            "可选 LLM_BASE_URL（默认 https://api.openai.com/v1）与 LLM_MODEL。\n"
        )
        raise SystemExit(2)
    base_url = args.base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model = args.model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    return LiveLLM(base_url, api_key, model)


def _force_utf8_stdout() -> None:
    """Windows 控制台默认可能是 GBK，强制 UTF-8 避免中文输出乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="ReAct 最小可实现（mock 模式无需 API Key）")
    parser.add_argument("--task", default=DEFAULT_TASK, help="要完成的任务")
    parser.add_argument("--live", action="store_true", help="切真实模型 API")
    parser.add_argument("--scenario", default="normal",
                        choices=["normal", "loop", "drift", "recover"],
                        help="mock 模式下的场景")
    parser.add_argument("--max-steps", type=int, default=8, help="步数上限")
    parser.add_argument("--obs-limit", type=int, default=600, help="观察截断长度")
    parser.add_argument("--repeat-limit", type=int, default=2, help="同动作重复几次判循环")
    parser.add_argument("--max-parse-fail", type=int, default=2, help="连续解析失败上限")
    parser.add_argument("--model", default="", help="覆盖 LLM_MODEL")
    parser.add_argument("--base-url", default="", help="覆盖 LLM_BASE_URL")
    parser.add_argument("--trace", action="store_true", help="打印每轮完整 prompt")
    args = parser.parse_args(argv)

    llm = build_llm(args)
    agent = ReActAgent(
        llm,
        TOOLS,
        max_steps=args.max_steps,
        obs_limit=args.obs_limit,
        repeat_limit=args.repeat_limit,
        max_parse_fail=args.max_parse_fail,
    )

    result = agent.run(args.task, trace=args.trace)

    print("=" * 72)
    print("task    :", args.task)
    print("status  :", result["status"])
    print("steps   :", result["steps"])
    print("-" * 72)
    print(result["text"])
    print("-" * 72)
    if result["status"] == "finished":
        print("answer  :", result["answer"])
    else:
        print("answer  : (未正常收尾)")
    print("=" * 72)
    return 0 if result["status"] == "finished" else 1


if __name__ == "__main__":
    raise SystemExit(main())
