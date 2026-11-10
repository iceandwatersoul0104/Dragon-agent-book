# ch34 评测体系 —— 配套示例

《开源 Agent 架构技术全书》第 34 章的参考实现。一个能真正演示"评测怎么用"的最小闭环：

1. **任务集**：6 条任务，覆盖 easy / medium / hard 三层，黄金答案写成「关键事实清单 + 禁止项」，不写标准答案句子；
2. **被测系统**：mock Agent 按任务预设缺陷作答；`--live` 用真实 API 作答；
3. **判定器**：关键事实覆盖（程序化断言）+ 一个**可控偏差的 mock Judge**（位置偏差 / 长度偏好 / 自偏好可单独开关）；
4. **偏差演示**：同一答案交换顺序判两次，统计 judge 不稳定率，再演示"位置交换取严"如何压住结论翻转；
5. **CI 门禁**：与 `baseline.json` 对比，通过率跌幅超阈值则退出码非 0，并列出具体回归的任务。

## 依赖

- Python **3.8+**，**只用标准库**，无需 `pip install`；
- 默认 mock 模式**不需要任何 API Key**；
- `--live` 才需要 `OPENAI_API_KEY`。

## 安装

```bash
cd code/ch34-evaluation
python -c "import sys; print(sys.version)"
```

Windows 下用 `py -3` 代替 `python`。

## 运行

```bash
# 1) 首次运行：建立基线，打印评测报告
python main.py

# 2) 第二次运行：与基线对比，通过率跌幅超阈值则退出码 1（CI 门禁）
python main.py --baseline baseline.json

# 3) 演示位置偏差：关掉它，不稳定率从 16.7% 降到 0
python main.py --baseline baseline.json --no-position-bias

# 4) 演示长度偏好：同样的事实，只因回答更啰嗦，multi-001 从失败变通过
python main.py --baseline baseline.json --agent-style verbose

# 5) 演示自偏好：judge 与被测同源时给加分
python main.py --baseline baseline.json --self-preference

# 6) 接受当前结果作为新基线
python main.py --baseline baseline.json --update-baseline

# 7) 真实 API 作答（判定器不变）
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.openai.com/v1     # 可选，任意 OpenAI 兼容端点
export OPENAI_MODEL=gpt-4o-mini                       # 可选
python main.py --live
```

主要参数：`--baseline`、`--threshold`、`--update-baseline`、`--agent-style {plain,verbose}`、`--position-bias/--no-position-bias`、`--length-bias/--no-length-bias`、`--self-preference`、`--live`、`--model`、`--base-url`。

## `--live` 所需环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `OPENAI_API_KEY` | 是 | — | OpenAI 兼容接口的密钥 |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | 接口根地址，可指向任意兼容端点或本地网关 |
| `OPENAI_MODEL` | 否 | `gpt-4o-mini` | 模型名 |

## 三组演示

| 演示 | 命令 | 现象 | 结论 |
|---|---|---|---|
| 位置偏差 | `--no-position-bias` | policy-001 的判定在两种顺序下翻转，不稳定率 16.7% → 0 | 位置偏差能直接翻转结论，必须校准 |
| 长度偏好 | `--agent-style verbose` | multi-001 缺 2 个关键事实，分数却从 6.6 涨到 7.4，从失败变通过 | 长度加分会让信息不全的答案蒙混过关 |
| CI 门禁 | 连跑两次基线对比 | 列出"基线通过→本次失败"的任务清单，退出码 1 | 门禁要指出具体任务，不只是平均分 |

## 输出说明

逐任务打印：得分、判定（通过/失败）、判定是否稳定（交换顺序后结论是否翻转）、缺失的关键事实、违反的禁止项。汇总打印：单次判定通过率、位置交换取严后的通过率、judge 不稳定率、按难度分层的通过率、总成本、每次成功成本。

`baseline.json` 保存判定结果与配置元信息（judge 参数、agent 风格、运行模式），不含答案全文。

## 与正文的对应关系

| 正文位置 | 代码位置 |
|---|---|
| 2.1 三段式结构 | `build_tasks()` / `MockAgent` / `MockJudge` 三者分离 |
| 2.4 黄金答案写成验收条件 | `Task.key_facts` + `Task.must_not` + `fact_coverage()` |
| 2.4 难度分层与通过率 | `Task.difficulty` + `Metrics.by_difficulty` |
| 2.5 位置偏差与缓解 | `judge_with_swap()`、`strict_pass`（两次都通过才算通过） |
| 2.5 长度偏好与自偏好 | `JudgeConfig.length_bias` / `self_preference` |
| 2.6 基线对比与门禁 | `compare_baseline()` + `--threshold` + 回归任务清单 |

## 已知限制

- 6 条任务只是最小样本，统计意义有限；真实任务集要 100~300 条起步，并按覆盖矩阵铺开；
- mock Judge 的偏差是人为设定的固定幅度，不是对真实模型的测量结果——真实 judge 的偏差需要用人工标注样本测出来；
- 成本是按 token 数与示意价目表估算的，没算重试、缓存命中与并行；
- 人工校准流程（抽 100~200 条人工标注、算一致率）本例没有实现；
- 隐藏集、多次采样（pass^k）、任务集版本 diff 都未实现；
- 本例只有确定性任务，没有随机性来源，因此没有演示"同一系统跑两遍结果不同"这一可靠性问题。
