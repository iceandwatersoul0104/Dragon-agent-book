# ch11 停止准则、步数上限与成本守卫 最小可实现

对应《开源 Agent 架构技术全书》第 11 章《停止准则、步数上限与成本守卫》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**。一次跑清五件事：

- **四类停止条件**：显式完成 / 预算耗尽 / 无进展 / 异常终止，判定全部由确定性代码做；
- **无进展三信号**：动作重复、观察不变、状态无变化，分别检测、互不误判；
- **预算守卫**：token 预算、时间预算、美元成本上限的统一抽象；
- **超时降级**：停止时返回部分结果，而不是抛异常；
- **模型说"完成"不算数**：还要过完成条件校验（产物真的存在）。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch11-stopping
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

### mock 模式（默认，无需 API Key）

```bash
python main.py                              # 跑全部六个场景
python main.py --scenario budget            # 只跑一个场景
python main.py --scenario budget --max-tokens 500
python main.py --max-steps 3                # 收紧步数上限
python main.py --max-usd 0.0005             # 用美元预算兜底
python main.py --repeat-limit 2             # 两次同样信号就判定无进展
python main.py --trace                      # 打印每轮动作、观察与预算余额
```

典型输出：

```
scenario   stop_reason      rounds  tokens        usd  status
normal     completed            5      196  0.000268  completed
repeat     no_progress          3       86  0.000119  partial
stale      no_progress          3       78  0.000112  partial
frozen     no_progress          3       89  0.000123  partial
budget     budget_exhausted     4      140  0.000195  partial
error      error                1       22  0.000035  failed
```

三个无进展场景分别命中三种信号：

| scenario | 现象 | 命中的信号 |
| --- | --- | --- |
| `repeat` | 同一个动作与参数反复调用 | 动作重复：search 出现 3 次 |
| `stale` | 动作在变，返回内容始终一样 | 观察不变：最近 3 次观察内容相同 |
| `frozen` | 动作不同、观察带时间戳在变，但世界状态没动 | 状态无变化：最近 3 次状态指纹相同 |

非 completed 的场景会打印**部分结果**：

```json
{
  "status": "partial",
  "stop_reason": "no_progress",
  "detail": "动作重复：search 出现 3 次",
  "completed_actions": ["search", "search", "search"],
  "facts_collected": ["命中 2 条相关结果：条款 A、条款 B"],
  "artifact": null,
  "next_step": "由上游或人工接手剩余步骤；已完成的产物可直接使用。"
}
```

### mock 说明

内置模型按场景脚本返回动作，环境是本地确定性模拟。**判定逻辑全部真实**：预算扣账、
无进展三信号、停止优先级、部分结果组装、完成条件校验。

美元金额由单价算出：`--price-in` / `--price-out` 是**示例单价**（默认 0.001 / 0.002 美元每千 token），
只为演示计算方式，**不代表任何厂商的真实价格**；实盘要把价格表做成配置。

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
python main.py --live --scenario normal --trace
```

Windows PowerShell：

```powershell
$env:LLM_API_KEY="sk-xxxxxxxx"
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="gpt-4o-mini"
python main.py --live --scenario normal
```

端点需兼容 `POST {base}/chat/completions`：请求体含 `model` 与 `messages`，返回体取
`choices[0].message.content`。模型每轮输出一个 JSON：`{"action": "...", "args": {...}}`。
换供应商时只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--scenario` | `all` | `normal` / `repeat` / `stale` / `frozen` / `budget` / `error` / `all` |
| `--max-steps` | `8` | 步数上限 |
| `--max-tokens` | `0` | token 预算，0 表示不限 |
| `--max-seconds` | `0` | 时间预算（秒），0 表示不限 |
| `--max-usd` | `0` | 美元预算，0 表示不限 |
| `--price-in` / `--price-out` | `0.001` / `0.002` | 示例单价（美元/千 token），实盘走配置 |
| `--repeat-limit` | `3` | 连续几次同样信号算作无进展 |
| `--live` | 关 | 切真实模型 API |
| `--temperature` | `0.0` | live 模式采样温度 |
| `--model` / `--base-url` | 空 | 覆盖对应环境变量 |
| `--trace` | 关 | 打印每轮动作、观察与预算余额 |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `Budget` | token / 时间 / 美元三种预算的统一抽象与扣账 |
| `ProgressDetector` | 无进展三信号：动作重复、观察不变、状态无变化 |
| `normalize_observation()` | 观察规范化后再比较（不抹数字，时间戳差异要保留） |
| `StopGuard.check()` | 四类停止条件与优先级：完成 → 异常 → 预算 → 无进展 → 步数 |
| `Agent._completion_check()` | 完成条件校验：产物必须真的存在，模型自述不算数 |
| `Agent._partial()` | 降级返回：做完多少 + 卡在哪 + 下一步建议 |
| `MockEnv` | 确定性环境，提供观察与状态指纹 |
| `MockLLM` / `LiveLLM` | 场景脚本与真实模型 |

## 换成自己的场景时要改什么

1. 改 `MockEnv.execute()`：换成你的真实环境，并给出一个**状态指纹**（state hash）——
   判断"有没有卡住"全靠它；
2. 改 `MockLLM` 或接上真实模型：动作名与参数格式对齐你的工具；
3. 改 `_completion_check()`：把"产物存在"换成你的完成条件（测试通过、字段齐全、对账平了）；
4. 改 `Budget` 的单价来源：从配置或价格表读，不要写死；
5. **调参要靠评测**：扫 `--max-steps` 与 `--repeat-limit` 的组合，同时看完成率、平均轮次
   和**过早停止率**（判定完成但其实没完成的比例）三个指标。

## 相关章节

- 第 4 章：ReAct（步数上限与循环检测）
- 第 6 章：反思与自纠错（反思循环的熔断策略）
- 第 7 章：规划范式（计划过长时的重规划）
- 第 10 章：上下文工程（步数越多，上下文越贵）
