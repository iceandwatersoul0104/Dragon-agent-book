"""任务状态的单元测试：唯一真相源、检查点、事件溯源、恢复。

全部离线：不联网、不需要 API Key、不写用户目录。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dragon_agent.state import Checkpoint, StateStore, TaskState


class TestStateStore(unittest.TestCase):
    def test_snapshot_returns_a_copy(self):
        """快照是副本——改它不会污染真相源。"""
        store = StateStore(task_id="t1")
        snapshot = store.snapshot()
        snapshot.data["偷偷写"] = 1
        snapshot.steps.append({"index": 99})
        fresh = store.snapshot()
        self.assertNotIn("偷偷写", fresh.data)
        self.assertEqual(fresh.steps, [])

    def test_update_changes_fields(self):
        store = StateStore(task_id="t1")
        store.update(status="completed")
        store.update(data={"answer": "42"})
        snapshot = store.snapshot()
        self.assertEqual(snapshot.status, "completed")
        self.assertEqual(snapshot.data["answer"], "42")

    def test_update_rejects_unknown_fields(self):
        """拼错字段名要当场报错，别默默吞掉。"""
        store = StateStore(task_id="t1")
        with self.assertRaises(AttributeError):
            store.update(没有这个字段=1)

    def test_record_step_appends_and_logs(self):
        """记一步：进 steps，同时留一条事件。"""
        store = StateStore(task_id="t1")
        store.record_step({"index": 1, "action": "add", "tokens": 10})
        store.record_step({"index": 2, "action": "echo", "tokens": 12})
        snapshot = store.snapshot()
        self.assertEqual(len(snapshot.steps), 2)
        self.assertEqual(snapshot.steps[0]["action"], "add")
        self.assertEqual(len(store.events()), 2)

    def test_record_keeps_dicts(self):
        """事件必须是 dict。"""
        store = StateStore(task_id="t1")
        store.record({"type": "custom", "payload": 1})
        events = store.events()
        self.assertTrue(events)
        for event in events:
            self.assertIsInstance(event, dict)

    def test_events_returns_a_copy(self):
        store = StateStore(task_id="t1")
        store.record({"type": "a"})
        events = store.events()
        events.append({"type": "伪造"})
        self.assertEqual(len(store.events()), 1)

    def test_checkpoint_bumps_version(self):
        """写一次检查点，版本号 +1。"""
        store = StateStore(task_id="t1")
        self.assertEqual(store.snapshot().version, 0)
        cp = store.checkpoint()
        self.assertEqual(cp.version, 1)
        self.assertEqual(store.snapshot().version, 1)
        cp2 = store.checkpoint()
        self.assertEqual(cp2.version, 2)

    def test_checkpoint_carries_the_snapshot(self):
        """检查点里带的是完整快照。"""
        store = StateStore(task_id="t1")
        store.update(data={"k": "v"})
        store.record_step({"index": 1, "action": "add"})
        cp = store.checkpoint()
        self.assertEqual(cp.task_id, "t1")
        self.assertEqual(cp.snapshot["data"]["k"], "v")
        self.assertEqual(len(cp.snapshot["steps"]), 1)
        self.assertGreater(cp.created_at, 0)

    def test_restore_round_trip(self):
        """从检查点恢复：任务号、版本、步骤数都要对上。"""
        store = StateStore(task_id="t1")
        store.record_step({"index": 1, "action": "add"})
        store.update(data={"k": "v"})
        cp = store.checkpoint()
        before = store.snapshot()

        store.record_step({"index": 2, "action": "echo"})
        store.update(status="completed")

        restored = store.restore(cp)
        self.assertEqual(restored.task_id, cp.task_id)
        self.assertEqual(restored.version, cp.version)
        self.assertEqual(len(restored.steps), len(before.steps))
        self.assertEqual(restored.data["k"], "v")
        self.assertEqual(store.snapshot().status, "running")

    def test_restore_records_an_event(self):
        """恢复这件事本身也要留痕。"""
        store = StateStore(task_id="t1")
        cp = store.checkpoint()
        store.restore(cp)
        self.assertTrue(any(e.get("type") == "restore" for e in store.events()))

    def test_task_state_defaults(self):
        """TaskState 的默认字段。"""
        state = TaskState(task_id="t1")
        self.assertEqual(state.status, "running")
        self.assertEqual(state.data, {})
        self.assertEqual(state.steps, [])
        self.assertEqual(state.version, 0)

    def test_checkpoint_dataclass_shape(self):
        """Checkpoint 的字段。"""
        cp = Checkpoint(task_id="t1", version=3, snapshot={"a": 1}, created_at=1.0)
        self.assertEqual((cp.task_id, cp.version, cp.snapshot, cp.created_at),
                         ("t1", 3, {"a": 1}, 1.0))

    def test_store_accepts_a_config(self):
        """构造器接受配置对象（为将来的持久化留位置）。"""
        store = StateStore(config=object(), task_id="t1")
        self.assertEqual(store.snapshot().task_id, "t1")

    def test_store_generates_a_task_id_when_not_given(self):
        store = StateStore()
        self.assertTrue(store.snapshot().task_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
