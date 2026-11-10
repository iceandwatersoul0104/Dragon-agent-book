# ch37 代码智能体与仓库级自治 —— 配套示例

《开源 Agent 架构技术全书》第 37 章的参考实现。一个能真正演示"仓库级代码智能体"最小回路的程序：

1. **仓库索引**：扫描代码文件（按扩展名过滤，跳过二进制与大文件），用正则抽取符号（def/class/function/fn/type/method），统计引用热度；
2. **仓库地图**：把"文件 → 符号签名"压成一张按引用热度排序的地图，只放签名不放实现体，用 `--map-lines` 控制预算；
3. **动作协议**：模型每轮只输出一个 JSON 动作，执行器只认白名单：`list_files` / `search` / `read_file` / `read_symbols` / `apply_patch` / `run_tests` / `done`；
4. **补丁机制**：SEARCH/REPLACE 精确替换，锚点找不到或不唯一都返回失败，逼模型读回真实内容再重试；
5. **验证闸门**：测试不过不许 `done`；收工时再跑一次测试做最终确认，不过就是失败退出；
6. **mock 模型**：动作序列由仓库里的 `.code-agent-task.json` 驱动，在没有 API Key 的情况下把上面的脚手架完整跑通；`--live` 换成真实模型，脚本、工具、闸门完全不变。

## 依赖

- Python **3.8+**，**只用标准库**，无需 `pip install`；
- 默认 mock 模式**不需要任何 API Key**；
- `--live` 才需要 `OPENAI_API_KEY`（任意 OpenAI 兼容端点）。

## 安装

```bash
cd code/ch37-coding-agents
python -c "import sys; print(sys.version)"
```

Windows 下用 `py -3` 代替 `python`。没有安装步骤——不依赖第三方包。

## 运行

```bash
# 1) 默认：mock 模型 + 内置 demo 仓库，完整跑通检索→定位→打补丁→跑测试→收口验证
python main.py

# 2) 只看仓库地图（不跑智能体）
python main.py --show-map

# 3) 换自己的仓库：仓库根目录 + 任务描述
#    mock 模式下还需要一个 .code-agent-task.json 描述要改什么
python main.py --repo /path/to/repo --task "把 add() 多加的 1 去掉"

# 4) 真实 API（脚本、工具、闸门与 mock 模式完全一致）
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.openai.com/v1     # 可选，任意 OpenAI 兼容端点
export OPENAI_MODEL=gpt-4o-mini                       # 可选
python main.py --repo /path/to/repo --live
```

主要参数：`--repo`、`--task`、`--task-file`、`--test-cmd`、`--map-lines`、`--max-steps`、`--in-place`、`--show-map`、`--live`、`--model`、`--base-url`。

退出码：`0` = 收口验证通过（测试是绿的）；`1` = 任务未完成或收口验证失败；`2` = 参数/环境不对。

**关于工作目录**：默认会把 `--repo` 复制到临时目录再动手，保证每次演示结果一致、不会把工作区改坏；加 `--in-place` 才直接改原目录。临时副本的路径会打印在第一行，方便你去看改完的文件。

## `--live` 所需环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `OPENAI_API_KEY` | 是 | — | OpenAI 兼容接口的密钥 |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | 接口根地址，可指向任意兼容端点或本地网关 |
| `OPENAI_MODEL` | 否 | `gpt-4o-mini` | 模型名 |

## demo 仓库里埋了什么

`demo_repo/calc.py` 的 `add()` 多加了一次 1。同一段 `return a + b + 1` 在整个文件里出现了两次——`add()` 里一次，`add_legacy()` 里一次。

于是第一次打补丁（按记忆里的片段猜锚点）会撞上唯一性检查：

```
[步骤 3] apply_patch -> 错误：待替换片段不唯一（出现 2 次），请扩大上下文
```

mock 模型随后按 2.5 节那条降级链回读文件真实内容，用"所在函数签名 + 目标行"组成更大的锚点，补丁才落下去：

```
[步骤 4] read_file     -> --- calc.py（第 1-20 行，共 20 行）---
[步骤 5] apply_patch   -> 已应用补丁：calc.py（1 处）
[步骤 6] run_tests     -> 测试通过（退出码 0）
[步骤 7] 模型宣布完成：已修复 calc.py，测试通过
[收口验证] 测试通过（退出码 0）
```

这一段是本示例的看点：**唯一性检查不是摆设，它把"改错地方"挡在门外**。没有它，这个 demo 会把 `add_legacy()` 改掉——测试照样是绿的，bug 还在。

## 与正文的对应关系

| 正文位置 | 代码位置 |
|---|---|
| 2.1 动作-观察闭环 | `run_task()` 主循环 |
| 2.2 五种形态的动作空间 | `Toolbox.dispatch()` 的白名单动作 |
| 2.3 仓库地图 / 符号索引 | `RepoIndex.repo_map()`、`RepoIndex.search()` |
| 2.4 搜索策略 | `RepoIndex.search()`（grep 式召回）+ `RepoIndex._count_refs()`（热度排序） |
| 2.5 补丁降级链 | `parse_patch()` / `apply_blocks()` + `MockModel._patch_action()` 的回读重试与扩大上下文 |
| 2.6 验证回路与闸门 | `Toolbox.run_tests()` + `run_task()` 收尾的收口验证 |
| 2.7 脚手架的四件事 | `MockModel` 只产生动作，检索 / 排序 / 裁剪 / 回灌都在 `Toolbox` 里 |
| 4.4 主循环伪代码 | `run_task()` 与正文 4.4 的伪代码一一对应 |

## 已知限制

- 符号抽取用的是正则，不是 tree-sitter：只覆盖常见语言形态，模板密集、宏密集或动态派发的代码会抽不全，而抽不全的地图比没有地图更危险；
- 引用热度只是"名字在源码里出现多少次"，不是真正的调用图，也没算 import 关系；
- `search` 是朴素的逐行包含匹配，不支持正则、不支持跨文件语义检索；
- 补丁只支持 SEARCH/REPLACE 单文件多块，不支持跨文件原子补丁，也没有 git 回滚（本示例不碰 git）；
- mock 模型不是模型：它只按任务文件驱动动作序列，用来验证脚手架，不具备任何泛化能力；
- 观察用 `role: user` 回灌而非 `role: tool`，是为了兼容只实现基础 chat/completions 的端点；
- 没有并行、没有 token 计费、没有重试退避、没有沙箱（`run_tests` 直接在工作目录里跑，命令可控性依赖使用者自己）。
