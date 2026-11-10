# ch32 提示注入与越权防护 —— 配套示例

《开源 Agent 架构技术全书》第 32 章的参考实现。四个部件：

1. **上下文分区**：不可信内容进 data 区，带来源与信任标记，分隔符被转义；
2. **策略引擎（在模型之外）**：工具分级 READ / WRITE / EXFIL / MEMORY，参数白名单、目的地白名单、DLP 扫描；
3. **独立审批通道**：审批器只收到结构化动作摘要，看不到原始不可信文本；
4. **长期记忆**：写入要求 provenance，不可信来源或指令式内容直接拒绝。

Mock 模型被刻意做成**易受攻击**的：它会扫描包括数据区在内的全部片段，发现注入模式就照做。演示的结论是——模型中招不可怕，可怕的是没有能力限制与审批兜底。

## 依赖

- Python **3.8+**，**只用标准库**，无需 `pip install`；
- 默认 mock 模式**不需要任何 API Key**，可直接跑通全部 6 个场景；
- `--live` 才需要 `OPENAI_API_KEY`。

## 安装

```bash
cd code/ch32-prompt-injection
python -c "import sys; print(sys.version)"
```

Windows 下用 `py -3` 代替 `python`。

## 运行

```bash
# 1) 全部场景（易受攻击的 mock 模型）
python main.py

# 2) 只跑某几个场景
python main.py --scenario direct,indirect

# 3) 换成"只看指令区"的严格模型，对比分区 + 标注能挡掉多少
python main.py --model-mode strict

# 4) 打印渲染后的完整 prompt（看指令区与数据区怎么分开）
python main.py --scenario indirect --show-prompt

# 5) 真实模型产生候选动作（候选同样要过策略引擎）
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.openai.com/v1     # 可选，任意 OpenAI 兼容端点
export OPENAI_MODEL=gpt-4o-mini                       # 可选
python main.py --live --scenario indirect
```

主要参数：`--scenario`、`--model-mode {vulnerable,strict}`、`--live`、`--model`、`--base-url`、`--audit`、`--show-prompt`。

## `--live` 所需环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `OPENAI_API_KEY` | 是 | — | OpenAI 兼容接口的密钥 |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | 接口根地址，可指向任意兼容端点或本地网关 |
| `OPENAI_MODEL` | 否 | `gpt-4o-mini` | 模型名 |

## 六个场景与预期

| 场景 | 注入形态 | 预期结果 |
|---|---|---|
| `benign` | 无 | 只读工具自动执行；写操作经独立通道确认后放行 |
| `direct` | 用户消息里夹带越权要求 | 导出客户清单、外发邮件全部被拦 |
| `indirect` | 工具返回内容里带指令 | 同上（间接注入不需要攻击者与 Agent 对话） |
| `memory` | 把不可信内容写进长期记忆 | 记忆写入被拒（来源不可信 + 内容含指令） |
| `exfil` | 目的地合法但载荷含密钥 | DLP 命中密钥，动作被拒 |
| `note` | 模型把注入文本写进工单备注 | 审批人看到载荷含指令式文本，独立通道拒绝 |

## 输出说明

每个场景打印：片段的 kind / trust / source → 模型的候选动作 → 策略引擎逐条判定 → 放行与拦截计数。审计文件 `audit.jsonl` 每行一条 JSON，含 `scenario / tool / args / decision / reason / tier`。

## 与正文的对应关系

| 正文位置 | 代码位置 |
|---|---|
| 2.1 / 2.4 L3 分区与来源标记 | `Segment` + `render_prompt()`（数据区分隔符转义） |
| 2.4 L1 能力最小化 | `TOOLS` 清单与 `ToolSpec.allowed_args` 参数白名单 |
| 2.4 L2 工具分级与审批 | `Tier`（READ/WRITE/EXFIL/MEMORY）+ `evaluate()` |
| 2.4 L4 输出净化与目的地校验 | `scan_secrets()`（DLP）+ `dest_allowed()`（目的地白名单） |
| 2.4 L5 双通道校验 | `summarize()` + `make_approver()`：审批器只收结构化摘要 |
| 2.2 记忆投毒 | `MemoryStore.write()` 要求 provenance |
| 三、7 沙箱不解决注入 | 见第 31 章；本例的拦截发生在能力层，与沙箱正交 |

## 已知限制

- DLP 规则只覆盖已知形态（AKIA、sk-、ghp_、私钥块、手机号、身份证），**不是完备的**，真实系统需要按自己的数据分级补充；
- 注入检测用的正则只是教学示例，对抗强度很低，正文 2.3 与三、6 已说明它不能当主防线；
- 审批器在本例里是本地函数，真实系统要接独立通道（另一个客户端、另一个进程、另一个服务）；
- 本例未实现工具输出的沙箱执行、速率限制、跨会话记忆的过期与清理；
- `--live` 只替换"候选动作的来源"，策略引擎、审批通道、审计完全不变——这正是本例想强调的分层。
