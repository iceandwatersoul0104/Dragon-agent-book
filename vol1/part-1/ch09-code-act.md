# 第 9 章 代码即行动：Code-Act 与 Program-of-Thought

## 一、本章要解决的问题

Agent 要给客户"张伟"出一份大额已支付订单的汇总。

用第 8 章的工具调用写法：`search_orders(customer="张伟")` 一次拿回 200 条订单，然后呢？"筛出金额大于 1000 且状态为已支付的，按金额降序取前 20 条，每条算含税金额，再按渠道分组汇总"——这些是参数吗？不是。工具调用的动作空间里根本没有"筛选""排序""分组""求和"这些动作。

于是只能把逻辑塞进工具：让 `search_orders` 支持 `filter`、`sort`、`limit`、`aggregate` 四组参数。结果是工具签名迅速爆炸，每个新业务都要加参数；模型要同时想清楚"调哪个工具、带哪些参数、筛选逻辑写在哪儿"；而工具本身越来越复杂，参数校验也越来越难（第 8 章）。

更糟的是这种写法没法复用。今天要"金额大于 100 的已支付订单"，明天要"金额小于 100 的退款订单"，工具就得加两组参数；等需求稳定下来，一个查询工具的签名可能有十几个可选字段，每个字段都是模型填错的机会。

Code-Act 的做法很直接：让模型直接写一段 Python 去调这些工具，逻辑交给 Python 表达。本章要拆的是——它到底强在哪、代价在哪，以及为什么"让模型写代码"这件事在工程上必须配一套硬性沙箱（sandbox）要求。

## 二、原理：机制拆解

### 2.1 两种"用代码"的机制差异

**Program-of-Thought（程序化推理）** 把**推理**转成程序：模型不直接算答案，而是写一个程序，把计算交给解释器，模型只负责"把题目翻译成程序"。PAL 在数学题上就是这么做的——模型只学"分解"，求解交给解释器。

**Code-Act（代码即行动）** 把**动作**组合成程序：工具不再是"被调用"的对象，而是被程序调用的函数。一次生成的代码可以调多个工具、可以循环、可以分支、可以接着上一轮的中间结果继续写。CodeAct 论文的核心动作是把 JSON 动作空间换成一个统一的 Python 动作空间。

差别在**谁决定流程**：PoT 里程序只是推理的载体，算完就结束；CodeAct 里程序是行动的载体，执行完还有副作用、有状态、有下一步。

### 2.2 表达力论证：代码能表达什么工具调用难表达

四类东西用函数调用很难写，用代码却很自然：

1. **循环与聚合**：对 200 条记录做过滤、排序、分组、求和。函数调用一次只能取回一个"预先定义好"的视图，要支持新的筛选维度就得改工具签名。
2. **组合与条件**：先查 A，如果 A 里有 `status=refund` 的订单，再查 B 并冲销。函数调用要表达这个，只能让工具自己长成工作流引擎。
3. **中间结果复用**：程序里的变量天然共享。第 7 章讲 ReWOO 要用 `#E1` 引用来预解耦，代码里一个变量名就解决了。
4. **标准库**：`json`、`re`、`datetime`、`statistics`、排序与分组——这些不是"业务工具"，但真实任务天天要用。全塞进工具里不可能。

反过来代价很清楚：**动作空间不再受限，也不再被约束**。函数调用有 schema 兜底，代码没有——这就是下一节沙箱问题的来源。

### 2.3 执行模型：四个必须定下来的决定

**一、解释器选择。**

| 形态 | 隔离强度 | 状态回传 | 启动开销 | 适用 |
| --- | --- | --- | --- | --- |
| 主进程 `exec()` | 无 | 天然共享 | 最低 | 只跑自己写的代码 |
| 子进程 | 进程级 | 要序列化 | 低 | 需要真实超时与配额 |
| 容器 | 强（可关网络、可限资源） | 要序列化 | 中 | 模型写的代码的标准答案 |
| 微虚机 | 最强 | 要序列化 | 高 | 多租户或对抗性输入 |

判据是"代码从哪来"：模型自己写的代码一律进容器，没有例外。

**二、状态保持。** Code-Act 常见做法是**同一个命名空间跨轮保留**。上一轮算出的 `candidates`，这一轮直接用，不必把结果重新叙述一遍（第 10 章的上下文问题）。代价是状态越滚越大，要定期清理或重置。

**三、标准库策略。** 白名单还是全放？白名单（math、json、re、statistics、datetime 等）可控但偶尔不够用；全放等于给模型一台没有网线的电脑——本地任意读写。折中做法是默认白名单，按任务显式放开（例如允许 pandas 要在配置里写明）。

**四、输出注入。** 程序执行完，`stdout`、最后一个表达式的值、异常堆栈，都要作为"观察"回灌给模型。这是循环能继续的关键——第 4 章的 Observation，在这里就是代码执行结果。

### 2.4 沙箱：硬性要求

模型写的代码是不可信输入。四条底线：

- **文件访问**：只给一个工作目录，其余路径一律拒绝；读与写分开授权（读可以宽，写要窄）。
- **网络**：默认全禁。真需要联网的工具走外部 API 代理，由宿主程序代为发起，而不是让代码自己 `import socket`。
- **进程**：禁止 `subprocess`、`os.system`、`multiprocessing`。模型不需要拉起子进程。
- **时限与配额**：墙上时钟时限 + 步数（行数）预算 + 内存上限。超时是必须的，否则一个 `while True` 就能挂住整个服务。

还有一条容易忽略：**沙箱报错要能被程序自己捕获**。`try: import socket / except SandboxError: ...` 是合理的降级路径；沙箱如果直接杀进程，模型什么也学不到。

> 现实提醒：上面这些在主进程里做，只能算"受限"，不算"沙箱"。真正的沙箱是容器或虚拟机 + seccomp + 只读根文件系统 + 资源限额。本章示例只演示机制，不是可上线的隔离方案。

### 2.5 工程陷阱

**幻觉 API。** 模型会写出看起来很像的库与方法（`slack.post_message`、环境里没有 pandas 却写 `pd.read_excel`）。表现是 NameError 或 ImportError。处理办法：把真实可用的工具签名写进提示，错误回灌让它自己改；**提示里的工具清单必须与命名空间里实际注入的函数一致**，否则模型永远学不会。

**隐式副作用。** 代码里的 `open(..., "w")`、全局变量、注册过的钩子，可能在模型没意识到的地方改了状态。工具调用和文件写入都要走宿主提供的受控接口，而不是裸 `open`。

**调试可观测性不足。** 代码一长，模型很难定位是哪一行错了。对策：stdout 分段回灌、traceback 保留关键行并截断、每轮给一份"执行摘要"（调了哪些工具、状态怎么变的）。这类可观测性缺失会让模型反复试错，每一轮都在烧 token。

## 三、为什么这样设计：取舍与失效条件

**什么时候不要用代码即行动：**

1. **动作集合固定且简单**：三五个工具、无组合需求，函数调用的 schema 约束更省心也更安全。
2. **必须严格约束的副作用**：支付、删库这类操作用代码写，等于把审批流交给一个概率组件。
3. **没有沙箱能力**：跑不了隔离环境，就不要让模型写代码——那是让模型在自己家里执行自己写的指令。
4. **模型代码能力弱**：小模型写长程序容易错，程序一错就是整轮白跑。

**表达力的代价：**

- 代码比 JSON 难校验：schema 能保证参数类型，代码保证不了，运行时得自己兜（校验、限额、审计）；
- 代码更长：token 消耗上升，且"写 50 行错一行"的概率不低；
- 可解释性下降：一段 50 行代码干了什么，比三次工具调用难读。

**Program-of-Thought 什么时候更合适**：纯计算、纯推理的题（数学、符号推理），没有副作用也不需要工具。PAL 这一路工作证明：模型负责翻译成程序、解释器负责算，比让模型直接算更准。

**Code-Act 什么时候更合适**：需要"取回一批数据再加工"、需要循环与条件、涉及多工具组合的任务。

一个务实的组合拳：把高风险动作（支付、发邮件）保留成函数调用或人工确认，只把只读的数据处理写成代码。

再具体一点，一条能落地的分工是：**取数与加工用代码，落地与变更用受控接口**。让模型写一段代码把 200 条订单筛好、算好、排好版，然后调用一个只接受"最终产物"的 `write_report`——写文件、发通知、扣款这一步不交给模型写的代码，而是交给宿主程序里的受控函数。代码负责表达力，接口负责安全，两边各取所长。

## 四、动手：最小可实现

代码在 `code/ch09-code-act/`，纯标准库、无需 API Key。

**依赖**：Python 3.9+，零第三方包。

**运行**：

```bash
cd code/ch09-code-act
python main.py
```

四轮演示：

1. **第 1 轮**：模型写程序调 `search_orders`，用循环筛选与求和——工具调用表达不了的部分；结果存在变量 `candidates` 里。
2. **第 2 轮**：模型写程序**复用**上一轮的 `candidates`（状态保持），调用 `write_report` 落盘；程序里 `import socket` 被沙箱拦下，程序自己捕获后继续跑完。
3. **第 3 轮**：模型调用了不存在的 `slack.post_message` → NameError → traceback 回灌。
4. **第 4 轮**：模型改用注册过的 `send_report`，任务完成。

```bash
python main.py --trace          # 打印每轮代码、stdout 与回灌内容
python main.py --timeout 1.0    # 墙上时钟时限（秒）
python main.py --max-lines 5000 # 行数预算
```

输出形如：

```
turn 1 | lines=31  | 命中 3 单，合计 1920.0
turn 2 | lines=128 | 沙箱拦截：模块 socket 不在允许列表里（可用：collections、datetime、…）/ 报告已生成
turn 3 | lines=1   | File "<agent_code>", line 1, in <module> / NameError: name 'slack' is not defined. Did you mean: 'slice'?
turn 4 | lines=5   | 已发送
turn 5 | lines=0   | (任务结束，模型不再输出代码)
```

第三轮的错误回灌里只有沙箱代码的帧——宿主文件路径和行号对模型没意义，还会泄漏内部结构。

**mock 说明**：内置"模型"按剧本返回四段程序，不做真实推理；解释器、工具、沙箱白名单、超时与行数预算都是真实逻辑。

**真实模型**：

```bash
export LLM_API_KEY=sk-xxx
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=gpt-4o-mini
python main.py --live
```

live 模式把可用工具的签名写进提示，要求模型输出 ```python 代码块。

### 4.1 沙箱的关键代码

```python
SAFE_MODULES = {"math", "json", "re", "statistics", "itertools", "datetime", "string"}

def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if root not in SAFE_MODULES:
        raise SandboxError("模块 %s 不在允许列表里" % name)
    return importlib.import_module(name)
```

行数预算与时限靠 `sys.settrace`：

```python
def tracer(frame, event, arg):
    budget["lines"] += 1
    if budget["lines"] > max_lines:
        raise SandboxError("超过行数预算")
    if time.perf_counter() > deadline:
        raise SandboxError("超过时限")
    return tracer
```

## 五、开源实现对照

- **smolagents**（Apache-2.0，https://github.com/huggingface/smolagents）：CodeAgent 把工具变成 Python 函数，模型直接写代码调用；沙箱用容器或子进程执行。
- **CodeAct**（https://github.com/xingyaoww/code-act）：CodeAct 论文的官方实现，把动作空间统一成 Python。
- **Aider**（Apache-2.0，https://github.com/Aider-AI/aider）：编码场景里的代码即行动——直接改文件、跑 git，每步自动提交，出了错能回滚。
- **OpenHands**（MIT，https://github.com/All-Hands-AI/OpenHands）：在 Docker 沙箱里执行模型写的代码与命令，是"沙箱是硬要求"的工程样板。
- **LangChain**（MIT，https://github.com/langchain-ai/langchain）与 **OpenAI Agents SDK**（MIT，https://github.com/openai/openai-agents-python）：函数调用路线，与代码路线互为对照。
- **DSPy**（MIT，https://github.com/stanfordnlp/dspy）：另一条中间路线——把"生成什么动作"声明成模块，用评测集优化提示，而不是让模型每次自由发挥。
- **PAL**（https://arxiv.org/abs/2211.10435）与 **When Do Program-of-Thoughts Work for Reasoning?**（https://arxiv.org/abs/2308.15452）：程序化推理的原始工作与边界分析——后者专门研究了代码复杂度与程序化提示效果的关系。

## 六、坑与检查清单

- [ ] 模型写的代码一律进隔离环境，没有例外。
- [ ] 文件访问限定在工作目录，读写分开授权。
- [ ] 网络默认全禁，联网需求走宿主代理。
- [ ] 禁止 `subprocess`、`os.system`、多进程。
- [ ] 同时设墙上时钟时限、行数预算、内存上限。
- [ ] 沙箱异常可被程序捕获，模型能读到错误内容。
- [ ] 提示里的工具清单与命名空间里实际注入的函数必须一致。
- [ ] 不给裸 `open`，文件读写走受控接口。
- [ ] stdout 分段回灌，traceback 截断但保留关键行。
- [ ] 每轮输出执行摘要：调了哪些工具、状态怎么变。
- [ ] 命名空间定期清理，变量别无限堆积。
- [ ] 高风险动作不要放进模型写的代码里。
- [ ] 长程序要求模型分段写，每段单独验证。

## 七、小结与延伸阅读

代码即行动强在表达力：循环、条件、中间结果复用、标准库——这些用函数调用要么表达不了，要么得把工具写成工作流引擎。它的代价同样明确：动作空间不再被 schema 约束，所以隔离从"可选的最佳实践"变成"硬性要求"——文件、网络、进程、时限、配额五条底线，一条都不能少。Program-of-Thought 与 Code-Act 的分界在于程序扮演的角色：前者是推理的载体，算完即弃；后者是行动的载体，执行完还有副作用和状态。工程上最实用的组合是：只读的数据处理写成代码，高风险动作保留成受约束的接口调用。

延伸阅读：

- PAL: Program-aided Language Models — https://arxiv.org/abs/2211.10435
- Executable Code Actions Elicit Better LLM Agents（CodeAct）— https://arxiv.org/abs/2402.01030
- When Do Program-of-Thoughts Work for Reasoning? — https://arxiv.org/abs/2308.15452
- Toolformer: Language Models Can Teach Themselves to Use Tools — https://arxiv.org/abs/2302.04761
- CodeAct 官方实现 — https://github.com/xingyaoww/code-act
- smolagents — https://github.com/huggingface/smolagents
- Aider — https://github.com/Aider-AI/aider
- OpenHands — https://github.com/All-Hands-AI/OpenHands
