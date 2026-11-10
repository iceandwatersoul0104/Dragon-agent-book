# 第 3 章 · 模型接入抽象：让 mock 与真实 API 共存

本卷第三章把"接模型"变成一层可以替换的薄壳：**一套接口，两种实现，同一种响应**。

- 正文：[`llm-client.md`](llm-client.md)
- 配套工程产物：`dragon-agent/` 下的四个新文件（见下）

## 本章给了什么

1. **`dragon_agent/llm_policy.py`**——失败分类（限流 / 5xx / 超时 / 传输 / 响应不对）、指数退避加抖动、请求指纹与幂等去重。
2. **`dragon_agent/pricing.py`**——计价：缓存命中与未命中分开算，单价表当参数传进来，不写死。
3. **`examples/llm_client_demo.py`**——可运行演示：剧本化 mock、确定性回放、重试退避、成本核算、契约自检。
4. **`tests/test_llm_client_contract.py`**——37 条契约断言，含一条用 AST 扫源码的架构断言。

**没有改动 `llm.py`**——它已经写好了，本章只读不改、只讲它的设计。也没有改 `config.py`、`tools.py`。

## 核心约束

> 任何业务模块都**不允许**用 `isinstance` 判断客户端类型——那会把 mock 与真实的行为差异泄漏到控制流里，测试就测不出线上问题。

这条约束由 `test_llm_client_contract.py` 里的一条 AST 扫描断言守着：

```python
    def test_no_isinstance_on_client_types(self):
        """扫全包源码：不允许出现 isinstance(..., MockLLM/OpenAICompatClient)。"""
```

## 怎么跑

```powershell
cd dragon-agent

# 1) 演示：离线跑通，退出码 0
python examples/llm_client_demo.py

# 2) 契约测试：37 个用例
python -m unittest tests.test_llm_client_contract
```

不需要 API Key，不联网，不装任何第三方包。连"网络失败"那条用例都是打桩 `urlopen` 造出来的，不真的建连接。

### 实跑结果

| 场景 | 结果 | 退出码 |
| --- | --- | --- |
| `python examples/llm_client_demo.py` | 剧本化 / 回放一致 / 四类失败处理 / 成本核算 / 契约自检全通过 | 0 |
| `python -m unittest tests.test_llm_client_contract` | `Ran 37 tests` / `OK` | 0 |

演示里的退避序列是 `0.2s → 0.4s`（指数退避）；成本示例 `0.114000` 美元（有缓存）对 `0.330000` 美元（无缓存），用的是演示里写死的**示例单价**，不是任何真实模型的价格。

## 一张表记住本章

| 坑 | 后果 | 守它的约定 |
| --- | --- | --- |
| `if isinstance(client, MockLLM)` | 没被测过的分支进了生产 | AST 扫描断言 |
| 不检查 `finish_reason` | 错误响应当成答案返回 | 降级约定 + 契约测试 |
| mock 的 `usage` 留默认值 0 | 成本断言全绿，预算守卫从不报警 | 剧本写死真实量级数字 |
| mock 只返回合法响应 | 畸形输出永远打不到你 | mock 必须能造错误响应 |
| 状态码被拍扁成字符串 | 判断逻辑散落多处 | 分类集中在 `classify_failure` |
| 重试没有上限 | 限流时把额度烧光 | `RetryPolicy.max_attempts` |
| 缓存命中当并列项算 | 重复计费 | `TokenCounts.uncached_input_tokens` |

## 局限（这一章不等于可以上线）

- **请求参数只有一个真正生效。** `complete()` 的 `**kwargs` 只被读了一次（`llm.py:155`），`max_tokens`、`stop`、`response_format`、`top_p` 传进去不报错也不生效——输出长度在这一层没有上界。
- **失败信息在进策略层之前就丢了。** HTTPError 只取状态码（`llm.py:173-176`），响应体和响应头一次都没读，拿不到供应商的错误类型与说明，`Retry-After` 也无从读起；而 `classify_failure()` 又只在 `"[llm] HTTP nnn"` 这句文本里找状态码（`llm_policy.py:78-94`）——这是作者自己在模块 docstring 里承认的契约缺口（`:9-13`），`LLMResponse` 一旦改了这句文案，分类会静默失灵。
- **缓存计价的分支在真实链路里是死代码。** `_parse()` 只取 `prompt_tokens` / `completion_tokens`（`llm.py:195-199`），`cached_input_tokens` 得由调用方显式补（`pricing.py:74-85`），而生产路径从不补——缓存命中的 token 全部按输入原价计费。
- **请求去重默认是关的，打开了指纹也不全。** `dedupe` 默认 `False`（`llm_policy.py:161`）；开启后指纹只算 messages 与 tools（`:132-144`），不含 temperature——温度不同的两次请求会算出同一个指纹。
- **重试期间已经花掉的 token 查不到。** 失败尝试只留 200 字符的文本（`llm_policy.py:188-192`），交给调用方的只有最后一条响应——本地账不重复记，但上游是否计了两次无从核对。
- **流式与熔断都不在本章交付物里。** `LLMClient` 协议只有 `complete()` 和 `describe()`（`llm.py:72-83`），响应一次性读完；退避重试之外没有熔断与降级模型——这两样要接工程线的 CLI，不在本章契约内。

## 与本书其它章节的关系

- 第二卷第 4 章 `p04-tool-layer`：工具契约、参数校验与错误回灌。
- 第二卷第 5 章 `p05-context-assembler`：`usage` 换成上下文预算的单位。
- 第二卷第 6 章 `p06-agent-loop`：预算守卫用 token 账触发停止。
- 第二卷第 12 章 `p12-observability`：成本账与 `describe()` 写进 trace。
- 第一卷第 3、8、10、33、35 章：可靠性天花板、工具契约、上下文预算、可观测性、成本优化。
