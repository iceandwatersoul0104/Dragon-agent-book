# 第 17 章 Semantic Kernel 与微软 Agent 栈

## 一、本章要解决的问题

一个 .NET 团队的内部助手项目：接公司组织架构（Microsoft Graph）、查工单、起草回复，还要满足两条硬要求——**用 Entra ID 的身份做审计**，**调用链要能进现有的 OpenTelemetry 管道**。

候选有三个：LangGraph（Python）、AutoGen（Python）、Semantic Kernel（C# 和 Python 都有）。他们选了 Semantic Kernel，理由很实在：类型安全、DI 容器原生、连接器把 Graph 和 Azure 服务接上了、过滤器可以把内容安全和审计统一挂载而不侵入业务代码。

半年后团队被通知：**微软把 AutoGen 和 Semantic Kernel 合并成了 Microsoft Agent Framework，两个前身都不再加新功能。**

于是问题来了：当初选 SK 错了吗？如果重来一次该选哪个？如果一个 Python 团队、没有任何 Azure 依赖，这套东西还值得看吗？

这不是"哪个框架更好"的问题，是**抽象层次选对了吗**的问题。SK 的核心比喻是操作系统——提示词是函数，你的业务方法是插件，一个内核统一调度它们；Agent Framework 的核心比喻是编排——Agent 是个体，Workflow 是把个体连起来的图。两者解决的不是同一层问题，而合并之后，这个分层被保留了下来。

## 二、原理：机制拆解

### 2.1 Kernel：把 LLM 当操作系统内核

SK 的四个核心抽象：

**Kernel（内核）**：中枢。在 Python 侧它是一个 Pydantic 模型，由四个扩展 mixin 组合而成，各自只管一件事——**挂调用、找函数、解析 AI 服务、重试策略**。这个拆法很能说明它的设计意图：内核不是一个"什么都干"的对象，它是四条正交关注点的汇合点。

**Plugin / Function（插件与函数）**：`KernelPlugin` 是一张具名函数注册表，`Kernel.add_plugin` 把它挂进内核。函数有两种来路：`KernelFunctionFromMethod` 把你的 Python 或 C# 方法包起来；`KernelFunctionFromPrompt` 把一段提示模板变成**一等函数**——它有自己的执行设置，可以像代码一样被调用、串联、复用。

"提示词是一等函数"是 SK 最独特的一笔。它意味着你写的一段提示词，和你写的一个方法，在内核眼里是同一种东西。

**Prompt Template（提示模板）**：三种语法并存（原生、Handlebars、Jinja2），渲染出来的就是函数调用的参数。

**Filters（过滤器）**：SK 最被低估的一块。三类接口分别挂在三个位置：

```
                    ┌────────────────────────────────┐
   AI 服务（模型）───→│            Kernel              │←── Plugins（函数注册表）
                    │  挂调用 / 找函数 / 选服务 / 重试   │
                    └───────────────┬────────────────┘
                                    │ 每次调用都穿过过滤链
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
    FunctionInvocation        PromptRender        AutoFunctionInvocation
    Filter（函数执行前后）       Filter（渲染前后）      Filter（自动调用循环里）
```

日志、缓存、重试、参数校验、内容安全、审计——全挂在这三处，而且**一行业务代码都不用改**。这是"中间件"思想在 Agent 框架里的最早实践之一。

还有两个容易被忽略的能力：`as_mcp_server` 可以把内核的函数集合直接导成一个 MCP 服务器；`invoke_stream` 的规则是——传多个函数时它们按顺序执行，只有最后一个被流式返回，前面的先跑完、输出作为上下文可用。

### 2.2 Memory 与 Connectors：接口的形状

**Memory**：SK 早期是 `ISemanticTextMemory` 那一套"检索 + 存储"的接口，重心后来挪到了 **VectorStore** 抽象——把向量库和嵌入模型解耦，各种向量库各有各的连接器。

把"检索"抽象成向量库接口，真正的收益不只是换个数据库那么表面——它把"怎么存"和"怎么算相似度"拆开了：嵌入模型可以单独换、索引可以单独重建，而上层调用代码一行不动。

**Connectors** 分两类，这个区分是企业集成视角的关键：

- **模型连接器**：把不同厂商的模型服务包成统一接口。服务选择交给 `AIServiceSelector`，按提示执行设置挑一个服务——同一个内核里可以挂多个模型，按任务路由。
- **业务连接器**：把企业系统接进来——Microsoft Graph、Azure 服务、MCP 服务器。

这两层合起来兑现一句承诺：**换模型不动业务代码，接业务系统不动模型代码。**

### 2.3 Process 编排：SK 自己也需要显式流程

SK 后来补了一套流程编排（`KernelProcess`），支持顺序、并发、汇合这些步骤模式，能跑在本地运行时或 Dapr 运行时上。

这条线索值得留意：SK 本来是"内核 + 插件"的抽象，可它自己最后也需要显式的流程控制——也就是第 12 章那套东西。

### 2.4 Agent Framework：合并的逻辑

时间线（多方交叉核实）：2023 年起 AutoGen 与 Semantic Kernel 并行开发；2025 年 10 月合并框架公开预览；**2026 年 4 月 3 日发布 1.0 正式版**，Python 与 .NET 双语言 GA。AutoGen 停止接收新功能，SK v1.x 至少还有一年支持期。

迁移难度并不对称：从 SK 迁相对平缓——它的中间件、连接器、遥测几乎能一一对上；AutoGen 那套"以对话为中心"的编程模型要改成"以图为中心"，得按重写来估工作量。

**为什么要合并**：AutoGen 出自微软研究院，强在多 Agent 编排的实验性模式（群聊、顺序流程、分层委派），弱在生产化——认证、插件管理、监控都得自己补；Semantic Kernel 出自产品团队，强在企业集成（插件架构、类型安全、遥测、中间件、函数调用治理），弱在多 Agent 编排是后加的，复杂模式要写更多样板代码。

**结果就是团队先选一个，然后撞上需要另一个的那堵墙。**

Agent Framework 的能力分三层：**Agent**（模型交互 + 工具 + MCP + 多轮会话 + 记忆）、**Workflow**（图式引擎，类型安全路由、检查点、流式、人工介入）、**企业层**（OpenTelemetry、Entra ID 认证、内容安全、CI/CD）。五种编排模式在 1.0 时已经稳定：sequential、concurrent、handoff、group chat、Magentic-One。模型接入有七家：Foundry、Azure OpenAI、OpenAI、Anthropic、Bedrock、Gemini、Ollama。记忆可插拔：会话历史、键值、向量检索，配 Foundry、Mem0、Redis、Neo4j 等适配器。

**一句话**：SK 的内核抽象和 AutoGen 的编排抽象，在合并后被拆成了两层——`Agent` 替掉 `Kernel` 成为主抽象，图式工作流引擎是新加的那一层。

## 三、为什么这样设计：取舍与失效条件

企业视角的优势确实存在：

1. **身份与审计天然接得上**。Entra ID 认证、OpenTelemetry 追踪、内容安全，这些是企业上线的准入条件，不是加分项；
2. **.NET 是一等公民**。C# 的类型安全和 DI 容器，对 .NET 团队是主场；
3. **横切关注点不进业务代码**。中间件、过滤器统一挂载；
4. **协议站在底层**。MCP 内建，外部工具目录演进时不用改应用代码。

约束也得讲清楚：

1. **耦合到微软生态**。Entra、Foundry、Content Safety、Graph——一旦用了这些，出 Azure 就有摩擦；
2. **Python 与 .NET 侧功能对齐还有差距**，较新的模式和记忆后端目前先在 Python 侧落地，迁移时要按"平台差异"预留工作量；
3. **A2A 还在路上**，跨运行时协作不能按已可用来排期；
4. **四项预览件**：DevUI 调试器、Foundry 托管 Agent、Skills、Copilot/Claude Code SDK 集成目前都还在预览阶段——别把生产方案建在预览件上；
5. **版本节奏快**，需要钉版本的策略；
6. **迁移是真成本**。从 AutoGen 迁过来，编程模型从"以对话为中心"变成"以图式工作流为中心"，要按**重写**而不是升级来估工作量。

**对 Python 开发者的现实建议**：

1. 你的团队在 Azure/.NET 生态里，或者必须接 Entra/Graph/内容安全——微软栈里 Agent Framework 是一等公民选择，SK 的设计（过滤器、连接器、服务选择）值得整体学一遍；
2. 你不在那个生态里——Python 社区更大、资料更多、LangGraph 的生产记录更长，Agent Framework 对你来说只是又一个选项，不用为了"用微软的"而绕远路；
3. 不管选哪个框架，**Filters/中间件这一层的设计都值得抄**。把日志、重试、校验、审计从业务代码里抽出去，这个模式跟框架无关，是你自己项目里就该有的东西；
4. 迁移决策：AutoGen 和 SK 项目都该往 Agent Framework 走，但**不急**——前身还在维护，新功能不再进而已。

什么时候不该碰这条线：Node.js/TypeScript 团队（Agent Framework 只有 .NET 和 Python）；只要一个 Agent 加几个工具（第 16 章那几条轻量路线）；检索才是主要工作量（第 13 章）。

## 四、动手：最小可实现

本章不配套独立示例目录，代码内嵌在这一节。这段脚本用纯标准库复刻 SK 最核心的四个语义——**插件注册表、函数化提示词、过滤器链、自动函数调用循环**——不需要 API Key 就能跑通：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SK 核心语义的最小复刻：Kernel + Plugin + Function + Filters + 自动函数调用。"""
import inspect
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文结果乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class MockModel:
    """可替换的模型层：按脚本先要工具调用，再给最终答案。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def request(self, messages, functions):
        self.calls += 1
        return self.script.pop(0) if self.script else {"content": "（脚本用尽）"}


@dataclass
class Function:
    """函数：名字、描述、参数、真正的实现。"""

    name: str
    description: str
    params: List[str]
    fn: Callable


class Plugin:
    """插件：一张具名函数注册表。"""

    def __init__(self, name):
        self.name = name
        self.functions: Dict[str, Function] = {}

    def add(self, fn, name=None, description=None):
        sig = inspect.signature(fn)
        self.functions[name or fn.__name__] = Function(
            name=name or fn.__name__,
            description=description or (fn.__doc__ or "").strip(),
            params=[p for p in sig.parameters if p != "ctx"],
            fn=fn,
        )
        return self  # 返回插件自己，方便链式添加


class Filter:
    """过滤器基类：调用前后各一个钩子。"""

    def before(self, ctx):
        pass

    def after(self, ctx):
        pass


class Kernel:
    """内核：挂调用、找函数、解析服务、重试策略，四件事分开管。"""

    def __init__(self):
        self.plugins: Dict[str, Plugin] = {}
        self.services: Dict[str, Any] = {}
        self.filters: List[Filter] = []
        self.trace: List[str] = []

    def add_plugin(self, plugin):
        self.plugins[plugin.name] = plugin
        return self

    def add_service(self, name, service):
        self.services[name] = service
        return self

    def add_filter(self, f):
        self.filters.append(f)
        return self

    def find(self, name):
        for p in self.plugins.values():
            if name in p.functions:
                return p.functions[name]
        raise KeyError("没有这个函数: %s" % name)

    def as_tool_manifest(self):
        """把函数集合导成工具清单——as_mcp_server 暴露的就是这个。"""
        return [
            {"name": f.name, "description": f.description, "parameters": f.params}
            for p in self.plugins.values()
            for f in p.functions.values()
        ]

    def invoke(self, name, **kwargs):
        fn = self.find(name)
        ctx = {"fn": name, "args": kwargs}
        for f in self.filters:
            f.before(ctx)
        out = fn.fn(**kwargs)
        ctx["result"] = out
        for f in self.filters:
            f.after(ctx)
        self.trace.append("invoke %s -> %r" % (name, out))
        return out

    def run(self, prompt, max_turns=4):
        """自动函数调用循环：模型点名要哪个函数，内核找出来执行，结果喂回去。"""
        model = self.services["default"]
        messages = [{"role": "user", "content": prompt}]
        for _ in range(max_turns):
            resp = model.request(messages, self.as_tool_manifest())
            if not resp.get("tool_calls"):
                return resp["content"], messages
            for call in resp["tool_calls"]:
                out = self.invoke(call["name"], **(call.get("args") or {}))
                messages.append(
                    {"role": "tool", "name": call["name"], "content": str(out)}
                )
        return "（达到最大轮数）", messages


class AuditFilter(Filter):
    """审计过滤器：横切关注点的最小例子——业务代码一行没改。"""

    def before(self, ctx):
        print("  [filter] 进入 %s %s" % (ctx["fn"], ctx["args"]))

    def after(self, ctx):
        print("  [filter] 离开 %s -> %r" % (ctx["fn"], ctx["result"]))


def lookup_order(order_id: str) -> str:
    """按订单号查订单状态。"""
    return {"A-91": "已发货，运单 SF7788"}.get(order_id, "订单不存在")


def refund(order_id: str) -> str:
    """发起退款。"""
    return "已受理 %s 的退款申请" % order_id


kernel = Kernel()
kernel.add_plugin(Plugin("orders").add(lookup_order).add(refund))
kernel.add_service(
    "default",
    MockModel(
        [
            {"tool_calls": [{"name": "lookup_order", "args": {"order_id": "A-91"}}]},
            {"content": "订单 A-91 已发货，运单 SF7788。"},
        ]
    ),
)
kernel.add_filter(AuditFilter())

print("工具清单：", kernel.as_tool_manifest())
answer, messages = kernel.run("订单 A-91 到哪了？")
print("回答：", answer)
print("消息序列：", messages)
print("调用轨迹：", kernel.trace)
```

跑起来能看到的四件事：工具清单是**从函数签名自动长出来的**，不用手写 schema；审计过滤器在函数执行前后各触发一次，**业务函数本身完全不知道有过滤器**；模型点名要函数，内核负责找到并执行；整个循环的每一步都落在 `trace` 里。

## 五、开源实现对照

- **Semantic Kernel**（MIT，https://github.com/microsoft/semantic-kernel）：本章前两节讲的范式的参考实现。以插件、技能、记忆与计划为企业应用接入 LLM。仓库里 Python、.NET、Java 三套 SDK 并排，还有一份跟踪各语言功能对齐情况的 FEATURE_MATRIX。
- **Microsoft Agent Framework**（MIT，https://github.com/microsoft/agent-framework）：合并后的统一框架。融合 AutoGen 与 Semantic Kernel，统一 Agent、工作流与部署，Python 与 .NET 双语言 1.0 GA，仓库是微软在 Agent 方向上唯一的主线投入。
- **AutoGen**（MIT（LICENSE-CODE）；根目录 LICENSE 为 CC-BY-4.0，https://github.com/microsoft/autogen）：路线 A 的来源，第 14 章拆过。它贡献了多 Agent 编排的实验性模式，但已被官方指定为"新功能不再进"的状态。
- **LangGraph**（MIT，https://github.com/langchain-ai/langgraph）：对照组。纯 Python，图式状态与检查点，生态和社区体量都更大。微软栈外的团队，状态型编排的首选仍然是它。
- **OpenAI Agents SDK**（MIT，https://github.com/openai/openai-agents-python）：对照组。轻量循环 + 交接 + 护栏，第 14 章拆过。Python 团队做多 Agent 的另一个成熟选择。

## 六、坑与检查清单

- [ ] 新项目直接考虑 Agent Framework，别在两个前身上写新功能。
- [ ] 迁移评估按"重写"算工作量，尤其从 AutoGen 迁到图式工作流的那部分。
- [ ] 迁移计划里单列一条：**Python 与 .NET 侧的功能对齐差距**。
- [ ] 生产方案不要建在预览件上：DevUI、Foundry 托管、Skills、Copilot/Claude Code SDK 集成都还是预览。
- [ ] A2A 跨运行时协作按"未到位"排期，不要写进本季度的交付。
- [ ] 钉版本策略定下来——发布节奏快，weekly release 会咬人。
- [ ] 用 MCP 前把安全公告过一遍，尤其是托管与令牌处理相关的部分。
- [ ] 把过滤器（中间件）当横切层用：日志、重试、缓存、校验、审计、内容安全都挂这里，别写进业务代码。
- [ ] 过滤器本身要有失败策略：审计过滤器自己挂了，要不要阻断业务调用？
- [ ] 服务选择策略写清楚：同一个内核挂多个模型时，按什么路由。
- [ ] `invoke_stream` 传多函数时注意只有最后一个被流式返回。
- [ ] 用 `as_mcp_server` 导出函数集合前，确认暴露的函数列表是你想对外的。
- [ ] Entra 权限范围按最小权限设计，Agent 的令牌能干什么要逐条确认。
- [ ] Python 团队不在 Azure 生态里的，别为"统一技术栈"绕远路。

## 七、小结与延伸阅读

这一章真正值得带走的，不是微软的 API 名字，是**抽象层次这件事**。

SK 选的是"内核"层：提示词是一等函数，业务方法是插件，横切关注点由过滤器统一承载。这套设计产生于一个具体的约束——企业应用里，模型只是众多依赖之一，身份、日志、审计、内容安全都得和它平等共存。Agent Framework 选的是"编排"层：Agent 是个体，Workflow 是把它们连起来的图，状态、检查点、暂停恢复是内建的。

两者不冲突，因为它们回答的是不同问题。**"这次调用要不要被审计、要不要重试、要不要过内容安全"是内核层的问题；"这三个 Agent 先跑谁、跑到一半失败了从哪继续"是编排层的问题。** 微软花了三年才把这两层分开说清楚——这本身就是一条值得学的经验：框架演进的历史，就是**概念分层逐渐清晰**的历史。

对 Python 团队最实际的一条：不管用不用微软的栈，**把横切关注点从业务代码里抽出去**这件事，你现在就可以做。一个十来行的过滤器链，能省掉后面无数个"这个日志到底加在哪儿"的争论。

延伸阅读：

- Semantic Kernel — https://github.com/microsoft/semantic-kernel
- Microsoft Agent Framework — https://github.com/microsoft/agent-framework
- AutoGen — https://github.com/microsoft/autogen
- LangGraph — https://github.com/langchain-ai/langgraph
- OpenAI Agents SDK — https://github.com/openai/openai-agents-python
