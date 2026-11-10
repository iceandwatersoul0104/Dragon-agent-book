# ch07 规划范式最小可实现

对应《开源 Agent 架构技术全书》第 7 章《规划范式：Plan-and-Execute、ReWOO 与搜索型推理》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**，一次跑清四件事：

- 先规划后执行（`plan_execute`）：计划是可编辑工件，执行期模型在线；
- 变量引用预解耦（`rewoo`）：执行期不需要模型，只调 1 次；
- 计划过时的三种处理：局部修补（`patch`）与全量重规划（`replan`）；
- 搜索型推理：分层展开 + 评估函数，对照贪心单路径。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch07-planning
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

### mock 模式（默认，无需 API Key）

```bash
python main.py                                  # plan_execute + patch
python main.py --form rewoo                     # 执行期无模型的 ReWOO
python main.py --form plan_execute --policy replan
python main.py --demo search                    # 搜索型推理 vs 贪心
python main.py --demo all                       # 三组范式 + 搜索，一次跑完
python main.py --trace                          # 打印计划与逐步事件
```

`--demo all` 的输出：

```
form / policy            llm_calls  tool_calls  status
plan_execute / patch             6           5  solved
plan_execute / replan           10           8  solved
rewoo / patch                    1           5  solved
```

三个数字说明两件事：

- **ReWOO 只用 1 次模型调用**跑完全程，另外两种分别用了 6 次和 10 次——省掉的是"执行期每步都要模型在线"；
- **全量重规划比局部修补多花 4 次模型调用、多跑 3 次工具**，因为前三步的成果作废重做。

搜索对照：

```
候选顺序     : 深圳→上海→北京→杭州→成都
贪心单路径   : 评估 2 次 | 路径 深圳→上海 走死 | 结果=无可行方案
分层搜索     : 评估 13 次 | 最优组合 上海→成都，客户数 5 | 结果=可行方案 上海→成都
```

场景设定：选两个城市开客户见面会，预算 5000，必须含成都，收益是能见的客户数。
模型第一顺位推荐深圳（客户最多），但深圳跟任何城市组合都超预算——**贪心单路径就此走死**，
分层搜索换一个分支才找到最优解。这就是搜索型推理要付钱买的东西。

### 场景任务

默认任务：读 `data.csv` → 汇总订单 → 写 `report.md` → 发给 `boss@example.com`。
第四步会失败：`boss@example.com` 不在通讯录里。这是一个**局部的**计划过时——前三步结果照旧可用。

## mock 到底做了什么（重要，别误读）

内置的"模型"**不是真的在推理**，它按角色返回预设内容：

- `plan`：返回四步计划（其中 `rows="#E1"` 这类变量引用）；
- `replan`：返回修正收件人后的完整计划；
- `resolve`：原样回显当前步骤（真实模型在这里把 `#E1` 填成实参）；
- `search`：返回候选城市的偏好顺序。

工具环境是本地确定性模拟：`read_csv` / `aggregate` / `write_report` / `send_email`，
其中"收件人不在通讯录"这个失败是固定可复现的。局部修补走的是一份通讯录映射
（`boss@example.com` → `manager@example.com`），不经过模型——这模拟的是"工具契约层的修复"。

**真实结论要用 `--live` 跑。**

### 真实模型（`--live`）

需要环境变量：

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_API_KEY` | 是 | — | 模型服务商的 API Key |
| `LLM_BASE_URL` | 否 | `https://api.openai.com/v1` | OpenAI 兼容的 chat completions 端点 |
| `LLM_MODEL` | 否 | `gpt-4o-mini` | 模型名 |

```bash
export LLM_API_KEY=sk-xxxxxxxx
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=gpt-4o-mini
python main.py --live --form rewoo
```

Windows PowerShell：

```powershell
$env:LLM_API_KEY="sk-xxxxxxxx"
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="gpt-4o-mini"
python main.py --live --form rewoo
```

端点需兼容 `POST {base}/chat/completions`：请求体含 `model` 与 `messages`，返回体取
`choices[0].message.content`。换供应商时只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

live 模式下模型要按固定格式产出计划：一行一步，`tool(arg="value")`，变量引用用 `#E1`。
解析器会忽略不合法的行；模型漏写变量引用时，该步参数按空串处理。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--form` | `plan_execute` | `plan_execute` / `rewoo` |
| `--policy` | `patch` | 计划过时处理：`patch` 局部修补 / `replan` 全量重规划 |
| `--demo` | `run` | `run` / `search` / `all` |
| `--task` | 内置任务 | 换成自己的任务描述 |
| `--live` | 关 | 切真实模型 API |
| `--temperature` | `0.0` | live 模式采样温度 |
| `--model` | 空 | 覆盖 `LLM_MODEL` |
| `--base-url` | 空 | 覆盖 `LLM_BASE_URL` |
| `--trace` | 关 | 打印计划与逐步事件 |

退出码：`0` 全部跑通，`1` 有任务未解决，`2` 缺 `LLM_API_KEY`。

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `Plan` / `Step` / `patch()` | 计划是可编辑工件：结构化对象，能渲染、能局部改 |
| `parse_plan()` | 一行一步的解析器，容忍非法行 |
| `resolve_arg()` | 变量引用 `#E1` 的解引用，ReWOO 的核心 |
| `Planner.run()` | 执行循环 + 计划过时的三条处理路径 |
| `local_patch()` | 局部修补：确定性地改参数，不调模型 |
| `evaluate()` / `beam_search()` / `greedy_path()` | 搜索型推理：分层展开 + 评估函数，对照贪心 |
| `MockLLM` / `LiveLLM` | 行为模拟器与真实模型 |

## 换成自己的任务时要改什么

1. 改 `TOOL_SPEC` 与 `TOOLS`：让工具的入参、返回值和你的真实环境对上；
2. 改 `MockLLM.PLAN`：把四步换成你的计划文本；
3. 改 `local_patch()`：定义"参数层错误"怎么修（查通讯录、换默认分支……）；
4. 改 `REPLAN_PROMPT`：重规划时要把哪些已完成结果带回去——这是全量重规划能不能复用什么的关键；
5. 搜索型演示换成你自己的组合优化问题，但**必须保留一个独立的评估函数**，
   否则搜索就退化成"多生成几个答案再挑一个看起来顺眼的"。

## 相关章节

- 第 4 章：ReAct（单步交错执行 vs 提前规划）
- 第 6 章：反思与自纠错（重规划触发信号之一的来源）
- 第 8 章：工具调用契约（计划里的 `tool(arg=value)` 怎么落到真实 API 上）
- 第 10 章：上下文工程（计划与执行历史占多少上下文）
- 第 11 章：停止准则、步数上限与成本守卫
