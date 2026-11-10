# -*- coding: utf-8 -*-
"""第二卷验收闸门：正文体例 + dragon-agent 工程完整性 + 测试实跑。

用法：
  python _lint_vol2.py          # 全量检查（会实跑测试）
  python _lint_vol2.py --no-test # 跳过测试实跑
"""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
VOL2 = ROOT / "vol2"
DRAGON = ROOT / "dragon-agent"
PLAN = ROOT / "_plan"

SECTIONS = [
    "## 一、本章要解决的问题",
    "## 二、原理：机制拆解",
    "## 三、为什么这样设计：取舍与失效条件",
    "## 四、动手：最小可实现",
    "## 五、开源实现对照",
    "## 六、坑与检查清单",
    "## 七、小结与延伸阅读",
]

# 内容质量启发式 —— 判据与第一卷 _lint.py 保持一致，避免两卷两套标准。
# 硬空话：一律要改
BANNED = [
    "赋能", "范式革新", "极大地提升", "具有重要意义", "革命性",
    "颠覆性", "保姆级", "无敌", "完美解决", "彻底解决", "一站式",
    "强大赋能", "质的飞跃", "全新范式",
]

# 元叙述：作者在讲这本书，而不是讲技术
META = [
    "本章将介绍", "本章将带", "接下来我们将", "在接下来的章节", "上一章我们",
    "让我们一起", "相信你已经", "值得注意的是，本节", "作为读者",
]

# 第三节的"什么时候不该用"信号词。命中 <2 处且没有子标题，多半写成了套话。
LOSS_SIGNALS = [
    "不适用", "不该", "不要用", "不适合", "代价", "失效", "不要选",
    "不划算", "风险", "不需要", "什么时候不", "别用", "不要", "不能",
    "过度", "反例", "判据", "避免", "禁用", "慎重", "慎", "别把",
    "负收益", "不推荐", "失效条件", "纯属浪费", "空转", "扛不住",
    "撑不住", "挂", "崩", "降级", "收益可疑", "不值得", "慎用", "别上",
    "白花", "反而更", "更稳", "不值得上", "是浪费",
]

MODULES = [
    "config.py", "llm.py", "tools.py", "context.py", "agent.py", "state.py",
    "memory.py", "guardrails.py", "orchestration.py", "mcp_server.py",
    "observability.py", "evaluation.py", "cli.py",
]


def prose_count(text: str) -> int:
    body = re.sub(r"```.*?```", "", text, flags=re.S)
    tail = body.find("## 七、小结与延伸阅读")
    if tail != -1:
        body = body[:tail] + re.sub(r"https?://\S+", "", body[tail:])
    return len(re.findall(r"[\u4e00-\u9fff]", body))


def main():
    run_tests = "--no-test" not in sys.argv
    problems = []

    # ---------------------------------------------------------- 规格表
    spec = json.loads((PLAN / "chapters-vol2.json").read_text(encoding="utf-8"))
    chapters = spec["chapters"]

    # ---------------------------------------------------------- 正文
    print("=== 第二卷正文 ===")
    got = 0
    for c in chapters:
        p = VOL2 / c["code_dir"] / f"{c['slug']}.md"
        if not p.exists():
            problems.append(f"[正文] 未落盘：{c['id']} {c['slug']}.md")
            continue
        got += 1
        t = p.read_text(encoding="utf-8")
        miss = [s for s in SECTIONS if s not in t]
        if miss:
            problems.append(f"[正文] {c['id']} 缺二级标题 {miss}")
        pos = [t.find(s) for s in SECTIONS if s in t]
        if pos != sorted(pos):
            problems.append(f"[正文] {c['id']} 七节顺序错")
        # 严格体例：正文里的二级标题必须**恰好**是那七个，多一个都不行。
        # （只查"缺"会漏掉插入的额外 ## 段，例如把子节提成二级。）
        h2 = [ln.strip() for ln in t.splitlines()
              if ln.startswith("## ") and not ln.startswith("### ")]
        if h2 != SECTIONS:
            extra = [h for h in h2 if h not in SECTIONS]
            dup = [h for h in set(h2) if h2.count(h) > 1]
            if extra:
                problems.append(f"[正文] {c['id']} 多余二级标题：{extra}")
            if dup:
                problems.append(f"[正文] {c['id']} 二级标题重复：{dup}")
            elif not extra and h2 != SECTIONS:
                problems.append(f"[正文] {c['id']} 二级标题集合/顺序不符：{h2}")
        if not t.splitlines() or not t.splitlines()[0].startswith("# 第"):
            problems.append(f"[正文] {c['id']} 首行不是 '# 第 N 章 ...'")
        n = prose_count(t)
        if n < 2000:
            problems.append(f"[正文] {c['id']} 正文仅 {n} 汉字（<2000）")
        raw = p.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            problems.append(f"[正文] {c['id']} 含 BOM")
        if "\ufffd" in t:
            problems.append(f"[正文] {c['id']} 含乱码字符")
        readme = p.parent / "README.md"
        if not readme.exists():
            problems.append(f"[正文] {c['id']} 缺本章 README.md")
        else:
            # 代码契约要求 README 说明依赖、运行命令、环境变量与**局限**
            # （明确写出「这不等于可以上线」的地方）。缺「局限」小节要报。
            rt = readme.read_text(encoding="utf-8")
            if not re.search(r"局限|不等于可以上线|不能上线|边界与限制", rt):
                problems.append(f"[README] {c['id']} 本章 README 缺「局限」小节")
            if not re.search(r"怎么跑|如何运行|运行方式|怎么执行|python ", rt):
                problems.append(f"[README] {c['id']} 本章 README 缺运行方式说明")

        # ---- 内容质量启发式（判据与第一卷 _lint.py 保持一致）----
        # 1) 第一节必须是场景或反例开场，禁止定义式
        m1 = re.search(r"## 一、本章要解决的问题(.*?)## 二、", t, re.S)
        if m1:
            opening = re.sub(r"```.*?```", "", m1.group(1), flags=re.S).strip()
            head = opening[:120]
            for pat in [r"^Agent\s*是", r"^所谓", r"^在当今", r"^随着.*的(发展|普及)",
                        r"^本[章篇]要讲", r"^.*?是一种", r"^.*?是指"]:
                if re.search(pat, head):
                    problems.append(f"[开场] {c['id']} 疑似定义式开场：{head[:40]}…")
                    break

        # 2) 硬空话：一律要改
        found_banned = [f"{w}×{t.count(w)}" for w in BANNED if t.count(w)]
        if found_banned:
            problems.append(f"[空话] {c['id']} 出现营销词：{'、'.join(found_banned)}")

        # 3) 元叙述：作者在讲这本书，而不是讲技术
        found_meta = [w for w in META if w in t]
        if found_meta:
            problems.append(f"[空话] {c['id']} 出现元叙述：{'、'.join(found_meta)}")

        # 4) 第六节要写成可勾选的清单
        m6 = re.search(r"## 六、坑与检查清单(.*?)## 七、", t, re.S)
        if m6:
            sec = m6.group(1)
            boxes = sec.count("- [ ]")
            bullets = len(re.findall(r"^\s*[-*]\s+", sec, re.M))
            if boxes == 0 and bullets > 0:
                problems.append(f"[清单] {c['id']} 第六节 {bullets} 条全是普通列表，无可勾选项")
            elif bullets and boxes / max(bullets, 1) < 0.7:
                problems.append(f"[清单] {c['id']} 第六节可勾选项仅 {boxes}/{bullets}")

        # 5) 第三节必须讲清"什么时候不该用"——这是全书价值所在
        m3 = re.search(r"## 三、为什么这样设计：取舍与失效条件(.*?)## 四、", t, re.S)
        if m3:
            sec = m3.group(1)
            hits = sum(sec.count(k) for k in LOSS_SIGNALS)
            has_sub = len(re.findall(r"^###\s", sec, re.M))
            if hits < 2 and has_sub == 0:
                problems.append(f"[取舍] {c['id']} 第三节失效条件信号词仅 {hits} 处，可能写成了套话")

        # 6) URL 畸形
        for u in re.findall(r"https?://[^\s，。、）)】\]]+", t):
            if re.search(r"[\u4e00-\u9fff\s]", u):
                problems.append(f"[事实] {c['id']} URL 含中文或空格：{u[:80]}")
                break

        print(f"  {c['id']} {c['slug']:<22} {n:>5} 汉字  {'OK' if not miss else 'MISSING'}")
    print(f"  正文 {got}/{len(chapters)}")

    # ---------------------------------------------------------- 工程
    print("\n=== dragon-agent 工程 ===")
    pkg = DRAGON / "dragon_agent"
    if not pkg.is_dir():
        problems.append("[工程] dragon_agent/ 目录不存在")
    else:
        for m in MODULES:
            f = pkg / m
            if not f.exists():
                problems.append(f"[工程] 缺模块 {m}")
            else:
                print(f"  模块 {m:<20} {f.stat().st_size:>6} B")
        init = pkg / "__init__.py"
        if not init.exists():
            problems.append("[工程] 缺 dragon_agent/__init__.py")

    for req, label in [(DRAGON / "README.md", "根 README.md"),
                       (DRAGON / "pyproject.toml", "pyproject.toml"),
                       (DRAGON / "tests", "tests/ 目录")]:
        if not req.exists():
            problems.append(f"[工程] 缺 {label}")
        else:
            print(f"  {label}  OK")

    if (DRAGON / "tests").is_dir():
        t_files = sorted(p.name for p in (DRAGON / "tests").glob("test_*.py"))
        print(f"  测试文件 {len(t_files)}: {', '.join(t_files) if t_files else '无'}")

    # ---------------------------------------------------------- 实跑测试
    if run_tests and (DRAGON / "dragon_agent").is_dir():
        print("\n=== 实跑测试 ===")
        try:
            r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                               cwd=str(DRAGON), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300)
            # unittest 的 TextTestRunner 默认把结果写到 **stderr**，不是 stdout。
            # 只取 stdout 会得到空输出，看不到"跑了多少条、skip 几条"。
            out = (r.stdout or "") + (r.stderr or "")
            tail = out[-2500:]
            print(tail[-1200:] if tail else "(无输出)")
            if r.returncode != 0:
                problems.append(f"[测试] 退出码 {r.returncode}，有测试未通过")
                err = (r.stderr or "")[-1200:]
                if err.strip():
                    print("  STDERR:", err[-600:])
        except subprocess.TimeoutExpired:
            problems.append("[测试] 超时（>300s）")
        except Exception as e:
            problems.append(f"[测试] 无法执行：{e}")

    # ---------------------------------------------------------- 汇总
    print("\n" + "=" * 62)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print("  " + p)
    else:
        print("全部通过")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
