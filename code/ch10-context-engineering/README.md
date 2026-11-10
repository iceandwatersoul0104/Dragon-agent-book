# ch10 上下文工程最小可实现

对应《开源 Agent 架构技术全书》第 10 章《上下文工程：把上下文当作稀缺资源预算》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**：一个可复用的**上下文组装器**，一次跑清四件事：

- **分层**：系统提示 / 工具定义 / 任务 / 检索结果 / 历史 / 复述，各自一个优先级与预算；
- **四种手段**：按需加载（工具）、排序截断（检索）、压缩+淘汰（历史）、保留（约束与提醒）；
- **缓存友好排布**：稳定块在前构成可缓存前缀，变动块在后；
- **预算报告**：每层原 token、保留 token、采取的动作、关键事实落在哪个位置。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch10-context-engineering
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

### mock 模式（默认，无需 API Key）

```bash
python main.py                 # 默认预算 6000，对照朴素与组装两种排布
python main.py --budget 4000   # 收紧预算，看降级动作
python main.py --budget 100000 # 放宽预算，看检索排序的影响
python main.py --layout naive  # 只跑朴素拼装
python main.py --trace         # 打印组装后的完整上下文
```

典型输出：

```
朴素拼装（不做任何管理）
context tokens : 6180
是否超预算     : 是（超出 180 tokens）
关键约束位置   : 中段（20%～80%）
模型回答       : 否……（没有找到关于生产库结构的硬约束，无法确认依据。）

上下文组装器
layer          before    after  action
system             52       52  keep
tools             425      110  按需加载：保留 2 / 12 个工具
task              147      147  keep
retrieval        2507     1933  按相关度排序后截断：保留 23 / 30 条
history          3072      428  压缩 18 轮为结构化摘要 + 淘汰中段，保留最近 2 轮
reminder           28       28  keep
total          6231 -> 2698  (budget=6000)
cache_prefix   319 tokens（稳定前缀，可缓存）
关键约束位置   : 头部（前 20%）
模型回答       : 否。依据：上下文写明本任务期间不要修改数据库 schema，未获批准前一律只读。
```

要点：**关键约束（"不要修改数据库 schema"）只出现在历史第 8 轮**。
朴素拼装时它落在中段，模型读不到；组装器把它结构化提取出来、前置到任务层，
再在结尾复述一次，位置从中段变成头部，模型就能给出有依据的回答。

### mock 说明（重要）

内置"模型"是一个**窗口模型**：它只可靠地读取上下文**开头 20% 与结尾 20%** 的内容。

这是对"长上下文里中间信息被忽略"（上下文腐化）现象的**近似模拟**，不代表任何真实模型的表现。
真实行为必须用 `--live` 测——不同模型、不同任务对长上下文的敏感度差别很大。

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
`choices[0].message.content`。换供应商时只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

live 模式会把组装好的上下文原样发给模型，并问："本次是否可以直接变更生产库结构？只回答：是/否，并说明依据。"

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--budget` | `6000` | 总 token 预算 |
| `--layout` | `both` | `naive` / `assembled` / `both` |
| `--live` | 关 | 切真实模型 API |
| `--temperature` | `0.0` | live 模式采样温度 |
| `--model` | 空 | 覆盖 `LLM_MODEL` |
| `--base-url` | 空 | 覆盖 `LLM_BASE_URL` |
| `--trace` | 关 | 打印组装后的完整上下文 |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `est_tokens()` | token 估算（CJK 按 1、ASCII 按 4 字符）——足够做"哪块大"的相对比较 |
| `ContextAssembler.assemble()` | 分层 → 按优先级收敛 → 缓存友好拼接 → 报告 |
| `_apply_tools()` | 按需加载：只留这一步用得上的工具定义 |
| `_apply_retrieval()` | 检索：先按相关度排序，再截断 |
| `_apply_history()` | 压缩（结构化提取）+ 淘汰，永远保留最近两轮原文 |
| `compress_history()` | 结构化提取：只留约束与动作，丢掉过程叙述 |
| `extract_constraints()` | 把硬约束从历史里拎出来，不许随历史一起被淘汰 |
| `constraint_position()` | 关键事实落在头部/中段/尾部 |
| `naive_assemble()` | 对照组：不做任何管理 |

## 换成自己的场景时要改什么

1. 改 `LAYERS`（在 `ContextAssembler.__init__` 里）：每层的**优先级、预算、策略**；
2. 策略目前实现了四种：`keep` / `load_on_demand` / `rank_then_truncate` / `compress_then_evict`，
   新增策略就加一个 `_apply_xxx` 分支；
3. 改 `compress_history()` 的抽取规则：现在是按关键词正则抓约束与 `Action:` 记录，
   换成你自己的状态字段（工具调用、参数、结论、待办）；
4. 改 `task_terms()` 的检索关键词生成：现在是英文单词 + 中文 2-gram；
5. **`est_tokens()` 只是估算**。要精确就用真实 tokenizer——组装器只关心相对大小，
   估算够用；但如果你要把预算卡死在某个数上，就得换真的。

## 相关章节

- 第 5 章：提示增强技术（few-shot 示例也吃上下文预算）
- 第 7 章：规划范式（计划与执行历史占多少上下文）
- 第 8 章：工具调用契约（工具定义是上下文里的大块常驻内容）
- 第 11 章：停止准则、步数上限与成本守卫
