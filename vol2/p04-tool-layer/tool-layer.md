# 第 4 章 工具层：注册、校验、错误回灌与副作用

## 一、本章要解决的问题

有个 Agent 接了三个工具：读文件、查数据库、发邮件。上线第二天，值班记录上写着四条：

- 模型把 `limit` 传成了字符串 `"100"`，工具层抛了 `TypeError`，任务崩了，用户看到"内部错误"；
- 模型幻觉出一个不存在的工具 `query_databse`，又崩一次；
- 查一个不存在的文件，崩第三次；
- 发邮件那一步超时，重试了两次——收件人收到了三封。

四条问题，同一个根因：**工具层的失败被当成"程序错误"处理了**。

前三条尤其荒唐。参数写错、工具名拼错、文件不存在——这三件事都是**模型自己造成的**，而模型完全有能力纠正它们：把错误告诉它，它下一次就会传对参数、换对工具名、换个文件。抛异常把这些信息全丢了，只剩下一个"内部错误"给用户看。

第四条则是另一类问题：发邮件是**不可逆**的，重试不是"再试一次"，是"再发一次"。

工具层要解决的就是这两类问题：

1. **失败怎么表达**——让模型的错误变成模型能读懂并纠正的信息，而不是一场崩溃；
2. **副作用怎么管**——哪些工具能重试、哪些不能、重复执行怎么挡。

## 二、原理：机制拆解

### 2.1 工具描述就是给模型看的提示词

工具层交给模型的唯一东西，是一份 schema 列表：

```python
    def schemas(self) -> list[dict[str, Any]]:
        """喂给 ``llm.complete(tools=...)`` 的参数列表。"""
        out = []
        for spec in self._specs.values():
            out.append({
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            })
        return out
```

三样东西：名字、描述、参数。**描述是模型唯一能看到的人类语言**，也是第一卷第 8 章那条"工具描述即提示词（Tool Description as Prompt）"的落点。描述写不清楚，等于把猜错的活儿推给模型。

跑一遍内置的四个工具，模型看到的就是这些：

```text
  read_text_file
    描述：读取工作目录下某个文本文件的全部内容。用于查看项目里的配置或源码。
    参数：['path']，必填 ['path']
  add
    描述：把两个整数相加，返回它们的和。
    参数：['a', 'b']，必填 ['a', 'b']
  submit_order
    描述：提交一张订单。**会产生不可撤销的副作用**，必须带幂等键。
    参数：['sku', 'qty']，必填 ['sku', 'qty']
  send_report
    描述：发送日报。幂等工具：同一个幂等键重复调用不会重复发送。
    参数：['content']，必填 ['content']
```

注意最后两个描述里带了副作用说明——**模型也需要知道这个工具会不会造成不可逆的后果**，那是它决定要不要重试的依据。

### 2.2 声明式注册：显式 schema 与类型注解生成

注册有两种写法。

**写法一，显式 JSON Schema**——`tools.py` 现在的做法：

```python
    @reg.tool(
        name="submit_order",
        description="提交一张订单。**会产生不可撤销的副作用**，必须带幂等键。",
        parameters={
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "qty": {"type": "integer"},
            },
            "required": ["sku", "qty"],
        },
        side_effect=True,
        replayable=False,
    )
```

**写法二，从类型注解生成**——本章新增的 `tool_schema.py` 提供的便利层，演示脚本里就是这么写的：

```python
from dragon_agent.tool_schema import schema_for

    def submit_order(sku: str, qty: int) -> dict:
        """提交一张订单。**会产生不可撤销的副作用**，必须带幂等键。"""
        return {"sku": sku, "qty": qty}

    generated = schema_for(submit_order)
    demo_reg = ToolRegistry()
    demo_reg.tool(name="submit_order", description=submit_order.__doc__,
                  parameters=generated)
```

生成出来的是这样：

```text
  生成出来的 schema：{'type': 'object', 'properties': {'sku': {'type': 'string'}, 'qty': {'type': 'integer'}}, 'required': ['sku', 'qty']}
  描述从哪来：函数 docstring——生成器不写描述
```

两者的取舍：

| 写法 | 好处 | 代价 |
| --- | --- | --- |
| 显式 JSON Schema | 结构与描述写在一处，一眼看出模型看到什么 | 字段多了啰嗦，改函数签名要改两处 |
| 类型注解生成 | 与函数签名一致，改签名自动改 schema | 生成的结构缺"什么时候用"，描述还得另写 |

`dragon-agent` 选显式，因为工具的 schema 同时挑两副担子：**给模型看的契约**和**执行前的校验依据**。两副担子都要求"写给人看"，注解生成器只擅长后者。

所以生成器是个**可选便利层**，不是默认路径。而且它有一条硬边界：**它不生成描述**。生成器能产出结构，产不出"这个参数什么时候该填、填什么格式"——而后者才是模型真正读的东西。

### 2.3 校验失败为什么不抛异常

`tools.py` 的 `invoke` 有个明确约定：**执行一个工具，不抛异常，所有失败都变成 `ToolResult(ok=False)`**。

```python
    def invoke(self, name: str, args: dict[str, Any],
               idempotency_key: str | None = None) -> ToolResult:
        """执行一个工具。**不抛异常**，所有失败都变成 ``ToolResult(ok=False)``。"""
        if name not in self._specs:
            return ToolResult(ok=False, error=f"未知工具 {name}")
        spec, fn = self._specs[name], self._fns[name]
```

失败有三个来源，出口是同一个：

1. **未知工具**——模型幻觉出来的名字；
2. **参数校验不通过**——缺必填、类型不对、枚举越界；
3. **工具内部抛异常**——文件不存在、数据库连不上。

三种都不抛异常，因为它们都是**模型可以纠正的信息**。第 2 章说过一句：Agent 的控制流必须把"失败"当成一等公民。工具层就是这句话的第一个落点。

校验的实现只覆盖 JSON Schema 的一个子集，但恰好是拦幻觉最有效的部分：

```python
        for key, value in args.items():
            if key not in props:
                continue  # 多余参数不致命，忽略即可
            expect = props[key].get("type")
            if expect in types:
                py_t = types[expect]
                if expect in ("number", "integer") and isinstance(value, bool):
                    return f"参数 {key} 应为 {expect}，实际是 boolean"
                if not isinstance(value, py_t):
                    return f"参数 {key} 应为 {expect}，实际是 {type(value).__name__}"
            enum = props[key].get("enum")
            if enum and value not in enum:
                return f"参数 {key} 只能取 {enum} 之一，收到 {value!r}"
```

两个细节值得说：

- **`bool` 不是 `integer`。** Python 里 `isinstance(True, int)` 是真的，所以不做特殊处理的话，模型传 `true` 会被当成 `1` 放过去。模型写 `true` 和写 `1` 不是一回事，这里必须分开。
- **多余参数在校验层被忽略。** 但它会在执行时失败——因为 `fn(**args)` 对不上函数签名：

```text
  [多余参数] add({'a': 1, 'b': 2, 'extra': '?'})
    ok=False  error=调用参数不匹配：default_registry.<locals>.add() got an unexpected keyword argument 'extra'
```

两种表现都不是异常——都是回灌给模型。

还有一条更重要的：**校验失败的调用，工具函数根本没被执行**。这关系到副作用：

```text
  实际执行的工具：[]  ← 校验失败的工具没有真的执行
```

### 2.4 错误回灌的三种形态

同样是"工具失败了"，模型需要做的动作完全不同：

| 形态 | 什么时候 | 模型该做什么 |
| --- | --- | --- |
| 字段级 | 参数缺失、类型不对、枚举越界 | 改参数重试 |
| 语义级 | 未知工具、权限不足、状态不允许、不可重放 | 换策略，不要硬试 |
| 环境级 | 文件不存在、网络不可用、服务端 5xx | 重试或换条路 |

分不清这三种，模型就会干蠢事：对着"文件不存在"反复改参数，对着"权限不足"反复重试——两种都在烧 token，而且都修不好。

所以回灌文本必须把形态标出来（实跑输出里抽三行）：

```text
    [field] 缺少必填参数 b —— 请修正参数后重新调用。
    [policy] submit_order 是不可重放的副作用工具，必须提供幂等键 —— 这个操作当前不可用，请换一种做法，或向用户说明情况。
    [environment] FileNotFoundError: 文件不存在：不存在的文件.txt —— 外部条件暂时不满足，可以稍后重试或改用其他来源。
```

**契约的缺口**：`tools.py` 的 `ToolResult.error` 是一条自由文本，上面的分类靠模式匹配。真正的做法是给 `ToolResult` 加一个 `error_kind` 字段——那是契约改动，按规矩要先改 `_plan/_dragon_contract.md`。在改之前，模式匹配集中在 `tool_runner.classify_feedback` 一处，判断逻辑只写一遍。

### 2.5 副作用与幂等：三档工具

工具按"会不会改变外部世界"分三档，每一档的重试和审批策略都不一样：

| 档位 | 例子 | 能重试吗 | 要幂等键吗 | 要审批吗 |
| --- | --- | --- | --- | --- |
| 只读 | 读文件、查数据库 | 能，重试代价只是慢一点 | 不需要 | 不需要 |
| 可逆写 | 发草稿、写临时文件 | 能 | 需要（防重复） | 视情况 |
| 不可逆写 | 下单、扣款、发邮件 | **不能** | 必须，且用过就拒绝 | 必须人工确认 |

`tools.py` 用两个布尔位表达这件事：`side_effect`（有没有副作用）与 `replayable`（能不能重放）。关键语义在幂等键这一段：

```python
        if idempotency_key:
            if idempotency_key in self._seen_keys:
                if not spec.replayable:
                    return ToolResult(
                        ok=False,
                        error=f"{name} 不可重放，幂等键 {idempotency_key} 已用过，拒绝重复执行",
                    )
                return ToolResult(ok=True, output=self._seen_keys[idempotency_key].output,
                                  idempotent_hit=True)
        else:
            # 没给幂等键：不可重放的副作用工具直接拒绝执行
            if spec.side_effect and not spec.replayable:
                return ToolResult(ok=False, error=f"{name} 是不可重放的副作用工具，必须提供幂等键")
```

三段语义，一段比一段严格：

1. **可重放的工具**（`replayable=True`）：同一个幂等键第二次调用，**不执行函数**，直接返回第一次的结果，并标记 `idempotent_hit=True`。调用方知道"这次是命中缓存"，可以据此决定要不要再发后续动作。
2. **不可重放的工具**（`replayable=False`）：同一个幂等键第二次调用，**拒绝执行**，返回一条错误。为什么不直接返回上次的结果？因为"返回上次的结果"等于骗调用方"它成功了"——而实际上第一次可能真的成功了，只是回执丢了。此时正确做法是**让调用方去查状态**，不是让工具层替它编一个答案。
3. **不给幂等键**：不可重放的副作用工具直接拒绝执行。理由很简单——**没有幂等键，就没法判断这次是不是重复操作**。

实跑结果：

```text
  send_report（可重放）
    第一次：ok=True  output={'sent': True, 'chars': 4}  idempotent_hit=False
    第二次：ok=True  output={'sent': True, 'chars': 4}  idempotent_hit=True
    实际执行次数：1（第二次没有真的发送）
  submit_order（不可重放）
    第一次：ok=True  output={'order_id': 'ORD-SKU-1-2', 'sku': 'SKU-1', 'qty': 2, 'status': 'submitted'}
    第二次：ok=False  error=submit_order 不可重放，幂等键 k2 已用过，拒绝重复执行
    实际执行次数：1（第二次被拒绝执行）
  submit_order 不给幂等键：ok=False  error=submit_order 是不可重放的副作用工具，必须提供幂等键
```

注意第二段：`submit_order` 的第一次执行**成功**了，但第二次仍然被拒绝。这就是不可逆动作的规矩——宁可让调用方去查一次状态，也不替它假设"上次成功了"。

### 2.6 并行调用的结果对齐与失败隔离

模型一轮可能返回多个工具调用。执行完之后有两条硬要求：

- **对齐**：`results[i]` 永远对应 `calls[i]`，不能靠"按名字猜"；
- **失败隔离**：一个失败不影响其他。模型想一次读三个文件，不能因为第一个不存在，后两个就不读了。

`tool_runner.invoke_many` 把这两条做成契约：

```python
    if not parallel:
        return [run(i, name, args) for i, (name, args) in enumerate(items)]

    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        futures = [pool.submit(run, i, name, args) for i, (name, args) in enumerate(items)]
        return [future.result() for future in futures]
```

注意并发的那支：**提交顺序**决定了返回顺序，即使线程完成顺序不同，返回的列表仍然按调用顺序排。顺序是对齐契约的一部分，不取决于用不用线程。

实跑：

```text
  发起 4 个调用，返回 4 个结果
    [0] read_text_file → 失败：FileNotFoundError: 文件不存在：不存在-a.txt
    [1] add → 成功：3
    [2] read_text_file → 失败：FileNotFoundError: 文件不存在：不存在-b.txt
    [3] add → 成功：30
  成功 2 个，失败 2 个——失败没有影响其他调用
  结果与调用一一对齐：True
```

## 三、为什么这样设计：取舍与失效条件

### 3.1 什么时候**不该**用工具层

- **只有一个工具，而且逻辑很简单。** 直接写代码调用更清楚——第一卷第 8 章说过：只有一个工具、逻辑又简单时，工具调用协议不如直接调用。工具层是为"工具多、且要交给模型选"准备的。
- **工具不是给模型选的。** 如果调用顺序固定、条件确定，那是工作流，写代码更省事（回到第 1 章的伪需求判据）。
- **纯本地的、一次性的脚本。** 直接调函数。

工具层的收益来自"**让模型自己决定调什么**"。没有这件事，这层抽象就是纯开销。

### 3.2 工具层的五种失效方式

| 失效方式 | 症状 | 后果 |
| --- | --- | --- |
| 描述写坏 | 模型调错工具、传错参数 | 猜错的活儿推给了模型 |
| 回灌不分形态 | 模型对着环境错误改参数 | 白烧 token，修不好 |
| 幂等键没带 | 不可逆工具重复执行 | 重复下单、重复扣款 |
| 校验与实现漂移 | schema 说要整数，实现收字符串 | 模型按 schema 调用却失败 |
| 失败被当成成功 | 上层不检查 `result.ok` | 工具没干成，报告说干成了 |

最后一条和第 3 章那个"不检查 `finish_reason`"是同一个毛病：**工具层努力把失败表达清楚，上层不看就白做**。

### 3.3 显式 schema 的代价

别只说好处。显式写 JSON Schema 的代价是实打实的：

- 字段一多，声明就长；
- 改函数签名要改两处，容易不同步；
- 嵌套结构写起来啰嗦。

所以本章提供了生成器作为补充。判断标准很简单：**字段少、描述和参数强相关 → 手写；字段多、描述能塞进 docstring → 生成**。生成之后**一定要人看一眼**——生成器不知道哪些参数是可选的语义、哪些是必填的业务条件。

## 四、动手：最小可实现

### 4.1 本章的工程产物

| 文件 | 作用 |
| --- | --- |
| `dragon_agent/tool_schema.py` | 从类型注解生成 JSON Schema（可选便利层） |
| `dragon_agent/tool_runner.py` | 批量调用的结果对齐与失败隔离；三种回灌形态的判定与渲染 |
| `examples/tool_layer_demo.py` | 实跑 `default_registry`，把六件事摊开给你看 |
| `tests/test_tool_layer_contract.py` | 契约测试：28 条断言 |

**没有改动 `dragon_agent/tools.py`**——它已经写好了，本章只读不改、只讲它的设计。

### 4.2 契约测试：把"不抛异常"变成断言

最有价值的一组断言，是"工具失败**必须**是结果而不是异常"：

```python
    def test_internal_exception_returns_failed_result(self):
        """工具内部抛异常，也要变成失败结果。"""
        reg = ToolRegistry()

        @reg.tool(name="boom", description="会炸",
                  parameters={"type": "object", "properties": {}})
        def boom():
            raise RuntimeError("炸了")

        result = reg.invoke("boom", {})
        self.assertFalse(result.ok)
        self.assertIn("RuntimeError", result.error or "")
        self.assertIn("炸了", result.error or "")
```

另一条守的是副作用底线——校验失败的工具函数**一次都不能被调用**：

```python
    def test_failed_validation_does_not_execute_the_tool(self):
        """校验失败的工具**不能真的执行**——这关系到副作用。"""
```

### 4.3 实跑

```powershell
cd dragon-agent
python examples/tool_layer_demo.py
python -m unittest tests.test_tool_layer_contract
```

演示的完整输出在上面各节已经贴了片段；最后一行是自检结论：

```text
==========================================================================
自检结果：全部通过
```

测试：

```text
----------------------------------------------------------------------
Ran 28 tests in 0.006s

OK
```

退出码 0。全部离线——不联网、不需要 API Key、不写用户目录。

### 4.4 骨架怎么被上下游调用

| 骨架里的东西 | 谁来用 | 怎么用 |
| --- | --- | --- |
| `ToolRegistry.schemas()` | 编排层、模型层 | 喂给 `llm.complete(tools=...)` |
| `ToolResult.ok` | 编排层 | **必须检查**：不检查就等于把失败当成功 |
| `ToolResult.error` | 编排层 | 回灌给模型，让它自己纠正 |
| `ToolResult.idempotent_hit` | 编排层 | 判断这次是不是命中了幂等缓存 |
| `side_effect` / `replayable` | 护栏、审批流程 | 决定这个工具要不要人工确认 |
| `tool_runner.invoke_many()` | 编排层 | 一轮多个调用：结果对齐、失败隔离 |
| `tool_runner.render_feedback()` | 编排层 | 把失败渲染成带形态标注的回灌文本 |
| `tool_schema.schema_for()` | 工具作者 | 字段多时省事，生成后要人看一遍 |

## 五、开源实现对照

### Pydantic

- 仓库：<https://github.com/pydantic/pydantic>
- 许可证：MIT

Python 里最流行的数据校验库，用类型注解声明结构，自动做校验与序列化。**差异**：Pydantic 的错误信息面向**开发者**（"Field required"、"Input should be a valid integer"）；本工程的校验错误面向**模型**——它要说清"哪个参数错了、期望什么"，好让模型改对。同一个校验，两套受众，这是本章最值得记住的一条差异。

### jsonschema

- 仓库：<https://github.com/python-jsonschema/jsonschema>
- 许可证：MIT

JSON Schema 的参考实现，完整支持 draft 2020-12。**差异**：`tools.py` 只实现了类型、必填、枚举三类检查，是刻意的取舍——完整的 JSON Schema 校验器是几百行的依赖，而模型幻觉出来的参数，绝大多数落在这三类里。想上完整校验器时，`jsonschema` 是现成的。

### MCP Python SDK

- 仓库：<https://github.com/modelcontextprotocol/python-sdk>
- 许可证：MIT

模型上下文协议（Model Context Protocol，MCP）的官方 Python SDK，把工具、资源、提示词标准化，跨进程暴露。**差异**：MCP 解决的是"**跨进程、跨厂商**地暴露工具"，本工程的工具层解决的是"**进程内**怎么把工具安全地交给模型"。第 11 章会把本工程的工具注册表直接暴露成 MCP server——那时候两套就接上了。

### instructor

- 仓库：<https://github.com/567-labs/instructor>
- 许可证：MIT

用 Pydantic 模型声明期望的输出结构，自动重试并修正模型返回。**差异**：instructor 管的是**模型的输出**（模型该返回什么结构），本工程的工具层管的是**模型的输入**（模型该传什么参数）。两者互补：instructor 修输出，工具层修输入。

### OpenAI Function Calling 指南

- <https://platform.openai.com/docs/guides/function-calling>

函数调用的官方用法说明：工具声明怎么写、参数怎么传、多工具并行怎么返回。**差异**：这是**接口规范**不是库，规范说明的是"请求长什么样"，而工具层要回答的是"失败怎么表达、副作用怎么管"。规范不会告诉你幂等键该怎么用。

## 六、坑与检查清单

### 注册与描述

- [ ] 每个工具都有名字、描述、参数 schema 三样
- [ ] 描述写给模型看：说清**什么时候用**、**参数什么格式**
- [ ] 有副作用的工具，在描述里就写明后果
- [ ] 生成 schema 之后人看一遍，别直接信生成器

### 校验与回灌

- [ ] 校验失败**不抛异常**，返回 `ToolResult(ok=False)`
- [ ] 工具内部异常也**不抛异常**，同样变成失败结果
- [ ] 校验失败的调用**没有真的执行**工具函数
- [ ] 回灌文本分得清字段级 / 语义级 / 环境级
- [ ] 上层**检查** `result.ok`，不检查等于白做

### 副作用与幂等

- [ ] 每个工具都标了 `side_effect` 与 `replayable`
- [ ] 不可逆写的工具：没有幂等键就拒绝执行
- [ ] 不可逆写的工具：幂等键用过之后拒绝执行，不返回上次的结果
- [ ] 可重放的工具：幂等命中要标 `idempotent_hit`，让调用方能区分
- [ ] 不可逆动作接人工确认（护栏层，第 9 章）

### 并行调用

- [ ] 结果按调用顺序一一对应
- [ ] 一个调用失败不影响其他调用
- [ ] 批次里的每个副作用调用有自己的幂等键

### 常见坑

- [ ] **把工具失败当程序错误**：抛异常，模型看不到纠正机会
- [ ] **工具描述写一半**：模型只能靠猜
- [ ] **不可逆工具返回上次的结果**：骗调用方说"成功了"，实际状态未知
- [ ] **忘了 `bool` 不是 `integer`**：模型传 `true` 被当成 `1` 放过去
- [ ] **schema 与函数签名漂移**：模型按 schema 调用却报"参数不匹配"
- [ ] **多余参数在校验层放行**：模型幻觉出的参数一路带到执行层才炸

## 七、小结与延伸阅读

这一章把"给模型一个函数调用"变成一份真正的契约。要点回顾：

1. **工具描述就是给模型看的提示词。** 它和 schema 一起构成模型能看到的全部信息。
2. **失败是结果，不是异常。** 参数错、工具名错、文件不存在，全都回灌给模型让它自己纠正；校验失败的调用不执行工具函数。
3. **回灌要分形态。** 字段级让模型改参数，语义级让模型换策略，环境级让模型重试或换路——分不清就在烧 token。
4. **不可逆的工具不能自动重放。** 幂等键用过就拒绝执行，而不是替调用方编一个"上次成功了"的答案。
5. **并行调用的对齐与隔离是硬契约。** `results[i]` 对应 `calls[i]`，一个失败不影响其他。

下一章 `p05-context-assembler` 做上下文装配器：把提示词变成资源分配——工具 schema、任务、历史、检索结果各自占多少预算，超了怎么裁。

**延伸阅读**

- 第一卷第 8 章《工具调用契约：把 Function Calling 当作 API 工程》——工具描述即提示词、参数校验、错误回灌、幂等与副作用
- 第一卷第 9 章《代码即行动：Code-Act 与 Program-of-Thought》——什么时候干脆不写工具调用，直接让模型写代码
- 第一卷第 10 章《上下文工程：把上下文当作稀缺资源预算》——工具定义也占上下文预算
- 第一卷第 11 章《停止准则、步数上限与成本守卫》——工具错误率是重要的 SLI
- 第一卷第 32 章《提示注入与越权防护》——工具返回值也是不可信输入
- 第一卷第 39 章《协议与生态全景》——MCP、A2A、AG-UI 与 Skills
- Pydantic：<https://github.com/pydantic/pydantic>（MIT）
- jsonschema：<https://github.com/python-jsonschema/jsonschema>（MIT）
- MCP Python SDK：<https://github.com/modelcontextprotocol/python-sdk>（MIT）
- instructor：<https://github.com/567-labs/instructor>（MIT）
