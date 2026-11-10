"""结构化日志：字段固定、trace_id 贯穿、敏感字段脱敏。

这一层只管**运行日志**，不管 trace 与成本账——那是第 12 章
``observability.py`` 的活。两者的关联点是同一个 ``trace_id``：
日志里的每一条都能按 trace_id 汇到那一次运行的 trace 上。

三条约定：

1. **一行一条 JSON。** 日志要被机器读、被 grep、被灌进日志系统，
   自由文本只能给人看。每条记录的固定字段是
   ``ts / level / logger / msg / trace_id``，其余字段作为扩展放进去。

2. **trace_id 用 contextvars 贯穿。** 不用显式层层传参——一线程一次调用
   链上，trace_id 是"当前这次任务"的属性，不是某个函数的参数。

3. **敏感字段在写入前脱敏。** 键名命中敏感词就整值替换；
   消息正文里的密钥形态（``sk-xxx``、``Bearer xxx``）也用正则遮掉。
   脱敏发生在**写出去之前**，不是在读的时候。
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

__all__ = [
    "LOGGER_ROOT",
    "SENSITIVE_KEYS",
    "MASK",
    "new_trace_id",
    "current_trace_id",
    "set_trace_id",
    "trace_scope",
    "redact",
    "redact_text",
    "JsonFormatter",
    "configure_logging",
    "get_logger",
    "log_event",
]

#: 本框架所有 logger 都挂在这个根名下面。
LOGGER_ROOT = "dragon_agent"

#: 命中即整值替换的键名（小写比较）。
SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "access_key", "secret_key",
    "authorization", "auth",
    "password", "passwd", "pwd",
    "secret", "token", "access_token", "refresh_token", "session_token",
    "cookie", "set_cookie",
    "private_key", "credential", "credentials",
})

#: 脱敏后写入的值。
MASK = "***REDACTED***"

#: 正文里的密钥形态——键名对不上时靠这个兜底。
_TEXT_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token)\s*[=:]\s*\S+"),
)

#: 当前 trace_id。contextvars 保证并发任务之间互不串味。
_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dragon_trace_id", default=None
)

# LogRecord 自带字段——这些不能被 extra 覆盖，也不该出现在扩展字段里。
_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


# --------------------------------------------------------------------------- trace_id


def new_trace_id() -> str:
    """生成一个新的 trace_id。"""
    return uuid.uuid4().hex


def current_trace_id() -> str | None:
    """当前 trace_id；不在任何 trace 作用域里时返回 None。"""
    return _trace_id.get()


def set_trace_id(trace_id: str | None) -> contextvars.Token:
    """直接设置 trace_id，返回可用于 :func:`contextvars.ContextVar.reset` 的令牌。"""
    return _trace_id.set(trace_id)


@contextlib.contextmanager
def trace_scope(trace_id: str | None = None) -> Iterator[str]:
    """在作用域内绑定 trace_id，退出时自动还原。

    嵌套使用时，内层还原成外层的值，不会把外层的 trace_id 冲掉。
    """
    tid = trace_id or new_trace_id()
    token = _trace_id.set(tid)
    try:
        yield tid
    finally:
        _trace_id.reset(token)


# --------------------------------------------------------------------------- 脱敏


def redact_text(text: str) -> str:
    """遮掉正文里的密钥形态。"""
    for pattern in _TEXT_PATTERNS:
        text = pattern.sub(MASK, text)
    return text


def redact(value: Any) -> Any:
    """递归脱敏：dict / list / tuple / 字符串。

    键名命中 :data:`SENSITIVE_KEYS` 就**整值替换**——不保留长度、不保留前缀，
    因为"知道密钥前 4 位"本身就是泄露。
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
                out[key] = MASK
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


# --------------------------------------------------------------------------- 格式化


class JsonFormatter(logging.Formatter):
    """一行一条 JSON 的日志格式化器。

    固定字段：``ts``（UTC，毫秒精度）、``level``、``logger``、``msg``、
    ``trace_id``。其余记录在日志调用处的字段作为扩展字段输出。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
            "trace_id": getattr(record, "trace_id", None) or current_trace_id(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_") or key in payload:
                continue
            payload[key] = redact(value)
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- 配置


def configure_logging(
    level: str = "INFO",
    stream: Any = None,
    json_output: bool = True,
) -> logging.Logger:
    """配置本框架的根 logger，返回它。

    重复调用是安全的：会先摘掉旧 handler，不至于打印两遍。
    日志默认走 stderr——stdout 留给程序自己的输出。
    """
    logger = logging.getLogger(LOGGER_ROOT)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-5s %(name)s %(message)s")
        )
    logger.addHandler(handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    """取一个框架内的 logger。名字自动挂到 ``dragon_agent.`` 根下。"""
    if name.startswith(LOGGER_ROOT + ".") or name == LOGGER_ROOT:
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_ROOT}.{name}")


def log_event(
    logger: logging.Logger,
    event: str,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """记一条结构化事件。

    扩展字段名如果撞上 ``LogRecord`` 的保留字段（比如 ``name``、``args``），
    会被改名成 ``field_<name>``——直接传会抛 ``KeyError``，那是调用方的 bug，
    但一条日志不该让业务崩掉。
    """
    extra: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _RESERVED or key in {"msg", "trace_id"}:
            extra[f"field_{key}"] = value
        else:
            extra[key] = value
    logger.log(level, event, extra=extra)
