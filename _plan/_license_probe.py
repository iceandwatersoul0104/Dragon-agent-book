#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用许可证核实器：查任意 GitHub 仓库的许可证，并可写回已核实清单。

网络现实（本环境实测）：
  - github.com 网页        可达
  - github.com/<repo>/raw/<ref>/<file> 可达（等价于 raw 原文）
  - raw.githubusercontent.com   被 TLS 中间设备阻断
  - api.github.com       共享出口 IP 未认证限流（60 次/小时）
  - pypi.org/pypi/<pkg>/json  可达（用于 license_expression 交叉验证）

用法：
    python _license_probe.py probe  owner/repo owner/repo ...
    python _license_probe.py import owner/repo owner/repo ...   # 核实后写回清单
    python _license_probe.py list                               # 看当前已核实清单
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PLAN = Path(__file__).resolve().parent
STORE = PLAN / "vol2-verified-refs.json"

sys.stdout.reconfigure(encoding="utf-8")

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# GitHub sidebar 会把许可证渲染成 "MIT license" / "Apache-2.0 license"
TYPE_RE = re.compile(r">\s*([A-Za-z0-9\.\-\+ ]{2,40}?)\s+license\s*<")
FILE_RE = re.compile(r'href="/[^"]+/blob/[^"]*/LICEN[SC]E[^"]*"')
# LICENSE 原文里的决定性首行
HEAD_LIC_RE = re.compile(
    r"(?i)^\s*(MIT License|Apache License|GNU AFFERO GENERAL PUBLIC LICENSE|"
    r"GNU GENERAL PUBLIC LICENSE|GNU LESSER GENERAL PUBLIC LICENSE|"
    r"Mozilla Public License|BSD \d-Clause|ISC License|The Unlicense|"
    r"Boost Software License|CC0 1\.0 Universal|"
    r"Creative Commons Zero v1\.0 Universal)")

CANON = {
    "mit": "MIT",
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "agpl-3.0": "AGPL-3.0",
    "gpl-3.0": "GPL-3.0",
    "gpl-2.0": "GPL-2.0",
    "lgpl-3.0": "LGPL-3.0",
    "mpl-2.0": "MPL-2.0",
    "isc": "ISC",
    "unlicense": "Unlicense",
    "bsl-1.0": "BSL-1.0",
    "cc0-1.0": "CC0-1.0",
}


def canon(text: str) -> str:
    t = text.strip().lower()
    if t in CANON:
        return CANON[t]
    if "apache" in t and "2.0" in t:
        return "Apache-2.0"
    if "mit" in t and "apache" in t:
        return "MIT OR Apache-2.0"
    if t.startswith("mit"):
        return "MIT"
    return text.strip()


def get(url: str, tries: int = 3) -> str | None:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 + i * 2)
        except Exception:
            time.sleep(2 + i * 2)
    return None


def probe(repo: str) -> dict:
    """返回 {repo, license, evidence, note}。拿不到就说实话，不猜。"""
    out = {"repo": repo, "license": "", "evidence": "", "note": ""}
    html = get(f"https://github.com/{repo}")
    if not html:
        out["evidence"] = "仓库页抓取失败"
        return out

    types = []
    for m in TYPE_RE.finditer(html):
        v = m.group(1).strip()
        if v and v not in types:
            types.append(v)
    if types:
        out["license"] = canon(types[0])
        out["evidence"] = f"仓库页 sidebar: '{types[0]} license'"
        return out

    # sidebar 没标（GitHub 没能识别），就自己去取 LICENSE 原文
    m = FILE_RE.search(html)
    if not m:
        out["evidence"] = "仓库页未标许可证类型，也未找到 LICENSE 文件链接"
        return out
    path = m.group(0)
    ref = re.search(r"/blob/([^/]+)/", path)
    fname = re.search(r"/(LICEN[SC]E[^/\"#]*)$", path)
    if not (ref and fname):
        out["evidence"] = "无法解析 LICENSE 路径"
        return out
    raw_url = f"https://github.com/{repo}/raw/{ref.group(1)}/{fname.group(1)}"
    text = get(raw_url)
    if text is None:
        out["evidence"] = f"取不到原文 {raw_url}"
        return out
    lines = text.splitlines()
    for ln in lines[:40]:
        s = ln.strip()
        if not s:
            continue
        hm = HEAD_LIC_RE.match(s)
        if hm:
            out["license"] = canon(hm.group(1))
            out["evidence"] = f"LICENSE 原文首行: {s[:60]}"
            break
    if not out["license"]:
        out["evidence"] = f"LICENSE 原文取到了但首行无法判定（前 3 行：{lines[:3]}）"
    # 记录附加条款（如 enterprise/ 另计、数据集另计）
    for ln in lines[:60]:
        low = ln.lower()
        if ("except for" in low or "enterprise/" in low
                or "does not apply" in low or "separately" in low):
            out["note"] = ln.strip()[:300]
            break
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "list":
        data = json.loads(STORE.read_text(encoding="utf-8"))
        for repo, item in data.get("repos", {}).items():
            print(f"{repo:<42} {item.get('license','')}")
        return 0

    repos = sys.argv[2:]
    if not repos:
        print("请给出至少一个 owner/repo")
        return 2

    results = []
    for repo in repos:
        r = probe(repo)
        results.append(r)
        mark = "OK " if r["license"] else "✗  "
        print(f"  {mark} {repo:<42} {r['license'] or '未判定'}   [{r['evidence']}]")
        if r["note"]:
            print(f"       附加条款: {r['note']}")
        time.sleep(0.8)

    if cmd == "import":
        data = json.loads(STORE.read_text(encoding="utf-8"))
        repos_map = data.setdefault("repos", {})
        for r in results:
            if not r["license"]:
                print(f"  跳过 {r['repo']}：未判定，不写入清单")
                continue
            entry = {"name": r["repo"].split("/")[-1], "license": r["license"],
                     "evidence": r["evidence"]}
            if r["note"]:
                entry["license_note"] = r["note"]
            repos_map[r["repo"]] = entry
        data["_meta"]["verified_at"] = time.strftime("%Y-%m-%d")
        STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
        print(f"\n已写回 {STORE}（共 {len(repos_map)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
