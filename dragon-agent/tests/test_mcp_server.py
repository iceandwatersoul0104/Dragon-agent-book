"""MCP server 与 A2A 节点的契约测试。

这个文件守的是 ``mcp_server.py`` docstring 里写死的几条：

- 协议层是**薄适配层**：schema 只有一份（来自注册表），执行只有一条路径
  （还是注册表那条）；
- **两种失败两条通道**：请求不合法 → JSON-RPC ``error``；工具跑过了但失败
  → 结果里 ``isError: true``；
- 版本差异**集中在表里**，不在流程里；
- 通知（没有 ``id``）**不返回响应**；
- stdout **只跑协议帧**。

全部离线：不联网、不需要 API Key、不写用户目录。
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.mcp_server import (
    A2ANode,
    AgentCard,
    Artifact,
    JSONRPC_ERRORS,
    LATEST_PROTOCOL_VERSION,
    PROTOCOL_PROFILES,
    SUPPORTED_PROTOCOL_VERSIONS,
    MCPServer,
)
from dragon_agent.tools import ToolRegistry, ToolSpec, default_registry


def request(method: str, request_id: int | None = 1, **params) -> dict:
    """造一条 JSON-RPC 请求。``request_id=None`` 表示通知。"""
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        msg["id"] = request_id
    if params:
        msg["params"] = params
    return msg


class _Registry(ToolRegistry):
    """记调用次数的注册表——用来证明"只有一条执行路径"。"""

    def __init__(self, source: ToolRegistry | None = None) -> None:
        super().__init__()
        self.counts: dict[str, int] = {}
        if source is not None:
            for name in source.names:
                spec, fn = source.get(name)
                self.register(spec, fn)

    def invoke(self, name, args, idempotency_key=None):
        self.counts[name] = self.counts.get(name, 0) + 1
        return super().invoke(name, args, idempotency_key=idempotency_key)


def build_server(reg: ToolRegistry | None = None, **kwargs) -> MCPServer:
    server = MCPServer(reg if reg is not None else default_registry(),
                       "dragon-test", "1.0", **kwargs)
    return server


def initialized(reg: ToolRegistry | None = None, **kwargs) -> MCPServer:
    """起一个已经握过手的 server。"""
    server = build_server(reg, **kwargs)
    server.handle(request("initialize", 0, protocolVersion=LATEST_PROTOCOL_VERSION,
                          clientInfo={"name": "test", "version": "1"}))
    return server


# --------------------------------------------------------------------------- 握手


class TestHandshake(unittest.TestCase):
    def test_initialize_returns_version_capabilities_and_server_info(self):
        """握手结果有三样：协议版本、能力、服务器身份。"""
        server = build_server()
        resp = server.handle(request("initialize", 1,
                                     protocolVersion=LATEST_PROTOCOL_VERSION,
                                     clientInfo={"name": "c", "version": "1"}))
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)
        result = resp["result"]
        self.assertEqual(result["protocolVersion"], LATEST_PROTOCOL_VERSION)
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "dragon-test")
        self.assertEqual(result["serverInfo"]["version"], "1.0")

    def test_client_version_is_echoed_when_supported(self):
        """客户端要的版本我们支持，就原样回它。"""
        server = build_server()
        for version in SUPPORTED_PROTOCOL_VERSIONS:
            resp = server.handle(request("initialize", 1, protocolVersion=version))
            self.assertEqual(resp["result"]["protocolVersion"], version)

    def test_unknown_client_version_falls_back_to_our_latest(self):
        """客户端要了一个不认识的版本：回我们最新的，**不**拒绝。

        拒绝等于把客户端挡在门外；回一个版本让**客户端**决定要不要继续，
        才是版本演进的正确接法。
        """
        server = build_server()
        resp = server.handle(request("initialize", 1, protocolVersion="1999-01-01"))
        self.assertEqual(resp["result"]["protocolVersion"], LATEST_PROTOCOL_VERSION)
        self.assertNotIn("error", resp)

    def test_initialize_marks_session_initialized(self):
        server = build_server()
        self.assertFalse(server.initialized)
        server.handle(request("initialize", 1, protocolVersion=LATEST_PROTOCOL_VERSION))
        self.assertTrue(server.initialized)

    def test_tools_call_before_initialize_is_rejected(self):
        """没握手就调工具 → 协议错误，不是崩溃。"""
        server = build_server()
        resp = server.handle(request("tools/call", 1, name="add",
                                     arguments={"a": 1, "b": 2}))
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["not_initialized"])

    def test_require_initialize_can_be_disabled(self):
        """嵌入式场景可以跳过握手。"""
        server = build_server(require_initialize=False)
        resp = server.handle(request("tools/list", 1))
        self.assertIn("result", resp)

    def test_notification_gets_no_response(self):
        """通知没有 id，按 JSON-RPC 规范不回响应。"""
        server = build_server()
        resp = server.handle(request("notifications/initialized", None))
        self.assertEqual(resp, {})


# --------------------------------------------------------------------------- tools/list


class TestToolsList(unittest.TestCase):
    def test_schema_comes_from_the_registry_verbatim(self):
        """schema 只有一份：内部叫 parameters，线上叫 inputSchema。"""
        reg = default_registry()
        server = initialized(reg)
        resp = server.handle(request("tools/list", 1))
        tools = resp["result"]["tools"]
        internal = {schema["name"]: schema for schema in reg.schemas()}
        self.assertEqual(len(tools), len(internal))
        for tool in tools:
            self.assertEqual(sorted(tool), ["description", "inputSchema", "name"])
            self.assertEqual(tool["inputSchema"], internal[tool["name"]]["parameters"])

    def test_list_is_deterministic(self):
        """两次 list 结果一致——模型看到的工具清单不能飘。"""
        server = initialized()
        first = server.handle(request("tools/list", 1))["result"]
        second = server.handle(request("tools/list", 2))["result"]
        self.assertEqual(first, second)


# --------------------------------------------------------------------------- tools/call


class TestToolsCall(unittest.TestCase):
    def test_successful_call_returns_text_content(self):
        server = initialized()
        resp = server.handle(request("tools/call", 1, name="add",
                                     arguments={"a": 2, "b": 3}))
        result = resp["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(result["content"][0]["text"], "5")

    def test_unknown_tool_is_a_protocol_error(self):
        """工具不存在 → 协议错误（调用方连工具清单都没看对）。"""
        server = initialized()
        resp = server.handle(request("tools/call", 1, name="nope", arguments={}))
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["invalid_params"])
        self.assertIn("available", resp["error"]["data"])

    def test_missing_name_is_a_protocol_error(self):
        server = initialized()
        resp = server.handle(request("tools/call", 1, arguments={}))
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["invalid_params"])

    def test_non_object_arguments_is_a_protocol_error(self):
        server = initialized()
        resp = server.handle(request("tools/call", 1, name="add", arguments="1,2"))
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["invalid_params"])

    def test_tool_error_is_an_execution_error_not_a_protocol_error(self):
        """工具跑过了但失败 → isError，让模型看得懂、能自己改。

        这是本章最关键的一条分界线：能纠正的失败**不能**用协议错误表达，
        否则模型看不到该改什么。
        """
        server = initialized()
        resp = server.handle(request("tools/call", 1, name="read_text_file",
                                     arguments={"path": "不存在的文件.txt"}))
        self.assertNotIn("error", resp)
        result = resp["result"]
        self.assertTrue(result["isError"])
        self.assertIn("read_text_file", result["content"][0]["text"])

    def test_validation_error_is_execution_error_on_new_protocol(self):
        """新协议版本下，参数取值不对也是执行错误。"""
        server = initialized()
        resp = server.handle(request("tools/call", 1, name="add", arguments={"a": 1}))
        self.assertNotIn("error", resp)
        self.assertTrue(resp["result"]["isError"])

    def test_validation_error_is_protocol_error_on_old_protocol(self):
        """老协议版本下，同样的参数错误报成协议错误——差异只在表里。"""
        server = build_server()
        server.handle(request("initialize", 1, protocolVersion="2024-11-05"))
        resp = server.handle(request("tools/call", 1, name="add", arguments={"a": 1}))
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["invalid_params"])

    def test_tool_call_requires_an_id(self):
        """通知形式调工具没法回结果，直接拒绝。"""
        server = initialized()
        resp = server.handle(request("tools/call", None, name="add",
                                     arguments={"a": 1, "b": 2}))
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["invalid_request"])

    def test_execution_goes_through_the_registry_only(self):
        """执行只有一条路径：注册表。协议层没有第二套实现。"""
        reg = _Registry(default_registry())
        server = initialized(reg)
        server.handle(request("tools/call", 1, name="add", arguments={"a": 1, "b": 1}))
        self.assertEqual(reg.counts.get("add"), 1)
        self.assertEqual(reg.invocations, ["add"])


# --------------------------------------------------------------------------- 幂等


class TestIdempotency(unittest.TestCase):
    def test_same_request_id_does_not_execute_side_effect_twice(self):
        """同一个 request id 重试 → 不可重放的工具拒绝重复执行。

        幂等键由 request id 派生，所以跨进程重试不会重复扣款。
        """
        reg = _Registry(default_registry())
        server = initialized(reg)
        first = server.handle(request("tools/call", 7, name="submit_order",
                                      arguments={"sku": "SKU-1", "qty": 1}))
        second = server.handle(request("tools/call", 7, name="submit_order",
                                       arguments={"sku": "SKU-1", "qty": 1}))
        self.assertFalse(first["result"]["isError"])
        self.assertTrue(second["result"]["isError"])
        self.assertEqual(reg.counts.get("submit_order"), 2)  # 两次进注册表
        self.assertEqual(reg.invocations.count("submit_order"), 1)  # 只真执行一次

    def test_different_request_id_executes_again(self):
        """不同的 id 是不同的动作，该执行就执行。"""
        reg = _Registry(default_registry())
        server = initialized(reg)
        server.handle(request("tools/call", 1, name="submit_order",
                              arguments={"sku": "SKU-1", "qty": 1}))
        server.handle(request("tools/call", 2, name="submit_order",
                              arguments={"sku": "SKU-2", "qty": 1}))
        self.assertEqual(reg.invocations.count("submit_order"), 2)

    def test_read_only_tool_needs_no_idempotency_key(self):
        """只读工具不生成幂等键。"""
        reg = _Registry(default_registry())
        server = initialized(reg)
        server.handle(request("tools/call", 1, name="add", arguments={"a": 1, "b": 1}))
        self.assertEqual(reg.invocations, ["add"])


# --------------------------------------------------------------------------- 结构化输出


class TestStructuredContent(unittest.TestCase):
    def test_structured_content_only_on_versions_that_support_it(self):
        """2025-06-18 起才有 structuredContent。"""
        reg = default_registry()
        server = build_server(reg)
        server.handle(request("initialize", 1, protocolVersion="2025-06-18"))
        resp = server.handle(request("tools/call", 1, name="submit_order",
                                     arguments={"sku": "S", "qty": 2}))
        self.assertIn("structuredContent", resp["result"])

    def test_no_structured_content_on_old_versions(self):
        reg = default_registry()
        server = build_server(reg)
        server.handle(request("initialize", 1, protocolVersion="2025-03-26"))
        resp = server.handle(request("tools/call", 1, name="submit_order",
                                     arguments={"sku": "S", "qty": 2}))
        self.assertNotIn("structuredContent", resp["result"])


# --------------------------------------------------------------------------- 传输


class TestTransport(unittest.TestCase):
    def test_stdio_serves_one_response_per_line(self):
        """stdio：每行一条消息，每行一条响应。"""
        server = initialized()
        stdin = io.StringIO(
            json.dumps(request("tools/list", 1)) + "\n"
            + json.dumps(request("tools/call", 2, name="add",
                                 arguments={"a": 1, "b": 2})) + "\n"
        )
        stdout = io.StringIO()
        code = server.serve_stdio(stdin, stdout)
        self.assertEqual(code, 0)
        lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        self.assertEqual(first["id"], 1)
        self.assertEqual(second["id"], 2)
        self.assertEqual(second["result"]["content"][0]["text"], "3")

    def test_stdout_carries_protocol_frames_only(self):
        """stdout 上不能混进别的输出——那会把客户端的解析带偏。"""
        server = initialized()
        stdin = io.StringIO(json.dumps(request("tools/list", 1)) + "\n")
        stdout = io.StringIO()
        server.serve_stdio(stdin, stdout)
        for line in stdout.getvalue().splitlines():
            if line.strip():
                json.loads(line)  # 每一行都必须是合法 JSON

    def test_invalid_json_line_yields_parse_error(self):
        server = initialized()
        stdin = io.StringIO("这不是 JSON\n")
        stdout = io.StringIO()
        server.serve_stdio(stdin, stdout)
        resp = json.loads(stdout.getvalue().strip())
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["parse_error"])
        self.assertIsNone(resp["id"])

    def test_blank_lines_are_skipped(self):
        server = initialized()
        stdin = io.StringIO("\n\n" + json.dumps(request("tools/list", 1)) + "\n\n")
        stdout = io.StringIO()
        server.serve_stdio(stdin, stdout)
        self.assertEqual(len(stdout.getvalue().strip().splitlines()), 1)

    def test_notification_on_stdio_produces_no_output(self):
        server = build_server()
        stdin = io.StringIO(json.dumps(request("notifications/initialized", None)) + "\n")
        stdout = io.StringIO()
        server.serve_stdio(stdin, stdout)
        self.assertEqual(stdout.getvalue(), "")

    def test_batching_only_on_versions_that_support_it(self):
        """2025-03-26 有批处理，2025-06-18 之后没了。"""
        server = build_server()
        server.handle(request("initialize", 1, protocolVersion="2025-03-26"))
        resp = server.handle_any([
            request("tools/list", 1),
            request("tools/list", 2),
        ])
        self.assertIsInstance(resp, list)
        self.assertEqual(len(resp), 2)

        server2 = build_server()
        server2.handle(request("initialize", 1, protocolVersion="2025-06-18"))
        resp2 = server2.handle_any([request("tools/list", 1)])
        self.assertNotIsInstance(resp2, list)
        self.assertIn("error", resp2)


# --------------------------------------------------------------------------- 协议错误


class TestProtocolErrors(unittest.TestCase):
    def test_unknown_method(self):
        server = initialized()
        resp = server.handle(request("resources/list", 1))
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["method_not_found"])

    def test_missing_method(self):
        server = initialized()
        resp = server.handle({"jsonrpc": "2.0", "id": 1})
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["invalid_request"])

    def test_wrong_jsonrpc_version(self):
        server = initialized()
        resp = server.handle({"jsonrpc": "1.0", "id": 1, "method": "tools/list"})
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["invalid_request"])

    def test_non_object_request(self):
        server = initialized()
        resp = server.handle(["not", "an", "object"])
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["invalid_request"])

    def test_error_response_shape(self):
        """error 对象至少有 code 与 message。"""
        server = initialized()
        resp = server.handle(request("resources/list", 1))
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)
        self.assertIn("code", resp["error"])
        self.assertIn("message", resp["error"])


# --------------------------------------------------------------------------- 版本表


class TestProtocolProfiles(unittest.TestCase):
    def test_every_supported_version_has_a_profile(self):
        """版本表覆盖全部支持版本——差异不能藏在流程里。"""
        for version in SUPPORTED_PROTOCOL_VERSIONS:
            self.assertIn(version, PROTOCOL_PROFILES)

    def test_profile_version_matches_its_key(self):
        for key, profile in PROTOCOL_PROFILES.items():
            self.assertEqual(key, profile.version)

    def test_versions_are_date_stamped_and_sortable(self):
        """版本号是日期串，字典序就是时间序。"""
        for version in SUPPORTED_PROTOCOL_VERSIONS:
            self.assertRegex(version, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(list(SUPPORTED_PROTOCOL_VERSIONS),
                         sorted(SUPPORTED_PROTOCOL_VERSIONS))

    def test_newer_versions_enable_more(self):
        """版本越新，能力越多——表里的差异是单调的。"""
        ordered = sorted(PROTOCOL_PROFILES.values(), key=lambda p: p.version)
        for older, newer in zip(ordered, ordered[1:]):
            self.assertFalse(older.supports_structured_content
                             and not newer.supports_structured_content)
            self.assertFalse(older.tool_input_error_is_execution_error
                             and not newer.tool_input_error_is_execution_error)


# --------------------------------------------------------------------------- A2A


def build_node(delegate=None, **kwargs) -> A2ANode:
    card = AgentCard(
        name="dragon-test-agent",
        description="测试用的 A2A 节点",
        url="http://127.0.0.1:9999/",
        version="1.0",
        capabilities={"streaming": False, "pushNotifications": False},
        skills=[{"id": "echo", "name": "回声", "description": "把话原样说一遍",
                 "tags": ["demo"], "examples": ["你好"]}],
        **kwargs,
    )
    counter = {"n": 0}

    def default_delegate(text: str) -> str:
        counter["n"] += 1
        return f"收到：{text}"

    return A2ANode(card, delegate or default_delegate)


def wait_for(task_id: str, node: A2ANode, timeout: float = 5.0) -> dict:
    """轮询到任务进入终态。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = node.handle(request("GetTask", 1, id=task_id))
        task = resp["result"]
        if task["status"]["state"] in ("completed", "failed", "canceled"):
            return task
        time.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内没到终态")


class TestAgentCard(unittest.TestCase):
    def test_card_fields(self):
        card = build_node().agent_card()
        self.assertEqual(card["name"], "dragon-test-agent")
        self.assertEqual(card["url"], "http://127.0.0.1:9999/")
        self.assertIn("streaming", card["capabilities"])
        self.assertEqual(card["defaultInputModes"], ["text/plain"])
        self.assertEqual(card["skills"][0]["id"], "echo")

    def test_card_capabilities_match_implementation(self):
        """卡片是承诺不是保证——声明的能力必须和实现对得上。

        本实现不推流，卡片就不能写 streaming: true，否则客户端的进度条
        是死的。
        """
        node = build_node()
        self.assertFalse(node.agent_card()["capabilities"]["streaming"])


class TestA2ATask(unittest.TestCase):
    def test_send_message_returns_a_task(self):
        """SendMessage 立刻返回任务，不等活干完——委托是异步的。"""
        release = threading.Event()
        started = threading.Event()

        def slow(text: str) -> str:
            started.set()
            release.wait(timeout=5)
            return "好了"

        node = build_node(delegate=slow)
        resp = node.handle(request("SendMessage", 1, message={
            "role": "user", "parts": [{"kind": "text", "text": "算一下"}]}))
        task = resp["result"]
        self.assertTrue(task["id"])
        self.assertTrue(task["contextId"])
        # 返回时任务还没到终态——这才是"委托"与"函数调用"的差别。
        self.assertIn(task["status"]["state"], ("submitted", "working"))
        self.assertEqual(task["artifacts"], [])
        started.wait(timeout=5)
        release.set()
        wait_for(task["id"], node)

    def test_task_reaches_completed_and_produces_artifact(self):
        """办完了的凭证是 artifact，不是聊天记录。"""
        node = build_node()
        resp = node.handle(request("SendMessage", 1, message={
            "role": "user", "parts": [{"kind": "text", "text": "算一下"}]}))
        task_id = resp["result"]["id"]
        task = wait_for(task_id, node)
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(len(task["artifacts"]), 1)
        artifact = task["artifacts"][0]
        self.assertTrue(artifact["artifactId"])
        self.assertEqual(artifact["parts"][0]["text"], "收到：算一下")

    def test_history_records_both_sides(self):
        node = build_node()
        resp = node.handle(request("SendMessage", 1, message={
            "role": "user", "parts": [{"kind": "text", "text": "你好"}]}))
        task = wait_for(resp["result"]["id"], node)
        roles = [item["role"] for item in task["history"]]
        self.assertEqual(roles, ["user", "agent"])

    def test_failing_delegate_produces_failed_task(self):
        """委托失败 → failed 状态，不是崩溃。"""
        def boom(text: str) -> str:
            raise RuntimeError("下游挂了")

        node = build_node(delegate=boom)
        resp = node.handle(request("SendMessage", 1, message={
            "role": "user", "parts": [{"kind": "text", "text": "干活"}]}))
        task = wait_for(resp["result"]["id"], node)
        self.assertEqual(task["status"]["state"], "failed")
        self.assertEqual(task["artifacts"], [])

    def test_delegate_returning_agent_result_shape(self):
        """delegate 返回带 answer/status 的对象也能吃。"""
        class Outcome:
            answer = "算完了"
            status = "completed"

        node = build_node(delegate=lambda text: Outcome())
        resp = node.handle(request("SendMessage", 1, message={
            "role": "user", "parts": [{"kind": "text", "text": "算"}]}))
        task = wait_for(resp["result"]["id"], node)
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(task["artifacts"][0]["parts"][0]["text"], "算完了")

    def test_get_task_unknown_id_is_an_error(self):
        node = build_node()
        resp = node.handle(request("GetTask", 1, id="task-nope"))
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["invalid_params"])

    def test_cancel_task_marks_canceled(self):
        """取消是委托语义的一部分——本地函数调用没有这个概念。"""
        started = threading.Event()
        release = threading.Event()

        def slow(text: str) -> str:
            started.set()
            release.wait(timeout=5)
            return "太晚了"

        node = build_node(delegate=slow)
        resp = node.handle(request("SendMessage", 1, message={
            "role": "user", "parts": [{"kind": "text", "text": "慢活"}]}))
        task_id = resp["result"]["id"]
        started.wait(timeout=5)
        canceled = node.handle(request("CancelTask", 2, id=task_id))["result"]
        self.assertEqual(canceled["status"]["state"], "canceled")
        release.set()
        time.sleep(0.05)
        final = node.handle(request("GetTask", 3, id=task_id))["result"]
        self.assertEqual(final["status"]["state"], "canceled")
        self.assertEqual(final["artifacts"], [])

    def test_list_tasks(self):
        node = build_node()
        node.handle(request("SendMessage", 1, message={
            "role": "user", "parts": [{"kind": "text", "text": "一"}]}))
        node.handle(request("SendMessage", 2, message={
            "role": "user", "parts": [{"kind": "text", "text": "二"}]}))
        resp = node.handle(request("ListTasks", 3))
        self.assertEqual(len(resp["result"]["tasks"]), 2)

    def test_agent_card_rpc(self):
        node = build_node()
        resp = node.handle(request("GetAgentCard", 1))
        self.assertEqual(resp["result"]["name"], "dragon-test-agent")

    def test_message_without_text_is_an_error(self):
        node = build_node()
        resp = node.handle(request("SendMessage", 1, message={"role": "user"}))
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["invalid_params"])

    def test_unknown_a2a_method(self):
        node = build_node()
        resp = node.handle(request("message/send", 1))
        self.assertEqual(resp["error"]["code"], JSONRPC_ERRORS["method_not_found"])

    def test_context_id_groups_tasks(self):
        """contextId 把同一段上下文下的多次委托串起来。"""
        node = build_node()
        first = node.handle(request("SendMessage", 1, contextId="ctx-A", message={
            "role": "user", "parts": [{"kind": "text", "text": "一"}]}))["result"]
        second = node.handle(request("SendMessage", 2, contextId="ctx-A", message={
            "role": "user", "parts": [{"kind": "text", "text": "二"}]}))["result"]
        self.assertEqual(first["contextId"], "ctx-A")
        self.assertEqual(second["contextId"], "ctx-A")
        self.assertNotEqual(first["id"], second["id"])


# --------------------------------------------------------------------------- 薄适配层


class TestThinAdapter(unittest.TestCase):
    def test_one_registry_serves_both_local_and_protocol_calls(self):
        """一套工具，两种暴露——内部直调与协议调用打的是同一个注册表。"""
        reg = _Registry(default_registry())
        server = initialized(reg)

        # 内部直调（Agent 平时就是这么用的）
        reg.invoke("add", {"a": 1, "b": 2})
        # 协议调用
        server.handle(request("tools/call", 1, name="add", arguments={"a": 3, "b": 4}))

        self.assertEqual(reg.counts["add"], 2)
        self.assertEqual(reg.invocations, ["add", "add"])

    def test_protocol_layer_adds_no_second_schema(self):
        """协议层不维护第二份 schema。"""
        reg = ToolRegistry()
        reg.register(ToolSpec(name="only_one",
                              description="只有一个实现",
                              parameters={"type": "object",
                                          "properties": {"x": {"type": "integer"}},
                                          "required": ["x"]}),
                     lambda x: x)
        server = initialized(reg)
        tools = server.handle(request("tools/list", 1))["result"]["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["inputSchema"], reg.schemas()[0]["parameters"])

    def test_tool_registry_stays_usable_without_the_server(self):
        """工具层不依赖协议层——分层方向不能反过来。"""
        reg = default_registry()
        result = reg.invoke("add", {"a": 1, "b": 1})
        self.assertTrue(result.ok)
        self.assertEqual(result.output, 2)


if __name__ == "__main__":
    unittest.main()
