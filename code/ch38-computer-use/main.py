#!/usr/bin/env python3
"""ch38 计算机使用与 GUI Agent —— 最小可实现参考实现。

一个纯标准库的"伪桌面"，用来演示 GUI Agent 的四个技术现实：

    1. 感知有三条通道：可访问性树（AX 树）、低分辨率像素（字符画面）、结构化定位。
    2. grounding 把"点某个按钮"变成坐标；坐标偏了，就会点到隔壁控件。
    3. 视口外的元素不在结构里（虚拟滚动），必须先滚动再谈定位。
    4. 动作不可逆：删除没有撤销，必须过一道显式确认。

演示剧本（mock 模型驱动）：

    [Search] 与 [Reset] 上下相邻，模型输出的 y 整体偏一行 → 点中 Reset，订单号被清空；
        模型从动作效果回报里发现意图与实际不符 → 改用可访问性树定位（半结构化动作）。
    输入框里只有下划线，没有可见文字 → 视觉 grounding 根本无从下手 → 只能靠结构。
    [Delete Draft] 在视口外 → 树里查不到 → 先滚动再定位。
    确认框里 [OK] 与 [Cancel] 上下相邻 → 视觉偏一行 → 点中 Cancel，删除没生效；
        状态断言发现草稿还在 → 重新打开 → 这次走结构定位，并且带上 confirm=true。

验证方式：不看截图，直接查应用状态字段（订单号、状态、草稿数、弹窗是否还在）。

依赖：仅 Python 标准库（>= 3.8）。默认 mock 模式不需要任何 API Key。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass


WIDTH = 52          # 画面宽度（字符）
VIS = 6             # 视口内可见的文档行数
CHROME = 2          # 分隔线 + 状态栏
IRREVERSIBLE = {"ok", "close"}   # 不可逆动作：必须显式 confirm=true


# ---------------------------------------------------------------------------
# 伪桌面：应用状态与元素表
# ---------------------------------------------------------------------------


@dataclass
class Element:
    eid: str
    role: str
    name: str
    row: int          # 文档行（对话框元素用屏幕行）
    col: int          # 起始列
    width: int        # 可点击区域宽度
    value: str = ""   # 仅状态类元素使用


STATIC_ELEMENTS: List[Element] = [
    Element("close", "button", "Close", 0, 37, 3),
    Element("minimize", "button", "Minimize", 0, 41, 3),
    Element("order_input", "textbox", "Order No", 1, 10, 22),
    Element("search", "button", "Search", 2, 10, 8),
    Element("reset", "button", "Reset", 3, 10, 7),
    Element("status", "status", "Status", 4, 0, 0),
    Element("drafts", "status", "Drafts", 5, 0, 0),
    Element("delete_draft", "button", "Delete Draft", 7, 26, 14),
    Element("export", "button", "Export Report", 11, 26, 15),
]

DIALOG_ELEMENTS: List[Element] = [
    Element("ok", "button", "OK", 2, 16, 6),
    Element("cancel", "button", "Cancel", 3, 16, 10),
]

DIALOG_ROWS = [
    "  Delete this draft? This cannot be undone.          ",
    "  ------------------------------------------------",
    "                [ OK ]                              ",
    "                [ Cancel ]                          ",
]


class App:
    """一个只有输入框、两个相邻按钮、状态行和删除草稿流程的最小应用。"""

    def __init__(self) -> None:
        self.order_no = ""
        self.status = "PENDING"
        self.drafts = 1
        self.offset = 0
        self.dialog_open = False
        self.focus: Optional[str] = None
        self.closed = False
        self.exported = 0
        self.actions: List[str] = []

    # -- 渲染 -------------------------------------------------------------
    def doc_row(self, r: int) -> str:
        if r == 0:
            return "Order Query Backend".ljust(37) + "[X] [-]"
        if r == 1:
            return "Order No: [" + self.order_no.ljust(20)[:20] + "]"
        if r == 2:
            return " " * 10 + "[Search]"
        if r == 3:
            return " " * 10 + "[Reset]"
        if r == 4:
            return "Status: " + self.status
        if r == 5:
            return "Drafts: " + str(self.drafts)
        if r == 7:
            return " " * 26 + "[Delete Draft]"
        if r == 11:
            return " " * 26 + "[Export Report]"
        return ""

    def visible_elements(self) -> List[Element]:
        out: List[Element] = []
        if self.dialog_open:
            out.extend(DIALOG_ELEMENTS)
            return out
        for el in STATIC_ELEMENTS:
            if el.row - self.offset < 0 or el.row - self.offset >= VIS:
                continue
            el.value = ""
            if el.eid == "status":
                el.value = self.status
            elif el.eid == "drafts":
                el.value = str(self.drafts)
            out.append(el)
        return out

    def screen(self) -> str:
        """低分辨率字符画面：这就是模型眼里的"像素"。"""
        if self.dialog_open:
            body = list(DIALOG_ROWS) + [" " * WIDTH]
        else:
            body = [self.doc_row(self.offset + i) for i in range(VIS)]
        body = [row.ljust(WIDTH)[:WIDTH] for row in body]

        tens = "".join(str((i // 10) % 10) if i % 10 == 0 else " " for i in range(WIDTH))
        ones = "".join(str(i % 10) for i in range(WIDTH))
        lines = [
            "屏幕截图（低分辨率字符画面；坐标 x=列、y=行，均从 0 开始）：",
            "     " + tens,
            "     " + ones,
        ]
        for i, row in enumerate(body):
            lines.append(f"{i:>4} |{row}|")
        lines.append(f"{len(body):>4} |{'-' * WIDTH}|")
        lines.append(f"{len(body) + 1:>4} |status bar: ready{'':<{WIDTH - 17}}|")
        return "\n".join(lines)

    def ax_list(self) -> List[Tuple[str, Optional[Element]]]:
        """当前视口内元素的无障碍树条目：(ref, 元素)。ref 每帧重新分配。"""
        out: List[Tuple[str, Optional[Element]]] = []
        idx = 1
        out.append((f"w{idx}", None))
        idx += 1
        for el in self.visible_elements():
            prefix = {"button": "b", "textbox": "t", "status": "s"}.get(el.role, "e")
            out.append((f"{prefix}{idx}", el))
            idx += 1
        return out

    def tree(self) -> str:
        """可访问性树：只暴露视口内的元素，语义齐全但没有像素坐标。"""
        lines = ["可访问性树（视口内元素；ref 可直接用于 click_ref）："]
        for ref, el in self.ax_list():
            if el is None:
                lines.append(f"  {ref}  window   \"Order Query Backend\"")
            elif el.role == "status":
                lines.append(f"  {ref}  status   \"{el.name}\"    value=\"{el.value}\"")
            elif el.role == "textbox":
                lines.append(f"  {ref}  textbox  \"{el.name}\"  value=\"{self.order_no}\"")
            else:
                lines.append(f"  {ref}  {el.role:<7} \"{el.name}\"")
        lines.append(f"（视口内共 {len(self.ax_list()) - 1} 个元素；视口外的内容不在树里）")
        return "\n".join(lines)

    # -- 交互 -------------------------------------------------------------
    def hit(self, x: int, y: int) -> Optional[Element]:
        for el in self.visible_elements():
            screen_row = el.row if self.dialog_open else el.row - self.offset
            if screen_row == y and el.col <= x <= el.col + el.width - 1:
                return el
        return None

    def click(self, el: Element, confirm: bool = False) -> str:
        if el.eid in IRREVERSIBLE and not confirm:
            return (f"错误：动作不可逆（{el.name}），必须显式传 confirm=true 才能执行；"
                    f"本动作没有回滚")
        if el.eid == "close":
            self.closed = True
            return "已点击【Close】｜窗口已关闭，不可撤销"
        if el.eid == "minimize":
            return "已点击【Minimize】｜窗口已最小化，后续点击无效"
        if el.eid == "order_input":
            self.focus = "order_input"
            return "已点击【Order No】｜焦点已移到输入框"
        if el.eid == "search":
            if self.order_no:
                self.status = f"PROCESSED: {self.order_no}"
                return f"已点击【Search】｜Status={self.status}"
            self.status = "ERROR: order no required"
            return "已点击【Search】｜Status=ERROR: order no required"
        if el.eid == "reset":
            self.order_no = ""
            self.status = "PENDING"
            return "已点击【Reset】｜订单号已清空，Status=PENDING"
        if el.eid == "delete_draft":
            self.dialog_open = True
            return "已点击【Delete Draft】｜弹出确认框，等待确认"
        if el.eid == "ok":
            self.drafts = max(0, self.drafts - 1)
            self.dialog_open = False
            return f"已点击【OK】｜草稿已删除，Drafts={self.drafts}（不可撤销）"
        if el.eid == "cancel":
            self.dialog_open = False
            return f"已点击【Cancel】｜确认框关闭，Drafts={self.drafts} 未变"
        if el.eid == "export":
            self.exported += 1
            return f"已点击【Export Report】｜已导出 {self.exported} 份"
        return f"已点击【{el.name}】｜无效果"

    def type_text(self, text: str) -> str:
        if self.focus != "order_input":
            return "错误：没有获得焦点的输入框，先 click 目标输入框"
        self.order_no = (self.order_no + text)[:20]
        return f"已输入【Order No】｜value={self.order_no}"

    def press(self, key: str) -> str:
        if key.lower() == "enter" and self.focus == "order_input":
            return self.click(self.find("search"))  # type="ignore"
        return f"已按下【{key}】｜无效果"

    def scroll(self, dy: int) -> str:
        old = self.offset
        max_offset = max(0, 12 - VIS)
        self.offset = max(0, min(max_offset, self.offset + dy))
        return (f"已滚动【{dy:+d}】｜视口偏移 {old} -> {self.offset}"
                f"（可见文档行 {self.offset}-{self.offset + VIS - 1}）")

    def find(self, eid: str) -> Element:
        for el in STATIC_ELEMENTS + DIALOG_ELEMENTS:
            if el.eid == eid:
                return el
        raise KeyError(eid)

    # -- 客观验证 ---------------------------------------------------------
    def verify(self) -> List[Tuple[str, bool, str]]:
        return [
            ("订单号", self.order_no == "A1001", f"order_no={self.order_no!r}"),
            ("状态", self.status == "PROCESSED: A1001", f"status={self.status!r}"),
            ("草稿数", self.drafts == 0, f"drafts={self.drafts}"),
            ("确认框已关闭", not self.dialog_open, f"dialog_open={self.dialog_open}"),
        ]


# ---------------------------------------------------------------------------
# 工具执行器
# ---------------------------------------------------------------------------


class Toolbox:
    def __init__(self, app: App) -> None:
        self.app = app
        self.last_ax: Dict[str, str] = {}   # 最近一次 observe 的 ref -> 元素名快照

    def dispatch(self, action: Dict[str, Any]) -> Tuple[str, str]:
        name = str(action.get("action", ""))
        try:
            if name == "observe":
                obs = self.app.tree()
                self.last_ax = {ref: (el.name if el else "") for ref, el in self.app.ax_list()}
                return name, obs
            if name == "screenshot":
                return name, self.app.screen()
            if name == "click_xy":
                return name, self.click_xy(action)
            if name == "click_ref":
                return name, self.click_ref(action)
            if name == "type":
                return name, self.app.type_text(str(action.get("text", "")))
            if name == "key":
                return name, self.app.press(str(action.get("key", "")))
            if name == "scroll":
                return name, self.app.scroll(int(action.get("dy", 0) or 0))
        except Exception as exc:
            return name, f"错误：工具执行异常 {type(exc).__name__}: {exc}"
        return name, (f"错误：未知动作 {name!r}，只允许 observe/screenshot/click_xy/click_ref/"
                      "type/key/scroll/done")

    def click_xy(self, action: Dict[str, Any]) -> str:
        try:
            x = int(action.get("x", 0))
            y = int(action.get("y", 0))
        except (TypeError, ValueError):
            return "错误：click_xy 需要整数坐标 x、y"
        el = self.app.hit(x, y)
        if el is None:
            return f"错误：坐标 ({x}, {y}) 处没有可交互元素（点空了）"
        self.app.actions.append(f"click_xy({x},{y})->{el.eid}")
        return self.app.click(el, bool(action.get("confirm", False)))

    def click_ref(self, action: Dict[str, Any]) -> str:
        ref = str(action.get("ref", ""))
        name = str(action.get("name", ""))
        current = {r: (el.name if el else "") for r, el in self.app.ax_list()}
        if ref:
            if ref not in current:
                return f"错误：ref {ref} 不在当前视口里（可能已随重渲染失效），请重新 observe"
            # ref 是每帧重新分配的：同一个 ref 可能已经指向别的元素了
            snapshot = self.last_ax.get(ref)
            if snapshot is not None and current[ref] != snapshot:
                return (f"错误：ref {ref} 已失效（observe 时它是 {snapshot!r}，"
                        f"现在指向 {current[ref]!r}），请重新 observe 再点")
        for ax_ref, el in self.app.ax_list():
            if el is None:
                continue
            if (ref and ax_ref == ref) or (name and el.name == name):
                self.app.actions.append(f"click_ref({ax_ref}={el.eid})")
                return self.app.click(el, bool(action.get("confirm", False)))
        return f"错误：当前视口里没有元素 {ref or name!r}（结构里查不到，可能在视口外或已失效）"


# ---------------------------------------------------------------------------
# 感知辅助：解析树与画面，做 grounding
# ---------------------------------------------------------------------------


def parse_screen(screen_text: str) -> List[Tuple[int, str]]:
    rows: List[Tuple[int, str]] = []
    for line in screen_text.splitlines():
        match = re.match(r"^\s*(\d+) \|(.*)\|\s*$", line)
        if match:
            rows.append((int(match.group(1)), match.group(2)))
    return rows


def ground(screen_text: str, label: str) -> Optional[Tuple[int, int]]:
    """在字符画面里找标签，返回 (x=列, y=行)；找不到返回 None。

    这就是 grounding 的最小版本：把"点 Search 按钮"变成一对坐标。
    """
    for y, content in parse_screen(screen_text):
        idx = content.find(label)
        if idx >= 0:
            return idx + len(label) // 2, y
    return None


def find_ref(tree_text: str, name: str) -> Optional[str]:
    for line in tree_text.splitlines():
        match = re.search(r"\b([a-z]\d+)\s+\w+\s+\"([^\"]+)\"", line)
        if match and match.group(2) == name:
            return match.group(1)
    return None


def hit_name(obs: str) -> Optional[str]:
    match = re.search(r"已点击【(.+?)】", obs)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# 模型：mock 与 live
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一个操作电脑界面的 GUI Agent。每轮只输出一个 JSON 对象，不要输出解释文字。

可用动作（一次只能给一个）：
{"action": "observe"}                                   读可访问性树（角色、名称、ref；只有视口内的元素）
{"action": "screenshot"}                                读屏幕（低分辨率画面，用于 grounding 出坐标）
{"action": "click_xy", "x": 12, "y": 3, "confirm": false}  按坐标点击（像素级）
{"action": "click_ref", "ref": "b5", "confirm": false}     按结构 ref 点击（更准，优先用它）
{"action": "type", "text": "A1001"}                     在当前焦点输入框打字
{"action": "key", "key": "Enter"}                       按键
{"action": "scroll", "dy": 3}                           滚动视口
{"action": "done", "summary": "一句话说明结果"}

规则：
1. 优先用 observe + click_ref（结构定位）；只有结构里没有目标时才用 screenshot + click_xy。
2. click_xy 的坐标必须来自你看到的那张画面，x 是列、y 是行，都从 0 开始。
3. 不可逆动作（删除、关闭等）必须显式带 "confirm": true，否则会被拒绝。
4. 每一步之后核对动作效果：如果实际命中不是你想要的元素，换一种定位方式，不要重复点同一个坐标。
5. 不要用截图判断业务结果是否成功，要看状态值。
"""


@dataclass
class MockStep:
    kind: str          # observe / screenshot / click / type / scroll / done
    target: str = ""   # 期望命中的元素名
    text: str = ""
    dy: int = 0
    channel: str = ""  # visual / ref（click 步骤用）
    confirm: bool = False


MOCK_PLAN: List[MockStep] = [
    MockStep("observe"),
    MockStep("screenshot"),
    MockStep("click", target="Search", channel="visual"),
    MockStep("click", target="Order No", channel="ref"),
    MockStep("type", text="A1001"),
    MockStep("click", target="Search", channel="ref"),
    MockStep("observe"),
    MockStep("scroll", dy=3),
    MockStep("observe"),
    MockStep("screenshot"),
    MockStep("click", target="Delete Draft", channel="visual"),
    MockStep("observe"),
    MockStep("click", target="OK", channel="ref"),          # 缺 confirm，应被闸门拒绝
    MockStep("screenshot"),
    MockStep("click", target="OK", channel="visual"),       # 偏一行，应点中 Cancel
    MockStep("click", target="Delete Draft", channel="ref"),
    MockStep("observe"),
    MockStep("click", target="OK", channel="ref", confirm=True),
    MockStep("done"),
]


class MockModel:
    """离线模型：按剧本驱动动作，用真实工具执行，靠观察里的动作效果判断对错。

    它模拟的是一个"视觉优先、错了就切结构"的模型：
    第一次用坐标（grounding），命中不是目标就换成结构 ref 再来一次；
    两种方式都不行就放弃这一步，继续走剧本。
    """

    MAX_TRIES = 3

    def __init__(self) -> None:
        self.i = 0
        self.pending: Optional[int] = None   # 正在等结果的 click 步骤下标
        self.tries = 0
        self.observe_fallbacks = 0
        self.last_tree = ""
        self.last_screen = ""
        self.mismatches = 0

    @staticmethod
    def _last_obs(history: List[Dict[str, Any]]) -> str:
        for entry in reversed(history):
            if entry.get("tool"):
                return str(entry.get("raw", entry.get("content", "")))
        return ""

    def next(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        obs = self._last_obs(history)
        if obs.startswith("可访问性树"):
            self.last_tree = obs
        elif obs.startswith("屏幕截图"):
            self.last_screen = obs

        # 1) 结算上一步 click 的实际结果
        if self.pending is not None:
            step = MOCK_PLAN[self.pending]
            if hit_name(obs) == step.target:
                self.i = self.pending + 1
                self.pending = None
            else:
                self.mismatches += 1
                self.tries += 1
                alt = self._alt_action(step) if self.tries < self.MAX_TRIES else None
                if alt is not None:
                    return alt          # 换一种定位方式，重试同一步
                self.i = self.pending + 1
                self.pending = None

        # 2) 产出当前步骤的动作
        while self.i < len(MOCK_PLAN):
            step = MOCK_PLAN[self.i]
            if step.kind == "click" and step.channel == "ref":
                ref = find_ref(self.last_tree, step.target)
                if ref is None:
                    # 树里没有这个元素（可能滚动了、弹框盖住了）：先刷新观察
                    self.observe_fallbacks += 1
                    if self.observe_fallbacks >= 3:
                        self.observe_fallbacks = 0
                        self.i += 1
                        continue
                    return {"action": "observe"}
                self.observe_fallbacks = 0
                self.pending = self.i
                self.tries = 1
                return {"action": "click_ref", "ref": ref, "confirm": step.confirm}
            action = self._action_for(step)
            if action is None:
                self.i += 1
                continue
            if step.kind == "click":
                self.pending = self.i
                self.tries = 1
            else:
                self.i += 1          # 非 click 步骤立刻推进
            return action
        return {"action": "done", "summary": "剧本执行完毕"}

    def _action_for(self, step: MockStep) -> Optional[Dict[str, Any]]:
        if step.kind == "observe":
            return {"action": "observe"}
        if step.kind == "screenshot":
            return {"action": "screenshot"}
        if step.kind == "type":
            return {"action": "type", "text": step.text}
        if step.kind == "scroll":
            return {"action": "scroll", "dy": step.dy}
        if step.kind == "done":
            return {"action": "done", "summary": "已完成查询与删除草稿"}
        if step.kind == "click":
            pos = ground(self.last_screen, step.target)
            if pos is not None:
                x, y = pos
                # 视觉通道：grounding 出坐标，并带一个系统性的 y 偏移（模拟截图对齐误差）
                return {"action": "click_xy", "x": x, "y": y + 1, "confirm": step.confirm}
            # 画面里根本没有这个标签（比如元素在视口外）→ 退回结构通道
            ref = find_ref(self.last_tree, step.target)
            if ref is not None:
                return {"action": "click_ref", "ref": ref, "confirm": step.confirm}
            return None
        return None

    def _alt_action(self, step: MockStep) -> Optional[Dict[str, Any]]:
        """同一步的另一种定位方式：视觉失败就换结构；结构通道失败就放弃这一步。"""
        if step.channel != "visual":
            return None
        ref = find_ref(self.last_tree, step.target)
        if ref is None:
            return None
        return {"action": "click_ref", "ref": ref, "confirm": step.confirm}


class LiveModel:
    """--live：调用真实 API（OpenAI 兼容的 chat/completions）。"""

    def __init__(self, model: str, base_url: str, api_key: str) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def next(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        payload = {"model": self.model, "messages": messages, "temperature": 0.0}
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"模型接口返回 {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"连不上模型接口 {self.base_url}: {exc.reason}") from exc
        content = data["choices"][0]["message"].get("content") or ""
        return parse_action(content)


def parse_action(content: str) -> Dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"模型输出里找不到 JSON 动作：{content[:200]!r}")
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict) or "action" not in data:
        raise ValueError(f"模型输出缺少 action 字段：{content[:200]!r}")
    return data


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

TASK = ("在订单查询后台里查询订单 A1001（填入 Order No 再点 Search），"
        "然后删除那条草稿。")


def run_task(app: App, toolbox: Toolbox, model: Any, max_steps: int = 24,
             verbose: bool = True) -> Dict[str, Any]:
    history: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"任务：{TASK}\n\n先 observe 看结构，必要时 screenshot 找坐标。"},
    ]
    status = "max_steps_reached"
    summary = ""

    for step in range(1, max_steps + 1):
        try:
            action = model.next(history)
        except Exception as exc:
            history.append({"role": "user", "content": f"错误：模型调用失败 {exc}"})
            status = "model_error"
            break

        name = str(action.get("action", ""))
        if name == "done":
            status = "done"
            summary = str(action.get("summary", ""))
            if verbose:
                print(f"\n[步骤 {step}] 模型宣布完成：{summary}")
            break

        tool, obs = toolbox.dispatch(action)
        history.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
        history.append({"role": "user", "content": f"观察结果（{tool}）：\n{obs}",
                        "tool": tool, "raw": obs})
        if verbose:
            print(f"\n[步骤 {step}] {name} -> {first_line(obs)}")
            for line in obs.splitlines()[1:4]:
                print(f"    {line}")

    # 客观验证：查状态字段，不看截图
    checks = app.verify()
    verified = all(ok for _, ok, _ in checks)
    return {
        "status": status,
        "summary": summary,
        "verified": verified,
        "checks": checks,
        "actions": list(app.actions),
    }


def first_line(text: str) -> str:
    return (text.strip().splitlines()[0] if text.strip() else "")[:160]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ch38 计算机使用与 GUI Agent —— 最小可实现")
    parser.add_argument("--render", action="store_true", help="只打印画面与可访问性树后退出")
    parser.add_argument("--max-steps", type=int, default=24, help="最大步数")
    parser.add_argument("--live", action="store_true", help="用真实 API（需 OPENAI_API_KEY）")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    args = parser.parse_args(argv)

    if args.live and not os.environ.get("OPENAI_API_KEY"):
        print("[!] --live 需要环境变量 OPENAI_API_KEY；不设置时可用 mock 模式跑通全回路。")
        return 2

    app = App()
    if args.render:
        print(app.screen())
        print()
        print(app.tree())
        return 0

    toolbox = Toolbox(app)
    model: Any
    if args.live:
        model = LiveModel(args.model, args.base_url, os.environ.get("OPENAI_API_KEY", ""))
    else:
        model = MockModel()

    print(f"任务：{TASK}")
    print("-" * 60)
    started = time.time()
    result = run_task(app, toolbox, model, max_steps=args.max_steps)
    elapsed = time.time() - started

    print("-" * 60)
    print("客观验证（查状态字段，不看截图）：")
    for label, ok, detail in result["checks"]:
        print(f"  [{'OK' if ok else '!!'}] {label}：{detail}")
    print(f"结束状态：{result['status']}，用时 {elapsed:.1f}s，模式：{'live' if args.live else 'mock'}")
    if result["summary"]:
        print(f"模型说明：{result['summary']}")
    if result["verified"]:
        print("[OK] 任务达成：所有状态断言通过。")
        return 0
    print("[FAIL] 任务未达成：至少一条状态断言没过。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
