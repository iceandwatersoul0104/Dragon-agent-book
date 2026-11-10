#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第二卷正文 × 第一卷已核实清单 的交叉核对。

第一卷的 59 条项目（URL + 许可证）已逐条联网核实，见 volume-refs.json。
第二卷正文由 Bot 独立编写，理论上可能：

  1. 把许可证写错（MIT 写成 Apache-2.0 之类）；
  2. 把 owner/repo 拼错（URL 指不到实际仓库）；
  3. 引入第一卷清单里没有的新项目（许可证是否属实需另行核实）。

本脚本做机器能做的部分：抓正文里所有 markdown 链接，与清单双向比对，
把「不一致」和「清单外新增」分别列出来，交给人工/联网核实。

只读，不改任何文件。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "_plan"
VOL2 = ROOT / "vol2"

sys.stdout.reconfigure(encoding="utf-8")

# SPDX 核心 token 的归一化顺序（先长后短，避免 MIT 匹配到 MIT-0）
SPDX_ORDER = [
    "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "MPL-2.0", "LGPL-3.0",
    "AGPL-3.0", "GPL-3.0", "GPL-2.0", "MIT-0", "CC-BY-4.0", "CC-BY-SA-4.0",
    "Elastic-2.0", "ELv2", "Unlicense", "WTFPL", "BSL-1.0", "ISC", "MIT",
]

LINK_RE = re.compile(
    r"<(?P<auto>https?://[^>\s]+)>"
    r"|\[(?P<label>[^\]]*)\]\((?P<md>https?://[^)\s]+)\)"
)
# 许可证可能出现在链接后的括号里，如 `>（MIT）` 或 `（Apache-2.0）`
LIC_RE = re.compile(r"[（(]([A-Za-z0-9\-\. ]{2,40})[）)]")


def spdx_set(text: str) -> frozenset[str]:
    """从一段许可证描述里抽出**全部**核心 SPDX token。

    必须用集合而不是单个 token：「MIT OR Apache-2.0」若只取一个，
    就会把「正文写 MIT、清单写双许可」误判成不一致。
    token 边界用 (?<![A-Za-z0-9\\-]) / (?![A-Za-z0-9\\-])，
    这样 MIT-0 不会被 MIT 误命中。
    """
    t = text.lower()
    hits = set()
    for tok in SPDX_ORDER:
        pat = r"(?<![a-z0-9\-])" + re.escape(tok.lower()) + r"(?![a-z0-9\-])"
        if re.search(pat, t):
            hits.add(tok)
    return frozenset(hits)


def spdx(text: str) -> str:
    """兼容旧调用：返回单个代表 token（取 SPDX_ORDER 里最靠前的）。"""
    s = spdx_set(text)
    for tok in SPDX_ORDER:
        if tok in s:
            return tok
    return ""


def norm_repo(url: str) -> str:
    """从 GitHub URL 里取出 owner/repo，转小写；非 GitHub 返回空。"""
    m = re.match(r"https?://github\.com/([^/\s]+)/([^/\s#?]+)", url)
    if not m:
        return ""
    owner, repo = m.group(1), m.group(2)
    repo = re.sub(r"\.git$", "", repo)
    return f"{owner.lower()}/{repo.lower()}"


def load_refs() -> dict[str, dict]:
    """汇总核对源：附录 B（逐条联网核实的速查表） + volume-refs.json。

    附录 B 是第一卷的最终成果：60 个项目的仓库 URL 与许可证都取自
    LICENSE 原文或 GitHub API 的 spdx_id，优先级最高。volume-refs.json
    里的分卷参考资料作为补充。
    """
    table: dict[str, dict] = {}

    # --- 来源 1：附录 B 速查表 ---
    appx = ROOT / "vol1" / "appendix" / "B-开源项目速查表.md"
    if appx.exists():
        row = re.compile(
            r"\[\s*([^\]\s]+)\s*\]\((https?://github\.com/[^)\s]+)\)\s*"
            r"\|\s*([^|]*?)\s*\|"
        )
        for line in appx.read_text(encoding="utf-8").splitlines():
            if not line.startswith("|"):
                continue
            m = row.search(line)
            if not m:
                continue
            label, url, lic = m.group(1), m.group(2), m.group(3)
            repo = norm_repo(url)
            if repo:
                table[repo] = {
                    "name": label,
                    "url": url,
                    "license": lic,
                    "spdx": spdx_set(lic),
                    "part": "appendix-B",
                }

    # --- 来源 2：第二卷专用的已核实清单（2026-11-08 逐个联网核实） ---
    v2 = PLAN / "vol2-verified-refs.json"
    if v2.exists():
        data = json.loads(v2.read_text(encoding="utf-8"))
        for repo, item in data.get("repos", {}).items():
            table[repo.lower()] = {
                "name": item.get("name", repo),
                "url": f"https://github.com/{repo}",
                "license": item.get("license", ""),
                "spdx": spdx_set(item.get("license", "")),
                "part": "vol2-verified",
            }

    # --- 来源 3：分卷参考资料（补清单没收录的） ---
    raw = json.loads((PLAN / "volume-refs.json").read_text(encoding="utf-8"))
    for part, block in raw.items():
        for line in block.get("refs", []):
            parts = [x.strip() for x in line.split("|")]
            if len(parts) < 3:
                continue
            name, url, lic = parts[0], parts[1], parts[2]
            repo = norm_repo(url)
            if not repo or repo in table:
                continue
            table[repo] = {
                "name": name,
                "url": url,
                "license": lic,
                "spdx": spdx_set(lic),
                "part": part,
            }
    return table


def main() -> int:
    refs = load_refs()
    print(f"已核实清单条目：{len(refs)} 个 GitHub 仓库\n")

    md_files = sorted(VOL2.glob("p*/**/*.md"))
    seen_repo: dict[str, list[str]] = {}   # repo -> 出现该 URL 的文件
    seen_license: dict[str, set] = {}     # repo -> 该处标注的许可证 token 集合
    extra_urls: dict[str, list[str]] = {}  # 清单外的 GitHub URL

    for f in md_files:
        text = f.read_text(encoding="utf-8")
        rel = f.relative_to(ROOT).as_posix()
        lines = text.splitlines()
        for i, line in enumerate(lines):
            for m in LINK_RE.finditer(line):
                url = m.group("auto") or m.group("md")
                label = m.group("label") or ""
                repo = norm_repo(url)
                if not repo:
                    continue
                # 许可证候选 = 本行 url 之后的括号 + 「同一条目」的后续行。
                # 「同一条目」= 往后最多 2 行，且中途不出现**别的**仓库的 URL。
                window = line[m.end():]
                for j in (i + 1, i + 2):
                    if j >= len(lines):
                        break
                    nxt = lines[j]
                    others = {norm_repo(x.group("auto") or x.group("md"))
                              for x in LINK_RE.finditer(nxt)}
                    others.discard(repo)
                    if others:
                        break
                    window += " " + nxt
                cand = label + " " + window
                tok = spdx_set(cand)
                seen_repo.setdefault(repo, []).append(rel)
                seen_license.setdefault(repo, set()).update(tok)
                if repo not in refs:
                    extra_urls.setdefault(repo, []).append(rel)

    bad_license: list[str] = []
    wrong_url: list[str] = []
    missing_license: list[str] = []

    for repo, files in sorted(seen_repo.items()):
        ref = refs.get(repo)
        if ref is None:
            continue
        toks = {t for t in seen_license[repo] if t}
        ref_set = set(ref["spdx"])
        if not toks:
            missing_license.append(
                f"{repo}（{ref['name']}）: 正文未标注许可证，清单为 "
                f"{sorted(ref_set) or ref['license']}")
            continue
        # 正文声明的 token 必须是清单 token 的子集：
        #   清单单许可 → 正文必须正好是它；
        #   清单双许可 → 正文写其中之一或全部都算对。
        if ref_set:
            if toks <= ref_set:
                pass
            elif ref_set <= toks:
                pass
            else:
                bad_license.append(
                    f"{repo}（{ref['name']}）: 正文写 {sorted(toks)}，清单为 {sorted(ref_set)}"
                    f"（原表述：{ref['license']}）")
        # URL 拼写：正文用的 repo 与清单一致才走到这里；再比对「同一 name 的清单 URL」
        for other_repo, other in refs.items():
            if other_repo != repo and other["name"] == ref["name"]:
                wrong_url.append(f"清单里 {ref['name']} 同时存在 {repo} 与 {other_repo}")

    print(f"正文共扫到 GitHub 链接 {sum(len(v) for v in seen_repo.values())} 处，"
          f"去重仓库 {len(seen_repo)} 个\n")

    print("=== 1. 许可证与清单不一致 ===")
    if bad_license:
        for x in bad_license:
            print("  ✗", x)
    else:
        print("  （无）")

    print("\n=== 2. 正文未标注许可证 ===")
    if missing_license:
        for x in sorted(set(missing_license)):
            print("  ?", x)
    else:
        print("  （无）")

    print("\n=== 3. 清单外的 GitHub 项目（许可证需另行核实） ===")
    if extra_urls:
        for repo, files in sorted(extra_urls.items()):
            print(f"  · {repo}  ← {', '.join(sorted(set(files)))}")
    else:
        print("  （无）")

    print("\n=== 4. 清单内同名多 URL（需人工判断） ===")
    if wrong_url:
        for x in sorted(set(wrong_url)):
            print("  !", x)
    else:
        print("  （无）")

    problems = len(bad_license) + len(wrong_url)
    print(f"\n共检出 {problems} 处需处理的不一致；"
          f"{len(extra_urls)} 个清单外项目待核实许可证。")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
