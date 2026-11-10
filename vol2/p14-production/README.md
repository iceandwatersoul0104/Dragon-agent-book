# 第 14 章 · 上线：容器化、成本控制与值班手册

本卷第十四章把"跑起来"变成"上得住"：**容器化清单、配置与密钥的边界、限流与背压、成本护栏，以及一份能照着做的值班手册。**

- 正文：[`production.md`](production.md)
- 配套工程产物：`dragon_agent/cli.py`、`dragon-agent/Dockerfile`、`dragon-agent/deploy/`、`dragon-agent/examples/production_demo.py`

## 本章给了什么

1. **`dragon_agent/cli.py`**——命令行入口 + 本章的全部运行期策略：子命令、成本阈值、值班动作条目、模型侧限流、工具侧限流、队列背压、自动熔断、降级模型。纯标准库。
2. **`dragon-agent/Dockerfile` + `dragon-agent/deploy/`**——镜像分层、只读根、非 root、最小出网的完整清单（Dockerfile、compose、K8s NetworkPolicy），以及配置与密钥的轮换审计说明。
3. **`dragon-agent/examples/production_demo.py`**——上线演练：直接读清单文件做自检，再把限流、背压、成本、熔断逐个亮出来。
4. **`dragon-agent/examples/eval_cases.jsonl`**——评测集，JSONL 格式，判定器不含 LLM 调用。
5. **`dragon-agent/tests/test_cli.py`**——53 条契约断言。

**没有改动 `dragon_agent/` 下的其他模块**——第 12 章的 tracer、第 13 章的评测器都是只读引用。

## 核心约定

> 退出码有语义：0 正常、2 用法错、3 配置错、4 任务失败、5 熔断或预算耗尽、6 评测回归。脚本与 CI 靠退出码判断，不解析输出。
> 缺 API Key 且带 `--live` 时，以退出码 3 明确提示后退出——**不静默失败**。
> mock 模式（默认）不联网、不需要 key，所有子命令都能跑通并以 0 退出。
> 阈值优先级：**命令行 > 环境变量 > 内置默认值**。
> 限流与熔断只返回"拿不到"，**不抛异常**；预算告警每档只报一次。

## 值班手册

> 本节是本章的**可执行版本**。正文讲的是这套东西怎么设计、什么时候不该用；这里给的是"看到什么信号、做什么动作、什么时候找谁"。

### 一、告警阈值

| 指标 | 来源 | 告警阈值 | 严重级别 | 首个动作 |
| --- | --- | --- | --- | --- |
| 任务成功率 | 第 12 章 SLI | 5 分钟内 < 95% | P1 | 冻结发布，回滚最近的模型/提示词变更 |
| 单工具错误率 | 按工具名分桶 | 10 分钟内 > 10% | P1 | 查该工具下游依赖与最近变更 |
| 模型侧 429 比例 | 模型调用错误分类 | 5 分钟内 > 5% | P2 | 确认降级模型已接管，核对上游配额 |
| 模型侧 5xx 比例 | 模型调用错误分类 | 5 分钟内 > 2% | P1 | 熔断已打开则保持，等上游恢复 |
| 无进展任务占比 | 第 12 章 SLI | 1 小时内 > 5% | P2 | 看 trace 里连续重复的动作 |
| 单任务成本 | 成本账 | > 预算的 80% | P2 | 收紧 `--budget` / `--cost`，考虑降级模型 |
| 单任务 token | 成本账 | > 预算的 80% | P3 | 检查上下文装配是否超预算 |
| 上下文超限 | 错误分类 `context_length_exceeded` | 出现即告警 | P2 | 查检索层是否塞进超长片段 |
| 容器重启次数 | 编排层事件 | 5 分钟内 > 3 次 | P1 | 看 OOM 事件，回滚镜像 tag |
| 评测回归 | `dragon-agent eval` | 有用例挂掉 | P2 | 跑回放定位差异，必要时回滚 |

阈值不是拍出来的：**取自 SLI 的历史分布**，并随分布漂移调整。定一个"理论上正确"的阈值然后天天误报，比不定阈值更糟——值班的人会静音它。

### 二、常见故障与处置动作

| 现象 | 先看什么 | 处置动作 | 禁忌 |
| --- | --- | --- | --- |
| 全部任务变慢 | SLI 的步数分布 p50/p90 | 看上下文 token 数是否变高；必要时收紧 `--max-steps` | 别直接加机器——先确认不是自己绕圈 |
| 大量 `stopped_no_progress` | trace 里连续重复的动作 | 收紧 `--max-steps`；检查工具返回值是否变空 | 别靠换模型解决——无进展是工具侧的事 |
| 大量 `error` 且错误是 `rate_limited` | 降级是否已生效 | 确认 `--fallback-model` 已配置；核对上游配额 | 别在熔断打开时硬重试 |
| 大量 `error` 且错误是 `timeout` | 单步耗时分布 | 调小 `--max-steps`；检查下游工具延迟 | 别只调大超时——那是在等更久 |
| 工具错误率集中在某个工具 | 该工具的 `tool_failure` 计数 | 查下游；必要时在护栏里禁用该工具 | 别在 agent.py 里加特判绕过护栏 |
| 成本突然上涨 | 成本账按模型/任务拆分 | 对比模型是否被换过；确认缓存是否命中 | 别直接停服——先降级模型 |
| 容器反复重启 | 编排层事件 + OOM 记录 | 回滚上一个镜像 tag；调高内存 limit | 别在重启循环里改镜像——回滚优先 |
| 写入 trace 失败 | 挂载点是否可写、uid 是否一致 | 对齐两侧 uid；给只读根补 `/tmp` tmpfs | 别把根文件系统改成可写来"解决" |
| 缺 key（退出码 3） | 环境变量 | 设 `LLM_API_KEY` 或 `OPENAI_API_KEY` | 别改成静默 fallback——那是在烧钱 |

### 三、升级路径

| 级别 | 多久没人响应就升级 | 升级给谁 | 交接什么 |
| --- | --- | --- | --- |
| P1 | 10 分钟 | 工具 owner / 平台值班 | 影响面、起始时间、已经做过什么、trace 链接 |
| P2 | 30 分钟 | 工程负责人 | 指标趋势、怀疑的范围、下一步打算 |
| P3 | 当日 | 值班交接 | 记录进当班日志，不打断任何人 |

升级不是甩锅，是**把上下文交接出去**：值班的人不该只丢一句"挂了"过去。交接内容固定四项——影响面、起始时间、已做过的动作、证据（trace 与日志的链接）。

### 四、值班前检查

- [ ] 当前生效的阈值是多少？（`dragon-agent runbook`）与上周一致吗？
- [ ] 降级模型是否可用、备用配额够不够？
- [ ] 上一个镜像 tag 还在吗？回滚演练过没有？
- [ ] 密钥有没有在轮换窗口内？轮换流程走通了没有？
- [ ] 评测基线是最新的吗？

## 怎么跑

```powershell
cd dragon-agent

# 1) 上线演练：清单自检 + 护栏演示，退出码 0
python examples/production_demo.py

# 2) 契约测试：53 条断言
python -m unittest tests.test_cli

# 3) CLI 各子命令（mock 模式，不需要 key）
python -m dragon_agent.cli run "查一下项目预算，并告诉我总额"
python -m dragon_agent.cli tools
python -m dragon_agent.cli demo
python -m dragon_agent.cli eval
python -m dragon_agent.cli runbook

# 4) 真跑（缺 key 会以退出码 3 明确提示，不会静默失败）
$env:LLM_API_KEY = "sk-..."
python -m dragon_agent.cli run "查一下项目预算" --budget 20000 --cost 0.05 --fallback-model gpt-4o-mini
```

不需要 API Key，不联网，不装任何第三方包。

### 实跑结果

| 场景 | 结果 | 退出码 |
| --- | --- | --- |
| `python examples/production_demo.py` | 清单自检与护栏演示 **34 / 34 项通过** | 0 |
| `python -m unittest tests.test_cli` | `Ran 53 tests` / `OK` | 0 |
| `python -m dragon_agent.cli eval` | 用例 **4** 条，通过 **4** 条 | 0 |
| `python -m dragon_agent.cli run "查一下项目预算，并告诉我总额"` | 2 步、512 token、状态 `completed` | 0 |
| `python -m dragon_agent.cli demo` | 七段护栏演示完成 | 0 |
| `python -m dragon_agent.cli runbook` | 打印 8 条值班条目 | 0 |
| `python -m dragon_agent.cli run "hi" --live`（无 key） | 打印配置提示 | **3** |

演练里的几个关键数字：

- 令牌桶：速率 5/秒、突发 10，连发 14 次 → 放行 **10** 次、拒绝 **4** 次；
- 队列背压：容量 3，塞 6 个 → 入队 **3** 个、丢弃 **3** 个（丢弃数被观测，不是凭空消失）；
- 一次任务（演示单价：输入 3、输出 15 美元/百万 token）：**380 token / 0.001860 美元**；
- 预算告警：把预算压到一半后触发 `critical` 档，**同一档位不重复告警**；
- 熔断：连续失败 3 次 → 状态 `open`；冷却后进入 `half_open` 放一个探测请求；
- 降级：主模型返回 429 → 切到备用模型并**粘住**；主模型返回 400（不可重试）→ **不降级**。

## 部署清单

四份文件，分工在 `dragon-agent/deploy/README.md` 里有完整说明：`Dockerfile`（镜像怎么建）、`.dockerignore`（什么不进构建上下文）、`compose.yaml`（单机编排）、`k8s.yaml`（集群编排与出网白名单）。

一件必须记住的事：**`deploy/k8s.yaml` 里的 `<模型服务网段>` 与 `<内部服务网段>` 是占位符，不换掉等于没写网络策略。** 上线演练会把"占位符还在吗"当成一条待办列出来。

## 局限（这一章不等于可以上线）

- **限流与背压只在演示里跑，没接进真实任务。** `TokenBucket` / `ToolRateLimiter` / `BoundedQueue` 只在 `cmd_demo`（`cli.py:1460`、`:1468`、`:1484`）和 `examples/production_demo.py` 里被直接实例化并手工调用；`cmd_run`、`cmd_chat` 与 `build_stack`（`:818-841`）都没有把它们接进 Agent——真实任务既不取令牌也不进队列。
- **成本护栏在本章这一层是事后回看，不是事前停手。** `BudgetWatcher.observe()` 在 `agent.run()` 返回之后才被调用（`cli.py:956`），告警是任务跑完的回执；真正能在花钱途中停手的是第 6 章的 `BudgetGuard`，它得靠 `--price-in` / `--price-out` 才有准数。而 `degrade_ratio` 只用来给告警定档位（`:386-389`），降级只由可重试故障触发（`:562-582`）——"按成本比例换便宜模型"并没有实现。
- **阈值是启动时定死的静态数字，值班手册也没有接告警系统。** `CostThresholds` 是 frozen dataclass，`build_thresholds()` 每次启动时拼一次（`cli.py:735-758`），运行中没有调整通道，改阈值要重启进程；`cmd_runbook` 只是把 `ONCALL_ACTIONS` 打印出来（`:1402`、`:171-220`），手册里那些"5 分钟内成功率 < 95%"的口径没有任何代码在算。
- **限流、队列、熔断的状态都在单进程内存里，多副本各限各的。** `TokenBucket.tokens`、`BoundedQueue._items`、`CircuitBreaker` 都是进程内对象（`cli.py:247`、`:499`、`:299-330`），副本之间不共享——而 `deploy/k8s.yaml` 写的是 `replicas: 2`（`:76`），实际并发上限是配置值的两倍，熔断也是各断各的。
- **配置中心与镜像里声明的变量，有一部分根本没人读。** `DRAGON_WORKSPACE` 与 `DRAGON_TRACE_DIR` 在 Dockerfile、`compose.yaml`、`k8s.yaml` 的 ConfigMap 里都声明了，却没有任何 Python 代码消费它们——trace 目录实际只认 `--trace`，否则落到 `cwd/traces`（`cli.py:852-858`）；ConfigMap 也只下发了 6 个阈值里的 2 个（`k8s.yaml:51-52` 对 `cli.py:714-721`）。
- **部署清单自己还带着几个必须先改的坑。** `k8s.yaml` 里实际有四类占位符——两处网段（`:196`、`:203`）、两处镜像地址（`:103`、`:248`）与 Secret 的 key（`:66`），上面的部署清单只点了网段那两处；traces 挂的是 `emptyDir`（`:160-162`），Pod 重建即丢；Deployment 跑的是交互式 `chat`（`:105`），容器里没有 stdin 会立刻 EOF 退出（`cli.py:1010`、`:1049`），随后被反复重启。

## 与本书其它章节的关系

- 第二卷第 3 章 `p03-llm-client`——成本账的单价算法来自那里。
- 第二卷第 9 章 `p09-guardrails`——护栏只能收紧不能放宽；降级与熔断是同一条纪律的延伸。
- 第二卷第 11 章 `p11-protocols`——跨进程之后调用链更难复盘，值班排查更依赖 trace。
- 第二卷第 12 章 `p12-observability`——本章所有阈值都来自那里的 SLI。
- 第二卷第 13 章 `p13-evaluation`——本章的 `eval` 子命令优先调用那里的 `Evaluator`。
- 第一卷第 36 章《生产化检查清单与真实故障模式》——更完整的上线前检查项。
