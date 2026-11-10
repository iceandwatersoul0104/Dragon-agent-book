# ch22 SOP 流水线：MetaGPT、ChatDev 与 AgentVerse 的软件公司隐喻 最小可实现

对应《开源 Agent 架构技术全书》第 22 章《SOP 流水线：MetaGPT、ChatDev 与 AgentVerse 的软件公司隐喻》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**。一次跑清六件事：

- **阶段契约**：每个阶段声明"读哪些标准文档、写哪些标准文档"，下游只认契约；
- **标准文档作为共享状态**：版本化、审计日志、历史版本可回放；
- **失效判定**：上游版本一变，下游自动判定为脏，且算传递闭包；
- **评审回路**：评审可以打回指定阶段，带返工次数上限；
- **成本对照**：契约式输入（只读该读的） vs 全量共享输入（什么都读）；
- **迁移到非软件任务**：换一套阶段契约，同一个引擎照跑。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch22-sop-pipeline
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

### mock 模式（默认，无需 API Key）

```bash
python main.py                  # A~E 五段演示
python main.py --trace          # 打印每一步的完整提示
```

### 典型输出

**A. 阶段契约**

```
阶段           角色           读                 写
产品经理       产品经理       需求               prd
架构师         架构师         prd                api
工程师         工程师         api                tasks、code
测试工程师     测试工程师     api、code          tests
评审           评审           prd、api、tests    review
```

**B. 执行审计**

```
阶段        读                      写                token      结论
产品经理    —                       prd@v1            455        —
架构师      prd@v1                  api@v1            640        —
工程师      api@v1                  tasks@v1、code@v1 769        —
测试工程师  api@v1、code@v1         tests@v1          735        —
评审        prd@v1、api@v1、tests@v1 review@v1        915        rework
工程师      api@v1                  tasks@v2、code@v2 769        —
测试工程师  api@v1、code@v2         tests@v2          735        —
评审        prd@v1、api@v1、tests@v2 review@v2        880        approve

终止原因 completed｜返工 1 次｜模型调用 8 次｜总 token 5898
```

评审第一轮打回工程师，第二轮通过——这就是评审回路。

**C. 成本对照**

```
契约式输入（只读声明的输入）   8 次       输入 4255        总 token 5898
全量共享输入（每阶段都读全部文档） 8 次       输入 7166        总 token 8809

全量共享比契约式多花 1.5 倍：SOP 的成本优势不在模型，在契约。
```

**D. 上游变更 → 下游失效 → 增量重跑**

```
文档版本谱系：
  prd      v1
  api      v1
  tasks    v1、v2
  code     v1、v2
  tests    v1、v2
  review   v1、v2

改了 PRD 之后，失效的文档：api、code、review、tasks、tests
增量重跑执行了：架构师、工程师、测试工程师、评审｜本次花费 3052 token
产物版本：prd v1、v2；api v1、v2；code v1、v2、v3；review v1、v2、v3
可回放：prd 一共有 2 个历史版本，随时能取回任意一版对比。
```

**E. 迁移到非软件任务（门店会员系统选型）**

```
阶段         读            写             结论
调研员       —             brief@v1       —
方案师       brief@v1      proposal@v1    —
风控评审     proposal@v1   review2@v1     approve

终止原因 completed｜返工 0 次｜总 token 951
```

同一套引擎，只换了阶段契约。

### mock 说明

内置模型按角色返回固定的标准文档（PRD、接口定义、任务清单、实现代码、测试用例、评审报告），
不做真实推理。**机制全部真实**：阶段契约、版本管理、失效判定、返工路由、审计日志、token 计量。

评审阶段的 mock 第一次返回 `verdict=rework`（打回工程师），第二次返回 `approve`，
用来演示返工回路。增量重跑那一段脚本会预置评审状态，让它直接放行，演示"只重跑脏掉的那几段"。

token 数是粗估值（中文按字、其余按 3.5 字符/token）；真实模型以服务端 `usage` 为准。

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
python main.py --live --trace
```

Windows PowerShell：

```powershell
$env:LLM_API_KEY="sk-xxxxxxxx"
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="gpt-4o-mini"
python main.py --live --trace
```

端点需兼容 `POST {base}/chat/completions`：请求体含 `model` 与 `messages`，返回体取
`choices[0].message.content`。模型需要输出 JSON：`{"artifacts": {"<文档名>": "<文档全文>"}}`，
评审阶段额外输出 `{"verdict": "approve|rework", "rework_stage": "<阶段名>"}`。
换供应商时只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

解析失败时脚本会把原文直接当成产物内容，不会让流水线崩掉——但那一版的文档质量就没有保障了，
这也是"产物要有 schema 校验"的理由。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--live` | 关 | 切真实模型 API |
| `--model` / `--base-url` | 环境变量 | 覆盖 `LLM_MODEL` / `LLM_BASE_URL` |
| `--trace` | 关 | 打印每一步的完整提示（看契约怎么落到提示里） |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `Stage` | 阶段契约：读什么、写什么、是不是评审阶段、打回到哪 |
| `Artifact` | 标准文档：版本号、产出者、输入版本谱系 |
| `ArtifactStore` | 文档仓库：当前版本、历史版本、审计日志 |
| `ArtifactStore.stale_set()` | 失效判定，迭代到不动点算传递闭包 |
| `Pipeline._build_prompt()` | 只把声明的输入拼进提示 |
| `Pipeline.run()` | 主循环：执行 → 写产物 → 评审路由，返工有次数上限 |
| `Pipeline.read_all` | 对照组：每个阶段都读全部文档 |
| `demo_staleness()` | 上游人工改文档 → 失效判定 → 增量重跑 |
| `demo_non_software()` | 同一引擎跑非软件任务 |
| `MockLLM` / `LiveLLM` | 确定性脚本与真实模型 |

## 换成自己的场景时要改什么

1. 改 `make_software_stages()`：按你的业务重写阶段契约，注意每个阶段的 `reads` 要**只列它真正需要的文档**；
2. 改 `software_payloads()`（mock）与提示里的 `instruction`：产物格式要和你的下游对齐；
3. **给文档定 schema**：脚本里产物是自由文本，接上真实系统时要在写入前做格式校验，
   否则"格式耦合"会变成"格式悄悄变了"；
4. 改 `ArtifactStore.stale_set()`：如果有跨文档的依赖（比如产出里引用了别的文档的章节），要补进去；
5. **返工上限是硬闸门**：`max_rework` 到了就停。评审一直打回，往往说明需求本身有问题，
   再跑一百遍流水线也没用；
6. **别照抄角色名**：迁移到新领域时阶段要重新定义，照抄 CEO/CTO/PM 只会产出正确的废话。

## 相关章节

- 第 11 章：停止准则、步数上限与成本守卫（返工上限与预算兜底）
- 第 20 章：多 Agent 的动机与何时不该上多 Agent（先判断该不该上流水线）
- 第 21 章：对话式协作（需求不确定时先用对话，别急着固化 SOP）
- 第 23 章：协作拓扑与调度（阶段之间的拓扑与并行化）
- 第 24 章：Agent 间通信协议（跨进程的阶段交接契约）
- 第 29 章：检查点、回放与多 Agent 共享状态（文档版本与运行回放）
