# ch33 可观测性 —— 配套示例

《开源 Agent 架构技术全书》第 33 章的参考实现。四件事做成可运行的最小闭环：

1. **Trace/Span 运行时**：trace 边界是"一次任务"，span 用 parent_id 串成因果树，自动计时，异常自动归类到固定错误枚举；
2. **LLM span**：记模型、token、参数、首 token 延迟与总延迟，按本地价目表算成本；
3. **脱敏与采样**：字段级 allowlist + 正则脱敏；按任务粒度采样并记录采样率；
4. **SLI 计算**：任务成功率、步数分布、工具错误率、无进展率、每次成功成本——全部来自全量计数指标，**不是**从采样后的 trace 里聚合。

## 依赖

- Python **3.8+**，**只用标准库**，无需 `pip install`；
- 默认 mock 模式**不需要任何 API Key**，可直接跑通三个任务并输出 SLI 报表；
- `--live` 才需要 `OPENAI_API_KEY`。

## 安装

```bash
cd code/ch33-observability
python -c "import sys; print(sys.version)"
```

Windows 下用 `py -3` 代替 `python`。

## 运行

```bash
# 1) 三个 mock 任务（正常完成 / 工具超时后重试成功 / 连续空转后放弃）+ SLI 报表
python main.py

# 2) 按任务粒度抽样：ratio 模式保留一半 trace
python main.py --sampling ratio --ratio 0.5

# 3) 只保留"出过问题"或整体过慢的 trace（error_or_slow）
python main.py --sampling error_or_slow --slow-ms 1

# 4) 真实 API 跑一轮任务并记录真实 span
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.openai.com/v1     # 可选，任意 OpenAI 兼容端点
export OPENAI_MODEL=gpt-4o-mini                       # 可选
python main.py --live

# 5) 指定 trace 落盘位置
python main.py --audit ./traces.jsonl
```

主要参数：`--live`、`--model`、`--base-url`、`--audit`、`--sampling {always,ratio,error_or_slow}`、`--ratio`、`--slow-ms`。

## `--live` 所需环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `OPENAI_API_KEY` | 是 | — | OpenAI 兼容接口的密钥 |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | 接口根地址，可指向任意兼容端点或本地网关 |
| `OPENAI_MODEL` | 否 | `demo-live` | 模型名；不在本地价目表里时回落到默认费率 |

## 三个 mock 任务

| 任务 | 场景 | 演示的指标行为 |
|---|---|---|
| T-10231 | 退款咨询，正常完成 | 步数分布的正常基准 |
| T-10255 | 工单查询，工具超时后重试成功 | 工具错误率、重试记在同一个 span 的 `retry.count` 上 |
| T-10301 | 资料收集，连续空转后放弃 | 无进展率：连续 3 步没有新状态即判定空转 |

## 输出说明

每个任务打印 span 明细（`llm.chat` / `tool.call` 的状态、耗时、模型、成本、错误类型、重试次数），最后打印 SLI 报表：任务成功率、步数分布 p50/p90/p99、工具调用与错误率（按工具分桶）、无进展率、每次成功成本、成本按模型分布、Trace 采样率。

`traces.jsonl` 每行一条 trace 记录，内含 `trace_id / task_id / sampled / spans[]`，span 记录 `name / span_id / parent_id / start_ms / duration_ms / status / attrs / events`。属性落盘前经过 allowlist 与正则脱敏，不在白名单的字段直接丢弃。

## 与正文的对应关系

| 正文位置 | 代码位置 |
|---|---|
| 2.1 trace 边界 = 任务 | `Tracer.trace()` / `Tracer.span()` 的 parent 链 |
| 2.2 span 命名与事件 | `Span.name` 约定、`Span.add_event()` |
| 2.3 字段设计与错误分桶 | `ALLOWED_ATTRS`、`ERROR_BUCKETS`、`RETRYABLE_ERRORS`、`classify_error()` |
| 2.4 任务成功率 / 步数分布 / 工具错误率 / 无进展率 / 每次成功成本 | `Metrics` + `print_sli()` |
| 2.5 脱敏与采样 | `redact_attrs()`（allowlist + 正则）、`SamplingPolicy`（按任务粒度） |
| 三、6 采样会污染指标 | SLI 只读 `Metrics`（全量计数），不读采样后的 trace |

## 已知限制

- 价格表是**示意值**（`demo-fast` / `demo-pro` / `demo-live`），只演示成本算法，生产环境要换成自己的价目表并定期更新；
- SLI 的"任务成功"在本例里由 mock 场景直接给出；真实系统需要任务级判定器（规则 + 抽检 + LLM Judge，见第 34 章）；
- 无进展检测只 diff 新增文件与 observation 哈希，信号偏少，真实系统要按业务补关键信号（数据库行数变化、文件变更、页面 DOM 差异）；
- 采样在内存里做，未实现远端写入、留存期到期删除与访问审计；
- 事件（events）当前只在本地 span 上保留，未做聚合与告警对接；
- 本例没有接任何时序库，指标全在内存里聚合，进程退出即丢失——真实系统应把 `Metrics` 推到 Prometheus / OTLP 端点。
