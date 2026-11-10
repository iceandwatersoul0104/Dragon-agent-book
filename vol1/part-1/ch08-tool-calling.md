# 第 8 章 工具调用契约：把 Function Calling 当作 API 工程

## 一、本章要解决的问题

给 Agent 接了三个工具：查天气、查汇率、发日报。第一次上生产就炸了三种方式：

- 模型传了 `{"city": "上海", "unit": "kelvin"}`，工具里一句 `if unit not in ("celsius", "fahrenheit"): raise ValueError(...)` 直接把整个进程带崩。用户看到的是"服务不可用"，而不是"这个参数写错了"；
- 改成把异常吞掉、返回空字符串，Agent 继续跑，最后给用户一份基于空数据的"分析结论"；
- 最糟的一次：发日报的工具超时了，框架自动重试，用户收到了两封一模一样的日报。

三种故障其实是同一件事：**工具调用被当成了"模型给个参数、代码跑一下"，而它实际上是一份接口契约**。描述、参数、返回、错误、副作用，五样都是契约的一部分。

模型是这条链路上最不可靠的调用方——它填参数像填表单，字段填错、类型填错、枚举值编造是常态。所以接口设计的第一原则不是"怎么让正常调用跑通"，而是**"出错了怎么接住"**。

这一章给一套能直接落地的规范：schema 怎么写、校验失败怎么回灌、错误怎么表达、什么时候能并行、副作用工具怎么设计幂等。

## 二、原理：机制拆解

### 2.1 工具描述即提示词

工具描述不是文档，是喂给模型的提示词。写砸了的真实反例：

- ❌ `"查天气"` —— 模型不知道该传 `city` 还是 `location`、要不要单位、参数叫什么；
- ❌ 参数叫 `q`、`key`、`data` —— 语义不明，模型只能猜；
- ❌ 不写返回契约 —— 模型不知道拿到的是数字、对象还是文本，于是开始编字段；
- ❌ 不写可选性 —— 所有参数都被当成必填，或者该填的被省略；
- ❌ 把实现细节塞进描述 —— "调用 v2 接口，底层走 Redis 缓存"这类信息模型用不上，纯占 token；
- ❌ 不标注副作用 —— 模型不知道这个工具是只读还是会把数据写出去。

一条能用的描述要包含五样东西：

1. **一句话说清做什么、不做什么**（划边界比说能力更重要）；
2. **每个参数的语义**：取值范围、单位、格式，枚举值必须给全；
3. **返回契约**：字段名、类型、单位、是否可能为空；
4. **副作用标注**：只读还是写入；
5. **失败语义**：什么时候会失败、失败后系统怎么处理。

对照示例：

```json
{
  "name": "get_exchange_rate",
  "description": "查询指定币种对在参考日期的兑换率。只读，不产生任何副作用。",
  "parameters": {
    "type": "object",
    "properties": {
      "base":  {"type": "string", "description": "基准币种 ISO 4217 代码，如 USD"},
      "quote": {"type": "string", "description": "目标币种 ISO 4217 代码，如 CNY"},
      "date":  {"type": "string", "description": "参考日期 YYYY-MM-DD，缺省表示今天"}
    },
    "required": ["base", "quote"]
  },
  "returns": "{\"rate\": number, \"base\": string, \"quote\": string, \"date\": string}；查不到时 rate 为 null"
}
```

原则一句话：**模型用得上的信息都写上，用不上的都删掉。** 描述是给模型的，不是给人看文档的。

### 2.2 参数校验与修复：回灌，不要抛错

校验要分三层，缺一层就漏：

1. **模型侧约束**：schema 里的 type、enum、required，在模型生成时就尽量拦住——这是 API 提供方的责任，不是调用方的；
2. **运行时校验**：模型仍然会绕过 schema（手写 JSON、老版本客户端、提示里直接塞了 JSON 片段），所以运行时必须再校验一次；
3. **业务校验**：类型对但语义错（日期在未来、账号不存在），只有工具自己知道。

校验失败的处理原则是**既不抛、也不吞**：把它变成一条模型看得懂的失败信息回灌，让模型有机会自己改。

- 直接抛：一次工具异常让整个 Agent 挂掉，用户拿到的是 500，而真正的原因被埋在堆栈里；
- 吞掉：模型以为调用成功了，继续基于空值推理——这就是第 4 章说的 Observation 污染。

修复分三个级别，按确定性从高到低选：

- **自动规范化（normalize）**：单位大小写、字符串去空格、日期格式统一。这类变换无歧义，直接改，不要浪费一次模型调用；
- **交给模型修（repair）**：把错误回灌，等模型给新参数；
- **确定性兜底（fallback）**：有明确默认值或替代数据源时直接用。

判据是：**能不能在不引入模型不确定性的前提下确定性地修？能，就别去问模型。**

### 2.3 错误回灌的三种形式

| 形式 | 载荷 | 优点 | 坑 |
| --- | --- | --- | --- |
| 原始报错 | 后端异常字符串 | 信息最全 | 泄漏内部实现；格式不稳定；模型会照抄堆栈、SQL、内网地址 |
| 结构化错误 | code / field / message / allowed / retryable | 模型能精确知道哪个字段错了、允许什么值、能不能重试 | 信息量偏少时模型猜不出来 |
| 修复建议 | 结构化错误 + suggestion | 多数情况下模型只需要一个明确改法 | 建议错了比没有更糟 |

三者不互斥。推荐组合是：**结构化错误为主体，修复建议作为可选字段，原始细节截断后放进 detail 里**——detail 给开发者看，不要原样喂给模型。

两个细节容易踩：

- 错误消息里不要出现"内部错误 id xxx，请联系管理员查日志"这类需要模型去查的东西——模型没有查询渠道，它只会把这句话复述给你；
- 错误的 `code` 要稳定。`INVALID_ENUM` 是模型可以据此做判断的信号，`failed` 不是。

### 2.4 并行调用与依赖调用

判别依据是**参数依赖**，不是语义相似度：

- **独立调用**：参数不引用任何其他调用的结果 → 可以并发；
- **依赖调用**：参数引用了别的调用的输出（把查到的温度传进计算函数）→ 必须等前面完成。

怎么表达依赖是关键。让模型在参数值里引用前序调用 id（本节用 `{{call_1.temp_c}}` 这种形式），运行时分三段处理：

1. 扫描所有调用的参数，把"无引用"的挑出来作为一批并发执行；
2. 这一批的结果回来后重新扫描，直到没有新的可执行调用；
3. 结果按**模型给出的调用顺序**回灌，而不是按完成顺序。

**结果对齐是并行最大的坑。** 并发返回顺序取决于谁先完成，而模型是按位置读结果的。三条纪律：

1. 每个调用带唯一 id，结果按 id 归档；
2. 回灌时严格按原始请求顺序；
3. 失败的调用也要占位（返回错误对象），不能让位置错位——否则模型会把 A 的结果当成 B 的。

并行的收益是延迟从"步骤数 × 单次耗时"降到"批次数 × 单次耗时"。但并行度不是越高越好：同时打二十个工具会触发限流，出错时你也不知道该重试哪个。要设并发上限。

还有一个容易漏的细节：依赖值替换进来时总是**字符串**。前一步返回 18.0，塞进一个 `number` 类型的参数里就是 `"18.0"`，校验器会判它类型错误。引用解析完要做一次按 schema 的类型归一，否则下游会收到一堆莫名其妙的类型错误。

### 2.5 副作用工具的幂等性

只读工具（查天气、查库）重试没有成本；写工具重试可能把事情做两遍。四件套：

1. **幂等键（idempotency key）**：调用方生成一个稳定 id，重试时复用同一个键，服务端凭键去重。这是同时满足"不重复执行"和"可以安全重试"的唯一机制。
2. **重试策略**：只对可重试错误（超时、限流、5xx）重试；参数错误（4xx）重试是纯浪费。
3. **事务**：多个写操作要么全成要么全不成，避免"转账成功但记账失败"。
4. **补偿（compensating action）**：已经发生的事没法回滚时，用一个反向操作抵消（发通知失败 → 补发草稿；下单失败 → 释放库存）。补偿不是回滚，是尽力把状态拉回去。

**不可重放工具必须显式标注。** 有些工具天然不能重放：发送短信、扣款、提交表单、刷新一次性 token。它们要在契约里写死 `replayable: false`，运行时看到就**禁止自动重试**，把错误直接交回模型或人。这件事不能靠调用方记性——半年后没人记得哪个工具碰不得。

幂等键不能是随机数——重试要复用同一个键。正确做法是从业务语义派生，例如 `send_report:{报告日期}:{收件人}`，或用"调用 id + 轮次"组合。

## 三、为什么这样设计：取舍与失效条件

- **描述写详细的代价**：每次调用都要把描述发给模型，占 token；工具一多，描述本身会挤爆上下文（第 10 章）。取舍：常用工具写细，冷门工具写简；工具多时先选工具再加载描述。
- **自动修复的代价**：规范化可能改错——把用户写的"3/4"当成日期格式硬转。规范化要保守：只处理无歧义的变换。
- **并行的代价**：并发调用共享同一份上下文预算和限流配额；一个失败要不要全部重来？答案是不——分开重试。
- **错误回灌的信息量**：越详细模型越可能修对，但也越可能泄漏内部信息，而且模型可能直接忽略长文本。折中就是结构化 + 建议 + 截断。
- **幂等的代价**：幂等键要存储、要有过期策略，服务端复杂度上升。不是所有工具都值得——只读工具不需要幂等键，只有写工具才需要。
- **什么时候不该用工具调用协议**：只有一个工具、逻辑又简单时，直接写代码调用更清楚；要做的是"把这段文本抽成 JSON"时，用结构化输出（schema 输出）比用工具更合适——工具是为了"做事"，不是为了"填表"。

## 四、动手：最小可实现

代码在 `code/ch08-tool-calling/`，纯标准库、无需 API Key。

**依赖**：Python 3.9+，零第三方包。

**运行**：

```bash
cd code/ch08-tool-calling
python main.py
```

一次跑完四件事：

1. **校验与回灌**：模型传 `unit="kelvin"`（不在枚举里），运行时校验拦下，按选定的错误形式回灌，模型自己改成 `celsius` 后跑通；
2. **并行与依赖**：三个调用里两个独立（天气、汇率）并发执行，一个依赖前两者的结果（计算），运行时自动分批；
3. **结果对齐**：并发完成顺序被打乱，回灌时仍按模型给出的顺序；
4. **幂等与不可重放**：发通知遇到瞬时错误，用同一个幂等键重试，服务端只记一次；扣款工具标注不可重放，不自动重试，直接把错误交回。

这里的"不可重放守卫"是**单次工具调用级别的保护**：它只管"这个工具要不要重试"。
第 11 章讲的停止守卫是**整次运行级别的判定**，管的是"这次任务还该不该继续跑"，两者不是一回事。

```bash
python main.py --error-form raw        # 原始报错回灌
python main.py --error-form structured # 结构化错误回灌
python main.py --error-form suggestion  # 结构化错误 + 修复建议（默认）
python main.py --trace                 # 打印每批调用、回灌载荷与重试记录
```

输出形如：

```
batch 1: call_1:get_weather, call_2:get_exchange_rate（2 个，并行）
batch 2: call_3:compute_savings（1 个）
batch 3: call_4:send_report（1 个）
不可重放守卫：
  call_5 (transfer_funds) 未自动重试，状态未知
服务端幂等账本：
  send_report:2024-06-01:boss@example.com -> 请求 2 次，实际执行 1 次
  transfer_funds:ops-002:500 -> 请求 1 次，实际执行 0 次（未落库）
```

第一行是并行批次：天气慢、汇率快，完成顺序与请求顺序不同，但回灌仍按请求顺序。第二行是依赖调用，必须等前面成功。第三行开始是幂等与守卫——发日报请求了两次只执行了一次（第一次上游超时，重试复用同一个幂等键），划款请求一次执行零次（`replayable=false`，网关超时后状态未知，不自动重试）。

**mock 说明**：内置的"模型"按提示特征返回预设的调用序列，不是真推理；但校验、瞬时错误、幂等去重、依赖分批、结果对齐都是真实逻辑。

**真实模型**：

```bash
export LLM_API_KEY=sk-xxx
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=gpt-4o-mini
python main.py --live
```

live 模式下模型按 JSON 输出调用列表，解析器容忍多余文本。

### 4.1 校验器与错误载荷

```python
def validate(spec, args):
    errors = []
    for name in spec.params.get("required", []):
        if name not in args:
            errors.append({"code": "MISSING_PARAM", "field": name,
                           "message": "缺少必填参数 %s" % name})
    for name, rule in spec.params.get("properties", {}).items():
        if name not in args:
            continue
        value = args[name]
        if rule.get("type") == "number" and not isinstance(value, bool) \
                and not isinstance(value, (int, float)):
            errors.append({"code": "TYPE_MISMATCH", "field": name,
                           "message": "%s 必须是数字" % name, "allowed": ["number"]})
        if "enum" in rule and value not in rule["enum"]:
            errors.append({"code": "INVALID_ENUM", "field": name,
                           "message": "%s 取值非法" % name, "allowed": rule["enum"]})
    return errors
```

回灌载荷按形式组装：`raw` 只带 message；`structured` 带 code/field/allowed/retryable；`suggestion` 再加一条 suggestion。模型收到的永远是"错在哪 + 怎么改"，而不是一段堆栈。

## 五、开源实现对照

- **LangChain**（MIT，https://github.com/langchain-ai/langchain）：工具的 schema 声明、参数解析与错误处理都在框架层做掉，接手成本低。
- **PydanticAI**（MIT，https://github.com/pydantic/pydantic-ai）：用 Pydantic 模型定义工具签名，schema 与校验由类型系统生成——描述与参数语义写在类型旁边，天然一致。
- **OpenAI Agents SDK**（MIT，https://github.com/openai/openai-agents-python）：工具定义、护栏与并行工具调用在同一套抽象里。
- **smolagents**（Apache-2.0，https://github.com/huggingface/smolagents）：工具以 Python 函数签名暴露，参数类型直接来自注解，少了一层手写 schema 的漂移风险。
- **LangSmith SDK**（MIT，https://github.com/langchain-ai/langsmith-python）：追踪每次工具调用的入参、耗时与错误，是排查契约问题的第一手证据。
- **Model Context Protocol**（https://github.com/modelcontextprotocol/modelcontextprotocol，规范见 https://modelcontextprotocol.io/specification）：把"工具 schema + 调用协议"标准化成跨进程接口，可以看作工具调用契约的通用版本。
- **BFCL**（https://github.com/gorilla-llm/gorilla）：函数调用准确率与多轮工具使用的公开评测。

## 六、坑与检查清单

- [ ] 每个工具的描述写清做什么、不做什么、副作用是什么。
- [ ] 每个参数有语义、单位、格式；枚举值给全；返回契约写明字段与可否为空。
- [ ] schema 在生成侧拦一道，运行时再校验一道，业务语义由工具自己判。
- [ ] 校验失败一律回灌，不抛异常、不吞异常。
- [ ] 能确定性规范化（大小写、去空格、日期格式）就不要浪费一次模型调用。
- [ ] 错误用稳定的 code，结构化为主，附可选修复建议，原始细节截断后只给开发者。
- [ ] 错误消息里不要放需要模型去查的内部 id。
- [ ] 依赖用"参数引用前序调用 id"表达，运行时按引用关系分批。
- [ ] 结果按调用 id 归档，回灌严格按模型给出的顺序，失败项占位。
- [ ] 并发设上限，别把下游限流打爆。
- [ ] 写工具一律带幂等键，重试复用同一个键，不能用随机数。
- [ ] 只对可重试错误重试；参数错误不重试。
- [ ] 不可重放的工具（发送、扣款、提交）在契约里显式标注 `replayable: false`。
- [ ] 多写操作用事务；无法回滚的用补偿操作。

## 七、小结与延伸阅读

工具调用是接口工程，不是函数调用。三个要点：**描述是提示词**，写给模型看，缺语义缺单位的描述等于把猜错的活儿推给模型；**校验失败要回灌**，抛异常让 Agent 崩、吞异常让空值污染推理，两头都错；**副作用要有幂等设计**，幂等键加可重放标注，决定了重试是安全动作还是事故源。并行调用只解决延迟，不解决对齐——结果必须按 id 归档、按原顺序回灌、失败项占位。把这套契约写进代码，而不是指望模型每次都填对。

延伸阅读：

- Model Context Protocol 规范 — https://modelcontextprotocol.io/specification
- Model Context Protocol 仓库 — https://github.com/modelcontextprotocol/modelcontextprotocol
- JSON Schema 2020-12 — https://json-schema.org/draft/2020-12/release-notes
- LangChain — https://github.com/langchain-ai/langchain
- PydanticAI — https://github.com/pydantic/pydantic-ai
- OpenAI Agents SDK — https://github.com/openai/openai-agents-python
- smolagents — https://github.com/huggingface/smolagents
- LangSmith SDK — https://github.com/langchain-ai/langsmith-python
- BFCL — https://github.com/gorilla-llm/gorilla
