#!/usr/bin/env python3
"""ch37 代码智能体与仓库级自治 —— 最小可实现参考实现。

一个能真正演示"仓库级代码智能体"最小回路的实现：

    1. 仓库索引：扫描代码文件，按扩展名过滤，跳过二进制与大文件；
       用正则抽取符号（def/class/function/fn/type/method），统计引用热度。
    2. 仓库地图：把"文件 -> 符号签名"压成一张按引用热度排序的地图，
       只放签名不放实现体，用 --map-lines 控制预算。
    3. 动作协议：模型每轮只输出一个 JSON 动作，执行器只认白名单：
       list_files / search / read_file / read_symbols / apply_patch / run_tests / done。
    4. 补丁机制：SEARCH/REPLACE 精确替换，锚点找不到或不唯一都返回失败，
       让模型读回真实内容再重试（降级链）。
    5. 验证闸门：测试不过不许 done；收工时再跑一次测试做最终确认。
    6. mock 模型：动作序列由仓库里的 .code-agent-task.json 驱动，
       用来在没有 API Key 的情况下离线验证脚手架本身是否通。
       --live 换成真实模型，脚本、工具、闸门完全不变。

依赖：仅 Python 标准库（>= 3.8）。默认 mock 模式不需要任何 API Key。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass


HERE = Path(__file__).resolve().parent
DEFAULT_REPO = HERE / "demo_repo"
TASK_FILE_NAME = ".code-agent-task.json"

CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".kt", ".kts", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".scala", ".sh", ".bash", ".sql", ".vue", ".svelte",
}
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "out", "target", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".idea", ".vscode", ".gradle", "vendor", "site-packages",
}
MAX_FILE_BYTES = 400 * 1024
MAX_LINE_CHARS = 240

KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "else", "elif", "do",
    "try", "with", "match", "case", "func", "fn", "def", "class", "new",
    "assert", "await", "async", "typeof", "sizeof", "throw", "print",
}

SYMBOL_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"^([ \t]*)((?:async[ \t]+)?def[ \t]+(\w+)[ \t]*(\([^()]*\)))", re.M), "def"),
    (re.compile(r"^([ \t]*)(class[ \t]+(\w+)[ \t]*(\([^()]*\))?)", re.M), "class"),
    (re.compile(r"^([ \t]*)(?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function[ \t]+(\w+)[ \t]*(\([^()]*\))", re.M), "function"),
    (re.compile(r"^([ \t]*)(?:export[ \t]+)?(?:const|let|var)[ \t]+(\w+)[ \t]*=[ \t]*(?:async[ \t]*)?\(([^()]*)\)", re.M), "const"),
    (re.compile(r"^([ \t]*)(?:export[ \t]+)?(?:const|let|var)[ \t]+(\w+)[ \t]*=[ \t]*(?:async[ \t]*)?(?:function\b|\([^()]*\)[ \t]*=>)"), "const"),
    (re.compile(r"^(func[ \t]+(?:[^\n(]*[ \t])?(\w+)[ \t]*(\([^()]*\)))", re.M), "func"),
    (re.compile(r"^(type[ \t]+(\w+)[ \t]+(?:struct|interface)\b)"), "type"),
    (re.compile(r"^([ \t]*)(?:pub[ \t]+)?(?:async[ \t]+)?(?:unsafe[ \t]+)?fn[ \t]+(\w+)[ \t]*(\([^()]*\))", re.M), "fn"),
    (re.compile(r"^([ \t]*)(?:pub[ \t]+)?(?:struct|enum|trait|impl)[ \t]+(\w+)"), "type"),
    (re.compile(r"^([ \t]*)(?:pub[ \t]+)?(?:const|static)[ \t]+(\w+)[ \t]*:"), "const"),
    (re.compile(r"^([ \t]*)(?:public|private|protected|internal|static|final|open|override|suspend|\s)*[\w<>\[\],.?]+[ \t]+(\w+)[ \t]*\([^;{]*\)[ \t]*\{", re.M), "method"),
]


@dataclass
class Symbol:
    name: str
    kind: str
    line: int
    signature: str


@dataclass
class Hit:
    path: str
    line: int
    text: str


@dataclass
class TaskSpec:
    goal: str
    keyword: str
    file: str
    find: str
    replace: str
    symbol: str
    source: str

    @property
    def has_edit(self) -> bool:
        return bool(self.file and self.find)


# ---------------------------------------------------------------------------
# 仓库索引：扫描 / 符号抽取 / 引用热度 / 仓库地图
# ---------------------------------------------------------------------------


class RepoIndex:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.files: List[str] = []
        self.symbols: Dict[str, List[Symbol]] = {}
        self.refs: Dict[str, int] = {}
        self._text_cache: Dict[str, str] = {}

    # -- 扫描 -------------------------------------------------------------
    def scan(self) -> None:
        self.files.clear()
        self.symbols.clear()
        self.refs.clear()
        self._text_cache.clear()
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                path = Path(dirpath) / name
                if path.suffix.lower() not in CODE_EXT:
                    continue
                try:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                rel = path.relative_to(self.root).as_posix()
                self.files.append(rel)
        for rel in self.files:
            text = self.read_text(rel)
            if text is None:
                continue
            self.symbols[rel] = extract_symbols(text)
        self._count_refs()

    def _count_refs(self) -> None:
        counts: Dict[str, int] = {}
        for rel in self.files:
            text = self.read_text(rel) or ""
            seen = set()
            for sym in self.symbols.get(rel, []):
                if sym.name in seen:
                    continue
                seen.add(sym.name)
                pattern = re.compile(r"\b" + re.escape(sym.name) + r"\b")
                hits = len(pattern.findall(text))
                counts[sym.name] = counts.get(sym.name, 0) + hits
        self.refs = counts

    # -- 读取 -------------------------------------------------------------
    def read_text(self, rel: str) -> Optional[str]:
        if rel in self._text_cache:
            return self._text_cache[rel]
        path = self.root / rel
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in data[:4096]:
            return None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("gbk")
            except UnicodeDecodeError:
                return None
        self._text_cache[rel] = text
        return text

    def abs_path(self, rel: str) -> Path:
        return self.root / rel

    def write_text(self, rel: str, text: str) -> None:
        path = self.abs_path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._text_cache[rel] = text

    # -- 检索 -------------------------------------------------------------
    def search(self, query: str, limit: int = 40) -> List[Hit]:
        if not query:
            return []
        needle = query.lower()
        hits: List[Hit] = []
        for rel in self.files:
            text = self.read_text(rel) or ""
            for lineno, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    hits.append(Hit(rel, lineno, line.strip()[:MAX_LINE_CHARS]))
                    if len(hits) >= limit:
                        return hits
        return hits

    def file_score(self, rel: str) -> int:
        return sum(min(self.refs.get(s.name, 0), 5) for s in self.symbols.get(rel, []))

    def repo_map(self, max_lines: int = 200) -> str:
        ranked = sorted(self.files, key=lambda r: (-self.file_score(r), r))
        out: List[str] = ["# 仓库地图（只含符号签名，按引用热度排序）"]
        used = 1
        for rel in ranked:
            syms = self.symbols.get(rel) or []
            if not syms:
                continue
            out.append(f"{rel}")
            used += 1
            for sym in syms:
                if used >= max_lines:
                    out.append("...（地图已按预算截断）")
                    return "\n".join(out)
                out.append(f"  {sym.signature}    # 引用 {min(self.refs.get(sym.name, 0), 99)}")
                used += 1
        return "\n".join(out)


def extract_symbols(text: str) -> List[Symbol]:
    symbols: List[Symbol] = []
    seen: Dict[Tuple[str, int], bool] = {}
    for pattern, kind in SYMBOL_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groups()
            name = None
            signature = match.group(0).strip()
            for g in groups:
                if g and g.isidentifier() and g not in KEYWORDS:
                    name = g
                    break
            if not name or name in KEYWORDS:
                continue
            line = text.count("\n", 0, match.start()) + 1
            key = (name, line)
            if key in seen:
                continue
            seen[key] = True
            sig = " ".join(signature.split())[:120]
            symbols.append(Symbol(name=name, kind=kind, line=line, signature=sig))
    symbols.sort(key=lambda s: s.line)
    return symbols


# ---------------------------------------------------------------------------
# 补丁：SEARCH / REPLACE 精确替换
# ---------------------------------------------------------------------------

PATCH_BEGIN = "<<<<<<< SEARCH"
PATCH_MID = "======="
PATCH_END = ">>>>>>> REPLACE"


def parse_patch(patch: str) -> List[Tuple[str, str]]:
    blocks: List[Tuple[str, str]] = []
    old: List[str] = []
    new: List[str] = []
    state = 0  # 0=无, 1=SEARCH 段, 2=REPLACE 段
    for raw in patch.splitlines():
        marker = raw.strip()
        if marker == PATCH_BEGIN:
            if state != 0:
                raise ValueError("补丁里出现了嵌套的 SEARCH 块")
            old, new, state = [], [], 1
        elif marker == PATCH_MID:
            if state != 1:
                raise ValueError("补丁里出现没有 SEARCH 头的 =======")
            state = 2
        elif marker == PATCH_END:
            if state != 2:
                raise ValueError("补丁里出现没有 ======= 的 >>>>>>> REPLACE")
            blocks.append(("\n".join(old), "\n".join(new)))
            old, new, state = [], [], 0
        else:
            if state == 1:
                old.append(raw)
            elif state == 2:
                new.append(raw)
    if state != 0:
        raise ValueError("补丁块没有闭合，缺少 ======= 或 >>>>>>> REPLACE")
    if not blocks:
        raise ValueError("补丁里没有 SEARCH/REPLACE 块")
    return blocks


def apply_blocks(text: str, blocks: List[Tuple[str, str]]) -> Tuple[str, List[str]]:
    errors: List[str] = []
    for old, new in blocks:
        if not old.strip():
            errors.append("待替换片段为空")
            continue
        count = text.count(old)
        if count == 0:
            errors.append("未找到待替换片段（锚点不匹配，先读回真实内容再重试）")
            continue
        if count > 1:
            errors.append(f"待替换片段不唯一（出现 {count} 次），请扩大上下文")
            continue
        text = text.replace(old, new, 1)
    return text, errors


# ---------------------------------------------------------------------------
# 工具执行器
# ---------------------------------------------------------------------------


class Toolbox:
    def __init__(self, index: RepoIndex, test_cmd: Optional[str]) -> None:
        self.index = index
        self.test_cmd = test_cmd or autodetect_test_cmd(index)
        self.patch_count = 0

    def dispatch(self, action: Dict[str, Any]) -> Tuple[str, str]:
        """返回 (工具名, 观察文本)。"""
        name = str(action.get("action", ""))
        try:
            if name == "list_files":
                return name, self.list_files()
            if name == "search":
                return name, self.search(action)
            if name == "read_file":
                return name, self.read_file(action)
            if name == "read_symbols":
                return name, self.read_symbols(action)
            if name == "apply_patch":
                return name, self.apply_patch(action)
            if name == "run_tests":
                return name, self.run_tests()
        except Exception as exc:  # 工具异常也作为观察回灌，不中断循环
            return name, f"错误：工具执行异常 {type(exc).__name__}: {exc}"
        return name, f"错误：未知动作 {name!r}，只允许 list_files/search/read_file/read_symbols/apply_patch/run_tests/done"

    def list_files(self) -> str:
        if not self.index.files:
            return "（仓库里没有可索引的代码文件）"
        lines = [f"{rel}" for rel in self.index.files[:200]]
        if len(self.index.files) > 200:
            lines.append(f"...（共 {len(self.index.files)} 个文件，只列前 200 个）")
        return "\n".join(lines)

    def search(self, action: Dict[str, Any]) -> str:
        query = str(action.get("query", ""))
        hits = self.index.search(query)
        if not hits:
            return f"（没有匹配 {query!r} 的行）"
        out = [f"命中 {len(hits)} 行："]
        for hit in hits:
            out.append(f"{hit.path}:{hit.line}: {hit.text}")
        return "\n".join(out)

    def read_file(self, action: Dict[str, Any]) -> str:
        rel = str(action.get("path", ""))
        text = self.index.read_text(rel)
        if text is None:
            return f"错误：读不到文件 {rel!r}"
        start = max(1, int(action.get("start", 1) or 1))
        end = int(action.get("end", 400) or 400)
        lines = text.splitlines()
        end = min(end, len(lines))
        if start > end:
            return f"错误：行号范围非法（start={start} end={end}，文件共 {len(lines)} 行）"
        out = [f"--- {rel}（第 {start}-{end} 行，共 {len(lines)} 行）---"]
        for lineno in range(start, end + 1):
            out.append(f"{lineno:>5}|{lines[lineno - 1]}")
        return "\n".join(out)

    def read_symbols(self, action: Dict[str, Any]) -> str:
        rel = str(action.get("path", ""))
        syms = self.index.symbols.get(rel)
        if not syms:
            return f"错误：{rel!r} 没有抽到符号（可能不是代码文件，或语法超出正则覆盖范围）"
        out = [f"{rel} 的符号："]
        for sym in syms:
            out.append(f"{sym.line:>5}  {sym.kind} {sym.signature}")
        return "\n".join(out)

    def apply_patch(self, action: Dict[str, Any]) -> str:
        rel = str(action.get("path", ""))
        patch = str(action.get("patch", ""))
        text = self.index.read_text(rel)
        if text is None:
            return f"错误：读不到文件 {rel!r}，不能打补丁"
        try:
            blocks = parse_patch(patch)
        except ValueError as exc:
            return f"错误：补丁格式不对（{exc}）"
        new_text, errors = apply_blocks(text, blocks)
        if errors:
            return "错误：" + "；".join(errors)
        self.index.write_text(rel, new_text)
        self.patch_count += 1
        self.index.scan()
        return f"已应用补丁：{rel}（{len(blocks)} 处）"

    def run_tests(self) -> str:
        if not self.test_cmd:
            return "错误：没有可用的测试命令，仓库里既没有 test_*.py，也没有 pytest 配置"
        argv = self.test_cmd.split()
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"   # 别在工作目录里落 __pycache__
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.index.root),
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return "错误：测试超时（300s）"
        except FileNotFoundError:
            return f"错误：测试命令不可用 {self.test_cmd!r}"
        output = (proc.stdout or "") + (proc.stderr or "")
        tail = output.strip().splitlines()[-40:]
        body = "\n".join(tail) if tail else "（无输出）"
        if proc.returncode == 0:
            return f"测试通过（退出码 0）\n{body}"
        return f"测试失败（退出码 {proc.returncode}）\n{body}"


def autodetect_test_cmd(index: RepoIndex) -> Optional[str]:
    names = [Path(rel).name for rel in index.files]
    if any(n.startswith("test_") and n.endswith(".py") for n in names):
        return f"{sys.executable} -m unittest discover -s . -p test_*.py"
    for cfg in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"):
        if (index.root / cfg).exists():
            return f"{sys.executable} -m pytest -q"
    return None


def prepare_workdir(repo: Path, in_place: bool) -> Tuple[Path, bool]:
    """默认在临时副本上操作，保证每次演示结果一致；--in-place 才直接改原目录。"""
    if in_place:
        return repo, False
    tmp = Path(tempfile.mkdtemp(prefix="code-agent-"))
    dst = tmp / repo.name
    shutil.copytree(repo, dst, ignore=shutil.ignore_patterns(*sorted(SKIP_DIRS), ".git", "*.pyc"))
    return dst, True


# ---------------------------------------------------------------------------
# 任务规格
# ---------------------------------------------------------------------------


def load_task(repo: Path, task_text: Optional[str], task_file: Optional[str]) -> TaskSpec:
    candidates: List[Path] = []
    if task_file:
        candidates.append(Path(task_file))
    candidates.append(repo / TASK_FILE_NAME)
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"[!] 任务文件解析失败 {path}: {exc}")
        edits = data.get("edits") or []
        edit = edits[0] if edits else {}
        return TaskSpec(
            goal=str(task_text or data.get("goal", "（未指定任务）")),
            keyword=str(data.get("keyword") or Path(str(edit.get("file", ""))).stem or "TODO"),
            file=str(edit.get("file", "")),
            find=str(edit.get("find", "")),
            replace=str(edit.get("replace", "")),
            symbol=str(data.get("symbol", "")),
            source=str(path),
        )
    return TaskSpec(
        goal=str(task_text or "（未指定任务）"),
        keyword="TODO",
        file="",
        find="",
        replace="",
        symbol="",
        source="（无任务文件）",
    )


# ---------------------------------------------------------------------------
# 模型：mock 与 live
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是仓库级代码智能体。每轮只输出一个 JSON 对象，不要输出任何解释文字。

可用动作（一次只能给一个）：
{"action": "list_files"}
{"action": "search", "query": "关键词"}
{"action": "read_file", "path": "仓库相对路径", "start": 1, "end": 200}
{"action": "read_symbols", "path": "仓库相对路径"}
{"action": "apply_patch", "path": "仓库相对路径", "patch": "<<<<<<< SEARCH\\n旧文本\\n=======\\n新文本\\n>>>>>>> REPLACE"}
{"action": "run_tests"}
{"action": "done", "summary": "一句话说明改了什么"}

规则：
1. apply_patch 的旧文本必须与文件内容逐字符一致，且在文件中唯一；不确定就先 read_file。
2. 补丁失败时不要重复提交同一个补丁，先读回真实内容再重试。
3. 改完必须 run_tests，测试通过才允许 done。
"""


class MockModel:
    """离线模型：动作序列由 .code-agent-task.json 驱动。

    它只负责产生动作，检索、补丁应用、失败回读、测试闸门全部走真实逻辑，
    所以可以在没有 API Key 的情况下验证脚手架是不是通的。
    """

    def __init__(self, spec: TaskSpec) -> None:
        self.spec = spec
        self.patch_failures = 0
        self.test_failures = 0
        self.use_exact = False   # 第一次先按记忆里的片段猜锚点，失败后才回读真实内容

    @staticmethod
    def _last_tool(history: List[Dict[str, Any]]) -> Tuple[Optional[str], str]:
        for entry in reversed(history):
            if entry.get("tool"):
                return str(entry["tool"]), str(entry.get("raw", entry.get("content", "")))
        return None, ""

    def next(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        tool, obs = self._last_tool(history)
        if tool is None:
            return {"action": "search", "query": self.spec.keyword}
        if tool == "search":
            if not self.spec.has_edit:
                return {"action": "done", "summary": "mock 模式没有可用的补丁目标，仅完成检索"}
            return {"action": "read_file", "path": self.spec.file, "start": 1, "end": 200}
        if tool == "read_file":
            return self._patch_action(obs)
        if tool == "apply_patch":
            if obs.startswith("错误"):
                self.patch_failures += 1
                self.use_exact = True
                if self.patch_failures > 2:
                    return {"action": "done", "summary": "补丁连续失败，停止"}
                return {"action": "read_file", "path": self.spec.file, "start": 1, "end": 200}
            return {"action": "run_tests"}
        if tool == "run_tests":
            if obs.startswith("测试通过"):
                return {"action": "done", "summary": f"已修复 {self.spec.file}，测试通过"}
            self.test_failures += 1
            if self.test_failures > 2:
                return {"action": "done", "summary": "测试仍不通过，停止"}
            return {"action": "read_file", "path": self.spec.file, "start": 1, "end": 200}
        return {"action": "done", "summary": "无事可做"}

    def _patch_action(self, obs: str) -> Dict[str, Any]:
        """先按任务文件里的片段猜锚点；失败后用读回来的真实内容重生成补丁。

        真实内容里往往有多处一样的片段，这里靠"所在函数名 + 规范化精确匹配"消歧，
        这正是唯一性检查失败之后该做的事：扩大上下文，而不是乱改一处。
        """
        lines = parse_numbered(obs)
        if not lines:
            return {"action": "read_file", "path": self.spec.file, "start": 1, "end": 200}

        target = " ".join(self.spec.find.split())
        joined = "\n".join(content for _, content in lines)

        # 已经改过了（替换文本在、原始片段不在）→ 不用再打补丁，直接去验证
        if self.spec.replace and self.spec.replace in joined and self.spec.find not in joined:
            return {"action": "run_tests"}

        if not self.use_exact:
            return {"action": "apply_patch", "path": self.spec.file,
                    "patch": make_patch(self.spec.find, self.spec.replace)}

        cands: List[Tuple[int, str, str, str]] = []
        for idx, (_, content) in enumerate(lines):
            norm = " ".join(content.split())
            if not norm or not target or target not in norm:
                continue
            cands.append((idx, content, norm, def_context(lines, idx)))

        if not cands:
            return {"action": "apply_patch", "path": self.spec.file,
                    "patch": make_patch(self.spec.find, self.spec.replace)}

        chosen: Optional[Tuple[int, str, str, str]] = None
        if self.spec.symbol:
            for cand in cands:
                if cand[3] == self.spec.symbol:
                    chosen = cand
                    break
        if chosen is None:
            for cand in cands:
                if cand[2] == target:
                    chosen = cand
                    break
        if chosen is None:
            chosen = cands[0]

        chosen_idx, old_line = chosen[0], chosen[1]
        if self.spec.find and self.spec.find in old_line:
            new_line = old_line.replace(self.spec.find, self.spec.replace)
        else:
            indent = old_line[: len(old_line) - len(old_line.lstrip())]
            new_line = indent + self.spec.replace

        # 唯一性检查失败之后该做的事：扩大上下文，把所在函数签名一起放进锚点。
        didx = def_line_index(lines, chosen_idx)
        if 0 <= didx < chosen_idx:
            old = "\n".join(content for _, content in lines[didx: chosen_idx + 1])
            new = "\n".join([content for _, content in lines[didx: chosen_idx]] + [new_line])
        else:
            old, new = old_line, new_line
        return {"action": "apply_patch", "path": self.spec.file, "patch": make_patch(old, new)}


class LiveModel:
    """--live：调用真实 API（OpenAI 兼容的 chat/completions）。"""

    def __init__(self, model: str, base_url: str, api_key: str) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def next(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"模型接口返回 {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"连不上模型接口 {self.base_url}: {exc.reason}") from exc
        content = data["choices"][0]["message"].get("content") or ""
        return parse_action(content)


def parse_numbered(obs: str) -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    for line in obs.splitlines():
        match = re.match(r"^\s*(\d+)\|(.*)$", line)
        if match:
            out.append((int(match.group(1)), match.group(2)))
    return out


DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")


def def_line_index(lines: List[Tuple[int, str]], idx: int) -> int:
    """返回第 idx 行所在函数定义的行下标（找不到返回 -1）。"""
    for i in range(idx, -1, -1):
        if DEF_RE.match(lines[i][1]):
            return i
    return -1


def def_context(lines: List[Tuple[int, str]], idx: int) -> str:
    """返回第 idx 行所在的最内层函数名（按 Python 风格的 def 往上找）。"""
    didx = def_line_index(lines, idx)
    if didx < 0:
        return ""
    match = DEF_RE.match(lines[didx][1])
    return match.group(1) if match else ""


def make_patch(old: str, new: str) -> str:
    return f"{PATCH_BEGIN}\n{old}\n{PATCH_MID}\n{new}\n{PATCH_END}"


def parse_action(content: str) -> Dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"模型输出里找不到 JSON 动作：{content[:200]!r}")
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict) or "action" not in data:
        raise ValueError(f"模型输出缺少 action 字段：{content[:200]!r}")
    return data


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


def run_task(index: RepoIndex, toolbox: Toolbox, model: Any, spec: TaskSpec,
             max_steps: int = 12, verbose: bool = True) -> Dict[str, Any]:
    repo_map = index.repo_map()
    first_user = (
        f"任务：{spec.goal}\n\n"
        f"仓库根目录：{index.root}\n\n"
        f"仓库地图：\n{repo_map}\n\n"
        "先用 search/read_symbols 定位，再 read_file 看真实内容，再 apply_patch，"
        "最后 run_tests 验证。测试通过前不要 done。"
    )
    history: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": first_user},
    ]
    applied = 0
    status = "max_steps_reached"
    summary = ""

    for step in range(1, max_steps + 1):
        try:
            action = model.next(history)
        except Exception as exc:
            history.append({"role": "user", "content": f"错误：模型调用失败 {exc}"})
            status = "model_error"
            break

        name = str(action.get("action", ""))
        if name == "done":
            status = "done"
            summary = str(action.get("summary", ""))
            if verbose:
                print(f"\n[步骤 {step}] 模型宣布完成：{summary}")
            break

        tool, obs = toolbox.dispatch(action)
        history.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
        history.append({"role": "user", "content": f"观察结果（{tool}）：\n{obs}",
                        "tool": tool, "raw": obs})
        if tool == "apply_patch" and not obs.startswith("错误"):
            applied += 1
        if verbose:
            print(f"\n[步骤 {step}] {name} -> {first_line(obs)}")
            for line in obs.splitlines()[1:6]:
                print(f"    {line}")

    # 闸门：收工时再跑一次测试，最终确认
    verified = False
    final = ""
    if status == "done":
        final = toolbox.run_tests()
        verified = final.startswith("测试通过")
        if verbose:
            print(f"\n[收口验证] {first_line(final)}")
    return {
        "status": status,
        "summary": summary,
        "verified": verified,
        "patches_applied": applied,
        "final_test": final,
        "steps": len(history),
    }


def first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:160]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="ch37 代码智能体与仓库级自治 —— 最小可实现")
    parser.add_argument("--repo", default=str(DEFAULT_REPO), help="仓库根目录（默认内置 demo_repo）")
    parser.add_argument("--task", default=None, help="任务描述（默认取任务文件里的 goal）")
    parser.add_argument("--task-file", default=None, help="任务文件路径（默认 <repo>/.code-agent-task.json）")
    parser.add_argument("--test-cmd", default=None, help="测试命令（默认自动探测）")
    parser.add_argument("--map-lines", type=int, default=200, help="仓库地图的行数预算")
    parser.add_argument("--max-steps", type=int, default=12, help="最大步数")
    parser.add_argument("--in-place", action="store_true",
                        help="直接改 --repo 指向的目录（默认在临时副本上操作）")
    parser.add_argument("--live", action="store_true", help="用真实 API（需 OPENAI_API_KEY）")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--show-map", action="store_true", help="只打印仓库地图后退出")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"[!] 仓库目录不存在：{repo}")
        return 2
    if args.live and not os.environ.get("OPENAI_API_KEY"):
        print("[!] --live 需要环境变量 OPENAI_API_KEY；不设置时可用 mock 模式跑通全回路。")
        return 2

    index = RepoIndex(repo)
    index.scan()
    spec = load_task(repo, args.task, args.task_file)

    if args.show_map:
        print(index.repo_map(args.map_lines))
        return 0

    workdir, copied = prepare_workdir(repo, args.in_place)
    if copied:
        index = RepoIndex(workdir)
        index.scan()
        spec = load_task(workdir, args.task, args.task_file)
        toolbox = Toolbox(index, args.test_cmd)
        print(f"工作目录：{workdir}（默认在副本上操作，加 --in-place 才直接改原目录）")
    else:
        toolbox = Toolbox(index, args.test_cmd)
        print(f"工作目录：{workdir}（--in-place，直接改原目录）")

    print(f"仓库：{index.root}")
    print(f"可索引文件：{len(index.files)} 个，符号：{sum(len(v) for v in index.symbols.values())} 个")
    print(f"任务：{spec.goal}")
    print(f"测试命令：{toolbox.test_cmd or '（未探测到）'}")
    print("-" * 60)

    model: Any
    if args.live:
        model = LiveModel(args.model, args.base_url, os.environ.get("OPENAI_API_KEY", ""))
    else:
        model = MockModel(spec)
        if not spec.has_edit:
            print("[i] 未找到任务文件，mock 模式只能演示检索；补丁演示请用内置 demo_repo 或补 .code-agent-task.json")

    started = time.time()
    result = run_task(index, toolbox, model, spec, max_steps=args.max_steps)
    elapsed = time.time() - started

    print("-" * 60)
    print(f"结束状态：{result['status']}，应用补丁 {result['patches_applied']} 处，"
          f"用时 {elapsed:.1f}s，模式：{'live' if args.live else 'mock'}")
    if result["summary"]:
        print(f"模型说明：{result['summary']}")
    if result["status"] == "done":
        if result["verified"]:
            print("[OK] 收口验证通过：测试是绿的。")
            return 0
        print("[FAIL] 收口验证失败：测试没过，不算修好。")
        for line in result["final_test"].splitlines()[:20]:
            print(f"    {line}")
        return 1
    print("[FAIL] 任务未完成（模型没有宣布完成，或触发了错误）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
