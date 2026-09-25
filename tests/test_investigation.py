"""调查数据层：展示排序等纯函数的固定测试集（不需要窗口、网络或真实 Blender 仓库）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugcompass.investigation import (  # noqa: E402
    empty_investigation,
    flow_step,
    merge_user_decisions,
    order_hypotheses,
    read_investigation,
    record_conclusion,
    write_investigation,
)
from bugcompass.workspace import BugCompassError  # noqa: E402


def _hypothesis(hypothesis_id: str, priority: str, status: str = "active") -> dict:
    return {
        "id": hypothesis_id,
        "title": hypothesis_id,
        "priority": priority,
        "status": status,
        "claim": "",
        "basis": [],
        "evidence_ids": [],
        "source_references": [],
        "next_step": "",
    }


class OrderHypothesesTests(unittest.TestCase):
    """三条路径必须按 高→中→低 排列、已否定沉底——界面编号 ①②③ 直接依赖这个顺序。"""

    def test_sorted_by_priority(self) -> None:
        items = [_hypothesis("c", "low"), _hypothesis("a", "high"), _hypothesis("b", "medium")]
        self.assertEqual([item["id"] for item in order_hypotheses(items)], ["a", "b", "c"])

    def test_rejected_sinks_even_when_high(self) -> None:
        items = [_hypothesis("a", "high", "rejected"), _hypothesis("b", "low")]
        self.assertEqual([item["id"] for item in order_hypotheses(items)], ["b", "a"])

    def test_only_rejected_sinks(self) -> None:
        items = [_hypothesis("weakened", "low", "weakened"), _hypothesis("supported", "high", "supported")]
        self.assertEqual([item["id"] for item in order_hypotheses(items)], ["supported", "weakened"])

    def test_stable_within_same_priority(self) -> None:
        items = [_hypothesis("first", "high"), _hypothesis("second", "high"), _hypothesis("third", "high")]
        self.assertEqual([item["id"] for item in order_hypotheses(items)], ["first", "second", "third"])

    def test_unknown_priority_goes_last(self) -> None:
        items = [_hypothesis("mystery", "unknown"), _hypothesis("known", "low")]
        self.assertEqual([item["id"] for item in order_hypotheses(items)], ["known", "mystery"])

    def test_missing_priority_goes_last(self) -> None:
        items = [{"id": "no-priority"}, _hypothesis("known", "low")]
        self.assertEqual([item["id"] for item in order_hypotheses(items)], ["known", "no-priority"])

    def test_empty_list(self) -> None:
        self.assertEqual(order_hypotheses([]), [])

    def test_original_order_is_not_mutated(self) -> None:
        items = [_hypothesis("c", "low"), _hypothesis("a", "high")]
        order_hypotheses(items)
        self.assertEqual([item["id"] for item in items], ["c", "a"])


class FlowStepTests(unittest.TestCase):
    """流程条只前进不后退：三条路径一旦生成，深入调查也不会把用户推回「调查中」。"""

    def test_new_case_starts_at_intake(self) -> None:
        self.assertEqual(flow_step(empty_investigation("c1")), "intake")
        # 新建案件（status="new"）即使还没有路径，也停在第一步，不能显示成「调查中」。
        self.assertEqual(flow_step(empty_investigation("c1"), status="new"), "intake")

    def test_investigating_before_any_path(self) -> None:
        data = empty_investigation("c1")
        self.assertEqual(flow_step(data, status="investigating"), "investigating")

    def test_paths_once_hypotheses_exist(self) -> None:
        data = empty_investigation("c1")
        data["hypotheses"] = [_hypothesis("a", "high")]
        self.assertEqual(flow_step(data, status="investigating"), "paths")

    def test_experiment_after_a_result_exists(self) -> None:
        data = empty_investigation("c1")
        data["hypotheses"] = [_hypothesis("a", "high")]
        data["suggested_experiments"] = [{"id": "X1", "hypothesis_id": "a", "result": "返回码 0"}]
        self.assertEqual(flow_step(data), "experiment")

    def test_conclusion_wins_over_experiment(self) -> None:
        data = empty_investigation("c1")
        data["hypotheses"] = [_hypothesis("a", "high")]
        data["suggested_experiments"] = [{"id": "X1", "hypothesis_id": "a", "result": "返回码 0"}]
        data["conclusion"] = {"hypothesis_id": "a", "statement": "就是这里", "recorded_at": "now"}
        self.assertEqual(flow_step(data), "conclusion")

    def test_failed_case_falls_back_to_intake(self) -> None:
        data = empty_investigation("c1")
        self.assertEqual(flow_step(data, status="failed"), "intake")

    def test_blank_conclusion_does_not_count(self) -> None:
        data = empty_investigation("c1")
        data["hypotheses"] = [_hypothesis("a", "high")]
        data["conclusion"] = {"hypothesis_id": "a", "statement": "   ", "recorded_at": "now"}
        self.assertEqual(flow_step(data), "paths")


class ConclusionTests(unittest.TestCase):
    """收口：结论是用户自己的判断，必须经得起 AI 回写和重新读盘。"""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "investigation.json"
        data = empty_investigation("c1")
        data["hypotheses"] = [_hypothesis("a", "high"), _hypothesis("b", "low")]
        write_investigation(self.path, data)

    def test_records_and_exports(self) -> None:
        record_conclusion(self.path, "a", "  属性缺失时没有做检查  ")
        conclusion = read_investigation(self.path)["conclusion"]
        self.assertEqual(conclusion["hypothesis_id"], "a")
        self.assertEqual(conclusion["statement"], "属性缺失时没有做检查")
        self.assertTrue(conclusion["recorded_at"])
        markdown = (self.path.parent / "hypotheses.md").read_text(encoding="utf-8")
        self.assertIn("## 结论", markdown)
        self.assertIn("属性缺失时没有做检查", markdown)

    def test_rejects_unknown_path(self) -> None:
        with self.assertRaises(BugCompassError):
            record_conclusion(self.path, "nope", "随便写一句")

    def test_rejects_blank_statement(self) -> None:
        with self.assertRaises(BugCompassError):
            record_conclusion(self.path, "a", "   ")

    def test_ai_merge_keeps_conclusion(self) -> None:
        record_conclusion(self.path, "a", "属性缺失时没有做检查")
        previous = read_investigation(self.path)
        # AI 重写的候选结果里没有 conclusion 字段，合并后不能把它弄丢。
        merged = merge_user_decisions(previous, empty_investigation("c1"))
        self.assertEqual(merged["conclusion"]["statement"], "属性缺失时没有做检查")

    def test_validation_rejects_dangling_conclusion(self) -> None:
        data = read_investigation(self.path)
        data["conclusion"] = {"hypothesis_id": "ghost", "statement": "x", "recorded_at": ""}
        with self.assertRaises(BugCompassError):
            write_investigation(self.path, data)

    def test_old_case_without_conclusion_still_reads(self) -> None:
        data = read_investigation(self.path)
        data.pop("conclusion", None)
        write_investigation(self.path, data)
        self.assertIsNone(read_investigation(self.path)["conclusion"])


if __name__ == "__main__":
    unittest.main()
