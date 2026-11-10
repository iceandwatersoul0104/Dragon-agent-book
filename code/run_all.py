#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量跑通 code/ 下的全部配套示例。

用法：
    python code/run_all.py            # 全部跑一遍（mock 模式，离线可用）
    python code/run_all.py --only ch04 # 只跑目录名包含 ch04 的示例
    python code/run_all.py --timeout 60

约定：每个示例目录下应有 main.py，默认 mock 模式可离线运行；
用 --live 时示例才会走真实 API（需要自行配置环境变量）。
本脚本不传 --live，因此永远不会消耗真实额度。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文结果乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def discover(only: str | None) -> list[Path]:
    dirs = sorted(
        p for p in CODE_ROOT.iterdir()
        if p.is_dir() and (p / "main.py").exists() and not p.name.startswith((".", "_"))
    )
    if only:
        dirs = [p for p in dirs if only in p.name]
    return dirs


def run_one(script: Path, timeout: int) -> tuple[bool, str, float]:
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(script.parent),
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
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    detail = tail[-1][:200] if tail else f"退出码 {proc.returncode}"
    return False, detail, elapsed


def main() -> int:
    ap = argparse.ArgumentParser(description="跑通全部配套示例")
    ap.add_argument("--only", default=None, help="只跑目录名包含该子串的示例")
    ap.add_argument("--timeout", type=int, default=60, help="单个示例超时秒数")
    args = ap.parse_args()

    dirs = discover(args.only)
    if not dirs:
        print("没有找到任何示例目录（需要 code/<章节>/main.py）")
        return 0

    print(f"共 {len(dirs)} 个示例，解释器 {sys.executable}")
    print("=" * 78)

    passed, failed = [], []
    for d in dirs:
        ok, detail, elapsed = run_one(d / "main.py", args.timeout)
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {d.name:<32} {elapsed:6.2f}s  {detail}")
        (passed if ok else failed).append((d.name, detail))

    print("=" * 78)
    print(f"通过 {len(passed)} / {len(dirs)}")
    if failed:
        print("\n失败明细：")
        for name, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
