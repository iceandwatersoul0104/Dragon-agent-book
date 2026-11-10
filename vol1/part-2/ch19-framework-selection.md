# 第 19 章 框架选型决策树与十个常见反模式

## 一、本章要解决的问题

前七章拆了七套框架，每一套都有各自的道理。合上书，真正难的问题才开始：**我这个项目该用哪一套？**

现实里的选型很少是技术问题：

- 需求是"加个客服助手"，方案却写成"做个多 Agent 系统"——于是上了最重的框架；
- 某框架的文档被团队读过，它就成了"我们的技术栈"，哪怕它解决的是我们没有的问题；
- 选型时比了功能表，上线后才发现缺东西：没有回溯、观测接不上、评测没法写；
- 半年后要换框架，发现业务代码里全是框架特有的类型，动不了。

本章把七章收敛成能拿去开会的三样东西：**一张按顺序问的决策树、一张能力对照表、十个反复踩到的反模式**，再加一条起步路径和一份迁移预案。

先说清楚：**决策树给的是起点，不是终点。** 它的作用是让你别一开始就上最重的框架，也别在第一步就踩第三级该解决的问题。

## 二、原理：机制拆解

### 2.1 决策树：六个轴，按这个顺序问

顺序是有讲究的——**先问团队和语言，再问需求**。因为框架的生态边界比功能边界硬：功能缺的可以自己写，生态缺的重写整个项目。

| 顺序 | 问什么 | 怎么答 | 答案指向 |
| --- | --- | --- | --- |
| 1 | 团队背景 | 主力语言是什么？谁维护？ | 生态边界 |
| 2 | 语言栈与部署形态 | Python / TS / .NET？常驻服务还是边缘？ | 直接划掉一半候选 |
| 3 | 任务性质 | 开放对话 / 知识检索 / 固定流程 / 数据计算 / 写代码 | 框架类别 |
| 4 | 是否真的需要多 Agent | 见下面那条判据 | 复杂度分水岭 |
| 5 | 是否需要持久化 | 跨轮记忆 / 中断等人 / 回溯重放 / 崩溃恢复 | 选型的硬指标 |
| 6 | 控制流复杂度 | 直线 / 分支 / 循环 / 并行分支汇合 | 编排抽象的选择 |

第 4 条的判据要单独说：**拆成多个 Agent 的正当理由只有三个——上下文不同（要读的东西不一样）、工具不同（权限不一样）、评测标准不同（各自的质量判据不一样）。三个都不满足，就别拆。** 拆了之后的成本是实打实的：上下文要复制或裁剪、消息要在 Agent 之间传、协调逻辑本身会出 bug、评测要按 Agent 分别写。

第 5 条是**硬指标**，不是加分项。需要在流程中间停下来等人批、或者能把上周那次跑飞的执行原样重放——那就必须选有检查点（checkpoint）和暂停恢复的框架（MCP 协议是另一回事，见第 13 章）。反过来，没有这些需求却被框架的宣传语推着上重框架，是最常见的一次性浪费。

第 6 条决定编排抽象：直线流程用任何工作流都行；出现循环和并行汇合，就需要状态图级别的编排（LangGraph、Agent Framework）；出现来回协商，就该用对话式（AutoGen）或角色式（CrewAI）。

### 2.2 能力对照表

下面这张表汇总第 12 到 18 章的判断。**"需另接"是指框架本身不提供，得自己接或者换一套。**

| 框架 | 编排 | 持久化 | 观测 | 评测 | 多 Agent | 协议 | 成本控制 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| LangGraph | 状态图（最强） | 检查点 + 时间旅行 | 需另接 LangSmith | 需另接 | 有 | MCP | 自己控 |
| Microsoft Agent Framework | 图式 Workflow | 检查点 + 暂停恢复 | 内建 OTel | 有 | 五种模式 | MCP + A2A | 需另接 |
| AutoGen | 会话式群聊 | 运行状态序列化 | 内建 tracing | 有 | 强 | MCP | 自己控 |
| OpenAI Agents SDK | 轻量循环 + 交接 | Session（会话记忆） | 内建 tracing | 需另接 | 有 | MCP | maxTurns 预算 |
| CrewAI | 顺序 / 层级（角色式） | 弱 | 有 | 有 | 有 | MCP（较新） | 层级三倍 |
| LlamaIndex | 事件驱动 Workflows | 弱 | 有 | 有 | 有 | MCP | 自己控 |
| Mastra | Workflow（挂起恢复） | 记忆后端 | 内建 OTel | 内置打分器 | 有 | MCP | 自己控 |
| PydanticAI | 自己写循环 | 无 | Logfire | 需另接 | 要自己拼 | MCP | retries 预算 |
| smolagents | 代码循环 | 无 | 弱 | 弱 | 弱 | MCP | max_steps |
| Vercel AI SDK | 工具循环 | 无 | 靠网关 | 无 | 无 | 可作 MCP 工具 | 无内置 |

读这张表有三个要点：

**持久化那一列差距最大。** "Session" 和"检查点"是两回事——前者存对话历史，后者存**流程走到哪儿了**。需要回溯和暂停恢复的，只有带检查点的那几行给得了。

**观测和评测是分开的两列。** 内建 tracing 不等于有评测能力，反过来也一样——两个都要另接一套。

**AutoGen 那一行要多看一眼**：它已进入维护模式，新功能不再进。技术上依然强，但新项目慎选。

### 2.3 十个反模式

每个都是"症状 → 为什么错 → 怎么办"。

**一、过早引入多 Agent。** 症状：需求从"加个客服助手"变成"五个角色协同"。为什么错：协调成本（上下文复制、消息传递、Agent 间的 bug）大于分工收益。怎么办：先用 2.1 那三个判据过一遍，过不了就不拆。

**二、把框架当业务逻辑。** 症状：退款上限写在提示词里，运营改规则要去改提示词。怎么办：业务规则进代码，提示词只写"什么时候调这个工具"。

**三、忽略可观测性。** 症状：线上出错只能看最终答案猜。怎么办：上线前先把追踪（trace）接好（第 14 章那套 span 树）。

**四、把记忆做成向量库即正义。** 症状：接了向量库就宣称"Agent 有记忆了"。为什么错：记忆分工作、情景、语义、程序性四类，向量库只解决语义检索那一块。怎么办：先说清缺哪一类。

**五、用角色扮演代替接口设计。** 症状：backstory 写了三段，输出格式全靠模型自觉。为什么错：角色是提示，不是契约。怎么办：能用 schema 约束的就别用提示约束（第 16 章）。

**六、没有终止条件。** 症状：`while` 循环跑到步数上限，代码里到处是 `if`。怎么办：停止条件写进图（第 11、12 章）。

**七、抽象泄漏到业务层。** 症状：业务函数的参数里出现框架类型。怎么办：端口隔离，见 2.5。

**八、同一套编排写两遍。** 症状：Python 后端一套流程，TS 前端又写一套。怎么办：编排放一处，另一侧用 MCP 或 HTTP 调（第 18 章）。

**九、先选框架再想问题。** 症状：会议第一个议题是"用哪个框架"。为什么错：框架是约束条件，不是目标。怎么办：先写清要解决什么、成功标准是什么。

**十、不写评测就上线。** 症状：改了个提示词，线上行为变了，没人发现。为什么错：模型有随机性，提示词一改行为就可能漂。怎么办：至少留一组固定输入 + 期望输出（第 15、16 章）。

### 2.4 从零开始的推荐起步路径

**第一步：手写。** 一个循环 + 几个工具函数 + 一个 JSON schema，不引框架。完全可控、零依赖；代价是工具循环、错误重试、消息历史、类型校验全得自己写。

出现任意一条就该升级：任务要跑十步以上；需要人审批才能继续；需要回放和回溯；要接三个以上模型供应商；除你之外还有人要维护它。

**第二步：轻框架。** PydanticAI、OpenAI Agents SDK、smolagents、TS 侧的 AI SDK。输出类型契约、护栏、会话、追踪这些横切能力白拿，编排仍然自己写循环。

再出现任意一条就该升级：出现了真正的分支和循环；出现了并行分支汇合；出现了"从某一步重来"的需求。

**第三步：重框架。** LangGraph、Agent Framework、CrewAI、AutoGen、LlamaIndex、Mastra。持久化、可视化、并行原语、生态都有，代价是抽象多、曲线陡、升级有破坏性变更。

**提醒：大多数项目停在第二步就够了。** 第三步不是"更专业"，是"更贵"——用不到的能力也在收你的心智税。

### 2.5 迁移预案：锁定风险与抽象泄漏

框架锁定的风险来自三处：**运行时泄漏**（业务代码里出现框架特有的类型）、**持久化格式**（检查点里存的是框架的状态模型）、**提示词与工具 schema 散落在框架的注册调用里**。

三种防护：

- **端口隔离**。业务层只依赖自己的接口——定义一个 `OrderWorkflow` 协议，框架只是它的一个实现，换框架等于换一个实现类。成本最高，也最有效。
- **状态自有格式**。状态用你自己定义的 schema，框架的 State 只是传输格式；检查点自己定义，框架的检查点当缓存、不当真相。
- **提示词与工具外置**。提示词存成文件，工具 schema 自己生成，不写在框架的注册调用里，这样版本管理独立于框架版本。

**什么时候该接受锁定**：只有当某项能力是框架的独有能力（比如 LangGraph 的检查点加时间旅行、图式编排），而自研代价高出一个数量级。前提是**你能说出是哪一项**——说不出，就不叫接受锁定，叫被绑架。

抽象泄漏的三个信号，出现任意一条就该动手隔离了：

1. 业务函数签名里出现了框架类型；
2. 单元测试必须先构造框架对象才能跑；
3. 框架升级要改业务代码。

## 三、为什么这样设计：取舍与失效条件

**为什么按这个顺序问。** 团队和语言在前，是因为它们是**不可协商的约束**：框架的生态边界（Python 的数据科学生态、TS 的边缘与流式、.NET 的企业集成）不随你的需求变化。需求在后，是因为需求会变，而框架选错可以换，团队语言换不了。

**决策树会失效的四种情况**：

1. **团队有既有基建和惯性**。换框架的成本常常大于收益，哪怕新框架更合适；
2. **框架本身在变**。AutoGen 合并进 Agent Framework 就是活例子：半年前的对错今天可能反过来。**选型结论的有效期以季度计**；
3. **需求还没定型**。产品早期做架构决策是浪费，此时该选最轻、能随时扔掉的方案；
4. **混合栈**。决策树是单选，真实项目常常是"这半用 A、那半用 B"——那就当成分层选型：交互层和编排层分开选（第 18 章那个组合）。

**决策树给的是起点不是终点。** 正确的用法是：按树选出最小可行方案，跑起来，撞到升级条件再往上走一格。反过来——先想清楚终点再倒推选型——在需求会变的前提下，是最常见的过度设计。

## 四、动手：最小可实现

本章不配套独立示例目录，代码内嵌在这一节。这段脚本把 2.1 的决策树写成一个**可执行的打分器**：填一个项目画像，它给出排序后的候选、命中的理由，以及警告。纯标准库，不需要 API Key：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把选型决策树写成打分器：填项目画像，输出候选、理由与警告。"""
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


@dataclass
class Project:
    """项目的六个轴。"""

    language: str = "python"       # python | ts | dotnet
    task: str = "workflow"         # chat | knowledge | workflow | data | code
    multi_agent: bool = False
    needs_persistence: bool = False
    control_flow: str = "linear"    # linear | branch | loop | parallel
    deployment: str = "server"      # server | edge
    ecosystem: str = "generic"      # generic | microsoft


@dataclass
class Framework:
    """框架的特征，以及它的匹配分。"""

    name: str
    orchestration: str        # state-graph | workflow | chat | loop | none
    persistence: str         # strong | session | weak | none
    observability: str       # builtin | optional | weak
    evals: str               # builtin | optional | weak
    multi_agent: bool
    protocols: str
    languages: Tuple[str, ...]
    edge_ready: bool
    cost_control: str        # explicit | framework | none
    strengths: Tuple[str, ...] = ()


FRAMEWORKS: List[Framework] = [
    Framework("LangGraph", "state-graph", "strong", "optional", "optional", True,
              "MCP", ("python",), False, "explicit",
              ("状态图", "检查点", "时间旅行")),
    Framework("Microsoft Agent Framework", "state-graph", "strong", "builtin", "builtin", True,
              "MCP + A2A", ("dotnet", "python"), False, "explicit",
              ("图式工作流", "暂停恢复", ".NET 一等公民")),
    Framework("OpenAI Agents SDK", "loop", "session", "builtin", "optional", True,
              "MCP", ("python", "ts"), True, "framework",
              ("轻量", "交接", "护栏")),
    Framework("CrewAI", "workflow", "weak", "builtin", "builtin", True,
              "MCP", ("python",), True, "explicit",
              ("角色式", "上手快")),
    Framework("LlamaIndex", "workflow", "weak", "builtin", "builtin", True,
              "MCP", ("python",), True, "explicit",
              ("数据与检索",)),
    Framework("Mastra", "workflow", "weak", "builtin", "builtin", True,
              "MCP", ("ts",), True, "explicit",
              ("TS 原生", "工作流", "评测")),
    Framework("PydanticAI", "loop", "none", "optional", "optional", False,
              "MCP", ("python",), True, "framework",
              ("输出契约", "依赖注入", "测试友好")),
    Framework("smolagents", "loop", "none", "weak", "weak", False,
              "MCP", ("python",), True, "framework",
              ("极简", "代码优先")),
    Framework("Vercel AI SDK", "loop", "none", "weak", "none", False,
              "MCP", ("ts",), True, "none",
              ("流式 UI", "全栈一致")),
]


def score(project: Project, fw: Framework) -> Tuple[int, List[str]]:
    """给一个框架打分，返回（分数，命中理由）。"""
    s, why = 0, []

    if project.language in fw.languages:
        s += 3
        why.append("语言匹配（%s）" % project.language)
    else:
        s -= 5
        why.append("语言不匹配（只支持 %s）" % "/".join(fw.languages))

    if project.task == "knowledge" and "数据与检索" in fw.strengths:
        s += 4
        why.append("检索是主要工作量")
    if project.task == "workflow" and fw.orchestration in ("state-graph", "workflow"):
        s += 2
        why.append("流程型任务，编排抽象对得上")
    if project.task == "chat" and fw.orchestration == "loop":
        s += 2
        why.append("对话型任务，工具循环够用")

    if project.needs_persistence:
        if fw.persistence == "strong":
            s += 5
            why.append("有检查点，能回溯")
        elif fw.persistence == "session":
            s += 1
            why.append("只有会话记忆，不是检查点")
        else:
            s -= 3
            why.append("没有持久化")

    if project.control_flow in ("loop", "parallel"):
        if fw.orchestration == "state-graph":
            s += 4
            why.append("循环/并行汇合需要状态图")
        elif fw.orchestration == "chat":
            s += 2
            why.append("对话式协调适合来回协商")

    if project.multi_agent:
        if fw.multi_agent:
            s += 3
            why.append("支持多 Agent")
        else:
            s -= 3
            why.append("多 Agent 要自己拼")
    else:
        if not fw.multi_agent:
            s += 2
            why.append("单 Agent 就够，抽象更简单")

    if project.deployment == "edge" and fw.edge_ready:
        s += 2
        why.append("能跑边缘运行时")

    if project.ecosystem == "microsoft" and "dotnet" in fw.languages:
        s += 3
        why.append("微软栈里是一等公民")

    return s, why


def advise(project: Project) -> None:
    warnings: List[str] = []

    # 硬否决：这些情况根本不该上 Agent 框架
    if project.task == "data":
        warnings.append("任务本质是数据/数值计算——直接调模型写代码，不要上 Agent 框架")
    if project.multi_agent:
        warnings.append("拆多 Agent 前先过三问：上下文不同？工具不同？评测标准不同？")
    if not project.needs_persistence and project.control_flow == "linear":
        warnings.append("既不要持久化也不要分支——手写循环可能就够了（第 19 章 2.4 第一步）")
    if project.deployment == "edge" and not project.needs_persistence:
        warnings.append("边缘部署下检查点通常落不了盘，先确认恢复需求从哪来")

    print("项目画像：")
    for k, v in vars(project).items():
        print("  %-20s %s" % (k, v))

    ranked = sorted(
        ((score(project, fw)[0], fw) for fw in FRAMEWORKS),
        key=lambda x: -x[0],
    )

    print("\n候选排序：")
    for i, (sc, fw) in enumerate(ranked[:5], 1):
        s, why = score(project, fw)
        print("  %d. %-30s %+d 分" % (i, fw.name, sc))
        print("     理由：%s" % "；".join(why))

    if warnings:
        print("\n警告：")
        for w in warnings:
            print("  ! %s" % w)

    print(
        "\n提醒：这只是起点。先按第一名跑起来，撞到升级条件再往上走一格——"
        "别一步跳到重框架。"
    )


if __name__ == "__main__":
    advise(
        Project(
            language="python",
            task="workflow",
            multi_agent=False,
            needs_persistence=True,
            control_flow="loop",
            deployment="server",
            ecosystem="generic",
        )
    )
```

跑一下默认画像（Python、流程型、循环、要持久化、单 Agent），第一名应该是 LangGraph——它的状态图、检查点和时间旅行正好对上这三条。改画像再看排序怎么变，比读十篇对比文章有用。

## 五、开源实现对照

这一节是"该读哪份文档"，不是"谁更好"。

- **LangGraph**（MIT，https://github.com/langchain-ai/langgraph）：需要循环、分支、并行汇合、回溯重放时的首选。第 12 章。
- **Microsoft Agent Framework**（MIT，https://github.com/microsoft/agent-framework）：微软栈（.NET、Entra、Graph、Azure）里的一等公民，第 17 章；也是 AutoGen 与 Semantic Kernel 的官方继任者。
- **AutoGen**（MIT（LICENSE-CODE）；根目录 LICENSE 为 CC-BY-4.0，https://github.com/microsoft/autogen）：会话式多 Agent 的经典形态，第 14 章。已停止接收新功能，新项目慎选。
- **OpenAI Agents SDK**（MIT，https://github.com/openai/openai-agents-python）：单 Agent 加交接加护栏的轻量解，第 14 章；TS 版见第 18 章。
- **CrewAI**（MIT，https://github.com/crewAIInc/crewAI）：角色—任务—流程心智模型，业务方看得懂，第 15 章。
- **LlamaIndex**（MIT，https://github.com/run-llama/llama_index）：答案必须来自私有文档时的选择，第 13 章。
- **PydanticAI**（MIT，https://github.com/pydantic/pydantic-ai）：输出契约和测试友好优先时的选择，第 16 章。
- **smolagents**（Apache-2.0，https://github.com/huggingface/smolagents）：极简和代码优先，第 16 章。
- **Mastra**（Apache-2.0，但 `ee/` 目录下的企业版组件另有许可，https://github.com/mastra-ai/mastra）：TS 全栈团队做 Agent 产品时的选择，第 18 章。
- **LangChain**（MIT，https://github.com/langchain-ai/langchain）：链式组合的底座，LangGraph 就建在它上面，第 12 章。

## 六、坑与检查清单

- [ ] 按决策树的顺序问一遍，别跳步——先定团队和语言，再谈需求。
- [ ] 拆多 Agent 前过那三问：上下文不同、工具不同、评测标准不同。
- [ ] 区分"会话记忆"和"检查点"：前者存对话，后者存流程位置。
- [ ] 需求还没定型的项目，选最轻且能随时扔掉的方案。
- [ ] 选型结论的有效期以季度计，尤其在框架合并频繁的时候。
- [ ] 有一项能力是框架独有、且自研代价高一个数量级——只有这时才接受锁定。
- [ ] 业务层不出现框架类型，单元测试不必构造框架对象。
- [ ] 状态用自己定义的 schema，框架的检查点当缓存不当真相。
- [ ] 提示词和工具 schema 存成文件，独立于框架版本管理。
- [ ] 业务规则写在代码里，不写在提示词里。
- [ ] 上线前先把 trace 接好，否则调试只能靠猜。
- [ ] 至少留一组固定输入 + 期望输出，作为回归基线。
- [ ] 终止条件写在编排里，不在每个节点各写一遍。
- [ ] 要跑不可信代码，先确认环境里有容器或托管沙箱。
- [ ] 不用"多 Agent"当项目目标，它是手段。

## 七、小结与延伸阅读

前面七章拆了七套框架，这章收敛成一句话：**先想清楚要解决什么，再让框架来适配。**

操作层面，三件事值得记住：

**决策树的顺序是"团队 → 语言 → 需求 → 复杂度"。** 生态边界最硬，需求会变。

**对照表里只有一列是硬指标——持久化。** 观测、评测、多 Agent 都是"接得上就行"，持久化是"有没有"的区别。

**最贵的两个反模式是"过早引入多 Agent"和"先选框架再想问题"。** 共同点是：在还不知道要解决什么时，先付了架构的债。

最后一句给还在纠结的人：**绝大多数项目停在"手写循环 + 几个工具"这一级就够了。** 框架是解决你已经遇到的具体问题的，不是让架构图变好看的。

延伸阅读：

- LangGraph — https://github.com/langchain-ai/langgraph
- Microsoft Agent Framework — https://github.com/microsoft/agent-framework
- AutoGen — https://github.com/microsoft/autogen
- OpenAI Agents SDK — https://github.com/openai/openai-agents-python
- CrewAI — https://github.com/crewAIInc/crewAI
- LlamaIndex — https://github.com/run-llama/llama_index
- PydanticAI — https://github.com/pydantic/pydantic-ai
- smolagents — https://github.com/huggingface/smolagents
- Mastra — https://github.com/mastra-ai/mastra
- LangChain — https://github.com/langchain-ai/langchain
