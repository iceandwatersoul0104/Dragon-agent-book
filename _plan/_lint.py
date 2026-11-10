# -*- coding: utf-8 -*-
"""第一卷体例 lint：抓结构缺失、禁用词、元叙述开场、术语不一致、篇幅不足。

用法：
  python _lint.py            # 报告全部问题
  python _lint.py --quiet    # 只输出问题行
"""
import re
import sys
import json
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
VOL1 = ROOT / "vol1"

SECTIONS = [
    "## 一、本章要解决的问题",
    "## 二、原理：机制拆解",
    "## 三、为什么这样设计：取舍与失效条件",
    "## 四、动手：最小可实现",
    "## 五、开源实现对照",
    "## 六、坑与检查清单",
    "## 七、小结与延伸阅读",
]

# 硬空话：一律要改
BANNED = [
    "赋能", "范式革新", "极大地提升", "具有重要意义", "革命性",
    "颠覆性", "保姆级", "无敌", "完美解决", "彻底解决", "一站式",
    "强大赋能", "质的飞跃", "全新范式",
]

# 需要看上下文才判断的词：技术语境里合法，人教版里是空话
# 注："闭环"已移除——技术语境里（控制闭环/评测闭环/反馈闭环）几乎总是正当用法，
#     真正的营销用法会被 BANNED 里的硬空话词捕获
REVIEW_WORDS = ["最强", "领先", "强大", "卓越", "海量", "极大", "彻底"]

# 「闭环」等词若处在技术语境（附近有这些词）则不算空话
TECH_CONTEXT = [
    "控制回路", "反馈", "决策", "回路", "自动", "策略", "迭代", "收敛",
    "隔离", "强度", "级别", "对比", "不构成闭环", "做了闭环", "系统",
    # 比较语境：技术写作里"最强/彻底"多用于横向比较，不是营销
    "更", "最", "相比", "比", "优于", "胜于", "差距", "排序", "排名",
    "没有", "不如", "胜过", "哪一个", "哪个",
]

# 元叙述：作者在讲这本书，而不是讲技术
META = [
    "本章将介绍", "本章将带", "接下来我们将", "在接下来的章节", "上一章我们",
    "让我们一起", "相信你已经", "值得注意的是，本节", "作为读者",
]


def cjk_count(text: str) -> int:
    body = re.sub(r"```.*?```", "", text, flags=re.S)
    body = re.sub(r"^\s*[-*]\s.*$", "", body, flags=re.M)  # 清单行另算
    return len(re.findall(r"[\u4e00-\u9fff]", body))


def prose_count(text: str) -> int:
    """正文汉字数：剔除代码块与第七节延伸阅读列表。"""
    body = re.sub(r"```.*?```", "", text, flags=re.S)
    # 去掉延伸阅读列表
    tail = body.find("## 七、小结与延伸阅读")
    if tail != -1:
        sec = body[tail:]
        body = body[:tail] + re.sub(r"https?://\S+", "", sec)
    return len(re.findall(r"[\u4e00-\u9fff]", body))


def check_file(p: Path):
    issues = []
    raw = p.read_bytes()
    text = raw.decode("utf-8")
    lines = text.splitlines()

    # 编码卫生：BOM / 乱码字符 / 行尾混用
    if raw.startswith(b"\xef\xbb\xbf"):
        issues.append(("编码", "文件带 UTF-8 BOM"))
    bad = text.count("\ufffd")
    if bad:
        i = text.find("\ufffd")
        issues.append(("编码", f"含 {bad} 个乱码字符(U+FFFD)：…{text[max(0,i-30):i+30].strip()}…"))
    if b"\r\n" in raw and b"\n" in raw.replace(b"\r\n", b""):
        issues.append(("编码", "CRLF/LF 行尾混用"))

    if not lines or not lines[0].startswith("# 第"):
        issues.append(("结构", "首行不是 '# 第 N 章 ...'"))

    for s in SECTIONS:
        if s not in text:
            issues.append(("结构", f"缺少二级标题：{s}"))

    # 标题顺序
    pos = [text.find(s) for s in SECTIONS if s in text]
    if pos != sorted(pos):
        issues.append(("结构", "七节顺序不对"))

    # 篇幅
    pc = prose_count(text)
    if pc < 2000:
        issues.append(("篇幅", f"正文仅 {pc} 汉字（<2000）"))
    if pc > 5200:
        issues.append(("篇幅", f"正文 {pc} 汉字（>5200，偏长）"))

    # 第一节开场：取 "## 一、" 到 "## 二、" 之间的前 120 字
    m1 = re.search(r"## 一、本章要解决的问题(.*?)## 二、", text, re.S)
    if m1:
        opening = re.sub(r"```.*?```", "", m1.group(1), flags=re.S).strip()
        head = opening[:120]
        # 定义式开场特征
        for pat in [r"^Agent\s*是", r"^所谓", r"^在当今", r"^随着.*的(发展|普及)", r"^本[章篇]要讲"]:
            if re.search(pat, head):
                issues.append(("开场", f"疑似定义式开场：{head[:40]}…"))
                break

    # 禁用词
    found_banned = []
    for w in BANNED:
        n = text.count(w)
        if n:
            found_banned.append(f"{w}×{n}")
    if found_banned:
        issues.append(("空话", "出现营销词：" + "、".join(found_banned)))

    # 需人工判断的词：只有脱离技术语境时才算空话
    for w in REVIEW_WORDS:
        for m in re.finditer(re.escape(w), text):
            ctx = text[max(0, m.start() - 60): m.end() + 60]
            if not any(t in ctx for t in TECH_CONTEXT):
                issues.append(("空话", f"疑似空话「{w}」：…{ctx[45:75].strip()}…"))
                break

    # 元叙述
    found_meta = [w for w in META if w in text]
    if found_meta:
        issues.append(("空话", "出现元叙述：" + "、".join(found_meta)))

    # 第六节清单格式
    m6 = re.search(r"## 六、坑与检查清单(.*?)## 七、", text, re.S)
    if m6:
        sec = m6.group(1)
        boxes = sec.count("- [ ]")
        bullets = len(re.findall(r"^\s*[-*]\s+", sec, re.M))
        if boxes == 0 and bullets > 0:
            issues.append(("清单", f"第六节 {bullets} 条全是普通列表，无可勾选项"))
        elif bullets and boxes / max(bullets, 1) < 0.7:
            issues.append(("清单", f"第六节可勾选项仅 {boxes}/{bullets}"))

    # 第三节失效条件关键词
    m3 = re.search(r"## 三、为什么这样设计：取舍与失效条件(.*?)## 四、", text, re.S)
    if m3:
        sec = m3.group(1)
        signals = [
            "不适用", "不该", "不要用", "不适合", "代价", "失效", "不要选",
            "不划算", "风险", "不需要", "什么时候不", "别用", "不要", "不能",
            "过度", "反例", "判据", "避免", "禁用", "慎重", "慎", "别把",
            "上框架是浪费", "负收益", "不推荐", "失效条件", "纯属浪费",
            "空转", "扛不住", "单点", "撑不住", "挂", "崩", "降级",
            "收益可疑", "不值得", "慎用", "别上", "白花", "反而更", "更稳",
        ]
        hits = sum(sec.count(k) for k in signals)
        has_sub = len(re.findall(r"^###\s", sec, re.M))
        if hits < 2 and has_sub == 0:
            issues.append(("取舍", f"第三节失效条件信号词仅 {hits} 处，可能写成了套话"))

    # URL 格式：只标记明显畸形的
    for u in re.findall(r"https?://[^\s，。、）)】\]]+", text):
        if re.search(r"[\u4e00-\u9fff\s]", u):
            issues.append(("事实", f"URL 含中文或空格：{u[:80]}"))
        elif not re.match(r"^https?://[\w.-]+\.[a-z]{2,}(/|$)", u):
            issues.append(("事实", f"URL 格式可疑：{u[:80]}"))

    # 章节内代码块与 code 目录一致性（文件名提示）
    return issues, pc


def main():
    files = sorted(VOL1.rglob("ch*.md"))
    if not files:
        print("没有章节文件")
        return

    # 路径一致性：落盘文件名必须是 chapters.json 规定的 {id}-{slug}.md
    names_by_part = {}
    for p in files:
        names_by_part.setdefault(p.parent.name, []).append(p.name)

    # 示例代码必须放在 code/<code_dir>/ 下，不得混进卷目录
    stray_py = [p.relative_to(VOL1) for p in VOL1.rglob("*.py")]
    if stray_py:
        print(f"[路径] 卷目录里有裸 .py 文件（应移到 code/<code_dir>/main.py）：{stray_py}")
    try:
        data = json.loads((ROOT / "_plan" / "chapters.json").read_text(encoding="utf-8"))
        chs = data["chapters"] if isinstance(data, dict) and "chapters" in data else data
        for c in chs:
            want = f"{c['id']}-{c['slug']}.md"
            actual = names_by_part.get(c["part"], [])
            if want not in actual:
                wrong = [n for n in actual if n.startswith(c["id"])]
                if wrong:
                    print(f"[路径] {c['id']}: 文件名应为 {want}，实际是 {wrong[0]} —— 需改名")
                else:
                    print(f"[路径] {c['id']}: 尚未落盘（应为 {want}）")
    except Exception as e:
        print(f"[路径] 无法校验：{e}")

    total_issues = 0
    print(f"{'章节':32} {'正文汉字':>8} {'问题':>4}")
    print("-" * 60)
    for p in files:
        issues, pc = check_file(p)
        total_issues += len(issues)
        flag = "" if not issues else "  <-- " + "; ".join(f"[{k}]{v}" for k, v in issues)
        print(f"{p.name:32} {pc:>8} {len(issues):>4}{flag}")
    print("-" * 60)
    print(f"章节文件 {len(files)} 个，问题合计 {total_issues} 条")

    # 代码目录覆盖
    code_dirs = {d.name for d in (ROOT / "code").iterdir() if d.is_dir()} if (ROOT / "code").exists() else set()
    need = set()
    data = json.loads((ROOT / "_plan" / "chapters.json").read_text(encoding="utf-8"))
    chs = data["chapters"] if isinstance(data, dict) and "chapters" in data else data
    for c in chs:
        if c.get("code") and c.get("code_dir"):
            need.add(c["code_dir"])
    missing = sorted(need - code_dirs)
    print(f"代码目录：需 {len(need)} 个，已有 {len(code_dirs)} 个，缺 {len(missing)} 个")
    if missing:
        print("  缺：" + "、".join(missing))


if __name__ == "__main__":
    main()
