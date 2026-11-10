#!/usr/bin/env python3
"""协议接入实跑：MCP server 走真实 stdio 子进程，A2A 走一次完整委托。

运行方式（工程根目录下）::

    python examples/mcp_protocol_demo.py

全程离线：不联网、不需要 API Key、不写用户目录。

演示六件事：

1. **真的起一个子进程**——``python -m dragon_agent.mcp_server``，用 stdin/stdout
   说 JSON-RPC。这是 MCP 的 stdio 传输：客户端拉起 server 进程，双方用
   一行一条 JSON 说话；
2. **握手与版本协商**——客户端要一个我们支持的版本，server 原样回；
   要一个不认识的版本，server 回自己最新的那个，而不是拒绝；
3. **两种失败，两条通道**——工具名不存在 → JSON-RPC ``error``；
   工具跑过了但参数不对 → 结果里 ``isError: true``，模型看得懂、能自己改；
4. **幂等**——同一个 request id 重试副作用工具，只真执行一次；
5. **一套工具，两种暴露**——同一个注册表，内部直调和协议调用打的是同一批工具；
6. **A2A 的委托语义**——AgentCard 是能力名片，Task 有生命周期，
   Artifact 是"办完了"的凭证。
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

# 允许不安装、直接在工程根目录跑这个脚本。
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.mcp_server import A2ANode, AgentCard, MCPServer
from dragon_agent.tools import default_registry

sys.stdout.reconfigure(encoding="utf-8")


def show(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


class StdioClient:
    """MCP 的 stdio 客户端：拉起 server 子进程，逐行收发 JSON-RPC。"""

    def __init__(self, args: list[str] | None = None) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "dragon_agent.mcp_server", *(args or [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._id = 0

    def call(self, method: str, request_id: int | None = None, **params) -> dict:
        """发一条请求，收一条响应。

        ``request_id`` 可以显式指定——重试同一个 id 是幂等演示的关键。
        """
        if request_id is None:
            self._id += 1
            request_id = self._id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params:
            message["params"] = params
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("server 提前退出了")
        return json.loads(line)

    def notify(self, method: str, **params) -> None:
        """发一条通知——不期待响应。"""
        message = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def main() -> int:
    checks: list[bool] = []

    # ---------------------------------------------------------------- 1
    show("1) 真的起一个子进程：stdin/stdout 走 JSON-RPC")
    client = StdioClient()
    print(f"  server 进程 pid={client.proc.pid}")
    print(f"  启动命令：python -m dragon_agent.mcp_server")

    # ---------------------------------------------------------------- 2
    show("2) 握手与版本协商")
    init = client.call("initialize", protocolVersion="2025-11-25",
                       capabilities={}, clientInfo={"name": "demo", "version": "1.0"})
    print(f"  initialize → {json.dumps(init['result'], ensure_ascii=False)}")
    checks.append(init["result"]["protocolVersion"] == "2025-11-25")
    client.notify("notifications/initialized")

    # 再起一个进程，要一个我们不认识的版本：回最新的，不拒绝。
    other = StdioClient()
    fallback = other.call("initialize", protocolVersion="1999-01-01",
                          capabilities={}, clientInfo={"name": "demo", "version": "1.0"})
    print(f"  客户端要 1999-01-01 → server 回 "
          f"{fallback['result']['protocolVersion']}（不拒绝，让客户端自己决定）")
    checks.append(fallback["result"]["protocolVersion"] != "1999-01-01")
    other.close()

    # ---------------------------------------------------------------- 3
    show("3) tools/list：schema 只有一份")
    listing = client.call("tools/list")
    for tool in listing["result"]["tools"]:
        print(f"  {tool['name']}")
        print(f"    描述：{tool['description']}")
        print(f"    inputSchema：{json.dumps(tool['inputSchema'], ensure_ascii=False)}")
    checks.append(len(listing["result"]["tools"]) == 4)

    # ---------------------------------------------------------------- 4
    show("4) tools/call：两种失败，两条通道")
    ok = client.call("tools/call", name="add", arguments={"a": 2, "b": 3})
    print(f"  调用成功 → {json.dumps(ok['result'], ensure_ascii=False)}")
    checks.append(ok["result"]["content"][0]["text"] == "5")
    checks.append(ok["result"]["isError"] is False)

    unknown = client.call("tools/call", name="no_such_tool", arguments={})
    print(f"  工具不存在 → JSON-RPC error：{json.dumps(unknown['error'], ensure_ascii=False)}")
    checks.append("error" in unknown)

    bad_args = client.call("tools/call", name="add", arguments={"a": 1})
    print(f"  参数不对 → 执行错误：{json.dumps(bad_args['result'], ensure_ascii=False)}")
    checks.append("error" not in bad_args)
    checks.append(bad_args["result"]["isError"] is True)

    # ---------------------------------------------------------------- 5
    show("5) 幂等：同一个 request id 重试，副作用只发生一次")
    first = client.call("tools/call", request_id=100, name="submit_order",
                        arguments={"sku": "SKU-1", "qty": 1})
    print(f"  第一次 → isError={first['result']['isError']}")
    retry = client.call("tools/call", request_id=100, name="submit_order",
                         arguments={"sku": "SKU-1", "qty": 1})
    print(f"  同一个 id 重试 → isError={retry['result']['isError']}")
    print(f"    拒绝原因：{retry['result']['content'][0]['text']}")
    checks.append(first["result"]["isError"] is False)
    checks.append(retry["result"]["isError"] is True)
    client.close()

    # ---------------------------------------------------------------- 6
    show("6) 一套工具，两种暴露：内部直调与协议调用打的是同一个注册表")
    registry = default_registry()
    server = MCPServer(registry, "demo-mcp", "1.0")
    server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                   "params": {"protocolVersion": "2025-11-25"}})

    registry.invoke("add", {"a": 1, "b": 1})          # 内部直调
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "add", "arguments": {"a": 2, "b": 2}}})
    print(f"  注册表记录的真实执行：{registry.invocations}")
    print("  两条路走的是同一个注册表——工具只实现了一次")
    checks.append(registry.invocations == ["add", "add"])

    # ---------------------------------------------------------------- 7
    show("7) A2A：AgentCard 是名片，Task 有生命周期，Artifact 是凭证")
    card = AgentCard(
        name="dragon-refund-agent",
        description="处理退款对账，产出差异报告",
        url="http://127.0.0.1:9000/",
        version="1.0",
        capabilities={"streaming": False, "pushNotifications": False},
        skills=[{
            "id": "refund_reconcile",
            "name": "退款对账",
            "description": "比对订单与退款流水，产出差异报告",
            "tags": ["finance", "reconcile"],
            "examples": ["核对 2026-03 的退款"],
        }],
    )

    def delegate(text: str) -> str:
        """被委托方干活的入口。这里用注册表真的查一次账。"""
        time.sleep(0.2)  # 假装对账要一会儿，好让"委托是异步的"看得见
        result = registry.invoke("add", {"a": 100, "b": 32})
        if not result.ok:
            raise RuntimeError(result.error or "对账失败")
        return f"{text}：差异合计 {result.output} 元"

    node = A2ANode(card, delegate)
    print("  AgentCard（能力名片）：")
    for line in json.dumps(node.agent_card(), ensure_ascii=False, indent=2).splitlines():
        print(f"    {line}")

    sent = node.handle({
        "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
        "params": {"message": {"role": "user",
                               "parts": [{"kind": "text", "text": "核对 2026-03 的退款"}]}},
    })
    task_id = sent["result"]["id"]
    print(f"  SendMessage → 立刻返回任务 {task_id}"
          f"（状态 {sent['result']['status']['state']}，不等活干完）")
    checks.append(sent["result"]["status"]["state"] in ("submitted", "working"))

    deadline = time.time() + 5
    task = sent["result"]
    while time.time() < deadline:
        got = node.handle({"jsonrpc": "2.0", "id": 2, "method": "GetTask",
                           "params": {"id": task_id}})
        task = got["result"]
        if task["status"]["state"] in ("completed", "failed", "canceled"):
            break
        time.sleep(0.05)

    print(f"  状态推进：{' → '.join(u['state'] for u in node.status_updates(task_id))}")
    print(f"  终态：{task['status']['state']}")
    print(f"  Artifact（办完了的凭证）："
          f"{json.dumps(task['artifacts'], ensure_ascii=False)}")
    checks.append(task["status"]["state"] == "completed")
    checks.append(len(task["artifacts"]) == 1)
    checks.append(task["artifacts"][0]["parts"][0]["text"].endswith("132 元"))

    # ---------------------------------------------------------------- 总结
    print()
    print("=" * 74)
    ok_all = all(checks)
    print(f"自检结果：{sum(1 for c in checks if c)}/{len(checks)} 项通过"
          f" —— {'全部通过' if ok_all else '有项目未通过'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
