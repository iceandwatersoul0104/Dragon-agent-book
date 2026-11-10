# ch23 协作拓扑与调度：Supervisor、层级、网状、辩论与黑板 最小可实现

对应《开源 Agent 架构技术全书》第 23 章《协作拓扑与调度：Supervisor、层级、网状、辩论与黑板》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**。一次跑清四件事：

- **五种拓扑跑同一个任务**：对比调用数、token 与"他人内容"token（上下文共享范围）；
- **失效注入**：让同一个角色失败，看五种拓扑的失败传播路径分别是什么；
- **层级成本模型**：深度与扇出如何决定调用数；
- **选型三维度**：任务可分解度、对抗性需求、可验证性。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch23-topology
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

### mock 模式（默认，无需 API Key）

```bash
python main.py                    # 四段演示
python main.py --fail 撰写员      # 换一个角色注入失败
python main.py --fail ""          # 关掉注入，只看健康基线
python main.py --depth 3 --fanout 3   # 层级成本模型的参数
```

### 典型输出

**A. 五种拓扑跑同一个任务（健康基线）**

```
拓扑                   调用    输入token   总token    他人内容    结果
Supervisor（集中调度） 9       1946        2305       601         成功
层级（Hierarchical）   13      2509        2977       721         成功
网状（Mesh，去中心化） 4       621         809        160         成功
辩论（Debate）         5       1046        1380       499         成功
黑板（Blackboard）     4       783         971        301         成功
```

"他人内容"这一列比总 token 更值得看：它直接反映共享范围。网状 160、黑板 301、Supervisor 601——同一批角色，彼此看见的信息量差了将近四倍。

**B. 失效注入（让「分析员」失败）**

```
[Supervisor（集中调度）] 部分结果
    失败角色：分析员
    传播路径：调度层捕获 → 重试 1 次 → 中止（已产出部分保留）

[层级（Hierarchical）] 部分结果
    失败角色：分析员
    传播路径：组调度器捕获 → 本组中止 → 整链中止（已产出部分保留）

[网状（Mesh，去中心化）] 成功
    失败角色：分析员
    传播路径：失败被吞 → 其余节点照常跑 → 结论缺一块，无人察觉

[辩论（Debate）] 成功
    传播路径：无失败

[黑板（Blackboard）] 部分结果
    失败角色：分析员
    传播路径：黑板缺栏 → 完备性检查不通过 → 达到轮次上限 → 返回部分结果
```

注意网状那一行：流程以**成功**收尾，交付物却少了一块。这是去中心化拓扑的固有语义，不是实现 bug。

**C. 层级成本模型**

```
完全展开时，一棵深度 d、扇出 f 的调度树，最坏情况的调用数：
    calls(d, f) = 1 + f + f^2 + ... + f^d = (f^(d+1) - 1) / (f - 1)

深度 d       扇出 f     最坏调用数
1            2          3 次
1            3          4 次
2            2          7 次
2            3          13 次
3            2          15 次
3            3          40 次
```

**D. 选型三维度**

```
任务特征                   可分解度   对抗性    可验证性   推荐拓扑
流水线式产出（文档/代码）  高         低        高         Supervisor 或层级
独立子任务（各查各的库）   高         低        低         扁平并行，不必多 Agent
方案需要被挑错             中         高        低         辩论
多角色共享一份工作区       中         低        高         黑板
无中心可自治推进           低         中        低         网状
全流程固定且要审计         高         低        高         SOP 流水线（第 22 章）
```

### mock 说明

内置模型不做真实推理：worker 返回固定产出，调度节点按预设计划派单，裁判按预设理由裁决。
**机制全部真实**：控制流、上下文共享范围、token 计量、失效注入、失败路径判定、完备性检查。

token 数是粗估值（中文按字、其余按 3.5 字符/token）；真实模型以服务端 `usage` 为准。
拓扑之间的相对关系（调用数、共享范围、失败语义）在任何模型上都成立。

### 真实模型（`--live`）

需要环境变量：

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_API_KEY` | 是 | — | 模型服务商的 API Key |
| `LLM_BASE_URL` | 否 | `https://api.openai.com/v1` | OpenAI 兼容的 chat completions 端点 |
| `LLM_MODEL` | 否 | `gpt-4o-mini` | 模型名 |

```bash
export LLM_API_KEY=sk-xxxxxxxx
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=gpt-4o-mini
python main.py --live
```

Windows PowerShell：

```powershell
$env:LLM_API_KEY="sk-xxxxxxxx"
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="gpt-4o-mini"
python main.py --live
```

端点需兼容 `POST {base}/chat/completions`：请求体含 `model` 与 `messages`，返回体取
`choices[0].message.content`。调度节点需要输出 JSON：
`{"next": "<角色名>", "handoff": "<交接>", "done": false}`，全部完成时输出
`{"done": true, "final": "<结论>"}`。换供应商时只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

live 模式下真实模型不保证 JSON 合法，解析失败时按"该轮已完成"处理——这本身就是生产上
必须给调度节点加格式校验与重试的理由。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--fail` | `分析员` | 注入失败的角色名；传空串则不注入 |
| `--depth` | `2` | 层级深度（成本模型用） |
| `--fanout` | `3` | 层级扇出（成本模型用） |
| `--live` | 关 | 切真实模型 API |
| `--model` / `--base-url` | 环境变量 | 覆盖 `LLM_MODEL` / `LLM_BASE_URL` |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `Topology` | 拓扑基类：统一的调用、计量与失败注入，保证对比公平 |
| `Topology._call()` | 一次 Agent 调用，返回产出与"他人内容"token 数 |
| `SupervisorTopology` | 中心派单、压缩上下文下发、失败重试与中止 |
| `HierarchicalTopology` | 根调度器 → 组调度器 → 执行者，两级扇出 |
| `MeshTopology` | 消息直达、无中心、失败被吞 |
| `DebateTopology` | 正反论证、交叉反驳、裁判裁决 |
| `BlackboardTopology` | 共享工作区 + 完备性检查（缺栏即部分结果） |
| `Result` | 指标：调用数、token、他人内容、是否成功、失败路径 |
| `demo_compare()` | 健康基线对比 |
| `demo_failure()` | 失效注入对比 |
| `demo_hierarchy()` | 深度与扇出的成本模型 |
| `demo_selection()` | 选型三维度对照表 |

## 换成自己的场景时要改什么

1. 改 `AGENT_ROLE` / `WORKER_ROLE` / `MOCK_WORKER`：换成你的角色与产出；
2. 改 `MOCK_SUPERVISOR_PLANS`：换成你的派单策略。**这是拓扑的核心**，不是样板代码；
3. **给调度节点加调用上限**：否则中心提示写不好就会无限派单；
4. 改 `BlackboardTopology.REQUIRED`：黑板的完成判据必须显式写出来，否则缺栏也会被当成完成；
5. 改 `Demo_selection` 的选型表：按你自己的任务分布调整；
6. **测失败路径，别只测 happy path**：`--fail` 换几个角色各跑一遍，看看你的拓扑会不会"假成功"。

## 相关章节

- 第 11 章：停止准则、步数上限与成本守卫（调度节点的调用上限）
- 第 20 章：多 Agent 的动机与何时不该上多 Agent（先判断该不该多 Agent）
- 第 21 章：对话式协作（GroupChat 是 Supervisor 的一种特例）
- 第 22 章：SOP 流水线（阶段固定时用流水线，而不是自由拓扑）
- 第 25 章：多 Agent 失效模式与成本治理（塌缩、循环与熔断）
