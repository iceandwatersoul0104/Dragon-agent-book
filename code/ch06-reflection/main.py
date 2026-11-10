#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ch06 反思与自纠错最小可实现 —— 纯标准库，无需 API Key 就能跑通完整回路。

演示内容：
  1) 三种反思形态：reflexion（带记忆重试）/ self_refine（多轮改写）/ judge（独立评审者）
  2) 三种反思信号：rule（真跑测试）/ llm（同一个模型自评）/ judge（独立评审者）
  3) 四道熔断闸：最大轮次、无进展检测、反思去重、成本预算

用法：
    python main.py                                  # 默认：judge 形态 + 规则信号
    python main.py --form reflexion --critic rule   # 带记忆重试
    python main.py --form self_refine --critic rule # 多轮改写
    python main.py --form judge --critic llm        # 自评 -> 无进展熔断
    python main.py --form judge --critic judge      # 独立评审者定位缺陷
    python main.py --live                           # 切真实模型

--live 所需环境变量：
    LLM_API_KEY    必填，模型服务商的 API Key
    LLM_BASE_URL   可选，默认 https://api.openai.com/v1
    LLM_MODEL      可选，默认 gpt-4o-mini
端点需兼容 `POST {base}/chat/completions`：body 含 model/messages，
返回 choices[0].message.content。换供应商时只改 base_url 与 model。

安全提示：规则评审会在受限命名空间里 exec 候选代码来跑断言。
生产环境必须换成真正的沙箱（容器 / seccomp / 受限进程），不要在主进程里跑模型产出的代码。
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
# 1. 任务、测试与候选代码
# --------------------------------------------------------------------------

SPEC = ("实现 is_palindrome(s)：忽略大小写与非字母字符，判断字符串是否为回文串；"
        "空串视为回文。只输出函数定义，不要解释。")

TESTS: List[Tuple[str, bool]] = [
    ("A man, a plan, a canal: Panama", True),
    ("racecar", True),
    ("No 'x' in Nixon", True),
    ("hello", False),
    ("", True),
    ("12321", True),
]

BUGGY_CODE = """def is_palindrome(s):
    return s == s[::-1]
"""

FIXED_CODE = """def is_palindrome(s):
    t = "".join(ch.lower() for ch in s if ch.isalpha())
    return t == t[::-1]
"""

# 候选代码的修复信号。只放"只出现在失败信息里"的词——
# 不能放需求里就有的词（比如"非字母字符"），否则 mock 会第一轮就修好，演示不出反思。
FIX_MARKERS = ("AssertionError", "归一化", "isalpha", "过滤")


def run_tests(code: str) -> Tuple[bool, str]:
    """真跑测试：受限命名空间里执行候选代码并断言。

    生产环境请换成真沙箱——这里只处理本例的玩具代码。
    """
    namespace: Dict[str, Any] = {"__builtins__": {"len": len, "range": range,
                                                  "str": str, "print": print,
                                                  "isinstance": isinstance}}
    try:
        exec(compile(code, "<candidate>", "exec"), namespace)  # noqa: S102 (演示用)
    except Exception as exc:
        return False, "AssertionError: 执行候选代码失败 %s: %s" % (type(exc).__name__, exc)
    fn = namespace.get("is_palindrome")
    if fn is None:
        return False, "AssertionError: 候选代码里没有 is_palindrome 函数"
    failures: List[str] = []
    for inp, expected in TESTS:
        try:
            got = fn(inp)
        except Exception as exc:
            failures.append("输入 %r 抛出 %s: %s" % (inp, type(exc).__name__, exc))
            continue
        if got != expected:
            failures.append("输入 %r 期望 %r，实际 %r" % (inp, expected, got))
    if failures:
        return False, "AssertionError: " + "；".join(failures)
    return True, "通过：全部 %d 个用例通过" % len(TESTS)


# --------------------------------------------------------------------------
# 2. 提示模板
# --------------------------------------------------------------------------

AUTHOR_SYSTEM = "你是资深 Python 工程师。只输出可运行的函数代码，不要输出解释。"
CRITIC_SYSTEM = ("你是代码评审者。只输出问题清单，不要表扬。"
                 "如果确实找不到问题，输出“未发现明显问题”。")
JUDGE_SYSTEM = ("你是独立的验收评审者，没有参与代码编写。"
                "只依据需求规格判定，输出“不通过 + 缺陷清单”或“通过”。")
REFLECT_SYSTEM = "你负责复盘失败原因。只输出具体的反思，说明哪一步想错了、下一轮怎么改。"


def build_author_prompt(form: str, spec: str, draft: Optional[str],
                        feedback: str, memory: List[str]) -> str:
    parts = ["需求：%s" % spec]
    if form == "reflexion":
        if memory:
            parts.append("以下是之前的失败与反思，请避免重犯：\n" + "\n".join(memory))
    else:
        if draft:
            parts.append("当前实现：\n%s" % draft)
        if feedback:
            parts.append("评审意见：\n%s" % feedback)
    return "\n\n".join(parts)


def build_critic_prompt(spec: str, code: str, role: str) -> str:
    parts = ["需求：%s" % spec, "候选实现：\n%s" % code]
    if role == "judge":
        parts.append("请只依据需求规格判定是否满足；不通过时必须指出具体是哪一处不满足。")
    else:
        parts.append("请指出这份实现的问题；不要夸奖。")
    return "\n\n".join(parts)


def build_reflect_prompt(failure: str) -> str:
    return "刚才的尝试失败了，失败信息如下：\n%s\n\n请给出具体反思。" % failure


def strip_code(text: str) -> str:
    """模型常把代码包在 ```python 围栏里，剥掉。"""
    m = re.search(r"```(?:python)?\s*(.*?)```", text, flags=re.S)
    return m.group(1).strip() if m else text.strip()


def parse_verdict(text: str) -> Tuple[str, str]:
    """把评审文本解析成 (verdict, text)。判不准时按 fail 处理，交给熔断。"""
    t = text.strip()
    if "不通过" in t or "FAIL" in t.upper() or "失败" in t:
        return "fail", t
    if "通过" in t:
        return "pass", t
    return "fail", t


def normalize_reflection(text: str) -> str:
    """反思判重前先规范化：压空白、去标点、忽略大小写。"""
    return re.sub(r"[\s，。、；：！？.,;:!?]+", "", text).lower()


def code_hash(code: str) -> str:
    import hashlib
    return hashlib.sha1(code.strip().encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------------
# 3. 模型接入：MockLLM / LiveLLM
# --------------------------------------------------------------------------


class MockLLM:
    """按提示特征模拟各角色的行为，不是真推理。

    author: 提示里出现"可定位的失败信息"时给出修正版，否则给带 bug 的版本
    critic: 永远给不出可定位的缺陷（自评确认偏见）
    judge:  能定位缺陷——检查候选代码有没有做归一化
    reflect:把失败信息转成一句反思
    """

    def __call__(self, role: str, prompt: str) -> str:
        if role == "author":
            if any(m in prompt for m in FIX_MARKERS):
                return FIXED_CODE
            return BUGGY_CODE
        if role == "critic":
            return "建议再检查一下边界情况。"
        if role == "judge":
            code = prompt
            if "isalpha" in code:
                return "通过：已做大小写与非字母字符归一化。"
            return ("不通过：实现直接比较原串与其反转，没有做归一化处理——"
                    "既没有过滤非字母字符，也没有统一大小写，"
                    "例如 'A man, a plan, a canal: Panama' 会被误判为 False。")
        if role == "reflect":
            if any(m in prompt for m in FIX_MARKERS):
                return "上一轮错在比较前没有归一化：直接比较原串与其反转，忽略了大小写和非字母字符。"
            return "再检查一下边界情况。"
        return ""


class LiveLLM:
    """真实模型：OpenAI 兼容的 chat completions 协议，按角色切换系统提示。"""

    SYSTEMS = {
        "author": AUTHOR_SYSTEM,
        "critic": CRITIC_SYSTEM,
        "judge": JUDGE_SYSTEM,
        "reflect": REFLECT_SYSTEM,
    }

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 60, temperature: float = 0.0) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    def __call__(self, role: str, prompt: str) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.SYSTEMS.get(role, AUTHOR_SYSTEM)},
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
# 4. 反思循环
# --------------------------------------------------------------------------


@dataclass
class Round:
    index: int
    code: str
    verdict: str
    critique: str
    chars: int
    note: str = ""


@dataclass
class Result:
    form: str
    critic_kind: str
    status: str = "not_solved"
    stop_reason: str = ""
    rounds: List[Round] = field(default_factory=list)
    final_code: str = ""
    ground_truth: str = ""
    total_chars: int = 0


class ReflectionLoop:
    def __init__(self, llm: Callable[[str, str], str], form: str, critic_kind: str,
                 max_rounds: int = 3, char_budget: int = 20000,
                 trace: bool = False) -> None:
        self.llm = llm
        self.form = form
        self.critic_kind = critic_kind
        self.max_rounds = max_rounds
        self.char_budget = char_budget
        self.trace = trace

    def _critique(self, code: str) -> Tuple[str, str]:
        """评审：规则信号是真的跑测试；llm/judge 信号走模型。"""
        if self.critic_kind == "rule":
            ok, report = run_tests(code)
            return ("pass" if ok else "fail"), report
        role = "judge" if self.critic_kind == "judge" else "critic"
        raw = self.llm(role, build_critic_prompt(SPEC, code, role))
        return parse_verdict(raw)

    def run(self) -> Result:
        result = Result(form=self.form, critic_kind=self.critic_kind)
        memory: List[str] = []
        seen_reflections: set = set()
        seen_code: set = set()
        draft: Optional[str] = None
        feedback = ""

        for index in range(1, self.max_rounds + 1):
            prompt = build_author_prompt(self.form, SPEC, draft, feedback, memory)
            code = strip_code(self.llm("author", prompt))
            verdict, critique = self._critique(code)
            result.total_chars += len(code) + len(critique)

            note = ""
            if self.form == "reflexion":
                reflection = self.llm("reflect", build_reflect_prompt(critique))
                result.total_chars += len(reflection)
                memory.append("失败信息：%s\n反思：%s" % (critique, reflection))

            result.rounds.append(Round(index, code, verdict, critique,
                                       len(code) + len(critique), note))
            if self.trace:
                print("\n" + "=" * 72)
                print("[round %d] verdict=%s" % (index, verdict))
                print(code)
                print("-- critique --")
                print(critique)
                print("=" * 72)

            draft = code

            # ---- 四道熔断闸 ----
            if verdict == "pass":
                result.stop_reason = "critic_passed"
                break
            if code_hash(code) in seen_code:
                result.stop_reason = "no_progress"
                break
            norm = normalize_reflection(critique)
            if norm in seen_reflections:
                result.stop_reason = "repeated_reflection"
                break
            seen_code.add(code_hash(code))
            seen_reflections.add(norm)
            if index >= self.max_rounds:
                result.stop_reason = "max_rounds"
                break
            if result.total_chars > self.char_budget:
                result.stop_reason = "budget_exceeded"
                break
            feedback = critique

        result.final_code = draft or ""
        ok, report = run_tests(result.final_code)
        result.ground_truth = report
        if result.stop_reason == "critic_passed":
            result.status = "solved" if ok else "false_pass"
        else:
            result.status = "not_solved"
        return result


# --------------------------------------------------------------------------
# 5. CLI
# --------------------------------------------------------------------------


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def build_llm(args: argparse.Namespace) -> Callable[[str, str], str]:
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


def print_report(res: Result, elapsed_ms: float) -> None:
    print("=" * 72)
    print("form        :", res.form)
    print("critic      :", res.critic_kind)
    print("status      :", res.status)
    print("stop_reason :", res.stop_reason)
    print("rounds      :", len(res.rounds))
    print("chars       :", res.total_chars, " (cost proxy)")
    print("elapsed_ms  : %.1f" % elapsed_ms)
    print("-" * 72)
    for r in res.rounds:
        print("round %d | %-4s | hash=%s | %s"
              % (r.index, r.verdict, code_hash(r.code), r.note or ""))
    print("-" * 72)
    print("ground truth:", res.ground_truth)
    print("=" * 72)


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="反思与自纠错最小可实现（mock 模式无需 API Key）")
    parser.add_argument("--form", default="judge",
                        choices=["reflexion", "self_refine", "judge"],
                        help="反思形态")
    parser.add_argument("--critic", default="rule",
                        choices=["rule", "llm", "judge"],
                        help="反思信号来源：rule=真跑测试，llm=自评，judge=独立评审")
    parser.add_argument("--max-rounds", type=int, default=3, help="最大轮次")
    parser.add_argument("--char-budget", type=int, default=20000, help="成本预算（字符代理）")
    parser.add_argument("--live", action="store_true", help="切真实模型 API")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--model", default="", help="覆盖 LLM_MODEL")
    parser.add_argument("--base-url", default="", help="覆盖 LLM_BASE_URL")
    parser.add_argument("--trace", action="store_true", help="打印每轮代码与评审意见")
    args = parser.parse_args(argv)

    llm = build_llm(args)
    loop = ReflectionLoop(llm, args.form, args.critic,
                          max_rounds=max(1, args.max_rounds),
                          char_budget=args.char_budget, trace=args.trace)
    t0 = time.perf_counter()
    res = loop.run()
    print_report(res, (time.perf_counter() - t0) * 1000.0)
    return 0 if res.status == "solved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
