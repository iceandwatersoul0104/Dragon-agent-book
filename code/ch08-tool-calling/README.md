# ch08 工具调用契约最小可实现

对应《开源 Agent 架构技术全书》第 8 章《工具调用契约：把 Function Calling 当作 API 工程》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**，一次跑清四件事：

1. **参数校验与错误回灌**：校验失败按 `raw` / `structured` / `suggestion` 三种形式回灌给模型；
2. **并行与依赖**：参数引用前序调用 id 的调用必须串行，其余并发执行；
3. **结果对齐**：并发完成顺序被打乱，回灌时仍按模型给出的调用顺序；
4. **幂等与不可重放**：写工具用幂等键去重重试；标注 `replayable=false` 的工具禁止自动重试。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch08-tool-calling
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

### mock 模式（默认，无需 API Key）

```bash
python main.py                              # 默认 suggestion 形式回灌
python main.py --error-form raw             # 原始报错回灌
python main.py --error-form structured      # 结构化错误回灌
python main.py --error-form suggestion       # 结构化错误 + 修复建议（默认）
python main.py --trace                       # 打印回灌载荷与重试记录
```

典型输出：

```
批次划分（批内并行，批间串行）：
  batch 1: call_1:get_weather, call_2:get_exchange_rate（2 个，并行）
  batch 2: call_3:compute_savings（1 个）
  batch 3: call_4:send_report（1 个）
  batch 4: call_1:get_weather, call_2:get_exchange_rate, call_5:transfer_funds（3 个，并行）
  batch 5: call_3:compute_savings（1 个）
  batch 6: call_4:send_report（1 个）
不可重放守卫：
  call_5 (transfer_funds) 未自动重试，状态未知
服务端幂等账本：
  send_report:2024-06-01:boss@example.com -> 请求 2 次，实际执行 1 次
  transfer_funds:ops-002:500 -> 请求 1 次，实际执行 0 次（未落库）
扣款流水： 无（未执行，转账状态未知）
```

四行重点：

- **batch 1 里两个调用并行**（天气慢 50ms、汇率快 10ms，完成顺序与请求顺序不同，但回灌仍按请求顺序）；
- **batch 2/3 是依赖调用**，必须等前面成功；
- **send_report 请求 2 次只执行 1 次**——第一次遇到上游超时，重试复用同一个幂等键，服务端去重生效；
- **transfer_funds 请求 1 次执行 0 次**——它标了 `replayable=false`，网关超时后状态未知，运行时拒绝自动重试，把错误交回模型；
  服务端同样拒绝无幂等键的重复请求，不会替你在不确定的状态上再扣一次。

### 脚本化的叙事

内置"模型"按提示特征走三步：

1. 第 1 轮：调用天气、汇率、计算、发日报，其中 `get_weather` 的 `unit` 写成 `kelvin`（不在枚举里）；
2. 第 2 轮：从回灌里读到校验错误，把 `unit` 改成 `celsius`，并把不可逆的划款放到最后；
3. 第 3 轮：读到 `NON_REPLAYABLE` 错误，停止调用，交回人工。

**mock 说明**：内置"模型"不是真的在推理，它按提示特征返回预设的调用序列。但校验、瞬时错误、幂等去重、依赖分批、结果对齐、类型归一都是真实逻辑。

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
`choices[0].message.content`。换供应商时只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

live 模式下模型按 JSON 数组输出调用列表：

```json
[{"id": "call_1", "name": "get_weather", "arguments": {"city": "上海", "unit": "celsius"}}]
```

依赖用 `{{call_id.字段名}}` 表达。解析器容忍多余文本（只取第一个 `[` 到最后一个 `]` 之间的内容），
解析失败的行直接丢弃。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--error-form` | `suggestion` | 回灌形式：`raw` / `structured` / `suggestion` |
| `--max-rounds` | `4` | 模型调用轮次上限 |
| `--max-parallel` | `4` | 并发上限 |
| `--live` | 关 | 切真实模型 API |
| `--temperature` | `0.0` | live 模式采样温度 |
| `--model` | 空 | 覆盖 `LLM_MODEL` |
| `--base-url` | 空 | 覆盖 `LLM_BASE_URL` |
| `--trace` | 关 | 打印回灌载荷与重试记录 |

退出码：`0` 正常结束，`2` 缺 `LLM_API_KEY`。

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `ToolSpec` | 契约本体：描述、参数 schema、返回契约、副作用与可重放标注 |
| `validate()` | 运行时校验：必填、类型、枚举 |
| `build_error_payload()` | 三种回灌形式：raw / structured / suggestion |
| `resolve_args()` / `coerce_args()` | 引用解析与类型归一 |
| `failed_deps()` | 依赖失败要明确报出，而不是变成下游的类型错误 |
| `run_batches()` | 按引用关系分批：批内并发、批间串行，结果按 id 归档 |
| `build_tool_message()` | 回灌按模型请求顺序，失败项也占位 |
| `execute_call()` | 重试策略 + 幂等键 + 不可重放守卫 |
| `h_send_report()` / `h_transfer_funds()` | 模拟服务端：瞬时错误、幂等去重、状态未知 |

## 换成自己的工具时要改什么

1. 改 `TOOLS`：为每个工具写 `description`、`params`（schema）、`returns`、`side_effect`、`replayable`；
2. 改 `h_*` 处理器：换成真实接口调用；写工具要实现幂等键去重；
3. 改 `key_builder`：幂等键必须从业务语义派生，重试复用同一个键；
4. 改 `MockLLM`：换成你的调用序列；
5. 把 `replayable=False` 用在真的不能重放的工具上——发送、扣款、提交、一次性 token。

## 相关章节

- 第 4 章：ReAct（动作解析与 Observation 回灌）
- 第 7 章：规划范式（计划里的 `tool(arg=value)` 怎么落到真实接口上）
- 第 10 章：上下文工程（工具描述与回灌载荷占多少上下文）
- 第 11 章：停止准则、步数上限与成本守卫
