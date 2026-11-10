#!/usr/bin/env python3
"""ch31 沙箱与执行隔离 —— 最小可实现参考实现。

设计目标
    1. 决策与执行分离：模型只产出"命令候选"，执行前必须过静态策略判定。
    2. 执行环境最小化：独立进程组 / 独立 workdir / 环境变量白名单 / 资源限额 /
       墙上时钟超时 / 整组 kill / 输出裁剪；Docker 引擎额外加网络与挂载策略。
    3. 全链路可审计：每条命令的判定、理由、退出码、耗时落成一行 JSON。

依赖
    仅 Python 标准库（>= 3.8）。Docker 引擎模式另需本机 Docker Engine。
    默认 mock 模式不需要任何 API Key；--live 才需要 OPENAI_API_KEY。

用法
    python main.py                       # mock 全场景
    python main.py --scenario egress     # 只跑一个场景
    python main.py --engine docker       # 用 Docker 引擎执行（需先 docker pull）
    python main.py --live                # 用真实模型产生命令候选
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # POSIX only
    import resource  # type: ignore
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore

IS_POSIX = os.name == "posix"

# Windows 控制台默认是 GBK，强制 UTF-8 输出，避免中文判定理由显示成乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover - 老版本或被重定向
        pass

# ---------------------------------------------------------------------------
# 策略常量
# ---------------------------------------------------------------------------

# shell 元字符：出现即拒绝，因为它们是绕过策略的常见载体（管道、重定向、命令替换）
SHELL_META = set(";|&$`><\n\r")

# 会发起网络连接的可执行文件，默认一律拒绝出网
NETWORK_TOOLS = {
    "curl", "wget", "nc", "ncat", "netcat", "telnet", "ftp", "ssh", "scp",
    "rsync", "git", "pip", "npm", "apt", "apt-get",
}

# 敏感路径提示：命中即拒绝，避免读宿主凭据与配置
SENSITIVE_HINTS = (
    "/etc/shadow", "/etc/passwd", "/etc/sudoers",
    "/.ssh/", "/.aws/", "/.gnupg/", "/.kube/",
    "/proc/1/environ", "/proc/self/environ",
    "/var/run/docker.sock", "/run/docker.sock",
    "169.254.169.254", "/.env",
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class SandboxPolicy:
    """一次执行使用的策略。所有字段都应该是"显式收紧"，不是默认值继承。"""

    workdir: Path
    allowed_commands: set = field(default_factory=set)
    allow_network: bool = False
    allow_shell_meta: bool = False
    timeout_s: float = 5.0
    mem_mb: int = 256
    cpu_s: int = 10
    max_pids: int = 64
    max_output_bytes: int = 2048
    env_allowlist: Tuple[str, ...] = ()


@dataclass
class RunResult:
    cmd: List[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# 策略判定（第一层：决策与执行分离）
# ---------------------------------------------------------------------------


def _looks_like_path(arg: str) -> bool:
    return ("/" in arg) or ("\\" in arg) or arg.startswith(".")


def _within(root: Path, candidate: str) -> bool:
    """candidate 必须落在 root 之内（绝对路径按原样判定，相对路径按 root 解析）。"""
    root_real = os.path.realpath(str(root))
    target = candidate if os.path.isabs(candidate) else os.path.join(root_real, candidate)
    target_real = os.path.realpath(target)
    return target_real == root_real or target_real.startswith(root_real + os.sep)


def judge(cmd: Any, policy: SandboxPolicy) -> Tuple[bool, str]:
    """静态判定。返回 (是否放行, 理由)。放行为 False 时绝不进入执行层。"""
    if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
        return False, "命令必须是非空字符串列表"
    if not policy.allow_shell_meta:
        for arg in cmd[1:]:
            if any(ch in arg for ch in SHELL_META):
                return False, "参数含 shell 元字符: {!r}".format(arg)

    prog = os.path.basename(cmd[0])
    if prog in NETWORK_TOOLS and not policy.allow_network:
        return False, "出网被拒绝: {}".format(prog)
    if prog not in policy.allowed_commands:
        return False, "可执行文件不在白名单: {}".format(prog)

    for arg in cmd[1:]:
        if not _looks_like_path(arg):
            continue
        if any(hint in arg for hint in SENSITIVE_HINTS):
            return False, "命中敏感路径: {}".format(arg)
        if not _within(policy.workdir, arg):
            return False, "路径越界: {}".format(arg)
    return True, "ok"


# ---------------------------------------------------------------------------
# 执行层
# ---------------------------------------------------------------------------


def _build_env(policy: SandboxPolicy, workdir: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for key in policy.env_allowlist:
        if key in os.environ:
            env[key] = os.environ[key]
    # HOME 指向沙箱工作目录，避免任务读到用户家目录下的凭据
    env["HOME"] = str(workdir)
    env["TMPDIR"] = str(workdir / "tmp")
    env["TMP"] = str(workdir / "tmp")
    env["TEMP"] = str(workdir / "tmp")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _apply_rlimits(policy: SandboxPolicy) -> None:
    """在子进程 exec 之前设置资源上限。仅 POSIX 可用。"""
    if resource is None:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (policy.cpu_s, policy.cpu_s + 1))
    except (ValueError, OSError):
        pass
    mem = policy.mem_mb * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    except (ValueError, OSError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (policy.max_pids, policy.max_pids))
    except (ValueError, OSError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
    except (ValueError, OSError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass


def _kill_tree(proc: "subprocess.Popen") -> None:
    """超时后杀掉整棵进程树，只 kill 父进程会留下孤儿继续跑。"""
    if proc.poll() is not None:
        return
    try:
        if IS_POSIX:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:  # pragma: no cover - Windows
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _truncate(data: str, limit: int) -> Tuple[str, bool]:
    if len(data.encode("utf-8", errors="replace")) <= limit:
        return data, False
    clipped = data.encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")
    return clipped + "\n...[output truncated at {} bytes]".format(limit), True


class LocalRunner:
    """本地模式：独立进程组 + rlimit + 超时 + 输出裁剪（POSIX 效果最完整）。"""

    engine = "local"

    def __init__(self, policy: SandboxPolicy) -> None:
        self.policy = policy
        self.workdir = policy.workdir / "run"
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / "tmp").mkdir(parents=True, exist_ok=True)

    def run(self, cmd: List[str]) -> RunResult:
        policy = self.policy
        env = _build_env(policy, self.workdir)
        kwargs: Dict[str, Any] = dict(
            cwd=str(self.workdir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if IS_POSIX:
            def preexec() -> None:  # noqa: WPS430 - 需要在 fork 后、exec 前执行
                os.setsid()  # 独立 session / 进程组，便于整组 kill
                _apply_rlimits(policy)

            kwargs["preexec_fn"] = preexec
        else:  # pragma: no cover - Windows
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        started = time.time()
        try:
            proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 - 已过白名单与元字符校验
        except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            return RunResult(cmd, -1, "", "", int((time.time() - started) * 1000), error=str(exc))

        timed_out = False
        try:
            out, err = proc.communicate(timeout=policy.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:  # pragma: no cover - 兜底
                out, err = b"", b""
        duration = int((time.time() - started) * 1000)

        out_text = out.decode("utf-8", errors="replace") if isinstance(out, bytes) else (out or "")
        err_text = err.decode("utf-8", errors="replace") if isinstance(err, bytes) else (err or "")
        out_text, cut1 = _truncate(out_text, policy.max_output_bytes)
        err_text, cut2 = _truncate(err_text, policy.max_output_bytes)
        code = proc.returncode if proc.returncode is not None else -9
        return RunResult(cmd, code, out_text, err_text, duration, timed_out, cut1 or cut2)


class DockerRunner:
    """Docker 模式：把正文 2.3 / 2.4 / 2.6 的参数逐条落到命令行上。"""

    engine = "docker"
    IMAGE = "python:3.12-slim"

    def __init__(self, policy: SandboxPolicy) -> None:
        self.policy = policy
        self.workdir = policy.workdir / "run"
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / "tmp").mkdir(parents=True, exist_ok=True)
        self.docker = shutil.which("docker")
        if not self.docker:
            raise RuntimeError("未找到 docker，可执行文件，请安装 Docker Engine 或改用 --engine local")
        try:  # 让容器内非 root 用户能写 workdir
            os.chmod(self.workdir, 0o777)
        except OSError:
            pass

    def _adapt(self, cmd: List[str]) -> List[str]:
        """把宿主解释器绝对路径翻译成镜像里的 python。"""
        out = list(cmd)
        if out and os.path.realpath(out[0]) == os.path.realpath(sys.executable):
            out[0] = "python"
        return out

    def run(self, cmd: List[str]) -> RunResult:
        policy = self.policy
        args = [
            self.docker, "run", "--rm",
            "--network", "none",                       # 默认禁止出网
            "--read-only",                             # 根文件系统只读
            "--tmpfs", "/tmp:rw,size=64m",             # 唯一写点
            "--cap-drop", "ALL",                       # 丢弃全部 capability
            "--security-opt", "no-new-privileges",    # 阻断 setuid 提权
            "--pids-limit", str(policy.max_pids),      # 防 fork 炸弹
            "--memory", "{}m".format(policy.mem_mb),   # 内存上限
            "--cpus", "1",                             # CPU 配额
            "--user", "65534:65534",                  # 非 root 运行
            "--workdir", "/work",
            "-v", "{}:/work".format(self.workdir),
            "--entrypoint", "",
            self.IMAGE,
        ] + self._adapt(cmd)

        started = time.time()
        timed_out = False
        try:
            proc = subprocess.Popen(  # noqa: S603 - 参数全部由本文件构造
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except (FileNotFoundError, PermissionError) as exc:
            return RunResult(cmd, -1, "", "", int((time.time() - started) * 1000), error=str(exc))

        try:
            out, err = proc.communicate(timeout=policy.timeout_s + 20)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:  # pragma: no cover
                out, err = b"", b""
        duration = int((time.time() - started) * 1000)

        out_text = out.decode("utf-8", errors="replace") if isinstance(out, bytes) else (out or "")
        err_text = err.decode("utf-8", errors="replace") if isinstance(err, bytes) else (err or "")
        out_text, cut1 = _truncate(out_text, policy.max_output_bytes)
        err_text, cut2 = _truncate(err_text, policy.max_output_bytes)
        code = proc.returncode if proc.returncode is not None else -9
        return RunResult(cmd, code, out_text, err_text, duration, timed_out, cut1 or cut2)


# ---------------------------------------------------------------------------
# 候选来源：mock / live
# ---------------------------------------------------------------------------


def _py(code: str) -> List[str]:
    return [sys.executable, "-c", code]


def mock_plan(scenario: str) -> List[Dict[str, Any]]:
    """内置假响应，覆盖必须测到的几种结局。"""
    plans: Dict[str, List[Dict[str, Any]]] = {
        "benign": [
            {"cmd": _py("print('hello from the sandbox')"), "why": "在沙箱内执行一条只读命令"},
        ],
        "write": [
            {"cmd": _py("open('out.txt','w').write('ok')"), "why": "在工作目录内写文件"},
        ],
        "egress": [
            {"cmd": ["curl", "http://169.254.169.254/latest/meta-data/"], "why": "尝试读取云元数据"},
        ],
        "escape": [
            {"cmd": _py("print(open('/etc/passwd').read())"), "why": "尝试读取宿主敏感文件"},
        ],
        "traversal": [
            {"cmd": _py("print(open('../../../etc/hosts').read())"), "why": "尝试用相对路径逃出工作目录"},
        ],
        "shellmeta": [
            {"cmd": ["sh", "-c", "echo pwned > /tmp/x"], "why": "尝试用 shell 重定向绕过"},
        ],
        "notallowed": [
            {"cmd": ["chmod", "777", "/etc/passwd"], "why": "尝试调用白名单外的可执行文件"},
        ],
        "timeout": [
            {"cmd": _py("__import__('time').sleep(30)"), "why": "故意挂起，验证墙上时钟超时"},
        ],
    }
    if scenario == "all":
        merged: List[Dict[str, Any]] = []
        for key in ("benign", "write", "egress", "escape", "traversal", "shellmeta", "notallowed", "timeout"):
            merged.extend(plans[key])
        return merged
    return list(plans.get(scenario, plans["benign"]))


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _parse_commands(content: str) -> List[Dict[str, Any]]:
    raw = _strip_fences(content)
    data: Any = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                data = None
    items: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        data = data.get("commands") or data.get("plan") or []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("cmd"), list):
                items.append({"cmd": [str(a) for a in item["cmd"]], "why": str(item.get("why", ""))})
            elif isinstance(item, list) and all(isinstance(a, str) for a in item):
                items.append({"cmd": list(item), "why": ""})
    return items


def live_plan(prompt: str, model: str, base_url: str, api_key: str, timeout: float = 60.0) -> List[Dict[str, Any]]:
    """调用 OpenAI 兼容接口拿到命令候选。候选仍要过 judge，模型不接触执行层。"""
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一个命令规划器。只输出 JSON，格式为 "
                    '{"commands":[{"cmd":["prog","arg"],"why":"原因"}]}，不要输出任何多余文字。'
                    "cmd 必须是参数数组，不要用 shell 管道或重定向。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer {}".format(api_key)},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    return _parse_commands(content)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_policy(workdir: Path, args: argparse.Namespace) -> SandboxPolicy:
    allowlist = {"echo", "ls", "cat", "sleep", "python", "python3"}
    allowlist.add(Path(sys.executable).name)
    env_allow = ["PATH", "LANG", "LC_ALL", "HOME", "TMPDIR"]
    if not IS_POSIX:
        env_allow += ["SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR"]
    return SandboxPolicy(
        workdir=workdir,
        allowed_commands=allowlist,
        allow_network=args.allow_network,
        timeout_s=args.timeout,
        mem_mb=args.mem_mb,
        cpu_s=args.cpu_s,
        max_pids=args.max_pids,
        max_output_bytes=args.max_output_bytes,
        env_allowlist=tuple(env_allow),
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ch31 沙箱与执行隔离最小实现")
    parser.add_argument(
        "--scenario",
        default="all",
        choices=["all", "benign", "write", "egress", "escape", "traversal",
                 "shellmeta", "notallowed", "timeout"],
        help="mock 模式下要跑的场景",
    )
    parser.add_argument("--engine", default="local", choices=["local", "docker"],
                        help="local=本地进程组隔离，docker=容器隔离")
    parser.add_argument("--live", action="store_true", help="用真实模型产生命令候选（需 OPENAI_API_KEY）")
    parser.add_argument("--prompt", default="在一个临时工作目录里写一个 hello.txt，内容为 hello",
                        help="--live 时交给模型的任务描述")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--timeout", type=float, default=5.0, help="单条命令的墙上时钟超时（秒）")
    parser.add_argument("--mem-mb", type=int, default=256)
    parser.add_argument("--cpu-s", type=int, default=10)
    parser.add_argument("--max-pids", type=int, default=64)
    parser.add_argument("--max-output-bytes", type=int, default=2048)
    parser.add_argument("--allow-network", action="store_true", help="放开出网白名单里的命令（默认禁止）")
    parser.add_argument("--audit", default="audit.jsonl", help="审计日志路径")
    args = parser.parse_args(argv)

    if args.live and not os.environ.get("OPENAI_API_KEY"):
        print("[!] --live 需要环境变量 OPENAI_API_KEY；未设置时可用 mock 模式跑通全回路。")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="ch31-sandbox-"))
    policy = build_policy(workdir, args)
    audit_path = Path(args.audit)

    try:
        if args.live:
            try:
                plan = live_plan(args.prompt, args.model, args.base_url,
                                 os.environ.get("OPENAI_API_KEY", ""))
            except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as exc:
                print("[!] 调用模型失败：{}\n    回落到 mock 场景继续演示。".format(exc))
                plan = mock_plan(args.scenario)
        else:
            plan = mock_plan(args.scenario)

        try:
            runner: Any = DockerRunner(policy) if args.engine == "docker" else LocalRunner(policy)
        except RuntimeError as exc:
            print("[!] {}".format(exc))
            return 2

        print("引擎={}  workdir={}  超时={}s  内存={}MB  进程数上限={}  出网={}".format(
            runner.engine, policy.workdir, policy.timeout_s, policy.mem_mb,
            policy.max_pids, "允许" if policy.allow_network else "禁止"))
        if not IS_POSIX and runner.engine == "local":
            print("[i] 当前平台不支持 POSIX rlimit，本地模式退化为：进程组 + 超时 + 输出裁剪 + 路径/环境策略。")
        print("-" * 78)

        results: List[Dict[str, Any]] = []
        for item in plan:
            cmd = item["cmd"]
            allowed, reason = judge(cmd, policy)
            record: Dict[str, Any] = {"cmd": cmd, "why": item.get("why", ""), "allowed": allowed}
            if not allowed:
                record.update({"reason": reason, "exit_code": None, "duration_ms": 0,
                               "stdout": "", "stderr": "", "timed_out": False})
                print("[BLOCK] {}  ->  {}".format(" ".join(cmd), reason))
            else:
                result = runner.run(cmd)
                record.update({
                    "reason": "ok", "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms, "stdout": result.stdout,
                    "stderr": result.stderr, "timed_out": result.timed_out,
                    "truncated": result.truncated, "error": result.error,
                })
                flag = "TIMEOUT" if result.timed_out else "exit={}".format(result.exit_code)
                print("[RUN   ] {}  ->  {}  {}ms".format(" ".join(cmd), flag, result.duration_ms))
                if result.stdout:
                    print("         stdout: {}".format(result.stdout.strip().replace("\n", "\n                 ")))
                if result.stderr:
                    print("         stderr: {}".format(result.stderr.strip().replace("\n", "\n                 ")))
                if result.error:
                    print("         error : {}".format(result.error))
            results.append(record)
            with audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        blocked = sum(1 for r in results if not r["allowed"])
        killed = sum(1 for r in results if r.get("timed_out"))
        print("-" * 78)
        print("合计 {} 条：放行并执行 {} 条，策略拦截 {} 条，超时终止 {} 条".format(
            len(results), len(results) - blocked, blocked, killed))
        print("审计日志：{}".format(audit_path.resolve()))
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
