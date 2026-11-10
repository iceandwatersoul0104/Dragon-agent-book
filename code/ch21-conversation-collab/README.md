# ch21 对话式协作：GroupChat 与 CAMEL 角色扮演 最小可实现

对应《开源 Agent 架构技术全书》第 21 章《对话式协作：GroupChat 与 CAMEL 角色扮演》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**。一次跑清五件事：

- **inception prompt 范式**：任务 + 角色设定 + 对话对象 + 输出格式，四段拼装出可用的角色扮演提示；
- **四种 speaker selection**：轮转 / 自动 / 手动 / 代码规则，含各自的模型调用次数；
- **对话协议**：结构化发言、`[DONE]` 终止词、`[INTERRUPT:名字]` 打断，打断优先级最高；
- **三种上下文策略**：共享全量历史 / 分区历史 / 共享摘要（黑板）的输入量差异；
- **token 放大系数**：按"内容下界"实测每个方案的总花费，并给出单 Agent 对照行。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch21-conversation-collab
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

### mock 模式（默认，无需 API Key）

```bash
python main.py                                # 协议演示 + 成本对照
python main.py --trace --mode shared          # 打印逐轮 token 明细
python main.py --selection auto               # 换选人策略（每次发言多一次仲裁调用）
python main.py --selection manual             # 手动选人（用预置顺序代替交互）
python main.py --selection callable           # 代码规则选人
python main.py --turns 20 --mode summary      # 换发言次数与上下文策略
```

### 典型输出

**A. inception prompt（节选）**

```
你正在参与一场多角色讨论。

【任务】为客服团队做一套退款与赔付工单的自动核对报表，要求口径统一、可追溯、每日八点前出数。

【你的角色】你是架构师。负责技术方案与容量估算，讨厌没有回滚路径的设计。你的风格：先讲约束，再讲方案，最后讲代价。你的目标：在现有技术栈内给出可运维、可回滚的方案。

【对话对象】需求分析师（负责把用户场景和验收口径讲清楚，讨厌模糊需求）、测试工程师（负责找出会上线出事的分支，讨厌只测正常路径）

【输出格式】每次只输出你自己的一段发言，100-150 字，不要替对方说话；需要对方回应时结尾写 [ASK:<对方名字>]；认为讨论已经收敛时结尾写 [DONE]；需要对方立刻接手时开头写 [INTERRUPT:<对方名字>]。
```

**B. 四种 speaker selection**

```
[round_robin] 发言顺序：需求分析师 → 架构师 → 测试工程师 → 需求分析师 → 架构师 → 测试工程师 → …
    调用 11 次｜终止原因 terminated
    第 9 轮 测试工程师：打断 → 架构师
    第 11 轮 架构师：命中终止词

[auto] 发言顺序：需求分析师 → 架构师 → 测试工程师 → 需求分析师 → …
    调用 23 次｜终止原因 terminated
```

`auto` 比 `round_robin` 贵在调用次数：每说一句话之前，先要开一次会决定谁说。

**C. 上下文策略与 token 放大系数**

```
方案                           轮次    调用    总token    内容下界   放大系数 A
3 人 · 共享全量历史            60      60      170049     4736         35.9x
3 人 · 分区历史                60      60       78910     4736         16.7x
3 人 · 共享摘要                60      60       45660     4736          9.6x
单 Agent 顺序扮演（摘要上下文） 60      60       42138     4736          8.9x

共享全量历史比共享摘要多花 3.7 倍；一个人带摘要（42138）比三个人共享全量历史（170049）省 4.0 倍。
结论：贵的不是参与者数量，是共享了多少历史。
```

四行的产出完全相同：同样数量、同样文本的发言，变的只是每个发言者能看到多少历史。

**D. 逐轮明细（`--trace --mode shared`）**

```
轮次    发言者         prompt     输出      备注
1       需求分析师     356        94
2       架构师         457        87
3       测试工程师     551        87
4       需求分析师     640        85
5       架构师         732        83
6       测试工程师     822        87
7       需求分析师     911        84
8       架构师         1002       88
9       测试工程师     1096       95         打断 → 架构师
10      架构师         1194       87
11      架构师         1286       65         命中终止词
```

prompt 随轮次线性增长，这就是共享全量历史二次方膨胀的来源。

### mock 说明

内置模型按角色返回固定发言，不做真实推理。**结构全部真实**：inception prompt 拼装、
四种选人策略、终止与打断判定、可见历史的三种渲染、token 计量与放大系数计算。

成本对照用的是 `scenario="plain"`（不带任何控制标记），保证四种方案的内容完全一致；
协议演示用 `scenario="control"`，会在固定位置插入 `[INTERRUPT:名字]` 与 `[DONE]`。

token 数是粗估值（中文按字、其余按 3.5 字符/token）；真实模型以服务端 `usage` 为准。
但"共享全量历史是二次方增长"这个结构在任何模型上都成立，因为它是上下文长度的算术。

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
python main.py --live --trace
```

Windows PowerShell：

```powershell
$env:LLM_API_KEY="sk-xxxxxxxx"
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="gpt-4o-mini"
python main.py --live --trace
```

端点需兼容 `POST {base}/chat/completions`：请求体含 `model` 与 `messages`，返回体取
`choices[0].message.content`。换供应商时只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

live 模式下真实模型不保证输出 `[DONE]` 或 `[INTERRUPT:名字]`，所以终止主要靠
`--turns` 的轮次上限兜底——这本身就是生产里必须三层兜底的原因。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--turns` | `60` | 最大发言次数（不是"轮"，一轮可能有多次发言） |
| `--mode` | `shared` | `shared` / `partitioned` / `summary` 上下文策略 |
| `--selection` | `round_robin` | `round_robin` / `auto` / `manual` / `callable` 选人策略 |
| `--live` | 关 | 切真实模型 API |
| `--model` / `--base-url` | 环境变量 | 覆盖 `LLM_MODEL` / `LLM_BASE_URL` |
| `--trace` | 关 | 打印单次会话的逐轮明细与选人开销 |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `inception_prompt()` | CAMEL 式角色扮演提示的四段结构 |
| `FORMAT_SPEC` | 对话协议：发言格式、`[ASK:]`、`[DONE]`、`[INTERRUPT:]` |
| `GroupChat._select_speaker()` | 四种 speaker selection，打断优先级最高 |
| `GroupChat._render_history()` | shared / partitioned / summary 三种可见历史 |
| `GroupChat._rollup()` | 滚动摘要（示例里是截断近似，生产要换成真摘要） |
| `GroupChat.run()` | 主循环：选人 → 发言 → 写消息表 → 终止判定 |
| `CostReport.amplification` | 放大系数 A = 总花费 / 内容下界 |
| `compare_modes()` | 四种方案的成本对照，产出内容完全一致 |
| `MockLLM` / `LiveLLM` | 确定性脚本与真实模型 |

## 换成自己的场景时要改什么

1. 改 `ROLES`：每个角色的 `persona` 写"他反对什么"，`goal` 写"他要什么"；
2. 改 `FORMAT_SPEC`：把控制标记对齐你自己的协议（`[DONE]` 换成你的收敛标志）；
3. 改 `TASK` 与 `SCRIPT`：真实场景不需要 `SCRIPT`，但要保证各角色的目标确实有冲突，
   否则角色扮演只是给同一个答案套三个名字；
4. 改 `_rollup()`：换成真正的摘要器，并给一条硬规则——**结论必须进摘要，讨论过程可以丢**；
5. **终止要做三层兜底**：协议终止词、`--turns` 轮次上限、预算上限（见第 11 章）；
6. **调上下文策略要靠数据**：用同一批 case 分别跑 shared / partitioned / summary，
   比总 token、主上下文大小和结论一致性，不要凭感觉选。

## 相关章节

- 第 11 章：停止准则、步数上限与成本守卫（对话的轮次上限与预算兜底）
- 第 20 章：多 Agent 的动机与何时不该上多 Agent（先判断该不该开会）
- 第 22 章：SOP 流水线（流程固定时，用流水线而不是对话）
- 第 23 章：协作拓扑与调度（Supervisor、层级、网状与黑板）
- 第 24 章：Agent 间通信协议（跨进程的对话消息契约）
