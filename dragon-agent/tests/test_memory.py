"""记忆模块的单元测试。

守四件必须成立的事：

1. **低可信来源写不进来**——``model`` 来源的语义记忆被拒；
2. **高可信覆盖并留版本**——同一条事实被更新，旧版本还在历史里；
3. **TTL 到期是降权不是删除**——过期条目仍能被召回，只是分低；
4. **显式遗忘后检索不到**——这条是用户按下"忘掉"的兜底保证。

外加写入时机规则、两条检索路线、上下文装配与记忆评测。
全部离线：假时钟 + 规则判定，不联网、不调模型。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.memory import (
    KINDS,
    PROCEDURAL_STREAK,
    SOURCE_TRUST,
    GraphMemory,
    InMemoryMemory,
    Memory,
    MemoryCase,
    MemoryItem,
    MemorySetup,
    MemoryWriteRejected,
    VectorMemory,
    default_memory_cases,
    default_memory_factory,
    evaluate_memory,
    extract_entities,
    extract_relations,
    write_decision,
)


class FakeClock:
    """可推进的假时钟。让 TTL 与新鲜度能确定性地测。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


# ---------------------------------------------------------------------------
# 一、来源可信度：低可信来源写不进来
# ---------------------------------------------------------------------------


class TestSourceTrust(unittest.TestCase):
    def test_model_source_semantic_write_is_rejected(self):
        """模型推断的事实不能进长期语义记忆。"""
        memory = VectorMemory()
        with self.assertRaises(MemoryWriteRejected):
            memory.write("semantic", "模型编造的用户偏好", source="model")
        self.assertEqual(memory.recall("模型编造的用户偏好", kind="semantic"), [])

    def test_model_source_working_memory_is_allowed(self):
        """受限制的是语义记忆；工作记忆是草稿，模型写不进去就没法干活了。"""
        memory = VectorMemory()
        item = memory.write("working", "临时草稿：下一步要查预算表", source="model")
        self.assertEqual(item.source, "model")
        self.assertEqual(item.kind, "working")

    def test_model_source_procedural_is_downgraded_to_episodic(self):
        """模型推断的"怎么做"只是"某次这么做过"，不构成规则。"""
        memory = VectorMemory()
        item = memory.write("procedural", "改完代码先跑测试", source="model")
        self.assertEqual(item.kind, "episodic")

    def test_unknown_source_is_rejected(self):
        memory = VectorMemory()
        with self.assertRaises(MemoryWriteRejected):
            memory.write("semantic", "来路不明的记忆", source="stranger")

    def test_trust_order_is_user_tool_model(self):
        self.assertGreater(SOURCE_TRUST["user"], SOURCE_TRUST["tool"])
        self.assertGreater(SOURCE_TRUST["tool"], SOURCE_TRUST["model"])

    def test_write_decision_is_pure(self):
        """判定规则是纯函数——同样的输入必须给同样的结论。"""
        first = write_decision("semantic", "model")
        second = write_decision("semantic", "model")
        self.assertEqual(first, second)
        self.assertFalse(first.allow)


# ---------------------------------------------------------------------------
# 二、版本：高可信覆盖并留版本
# ---------------------------------------------------------------------------


class TestVersioning(unittest.TestCase):
    def test_higher_trust_overwrites_and_keeps_old_version(self):
        memory = VectorMemory()
        first = memory.write("semantic", "Orion 项目预算是 8 万元", source="user")
        second = memory.write("semantic", "Orion 项目预算是 12 万元", source="user")

        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)

        history = memory.history(second.id)
        self.assertEqual(len(history), 1, "旧版本要留着")
        self.assertEqual(history[0].version, 1)
        self.assertIn("8 万元", history[0].text)

        # 当前版本只剩一条——矛盾的事实不能同时躺在库里
        hits = memory.recall("Orion 预算是多少", kind="semantic")
        self.assertEqual([h.text for h in hits], ["Orion 项目预算是 12 万元"])

    def test_lower_trust_cannot_override_higher_trust(self):
        """工具返回不能盖掉用户亲口说的话。"""
        memory = VectorMemory()
        memory.write("semantic", "Orion 项目预算是 12 万元", source="user")
        with self.assertRaises(MemoryWriteRejected):
            memory.write("semantic", "Orion 项目预算是 99 万元", source="tool")

        hits = memory.recall("Orion 预算是多少", kind="semantic")
        self.assertEqual(len(hits), 1)
        self.assertIn("12 万元", hits[0].text)

    def test_episodic_memory_is_append_only(self):
        """情景记忆只追加不修改——两条记录都要在。"""
        memory = VectorMemory()
        memory.write("episodic", "3 月批的预算是 8 万元", source="user")
        memory.write("episodic", "4 月批的预算是 12 万元", source="user")
        self.assertEqual(memory.stats()["episodic"], 2)

    def test_procedural_memory_needs_consecutive_successes(self):
        """一次偶然成功不能固化成规则。"""
        memory = VectorMemory()
        with self.assertRaises(MemoryWriteRejected):
            memory.write("procedural", "改完代码先跑测试", source="user")
        for _ in range(PROCEDURAL_STREAK):
            memory.record_success("改完代码先跑测试")
        item = memory.write("procedural", "改完代码先跑测试", source="user")
        self.assertEqual(item.kind, "procedural")


# ---------------------------------------------------------------------------
# 三、TTL：到期降权，不是删除
# ---------------------------------------------------------------------------


class TestTTL(unittest.TestCase):
    def test_expired_item_is_downweighted_not_deleted(self):
        clock = FakeClock()
        memory = VectorMemory(clock=clock)
        memory.write("semantic", "Orion 项目预算是 12 万元", source="user", ttl=60)
        memory.write("semantic", "Argus 项目预算是 5 万元", source="user")

        before = {h.id: h.score for h in memory.recall("Orion 预算是多少", k=5)}
        target = next(h for h in memory.recall("Orion 预算是多少", k=5) if "Orion" in h.text)

        clock.advance(120)  # TTL 到期

        after = memory.recall("Orion 预算是多少", k=5)
        still = [h for h in after if "Orion" in h.text]
        self.assertTrue(still, "TTL 到期是降权，不是删除")
        self.assertLess(still[0].score, before[target.id], "过期条目必须掉分")
        self.assertTrue(memory.is_expired(target))

    def test_expired_item_is_still_readable_by_id(self):
        clock = FakeClock()
        memory = VectorMemory(clock=clock)
        item = memory.write("semantic", "Orion 项目预算是 12 万元", source="user", ttl=1)
        clock.advance(10)
        self.assertTrue(memory.is_expired(item))
        self.assertEqual(memory.expires_at(item), item.created_at + 1)
        # 条目本身没被抹掉，历史记录里还在
        self.assertEqual(memory.stats()["semantic"], 1)

    def test_no_ttl_means_never_expires(self):
        clock = FakeClock()
        memory = VectorMemory(clock=clock)
        item = memory.write("semantic", "Orion 项目预算是 12 万元", source="user")
        clock.advance(10 ** 9)
        self.assertFalse(memory.is_expired(item))


# ---------------------------------------------------------------------------
# 四、遗忘：显式忘掉就检索不到
# ---------------------------------------------------------------------------


class TestForget(unittest.TestCase):
    def test_forgotten_item_cannot_be_recalled(self):
        memory = VectorMemory()
        item = memory.write("semantic", "Orion 项目预算是 12 万元", source="user")
        self.assertTrue(memory.recall("Orion 预算是多少", kind="semantic"))

        self.assertTrue(memory.forget(item.id))
        self.assertEqual(memory.recall("Orion 预算是多少", kind="semantic"), [])
        self.assertFalse(memory.forget(item.id), "重复遗忘返回 False")

    def test_forget_also_purges_history_versions(self):
        memory = VectorMemory()
        first = memory.write("semantic", "Orion 项目预算是 8 万元", source="user")
        second = memory.write("semantic", "Orion 项目预算是 12 万元", source="user")
        self.assertEqual(len(memory.history(second.id)), 1)

        self.assertTrue(memory.forget(first.id))
        self.assertEqual(memory.history(second.id), [])
        # 当前版本不受影响
        self.assertEqual(memory.stats()["semantic"], 1)

    def test_forget_unknown_id_returns_false(self):
        memory = VectorMemory()
        self.assertFalse(memory.forget("mem-99999"))


# ---------------------------------------------------------------------------
# 五、写入时机与上下文装配
# ---------------------------------------------------------------------------


class TestWritePolicyAndRead(unittest.TestCase):
    def test_empty_text_is_rejected(self):
        memory = VectorMemory()
        with self.assertRaises(ValueError):
            memory.write("semantic", "   ", source="user")

    def test_read_injects_working_memory_but_not_semantic(self):
        """短期与长期的边界：工作记忆进上下文，语义记忆要检索才进。"""
        memory = VectorMemory()
        memory.write("working", "下一步要查 2026 预算表", source="user")
        memory.write("semantic", "Orion 项目预算是 12 万元", source="user")

        injected = memory.read("接下来查什么")
        texts = [i.text for i in injected]
        self.assertIn("下一步要查 2026 预算表", texts)
        self.assertNotIn("Orion 项目预算是 12 万元", texts)

    def test_read_matches_procedural_memory_by_trigger(self):
        memory = VectorMemory()
        for _ in range(PROCEDURAL_STREAK):
            memory.record_success("提交代码前先跑测试")
        memory.write("procedural", "提交代码前先跑测试", source="user")

        self.assertTrue(memory.read("提交代码前先跑测试"))
        self.assertFalse(memory.read("今天午饭吃什么"))

    def test_all_four_kinds_are_supported(self):
        memory = VectorMemory()
        memory.write("working", "草稿", source="user")
        memory.write("episodic", "3 月批的预算是 8 万元", source="user")
        memory.write("semantic", "Orion 项目预算是 12 万元", source="user")
        for _ in range(PROCEDURAL_STREAK):
            memory.record_success("改完代码先跑测试")
        memory.write("procedural", "改完代码先跑测试", source="user")
        stats = memory.stats()
        for kind in KINDS:
            self.assertEqual(stats[kind], 1, f"{kind} 应该各有一条")


# ---------------------------------------------------------------------------
# 六、两条检索路线
# ---------------------------------------------------------------------------


class TestRoutes(unittest.TestCase):
    def test_vector_route_chunks_long_text(self):
        memory = VectorMemory(chunk_chars=40, overlap=8)
        long_text = "预算评审会议纪要。" * 12 + "Orion 项目预算是 12 万元。"
        memory.write("semantic", long_text, source="user")
        hits = memory.recall("Orion 预算是多少", kind="semantic")
        self.assertTrue(hits)
        self.assertGreater(len(memory._vectors[hits[0].id]), 1, "长文本应该被切成多块")

    def test_hash_embedding_is_not_semantic(self):
        """哈希投影只认字面：同义不同字就对不上。"""
        from dragon_agent.memory import cosine, hash_embed

        near = cosine(hash_embed("项目预算是 12 万元"), hash_embed("项目预算 12 万元"))
        far = cosine(hash_embed("项目预算是 12 万元"), hash_embed("今天午饭吃什么"))
        self.assertGreater(near, far)
        self.assertLess(cosine(hash_embed("项目预算是 12 万元"),
                               hash_embed("运维预算是 30 万")), 1.0)

    def test_graph_route_invalidates_old_edge_instead_of_deleting(self):
        clock = FakeClock()
        memory = GraphMemory(clock=clock)
        memory.write("semantic", "Orion 项目预算是 8 万元", source="user")
        clock.advance(3600)
        memory.write("semantic", "Orion 项目预算是 12 万元", source="user")

        now = memory.facts("Orion")
        self.assertEqual(len(now), 1)
        self.assertEqual(now[0].object, "12 万元")

        earlier = memory.facts("Orion", at=clock.now - 1800)
        self.assertEqual(len(earlier), 1)
        self.assertEqual(earlier[0].object, "8 万元", "旧边应该失效而不是被删")

    def test_graph_route_separates_entities(self):
        """图谱能分清 Orion 和 Argus——这正是向量路线的盲区。"""
        memory = GraphMemory()
        memory.write("semantic", "Orion 项目预算是 12 万元", source="user")
        memory.write("semantic", "Argus 项目预算是 5 万元", source="user")

        hits = memory.recall("Orion 预算是多少", kind="semantic")
        self.assertEqual([h.text for h in hits], ["Orion 项目预算是 12 万元"])

    def test_relation_extraction_picks_both_shapes(self):
        owned = extract_relations("Orion 项目的负责人是 张三")
        self.assertEqual(len(owned), 1)
        self.assertEqual(owned[0].subject, "Orion 项目")
        self.assertEqual(owned[0].predicate, "负责人")

        plain = extract_relations("Orion 项目预算是 12 万元")
        self.assertEqual(len(plain), 1)
        self.assertEqual(plain[0].subject, "Orion 项目预算")
        self.assertEqual(plain[0].object, "12 万元")

    def test_entity_extraction_finds_latin_names(self):
        self.assertIn("Orion", extract_entities("Orion 项目预算是多少"))


# ---------------------------------------------------------------------------
# 七、记忆评测
# ---------------------------------------------------------------------------


class TestEvaluation(unittest.TestCase):
    def test_default_case_set_passes(self):
        report = evaluate_memory(default_memory_cases(), default_memory_factory)
        self.assertEqual(report.total, 8)
        self.assertEqual(report.recalled, report.total, f"漏掉的用例：{report.missed}")
        self.assertEqual(report.false_recall, 0, f"乱想的用例：{report.polluted}")
        self.assertGreater(report.precision, 0.0)

    def test_evaluation_detects_a_missing_memory(self):
        """把某条记忆删掉，评测要能报出漏想。"""
        broken = []
        for case in default_memory_cases():
            if case.id == "semantic-user-budget":
                case = MemoryCase(
                    id=case.id, setup=[], query=case.query, expect=case.expect,
                    forbid=case.forbid, expect_empty=case.expect_empty,
                    k=case.k, route=case.route,
                )
            broken.append(case)

        report = evaluate_memory(broken, default_memory_factory)
        self.assertLess(report.recall_rate, 1.0)
        self.assertIn("semantic-user-budget", report.missed)

    def test_evaluation_detects_a_forbidden_hit(self):
        """记忆里混进别人的项目，评测要能报出乱想。"""
        cases = [
            MemoryCase(
                id="forbidden",
                setup=[MemorySetup("semantic", "Argus 项目预算是 5 万元", "user")],
                query="预算是多少",
                expect=["5 万元"],
                forbid=["5 万元"],
                k=5,
                route="vector",
            )
        ]
        report = evaluate_memory(cases, default_memory_factory)
        self.assertIn("forbidden", report.polluted)
        self.assertEqual(report.false_recall, 1)

    def test_report_serializes(self):
        report = evaluate_memory(default_memory_cases(), default_memory_factory)
        payload = report.to_dict()
        self.assertEqual(payload["total"], 8)
        self.assertIn("recall_rate", payload)
        self.assertIsInstance(payload["missed"], list)


# ---------------------------------------------------------------------------
# 八、与契约的兼容性
# ---------------------------------------------------------------------------


class TestContract(unittest.TestCase):
    def test_memory_item_fields_match_the_contract(self):
        """契约第 7 节写死了 8 个字段，多一个都算破坏契约。"""
        fields = list(MemoryItem.__dataclass_fields__)
        self.assertEqual(
            fields,
            ["id", "kind", "text", "source", "score", "ttl", "created_at", "version"],
        )

    def test_concrete_classes_satisfy_the_protocol(self):
        for cls in (InMemoryMemory, VectorMemory, GraphMemory):
            self.assertTrue(issubclass(cls, Memory) or isinstance(cls(), Memory))
            self.assertTrue(hasattr(cls, "write"))
            self.assertTrue(hasattr(cls, "recall"))
            self.assertTrue(hasattr(cls, "forget"))

    def test_memory_module_has_no_third_party_imports(self):
        """纯标准库：读者 clone 下来就能跑。"""
        source = (ROOT / "dragon_agent" / "memory.py").read_text(encoding="utf-8")
        for banned in ("import numpy", "import requests", "from numpy", "import torch"):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
