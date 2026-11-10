# 第 3 章 模型接入抽象：让 mock 与真实 API 共存

## 一、本章要解决的问题

团队里的 Agent 用 mock 跑了一整周，一切正常。第三周接上真实 API，同样的代码路径开始出怪问题。

第一个问题是**成本账全是零**。开发时用 mock，`usage` 字段没人填，预算守卫每轮都显示"还很宽裕"；接上真实 API 才发现，有些兼容接口根本不返回 `usage`，token 数就是 0。预算守卫于是变成一个永远不响的警报——它测过、它通过了、它没在起作用。

第二个问题更隐蔽。有位同事为了让 mock "更像真的"，在业务代码里写了这么一行：

```python
if isinstance(client, MockLLM):
    args = json.loads(call.arguments)
else:
    args = call.arguments
```

（反面教材——这段代码不该出现在任何模块里。）

理由听着很合理：真实 API 返回的 `arguments` 是 JSON 字符串，mock 返回的是字典。这行代码让测试跑通了。

上线第二天，这条分支走的是另一半，而另一半**从来没有被测过**。

问题的根子不是 mock 不够真，是**mock 和真实返回的东西不是同一种东西**。一旦两者形状不同，业务代码迟早要写"如果是 A 就这样，否则那样"，而那条"否则"分支就是没人测过的分支。

本章要解决的就是这一件事：**一套接口，两种实现，同一种响应**。让"用 mock 开发"和"用真实 API 运行"变成同一件事的两种数据来源，而不是两件需要分别适配的事。

## 二、原理：机制拆解

### 2.1 请求与响应：三个数据对象

模型接入层要传的东西其实很少：**一组消息、一份工具声明、一把采样参数**。响应也只有四样：**文本、要调的工具、用了多少 token、为什么停**。

`llm.py` 把它们收敛成三个对象：

```python
@dataclass
class Usage:
    """token 用量。``total`` 是属性而不是字段，避免两个地方各算一遍算出不同值。"""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens
```

```python
@dataclass
class LLMResponse:
    """一次模型调用的结果。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)
```

三个决定值得单独说：

**`total` 是属性，不是字段。** 如果 `total` 是字段，谁忘了填它，账就少算一截。做成属性之后，`Usage` 不可能被算错——这也是第 2 章说的"配置即事实"同一个思路：把可推导的东西从"需要维护的状态"里拿掉。

**`finish_reason` 承载成功与失败。** 这是全章最关键的一个字段。模型调用失败时，`llm.py` 不抛异常，而是返回一条 `finish_reason="error"` 的响应。**上层必须检查这个字段**——不检查，就等于把失败当成功用了。这是"运行期的可预期失败用返回值表达"（第 2 章）在模型层的具体样子。

**请求侧刻意用朴素 dict。** `messages` 是 `list[dict]`，`tools` 是 `list[dict]`，不是自定义类。理由是这一层要能原样转成 JSON 发给任何兼容接口；一旦定义了请求类，就得写"怎么把它变成供应商要的形状"的转换层，而那一层的复杂度会直接吃掉这层抽象的收益。请求侧用 dict 换来的是"换个供应商只改客户端、不改请求"。

### 2.2 协议：唯一契约

`llm.py` 用 `Protocol` 定义接口，不要求继承：

```python
@runtime_checkable
class LLMClient(Protocol):
    """模型客户端协议。``complete`` 是唯一的调用入口。"""

    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None = None,
                 **kwargs: Any) -> LLMResponse:
        ...

    def describe(self) -> str:
        """可读的模型标识，写进 trace 用。"""
        ...
```

协议只有两个方法：`complete`（调一次）和 `describe`（报一下自己是谁，写进 trace 用）。**`describe()` 的存在是个容易被忽略的好设计**——trace 里如果只写"某次 LLM 调用"，你永远不知道当时连的是 mock 还是真模型、哪个模型、哪个地址。

关键约束写在 `llm.py` 的 docstring 里：

> 任何业务模块都**不允许**用 `isinstance` 判断客户端类型——那会把 mock 与真实的行为差异泄漏到控制流里，测试就测不出线上问题。

这条约束要成立，靠的不是自觉，是**形状必须一致**：

- mock 不能返回真实不存在的字段；
- 真实不能返回 mock 没有的字段；
- mock 不能比真实更宽容。

第三条最容易被违反。真实模型会返回空字符串的工具名、返回非 JSON 的 arguments、返回少字段的 usage；mock 如果只写"正常情况"，那么这些边界永远没被测过。本章的契约测试里有一条专门扫源码，断言全包里不出现 `isinstance(..., MockLLM)` 这样的分支。

### 2.3 剧本化 mock 与确定性回放

`MockLLM` 的机制很简单：**按剧本返回，用完循环最后一条**。

```python
    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None = None,
                 **kwargs: Any) -> LLMResponse:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self.script:
            return LLMResponse(text="", usage=Usage(input_tokens=0, output_tokens=0))
        idx = min(self._cursor, len(self.script) - 1)
        self._cursor += 1
        return self.script[idx]
```

"用完循环最后一条"这个设计是有讲究的：写测试时你不必关心这个任务到底调了几次模型，多调一次不会让测试莫名其妙挂掉。代价是——**如果你忘了断言调用次数，测试可能悄悄多跑了几轮**。所以 `MockLLM` 把每次调用都记下来：

```python
        self.calls.append({"messages": list(messages), "tools": tools})
```

`calls` 让你能断言"到底发了几次、带了什么参数"。测试断言要写期望值，不能只写"跑完不崩就算过"——这是本工程测试的硬约定。

**确定性回放**要成立，需要三个条件同时满足：

1. 同样的输入得到同样的输出——所以剧本写死，不靠随机；
2. 没有隐含的时间、随机数、并发——`complete` 里不读时钟；
3. **`usage` 要写死真实数字**，不能默认 0。

第三条是第 2 章那个"成本账全是零"事故的根因。mock 返回 `Usage()` 默认零值，测试里所有成本断言都通过，而线上一个 token 都没记上。**mock 的 usage 必须是写死的、和真实量级相当的数字**，否则预算守卫在测试里永远是绿的。

### 2.4 离线开发不成为自欺欺人：四条守则

把上面这些收拢成四条可以直接贴在墙上的规矩：

| 守则 | 违反后果 |
| --- | --- |
| mock 的 `usage` 写死真实量级的数字 | 成本断言永远通过，预算守卫形同虚设 |
| mock 必须能返回 `finish_reason="error"` 的响应 | 错误路径一次都没被测过 |
| mock 必须能返回"不太合法"的响应 | 真实模型的畸形输出永远打不到你 |
| 定期把真实响应录成剧本（record/replay） | mock 离真实越来越远 |

第四条是关键动作：拿一批真实任务跑一遍，把响应原样录成剧本，下次开发就拿这份剧本。录制是最便宜的"真实与 mock 对账"。

### 2.5 真实 API 的错误处理：降级不等于放弃

`llm.py` 的真实客户端有一个明确约定：**网络层任何异常都不该冒泡成任务失败**。

```python
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return LLMResponse(text=f"[llm] HTTP {e.code}", finish_reason="error")
        except Exception as e:  # 网络层任何异常都不该冒泡成任务失败
            return LLMResponse(text=f"[llm] {type(e).__name__}: {e}", finish_reason="error")
```

这个约定是对的：一次模型故障不该让整个任务崩掉。但**降级和放弃之间还差几步**：

- **限流（429）**：等一下就好，退避后重试；
- **服务端错误（5xx）**：大概率是临时的，重试；
- **超时**：可能请求已经到达服务端了，重试要小心重复计费；
- **参数错（400）**：重试多少次都一样，不重试；
- **响应不对（没有 choices）**：重试也没用。

所以要有一个**重试策略**，按失败类别决定要不要重试。退避是指数的，还要加抖动：

```python
    def delay_for(self, attempt: int, jitter: float = 0.0) -> float:
        """第 ``attempt`` 次失败之后（从 1 起算）该等多久。

        ``jitter`` 取值在 ``[-1, 1]``，用来加噪。测试注入固定的 jitter，
        结果就完全确定。
        """
        raw = self.base_delay_s * (2 ** max(0, attempt - 1))
        raw = min(raw, self.max_delay_s)
        if self.jitter_ratio:
            raw += raw * self.jitter_ratio * max(-1.0, min(1.0, jitter))
        return max(0.0, raw)
```

抖动不是为了好看：没有抖动，一批并发请求会整齐地一起超时、一起重试、一起再失败。抖动把尖峰摊平。

**幂等**：重试可能让同一个请求被计费两次。所以要有**请求指纹**——同样的输入算出同样的指纹，**在请求层**判断"这是同一笔"。`llm_policy.py` 里的 `request_fingerprint()` 算出指纹，而带 `dedupe=True` 的客户端会拿它查缓存，同指纹只发一次。注意两件事：**一是 `dedupe` 默认是关的**（`False`），不开就没有去重；二是**成本账本身不按指纹去重**——`CostMeter.charge()` 只按 `usage` 累加，防重复计费靠的是请求层不重发，而不是账本层做账时辨认。

**这里有一个契约的缺口，值得单独说**：上面那段降级代码把 HTTP 状态码拍扁成了字符串（`"[llm] HTTP 429"`）。于是上层要判断"是不是限流"，只能去匹配字符串。正确的做法是让 `LLMResponse` 带一个 `status_code` 字段——但那是契约改动，按规矩要先改 `_plan/_dragon_contract.md`。在改之前，模式匹配集中在 `classify_failure` 一处：判断逻辑只写一遍，将来改契约时也只改这一处。

### 2.6 用量与成本核算

成本 = token 数 × 单价。麻烦的是**缓存命中**：命中缓存的输入 token 通常便宜得多，而且它**是输入 token 的子集**，不是并列的一项。

```text
未命中输入 = input_tokens - cached_input_tokens      按输入单价算
命中输入   = cached_input_tokens                    按缓存单价算
输出       = output_tokens                          按输出单价算
```

写成代码：

```python
    def cost(self, counts: TokenCounts) -> float:
        """这一次调用的成本（美元）。"""
        return (
            counts.uncached_input_tokens * self.input_per_mtok
            + counts.cached_input_tokens * self.effective_cached_price
            + counts.output_tokens * self.output_per_mtok
        ) / PER_MTOK
```

两条纪律：

1. **单价表从配置进来，不写死在代码里。** 模型价格几个月一变，写死就是埋雷。本章的 `pricing.py` 只负责算法，单价是参数。
2. **缓存命中数要能拿到。** 真实 API 会返回 `usage.prompt_tokens_details.cached_tokens`，但 `llm.py` 的 `_parse` 目前只取了 `prompt_tokens` 和 `completion_tokens`，缓存 token 被丢掉了。要把缓存命中真正纳入成本账，得先给 `Usage` 加字段——又是契约改动。`pricing.py` 先把算法备好，那次扩展只需要加字段。

## 三、为什么这样设计：取舍与失效条件

### 3.1 什么时候**不该**做这层抽象

- **一次性脚本，只调一次模型。** 直接 `urllib` 请求写完就删。为了一个请求写三个类，是过度工程化。
- **永远不会接真实 API。** 那 mock 就够了，不需要"统一两种实现"——但**仍然值得一个统一接口**，否则将来换实现要改一片代码。
- **只用一家供应商且不打算换。** 这层抽象的收益是"以后能换"，成本是"现在要多写一层"。如果"以后"很遥远，可以先不抽象，但**请求和响应的形状要一开始就定下来**——形状比接口更值钱。

真正该做这层抽象的信号有两个：**你会同时用 mock 和真实 API**，以及**你已经开始因为它们形状不同而写分支**。

### 3.2 这层抽象的四种失效方式

| 失效方式 | 症状 | 后果 |
| --- | --- | --- |
| 抽象泄漏 | 业务代码出现 `isinstance(client, MockLLM)` | 没被测过的分支进了生产 |
| mock 骗人 | mock 的 usage 全是 0、只返回合法响应 | 成本与错误路径从来没被测过 |
| 降级被当成成功 | 上层不检查 `finish_reason` | 错误响应当成答案返回给用户 |
| 重试放大成本 | 重试成功但服务端计了两次费 | 成本账高于预期，预算提前告警 |

第三种最隐蔽。`llm.py` 之所以把失败表达成 `finish_reason="error"` 而不是抛异常，就是为了上层能**统一地**检查它——但如果上层不检查，这个设计就白费了，反而制造了"看起来成功了"的机会。

### 3.3 统一抽象的代价

- **多一层间接**：想知道"这次调用发了什么"，要读协议再读实现。
- **协议字段要取舍**：`messages` 用裸 dict 是妥协——换来的是换供应商不用改请求，代价是失去类型检查。
- **协议会过期**：供应商加了新参数（响应格式、缓存控制、推理预算），协议要跟着加。

这些代价换来的是一件事：**你可以在完全离线的情况下，把 Agent 的每一个分支都跑一遍。** 如果你的 Agent 需要联网才能测，大部分分支就永远测不到。

## 四、动手：最小可实现

### 4.1 本章的工程产物

| 文件 | 作用 |
| --- | --- |
| `dragon_agent/pricing.py` | 计价：缓存命中与未命中分开算，单价表当参数 |
| `dragon_agent/llm_policy.py` | 失败分类、重试退避、请求指纹与幂等去重 |
| `examples/llm_client_demo.py` | 可运行演示：剧本化、确定性回放、重试退避、成本核算、契约自检 |
| `tests/test_llm_client_contract.py` | 契约测试：37 条断言 |

**没有改动 `llm.py`**——它已经写好了，本章只读不改、只讲它的设计。也没有改 `config.py`、`tools.py`。

### 4.2 两个实现，一个工厂

`llm.py` 的两个实现由一个工厂按配置选：

```python
def build_client(config) -> LLMClient:
    """按配置选客户端。``--live`` 走真实 API，否则走 mock。"""
    if getattr(config, "live", False):
        return OpenAICompatClient(
            model=config.model,
            api_key=config.api_key or "",
            base_url=config.base_url,
            timeout_s=config.timeout_s,
            temperature=config.temperature,
        )
    return MockLLM(name=config.model)
```

这是整层抽象里**唯一**允许出现的"按实现分支"的地方，而且它分的是**构造**，不是**行为**。业务代码拿到的永远是一个 `LLMClient`。

### 4.3 失败分类与重试

`llm_policy.py` 把"这条失败该不该重试"收敛到一个函数：

```python
def classify_failure(resp: LLMResponse) -> str:
    """把一条响应判成失败类别。

    ``finish_reason`` 不是 ``"error"`` 就直接算成功——这条判断和
    ``llm.py`` 的降级约定严格对齐。
    """
    if resp.finish_reason != "error":
        return FAILURE_NONE

    text = resp.text or ""
    m = _HTTP_RE.search(text)
    if m:
        code = int(m.group(1))
        if code == 429:
            return FAILURE_RATE_LIMITED
        if code == 408:
            return FAILURE_TIMEOUT
        if 500 <= code < 600:
            return FAILURE_SERVER_ERROR
        return FAILURE_BAD_RESPONSE
```

包装器 `ResilientLLMClient` 的语义是**重试透明**：重试次数用尽后，把最后那条错误响应原样返回。调用方看到的东西和不用重试时完全一样——它不需要知道重试发生过。

### 4.4 契约测试：把"同一种响应"变成断言

最有意思的一条测试，是用 AST 扫源码，断言全包里不出现按客户端类型分支的代码：

```python
    def test_no_isinstance_on_client_types(self):
        """扫全包源码：不允许出现 isinstance(..., MockLLM/OpenAICompatClient)。"""
        package = ROOT / "dragon_agent"
        offenders: list[str] = []
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Name) and func.id == "isinstance"):
                    continue
                for arg in node.args[1:]:
                    name = self._dotted_name(arg)
                    if name and name.split(".")[-1] in {"MockLLM", "OpenAICompatClient"}:
                        offenders.append(f"{path.name}:{node.lineno} {name}")
        self.assertEqual(offenders, [], "业务代码按客户端类型分支，mock 就测不出线上问题")
```

把一条架构约束写成一条可执行的断言，比写在文档里等 reviewers 记得看要可靠得多。

### 4.5 实跑

```powershell
cd dragon-agent
python examples/llm_client_demo.py
python -m unittest tests.test_llm_client_contract
```

演示的实际输出（节选）：

```text
1) 脚本化 mock：剧本用完后循环最后一条
  第 1 次：finish_reason=tool_calls  usage={'input_tokens': 128, 'output_tokens': 21, 'total': 149}
  第 2 次：finish_reason=stop  usage={'input_tokens': 196, 'output_tokens': 34, 'total': 230}
  第 3 次：finish_reason=stop  usage={'input_tokens': 196, 'output_tokens': 34, 'total': 230}

2) 确定性回放：同一份剧本跑两遍，结果逐字相同
  两次结果相同：True

3) 重试与退避：只有该重试的才重试
  限流 HTTP 429
    尝试 3 次，等待：0.2s, 0.4s
    最终失败类别：none
  服务端错误 HTTP 503
    尝试 2 次，等待：0.2s
    最终失败类别：none
  响应不对（没有 choices）
    尝试 1 次，等待：（没等）
    最终失败类别：bad_response
  超时（重试到用尽）
    尝试 3 次，等待：0.2s, 0.4s
    最终失败类别：timeout

4) 成本核算：缓存命中与未命中分开算
  输入 100000 token（其中 80000 命中缓存），输出 2000 token
  有缓存：0.114000 美元
  无缓存：0.330000 美元
  省下  ：0.216000 美元

5) 契约自检：mock 与真实形状必须是同一种响应
  类型相同：True（真实=LLMResponse，mock=LLMResponse）
  字段集合相同：True
  字段：['finish_reason', 'text', 'tool_calls', 'usage']
```

退避序列是 `0.2s → 0.4s`，正好是指数退避。成本那三行数字来自演示里写死的**示例单价**（输入 3.0、输出 15.0、缓存 0.3 美元/百万 token），不是任何真实模型的价格。

测试：

```text
----------------------------------------------------------------------
Ran 37 tests in 0.085s

OK
```

退出码 0。全部离线——连"网络失败"那条用例都是打桩 `urlopen` 造出来的，不真的建连接。（用例数固定是 37，耗时随机器而变。）

### 4.6 骨架怎么被上下游调用

| 骨架里的东西 | 谁来用 | 怎么用 |
| --- | --- | --- |
| `LLMClient` 协议 | 编排层、工具层 | 只认协议，不认实现 |
| `LLMResponse.finish_reason` | 编排层 | **必须检查**：不检查就等于把失败当成功 |
| `LLMResponse.usage` | 编排层、成本账 | 累加进预算；mock 也要写死真实数字 |
| `ResilientLLMClient` | 应用层 | 包装任意客户端，得到重试与退避 |
| `pricing.TokenCounts` / `PriceTable` | 成本账 | 把 token 账换算成钱，缓存命中分开算 |
| `request_fingerprint()` | 请求层去重 | 算出请求指纹；`ResilientLLMClient(dedupe=True)` 用它查缓存，同指纹只发一次。**默认是关的** |

## 五、开源实现对照

### OpenAI Python SDK

- 仓库：<https://github.com/openai/openai-python>
- 许可证：Apache-2.0

官方 Python 客户端，类型由 Pydantic 模型定义，响应对象带全套字段。**差异**：它要求装 Pydantic 和 httpx，本工程的契约要求纯标准库；而且它返回的是**它自己那套**响应类型——业务模块一旦直接用它的字段，就绑死在这一家上了。本工程的 `LLMResponse` 是自研的四个字段，换供应商时改动最小。

### Anthropic Python SDK

- 仓库：<https://github.com/anthropics/anthropic-sdk-python>
- 许可证：Apache-2.0

另一家官方客户端，同样是 Pydantic + httpx，并且内置了 OpenTelemetry 埋点。**差异**：值得抄的是它的**分块流式**接口设计——把"边生成边返回"表达成一个迭代器，而不是一个回调。本工程的 `LLMResponse` 目前是一次性返回，加流式是契约扩展；第 6 章讲预算守卫时，"流式"和"预算"会正面相遇。

### httpx

- 仓库：<https://github.com/encode/httpx>
- 许可证：BSD-3-Clause

新一代 HTTP 客户端，连接池、超时、重试、HTTP/2 都是现成的。**差异**：`llm.py` 用标准库 `urllib` 发请求，能跑但没有连接池——每次调用建一次连接，高频调用下延迟明显。要换成 httpx 只需改 `OpenAICompatClient` 一个类，因为协议不变。这正是这层抽象的兑现方式。

### tenacity

- 仓库：<https://github.com/jd/tenacity>
- 许可证：Apache-2.0

重试策略库：重试次数、退避、只对特定异常重试、失败回调、抖动全都现成。**差异**：本工程手写了 `RetryPolicy`，因为它重试的对象是**响应**（`finish_reason="error"`）而不是异常，tenacity 那套基于异常重试的模型套不上。等重试策略再复杂些（不同错误不同退避、熔断），再引它不迟。

### LiteLLM

- 仓库：<https://github.com/BerriAI/litellm>
- 许可证：MIT，但**仅适用于 `enterprise/` 目录之外的部分**——根目录 LICENSE 原文写明，落在 `enterprise/` 目录下的内容另由 `enterprise/LICENSE` 授权。

把几十家模型供应商统一成一套调用接口，还带路由、预算和成本跟踪。**差异**：LiteLLM 走的是"把供应商差异全塞进一个大库"的路子，本工程走的是"只依赖 OpenAI 兼容接口 + 自己定义最小响应对象"的路子。LiteLLM 的代价是依赖树巨大（它引了 openai、anthropic、tiktoken 等一长串）；本工程的代价是**只支持 OpenAI 兼容的那一类接口**。选哪条路，看你的供应商是不是都在这一类里。

## 六、坑与检查清单

### 接口与形状

- [ ] mock 与真实返回**同一个类**、**同一组字段**，有断言守着
- [ ] 业务代码里不出现 `isinstance(client, MockLLM)` 这类分支
- [ ] 响应里有 `finish_reason`，上层**真的检查**它
- [ ] 请求侧用朴素 dict，不引入只为了"看起来规范"的自定义请求类
- [ ] 协议只有真正需要的接口，不要顺手加"以后可能用"的

### mock

- [ ] `usage` 写死真实量级的数字，不是默认值 0
- [ ] mock 能返回 `finish_reason="error"` 的响应
- [ ] mock 能返回"不太合法"的响应（空工具名、非 JSON 参数）
- [ ] 剧本用完后循环最后一条，且测试用 `calls` 断言调用次数
- [ ] 定期把真实响应录成剧本，核对 mock 与真实的差距

### 真实调用

- [ ] 网络层异常降级成错误响应，不冒泡成任务失败
- [ ] 限流、5xx、超时会退避重试；参数错、响应不对不重试
- [ ] 退避有上限、有抖动；测试注入 `sleep` 与 `jitter`，不真的等
- [ ] 重试次数用尽后原样返回最后一条错误响应，调用方无感
- [ ] 请求指纹稳定；开了 `dedupe` 才在请求层挡住重复发送（成本账本身不按指纹去重）

### 成本

- [ ] 单价从配置进来，不写死在代码里
- [ ] 缓存命中数是输入 token 的**子集**，算钱时先减出来
- [ ] 明细三项加起来等于总额
- [ ] mock 模式下单价为 0，测试不产生真实成本

### 常见坑

- [ ] **用 `if mock` 绕开问题**：绕过的是症状，代价是没测过的分支
- [ ] **不检查 `finish_reason`**：错误响应当成答案返回，用户看到一段胡话
- [ ] **mock 的 usage 留默认值**：成本断言全绿，预算守卫从不报警
- [ ] **把状态码拍扁进字符串还在别处重复匹配**：判断逻辑散落多处，改契约时漏改
- [ ] **重试没有上限**：限流时无限重试，把额度烧光
- [ ] **每次调用重建 HTTP 连接**：延迟和成本都上去了，还以为是自己代码慢

## 七、小结与延伸阅读

这一章把"接模型"这件事变成一层可以替换的薄壳。要点回顾：

1. **同一种响应是这层抽象的全部意义。** mock 与真实返回同一个类、同一组字段，业务代码就不需要分支；一旦出现 `isinstance` 判断，说明抽象已经漏了。
2. **`finish_reason` 是失败的唯一出口。** 上层必须检查它，不检查等于把失败当成功。
3. **mock 的价值取决于它有多不真实。** usage 要写死、错误响应要能返回、畸形输出要能造出来；再加一条：定期录真实响应做对账。
4. **降级不等于放弃。** 失败要分类，限流与 5xx 该退避重试，参数错不该重试；退避要有上限和抖动。
5. **缓存命中是输入的子集，不是并列项。** 算错就会重复计费；单价表从配置来，别写死。

下一章 `p04-tool-layer` 做工具层：工具契约、参数校验、错误回灌与幂等。模型层解决"怎么问"，工具层解决"怎么做事"。

**延伸阅读**

- 第一卷第 3 章《为什么需要架构：可靠性、复利与工程约束》——可靠性天花板与过度工程化
- 第一卷第 8 章《工具调用契约：把 Function Calling 当作 API 工程》——工具描述即提示词、参数校验
- 第一卷第 10 章《上下文工程：把上下文当作稀缺资源预算》——token 预算与稳定前缀
- 第一卷第 33 章《可观测性：Trace、Span 与 Agent 专属指标》——`describe()` 写进 trace 的意义
- 第一卷第 35 章《成本与性能优化》——模型路由、级联策略、帕累托前沿
- OpenAI Python SDK：<https://github.com/openai/openai-python>（Apache-2.0）
- Anthropic Python SDK：<https://github.com/anthropics/anthropic-sdk-python>（Apache-2.0）
- httpx：<https://github.com/encode/httpx>（BSD-3-Clause）
- tenacity：<https://github.com/jd/tenacity>（Apache-2.0）
- LiteLLM：<https://github.com/BerriAI/litellm>（MIT；`enterprise/` 目录另计）
