# ch05 提示增强技术评测器（最小可实现）

对应《开源 Agent 架构技术全书》第 5 章《提示增强技术：CoT、Few-shot、Self-ask 与 Least-to-Most》。

纯标准库实现，**不需要 API Key 就能跑通完整评测流水线**：

```
策略矩阵 -> 提示装配（纯函数） -> LLM 调用 -> 判分 -> 指标表
```

六种策略：`direct` / `cot` / `fewshot` / `cot_fewshot` / `selfask` / `ltm`。
两个任务：一个是需要中间计算的几何题，一个是纯事实题——用来观察 CoT 类技巧在两类任务上的差别。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch05-prompting
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

### mock 模式（默认，无需 API Key）

```bash
python main.py
```

输出示例：

```
strategy      acc         fmt_rate    avg_chars   avg_ms      geometry    fact
direct        0.50        0.00        12.5        0.0         MISS        OK
cot           1.00        1.00        47.0        0.0         OK          OK
fewshot       0.50        1.00        6.0         0.0         MISS        OK
cot_fewshot   1.00        1.00        47.0        0.0         OK          OK
selfask       1.00        1.00        71.5        0.0         OK          OK
ltm           1.00        1.00        64.0        0.0         OK          OK
```

几个常用组合：

```bash
python main.py --strategies cot,ltm,direct   # 只跑部分策略
python main.py --repeat 3                     # 每个任务重复 3 次，看均值与方差
python main.py --trace                        # 打印每种策略装配出的完整 prompt
python main.py --tasks geometry               # 只跑某一个任务
```

### mock 到底做了什么（重要，别误读）

内置的"模型"**不是真的在推理**，它是一个**行为模拟器**：只看提示里有没有
`step by step` / `Self-ask` / `Least-to-Most` / 示例块，然后返回该场景下的典型形态。

它模拟了三条常见现象，用来演示评测流水线本身：

- 零样本直答容易不守输出格式（`fmt_rate` 低）；
- 给示例后格式立刻合规，但算术仍然错（示例教的是格式，不是推理）；
- CoT 类技巧在计算题上正确率提升、在事实题上只增加输出长度。

**它不代表任何真实模型的表现。** 真实结论必须用 `--live` 跑。

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
python main.py --live --repeat 3
```

Windows PowerShell：

```powershell
$env:LLM_API_KEY="sk-xxxxxxxx"
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="gpt-4o-mini"
python main.py --live --repeat 3
```

端点需兼容 `POST {base}/chat/completions`：请求体含 `model` 与 `messages`，返回体取
`choices[0].message.content`。换供应商时只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--strategies` | 六种全部 | 逗号分隔的策略名 |
| `--tasks` | `geometry,fact` | 逗号分隔的任务 id |
| `--repeat` | `1` | 每个任务重复次数（采样次数） |
| `--live` | 关 | 切真实模型 API |
| `--temperature` | `0.0` | live 模式采样温度 |
| `--model` | 空 | 覆盖 `LLM_MODEL` |
| `--base-url` | 空 | 覆盖 `LLM_BASE_URL` |
| `--trace` | 关 | 打印每种策略装配出的 prompt |

退出码：`0` 正常结束，`1` 参数错误，`2` 缺 `LLM_API_KEY`。

## 指标说明

| 指标 | 含义 |
| --- | --- |
| `acc` | 正确率：答案是否命中期望值（按完整数字段匹配，避免 21 命中 210） |
| `fmt_rate` | 格式合规率：输出里是否出现 `答案：<x>` |
| `avg_chars` | 平均输出字符数，作为 token 成本的代理 |
| `avg_ms` | 平均调用延迟毫秒 |
| `geometry` / `fact` | 逐题结果：`OK` 命中，`MISS` 未命中 |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `build_prompt()` | 提示装配是纯函数：任务 + 示例 + 策略 → 一条字符串，可单测可快照 |
| `format_examples()` | few-shot 的格式一致性硬要求 |
| `MockLLM` | 行为模拟器，用于离线回归 |
| `LiveLLM` | OpenAI 兼容端点的真实调用（`urllib`，无第三方依赖） |
| `evaluate()` / `print_table()` | 策略矩阵与指标统计 |
| `is_correct()` / `format_ok()` | 判分逻辑，与模型无关的确定性代码 |

## 把这套东西用成真正的对照实验

对应正文 4.1 节。脚本只提供了骨架，真实实验还要补三件事：

1. **换真实任务集**：把 `TASKS` 换成 20～30 条有标准答案的线上样本，示例集与评测集分开；
2. **调温度并多次采样**：`--temperature 0.7 --repeat 5`，看均值与方差，别用单次结果下结论；
3. **配对比较**：同一条任务跑 A、B 两种策略，比较配对差值，而不是两组独立均值。

## 相关章节

- 第 4 章：ReAct（提示里的"分步思考"如何变成真正的交错循环）
- 第 6 章：反思与自纠错（提示解决不了"发现自己答错了"）
- 第 7 章：规划范式（提示里的分解 vs 架构里的规划）
- 第 10 章：上下文工程（few-shot 示例吃掉的上下文预算）
- 第 11 章：停止准则、步数上限与成本守卫
