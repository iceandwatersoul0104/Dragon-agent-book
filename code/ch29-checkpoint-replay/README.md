# ch29-checkpoint-replay —— 检查点、回放与多 Agent 共享状态最小可实现

第 29 章《检查点、回放与多 Agent 共享状态》的配套代码。

用一个三角色（planner / buyer / approver）的采购审批流程，把检查点的两种模式、时间旅行调试、崩溃恢复、以及多 Agent 共享状态的四种模式全部跑一遍，并给出可比较的数字。

## 目录

```
ch29-checkpoint-replay/
├── main.py             # 检查点 / 回放 / 共享状态
└── README.md           # 本文件
```

## 依赖

- Python 3.9 或更高
- **仅使用标准库**，不需要 `pip install` 任何东西
- `--live` 模式需要能访问 OpenAI 兼容的 chat/completions 接口（可选）

## 安装

没有安装步骤，下载后直接跑：

```bash
cd ch29-checkpoint-replay
python main.py
```

Windows 控制台若出现中文乱码：

```powershell
$env:PYTHONIOENCODING="utf-8"; python main.py
```

## 运行命令

| 命令 | 作用 |
|---|---|
| `python main.py` | 跑全部五段演示 |
| `python main.py --only storage` | 只看检查点的存储成本对比 |
| `python main.py --only replay` | 只看两种模式回放到同一步的一致性 |
| `python main.py --only time-travel` | 只看时间旅行与分支对比 |
| `python main.py --only recovery` | 只看崩溃恢复与幂等去重 |
| `python main.py --only shared` | 只看多 Agent 共享状态的四种模式 |
| `python main.py --live` | 最终答复改由真实 LLM 生成 |

## `--live` 所需环境变量

| 变量 | 必需 | 默认值 | 说明 |
|---|---|---|---|
| `OPENAI_API_KEY` | 是 | 无 | 未设置时打印告警并自动回退 mock |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | 任意 OpenAI 兼容网关 |
| `OPENAI_MODEL` | 否 | `gpt-4o-mini` | 生成答复用的模型 |

```powershell
$env:OPENAI_API_KEY="sk-..."
python main.py --live
```

调用失败（网络错误、返回不是合法 JSON）时会打印告警并回退 mock，不会让链路挂掉。

## 输出怎么读

- **检查点的存储成本** —— 60 步、状态随步数增长的流程里，快照模式与事件模式各自的总字节数。快照每步存整份状态，是双重增长；事件只存变化，是线性。
- **回放一致性** —— 两种模式回放到同一步，状态必须一致（代码里做的是相等断言）。
- **时间旅行** —— 回到第 1 步之后，把 widget 单价从 1200 改成 1800 重跑，给出分支答案与逐字段差异。分支跑在沙箱里，副作用不落库。
- **恢复语义** —— 在"工具已执行、检查点未写入"的瞬间崩溃，从最后一份检查点恢复，统计工具调用次数、真实执行次数与幂等命中次数。
- **多 Agent 共享状态** —— 共享内存 / 共享文档 / 消息传递 / 共享存储四种模式在"丢失更新、冲突检测、往返轮数、状态可回放性"上的差异。

## 关键实现点

| 主题 | 位置 | 说明 |
|---|---|---|
| 事件记录 | `Store.set()` | 每次写入同时产生一个事件，事件即状态的可重放表示 |
| 快照模式 | `CheckpointStore("snapshot")` | 每步存整份 `state`，恢复 O(1)，存储双重增长 |
| 事件模式 | `CheckpointStore("event")` | 每步只存该步的事件，恢复靠重放 |
| 幂等去重 | `ToolRunner.call()` | 幂等键命中即复用结果，不真正执行副作用 |
| 时间旅行 | `run_flow(..., price_override=, skip_through=)` | 从任意历史步改输入重跑，分支差异由 `diff_states()` 给出 |
| 崩溃恢复 | `run_flow(..., crash_after_tool=)` | 模拟"工具执行后、检查点前"崩溃 |
| 共享存储冲突 | `SharedKV.put(expected=)` | 版本对不上抛 `ConflictError`，冲突显式化 |

## 局限（生产化时要换掉什么）

1. 检查点只写内存和 JSON，生产环境要用数据库或对象存储，并考虑压缩与保留期。
2. 没有并发与事务：真实系统里检查点写入本身也可能失败，需要处理"写了半条记录"。
3. 幂等表只在进程内，生产环境需要持久化到外部（幂等键通常由业务系统自己保证）。
4. 时间旅行分支是沙箱模拟，没有接入真实外部系统的 dry-run 能力。
5. 共享状态的四种模式是行为模拟，没有真实的并发调度与网络延迟。

生产环境对应替换：持久化检查点存储、带事务语义的写入、跨进程幂等表、外部系统的沙箱/试运行接口、以及真正的并发协调（版本号或租约）。其余接口不变。

## 对应章节

正文见 `../../vol1/part-4/ch29-checkpoint-replay.md`。
