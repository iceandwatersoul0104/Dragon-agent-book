# ch09 代码即行动（Code-Act）最小可实现

对应《开源 Agent 架构技术全书》第 9 章《代码即行动：Code-Act 与 Program-of-Thought》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**，一次跑清五件事：

- **表达力**：模型写程序调工具，用循环筛选/排序/求和——工具调用表达不了的部分；
- **状态保持**：同一命名空间跨轮复用变量；
- **输出注入**：stdout、最后一个表达式的值、traceback 回灌给模型；
- **沙箱**：标准库白名单、无裸 `open`、网络与子进程被拦、行数预算与墙上时钟时限；
- **幻觉 API**：调用不存在的 `slack.post_message` → NameError → 错误回灌 → 模型自己改。

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch09-code-act
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

### mock 模式（默认，无需 API Key）

```bash
python main.py                 # 四轮演示
python main.py --trace         # 打印每轮代码与回灌内容
python main.py --timeout 1.0   # 墙上时钟时限（秒）
python main.py --max-lines 5000  # 行数预算
python main.py --max-turns 6   # 最多几轮
```

典型输出：

```
turns           : 5
tool_calls      : 3
sandbox_blocks  : 1
turn 1 | lines=31  | 命中 3 单，合计 1920.0
turn 2 | lines=128 | 沙箱拦截：模块 socket 不在允许列表里 / 报告已生成
turn 3 | lines=1   | File "<agent_code>", line 1, in <module> / NameError: name 'slack' is not defined
turn 4 | lines=5   | 已发送
turn 5 | lines=0   | (任务结束，模型不再输出代码)
```

四轮分别对应：

1. **表达力**：`search_orders` 拿回 5 条订单，用 `for` 循环筛选 `paid and amount > 100`、排序、求和——这套逻辑写成函数调用的参数根本放不下；
2. **状态保持 + 沙箱**：复用上一轮留在命名空间里的 `candidates` 生成报告并落盘；`import socket` 被白名单拦下，程序自己 `except SandboxError` 接住后继续跑完；
3. **幻觉 API**：调用不存在的 `slack.post_message`，NameError 回灌；
4. **自我纠正**：改用注册过的 `send_report`，完成。

注意 `tool_calls = 3`：`search_orders`、`write_report`、`send_report` 各调一次，**没有重复的副作用**。

### 把沙箱逼到极限

```bash
python main.py --max-lines 20            # 行数预算：执行中途被拦
python main.py --timeout 0.001           # 时限：几乎立刻超时
```

输出里会出现 `SandboxError: 超过行数预算（20 行）`，并且**下游拿不到上一步的变量**——
第 3 轮就会因为 `content` 没定义而失败。这正是"每一步都可能失败"的可观测证据。

### mock 说明

内置"模型"是按剧本返回四段程序，**不做真实推理**。但解释器、受限命名空间、白名单导入、行数预算、墙上时钟时限、输出注入、traceback 裁剪都是真实逻辑。

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
python main.py --live
```

端点需兼容 `POST {base}/chat/completions`：请求体含 `model` 与 `messages`，返回体取
`choices[0].message.content`。换供应商时只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

live 模式把可用函数的真实签名和 docstring 写进系统提示，要求模型输出 ```python 代码块；
没有围栏时整段当作代码处理。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--task` | 内置任务 | 换成自己的任务描述 |
| `--max-turns` | `6` | 最多几轮 |
| `--max-lines` | `5000` | 行数预算 |
| `--timeout` | `2.0` | 墙上时钟时限（秒） |
| `--live` | 关 | 切真实模型 API |
| `--temperature` | `0.0` | live 模式采样温度 |
| `--model` | 空 | 覆盖 `LLM_MODEL` |
| `--base-url` | 空 | 覆盖 `LLM_BASE_URL` |
| `--trace` | 关 | 打印每轮代码与回灌内容 |

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `safe_import()` | 标准库白名单：网络、进程、文件系统相关的模块进不来 |
| `workspace_read/write()` | 受控文件接口，不给裸 `open` |
| `CodeExecutor.run()` | 受限命名空间 + 行数预算 + 墙上时钟时限 + stdout 捕获 |
| `CodeExecutor._last_value()` | 最后一个表达式的值（含调用的行不重复执行） |
| `CodeExecutor.observation()` | 输出注入：stdout + value + 错误 |
| `trim_traceback()` | 只保留沙箱代码的帧，不泄漏宿主结构 |
| `MockLLM` | 四段脚本化程序 |
| `LiveLLM` | OpenAI 兼容端点，注入真实函数签名 |

## ⚠️ 安全提醒

**本示例不是真正的沙箱。** 它在主进程里 `exec` 模型写的代码，只做了：

- 内置函数白名单（没有 `open`、`eval`、`exec`、`input`、`subprocess`）；
- 标准库导入白名单；
- 工作目录路径限制；
- 行数预算与墙上时钟时限。

它**挡不住**通过属性链逃逸（`().__class__.__bases__…`）这类手法，也不能隔离 C 扩展的死循环、
内存耗尽和文件描述符泄漏。上生产必须换成：容器或虚拟机 + seccomp/非 root + 只读根文件系统 +
`--network none` + cgroup 资源限额 + 每次执行独立进程。

## 换成自己的工具时要改什么

1. 写工具函数，**docstring 要写清楚**——它是模型看到的唯一说明；
2. 把函数注册进 `TOOLS`，它们会自动注入命名空间；
3. 需要写文件就调 `workspace_write`，不要给裸 `open`；
4. 提示里的工具签名要和命名空间里的函数**完全一致**，否则模型会一直调不存在的 API；
5. 有副作用的工具要考虑幂等键（见第 8 章），别让重试重复发消息。

## 相关章节

- 第 4 章：ReAct（Observation 在这里就是代码执行结果）
- 第 7 章：规划范式（代码如何替代 `#E1` 这类变量引用）
- 第 8 章：工具调用契约（函数调用路线与代码路线的对照）
- 第 10 章：上下文工程（状态保持与上下文预算的取舍）
- 第 11 章：停止准则、步数上限与成本守卫
