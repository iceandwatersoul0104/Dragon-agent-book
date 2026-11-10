# dragon-agent

《开源 Agent 架构技术全书》第二卷《自建 Agent 实践手册》的配套工程。

一句话：**一个纯标准库实现的 Agent 框架，不需要 API Key 就能跑通完整回路。**

它不是玩具。第二卷 14 章的例子里出现的每一个零件——上下文装配、预算守卫、
状态检查点、记忆、护栏、编排、MCP 接入、trace、成本账、评测——都能在这套代码里
找到对应实现，而且它们之间是**互相咬合**的，不是 14 段互不相干的样例代码。

---

## 1. 环境与安装

| 要求 | 说明 |
| --- | --- |
| Python | **>= 3.9**（开发与验证环境为 3.13） |
| 第三方依赖 | **零**。运行期只用标准库 |
| API Key | **不需要**。默认走内置 mock 模型 |
| 网络 | **不需要**。全部测试与示例离线可跑 |

工程不需要安装就能用——在工程根目录下直接跑就行：

```bash
cd dragon-agent
python examples/end_to_end_demo.py
```

想装成命令行工具（可选）：

```bash
pip install -e .
dragon-agent demo
```

## 2. 一条命令跑通完整链路

```bash
python examples/end_to_end_demo.py
```

这条命令会走完整条回路——输入 → 规划 → 调工具 → 观察 → 停止——并把每一帧都打印出来：
每一步的动作、观察、token、成本，最后是答案、停止原因与总账，还有这一轮产生的 trace。

命令行入口（由 `dragon_agent/cli.py` 提供，**该模块尚未落地**，集成终验后可用）：

```bash
python -m dragon_agent.cli demo          # 端到端演示
python -m dragon_agent.cli run "帮我查一下项目预算"   # 跑一次完整回路
python -m dragon_agent.cli tools          # 列出可用工具
python -m dragon_agent.cli eval           # 跑评测集
```

切真实模型：加 `--live` 并设置 `LLM_API_KEY`。**缺 key 时会明确报错退出，不会静默失败**：

```bash
set LLM_API_KEY=sk-...
python -m dragon_agent.cli run "..." --live
```

## 3. 跑测试

```bash
python -m unittest discover
```

一条命令跑完全部测试，**不依赖网络**。测试分三类：

- `tests/test_<模块>.py`——每个模块的单元测试（由两位正文编委分工撰写）；
- `tests/test_end_to_end.py`——从输入到答案走完整条链路，断言最终状态与成本账；
- `tests/test_invariants.py`——把契约里的 **12 条跨模块不变式**逐条变成断言。

**实测结果**（Python 3.13.5，工程根目录下执行）：

```
$ python -m unittest discover
Ran 391 tests in 0.277s
OK (skipped=6)
```

其中本编委负责的 `test_invariants.py` + `test_end_to_end.py` 共 51 个用例，
现在真跑 45 个、跳过 6 个。**跳过的 6 个只依赖尚未落地的模块**
（`observability` / `evaluation` / `cli`），
用 `unittest.skipUnless` + `importlib.util.find_spec` 判定模块是否存在；
只依赖已落地模块的用例一律真跑，不许 skip。
集成终验时把这 6 个 skip 撤掉，要求 100% 真跑全绿。

## 4. 模块地图

14 个模块，依赖方向**不可逆**（下层不知道上层的存在）：

```
        cli.py
          |
     orchestration.py
          |
       agent.py  <---- guardrails.py
        |  |  \
        |  |   +---- state.py ---- memory.py
        |  +-------- context.py
        +----------- tools.py
                      |
                    llm.py
                      |
                   config.py
```

| 模块 | 职责 | 对应第一卷章节 |
| --- | --- | --- |
| `config.py` | 统一配置：模型、预算、路径；`to_dict()` 不含密钥 | 第 2、11 章 |
| `llm.py` | 模型接入抽象：mock 与真实 API 双模式，返回同一种 `LLMResponse` | 第 3、4、8 章 |
| `tools.py` | 工具注册、参数校验、错误回灌、幂等与可重放语义 | 第 8、9 章 |
| `context.py` | 上下文装配器：预算分配、检索、压缩、淘汰 | 第 10、28 章 |
| `agent.py` | Agent 主循环：规划、行动、观察、停止准则、预算守卫 | 第 4、7、11 章 |
| `state.py` | 任务状态机、检查点与回放 | 第 7、29 章 |
| `memory.py` | 短期/长期记忆：working / episodic / semantic / procedural 四类 | 第 26、27 章 |
| `guardrails.py` | 工具分级、二次确认、提示注入防护 | 第 31、32 章 |
| `orchestration.py` | 编排层：单 Agent 升级到多 Agent 的判据与 Supervisor | 第 20、23 章 |
| `mcp_server.py` | 把工具注册表暴露为 MCP server（JSON-RPC over stdio） | 第 24、39 章 |
| `observability.py` | OTel 风格结构化 trace 与成本核算 | 第 33 章 |
| `evaluation.py` | 自建任务集、判定器、回归对比 | 第 34 章 |
| `cli.py` | 命令行入口 | 第 14 章 |
| `tests/` | 确定性测试与回归基线 | — |

每个模块都能独立跑通（`python -m dragon_agent.<模块名>`），mock 优先、离线可跑、
`--live` 切真实 API。

**当前落地状态**：契约 14 个模块中已落地 11 个——
`config` / `llm` / `tools` / `context` / `agent` / `state` / `memory` /
`guardrails` / `orchestration` / `mcp_server`；
尚待落地 3 个——`observability` / `evaluation` / `cli`（对应第 12、13、14 章，
正在并行编写中）。

另有 6 个**共享基础设施模块**，不在契约的 14 个之列，是各模块共用的底层零件：

| 模块 | 职责 |
| --- | --- |
| `errors.py` | 异常分类：按 `kind` / `retryable` 决定处理策略 |
| `logging_setup.py` | 结构化日志与 `trace_id` 贯穿 |
| `pricing.py` | 计价：token 账换算成钱，缓存命中与未命中分开算 |
| `llm_policy.py` | 模型调用的失败分类与降级策略 |
| `tool_schema.py` | 工具 schema 的生成与规范化 |
| `tool_runner.py` | 工具执行的统一包装与错误回灌 |

## 5. 跨模块不变式

这套工程最容易坏的地方不是某个模块写错了，而是**两个模块的假设对不上**。
所以接口先定死（见 `agent-book/_plan/_dragon_contract.md`），再由 `tests/test_invariants.py`
把 12 条不变式逐条钉住：

1. `Agent.run()` 不抛异常给调用方——失败转成 `status="error"`；
2. 停止优先级固定：完成 > 异常 > 预算 > 无进展 > 步数上限；
3. 工具参数校验失败不抛异常，返回 `ToolResult(ok=False)` 让上层回灌给模型；
4. `replayable=False` 的工具在重试中必须拒绝执行；
5. 同一输入的 `ContextAssembler.render()` 输出完全确定；
6. `TaskState` 是唯一状态真相源，`context.py` / `memory.py` 不得各存一份副本；
7. `model` 来源的语义记忆写入被拒绝或降级；
8. 护栏只能收紧不能放宽；
9. 默认编排器是 `SingleAgent`，多 Agent 必须显式选择；
10. trace 属性用 OTel GenAI 约定（`gen_ai.*`），不用自定义的 `llm.*`；
11. 评测判定器不含 LLM 调用（关键字与规则，保证确定性与可离线运行）；
12. `Config.to_dict()` 不含密钥。

## 6. 目录结构

```
dragon-agent/
├── dragon_agent/            # 契约的 14 个模块 + 6 个共享基础设施模块
│                            # + __init__.py（公共 API 导出）
├── examples/
│   ├── end_to_end_demo.py   # 端到端示例：完整轨迹 + 成本账 + 停止原因（本编委）
│   ├── context_budget_demo.py   # 上下文预算分配演示
│   ├── llm_client_demo.py        # 模型客户端契约演示
│   ├── skeleton_hello.py         # 骨架自检：配置/异常/日志
│   └── tool_layer_demo.py        # 工具层演示
├── specs/                   # 需求规格模板与检查器
├── tests/
│   ├── _compat.py           # 构造适配层（按签名挑参数，不断言行为细节）
│   ├── test_invariants.py   # 12 条跨模块不变式
│   └── test_end_to_end.py   # 全链路 + 成本账
├── pyproject.toml
└── README.md
```

### examples/ 怎么跑

每个示例都可以在工程根目录下直接跑，全部离线、不需要 API Key、退出码 0：

```bash
python examples/end_to_end_demo.py      # 端到端：输入 → 工具 → 观察 → 答案 → 成本账 → 停止原因
python examples/context_budget_demo.py  # 上下文五层的预算分配、检索、压缩、淘汰
python examples/llm_client_demo.py      # mock 与真实客户端返回同一种 LLMResponse
python examples/skeleton_hello.py       # 配置早失败、异常分类、结构化日志
python examples/tool_layer_demo.py      # 参数校验、错误回灌、幂等与可重放
```

`end_to_end_demo.py` 的预期输出：任务与预算抬头 → 两步轨迹（动作、观察、
token、成本）→ 最终答案 → 成本账（步数 / token 合计 / 成本合计 / 最终状态 /
停止原因）→ trace 段（`observability` 落地前会明确提示这一轮没有 trace）。

## 7. 局限声明

**这套实现离生产还差得远。** 它是为了把一本书讲清楚而写的，不是拿来上线托管钱的。
下面逐条写清楚缺什么——不是客套话，是接手这套代码的人必须知道的事：

- **没有真沙箱。** 工具函数在宿主进程里直接执行，没有容器、没有 seccomp、
  没有资源限额。模型给的路径参数可以直接落到工作区之外的文件上。
  真实系统需要进程隔离或容器沙箱，外加文件系统与网络的显式白名单。
- **没有并发与多租户。** 一个 `Agent` 实例对应一个任务，状态全在内存里，
  没有租户隔离、没有并发上限、没有跨任务资源仲裁。
  真实系统需要任务队列、每租户配额，以及共享状态的一致性策略。
- **没有鉴权。** MCP server 是 stdio 上的裸 JSON-RPC，没有认证、没有授权模型、
  没有按调用方身份分级的工具权限。工具分级逻辑在 `guardrails.py` 里，
  但它判断的是**工具与参数**，不是**调用者**。
- **持久化很弱。** 状态与检查点是进程内的；`trace_path` 指向的目录才会有落盘产物。
  进程一挂，状态就没了，也没有崩溃恢复、没有分布式事务、没有备份。
  检查点能**存**快照，但没有真正的**回放引擎**去从检查点重跑一遍。
- **没有重试/退避/限流的真实策略。** 真实 API 客户端只做了一次 HTTP 请求，
  网络异常退化成一条错误文本，不做指数退避、不做熔断、不处理 429。
- **token 与成本是估算。** 上下文预算与成本账用的是估算的 token 数与每百万
  token 单价表，不是账单。单价表也需要随模型涨价而人工维护。
- **评测是关键词规则，不是真实任务集。** 判定器用关键字和禁用词，
  保证确定性和离线可跑，但它衡量不了任务的真实难度——
  一个能把禁用词写对的模型，不等于一个能答对的模型。
- **护栏是规则式，不是语义式。** 提示注入防护靠的是模式匹配与工具分级，
  对改写过的注入无能为力。护栏在主循环里接了两处：工具调用前的 `before_tool`
  与给出最终答案前的 `after_output`——后者的语义是"拦下就不进答案、也不进状态"。
  两处的失败方向故意相反：工具前护栏坏了就**不执行**（有副作用，关不住就别动），
  输出护栏坏了就**放行**（没有副作用，拦下来只会白白毁掉一次成功）。
- **没有流式输出、没有异步。** 一次模型调用是阻塞的，长任务的中间态拿不到。
- **没有提示缓存的实测。** 稳定块在前只是为缓存前缀命中留了位置，
  工程里没有做命中率统计。

换句话说：**它把「控制流与不确定性分离」「一次迭代三份账」这些设计原则做成了
可运行的代码，但生产化需要的那些工程能力（沙箱、并发、鉴权、持久化、可靠性）
都还只是空位。** 书中每一处提到生产化差距的地方，都可以在这一节找到对应的名字。

## 8. 与第一卷的对应关系

这套工程是第二卷的骨架，每个模块都能在第一卷里找到它的设计出处。
对照表见上面的「模块地图」。术语以第一卷为准：
一次迭代三份账（`context.py` / `state.py` / `observability.py`）、
控制流与不确定性分离（`agent.py` / `llm.py`）、
工具是 API 工程而不是提示词工程（`tools.py`）。
