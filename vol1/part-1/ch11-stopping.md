# 第 11 章 停止准则、步数上限与成本守卫

## 一、本章要解决的问题

给客户演示的 Agent 跑在一台有配额限制的机器上，对话式接口，前端有超时。问题不是"能不能做完"，而是"做不完的时候怎么办"：

- 第一个 Agent 跑了四十轮还在原地打转，把配额烧光才停；
- 第二个 Agent 第二轮就停了——工具返回"部分成功"，它当成完成，直接把半成品交给用户；
- 最糟的一个：Agent 已经执行了三十步，第三十一步超时，框架直接抛异常，用户拿到一个 500，而前三十步的成果全躺在日志里没人看。

三件事暴露同一个问题：**Agent 的终止机制没有被当成一等公民设计。** 默认行为要么是"没话说才停"，要么是"出异常就死"。

本章要把终止做成守卫（guard）：四类停止条件各有明确判定；无进展能从动作、观察、状态三个信号里看出来；token、时间、美元三种预算统一抽象；停止时返回**可用的部分结果（partial result）**而不是失败。

## 二、原理：机制拆解

### 2.1 四类停止条件

本章说的守卫（guard）是**运行级**的停止判定：管"这次任务还该不该继续跑"。第 8 章那个"不可重放守卫"是**调用级**的保护：只管"这个工具要不要重试"。两者不是一回事。

| 类型 | 触发 | 判定实现 | 返回什么 |
| --- | --- | --- | --- |
| 显式完成 | 模型说做完了 | 动作是 finish/answer，且通过完成条件校验 | 完整结果 |
| 预算耗尽 | token / 时间 / 美元越界 | 每次调用后扣账，任一维度越界即停 | 部分结果 + 建议 |
| 无进展 | 连续多轮没有新信息 | 三信号检测（见 2.2） | 部分结果 + 卡点 |
| 异常终止 | 工具抛异常、输出不可解析 | 捕获后区分可重试与不可重试 | 部分结果 + 错误 |

关键点：**四类的判定都必须由确定性代码做，不能交给模型自己说"我完成了"。** 模型说完成只是一种输入信号，真正的完成判定要落在可验证的条件上——输出 schema 通过、必填字段齐全、测试跑过、产物确实落盘。

**停止优先级要写清楚。** 一次异常可能同时踩到预算和步数上限，先判哪个？建议顺序是：显式完成 → 异常终止 → 预算耗尽 → 无进展 → 步数上限。理由是：完成是唯一的好结果；异常要如实报告；预算和无进展都是"被迫停"，但预算既然已经超了就该立刻停，不要再多跑一轮去试探还有没有希望。

### 2.2 无进展检测：三个信号

同一个"没进展"有三种不同形态，必须分开检测，否则一定误判。

**信号一，动作重复。** 同一个 (action, args) 连续或累计出现 N 次。实现最简单：计数器。适合"反复调用同一个搜索、参数一模一样"的机械死循环。

**信号二，观察不变。** 动作在变，返回内容却一样——每次 search 都给出同样结果，因为查询词没变或数据没更新。这是"看起来在动、其实没动"，动作重复检测抓不到。对策：把最近 K 次的 observation 规范化后哈希（去掉时间戳、随机 id 这类每次都变的东西），相同就计数。

**信号三，状态无变化。** 动作不同、观察也不同，但环境状态没变——比如 Agent 反复查看某个工单，每次内容有细微差别（时间戳变了），工单状态却始终是"待处理"。这类最难抓，但对长任务最值钱。实现：让环境暴露一个状态指纹（state hash），Agent 层面比较它是否变化。

三信号分工明确：动作重复抓机械死循环，观察不变抓"假动作"，状态不变抓"真卡住"。**必须一起用**：只看动作重复会漏掉后两种；只看状态不变会误伤正常的长任务——有些任务本来就要好几轮才能推动状态。

一个容易忽略的细节：**重复判定要有窗口**。同一动作隔了十几轮又出现一次，未必是死循环，可能只是走了一条分支又回来。所以计数应该带窗口或衰减，而不是一路累计到死。

### 2.3 预算守卫：三种预算的统一抽象

token 预算、时间预算、美元预算看起来是三件事，其实是同一件事：**这次运行最多允许消耗多少资源**。统一抽象成一个对象：

```python
class Budget:
    max_tokens: int | None
    max_seconds: float | None
    max_usd: float | None
    price_in_per_1k: float      # 从价格表配置，不写死
    price_out_per_1k: float
```

每次模型调用后扣账：`tokens += 输入 + 输出`、`seconds += 耗时`、`usd += 输入/1000*价in + 输出/1000*价out`。任一维度越界即停。

三条实践要点：

1. **美元预算必须由单价表算，不要凭感觉估。** 模型价格会变，输入输出不同价，缓存命中另有计价规则。把价格表做成配置，代码只读配置。
2. **预算是硬边界，不是平均值。** 按均值定预算，会让百分之十的任务烧掉三倍配额。
3. **预算要分归属层级。** 单次调用的上限、单个任务的总量、单租户每小时的总量，是三道不同的闸，别拿一个数字糊弄。

### 2.4 超时与降级：返回部分结果，而不是失败

Agent 停止时，返回值应该是"**做完了多少 + 卡在哪 + 下一步建议**"，而不是异常。

部分结果至少包含四样：已完成的动作清单与结果摘要（上游可能已经用得上）；已获得的事实（检索到的关键信息、已写入的文件）；明确的停止原因与最后一步的观察；建议的下一步动作（给上游或人来接手）。

为什么重要：上游系统一般有"部分可用"这个状态。抛异常意味着上游只能整体回滚或整体失败；而返回部分结果，上游可以选择接受、提示用户、或把剩下的活交给人。

实现上有个坑：**不要把"部分成功"当成"成功"返回**。结果对象要同时带状态（completed / partial / failed）和内容，由调用方决定怎么用。把 budget_exhausted 包装成 completed，是这条路上最贵的一种偷懒。

### 2.5 停止条件的误判：过早 vs 永不

两类误判是对称的：

**过早停止。** 表现是"活没干完就宣布完成"。根因是完成判定太松——模型说完成了就信。修法：把判定从"模型说"换成"可验证的事实"。

**永不停止。** 表现是"一直转圈直到预算耗尽"。根因是只靠步数上限兜底，或无进展检测太钝。修法：无进展信号加预算双保险。

**怎么调？靠评测，不靠直觉。** 三组参数要用评测集扫：最大轮次、无进展窗口（重复几次算卡住）、预算上限。做法是拿一批真实任务跑不同参数组合，看两个指标：**完成率**与**平均轮次**（成本）。完成率随轮次上限上升而趋于饱和，平均轮次线性上升——拐点就是该设的上限。

还有一个必须测、却最容易被漏掉的指标：**过早停止率**（判定完成但其实没完成）。它不出现在完成率里，只出现在用户投诉里。

## 三、为什么这样设计：取舍与失效条件

- **上限设太紧 vs 太松**：太紧，复杂任务被腰斩；太松，烧钱。判据是任务复杂度的分布：多数任务几步能完，就按 p80 设；少数长任务靠分阶段、持久化解决，而不是靠拉高上限。
- **无进展检测会误伤**：观察不变在正常场景里也常见——用户在等异步任务、工具在轮询同一个资源。所以要把"观察不变"和"状态不变"分开，并允许显式声明"我在等待"。**等待要有心跳**：工具返回"处理中"时带一个推进标记，标记变了就不算无进展。
- **降级不是兜底，是接口的一部分**：如果上游只处理"成功/失败"两种状态，部分结果就没有意义。降级方案要在接口设计时就定下来，不是事后补。
- **别把成本守卫做成会计工具**：它的作用是在烧穿之前停下来，不是算账。
- **什么时候不需要守卫**：单次调用型任务没有循环，也就没有停止问题；有外部调度器兜底的任务（上层框架管超时和重试），Agent 内部不用重复实现。
- **一句诚实的提醒**：守卫只能保证"停下来"，不能保证"停下来是对的"。停止原因要如实上报。

## 四、动手：最小可实现

代码在 `code/ch11-stopping/`，纯标准库、无需 API Key。

**依赖**：Python 3.9+，零第三方包。

**运行**：

```bash
cd code/ch11-stopping
python main.py
```

一次跑六种场景，对照四类停止条件：

| scenario | 现象 | stop_reason |
| --- | --- | --- |
| normal | 三步后显式完成 | completed |
| repeat | 同一动作重复三次 | no_progress（动作重复） |
| stale | 动作不同、观察相同 | no_progress（观察不变） |
| frozen | 动作不同、观察不同、状态不变 | no_progress（状态无变化） |
| budget | token 预算耗尽 | budget_exhausted |
| error | 工具抛异常 | error |

```bash
python main.py --scenario budget --max-tokens 500
python main.py --max-steps 3
python main.py --trace
```

输出形如：

```
scenario   stop_reason      rounds  tokens        usd  status
normal     completed            5      196  0.000268  completed
repeat     no_progress          3       86  0.000119  partial
stale      no_progress          3       78  0.000112  partial
frozen     no_progress          3       89  0.000123  partial
budget     budget_exhausted     4      140  0.000195  partial
error      error                1       22  0.000035  failed
```

三个无进展场景分别命中三种信号，不会互相误判：`repeat` 报"动作重复"，
`stale` 报"观察不变"，`frozen` 报"状态无变化"。

非 completed 的场景都会打印"部分结果"的内容，演示降级返回长什么样：

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

注意 `"status"` 是 `partial` 而不是 `completed`——没做完就是没做完，这个字段不能粉饰。

**mock 说明**：内置模型按脚本返回动作，环境是本地确定性模拟；判定逻辑（预算扣账、无进展三信号、停止优先级、部分结果组装）全部真实。

**真实模型**：

```bash
export LLM_API_KEY=sk-xxx
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=gpt-4o-mini
python main.py --live --scenario normal
```

### 4.1 守卫的实现骨架

```python
class StopGuard:
    def check(self, step, steps_done) -> Optional[StopDecision]:
        # 1) 显式完成：模型给出 finish 动作，且通过完成条件校验
        if step.action.name == "finish":
            if self.completion_check(step):     # 可验证的事实，不是模型自述
                return StopDecision("completed", step.observation)
        # 2) 异常终止
        if step.error:
            return StopDecision("error", step.error)
        # 3) 预算耗尽
        exhausted = self.budget.check()
        if exhausted:
            return StopDecision("budget_exhausted", exhausted)
        # 4) 无进展
        stalled = self.progress.check(step)
        if stalled:
            return StopDecision("no_progress", stalled)
        # 5) 步数上限
        if steps_done >= self.max_steps:
            return StopDecision("max_steps", "达到步数上限")
        return None
```

注意 `completion_check` 是独立一步：模型说"完成了"只是候选，还要看产物是否真的存在、测试是否真的通过。

## 五、开源实现对照

- **LangGraph**（MIT，https://github.com/langchain-ai/langgraph）：递归上限与检查点，循环的停止是框架级配置；配合持久化还能在中断后继续。
- **LangChain**（MIT，https://github.com/langchain-ai/langchain）：Agent 执行器的最大迭代次数与早停。
- **OpenAI Agents SDK**（MIT，https://github.com/openai/openai-agents-python）：运行器的最大轮次、护栏与追踪。
- **smolagents**（Apache-2.0，https://github.com/huggingface/smolagents）：CodeAgent 的最大步数与执行超时。
- **Aider**（Apache-2.0，https://github.com/Aider-AI/aider）：编码场景里的另一条路——每步提交一个 git checkpoint，失败直接回滚，用"可回滚"代替"抛异常"。
- **Letta**（Apache-2.0，https://github.com/letta-ai/letta）：常驻服务里的轮次与预算上限。
- **LangSmith SDK**（MIT，https://github.com/langchain-ai/langsmith-python）：追踪与评测，是调停止阈值唯一靠得住的手段。
- **DSPy**（MIT，https://github.com/stanfordnlp/dspy）：把"什么时候停、怎么验"也当成可优化的模块。

## 六、坑与检查清单

- [ ] 四类停止条件都有确定性判定，不靠模型自述。
- [ ] 完成判定落到可验证事实：产物存在、测试通过、必填齐全。
- [ ] 停止优先级写清楚：完成 → 异常 → 预算 → 无进展 → 步数。
- [ ] 动作重复、观察不变、状态无变化三个信号分开检测。
- [ ] 观察哈希先规范化：去掉时间戳与随机 id。
- [ ] 重复计数带窗口或衰减，不要无限累计。
- [ ] "等待中"要有心跳标记，别把轮询判成卡死。
- [ ] token、时间、美元三种预算统一抽象，美元由价格表算。
- [ ] 价格表走配置，不写死在代码里。
- [ ] 预算分单次调用、单任务、单租户三层。
- [ ] 停止时返回部分结果：已完成动作、已知事实、停止原因、下一步建议。
- [ ] 结果对象区分 completed / partial / failed，由上游决定怎么用。
- [ ] 不要把"预算耗尽"包装成"已完成"。
- [ ] 用评测集扫参数：完成率、平均轮次、过早停止率三个指标一起看。
- [ ] 守卫日志落盘：每轮的动作、观察、预算余额、停止判定。

## 七、小结与延伸阅读

Agent 的终止机制应该和它的执行机制一样被认真设计。四类停止条件——显式完成、预算耗尽、无进展、异常终止——各有确定性的判定实现和明确的优先级；无进展要看三个信号，因为"重复动作""观察不变""状态不动"是三种完全不同的卡死；预算要用一个统一抽象把 token、时间、美元收在一起，单价从配置读而不是写死；停止时返回部分结果而不是异常，让上游还能用上已经做完的部分。真正的难点不在写守卫，而在调阈值：完成率、平均轮次、过早停止率三个指标必须一起看，靠评测集扫出来，不能靠直觉。

延伸阅读：

- LangGraph — https://github.com/langchain-ai/langgraph
- LangChain — https://github.com/langchain-ai/langchain
- OpenAI Agents SDK — https://github.com/openai/openai-agents-python
- smolagents — https://github.com/huggingface/smolagents
- Aider — https://github.com/Aider-AI/aider
- Letta — https://github.com/letta-ai/letta
- LangSmith SDK — https://github.com/langchain-ai/langsmith-python
- DSPy — https://github.com/stanfordnlp/dspy
