# 第 1 章 Agent 的定义、边界与心智模型

## 一、本章要解决的问题

先看一个反例。

某团队做了个"智能客服"，自称 Agent：把用户问句塞进一段 2000 字系统提示词，提示词写着"用户问退款时，先查订单表，再查物流表，最后按七天无理由政策回答"，调一次大模型，把文本丢回用户。

上线能演示，老板满意。直到客服收到一条："我的订单 3 号到的，怎么还没退款"，系统回了"您的订单已于 3 号签收，符合七天无理由退款条件"——可这订单号根本不存在。模型照提示词的格式编了个合规答案。

不是模型不够聪明，也不是提示词不够长。问题在结构：动作序列是人写死在提示词里的自然语言；工具返回的结果没被读回来修正下一步；执行到哪一步靠模型在文本里推断；它甚至没有"目标达成"的概念，只有"生成一段回答"。

于是"Agent"这个词废了。这边说 Agent 就是大模型加提示词，隔壁用同一套东西说自己不是 Agent，理由是提示词更结构化。评审会上争的不是技术，是定义。

差别不在提示词，在**控制权归属**——谁决定下一步做什么、谁决定什么时候停。而这个差别能拆成对着代码检查的东西。

本章解决三件事：给出可操作的判定标准；划清与工作流（workflow）、链式调用（chain）、RPA、Copilot 的边界和混用代价；建立自主性（autonomy）光谱与代价模型——自主性不是越高越好，它有账单。

## 二、原理：机制拆解

### 2.1 四条判定标准

逐条检查，四条全满足才算 Agent，缺一条掉回下一级。

**标准一：动作序列由系统自己决策，不人预先写死。**
判断方法：把调用工具（tool use）的顺序遮住，看决策在编译期（人写的 if/else、固定序列）还是运行期（模型读完当前状态才决定调哪个工具）。开头那个客服属于前者。

**标准二：依据环境反馈修正下一步。**
判断方法：工具返回值必须进入下一轮决策的依据。比"调过工具"严格——调了工具却把结果原样贴回最终答案的系统，中间没有任何修正。

**标准三：持有跨步骤的持久状态。**
判断方法：存在明确的状态（state）对象，装已完成动作、中间观测、当前进度，且能被检查点（checkpoint）存下来。状态躺在提示词字符串里、或只存在于单次请求的上下文里，都不算。

**标准四：以"完成目标"为终止条件。**
判断方法：存在成功判据（success criterion），命中即停。步数上限只是兜底——"跑满 8 步就返回"是放弃，"任务做完了就返回"才是完成。

标准一、二区分 Agent 与工作流，标准三、四区分 Agent 与一次性调用。

### 2.2 五级自主性光谱

自主性不是有也无，是连续谱。同一个业务，按放开决策权的程度分五种：

| 级别 | 决策权归属 | 典型形态 | 失败表现 |
| --- | --- | --- | --- |
| L1 单轮调用 | 全在人 | 一次 prompt 一次调用 | 答非所问、编造信息 |
| L2 固定流水线 | 人定步骤，模型填空 | chain、DAG | 顺序不匹配整条崩 |
| L3 受控循环 | 模型选动作，人定边界 | 带工具列表的 ReAct 循环 | 死循环、重复调同一工具 |
| L4 规划型 | 模型先出计划再执行 | Plan-and-Execute | 计划过时、计划本身错 |
| L5 开放式自治 | 模型自定目标与子目标 | 探索式代理、技能库 | 目标漂移、成本失控 |

L2 到 L3 的分界是**动作序列是谁写的**：L2 里"先 A 后 B"写在代码里，L3 里模型拿到工具清单，按当前观测自己决定调哪个、调几次。

L4 到 L5 的分界是**是否允许修改自己的目标**：L4 的目标是外部给定的常量，能写进验收标准；L5 允许自调目标优先级，只能写进行为约束。运维难度差一个量级。

### 2.3 与相邻概念的边界

| 形态 | 谁决定下一步动作 | 边界特征 |
| --- | --- | --- |
| 链式调用（chain） | 人，顺序固定 | 上一步输出喂下一步，无分支决策 |
| 工作流（workflow） | 人，带分支循环 | 分支条件写死在图里，状态显式 |
| Agent | 模型，按观测动态选 | 有工具、有反馈、有状态、有终止判据 |
| RPA | 脚本，按坐标或选择器 | 界面不变则可靠，界面一变就碎 |
| Copilot | 人 | 模型给建议，人执行 |

混用的代价双向。把工作流当 Agent，白付自主性的钱：每次决策多过一遍模型，多花延迟和 token，换来同一个本可写死的分支。把 Agent 当工作流更隐蔽——以为形成了反馈回路，其实反馈没回流到决策，模型拿旧观测做新决策，团队却按"它会自我修正"的假设排障。

Copilot 最容易混：都支持自然语言、都调了工具。区别只有一条——**最后一步的按钮是人按的还是机器按的**，这决定了责任归属。

### 2.4 从 ReAct 到框架化实现

每一步演进都在补一个具体缺口。

**ReAct**（2022，arXiv:2210.03629）让推理和行动交替：模型输出思考、输出动作，环境返回观察（Observation），进入下一轮。这确立了今天主流 Agent 循环的骨架。

**Reflexion**（2023，arXiv:2303.11366）补"改了之后怎么办"：不改权重，让 Agent 用自然语言写下失败原因存进情节记忆（episodic memory），下次尝试时读它。反馈从"重新采样"升级为"积累经验"，同时暴露一个至今仍存在的坑——错误反思会污染后续所有尝试。

**Generative Agents**（2023，arXiv:2304.03442）把状态拆成三层：记忆流（memory stream）存原始经历，反思（reflection）压成高层结论，计划（planning）给日程。它证明长期状态不是装饰，而是长程行为一致的前提。

**Voyager**（2023，arXiv:2305.16291）往 L5 走一步：自动课程（automatic curriculum）自排难度递增的任务，可增长技能库存下验证过的可执行代码供检索组合。关键贡献是把"学会的能力"存成可复用程序而非文字记录。

今天这些被拆进工程框架：循环下沉到框架里，开发者关心的是状态图怎么画、工具契约怎么定、多 Agent 怎么通信。

## 三、为什么这样设计：取舍与失效条件

### 3.1 自主性的代价模型

自主性换来的是"面对没见过的输入也能推进"，代价是**错误概率的乘法**。单步成功率 p，执行 n 步全对是 pⁿ。p=0.9 时十步只剩约 35%；p=0.95 时二十步只剩约 36%。步数从 3 提到 10 不是线性变差，代价是指数级的。

还有三个放大器：**反馈有噪声**，工具的部分失败被当成有效观测继续推理；**观测污染上下文**，早期的重要约束被稀释，模型开始忘事；**错误自我强化**，一步判断错了，后续基于错的状态继续。

所以自主性必须设上限，而**上限的设置方式比数值更重要**。坏设法是"跑满 8 步就返回"——第 8 步比第 3 步更差，它建在 7 步错误之上。好设法三条线齐设：步数上限、预算上限（token / 时间 / 金钱）、**无进展检测（no-progress detection）**（连续 k 步状态无实质变化即判定卡住并终止）。第三条最关键，它把"卡住"和"快跑完了"分开。

第二笔账是**成本不可预测**：简单 case 两步收工，复杂 case 可能打满上限。容量规划要按最坏情况算，不是平均。

### 3.2 什么时候不该提高自主性

**流程已确定且大概率不变。** 比如每天凌晨把固定几个数据源的字段对齐写进报表。L2 流水线全面胜出——它每次跑结果一样，Agent 每次可能不同。

**错误代价不可逆。** 转账、删除、对外发送、改生产配置。正确做法不是禁止 Agent，而是**把写操作从动作空间（action space）里拿走**：Agent 只负责读和提议，写操作走独立的、带人工确认或强校验的通道。这等于把自主性 L3/L4 和副作用 L0 拆开设计。

**延迟敏感。** 自主性意味着模型调用次数不可控。自助查询、实时风控这类场景，Agent 的 p95 延迟是工作流的数倍——每一步都是一次串行的模型往返。

**需要可复现、可审计。** 合规场景要求给出"这次为什么这么处理"的依据链。Agent 的决策依据是权重里的统计倾向，不是可重放的规则。LangGraph 强调显式状态图和检查点，正是冲着这个需求。

**目标本身清晰，不需要探索。** 你已经知道查哪张表、问哪个 API，自主决策就没发挥空间，只是把确定逻辑写成了不确定形式。

反过来，**该提高自主性**的信号有：分支事先枚举不完；异常路径比正常路径多；反馈能真正改变后续动作；领域知识无法完全写成规则。

### 3.3 失效条件

- **目标漂移**：模型开始优化"看起来在推进"而非"真的在完成"。
- **工具失败被静默吞掉**：超时这类错误被当成有效数据继续用，最后输出自信的错误结论。
- **重复动作**：反复用同参数调同一工具。循环检测能发现，但要先有状态可比较——这就是标准三的实际原因。
- **上下文挤爆**：步数多到上下文放不下，早期约束被截断，行为退化。
- **反思污染**：Reflexion 式机制把错误结论写进长期记忆，之后每个新任务都被影响。

## 四、动手：最小可实现

下面是一份完整可跑的最小 Agent：有工具列表、跨步骤状态、反馈回灌、成功判据和步数上限。把它保存成 `mini_agent.py` 即可运行。

**依赖**：mock 模式零第三方依赖，只需 Python 3.9+ 标准库，不需要 API Key，可离线跑通。`--live` 模式也只用标准库 `urllib`，额外需要 `OPENAI_API_KEY`，可选 `OPENAI_BASE_URL`（默认 `https://api.openai.com/v1`）和 `OPENAI_MODEL`（默认 `gpt-4o-mini`）。请求体遵循 Chat Completions 的 tools / function-calling 协议。

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小 Agent：一个可运行的「目标 → 选动作 → 执行 → 读反馈 → 再决策」循环。

依赖
  mock 模式（默认）：Python 3.9+ 标准库，零第三方依赖，不需要 API Key，可离线跑通。
  live 模式（--live）：同样只用标准库 urllib，需要环境变量
      OPENAI_API_KEY    必填
      OPENAI_BASE_URL   可选，默认 https://api.openai.com/v1
      OPENAI_MODEL      可选，默认 gpt-4o-mini（换成你账号实际可用的模型）
    请求体遵循 Chat Completions 的 tools / function-calling 协议。

运行
    python mini_agent.py
    python mini_agent.py --goal "订单 A1001 能不能退"
    python mini_agent.py --live
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List

MAX_STEPS = 6  # 自主性预算：步数上限是兜底，不是终止条件

# ---------------------------------------------------------------------------
# 动作空间：Agent 只能在这些工具里挑
# ---------------------------------------------------------------------------

MOCK_ORDERS: Dict[str, Dict[str, Any]] = {
    "A1001": {"status": "已签收", "paid": 199.0, "days_since_delivery": 3},
    "B2002": {"status": "运输中", "paid": 89.0, "days_since_delivery": None},
}

TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_orders",
            "description": "按订单号或状态检索订单，返回订单号、状态、金额、签收天数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "订单号或状态关键词，如 A1001 / 已签收 / 所有"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_refund_policy",
            "description": "查询指定订单是否可退、可退金额与依据。",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "订单号"}},
                "required": ["order_id"],
            },
        },
    },
]


def tool_search_orders(query: str) -> Dict[str, Any]:
    """检索订单。命中返回列表，未命中返回空列表——这就是给模型的负反馈。"""
    q = (query or "").strip()
    if not q:
        return {"orders": [], "note": "查询为空"}
    hits: List[Dict[str, Any]] = []
    for oid, rec in MOCK_ORDERS.items():
        matched = (
            oid.lower() in q.lower()
            or rec["status"] in q
            or ("所有" in q or "全部" in q or "list" in q.lower())
        )
        if matched:
            hits.append(
                {
                    "order_id": oid,
                    "status": rec["status"],
                    "paid": rec["paid"],
                    "days_since_delivery": rec["days_since_delivery"],
                }
            )
    return {"orders": hits, "note": "命中" if hits else "无匹配订单"}


def tool_check_refund_policy(order_id: str) -> Dict[str, Any]:
    """按退款政策判定某个订单能不能退。"""
    rec = MOCK_ORDERS.get((order_id or "").strip().upper())
    if rec is None:
        return {"refundable": False, "reason": f"订单 {order_id} 不存在", "amount": 0.0}
    if rec["status"] != "已签收":
        return {
            "refundable": False,
            "reason": f"订单状态为「{rec['status']}」，尚未签收，不支持退货",
            "amount": 0.0,
        }
    days = rec["days_since_delivery"]
    if days is not None and days <= 7:
        return {
            "refundable": True,
            "reason": f"签收后第 {days} 天，在 7 天无理由退货期内",
            "amount": rec["paid"],
        }
    return {"refundable": False, "reason": f"已签收 {days} 天，超过 7 天退货期", "amount": 0.0}


TOOL_IMPLS = {
    "search_orders": tool_search_orders,
    "check_refund_policy": tool_check_refund_policy,
}


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """派发工具。工具出错也要返回结构化结果，而不是抛异常中断整个循环。"""
    fn = TOOL_IMPLS.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return fn(**args)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}


# ---------------------------------------------------------------------------
# 状态：跨步骤保留的东西
# ---------------------------------------------------------------------------


@dataclass
class State:
    goal: str
    step: int = 0
    phase: int = 0       # mock 策略的阶段，等价于模型对当前处境的判断
    retries: int = 0
    order_id: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)
    observations: List[str] = field(default_factory=list)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    done: bool = False


@dataclass
class Decision:
    kind: str            # "tool" | "final"
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    text: str = ""
    tool_call_id: str = ""
    raw_calls: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 策略：下一步做什么。mock 是脚本化的假模型，live 是真模型
# ---------------------------------------------------------------------------

ORDER_ID_RE = re.compile(r"[A-Za-z]\d{3,}")
SYSTEM_PROMPT = (
    "你是一个订单服务 Agent。用户会给出目标。你只能通过 search_orders 与 "
    "check_refund_policy 两个工具获取信息；不要编造订单号、金额或政策条款。"
    "信息足够后直接给出结论。"
)


def mock_policy(state: State) -> Decision:
    """脚本化的假模型：会依据上一步的反馈改变下一步动作。"""
    if state.phase == 0:
        state.phase = 1
        return Decision(kind="tool", tool="search_orders", args={"query": state.goal})

    if state.phase == 1:
        res = json.loads(state.observations[-1])
        if not res.get("orders"):
            match = ORDER_ID_RE.search(state.goal)
            if match and state.retries < 1:
                state.retries += 1
                return Decision(kind="tool", tool="search_orders", args={"query": match.group(0)})
            return Decision(kind="final", text=f"没有查到与「{state.goal}」匹配的订单，无法判断能否退款。")
        state.order_id = res["orders"][0]["order_id"]
        state.phase = 2
        return Decision(kind="tool", tool="check_refund_policy", args={"order_id": state.order_id})

    # phase 2：拿到政策判定，出结论
    pol = json.loads(state.observations[-1])
    state.phase = 3
    if pol.get("refundable"):
        text = f"订单 {state.order_id} 可以退款：{pol['reason']}，应退 {pol['amount']} 元。"
    else:
        text = f"订单 {state.order_id} 不能退款：{pol['reason']}。"
    return Decision(kind="final", text=text)


def live_policy(state: State) -> Decision:
    """真实模型：Chat Completions + tools / function-calling。"""
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    payload = {
        "model": model,
        "messages": state.messages,
        "tools": TOOL_SPECS,
        "tool_choice": "auto",
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", ""),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"模型接口返回 {exc.code}: {detail}") from exc

    message = data["choices"][0]["message"]
    calls = message.get("tool_calls") or []
    if calls:
        call = calls[0]
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        state.messages.append(
            {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
        )
        return Decision(
            kind="tool",
            tool=fn.get("name", ""),
            args=args,
            tool_call_id=call.get("id", ""),
            raw_calls=calls,
        )
    state.messages.append({"role": "assistant", "content": message.get("content") or ""})
    return Decision(kind="final", text=(message.get("content") or "").strip())


# ---------------------------------------------------------------------------
# 循环：Agent 的最小骨架
# ---------------------------------------------------------------------------


def run(goal: str, policy) -> State:
    state = State(goal=goal)
    state.messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": goal},
    ]
    while state.step < MAX_STEPS:
        state.step += 1
        decision = policy(state)
        if decision.kind == "final":
            state.answer = decision.text
            state.done = True
            break
        result = call_tool(decision.tool, decision.args)
        state.history.append(
            {"step": state.step, "tool": decision.tool, "args": decision.args, "result": result}
        )
        payload = json.dumps(result, ensure_ascii=False)
        state.observations.append(payload)
        if decision.tool_call_id:
            state.messages.append(
                {"role": "tool", "tool_call_id": decision.tool_call_id, "content": payload}
            )
    if not state.done:
        # 步数上限触发：降级输出，别返回空。
        last = state.observations[-1] if state.observations else "（无观测）"
        state.answer = f"步数上限 {MAX_STEPS} 用尽，基于已有观测给结论：{last}"
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="最小 Agent 示例")
    parser.add_argument("--goal", default="订单 A1001 能不能退", help="要完成的目标")
    parser.add_argument("--live", action="store_true", help="切换到真实模型（需要 OPENAI_API_KEY）")
    args = parser.parse_args()

    if args.live and not os.environ.get("OPENAI_API_KEY"):
        print("--live 需要环境变量 OPENAI_API_KEY", file=sys.stderr)
        return 2

    state = run(args.goal, live_policy if args.live else mock_policy)

    print(f"目标：{state.goal}")
    print(f"模式：{'live' if args.live else 'mock'}    步数：{state.step}/{MAX_STEPS}")
    print("-" * 68)
    for item in state.history:
        print(f"[{item['step']}] {item['tool']}({json.dumps(item['args'], ensure_ascii=False)})")
        print(f"    -> {json.dumps(item['result'], ensure_ascii=False)}")
    print("-" * 68)
    print(f"结论：{state.answer}")
    print(f"终止依据：{'目标达成' if state.done else '步数上限'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

mock 模式的输出：

```
目标：订单 A1001 能不能退
模式：mock    步数：3/6
----------------------------------------------------------------------
[1] search_orders({"query": "订单 A1001 能不能退"})
    -> {"orders": [{"order_id": "A1001", "status": "已签收", "paid": 199.0, "days_since_delivery": 3}], "note": "命中"}
[2] check_refund_policy({"order_id": "A1001"})
    -> {"refundable": true, "reason": "签收后第 3 天，在 7 天无理由退货期内", "amount": 199.0}
----------------------------------------------------------------------
结论：订单 A1001 可以退款：签收后第 3 天，在 7 天无理由退货期内，应退 199.0 元。
终止依据：目标达成
```

换成查不到的 `Z9999`，第一轮返回空列表，策略**改写查询再试一次**，第二次仍空才终止并如实说明：

```
[1] search_orders({"query": "订单 Z9999 能不能退"})
    -> {"orders": [], "note": "无匹配订单"}
[2] search_orders({"query": "Z9999"})
    -> {"orders": [], "note": "无匹配订单"}
结论：没有查到与「订单 Z9999 能不能退」匹配的订单，无法判断能否退款。
```

这几行正对着开头那个反例：老系统会编一个运单号，这个系统不会，因为"检索为空"是必须回灌、必须影响下一步的观测，最终答案从观测推出，不是从提示词格式套出来的。

`--live` 只替换 `policy` 一个函数，循环、状态、终止逻辑完全不变：

```bash
export OPENAI_API_KEY=sk-...
python mini_agent.py --live
```

这份实现刻意保持最小：没有护栏（guardrail）、检查点、重试退避、并发调用工具——那些是第 2 章四回路解剖和第 6 卷工程化要展开的。这里只建立一件事：**Agent 的骨架是"状态 + 策略 + 动作空间 + 终止判据"这四件套，不是一段更长的提示词**。

## 五、开源实现对照

拿四条标准去卡五个主流框架，能看出它们各自在光谱上的位置。

| 项目 | 许可证 | 自主性定位 | 强在哪条标准 |
| --- | --- | --- | --- |
| LangGraph | MIT | L3–L4 | 标准三：显式状态图 + 检查点，状态可存可恢复 |
| AutoGen | 代码 MIT（LICENSE-CODE）；根目录 LICENSE 为 CC-BY-4.0 | L4 | 标准二：对话即反馈通道，多 Agent 靠消息互相修正 |
| OpenAI Agents SDK | MIT | L3–L4 | 标准四：交接（handoff）与护栏（guardrail）显式定义边界 |
| PydanticAI | MIT | L3 | 标准一、二：Pydantic 模型定义输入输出，类型安全注入依赖 |
| smolagents | Apache-2.0 | L3 | 标准二：用 Python 代码块代替 JSON 工具调用 |

**LangGraph** 的价值在标准三：多数框架把状态藏在消息列表里，进程一挂就没了；它把状态显式建模成图上的数据，检查点让运行可存下、恢复、回放，决策依据因此是一条可重放的轨迹。

**AutoGen** 走标准二：把反馈做成 Agent 之间的对话，GroupChat（群聊）里一个 Agent 的输出就是另一个的输入。轮次一多，成本和"谁在主导"都会失控。

**OpenAI Agents SDK** 走标准四：交接把"这任务归谁"变成结构化声明，护栏在输入输出两侧校验。

**PydanticAI** 把类型契约放最前，把"模型给出格式不合法的动作参数"从运行时错误前移成编译期检查。

**smolagents** 换了动作表示：用 Python 代码块代替 JSON 工具调用，模型一次能表达组合动作；代价是必须进沙箱（sandbox）里跑。

横向看，五个项目没有一个把自主性做成"越大越好"的默认，都是把决策权**收窄到人看得见的范围**。框架的成熟度体现在边界划得多清楚，不是自主性开得多高。

## 六、坑与检查清单

**定义与边界**

- [ ] 能说清系统当前在光谱哪一级（L1–L5），决策权具体在谁手里
- [ ] 调用工具的顺序是人写死的（编译期），还是模型读完观测后选的（运行期）
- [ ] 工具返回值真的进入下一步决策，而不只是被原样贴进最终答案
- [ ] 存在显式 state 对象，能存下来、能恢复，而非只活在单次请求的上下文里
- [ ] 有明确的任务完成判据，而不是"跑满 N 步就算完"
- [ ] 分清这是 Agent、Copilot 还是工作流——最后一步的动作是谁触发的

**自主性与上限**

- [ ] 设了步数上限、预算上限（token / 时间 / 金钱）三重上限
- [ ] 有无进展检测：连续 k 步状态无实质变化就判定卡住并终止
- [ ] 触发上限时降级输出已知信息，而不是返回空结果或半成品
- [ ] 预算按最坏情况（打满上限）估过，不是按平均步数
- [ ] 副作用不可逆的动作不在动作空间里，或有独立的确认与校验通道

**工具契约**

- [ ] 每个工具的参数和返回都是结构化的，不是自然语言
- [ ] 工具失败时返回结构化错误并被当作有效观测回灌，而不是静默吞掉
- [ ] 工具返回有长度上限，不会把上下文撑爆
- [ ] 调用工具的超时和重试有上限，重试次数计入步数预算

**观测与记忆**

- [ ] 每一步都记录了动作、参数、返回、耗时，能事后复现
- [ ] 写入长期记忆的内容经过筛选，失败尝试和错误结论没被直接沉淀进去
- [ ] 上下文保留了对后续步骤仍有效的约束，早期关键约束没被稀释掉

## 七、小结与延伸阅读

三条核心结论。

第一，**Agent 和"更长的提示词"不是一回事**，差别在控制权归属：谁决定下一步做什么、谁决定什么时候停。判定标准是四条可操作检查——自主决策动作序列、依据环境反馈修正、持有持久状态、以完成目标为终止条件。四条全满足才是 Agent，缺一条掉回下一级。

第二，**自主性是连续谱，有账单**。错误概率随步数指数放大（单步 0.9 时十步全对只剩约 35%），所以自主性越高，越要同时设步数、预算和无进展三重上限。

第三，**知道什么时候不该用，比知道怎么用更重要**。流程确定、副作用不可逆、延迟敏感、需要可审计——这四种场景下工作流在延迟、成本、稳定性上全面胜出；而把写操作从 Agent 的动作空间里拿走，往往比给 Agent 加更多护栏更有效。

下一章把这套心智模型落成具体结构：Agent 系统的四回路解剖——感知、规划、行动、反馈，加上状态、记忆、工具、护栏四个横切面，并给出一个能跑起来的 ReAct 骨架。

### 延伸阅读

论文：

- ReAct: Synergizing Reasoning and Acting in Language Models — https://arxiv.org/abs/2210.03629 （代码：https://github.com/ysymyth/ReAct ）
- Reflexion: Language Agents with Verbal Reinforcement Learning — https://arxiv.org/abs/2303.11366 （代码：https://github.com/noahshinn/reflexion ）
- Generative Agents: Interactive Simulacra of Human Behavior — https://arxiv.org/abs/2304.03442 （代码：https://github.com/joonspk-research/generative_agents ）
- Voyager: An Open-Ended Embodied Agent with Large Language Models — https://arxiv.org/abs/2305.16291 （代码：https://github.com/MineDojo/Voyager ）
- Why Do Multi-Agent LLM Systems Fail? — https://arxiv.org/abs/2503.13657
- A Survey on Large Language Model based Autonomous Agents — https://arxiv.org/abs/2308.11432

开源实现：

- LangGraph — https://github.com/langchain-ai/langgraph （MIT）
- AutoGen — https://github.com/microsoft/autogen （代码 MIT，根目录 LICENSE 为 CC-BY-4.0）
- OpenAI Agents SDK — https://github.com/openai/openai-agents-python （MIT）
- PydanticAI — https://github.com/pydantic/pydantic-ai （MIT）
- smolagents — https://github.com/huggingface/smolagents （Apache-2.0）
