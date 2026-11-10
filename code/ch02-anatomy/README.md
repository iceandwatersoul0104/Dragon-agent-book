# ch02-anatomy · 四回路骨架

《开源 Agent 架构技术全书》第 2 章「Agent 系统的最小解剖：四回路架构」的配套示例。

一个 149 行的最小 ReAct 骨架，把 Agent 的一次迭代拆成四个回路：**感知 → 行动 → 反馈 → 规划**，
外加四个横切面：**状态 / 记忆 / 工具 / 护栏**。这个文件实现的是四个回路和其中两个横切面
（状态、工具）；缺哪些、为什么缺，第 2 章第四节有逐条说明。

## 依赖

- Python 3.9 或更高版本
- **mock 模式（默认）：零第三方依赖**，只用标准库，不需要 API Key，不联网即可跑通完整回路
- live 模式：同样只用标准库 `urllib`，无需 `pip install` 任何包

不需要虚拟环境，不需要 `pip install`，克隆下来直接跑。

## 安装

本目录没有需要安装的东西。若希望在一个干净环境里运行：

```bash
cd code/ch02-anatomy
python main.py
```

`python` 换成 `python3` 同样可用。

## 运行

```bash
# 1) 默认目标：内置 mock 策略，离线跑通
python main.py

# 2) 自定义目标
python main.py --goal "订单 A1001 能不能退"
python main.py --goal "订单 Z9999 能不能退"      # 演示查不到时的反馈修正
python main.py --goal "所有已签收的订单能退吗"

# 3) 切换到真实模型
python main.py --live
```

mock 模式的典型输出：

```
目标：订单 A1001 能不能退  模式：mock  步数：3/6
  感知 <- [search_orders] 命中 1 个订单
         data={"orders": [{"order_id": "A1001", "status": "已签收", "paid": 199.0, "days": 3}]}
  感知 <- [check_refund_policy] 签收后第 3 天，在退货期内
         data={"order_id": "A1001", "refundable": true, "amount": 199.0, "reason": "签收后第 3 天，在退货期内"}
结论：订单 A1001 可以退款：签收后第 3 天，在退货期内
终止依据：goal_met
```

输出里每一行 `感知 <- [...]` 就是感知回路的产物 `Observation`：它把工具的原始返回规范化，
并区分**成功 / 空结果 / 失败**三种情况。最后的 `终止依据` 由反馈回路给出，
取值 `goal_met`（目标达成）、`no_progress`（同样的动作加同样的结果，判定卡住）、
`budget`（步数上限用尽）。

## `--live` 所需环境变量

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | 是 | 无 | 模型接口的 API Key。缺失时程序以退出码 2 退出并给出提示 |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | OpenAI 兼容接口的基地址，可指向自建网关或其他兼容服务 |
| `OPENAI_MODEL` | 否 | `gpt-4o-mini` | 模型名，换成你账号实际可用的模型 |

```bash
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=gpt-4o-mini          # 可选
export OPENAI_BASE_URL=https://api.openai.com/v1   # 可选
python main.py --live
```

Windows PowerShell：

```powershell
$env:OPENAI_API_KEY = "sk-..."
python main.py --live
```

live 模式的请求体遵循 Chat Completions 的 `POST /chat/completions`，但**不使用 tools /
function-calling 协议**：模型只被要求输出一个 JSON 动作（`{"tool": ..., "args": ...}` 或
`{"final": ...}`），真正的调用由行动回路执行。这样模型与工具之间隔着一层结构化的决策对象，
四回路的边界不会被协议细节抹平。模型返回带 markdown 围栏的 JSON 也能正确解析。

## 文件里有什么

| 位置 | 内容 |
| --- | --- |
| `Observation` | 感知回路的产物：来源、成功标志、结构化数据、一句话摘要 |
| `Decision` | 规划回路的产物：工具名与参数，或最终答案 |
| `AgentState` | 状态横切面：唯一真相源，含观测、裁剪后的摘要、终止依据 |
| `search_orders` / `check_refund_policy` | 工具横切面：行动回路唯一能触达世界的地方 |
| `perceive` | 感知回路：原始返回 → 结构化观测，区分成功 / 空结果 / 失败 |
| `act` | 行动回路：决策 → 世界，副作用只发生在这里 |
| `feed_back` | 反馈回路：观测写回状态，判定进展与终止 |
| `mock_policy` / `live_policy` | 规划回路：mock 是脚本化的假模型，live 是真实模型 |
| `run` | 主循环：把四个回路串起来，控制流只在规划回路分叉 |

## 这个骨架没有的东西

刻意留白，用来对照第 2 章第四节：

- **记忆（Memory）**：没有跨迭代、跨会话的长期知识，观测只留在本次运行的内存里
- **护栏（Guardrails）**：没有输入输出校验，没有副作用确认，行动回路的调用不做任何拦截
- **检查点（Checkpoint）**：状态没有落盘，进程一挂就全丢，无法恢复与回放
- **幂等键、超时、并发工具调用**：行动回路是最朴素的同步单调用
- **上下文预算**：摘要没有长度上限，观测增多时上下文会线性膨胀
- **并行决策**：规划回路每轮只产出一个 `Decision`，不支持一次规划多个动作
