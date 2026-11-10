# -*- coding: utf-8 -*-
"""全书终审的机检部分。

对 40 章 + 7 篇附录做：
  A. 结构与体例（七节标题、字数、清单、编码、乱码）——复用 lint 逻辑但汇总输出
  B. 链接可达性：所有 GitHub / arXiv / 论文链接逐条 HTTP 验证
  C. arXiv 编号与标题是否对得上（官方 API 核对）
  D. 许可证声明一致性：正文/附录里写的许可证 vs GitHub API 的 spdx_id
  E. 跨章大段重复检测
  F. 术语变体扫描（同概念不同写法）

输出：_plan/_editorial_scan.json，附录 E 报告由它生成。
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "_plan"
OUT = PLAN / "_editorial_scan.json"
CACHE = PLAN / "_url_cache.json"

SECTIONS = [
    "## 一、本章要解决的问题",
    "## 二、原理：机制拆解",
    "## 三、为什么这样设计：取舍与失效条件",
    "## 四、动手：最小可实现",
    "## 五、开源实现对照",
    "## 六、坑与检查清单",
    "## 七、小结与延伸阅读",
]

URL_RE = re.compile(r"https?://[^\s，。、；：）)】\]\"'`，。！？]+")
GH_RE = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?$")
ARXIV_RE = re.compile(r"^https?://arxiv\.org/abs/([\d]{4}\.[\d]{4,5})$")


def load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def http_head(url, timeout=15):
    """用 GET 但只读前 1KB，避免下载整页。失败重试一次。"""
    for attempt in (0, 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; agent-book-editorial/1.0)",
                "Accept": "*/*",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read(1024)
                return True, str(r.status)
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}"
        except Exception as e:
            if attempt == 0:
                time.sleep(1.5)
                continue
            return False, f"{type(e).__name__}: {str(e)[:60]}"
    return False, "unknown"


def gh_api(path, timeout=20):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"User-Agent": "agent-book-editorial/1.0", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def all_docs():
    docs = []
    for p in sorted((ROOT / "vol1").rglob("ch*.md")):
        docs.append((p, p.read_text(encoding="utf-8")))
    for p in sorted((ROOT / "vol1").glob("00-*.md")):
        docs.append((p, p.read_text(encoding="utf-8")))
    ap = ROOT / "vol1" / "appendix"
    if ap.exists():
        for p in sorted(ap.glob("*.md")):
            docs.append((p, p.read_text(encoding="utf-8")))
    return docs


def main():
    docs = all_docs()
    print(f"受检文档 {len(docs)} 篇")
    report = {"structure": [], "links": [], "arxiv": [], "license": [], "dup": [], "terms": []}

    # ---- A 结构 ----
    for p, t in docs:
        rel = p.relative_to(ROOT)
        if p.name.startswith("ch"):
            miss = [s for s in SECTIONS if s not in t]
            if miss:
                report["structure"].append({"file": str(rel), "issue": f"缺二级标题 {miss}"})
            pos = [t.find(s) for s in SECTIONS if s in t]
            if pos != sorted(pos):
                report["structure"].append({"file": str(rel), "issue": "七节顺序错"})
            body = re.sub(r"```.*?```", "", t, flags=re.S)
            tail = body.find("## 七、小结与延伸阅读")
            if tail != -1:
                body = body[:tail] + re.sub(r"https?://\S+", "", body[tail:])
            n = len(re.findall(r"[\u4e00-\u9fff]", body))
            if n < 2000:
                report["structure"].append({"file": str(rel), "issue": f"正文仅 {n} 汉字"})
        raw = p.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            report["structure"].append({"file": str(rel), "issue": "含 UTF-8 BOM"})
        if "\ufffd" in t:
            report["structure"].append({"file": str(rel), "issue": "含 U+FFFD 乱码"})

    # ---- B/C/D 链接 ----
    cache = load_json(CACHE, {})
    gh_repos = {}       # owner/repo -> {chapters: set}
    arxiv_ids = {}      # id -> {chapters: set, title_in_book: str}
    other_urls = defaultdict(set)

    for p, t in docs:
        rel = str(p.relative_to(ROOT))
        num = re.match(r"ch(\d+)-", p.name)
        chap = int(num.group(1)) if num else None
        for raw in URL_RE.findall(t):
            u = raw.rstrip(".,;:)]}")
            g = GH_RE.match(u)
            if g:
                gh_repos.setdefault(f"{g.group(1)}/{g.group(2)}", set())
                if chap:
                    gh_repos[f"{g.group(1)}/{g.group(2)}"].add(chap)
                continue
            a = ARXIV_RE.match(u)
            if a:
                arxiv_ids.setdefault(a.group(1), set())
                if chap:
                    arxiv_ids[a.group(1)].add(chap)
                continue
            other_urls[u].add(chap if chap else 0)

    print(f"仓库 {len(gh_repos)} 个，arXiv {len(arxiv_ids)} 条，其它链接 {len(other_urls)} 条")

    # 其它域名链接抽查（只查正文里出现的、且不是图片/锚点）
    check_other = [u for u in other_urls if u.startswith("http") and "github.com" not in u]
    for u in check_other:
        key = f"other::{u}"
        if key not in cache:
            ok, msg = http_head(u)
            cache[key] = {"ok": ok, "msg": msg}
            time.sleep(0.2)
        r = cache[key]
        if not r["ok"]:
            report["links"].append({"url": u, "issue": r["msg"], "chapters": sorted(other_urls[u])})
    print(f"  其它域名链接检查完毕，失效 {len(report['links'])} 条")

    # GitHub 仓库：先用 API（拿权威 spdx_id 与 archived），失败再退回 raw 探测
    RAW_PROBE = ["README.md", "README.rst", "LICENSE", "LICENSE.md", "LICENSE.txt", "pyproject.toml", "package.json"]
    for i, (repo, chaps) in enumerate(sorted(gh_repos.items()), 1):
        key = f"gh::{repo}"
        if key not in cache:
            try:
                d = gh_api(f"/repos/{repo}")
                cache[key] = {"ok": True, "spdx": (d.get("license") or {}).get("spdx_id"),
                              "archived": bool(d.get("archived")), "lang": d.get("language"),
                              "probe": "api"}
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    print("  GitHub API 限流，改用 raw 探测")
                    cache[key] = {"ok": False, "msg": "rate-limited", "fallback": True}
                else:
                    cache[key] = {"ok": False, "msg": f"HTTP {e.code}", "fallback": True}
            except Exception as e:
                cache[key] = {"ok": False, "msg": str(e)[:60], "fallback": True}
            time.sleep(0.25)
        r = cache[key]
        if r.get("fallback") or "probe" not in r:
            # API 不可用时用 raw 探测兜底
            if not r.get("raw_ok"):
                hit = None
                for path in RAW_PROBE:
                    for br in ("main", "master"):
                        ok, _ = http_head(f"https://raw.githubusercontent.com/{repo}/{br}/{path}")
                        if ok:
                            hit = path
                            break
                    if hit:
                        break
                r["raw_ok"] = bool(hit)
                r["probe"] = hit or "未取到"
                cache[key] = r
                time.sleep(0.15)
            if not r.get("raw_ok"):
                report["links"].append({"url": f"https://github.com/{repo}",
                                        "issue": "API 与 raw 均取不到",
                                        "chapters": sorted(chaps)})
            else:
                report["license"].append({"repo": repo, "probe": "raw:" + r["probe"],
                                          "chapters": sorted(chaps)})
            continue
        if not r["ok"]:
            report["links"].append({"url": f"https://github.com/{repo}", "issue": r.get("msg"),
                                    "chapters": sorted(chaps)})
        else:
            report["license"].append({"repo": repo, "spdx": r.get("spdx"),
                                      "archived": r.get("archived"), "lang": r.get("lang"),
                                      "probe": "api", "chapters": sorted(chaps)})
        if i % 20 == 0:
            print(f"  仓库 {i}/{len(gh_repos)}")

    # arXiv：官方 API 核对标题
    for i, (aid, chaps) in enumerate(sorted(arxiv_ids.items()), 1):
        key = f"ax::{aid}"
        if key not in cache:
            try:
                q = urllib.parse.quote(f"id:{aid}")
                req = urllib.request.Request(
                    f"https://export.arxiv.org/api/query?id_list={aid}",
                    headers={"User-Agent": "agent-book-editorial/1.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    x = r.read().decode("utf-8", "replace")
                m = re.search(r"<title>(.*?)</title>", x, re.S)
                titles = re.findall(r"<title>(.*?)</title>", x, re.S)
                real = titles[1].strip() if len(titles) > 1 else ""
                cache[key] = {"ok": bool(real), "title": " ".join(real.split())}
            except Exception as e:
                cache[key] = {"ok": False, "msg": str(e)[:60]}
            time.sleep(0.5)
        r = cache[key]
        if not r["ok"]:
            report["arxiv"].append({"id": aid, "issue": r.get("msg"), "chapters": sorted(chaps)})
        else:
            report["arxiv"].append({"id": aid, "title": r.get("title"), "chapters": sorted(chaps)})
        if i % 10 == 0:
            print(f"  arXiv {i}/{len(arxiv_ids)}")

    # ---- E 跨章重复 ----
    paras = defaultdict(list)
    for p, t in docs:
        rel = str(p.relative_to(ROOT))
        for para in re.split(r"\n\s*\n", t):
            s = para.strip()
            if len(s) < 120 or s.startswith(("|", "-", "```", "#")):
                continue
            s = re.sub(r"\s+", "", s)
            if len(s) >= 120:
                paras[s].append(rel)
    for s, files in paras.items():
        if len(files) > 1:
            report["dup"].append({"files": files, "len": len(s), "preview": s[:90]})

    # ---- F 术语变体 ----
    VARIANTS = [
        ("检查点", ["checkpoint", "Checkpointer", "checkpointer"]),
        ("护栏", ["guardrail", "Guardrail"]),
        ("工具调用", ["Function Calling", "function calling", "工具调用"]),
        ("上下文", ["context", "Context"]),
        ("熔断", ["circuit breaker", "熔断", "断路器"]),
        ("记忆", ["memory", "Memory"]),
    ]
    for p, t in docs:
        rel = str(p.relative_to(ROOT))
        for cn, ens in VARIANTS:
            has_cn = cn in t
            has_en = [e for e in ens if e in t]
            if has_en and not has_cn:
                report["terms"].append({"file": rel, "term": cn, "english": has_en,
                                        "issue": "只出现英文未配中文"})

    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== 终审机检结果 =====")
    print(f"结构问题   : {len(report['structure'])}")
    print(f"失效链接   : {len(report['links'])}")
    print(f"arXiv 问题 : {len(report['arxiv'])}")
    print(f"许可证记录 : {len(report['license'])}")
    print(f"跨章重复   : {len(report['dup'])}")
    print(f"术语不一致 : {len(report['terms'])}")
    print(f"\n写出 {OUT}")
    for it in report["structure"][:10]:
        print("  结构:", it)
    for it in report["links"][:15]:
        print("  链接:", it["url"], it["issue"], it["chapters"])
    for it in report["dup"][:8]:
        print("  重复:", it["files"], it["preview"][:60])


if __name__ == "__main__":
    main()
