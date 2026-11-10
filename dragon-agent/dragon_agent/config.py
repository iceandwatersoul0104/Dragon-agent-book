"""统一配置：模型、预算、路径、开关。

约定：
- ``Config.from_env_and_args`` 是唯一的构造入口。
- ``to_dict()`` 绝不包含密钥——它会被写进 trace 和检查点。
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """配置不合法（缺 key、预算为负、路径冲突等）。"""


@dataclass(frozen=True)
class Config:
    """一次运行所需的全部静态配置。

    用 ``frozen=True`` 是有意的：配置在运行中不可变，任何模块想改配置都得显式重建，
    这样 trace 里记录的配置一定和实际生效的一致。
    """

    model: str = "mock-model"
    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.0

    # 三道预算闸门：步数、token、钱。任何一道到顶都触发停止。
    max_steps: int = 12
    token_budget: int = 120_000
    cost_budget: float = 0.50

    timeout_s: int = 60
    live: bool = False
    workspace: Path = field(default_factory=lambda: Path.cwd())
    trace_path: Path | None = None

    # 单价表：每百万 token 的美元价格。mock 为 0，live 由配置覆盖。
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ConfigError(f"max_steps 必须 ≥1，收到 {self.max_steps}")
        if self.token_budget < 1:
            raise ConfigError(f"token_budget 必须 ≥1，收到 {self.token_budget}")
        if self.cost_budget < 0:
            raise ConfigError(f"cost_budget 不能为负，收到 {self.cost_budget}")
        if self.live and not self.api_key:
            raise ConfigError("--live 需要设置 LLM_API_KEY（或 OPENAI_API_KEY）")

    # ------------------------------------------------------------------ 构造

    @classmethod
    def from_env_and_args(cls, argv: list[str] | None = None) -> "Config":
        """从环境变量与命令行参数组装配置。

        环境变量：``LLM_API_KEY`` / ``OPENAI_API_KEY``、``LLM_BASE_URL``、``LLM_MODEL``。
        命令行：``--live``、``--budget``（token）、``--max-steps``、``--cost``、``--trace``、``--model``。
        """
        parser = argparse.ArgumentParser(prog="dragon-agent", add_help=False)
        parser.add_argument("--live", action="store_true", help="切到真实 API")
        parser.add_argument("--budget", type=int, default=None, help="单任务 token 预算")
        parser.add_argument("--cost", type=float, default=None, help="单任务成本上限（美元）")
        parser.add_argument("--max-steps", type=int, default=None, help="步数上限")
        parser.add_argument("--model", default=None, help="模型标识")
        parser.add_argument("--trace", default=None, help="trace 输出目录")
        parser.add_argument("--workspace", default=None, help="工作目录")
        args, _ = parser.parse_known_args(argv if argv is not None else [])

        api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or cls.base_url
        model = args.model or os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL") or cls.model

        workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()
        trace_path = Path(args.trace).resolve() if args.trace else None

        cfg = cls(
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_steps=args.max_steps if args.max_steps is not None else cls.max_steps,
            token_budget=args.budget if args.budget is not None else cls.token_budget,
            cost_budget=args.cost if args.cost is not None else cls.cost_budget,
            live=bool(args.live),
            workspace=workspace,
            trace_path=trace_path,
        )
        return cfg

    # ------------------------------------------------------------------ 输出

    def to_dict(self) -> dict[str, Any]:
        """可安全落盘的配置表示——**不含 api_key**。"""
        d = asdict(self)
        d.pop("api_key", None)
        d["workspace"] = str(self.workspace)
        d["trace_path"] = str(self.trace_path) if self.trace_path else None
        return d

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        d = self.to_dict()
        d["api_key"] = "***" if self.api_key else None
        return f"Config({d})"
