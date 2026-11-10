# ch04 ReAct 最小可实现

对应《开源 Agent 架构技术全书》第 4 章《ReAct：推理与行动交替的经典范式》。

纯标准库实现，**不需要 API Key 就能跑通完整 ReAct 回路**：模型回复是脚本化的，工具是内置的假工具，但轨迹格式、动作解析、观察回灌、循环检测、停止条件全部是真实逻辑。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch04-react
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

### mock 模式（默认，无需 API Key）

```bash
python main.py
python main.py --task "阿尔伯特·爱因斯坦出生于哪一年？当时德国的首都是哪里？"
```

输出是一个完整轨迹：思考 → 动作 → 工具执行 → 观察回灌 → finish，最后打印 `status` 与 `answer`。

### 故障场景（演示守卫真的会生效）

| 命令 | 演示的机制 | 预期结果 |
| --- | --- | --- |
| `python main.py --scenario normal` | 正常三步 | `status: finished` |
| `python main.py --scenario loop` | 同一动作重复调用 | `status: loop_detected` |
| `python main.py --scenario drift` | 输出不带 Action 字段 | `status: parse_failed` |
| `python main.py --scenario recover` | 首轮格式错、次轮自我纠正 | `status: finished` |

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
python main.py --live --task "..."
```

Windows PowerShell：

```powershell
$env:LLM_API_KEY="sk-xxxxxxxx"
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="gpt-4o-mini"
python main.py --live
```

端点需兼容 `POST {base}/chat/completions`：请求体含 `model` 与 `messages`，返回体取 `choices[0].message.content`。换供应商时只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量，代码不用动。缺 `LLM_API_KEY` 时脚本会直接报错退出（exit code 2），不会带着空 Key 去请求。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--task` | 见脚本内 `DEFAULT_TASK` | 要完成的任务 |
| `--live` | 关 | 切真实模型 API |
| `--scenario` | `normal` | mock 场景：`normal` / `loop` / `drift` / `recover` |
| `--max-steps` | `8` | 步数上限 |
| `--obs-limit` | `600` | 观察截断长度（字符） |
| `--repeat-limit` | `2` | 同一动作重复几次判循环 |
| `--max-parse-fail` | `2` | 连续解析失败上限 |
| `--model` | 空 | 覆盖 `LLM_MODEL` |
| `--base-url` | 空 | 覆盖 `LLM_BASE_URL` |
| `--trace` | 关 | 打印每轮完整 prompt |

退出码：`0` 表示正常收尾（`finished`），`1` 表示被守卫或步数上限中止，`2` 表示参数/环境缺失。

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `parse_action()` | 动作解析的鲁棒性：正则 + 从后往前取合法工具名 + JSON 兜底，禁止 eval |
| `sanitize_observation()` | 观察净化：行首保留标签改写 + 长度截断（保留头尾） |
| `ReActAgent.run()` | ReAct 循环骨架与四个停止条件 |
| `MockLLM` | 脚本化的假模型，按轨迹状态决定下一轮输出 |
| `LiveLLM` | OpenAI 兼容端点的真实调用（`urllib`，无第三方依赖） |

## 复盘要点

- 观察是不可信数据，不是指令：工具返回里的 `Action:` 会被改写前缀，否则下一轮会被模型当成自己的动作执行。
- 四个停止条件都要有：`finish`、步数上限、循环检测、连续解析失败上限。只写 `finish` 的实现上生产会卡死。
- 同一 `(action, args)` 重复计数是最省事的循环检测，比比较动作序列更好用。
- 解析失败不要用 `eval` 兜底，那等于把提示注入变成 RCE。
- 跑真实模型前先跑一遍 `--scenario loop` 和 `--scenario drift`，确认守卫逻辑本身没写错。

## 相关章节

- 第 8 章：工具调用契约（把 Function Calling 当作 API 工程）
- 第 9 章：代码即行动（Code-Act 与 Program-of-Thought）
- 第 10 章：上下文工程（观察截断的代价核算）
- 第 11 章：停止准则、步数上限与成本守卫
