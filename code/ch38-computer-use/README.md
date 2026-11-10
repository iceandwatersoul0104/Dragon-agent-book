# ch38 计算机使用与 GUI Agent —— 配套示例

《开源 Agent 架构技术全书》第 38 章的参考实现。一个纯标准库的"伪桌面"，把 GUI Agent 的技术现实一次跑出来：

1. **三条感知通道**：可访问性树（AX 树，只有视口内元素）、低分辨率像素（字符画面）、结构化定位；
2. **grounding**：在画面里找标签文本算出坐标，把"点 Search 按钮"变成一对 (x, y)；
3. **动作空间**：`observe` / `screenshot` / `click_xy` / `click_ref` / `type` / `key` / `scroll` / `done`，原子动作与半结构化动作并存；
4. **客观验证**：不看截图，直接查应用状态字段（订单号、状态、草稿数、确认框是否还在）；
5. **不可逆闸门**：删除、关闭这类动作必须显式带 `confirm=true`，否则被拒绝。

界面用 ASCII 画是为了让坐标和字符网格严格对齐——坐标就是真实的行列号，读者可以自己数。

## 依赖

- Python **3.8+**，**只用标准库**，无需 `pip install`；
- 默认 mock 模式**不需要任何 API Key**；
- `--live` 才需要 `OPENAI_API_KEY`（任意 OpenAI 兼容端点）。

## 安装

```bash
cd code/ch38-computer-use
python -c "import sys; print(sys.version)"
```

Windows 下用 `py -3` 代替 `python`。没有安装步骤——不依赖第三方包。

## 运行

```bash
# 1) 默认：mock 模型跑完整回路（23 步左右），末尾打印客观验证
python main.py

# 2) 只看伪桌面的画面与可访问性树
python main.py --render

# 3) 真实模型（工具、闸门、验证与 mock 模式完全一致）
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.openai.com/v1     # 可选，任意 OpenAI 兼容端点
export OPENAI_MODEL=gpt-4o-mini                       # 可选
python main.py --live
```

主要参数：`--render`、`--max-steps`、`--live`、`--model`、`--base-url`。

退出码：`0` = 全部状态断言通过；`1` = 至少一条状态断言没过；`2` = 参数/环境不对。

## 一次运行演出来的六个坑

| # | 现象 | 暴露的技术现实 | 程序里的位置 |
|---|---|---|---|
| 1 | 本要点 Search，点中 Reset，订单号被清空 | 像素级脆弱：坐标偏一行就点到隔壁控件 | `MockModel` 的视觉通道带系统性 y 偏移 |
| 2 | 输入框只有下划线，视觉 grounding 无从下手 | 视觉的适用边界：没有可见文字就没法定位 | `ground()` 只认画面里能看到的标签 |
| 3 | Delete Draft 在视口外，树里查不到 | 视口陷阱：虚拟滚动只暴露可见元素 | `App.visible_elements()` 按视口过滤 |
| 4 | 坐标 (33, 5) 处没有可交互元素 | 点空：偏出去的坐标不会报错，只会什么都不发生 | `App.hit()` 返回 None |
| 5 | 确认框里 OK 与 Cancel 相邻，视觉点中 Cancel，删除没生效 | 不可逆动作的代价：点错一步可能丢数据 | `App.click()` 的 Cancel 分支 |
| 6 | 旧 ref 在重渲染后指向别的元素；不可逆动作缺 `confirm=true` | ref 会失效；不可逆动作需要独立闸门 | `Toolbox.click_ref()` 的 ref 校验 + `App.click()` 的 `IRREVERSIBLE` 检查 |

其中第 1 条和第 5 条用的是同一个系统性偏移（y 整体偏一行）：这个偏移只有在目标正下方紧挨着另一个控件时才会造成误伤——系统性偏差比随机噪声更危险，随机噪声至少偶尔蒙对，系统性偏差会稳定地错。

第 6 条值得单独看：模型拿着上一帧的 ref `b2` 去点，而这一帧的 `b2` 已经指向 Reset 了。工具层把它拦下来：

```
[步骤 19] click_ref -> 错误：ref b2 已失效（observe 时它是 'OK'，现在指向 'Reset'），请重新 observe 再点
```

没有这道校验，一次"重试"就会把订单号清空。

## 与正文的对应关系

| 正文位置 | 代码位置 |
|---|---|
| 2.1 三条感知通道 | `App.tree()` / `App.screen()` / `Toolbox.click_ref()` |
| 2.2 分辨率与视觉成本 | `App.screen()`（52×8 字符画面即低分辨率像素） |
| 2.3 grounding | `ground()`：标签文本 → (x, y) |
| 2.4 动作空间：原子 vs 半结构化 | `click_xy` vs `click_ref` |
| 2.5 状态与不可观测 | `App` 的状态字段与 `App.verify()` |
| 2.6 失败模式 | `App.hit()` / `App.click()` / `Toolbox.click_ref()` |
| 2.7 混合策略 | `MockModel._alt_action()`：视觉失败就换结构 |
| 3.4 不可逆动作要设闸 | `IRREVERSIBLE` + `confirm=true` |
| 4 动手 | `run_task()` 主循环 |

## 已知限制

- 字符画面不是真图像：它模拟的是"低分辨率、语义全丢、控件挨得近"这几个关键特征，不涉及真实渲染、抗锯齿、动画；
- grounding 是模板匹配，不是模型：它只在画面里找字面标签，认不出图标按钮和自绘控件；
- 视口只有一屏高、只有一个滚动方向，没有分页和懒加载；
- 没有真实的键盘输入和焦点管理（`type` 只写当前聚焦的输入框，`key` 只处理 Enter）；
- mock 模型按固定剧本驱动，用来看清失败模式与恢复路径，不具备泛化能力；
- 没有真并发、没有 token 计费、没有超时控制、没有沙箱；
- 观察用 `role: user` 回灌而非 `role: tool`，是为了兼容只实现基础 chat/completions 的端点。
