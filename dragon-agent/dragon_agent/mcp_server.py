"""MCP server 与 A2A 节点：把自建 Agent 的能力标准化暴露出去。

对应第二卷第 11 章。设计要点：

1. **协议层是薄适配层。** 本模块不实现业务逻辑——工具的校验与执行
   全部委托给 ``tools.py`` 的 ``ToolRegistry``。协议层只干三件事：
   把请求翻译成一次工具调用、把结果翻译成协议形状、把失败翻译成协议形状。

2. **两种失败，两条通道。** 请求本身不合法（方法不存在、参数结构不对）
   走 JSON-RPC 的 ``error``；工具跑过了但失败（参数取值不对、业务报错）
   走结果里的 ``isError: true``。分界线是**"模型能不能自己纠正"**：
   能纠正的留给模型，不能纠正的才是协议错误。

3. **版本差异集中在一张表里。** ``PROTOCOL_PROFILES`` 把每个协议版本的
   行为差异写成**数据**。新版本来了改表不改流程——这是不被协议演进
   锁死的唯一可靠办法。

4. **stdout 只跑协议帧。** stdio 传输下 stdout 就是协议信道，打印一行
   日志就会把整条流弄坏。日志一律走 stderr。

5. **一套工具，两种暴露。** MCP server 与内部 Agent 调用的是**同一个**
   ``ToolRegistry`` 实例：schema 只有一份，执行只有一条路径，幂等语义也
   只有一份。副作用工具在协议层用 JSON-RPC 的 request id 派生幂等键，
   跨进程重试因此不会重复扣款。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .logging_setup import get_logger, log_event
from .tools import ToolRegistry

__all__ = [
    "MCPServer",
    "A2ANode",
    "AgentCard",
    "A2ATask",
    "Artifact",
    "ProtocolProfile",
    "PROTOCOL_PROFILES",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "LATEST_PROTOCOL_VERSION",
    "JSONRPC_ERRORS",
]

log = get_logger("mcp")

# --------------------------------------------------------------------------- JSON-RPC

#: JSON-RPC 2.0 的标准错误码。协议级失败一律用这里的码。
JSONRPC_ERRORS: dict[str, int] = {
    "parse_error": -32700,
    "invalid_request": -32600,
    "method_not_found": -32601,
    "invalid_params": -32602,
    "internal_error": -32603,
    #: -32000 到 -32099 是留给实现自定义的服务器错误。
    "not_initialized": -32002,
}


class _JsonRpcError(Exception):
    """协议级错误。会被翻译成 JSON-RPC 的 ``error`` 对象。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# --------------------------------------------------------------------------- 协议版本


@dataclass(frozen=True)
class ProtocolProfile:
    """某个协议版本下的行为差异。

    版本号是日期串（``YYYY-MM-DD``），所以字典序比较就是时间序比较——
    版本判断可以直接写 ``profile.version >= "2025-11-25"``。

    字段：

    - ``tool_input_error_is_execution_error``：工具**参数取值**不合法时，
      报成"工具执行错误"（结果里 ``isError: true``）还是"协议错误"。
      2025-11-25 起改成执行错误，好让模型能看着错误改参数重试。
    - ``supports_structured_content``：结果里能不能带 ``structuredContent``。
      2025-06-18 起支持。
    - ``supports_batching``：请求能不能是 JSON-RPC 数组（批处理）。
      2025-03-26 加进来，2025-06-18 又去掉。
    """

    version: str
    tool_input_error_is_execution_error: bool = False
    supports_structured_content: bool = False
    supports_batching: bool = False


#: 每个协议版本的行为差异表。**新版本来了改这张表，不改流程。**
#:
#: 官方 schema 目录里目前有五个定版修订（外加一个 draft）：``2024-11-05``、
#: ``2025-03-26``、``2025-06-18``、``2025-11-25``、``2026-07-28``。
#: 本表覆盖的是**握手家族**的四个修订——``2026-07-28`` 是无状态修订，
#: 取消了初始化握手，与本实现的会话模型不同，所以**不在支持集合里**。
PROTOCOL_PROFILES: dict[str, ProtocolProfile] = {
    "2024-11-05": ProtocolProfile(
        version="2024-11-05",
        tool_input_error_is_execution_error=False,
        supports_structured_content=False,
        supports_batching=False,
    ),
    "2025-03-26": ProtocolProfile(
        version="2025-03-26",
        tool_input_error_is_execution_error=False,
        supports_structured_content=False,
        supports_batching=True,
    ),
    "2025-06-18": ProtocolProfile(
        version="2025-06-18",
        tool_input_error_is_execution_error=False,
        supports_structured_content=True,
        supports_batching=False,
    ),
    "2025-11-25": ProtocolProfile(
        version="2025-11-25",
        tool_input_error_is_execution_error=True,
        supports_structured_content=True,
        supports_batching=False,
    ),
}

#: 本实现能说的版本集合。**按官方 schema 目录里的定版修订维护**，
#: 写稿时要按写作时点复核——规范版本是滚动的，不是一次性写死的常量。
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = tuple(sorted(PROTOCOL_PROFILES))

#: 兜底版本：客户端要了一个我们不认识的版本时，回这个。
LATEST_PROTOCOL_VERSION: str = SUPPORTED_PROTOCOL_VERSIONS[-1]


# --------------------------------------------------------------------------- MCP server


class MCPServer:
    """把工具注册表暴露为 MCP server（JSON-RPC over stdio）。

    服务器是一层**薄适配层**：工具的校验与执行仍然走 ``tools.py``。
    协议层做的事只有四件——认方法、翻译参数、翻译结果、翻译错误。

    一次会话的生命周期：

    1. 客户端发 ``initialize``，带上它支持的 ``protocolVersion``；
    2. 服务器在 ``SUPPORTED_PROTOCOL_VERSIONS`` 里挑一个版本回给他
       （挑不到就回自己最新的，由客户端决定要不要继续）；
    3. 客户端发 ``notifications/initialized``（通知，不回响应）；
    4. 之后才是 ``tools/list`` 与 ``tools/call``。

    ``require_initialize=False`` 可以让服务器跳过握手直接干活——
    嵌入式场景（同一个进程里换个协议壳）常用，但会失去版本协商。
    """

    def __init__(
        self,
        tools: ToolRegistry,
        name: str,
        version: str,
        *,
        supported_versions: tuple[str, ...] | None = None,
        require_initialize: bool = True,
    ) -> None:
        self.tools = tools
        self.name = name
        self.version = version
        self.supported_versions = tuple(supported_versions or SUPPORTED_PROTOCOL_VERSIONS)
        self.require_initialize = require_initialize

        # 会话状态。stdio 下一个进程就是一段会话。
        self.initialized: bool = False
        self.negotiated_version: str = LATEST_PROTOCOL_VERSION
        self.client_info: dict[str, Any] = {}

    # ------------------------------------------------------------------ 入口

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """处理单条 JSON-RPC 请求，返回单条 JSON-RPC 响应。

        通知（没有 ``id`` 的消息）按 JSON-RPC 规范**不返回响应**——
        这里返回空字典，``serve_stdio`` 见到空字典就不往回写。
        """
        if not isinstance(request, dict):
            return self._error(None, JSONRPC_ERRORS["invalid_request"],
                               "请求必须是 JSON 对象")
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str) or not method:
            return self._error(request_id, JSONRPC_ERRORS["invalid_request"],
                               "请求缺少 method")
        if request.get("jsonrpc") != "2.0":
            return self._error(request_id, JSONRPC_ERRORS["invalid_request"],
                               'jsonrpc 字段必须是 "2.0"')
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return self._error(request_id, JSONRPC_ERRORS["invalid_params"],
                               "params 必须是对象")
        try:
            result = self._dispatch(method, params, request_id)
        except _JsonRpcError as exc:
            return self._error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # 内部意外也不能让会话崩掉
            log_event(log, "mcp.internal_error", level=40,
                      method=method, error=f"{type(exc).__name__}: {exc}")
            return self._error(request_id, JSONRPC_ERRORS["internal_error"],
                               f"{type(exc).__name__}: {exc}")
        if request_id is None:
            return {}  # 通知：不回响应
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def handle_any(self, message: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
        """处理一条消息：单条请求、通知，或者批处理数组。

        返回 ``None`` 表示"没有响应可写"（通知）。
        """
        if isinstance(message, list):
            profile = self._profile()
            if not profile.supports_batching:
                return self._error(None, JSONRPC_ERRORS["invalid_request"],
                                   f"{profile.version} 不支持 JSON-RPC 批处理")
            responses = []
            for item in message:
                response = self.handle(item)
                if response:
                    responses.append(response)
            return responses
        response = self.handle(message)
        return response or None

    def serve_stdio(self, stdin: Any = None, stdout: Any = None) -> int:
        """读 stdin 写 stdout：每行一条 JSON-RPC 消息。

        **stdout 只跑协议帧。** 任何一行多余输出都会让客户端的解析错位，
        所以日志必须走 stderr。

        读到 EOF 返回 0。
        """
        stdin = stdin if stdin is not None else sys.stdin
        stdout = stdout if stdout is not None else sys.stdout
        self._force_utf8(stdin)
        self._force_utf8(stdout)
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                response: Any = self._error(
                    None, JSONRPC_ERRORS["parse_error"], f"JSON 解析失败：{exc}")
            else:
                response = self.handle_any(message)
            if not response:
                continue
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
        return 0

    # ------------------------------------------------------------------ 派发

    def _dispatch(self, method: str, params: dict[str, Any],
                  request_id: Any) -> dict[str, Any]:
        if method == "initialize":
            return self._mcp_initialize(params)
        if method in ("notifications/initialized", "initialized"):
            return {}
        if method == "ping":
            return {}
        if self.require_initialize and not self.initialized:
            raise _JsonRpcError(
                JSONRPC_ERRORS["not_initialized"],
                "尚未 initialize：先发 initialize 再发别的请求",
            )
        if method == "tools/list":
            return self._mcp_tools_list(params)
        if method == "tools/call":
            return self._mcp_tools_call(params, request_id)
        raise _JsonRpcError(JSONRPC_ERRORS["method_not_found"], f"未知方法：{method}")

    def _mcp_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """握手：协商协议版本，对齐能力。"""
        requested = params.get("protocolVersion")
        if isinstance(requested, str) and requested in self.supported_versions:
            negotiated = requested
        else:
            # 客户端要了一个我们不认识的版本：回自己最新的那个，
            # 由**客户端**决定要不要继续。这是版本演进的正确接法。
            negotiated = self.supported_versions[-1] if self.supported_versions \
                else LATEST_PROTOCOL_VERSION
        self.negotiated_version = negotiated
        self.initialized = True
        self.client_info = dict(params.get("clientInfo") or {})
        log_event(log, "mcp.initialized", protocol_version=negotiated,
                  requested=requested, client=self.client_info.get("name"))
        return {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }

    def _mcp_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """列工具：schema 直接来自注册表，**不维护第二份**。

        内部叫 ``parameters``，MCP 线格式叫 ``inputSchema``——同一个
        JSON Schema，换了个字段名。
        """
        tools = []
        for schema in self.tools.schemas():
            tools.append({
                "name": schema["name"],
                "description": schema["description"],
                "inputSchema": schema["parameters"],
            })
        return {"tools": tools}

    def _mcp_tools_call(self, params: dict[str, Any], request_id: Any) -> dict[str, Any]:
        """调工具：校验、幂等、执行，全部委托给注册表。

        错误分两条通道走：

        - **协议错误**（JSON-RPC ``error``）：请求结构不对、工具不存在。
        - **执行错误**（结果里 ``isError: true``）：工具存在，但参数取值
          不对或者工具自己抛了错——这类错误模型看得懂、能自己改。
        """
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _JsonRpcError(JSONRPC_ERRORS["invalid_params"],
                                "tools/call 缺少 name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise _JsonRpcError(JSONRPC_ERRORS["invalid_params"],
                                "arguments 必须是对象")
        if request_id is None:
            raise _JsonRpcError(JSONRPC_ERRORS["invalid_request"],
                                "tools/call 必须是请求（带 id），通知无法回结果")
        if name not in self.tools.names:
            raise _JsonRpcError(JSONRPC_ERRORS["invalid_params"],
                                f"未知工具：{name}", {"available": self.tools.names})

        spec, _fn = self.tools.get(name)
        profile = self._profile()

        # 先做一次**输入校验**，只为分清"参数取值不对"和"工具执行失败"
        # 这两种错误——它们该走不同的通道。校验逻辑仍然只有 tools.py 一份。
        input_error = self.tools.validate(spec, arguments)
        if input_error and not profile.tool_input_error_is_execution_error:
            raise _JsonRpcError(JSONRPC_ERRORS["invalid_params"],
                                f"{name} 参数不合法：{input_error}")

        # 副作用工具的幂等键：拿 JSON-RPC 的 request id 派生。
        # 同一个 id 重试就是同一次动作，跨进程也不会重复执行。
        key = f"mcp:{name}:{request_id}" if spec.side_effect else None
        result = self.tools.invoke(name, arguments, idempotency_key=key)

        if not result.ok:
            text = f"[工具 {name}] {result.error}"
            log_event(log, "mcp.tool_failed", tool=name, error=result.error)
            return {"content": [{"type": "text", "text": text}], "isError": True}

        payload: dict[str, Any] = {
            "content": [{"type": "text", "text": result.render()}],
            "isError": False,
        }
        if profile.supports_structured_content and isinstance(result.output, (dict, list)):
            payload["structuredContent"] = result.output
        return payload

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _force_utf8(stream: Any) -> None:
        """把标准流切到 UTF-8。

        协议帧里全是中文工具描述，而 Windows 上管道默认走系统代码页
        （GBK/CP936）——不切 UTF-8，客户端读到的是乱码。这一步不能省。
        """
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            return
        try:
            reconfigure(encoding="utf-8")
        except Exception:  # pragma: no cover - 某些流不支持重配
            pass

    def _profile(self) -> ProtocolProfile:
        return PROTOCOL_PROFILES.get(
            self.negotiated_version,
            ProtocolProfile(version=self.negotiated_version),
        )

    @staticmethod
    def _error(request_id: Any, code: int, message: str,
               data: Any = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


# --------------------------------------------------------------------------- A2A


@dataclass
class AgentCard:
    """A2A 的能力名片：这个 Agent 是谁、能干什么、在哪。

    卡片是**承诺不是保证**——它描述能力，不描述质量、延迟与失败行为。
    字段名随 A2A 修订演进，写的时候要照着你要接入的 SDK 版本核对。
    """

    name: str
    description: str
    url: str
    version: str
    capabilities: dict[str, bool] = field(default_factory=lambda: {
        "streaming": False, "pushNotifications": False,
    })
    skills: list[dict[str, Any]] = field(default_factory=list)
    default_input_modes: list[str] = field(default_factory=lambda: ["text/plain"])
    default_output_modes: list[str] = field(default_factory=lambda: ["text/plain"])
    protocol_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """线格式：字段名是 camelCase。"""
        card: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "capabilities": dict(self.capabilities),
            "defaultInputModes": list(self.default_input_modes),
            "defaultOutputModes": list(self.default_output_modes),
            "skills": [dict(skill) for skill in self.skills],
        }
        if self.protocol_version:
            card["protocolVersion"] = self.protocol_version
        return card


@dataclass
class Artifact:
    """任务的产出物。

    产出物**独立于消息存在**：消息是过程（发完就没意义了），
    产出物是结果（要能存档、引用、下载、算作交付物）。
    """

    id: str
    name: str
    parts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifactId": self.id,
            "name": self.name,
            "parts": [dict(part) for part in self.parts],
        }


@dataclass
class A2ATask:
    """一次委托。

    状态机：``submitted → working → completed / failed / canceled``，
    还有 ``input-required``（等你补信息）。任务用 ``context_id`` 把同一段
    上下文下的多次委托串起来。
    """

    id: str
    context_id: str
    state: str = "submitted"
    history: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    status_updates: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    #: 终态集合。终态的任务不能再推进。
    TERMINAL_STATES = ("completed", "failed", "canceled")

    def is_terminal(self) -> bool:
        return self.state in self.TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.state}
        if self.status_updates:
            last = self.status_updates[-1]
            status["message"] = last.get("message")
            status["timestamp"] = last.get("timestamp")
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": [dict(item) for item in self.history],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


class A2ANode:
    """A2A 节点：接受别人的委托，把一件事办完，产出 artifact。

    委托与本地调用最大的差别是**它有生命周期**：

    - ``SendMessage`` 立刻返回一个 ``submitted`` 的任务，真正的工作在后台跑；
    - 客户端用 ``GetTask`` 轮询，或者用 ``CancelTask`` 取消；
    - 任务完成后 ``artifacts`` 里躺着交付物——那才是"办完了"的凭证。

    和 MCP 的关系：MCP 是**上下文供给层**（工具从哪来），
    A2A 是**委托执行层**（一整件事交出去）。两层互不感知。

    ``delegate`` 收到任务文本，返回一个答案字符串，或者一个带
    ``answer`` / ``status`` 属性的对象（比如 ``AgentResult``）。
    """

    def __init__(
        self,
        card: AgentCard,
        delegate: Callable[[str], Any],
        *,
        id_factory: Callable[[str, int], str] | None = None,
    ) -> None:
        self.card = card
        self.delegate = delegate
        self._id_factory = id_factory or (lambda prefix, seq: f"{prefix}-{seq}")
        self._tasks: dict[str, A2ATask] = {}
        self._order: list[str] = []
        self._counter = 0
        self._lock = threading.Lock()

    def _next_id(self, prefix: str) -> str:
        """生成节点内唯一的 id。

        序号保证同一节点内的 id 不重复——用时间戳做 id 会在同一毫秒内
        撞车，而任务 id 撞车意味着"委托一件都不见了"。
        """
        self._counter += 1
        return self._id_factory(prefix, self._counter)

    # ------------------------------------------------------------------ 名片

    def agent_card(self) -> dict[str, Any]:
        """公开名片。真实部署里它挂在一个 well-known 路径下。

        路径随 A2A 修订变过（``agent-card.json`` 与 ``agent.json`` 都有人用），
        接入前照着你用的 SDK 版本核对。
        """
        return self.card.to_dict()

    def status_updates(self, task_id: str) -> list[dict[str, Any]]:
        """某个任务的状态推进轨迹。

        真实 A2A 里这些更新是**推**给客户端的（流式或推送通知），
        这里留在本地供复盘——状态机走过的每一步都得留痕。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return []
        return [dict(update) for update in task.status_updates]

    # ------------------------------------------------------------------ 入口

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """处理一条 A2A JSON-RPC 请求。

        方法名用 A2A v1.0 的 PascalCase 形式（``SendMessage``、``GetTask``…），
        不是 v0.x 时代的斜杠形式。
        """
        if not isinstance(request, dict):
            return self._error(None, JSONRPC_ERRORS["invalid_request"],
                               "请求必须是 JSON 对象")
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str) or not method:
            return self._error(request_id, JSONRPC_ERRORS["invalid_request"],
                               "请求缺少 method")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return self._error(request_id, JSONRPC_ERRORS["invalid_params"],
                               "params 必须是对象")
        try:
            result = self._dispatch(method, params)
        except _JsonRpcError as exc:
            return self._error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:
            log_event(log, "a2a.internal_error", level=40,
                      method=method, error=f"{type(exc).__name__}: {exc}")
            return self._error(request_id, JSONRPC_ERRORS["internal_error"],
                               f"{type(exc).__name__}: {exc}")
        if request_id is None:
            return {}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "SendMessage":
            return self._send_message(params)
        if method == "GetTask":
            return self._get_task(params)
        if method == "CancelTask":
            return self._cancel_task(params)
        if method == "ListTasks":
            return self._list_tasks(params)
        if method in ("GetAgentCard", "GetExtendedAgentCard"):
            return self.agent_card()
        raise _JsonRpcError(JSONRPC_ERRORS["method_not_found"], f"未知方法：{method}")

    # ------------------------------------------------------------------ 方法

    def _send_message(self, params: dict[str, Any]) -> dict[str, Any]:
        """接收一条委托消息，起一个后台任务。"""
        message = params.get("message") or {}
        text = self._text_of(message)
        if not text:
            raise _JsonRpcError(JSONRPC_ERRORS["invalid_params"],
                                "message 里没有文本内容")
        with self._lock:
            task_id = self._next_id("task")
            context_id = params.get("contextId") or self._next_id("ctx")
            task = A2ATask(id=task_id, context_id=context_id, state="submitted")
            task.history.append(self._message("user", text, task_id, context_id))
            self._tasks[task_id] = task
            self._order.append(task_id)
            self._push_status(task, "submitted", "已接收委托")
        worker = threading.Thread(target=self._run_task, args=(task_id, text),
                                  name=f"a2a-{task_id}", daemon=True)
        worker.start()
        return task.to_dict()

    def _get_task(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = params.get("id")
        task = self._tasks.get(task_id) if isinstance(task_id, str) else None
        if task is None:
            raise _JsonRpcError(JSONRPC_ERRORS["invalid_params"],
                                f"未知任务：{task_id}")
        return task.to_dict()

    def _cancel_task(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = params.get("id")
        task = self._tasks.get(task_id) if isinstance(task_id, str) else None
        if task is None:
            raise _JsonRpcError(JSONRPC_ERRORS["invalid_params"],
                                f"未知任务：{task_id}")
        with self._lock:
            if not task.is_terminal():
                task.state = "canceled"
                task.updated_at = time.time()
                self._push_status(task, "canceled", "委托已取消")
        return task.to_dict()

    def _list_tasks(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            tasks = [self._tasks[task_id].to_dict() for task_id in self._order]
        return {"tasks": tasks}

    # ------------------------------------------------------------------ 执行

    def _run_task(self, task_id: str, text: str) -> None:
        """后台跑一件事，把结果变成 artifact。"""
        task = self._tasks.get(task_id)
        if task is None:
            return
        with self._lock:
            if task.state == "canceled":
                return
            task.state = "working"
            task.updated_at = time.time()
            self._push_status(task, "working", "开始处理委托")
        try:
            outcome = self.delegate(text)
        except Exception as exc:
            with self._lock:
                task.state = "failed"
                task.updated_at = time.time()
                self._push_status(task, "failed", f"{type(exc).__name__}: {exc}")
                task.history.append(self._message("agent", f"委托失败：{exc}",
                                                  task_id, task.context_id))
            log_event(log, "a2a.task_failed", task_id=task_id,
                      error=f"{type(exc).__name__}: {exc}")
            return
        answer, status = self._outcome(outcome)
        with self._lock:
            if task.state == "canceled":
                return
            if status == "completed":
                task.state = "completed"
                artifact = Artifact(
                    id=self._next_id("art"),
                    name="answer",
                    parts=[{"kind": "text", "text": answer}],
                )
                task.artifacts.append(artifact)
                self._push_status(task, "completed", f"已产出 artifact {artifact.id}")
            else:
                task.state = "failed"
                self._push_status(task, "failed", f"委托未完成：{status}")
            task.updated_at = time.time()
            task.history.append(self._message("agent", answer, task_id, task.context_id))

    @staticmethod
    def _outcome(outcome: Any) -> tuple[str, str]:
        """把 delegate 的返回值归一成 ``(答案, 状态)``。"""
        if isinstance(outcome, str):
            return outcome, "completed"
        answer = getattr(outcome, "answer", "")
        status = getattr(outcome, "status", "completed")
        return str(answer), str(status)

    @staticmethod
    def _text_of(message: dict[str, Any]) -> str:
        parts = message.get("parts") or []
        chunks = []
        for part in parts:
            if isinstance(part, dict) and part.get("kind") == "text":
                chunks.append(str(part.get("text") or ""))
        return "\n".join(chunks).strip()

    @staticmethod
    def _message(role: str, text: str, task_id: str, context_id: str) -> dict[str, Any]:
        return {
            "role": role,
            "parts": [{"kind": "text", "text": text}],
            "taskId": task_id,
            "contextId": context_id,
        }

    def _push_status(self, task: A2ATask, state: str, message: str) -> None:
        task.status_updates.append({
            "state": state,
            "message": {"role": "agent", "parts": [{"kind": "text", "text": message}]},
            "timestamp": time.time(),
        })

    @staticmethod
    def _error(request_id: Any, code: int, message: str,
               data: Any = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


# --------------------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    """``python -m dragon_agent.mcp_server``：用默认工具起一个 stdio server。"""
    from .tools import default_registry

    argv = list(sys.argv[1:] if argv is None else argv)
    name = "dragon-agent-mcp"
    version = "1.0"
    for i, arg in enumerate(argv):
        if arg == "--name" and i + 1 < len(argv):
            name = argv[i + 1]
        if arg == "--version" and i + 1 < len(argv):
            version = argv[i + 1]
    server = MCPServer(default_registry(), name, version)
    log_event(log, "mcp.serve_stdio", name=name, version=version)
    return server.serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
