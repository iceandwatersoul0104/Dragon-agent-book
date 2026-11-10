# ch16 三条轻量路线最小可实现

对应《开源 Agent 架构技术全书》第 16 章《PydanticAI、Agno 与 smolagents：类型驱动与轻量路线》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**：用 1121 行代码把三个框架的核心机制各写一遍，放在同一个文件里对照。

- **路线一 PydanticAI 风格**：依赖注入 + 输出类型契约 + 校验失败让模型重试 + 测试替身
- **路线二 Agno 风格**：极简 `Agent` / `Team` / `Workflow` + 知识层
- **路线三 smolagents 风格**：`CodeAgent`（模型写代码）与 `ToolCallingAgent`（模型发 JSON）

三个场景跑**同一个任务**：统计订单各状态的数量，找出金额最大的一单。

> 说明：本目录不是这三个框架的移植版，是**教学用的最小复刻**。
> 它用标准库复现了依赖注入、输出契约、校验重试、知识检索、代码沙箱执行等运行语义，
> 目的是让你看懂机制；生产项目请直接用对应框架。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch16-light-frameworks
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

```bash
python main.py                     # 跑全部场景
python main.py --demo pydanticai   # 只跑类型驱动路线
python main.py --demo agno         # 只跑极简路线
python main.py --demo smolagents   # 只跑代码优先路线
python main.py --demo compare      # 三条路线并排对比
python main.py --trace             # 打印提示词与工具调用
```

## 路线一：类型驱动（PydanticAI 风格）

模型先查工具，再交产出；产出**两次不合格**，被校验器打回重做：

```
依赖：tenant=acme，orders=6 条（靠 deps 传进来，不进提示词）
模型调用：4 次，请求数 4

对话里模型收到的纠错反馈：
  -> 输出不合格：字段 top_amount 类型不对：期望 float，实际 str，请按契约重做
  -> 输出不合格：total 与 status_counts 之和对不上，请按契约重做

最终产出（已经被校验器确认符合契约）：
  OrderDigest(total=6, status_counts={'shipped': 3, 'pending': 2, 'cancelled': 1},
              top_order_id='ord-44', top_amount=1240.0)
  类型：OrderDigest
```

两道检查分工明确：**字段类型**由类型系统查（`top_amount` 传了字符串），
**字段之间是否自洽**由校验器查（`total` 和各状态数量之和对不上）。后者类型系统管不了。

## 路线二：极简 API（Agno 风格）

```
构造 Agent 的代码量：一个名字、一个模型、一条指令、两个可选参数（tools / knowledge）。
知识层命中：['订单状态说明', '退款政策']

产出（内容是自由文本，没有类型契约）：
  共 6 单：shipped 3 单、pending 2 单、cancelled 1 单；金额最大的是 ord-44，1240 元。
```

知识库命中就自动拼进上下文，团队按知识命中路由成员。代价是产出是自由文本，下游要自己解析。

## 路线三：代码优先（smolagents 风格）

```
【CodeAgent】模型写了一段 Python：
  orders = fetch_orders()
  counts = {}
  for o in orders:
      counts[o['status']] = counts.get(o['status'], 0) + 1
  top = max(orders, key=lambda o: o['amount'])
  print('Final Answer: 共 %d 单，%s；金额最大 %s，%.2f' % (...))
  执行输出即最终答案：共 6 单，{'shipped': 3, 'pending': 2, 'cancelled': 1}；金额最大 ord-44，1240.00
  模型调用 1 次

【ToolCallingAgent】模型发 JSON 工具调用：
  {"name": "fetch_orders", "args": {}}
  模型调用 2 次，工具调用 1 次
```

**关键差别**：CodeAgent 在**一次**回复里就把循环、工具调用和计算全写完；
而且按 smolagents 的约定，代码里 `print("Final Answer: ...")` 的输出**直接就是最终答案**，
不必再让模型复述一遍——所以 1 次模型调用就结束了。ToolCallingAgent 则是一个工具一次调用，
聚合计算还得模型自己在文本里做。

### ⚠️ 沙箱说明

代码智能体那一路会执行模型生成的 Python。本文件用**受限 builtins 白名单**跑它：
不给 `import`、不给 `open`、不给 `eval`，只够做算术和调用已注册的工具。

**这不足以当安全边界。** `--live` 模式下执行的是真实模型生成的代码，
生产环境必须放进容器沙箱（Docker、gVisor、独立的子进程 + 资源限制），
不要在宿主机上直接跑。

## 三条路线并排对比

```
维度             PydanticAI 型               Agno 型                 smolagents 型
--------------------------------------------------------------------------------------
模型调用次数         4 次                        1 次                    2 次
产出类型           OrderDigest（数据类）           str（自由文本）              str（自由文本）
产出有校验          有：类型 + 语义，不合格重做            无                      无
依赖怎么传          deps 注入，不进提示词              全局/构造时绑定               全局/构造时绑定
能离线测吗          能：换测试替身即可                  能：换模型即可                能：换模型即可
多步编排           自己写循环                      自己写 Workflow           自己写循环
```

注：类型驱动路线那 4 次调用里，有 2 次是模型**故意**交错产出后被校验器打回重做——
这正是输出契约的账：多几次往返，换下游不用写解析和防御。

## 真实模型（`--live`）

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
python main.py --live
```

Windows PowerShell：

```powershell
$env:LLM_API_KEY="sk-xxxxxxxx"
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="gpt-4o-mini"
python main.py --live
```

端点需兼容 `POST {base}/chat/completions`：请求体含 `model` 与 `messages`，返回体取
`choices[0].message.content`。换供应商只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

live 模式下换成真模型的有三处：

| 场景 | 真实模型要做的事 |
| --- | --- |
| 类型驱动路线 | 按工具 schema 发工具调用、按契约返回 JSON 产出 |
| 极简路线 | 直接回答（产出仍是自由文本） |
| 代码路线 | 写一段 Python（会被执行） |

**依赖注入、输出契约、校验重试、知识检索、沙箱执行全部不变**——这正是要验证的：
换个模型之后，框架那层的保证还在不在。

### mock 说明（重要）

内置的"模型"是按脚本返回固定内容的替身，不是真实模型。
它不代表任何真实模型的表现——真实模型写出来的措辞、条数、工具选择每次都可能不同。
要评估真模型的行为，用 `--live`。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--demo` | `all` | `all` / `pydanticai` / `agno` / `smolagents` / `compare` |
| `--trace` | 关 | 打印提示词与工具调用 |
| `--live` | 关 | 切真实模型 API |
| `--model` | 空 | 覆盖 `LLM_MODEL` |
| `--base-url` | 空 | 覆盖 `LLM_BASE_URL` |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `Deps` / `RunContext` | 依赖注入：依赖按次运行传入，不进提示词 |
| `schema_from_func()` | 从签名和 docstring 生成工具 schema——类型提示即契约 |
| `ModelRetry` | 工具或校验器判定不行，让模型带着理由重来 |
| `TypedAgent` | `deps_type` / `output_type` / `retries` 三个核心参数 |
| `TypedAgent._validate_output()` | 三步：构造目标类型 → 查字段类型 → 跑语义校验 |
| `ScriptedModel` | 测试替身：把模型每一步钉死，测试不用真调 API |
| `MiniAgent` / `MiniTeam` / `MiniWorkflow` | 极简 API 路线 |
| `Knowledge` | 知识层：切块 + 检索 + 命中自动入上下文 |
| `CodeAgent` / `ToolCallingAgent` | 代码优先与工具调用两条路径 |
| `run_sandboxed()` | 受限 builtins 白名单执行模型生成的代码 |
| `LiveCodeAgent` / `LiveToolAgent` | live 模式下的两个智能体 |

### 三个值得抄走的实现细节

1. **工具 schema 从函数签名长出来**。名字是函数名，描述是 docstring，参数来自类型注解——不用另外手写一份，签名改了 schema 自动跟着改。
2. **输出校验分两层**。字段类型用类型系统查（注意 `from __future__ import annotations` 之后字段类型是字符串，必须用 `get_type_hints` 还原，否则检查形同虚设）；字段之间是否自洽用校验器查。
3. **`print("Final Answer: ...")` 就是最终答案**。代码路径不用把结果再喂回模型让模型复述，省一次模型往返——这是代码智能体相对工具调用智能体最实在的一处优势。

## 换成自己的场景时要改什么

1. 改 `OrderDigest` 换你的输出契约——字段名、类型就是模型必须满足的形状；
2. 改 `build_typed_agent()` 换工具和校验器；
3. 改 `Deps` 换你要注入的依赖（数据库连接、HTTP 客户端、当前用户）；
4. 改 `ORDERS` / `fetch_orders()` 换数据源和工具；
5. 改 `run_sandboxed()` 的白名单要谨慎——**加进去的每个 builtins 都是安全边界的松动**。

## 相关章节

- 第 8 章：工具调用契约（schema 从签名生成的具体收益）
- 第 9 章：Code-Act（代码优先路线的完整讨论与沙箱要求）
- 第 12 章：LangChain 与 LangGraph（重编排路线时的选择）
- 第 16 章正文：三条路线的完整对比与选型判据
