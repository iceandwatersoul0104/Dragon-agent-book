#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量跑通 dragon-agent 的全部示例脚本与测试。

用法：
    python dragon-agent/run_all.py                 # 跑全部示例（mock 模式，离线可用）
    python dragon-agent/run_all.py --only guard    # 只跑名字含该子串的示例
    python dragon-agent/run_all.py --skip-tests    # 只跑示例，不跑测试
    python dragon-agent/run_all.py --timeout 90

与第一卷 `code/run_all.py` 同构。两卷各自有配套代码，验收脚本也各有一份，
这样"书里说能跑"和"实测能跑"之间没有中间环节。

约定：示例默认 mock 模式可离线运行；用 --live 时才会走真实 API（需自行配
环境变量）。本脚本不传 --live，因此永远不会消耗真实额度。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"


# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文结果乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def discover(only: str | None) -> list[Path]:
    scripts = sorted(
        p for p in EXAMPLES.glob("*.py")
        if not p.name.startswith((".", "_"))
    )
    if only:
        scripts = [p for p in scripts if only in p.name]
    return scripts


def run_one(args_list: list[str], cwd: Path, timeout: int, label: str) -> tuple[bool, str, float]:
    started = time.time()
    try:
        proc = subprocess.run(
            args_list,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                # 验收不该在交付目录里留下 __pycache__/.pyc
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
    except subprocess.TimeoutExpired:
        return False, f"超时（>{timeout}s）", time.time() - started
    except Exception as exc:  # noqa: BLE001
        return False, f"启动失败: {exc}", time.time() - started

    elapsed = time.time() - started
    if proc.returncode == 0:
        return True, "OK", elapsed
    # unittest 的结果写在 stderr，不合并会看到空输出
    tail = (proc.stderr or "") + (proc.stdout or "")
    lines = [x for x in tail.strip().splitlines() if x.strip()]
    detail = lines[-1][:200] if lines else f"退出码 {proc.returncode}"
    return False, f"{label} {detail}", elapsed


def main() -> int:
    ap = argparse.ArgumentParser(description="跑通 dragon-agent 的全部示例与测试")
    ap.add_argument("--only", default=None, help="只跑名字含该子串的示例")
    ap.add_argument("--timeout", type=int, default=90, help="单个子进程超时秒数")
    ap.add_argument("--skip-tests", action="store_true", help="不跑测试套件")
    args = ap.parse_args()

    scripts = discover(args.only)
    if not scripts:
        print("没有找到任何示例脚本（需要 examples/*.py）")
        return 0

    print(f"解释器 {sys.executable}")
    print(f"示例 {len(scripts)} 个，{'不跑测试' if args.skip_tests else '含测试套件'}")
    print("=" * 78)

    passed, failed = [], []

    for s in scripts:
        ok, detail, elapsed = run_one([sys.executable, s.name], s.parent, args.timeout, "示例")
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] examples/{s.name:<34} {elapsed:6.2f}s  {detail}")
        (passed if ok else failed).append((f"examples/{s.name}", detail))

    if not args.skip_tests:
        ok, detail, elapsed = run_one(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            ROOT, max(args.timeout, 180), "测试",
        )
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] tests/ (unittest discover)              {elapsed:6.2f}s  {detail}")
        (passed if ok else failed).append(("tests/", detail))

    print("=" * 78)
    print(f"通过 {len(passed)} / {len(passed) + len(failed)}")
    if failed:
        print("\n失败明细：")
        for name, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
