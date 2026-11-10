# ch27-long-term-memory —— 三条长期记忆后端路线的最小可实现

第 27 章《长期记忆实现：向量、摘要与实体图谱三条路线》的配套代码。

同一个问题（"项目预算是多少？"）分别喂给三条长期记忆后端，把它们的答案、盲区和成本摆在一起对照。

## 目录

```
ch27-long-term-memory/
├── main.py      # 三条后端 + 混合仲裁, 全部实现
└── README.md    # 本文件
```

## 依赖

- Python 3.9 或更高
- **仅使用标准库**，不需要 `pip install` 任何东西
- `--live` 模式需要能访问 OpenAI 兼容的 chat/completions 接口（可选）

## 安装

没有安装步骤，克隆或下载后直接跑：

```bash
cd ch27-long-term-memory
python main.py
```

Windows 下如果控制台出现中文乱码，先设一下编码：

```powershell
$env:PYTHONIOENCODING="utf-8"; python main.py
```

## 运行命令

| 命令 | 作用 |
|---|---|
| `python main.py` | 载入内置语料，对三个问题依次打印三条路线的回答 |
| `python main.py --query "项目预算是多少？"` | 只对指定问题跑一次对照 |
| `python main.py --live` | 用真实 LLM 做事实抽取（其余逻辑不变） |
| `python main.py --live --query "Orion 现在的预算是多少？"` | 组合使用 |

## `--live` 所需环境变量

| 变量 | 必需 | 默认值 | 说明 |
|---|---|---|---|
| `OPENAI_API_KEY` | 是 | 无 | 未设置时会打印告警并自动回退到 mock |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | 换成任意 OpenAI 兼容网关 |
| `OPENAI_MODEL` | 否 | `gpt-4o-mini` | 抽取用的模型 |

示例（PowerShell）：

```powershell
$env:OPENAI_API_KEY="sk-..."
$env:OPENAI_BASE_URL="https://api.openai.com/v1"
$env:OPENAI_MODEL="gpt-4o-mini"
python main.py --live
```

抽取失败（网络错误、返回不是合法 JSON）时会打印告警并自动回退到规则抽取，不会让整条链路挂掉。

## 输出怎么读

每个问题会打印四段：

- **[向量路线]** —— 只按余弦相似度排序的 top-3 块。故意不加任何过滤，你会看到它把另一个项目（Argus）的块排在第一位，也会看到"一开始"这种时间指代被忽略。
- **[摘要路线]** —— 滚动摘要的内容、命中项和漂移记录。摘要会粘住首次见到的旧值（Orion 预算仍写着 8万元），这就是摘要漂移。
- **[图谱路线]** —— 实体 + 带有效期的边。旧边不会被删除，只会被打上失效时间；`bi-temporal` 行给出"事件时间轴"和"事务时间轴"两个视角的查询结果。
- **[混合]** —— 图谱定事实（带有效期），向量补证据原文，摘要补脉络；提问没指明项目时给出消歧警告。

## 三条路线的关键实现点

| 路线 | 关键函数 | 说明 |
|---|---|---|
| 向量 | `chunk_text()` / `embed()` / `VectorStore.search()` | 句末标点切句 + 重叠窗口；`must_contain` 模拟元数据过滤；同分时按时间倒序，复刻"永远召回最新那条"的默认行为 |
| 摘要 | `SummaryStore._compress()` | 摘要只保留首次见到的值，后续变更不更新；`ledger` 存结构化事实与历史，`drift` 记录每次漂移 |
| 图谱 | `GraphStore.assert_fact()` / `query()` | 新事实写入时给旧边补 `valid_to` / `invalidated_at`；`as_of` 查事件时间轴，`knew_at` 查事务时间轴 |

## 局限（生产化时要换掉什么）

1. `embed()` 是确定性哈希投影，**不是语义模型**。它只复刻了向量路线"按字面相似度排序"的盲区，不复刻真实 embedding 的语义泛化能力。
2. 事实抽取用的是正则规则（`extract_mock`），只覆盖预算、错误码、上线时间三类。
3. 图谱没有真正的实体消歧，实体名靠语料里显式给出；没有向量检索的持久化索引，也没有并发写入控制。
4. 摘要不是 LLM 生成的滚动摘要，而是"话题 + 首次值"的确定性压缩，目的是让漂移可复现。

生产环境的对应替换：真实 embedding 模型 + 向量库、实体消歧与写入队列、LLM 滚动摘要 + 结构化事实台账、图数据库的边失效事务。四条路线的接口（`ingest` / `search` / `describe`）不用改。

## 对应章节

正文见 `../../vol1/part-4/ch27-long-term-memory.md`。
