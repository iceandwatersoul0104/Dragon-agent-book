# ch28-context-windowing —— 上下文窗口工程最小可实现

第 28 章《上下文窗口工程：检索、压缩、淘汰与分层》的配套代码。

把「分层检索 / 历史压缩 / 价值淘汰 / Prompt Caching 布局」四种操作放进一个循环里跑，每轮打印各自的实际指标：召回了什么、压缩丢了什么、淘汰了谁、前缀缓存命中多少。

## 目录

```
ch28-context-windowing/
├── main.py             # 四种操作 + 完整回路
└── README.md           # 本文件
```

## 依赖

- Python 3.9 或更高
- **仅使用标准库**，不需要 `pip install` 任何东西
- `--live` 模式需要能访问 OpenAI 兼容的 chat/completions 接口（可选）

## 安装

没有安装步骤，下载后直接跑：

```bash
cd ch28-context-windowing
python main.py
```

Windows 控制台若出现中文乱码：

```powershell
$env:PYTHONIOENCODING="utf-8"; python main.py
```

## 运行命令

| 命令 | 作用 |
|---|---|
| `python main.py` | 跑完整语料（4 轮陈述 + 2 轮提问），打印每轮四项操作的指标 |
| `python main.py --query "现在预算是多少？"` | 用自定义问题替换最后一轮提问 |
| `python main.py --live` | 最终回答改由真实 LLM 生成（其余逻辑不变） |

## `--live` 所需环境变量

| 变量 | 必需 | 默认值 | 说明 |
|---|---|---|---|
| `OPENAI_API_KEY` | 是 | 无 | 未设置时打印告警并自动回退 mock |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | 任意 OpenAI 兼容网关 |
| `OPENAI_MODEL` | 否 | `gpt-4o-mini` | 生成回答用的模型 |

```powershell
$env:OPENAI_API_KEY="sk-..."
python main.py --live
```

抽取或生成失败（网络错误、返回不是合法 JSON）时会打印告警并回退，不会让链路挂掉。

## 输出怎么读

每轮提问打印五段：

- **[① 分层检索]** —— `core` 常驻约束、`recall` 历史命中、`archival` 归档资料。缺口检测按「问句要的量值类型（时长/金额/时间点）当前层有没有出现」判断是否下沉到归档层。
- **[② 历史压缩]** —— 滑窗、滚动摘要、抽取式三种算法对同一段历史的结果对比：`tokens`、事实保留率、以及同一份内容分别回答「现在预算是多少」和「最初的预算是多少」的结果。压缩算法选错，答案会直接错。
- **[③ 价值淘汰]** —— 按相关度 / 新鲜度 / 未闭合约束 / 来源可信度打分，按 token 预算保留。被新事实覆盖的旧值默认排除在上下文之外，只有当用户明确追问历史时才请回来。
- **[④ Prompt Caching]** —— 三种布局的前缀命中率：`retrieval_first`（检索结果放最前）、`retrieval_last`（检索结果放最后）、`history_rewritten`（历史每轮整体重写）。
- **[⑤ 最终回答]** —— 用保留下来的上下文拼出的 prompt 得到的答案。

## 关键实现点

| 操作 | 函数 / 类 | 说明 |
|---|---|---|
| 分层检索 | `LayeredRetriever.retrieve()` | core 按主题槽位常驻，同主题新值覆盖旧值；recall 按相关度命中；缺口检测决定是否下沉 archival |
| 压缩 | `compress_sliding_window()` / `compress_rolling_summary()` / `compress_extractive()` | 滑窗留最近 N 轮；滚动摘要只保留首次值（粘性旧值）；抽取式只留带约束信号的子句 |
| 价值淘汰 | `ValueEvictor.score()` / `.evict()` | 四维加权打分 + token 预算；同文本去重；`stale` 项 obligation 归零并降权 |
| 缓存布局 | `PromptCache.observe()` / `build_layout()` | 伪 token 序列比对最长公共前缀，得出前缀命中率 |

## 局限（生产化时要换掉什么）

1. token 计数是近似值（中文按字、拉丁按 4 字符），真实分词器与服务商计费口径不同。
2. 相关度用字符集合的重合度，不是语义相似度。
3. 事实抽取、约束识别、槽位判定都是规则，没有模型参与。
4. 缓存是本地模拟，没有真实的最小缓存单元粒度与过期时间。
5. 淘汰只做了一次线性打分，没有做「被淘汰项是否仍被其他项依赖」的引用检查。

生产环境对应替换：真实 tokenizer 与计价口径、语义检索或重排模型、模型辅助的约束抽取、供应商的缓存接口与 TTL、以及带引用计数的上下文依赖图。其余接口不变。

## 对应章节

正文见 `../../vol1/part-4/ch28-context-windowing.md`。
