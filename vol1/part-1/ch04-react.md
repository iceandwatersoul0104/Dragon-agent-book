# 第 4 章 ReAct：推理与行动交替的经典范式

## 一、本章要解决的问题

先讲一个翻车现场。

团队里有 Agent 回答"爱因斯坦出生于哪一年、当时德国的首都是哪里"。最早那套写法是"先规划、再执行"：模型输出一整段思考——"我需要先确认爱因斯坦的出生年份，然后查 1879 年德国的首都，最后核对两个答案"——然后在**没有拿到任何一条外部数据**的情况下，直接写出了结论："1879 年，当时德意志帝国的首都是柏林"。

这次侥幸对了。问题在于这套写法在结构上无法被证伪：思考链里没有任何来自外部世界的字节，模型在"确认"这两个字上做的其实是从参数记忆里抄答案。抄对了叫运气，抄错了叫幻觉，两者在日志里长得一模一样，事后根本分不开。

换个更典型的。Agent 要查"公司 A 的营收"，规划型写法的规划里写的是"调用 search(query='公司A 营收')"，执行层返回了一段超时错误。但由于规划已经全量展开、后续步骤都建立在"我已经拿到搜索结果"这个假设上，错误沿着假设一路传播，最后交出一份排版完整、逻辑通顺、逐项虚构的营收报表。这就是链式推理（Chain-of-Thought, CoT）范式里典型的错误传播（error propagation）：错误不会停在出错的那一步，它会污染后面所有以它为前提的推理。

ReAct（Reasoning + Acting，推理与行动交替）要解决的正是这件事：**把外部事实插进思考链内部，让每一步推理都被环境真实返回的结果修正，而不是在开头把所有假设一次性押上。**

这一章要拆到能照着写的颗粒度：轨迹格式为什么长这样、交错什么时候赢什么时候输、它和 Function Calling 到底什么关系。

## 二、原理：机制拆解

### 2.1 轨迹格式：四个字段，两种角色

ReAct 原始论文（Yao 等，2022）的轨迹由四个字段交替组成：

```
Thought: 检索爱因斯坦的出生信息，确认出生年份。
Action: search
Action Input: Albert Einstein
Observation: Albert Einstein (1879-1955) was a German-born theoretical physicist.
He was born in Ulm, in the Kingdom of Württemberg in the German Empire.
```

- **Thought（思考）**：一小段自然语言推理，说明当前已知状态和下一步意图。它是模型写给自己的记账本，也是事后排查时唯一能看出"它为什么调这个工具"的地方。
- **Action（动作）**：工具名。
- **Action Input（动作输入）**：传给工具的参数。
- **Observation（观察）**：环境的真实返回。

最关键的一层语义在 Observation：**它是系统写入、模型只读的事实通道**。模型在生成下一步时看到的是"工具真的返回了什么"，而不是"我希望它返回什么"。这个方向性是 ReAct 全部收益的来源——只要 Observation 允许模型自己编，ReAct 就退化成一个更啰嗦的 CoT。

字段顺序也有讲究：Thought 在前，意味着每一步思考都发生在**已知最新观察之后**；Observation 在后，意味着每个观察都立刻影响下一次生成，而不是攒到最后统一总结。

### 2.2 循环骨架

```
┌────────────────────────────────────────────────────────────────┐
│  ReAct 循环（单 Agent，严格串行）                                │
└────────────────────────────────────────────────────────────────┘

  Task ──► Prompt = 系统指令(角色/工具清单) + Few-shot 轨迹 + Task
             │
             ▼
      ┌─────────────┐   自由文本    ┌────────────────┐
      │  LLM 调用    │──────────────►│  动作解析器     │
      └─────────────┘               │ (正则+容错)    │
             ▲                      └───────┬────────┘
             │ 下一轮 prompt                   │ (name, args)
             │                                 ▼
             │                         ┌────────────────┐
             │                         │  工具执行器     │──► 真实副作用
             │                         └───────┬────────┘
             │                                 │ 原始返回值
             │                                 ▼
             │                        截断 + 保留标签净化
             │                                 │
             │                                 ▼
             └──── transcript 追加 Observation ────┘

  停止条件（任一命中即停）：模型 finish / 步数到顶 / 循环检测 /
                              连续解析失败到顶
```

对应的伪代码，四个要点：解析失败要能降级、观察要净化、动作要计数、步数要封顶。

```python
transcript = []
counts = Counter()
for step in range(max_steps):
    out = llm(prompt(system, few_shot, task, transcript))
    action = parse_action(out)            # 解析不出就返回 None
    if action is None:
        transcript.append(out)
        if consecutive_parse_failures >= max_parse_fail:
            return bail_out("格式漂移")
        continue
    if action.name == "finish":
        return answer(action.args)
    counts[(action.name, action.args)] += 1
    if counts[(action.name, action.args)] > repeat_limit:   # 循环检测
        return bail_out("重复动作")
    raw = tool_registry.call(action.name, action.args)
    obs = sanitize(truncate(raw, obs_limit))                # 观察回灌
    transcript.append(out)
    transcript.append(f"Observation: {obs}")
```

### 2.3 提示模板：三个块

**系统指令**说明有哪些工具、每个工具的参数格式、返回契约，以及输出必须遵循的字段顺序。工具清单要写参数签名，例如 `search(query: str) -> str`，因为模型是靠这段文字认识工具的。

**Few-shot 轨迹示例**是格式打样，不是教模型怎么思考。示例里最容易被写错的一点：**Observation 必须写成真实工具的返回（或高度接近的形态）**。如果示例里 Observation 是编的，模型会学会"Observation 我自己也能写"，ReAct 的前提当场塌掉。

**当前任务**放在最后，紧挨着轨迹，位置越靠后权重越高。

工程上还要考虑 stop 序列：如果模型支持停词，用 `\nObservation:` 之类作为停止边界，避免它一口气把好几轮轨迹都吐出来再让你去切。不支持也没关系，正则解析同样能跑，只是多花 token。

### 2.4 动作解析：容错是刚需

模型不会永远听话。实测最常见的偏差有六种：

- 标签大小写不一致：`action:` / `ACTION:`；
- 输出里出现多个 `Action:` 块；
- `Action Input` 跨多行，尤其是 JSON 参数换行；
- 参数写成 JSON：`Action Input: {"query": "einstein"}`；
- Thought 里提到"Action:"字样，模型其实在解释而不是在调用；
- 工具名拼错或用了中文名。

解析策略分三步：先用正则找所有 `Action\s*:\s*([A-Za-z_][A-Za-z0-9_]*)` 的匹配，**从后往前**挑第一个命中已注册工具名的（模型有时会先想一个动作再改主意，最后一个才是最终意图）；再在它后面找 `Action Input:` 段，注意终止条件要能识别下一个保留标签；参数统一按字符串处理，遇到 `{` 开头尝试 `json.loads`，失败就退回原字符串。

**绝对不要用 eval 解析模型输出**——模型输出就是不可信输入，`eval` 等于把 RCE 洞开给提示注入。

### 2.5 观察截断与保留标签净化

两个独立的坑。

**长度**：工具可能返回整页 HTML 或几万行日志。全灌进上下文，后面每一轮都要为它重复付费。先截断，保留头尾、中间用省略标记，比只留头部更不容易丢掉关键信息。

**污染**：工具返回里如果出现 `Action: delete_everything` 这样的字样，原样写进 transcript，模型下一轮很可能把 Observation 里的内容当成自己的动作去执行——轨迹瞬间被劫持。所以回灌前必须扫一遍：行首命中 `Thought:` / `Action:` / `Action Input:` / `Observation:` 的行，加前缀改写掉保留标签的语义。工具返回是不可信输入，要当数据看，不能当指令看。

### 2.6 停止条件：至少四个

1. **模型主动 finish**：由 `finish` 动作或框架约定的终止信号触发；
2. **步数上限**：硬顶。默认给个 5～10，具体看任务复杂度；
3. **循环检测**：同一个 `(action, args)` 重复 N 次直接中止；更宽松的做法是看最近 K 步的 action 签名集合是否完全相同；
4. **连续解析失败上限**：格式漂移不是靠重试能救的，连续 M 次解析不出动作就该终止并把原始输出留下——重试三次还是解析不出，说明模型没有按格式输出，继续烧钱只会重复失败。

只写第 1 条的实现在生产上活不过一周。

### 2.7 和 Function Calling 的关系：范式 vs 协议

这两件事经常被混为一谈，但它们在不同层。

**ReAct 是推理范式**，回答的是"模型和世界怎么交互"：一轮只做一个动作、拿到观察再决定下一步、思考与行动交替推进。

**工具调用（Function Calling）是一种协议**，回答的是"这一轮的动作怎么安全地传到运行时"：模型输出结构化的函数名和 JSON 参数，由运行时校验 schema、执行并把结果作为工具消息回灌。

两者可以叠加，今天的工程实现大多是叠加关系：

- ReAct 的思想诞生于 2022 年，早于主流厂商的 function calling 接口；
- 早期实现里，动作的载体是纯文本标签，需要正则从自由文本里抠出来；
- 换成 Function Calling 后，"Action / Action Input" 变成了结构化字段，"Observation" 变成了 tool message。**但循环结构一模一样**：模型思考 → 产出工具调用 → 运行时执行 → 结果回灌 → 再思考。

一句话记住：**ReAct 决定"要不要一轮轮地来"，Function Calling 决定"这一轮的动作怎么安全传过去"。思想在前，协议在后。** 反过来讲，只用 Function Calling 而每轮只调一次工具，那是"工具增强的单轮问答"，不是 ReAct。

## 三、为什么这样设计：取舍与失效条件

这一节比上一节重要。一个方案如果只有"什么时候好用"，那不叫架构判断，叫推销。

**第一，流程固定的任务别用 ReAct。** 报表生成、字段抽取、格式转换——你事先就知道哪一步接哪一步。用确定性代码或工作流引擎（DAG）跑，每一步可单测、可重放、可并行、失败可定位。用 ReAct 等于让一个概率组件来决定流程走向，还额外付每一步一次 LLM 调用的延迟和钱。

**第二，一步能答完的别用。** ReAct 的成本下限是两次 LLM 调用（至少一次思考、一次收尾）。分类、改写、摘要、单字段抽取，一次调用加一道输出校验更划算。

**第三，工具多而调用关系稳定时，用 Function Calling 加确定性路由。** 有 30 个工具、但调用模式永远是"先查后写"，让模型自由决策只会平白增加选错工具的概率和解析失败率。ReAct 的动作空间越大，动作分布越容易漂。

**第四，观察质量差时，交错是负收益。** ReAct 把工具返回当事实灌进上下文。返回本身是噪音——搜索结果全是广告、检索召回错、异常栈又臭又长——那么交错一次错一次，错误会被当成既定事实继续推理。这时候一次性 CoT 反而更稳，因为至少没有错误事实往上下文里灌。**ReAct 假设观察是可信的，观察不可信时它没有降级方案。**

**第五，需要并行调用时不适合。** ReAct 是严格串行：一次一个动作。要同时查 5 个数据源，ReAct 要 5 轮；拆成"规划 + 并行执行"的 Plan-and-Execute（第 7 章）更合适。

**第六，上下文成本。** 轨迹全在上下文里，10 步下来几万 token 很正常，而且每一步都要重发全部历史——第 10 章的上下文工程、第 11 章的成本守卫（guard）处理的就是这件事。

**第七，副作用型工具要隔离。** ReAct 允许模型在看到观察之前就决定动作。动作空间里一旦混着"发邮件""删文件""改数据库"，交错决策就直接落到不可逆操作上。正确做法是拆成两段：只读探查阶段随便调，写操作阶段加人工确认或策略校验。

再说取舍本身：ReAct 用**多轮延迟**换**每步的正确性**。在"一步错、步步错"的查证类任务上收益明显；在"多步但每步都很简单"的任务上，收益会被调用次数和解析失败吞掉。论文报告在 HotpotQA、FEVER 上相比只用推理或只用行动的方法都有改进，在 ALFWorld、WebShop 上相对模仿学习和强化学习基线有绝对成功率优势——但那是 1～2 个上下文示例的受控设定，别读成"随便接个工具就能超基线"。

## 四、动手：最小可实现

配套代码在 `code/ch04-react/`，纯标准库，没有第三方依赖，没有 API Key 也能跑通完整回路。

**依赖**：Python 3.9+，不需要 pip install 任何东西。

**安装**：拿到目录直接用，不需要安装：

```bash
cd code/ch04-react
python main.py
```

默认是 mock 模式：内置脚本化的"模型回复"和两个查询工具（`search` / `lookup`），外加一个 `finish`，完整演出"思考 → 解析动作 → 执行工具 → 观察回灌 → finish"这一圈。任务可以换：

```bash
python main.py --task "爱因斯坦出生于哪一年？当时德国的首都是哪里？"
```

三个故障场景开关，用来演示守卫真的会生效：

```bash
python main.py --scenario loop     # 反复调同一个动作 -> 触发循环检测
python main.py --scenario drift    # 输出不带 Action -> 触发格式漂移中止
python main.py --scenario recover  # 第一轮格式不对，第二轮自我纠正
```

**切真实模型**用 `--live`：

```bash
export LLM_API_KEY=sk-xxxxxxxx
export LLM_BASE_URL=https://api.openai.com/v1   # 任何 OpenAI 兼容的 chat completions 端点
export LLM_MODEL=gpt-4o-mini
python main.py --live --task "..."
```

`LLM_BASE_URL` 指向的端点需要兼容 `POST {base}/chat/completions` 的请求格式（body 含 `model`/`messages`，返回 `choices[0].message.content`）；换供应商时只改 `base_url` 和 `model` 两个环境变量，代码不用动。

常用参数：`--max-steps`（默认 8）、`--obs-limit`（观察截断长度，默认 600 字符）、`--repeat-limit`（同一动作重复几次判循环，默认 2）、`--trace`（打印每轮完整 prompt）。

代码里三个关键片段：

```python
RESERVED = ("Thought:", "Action:", "Action Input:", "Observation:")
ACTION_RE = re.compile(r"Action\s*:\s*([A-Za-z_][A-Za-z0-9_]*)", re.I)
INPUT_RE = re.compile(
    r"Action\s*Input\s*:\s*(.*?)(?=\n\s*(?:Thought|Action|Observation)\s*:|\Z)",
    re.S | re.I,
)

def parse_action(text, valid_names):
    """从模型输出里抽出一个动作；抽不出返回 None，交给上层降级。"""
    hits = list(ACTION_RE.finditer(text))
    for m in reversed(hits):                 # 从后往前，取最后一个合法工具名
        name = m.group(1)
        if name not in valid_names:          # valid_names 来自工具注册表
            continue
        seg = text[m.end():]
        m2 = INPUT_RE.search(seg)
        raw = (m2.group(1).strip() if m2 else "")
        args = raw
        if raw.startswith("{"):
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = raw                     # 解析不了就当字符串，不抛
        return Action(name=name, args=args)
    return None

def sanitize_observation(text, limit):
    """观察是不可信数据：先毁掉保留标签，再截断。"""
    lines = []
    for line in str(text).splitlines():
        stripped = line.strip()
        if any(stripped.startswith(tag) for tag in RESERVED):
            line = "[tool-output] " + stripped   # 保留标签失去语义
        lines.append(line)
    out = "\n".join(lines)
    if len(out) > limit:
        head = limit // 2
        out = out[:head] + "\n...[truncated]...\n" + out[-(limit - head):]
    return out
```

循环检测就三行：

```python
key = (action.name, json.dumps(action.args, ensure_ascii=False, sort_keys=True, default=str))
counts[key] += 1
if counts[key] > repeat_limit:
    return stop("loop_detected", f"动作 {action.name} 重复 {counts[key]} 次")
```

## 五、开源实现对照

- **LangChain**（MIT，https://github.com/langchain-ai/langchain）：把"模型 + 工具 + 提示"封装成一条链，ReAct 循环在链内跑。上手快，适合工具不多、流程简单的情况；想加护栏（guardrail）、想逐查看中间态，就得往下沉到 LangGraph。
- **LangGraph**（MIT，https://github.com/langchain-ai/langgraph）：把 ReAct 循环显式画成状态图，节点是模型和工具，边是转移。好处在条件边可以挂"步数上限、重复检测、转人工"，还有检查点（checkpoint）能断点续跑；代价是简单任务上样板代码比手写一百行循环还多。
- **smolagents**（Apache-2.0，https://github.com/huggingface/smolagents）：换了个载体。让模型输出 Python 代码块而不是结构化动作，工具以函数形式调用。ReAct 的"推理与行动交替"内核保留，但动作从标签文本变成可执行代码，解析失败率大幅下降——代价是必须沙箱（sandbox）执行。
- **OpenAI Agents SDK**（MIT，https://github.com/openai/openai-agents-python）：Agent + 工具 + 交接（handoff）+ 护栏（guardrail）+ 追踪，工具调用走 Function Calling，轨迹靠追踪记录回看。

一句话对照：LangChain / LangGraph 是"ReAct 作为默认 Agent 形态"，smolagents 是"ReAct 精神但换了动作载体"，OpenAI Agents SDK 是"ReAct 循环 + 结构化工具协议"。

## 六、坑与检查清单

- [ ] Few-shot 示例里的 Observation 必须是真实工具返回，别让模型学会"Observation 我自己写"。
- [ ] 解析用正则 + 容错，禁止 `eval`；解析失败要有明确降级路径（重试 / 回灌纠错提示 / 终止）。
- [ ] 保留标签（Thought / Action / Action Input / Observation）在工具返回里必须改写，防止轨迹被劫持。
- [ ] Observation 有长度上限，超出要截断，别把整页 HTML 灌进上下文。
- [ ] 步数上限、循环检测、连续解析失败上限，三个守卫同时在。
- [ ] 副作用型工具与只读查询分开管控，不要混在同一个动作空间里让模型自由选。
- [ ] 同一 `(action, args)` 重复出现要计数并中止。
- [ ] 日志保存完整未截断的原始轨迹，上下文用截断版——否则排查时你看到的和模型看到的不一样。
- [ ] 对模型输出里出现保留标签的次数打点，这是格式漂移的早期信号。
- [ ] 工具名统一小写下划线风格；参数要么全字符串、要么全 JSON，别混。
- [ ] 明确区分"模型说要做"和"系统真的做了"：动作必须由解析器执行，光写在思考里不算数。
- [ ] mock 场景要覆盖正常、循环、格式漂移三条路径，回归时先跑 mock。

## 七、小结与延伸阅读

ReAct 的贡献不是让模型变聪明，而是**把外部事实变成推理链里的一等公民**，从而把"猜"变成"查"。它的完整契约是四个字段（Thought / Action / Action Input / Observation）加一组守卫（解析容错、观察截断与净化、循环检测、步数上限、失败重试上限）。它和 Function Calling 不是二选一：一个是推理范式，一个是调用协议，实践中通常叠加使用。

延伸阅读：

- ReAct: Synergizing Reasoning and Acting in Language Models — https://arxiv.org/abs/2210.03629（arXiv:2210.03629，2022-10-06 首次提交；项目页 https://react-lm.github.io）
- ReAct 论文官方实现 — https://github.com/ysymyth/ReAct
- LangChain — https://github.com/langchain-ai/langchain
- LangGraph — https://github.com/langchain-ai/langgraph
- smolagents — https://github.com/huggingface/smolagents
- OpenAI Agents SDK — https://github.com/openai/openai-agents-python
