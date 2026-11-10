"""上下文装配器的单元测试。

守四件必须成立的事：

1. **超预算时按优先级裁剪**——先砍历史，再砍检索，任务与稳定块最后动；
2. **关键约束前置到头部**——system / tools 在最前面，保证提示缓存前缀命中；
3. **预算报告数字对得上**——per_layer、total、dropped 都与实际内容一致；
4. **相同输入两次 render 结果逐字相同**——装配器没有隐藏状态。

外加两条纯函数性质：装配器不修改入参，不同实例之间结果一致。
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.context import (
    LAYER_ORDER,
    AssembledContext,
    BudgetReport,
    ContextAssembler,
    ContextBlock,
    compress,
    estimate_tokens,
    truncate_to_tokens,
)

TASK = "查一下 2026 年的项目预算，并告诉我总额"

HISTORY = [
    {"role": "user", "content": "先看看预算"},
    {"role": "assistant", "content": "好的，我去查"},
    {"role": "user", "content": "顺便看看上季度"},
]

RETRIEVAL = [
    "预算表：研发 62 万，市场 25 万，运维 13 万，总计 100 万。",
    "上季度预算：90 万。",
    "预算相关：总计 100 万。",
]

TOOLS = [
    {
        "name": "read_budget",
        "description": "读取指定财年的预算表。",
        "parameters": {
            "type": "object",
            "properties": {"fiscal_year": {"type": "string"}},
            "required": ["fiscal_year"],
        },
    },
    {
        "name": "add",
        "description": "把两个整数相加。",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
]


class TestDeterminism(unittest.TestCase):
    """断言四：相同输入两次 render 结果逐字相同。"""

    def test_same_input_renders_identically(self):
        assembler = ContextAssembler(token_budget=4000)
        first = assembler.assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        second = assembler.assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertEqual(first.render(), second.render())
        self.assertEqual(
            [(b.layer, b.text, b.tokens, b.priority) for b in first.blocks],
            [(b.layer, b.text, b.tokens, b.priority) for b in second.blocks],
        )

    def test_different_instances_render_identically(self):
        """没有隐藏状态：两个装配器实例结果一致。"""
        a = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        b = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertEqual(a.render(), b.render())

    def test_assemble_does_not_mutate_inputs(self):
        """纯函数：不能偷偷改调用方传进来的列表。"""
        history = [dict(h) for h in HISTORY]
        retrieval = list(RETRIEVAL)
        tools = [dict(t) for t in TOOLS]
        before = json.dumps([history, retrieval, tools], ensure_ascii=False, sort_keys=True)
        ContextAssembler(token_budget=4000).assemble(TASK, history, retrieval, tools)
        after = json.dumps([history, retrieval, tools], ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after)

    def test_render_is_stable_under_repeated_calls(self):
        """同一个 AssembledContext 反复 render，结果不变。"""
        assembled = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertEqual(assembled.render(), assembled.render())


class TestStablePrefix(unittest.TestCase):
    """断言二：关键约束前置到头部。"""

    def test_stable_blocks_come_first(self):
        """system 与 tools 排在 task / retrieval / history 之前。"""
        assembled = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        layers = [b.layer for b in assembled.blocks]
        for stable in ("system", "tools"):
            for later in ("task", "retrieval", "history"):
                if stable in layers and later in layers:
                    self.assertLess(
                        layers.index(stable), layers.index(later),
                        f"{stable} 必须在 {later} 之前",
                    )

    def test_block_order_follows_the_fixed_layer_order(self):
        """块顺序严格按 LAYER_ORDER，不随内容变化。"""
        assembled = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        layers = [b.layer for b in assembled.blocks]
        expected = [layer for layer in LAYER_ORDER if layer in layers]
        self.assertEqual(layers, expected)

    def test_render_starts_with_the_system_block(self):
        """渲染结果的第一段是系统约束。"""
        assembled = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertTrue(assembled.render().startswith("[system]"))

    def test_constraints_are_at_the_head(self):
        """关键约束（系统提示）在头部，且含可执行的规定。"""
        assembled = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        head = assembled.render().split("\n\n")[0]
        self.assertIn("必须", head)
        self.assertIn("[system]", head)


class TestBudgetReport(unittest.TestCase):
    """断言三：预算报告数字对得上。"""

    def test_per_layer_sums_to_total(self):
        assembled = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        report = assembled.report
        self.assertEqual(sum(report.per_layer.values()), report.total)
        self.assertEqual(report.budget, 4000)

    def test_per_layer_matches_the_blocks(self):
        """报告里每一层的 token 数要等于对应块的 token 数。"""
        assembled = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        for block in assembled.blocks:
            self.assertEqual(assembled.report.per_layer[block.layer], block.tokens)

    def test_block_tokens_match_the_estimate(self):
        """块的 token 数就是 estimate_tokens(text)。"""
        assembled = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        for block in assembled.blocks:
            self.assertEqual(block.tokens, estimate_tokens(block.text))

    def test_report_lists_all_five_layers(self):
        assembled = ContextAssembler(token_budget=4000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertEqual(
            sorted(assembled.report.per_layer),
            sorted(["system", "tools", "task", "retrieval", "history"]),
        )

    def test_dropped_is_empty_when_everything_fits(self):
        """预算充裕时不该丢东西。"""
        assembled = ContextAssembler(token_budget=100_000).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertEqual(assembled.report.dropped, [])
        self.assertLessEqual(assembled.report.total, assembled.report.budget)


class TestTrimming(unittest.TestCase):
    """断言一：超预算时按优先级裁剪。"""

    def test_history_is_evicted_when_budget_is_tight(self):
        """预算紧张时，历史被整条淘汰——它优先级最低。"""
        assembled = ContextAssembler(token_budget=120).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertTrue(
            any("history" in d for d in assembled.report.dropped),
            f"应当淘汰历史，实际丢弃：{assembled.report.dropped}",
        )

    def test_task_survives_while_history_is_cut(self):
        """任务比历史重要：历史被砍光时任务还在。"""
        assembled = ContextAssembler(token_budget=120).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertIsNotNone(assembled.block_for("task"))
        self.assertIsNone(assembled.block_for("history"))

    def test_free_space_flows_to_the_higher_priority_layer(self):
        """配额用不完的富余额度按优先级让出去——检索层优先于历史。

        这里的预算刚好让检索层超配额一点：没有富余额度回流的话，
        就会白白丢掉一段检索结果。
        """
        assembled = ContextAssembler(token_budget=200).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertEqual(assembled.report.dropped, [])
        block = assembled.block_for("retrieval")
        self.assertIsNotNone(block)
        self.assertEqual(block.text.count("- "), len(RETRIEVAL))

    def test_stable_blocks_are_never_dropped(self):
        """system 与 tools 永远在，哪怕预算极小。"""
        assembled = ContextAssembler(token_budget=1).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertIsNotNone(assembled.block_for("system"))
        self.assertIsNotNone(assembled.block_for("tools"))

    def test_total_never_exceeds_budget_after_trimming(self):
        """裁剪之后总量不该超预算——除非固定块本身就超了。"""
        assembled = ContextAssembler(token_budget=120).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertLessEqual(assembled.report.total, assembled.report.budget)

    def test_dropped_entries_explain_what_was_lost(self):
        """被丢弃的东西要写清楚，不是一句"超预算"。"""
        assembled = ContextAssembler(token_budget=120).assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertTrue(assembled.report.dropped)
        for entry in assembled.report.dropped:
            self.assertTrue(entry.strip())
            self.assertIn(":", entry)

    def test_oversized_task_is_truncated_not_dropped(self):
        """任务太长时压缩并截断，而不是整条丢掉。"""
        long_task = "必须完成的任务。" + "补充说明。" * 200
        assembled = ContextAssembler(token_budget=300).assemble(long_task, [], [], TOOLS)
        block = assembled.block_for("task")
        self.assertIsNotNone(block)
        self.assertTrue(any("task" in d for d in assembled.report.dropped))
        self.assertLessEqual(assembled.report.per_layer["task"], 300)


class TestOperations(unittest.TestCase):
    """四种操作本身的行为。"""

    def test_retrieval_is_ordered_by_relevance(self):
        """与任务相关的检索排在前面。"""
        assembled = ContextAssembler(token_budget=100_000).assemble(
            "预算总计是多少", [], ["无关内容：天气", "预算总计 100 万"], TOOLS
        )
        text = assembled.block_for("retrieval").text
        self.assertLess(text.index("预算总计 100 万"), text.index("无关内容"))

    def test_compression_keeps_constraint_sentences(self):
        """压缩保留含约束信号的句子。"""
        text = "今天天气不错。必须先核对预算。下午开会。禁止跳过审批。"
        compressed = compress(text)
        self.assertIn("必须先核对预算", compressed)
        self.assertIn("禁止跳过审批", compressed)
        self.assertNotIn("今天天气不错", compressed)

    def test_compression_keeps_first_sentence_when_no_signal(self):
        """没有信号词时保留第一句，规则同样确定。"""
        self.assertEqual(compress("第一句。第二句。"), "第一句。")

    def test_truncation_respects_the_limit(self):
        """截断后 token 数不超上限。"""
        text = "字" * 500
        truncated = truncate_to_tokens(text, 50)
        self.assertLessEqual(estimate_tokens(truncated), 50)
        self.assertIn("已截断", truncated)

    def test_token_estimate_is_stable(self):
        """同样的文本永远算出同一个 token 数。"""
        self.assertEqual(estimate_tokens(TASK), estimate_tokens(TASK))
        self.assertEqual(estimate_tokens(""), 0)
        self.assertGreater(estimate_tokens("中文四个字"), 0)


class TestDataStructures(unittest.TestCase):
    def test_report_to_dict(self):
        report = BudgetReport(per_layer={"system": 10}, total=10, budget=100, dropped=[])
        self.assertEqual(
            report.to_dict(),
            {"per_layer": {"system": 10}, "total": 10, "budget": 100, "dropped": []},
        )

    def test_block_fields(self):
        block = ContextBlock(layer="task", text="x", tokens=1, priority=80)
        self.assertEqual((block.layer, block.text, block.tokens, block.priority),
                         ("task", "x", 1, 80))

    def test_assembled_context_defaults(self):
        empty = AssembledContext()
        self.assertEqual(empty.render(), "")
        self.assertEqual(empty.report.total, 0)

    def test_assembler_accepts_a_config_like_object(self):
        """构造器接受带 token_budget 的配置对象。"""

        class FakeConfig:
            token_budget = 3000

        assembler = ContextAssembler(FakeConfig())
        self.assertEqual(assembler.token_budget, 3000)
        assembled = assembler.assemble(TASK, HISTORY, RETRIEVAL, TOOLS)
        self.assertEqual(assembled.report.budget, 3000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
