# 第 2 章 · 工程骨架：目录、配置与错误处理约定

本卷第二章不写业务逻辑，只把地基打四根桩：**目录怎么分、配置从哪来、错误怎么分类、日志怎么写**。

- 正文：[`skeleton.md`](skeleton.md)
- 配套工程产物：`dragon-agent/` 下的四个新文件（见下）

## 本章给了什么

1. **四层依赖图**——应用层 / 编排层 / 工具层 / 模型层，加上 `config.py`、`errors.py`、`logging_setup.py` 三个叶子模块。依赖方向不可逆。
2. **`dragon_agent/errors.py`**——异常分类：四类运行期错误 + 一类启动期错误，带 `kind` / `retryable` / `details`，外加可查询的策略表。
3. **`dragon_agent/logging_setup.py`**——结构化日志：一行一条 JSON、`trace_id` 用 `contextvars` 贯穿、敏感字段在写出去之前脱敏。
4. **`examples/skeleton_hello.py`**——能跑的 hello-world：配置 → 日志 → 模型 → 工具 → 错误分类的完整闭环。
5. **`tests/test_skeleton_basics.py`**——25 条断言，守住上面每一条骨架约定。

**没有改动** `dragon_agent/config.py`、`llm.py`、`tools.py`，也没有改 `__init__.py`（那张导出表就是公共 API，改它必须同步改契约文档）。

## 怎么跑

```powershell
cd dragon-agent

# 1) hello-world：离线跑通，退出码 0
python examples/skeleton_hello.py

# 2) 指定 trace_id，日志会用同一个 id 串起来
python examples/skeleton_hello.py --trace-id my-trace-001

# 3) --live 缺 key：启动期早失败，退出码 2
python examples/skeleton_hello.py --live

# 4) 跑测试：25 个用例
python -m unittest tests.test_skeleton_basics -v
```

不需要 API Key，不联网，不装任何第三方包（测试用标准库 `unittest`）。

### 实跑结果

| 场景 | 结果 | 退出码 |
| --- | --- | --- |
| `python examples/skeleton_hello.py` | 4 行 JSON 日志（同一个 trace_id）+ 最终答案 | 0 |
| `python examples/skeleton_hello.py --live`（无 key） | 打印"配置不合法，已退出"并退出 | 2 |
| `python -m unittest tests.test_skeleton_basics` | `Ran 25 tests` / `OK` | 0 |

## 骨架约定速查

| 约定 | 落在哪 |
| --- | --- |
| 配置单一入口 | `Config.from_env_and_args()` 是唯一构造入口 |
| 配置不可变 | `frozen=True`，要改用 `dataclasses.replace` 重建 |
| 早失败 | `__post_init__` 校验预算与 key，非法就抛 `ConfigError` |
| 落盘不含密钥 | `Config.to_dict()` 摘掉 `api_key` |
| 错误分四类 | `ModelError` / `ToolError` / `OrchestrationError` / `DependencyError` |
| 不造同名分身 | `errors.ToolError is tools.ToolError` 恒为真 |
| 运行期失败用返回值 | `ToolResult(ok=False)`、`finish_reason="error"` |
| 异常层内传播 | 工具层边界把 `DependencyError` 转成 `ToolResult` 回灌模型 |
| 日志固定字段 | `ts / level / logger / msg / trace_id` |
| trace_id 贯穿 | `trace_scope()` + `contextvars` |
| 写入前脱敏 | `redact()` 处理键名，`redact_text()` 处理正文 |

## 局限（这一章不等于可以上线）

- **"catch DragonError 兜住全部框架异常"不成立。** `ConfigError`（`config.py:16`）和 `tools.ToolError`（`tools.py:18`）都直接继承 `Exception`，不是 `DragonError` 子类；`classify()` 靠两条硬编码的 isinstance 特判（`errors.py:167-174`）把它们捡回来——自己新增一个框架异常类不会自动进分类表，一律落到 `"unknown"`（不重试、结束任务）。
- **重试性有两个真相来源。** `policy_for()` 读的是策略表（`errors.py:178-180`），不看异常自己的 `retryable`；实例级覆盖（`errors.py:92-93`）能让 `exc.retryable` 变成 False，`policy_for()` 却仍按分类表返回 True——按字段判断与按类判断会给出不同答案。
- **参数写错和没传无法区分。** `from_env_and_args()` 用的是 `parse_known_args`（`config.py:74`），未知参数连 `--help` 一起被静默吞掉——`--max-steps` 少写一个连字符，会安静地退回默认 12。
- **单价表进不了 `Config.from_env_and_args()`。** 这个入口构造时不传 `price_in_per_mtok` / `price_out_per_mtok`（`config.py:83-93`），两个字段默认 0.0，环境变量与命令行参数里都没有价格项——从这条入口拿到的配置，成本恒为 0，`cost_budget` 永远不会触发（`agent.py:501-511`）。
- **脱敏只在 JSON 那一条路径上，扩展字段写不进去还会整条日志一起丢。** `configure_logging(json_output=False)` 换成了裸 `logging.Formatter`（`logging_setup.py:194-200`），`redact_text` 只在 `JsonFormatter.format` 里被调（`:162`）——非 JSON 模式下密钥形态原样写出去；而 `redact()` 只认 dict/list/tuple/str（`:140-142`），别的对象进 JSON 扩展字段会让 `json.dumps`（`:171`）抛错，整条记录连同扩展字段一起消失。
- **日志出口只有一个。** `propagate = False` 加单一 stderr handler（`logging_setup.py:189-194`）——宿主应用的日志配置收不到框架日志，文件输出、轮转、异步队列都得自建。

## 与本书其它章节的关系

- 第二卷第 3 章 `p03-llm-client`：`llm.py` 的 mock 与真实客户端抽象。
- 第二卷第 4 章 `p04-tool-layer`：工具层的参数校验与错误回灌。
- 第二卷第 12 章 `p12-observability`：`observability.py` 的 trace 与成本账，与本章共用同一个 `trace_id`。
- 第二卷第 14 章 `p14-production`：配置与密钥管理、值班手册。
- 第一卷第 3、8、11、33、36 章：过度工程化、工具契约、停止准则、可观测性、生产化检查清单。
