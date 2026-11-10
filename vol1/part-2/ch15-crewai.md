# 第 15 章 CrewAI：角色—任务—流程心智模型

## 一、本章要解决的问题

一个"调研 + 出简报"的流程，团队选了 CrewAI，因为好上手：给三个角色起名字、写目标、写背景，串两个任务，一天就跑通了。

两个月后出了三件事：

- **测试写不出来**。期望产出写的是"5 条要点，每条带出处"，但框架不拿它去验收——研究员交 2 条，Crew 照样报成功。断言最后只能写成"输出里包含'调研'"这种废话，等于没测。
- **跑偏了查不出原因**。想知道上一次为什么写歪了，只能重跑；重跑的结果又不一样。
- **加不了人工环节**。想在出简报前让人确认一下，不知道往哪儿加。

问题的根子在心智模型本身：CrewAI 把多 Agent 系统建模成**组织结构**——谁是什么角色、干什么活、谁先谁后。这套结构表达"谁做什么"非常清楚，表达不了"什么时候停、怎么验收、怎么回滚"。

本章拆这套模型：四个零件怎么拼起来、上下文怎么传、期望产出（`expected_output`）到底有多大作用、层级流程多花的钱花在哪、Flows 补的是哪块，以及最要紧的——角色扮演带来的自由裁量，怎么让行为难以断言。

## 二、原理：机制拆解

### 2.1 四个零件

**Agent（角色 + 目标 + 背景）**：`role` / `goal` / `backstory` 三行字直接决定系统提示词怎么写。这是 CrewAI 最核心也最容易被误解的地方——给 Agent 起个名字不是元数据，那三行就是提示词的主体。

**Task（描述 + 期望产出）**：一件活儿。`description` 说做什么，可以带 `{topic}` 这类占位符；`expected_output` 说"做成什么样"；还可以挂 `agent`、`context`，以及 `output_pydantic` 要求的结构化输出。

**Process（流程）**：`Process.sequential`（默认，任务按列表顺序跑）和 `Process.hierarchical`（引入 manager）。

**Crew（组装）**：`agents` 和 `tasks` 是两个平铺的列表，`process` 决定怎么驱动。没有图、没有节点、没有边——这是它和第 12 章的状态图最直观的区别。

```
   Agent(role, goal, backstory)        Task(description, expected_output, agent, context)
            │                                      │
            └────────────────┬─────────────────────┘
                             ▼
               Crew(agents=[...], tasks=[...], process=...)
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
      Process.sequential             Process.hierarchical
      任务按顺序跑，每个任务一次调用       manager 分配 → 成员执行 → manager 验收
```

### 2.2 一条提示词是怎么拼出来的

角色三件套 + 工具 + 上下文 + 任务描述 + 期望产出，就是全部。配套代码里第二个角色实际收到的提示词：

```
你是撰稿人。
目标：把研究结论写成一段读得懂、读得完的简报
背景：做过三年技术编辑，讨厌堆术语。
参考上下文（来自前置任务的产出）：
【根据调研结果写一份 开源 Agent 编排框架 简报 的产出】
调研结论（5 条）：
1. 多 Agent 编排正在从'写死流程'转向'模型选路'；
...
任务描述：根据调研结果写一份 开源 Agent 编排框架 简报
期望产出：一段 300 字以内的简报
```

没有别的隐藏配置。这既是 CrewAI 的优点（可读、可解释），也是它的麻烦（你只有这几个旋钮可调）。

### 2.3 上下文传递：`context=[前一个任务]`

CrewAI 传上下文的方式是往任务的 `context` 里放**任务对象**，不是字符串。框架在跑第二个任务之前，把 task1 的实际产出拼进第二个任务的提示词。

三点要注意：

1. **传的是完整产出，不是摘要**。前面产出越长，后面提示词越长，token 线性涨；
2. **上下文是显式声明的**：`context` 里没列的任务，它的产出不会自动流过去；
3. **"顺序"不等于"上下文相邻"**：顺序流程里 task2 紧跟 task1，但除非 `context=[task1]`，task1 的产出不会进 task2 的提示词。

### 2.4 `expected_output` 的实际作用

这是本章最值钱的一节。

**`expected_output` 会写进提示词，当成"合格线"给模型看。但框架不拿它去验收产出。**

配套代码的实测：期望"5 条要点，每条带出处"，mock 只交 2 条，Crew 照样报成功——两个任务都有输出，没有报错，没有异常。

三个原因叠在一起：

1. **角色扮演给了模型自由裁量**。"你是资深研究员，只信能追到原始记录的数字"这个设定本身就包含"你自己判断怎么干"，把验收权交给了角色；
2. `expected_output` 是自然语言，CrewAI 没有把它编译成可执行断言；
3. 框架把"任务跑完了"和"产出合格了"当成两件事，顺序流程只检查前者。

补法有三种：给任务挂 guardrail（框架新版本提供这个钩子）；在 Flow 里写死一道校验；或者用 `output_pydantic` 把输出形状固定成 schema——但要注意，**schema 只管形状，不管内容**。"5 条要点"写成 schema 也拦不住模型只交 2 条。

### 2.5 层级流程：manager 与它的额外成本

`Process.hierarchical` 会引入一个 manager。每个任务的动作从一步变成三步：**分配**（manager 看任务和成员名单挑人）→ **执行**（那个人干活）→ **验收**（manager 看产出判断过没过）。

配套代码的实测数字：

| 维度 | 顺序 sequential | 层级 hierarchical |
| --- | --- | --- |
| 任务数 | 2 | 2 |
| 模型调用总数 | 2 | 6 |
| 其中 manager 调用 | 0 | 4 |
| 平均每个任务 | 1.0 次 | 3.0 次 |

**平均每个任务从 1 次涨到 3 次。** 多出来的两次买到的是：派活儿不写在代码里，manager 按角色挑人；多了一层验收。

代价同样实在：成本三倍，manager 用贵模型就是直接三倍；**manager 派错人没人拦**——顺序流程里"谁干哪个任务"写在 `Task(agent=...)` 里，是确定的；层级流程里它变成了模型的判断，而模型的判断没有兜底。验收也在模型手里——用模型验模型。

所以层级的真实收益只有一个：**任务到成员的映射不固定**。映射固定的时候，层级流程纯属浪费。

### 2.6 Flows：把 Crew 装进确定性控制流

Flows 是 CrewAI 的第二层，用 `@start` / `@listen` / `@router` 三个装饰器，共享一份类型化状态。`@router` 方法的返回值是一个标签，标签本身是触发器，`@listen("标签")` 的方法据此分支。

分工很清楚：**Crew 负责中间那段开放式的活，Flow 负责前后的确定性部分**——校验、分支、人工闸门、落库。配套代码里的 Flow：

```
prepare → do_research（调 Crew）→ gate（校验条数，router）→ do_write（调 Crew）
                                                    ↓ insufficient
                                              need_more（停）
```

这就是 CrewAI 对前面那些问题的答案：**别把所有确定性都交给 Crew。** 能写进代码的判断就写进代码，Crew 只做它擅长的那段。

### 2.7 可测试性：角色扮演带来的自由裁量

这是整套心智模型最贵的一笔账。

一个普通函数是确定的：同样的输入给同样的输出，可以断言。一个"角色扮演"的 Agent 不是——`backstory` 改一个字，行为可能完全变；模型版本升级，行为也变。

CrewAI 里所有影响行为的自由度集中在三处：`backstory`（角色设定）、`expected_output`（合格线描述）、以及模型本身。所以断言只能写在**形状**上，不能写在**语义**上。配套代码里那道校验能成立，正是因为它不依赖语义：

```python
def check_expected_output(task, raw):
    """只数条数，不判断内容对不对——这是对角色扮演系统写断言的正确姿势。"""
    want = int(re.search(r"(\d+)\s*条", task.expected_output).group(1))
    got = len(re.findall(r"^\s*\d+\.", raw, flags=re.M))
    return got >= want, "条数 %d vs 期望 %d" % (got, want)
```

四条缓解手段：

1. **断言只写形状**：条数、字段、长度、是否包含某个 ID，不写"这段话是否讲清了 X"；
2. **把关键判断从 Crew 挪进 Flow**：Flow 里是普通函数，可以直接单测；
3. **用 `output_pydantic` 固定 schema**，把自由文本变成可校验的对象；
4. **评测用模型判分 + 人工抽检**，别指望字符串相等。

还有一个洞：回溯。CrewAI 有 `replay_from_task`（从最近一次 kickoff 重放），但没有 LangGraph 那种通用的检查点与恢复。任务跑一半崩了、跑到一半发现前面错了想改，都不好办。

## 三、为什么这样设计：取舍与失效条件

CrewAI 换来的东西很实在：极低的入门门槛（四行代码就能跑一个多 Agent 流程）、组织结构直觉（非工程背景的人看得懂、改得动）、以及完整脚手架（工具、记忆、委托、追踪都有）。

代价也很实在：行为不可断言；顺序流程的上下文是完整产出，token 线性涨；没有检查点、回溯和审批暂停；层级流程成本三倍。

下面是**不该选 CrewAI** 的几种情况：

1. **流程需要回滚、审批暂停、时间旅行**——去第 12 章的 LangGraph；
2. **需要精确控制每一步**——顺序流程写死了顺序，Flow 是事件驱动的，两者都不如显式状态图；
3. **产出要严格可断言**——角色扮演的自由度和这个需求直接冲突；
4. **一个 Agent 就够**——"一个人的 Crew"就是普通循环加一堆词汇；
5. **上下文很长**——顺序流程每个任务都带着前序全部产出，第三个任务时提示词已经不小了。

反过来，这些情况**该选 CrewAI**：

1. 流程结构本身就是角色分工，"调研—撰写—审校"这种；
2. 角色的专业性主要靠 backstory 体现——同一件事，不同资历的人写出来确实不一样；
3. 团队里不全是工程师，要让业务方自己调整流程；
4. 中间那段开放式的、需要来回试的任务，正好交给 Crew。

## 四、动手：最小可实现

配套代码在 `code/ch15-crewai/`，`main.py` 用 828 行纯标准库复刻这套心智模型，**不需要 API Key 就能跑通完整回路**，内置 mock 模型。

依赖：Python 3.9 及以上，零第三方包，不需要 `pip install`。

```bash
cd code/ch15-crewai
python main.py                    # 跑全部四个场景
python main.py --demo crew        # 只跑顺序 Crew
python main.py --demo expected    # 只跑 expected_output 的"软约束"演示
python main.py --demo hierarchical # 只跑顺序 vs 层级
python main.py --demo flow        # 只跑 Flow
python main.py --trace            # 打印每个角色收到的完整提示词
```

四个场景：

- **crew**：两个任务跑完，`--trace` 打印两个角色各自的完整提示词，能看到上下文是怎么被拼进去的。
- **expected**：故意让研究员只交 2 条（期望 5 条），看 Crew 怎么"报成功"，再自己加一道校验把它拦下来。
- **hierarchical**：同一个目标，顺序跑一遍、层级跑一遍，逐项对比调用次数。
- **flow**：Flow 里包两个 Crew，中间那道闸门真的会检查条数并决定走哪条分支。

几个关键机制在代码里长这样：

```python
# 1) 角色三件套直接拼成提示词
prompt = agent.build_prompt(task, inputs, context_text)

# 2) 上下文靠 context=[前一个任务] 传，传的是实际产出
def _context_text(task, inputs):
    return "\n\n".join("【%s 的产出】\n%s" % (t.render(inputs), t.output)
                       for t in task.context if t.output)

# 3) 层级流程：分配 + 执行 + 验收，每个任务三次调用
assignee = llm.assign(assign_prompt, roster, task.render(inputs))
raw = llm.complete(member_prompt, assignee, task)
verdict = llm.review(review_prompt)

# 4) Flow：路由标签就是触发器
@flow.router("do_research")
def gate(self):
    ok, _ = check_expected_output(research_task, self.state["research"])
    return "enough" if ok else "insufficient"

@flow.listen("insufficient")
def need_more(self):
    self.state["brief"] = "材料不足，先补齐再出简报。"
```

切真实模型加 `--live`，需要三个环境变量：

| 变量 | 必填 | 默认值 |
| --- | --- | --- |
| `LLM_API_KEY` | 是 | — |
| `LLM_BASE_URL` | 否 | `https://api.openai.com/v1` |
| `LLM_MODEL` | 否 | `gpt-4o-mini` |

live 模式下只有"干活、派活儿、验收"三处换成真模型，**角色三件套、上下文拼装、流程驱动、Flow 的分支全部不变**——这正是要验证的：换个模型之后，编排的行为是不是还是可预期的。

## 五、开源实现对照

- **CrewAI**（MIT，https://github.com/crewAIInc/crewAI）：本章讲的范式的参考实现。用角色、任务与流程模板组织 Agent 团队协作达成目标，`Flows` 是它对"确定性控制流"的补充。
- **AutoGen**（MIT（LICENSE-CODE）；根目录 LICENSE 为 CC-BY-4.0，https://github.com/microsoft/autogen）：对照组。同样是多 Agent 协作，它建模的是**会话**——共享历史、轮流发言；CrewAI 建模的是**组织**——角色分工、任务分派。想清楚"你要的是开会还是分工"，两者的差别自己就出来了。
- **LangGraph**（MIT，https://github.com/langchain-ai/langgraph）：对照组。显式状态图、检查点、中断与人工介入。它比 CrewAI 重，但多了 CrewAI 没有的三样：可回溯、可暂停、可断言的状态。
- **Agno**（Apache-2.0，https://github.com/agno-agi/agno）：相近的思路，用 Agent、Team、Workflow 组织多 Agent，AgentOS 承载持久运行。可以和 CrewAI 放一起比，看"角色式编排"这个抽象还有哪些变体。
- **Microsoft Agent Framework**（MIT，https://github.com/microsoft/agent-framework）：融合 AutoGen 与 Semantic Kernel 的统一层。它的存在说明一件事：微软自己也不认为会话式和角色式该二选一。

## 六、坑与检查清单

- [ ] `backstory` 写具体行为，不要写形容词——"只信能追到原始记录的数字"比"非常严谨"有用得多。
- [ ] `goal` 里的占位符和 `kickoff(inputs=...)` 的键要对齐，缺一个就报错。
- [ ] 期望产出写成可数的形状（"5 条要点"），别写成"高质量的分析"。
- [ ] 别指望 `expected_output` 自动验收——产出不合格框架不会拦。
- [ ] 需要严格产出形状时用 `output_pydantic`，但要知道它管不了条数。
- [ ] `context` 要显式列，别假设前一个任务的产出自动流过来。
- [ ] 上下文带的是完整产出，链长了先算 token 再决定要不要拆。
- [ ] 层级流程只在"任务到成员的映射不固定"时才用，固定映射纯属三倍成本。
- [ ] manager 用便宜模型，验收用便宜的模型——它们是每轮都要付钱的。
- [ ] 关键判断放进 Flow，Flow 里的函数可以直接单测。
- [ ] 断言只写形状：条数、字段、长度、包含某个 ID。
- [ ] 跨轮业务语义用模型判分加人工抽检，别用字符串相等。
- [ ] 需要回滚和审批暂停就别停在 Crew，去第 12 章。
- [ ] 每个角色单独设 `max_iter`，别让一个角色在内部循环里烧掉整轮预算。

## 七、小结与延伸阅读

CrewAI 的心智模型是一套**组织结构**：角色三件套定义提示词，任务是活儿，流程决定谁先谁后，Crew 把它们组装起来。它的强项是表达力和可读性——业务方看得懂，改得动。它的软肋是**自由裁量无处安放**：`expected_output` 只是提示不是契约，角色扮演让行为难以断言，顺序流程没有回溯能力。

三条带走的结论：**上下文靠 `context=[前一个任务]` 显式传，传的是完整产出；`expected_output` 是提示不是契约，验收要自己补；层级流程每个任务多两次调用，映射固定时纯属浪费。**

真要记住的只有一句：**凡是可以写进代码的判断，就别交给角色。** Crew 负责中间那段开放式的活，校验、分支、闸门、回滚留给 Flow 或状态图。角色扮演负责"谁来做"，控制流负责"什么时候停"——两件事混在一起，才是这套模型最贵的账单。

延伸阅读：

- CrewAI — https://github.com/crewAIInc/crewAI
- AutoGen — https://github.com/microsoft/autogen
- LangGraph — https://github.com/langchain-ai/langgraph
- Agno — https://github.com/agno-agi/agno
- Microsoft Agent Framework — https://github.com/microsoft/agent-framework
