# 第 2 章 Agent 系统的最小解剖：四回路架构

## 一、本章要解决的问题

先看一次线上故障。

某公司的订单 Agent 接了查订单、查物流、查退款政策三个工具。接入物流工具第二周，客服收到投诉：用户说包裹一周没动静，系统回复"您的包裹尚未发出"。用户放了个截图——物流单号早就签收了。

排查结论很朴素：物流工具在高峰期有 3% 的超时率。超时被当成"查询成功，结果为空"。一次工具故障被下游当成业务事实，写进了给用户的最终回答。

不是模型不够聪明，提示词也写得规规矩矩。问题出在这个实现里，"调用工具失败"和"调用成功但没有记录"是同一个东西——都只是一个空字符串。感知这步没做区分，后面每步都建在缺失的区分上。

结论是：**Agent 的可靠性不取决于提示词写得多好，而取决于一次迭代里的边界划得清不清。** 世界返回的、状态机里存的、上下文里放的、落盘的东西混在一起，任何一个失败都会被当成事实往下推。

本章解决三件事：给出一套贯穿全书的解剖坐标系——四个回路加四个横切面；说清数据流向和控制流向；指出只有六个部件的骨架还差哪些横切面。
## 二、原理：机制拆解

### 2.1 四个回路

一次迭代可以拆成四个回路，每个回路有明确的输入、输出和职责。

| 回路 | 输入 | 输出 | 职责 |
| --- | --- | --- | --- |
| 规划 | 目标、状态、摘要、工具清单 | Decision | 决定下一步做什么 |
| 行动 | Decision | 原始返回 | 触达世界，副作用只在这里 |
| 感知 | 原始返回 | Observation | 规范化，区分成功/空/失败 |
| 反馈 | Observation | 状态更新 + 继续/终止 | 写状态、判进展、判终止 |

四个回路构成一个环，但每步传的东西不同：规划输出决策对象，行动输出原始返回，感知输出规范化观测，反馈输出"继续还是停"。

### 2.2 数据流与控制流

这是本章最要紧的一张图。下面是完整的一次迭代：

```
                            ┌─ 规划回路 PLAN ─────────────────────────────
                            │  读：state · 观测摘要 · 工具清单
   目标 goal ──────────────▶│  写：Decision（唯一的控制流分叉点）
                            └──────────────────────────────────────────────
                                          │ Decision（数据流）
                                          ▼
   工具 TOOLS ─────────────▶┌─ 行动回路 ACT ─────────────────────────────
   （只读 schema）           │  唯一产生副作用的地方
   护栏 GUARDRAILS ────────▶│  执行前拦截 · 超时 · 限流
                            └──────────────────────────────────────────────
                                          │ raw result（数据流）
                                          ▼
                            ┌─ 感知回路 PERCEIVE ────────────────────────
                            │  规范化：成功 / 空结果 / 失败 三态分开
                            └──────────────────────────────────────────────
                                          │ Observation（数据流）
                                          ▼
                            ┌─ 反馈回路 FEEDBACK ───────────────────────
   状态 STATE ────────────▶│  写 state：观测入档 · 摘要裁剪
   （唯一真相源）           │  进展判定 · 终止判定
   记忆 MEMORY ───────────▶│  写记忆：值得沉淀的经验才落库
                            └──────────────────────────────────────────────
                                          │ 继续 / 终止
                                          └──────────────► 回到规划回路
```

图里有两条流，必须分清。图上那四条穿过回路的带子，是状态（state）、记忆（memory）、工具（tools）、护栏（guardrail）四个横切关注点，2.4 节展开。

**数据流（data plane）** 是内容——Observation、Decision、raw result，特点是**可以批处理**：一次规划多个动作、并行调多个工具、合并多个观测，都不破坏语义。

**控制流（control plane）** 是调度——下一步做什么、要不要继续、要不要重规划，特点是**必须串行且单点**：整个系统有且只有一个地方决定"接下来干什么"。

口诀是：**数据流可以批处理，控制流只能串行。** 把两者塞进同一段提示词，实际是把调度规则塞进了数据。代价是没法并行调工具、没法插人工确认、没法做检查点。

### 2.3 一次迭代的三份账

四个回路跑一轮，产生三份不同的东西；分清它们是可观测、可恢复、可审计的前提。

| 位置 | 放什么 | 特点 |
| --- | --- | --- |
| 进入上下文 | 系统提示 + 目标 + 工具 schema + 观测摘要 + 记忆 | 每轮重建有预算；可从状态重放 |
| 离开状态机 | 一个 Decision：工具名 + 参数或最终答案 | 本轮唯一的新信息，不可重建 |
| 落盘 | 状态快照 + 步序号 + Decision + Observation + 原始返回 + 时间 | 唯一真相来源，无上限 |

一句话：**上下文是预算，落盘是无限；上下文里放引用，落盘里放原文。**

推论：工具的原始返回不进上下文，进上下文的是摘要和引用，而上下文每轮重读一次就花一次钱。上一章"上下文挤爆"的根子在这里——不是窗口小，是没人裁剪。

### 2.4 四个横切面

四个回路是流程内的切分，四个横切关注点是流程外的切分：不属于任何单一回路，而是横切所有回路。

| 横切面 | 读 | 写 | 接口形态 |
| --- | --- | --- | --- |
| 状态 STATE | 全部回路 | 全部回路 | `read(state)` / `update(state, patch)` |
| 记忆 MEMORY | 只有感知 | 只有反馈 | `retrieve(query)` / `write(event)` |
| 工具 TOOLS | 规划只读 schema | 行动调用 | `register(spec, fn)` |
| 护栏 GUARDRAILS | 四回路全切 | 四回路全切 | `check(stage, payload)` → allow/deny/modify |

这样解耦的收益是：换策略不换状态，长期知识不进上下文主线，加工具不改回路，一份护栏挂四处而不污染主干。

两条最容易被忽略的边界：

**护栏不是一个步骤。** 同一份策略要挂在四处：感知入口（校验数据）、规划输出（校验意图）、行动执行前（拦截危险动作）、落盘前（拦住不该记的内容）。只写了行动前那一处的实现，护栏就只剩四分之一。

**记忆只被感知回路读、被反馈回路写。** 这让"检索"不污染"执行"，也让模型不能随手把推断写进长期记忆。

### 2.5 同一模型的三种映射

这套坐标系不因形态而变，变的是每个回路装了什么。

| 形态 | 感知 | 规划 | 行动 | 反馈 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 单 Agent | 工具返回 | 模型选下一步 | 调工具 | 观测入 state、判进展 | 一个 state |
| 多 Agent | 收其他 Agent 的消息 | 路由：交给谁做 | 调工具或转发 | 汇总并判全局进展 | 共享 state（黑板） |
| 代码智能体 | 读文件、检索、跑 diff | 生成补丁或命令 | 沙箱执行、跑测试 | 测试结果 + diff 摘要 | 仓库工作区 + 状态 |

**多 Agent** 的增量在规划回路多了一个"路由"决策——交给谁做。这带来新的失败面：委派了但对方没做完。坐标系不变，变的是规划回路的输出类型：不再只有"调哪个工具"，还有"交给哪个 Agent"。

**代码智能体** 的优势在反馈回路：它能比通用 Agent 严格得多，因为"测试通过与否"是客观判据，不依赖模型自评——这也是它更容易收敛的原因。

## 三、为什么这样设计：取舍与失效条件

### 3.1 拆开的代价

四个回路意味着一次迭代里有四次边界穿越，比"一个大函数"多几次参数传递。对延迟敏感的场景这点开销可测。所以拆分理由不能是"更优雅"，必须是这些收益：

- **可替换**：换模型只改规划回路，换工具只改行动回路，换后端只改记忆。
- **可恢复**：状态是唯一真相源，进程挂在哪都能从快照续跑。
- **可插入**：人工确认、限流、超时重试插在回路之间，不改主干。
- **可定位**：出问题时能立刻回答是模型选错动作、工具返回错，还是状态没写对。

场景一个都用不上，就不该拆。

### 3.2 什么时候不该拆

**单步任务。** 一次查询、一次返回，拆四回路是自找麻烦。

**没有反馈的领域。** 工具返回不影响下一步、只是原样贴进答案时，感知和反馈回路退化成搬运工。

**极短的强顺序流程。** 三步以内、每步结果唯一，写成工作流更好读也好测。

**强实时场景。** 状态管理和序列化开销可能超过模型调用本身。

**副作用是核心且不可逆。** 这条最反直觉。如果每个动作都要人工确认，四回路的自动化收益被人人在环吃掉一半——这时更该做的是**把动作空间收窄**（Agent 只能读不能写），而不是加回路。

### 3.3 拆错了的典型症状

- **感知回路不区分三态**：工具失败被当成业务事实——开头那个"超时被当成没有物流记录"。
- **规划回路顺手改状态**：和状态更新混在一起，回放说不清谁写的。
- **上下文塞满原始返回**：没人裁剪，步数一多就挤爆，模型被无关细节带偏。
- **状态不落盘**：出事后无法回放，只能读日志猜决策依据。
- **控制流写进提示词**：没法并行调工具、插人工确认、做检查点。
- **终止条件埋在提示词里**："信息足够了就回答"不是终止条件，"够了"不可判定，终止必须是代码判断。

## 四、动手：最小可实现

下面是一个 149 行的最小 ReAct 骨架，实现了四个回路加状态、工具两个横切面。完整文件见 `code/ch02-anatomy/main.py`。

**依赖**：Python 3.9+，只用标准库。mock 模式不需要 API Key、不联网即可跑通完整回路；`--live` 额外需要 `OPENAI_API_KEY`，可选 `OPENAI_BASE_URL`（默认 `https://api.openai.com/v1`）和 `OPENAI_MODEL`（默认 `gpt-4o-mini`）。

```python
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
```

跑起来是这样（`python code/ch02-anatomy/main.py`）：

```
目标：订单 A1001 能不能退  模式：mock  步数：3/6
  感知 <- [search_orders] 命中 1 个订单
         data={"orders": [{"order_id": "A1001", "status": "已签收", "paid": 199.0, "days": 3}]}
  感知 <- [check_refund_policy] 签收后第 3 天，在退货期内
         data={"order_id": "A1001", "refundable": true, "amount": 199.0, "reason": "签收后第 3 天，在退货期内"}
结论：订单 A1001 可以退款：签收后第 3 天，在退货期内
终止依据：goal_met
```

三处设计值得注意。

**live 模式没用 function-calling 协议。** 模型只吐一个 JSON 动作，真正的调用由行动回路执行。模型和工具之间隔着一层结构化决策对象，四回路的边界才不会被协议细节抹平。

**上下文每轮由状态重建。** live 模式每次请求只发一条消息：目标 + 工具清单 + 状态里的观测摘要，模型不依赖会话历史。这是"状态是唯一真相源"的兑现，也是"上下文是预算"的落地方式。

**`no_progress` 是可判定的。** 反馈回路比较连续两次的动作与结果，完全相同就判定卡住并终止。判据在框架里，不在提示词里。

**这个骨架缺什么。** 八个部件里实现了六个：四个回路加状态、工具。缺记忆（没有跨迭代、跨会话的长期知识；加它要在感知回路读前插入检索、在反馈回路写后加一次沉淀）和护栏（没有输入输出校验和副作用确认；加它要在四处各插一次 `check`）。另外还缺：状态没落盘、工具没有幂等键和超时、没有并行调用、摘要没有长度上限。这些会在后续卷里补上。

## 五、开源实现对照

拿四回路坐标系去看几个主流框架，能看出各自的重心在哪。

| 项目 | 感知 | 规划 | 行动 | 反馈 | 状态 | 工具 | 护栏 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| LangGraph | 节点入边 | 节点内决策 | 工具节点 | 边上的归约 | 显式状态图 + 检查点 | 节点内注册 | 需自建 |
| AutoGen | 收到消息 | 谁发言 | 代码执行 | 对话回复 | 对话历史 | 注册即可 | 需自建 |
| OpenAI Agents SDK | 工具结果 | 模型决策 | function 工具 | 回灌 + 追踪 | 会话状态 | 函数 | 输入输出护栏 |
| PydanticAI | 依赖注入结果 | 模型决策 | 调用工具 | 结果回灌 | 运行上下文 | 带类型契约 | 类型校验前置 |
| smolagents | 执行结果 | 模型决策 | 代码块执行 | 结果回灌 | 变量与步骤 | Python 代码块 | 靠沙箱兜底 |

这五个框架都不自带跨会话的长期记忆层，表里不再单列。

**LangGraph** 把状态横切面做到极致：状态是显式的图数据，节点是回路的组合，检查点让一次运行可存可恢复——正对上 2.2 节"数据流与控制流分离"，图上的边是控制流，节点里是数据流。

**AutoGen** 把反馈做成 Agent 之间的对话，一个 Agent 的输出就是另一个的输入，反馈天然在回路里。代价是反馈形态被固定成自然语言消息，机器不好解析。

**OpenAI Agents SDK** 把控制流和护栏做成产品概念：交接（handoff）让"这任务归谁"变成结构化声明，护栏在两侧校验，横切面最完整。

**PydanticAI** 把工具契约前移到编译期，参数不合法在运行前就拦下，等于给行动回路装了道类型闸门。

**smolagents** 用 Python 代码块代替 JSON 工具调用，表达力大了，但代码块意味着真实执行，护栏必须换成沙箱。

横向看，**这五个框架没有一个把"让模型自己决定"当成默认**：状态显式化、契约类型化、护栏产品化、动作沙箱化，都是把决策权收窄到人看得见、能审核的范围里。

## 六、坑与检查清单

**四个回路**

- [ ] 能说出每一步属于哪个回路
- [ ] 调用工具失败与"成功但没有记录"在代码里可区分
- [ ] 副作用只在行动回路发生
- [ ] 状态只由反馈回路写
- [ ] 终止判据在代码里，不在提示词里

**数据流与控制流**

- [ ] "下一步做什么"只有一个地方决定
- [ ] 观测进上下文前先裁剪
- [ ] 原始返回落盘，不占上下文预算
- [ ] 任意两步之间能插人工确认
- [ ] 一次规划多动作时彼此不依赖

**四个横切面**

- [ ] 状态是唯一真相源，无私有副本
- [ ] 记忆读在感知侧，写在反馈侧
- [ ] 工具对规划侧只暴露 schema
- [ ] 护栏挂在感知入口、规划输出、行动执行前、落盘前四处
- [ ] 状态有检查点，中断可续跑

**落盘与回放**

- [ ] 每一轮都记了动作、参数、返回、耗时
- [ ] 能用落盘记录重放一次完整运行
- [ ] 上下文可完全从状态重建

## 七、小结与延伸阅读

本章的骨架记成两句话加一张图。

**两句话。** 第一句：一次迭代由感知、规划、行动、反馈四个回路组成，每个回路的输入输出都是结构化对象；四个回路之上横切状态、记忆、工具、护栏。第二句：数据流是内容，可以并行可以批处理；控制流是调度，必须串行必须单点。

**一张图。** 记住 2.2 节那张图就够了。判断实现有没有问题，只问三问：工具失败和空结果分得开吗？上下文里放的是摘要还是原文？终止判据在代码里还是提示词里？开头那个故障，答案就藏在后两问里。

下一章从"骨架齐了"走到"为什么要这么搭"：可靠性、复利与工程约束。

### 延伸阅读

论文：

- ReAct: Synergizing Reasoning and Acting in Language Models — https://arxiv.org/abs/2210.03629 （代码：https://github.com/ysymyth/ReAct ）
- Reflexion: Language Agents with Verbal Reinforcement Learning — https://arxiv.org/abs/2303.11366 （代码：https://github.com/noahshinn/reflexion ）
- Generative Agents: Interactive Simulacra of Human Behavior — https://arxiv.org/abs/2304.03442 （代码：https://github.com/joonspk-research/generative_agents ）
- Voyager: An Open-Ended Embodied Agent with Large Language Models — https://arxiv.org/abs/2305.16291 （代码：https://github.com/MineDojo/Voyager ）
- AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation — https://arxiv.org/abs/2308.08155
- SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering — https://arxiv.org/abs/2405.15793 （代码：https://github.com/SWE-agent/SWE-agent ）
- Why Do Multi-Agent LLM Systems Fail? — https://arxiv.org/abs/2503.13657
- A Survey on Large Language Model based Autonomous Agents — https://arxiv.org/abs/2308.11432

开源实现：

- LangGraph — https://github.com/langchain-ai/langgraph （MIT）
- AutoGen — https://github.com/microsoft/autogen （代码 MIT，根目录 LICENSE 为 CC-BY-4.0）
- OpenAI Agents SDK — https://github.com/openai/openai-agents-python （MIT）
- PydanticAI — https://github.com/pydantic/pydantic-ai （MIT）
- smolagents — https://github.com/huggingface/smolagents （Apache-2.0）
