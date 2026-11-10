# 第 14 章 上线：容器化、成本控制与值班手册

## 一、本章要解决的问题

上线前最后一次演练：单元测试全绿，评测集全过，容器起来了，命令行跑一遍顺得像 demo。第一次接真实流量，问题立刻来了——一个任务跑了 17 分钟、花掉 40 美元，返回一句话；容器因为写不进去 trace 目录重启两次；模型侧一限流，整个进程挂掉，值班的人只看到一行 `Connection reset`。

这四件事看着不相干，其实同源：**前 13 章把"能跑"做完了，但没回答"跑在哪儿、跑多快、花多少、坏了谁管"**。测试环境里这些问题的后果是"重跑一次"，生产环境里是"烧钱、宕机、无人认领"。

上线要补的是五块：

1. **容器化**：镜像分层、只读根、非 root、最小出网。目标不是"能打包"，是把攻击面和故障面收到最小。
2. **配置与密钥**：环境变量还是配置中心，哪些东西能改、哪些绝对不能进代码库，密钥怎么轮换、谁来盯。
3. **并发与限流**：模型侧限流（保护上游）、工具侧限流（保护自己的下游）、队列与背压（把压力顶回去）。
4. **成本护栏**：预算告警、降级模型、自动熔断——共同点是**在花钱之前先停手**。
5. **值班手册**：告警阈值、故障处置动作、升级路径。半夜三点做决策的人需要一份"看到这个信号就做这几步"的清单。

本章的工程落点是 `dragon_agent/cli.py`：它站在依赖图最上层，把上面五件事变成可执行的代码与可打印的清单。配套的 `Dockerfile` 与 `deploy/` 给出容器化方案，`dragon_agent.cli runbook` 打印值班手册的机器可读版本。

## 二、原理：机制拆解

### 2.1 容器化的四条约束，各自在挡什么

| 约束 | 清单里怎么写 | 挡的是什么 |
| --- | --- | --- |
| 镜像分层 | `AS builder` / `AS runtime` 两阶段 | 构建产物污染运行产物：运行镜像里只剩解释器和业务包，改一行代码不用重装依赖层，发版从"重跑构建"变成"推一个 tag" |
| 只读根 | `read_only: true` + `PYTHONDONTWRITEBYTECODE=1` + 显式挂载可写路径 | 进程写了不该写的地方：把一类运行期异常变成**启动即失败**——可写容器里这种错误可能几天都不暴露 |
| 非 root | uid 10001 + `cap_drop: ALL` + `allowPrivilegeEscalation: false` | 容器逃逸之后的横向移动：root 容器能做的事比 nobody 多一个数量级 |
| 最小出网 | 编排层的 NetworkPolicy（**不是** Dockerfile） | 容器被当成跳板：K8s 默认网络是放行的，一条策略就能把出网收成模型服务网段加内部服务网段 |

最后一条有个必须说清的事实：**Dockerfile 管不了出网，Compose 也管不了**。Compose 能保证内部服务之间不通外网，但应用容器要访问模型 API，它自己必然在能出网的网段上。所以本章清单里，NetworkPolicy 是"最小出网"唯一真正落地的地方。

### 2.2 配置与密钥：一条界线

非密配置（模型标识、预算档位、工作目录）和密钥（API Key）走**两条完全不同的路**，理由不是安全洁癖，而是**变更代价不同**：

- 非密配置从配置中心下发成环境变量。改它不该重新构建镜像——同一个镜像在测试和生产跑，差别只在环境变量，改动代价是一次滚动更新。
- 密钥只在运行时由密钥管理系统注入。它**绝不能**进镜像层：镜像层一旦进了密钥，`docker history` 能看见，`docker save` 出来的镜像里也还在，删掉 `Dockerfile` 里那一行并不能把它从历史层里抹掉。

`Config.to_dict()` 是配置落盘的唯一入口，它刻意不含 `api_key`——它会被写进 trace 和检查点，那两处都是长期留存的数据。

轮换与审计要提前定好。**轮换**的正确顺序是"先下发新 key、保留旧 key、确认新 key 有流量之后再撤销旧 key"，反过来做，轮换瞬间会全量 401。**审计**要能回答三个问题：谁在什么时候用了哪把 key、有没有 key 漏进镜像层或代码库、key 多久没换了。前两个靠密钥系统的访问日志加 CI 里的密钥扫描，第三个靠给 Secret 打上轮换周期注解，到点没换由巡检报出来。

### 2.3 限流的三层，各保护谁

**模型侧限流**保护上游供应商。一把令牌桶，匀速发牌、允许突发——Agent 的真实流量就是突发形的：模型可能突然要连调三次工具。`TokenBucket.acquire()` 超时拿不到令牌就返回 `False`，**不抛异常**：限流器只是把请求压慢，压慢失败不该让任务崩掉。

**工具侧限流**保护自己的下游。模型侧好好的、工具连不上，照样能把任务打穿——两类故障互不相干，护栏必须分开设。每个工具一把令牌桶，外加一把全局并发闸。

**队列与背压**解决"来的人比能处理的人多"。有界队列满了只有两条路：**等**（把压力顶回上游）或**丢**（保主链路）。两种都行，但丢弃数必须被观测到——被丢掉的请求如果没人知道，就等于凭空消失的任务。

### 2.4 成本护栏：三件事，同一个时机

**预算告警**在花掉预算的某个比例时喊一嗓子。关键细节：每档只报一次。告警每一步都刷屏等于没有告警——值班的人会把它静音，然后真正的那次也看不见了。

**降级模型**在主模型出可重试故障时切到备用模型。切换是**粘**的：一旦降级，本次运行剩下的调用全走备用模型。半路切回会让同一次任务的前后答案来自不同模型，比一直用备用模型更难解释。还有一条硬规则：**只有可重试的故障才降级**。参数错、上下文超长这类故障换模型也解决不了，降级只会把一个明确的错误变成一个模糊的错误，还顺手把备份配额烧掉。

**自动熔断**在连续失败到顶时停止打上游。状态机是三态：`closed` 正常放行；`open` 全部拒绝直到冷却结束；`half_open` 放**一个**探测请求过去，成功就闭合，失败就重新打开。为什么要有 `half_open`：一直 `open` 的话上游恢复了也没人知道，只能等人手动介入。

### 2.5 值班手册的四个字段

一条值班条目必须有四样东西：**信号**（看到什么）、**阈值**（多算数算异常）、**动作**（做什么）、**升级路径**（什么时候找谁）。缺阈值就变成了凭感觉，缺动作就变成了开会，缺升级路径就变成了一个人扛到天亮。

第 12 章的 SLI 正好是信号的来源：任务成功率、工具错误率（按工具名分桶）、无进展率、每次成功成本。**阈值应该从 SLI 的历史分布里取**——拍出来的阈值要么天天误报，要么永远不报。

## 三、为什么这样设计：取舍与失效条件

这一节讲的是**什么时候不该这么做**。上线方案照抄别人的，通常就是从这一步开始的。

**什么时候不该容器化。** 本地单人用的脚本、每天跑一次的批处理、需要 GPU 的本机推理——容器化对它们是纯负担：多一层镜像构建，多一层排障（出错了得先知道是代码的问题还是容器的问题）。判据很简单：**这个进程的生命周期超过几天、或者有第二个人会碰它**，才值得容器化。

**什么时候不该上熔断。** 低 QPS 的内部工具、故障本来五分钟就自愈的场景。熔断打开一次，就有一段时间所有请求被拒——这个代价必须小于它挡住的代价。QPS 低于 1 的服务，熔断带来的拒流量通常比它省下的故障还多。

**什么时候不该用配置中心。** 三个环境、五个配置项的时候，一份 `.env` 加一份 YAML 就够了。配置中心的真实成本是：多一套需要高可用的基础设施、多一套权限与审计、一条"配置改错了导致全量实例行为异常"的故障路径。这些成本要等到配置项多到几十个、或者多团队需要分别改不同环境时，才摊得平。

**什么时候不该降级模型。** 答案正确性比可用性更重要的场景——合同审核、医疗辅助、法律检索。降级模型换来的是"给出一个大概率错误的答案"，这比"明确地失败"更糟。判据是：**降级后的答案错了，后果比不给答案更严重吗？** 是的话就别降级。

**为什么把这些放在 CLI 层。** 因为它们是**运行期策略**，不是业务逻辑。放进 `agent.py` 就会污染主循环（契约里明写"主循环一行不用改"）；放进配置文件又无法表达状态机。CLI 站在依赖图最上层，可以读所有下层模块而不被任何下层依赖，是策略代码的天然位置。代价是：**这些护栏只在通过 CLI 启动时生效**——如果你的 Agent 是被别的服务当库调用的，护栏得下沉到编排层。

**还有一条容易忽略的取舍**：本工程的 `Agent.run()` 是单任务接口，多轮对话是把历史拼进任务文本实现的（`cmd_chat` 里那段拼接）。这是 CLI 层的补偿，不是引擎能力。接入有会话状态的编排层之后应该换掉——否则对话越长，任务文本越长，token 成本线性上涨。

## 四、动手：最小可实现

### 4.1 退出码先定下来

CLI 的第一个产出不是功能，是**退出码语义**。值班脚本和 CI 靠它判断，不用去解析输出：

| 退出码 | 含义 | 什么时候出现 |
| --- | --- | --- |
| 0 | 正常 | 任务完成、评测全过 |
| 2 | 用法错 | 参数缺失、子命令不存在 |
| 3 | 配置错 | `--live` 但没有 API Key |
| 4 | 任务失败 | `error` / `max_steps` / `stopped_no_progress` |
| 5 | 熔断或预算耗尽 | `stopped_budget` |
| 6 | 评测回归 | 有用例挂掉，或相对基线退步 |

### 4.2 缺 key 要早失败，而且要说清楚

缺 key 属于启动期错误：说清楚缺什么、怎么给，然后非 0 退出。**静默失败比报错贵得多**——报错还能改，静默失败只会烧钱。

```python
    try:
        config = build_config(args)
    except ConfigError as exc:
        # 缺 API Key 属于启动期错误：说清楚缺什么、怎么给，然后非 0 退出。
        _err("")
        _err("=" * 72)
        _err(f"错误：{exc}")
        _err("")
        _err("配置方式（二选一）：")
        _err('  PowerShell : $env:LLM_API_KEY = "sk-..."')
        _err('  bash       : export LLM_API_KEY="sk-..."')
        _err("  自建服务用 LLM_BASE_URL 指定地址，OPENAI_API_KEY 同样有效。")
        _err("")
        _err("不加 --live 就能用 mock 模式跑通全部子命令，不需要 key。")
        _err("=" * 72)
        return EXIT_CONFIG
```

### 4.3 阈值集中在一处，且优先级明确

```python
#: 阈值的环境变量名。配置文件中心（ConfigMap / 配置中心）把它下发成环境变量，
#: 命令行开关再覆盖它。优先级：**命令行 > 环境变量 > 内置默认值**。
THRESHOLD_ENV_VARS: dict[str, str] = {
    "warn_ratio": "DRAGON_WARN_RATIO",
    "degrade_ratio": "DRAGON_DEGRADE_RATIO",
    "breaker_failures": "DRAGON_BREAKER_THRESHOLD",
    "model_rps": "DRAGON_MODEL_RPS",
    "tool_concurrency": "DRAGON_TOOL_CONCURRENCY",
    "queue_capacity": "DRAGON_QUEUE_CAPACITY",
}
```

环境变量夹在中间是刻意的：它既能承载"配置中心下发的全局档位"，又能让值班的人在某一次运行里用命令行临时覆盖。`CostThresholds` 是 frozen dataclass——可以整体替换、可以打印、可以被断言，而且不会在运行期被某个模块偷偷改掉。

### 4.4 限流与熔断的返回值，不是异常

```python
    def allow(self) -> bool:
        """现在放不放行。"""
        if self.state == "closed":
            return True
        if self.state == "open":
            if self._clock() - self._opened_at >= self.cooldown_s:
                self.state = "half_open"
                return True
            self.rejected += 1
            return False
        # half_open：放一个探测请求。
        return True
```

三态之间只有这一个入口能改状态，任何模块都改不了别处的状态——护栏只能收紧不能放宽，这条纪律在第 9 章立过，这里同样成立。

### 4.5 容器化清单的最小骨架

```dockerfile
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY dragon_agent ./dragon_agent
RUN python -m compileall -q dragon_agent \
 && PYTHONPATH=/build python -c "import dragon_agent.cli as c; c.build_parser()" \
 && echo "构建阶段自检通过：CLI 解析器可构造"

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/dragon-agent \
    DRAGON_WORKSPACE=/workspace \
    DRAGON_TRACE_DIR=/workspace/traces \
    HOME=/tmp

RUN groupadd --gid 10001 dragon \
 && useradd --uid 10001 --gid dragon --no-create-home --shell /usr/sbin/nologin dragon \
 && mkdir -p /workspace /tmp/dragon \
 && chown -R dragon:dragon /workspace /tmp/dragon

COPY --from=builder --chown=dragon:dragon /build/dragon_agent /opt/dragon-agent/dragon_agent

WORKDIR /workspace
USER 10001:10001
```

构建阶段那句 `python -c "import dragon_agent.cli ..."` 是**构建期自检**：镜像构建完就已经证明 CLI 解析器能构造，比推到线上才发现 import 失败便宜得多。

### 4.6 跑起来

```powershell
cd dragon-agent

# mock 模式：不需要 key，全部子命令都能跑通
python -m dragon_agent.cli run "查一下项目预算，并告诉我总额"
python -m dragon_agent.cli demo
python -m dragon_agent.cli eval
python -m dragon_agent.cli runbook

# 真跑：缺 key 会以退出码 3 明确提示，不会静默失败
$env:LLM_API_KEY = "sk-..."
python -m dragon_agent.cli run "查一下项目预算" --budget 20000 --cost 0.05 --fallback-model gpt-4o-mini
```

## 五、开源实现对照

本章的每个机制在生态里都有对应物，值得对照着看——**看别人怎么做，比看别人怎么说有用**。

| 项目 | 仓库 | 许可证 | 本章可对照的点 |
| --- | --- | --- | --- |
| OpenHands | https://github.com/All-Hands-AI/OpenHands | MIT | 以 Docker 沙箱作为通用开发智能体的默认执行环境，与本章的镜像清单同源 |
| OpenLLMetry | https://github.com/traceloop/openllmetry | Apache-2.0 | 自动插桩把 LLM 调用转成 OpenTelemetry span，与第 12 章的埋点思路一致 |
| OpenTelemetry GenAI 语义约定 | https://github.com/open-telemetry/semantic-conventions | Apache-2.0 | 跨厂商的属性命名约定，成本账与告警要跨后端时得靠它 |
| Langfuse | https://github.com/langfuse/langfuse | MIT（核心）+ EE 目录企业许可 | 自托管的追踪、评估与提示管理，值班看板的现成底座 |
| Arize Phoenix | https://github.com/Arize-ai/phoenix | Elastic License 2.0（`Elastic-2.0`） | 基于 OpenTelemetry 的 LLM 追踪与嵌入评估 |
| AgentOps | https://github.com/AgentOps-AI/agentops | MIT | 记录运行轨迹、错误与会话回放的 SDK |
| LangSmith SDK | https://github.com/langchain-ai/langsmith-python | MIT | 追踪、数据集与评估的平台客户端 |
| OpenAI Agents SDK | https://github.com/openai/openai-agents-python | MIT | 用 Agent、工具、交接、护栏与追踪拼装工作流，护栏思路可参考 |

对照时值得注意的是**分工**：OpenLLMetry 与 OpenTelemetry 语义约定解决"埋点与导出"，Langfuse、Phoenix、AgentOps、LangSmith 解决"存下来看"，OpenHands 解决"在受控环境里执行"。本章的 `cli.py` 做的是这两者之间那一层——**策略与清单**：阈值、限流、熔断、降级、值班条目。这些东西在生态里往往散落在各自产品的配置项里，没有一份统一的、能打印的、能在凌晨三点被人照着做的清单。

## 六、坑与检查清单

**坑一：把"限制出网"写在 Dockerfile 里。** 没有网络语义的地方写不出网络策略——唯一能落地的地方是编排层（见 §2.1）。写进 README 的"本容器只访问模型 API"是愿望，不是控制措施。

**坑二：占位符没换就上线。** 本章 `deploy/k8s.yaml` 里的 `<模型服务网段>` 是占位符。它留在文件里时，上线演练会把它标成待办而不是通过——因为检查项本身就是"这个占位符还在吗"。

**坑三：探针写成 httpGet，但服务是 CLI。** 本工程的命令行入口没有 HTTP 服务端，探针会永远连不上，表现为 Pod 永远不 Ready、发布卡住，最后有人把探针删掉。用 exec 跑 `python -m dragon_agent.cli tools`——不联网、不调模型、只读内存，退出码 0 即健康。

**坑四：两侧用户 id 不一致。** Dockerfile 用 10001，编排层就得写 `runAsUser: 10001`。不一致的后果不是报错，是挂载卷属主对不上、进程写不了 trace 目录——表现为一重启就丢数据。

**坑五：只读根忘了留 `/tmp`。** Python 的临时文件需要它。忘了写 tmpfs，容器会在一个看起来毫不相关的地方失败。

**坑六：预算只设了 token 没设钱。** token 预算和美元预算是两笔账：换一个有缓存的模型，token 数不变但成本翻倍。`--budget` 管前者，`--cost` 管后者。

**坑七：熔断没有半开。** 只会打开不会恢复的熔断，等于一次故障变成永久停服——比没有熔断更糟。

**坑八：告警刷屏。** 每一步都报一次同一个告警，值班的人会静音它，然后真正的那次也看不见。每档只报一次。

**上线检查清单**（可直接抄成流水线里的检查项）：

- [ ] 镜像分层：构建与运行阶段分开，运行镜像里没有编译器与构建缓存
- [ ] 只读根：运行期不写文件系统，可写路径显式挂载，`/tmp` 单独给 tmpfs
- [ ] 非 root：非 root 用户运行，丢弃全部 capabilities，禁止提权
- [ ] 最小出网：编排层有 NetworkPolicy，**占位符已替换**
- [ ] 密钥：不进镜像层、不进代码库、不进日志；轮换流程演练过
- [ ] 配置：非密配置走配置中心，改动不需要重新构建镜像
- [ ] 限流：模型侧与工具侧**分别**设阈值；队列有界且丢弃数被观测
- [ ] 成本：token 与美元两笔预算都设了；告警、降级、熔断三档线都配了
- [ ] 熔断：带半开探测，冷却时间有依据
- [ ] 值班手册：每条信号都有阈值、动作、升级路径；阈值取自 SLI 历史分布
- [ ] 退出码：脚本与 CI 靠退出码判断，不解析输出
- [ ] 探针：exec 探针能真实反映进程健康

## 七、小结与延伸阅读

上线不是"部署"这一步，是**四个问题同时有人管**：跑在哪儿（容器化清单）、配置和密钥从哪来（配置中心与密钥管理）、一次任务最多花多少（成本护栏）、坏了谁在几分钟内动手（值班手册）。

这四件事有一个共同的结构：**它们都是"在事情变糟之前先停手"**。只读根在进程写坏东西之前停手，限流在压垮下游之前停手，熔断在持续烧钱之前停手，预算告警在账单失控之前停手。而值班手册是最后一层——停手之后，得有人知道该做什么。

本章的代码全部落在一个文件里（`dragon_agent/cli.py`），因为它们共享同一批参数与阈值。真实系统里这些会散开：限流器进网关，熔断器进服务网格，预算进计费系统。但**清单必须是统一的一份**——值班手册的价值恰恰在于它只有一份。

延伸阅读：

- 第一卷第 31 章《沙箱与执行隔离》：为什么容器是 Agent 的默认执行环境，以及沙箱与容器的关系
- 第一卷第 33 章《可观测性：Trace、Span 与 Agent 专属指标》：SLI 的定义，值班阈值的来源
- 第一卷第 35 章《成本与性能优化》：成本模型与每次成功成本
- 第一卷第 36 章《生产化检查清单与真实故障模式》：更完整的上线前检查项
- 第二卷第 9 章《护栏与权限：能力最小化》：护栏只能收紧不能放宽这条纪律
- 第二卷第 12 章《可观测性：接上 trace、指标与成本账》：`gen_ai.*` 命名约定与 trace 抽样
- 本章 README：完整的值班手册与部署清单说明
