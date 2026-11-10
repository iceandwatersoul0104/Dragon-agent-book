#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四回路骨架：感知 / 规划 / 行动 / 反馈。纯标准库，mock 模式无需 API Key。
运行：python main.py | python main.py --goal "订单 A1001 能不能退" | python main.py --live"""
import argparse, json, os, re, sys, urllib.error, urllib.request
from dataclasses import dataclass, field

MAX_STEPS = 6
ORDER_ID_RE = re.compile(r"[A-Za-z]\d{3,}")

@dataclass
class Observation:            # 感知回路的产物
    source: str
    ok: bool
    data: dict = field(default_factory=dict)
    note: str = ""

@dataclass
class Decision:               # 规划回路的产物
    kind: str                 # "tool" | "final"
    tool: str = ""
    args: dict = field(default_factory=dict)
    text: str = ""

@dataclass
class AgentState:             # 状态横切面：唯一真相源
    goal: str
    step: int = 0
    observations: list = field(default_factory=list)
    scratch: list = field(default_factory=list)   # 裁剪后才进上下文
    answer: str = ""
    stop_reason: str = ""

ORDERS = {"A1001": {"status": "已签收", "paid": 199.0, "days": 3},
          "B2002": {"status": "运输中", "paid": 89.0, "days": None}}

def search_orders(query: str) -> dict:
    """按订单号或状态检索订单。"""
    return {"orders": [{"order_id": o, "status": r["status"], "paid": r["paid"], "days": r["days"]}
                       for o, r in ORDERS.items() if o.lower() in (query or "").lower() or r["status"] in (query or "") or "所有" in (query or "")]}

def check_refund_policy(order_id: str) -> dict:
    """查询订单是否可退及可退金额。"""
    oid = (order_id or "").strip().upper()
    rec = ORDERS.get(oid) or {"status": "不存在", "paid": 0.0, "days": 0}
    ok = rec["status"] == "已签收" and rec["days"] is not None and rec["days"] <= 7
    return {"order_id": oid, "refundable": ok, "amount": rec["paid"] if ok else 0.0, "reason": f"签收后第 {rec['days']} 天，在退货期内" if ok else f"不可退（{rec['status']}）"}

TOOLS = {"search_orders": search_orders, "check_refund_policy": check_refund_policy}

# ==================== 四个回路 ====================
def perceive(tool: str, raw: dict) -> Observation:
    """感知回路：世界原始返回 -> 结构化观测。区分成功 / 空结果 / 失败。"""
    if "error" in raw:
        return Observation(tool, False, {}, str(raw["error"]))
    if "orders" in raw:
        return Observation(tool, True, raw, f"命中 {len(raw['orders'])} 个订单" if raw["orders"] else "无匹配订单")
    return Observation(tool, True, raw, raw.get("reason", "ok"))

def act(decision: Decision) -> dict:
    """行动回路：决策 -> 世界。副作用只发生在这里。"""
    fn = TOOLS.get(decision.tool)
    try:
        return fn(**decision.args) if fn else {"error": f"unknown tool: {decision.tool}"}
    except TypeError as exc:
        return {"error": f"bad arguments: {exc}"}

def feed_back(state: AgentState, obs: Observation, decision: Decision) -> str:
    """反馈回路：观测 -> 状态更新 + 是否继续。返回停止原因，空串表示继续。"""
    state.observations.append(obs)
    state.scratch.append(f"[{obs.source}] {decision.args} {obs.note}")
    if len(state.scratch) >= 2 and state.scratch[-1] == state.scratch[-2]:
        return "no_progress"          # 同样的动作 + 同样的结果 = 卡住
    return ""

def mock_policy(state: AgentState) -> Decision:
    """脚本化的假模型：依据上一轮观测改下一步动作。"""
    if not state.observations:
        return Decision("tool", "search_orders", {"query": state.goal})
    obs = state.observations[-1]
    if obs.source == "search_orders":
        orders = obs.data.get("orders", [])
        if orders:
            return Decision("tool", "check_refund_policy", {"order_id": orders[0]["order_id"]})
        if len(state.observations) < 2 and (m := ORDER_ID_RE.search(state.goal)):
            return Decision("tool", "search_orders", {"query": m.group(0)})
        return Decision("final", text=f"没有查到与「{state.goal}」匹配的订单，无法判断能否退款。")
    pol = obs.data
    return Decision("final", text=f"订单 {pol['order_id']} "
                   f"{'可以退款' if pol.get('refundable') else '不能退款'}：{pol['reason']}")

def live_policy(state: AgentState) -> Decision:
    """真实模型：模型只提议动作，由行动回路执行；上下文每轮由状态重建。"""
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    prompt = (f"目标：{state.goal}\n可用工具：{','.join(TOOLS)}\n已有观测：{json.dumps(state.scratch, ensure_ascii=False)}\n"
              '只输出 JSON：{"tool": "工具名", "args": {...}} 或 {"final": "结论"}')
    payload = {"model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
               "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", "")},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"模型接口返回 {exc.code}") from exc
    text = (data["choices"][0]["message"].get("content") or "").strip()
    try:
        obj = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except json.JSONDecodeError: obj = {}
    if not obj or "final" in obj:
        return Decision("final", text=str(obj.get("final", text)))
    return Decision("tool", str(obj.get("tool", "")), obj.get("args") or {})

def run(goal: str, policy) -> AgentState:
    state = AgentState(goal=goal)
    while state.step < MAX_STEPS:
        state.step += 1
        decision = policy(state)                     # 规划回路：控制流在这里分叉
        if decision.kind == "final":
            state.answer, state.stop_reason = decision.text, "goal_met"
            break
        raw = act(decision)                          # 行动回路
        obs = perceive(decision.tool, raw)          # 感知回路
        stop = feed_back(state, obs, decision)      # 反馈回路
        if stop:
            state.stop_reason, state.answer = stop, state.scratch[-1]
            break
    else:
        state.stop_reason, state.answer = "budget", f"步数上限 {MAX_STEPS} 用尽：{state.scratch[-1] if state.scratch else '（无观测）'}"
    return state

def main() -> int:
    ap = argparse.ArgumentParser(description="四回路骨架")
    ap.add_argument("--goal", default="订单 A1001 能不能退")
    ap.add_argument("--live", action="store_true", help="切换到真实模型（需 OPENAI_API_KEY）")
    a = ap.parse_args()
    if a.live and not os.environ.get("OPENAI_API_KEY"):
        print("--live 需要环境变量 OPENAI_API_KEY", file=sys.stderr)
        return 2
    st = run(a.goal, live_policy if a.live else mock_policy)
    print(f"目标：{st.goal}  模式：{'live' if a.live else 'mock'}  步数：{st.step}/{MAX_STEPS}")
    print("\n".join(f"  感知 <- [{o.source}] {o.note}\n         data={json.dumps(o.data, ensure_ascii=False)}" for o in st.observations))
    print(f"结论：{st.answer}\n终止依据：{st.stop_reason}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
