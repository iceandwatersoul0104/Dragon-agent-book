# ch25 多 Agent 失效模式与成本治理 最小可实现

对应《开源 Agent 架构技术全书》第 25 章《多 Agent 失效模式与成本治理》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**。一次跑清四件事：

- **失效清单**：无限循环、角色塌缩、共识幻觉、错误放大、委派死锁、上下文污染，
  七个场景逐个跑，看各自触发什么信号、走哪条降级路径；
- **检测信号**：重复率、消息长度趋势、角色熵、token 斜率，以及无进展、
  断言放大、注入回声三个语义信号；
- **熔断与降级**：步数/时长/token 预算/重复率熔断，降级阶梯三级；
- **成本可视化与评测**：一次任务的总账与按角色归因；两套任务集上单 Agent
  与多 Agent 的正确率与成本对比。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch25-multi-agent-failures
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

### mock 模式（默认，无需 API Key）

```bash
python main.py                       # A~D 四段演示
python main.py --scenario collapse   # 只跑一个失效场景
python main.py --max-steps 5         # 收紧步数上限看熔断
```

### 典型输出

**A. 失效清单**

```
[amplify] 期望现象：上游的数字被下游用更强措辞复述，错误被放大
    状态：中止（部分结果）
    检测信号：断言放大（第 2 步把上游的数字 3/38/6.7 换成了强化表述）
    熔断：检测到断言放大
    降级：上游的 38% 与 6.7% 未被交叉验证，下游的强化表述不采信，只保留原始数字。
    调用 3 次｜输入 138｜输出 82｜合计 220 token｜估算 $0.000302
    按角色归因：审核员 114 token（52%）；分析员 68 token（31%）；检索员 38 token（17%）

[contamination] 期望现象：工具返回里的注入指令被当成任务执行
    状态：中止（部分结果）
    检测信号：注入回声（第 1 步照着工具返回里的指令输出了「检索完成」）
    熔断：检测到注入回声
    降级：工具返回中包含指令注入，检索结论不可采信，需要换可信数据源重跑。
```

**B. 指标对照**

```
场景           重复率     长度趋势    角色熵     token斜率    步数    状态
normal         0.03       -4.8        1.00       +43          4       成功
collapse       1.00       0.0         1.00       +20          4       中止（部分结果）
loop           0.75       0.0         0.97       +28          6       中止（部分结果）
deadlock       0.59       0.0         0.92       +27          4       中止（部分结果）
amplify        0.23       4.0         1.00       +38          3       中止（部分结果）
contamination  0.00       0.0         0.00       -9           2       中止（部分结果）
```

注意塌缩那一行的**角色熵是 1.00**（满的）——因为四个角色各说一遍同样的话，分布依然均匀。
角色熵抓的是分布塌缩，抓不到内容塌缩；抓住塌缩的是重复率。指标必须一起看。

**C. 成本总账与归因**

```
场景           调用    总token    估算$        状态
normal         4       457        0.000610     成功
loop           6       605        0.000749     中止（部分结果）
collapse       4       184        0.000235     中止（部分结果）
consensus      4       283        0.000366     中止（部分结果）
amplify        3       220        0.000302     中止（部分结果）
deadlock       4       298        0.000382     中止（部分结果）
contamination  2       137        0.000172     中止（部分结果）

单价为示例值（$0.001/$0.002 每千 token），不代表任何厂商真实价格。
归因的关键：把 token 按角色摊开，就能看出是调度在烧钱，还是执行在烧钱。
```

**D. 用评测而不是直觉**

```
判据：多 Agent 值得，当且仅当正确率提升 ≥ 0.2 且成本倍数 ≤ 3。

任务集               单Agent正确率   多Agent正确率   提升      成本倍数    结论
需要检索的任务       75%             100%            +25%      2.6×        值得上多 Agent
格式固定的任务       100%            100%            +0%       3.1×        不值得，别上

同一个问题，两套任务集给出相反结论——所以「值不值得」不是能拍脑袋的，
必须在自己的任务集上测。
```

### mock 说明

内置模型按场景脚本返回确定性发言，不做真实推理。**机制全部真实**：七个失效场景、
七类检测信号、熔断条件、降级阶梯的三级尝试、成本计量与按角色归因、评测对比。

token 数是粗估值（中文按字、其余按 3.5 字符/token）；美元金额用的是**示例单价**
（$0.001/$0.002 每千 token），只为演示计算方式，**不代表任何厂商的真实价格**，
实盘要把价格表做成配置。

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

live 模式下场景脚本不再生效，由真实模型自己发言——这时能真正看到塌缩、复读、
论证放大这些现象。检测信号与熔断逻辑不变。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--scenario` | 全部 | `normal` / `loop` / `collapse` / `consensus` / `amplify` / `deadlock` / `contamination` |
| `--max-steps` | `8` | 步数上限 |
| `--max-seconds` | `30` | 总时长上限（秒） |
| `--max-tokens` | `60000` | token 预算上限 |
| `--live` | 关 | 切真实模型 API |
| `--model` / `--base-url` | 环境变量 | 覆盖 `LLM_MODEL` / `LLM_BASE_URL` |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `Metrics.duplicate_ratio()` | 重复率：字符 3-gram Jaccard，不需要模型 |
| `Metrics.length_trend()` / `token_slope()` | 长度趋势与 token 斜率（最小二乘斜率） |
| `Metrics.role_entropy()` | 角色熵：发言者分布的归一化熵 |
| `Metrics.no_progress()` | 无进展：连续几条都在等待/依赖 |
| `Metrics.amplification()` | 断言放大：上游数字 + 下游强化措辞 |
| `Metrics.injection_echo()` | 注入回声：输出复述了工具返回里的指令 |
| `Metrics.consensus_hallucination()` | 共识幻觉：宣称共识但内容为空 |
| `Breakers` | 熔断阈值：步数、时长、token、重复率 |
| `Runtime._detect()` / `_trip()` | 信号采集与熔断判定 |
| `Runtime._converge()` | 降级阶梯：强制收敛 → 单 Agent 回退 → 中止 |
| `cost_report()` | 成本总账与按角色归因 |
| `eval_suites()` | 两套任务集上单/多 Agent 的正确率与成本对比 |

## 换成自己的场景时要改什么

1. 改 `SCENARIOS`：把你线上真实踩过的失效写成脚本——清单永远只能覆盖见过的坑；
2. **调阈值要靠标注**：现在重复率 0.75、无进展窗口 3 都是示例值。
   拿一批真实对话标出来（正常/异常各几十条），扫一遍阈值，
   看误杀率与漏杀率，再定。拍出来的阈值第一天就会误杀；
3. 改 `_detect()`：加自己的信号。新失效模式靠线上 trace 发现，不靠清单；
4. **信号要分级**：脚本里是"命中即熔断"，生产上建议先只告警、积累标注数据再收紧；
5. 改 `_converge()`：另外造一批"强制收敛也拿不到结论"的 case，
   验证第二级（单 Agent 回退）和第三级（中止返回部分结果）接得住；
6. 改 `EVAL_SUITES`：换成你自己的任务集，用你自己的单 Agent 基线。
   评测集不来自你的业务，结论就没有意义。

## 相关章节

- 第 11 章：停止准则、步数上限与成本守卫（无进展检测与预算守卫）
- 第 20 章：多 Agent 的动机与何时不该上多 Agent（先判断该不该多 Agent）
- 第 21 章：对话式协作（对话式协作是塌缩的高发区）
- 第 23 章：协作拓扑与调度（拓扑决定了失败传播路径）
- 第 29 章：检查点、回放与多 Agent 共享状态（熔断之外，别把已做的工作丢掉）
- 第 32 章：提示注入与越权防护（上下文污染的防御）
