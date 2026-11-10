# ch06 反思与自纠错最小可实现

对应《开源 Agent 架构技术全书》第 6 章《反思与自纠错：Reflexion、Self-Refine 与独立评审者》。

纯标准库实现，**不需要 API Key 就能跑通完整回路**，一次跑清三件事：

- 三种反思形态：`reflexion`（带记忆重试）/ `self_refine`（多轮改写）/ `judge`（独立评审者）
- 三种反思信号：`rule`（真跑测试）/ `llm`（同一个模型自评）/ `judge`（独立评审者）
- 四道熔断闸：最大轮次、无进展检测、反思去重、成本预算

## 依赖

- Python 3.9 及以上（开发与验证使用 3.13）
- 零第三方依赖，不需要 `pip install`
- 不需要 API Key（mock 模式）

## 安装

无需安装，拿到目录直接跑：

```bash
cd code/ch06-reflection
python main.py
```

Windows 上如果 `python` 不可用，换成 `py -3 main.py`。

## 运行

### mock 模式（默认，无需 API Key）

```bash
python main.py                                   # 默认：judge 形态 + 规则信号
python main.py --form reflexion --critic rule    # 带记忆重试
python main.py --form self_refine --critic rule  # 多轮改写
python main.py --form judge --critic llm         # 自评 -> 无进展熔断
python main.py --form judge --critic judge       # 独立评审者定位缺陷
python main.py --trace                           # 打印每轮代码与评审意见
```

典型输出：

```
form        : judge
critic      : rule
status      : solved
stop_reason : critic_passed
rounds      : 2
chars       : 265  (cost proxy)
```

### 三种信号的对照结果

| form | critic | status | stop_reason | 说明 |
| --- | --- | --- | --- | --- |
| judge | rule | solved | critic_passed | 测试直接指出失败用例，产物重写后通过 |
| judge | judge | solved | critic_passed | 独立评审定位到"未做归一化" |
| judge | llm | not_solved | no_progress | 自评给不出可定位缺陷，产物原地不动，熔断 |
| reflexion | rule | solved | critic_passed | 反思写进记忆，下一轮重写时用上 |
| self_refine | rule | solved | critic_passed | 反馈就地改写当前产物 |

### 状态语义

| status | 含义 |
| --- | --- |
| `solved` | 循环以评审通过结束，且**规则校验**也确认产物正确 |
| `not_solved` | 被某道熔断闸拦下，产物仍未通过规则校验 |
| `false_pass` | 评审放行了，但规则校验发现产物是错的——最危险的一种失败 |

`stop_reason` 取值：`critic_passed` / `no_progress` / `repeated_reflection` / `max_rounds` / `budget_exceeded`。

## mock 到底做了什么（重要，别误读）

内置的"模型"**不是真的在推理**，它是一个行为模拟器：按提示里的特征决定返回什么。

- `author`：提示里出现"可定位的失败信息"（`AssertionError` / `归一化` 等）时给修正版，否则给带 bug 的版本；
- `critic`（自评）：永远只给出"建议再检查一下边界情况"这类**不可定位**的意见，模拟确认偏见；
- `judge`（独立评审者）：检查候选代码有没有做归一化，能指出具体缺陷；
- `reflect`：把失败信息转成一句反思。

**但规则评审是真的**：它真的在受限命名空间里执行候选代码并跑 6 条断言，判定完全确定。

> 安全提示：脚本用 `exec` 执行候选代码来跑断言，仅适用于本例的玩具代码。
> 生产环境必须换成真正的沙箱（容器 / 受限进程），不要在主进程里执行模型产出的代码。

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
python main.py --live --critic rule --max-rounds 3
```

Windows PowerShell：

```powershell
$env:LLM_API_KEY="sk-xxxxxxxx"
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="gpt-4o-mini"
python main.py --live --critic rule
```

端点需兼容 `POST {base}/chat/completions`：请求体含 `model` 与 `messages`，返回体取
`choices[0].message.content`。换供应商时只改 `LLM_BASE_URL` 与 `LLM_MODEL` 两个变量。
缺 `LLM_API_KEY` 时脚本直接报错退出（exit code 2）。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--form` | `judge` | 反思形态：`reflexion` / `self_refine` / `judge` |
| `--critic` | `rule` | 反思信号：`rule` / `llm` / `judge` |
| `--max-rounds` | `3` | 最大轮次 |
| `--char-budget` | `20000` | 成本预算（输出字符数，token 的代理指标） |
| `--live` | 关 | 切真实模型 API |
| `--temperature` | `0.0` | live 模式采样温度 |
| `--model` | 空 | 覆盖 `LLM_MODEL` |
| `--base-url` | 空 | 覆盖 `LLM_BASE_URL` |
| `--trace` | 关 | 打印每轮代码与评审意见 |

退出码：`0` 表示最终产物通过规则校验（`solved`），`1` 表示未解决，`2` 表示缺 `LLM_API_KEY`。

## 四道熔断闸怎么对应

| 闸 | 代码位置 | 触发条件 |
| --- | --- | --- |
| 最大轮次 | `index >= self.max_rounds` | 轮次用完 |
| 无进展检测 | `code_hash(code) in seen_code` | 产物哈希没变 |
| 反思去重 | `normalize_reflection(critique) in seen_reflections` | 规范化后与之前的反思重复 |
| 成本预算 | `result.total_chars > self.char_budget` | 累计输出字符超预算 |

注意判重用的是**规范化后**的反思文本（压空白、去标点、忽略大小写），不是字符串完全相等——
真实模型每轮换个说法，字符串永远不相等。

在 mock 的自评场景里，先触发的是"无进展"（产物压根没变）；"反思去重"是第二道防线，
要在产物会变、但评审意见始终重复时才会触发。

## 代码结构

| 位置 | 对应章节的构件 |
| --- | --- |
| `run_tests()` | 规则信号：真执行候选代码跑断言（确定性信号源） |
| `build_author_prompt()` | 三种形态的差别：反思走记忆、多轮改写就地修订、独立评审拿结构化反馈 |
| `MockLLM` | 行为模拟器，演示确认偏见与无进展熔断 |
| `LiveLLM` | OpenAI 兼容端点，按角色切换系统提示 |
| `ReflectionLoop.run()` | 反思循环 + 四道熔断闸 + 终局 ground truth 校验 |
| `parse_verdict()` | 把评审文本解析成通过/不通过，判不准时按不通过处理 |

## 真实项目里怎么用

1. **优先上规则信号**：能编译、能量化、能跑断言的错误，不要交给 LLM 评审。
2. **评审者与生成者分离**：换系统提示、换模型，最少也要换上下文（不给作者的解释）。
3. **评审输出要可核验**：要求"通过/不通过 + 具体位置 + 依据"，不要"看起来不错"。
4. **把熔断写进框架**：最大轮次、无进展、反思去重、成本预算，四道都要有默认值。
5. **熔断后要有出路**：转人工、走兜底路径，或者把失败证据完整交出去。

## 相关章节

- 第 4 章：ReAct（反思循环与交错推理的区别）
- 第 5 章：提示增强技术（评审提示怎么写）
- 第 7 章：规划范式（规划节点本身就是一种"外部信号"）
- 第 10 章：上下文工程（反思记忆占上下文）
- 第 11 章：停止准则、步数上限与成本守卫
